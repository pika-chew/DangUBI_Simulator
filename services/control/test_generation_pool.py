from pathlib import Path
from types import SimpleNamespace
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import pytest
from services.control.generation_pool import GenerationPool,owner_for
from services.control.bounded_schedule import BoundedSchedule
from services.control.generation_session import GenerationFailure
from services.control.execution_checkpoints import ExecutionCheckpoints
from services.control.schedule_cursor import provider_for
from services.control.schedule import ScheduleRequest,plan


def test_cursor_matches_sorted_preview_across_day_edges():
    req=ScheduleRequest(mode="patterned",measurement={"start":"2026-11-17T00:00:00+09:00","end":"2026-11-20T00:00:00+09:00"},vehicles=[{"vehicle_id":"v000001","pattern":{"weekdays":[1,2,3,4,5,6,7],"day_off_probability":0,"baseline_jitter_minutes":0,"slots":[{"start_time":"23:59","duration_min_minutes":1,"duration_max_minutes":1,"departure_jitter_minutes":120,"road_profile":"urban"}]}}])
    expected=plan(req)["trips"]
    source=SimpleNamespace(fleet=None,schedule=req)
    assert list(provider_for(source)("v000001"))==expected


def test_execution_checkpoint_cache_is_bounded_and_independent():
    c=ExecutionCheckpoints()
    state=dict(lat=37.5,lon=127,odometer_m=100,coolant_c=80,heading_rad=0)
    for i in range(300):c.put("scope",i,i*1000,state,0)
    assert c.stats()["entries"]==256
    assert c.resume("scope",300)[1]==(299,299000,state)
    assert c.resume("scope",0)==(0,None)
    copy=c.resume("scope",300)[1][2];copy["odometer_m"]=999
    assert c.resume("scope",300)[1][2]==state


class FakePool:
    def __init__(self,count,job,fail=False):
        self.sessions=[None]*count;self.job=job;self.cancelled=threading.Event();self.active=set();self.ex=ThreadPoolExecutor(max_workers=count);self.fail=fail
    def submit(self,owner,call):
        f=self.ex.submit(call,None);self.active.add(f);return f
    def release(self,f):self.active.discard(f)
    def close(self):self.cancelled.set();self.ex.shutdown(wait=True)


def test_bounded_queue_waits_for_slow_owner_and_preserves_all_messages():
    for workers in [1,2,4]:
        job=SimpleNamespace(stop_requested=False,pause_requested=False)
        pool=FakePool(workers,job)
        start=1794960000000
        def provide(v):
            yield dict(vehicle_id=v,trip_key=v,start_ms=start,end_ms=start+10000,observed_start_ms=start,observed_end_ms=start+10000)
            # 전체 운행 목록 대신 다음 운행만 순차로 준비한다.
            yield dict(vehicle_id=v,trip_key=v+"next",start_ms=start+1800000,end_ms=start+1810000,observed_start_ms=start+1800000,observed_end_ms=start+1810000)
        def generate(v,key,offset,session,limit):
            if v=="v000001":time.sleep(.02)
            origin=start+(1800000 if key.endswith("next") else 0)
            msgs=[dict(ty=4,ts=origin+i*1000,pld={"sp":i,"padding":"x"*2000}) for i in range(offset,min(5,offset+limit))]
            return dict(status="generated_local_only",messages=msgs,has_more=offset+len(msgs)<5,trip=None,synthetic_trip=False)
        data=dict(assignments=[{"vehicle_id":f"v{i:06d}"} for i in range(1,5)],trip_provider=provide,generate=generate,config={"interval_ms":1000},measurement_start_ms=start,realtime=False,generation_queue_messages=32,generation_queue_bytes=262144)
        result={};q=BoundedSchedule(data,pool,lambda due=None:True,result)
        try:rows=list(q)
        finally:pool.close()
        assert len(rows)==40 and [(r[0],r[1]) for r in rows]==sorted((r[0],r[1]) for r in rows)
        assert len({(r[0],r[1]) for r in rows})==40
        assert q.stats["peak_reserved_messages"]<=32 and q.stats["peak_reserved_bytes"]<=262144
        assert q.stats["queued_messages"]==q.stats["queued_bytes"]==0


def test_real_workers_match_every_legacy_message_and_clean_up():
    import os
    from services.control.schedule_obd import ScheduledOBDRequest,generate
    worker=Path(__file__).resolve().parents[2]/".local"/("worker.exe" if os.name=="nt" else "worker")
    req=ScheduleRequest(mode="manual",measurement={"start":"2026-11-17T13:00:00+09:00","end":"2026-11-17T13:34:00+09:00"},vehicles=[{"vehicle_id":f"v{i:06d}","trips":[{"start":"2026-11-17T13:00:00+09:00","end":"2026-11-17T13:02:00+09:00","road_profile":"mixed"},{"start":"2026-11-17T13:32:00+09:00","end":"2026-11-17T13:34:00+09:00","road_profile":"urban"}]} for i in range(1,9)])
    provider=provider_for(SimpleNamespace(fleet=None,schedule=req))
    expected=[];old_cache=ExecutionCheckpoints()
    for vehicle in req.vehicles:
        for trip in provider(vehicle.vehicle_id):
            out=generate(ScheduledOBDRequest(schedule=req,checkpoint_epoch=0),vehicle.vehicle_id,trip["trip_key"],worker,None,None,0,100,checkpoint_store=old_cache)
            for m in out["messages"]:expected.append((m["ts"],vehicle.vehicle_id,m,False))
            expected.append((out["trip"]["ts"],vehicle.vehicle_id,out["trip"],False))
    expected.sort(key=lambda row:(row[0],row[1]))
    for workers in [1,2,4]:
        checkpoints=ExecutionCheckpoints();job=SimpleNamespace(stop_requested=False,pause_requested=False,processes=[])
        manager=SimpleNamespace(lock=threading.RLock())
        pool=GenerationPool(worker,job,manager,workers)
        def page(vehicle,key,offset,session,limit):
            return generate(ScheduledOBDRequest(schedule=req,checkpoint_epoch=0),vehicle,key,worker,None,None,offset,limit,checkpoint_store=checkpoints,session=session)
        data=dict(assignments=[{"vehicle_id":v.vehicle_id} for v in req.vehicles],trip_provider=provider,generate=page,config={"interval_ms":2000},measurement_start_ms=1794888000000,realtime=False,generation_queue_messages=256,generation_queue_bytes=4194304)
        try:
            queue=BoundedSchedule(data,pool,lambda due=None:True,{})
            observed=list(queue)
            assert observed==expected
            assert len(observed)==976
            assert checkpoints.stats()["entries"]<=256
        finally:pool.close(abort=False)
        assert pool.cleaned and pool.close_acknowledged and all(p.poll() is not None for p in job.processes)
        if workers>1:assert pool.peak_active_workers>1


def test_worker_failure_stops_all_processes():
    import os
    worker=Path(__file__).resolve().parents[2]/".local"/("worker.exe" if os.name=="nt" else "worker")
    job=SimpleNamespace(stop_requested=False,pause_requested=False,processes=[])
    pool=GenerationPool(worker,job,SimpleNamespace(lock=threading.RLock()),4)
    f=pool.submit(0,lambda session:session.page({"limit":101}))
    try:
        with pytest.raises(GenerationFailure):f.result(timeout=5)
    finally:pool.close(abort=True)
    assert pool.cleaned and all(p.poll() is not None for p in job.processes)


def test_bounded_mqtt_boundaries_merge_touching_trips_and_order_equal_timestamps():
    job=SimpleNamespace(stop_requested=False,pause_requested=False)
    pool=FakePool(2,job)
    rows={"a":[(0,200,0,200),(200,400,200,400)],"b":[(100,200,100,200)],"c":[(0,10000,25,75)]}
    def provide(vehicle):
        for i,(a,b,c,d) in enumerate(rows[vehicle]):
            yield dict(trip_key=f"{vehicle}-{i}",start_ms=a,end_ms=b,observed_start_ms=c,observed_end_ms=d)
    def generate(vehicle,key,offset,session,limit):
        a,b,c,d=rows[vehicle][int(key.split("-")[1])]
        all_messages=[dict(ty=4,ts=t,pld={}) for t in range(a,b,100) if c<=t<d]
        page=all_messages[offset:offset+limit];more=offset+len(page)<len(all_messages)
        return dict(status="generated",messages=page,has_more=more,
                    trip=dict(ty=3,ts=b,pld={}) if not more and d==b else None,synthetic_trip=False)
    data=dict(assignments=[{"vehicle_id":v} for v in rows],trip_provider=provide,generate=generate,
              config={"interval_ms":100},measurement_start_ms=0,realtime=False,
              generation_queue_messages=32,generation_queue_bytes=262144)
    q=BoundedSchedule(data,pool,lambda due=None:True,{},include_boundaries=True)
    try:events=list(q)
    finally:pool.close();q.close()
    assert [(t,p,v) for t,p,v,_,_ in events]==sorted((t,p,v) for t,p,v,_,_ in events)
    assert [(t,p,v) for t,p,v,_,_ in events if p!=1]==[(0,0,"a"),(25,0,"c"),(75,2,"c"),(100,0,"b"),(200,2,"b"),(400,2,"a")]
    assert [(m["ts"],m["ty"]) for _,p,v,m,_ in events if p==1 and v=="a"]==[(0,4),(100,4),(200,3),(200,4),(300,4),(400,3)]
    assert q.stats["reserved_bytes"]==q.stats["reserved_messages"]==0


def test_thousand_vehicle_queue_with_large_pages_keeps_headroom():
    pool=FakePool(4,SimpleNamespace(stop_requested=False,pause_requested=False))
    def provide(v):yield dict(trip_key=v,start_ms=0,end_ms=2000,observed_start_ms=0,observed_end_ms=2000)
    def generate(v,key,offset,session,limit):
        return dict(status="generated",messages=[dict(ty=4,ts=offset*1000,pld={"padding":"x"*14000})],has_more=offset==0,trip=None)
    data=dict(assignments=[dict(vehicle_id=f"v{i:06d}") for i in range(1000)],trip_provider=provide,generate=generate,
              config={"interval_ms":1000},measurement_start_ms=0,realtime=False)
    q=BoundedSchedule(data,pool,lambda due=None:True,{})
    try:
        keys=[(t,v) for t,v,_,_ in q]
        assert len(keys)==2000 and keys==sorted(keys)
        assert q.stats["peak_reserved_bytes"]<=8388608 and q.stats["peak_reserved_messages"]<=2000
    finally:pool.close();q.close()
