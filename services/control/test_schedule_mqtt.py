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
from services.control.vin import trial_vins, is_valid

ROOT=Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def fixture_binary(tmp_path_factory):
    env=os.environ.copy()
    env.update(GOCACHE=str(ROOT/".local/go-cache"),GOMODCACHE=str(ROOT/".local/go-mod"))
    target=tmp_path_factory.mktemp("schedule-mqtt-broker")/("fixture.exe" if os.name=="nt" else "fixture")
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


def test_trial_pause_and_recover_preserve_mqtt_trip(mqtt_client):
    client, profile = mqtt_client
    source = schedule(end="08", interval=1000)
    source["vehicles"] = [{"vehicle_id": "v000001", "trips": [{
        "start": "2026-11-18T13:00:00+09:00", "end": "2026-11-18T13:00:06+09:00", "road_profile": "urban"}]}]
    run_id, _ = start(client, source)
    job = "/api/jobs/" + run_id

    def wait_for_result(predicate):
        for _ in range(200):
            result = client.get(job).json()["result"]
            if predicate(result):
                return result
            time.sleep(.02)
        raise AssertionError(result)

    wait_for_result(lambda r: r["puback_count"] >= 1)
    assert client.post(job + "/pause").status_code == 202
    paused = wait_for_result(lambda r: r["job_state"] == "paused")
    checkpoint = paused["schedule_checkpoint"]
    assert 0 < checkpoint["count"] < 7
    time.sleep(.35)
    assert client.get(job).json()["result"]["schedule_checkpoint"] == checkpoint
    assert client.post(job + "/stop").status_code == 202
    assert finished(client, run_id)["result"]["job_state"] == "stopped"
    # 서버 재시작에 해당하는 복구는 실행 프로세스가 없는 상태에서 저장 기록만 이용한다.
    assert client.post(job + "/recover", json={}).status_code == 409
    response = client.post(job + "/recover", json={"confirm_mqtt": True})
    assert response.status_code == 202, response.text
    assert response.json()["id"] == run_id
    result = finished(client, run_id)["result"]
    assert result["job_state"] == "completed", result["errors"]
    assert result["recovery_confirmed_before"] == checkpoint["count"]
    assert result["schedule_checkpoint"]["count"] == result["planned_messages"] == 7
    report = broker_report(profile)
    assert [m["ty"] for m in report["messages"]] == [4] * 6 + [3]
    assert len({m["vehicleId"] for m in report["messages"]}) == 1
    assert len({m["pld"]["tid"] for m in report["messages"]}) == 1


@pytest.mark.parametrize("workers",[1,2,4])
@pytest.mark.parametrize("mode,lifetime",[("shared","always"),("shared","while_driving")])
def test_schedule_mqtt_mapping_lifetimes_and_payload(mqtt_client,mode,lifetime,workers):
    client,profile=mqtt_client
    source=schedule()
    run_id,plan=start(client,source,mapping_mode=mode,connection_lifetime=lifetime,generation_workers=workers)
    run=finished(client,run_id);r=run["result"]
    assert r["job_state"]=="completed",r
    assert r["processed_messages"]==r["planned_messages"]==r["mqtt_published"]==r["puback_count"]==6
    assert r["unconfirmed_messages"]==r["active_clients"]==0
    goal=r["transmission_goal"]
    measured=r["performance"]["schedule"]
    assert goal["status"] in {"passed","exceeded"},goal
    assert goal["sample_count"]==measured["count"]==6
    assert 0<=goal["within_1s_count"]<=goal["within_2s_count"]<=6
    assert measured["p99_upper_ms"]>=measured["p95_upper_ms"]
    assert (goal["status"]=="passed")==bool(measured["within_1s_count"]*100>=6*95 and measured["within_2s_count"]*100>=6*99)
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
    # 시험 실행 본문은 {vehicleId(시험 VIN), ty, ts, pld}. 생성 내용은 조회 결과와 같다.
    vins=trial_vins(v["vehicle_id"] for v in source["vehicles"])
    actual=[(s["message"]["ts"],s["vehicle_id"],{k:v for k,v in s["message"].items() if k!="vehicleId"}) for s in r["samples"]]
    assert actual==sorted(expected,key=lambda x:(x[0],x[1]))
    assert all(list(s["message"])[0]=="vehicleId" and s["message"]["vehicleId"]==s["vin"]==vins[s["vehicle_id"]] for s in r["samples"])
    assert all(s["topic"]==f'sim-test/{run_id}/{s["vin"]}/obd' for s in r["samples"])
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


def test_schedule_mqtt_rejects_dedicated_synthetic_and_missing_confirmation(mqtt_client):
    client,_=mqtt_client
    plan=client.post("/api/workspace/plans",json={"title":"거절 시험","schedule":schedule()}).json()
    base={"plan_key":plan["key"],"transport":"mqtt"}
    assert client.post("/api/workspace/execute",json=base).status_code==409
    for extra in [{"mapping_mode":"dedicated"},{"force_end_trip":True}]:
        assert client.post("/api/workspace/execute",json={**base,"confirm_mqtt":True,**extra}).status_code==422
    assert control.JOBS.active_id() is None


def test_trial_fast_and_basic_send_identical_payloads(mqtt_client):
    client, profile = mqtt_client
    source = schedule()
    source["vehicles"].extend({"vehicle_id": f"v{i:06d}", "trips": []} for i in range(3, 1000))
    source["vehicles"][2]["vehicle_id"] = "v001000"
    basic, _ = start(client, source)
    basic_result = finished(client, basic)["result"]
    fast, _ = start(client, source, realtime=False)
    fast_result = finished(client, fast)["result"]
    assert basic_result["job_state"] == fast_result["job_state"] == "completed"
    assert basic_result["assignment_count"] == fast_result["assignment_count"] == 1000
    assert basic_result["puback_count"] == fast_result["puback_count"] == 6
    assert fast_result["transmission_goal"]["status"] == "not_applicable"
    report = broker_report(profile)
    messages = {
        run_id: [m for topic, m in zip(report["topics"], report["messages"])
                 if f"/{run_id}/" in topic]
        for run_id in (basic, fast)
    }
    assert messages[basic] == messages[fast]
    assert fast_result["elapsed_ms"] + 3000 < basic_result["elapsed_ms"]


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
@pytest.mark.parametrize("mode,lifetime",[("shared","always"),("shared","while_driving")])
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
    vins=trial_vins(["v000001","v000002","v000003"])
    for vehicle in ["v000001","v000002","v000003"]:
        vin=vins[vehicle]
        assert is_valid(vin) and vin[10]=="T"  # 시험 VIN(공장 T): 운영 VIN(공장 S)과 겹치지 않음
        received=[m for topic,m in zip(report["topics"],report["messages"]) if topic.endswith(f"/{vin}/obd")]
        assert len(received)==6 and [m["ty"] for m in received]==[4]*5+[3]
        assert [m["ts"] for m in received]==sorted(m["ts"] for m in received)
        assert all(set(m)=={"vehicleId","ty","ts","pld"} and m["vehicleId"]==vin for m in received)


@pytest.mark.parametrize("mqtt_client",["no_ack"],indirect=True)
def test_schedule_mqtt_parallel_failure_accounts_all_attempts_and_queue(mqtt_client):
    client,profile=mqtt_client
    source=schedule([{"vehicle_id":v,"trips":[{"start":"2026-11-18T13:00:00+09:00","end":"2026-11-18T13:00:01+09:00","road_profile":"urban"}]}
                     for v in ["v000001","v000002"]],end="03",interval=100)
    run_id,_=start(client,source,client_count=2,connection_lifetime="while_driving")
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
    run_id,_=start(client,source,client_count=2,generation_workers=4)
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


def test_scale_thousand_connected_clients_stop_cleans_resources(mqtt_client,monkeypatch):
    from scripts.scale_schedule import prepare
    from services.control import schedule_execution
    client,path=mqtt_client
    profile=json.loads(path.read_text())
    profile.update(local_benchmark=True,max_clients=1000,max_messages=10000,max_duration_seconds=120,connect_per_second=200,publish_per_second=5000)
    source=short_parallel_schedule()
    source["vehicles"]=[dict(vehicle_id=f"v{i:06d}",trips=[dict(start="2026-11-18T13:00:00+09:00",end="2026-11-18T13:00:12+09:00",road_profile="mixed")]) for i in range(1000)]
    source["measurement"]["end"]="2026-11-18T13:00:14+09:00";source["interval_ms"]=2000
    bridges=[];original=schedule_execution.MQTTBridge
    def bridge(*args,**kwargs):
        b=original(*args,**kwargs);bridges.append(b);return b
    monkeypatch.setattr(schedule_execution,"MQTTBridge",bridge)
    start_run=prepare(source,dict(local_scale=True,clients=1000,workers=2),control.WORKER,profile,control.validate_message)
    started=start_run(control.JOBS);job=control.JOBS.active
    deadline=time.monotonic()+40
    while time.monotonic()<deadline:
        r=client.get("/api/jobs/"+started["id"]).json()["result"]
        if r.get("connected_clients")==1000:break
        if r["job_state"] not in control.ACTIVE:break
        time.sleep(.1)
    assert r["connected_clients"]==1000,r
    client.post("/api/jobs/"+started["id"]+"/stop")
    assert job.finished.wait(10)
    r=client.get("/api/jobs/"+started["id"]).json()["result"]
    assert r["job_state"]=="stopped" and r["execution_cleanup_confirmed"],r
    assert r["buffer_budget"]["reserved_bytes"]==r["active_clients"]==0
    assert all(p.poll() is not None for p in job.processes) and bridges[0].process.poll() is not None


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
    run_id,_=start(client,short_parallel_schedule(),generation_workers=4,client_count=3)
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
    run_id,_=start(client,short_parallel_schedule(),generation_workers=4,client_count=3)
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
    run_id,_=start(client,short_parallel_schedule(),generation_workers=4,client_count=3,
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
    assert all(m["ty"]==4 and set(m)=={"vehicleId","ty","ts","pld"} for m in report["messages"])
    assert {t.split("/")[-2] for t in report["topics"]}=={m["vehicleId"] for m in report["messages"]}


@pytest.mark.parametrize("mqtt_client",["drop_ack_once"],indirect=True)
def test_missing_puback_is_resent_and_run_completes(mqtt_client):
    """응답이 한 번 빠져도 실행을 멈추지 않고 재접속 뒤 같은 메시지를 다시 보내 완료한다(중복 1건 기록)."""
    client,profile=mqtt_client
    run_id,_=start(client,schedule(),realtime=False)
    result=finished(client,run_id)["result"]
    assert result["job_state"]=="completed",result["errors"]
    assert result["resent_messages"]==1 and result["unconfirmed_messages"]==0
    assert result["transmission"]["queue_capacity"]==1000  # 시험 실행 송신 대기 기본값
    assert result["puback_count"]==result["planned_messages"]
    report=broker_report(profile)
    assert report["received"]==result["planned_messages"]+1  # 원본이 실제로 도착했다면 중복 1건
    unique={(m["vehicleId"],m["pld"]["tid"],m["ts"],m["ty"]) for m in report["messages"]}
    assert len(unique)==result["planned_messages"]


def test_auto_resume_continues_interrupted_trial_and_skips_user_stop(mqtt_client):
    """서버 재시작으로 끊긴 실행은 자동으로 확인 지점부터 이어가고, 사용자가 중단한 실행은 그대로 둔다."""
    import sqlite3
    from services.control.auto_resume import AutoResumer
    client,profile=mqtt_client
    source=schedule(end="08",interval=1000)
    source["vehicles"]=[{"vehicle_id":"v000001","trips":[{"start":"2026-11-18T13:00:00+09:00","end":"2026-11-18T13:00:06+09:00","road_profile":"urban"}]}]
    run_id,_=start(client,source,auto_resume=True)
    job="/api/jobs/"+run_id
    for _ in range(300):
        r=client.get(job).json()["result"]
        if r["puback_count"]>=1:break
        time.sleep(.02)
    client.post(job+"/pause")
    for _ in range(300):
        r=client.get(job).json()["result"]
        if r["job_state"]=="paused":break
        time.sleep(.02)
    client.post(job+"/stop")
    stopped=finished(client,run_id)["result"]
    assert stopped["job_state"]=="stopped" and stopped["failure_kind"]=="user"
    recovered=[]
    resumer=AutoResumer(control.JOBS,lambda rid:recovered.append(rid) or control.recover_schedule(rid,control.RecoverScheduleRequest(confirm_mqtt=True)),delays=(0,))
    resumer.tick();resumer.tick()
    assert recovered==[]  # 직접 중단은 이어가지 않는다
    # 제어 API 재시작으로 끊긴 기록을 만든다(JOBS.recover가 남기는 상태와 같다).
    with sqlite3.connect(control.DB) as db:
        raw=db.execute("SELECT result FROM runs WHERE id=?",(run_id,)).fetchone()[0]
        result=json.loads(raw);result.update(job_state="interrupted",state="interrupted",failure_kind=None)
        db.execute("UPDATE runs SET result=? WHERE id=?",(json.dumps(result),run_id))
    checkpoint=result["schedule_checkpoint"]["count"]
    resumer.tick()  # 대기 예약
    assert client.get(job).json()["result"]["auto_recovery"]["next_at"] is not None
    resumer.tick()  # 이어가기
    assert recovered==[run_id]
    done=finished(client,run_id)["result"]
    assert done["job_state"]=="completed",done["errors"]
    assert done["recovery_confirmed_before"]==checkpoint and done["schedule_checkpoint"]["count"]==7
    assert done["auto_recovery"]["attempts"]==1 and done["auto_recovery"]["history"][0]["state"]=="interrupted"
    report=broker_report(profile)
    unique={(m["pld"]["tid"],m["ts"],m["ty"]) for m in report["messages"]}
    assert len(unique)==7 and sorted(unique)[-1][2]==3  # 확인 지점 이후 재전송 중복을 빼면 계획 7건(종료 Trip 포함)


@pytest.mark.parametrize("mqtt_client",["no_ack"],indirect=True)
def test_auto_resume_gives_up_without_progress(mqtt_client):
    """확인 지점이 나아가지 않는 실패가 반복되면 자동 이어가기를 멈추고 기록한다."""
    from services.control.auto_resume import AutoResumer
    client,profile=mqtt_client
    run_id,_=start(client,schedule(),realtime=False,auto_resume=True)
    first=finished(client,run_id)["result"]
    assert first["job_state"]=="failed" and first["failure_kind"]=="error"
    resumer=AutoResumer(control.JOBS,lambda rid:control.recover_schedule(rid,control.RecoverScheduleRequest(confirm_mqtt=True)),delays=(0,),max_without_progress=2)
    for _ in range(8):
        resumer.tick()
        if control.JOBS.active_id():finished(client,run_id)
    final=client.get("/api/jobs/"+run_id).json()["result"]
    assert final["auto_recovery"]["gave_up"] and final["auto_recovery"]["attempts"]==2


def test_realtime_mqtt_requires_duration_margin(mqtt_client):
    """1배속 실제 MQTT는 프로필 실행 시간 한도가 일정 길이보다 10% 이상 넉넉해야 시작한다(fixture 한도 30초)."""
    client,_=mqtt_client
    source=schedule(end="28")
    plan=client.post("/api/workspace/plans",json={"title":"여유 부족","schedule":source}).json()
    response=client.post("/api/workspace/execute",json={"plan_key":plan["key"],"transport":"mqtt","confirm_mqtt":True,"realtime":True})
    assert response.status_code==422 and "여유" in response.json()["detail"]
