"""UI 분석 화면 없이 생활 운행의 길이·일정 연속성·재현을 개발 검증한다."""
from collections import Counter
from pathlib import Path
import json

from services.control.fleet import FleetRequest, allocation, expand
from services.control.schedule import instant


def request(**changes):
    body=json.loads((Path(__file__).resolve().parents[2]/"configs/fleet-diverse.example.json").read_text(encoding="utf-8"))
    body.update(vehicle_count=60,day_off_probability=0.0,weekend_activity_probability=1.0,
                measurement={"start":"2026-11-16T00:00:00+09:00","end":"2026-11-23T00:00:00+09:00","timezone":"Asia/Seoul"})
    body.update(changes)
    return FleetRequest(**body)


def test_varied_journeys_include_short_regular_and_long_without_daily_forcing():
    req=request()
    schedule,_=expand(req)
    assert schedule==expand(req)[0]
    lengths=[]
    totals=[]
    for vehicle in schedule.vehicles:
        days=Counter()
        trips=sorted(vehicle.trips,key=lambda t:t.start)
        for before,after in zip(trips,trips[1:]):
            assert instant(after.start)-instant(before.end)>=1800000
        for trip in trips:
            length=(instant(trip.end)-instant(trip.start))/60000
            assert 2<=length<=120
            assert trip.start[:10]==trip.end[:10] or trip.end[11:19]=="00:00:00"
            if req.measurement.start[:10]<=trip.start[:10]<req.measurement.end[:10]:
                lengths.append(length);days[trip.start[:10]]+=length
        totals.extend(days.values())
    assert sum(n<=10 for n in lengths)/len(lengths)>.15
    assert sum(n>=60 for n in lengths)/len(lengths)<.2
    assert any(n>=60 for n in lengths) and any(10<n<30 for n in lengths)
    assert len(set(totals))>30 and any(n<30 for n in totals) and any(n>90 for n in totals)
    print("운행 길이 표본",dict(Counter("10분 이하" if n<=10 else "10~30분" if n<30 else "30~60분" if n<60 else "60분 이상" for n in lengths)))


def test_varied_journeys_preserve_calendar_and_allow_road_changes():
    req=request(vehicle_count=5,road_mix={"urban":0,"highway":0,"mixed":0,"congested":100})
    before,_=expand(req)
    wider,_=expand(request(vehicle_count=5,road_mix=req.road_mix,measurement={"start":"2026-11-15T00:00:00+09:00","end":"2026-11-25T00:00:00+09:00","timezone":"Asia/Seoul"}))
    for a,b in zip(before.vehicles,wider.vehicles):
        assert [t for t in a.trips if t.start.startswith("2026-11-18")]==[t for t in b.trips if t.start.startswith("2026-11-18")]
    assert {t.road_profile for v in before.vehicles for t in v.trips}>={"urban","congested"}
    # 미운행은 운행 길이 조절과 별도이며 100% 미운행도 보존한다.
    assert all(not v.trips for v in expand(request(day_off_probability=1.0))[0].vehicles)


def test_legacy_fleet_version_still_divides_daily_budget():
    req=request(fleet_version="fleet-plan-1",daily_driving_minutes=120,vehicle_variation_percent=0,daily_variation_percent=0)
    schedule,_=expand(req)
    for vehicle in schedule.vehicles:
        totals=Counter()
        for trip in vehicle.trips:
            totals[trip.start[:10]]+=(instant(trip.end)-instant(trip.start))/60000
        assert set(totals.values())=={120}


def bounded_request(**changes):
    body=json.loads((Path(__file__).resolve().parents[2]/"configs/fleet-bounded.example.json").read_text(encoding="utf-8"))
    body.update(vehicle_count=24,day_off_probability=0.0,weekend_activity_probability=1.0,
                measurement={"start":"2026-11-16T00:00:00+09:00","end":"2026-11-23T00:00:00+09:00","timezone":"Asia/Seoul"})
    body.update(changes)
    return FleetRequest(**body)


def test_bounded_journeys_obey_trip_limits_parking_and_replay_at_extremes():
    for low,high in [(3,90),(2,2),(12,17),(180,240),(240,240)]:
        req=bounded_request(trip_min_minutes=low,trip_max_minutes=high,daily_variation_percent=50,departure_jitter_minutes=120)
        schedule,assigned=expand(req)
        assert req.daily_driving_minutes is None
        assert all("daily_target_minutes" not in v for v in assigned.values())
        assert schedule==expand(req)[0]
        lengths=[]
        for vehicle in schedule.vehicles:
            trips=sorted(vehicle.trips,key=lambda t:t.start)
            for before,after in zip(trips,trips[1:]):
                assert instant(after.start)-instant(before.end)>=1800000
            for trip in trips:
                length=(instant(trip.end)-instant(trip.start))/60000
                assert low<=length<=high
                assert trip.start[:10]==trip.end[:10] or trip.end[11:19]=="00:00:00"
                lengths.append(length)
        if high-low>20:
            assert len(set(lengths))>20


def test_bounded_rejects_daily_target_and_reversed_limits():
    import pytest
    for fields in [dict(trip_min_minutes=20,trip_max_minutes=10),dict(daily_driving_minutes=160),dict(trip_min_minutes=1),dict(trip_max_minutes=241)]:
        with pytest.raises(ValueError):
            bounded_request(**fields)


def test_bounded_calendar_replay_and_off_days():
    req=bounded_request(vehicle_count=2)
    original,_=expand(req)
    wider,_=expand(bounded_request(vehicle_count=2,measurement={"start":"2026-11-15T00:00:00+09:00","end":"2026-11-25T00:00:00+09:00","timezone":"Asia/Seoul"}))
    for a,b in zip(original.vehicles,wider.vehicles):
        assert [t for t in a.trips if t.start.startswith("2026-11-18")]==[t for t in b.trips if t.start.startswith("2026-11-18")]
    assert all(not v.trips for v in expand(bounded_request(day_off_probability=1))[0].vehicles)


def test_new_fleet_removes_vehicle_coefficient_and_preserves_legacy_schedule():
    import hashlib
    import pytest
    req=bounded_request()
    assert req.vehicle_variation_percent is None
    assert all("duration_preference" not in v for v in allocation(req).values())
    with pytest.raises(ValueError,match="차량 간"):
        bounded_request(vehicle_variation_percent=20)
    body=json.loads((Path(__file__).resolve().parents[2]/"configs/fleet-bounded.example.json").read_text(encoding="utf-8"))
    # 새 예제의 기본 범위와 독립적으로 이전 3~90분 일정의 지문을 검증한다.
    body.update(fleet_version="fleet-plan-3",vehicle_count=2,vehicle_variation_percent=20,trip_max_minutes=90)
    schedule=expand(FleetRequest(**body))[0]
    assert hashlib.sha256(schedule.model_dump_json().encode()).hexdigest()=="869b084ca26842f237248868606deb28fd929cd659a50d7c225274279e6cf8dd"
