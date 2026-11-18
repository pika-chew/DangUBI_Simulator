"""일회성 로컬 mTLS 브로커까지의 일정 전송 시험. AWS에 접속하지 않는다."""
import json
import os
from pathlib import Path
import subprocess
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from services.control import app as control

ROOT=Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def fixture_binary():
    env=os.environ.copy()
    env.update(GOCACHE=str(ROOT/".local/go-cache"),GOMODCACHE=str(ROOT/".local/go-mod"))
    target=ROOT/".local/schedule-mqtt-fixture.exe"
    subprocess.run(["go","test","-c","-o",str(target),"."],cwd=ROOT/"services/worker",env=env,check=True,timeout=120)
    return target


@pytest.fixture
def mqtt_client(tmp_path,monkeypatch,fixture_binary,request):
    profile=tmp_path/"profile.json"
    env=os.environ.copy()
    fault=getattr(request,"param","none")
    env.update(DANGUBI_SCHEDULE_PROFILE=str(profile),DANGUBI_SCHEDULE_FAULT=fault)
    proc=subprocess.Popen([str(fixture_binary),"-test.run=^TestScheduleMQTTFixture$"],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        for _ in range(300):
            if profile.is_file():break
            if proc.poll() is not None:raise AssertionError("로컬 MQTT fixture 종료")
            time.sleep(.02)
        assert profile.is_file()
        monkeypatch.setattr(control,"DB",tmp_path/"runs.sqlite3")
        monkeypatch.setattr(control,"CHECKPOINTS",control.CheckpointStore(tmp_path/"states.sqlite3"))
        monkeypatch.setattr(control,"MQTT_PROFILE",profile)
        with TestClient(control.app) as client:
            yield client,profile
    finally:
        Path(str(profile)+".done").touch()
        try:proc.wait(timeout=5)
        except subprocess.TimeoutExpired:proc.kill();proc.wait()


def schedule(vehicles=None,end="08",interval=2000):
    def trip(start,finish):return {"start":f"2026-11-18T13:00:{start}+09:00","end":f"2026-11-18T13:00:{finish}+09:00","road_profile":"urban"}
    return {"schedule_version":"schedule-plan-1","mode":"manual","seed":42,"interval_ms":interval,
            "measurement":{"start":"2026-11-18T13:00:00+09:00","end":f"2026-11-18T13:00:{end}+09:00","timezone":"Asia/Seoul"},
            "vehicles":vehicles or [{"vehicle_id":"v000001","trips":[trip("00","02")]},{"vehicle_id":"v000002","trips":[trip("01","03")]},{"vehicle_id":"v000003","trips":[trip("05","07")]}]}


def start(client,source,**options):
    plan=client.post("/api/workspace/plans",json={"title":"MQTT 일정 시험","schedule":source}).json()
    response=client.post("/api/workspace/execute",json={"plan_key":plan["key"],"transport":"mqtt","confirm_mqtt":True,**options})
    assert response.status_code==202,response.text
    return response.json()["id"],plan


def finished(client,run_id):
    for _ in range(800):
        run=client.get("/api/jobs/"+run_id).json()
        if run["result"]["job_state"] not in control.ACTIVE:return run
        time.sleep(.02)
    raise AssertionError("MQTT 실행 종료 대기 초과")


def broker_report(profile):
    Path(str(profile)+".done").touch()
    report=Path(str(profile)+".report")
    for _ in range(200):
        if report.is_file():return json.loads(report.read_text())
        time.sleep(.02)
    raise AssertionError("브로커 결과 대기 초과")


@pytest.mark.parametrize("workers",[1,2,4])
@pytest.mark.parametrize("mode,lifetime",[("shared","always"),("shared","while_driving"),("dedicated","always"),("dedicated","while_driving")])
def test_schedule_mqtt_mapping_lifetimes_and_payload(mqtt_client,mode,lifetime,workers):
    client,profile=mqtt_client
    source=schedule()
    run_id,plan=start(client,source,mapping_mode=mode,connection_lifetime=lifetime,generation_workers=workers)
    run=finished(client,run_id);r=run["result"]
    assert r["job_state"]=="completed",r
    assert r["processed_messages"]==r["planned_messages"]==r["mqtt_published"]==r["puback_count"]==6
    assert r["unconfirmed_messages"]==r["active_clients"]==0
    assert r["connection_cleanup_confirmed"] and r["reconnect_count"]==0
    assert r["connected_clients"]==(1 if mode=="shared" else 3)
    assert r["connection_count"]==(2 if mode=="shared" and lifetime=="while_driving" else 1 if mode=="shared" else 3)
    assert r["disconnect_count"]==r["connection_count"]
    assert r["generation_session"]["worker_count"]==r["generation_session"]["process_starts"]==workers
    assert r["generation_session"]["cleanup_confirmed"] and r["generation_session"]["close_acknowledged"] and r["execution_cleanup_confirmed"]
    budget=r["buffer_budget"]
    assert budget["reserved_messages"]==budget["reserved_bytes"]==0
    assert budget["peak_reserved_messages"]<=budget["max_messages"] and budget["peak_reserved_bytes"]<=budget["max_bytes"]
    assert budget["peak_reserved_bytes"]>0
    assert r["generation_pool"]["queue"]["reserved_bytes"]==r["generation_pool"]["queue"]["reserved_messages"]==0
    assert client.get("/api/jobs/"+run_id).json()["request"]["generation"]["workers"]==workers
    tx=r["transmission"]
    assert tx["mode"]=="parallel" and tx["per_client_in_flight"]==1
    assert tx["in_flight"]==tx["queued_messages"]==tx["reserved_bytes"]==tx["reserved_commands"]==0
    assert tx["peak_in_flight"]<=r["allocated_clients"]
    assert tx["peak_reserved_bytes"]<=tx["queue_byte_capacity"] and tx["peak_reserved_commands"]<=tx["queue_capacity"]
    expected=[]
    for row in client.post("/api/schedules/preview",json=source).json()["trips"]:
        output=client.post(f'/api/schedules/obd/{row["vehicle_id"]}/{row["trip_key"]}',json={"schedule":source}).json()
        expected.extend((m["ts"],row["vehicle_id"],m) for m in output["messages"]+[output["trip"]])
    actual=[(s["message"]["ts"],s["vehicle_id"],s["message"]) for s in r["samples"]]
    assert actual==sorted(expected,key=lambda x:(x[0],x[1]))
    assert all(s["topic"]==f'sim-test/{run_id}/{s["vehicle_id"]}/obd' for s in r["samples"])
    assert "cert_file" not in json.dumps(run) and str(profile) not in json.dumps(run)
    assert client.get("/api/workspace/records/"+plan["key"]).json()==plan


@pytest.mark.parametrize("mqtt_client",["no_ack"],indirect=True)
def test_schedule_mqtt_no_ack_stops_without_replay(mqtt_client):
    client,_=mqtt_client
    run_id,_=start(client,schedule())
    r=finished(client,run_id)["result"]
    assert r["job_state"]=="failed" and r["state"]=="mqtt_failed",r
    assert r["publish_attempts"]==r["unconfirmed_messages"]==1 and r["puback_count"]==0
    assert r["active_clients"]==0 and r["connection_cleanup_confirmed"]
    assert control.JOBS.active_id() is None


@pytest.mark.parametrize("mqtt_client",["disconnect_once"],indirect=True)
def test_schedule_mqtt_reconnects_same_client(mqtt_client):
    client,_=mqtt_client
    source=schedule([{"vehicle_id":"v000001","trips":[{"start":"2026-11-18T13:00:00+09:00","end":"2026-11-18T13:00:06+09:00","road_profile":"urban"}]}])
    run_id,_=start(client,source)
    r=finished(client,run_id)["result"]
    assert r["job_state"]=="completed",r
    assert r["reconnect_count"]>=1 and r["connected_clients"]==1 and r["puback_count"]==4


@pytest.mark.parametrize("mqtt_client",["delay_ack"],indirect=True)
def test_schedule_mqtt_slow_ack_backpressure(mqtt_client):
    client,_=mqtt_client
    source=schedule([{"vehicle_id":"v000001","trips":[{"start":"2026-11-18T13:00:00+09:00","end":"2026-11-18T13:00:01+09:00","road_profile":"urban"}]}],end="02",interval=100)
    run_id,_=start(client,source)
    r=finished(client,run_id)["result"]
    assert r["job_state"]=="completed",r
    assert r["puback_count"]==11 and r["transmission"]["peak_in_flight"]==1
    assert r["transmission"]["peak_queued"]>0 and r["schedule_delay_ms"]>100


def test_schedule_mqtt_rejects_virtual_synthetic_and_missing_confirmation(mqtt_client):
    client,_=mqtt_client
    plan=client.post("/api/workspace/plans",json={"title":"거절 시험","schedule":schedule()}).json()
    base={"plan_key":plan["key"],"transport":"mqtt"}
    assert client.post("/api/workspace/execute",json=base).status_code==409
    for extra in [{"realtime":False},{"force_end_trip":True}]:
        assert client.post("/api/workspace/execute",json={**base,"confirm_mqtt":True,**extra}).status_code==422
    assert control.JOBS.active_id() is None


@pytest.mark.parametrize("mqtt_client",["no_ack"],indirect=True)
def test_schedule_mqtt_stop_during_ack_wait(mqtt_client):
    client,_=mqtt_client
    run_id,_=start(client,schedule(),generation_workers=4)
    job=control.JOBS.active
    for _ in range(200):
        r=client.get("/api/jobs/"+run_id).json()["result"]
        if r.get("transmission",{}).get("in_flight")==1:break
        time.sleep(.01)
    client.post("/api/jobs/"+run_id+"/stop")
    r=finished(client,run_id)["result"]
    assert r["job_state"]=="stopped",r
    assert r["puback_count"]==0 and r["trip_count"]==0 and r["processed_messages"]<r["planned_messages"]
    assert r["active_clients"]==0 and r["connection_cleanup_confirmed"]
    assert r["execution_cleanup_confirmed"] and len(job.processes)==4 and all(p.poll() is not None for p in job.processes)
    assert control.MQTT_LOCK.acquire(blocking=False)
    control.MQTT_LOCK.release()


def test_schedule_mqtt_pause_resume_preserves_schedule(mqtt_client):
    client,_=mqtt_client
    run_id,_=start(client,schedule(),connection_lifetime="while_driving")
    for _ in range(200):
        r=client.get("/api/jobs/"+run_id).json()["result"]
        if r["puback_count"]>=1:break
        time.sleep(.01)
    client.post("/api/jobs/"+run_id+"/pause")
    for _ in range(200):
        r=client.get("/api/jobs/"+run_id).json()["result"]
        if r["job_state"]=="paused":break
        time.sleep(.01)
    assert r["job_state"]=="paused"
    before=r["puback_count"]
    time.sleep(.25)
    assert client.get("/api/jobs/"+run_id).json()["result"]["puback_count"]==before
    client.post("/api/jobs/"+run_id+"/resume")
    r=finished(client,run_id)["result"]
    assert r["job_state"]=="completed" and r["puback_count"]==6,r
    assert r["paused_ms"]>=200 and r["connection_count"]==2


@pytest.mark.parametrize("mqtt_client",["split_ack"],indirect=True)
@pytest.mark.parametrize("mode,lifetime",[("dedicated","always"),("shared","while_driving")])
def test_schedule_mqtt_slow_client_does_not_block_other_and_global_pacing(mqtt_client,mode,lifetime):
    client,profile=mqtt_client
    source=schedule([{"vehicle_id":vehicle,"trips":[{"start":"2026-11-18T13:00:00+09:00","end":"2026-11-18T13:00:01+09:00","road_profile":"urban"}]}
                     for vehicle in ["v000001","v000002","v000003"]],end="03",interval=200)
    run_id,_=start(client,source,mapping_mode=mode,client_count=2,connection_lifetime=lifetime,generation_workers=4,generation_queue_messages=256,generation_queue_bytes=4194304)
    r=finished(client,run_id)["result"]
    assert r["job_state"]=="completed" and r["puback_count"]==18,r
    assert r["transmission"]["peak_in_flight"]>=2 and r["connection_cleanup_confirmed"]
    assert r["schedule_delay_ms"]>500
    report=broker_report(profile)
    times=[datetime.fromisoformat(t.replace("Z","+00:00")).timestamp() for t in report["received_at"]]
    slow=next(i for i,c in enumerate(report["client_ids"]) if c.endswith("-c001"))
    fast=next(i for i,c in enumerate(report["client_ids"]) if c.endswith("-c002"))
    assert times[fast]<times[slow]+.35,report
    # 전체 10건/초 제한이며 Client별 10건/초로 곱해지지 않는다.
    assert all(b-a>.075 for a,b in zip(times,times[1:])),times
    assert report["received"]==18 and report["unique_clients"]==(3 if mode=="dedicated" else 2)
    for vehicle in ["v000001","v000002","v000003"]:
        received=[m for topic,m in zip(report["topics"],report["messages"]) if topic.endswith(f"/{vehicle}/obd")]
        assert len(received)==6 and [m["ty"] for m in received]==[4]*5+[3]
        assert [m["ts"] for m in received]==sorted(m["ts"] for m in received)
        assert all(set(m)=={"ty","ts","pld"} for m in received)


@pytest.mark.parametrize("mqtt_client",["no_ack"],indirect=True)
def test_schedule_mqtt_parallel_failure_accounts_all_attempts_and_queue(mqtt_client):
    client,profile=mqtt_client
    source=schedule([{"vehicle_id":v,"trips":[{"start":"2026-11-18T13:00:00+09:00","end":"2026-11-18T13:00:01+09:00","road_profile":"urban"}]}
                     for v in ["v000001","v000002"]],end="03",interval=100)
    run_id,_=start(client,source,mapping_mode="dedicated",connection_lifetime="while_driving")
    r=finished(client,run_id)["result"]
    assert r["job_state"]=="failed" and r["connection_cleanup_confirmed"],r
    assert r["publish_attempts"]==r["unconfirmed_messages"]==2 and r["puback_count"]==0
    assert r["processed_messages"]==r["publish_attempts"]+r["transmission"]["discarded_queued"]
    assert r["transmission"]["reserved_bytes"]==r["transmission"]["in_flight"]==0
    assert broker_report(profile)["received"]==2


@pytest.mark.parametrize("mqtt_client",["delay_ack"],indirect=True)
def test_schedule_mqtt_parallel_pause_drains_active_and_retains_queue(mqtt_client):
    client,_=mqtt_client
    source=schedule([{"vehicle_id":v,"trips":[{"start":"2026-11-18T13:00:00+09:00","end":"2026-11-18T13:00:01+09:00","road_profile":"urban"}]}
                     for v in ["v000001","v000002"]],end="03",interval=100)
    run_id,_=start(client,source,mapping_mode="dedicated",generation_workers=4)
    for _ in range(200):
        r=client.get("/api/jobs/"+run_id).json()["result"]
        if r.get("transmission",{}).get("peak_in_flight",0)>=2:break
        time.sleep(.01)
    assert r["transmission"]["peak_in_flight"]==2
    client.post("/api/jobs/"+run_id+"/pause")
    for _ in range(200):
        r=client.get("/api/jobs/"+run_id).json()["result"]
        if r["job_state"]=="paused":break
        time.sleep(.01)
    assert r["job_state"]=="paused" and r["transmission"]["in_flight"]==0,r
    before=(r["puback_count"],r["processed_messages"],r["transmission"]["reserved_commands"])
    assert before[2]>0
    time.sleep(.25)
    r=client.get("/api/jobs/"+run_id).json()["result"]
    assert (r["puback_count"],r["processed_messages"],r["transmission"]["reserved_commands"])==before
    client.post("/api/jobs/"+run_id+"/resume")
    r=finished(client,run_id)["result"]
    assert r["job_state"]=="completed" and r["puback_count"]==22 and r["connection_cleanup_confirmed"],r


def short_parallel_schedule():
    return schedule([{"vehicle_id":v,"trips":[{"start":"2026-11-18T13:00:00+09:00","end":"2026-11-18T13:00:01+09:00","road_profile":"urban"}]}
                     for v in ["v000001","v000002","v000003"]],end="03",interval=200)


def test_schedule_mqtt_slow_generation_uses_workers_and_isolates_ui_cache(mqtt_client,monkeypatch):
    client,_=mqtt_client
    generate=control.generate_scheduled_obd
    def slow(body,vehicle,*args,**kwargs):
        if vehicle=="v000001":time.sleep(.35)
        return generate(body,vehicle,*args,**kwargs)
    monkeypatch.setattr(control,"generate_scheduled_obd",slow)
    # MQTT 실행은 조회용 DB 중간 상태를 사용하지 않는다.
    def forbidden(*args,**kwargs):raise AssertionError("조회 캐시를 실행에서 사용함")
    monkeypatch.setattr(control.CHECKPOINTS,"resume",forbidden)
    monkeypatch.setattr(control.CHECKPOINTS,"put",forbidden)
    run_id,_=start(client,short_parallel_schedule(),generation_workers=4,mapping_mode="dedicated")
    r=finished(client,run_id)["result"]
    assert r["job_state"]=="completed" and r["puback_count"]==18 and r["execution_cleanup_confirmed"],r
    assert r["generation_session"]["peak_active_workers"]>=2
    assert r["generation_pool"]["execution_checkpoints"]["entries"]<=256


def test_schedule_mqtt_generation_failure_closes_all_workers_and_sender(mqtt_client,monkeypatch):
    from services.control import schedule_execution
    from services.control.generation_session import GenerationFailure
    client,_=mqtt_client;captured=[];bridges=[]
    generate=control.generate_scheduled_obd;bridge_class=schedule_execution.MQTTBridge
    def bridge(*args,**kwargs):
        b=bridge_class(*args,**kwargs);bridges.append(b);return b
    def fail(body,vehicle,*args,**kwargs):
        if vehicle=="v000001":
            captured.append(control.JOBS.active)
            time.sleep(.1)
            raise GenerationFailure("시험용 생성 실패")
        return generate(body,vehicle,*args,**kwargs)
    monkeypatch.setattr(control,"generate_scheduled_obd",fail)
    monkeypatch.setattr(schedule_execution,"MQTTBridge",bridge)
    run_id,_=start(client,short_parallel_schedule(),generation_workers=4,mapping_mode="dedicated")
    r=finished(client,run_id)["result"]
    assert r["job_state"]=="failed" and r["execution_cleanup_confirmed"],r
    assert r["generation_session"]["process_starts"]==4 and r["generation_session"]["cleanup_confirmed"]
    assert len(captured[0].processes)==4 and all(p.poll() is not None for p in captured[0].processes)
    assert bridges[0].process.poll() is not None and bridges[0].cleaned
    assert r["buffer_budget"]["reserved_bytes"]==r["buffer_budget"]["reserved_messages"]==0
    assert control.JOBS.active_id() is None


@pytest.mark.parametrize("mqtt_client",["delay_ack"],indirect=True)
def test_schedule_mqtt_sender_pressure_stalls_generation_with_combined_budget(mqtt_client,monkeypatch):
    from services.control import schedule_execution
    client,_=mqtt_client;publisher_class=schedule_execution.SchedulePublisher
    class SmallPublisher(publisher_class):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs)
            self.state.update(queue_capacity=4,queue_byte_capacity=8192)
    monkeypatch.setattr(schedule_execution,"SchedulePublisher",SmallPublisher)
    run_id,_=start(client,short_parallel_schedule(),generation_workers=4,mapping_mode="dedicated",
                   generation_queue_messages=256,generation_queue_bytes=4194304)
    r=finished(client,run_id)["result"]
    assert r["job_state"]=="completed" and r["puback_count"]==18 and r["execution_cleanup_confirmed"],r
    assert r["transmission"]["backpressure_count"]>0
    assert r["transmission"]["peak_reserved_commands"]<=4 and r["transmission"]["peak_reserved_bytes"]<=8192
    assert r["generation_pool"]["queue"]["peak_reserved_messages"]<=256
    assert r["generation_pool"]["queue"]["peak_reserved_bytes"]<=4194304
    b=r["buffer_budget"]
    assert b["peak_reserved_messages"]<=b["max_messages"] and b["peak_reserved_bytes"]<=b["max_bytes"]
    assert b["reserved_bytes"]==b["reserved_messages"]==0


def test_schedule_mqtt_automatic_fleet_window_preserves_microtrip_only(mqtt_client):
    from datetime import timedelta,timezone
    client,profile=mqtt_client
    body=json.loads((ROOT/"configs/fleet-bounded.example.json").read_text(encoding="utf-8"))
    body.update(vehicle_count=3,day_off_probability=0,weekend_activity_probability=1,
                measurement={"start":"2026-11-18T00:00:00+09:00","end":"2026-11-19T00:00:00+09:00","timezone":"Asia/Seoul"})
    preview=client.post("/api/fleets/preview",json=body)
    assert preview.status_code==200,preview.text
    trip=preview.json()["trips"][0]
    # 실제 운행 도중 2초를 관측한다. 측정 종료를 운행 종료로 바꾸지 않는다.
    start_at=datetime.fromtimestamp((trip["start_ms"]+4000)/1000,tz=timezone(timedelta(hours=9)))
    body["measurement"]={"start":start_at.isoformat(),"end":(start_at+timedelta(seconds=2)).isoformat(),"timezone":"Asia/Seoul"}
    plan=client.post("/api/workspace/plans",json={"title":"자동 배정 MQTT 통합","fleet":body})
    assert plan.status_code==201,plan.text
    source=plan.json()
    assert 0<source["planned_messages"]<=100
    response=client.post("/api/workspace/execute",json={"plan_key":source["key"],"transport":"mqtt","confirm_mqtt":True,"generation_workers":4,"connection_lifetime":"while_driving"})
    assert response.status_code==202,response.text
    r=finished(client,response.json()["id"])["result"]
    assert r["job_state"]=="completed" and r["puback_count"]==source["planned_messages"] and r["execution_cleanup_confirmed"],r
    assert r["trip_count"]==0 and all(s["message"]["ty"]==4 for s in r["samples"])
    assert r["generation_session"]["worker_count"]==4
    report=broker_report(profile)
    assert all(m["ty"]==4 and set(m)=={"ty","ts","pld"} for m in report["messages"])
