"""상시 운영(OperationController) 실제 규모 계측. 외부 전송 없음(송신 없는 생성).

A. 계획: N대 × D일의 날짜별 운행·운행 집중 계산 시간, 분 단위 최대 동시 운행과 필요한 초당 메시지 수, 운행 수.
B. 피크 실시간: 임시 명부에서 가상 시각을 지정 시각(예: 08:15)에 두고, 그 순간 진행 중인 운행은 비정상 종료 복구
   경로로 이어 보내 실제 1배속으로 M분 운영한다. 메시지별 일정 지연, 누락·중복, 메모리·CPU, 체크포인트 간격,
   상태 조회 시간을 잰다.

임시 폴더(.local 아래)의 별도 명부만 쓰고 끝나면 지운다. 사용자 서버·DB·AWS에 접속하지 않는다.
결과는 .local/measurements/persistent-*/ 에 JSON·요약으로 남긴다.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
LOCAL = ROOT / ".local"

from services.control.operation_plan import (FocusPlanner, OperationSettings, base_day_trips, focus_rng, trip_key,  # noqa: E402
                                             vehicle_key)
from services.control import operation_runtime  # noqa: E402
from services.control.operation_runtime import INITIAL_STATE, OperationController  # noqa: E402

POOLS = []  # 실행 중 생성 풀(Worker 캐시 적중·계획 재계산 통계 기록용)


class _TrackedPool(operation_runtime.GenerationPool):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        POOLS.append(self)


operation_runtime.GenerationPool = _TrackedPool
from services.control.registry import DevelopmentPseudonyms, Registry  # noqa: E402
from services.control.schedule import KST  # noqa: E402
from services.control.traffic_focus import adjust, midnight_ms  # noqa: E402

REGISTER = {"pattern_mix": {"commute": 60, "daytime": 20, "flexible": 20},
            "road_mix": {"urban": 50, "highway": 20, "congested": 10, "mixed": 20},
            "anomaly_vehicle_percent": 5, "trip_min_minutes": 3, "trip_max_minutes": 120}
HISTORY_BYTES_PER_TRIP = 451  # 2026-11-25 로컬 실측(200대·30일). 추정 환산에만 쓴다.


def settings_for(args, focus: bool) -> OperationSettings:
    window = None
    if focus:
        start, end = args.window.split("-")
        window = {"windows": [{"start": start, "end": end, "target_percent": args.focus_percent, "shape": "peak"}]}
    return OperationSettings(vehicle_count=args.vehicles, interval_ms=args.interval_ms, client_count=min(500, args.vehicles),
                             generation_workers=args.workers, traffic_focus=window, duration_cap_days=7)


def concurrency(plans, day: date):
    """분 단위(1440칸) 순간 동시 운행 대수."""
    base = midnight_ms(day)
    diff = [0] * 1442
    for trips in plans:
        for a, b, _ in trips:
            first = max(0, (a - base + 59999) // 60000)
            last = min(1440, (b - base + 59999) // 60000)
            if first < last:
                diff[first] += 1
                diff[last] -= 1
    out, running = [], 0
    for k in range(1440):
        running += diff[k]
        out.append(running)
    return out


def plan_part(args, reg: Registry) -> dict:
    infos = reg.session_vehicles()
    seed = reg.seed()
    focus = settings_for(args, True)
    planner = FocusPlanner(focus, seed, None)
    for v in infos:
        planner.add(v["id"], v["fixed"])
    first = date.fromisoformat(args.start_date)
    rows = []
    for n in range(args.days):
        day = first + timedelta(days=n)
        t0 = time.perf_counter()
        base = [base_day_trips(focus, seed, vehicle_key(v["id"]), v["fixed"], day) for v in infos]
        t1 = time.perf_counter()
        report = planner.report(day, save=False)
        t2 = time.perf_counter()
        ps = {int(k): p for k, p in report["ps"].items()}
        adjusted = [adjust(trips, focus.traffic_focus, day, ps, focus_rng(seed, vehicle_key(v["id"]), day)) for trips, v in zip(base, infos)]
        t3 = time.perf_counter()
        c0, c1 = concurrency(base, day), concurrency(adjusted, day)
        p0, p1 = max(c0), max(c1)
        rows.append({"date": day.isoformat(), "weekday": day.isoweekday(), "trips": sum(len(t) for t in base),
                     "vehicles_driving": sum(1 for t in base if t),
                     "base_plan_seconds": round(t1 - t0, 3), "focus_calibration_seconds": round(t2 - t1, 3), "focus_apply_seconds": round(t3 - t2, 3),
                     "peak_concurrent_baseline": p0, "peak_minute_baseline": f"{c0.index(p0) // 60:02d}:{c0.index(p0) % 60:02d}",
                     "peak_concurrent_focus": p1, "peak_minute_focus": f"{c1.index(p1) // 60:02d}:{c1.index(p1) % 60:02d}",
                     "focus_windows": [{k: w[k] for k in ("start", "end", "target_percent", "baseline_percent", "planned_percent", "maximum_percent", "within_tolerance")}
                                       for w in report["windows"]]})
        print(f"[A] {day} 운행 {rows[-1]['trips']:,} · 피크 기본 {p0:,}대 / 집중 {p1:,}대 · 계산 {t3 - t0:.1f}s", flush=True)
    per_second = 1000 / args.interval_ms
    trips = sum(r["trips"] for r in rows)
    return {"days": rows, "total_trips": trips, "history_rows_estimate": trips,
            "history_bytes_estimate": trips * HISTORY_BYTES_PER_TRIP,
            "peak_messages_per_second_baseline": round(max(r["peak_concurrent_baseline"] for r in rows) * per_second, 1),
            "peak_messages_per_second_focus": round(max(r["peak_concurrent_focus"] for r in rows) * per_second, 1),
            "day_plan_seconds_max": max(r["base_plan_seconds"] + r["focus_calibration_seconds"] + r["focus_apply_seconds"] for r in rows)}


class Sampler:
    """1초마다 제어기 상태 조회 시간, 생성 건수, 프로세스 RSS·CPU 합계를 기록한다."""

    def __init__(self, controller, interval=1.0):
        from scripts.measure_mqtt import ProcessResources
        self.ProcessResources = ProcessResources
        self.controller, self.interval = controller, interval
        handle = __import__("ctypes").windll.kernel32.GetCurrentProcess() if os.name == "nt" else 0
        self.own = SimpleNamespace(pid=os.getpid(), _handle=int(handle), poll=lambda: None)
        self.items = {}
        self.rows = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def sample(self, elapsed):
        processes = [("control", self.own)] + [("worker", p) for p in list(getattr(self.controller.job, "processes", []) or [])]
        for role, p in processes:
            if p.pid not in self.items:
                self.items[p.pid] = (role, self.ProcessResources(p), p)
        rss, cpu, by_role = 0, 0.0, {}
        for role, r, p in self.items.values():
            r.sample()
            cpu += r.cpu
            by_role[role] = round(by_role.get(role, 0.0) + r.cpu, 2)
            if p.poll() is None:
                rss += r.current_rss
        t = time.perf_counter()
        status = self.controller.status()
        status_ms = (time.perf_counter() - t) * 1000
        live = status["live"]
        result = live.get("result") or {}
        self.rows.append({"elapsed_s": round(elapsed, 1), "rss_bytes": rss, "cpu_seconds": round(cpu, 2), "cpu_by_role": by_role, "status_ms": round(status_ms, 1),
                          "processed": result.get("processed_messages", 0), "driving_now": live.get("driving_now"),
                          "virtual_now_ms": live.get("virtual_now_ms"), "focus": live.get("focus"),
                          "pool": {k: v for k, v in POOLS[-1].stats.items() if k in ("plan_builds", "cache_hits", "cache_evictions", "cache_entries", "page_requests", "batch_requests", "worker_compute_ms", "page_roundtrip_ms", "generation_task_ms")} if POOLS else None})

    def run(self):
        started = time.monotonic()
        while not self.stop.wait(self.interval):
            self.sample(time.monotonic() - started)


def arm(reg: Registry, settings: OperationSettings, anchor: int, worker_hash: str):
    """가상 anchor 시각의 운영 상태를 만든다: 진행 중 운행은 복구 대상, 나머지는 그날부터 운행.
    반환: 차량별 그날 계획(조정 포함)."""
    seed = reg.seed()
    day = datetime.fromtimestamp(anchor / 1000, KST).date()
    infos = reg.session_vehicles()
    planner = FocusPlanner(settings, seed, reg)
    for v in infos:
        planner.add(v["id"], v["fixed"])
    ps = planner.ps(day)  # 집중 결과를 명부에 고정 저장해 실행이 같은 값을 쓰게 한다
    plans, rows = {}, []
    stamp = datetime.now().isoformat()
    snapshot = settings.model_dump()
    for v in infos:
        key = vehicle_key(v["id"])
        base = base_day_trips(settings, seed, key, v["fixed"], day)
        trips = adjust(base, settings.traffic_focus, day, ps, focus_rng(seed, key, day)) if settings.traffic_focus else base
        plans[v["id"]] = trips
        current = next(((a, b, p) for a, b, p in trips if a < anchor < b), None)
        ended = [b for a, b, _ in trips if b <= anchor]
        trip = None
        if current:
            a, b, profile = current
            trip = json.dumps({"key": trip_key(key, a, b, profile), "a": a, "b": b, "profile": profile, "initial": dict(INITIAL_STATE),
                               "worker_hash": worker_hash, "alias": v["wire_identity"], "destination": None, "settings": snapshot})
        rows.append((v["id"], day.isoformat(), max(ended) if ended else None, None, trip, anchor if trip else None, stamp))
    with sqlite3.connect(reg.path) as con:
        con.executemany("INSERT OR REPLACE INTO vehicle_state(id, start_day, last_end_ms, final, trip, confirmed_ts, updated_at) VALUES (?,?,?,?,?,?,?)", rows)
        con.execute("UPDATE operation SET state='stopped', last_stop='abnormal', stop_reason='계측 준비', virtual_checkpoint_ms=?, window_end_ms=?, settings=?",
                    (anchor, anchor + 7 * 86400000, json.dumps(snapshot)))
    return plans, sum(1 for r in rows if r[4])


def expected_counts(plans: dict, anchor: int, cutoff: int, interval: int):
    """anchor 다음부터 cutoff까지 만들어져야 할 Microtrip·Trip 수(운행 번호별). Microtrip ts = 출발 + k×주기."""
    micro, trips = 0, 0
    for trips_of in plans.values():
        for a, b, profile in trips_of:
            if b <= anchor or a > cutoff:
                continue
            first = a if a >= anchor else a + ((anchor - a) // interval + 1) * interval
            last = min(b - interval, cutoff)
            n = 0 if last < first else (last - first) // interval + 1
            t = 1 if b <= cutoff else 0
            micro += n
            trips += t
    return micro, trips


def live_part(args, base_registry: Path, scenario: str, out: Path) -> dict:
    from services.control.app import validate_message
    folder = Path(tempfile.mkdtemp(prefix=f"ops-{scenario}-", dir=LOCAL))
    try:
        path = folder / "registry.sqlite3"
        with sqlite3.connect(base_registry) as src, sqlite3.connect(path) as dst:
            src.backup(dst)
        reg = Registry(path, DevelopmentPseudonyms(), synthetic_raw_vin=args.vin_mode == "synthetic_raw")
        settings = settings_for(args, scenario == "focus")
        worker = Path(args.worker)
        worker_hash = hashlib.sha256(worker.read_bytes()).hexdigest()
        day = date.fromisoformat(args.start_date)
        hour, minute = map(int, args.start_time.split(":"))
        anchor = midnight_ms(day) + (hour * 60 + minute) * 60000
        t = time.perf_counter()
        plans, in_progress = arm(reg, settings, anchor, worker_hash)
        prepare_s = time.perf_counter() - t
        cutoff = anchor + args.minutes * 60000 - 30000  # 마지막 30초는 생성 중일 수 있어 비교에서 뺀다
        expect_micro, expect_trip = expected_counts(plans, anchor, cutoff, args.interval_ms)

        state = {"t0": None, "lag_max": 0.0, "count": 0, "micro": 0, "trip": 0, "late_over_5s": 0, "seen": {}, "dup": 0,
                 "lag_hist": [0] * 7201, "by_window": {}}  # 지연: 1초 칸, 2시간까지
        lock = threading.Lock()

        def validate(message):
            encoded = validate_message(message)
            now = time.monotonic()
            with lock:
                if state["t0"] is not None:
                    lag = (now - state["t0"]) * 1000 - (message["ts"] - anchor)
                    state["lag_max"] = max(state["lag_max"], lag)
                    state["lag_hist"][min(7200, max(0, int(lag // 1000)))] += 1
                    if lag > 5000:
                        state["late_over_5s"] += 1
                    window = int((now - state["t0"]) // 60)
                    bucket = state["by_window"].setdefault(window, [0, 0.0])
                    bucket[0] += 1
                    bucket[1] = max(bucket[1], lag)
                state["count"] += 1
                if message["ts"] <= cutoff:
                    # 운행별로 시각이 늘어나야 한다. 같거나 이전 시각이면 중복(메시지 전체를 보관하지 않음).
                    tid, ts, ty = message["pld"]["tid"], message["ts"], message["ty"]
                    last = state["seen"].get(tid)
                    if last is not None and (ts, ty) <= last:
                        state["dup"] += 1
                    state["seen"][tid] = (ts, ty)
                    state["micro" if ty == 4 else "trip"] += 1
            return encoded

        checkpoints = []
        original = reg.save_checkpoint

        def timed_checkpoint(*a, **k):
            t = time.perf_counter()
            original(*a, **k)
            checkpoints.append((time.monotonic(), (time.perf_counter() - t) * 1000))
        reg.save_checkpoint = timed_checkpoint

        ctrl = OperationController(reg, worker, validate, clock=lambda: anchor / 1000)
        ctrl.start(settings)
        while not ctrl.live.get("virtual_anchor_ms"):
            time.sleep(.001)
        state["t0"] = time.monotonic()
        sampler = Sampler(ctrl)
        sampler.thread.start()
        started_wall = time.time()
        deadline = state["t0"] + args.minutes * 60
        error = None
        while time.monotonic() < deadline:
            if not ctrl.running():
                error = ctrl.registry.operation().get("error") or "운영이 예상보다 먼저 멈췄습니다."
                break
            time.sleep(1)
        sampler.stop.set()
        sampler.thread.join()
        ctrl.stop(force=True)
        ctrl.wait(60)
        elapsed = time.time() - started_wall
        seen = state.pop("seen")
        hist = state.pop("lag_hist")
        total = sum(hist)

        def percentile(q):
            target, acc = total * q, 0
            for i, n in enumerate(hist):
                acc += n
                if acc >= target:
                    return i * 1000
            return None
        gaps = [b[0] - a[0] for a, b in zip(checkpoints, checkpoints[1:])]
        rss = [r["rss_bytes"] for r in sampler.rows if r["rss_bytes"]]
        steady = [r for r in sampler.rows if r["elapsed_s"] >= 60]
        status_ms = sorted(r["status_ms"] for r in sampler.rows)
        windows = [{"minute": k, "messages": v[0], "max_lag_ms": round(v[1])} for k, v in sorted(state.pop("by_window").items())]
        focus_rows = [r["focus"] for r in sampler.rows if r.get("focus")]
        result = {
            "scenario": scenario, "vehicles": args.vehicles, "interval_ms": args.interval_ms, "generation_workers": args.workers, "vin_mode": args.vin_mode,
            "virtual_start": datetime.fromtimestamp(anchor / 1000, KST).isoformat(), "minutes": args.minutes, "elapsed_s": round(elapsed, 1),
            "prepare_s": round(prepare_s, 1), "in_progress_trips_at_start": in_progress, "stopped_early": error,
            "messages_generated": state["count"], "messages_per_second_avg": round(state["count"] / max(1, elapsed), 1),
            "messages_per_second_peak_minute": round(max((w["messages"] for w in windows), default=0) / 60, 1),
            "lag_ms": {"max": round(state["lag_max"]), "p50": percentile(.5), "p95": percentile(.95), "p99": percentile(.99),
                       "over_5s_messages": state["late_over_5s"]},
            "lag_by_minute": windows,
            "completeness": {"cutoff": datetime.fromtimestamp(cutoff / 1000, KST).isoformat(), "expected_microtrips": expect_micro,
                             "generated_microtrips": state["micro"], "expected_trips": expect_trip, "generated_trips": state["trip"],
                             "duplicates": state["dup"], "trips_seen": len(seen)},
            "memory": {"rss_peak_bytes": max(rss) if rss else None, "rss_first_minute_peak_bytes": max((r["rss_bytes"] for r in sampler.rows if r["elapsed_s"] < 60), default=None),
                       "rss_last_minute_peak_bytes": max((r["rss_bytes"] for r in sampler.rows if r["elapsed_s"] >= args.minutes * 60 - 60), default=None),
                       "scope": "제어기(이 계측 프로세스 포함)+생성 Worker RSS 합계, 1초 표본"},
            "cpu_seconds_total": sampler.rows[-1]["cpu_seconds"] if sampler.rows else None,
            "cpu_one_core_percent_steady": round(100 * (steady[-1]["cpu_seconds"] - steady[0]["cpu_seconds"]) / max(1, steady[-1]["elapsed_s"] - steady[0]["elapsed_s"]), 1) if len(steady) > 1 else None,
            "checkpoint": {"count": len(checkpoints), "interval_max_s": round(max(gaps), 2) if gaps else None,
                           "interval_avg_s": round(sum(gaps) / len(gaps), 2) if gaps else None,
                           "save_ms_max": round(max(c[1] for c in checkpoints), 1) if checkpoints else None},
            "status_query_ms": {"p50": status_ms[len(status_ms) // 2] if status_ms else None, "max": status_ms[-1] if status_ms else None},
            "driving_now_max": max((r["driving_now"] or 0 for r in sampler.rows), default=0),
            "focus_live_samples": focus_rows[:: max(1, len(focus_rows) // 20)] if focus_rows else [],
        }
        (out / f"live-{scenario}-samples.json").write_text(json.dumps(sampler.rows, ensure_ascii=False), encoding="utf-8")
        return result
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def summary_md(report) -> str:
    lines = ["# 상시 운영 실제 규모 계측", "", f"- 환경: {report['environment']['os']} · 논리 CPU {report['environment']['logical_cpus']}",
             f"- 차량 {report['args']['vehicles']:,}대 · 주기 {report['args']['interval_ms'] / 1000:g}초 · 집중 {report['args']['window']} 목표 {report['args']['focus_percent']}%", ""]
    if report.get("plan"):
        p = report["plan"]
        lines += ["## A. 계획", "", f"- 운행 {p['total_trips']:,}회 / 이력 약 {p['history_bytes_estimate'] / 1e6:,.0f}MB(추정)",
                  f"- 피크 필요 처리량: 기본 {p['peak_messages_per_second_baseline']:,}건/초 · 집중 {p['peak_messages_per_second_focus']:,}건/초",
                  f"- 하루 계획 계산 최대 {p['day_plan_seconds_max']:.1f}초", ""]
    for r in report.get("live", []):
        c = r["completeness"]
        lines += [f"## B. 피크 실시간 — {r['scenario']}", "",
                  f"- 가상 {r['virtual_start'][11:16]}부터 {r['minutes']}분 · 시작 때 진행 중 운행 {r['in_progress_trips_at_start']:,}건 · 준비 {r['prepare_s']}초",
                  f"- 생성 {r['messages_generated']:,}건 · 평균 {r['messages_per_second_avg']:,}건/초 · 가장 많은 1분 {r['messages_per_second_peak_minute']:,}건/초 · 최대 동시 운행 {r['driving_now_max']:,}대",
                  f"- 일정 지연: 최대 {r['lag_ms']['max']:,}ms · p50 {r['lag_ms']['p50']}ms · p99 {r['lag_ms']['p99']}ms · 5초 초과 {r['lag_ms']['over_5s_messages']:,}건",
                  f"- 누락 확인(~{c['cutoff'][11:19]}): Microtrip {c['generated_microtrips']:,}/{c['expected_microtrips']:,} · Trip {c['generated_trips']:,}/{c['expected_trips']:,} · 중복 {c['duplicates']}",
                  f"- 메모리 최대 {(r['memory']['rss_peak_bytes'] or 0) / 1048576:,.0f}MiB (첫 1분 {(r['memory']['rss_first_minute_peak_bytes'] or 0) / 1048576:,.0f} → 마지막 1분 {(r['memory']['rss_last_minute_peak_bytes'] or 0) / 1048576:,.0f}) · CPU {r['cpu_one_core_percent_steady']}%(코어 1개=100%)",
                  f"- 체크포인트 간격 최대 {r['checkpoint']['interval_max_s']}초 · 저장 최대 {r['checkpoint']['save_ms_max']}ms · 상태 조회 p50 {r['status_query_ms']['p50']}ms / 최대 {r['status_query_ms']['max']}ms",
                  f"- 중간 멈춤: {r['stopped_early'] or '없음'}", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vehicles", type=int, default=20000)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--start-date", default="2026-12-01")
    parser.add_argument("--interval-ms", type=int, default=2000)
    parser.add_argument("--window", default="08:20-08:50")
    parser.add_argument("--focus-percent", type=int, default=50)
    parser.add_argument("--start-time", default="08:15")
    parser.add_argument("--minutes", type=int, default=36)
    parser.add_argument("--workers", type=int, default=1, choices=[1, 2, 4])
    parser.add_argument("--vin-mode", default="pseudonym", choices=["pseudonym", "synthetic_raw"], help="상시 운영 송신 VIN 모드")
    parser.add_argument("--scenarios", default="baseline,focus")
    parser.add_argument("--skip-plan", action="store_true")
    parser.add_argument("--skip-live", action="store_true")
    parser.add_argument("--worker", default=str(LOCAL / ("worker.exe" if os.name == "nt" else "worker")))
    args = parser.parse_args()
    if not 1 <= args.vehicles <= 20000 or not 1 <= args.days <= 30 or not 1 <= args.minutes <= 120:
        raise SystemExit("차량 1~20,000대, 1~30일, 1~120분입니다.")
    out = LOCAL / "measurements" / ("persistent-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    out.mkdir(parents=True)
    report = {"schema_version": "persistent-ops-measure-1", "args": vars(args),
              "environment": {"os": platform.platform(), "logical_cpus": os.cpu_count(), "python": sys.version.split()[0],
                              "worker_sha256": hashlib.sha256(Path(args.worker).read_bytes()).hexdigest()},
              "plan": None, "live": []}
    base = Path(tempfile.mkdtemp(prefix="ops-registry-", dir=LOCAL))
    try:
        t = time.perf_counter()
        reg = Registry(base / "registry.sqlite3", DevelopmentPseudonyms(), seed=20261201)
        reg.register(args.vehicles, REGISTER)
        report["register_seconds"] = round(time.perf_counter() - t, 1)
        print(f"명부 {args.vehicles:,}대 등록 {report['register_seconds']}초", flush=True)
        if not args.skip_plan:
            report["plan"] = plan_part(args, reg)
            (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        if not args.skip_live:
            for scenario in [s for s in args.scenarios.split(",") if s]:
                print(f"[B] {scenario} 시작 · {args.minutes}분", flush=True)
                report["live"].append(live_part(args, reg.path, scenario, out))
                (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
                print(json.dumps({k: report["live"][-1][k] for k in ("lag_ms", "completeness", "messages_per_second_avg", "stopped_early")}, ensure_ascii=False), flush=True)
    finally:
        shutil.rmtree(base, ignore_errors=True)
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "summary.md").write_text(summary_md(report), encoding="utf-8")
    print(f"결과: {out}", flush=True)


if __name__ == "__main__":
    main()
