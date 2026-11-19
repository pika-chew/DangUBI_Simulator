"""실제 생성 본문과 검색 결과의 일치 및 실행 기록을 만들지 않음을 확인한다."""
import copy
import json
import os
from fastapi import HTTPException
from pathlib import Path

from services.control.test_app import client,control,scheduled_obd_body

ROOT=Path(__file__).resolve().parents[2]
WORKER=Path(os.environ.get("DANGUBI_TEST_WORKER",ROOT/".local/worker.exe" if os.name=="nt" else ROOT/".local/worker"))

def test_fleet_event_search_matches_microtrip_body_and_filter(client,monkeypatch):
    monkeypatch.setattr(control,"WORKER",WORKER)
    source=json.loads((ROOT/"apps/web/src/event-inspection.example.json").read_text(encoding="utf-8"))["fleet"]
    plan=client.post("/api/fleets/preview",json=source)
    assert plan.status_code==200,plan.text
    t=plan.json()["trips"][0]
    url="/api/fleets/obd/"+t["vehicle_id"]+"/"+t["trip_key"]
    expected=[];offset=0
    while True:
        response=client.post(url+"?offset="+str(offset),json={"fleet":source})
        assert response.status_code==200,response.text
        data=response.json()
        assert data["status"]=="generated_local_only"
        expected.extend(m for m in data["messages"] if m["pld"]["em"])
        offset+=len(data["messages"])
        if not data["has_more"]:break
    assert expected,"이상 발생 조건에서 검색 대조할 메시지가 없음"
    for bit in (0,1,8,64):
        cursor=0;found=[];nonzero=zero=missing=0
        while True:
            response=client.post(url+"?offset="+str(cursor)+"&limit=3",json={"fleet":source,"search_events":True,"event_mask":bit})
            assert response.status_code==200,response.text
            page=response.json()["event_search"]
            nonzero+=page["nonzero"];zero+=page["zero"];missing+=page["missing"]
            for row in page["rows"]:
                control.validate_message(row["message"])
                assert row["evidence"]
                assert all(e["end_ms"]<=row["message"]["ts"] for e in row["evidence"])
            found.extend(row["message"] for row in page["rows"])
            assert page["next_offset"]>cursor or page["complete"]
            cursor=page["next_offset"]
            if page["complete"]:break
        assert found==[m for m in expected if bit==0 or m["pld"]["em"]&bit]
        assert nonzero==len(expected) and nonzero+zero+missing==page["total"]
    assert client.get("/api/runs").json()==[]

def test_schedule_search_distinguishes_missing_and_zero(client,monkeypatch):
    monkeypatch.setattr(control,"WORKER",WORKER)
    body=scheduled_obd_body()
    t=client.post("/api/schedules/preview",json=body["schedule"]).json()["trips"][0]
    url="/api/schedules/obd/"+t["vehicle_id"]+"/"+t["trip_key"]
    body["search_events"]=True
    off=client.post(url,json=body).json()["event_search"]
    assert off["missing"]==0 and off["zero"]==off["total"] and off["complete"]
    body["schedule"]["include_em"]=True
    on=client.post(url,json=body).json()["event_search"]
    assert on["zero"]==on["total"] and on["missing"]==0 and on["complete"]
    bad=copy.deepcopy(body);bad["event_mask"]=128
    assert client.post(url,json=bad).status_code==422
    assert client.get("/api/health").json()["features"]["microtrip_event_search"]


def test_event_thirty_second_detail_query_needs_no_whole_trip_query(client,monkeypatch):
    monkeypatch.setattr(control,"WORKER",WORKER)
    source=json.loads((ROOT/"apps/web/src/event-inspection.example.json").read_text(encoding="utf-8"))["fleet"]
    t=client.post("/api/fleets/preview",json=source).json()["trips"][0]
    url="/api/fleets/obd/"+t["vehicle_id"]+"/"+t["trip_key"]
    search=client.post(url,json={"fleet":source,"search_events":True}).json()["event_search"]
    chosen=search["rows"][0]["message"]
    start=max(t["observed_start_ms"],chosen["ts"]-30000)
    end=min(t["observed_end_ms"],chosen["ts"]+30000)
    response=client.post(url+f"?start_ms={start}&end_ms={end}",json={"fleet":source})
    assert response.status_code==200,response.text
    data=response.json()
    assert data["trace"] and data["messages"] and chosen in data["messages"]
    assert all(start<=m["ts"]<=end for m in data["messages"])
    assert client.get("/api/runs").json()==[]


def test_microtrip_schema_requires_em_and_reserves_bit_seven():
    body=copy.deepcopy(control.CATALOG["profiles"]["4"]["example"])
    body["pld"]["em"]=256|32
    control.validate_message(body)
    import pytest
    body["pld"]["em"]=128
    with pytest.raises(HTTPException) as error:control.validate_message(body)
    assert error.value.status_code==422
    del body["pld"]["em"]
    with pytest.raises(HTTPException) as error:control.validate_message(body)
    assert error.value.status_code==422
