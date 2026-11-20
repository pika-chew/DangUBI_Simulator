"""묶음 크기·Worker 수와 무관한 원문/종료 경계·순서·예산을 검증한다."""
import os
from pathlib import Path
from types import SimpleNamespace
import threading
import time

import pytest
from services.control.bounded_schedule import BoundedSchedule
from services.control.fleet import FleetRequest
from services.control.fleet_execution import FleetExecution
from services.control.generation_pool import GenerationPool
from services.control.generation_session import GenerationSession
from services.control.test_generation_pool import FakePool
from services.control import fleet_execution
from services.control.runtime import RuntimePaths

WORKER=Path(os.environ.get("DANGUBI_TEST_WORKER",RuntimePaths.from_env(Path(__file__).resolve().parents[2]).worker))
REFERENCE=Path(os.environ.get("DANGUBI_REFERENCE_WORKER",WORKER))


@pytest.mark.parametrize("workers",[1,2,4])
@pytest.mark.parametrize("force",[False,True])
def test_batch_matches_individual_messages_and_final_state(monkeypatch,workers,force):
    req=FleetRequest(execution_profile="operations",vehicle_count=8,interval_ms=70000,
        measurement=dict(start="2026-11-19T08:00:00+09:00",end="2026-11-19T08:34:30+09:00"))
    def trips(req,vehicle,fixed):
        yield dict(start="2026-11-19T08:00:00+09:00",end="2026-11-19T08:03:00+09:00",road_profile="mixed")
        yield dict(start="2026-11-19T08:33:00+09:00",end="2026-11-19T08:36:00+09:00",road_profile="highway")
    monkeypatch.setattr(fleet_execution,"iter_vehicle_days",trips)
    reference=FleetExecution(req,REFERENCE,force)
    expected=[]
    session=GenerationSession(REFERENCE)
    try:
        for vehicle in reference.assigned:
            for trip in reference.trips(vehicle):
                offset=0
                while True:
                    out=reference.generate(vehicle,trip["trip_key"],offset,session,1)
                    expected.extend((m["ts"],vehicle,m,False) for m in out["messages"])
                    offset+=len(out["messages"])
                    if not out["has_more"]:
                        if out["trip"]:expected.append((out["trip"]["ts"],vehicle,out["trip"],out["synthetic_trip"]))
                        break
    finally:session.close()
    stream=FleetExecution(req,WORKER,force)
    job=SimpleNamespace(stop_requested=False,pause_requested=False,processes=[])
    pool=GenerationPool(WORKER,job,SimpleNamespace(lock=threading.RLock()),workers)
    data=dict(execution_profile="operations",assignments=[dict(vehicle_id=v) for v in stream.assigned],
        trip_provider=stream.trips,generate=stream.generate,generate_batch=stream.generate_many,
        config=dict(interval_ms=req.interval_ms),measurement_start_ms=stream.start,realtime=False,
        generation_queue_messages=18,generation_queue_bytes=4194304)
    queue=BoundedSchedule(data,pool,lambda due=None:True,{})
    try:
        actual=list(queue)
        assert actual==sorted(expected,key=lambda r:(r[0],r[1]))
        assert {k:v["final"] for k,v in stream.cursors.items()}=={k:v["final"] for k,v in reference.cursors.items()}
        assert pool.stats["batch_requests"]>0
        assert queue.stats["peak_reserved_messages"]<=18
        assert queue.stats["peak_reserved_bytes"]<=4194304
    finally:
        queue.close();pool.close()
    assert pool.cleaned and pool.close_acknowledged


def test_batch_reserves_head_room_and_finishes_with_no_queued_messages():
    job=SimpleNamespace(stop_requested=False,pause_requested=False)
    pool=FakePool(4,job)
    def trips(v):yield dict(trip_key=v,start_ms=0,end_ms=2500,observed_start_ms=0,observed_end_ms=2500)
    def generate(v,key,offset,session=None,limit=1):
        messages=[dict(ty=4,ts=offset*2000,pld={})] if offset<2 else []
        return dict(status="generated_local_only",messages=messages,has_more=offset<1,
            trip=dict(ty=3,ts=2500,pld={}),synthetic_trip=False)
    def batch(requests,session):
        if any(v=="v000000" for v,_,_,_ in requests):time.sleep(.01)
        return [generate(v,k,o) for v,k,o,_ in requests]
    data=dict(execution_profile="operations",assignments=[dict(vehicle_id=f"v{i:06d}") for i in range(80)],
        trip_provider=trips,generate=generate,generate_batch=batch,config=dict(interval_ms=2000),
        measurement_start_ms=0,realtime=False,generation_queue_messages=18,generation_queue_bytes=1048576)
    queue=BoundedSchedule(data,pool,lambda due=None:not job.stop_requested,{})
    try:
        rows=list(queue)
        assert len(rows)==240
        assert [(r[0],r[1]) for r in rows]==sorted((r[0],r[1]) for r in rows)
        assert queue.stats["peak_reserved_messages"]<=18
        assert queue.stats["peak_reserved_bytes"]<=1048576
    finally:queue.close();pool.close()
    assert queue.stats["reserved_messages"]==queue.stats["reserved_bytes"]==0


def test_stop_during_batch_releases_reservations_and_emits_nothing():
    job=SimpleNamespace(stop_requested=False,pause_requested=False)
    pool=FakePool(1,job)
    def trips(v):yield dict(trip_key=v,start_ms=0,end_ms=2000,observed_start_ms=0,observed_end_ms=2000)
    def batch(requests,session):
        job.stop_requested=True
        return [dict(status="generated_local_only",messages=[dict(ty=4,ts=0,pld={})],has_more=False,
                     trip=None,synthetic_trip=False) for _ in requests]
    data=dict(execution_profile="operations",assignments=[dict(vehicle_id=f"v{i:06d}") for i in range(8)],
        trip_provider=trips,generate=lambda *a:None,generate_batch=batch,config=dict(interval_ms=2000),
        measurement_start_ms=0,realtime=False,generation_queue_messages=18,generation_queue_bytes=1048576)
    queue=BoundedSchedule(data,pool,lambda due=None:not job.stop_requested,{})
    try:
        assert list(queue)==[]
    finally:
        queue.close();pool.close()
    assert queue.stats["reserved_messages"]==queue.stats["reserved_bytes"]==0
