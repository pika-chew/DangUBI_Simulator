"""고정 차량 배정의 생성 세션 풀. Worker마다 진행 중 작업은 하나만 둔다."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import threading
import time
from services.control.generation_session import GenerationSession,GenerationFailure


def owner_for(vehicle,count):
    return int.from_bytes(hashlib.sha256(vehicle.encode()).digest()[:8],"big")%count


class PoolJob:
    def __init__(self,job,cancelled):self.job,self.cancelled=job,cancelled
    @property
    def stop_requested(self):return self.cancelled.is_set() or self.job.stop_requested


class GenerationPool:
    def __init__(self,worker,job,manager,count=1,session_factory=GenerationSession):
        self.job=job
        self.sessions=[];self.executors=[];self.active=set();self.lock=threading.Lock()
        self.cancelled=threading.Event();self.closed=False;self.helpers_cleaned=False
        self.active_workers=0;self.peak_active_workers=0;self.work_ms=[0.0]*count
        try:
            for i in range(count):
                s=session_factory(worker,PoolJob(job,self.cancelled));self.sessions.append(s)
                with manager.lock:job.processes.append(s.process)
                self.executors.append(ThreadPoolExecutor(max_workers=1,thread_name_prefix=f"generation-{i}"))
        except BaseException:
            self.close(abort=True);raise
    def submit(self,owner,call):
        if self.closed:raise GenerationFailure("종료된 생성 풀입니다.")
        def run():
            before=time.monotonic()
            with self.lock:
                self.active_workers+=1;self.peak_active_workers=max(self.peak_active_workers,self.active_workers)
            try:return call(self.sessions[owner])
            finally:
                with self.lock:self.active_workers-=1;self.work_ms[owner]+=(time.monotonic()-before)*1000
        f=self.executors[owner].submit(run);self.active.add(f);return f
    def release(self,f):self.active.discard(f)
    @property
    def stats(self):
        rows=[s.stats.copy() for s in self.sessions]
        keys=["process_starts","page_requests","batch_requests","plan_builds","cache_hits","cache_evictions","cache_entries","cache_estimated_bytes","cache_max_entries","cache_max_estimated_bytes","peak_cache_estimated_bytes","peak_heap_alloc_bytes",
              "page_roundtrip_ms","request_write_ms","response_wait_ms","response_decode_ms","worker_identity_ms","worker_compute_ms","worker_serialize_ms","worker_stats_ms",
              "prepared_trip_hits","prepared_trip_misses","schedule_context_ms","checkpoint_restore_ms","request_payload_ms","checkpoint_update_ms"]
        timing=dict(generation_task_ms=sum(self.work_ms))
        timing["request_preparation_ms"]=max(0,timing["generation_task_ms"]-sum(r.get("page_roundtrip_ms",0) for r in rows))
        return {**{key:sum(r.get(key,0) for r in rows) for key in keys},**timing,"worker_count":len(rows),"peak_active_workers":self.peak_active_workers,"close_acknowledged":self.close_acknowledged}
    @property
    def cleaned(self):return self.helpers_cleaned and all(s.cleaned for s in self.sessions)
    @property
    def forced(self):return any(s.forced for s in self.sessions)
    @property
    def close_acknowledged(self):return all(s.close_acknowledged for s in self.sessions)
    def close(self,abort=False):
        if self.closed:return
        self.closed=True
        if abort:self.cancelled.set()
        deadline=time.monotonic()+2
        while any(not f.done() for f in self.active) and time.monotonic()<deadline:time.sleep(.01)
        self.helpers_cleaned=all(f.done() for f in self.active)
        for ex in self.executors:ex.shutdown(wait=self.helpers_cleaned,cancel_futures=True)
        # 중단은 PoolJob으로 알린 뒤 작업 경계에서 정리한다. 응답이 없으면 프로세스를 강제 종료한다.
        for s in self.sessions:
            if not self.helpers_cleaned and s.process.poll() is None:
                s.forced=True;s.process.kill()
            s.close(abort=abort)
        self.helpers_cleaned=all(f.done() for f in self.active)
        self.active.clear()
