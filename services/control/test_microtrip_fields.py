"""이벤트 필드 선택이 조회·순차 실행에 동일하게 적용되는지 확인한다."""
import copy
import json
import os
from pathlib import Path

from services.control.fleet import FleetRequest, FleetOBDRequest, obd, preview
from services.control.fleet_execution import FleetExecution
from services.control.generation_session import GenerationSession
from services.control.test_app import client, control, wait_workspace_job, automatic_request
from services.control.test_schedule_mqtt import mqtt_client, fixture_binary, start, schedule, finished
from services.control import app as mqtt_control

ROOT=Path(__file__).resolve().parents[2]
WORKER=Path(os.environ.get("DANGUBI_TEST_WORKER",ROOT/".local/worker.exe" if os.name=="nt" else ROOT/".local/worker"))


def fleet(include):
    body=json.loads((ROOT/"configs/fleet-bounded.example.json").read_text(encoding="utf-8"))
    body.update(execution_profile="operations",vehicle_count=1,include_em=include,
                trip_min_minutes=3,trip_max_minutes=3,interval_ms=60000,
                day_off_probability=0,weekend_activity_probability=1,
                measurement=dict(start="2026-11-16T00:00:00+09:00",end="2026-11-17T00:00:00+09:00",timezone="Asia/Seoul"))
    return FleetRequest(**body)


def test_em_fleet_query_and_operating_stream_match_without_altering_schedule():
    off,on=fleet(False),fleet(True)
    off_plan,on_plan=preview(off),preview(on)
    assert off_plan["trips"]==on_plan["trips"]
    trip=on_plan["trips"][0]
    vehicle,key=trip["vehicle_id"],trip["trip_key"]
    disabled=obd(FleetOBDRequest(fleet=off),vehicle,key,WORKER,None,None,0,100)
    enabled=obd(FleetOBDRequest(fleet=on),vehicle,key,WORKER,None,None,0,100)
    assert disabled["trip"]==enabled["trip"]
    assert enabled["messages"]==disabled["messages"]
    assert all(isinstance(m["pld"]["em"],int) for m in disabled["messages"])
    stream=FleetExecution(on,WORKER)
    session=GenerationSession(WORKER)
    try:
        while True:
            actual=stream.generate(vehicle,key,0,session,100)
            if actual["status"]!="preparing":break
    finally:session.close()
    assert actual["messages"]==enabled["messages"]
    assert actual["trip"]==enabled["trip"]


def test_em_schedule_api_and_local_execution_samples(client,monkeypatch):
    monkeypatch.setattr(control,"WORKER",WORKER)
    source=dict(mode="manual",include_em=True,seed=42,interval_ms=2000,
                measurement=dict(start="2026-11-16T13:00:00+09:00",end="2026-11-16T13:00:04+09:00",timezone="Asia/Seoul"),
                vehicles=[dict(vehicle_id="v000001",trips=[dict(start="2026-11-16T13:00:00+09:00",end="2026-11-16T13:00:04+09:00",road_profile="urban")])])
    plan=client.post("/api/workspace/plans",json=dict(title="em 확인",schedule=source))
    assert plan.status_code==201,plan.text
    key=plan.json()["key"]
    # 실제 workspace 실행과 같은 경로를 사용하며 외부 MQTT 연결은 하지 않는다.
    response=client.post("/api/workspace/execute",json=dict(plan_key=key,transport="dry_run",realtime=False))
    assert response.status_code==202,response.text
    run=wait_workspace_job(client,response.json()["id"])
    assert run["result"]["job_state"]=="completed",run
    # 시험 실행 표본은 {vehicleId(시험 VIN), ty, ts, pld}. profiles 스키마는 봉투 안쪽 본문을 검사한다.
    assert all(s["message"]["vehicleId"]==s["vin"] for s in run["result"]["samples"])
    messages=[{k:v for k,v in s["message"].items() if k!="vehicleId"} for s in run["result"]["samples"]]
    assert sum(m["ty"]==4 for m in messages)==2
    assert sum(m["ty"]==3 for m in messages)==1
    for m in messages:control.validate_message(m)
    assert all("em" in m["pld"] for m in messages if m["ty"]==4)
    assert all("em" not in m["pld"] for m in messages if m["ty"]==3)
    assert client.get("/api/health").json()["features"]["microtrip_em"] is True


def test_em_single_journey_preview_preserves_other_fields(client,monkeypatch):
    monkeypatch.setattr(control,"WORKER",WORKER)
    body=automatic_request()
    off=client.post("/api/preview",json=body)
    assert off.status_code==200,off.text
    body["generation"]["include_em"]=True
    on=client.post("/api/preview",json=body)
    assert on.status_code==200,on.text
    a,b=off.json()["plan"]["samples"],on.json()["plan"]["samples"]
    assert a and len(a)==len(b)
    for old,new in zip(a,b):
        message=new["message"]
        control.validate_message(message)
        if message["ty"]==4:assert message["pld"]["em"]==0
        assert old==new


def test_em_selection_reaches_local_mqtt_publication(mqtt_client,monkeypatch):
    client,_=mqtt_client
    monkeypatch.setattr(mqtt_control,"WORKER",WORKER)
    for include in (False,True):
        source=schedule()
        source["include_em"]=include
        run_id,_=start(client,source,realtime=True)
        result=finished(client,run_id)["result"]
        assert result["job_state"]=="completed",result
        assert result["processed_messages"]==result["mqtt_published"]==result["puback_count"]==6
        micro=[s["message"] for s in result["samples"] if s["message"]["ty"]==4]
        assert len(micro)==3
        assert all("em" in m["pld"] for m in micro)
