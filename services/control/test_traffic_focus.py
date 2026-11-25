"""운행 집중 시간대: 조정 규칙, 목표 맞춤, 상시 운영 날짜별 고정, 시험 일정 연결, 설정 변경 자동 재시작."""
from datetime import date, datetime
import time

import pytest
from pydantic import ValidationError

from services.control.operation_plan import FocusPlanner, OperationSettings, base_day_trips, day_trips, vehicle_key
from services.control.operation_runtime import OperationController
from services.control.registry import DevelopmentPseudonyms, Registry
from services.control.schedule import KST
from services.control.test_operation_runtime import SHORT, WORKER, validate, wait_for
from services.control.traffic_focus import TrafficFocus, active_windows, coverage

DAY = date(2026, 12, 1)  # 화요일
FOCUS = {"windows": [{"start": "08:20", "end": "08:50", "target_percent": 40, "shape": "peak"},
                     {"start": "18:00", "end": "18:40", "target_percent": 35, "shape": "flat"}]}


def fleet(tmp_path, count=400, focus=FOCUS, **extra):
    reg = Registry(tmp_path / "reg.sqlite3", DevelopmentPseudonyms(b"k" * 32), seed=9)
    reg.register(count, {**SHORT, "trip_min_minutes": 3, "trip_max_minutes": 120})
    settings = OperationSettings(traffic_focus=focus, **extra)
    planner = FocusPlanner(settings, reg.seed(), reg)
    for v in reg.session_vehicles():
        planner.add(v["id"], v["fixed"])
    return reg, settings, planner


def test_focus_settings_validation():
    for bad in ({"windows": []}, {"windows": [{"start": "03:00", "end": "03:30", "target_percent": 50}]},
                {"windows": [{"start": "08:00", "end": "08:05", "target_percent": 50}]},
                {"windows": [{"start": "08:00", "end": "09:00", "target_percent": 0}]},
                {"windows": [{"start": "08:00", "end": "09:00", "target_percent": 50, "weekdays": [8]}]},
                {"windows": [{"start": "08:00", "end": "09:00", "target_percent": 50}, {"start": "08:30", "end": "09:30", "target_percent": 50}]},
                {"windows": [{"start": f"{h:02d}:00", "end": f"{h:02d}:30", "target_percent": 50} for h in (6, 8, 10, 12)]}):
        with pytest.raises(ValidationError):
            TrafficFocus.model_validate(bad)
    ok = TrafficFocus.model_validate(FOCUS)
    assert ok.windows[0].weekdays == [1, 2, 3, 4, 5]
    assert active_windows(ok, date(2026, 12, 5)) == []  # 토요일: 기본 평일만


def test_focus_raises_concurrency_and_keeps_vehicle_rules(tmp_path):
    reg, settings, planner = fleet(tmp_path)
    report = planner.report(DAY)
    seed, infos = reg.seed(), reg.session_vehicles()
    for item, (_, _, w0, w1) in zip(report["windows"], active_windows(settings.traffic_focus, DAY)):
        assert item["baseline_percent"] < item["target_percent"]
        assert item["within_tolerance"] and abs(item["planned_percent"] - item["target_percent"]) <= 5
        real = sum(coverage(day_trips(settings, seed, vehicle_key(v["id"]), v["fixed"], DAY, planner), w0, w1) for v in infos) / len(infos) * 100
        assert round(real, 1) == item["planned_percent"]  # 계획값 = 실제 적용 결과
    starts = set()
    for v in infos:
        base = base_day_trips(settings, seed, vehicle_key(v["id"]), v["fixed"], DAY)
        moved = day_trips(settings, seed, vehicle_key(v["id"]), v["fixed"], DAY, planner)
        assert sorted(b - a for a, b, _ in base) == sorted(b - a for a, b, _ in moved)  # 운행 길이·횟수 유지
        assert all(y[0] - x[1] >= 1800000 for x, y in zip(moved, moved[1:]))           # 주차 30분
        assert bool(base) == bool(moved)                                                 # 미운행일은 그대로
        starts |= {a for a, _, _ in moved if a not in {x[0] for x in base}}
    assert len(starts) > 20  # 옮긴 출발 시각이 한 시각에 몰리지 않는다
    assert report["bins"]["adjusted"][50] > report["bins"]["baseline"][50]  # 08:20~08:30 칸


def test_unreachable_and_already_met_targets_are_reported(tmp_path):
    reg, settings, planner = fleet(tmp_path, focus={"windows": [{"start": "08:20", "end": "08:50", "target_percent": 99},
                                                                {"start": "13:00", "end": "13:30", "target_percent": 1}]})
    high, low = planner.report(DAY)["windows"]
    assert high["planned_percent"] == high["maximum_percent"] < 99 and not high["within_tolerance"] and high["reasons"]
    assert low["probability"] == 0 and "줄이지 않음" in low["reasons"][0]


def test_operation_focus_day_is_frozen_when_fleet_changes(tmp_path):
    reg, settings, planner = fleet(tmp_path, count=200)
    first = planner.report(DAY)
    reg.register(50, {**SHORT, "trip_min_minutes": 3, "trip_max_minutes": 120})
    again = FocusPlanner(settings, reg.seed(), reg)
    for v in reg.session_vehicles():
        again.add(v["id"], v["fixed"])
    assert again.report(DAY) == first  # 이미 정한 날짜는 기존 차량 일정이 바뀌지 않는다
    other = FocusPlanner(OperationSettings(traffic_focus={"windows": [{"start": "09:00", "end": "09:30", "target_percent": 40}]}), reg.seed(), reg)
    assert other.digest != planner.digest  # 설정이 다르면 따로 계산


def test_trial_fleet_focus_changes_schedule_and_reports():
    from services.control.fleet import FleetRequest, preview
    body = {"fleet_version": "fleet-plan-4", "vehicle_count": 150, "trip_min_minutes": 3, "trip_max_minutes": 120,
            "measurement": {"start": "2026-12-01T00:00:00+09:00", "end": "2026-12-02T00:00:00+09:00"}}
    plain, focused = FleetRequest(**body), FleetRequest(**body, traffic_focus=FOCUS)
    a, b = preview(plain), preview(focused)
    assert a["traffic_focus"] is None and len(b["traffic_focus"]) == 1
    report = b["traffic_focus"][0]
    assert report["date"] == "2026-12-01" and report["windows"][0]["planned_percent"] > report["windows"][0]["baseline_percent"]
    assert preview(focused) == b  # 결정적
    with pytest.raises(ValidationError):
        FleetRequest(**{**body, "fleet_version": "fleet-plan-3", "daily_driving_minutes": 60}, traffic_focus=FOCUS)


@pytest.mark.skipif(not WORKER.is_file(), reason="Worker 필요")
def test_settings_change_restarts_automatically_and_can_be_cancelled(tmp_path):
    reg = Registry(tmp_path / "reg.sqlite3", DevelopmentPseudonyms(b"k" * 32), seed=9)
    reg.register(3, SHORT)
    noon = datetime(2026, 12, 1, 12, 0, tzinfo=KST).timestamp()
    quiet = OperationSettings(interval_ms=1000, client_count=1, day_off_probability=1)  # 진행 운행이 없어 중단이 바로 끝난다
    ops = OperationController(reg, WORKER, validate, clock=lambda: noon)
    with pytest.raises(RuntimeError):
        ops.apply_settings(quiet)  # 실행 중이 아니면 거절
    ops.start(quiet)
    assert wait_for(lambda: ops.live.get("vehicles") == 3)
    with pytest.raises(RuntimeError, match="같습니다"):
        ops.apply_settings(quiet)
    changed = quiet.model_copy(update={"client_count": 2, "traffic_focus": TrafficFocus.model_validate(FOCUS)})
    ops.apply_settings(changed)
    assert ops.status()["pending_settings"]["changed"] == ["Client 수", "운행 집중 시간대"]
    assert wait_for(lambda: ops.pending is None and ops.running(), timeout=30)
    op = reg.operation()
    assert op["settings"]["client_count"] == 2 and op["settings"]["traffic_focus"]["windows"][0]["start"] == "08:20"
    kinds = [e["kind"] for e in reg.events(20)]
    assert kinds.index("settings_restarted") < kinds.index("graceful_stop") < kinds.index("settings_change")
    # 취소: 정상 중단은 끝나고 새 설정은 적용하지 않는다.
    ops.apply_settings(quiet)
    ops.cancel_pending()
    assert ops.wait(20)
    time.sleep(.5)
    assert not ops.running() and reg.operation()["settings"]["client_count"] == 2 and ops.pending is None
    # 강제 중단으로 끝나면 자동 시작하지 않고 이유를 남긴다.
    ops.start(changed)
    assert wait_for(lambda: ops.live.get("vehicles") == 3)
    ops.apply_settings(quiet)
    ops.stop(force=True)
    assert ops.wait(20)
    assert wait_for(lambda: bool(ops.pending) and ops.pending["state"] == "blocked", timeout=10)
    assert not ops.running() and "강제 중단" in ops.status()["pending_settings"]["reason"]
    ops.start(changed)  # 직접 시작하면 막혀 있던 대기는 정리된다
    assert ops.pending is None
    ops.stop(force=True)
    ops.wait(20)


def test_focus_api_preview_and_apply_guards(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from services.control.test_app import control
    reg, _, _ = fleet(tmp_path, count=120)
    monkeypatch.setattr(control, "WORKER", WORKER)
    monkeypatch.setattr(control, "_REGISTRY", reg)
    monkeypatch.setattr(control, "_OPERATION", OperationController(reg, WORKER, validate))
    with TestClient(control.app) as c:
        result = c.post("/api/operations/focus/preview", json={"settings": {"traffic_focus": FOCUS}, "date": "2026-12-01"})
        assert result.status_code == 200, result.text
        data = result.json()
        assert data["vehicles"] == 120 and len(data["bins"]["adjusted"]) == 144 and data["frozen"] is False
        assert c.post("/api/operations/focus/preview", json={"settings": {}, "date": "2026-12-01"}).status_code == 422
        bad = c.post("/api/operations/focus/preview", json={"settings": {"traffic_focus": {"windows": [{"start": "08:00", "end": "08:05", "target_percent": 5}]}}, "date": "2026-12-01"})
        assert bad.status_code == 422 and "10분" in bad.json()["detail"]
        assert c.post("/api/operations/settings/apply", json={"settings": {}}).status_code == 409  # 실행 중 아님
        assert c.post("/api/operations/settings/cancel").status_code == 409


def test_small_fleet_stops_at_the_closest_point_to_target():
    """차량이 적으면 한 대의 몫이 커서 목표를 넘기는 쪽과 못 미치는 쪽 중 가까운 쪽을 고른다."""
    import random
    from services.control.traffic_focus import calibrate, midnight_ms
    base = midnight_ms(DAY)
    trips = lambda h: [(base + h * 3600000, base + h * 3600000 + 60 * 60000, "urban")]
    rngs = lambda n: (lambda kind, index: random.Random(f"{n}|{kind}|{index}"))
    vehicles = [(trips(6), rngs(i)) for i in range(3)]
    report = calibrate(vehicles, TrafficFocus.model_validate({"windows": [{"start": "08:20", "end": "08:50", "target_percent": 40}]}), DAY)
    item = report["windows"][0]
    assert abs(item["planned_percent"] - 40) <= 100 / 3 / 2 + .1
    if not item["within_tolerance"]:
        assert any("한 대가" in r for r in item["reasons"])
