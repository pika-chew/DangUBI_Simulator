"""실제 일정 API→생성 풀→MQTT 경로의 로컬 계측. 사용자 DB와 AWS는 사용하지 않는다."""
import argparse
import ctypes
from datetime import datetime,timedelta,timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import struct
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlparse
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.measure_mqtt import ProcessResources
from scripts.measurement_process import ProcessTree

DEPARTURE_SPACING_SECONDS = 20
PARKING_SECONDS = 1802
PARKING_TIMEOUT_SECONDS = 1880
REPLAY_ROUNDS = 3
REPLAY_DRIVING_SECONDS = 4
REPLAY_SPACING_SECONDS = 35


def compressed_due_offset(start_ms, timestamp_ms):
    original_offset = timestamp_ms-start_ms
    original_spacing = (REPLAY_DRIVING_SECONDS+PARKING_SECONDS)*1000
    round_index = min(REPLAY_ROUNDS-1, original_offset//original_spacing)
    return original_offset-round_index*(original_spacing-REPLAY_SPACING_SECONDS*1000)


def validate_config(config):
    if not isinstance(config,dict) or set(config)-{"cases","repeats"}:raise ValueError("측정 설정 형식을 확인하세요.")
    cases=config.get("cases");repeats=config.get("repeats",2)
    if type(repeats) is not int or not 1<=repeats<=3 or not isinstance(cases,list) or not 1<=len(cases)<=24:raise ValueError("조건1~24개·반복1~3회입니다.")
    names=set();seconds=0
    for c in cases:
        if not isinstance(c,dict) or set(c)-{"name","vehicles","clients","workers","lifetime","history_minutes","fault","local_scale","driving_seconds","departure_groups","parking_replay","compressed_replay"}:raise ValueError("알 수 없는 조건·주소 입력은 허용하지 않습니다.")
        if type(c.get("local_scale",False)) is not bool:raise ValueError("로컬 확장 플래그는 boolean입니다.")
        cap=1000 if c.get("local_scale") else 20
        name=c.get("name")
        if not isinstance(name,str) or not 1<=len(name)<=60 or any(x in name for x in "|\r\n") or name in names:raise ValueError("조건 이름1~60자를 중복 없이 지정하세요.")
        names.add(name)
        for key,lo,hi in [("vehicles",1,cap),("clients",1,cap),("workers",1,4),("history_minutes",0,120)]:
            value=c.get(key,0 if key=="history_minutes" else None)
            if type(value) is not int or not lo<=value<=hi:raise ValueError(f"차량/Client1~{cap}·Worker1/2/4·과거 운행0~120분입니다.")
        if c["workers"] not in (1,2,4) or c["clients"]>c["vehicles"]:raise ValueError("Worker 수 또는 고정 Client 수를 확인하세요.")
        if c.get("lifetime","always") not in ("always","while_driving") or c.get("fault","none") not in ("none","delay_ack"):raise ValueError("유지 방식·지연 조건을 확인하세요.")
        driving=c.get("driving_seconds",12 if c.get("local_scale") else 4)
        if "driving_seconds" in c and (not c.get("local_scale") or type(driving) is not int or not 12<=driving<=90
                                       or c["vehicles"]*((driving+1)//2+1)>10000):
            raise ValueError("확장 계측의 운행은 12~90초, 전체 메시지는 10,000건 이하여야 합니다.")
        groups=c.get("departure_groups",1)
        if "departure_groups" in c and (not c.get("local_scale") or type(groups) is not int
                or not 2<=groups<=min(5,c["vehicles"]) or driving>15 or c.get("history_minutes",0)):
            raise ValueError("분산 출발은 로컬 확장 전용이며 출발 그룹 2~5개, 운행 12~15초, 사전 운행 0분입니다.")
        if type(c.get("parking_replay",False)) is not bool:
            raise ValueError("parking_replay는 boolean입니다.")
        if c.get("parking_replay") and (not c.get("local_scale") or c["vehicles"]>8 or driving!=12 or
                groups!=1 or c.get("history_minutes",0) or c.get("lifetime")!="while_driving" or c.get("fault","none")!="none"):
            raise ValueError("장시간 주차는 로컬 1~8대·12초 운행·운행 중 연결 전용입니다.")
        compressed=c.get("compressed_replay",False)
        if type(compressed) is not bool or compressed and (not c.get("local_scale") or
                c.get("parking_replay") or groups!=1 or "driving_seconds" in c or c.get("history_minutes",0) or
                c.get("lifetime")!="while_driving" or c.get("fault","none")!="none"):
            raise ValueError("압축 반복 시험은 분산 출발·장시간 주차·장애 옵션과 함께 사용할 수 없습니다.")
        if compressed:
            seconds+=(REPLAY_ROUNDS-1)*REPLAY_SPACING_SECONDS+6
            continue
        seconds+=max(60,(groups-1)*DEPARTURE_SPACING_SECONDS+driving+2) if c.get("local_scale") else 10
    if seconds*repeats>900:raise ValueError("예상15분 이내의 측정 계획만 허용합니다.")
    if any(c.get("parking_replay") for c in cases) and (len(cases)>2 or repeats!=1 or not all(c.get("parking_replay") for c in cases)):
        raise ValueError("장시간 주차 조건만 최대 2개, 반복 1회로 실행하세요.")
    return cases,repeats


def schedule_source(case):
    base=datetime(2026,11,18,13,tzinfo=timezone(timedelta(hours=9)))
    driving=case.get("driving_seconds",12 if case.get("local_scale") else 4)
    groups=case.get("departure_groups",1)
    count=case["vehicles"]
    duration=(groups-1)*DEPARTURE_SPACING_SECONDS+driving+2
    parking=case.get("parking_replay",False)
    compressed=case.get("compressed_replay",False)
    if compressed:
        driving=REPLAY_DRIVING_SECONDS
        duration=(REPLAY_ROUNDS-1)*(driving+PARKING_SECONDS)+driving+2
    elif parking:duration+=driving+PARKING_SECONDS
    vehicles=[]
    for index in range(count):
        departure=base+timedelta(seconds=(index*groups//count)*DEPARTURE_SPACING_SECONDS)
        vehicles.append(dict(vehicle_id=f"v{index+1:06d}",trips=[dict(
            start=(departure-timedelta(minutes=case.get("history_minutes",0))).isoformat(),
            end=(departure+timedelta(seconds=driving)).isoformat(),road_profile="mixed")]))
        for round_index in range(1, REPLAY_ROUNDS if compressed else 2 if parking else 1):
            second=departure+timedelta(seconds=round_index*(driving+PARKING_SECONDS))
            vehicles[-1]["trips"].append(dict(start=second.isoformat(),end=(second+timedelta(seconds=driving)).isoformat(),road_profile="mixed"))
    return dict(schedule_version="schedule-plan-1",mode="manual",seed=42,interval_ms=2000 if case.get("local_scale") else 1000,
                measurement=dict(start=base.isoformat(),end=(base+timedelta(seconds=duration)).isoformat(),timezone="Asia/Seoul"),
                vehicles=vehicles)


def verify_parking_continuity(source, trip_summaries, result=None):
    from services.control.schedule import instant
    from services.control.schedule_obd import key_for

    sends=(result["parking_first_sends"] or {}) if result is not None else {}
    connects=(result["parking_connects"] or {}) if result is not None else {}
    for vehicle in source["vehicles"]:
        vehicle_id=vehicle["vehicle_id"]
        trips=[]
        starts=[]
        for trip in vehicle["trips"]:
            start,end=instant(trip["start"]),instant(trip["end"])
            tid=int(key_for(vehicle_id,start,end,trip["road_profile"])[:12],16)
            summary=trip_summaries.get(tid)
            if not summary or (summary["stt"],summary["edt"])!=(start,end):
                return False
            trips.append(summary)
            starts.append(start)
        for first,second in zip(trips,trips[1:]):
            if ((first["edlat"],first["edlon"])!=(second["stlat"],second["stlon"]) or
                    abs(first["tdis"]+second["dis"]-second["tdis"])>1e-7):
                return False
        if result is None:
            continue
        messages=sends.get(vehicle_id,[])
        if len(messages)!=2 or [m["timestamp_ms"] for m in messages]!=starts:
            return False
        if messages[0]["client_id"]!=messages[1]["client_id"] or messages[0]["client_id"] not in connects:
            return False
    return len(trip_summaries)==sum(len(vehicle["trips"]) for vehicle in source["vehicles"])


def canonical(value):
    if value is None:return b"n"
    if type(value) is bool:return b"t" if value else b"f"
    if isinstance(value,(int,float)):
        return b"d"+struct.pack(">d",0.0 if value==0 else float(value))
    if isinstance(value,str):
        raw=value.encode();return b"s"+struct.pack(">I",len(raw))+raw
    if isinstance(value,list):return b"a"+struct.pack(">I",len(value))+b"".join(canonical(v) for v in value)
    if isinstance(value,dict):return b"o"+struct.pack(">I",len(value))+b"".join(canonical(k)+canonical(value[k]) for k in sorted(value))
    raise ValueError("지문 값 형식을 확인하세요.")


def local_profile(profile):
    url=urlparse(profile.get("broker_url",""))
    if url.scheme!="ssl" or url.hostname not in ("127.0.0.1","::1") or not url.port or url.username or url.password or url.path or url.query or url.fragment:
        raise ValueError("계측은 직접 만든 루프백 mTLS 브로커만 사용합니다.")
    # 기존 fixture의 확장 프로필을 일반 송신 상한으로 다시 제한한다.
    normal=dict(profile,max_clients=20,max_messages=100,max_duration_seconds=30,
                connect_per_second=5,publish_per_second=70,timeout_ms=1000,reconnect_attempts=2)
    normal.pop("local_benchmark",None)
    return normal


class Resources:
    def __init__(self,processes,interval=.1):
        handle=int(ctypes.windll.kernel32.GetCurrentProcess()) if os.name=="nt" else 0
        self.own=SimpleNamespace(pid=os.getpid(),_handle=handle,poll=lambda:None)
        self.processes=processes;self.items={};self.stop_event=threading.Event()
        self.interval=interval
        self.peak=0;self.samples=0;self.cpu_baseline=0
        self.started=time.monotonic()
        self.rss_windows={}
        self._sample();self.cpu_baseline=self.items[self.own.pid][1].cpu
        self.thread=threading.Thread(target=self._run,daemon=True)
        self.thread.start()

    def _sample(self):
        for role,p in [("control",self.own)]+list(self.processes):
            if p.pid not in self.items:self.items[p.pid]=(role,ProcessResources(p),p)
        current=0
        for role,r,p in list(self.items.values()):
            r.sample()
            if p.poll() is None:current+=r.current_rss
        self.peak=max(self.peak,current);self.samples+=1
        window=int((time.monotonic()-self.started)//10)*10
        bucket=self.rss_windows.setdefault(window,dict(min_bytes=current,peak_bytes=0,samples=0))
        bucket["min_bytes"]=min(bucket["min_bytes"],current)
        bucket["peak_bytes"]=max(bucket["peak_bytes"],current)
        bucket["samples"]+=1

    def _run(self):
        while not self.stop_event.wait(self.interval):self._sample()

    def finish(self,elapsed):
        self.stop_event.set();self.thread.join();self._sample()
        roles={}
        for role,r,p in self.items.values():
            row=roles.setdefault(role,dict(processes=0,cpu_seconds=0,all_cpu_available=True,memory_samples=0))
            row["processes"]+=1;row["cpu_seconds"]+=max(0,r.cpu-(self.cpu_baseline if role=="control" else 0))
            row["all_cpu_available"] &= r.available;row["memory_samples"]+=r.samples
        available=all(v["all_cpu_available"] for v in roles.values())
        total=sum(v["cpu_seconds"] for v in roles.values()) if available else None
        return dict(roles=roles,cpu_seconds=total,cpu_one_core_percent=100*total/elapsed if available else None,
                    observed_peak_process_rss_sum_bytes=self.peak,samples=self.samples,sample_interval_ms=int(self.interval*1000),rss_windows=self.rss_windows,
                    all_children_exited=all(p.poll() is not None for _,_,p in self.items.values() if p is not self.own))


def child(case,profile_path,worker,temporary):
    expected=(ROOT/".local").resolve()
    folder=temporary.resolve()
    suffix=".exe" if os.name=="nt" else ""
    parking=case.get("parking_replay") is True
    worker_name="schedule-parking-worker" if parking else "schedule-measure-worker"
    if folder.parent!=expected or not folder.name.startswith("schedule-measure-") or profile_path.resolve().parent!=folder or worker.resolve()!=expected/(worker_name+suffix):
        raise ValueError("계측 명령이 만든 임시 경로와 Worker만 사용합니다.")
    validate_config(dict(cases=[case],repeats=1))
    if os.environ.get("DANGUBI_PROFILE_SCHEDULE")=="1":
        import faulthandler
        faulthandler.dump_traceback_later(30,repeat=True)
    deadline=time.monotonic()+5
    while not (temporary/"ready").exists():
        if time.monotonic()>deadline:raise RuntimeError("계측 수명 연결 대기 초과")
        time.sleep(.01)
    from fastapi.testclient import TestClient
    from services.control import app as control,schedule_execution
    profile_samples={};profile_stop=threading.Event();profile_thread=None
    if os.environ.get("DANGUBI_PROFILE_SCHEDULE")=="1":
        original_execute=schedule_execution.execute
        def profiled_execute(*args,**kwargs):
            nonlocal profile_thread
            identifier=threading.get_ident()
            def sample():
                while not profile_stop.wait(.02):
                    frame=sys._current_frames().get(identifier)
                    if frame is None:continue
                    stack=[]
                    while frame is not None:
                        stack.append(f"{Path(frame.f_code.co_filename).name}:{frame.f_code.co_name}")
                        frame=frame.f_back
                    key=" <- ".join(stack)
                    profile_samples[key]=profile_samples.get(key,0)+1
            profile_thread=threading.Thread(target=sample,daemon=True);profile_thread.start()
            try:return original_execute(*args,**kwargs)
            finally:profile_stop.set();profile_thread.join(timeout=2)
        schedule_execution.execute=profiled_execute
    profile=local_profile(json.loads(profile_path.read_text(encoding="utf-8")))
    if case.get("local_scale"):
        profile.update(local_benchmark=True,max_clients=1000,max_messages=10000,max_duration_seconds=120,connect_per_second=200,publish_per_second=5000)
    if parking:
        profile.update(max_clients=8,max_messages=128,max_duration_seconds=PARKING_TIMEOUT_SECONDS,connect_per_second=5,publish_per_second=70)
    profile_path.write_text(json.dumps(profile),encoding="utf-8")
    control.DB=temporary/"runs.sqlite3";control.CHECKPOINTS=control.CheckpointStore(temporary/"states.sqlite3")
    control.MQTT_PROFILE=profile_path;control.WORKER=worker
    processes=[];original_bridge=schedule_execution.MQTTBridge
    class Bridge(original_bridge):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs);processes.append(("sender",self.process))
    schedule_execution.MQTTBridge=Bridge
    digest=0;accepted=0;original_validate=control.validate_message
    compressed=case.get("compressed_replay",False)
    trip_summaries={}
    def validate(message):
        nonlocal digest,accepted
        original_validate(message);digest=(digest+int.from_bytes(hashlib.sha256(canonical(message)).digest(),"big"))%(1<<256);accepted+=1
        if (parking or compressed) and message["ty"]==3:
            payload=message["pld"]
            trip_summaries[payload["tid"]]={key:payload[key] for key in
                ("stt","edt","stlat","stlon","edlat","edlon","dis","tdis")}
    control.validate_message=validate
    source=schedule_source(case)
    groups=case.get("departure_groups",1)
    driving=case.get("driving_seconds",12)
    if compressed:
        groups,driving=REPLAY_ROUNDS,REPLAY_DRIVING_SECONDS
    spacing=REPLAY_SPACING_SECONDS if compressed else DEPARTURE_SPACING_SECONDS
    idle_samples=[dict(samples=0,empty_queue_samples=0,disconnected_samples=0,ready_samples=0) for _ in range(groups-1)]
    parking_samples=dict(samples=0,empty_and_disconnected=0)
    sampler=None
    with TestClient(control.app) as client:
        before=time.monotonic()
        if case.get("local_scale"):
            from scripts.scale_schedule import prepare
            start_scale=prepare(source,case,worker,profile,validate)
        else:
            plan=client.post("/api/workspace/plans",json={"title":"일정 통합 계측","schedule":source})
            if plan.status_code!=201:raise RuntimeError("측정 일정 준비 실패")
        preparation=time.monotonic()-before
        sampler=Resources(processes,interval=1 if parking else .1);started=time.monotonic()
        try:
            if case.get("local_scale"):run_id=start_scale(control.JOBS)["id"]
            else:
                response=client.post("/api/workspace/execute",json=dict(plan_key=plan.json()["key"],transport="mqtt",confirm_mqtt=True,
                         generation_workers=case["workers"],mapping_mode="shared",
                         client_count=case["clients"],connection_lifetime=case.get("lifetime","always")))
                if response.status_code!=202:raise RuntimeError("측정 실행 시작 실패")
                run_id=response.json()["id"]
            job=control.JOBS.active
            while True:
                for p in list(job.processes):
                    if not any(q.pid==p.pid for _,q in processes):processes.append(("generation",p))
                result=client.get("/api/jobs/"+run_id).json()["result"]
                if result["job_state"] not in control.ACTIVE:break
                elapsed_ms=result.get("elapsed_ms",0)
                if parking and (driving+2)*1000<=elapsed_ms<(driving+PARKING_SECONDS-2)*1000:
                    parking_samples["samples"]+=1
                    budget=result.get("buffer_budget",{})
                    parking_samples["empty_and_disconnected"]+=(
                        budget.get("reserved_messages")==budget.get("reserved_bytes")==result.get("active_clients")==0)
                    if parking_samples["samples"]==1 or parking_samples["samples"]%60==0:
                        (temporary/"progress.json").write_text(json.dumps(dict(elapsed_ms=elapsed_ms,
                            puback_count=result.get("puback_count"),**parking_samples)),encoding="utf8")
                group,within_group=divmod(elapsed_ms,spacing*1000)
                # 종료 경계 처리와 다음 그룹의 2초 선행 생성을 피해 주차 중간을 관측한다.
                if group<len(idle_samples) and driving*1000+500<=within_group<spacing*1000-2500:
                    sample=idle_samples[group]
                    sample["samples"]+=1
                    budget=result.get("buffer_budget",{})
                    sample["empty_queue_samples"]+=budget.get("reserved_messages")==budget.get("reserved_bytes")==0
                    sample["disconnected_samples"]+=result.get("active_clients")==0
                    sample["ready_samples"]+=(budget.get("reserved_messages")==budget.get("reserved_bytes")==result.get("active_clients")==0)
                if time.monotonic()-started>(PARKING_TIMEOUT_SECONDS+10 if parking else 130 if case.get("local_scale") else 40):raise RuntimeError("통합 계측 시간 초과")
                time.sleep(1 if parking else .5 if case.get("local_scale") else .05)
            elapsed=time.monotonic()-started;resources=sampler.finish(elapsed);sampler=None
            keys=("job_state","planned_messages","processed_messages","publish_attempts","mqtt_published","puback_count","unconfirmed_messages",
                  "connected_clients","active_clients","connection_count","disconnect_count","schedule_delay_ms","elapsed_ms","performance",
                  "generation_session","buffer_budget","transmission","execution_cleanup_confirmed","forced_termination")
            keys+= ("initial_connect_ms","post_connect_schedule","execution_timing","publish_windows","microtrip_count","trip_count","reconnect_count")
            row=dict(case=case,result={k:result.get(k) for k in keys},resources=resources,wall_seconds=elapsed,
                     plan_preparation_seconds=preparation,accepted_messages=accepted,fingerprint=f"{digest:064x}")
            if idle_samples:
                row["idle_gaps"]=idle_samples
            if compressed:
                row["parking_continuity"]=verify_parking_continuity(source,trip_summaries)
            if parking:
                row["parking_samples"]=parking_samples
                row["result"].update(parking_connects=result.get("parking_connects"),parking_first_sends=result.get("parking_first_sends"))
                row["parking_continuity"]=verify_parking_continuity(source,trip_summaries,row["result"])
            if profile_thread:
                job.thread.join(timeout=5)
                if job.thread.is_alive():raise RuntimeError("프로파일 종료를 확인하지 못했습니다.")
                row["control_profile"]=dict(interval_ms=20,samples=sum(profile_samples.values()),basis="실행 스레드의 대기를 포함한 벽시계 스택 표본; CPU 시간 비율이 아님",
                    stacks=[dict(stack=k,samples=v) for k,v in sorted(profile_samples.items(),key=lambda item:item[1],reverse=True)[:100]])
            return row
        finally:
            if sampler:
                active=control.JOBS.active
                if active:control.JOBS.stop(active.id);active.finished.wait(5)
                sampler.finish(time.monotonic()-started)


def measure(case,fixture,worker):
    with tempfile.TemporaryDirectory(prefix="schedule-measure-",dir=ROOT/".local") as folder:
        temporary=Path(folder);profile=temporary/"profile.json"
        env=os.environ.copy();env.update(DANGUBI_MEASURE_PROFILE=str(profile),DANGUBI_MEASURE_FAULT=case.get("fault","none"),DANGUBI_MEASURE_DIGEST="1")
        if case.get("parking_replay"):
            env["DANGUBI_MEASURE_PARKING"]="1"
        if case.get("compressed_replay"):
            env["DANGUBI_MEASURE_COMPRESSED"]="1"
        with tempfile.TemporaryFile() as log:
            broker=subprocess.Popen([str(fixture),"-test.run=^TestMQTTMeasurementFixture$"],env=env,stdout=log,stderr=log)
            try:
                deadline=time.monotonic()+10
                while not profile.exists():
                    if broker.poll() is not None or time.monotonic()>deadline:raise RuntimeError("측정 브로커 준비 실패")
                    time.sleep(.02)
                tree=ProcessTree()
                try:
                    with tempfile.TemporaryFile() as child_log:
                        process=tree.start([sys.executable,str(Path(__file__).resolve()),"--child",json.dumps(case),str(profile),str(worker),str(temporary)],stdout=subprocess.PIPE,stderr=child_log)
                        (temporary/"ready").touch()
                        try:raw,_=process.communicate(timeout=PARKING_TIMEOUT_SECONDS+20 if case.get("parking_replay") else 145 if case.get("local_scale") else 50)
                        except subprocess.TimeoutExpired:
                            if os.environ.get("DANGUBI_PROFILE_SCHEDULE")=="1":
                                child_log.seek(0);(ROOT/".local/schedule-profile-timeout.log").write_bytes(child_log.read())
                            raise
                    if process.returncode:raise RuntimeError("격리 측정 프로세스 실패")
                    row=json.loads(raw)
                finally:tree.close()
                Path(str(profile)+".done").touch();broker.wait(timeout=5)
                if broker.returncode:raise RuntimeError("브로커 정리 실패")
                observed=json.loads(Path(str(profile)+".report").read_text());row["local_broker"]=observed
                r=row["result"]
                row["integrity_passed"]=r["job_state"]=="completed" and r["puback_count"]==r["planned_messages"]==observed["received"]==row["accepted_messages"] and row["fingerprint"]==observed["fingerprint"] and observed["out_of_order"]==observed["invalid_messages"]==0 and observed["vehicle_topics"]==case["vehicles"] and observed["unique_client_ids"]==case["clients"]
                row["cleanup_passed"]=r["execution_cleanup_confirmed"] is True and row["resources"]["all_children_exited"] and not r["forced_termination"] and r["buffer_budget"]["reserved_bytes"]==r["buffer_budget"]["reserved_messages"]==0
                if case.get("compressed_replay"):
                    expected=case["clients"]*REPLAY_ROUNDS
                    row["integrity_passed"] &= (r["connection_count"]==r["disconnect_count"]==expected and
                        r["active_clients"]==r["reconnect_count"]==0 and
                        r["trip_count"]==case["vehicles"]*REPLAY_ROUNDS and
                        r["puback_count"]==case["vehicles"]*REPLAY_ROUNDS*3 and
                        observed.get("checked_client_topics")==case["vehicles"] and observed.get("client_changes")==0 and
                        observed.get("connections_by_client")=={client:REPLAY_ROUNDS for client in observed.get("connections_by_client",{})} and
                        len(observed.get("connections_by_client",{}))==case["clients"] and
                        all(gap["ready_samples"] for gap in row["idle_gaps"]) and row["parking_continuity"])
                elif "idle_gaps" in row:
                    groups=case["departure_groups"]
                    assigned=[set() for _ in range(groups)]
                    for index in range(case["vehicles"]):
                        assigned[index*groups//case["vehicles"]].add(index%case["clients"])
                    while_driving=case.get("lifetime","always")=="while_driving"
                    expected_connections=sum(map(len,assigned)) if while_driving else case["clients"]
                    row["expected_connections"]=expected_connections
                    row["integrity_passed"] &= (
                        r["connection_count"]==r["disconnect_count"]==expected_connections and
                        r["trip_count"]==case["vehicles"] and
                        r["microtrip_count"]==r["planned_messages"]-case["vehicles"] and
                        all(gap["empty_queue_samples"] and (not while_driving or gap["disconnected_samples"])
                            for gap in row["idle_gaps"]))
                if case.get("parking_replay"):
                    connects=r["parking_connects"] or {}
                    samples=row["parking_samples"]
                    row["integrity_passed"] &= (
                        r["connection_count"]==r["disconnect_count"]==case["clients"]*2 and
                        r["active_clients"]==r["reconnect_count"]==0 and
                        r["trip_count"]==case["vehicles"]*2 and r["microtrip_count"]==case["vehicles"]*12 and
                        observed.get("connections_by_client")=={client:2 for client in connects} and
                        len(connects)==case["clients"] and
                        all(len(attempts)==2 and attempts[1]["offline_ms"]>=1800000 for attempts in connects.values()) and
                        samples["samples"]>=1700 and samples["samples"]==samples["empty_and_disconnected"] and
                        row["parking_continuity"])
                roles=row["resources"]["roles"]
                row["resources_valid"]=row["resources"]["cpu_seconds"] is not None and all(roles.get(k,{}).get("processes")==n and roles[k]["memory_samples"]>0 for k,n in [("control",1),("sender",1),("generation",case["workers"])])
                return row
            finally:
                if broker.poll() is None:
                    Path(str(profile)+".done").touch()
                    try:broker.wait(timeout=5)
                    except subprocess.TimeoutExpired:broker.kill();broker.wait()


def compare(report):
    groups={};fingerprints={}
    for row in report["results"]:
        if "error" in row:continue
        c=row["case"];groups.setdefault(c["name"],[]).append(row)
        key=(c["vehicles"],c.get("history_minutes",0),c.get("local_scale",False),c.get("driving_seconds",12 if c.get("local_scale") else 4))
        key=f"vehicles={key[0]},history_minutes={key[1]},local_scale={key[2]},driving_seconds={key[3]},departure_groups={c.get('departure_groups',1)},parking_replay={c.get('parking_replay',False)},compressed_replay={c.get('compressed_replay',False)}"
        fingerprints.setdefault(key,set()).add((row["accepted_messages"],row["fingerprint"]))
    comparison=[]
    for name,rows in groups.items():
        c=rows[0]["case"]
        metrics={}
        for key,values in {
            "cpu_seconds":[r["resources"]["cpu_seconds"] for r in rows],
            "rss_sum_mib":[r["resources"]["observed_peak_process_rss_sum_bytes"]/1048576 for r in rows],
            "max_schedule_delay_ms":[r["result"]["schedule_delay_ms"] for r in rows],
            "initial_connect_ms":[r["result"].get("initial_connect_ms") for r in rows],
            "post_connect_p95_upper_ms":[(r["result"].get("post_connect_schedule") or {}).get("p95_upper_ms") if (r["result"].get("post_connect_schedule") or {}).get("count",0)>0 else None for r in rows],
            "ack_mean_ms":[(r["result"].get("performance") or {}).get("ack",{}).get("mean_ms") for r in rows]
        }.items():
            available=[v for v in values if v is not None]
            metrics[key]=dict(median=statistics.median(available),min=min(available),max=max(available)) if available else None
        comparison.append(dict(case=c,repeats=len(rows),metrics=metrics,all_passed=all(r["integrity_passed"] and r["cleanup_passed"] for r in rows)))
    return dict(cases=comparison,source_groups={k:len(v) for k,v in fingerprints.items()},
                determinism_passed=bool(fingerprints) and all(len(v)==1 for v in fingerprints.values()),
                scale_expansion_approved=False)


def main():
    if hasattr(sys.stdout,"reconfigure"):sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv)>1 and sys.argv[1]=="--child":
        row=child(json.loads(sys.argv[2]),Path(sys.argv[3]),Path(sys.argv[4]),Path(sys.argv[5]))
        print(json.dumps(row,ensure_ascii=True));return
    parser=argparse.ArgumentParser(description="일정 생성·송신 통합 로컬 측정")
    parser.add_argument("--config",type=Path,default=ROOT/"configs/schedule-measurement.example.json")
    parser.add_argument("--profile",action="store_true",help="제어 실행 스레드의 대기 포함 스택 표본을 추가 수집합니다. 계측 부하가 포함됩니다.")
    args=parser.parse_args()
    if args.profile:os.environ["DANGUBI_PROFILE_SCHEDULE"]="1"
    try:cases,repeats=validate_config(json.loads(args.config.read_text(encoding="utf-8-sig")))
    except (OSError,ValueError) as error:parser.error(str(error))
    local=ROOT/".local";local.mkdir(exist_ok=True);suffix=".exe" if os.name=="nt" else ""
    parking=cases[0].get("parking_replay",False)
    worker=local/(("schedule-parking-worker" if parking else "schedule-measure-worker")+suffix);fixture=local/("schedule-measure-fixture"+suffix)
    env=os.environ.copy();env.update(GOCACHE=str(local/"go-cache"),GOMODCACHE=str(local/"go-mod"))
    tags=["-tags","parking_benchmark"] if parking else []
    subprocess.run(["go","build",*tags,"-o",str(worker),"."],cwd=ROOT/"services/worker",env=env,check=True)
    subprocess.run(["go","test","-c","-o",str(fixture)],cwd=ROOT/"services/worker",env=env,check=True)
    directory=local/"measurements"/("schedule-"+datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%d-%H%M%S-")+uuid.uuid4().hex[:6]);directory.mkdir(parents=True)
    report=dict(schema_version="schedule-measure-2",environment=dict(os=platform.platform(),processor=platform.processor(),logical_cpus=os.cpu_count(),python=sys.version.split()[0],worker_sha256=hashlib.sha256(worker.read_bytes()).hexdigest(),config_sha256=hashlib.sha256(args.config.read_bytes()).hexdigest(),resource_scope="제어기+생성Worker+송신프로세스, 브로커 제외; 제어기 계측·TestClient 부하 포함",control_poll_ms={"normal":50,"local_scale":500},cpu_basis="100%=코어1개",memory_basis="100ms 동시 관측 RSS 합계의 최댓값; 공유 메모리 중복 가능"),results=[])
    report["environment"]["control_stack_sampling"]=os.environ.get("DANGUBI_PROFILE_SCHEDULE")=="1"
    report["environment"]["schedule_deadline_basis"]="실행 시작의 고정 시각 + 일시정지 누적 시간 + 매핑된 메시지 예정 오프셋"
    if parking:
        report["environment"].update(control_poll_ms={"parking":1000},
            memory_basis="1000ms 동시 관측 RSS 합계; 공유 메모리 중복 가능")
    def run_case(case):
        try:return measure(case,fixture,worker)
        except Exception as error:
            return dict(case=case,error=str(error) if isinstance(error,RuntimeError) else "측정 실행 또는 정리에 실패했습니다.",error_kind=type(error).__name__)

    def save_rows(rows,repeat):
        for row in rows:
            row["repeat"]=repeat+1;report["results"].append(row)
            (directory/"summary.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    if parking:
        from concurrent.futures import ThreadPoolExecutor
        print("실제 30분 주차 시험 시작: "+", ".join(c["name"] for c in cases),flush=True)
        with ThreadPoolExecutor(max_workers=len(cases)) as executor:
            save_rows(executor.map(run_case,cases),0)
    else:
        for repeat in range(repeats):
            for case in cases:
                print(f"측정 {repeat+1}/{repeats}: {case['name']}",flush=True)
                save_rows([run_case(case)],repeat)
    lines=["# 일정 생성·송신 통합 측정","","CPU100%=코어1개. 메모리는 동시 관측 RSS 합계입니다. 관측은 마지막 운행 종료 2초 후까지입니다.","","|조건|Worker|Client|무결성/정리|CPU초|관측 RSS합계 MiB|최대 지연 ms|ACK 평균 ms|","|---|---:|---:|---|---:|---:|---:|---:|"]
    for row in report["results"]:
        c=row["case"]
        if "error" in row:lines.append(f"|{c['name']}|{c['workers']}|{c['clients']}|측정 오류|—|—|—|—|");continue
        r=row["result"];s=row["resources"]
        cpu=f"{s['cpu_seconds']:.3f}" if s['cpu_seconds'] is not None else "측정 불가"
        ack=(r.get('performance') or {}).get('ack',{}).get('mean_ms')
        ack_text=f"{ack:.3f}" if ack is not None else "—"
        lines.append(f"|{c['name']}|{c['workers']}|{c['clients']}|{'통과' if row['integrity_passed'] and row['cleanup_passed'] else '미통과'}|{cpu}|{s['observed_peak_process_rss_sum_bytes']/1048576:.2f}|{r['schedule_delay_ms']}|{ack_text}|")
    (directory/"summary.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
    comparison=compare(report)
    (directory/"comparison.json").write_text(json.dumps(comparison,ensure_ascii=False,indent=2),encoding="utf-8")
    print(str(directory))
    if not comparison["determinism_passed"] or any("error" in r or not r["integrity_passed"] or not r["cleanup_passed"] or not r["resources_valid"] for r in report["results"]):raise SystemExit(1)


if __name__=="__main__":main()
