"""시각순 처리와 고정 Worker 배정을 유지하는 건수·UTF-8 바이트 한정 대기열."""
from collections import deque
import json
import time
from services.control.generation_pool import owner_for
from services.control.generation_session import GenerationFailure


class PageSession:
    def __init__(self,session,byte_limit):self.session,self.byte_limit=session,byte_limit
    def page(self,payload,timeout=15):return self.session.page(payload,timeout,byte_limit=self.byte_limit)


class BoundedSchedule:
    def __init__(self,data,pool,gate,result):
        self.data,self.pool,self.gate,self.result=data,pool,gate,result
        count=len(data["assignments"]);workers=len(pool.sessions)
        self.byte_cap=data.get("generation_queue_bytes",8*1024*1024)
        self.count_cap=data.get("generation_queue_messages",2000)
        self.frame=min(1048576,self.byte_cap//max(1,count+2*workers+1))
        self.limit=min(100,self.count_cap//max(1,count+workers+1)-1)
        if self.frame<16384 or self.limit<1:raise GenerationFailure("대기열 예산이 차량·Worker 수에 비해 작습니다.")
        self.closed=False
        self.states={};self.busy={};self.queued_bytes=0;self.queued_count=0;self.consuming_bytes=0;self.consuming_count=0
        self.stats={"max_messages":self.count_cap,"max_bytes":self.byte_cap,"page_limit":self.limit,"response_byte_limit":self.frame,"peak_messages":0,"peak_bytes":0,"peak_reserved_messages":0,"peak_reserved_bytes":0,"backpressure_count":0,"emitted_messages":0}
        for a in data["assignments"]:
            v=a["vehicle_id"];s={"vehicle":v,"owner":owner_for(v,workers),"trips":iter(data["trip_provider"](v)),"queue":deque(),"future":None}
            self.advance(s);self.states[v]=s
        self.assignments=[{"vehicle_id":v,"worker_index":s["owner"]} for v,s in sorted(self.states.items())]
    def advance(self,s):
        s["trip"]=next(s["trips"],None);s["offset"]=0;s["more"]=True
        if s["trip"]:
            t=s["trip"];interval=self.data["config"]["interval_ms"]
            seq=max(0,(t["observed_start_ms"]-t["start_ms"]+interval-1)//interval)
            s["due"]=min(t["start_ms"]+seq*interval,t["observed_end_ms"])
    def account(self):
        count=self.queued_count+self.consuming_count+len(self.busy)*(self.limit+1)
        size=self.queued_bytes+self.consuming_bytes+len(self.busy)*self.frame*2
        if count>self.count_cap or size>self.byte_cap:raise GenerationFailure("생성 대기열 예산을 초과했습니다.")
        self.stats.update(queued_messages=self.queued_count,queued_bytes=self.queued_bytes,reserved_messages=count,reserved_bytes=size)
        for key,value in [("peak_messages",self.queued_count),("peak_bytes",self.queued_bytes),("peak_reserved_messages",count),("peak_reserved_bytes",size)]:self.stats[key]=max(self.stats[key],value)
    def prepare(self,s,session):
        wrapped=PageSession(session,self.frame)
        previous=-1
        for _ in range(100):
            while self.pool.job.pause_requested and not self.pool.cancelled.is_set() and not self.pool.job.stop_requested:time.sleep(.01)
            if self.pool.cancelled.is_set() or self.pool.job.stop_requested:raise GenerationFailure("생성 풀을 중단했습니다.")
            out=self.data["generate"](s["vehicle"],s["trip"]["trip_key"],s["offset"],session=wrapped,limit=self.limit)
            if out["status"]!="preparing":return out
            completed=out["restoration"]["completed_preceding_trips"]
            if completed<=previous:raise GenerationFailure("실행 중간 상태 복원이 진행되지 않습니다.")
            previous=completed
        raise GenerationFailure("실행 중간 상태 복원 상한을 초과했습니다.")
    def __iter__(self):
        while any(s["trip"] is not None for s in self.states.values()):
            if not self.gate():return
            for s in self.states.values():
                f=s["future"]
                if f is None or not f.done():continue
                out=f.result();self.pool.release(f);del self.busy[s["owner"]];s["future"]=None
                items=[(m,False) for m in out["messages"]]
                if not out["has_more"] and out.get("trip"):items.append((out["trip"],out["synthetic_trip"]))
                used=0
                for m,synthetic in items:
                    size=len(json.dumps(m,separators=(",",":"),ensure_ascii=False).encode())
                    s["queue"].append((m,synthetic,size));used+=size
                if used>self.frame or len(items)>self.limit+1:raise GenerationFailure("생성 응답이 예약 예산을 초과했습니다.")
                self.queued_bytes+=used;self.queued_count+=len(items);s["more"]=out["has_more"];s["offset"]+=len(out["messages"])
                if s["queue"]:s["due"]=s["queue"][0][0]["ts"]
                elif s["more"]:raise GenerationFailure("빈 페이지가 계속된다고 응답했습니다.")
                else:self.advance(s)
                self.account()
                m=None
                del out,items,f
            candidates=sorted((s for s in self.states.values() if s["trip"] is not None),key=lambda s:(s["due"],s["vehicle"]))
            if not candidates:break
            for s in candidates:
                if s["queue"] or s["future"] is not None or s["owner"] in self.busy:continue
                if self.data["realtime"] and s["due"]-self.data["measurement_start_ms"]>self.result.get("simulation_time_ms",0)+2000:continue
                # 최악 응답의 원문과 디코딩 객체를 위한 바이트 credit을 먼저 예약한다.
                if self.queued_bytes+self.consuming_bytes+(len(self.busy)+1)*self.frame*2>self.byte_cap or self.queued_count+self.consuming_count+(len(self.busy)+1)*(self.limit+1)>self.count_cap:
                    self.stats["backpressure_count"]+=1;continue
                self.busy[s["owner"]]=time.monotonic();s["future"]=self.pool.submit(s["owner"],lambda session,s=s:self.prepare(s,session));self.account()
            s=candidates[0]
            if s["queue"]:
                message,synthetic,size=s["queue"][0]
                if not self.gate(message["ts"]-self.data["measurement_start_ms"]):return
                s["queue"].popleft();self.queued_count-=1;self.queued_bytes-=size;self.stats["emitted_messages"]+=1;self.consuming_bytes=size;self.consuming_count=1;self.account()
                if s["queue"]:s["due"]=s["queue"][0][0]["ts"]
                elif not s["more"]:self.advance(s)
                else:s["due"]=message["ts"]+self.data["config"]["interval_ms"]
                yield message["ts"],s["vehicle"],message,synthetic
                self.consuming_bytes=0;self.consuming_count=0;self.account()
                del message
            elif not self.busy:
                if self.data["realtime"]:
                    if not self.gate(max(0,s["due"]-self.data["measurement_start_ms"]-2000)):return
                else:raise GenerationFailure("생성 대기열이 진행되지 않습니다.")
            else:
                if any(time.monotonic()-started>30 for started in self.busy.values()):raise GenerationFailure("생성 Worker 작업 제한시간을 초과했습니다.")
                time.sleep(.005)

    def close(self):
        if self.closed:return
        self.closed=True
        self.stats["discarded_queued_messages"]=self.queued_count
        self.stats["discarded_queued_bytes"]=self.queued_bytes
        self.stats["cancelled_page_requests"]=len(self.busy)
        for s in self.states.values():s["queue"].clear();s["future"]=None
        self.busy.clear();self.queued_bytes=0;self.queued_count=0;self.consuming_bytes=0;self.consuming_count=0;self.account()
