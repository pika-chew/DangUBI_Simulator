"""신규 본문 지원 협상과 이전 Worker의 본문 전달 호환성을 확인한다."""
import copy
import json
import os
from pathlib import Path
import queue
from types import SimpleNamespace
import pytest
from services.control.mqtt_bridge import MQTTBridge
from services.control.message_codec import prepare_message
from services.control.test_schedule_mqtt import mqtt_client,fixture_binary


@pytest.mark.parametrize("capability,expected",[(None,False),({},False),({"raw_message":True},True),({"raw_message":False},False),({"raw_message":"yes"},False)])
def test_raw_body_requires_explicit_worker_capability(capability,expected):
    b=MQTTBridge.__new__(MQTTBridge);b.pending={};b.stats={};b.responses=queue.Queue();b.raw_supported=False
    reply=dict(id=0,ok=True,stats={"active_clients":0})
    if capability is not None:reply["capabilities"]=capability
    b.responses.put(reply);b.receive()
    assert b.raw_supported is expected


@pytest.mark.skipif(not os.environ.get("DANGUBI_REFERENCE_WORKER"),reason="이전 버전 Worker 선택 검증")
def test_prepared_body_falls_back_for_previous_worker(mqtt_client):
    client,profile_path=mqtt_client
    profile=json.loads(profile_path.read_text())
    worker=Path(os.environ["DANGUBI_REFERENCE_WORKER"])
    body=copy.deepcopy(client.get('/api/schema').json()['profiles']['4']['example'])
    bridge=MQTTBridge(worker,dict(mqtt=profile,assignments=[dict(vehicle_id="v000001",client_id="legacy-raw-c1")]),SimpleNamespace(id="legacy-raw-check"))
    try:
        bridge.receive();assert not bridge.raw_supported
        bridge.submit("publish",vehicle_id="v000001",message=prepare_message(body))
        assert bridge.poll(wait=3)["ok"]
    finally:bridge.close()
    assert bridge.stats["puback_count"]==1 and bridge.cleaned and bridge.close_acknowledged
