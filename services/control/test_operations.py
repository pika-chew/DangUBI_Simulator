import json
import os
from pathlib import Path
from types import SimpleNamespace
import pytest
from services.control.fleet import FleetRequest, FleetOBDRequest, preview, obd
from services.control.fleet_execution import FleetExecution
from services.control.generation_session import GenerationSession
from services.control.checkpoints import CheckpointStore
from services.control.bounded_schedule import BoundedSchedule
from services.control.test_generation_pool import FakePool
from services.control.test_app import client, control, wait_job
from services.control.test_schedule_mqtt import mqtt_client, fixture_binary, finished

ROOT=Path(__file__).resolve().parents[2]


def fleet(**patch):
    body=json.loads((ROOT/"configs/fleet-bounded.example.json").read_text(encoding="utf-8"))
    body.update(execution_profile="operations",vehicle_count=3,trip_min_minutes=3,trip_max_minutes=3,
                interval_ms=60000,day_off_probability=0,weekend_activity_probability=1,
                measurement=dict(start="2026-11-16T00:00:00+09:00",end="2026-11-19T00:00:00+09:00",timezone="Asia/Seoul"))
    body.update(patch)
    return FleetRequest(**body)


def test_operating_bounds_and_small_test_remain_separate():
    assert fleet(vehicle_count=25000).vehicle_count==25000
    with pytest.raises(ValueError):fleet(vehicle_count=25001)
    assert fleet(vehicle_count=1000,execution_profile="small_test").vehicle_count==1000
    with pytest.raises(ValueError):fleet(vehicle_count=1001,execution_profile="small_test")
    with pytest.raises(ValueError):fleet(measurement=dict(start="2026-11-01T00:00:00+09:00",end="2026-12-02T00:00:00+09:00"))
    p=json.loads((ROOT/"configs/mqtt.operations.example.json").read_text())
    profile=control.MQTTProfile(**p)
    assert profile.connect_per_second==1000 and profile.max_clients==25000 and not profile.enabled
    p["execution_profile"]="small_test"
    with pytest.raises(ValueError):control.MQTTProfile(**p)


def test_operating_server_does_not_lift_small_mqtt_limits(tmp_path,monkeypatch):
    p=json.loads((ROOT/"configs/mqtt.operations.example.json").read_text())
    p.update(enabled=True,contract_reviewed=True)
    path=tmp_path/'mqtt.json';path.write_text(json.dumps(p))
    monkeypatch.setattr(control,'MQTT_PROFILE',path)
    small=control.mqtt_profile('small_test')
    assert small['execution_profile']=='small_test'
    assert (small['max_clients'],small['max_messages'],small['max_duration_seconds'])==(20,100,30)
    assert (small['connect_per_second'],small['publish_per_second'])==(5,70)
    assert control.mqtt_profile('operations')['connect_per_second']==1000


def test_streaming_plan_matches_previous_model_and_selected_scope():
    req=fleet()
    old=req.model_copy(update={"execution_profile":"small_test"})
    expected=preview(old)
    actual=preview(req)
    for key in ("vehicles","microtrip_count","trip_count","planned_messages","trips","observed_trip_count"):
        assert actual[key]==expected[key],key
    page=preview(req,offset=2,limit=3)
    assert page["trips"]==expected["trips"][2:5] and page["matched_trip_count"]==len(expected["trips"])
    selected=preview(req,vehicle_id="v000003")
    assert selected["summary_scope"]=="selected_vehicle" and selected["fleet_vehicle_count"]==3
    assert selected["trips"]==[r for r in expected["trips"] if r["vehicle_id"]=="v000003"]


@pytest.mark.parametrize("interval",[60000,70000])
def test_sequential_real_obd_matches_replay_across_trips_and_pages(tmp_path,interval):
    req=fleet(interval_ms=interval)
    worker=control.WORKER
    stream=FleetExecution(req,worker)
    cache=CheckpointStore(tmp_path/"reference.sqlite3")
    with_session=GenerationSession(worker)
    try:
        for vehicle in stream.assigned:
            for trip in stream.trips(vehicle):
                offset=0
                while True:
                    actual=stream.generate(vehicle,trip["trip_key"],offset,with_session,2)
                    if actual["status"]=="preparing":continue
                    reference=obd(FleetOBDRequest(fleet=req),vehicle,trip["trip_key"],worker,None,None,offset,2,checkpoint_store=cache)
                    for key in ("messages","initial_state","has_more","synthetic_trip"):
                        assert actual[key]==reference[key],(vehicle,key)
                    assert actual["trip"]==(None if actual["has_more"] else reference["trip"])
                    offset+=len(actual["messages"])
                    if not actual["has_more"]:break
        assert stream.stats()["entries"]==3 and stream.stats()["allocation_builds"]==1
    finally:with_session.close()


def test_25000_simultaneous_vehicles_do_not_require_25000_message_slots():
    start=1794960000000
    def provide(vehicle):
        yield dict(trip_key=vehicle,start_ms=start,end_ms=start+2000,observed_start_ms=start,observed_end_ms=start+2000)
    def generate(vehicle,key,offset,session,limit):
        return dict(status="generated_local_only",messages=[dict(ty=4,ts=start,pld={})] if offset==0 else [],
                    has_more=False,trip=dict(ty=3,ts=start+2000,pld={}),synthetic_trip=False)
    pool=FakePool(4,SimpleNamespace(stop_requested=False,pause_requested=False))
    queue=None
    try:
        data=dict(execution_profile="operations",assignments=[dict(vehicle_id=f"v{i:06d}") for i in range(25000)],
                  trip_provider=provide,generate=generate,config=dict(interval_ms=2000),measurement_start_ms=start,realtime=False,
                  generation_queue_messages=256,generation_queue_bytes=4194304)
        queue=BoundedSchedule(data,pool,lambda due=None:True,{})
        previous=None;counts={3:0,4:0}
        for ts,vehicle,message,_ in queue:
            key=(ts,vehicle)
            assert previous is None or key>=previous
            previous=key;counts[message["ty"]]+=1
        assert counts=={3:25000,4:25000}
        assert queue.stats["peak_reserved_messages"]<=256 and queue.stats["peak_reserved_bytes"]<=4194304
    finally:
        pool.close()
        if queue:queue.close()


def test_operating_api_starts_long_plan_and_freezes_settings(client):
    req=fleet(vehicle_count=2)
    created=client.post('/api/workspace/plans',json=dict(title="운영 일정",fleet=req.model_dump()))
    assert created.status_code==201,created.text
    plan=created.json()
    started=client.post('/api/workspace/execute',json=dict(plan_key=plan['key'],realtime=False))
    assert started.status_code==202,started.text
    run=wait_job(client,started.json()['id'],lambda r:r['job_state'] not in control.ACTIVE)
    assert run['result']['job_state']=='completed',run['result']
    assert run['result']['processed_messages']==plan['planned_messages']
    assert run['request']['execution_profile']=='operations' and run['request']['config']['client_count']==2
    assert run['result']['generation_pool']['execution_checkpoints']['allocation_builds']==1
    assert client.patch('/api/runs/'+run['id'],json={"client_count":1}).status_code==409


def test_server_profile_is_required_before_operating_mqtt(client):
    req=fleet(vehicle_count=1,day_off_probability=1)
    plan=client.post('/api/workspace/plans',json=dict(title="운영 설정 확인",fleet=req.model_dump())).json()
    response=client.post('/api/workspace/execute',json=dict(plan_key=plan['key'],transport='mqtt',confirm_mqtt=True))
    assert response.status_code==409


def test_operating_profile_actual_mtls_and_large_initial_assignment_frame(mqtt_client):
    from datetime import datetime,timezone,timedelta
    from services.control.mqtt_bridge import MQTTBridge
    client,profile_path=mqtt_client
    profile=json.loads(profile_path.read_text())
    profile.update(execution_profile="operations",max_connecting=2,connect_per_second=1000,publish_per_second=15000,max_clients=500)
    profile_path.write_text(json.dumps(profile))
    req=fleet(vehicle_count=1,interval_ms=2000)
    first=preview(req)["trips"][0]["start_ms"]
    req.measurement.start=datetime.fromtimestamp(first/1000,tz=timezone(timedelta(hours=9))).isoformat()
    req.measurement.end=datetime.fromtimestamp((first+4000)/1000,tz=timezone(timedelta(hours=9))).isoformat()
    plan=client.post('/api/workspace/plans',json=dict(title="운영 mTLS",fleet=req.model_dump())).json()
    response=client.post('/api/workspace/execute',json=dict(plan_key=plan['key'],transport='mqtt',confirm_mqtt=True))
    assert response.status_code==202,response.text
    result=finished(client,response.json()['id'])['result']
    assert result['job_state']=='completed',result
    assert result['puback_count']==result['planned_messages']>0
    assert result['generation_pool']['execution_checkpoints']['allocation_builds']==1
    assert result['transmission']['queue_byte_capacity']==8388608
    assert result['transmission_goal']['status'] in {'passed','exceeded'}

    # 실제 접속은 하지 않고 25,000대 배정의 초기 프레임과 종료 응답만 왕복한다.
    assignments=[dict(vehicle_id=f"v{i:06d}",client_id=f"sim-large-assignment-frame-c{i%500:03d}") for i in range(25000)]
    assert len(json.dumps(assignments).encode())>1048576
    bridge=MQTTBridge(control.WORKER,dict(mqtt=profile,assignments=assignments),SimpleNamespace(id="large-frame-check",stop_requested=False))
    try:
        bridge.receive()
        assert bridge.stats['active_clients']==0
    finally:bridge.close()
    assert bridge.cleaned and bridge.close_acknowledged


def test_operation_defers_trip_at_actual_end_not_next_sampling_tick():
    origin=1794960000000
    def provide(v):yield dict(trip_key=v,start_ms=origin,end_ms=origin+180000,observed_start_ms=origin,observed_end_ms=origin+180000)
    def generate(v,key,offset,session,limit):
        messages=[dict(ty=4,ts=origin+70000*offset,pld={})] if offset<3 else []
        return dict(status="generated_local_only",messages=messages,has_more=offset+len(messages)<3,
                    trip=dict(ty=3,ts=origin+180000,pld={}) if offset+len(messages)>=3 else None,synthetic_trip=False)
    pool=FakePool(2,SimpleNamespace(stop_requested=False,pause_requested=False))
    try:
        q=BoundedSchedule(dict(execution_profile="operations",assignments=[dict(vehicle_id=v) for v in ["a","b"]],
            trip_provider=provide,generate=generate,measurement_start_ms=origin,config=dict(interval_ms=70000),
            realtime=False,generation_queue_messages=16,generation_queue_bytes=262144),pool,lambda due=None:True,{})
        rows=list(q)
        assert [r[0]-origin for r in rows]==[0,0,70000,70000,140000,140000,180000,180000]
        assert sum(r[2]["ty"]==3 for r in rows)==2
    finally:pool.close()


def test_large_api_assignment_uses_entire_roster_not_display_page(client,monkeypatch):
    req=fleet(vehicle_count=25000,day_off_probability=1,
              measurement=dict(start="2026-11-16T00:00:00+09:00",end="2026-11-16T00:00:01+09:00",timezone="Asia/Seoul"))
    plan=client.post('/api/workspace/plans',json=dict(title="전체 차량 배정",fleet=req.model_dump())).json()
    assert plan['vehicle_count']==25000 and len(plan['result']['vehicles'])==100
    captured={}
    def capture(run_id,request,data,*args,**kwargs):
        captured.update(data)
        return dict(id=run_id,request=request)
    monkeypatch.setattr(control.JOBS,'start',capture)
    response=client.post('/api/workspace/execute',json=dict(plan_key=plan['key'],realtime=False))
    assert response.status_code==202,response.text
    assert len(captured['assignments'])==25000
    assert len({a['client_id'] for a in captured['assignments']})==captured['config']['client_count']==500
    assert captured['execution_profile']=='operations'


def test_thirty_day_idle_execution_can_be_stopped_without_waiting(client):
    req=fleet(vehicle_count=1,day_off_probability=1,
              measurement=dict(start="2026-11-01T00:00:00+09:00",end="2026-12-01T00:00:00+09:00",timezone="Asia/Seoul"))
    plan=client.post('/api/workspace/plans',json=dict(title="30일 중단 경계",fleet=req.model_dump())).json()
    response=client.post('/api/workspace/execute',json=dict(plan_key=plan['key'],realtime=True))
    assert response.status_code==202,response.text
    run_id=response.json()['id']
    client.post('/api/jobs/'+run_id+'/stop')
    run=wait_job(client,run_id,lambda r:r['job_state'] not in control.ACTIVE)
    assert run['result']['job_state']=='stopped',run['result']
    assert not run['result']['forced_termination']


@pytest.mark.parametrize("force",[False,True])
def test_operating_observation_end_preserves_trip_policy(client,force):
    from datetime import datetime,timezone,timedelta
    req=fleet(vehicle_count=1,interval_ms=700)
    first=preview(req)["trips"][0]["start_ms"]
    req.measurement.start=datetime.fromtimestamp(first/1000,tz=timezone(timedelta(hours=9))).isoformat()
    req.measurement.end=datetime.fromtimestamp((first+4000)/1000,tz=timezone(timedelta(hours=9))).isoformat()
    plan=client.post('/api/workspace/plans',json=dict(title="관측 끝 경계",fleet=req.model_dump())).json()
    started=client.post('/api/workspace/execute',json=dict(plan_key=plan['key'],realtime=False,force_end_trip=force))
    assert started.status_code==202,started.text
    run=wait_job(client,started.json()['id'],lambda r:r['job_state'] not in control.ACTIVE)
    result=run['result']
    assert result['job_state']=='completed',result
    assert result['processed_messages']==result['planned_messages']
    assert result['trip_count']==int(force)
    assert sum(s['message']['ty']==3 for s in result['samples'])==int(force)
