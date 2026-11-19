"""일정 송신의 한정 대기열. 고정 Client마다 명령 한 개만 진행한다."""
from collections import deque
import json
import time

from services.control.mqtt_bridge import MQTTFailure


class SchedulePublisher:
    def __init__(self,bridge,result,client_by_vehicle,measurement_start_ms=0,on_account=None,due_offset=None,queue_capacity=100,queue_bytes=1048576):
        self.bridge=bridge
        self.on_account=on_account
        self.due_offset=due_offset or (lambda ts:ts-measurement_start_ms)
        self.clients=client_by_vehicle
        self.queues={client:deque() for client in sorted(set(self.clients.values()))}
        self.active={}
        self.busy=set()
        self.reserved_bytes=0
        self.reserved_count=0
        self.state=result["transmission"]={
            "mode":"parallel","max_in_flight":len(self.queues),"per_client_in_flight":1,
            "queue_capacity":queue_capacity,"queue_byte_capacity":queue_bytes,
            "queued_messages":0,"in_flight":0,"peak_queued":0,"peak_in_flight":0,
            "reserved_bytes":0,"peak_reserved_bytes":0,"reserved_commands":0,"peak_reserved_commands":0,
            "backpressure_count":0,"discarded_queued":0,"discarded_queued_bytes":0}

    def enqueue(self,command,gate,**fields):
        client=self.clients[fields["vehicle_id"]] if command=="publish" else fields["client_id"]
        size=len(json.dumps(dict(command=command,**fields),ensure_ascii=False,separators=(",",":")).encode())
        if size>self.state["queue_byte_capacity"]:raise MQTTFailure("한 메시지가 송신 대기열 바이트 상한을 초과했습니다.")
        full=False
        while self.reserved_count>=self.state["queue_capacity"] or self.reserved_bytes+size>self.state["queue_byte_capacity"]:
            if not full:self.state["backpressure_count"]+=1;full=True
            if not gate():return False
            time.sleep(.01)
        self.queues[client].append((command,fields,size))
        self.reserved_count+=1;self.reserved_bytes+=size
        if command=="publish":
            self.state["queued_messages"]+=1
            self.state["peak_queued"]=max(self.state["peak_queued"],self.state["queued_messages"])
        self._budget()
        return True

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
            client,command,size=self.active.pop(identifier)
            self.busy.remove(client)
            self.reserved_count-=1;self.reserved_bytes-=size
            if command=="publish":
                self.state["in_flight"]-=1
            self._budget()
        if not allow_start:return
        # 매 순회에 Client당 하나씩 허용한다. 동일 Client의 차량들은 한 순서를 공유한다.
        for client,queue in self.queues.items():
            if not queue or client in self.busy:continue
            command,fields,size=queue[0]
            if command=="publish":
                fields=dict(fields,due_at_ms=int(due_origin_ms+self.due_offset(fields["message"]["ts"])))
            identifier=self.bridge.submit(command,**fields)
            queue.popleft()
            self.active[identifier]=(client,command,size)
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
        self.reserved_count=0;self.reserved_bytes=0
        self.state.update(queued_messages=0,in_flight=0)
        self._budget()
