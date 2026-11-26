"""상시 운영 차량 운행 조회: 운행 이력 기록과 재생성 일치. 실제 Worker만 쓰며 외부 전송은 없다."""
import hashlib
import json
import sqlite3

import pytest

from services.control.operation_query import generate as regenerate
from services.control.operation_runtime import INITIAL_STATE, OperationController, OperationGenerator
from services.control.test_operation_runtime import (SETTINGS, WORKER, collect, controller, first_trip, registry, state,
                                                     validate, wait_for)


def worker_hash():
    return hashlib.sha256(WORKER.read_bytes()).hexdigest()


def start_record(reg, row, settings=SETTINGS, worker=None, session="s1"):
    trip = {"key": row["trip_key"], "a": row["start_ms"], "b": row["end_ms"], "profile": row["road_profile"],
            "initial": dict(INITIAL_STATE), "worker_hash": worker or worker_hash(),
            "alias": reg.vehicles()["vehicles"][0]["pseudonym"], "destination": None,
            "settings": settings.model_dump()}
    reg.save_trip_start(1, trip, session=session)
    return trip


def pages(trip, low=None, high=None):
    out, offset = [], 0
    while True:
        page = regenerate(trip, WORKER, low, high, offset, 100)
        out += page["messages"]
        offset += len(page["messages"])
        if not page["has_more"]:
            return out, page


def test_history_lifecycle_and_lookup(tmp_path):
    reg = registry(tmp_path)
    _, row = first_trip(reg)
    start_record(reg, row)
    vehicle = reg.vehicles()["vehicles"][0]
    assert reg.find_vehicle("1")["id"] == 1 and reg.find_vehicle(vehicle["pseudonym"])["id"] == 1
    assert reg.find_vehicle("999") is None
    # 합성 VIN은 표시하고 조회 입력으로도 쓴다(대소문자 무관).
    with sqlite3.connect(reg.path) as con:
        raw = con.execute("SELECT vin FROM vehicles WHERE id=1").fetchone()[0]
    assert reg.find_vehicle(raw)["id"] == 1 and reg.find_vehicle(raw.lower())["id"] == 1
    found = reg.history(1, row["start_ms"] - 1, row["end_ms"] + 1)
    assert found["vehicle"]["vin"] == raw
    assert [t["status"] for t in found["trips"]] == ["sending"] and found["total_trips"] == 1
    reg.save_confirmed({1: row["start_ms"] + 3000})
    assert reg.history(1, 0, row["end_ms"] + 1)["trips"][0]["confirmed_ts"] == row["start_ms"] + 3000  # 진행 중 확인 시각
    reg.mark_history_interrupted()
    assert reg.history(1, 0, row["end_ms"] + 1)["trips"][0]["status"] == "interrupted"
    reg.recover_history(1, row["trip_key"], row["start_ms"] + 3000)
    reg.save_trip_end(1, row["end_ms"], dict(INITIAL_STATE))
    done = reg.history(1, 0, row["end_ms"] + 1)["trips"][0]
    assert done["status"] == "completed" and done["recoveries"] == 1 and done["resume_after_ms"] == row["start_ms"] + 3000
    assert done["confirmed_ts"] == row["end_ms"] and done["finished_at"]
    assert reg.history(1, row["end_ms"], row["end_ms"] + 10)["trips"] == []  # 기간 밖
    latest = reg.history(1)  # 기본: 마지막 운행 날짜(KST)까지 7일
    assert latest["to_ms"] - latest["from_ms"] == 7 * 86400000 and latest["from_ms"] < row["start_ms"] < latest["to_ms"]
    assert (latest["to_ms"] + 9 * 3600000) % 86400000 == 0 and latest["trips"][0]["trip_key"] == row["trip_key"]


@pytest.mark.skipif(not WORKER.is_file(), reason="Worker 필요")
def test_regeneration_matches_operation_generator(tmp_path):
    """조회 재생성(schedule_obd)과 운영 생성 세션(schedule_stream)이 같은 본문을 만든다."""
    reg = registry(tmp_path)
    info, row = first_trip(reg)
    gen = OperationGenerator(reg, WORKER, SETTINGS, reg.seed())
    gen.add(row["vehicle_id"], info)
    gen.remember(row)
    sent, sent_trip = collect(gen, row["vehicle_id"], row["trip_key"])
    start_record(reg, row)
    trip = reg.history_trip(1, row["trip_key"])
    again, page = pages(trip)
    assert again == sent and page["trip"] == sent_trip and page["synthetic_trip"] is False
    assert page["same_worker"] is True and page["delivery"]["status"] == "sending"
    assert page["trace"] and page["model"]
    # 구간 조회는 전체 운행 메시지의 해당 부분과 같다.
    low, high = sent[10]["ts"], sent[20]["ts"]
    part, _ = pages(trip, low, high)
    assert part == [m for m in sent if low <= m["ts"] <= high]
    # 당시 Worker 지문이 다르면 경고한다.
    other = dict(trip, worker_hash="0" * 64)
    assert "당시 Worker와 다릅니다" in regenerate(other, WORKER, None, None, 0, 1)["warnings"][0]
    with pytest.raises(ValueError):
        regenerate(trip, WORKER, row["start_ms"] - 1, None, 0, 1)


@pytest.mark.skipif(not WORKER.is_file(), reason="Worker 필요")
def test_crash_recovery_history_and_sent_messages_match(tmp_path):
    reg = registry(tmp_path)
    _, row = first_trip(reg)
    start_record(reg, row)
    reg.save_confirmed({1: row["end_ms"] - 5000})
    reg.update_operation(state="running", virtual_checkpoint_ms=row["end_ms"] - 5000, settings=SETTINGS.model_dump())
    # 서버가 실행 중 꺼진 상태로 다시 뜬다 → 비정상 종료·복구 대기.
    ops = controller(reg, clock=lambda: (row["end_ms"] - 5000) / 1000)
    item = reg.history(1, 0, row["end_ms"] + 1)["trips"][0]
    assert item["status"] == "interrupted" and item["confirmed_ts"] == row["end_ms"] - 5000
    ops.start(SETTINGS)
    assert wait_for(lambda: state(reg)[0] is None)
    ops.stop()
    assert ops.wait(20)
    item = reg.history(1, 0, row["end_ms"] + 1)["trips"][0]
    assert item["status"] == "completed" and item["recoveries"] == 1 and item["resume_after_ms"] == row["end_ms"] - 5000
    samples = [json.loads(s["message"]) for s in ops.live["result"]["samples"]]
    alias = reg.vehicles()["vehicles"][0]["pseudonym"]
    assert all(s.pop("vehicleId") == alias for s in samples)
    trip = reg.history_trip(1, row["trip_key"])
    again, page = pages(trip, row["end_ms"] - 4000, row["end_ms"])
    assert samples == again + [page["trip"]]  # 실제 보낸 복구 구간 = 재생성 구간
    assert page["delivery"]["status"] == "completed" and page["delivery"]["duplicate_risk_after_ms"] == row["end_ms"] - 5000
    assert "생성 완료" in page["delivery"]["description"]


@pytest.mark.skipif(not WORKER.is_file(), reason="Worker 필요")
def test_new_trip_in_running_operation_is_recorded(tmp_path):
    """운영 중 새로 시작한 운행이 첫 메시지 전에 이력에 남고, 세션·설정 지문을 갖는다."""
    reg = registry(tmp_path)
    _, row = first_trip(reg)
    ops = controller(reg, clock=lambda: (row["start_ms"] - 1000) / 1000)
    ops.start(SETTINGS)
    assert wait_for(lambda: reg.history(1, 0, row["end_ms"] + 1)["trips"], timeout=20)
    ops.stop(force=True)
    assert ops.wait(20)
    item = reg.history(1, 0, row["end_ms"] + 1)["trips"][0]
    assert item["trip_key"] == row["trip_key"] and item["status"] == "interrupted" and item["session"]
    trip = reg.history_trip(1, row["trip_key"])
    assert trip["worker_hash"] == worker_hash() and trip["settings"]["interval_ms"] == SETTINGS.interval_ms


def test_history_backfill_for_trip_started_before_feature(tmp_path):
    reg = registry(tmp_path)
    _, row = first_trip(reg)
    legacy = {"key": row["trip_key"], "a": row["start_ms"], "b": row["end_ms"], "profile": row["road_profile"], "initial": dict(INITIAL_STATE)}
    with sqlite3.connect(reg.path) as con:
        con.execute("UPDATE vehicle_state SET trip=?, confirmed_ts=? WHERE id=1", (json.dumps(legacy), row["start_ms"] + 2000))
        con.execute("UPDATE operation SET settings=?", (json.dumps(SETTINGS.model_dump()),))
    reopened = type(reg)(reg.path, reg.provider)
    item = reopened.history(1, 0, row["end_ms"] + 1)["trips"][0]
    assert item["status"] == "interrupted" and item["confirmed_ts"] == row["start_ms"] + 2000
    assert reopened.history_trip(1, row["trip_key"])["worker_hash"] is None  # 지문이 없으면 일치 확인 불가로 표시


def test_operations_query_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from services.control.test_app import control
    reg = registry(tmp_path)
    _, row = first_trip(reg)
    start_record(reg, row)
    monkeypatch.setattr(control, "WORKER", WORKER)
    monkeypatch.setattr(control, "_REGISTRY", reg)
    monkeypatch.setattr(control, "_OPERATION", OperationController(reg, WORKER, validate))
    with TestClient(control.app) as c:
        alias = reg.vehicles()["vehicles"][0]["pseudonym"]
        assert c.get("/api/operations/vehicles/lookup", params={"q": alias}).json()["id"] == 1
        assert c.get("/api/operations/vehicles/lookup", params={"q": "77"}).status_code == 404
        assert c.get("/api/operations/vehicles/1/history", params={"from_ms": 10, "to_ms": 5}).status_code == 422
        assert c.get("/api/operations/vehicles/1/history", params={"from_ms": 0, "to_ms": 40 * 86400000}).status_code == 422
        history = c.get("/api/operations/vehicles/1/history", params={"from_ms": row["start_ms"] - 1, "to_ms": row["end_ms"]}).json()
        assert history["trips"][0]["trip_key"] == row["trip_key"]
        assert c.get("/api/operations/vehicles/9/history", params={"from_ms": 0, "to_ms": 1000}).status_code == 404
        if WORKER.is_file():
            page = c.post(f"/api/operations/obd/1/{row['trip_key']}?limit=5", json={"checkpoint_epoch": 3}).json()
            assert len(page["messages"]) == 5 and page["pseudonym"] == alias
            search = c.post(f"/api/operations/obd/1/{row['trip_key']}?limit=100", json={"search_events": True, "event_mask": 0}).json()
            assert search["event_search"] is not None
        assert c.post("/api/operations/obd/1/" + "0" * 64, json={}).status_code == 404
        assert c.post(f"/api/operations/obd/1/{row['trip_key']}", json={"event_mask": 256}).status_code == 422  # bit8 이상 없음


def test_recovery_requirement_is_visible_and_names_changed_settings(tmp_path):
    """비정상 종료 뒤 당시 설정을 화면에서 알 수 있고, 다른 항목을 오류에 적는다. 처음 시작에는 복구 안내가 없다."""
    reg = registry(tmp_path)
    ops = OperationController(reg, WORKER, validate)
    assert ops.status()["recovery"] is None  # 처음 시작: 복구 대상 없음
    _, row = first_trip(reg)
    start_record(reg, row)
    reg.update_operation(last_stop="abnormal", virtual_checkpoint_ms=row["start_ms"], settings=SETTINGS.model_dump())
    recovery = ops.status()["recovery"]
    assert recovery["trips"] == 1 and recovery["settings"] == SETTINGS.model_dump()
    changed = SETTINGS.model_copy(update={"vehicle_count": 1000, "client_count": 5})
    with pytest.raises(RuntimeError) as error:
        ops.start(changed)
    assert "운영 차량 수" in str(error.value) and "Client 수" in str(error.value) and "당시 설정 불러오기" in str(error.value)
    assert not ops.running()


def test_history_identity_column_migration_and_legacy_rows(tmp_path):
    """송신 식별자 열이 없던 기존 명부: 열을 추가하고 기존 이력은 보존한다. 기록이 없는 운행은 현재 모드 값으로 표시하고 안내한다."""
    reg = registry(tmp_path)
    _, row = first_trip(reg)
    start_record(reg, row)
    with sqlite3.connect(reg.path) as con:
        con.execute("ALTER TABLE trip_history DROP COLUMN wire_identity")  # 이전 버전 명부 상태
        before = con.execute("SELECT vehicle_id, trip_key, status, start_ms, end_ms FROM trip_history").fetchall()
    pseudo = type(reg)(reg.path, reg.provider)
    with sqlite3.connect(reg.path) as con:
        assert con.execute("SELECT vehicle_id, trip_key, status, start_ms, end_ms FROM trip_history").fetchall() == before
        assert con.execute("SELECT wire_identity FROM trip_history").fetchone()[0] is None
    legacy = pseudo.history_trip(1, row["trip_key"])
    assert legacy["vehicleId"] == legacy["pseudonym"] and not legacy["identity_recorded"]
    page = regenerate(legacy, WORKER, None, None, 0, 1)
    assert page["vehicleId"] == legacy["pseudonym"] and any("송신 식별자 기록이 없어" in w for w in page["warnings"])
    raw_mode = type(reg)(reg.path, reg.provider, synthetic_raw_vin=True)
    with sqlite3.connect(reg.path) as con:
        raw = con.execute("SELECT vin FROM vehicles WHERE id=1").fetchone()[0]
    assert raw_mode.history_trip(1, row["trip_key"])["vehicleId"] == raw  # 기록 없는 운행은 현재 모드 기준
    # 새로 기록하는 운행은 당시 송신 식별자를 저장하고 모드가 바뀌어도 그 값을 보여 준다.
    with sqlite3.connect(reg.path) as con:
        con.execute("DELETE FROM trip_history")
    start_record(reg, row)
    assert raw_mode.history_trip(1, row["trip_key"])["vehicleId"] == legacy["pseudonym"]
    assert raw_mode.history(1, 0, row["end_ms"] + 1)["vehicle"]["vin"] == raw  # 합성 VIN 전체 표시
