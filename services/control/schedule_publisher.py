"""일정 송신의 한정 대기열. 고정 Client마다 명령 한 개만 진행한다."""
from collections import deque
import time

from services.control.mqtt_bridge import MQTTFailure
from services.control.message_codec import encode_command


class SchedulePublisher:
    def __init__(self,bridge,result,client_by_vehicle,measurement_start_ms=0,on_account=None,due_offset=None,queue_capacity=100,queue_bytes=1048576,clients=None,on_confirm=None):
        self.bridge=bridge
        self.on_account=on_account
        # 상시 운영: 명령이 PUBACK 확인으로 끝날 때 차량별 진행을 기록한다.
        self.on_confirm=on_confirm
        self.due_offset=due_offset or (lambda ts:ts-measurement_start_ms)
        self.clients=client_by_vehicle
        # 상시 운영은 차량 0대로 시작할 수 있어 Client 풀을 따로 받는다.
        self.queues={client:deque() for client in sorted(set(self.clients.values())|set(clients or ()))}
        self.active={}
        self.busy=set()
        self.ready=deque()
        self.ready_set=set()
        self.reserved_bytes=0
        self.reserved_count=0
        self.state=result["transmission"]={
            "mode":"parallel","max_in_flight":len(self.queues),"per_client_in_flight":1,
            "queue_capacity":queue_capacity,"queue_byte_capacity":queue_bytes,
            "queued_messages":0,"in_flight":0,"peak_queued":0,"peak_in_flight":0,
            "reserved_bytes":0,"peak_reserved_bytes":0,"reserved_commands":0,"peak_reserved_commands":0,
            "backpressure_count":0,"discarded_queued":0,"discarded_queued_bytes":0}

    def enqueue(self,command,gate,**fields):
        # route: 상시 운영의 attach/detach처럼 보낼 Client를 따로 지정하는 명령(전송 필드 아님).
        route=fields.pop("route",None)
        client=route or (self.clients[fields["vehicle_id"]] if command=="publish" else fields["client_id"])
        size=len(encode_command(dict(command=command,**fields)).encode())
        if size>self.state["queue_byte_capacity"]:raise MQTTFailure("한 메시지가 송신 대기열 바이트 상한을 초과했습니다.")
        full=False
        while self.reserved_count>=self.state["queue_capacity"] or self.reserved_bytes+size>self.state["queue_byte_capacity"]:
            if not full:self.state["backpressure_count"]+=1;full=True
            if not gate():return False
            time.sleep(.01)
        self.queues[client].append((command,fields,size))
        self._ready(client)
        self.reserved_count+=1;self.reserved_bytes+=size
        if command=="publish":
            self.state["queued_messages"]+=1
            self.state["peak_queued"]=max(self.state["peak_queued"],self.state["queued_messages"])
        self._budget()
        return True

    def _ready(self,client):
        if client not in self.busy and client not in self.ready_set and self.queues[client]:
            self.ready.append(client);self.ready_set.add(client)

    def _budget(self):
        self.state.update(reserved_bytes=self.reserved_bytes,reserved_commands=self.reserved_count)
        self.state["peak_reserved_bytes"]=max(self.state["peak_reserved_bytes"],self.reserved_bytes)
        self.state["peak_reserved_commands"]=max(self.state["peak_reserved_commands"],self.reserved_count)
        if self.on_account:self.on_account()

    def pump(self,allow_start=True,elapsed_ms=0,origin_ms=None):
        # 실행기가 전달한 시작 원점에 일시정지 누적 시간만 더한다.
        # 응답 회수·파이프 쓰기에 걸린 시간이 예정 시각을 뒤로 밀지 않게 한다.
        due_origin_ms=origin_ms if origin_ms is not None else time.time()*1000-elapsed_ms
        while self.bridge.pending:
            reply=self.bridge.poll()
            if reply is None:break
            identifier=reply["id"]
            if identifier not in self.active:raise MQTTFailure("송신 완료 명령을 확인하지 못했습니다.")
            client,command,size,fields=self.active.pop(identifier)
            self.busy.remove(client)
            self._ready(client)
            self.reserved_count-=1;self.reserved_bytes-=size
            if command=="publish":
                self.state["in_flight"]-=1
            self._budget()
            if self.on_confirm:self.on_confirm(command,fields)
        if not allow_start:return
        # 매 순회에 Client당 하나씩 허용한다. 동일 Client의 차량들은 한 순서를 공유한다.
        for _ in range(len(self.ready)):
            client=self.ready.popleft();self.ready_set.remove(client)
            queue=self.queues[client]
            command,fields,size=queue[0]
            if command=="publish":
                fields=dict(fields,due_at_ms=int(due_origin_ms+self.due_offset(fields["message"]["ts"])))
            try:identifier=self.bridge.submit(command,**fields)
            except MQTTFailure:
                self._ready(client)
                raise
            queue.popleft()
            self.active[identifier]=(client,command,size,fields)
            self.busy.add(client)
            if command=="publish":
                self.state["queued_messages"]-=1;self.state["in_flight"]+=1
                self.state["peak_in_flight"]=max(self.state["peak_in_flight"],self.state["in_flight"])

    def drain(self,gate):
        while self.reserved_count:
            if not gate():return
            time.sleep(.01)

    def discard(self):
        for queue in self.queues.values():
            while queue:
                command,_,size=queue.popleft()
                if command=="publish":
                    self.state["discarded_queued"]+=1
                    self.state["discarded_queued_bytes"]+=size
        self.active.clear();self.busy.clear()
        self.ready.clear();self.ready_set.clear()
        self.reserved_count=0;self.reserved_bytes=0
        self.state.update(queued_messages=0,in_flight=0)
        self._budget()
