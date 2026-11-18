"""시각순 처리와 고정 Worker 배정을 유지하는 건수·UTF-8 바이트 한정 대기열."""
from collections import deque
from concurrent.futures import wait, FIRST_COMPLETED
import heapq
import json
import time
from services.control.generation_pool import owner_for
from services.control.generation_session import GenerationFailure


class PageSession:
    def __init__(self,session,byte_limit):self.session,self.byte_limit=session,byte_limit
    def page(self,payload,timeout=15):return self.session.page(payload,timeout,byte_limit=self.byte_limit)
    def worker_digest(self,worker):return self.session.worker_digest(worker)
    def prepare_trip(self,*args):return self.session.prepare_trip(*args)
    def record_metric(self,name,value):self.session.record_metric(name,value)


def interval_boundaries(rows):
    """같은 차량의 맞닿은 운행은 연결 구간 하나로 합친다. 다음 행 하나만 보관한다."""
    rows=iter(rows);row=next(rows,None)
    while row is not None:
        start,end=row["observed_start_ms"],row["observed_end_ms"]
        yield start,0
        row=next(rows,None)
        while row is not None and row["observed_start_ms"]<=end:
            end=max(end,row["observed_end_ms"]);row=next(rows,None)
        yield end,2


class BoundedSchedule:
    def __init__(self,data,pool,gate,result,include_boundaries=False,on_account=None):
        self.data,self.pool,self.gate,self.result=data,pool,gate,result
        self.include_boundaries=include_boundaries;self.on_account=on_account
        self.boundaries=[];self.boundary_streams={}
        count=len(data["assignments"]);workers=len(pool.sessions)
        self.byte_cap=data.get("generation_queue_bytes",8*1024*1024)
        self.count_cap=data.get("generation_queue_messages",2000)
        self.frame=max(16384,min(1048576,self.byte_cap//max(1,count+2*workers+1)))
        self.limit=max(1,min(100,self.count_cap//max(1,count+workers+1)-1))
        if self.frame<16384 or self.limit<1:raise GenerationFailure("대기열 예산이 차량·Worker 수에 비해 작습니다.")
        self.closed=False
        self.states={};self.busy={};self.inflight={};self.heads=[];self.ready=[[] for _ in pool.sessions]
        self.owner_counts=[0 for _ in pool.sessions]
        self.queued_bytes=0;self.queued_count=0;self.consuming_bytes=0;self.consuming_count=0
        self.stats={"max_messages":self.count_cap,"max_bytes":self.byte_cap,"page_limit":self.limit,"response_byte_limit":self.frame,"peak_messages":0,"peak_bytes":0,"peak_reserved_messages":0,"peak_reserved_bytes":0,"backpressure_count":0,"emitted_messages":0}
        for a in data["assignments"]:
            v=a["vehicle_id"];s={"vehicle":v,"owner":owner_for(v,workers),"trips":iter(data["trip_provider"](v)),"queue":deque(),"future":None}
            self.advance(s);self.states[v]=s;self.owner_counts[s["owner"]]+=1
        for s in self.states.values():self.track(s)
        for v in self.states:
            if include_boundaries:
                stream=iter(interval_boundaries(data["trip_provider"](v)))
                self.boundary_streams[v]=stream
                boundary=next(stream,None)
                if boundary is not None:heapq.heappush(self.boundaries,(*boundary,v))
        self.assignments=[{"vehicle_id":v,"worker_index":s["owner"]} for v,s in sorted(self.states.items())]
    def track(self,s):
        """변경된 차량의 시각순 색인만 갱신하고 오래된 항목과 색인 크기를 제한한다."""
        s["revision"]=s.get("revision",0)+1
        if s["trip"] is not None:
            item=(s["due"],s["vehicle"],s["revision"])
            heapq.heappush(self.heads,item)
            if not s["queue"] and s["future"] is None:heapq.heappush(self.ready[s["owner"]],item)
        if len(self.heads)>2*len(self.states)+1:
            self.heads=[(x["due"],x["vehicle"],x["revision"]) for x in self.states.values() if x["trip"] is not None]
            heapq.heapify(self.heads)
        owner=s["owner"]
        if len(self.ready[owner])>2*self.owner_counts[owner]+1:
            self.ready[owner]=[(x["due"],x["vehicle"],x["revision"]) for x in self.states.values()
                if x["owner"]==owner and x["trip"] is not None and not x["queue"] and x["future"] is None]
            heapq.heapify(self.ready[owner])
    def peek(self,heap,ready=False):
        while heap:
            _,v,revision=heap[0];s=self.states[v]
            if revision==s["revision"] and s["trip"] is not None and (not ready or not s["queue"] and s["future"] is None):return s
            heapq.heappop(heap)
        return None
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
        if self.on_account:self.on_account()
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
        while self.boundaries or self.peek(self.heads) is not None:
            if not self.gate():return
            for owner,s in list(self.inflight.items()):
                f=s["future"]
                if f is None or not f.done():continue
                out=f.result();self.pool.release(f);del self.busy[owner];del self.inflight[owner];s["future"]=None
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
                self.track(s)
                self.account()
                m=None
                del out,items,f
            head=self.peek(self.heads)
            candidates=sorted((s for owner,heap in enumerate(self.ready) if owner not in self.busy and (s:=self.peek(heap,True)) is not None),key=lambda s:(s["due"],s["vehicle"]))
            for s in candidates:
                if self.data["realtime"] and s["due"]-self.data["measurement_start_ms"]>self.result.get("simulation_time_ms",0)+2000:continue
                # 최악 응답의 원문과 디코딩 객체를 위한 바이트 credit을 먼저 예약한다.
                # 가장 이른 미준비 차량을 위한 응답 공간을 남겨 순서 대기 교착을 막는다.
                reserve=0 if s is head else 1
                if self.queued_bytes+self.consuming_bytes+(len(self.busy)+1+reserve)*self.frame*2>self.byte_cap or self.queued_count+self.consuming_count+(len(self.busy)+1+reserve)*(self.limit+1)>self.count_cap:
                    self.stats["backpressure_count"]+=1;continue
                heapq.heappop(self.ready[s["owner"]])
                self.busy[s["owner"]]=time.monotonic();s["future"]=self.pool.submit(s["owner"],lambda session,s=s:self.prepare(s,session));self.inflight[s["owner"]]=s;self.account()
            if self.boundaries and (head is None or self.boundaries[0]<(head["due"],1,head["vehicle"])):
                timestamp,priority,vehicle=self.boundaries[0]
                due=timestamp-self.data["measurement_start_ms"]
                wake=due
                if self.data["realtime"]:
                    now=self.result.get("simulation_time_ms",0)
                    if self.busy:wake=min(wake,now+50)
                    for s in candidates:
                        preparation=s["due"]-self.data["measurement_start_ms"]-2000
                        if not s["queue"] and s["future"] is None and now<preparation<due:wake=min(wake,preparation)
                if not self.gate(wake):return
                if wake<due:continue
                heapq.heappop(self.boundaries)
                boundary=next(self.boundary_streams[vehicle],None)
                if boundary is not None:heapq.heappush(self.boundaries,(*boundary,vehicle))
                yield timestamp,priority,vehicle,None,False
                continue
            if head is None:break
            s=head
            if s["queue"]:
                message,synthetic,size=s["queue"][0]
                if not self.gate(message["ts"]-self.data["measurement_start_ms"]):return
                s["queue"].popleft();self.queued_count-=1;self.queued_bytes-=size;self.stats["emitted_messages"]+=1;self.consuming_bytes=size;self.consuming_count=1;self.account()
                if s["queue"]:s["due"]=s["queue"][0][0]["ts"]
                elif not s["more"]:self.advance(s)
                else:s["due"]=message["ts"]+self.data["config"]["interval_ms"]
                self.track(s)
                if self.include_boundaries:yield message["ts"],1,s["vehicle"],message,synthetic
                else:yield message["ts"],s["vehicle"],message,synthetic
                self.consuming_bytes=0;self.consuming_count=0;self.account()
                del message
            elif not self.busy:
                if self.data["realtime"]:
                    if not self.gate(max(0,s["due"]-self.data["measurement_start_ms"]-2000)):return
                else:raise GenerationFailure("생성 대기열이 진행되지 않습니다.")
            else:
                if any(time.monotonic()-started>30 for started in self.busy.values()):raise GenerationFailure("생성 Worker 작업 제한시간을 초과했습니다.")
                wait([s["future"] for s in self.inflight.values()],timeout=.005,return_when=FIRST_COMPLETED)

    def close(self):
        if self.closed:return
        self.closed=True
        self.stats["discarded_queued_messages"]=self.queued_count
        self.stats["discarded_queued_bytes"]=self.queued_bytes
        self.stats["cancelled_page_requests"]=len(self.busy)
        for s in self.states.values():s["queue"].clear();s["future"]=None
        self.boundaries.clear();self.boundary_streams.clear()
        self.heads.clear()
        for heap in self.ready:heap.clear()
        self.inflight.clear();self.busy.clear();self.queued_bytes=0;self.queued_count=0;self.consuming_bytes=0;self.consuming_count=0;self.account()
