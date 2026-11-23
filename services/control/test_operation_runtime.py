"""상시 운영 실행 시험. 실제 Worker 생성 세션과 일회성 로컬 mTLS 브로커만 사용한다(AWS 미접속)."""
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time

import pytest

from services.control.generation_session import GenerationSession
from services.control.operation_plan import OperationSettings, day_of, trip_row, upcoming, vehicle_key, MIN_PARKING_MS
from services.control.operation_runtime import INITIAL_STATE, OperationController, OperationGenerator, with_vehicle_id
from services.control.message_codec import prepare_message
from services.control.registry import Registry, DevelopmentPseudonyms
from services.control.schedule import KST

ROOT = Path(__file__).resolve().parents[2]
WORKER = Path(os.environ.get("DANGUBI_TEST_WORKER") or os.environ.get("DANGUBI_WORKER") or ROOT / ".local" / ("worker.exe" if os.name == "nt" else "worker"))
SHORT = {"pattern_mix": {"commute": 60, "daytime": 20, "flexible": 20},
         "road_mix": {"urban": 50, "highway": 20, "congested": 10, "mixed": 20},
         "anomaly_vehicle_percent": 0, "trip_min_minutes": 2, "trip_max_minutes": 2}
SETTINGS = OperationSettings(interval_ms=1000, client_count=1)
DAY = date(2026, 11, 24)


def validate(message):
    # 제품 검사(app.validate_message)와 같은 본문 준비. 스키마 검사는 test_app에서 다룬다.
    return prepare_message(message)


def registry(tmp_path, count=1):
    reg = Registry(tmp_path / "registry.sqlite3", DevelopmentPseudonyms(b"k" * 32), seed=7)
    reg.register(count, SHORT)
    for info in reg.session_vehicles():
        reg.ensure_start_day(info["id"], DAY.isoformat())
    return reg


def first_trip(reg, vehicle=1):
    info = next(v for v in reg.session_vehicles() if v["id"] == vehicle)
    return info, next(upcoming(SETTINGS, reg.seed(), vehicle, info["fixed"], DAY, 0, None))


def test_plan_rules_next_day_resume_and_parking(tmp_path):
    reg = registry(tmp_path)
    info, _ = first_trip(reg)
    seed = reg.seed()
    rows = []
    for row in upcoming(SETTINGS, seed, 1, info["fixed"], DAY, 0, None):
        rows.append(row)
        if len(rows) == 20:
            break
    assert day_of(rows[0]["start_ms"]) >= DAY  # 시작 날짜 이전 운행 없음(다음 날부터)
    assert all(b["start_ms"] - a["end_ms"] >= MIN_PARKING_MS for a, b in zip(rows, rows[1:]))
    again = [r for r, _ in zip(upcoming(SETTINGS, seed, 1, info["fixed"], DAY, 0, None), range(20))]
    assert [r["trip_key"] for r in again] == [r["trip_key"] for r in rows]  # 결정적
    # 재개: 기준 시각이 운행 중간이면 그 운행은 건너뛰고(R3) 다음 운행부터(R1·R2).
    middle = (rows[3]["start_ms"] + rows[3]["end_ms"]) // 2
    resumed = next(upcoming(SETTINGS, seed, 1, info["fixed"], DAY, middle, rows[2]["end_ms"]))
    assert resumed["trip_key"] == rows[4]["trip_key"]
    assert vehicle_key(1) == "op000001" and rows[0]["vehicle_id"] == "op000001"


def collect(generator, vehicle, key):
    session = GenerationSession(WORKER)
    try:
        out, offset = [], 0
        while True:
            page = generator.generate(vehicle, key, offset, session=session, limit=50)
            out += page["messages"]
            offset += len(page["messages"])
            if not page["has_more"]:
                return out, page["trip"]
    finally:
        session.close()


@pytest.mark.skipif(not WORKER.is_file(), reason="Worker 필요")
def test_regeneration_from_confirmed_point_is_identical(tmp_path):
    reg = registry(tmp_path)
    info, row = first_trip(reg)
    full_gen = OperationGenerator(reg, WORKER, SETTINGS, reg.seed())
    full_gen.add(row["vehicle_id"], info)
    full_gen.remember(row)
    messages, trip = collect(full_gen, row["vehicle_id"], row["trip_key"])
    assert len(messages) == (row["end_ms"] - row["start_ms"]) // 1000 and trip["ty"] == 3
    # 재시작한 프로세스처럼 새 생성기로 17번째 메시지까지 확인된 상태에서 다시 만든다.
    confirmed = messages[16]["ts"]
    recovered = trip_row(row["vehicle_id"], row["start_ms"], row["end_ms"], row["road_profile"], 1000, resume_after_ms=confirmed)
    recovered.update(recovered=True, initial=dict(INITIAL_STATE))
    again = OperationGenerator(reg, WORKER, SETTINGS, reg.seed())
    again.add(row["vehicle_id"], info)
    again.remember(recovered)
    rest, trip_again = collect(again, row["vehicle_id"], row["trip_key"])
    assert rest == messages[17:]
    assert trip_again == trip


def test_vehicle_id_envelope_is_first_and_keeps_body():
    wire = with_vehicle_id("PS64C36B1HDA81N43", prepare_message({"ty": 4, "ts": 5, "pld": {"tid": 1}}))
    assert wire.text == '{"vehicleId":"PS64C36B1HDA81N43","ty":4,"ts":5,"pld":{"tid":1}}'
    assert wire.size == len(wire.text.encode()) and wire["ts"] == 5


def controller(reg, clock):
    return OperationController(reg, WORKER, validate, clock=clock)


def arm_recovery(reg, row, remaining_ms=5000):
    """비정상 종료 직후 상태: 운행 시작 기록과 마지막 확인 시각을 둔다."""
    trip = {"key": row["trip_key"], "a": row["start_ms"], "b": row["end_ms"], "profile": row["road_profile"], "initial": dict(INITIAL_STATE)}
    reg.save_trip_start(row_id(row), trip)
    reg.save_confirmed({row_id(row): row["end_ms"] - remaining_ms})
    reg.update_operation(last_stop="abnormal", virtual_checkpoint_ms=row["end_ms"] - remaining_ms)


def row_id(row):
    return int(row["vehicle_id"][2:])


def wait_for(predicate, timeout=30):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if predicate():
            return True
        time.sleep(.1)
    return False


def state(reg, vehicle=1):
    with sqlite3.connect(reg.path) as con:
        return con.execute("SELECT trip, confirmed_ts, last_end_ms FROM vehicle_state WHERE id=?", (vehicle,)).fetchone()


@pytest.mark.skipif(not WORKER.is_file(), reason="Worker 필요")
def test_recovery_then_graceful_stop_completes_trip(tmp_path):
    reg = registry(tmp_path)
    _, row = first_trip(reg)
    arm_recovery(reg, row)
    ops = controller(reg, clock=lambda: (row["end_ms"] - 5000) / 1000)
    ops.start(SETTINGS)
    assert wait_for(lambda: state(reg)[0] is None)  # Trip 확인 → 운행 종료 기록
    ops.stop()
    assert ops.wait(20)
    trip, confirmed, last_end = state(reg)
    assert trip is None and confirmed is None and last_end == row["end_ms"]
    live = ops.live["result"]
    assert live["recovered_trips"] == 1 and live["trip_count"] == 1
    assert live["microtrip_count"] == 4  # 확인된 이후(b-4s..b-1s)만 다시 보냈다.
    sample = json.loads(live["samples"][0]["message"])
    assert list(sample)[0] == "vehicleId" and sample["vehicleId"] == reg.vehicles()["vehicles"][0]["pseudonym"]
    op = reg.operation()
    assert op["state"] == "stopped" and op["last_stop"] == "graceful"
    assert reg.summary()["status"]["ready"]["count"] == 1
    kinds = [e["kind"] for e in reg.events()]
    assert "recover_trip" in kinds and "graceful_stop" in kinds


@pytest.mark.skipif(not WORKER.is_file(), reason="Worker 필요")
def test_force_stop_keeps_confirmed_point_and_resumes(tmp_path):
    reg = registry(tmp_path)
    _, row = first_trip(reg)
    arm_recovery(reg, row, remaining_ms=20000)
    first = controller(reg, clock=lambda: (row["end_ms"] - 20000) / 1000)
    first.start(SETTINGS)
    time.sleep(7)  # 체크포인트(5초) 이후 강제 중단
    first.stop(force=True)
    assert first.wait(20)
    op = reg.operation()
    trip, confirmed, _ = state(reg)
    assert op["last_stop"] == "abnormal" and trip is not None
    assert confirmed > row["end_ms"] - 20000  # 확인 지점이 앞으로 갔다.
    resumed = controller(reg, clock=lambda: time.time())
    resumed.start(SETTINGS)
    assert wait_for(lambda: state(reg)[0] is None)
    resumed.stop()
    assert resumed.wait(20)
    assert resumed.live["result"]["microtrip_count"] <= (row["end_ms"] - confirmed) // 1000
    assert reg.operation()["last_stop"] == "graceful"


@pytest.mark.skipif(not WORKER.is_file(), reason="Worker 필요")
def test_add_and_remove_while_running_and_duration_cap(tmp_path):
    reg = registry(tmp_path)
    ops = controller(reg, clock=lambda: datetime(2026, 11, 24, 1, 0, tzinfo=KST).timestamp())  # 운행 없는 새벽
    ops.day_seconds = 20  # 기간 상한 1일 = 20초(검사 전용)
    ops.start(OperationSettings(interval_ms=1000, client_count=2, duration_cap_days=1))
    assert wait_for(lambda: reg.summary()["status"].get("driving", {}).get("count") == 1)
    added = reg.register(1, SHORT)
    assert wait_for(lambda: reg.summary()["status"].get("driving", {}).get("count") == 2, 15)
    assert state(reg, added["first_id"]) is not None
    with sqlite3.connect(reg.path) as con:
        start_day = con.execute("SELECT start_day FROM vehicle_state WHERE id=?", (added["first_id"],)).fetchone()[0]
    assert start_day == "2026-11-25"  # 운영 중 등록 → 다음 가상 날짜부터
    reg.request_removal([1])
    assert wait_for(lambda: reg.summary()["status"].get("removed", {}).get("count") == 1, 15)
    assert ops.wait(30)  # 기간 상한 도달 → 자동 정상 중단
    op = reg.operation()
    assert op["last_stop"] == "graceful" and "기간 상한" in op["stop_reason"]
    assert reg.summary()["status"]["ready"]["count"] == 1


@pytest.fixture
def fixture_profile(tmp_path):
    env = os.environ.copy()
    env.update(GOCACHE=str(ROOT / ".local/go-cache"), GOMODCACHE=str(ROOT / ".local/go-mod"))
    binary = tmp_path / ("fixture.exe" if os.name == "nt" else "fixture")
    subprocess.run(["go", "test", "-c", "-o", str(binary), "."], cwd=ROOT / "services/worker", env=env, check=True, timeout=180)
    profile = tmp_path / "profile.json"
    env.update(DANGUBI_SCHEDULE_PROFILE=str(profile), DANGUBI_SCHEDULE_FAULT="none")
    proc = subprocess.Popen([str(binary), "-test.run=^TestScheduleMQTTFixture$"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert wait_for(profile.is_file, 10)
        yield profile
    finally:
        Path(str(profile) + ".done").touch()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


@pytest.mark.skipif(not WORKER.is_file(), reason="Worker 필요")
def test_mqtt_uses_fixed_operation_topic_client_and_pseudonym(tmp_path, fixture_profile):
    reg = registry(tmp_path)
    _, row = first_trip(reg)
    arm_recovery(reg, row, remaining_ms=4000)
    profile = json.loads(fixture_profile.read_text(encoding="utf-8"))
    profile.update(execution_profile="operations", max_connecting=10, enabled=True, contract_reviewed=True)
    ops = controller(reg, clock=lambda: (row["end_ms"] - 4000) / 1000)
    ops.start(OperationSettings(transport="mqtt", interval_ms=1000, client_count=2), profile)
    assert wait_for(lambda: state(reg)[0] is None)
    ops.stop()
    assert ops.wait(20)
    assert reg.operation()["last_stop"] == "graceful", reg.operation()
    Path(str(fixture_profile) + ".done").touch()
    report_path = Path(str(fixture_profile) + ".report")
    assert wait_for(report_path.is_file, 10)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    alias = reg.vehicles()["vehicles"][0]["pseudonym"]
    assert report["received"] == 4  # 남은 Microtrip 3건 + Trip 1건
    assert set(report["topics"]) == {f"{profile['topic_prefix']}/ops-main/{alias}/obd"}
    assert set(report["client_ids"]) <= {"sim-ops-main-c001", "sim-ops-main-c002"}
    assert all(m["vehicleId"] == alias for m in report["messages"])
    with sqlite3.connect(reg.path) as con:
        raw = con.execute("SELECT vin FROM vehicles").fetchone()[0]
    assert raw not in json.dumps(report)  # 원본 VIN은 전송되지 않는다.


@pytest.mark.skipif(not WORKER.is_file(), reason="Worker 필요")
def test_operations_api_guards_and_dry_run_cycle(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from services.control.test_app import control
    reg = registry(tmp_path)
    monkeypatch.setattr(control, "DB", tmp_path / "runs.sqlite3")
    monkeypatch.setattr(control, "CHECKPOINTS", control.CheckpointStore(tmp_path / "states.sqlite3"))
    monkeypatch.setattr(control, "WORKER", WORKER)
    monkeypatch.setattr(control, "_REGISTRY", reg)
    monkeypatch.setattr(control, "_OPERATION", OperationController(reg, WORKER, validate))
    profile = tmp_path / "mqtt.json"
    profile.write_text(json.dumps({"enabled": True, "contract_reviewed": True, "execution_profile": "operations",
                                   "broker_url": "ssl://broker.example.com:8883", "ca_file": "a", "cert_file": "b", "key_file": "c",
                                   "max_clients": 500, "max_duration_seconds": 2592600, "max_messages": 1000000}), encoding="utf-8")
    monkeypatch.setattr(control, "MQTT_PROFILE", profile)
    with TestClient(control.app) as c:
        assert c.post("/api/operations/start", json={"settings": {"transport": "mqtt"}}).status_code == 409  # 송신 확인 필요
        blocked = c.post("/api/operations/start", json={"settings": {"transport": "mqtt"}, "confirm_mqtt": True})
        assert blocked.status_code == 409 and "vault" in blocked.json()["detail"]  # 개발용 가명은 외부 브로커 불가
        assert c.post("/api/operations/start", json={"settings": {"interval_ms": 10}}).status_code == 422
        assert c.post("/api/operations/stop", json={}).status_code == 409
        started = c.post("/api/operations/start", json={"settings": {"client_count": 1}})
        assert started.status_code == 202 and started.json()["running"] is True
        assert c.post("/api/operations/start", json={}).status_code == 409  # 동시에 하나
        plan = c.post("/api/workspace/plans", json={"title": "운영 프로필", "fleet": {"execution_profile": "operations", "fleet_version": "fleet-plan-4", "vehicle_count": 2,
                      "measurement": {"start": "2026-11-24T00:00:00+09:00", "end": "2026-11-24T01:00:00+09:00"}}})
        assert plan.status_code == 201, plan.text
        refused = c.post("/api/workspace/execute", json={"plan_key": plan.json()["key"]})
        assert refused.status_code == 409 and "소량 시험" in refused.json()["detail"]
        assert c.post("/api/operations/stop", json={}).status_code == 200
        assert control._OPERATION.wait(20)
        status = c.get("/api/operations/status").json()
        assert status["state"] == "stopped" and status["last_stop"] == "graceful"
        assert [e["kind"] for e in c.get("/api/operations/events").json()["events"]][:2] == ["graceful_stop", "stopping"]
