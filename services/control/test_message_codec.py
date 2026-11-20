"""표준 검사기와 계약 기반 정상 경로의 경계 판정·불변 본문을 대조한다."""
import copy
from dataclasses import FrozenInstanceError
import json
import math
from pathlib import Path
import pytest
from jsonschema import Draft202012Validator
from services.control.message_codec import compile_check,prepare_message,encode_command

CATALOG=json.loads((Path(__file__).resolve().parents[2]/"contracts/obd-development.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("kind",["3","4"])
def test_compiled_rules_match_standard_for_all_field_boundaries(kind):
    profile=CATALOG["profiles"][kind];schema=profile["schema"]
    check=compile_check(schema);standard=Draft202012Validator(schema)
    assert check is not None
    base=copy.deepcopy(profile["example"])
    for key,prop in schema["properties"]["pld"]["properties"].items():
        base["pld"].setdefault(key,prop["default"])
    assert check(base) and standard.is_valid(base)
    values=[None,True,False,-1,0,0.5,1.0,128,255,256,383,384,1e100,[],{},"", "0,0,0", "0,0,0\n","١,٢,٣","한"*121]
    for container,rules in [((),schema["properties"]),(("pld",),schema["properties"]["pld"]["properties"])]:
        for key,prop in rules.items():
            boundaries=[prop[n]+delta for n in ("minimum","maximum") if n in prop for delta in (-1,0,1)]
            for value in values+boundaries:
                message=copy.deepcopy(base);target=message["pld"] if container else message
                target[key]=value
                assert check(message)==standard.is_valid(message),(kind,key,value)
            message=copy.deepcopy(base);target=message["pld"] if container else message
            del target[key]
            assert check(message)==standard.is_valid(message),(kind,key,"missing")
    for container in ((),("pld",)):
        message=copy.deepcopy(base);target=message["pld"] if container else message
        target["extra"]=1
        assert not check(message) and not standard.is_valid(message)


def test_unknown_rules_disable_fast_path_and_nonfinite_values_fall_back():
    schema=copy.deepcopy(CATALOG["profiles"]["4"]["schema"])
    schema["properties"]["pld"]["properties"]["sp"]["multipleOf"]=2
    assert compile_check(schema) is None
    check=compile_check(CATALOG["profiles"]["4"]["schema"])
    for value in (math.nan,math.inf,-math.inf):
        message=copy.deepcopy(CATALOG["profiles"]["4"]["example"]);message["pld"]["sp"]=value
        assert not check(message)


def test_wire_body_is_snapshot_and_has_exact_utf8_size():
    message=copy.deepcopy(CATALOG["profiles"]["4"]["example"])
    message["pld"]["xyz"]="١,٢,٣"
    wire=prepare_message(message)
    message["pld"]["sp"]=99
    message["ts"]+=2000
    command=dict(id=1,command="publish",vehicle_id="v000001",message=wire,due_at_ms=1000)
    raw=encode_command(command)
    assert json.loads(raw)["message"]["pld"]["sp"]!=99
    assert wire.size==len(wire.text.encode("utf-8"))
    assert wire.size>len(wire.text)
    assert wire["ts"]==json.loads(wire.text)["ts"]
    with pytest.raises(FrozenInstanceError):wire.text="{}"
    assert json.loads(encode_command({"id":1,"command":"connect"}))=={"id":1,"command":"connect"}
    assert json.loads(encode_command({"message":wire}))["message"]==json.loads(wire.text)


def test_wire_queue_reserves_bytes_and_releases_after_ack():
    from services.control.schedule_publisher import SchedulePublisher
    from services.control.test_schedule_publisher import Bridge
    wire=prepare_message(CATALOG["profiles"]["4"]["example"])
    bridge=Bridge();publisher=SchedulePublisher(bridge,{}, {"a":"c1"})
    assert publisher.enqueue("publish",lambda:True,vehicle_id="a",message=wire)
    expected=len(encode_command(dict(command="publish",vehicle_id="a",message=wire)).encode("utf-8"))
    assert publisher.reserved_bytes==expected
    publisher.pump(origin_ms=100000)
    assert json.loads(encode_command(bridge.sent[0][2]))["message"]==json.loads(wire.text)
    bridge.replies=[1];publisher.pump(allow_start=False)
    assert publisher.reserved_bytes==publisher.reserved_count==0
