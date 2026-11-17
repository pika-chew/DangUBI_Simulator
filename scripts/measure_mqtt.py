"""일회성 로컬 mTLS 측정. AWS/실행 DB/전체 메시지 저장을 사용하지 않는다."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local"


def validate_config(config):
    if not isinstance(config, dict):
        raise ValueError("측정 설정은 JSON 객체여야 합니다.")
    if set(config) - {"cases", "repeats"}:
        raise ValueError("알 수 없는 측정 설정입니다.")
    repeats = config.get("repeats", 1)
    if type(repeats) is not int or not 1 <= repeats <= 3:
        raise ValueError("반복 수는1~3입니다.")
    cases = config.get("cases", [])
    if not isinstance(cases, list) or not 1 <= len(cases) <= 12:
        raise ValueError("측정 조건은1~12개입니다.")
    total = 0
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("각 측정 조건은 JSON 객체여야 합니다.")
        if set(case) - {"name", "vehicles", "clients", "mode", "duration_seconds", "interval_ms", "lifetime", "fault"}:
            raise ValueError("알 수 없는 조건 설정입니다.")
        if not isinstance(case.get("name"), str) or not 1 <= len(case["name"]) <= 60 or any(c in case["name"] for c in "|\r\n"):
            raise ValueError("조건 이름은1~60자입니다.")
        for key, low, high in [("vehicles", 1, 100), ("clients", 1, 100), ("duration_seconds", 10, 60), ("interval_ms", 100, 6000)]:
            if type(case.get(key)) is not int or not low <= case[key] <= high:
                raise ValueError(f"{key} 범위는{low}~{high}입니다.")
        duration, interval = case["duration_seconds"] * 1000, case["interval_ms"]
        if duration % interval or duration // interval < 10 or (duration // interval + 2) * case["vehicles"] > 5000:
            raise ValueError("주기는 운행시간을 나누고10구간 이상, 총5000건 이하여야 합니다.")
        mode = case.get("mode")
        if mode not in ("shared", "dedicated") or case["clients"] > case["vehicles"] or (mode == "dedicated" and case["clients"] != case["vehicles"]):
            raise ValueError("연결 배정 설정을 확인하세요.")
        if case.get("lifetime", "always") not in ("always", "while_driving") or case.get("fault", "none") not in ("none", "disconnect_once", "delay_ack", "no_ack", "stop_wait"):
            raise ValueError("연결 유지·장애 설정을 확인하세요.")
        total += case["duration_seconds"] + case["clients"] / 100 + 5
    if total * repeats > 900:
        raise ValueError("측정 계획은 기본 예상15분 이내로 제한합니다.")
    return cases, repeats


class ProcessResources:
    """Worker만 측정. CPU100%=코어1개, RSS는250ms 관측 최댓값."""
    def __init__(self, proc):
        self.proc = proc
        self.cpu = self.rss = self.samples = 0
        self.available = False
        if os.name == "nt":
            from ctypes import wintypes
            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self.kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
            self.kernel.GetProcessTimes.restype = wintypes.BOOL
            class Counters(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [(name, ctypes.c_size_t) for name in ("peak_ws", "ws", "peak_paged", "paged", "peak_nonpaged", "nonpaged", "pagefile", "peak_pagefile")]
            self.Counters = Counters
            self.kernel.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
            self.kernel.K32GetProcessMemoryInfo.restype = wintypes.BOOL
            self.handle = wintypes.HANDLE(int(proc._handle))

    def sample(self):
        try:
            if os.name == "nt":
                from ctypes import wintypes
                times = [wintypes.FILETIME() for _ in range(4)]
                if self.kernel.GetProcessTimes(self.handle, *[ctypes.byref(t) for t in times]):
                    self.cpu = sum((t.dwHighDateTime << 32) + t.dwLowDateTime for t in times[2:]) / 10_000_000
                    self.available = True
                counters = self.Counters()
                counters.cb = ctypes.sizeof(counters)
                if self.kernel.K32GetProcessMemoryInfo(self.handle, ctypes.byref(counters), counters.cb):
                    self.rss = max(self.rss, counters.ws)
                    self.samples += 1
            elif platform.system() == "Linux":
                stat = Path(f"/proc/{self.proc.pid}/stat").read_text().rsplit(")", 1)[1].split()
                self.cpu = (int(stat[11]) + int(stat[12])) / os.sysconf("SC_CLK_TCK")
                pages = int(Path(f"/proc/{self.proc.pid}/statm").read_text().split()[1])
                self.rss = max(self.rss, pages * os.sysconf("SC_PAGE_SIZE"))
                self.samples += 1
                self.available = True
        except (OSError, ValueError, IndexError):
            pass

    def result(self, elapsed):
        return {"available": self.available, "cpu_seconds": self.cpu if self.available else None,
                "cpu_one_core_percent": 100 * self.cpu / elapsed if self.available and elapsed else None,
                "observed_peak_rss_bytes": self.rss if self.samples else None, "memory_samples": self.samples,
                "sample_interval_ms": 250}


def measure(case, fixture, worker):
    with tempfile.TemporaryDirectory(prefix="mqtt-measure-", dir=LOCAL) as temporary:
        profile_path = Path(temporary) / "profile.json"
        env = os.environ.copy()
        env.update(DANGUBI_MEASURE_PROFILE=str(profile_path), DANGUBI_MEASURE_FAULT=case.get("fault", "none"))
        with tempfile.TemporaryFile() as broker_log, tempfile.TemporaryFile() as worker_log:
            broker = subprocess.Popen([str(fixture), "-test.run=^TestMQTTMeasurementFixture$", "-test.v"], env=env, stdout=broker_log, stderr=broker_log)
            process = None
            try:
                deadline = time.monotonic() + 10
                while not profile_path.exists():
                    if broker.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError("측정 브로커 준비 실패")
                    time.sleep(.05)
                # 브로커는 자기 루프백 주소와 일회성 인증서만 제공한다.
                profile = json.loads(profile_path.read_text(encoding="utf-8"))
                body = {"operation": "mqtt", "run_id": "measure-" + uuid.uuid4().hex,
                        "config": {"vehicle_count": case["vehicles"], "client_count": case["clients"], "mapping_mode": case["mode"], "messages_per_vehicle": 1, "interval_ms": case["interval_ms"], "connection_lifetime": case.get("lifetime", "always")},
                        "generation": {"model_version": "combustion-events-6", "engine_start": "warm", "weather": "dry", "anomaly_intensity": "medium", "profile": "mixed", "seed": 42, "start_time_ms": 1794783600000, "duration_seconds": case["duration_seconds"], "start_lat": 37.5, "start_lon": 127., "initial_odometer_m": 500000., "anomaly_vehicle_percent": 5, "anomaly_events_per_hour": 2}, "mqtt": profile}
                worker_env = os.environ.copy()
                worker_env["DANGUBI_MANAGED"] = "1"
                process = subprocess.Popen([str(worker)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=worker_log, env=worker_env)
                latest = {}
                def consume():
                    for line in process.stdout:
                        try:
                            data = json.loads(line)
                            if data.get("kind") == "result":
                                latest["result"] = data["result"]
                            elif "error" in data:
                                latest["error"] = data["error"]
                        except (ValueError, KeyError):
                            latest["error"] = "Worker 응답 형식 오류"
                reader = threading.Thread(target=consume, daemon=True)
                reader.start()
                started = time.monotonic()
                resources = ProcessResources(process)
                process.stdin.write((json.dumps(body) + "\n").encode())
                process.stdin.flush()
                stop_sent = False
                while process.poll() is None:
                    resources.sample()
                    elapsed = time.monotonic() - started
                    if elapsed > 130:
                        raise RuntimeError("Worker 측정 제한시간 초과")
                    if case.get("fault") == "stop_wait" and elapsed >= .3 and not stop_sent:
                        process.stdin.write(b'{"command":"stop"}\n')
                        process.stdin.flush()
                        stop_sent = True
                    time.sleep(.25)
                resources.sample()
                elapsed = time.monotonic() - started
                process.stdin.close()
                reader.join(3)
                if reader.is_alive() or process.returncode or "result" not in latest:
                    raise RuntimeError(latest.get("error", "Worker 측정 결과 없음"))
                result = latest["result"]
                Path(str(profile_path) + ".done").touch()
                broker.wait(timeout=5)
                if broker.returncode:
                    raise RuntimeError("측정 브로커 종료 실패")
                observed = json.loads(Path(str(profile_path) + ".report").read_text(encoding="utf-8"))
                keys = ("state", "planned_messages", "processed_messages", "publish_attempts", "mqtt_published", "puback_count", "unconfirmed_messages", "remaining_messages", "unsent_messages", "reconnect_count", "connected_clients", "peak_active_clients", "active_clients", "elapsed_ms", "schedule_delay_ms", "performance")
                return {"case": case, "worker": {k: result.get(k) for k in keys}, "resources": resources.result(elapsed), "wall_seconds": elapsed, "local_broker": observed}
            finally:
                if process is not None and process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                if process is not None:
                    for stream in (process.stdin, process.stdout):
                        if stream and not stream.closed:
                            stream.close()
                if broker.poll() is None:
                    # 실패/중단에도 시험의 Cleanup이 일회성 인증서를 지우도록 정상 종료를 먼저 요청한다.
                    Path(str(profile_path) + ".done").touch()
                    try:
                        broker.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        broker.terminate()
                        broker.wait(timeout=5)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="로컬 MQTT 성능·장애 측정")
    parser.add_argument("--config", type=Path, default=ROOT / "configs/measurement.example.json")
    args = parser.parse_args()
    try:
        cases, repeats = validate_config(json.loads(args.config.read_text(encoding="utf-8-sig")))
    except (OSError, ValueError) as error:
        parser.error(str(error))
    LOCAL.mkdir(exist_ok=True)
    suffix = ".exe" if os.name == "nt" else ""
    worker = LOCAL / ("dangubi-worker" + suffix)
    fixture = LOCAL / ("mqtt-measure-fixture" + suffix)
    env = os.environ.copy()
    env.update(GOCACHE=str(LOCAL / "go-cache"), GOMODCACHE=str(LOCAL / "go-mod"))
    subprocess.run(["go", "build", "-o", str(worker), "."], cwd=ROOT / "services/worker", env=env, check=True)
    subprocess.run(["go", "test", "-c", "-o", str(fixture)], cwd=ROOT / "services/worker", env=env, check=True)
    report = {"schema_version": "mqtt-measure-1", "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "environment": {"os": platform.platform(), "logical_cpus": os.cpu_count(), "resource_scope": "Worker 프로세스만, 제어 API/브로커 제외", "cpu_basis": "100%=코어1개", "memory_basis": "250ms 관측 RSS 최댓값"}, "limitations": ["루프백 일회성 mTLS 시험 브로커이며 AWS/운영 브로커 성능이 아님", "현재 Worker는 전체 연결에서 발행/PUBACK을 순차 처리", "전량 메시지·접속 정보·인증서는 결과에 저장하지 않음", "단일 회차는 처리 용량이나 장시간 안정성을 보증하지 않음"], "results": []}
    directory = LOCAL / "measurements" / (time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])
    directory.mkdir(parents=True)
    for repeat in range(repeats):
        for case in cases:
            print(f"측정 {repeat+1}/{repeats}: {case['name']}", flush=True)
            try:
                row = measure(case, fixture, worker)
                row["repeat"] = repeat + 1
                report["results"].append(row)
            except Exception as error:
                report["results"].append({"case": case, "repeat": repeat + 1, "error": str(error)})
            (directory / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 로컬 MQTT 측정 결과", "", "CPU100%=코어1개. 메모리는250ms 간격의 Worker RSS 관측 최댓값. AWS 처리량 추정값이 아닙니다.", "", "| 조건 | 상태 | ACK/계획 | 최대 지연(ms) | ACK 평균(ms) | CPU(%) | RSS(MiB) |", "|---|---|---|---|---|---|---|"]
    for row in report["results"]:
        if "error" in row:
            lines.append(f"| {row['case']['name']} | 측정 오류 | — | — | — | — | — |")
            continue
        worker_result, resource = row["worker"], row["resources"]
        cpu, rss = resource["cpu_one_core_percent"], resource["observed_peak_rss_bytes"]
        cpu_text = f"{cpu:.2f}" if cpu is not None else "측정 불가"
        rss_text = f"{rss/1048576:.2f}" if rss is not None else "측정 불가"
        lines.append(f"| {row['case']['name']} · {row['repeat']}회차 | {worker_result['state']} | {worker_result['puback_count']}/{worker_result['planned_messages']} | {worker_result['schedule_delay_ms']} | {worker_result['performance']['ack']['mean_ms']:.3f} | {cpu_text} | {rss_text} |")
    lines += ["", "측정 범위: Worker 프로세스만 포함하며 제어 API/브로커는 제외합니다.", "", "모형: combustion-events-6 · seed42 · MQTT3.1.1/QoS1/mTLS · 초당100접속/2000발행 상한.", "", "응답없음의 mqtt_failed, 응답대기중단의 stopped는 의도한 장애 조건입니다. 정상 조건의 실패와 구분해 읽으세요.", "", "P95는 summary.json의 고정 버킷 상한입니다. 0.000ms는 연산 비용이 없다는 뜻이 아니며 시계·표시 해상도의 영향을 받을 수 있습니다.", ""]
    lines += ["- " + note for note in report["limitations"]]
    (directory / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"요약 저장: {directory}", flush=True)
    return int(any("error" in row for row in report["results"]))


if __name__ == "__main__":
    raise SystemExit(main())
