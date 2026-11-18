"""고정 Client 명령 순서와 공통 건수·바이트 예약을 검증한다."""
import pytest

from services.control.mqtt_bridge import MQTTFailure
from services.control.schedule_publisher import SchedulePublisher


class Bridge:
    profile={"publish_per_second":70}

    def __init__(self):
        self.pending={};self.sent=[];self.replies=[]

    def submit(self,command,**fields):
        identifier=len(self.sent)+1
        self.sent.append((identifier,command,fields))
        self.pending[identifier]=True
        return identifier

    def poll(self):
        if not self.replies:return None
        identifier=self.replies.pop(0)
        self.pending.pop(identifier)
        return {"id":identifier}


def test_client_lanes_preserve_shared_vehicle_order_and_disconnect():
    bridge=Bridge();result={}
    publisher=SchedulePublisher(bridge,result,{"a":"c1","b":"c1","c":"c2"})
    for command,fields in [("connect",{"client_id":"c1"}),
                           ("publish",{"vehicle_id":"a","message":{"ts":1}}),
                           ("publish",{"vehicle_id":"b","message":{"ts":2}}),
                           ("disconnect",{"client_id":"c1"}),
                           ("publish",{"vehicle_id":"c","message":{"ts":1}})]:
        assert publisher.enqueue(command,lambda:True,**fields)
    publisher.pump()
    assert [s[1] for s in bridge.sent]==["connect","publish"]
    # 빠른 c2가 끝나도 c1의 대기 순서는 바뀌지 않는다.
    bridge.replies=[2];publisher.pump()
    assert len(bridge.sent)==2
    bridge.replies=[1];publisher.pump()
    assert bridge.sent[-1][2]["vehicle_id"]=="a"
    bridge.replies=[3];publisher.pump()
    assert bridge.sent[-1][2]["vehicle_id"]=="b"
    bridge.replies=[4];publisher.pump()
    assert bridge.sent[-1][1]=="disconnect"
    bridge.replies=[5];publisher.pump()
    assert publisher.reserved_count==publisher.reserved_bytes==0
    assert result["transmission"]["peak_in_flight"]==1


@pytest.mark.parametrize("kind",["count","bytes"])
def test_budget_counts_waiting_and_in_flight_and_stops_producer(kind):
    bridge=Bridge();publisher=SchedulePublisher(bridge,{}, {"a":"c1","b":"c2"})
    if kind=="count":publisher.state["queue_capacity"]=1
    else:publisher.state["queue_byte_capacity"]=120
    assert publisher.enqueue("publish",lambda:True,vehicle_id="a",message={"ts":1,"pld":"x"*20})
    publisher.pump()
    before=publisher.reserved_bytes
    assert before>0 and publisher.state["in_flight"]==1
    assert not publisher.enqueue("publish",lambda:False,vehicle_id="b",message={"ts":1,"pld":"x"*20})
    assert publisher.reserved_bytes==before and len(bridge.sent)==1
    bridge.replies=[1];publisher.pump(allow_start=False)
    assert publisher.reserved_count==publisher.reserved_bytes==0


def test_pause_holds_queue_while_draining_active_and_discard_counts_only_waiting():
    bridge=Bridge();publisher=SchedulePublisher(bridge,{}, {"a":"c1","b":"c2"})
    for vehicle in ["a","b","a"]:
        publisher.enqueue("publish",lambda:True,vehicle_id=vehicle,message={"ts":1})
    publisher.pump()
    assert publisher.state["in_flight"]==publisher.state["peak_in_flight"]==2
    bridge.replies=[2,1];publisher.pump(allow_start=False)
    assert publisher.state["in_flight"]==0 and publisher.state["queued_messages"]==1
    assert len(bridge.sent)==2
    publisher.discard()
    assert publisher.state["discarded_queued"]==1 and publisher.state["discarded_queued_bytes"]>0
    assert publisher.state["reserved_bytes"]==publisher.state["reserved_commands"]==0


def test_oversized_command_is_rejected_before_reservation():
    publisher=SchedulePublisher(Bridge(),{}, {"a":"c1"})
    with pytest.raises(MQTTFailure):
        publisher.enqueue("publish",lambda:True,vehicle_id="a",message={"ts":1,"pld":"x"*1048576})
    assert publisher.reserved_count==publisher.reserved_bytes==0


def test_bridge_write_failure_keeps_unsent_queue_for_final_accounting():
    bridge=Bridge();publisher=SchedulePublisher(bridge,{}, {"a":"c1"})
    publisher.enqueue("publish",lambda:True,vehicle_id="a",message={"ts":1})
    def broken(*args,**kwargs):raise MQTTFailure("통신 실패")
    bridge.submit=broken
    with pytest.raises(MQTTFailure):publisher.pump()
    publisher.discard()
    assert publisher.state["discarded_queued"]==1
    assert publisher.state["reserved_commands"]==publisher.state["reserved_bytes"]==0


def test_slow_commands_keep_same_deadline_across_clients_and_pumps(monkeypatch):
    bridge=Bridge()
    clock=[100.]
    monkeypatch.setattr("services.control.schedule_publisher.time.time",lambda:clock[0])
    submit=bridge.submit
    def slow_submit(command,**fields):
        clock[0]+=.04
        return submit(command,**fields)
    bridge.submit=slow_submit
    publisher=SchedulePublisher(bridge,{},{"a":"c1","b":"c2"},measurement_start_ms=5000)
    for vehicle in ("a","b","a"):
        publisher.enqueue("publish",lambda:True,vehicle_id=vehicle,message={"ts":6000})
    publisher.pump(origin_ms=100000)
    bridge.replies=[1,2]
    clock[0]+=3
    publisher.pump(origin_ms=100000)
    assert [fields["due_at_ms"] for _,_,fields in bridge.sent]==[101000]*3
    assert clock[0]*1000-bridge.sent[-1][2]["due_at_ms"]>2000


def test_pause_compensation_and_compressed_offsets_preserve_payload():
    bridge=Bridge()
    publisher=SchedulePublisher(bridge,{},{"a":"c1"},due_offset=lambda ts:35000)
    message={"ts":1794976206000}
    publisher.enqueue("publish",lambda:True,vehicle_id="a",message=message)
    publisher.pump(allow_start=False,origin_ms=100000)
    assert not bridge.sent
    publisher.pump(origin_ms=100500)
    assert bridge.sent[0][2]["due_at_ms"]==135500
    assert bridge.sent[0][2]["message"] is message
