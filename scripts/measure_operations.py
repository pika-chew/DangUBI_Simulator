"""격리 프로세스에서 운영 API/생성/송신을 목표 규모로 계측한다. AWS·사용자 DB는 사용하지 않는다."""
import argparse
from contextlib import contextmanager
from datetime import datetime,timedelta
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
LOCAL=ROOT/".local"

def validate(case):
    case=dict(case)
    case.setdefault("instrumentation","full")
    case.setdefault("sample_ms",250)
    bounds={"vehicles":(1,25000),"clients":(1,25000),"workers":(1,4),"window_seconds":(2,60),
            "wall_seconds":(10,120),"rss_mib":(256,4096),"publish_per_second":(1,50000),"sample_ms":(250,1000)}
    if set(case)!=set(bounds)|{"stage","phase","instrumentation"}:raise ValueError("알 수 없는 계측 설정입니다.")
    for k,(lo,hi) in bounds.items():
        if type(case[k]) is not int or not lo<=case[k]<=hi:raise ValueError(k+" 범위를 확인하세요.")
    if case["workers"] not in (1,2,4) or case["clients"]>case["vehicles"]:raise ValueError("Worker/Client 수를 확인하세요.")
    if case["stage"] not in ("generation","mqtt") or case["phase"] not in ("aligned","spread"):raise ValueError("계측 단계를 확인하세요.")
    if case["instrumentation"] not in ("full","counts"):raise ValueError("계측 모드를 확인하세요.")
    return case

def feasibility(case):
    return dict(target_microtrips_per_second=case["vehicles"]/2,
                aligned_burst_messages=case["vehicles"] if case["phase"]=="aligned" else None,
                aligned_burst_p95_service_floor_seconds=.95*case["vehicles"]/case["publish_per_second"] if case["phase"]=="aligned" else None,
                connection_admission_floor_seconds=case["clients"]/1000,
                note="설정 상한만 고려한 산술 하한입니다. 실제 처리량·AWS 할당량을 보장하지 않습니다.")

def compact_assignments(pool):
    if pool and "assignments" in pool:
        shown=pool.pop("assignments")
        pool.setdefault("assignment_count",len(shown))


def finish_broker(broker,profile_path):
    Path(str(profile_path)+".done").touch()
    if broker.wait(timeout=5)!=0:raise RuntimeError("로컬 브로커가 정상 종료되지 않았습니다.")
    report=Path(str(profile_path)+".report")
    if not report.exists():raise RuntimeError("브로커 최종 보고서가 없습니다.")
    return json.loads(report.read_text(encoding="utf-8"))

@contextmanager
def peak_schedule(case):
    from services.control import fleet,fleet_execution
    originals=fleet.iter_vehicle_days,fleet_execution.iter_vehicle_days
    def trips(req,vehicle,fixed):
        start=datetime.fromisoformat(req.measurement.start)
        phase=(int(vehicle[1:])-1)%2000 if case["phase"]=="spread" else 0
        start+=timedelta(milliseconds=phase)
        yield dict(start=start.isoformat(timespec="milliseconds"),end=(start+timedelta(seconds=180)).isoformat(timespec="milliseconds"),road_profile=fixed["road_profile"])
    fleet.iter_vehicle_days=fleet_execution.iter_vehicle_days=trips
    try:yield
    finally:fleet.iter_vehicle_days,fleet_execution.iter_vehicle_days=originals

def binary_paths():
    suffix=".exe" if os.name=="nt" else ""
    directory=Path(os.environ.get("DANGUBI_MEASURE_BIN_DIR",str(LOCAL)))
    # 별칭을 매 페이지 resolve하는 시험 구성 비용이 제품 기준선을 왜곡하지 않도록 한 번 정규화한다.
    return (directory/("operations-worker"+suffix)).resolve(),(directory/("operations-broker"+suffix)).resolve()

def build(stage):
    if os.environ.get("DANGUBI_MEASURE_BIN_DIR"):
        paths=binary_paths()
        for path in paths[:2 if stage=="mqtt" else 1]:
            if not path.is_file():raise ValueError("사전 빌드 계측 파일이 없습니다: "+str(path))
        return paths
    suffix=".exe" if os.name=="nt" else ""
    worker=LOCAL/("operations-worker"+suffix)
    fixture=LOCAL/("operations-broker"+suffix)
    env=dict(os.environ,GOCACHE=str(LOCAL/"go-cache"),GOMODCACHE=str(LOCAL/"go-mod"))
    subprocess.run(["go","build","-o",str(worker),"."],cwd=ROOT/"services/worker",env=env,check=True,timeout=120)
    if stage=="mqtt":
        subprocess.run(["go","test","-c","-o",str(fixture),"."],cwd=ROOT/"services/worker",env=env,check=True,timeout=120)
    return worker,fixture

def child(case,folder):
    case=validate(case)
    folder=folder.resolve()
    if folder.parent!=LOCAL.resolve() or not folder.name.startswith("operations-measure-"):
        raise ValueError("계측 전용 임시 경로만 사용합니다.")
    worker,fixture=binary_paths()
    profile_path=folder/"mqtt.json"
    os.environ.update(DANGUBI_DATA_DIR=str(folder),DANGUBI_DB=str(folder/"runs.sqlite3"),
        DANGUBI_CHECKPOINT_DB=str(folder/"states.sqlite3"),DANGUBI_MQTT_PROFILE=str(profile_path),DANGUBI_WORKER=str(worker))
    from fastapi.testclient import TestClient
    from services.control import app as control,schedule_execution
    from scripts.measure_schedule import Resources,canonical,local_profile
    processes=[];broker=None;sampler=None
    sent_digest=0;validated=0;validation_seconds=0.
    original_validate=control.validate_message
    def check(message):
        nonlocal sent_digest,validated,validation_seconds
        t=time.perf_counter();encoded=original_validate(message);validation_seconds+=time.perf_counter()-t
        if case["instrumentation"]=="full":sent_digest=(sent_digest+int.from_bytes(hashlib.sha256(canonical(message)).digest(),"big"))%(1<<256)
        validated+=1
        return encoded
    control.validate_message=check
    original_bridge=schedule_execution.MQTTBridge
    original_execute=schedule_execution.execute
    execution_profile=[]
    if os.environ.get("DANGUBI_MEASURE_EXECUTION_PROFILE")=="1":
        def profiled_execute(*args,**kwargs):
            import cProfile,pstats
            profile=cProfile.Profile()
            try:return profile.runcall(original_execute,*args,**kwargs)
            finally:
                rows=pstats.Stats(profile).stats
                for (filename,line,name),value in sorted(rows.items(),key=lambda item:item[1][2],reverse=True)[:25]:
                    execution_profile.append(dict(function=f"{Path(filename).name}:{line}:{name}",calls=value[1],self_seconds=value[2],inclusive_seconds=value[3]))
        schedule_execution.execute=profiled_execute
    bridge_timing={"write_calls":0,"write_seconds":0.,"poll_calls":0,"poll_seconds":0.,
                   "responses":0,"response_bytes":0,"decode_seconds":0.,"full_stats_responses":0}
    class Bridge(original_bridge):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs);processes.append(("sender",self.process))
        def _write(self,*args,**kwargs):
            t=time.perf_counter()
            try:return super()._write(*args,**kwargs)
            finally:
                bridge_timing["write_calls"]+=1;bridge_timing["write_seconds"]+=time.perf_counter()-t
        def poll(self,*args,**kwargs):
            t=time.perf_counter()
            try:return super().poll(*args,**kwargs)
            finally:
                bridge_timing["poll_calls"]+=1;bridge_timing["poll_seconds"]+=time.perf_counter()-t
        def _read(self):
            import queue
            try:
                while line:=self.process.stdout.readline(262145):
                    size=len(line.encode("utf-8"))
                    if size>262144:break
                    t=time.perf_counter();reply=json.loads(line)
                    bridge_timing["decode_seconds"]+=time.perf_counter()-t
                    bridge_timing["responses"]+=1;bridge_timing["response_bytes"]+=size
                    if reply.get("stats_complete",True):bridge_timing["full_stats_responses"]+=1
                    self.responses.put(reply,timeout=2)
                    line=None;reply=None
            except (ValueError,OSError,queue.Full):pass
            finally:
                try:self.responses.put(None,timeout=2)
                except queue.Full:pass
    schedule_execution.MQTTBridge=Bridge
    result=None;resources=None;reason=None;prepare_seconds=0.;elapsed=0.
    try:
        if case["stage"]=="mqtt":
            env=dict(os.environ,DANGUBI_MEASURE_PROFILE=str(profile_path),DANGUBI_MEASURE_DIGEST="1" if case["instrumentation"]=="full" else "0",DANGUBI_MEASURE_OPERATIONS="1")
            broker=subprocess.Popen([str(fixture),"-test.run=^TestMQTTMeasurementFixture$"],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            for _ in range(500):
                if profile_path.exists():break
                if broker.poll() is not None:raise RuntimeError("로컬 브로커가 종료됐습니다.")
                time.sleep(.02)
            if not profile_path.exists():raise RuntimeError("로컬 브로커 준비 시간 초과")
            profile=json.loads(profile_path.read_text())
            local_profile(profile) # 프로필을 수정하기 전에 주소가 루프백 mTLS인지 확인한다.
            profile.pop("local_benchmark",None)
            profile.update(execution_profile="operations",max_clients=case["clients"],
                max_messages=2_000_000,max_duration_seconds=case["window_seconds"]+600,
                connect_per_second=1000,max_connecting=100,publish_per_second=case["publish_per_second"],timeout_ms=3000)
            profile_path.write_text(json.dumps(profile),encoding="utf-8")
        source=dict(execution_profile="operations",fleet_version="fleet-plan-4",vehicle_count=case["vehicles"],
            measurement=dict(start="2026-11-19T08:00:00+09:00",end=(datetime.fromisoformat("2026-11-19T08:00:00+09:00")+timedelta(seconds=case["window_seconds"])).isoformat(),timezone="Asia/Seoul"),
            seed=42,interval_ms=2000,trip_min_minutes=3,trip_max_minutes=3)
        with peak_schedule(case),TestClient(control.app) as client:
            sampler=Resources(processes,interval=case["sample_ms"]/1000)
            t=time.monotonic()
            response=client.post("/api/workspace/plans",json=dict(title="격리 운영 피크 계측",fleet=source))
            if response.status_code!=201:raise RuntimeError("운영 계획 준비 실패: "+response.text)
            plan=response.json();prepare_seconds=time.monotonic()-t
            started=time.monotonic()
            response=client.post("/api/workspace/execute",json=dict(plan_key=plan["key"],transport="mqtt" if broker else "dry_run",
                confirm_mqtt=bool(broker),realtime=bool(broker),client_count=case["clients"],
                mapping_mode="shared",generation_workers=case["workers"]))
            if response.status_code!=202:raise RuntimeError("운영 실행 시작 실패: "+response.text)
            run_id=response.json()["id"];job=control.JOBS.active;seen=set()
            while True:
                for p in list(job.processes):
                    if p.pid not in seen:processes.append(("generation",p));seen.add(p.pid)
                result=client.get("/api/jobs/"+run_id).json()["result"]
                elapsed=time.monotonic()-started
                if result["job_state"] not in control.ACTIVE:break
                if reason is None:
                    if elapsed>=case["wall_seconds"]:reason="wall_budget"
                    elif sampler.peak>case["rss_mib"]*1048576:reason="rss_budget"
                    if reason:client.post("/api/jobs/"+run_id+"/stop")
                if elapsed>case["wall_seconds"]+25:raise RuntimeError("중단 정리 상한 초과")
                (folder/"progress.json").write_text(json.dumps(dict(elapsed_seconds=elapsed,processed=result["processed_messages"],puback=result["puback_count"],stop_reason=reason)),encoding="utf-8")
                time.sleep(case["sample_ms"]/1000)
            resources=sampler.finish(time.monotonic()-sampler.started);sampler=None
        broker_report=None
        if broker:
            broker_report=finish_broker(broker,profile_path)
        complete=result["job_state"]=="completed" and result["processed_messages"]==result["planned_messages"]
        fingerprint=f"{sent_digest:064x}" if case["instrumentation"]=="full" else None
        count_consistent=bool(broker_report and validated==result["puback_count"]==result["processed_messages"]==broker_report["received"] and result.get("connection_cleanup_confirmed") and result.get("execution_cleanup_confirmed"))
        observed_consistent=bool(broker_report and validated==result["puback_count"]==result["processed_messages"]==broker_report["received"]
            and broker_report.get("fingerprint")==fingerprint and broker_report.get("out_of_order")==0 and broker_report.get("invalid_messages")==0
            and result.get("connection_cleanup_confirmed") and result.get("execution_cleanup_confirmed"))
        mqtt_verified=complete and observed_consistent
        keys=["job_state","planned_messages","processed_messages","remaining_messages","unsent_messages","mqtt_published","puback_count","publish_attempts","unconfirmed_messages","errors",
              "payload_bytes","microtrip_count","trip_count","peak_active_clients","connected_clients","connection_count","disconnect_count",
              "generation_pool","generation_session","execution_timing","transmission","performance","transmission_goal","buffer_budget",
              "publish_rate_wait_ms","publish_rate_overshoot_ms",
              "connection_cleanup_confirmed","execution_cleanup_confirmed","execution_timing","schedule_delay_ms","simulation_time_ms"]
        report=dict(case=case,scope="운영 API/실제 OBD 생성기/순차 실행기. 시험용 동시 출발 일정이며 실제 운행 분포가 아님.",
            environment=dict(platform=sys.platform,python=sys.version.split()[0],worker_sha256=hashlib.sha256(worker.read_bytes()).hexdigest()),
            feasibility=feasibility(case),plan_preparation_seconds=prepare_seconds,execution_seconds=elapsed,
            observed_messages_per_second=result["processed_messages"]/max(elapsed,.001),stop_reason=reason,
            puback_messages_per_second=result["puback_count"]/max(elapsed,.001) if broker else None,
            completed_within_budget=complete,broker_integrity_verified=mqtt_verified if broker and case["instrumentation"]=="full" else None,
            observed_count_consistent=count_consistent if broker else None,
            observed_delivery_consistent=observed_consistent if broker and case["instrumentation"]=="full" else None,
            validated_messages=validated,validation_seconds=validation_seconds,validated_fingerprint=fingerprint if broker else None,
            broker=broker_report,resources=resources,result={k:result[k] for k in keys if k in result})
        report["resources"]["scope"]="제어기와 생성/송신 프로세스의 표본 RSS 합계·CPU. 시험 브로커·Docker 엔진·호스트 전체 사용량은 제외."
        report["instrumentation_coverage"]="규격·건수·PUBACK·본문 지문·차량별 순서" if case["instrumentation"]=="full" else "규격·건수·PUBACK. 본문 지문·차량별 순서 검사는 생략"
        report["measurement_overhead"]=f"{case["sample_ms"]}ms마다 실행 조회·RSS 표본을 수집합니다. 규격 검사는 항상 유지합니다. full 모드는 시험용 본문 지문·순서 검사도 포함합니다."
        report["bridge_timing"]=bridge_timing
        report["bridge_timing_scope"]="write/poll은 호출 경과 시간, decode는 별도 수신 스레드 시간입니다. 서로 겹칠 수 있어 합산 CPU 시간으로 해석하지 않습니다. 함수 프로파일을 켠 실행은 처리량 비교에서 제외합니다."
        report["execution_profile"]=execution_profile
        report["execution_profile_enabled"]=bool(execution_profile)
        report["execution_profile_timing_consistent"]=all(row["self_seconds"]>=0 and row["inclusive_seconds"]+1e-6>=row["self_seconds"] for row in execution_profile) if execution_profile else None
        pool=report["result"].get("generation_pool")
        compact_assignments(pool)
        (folder/"result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    finally:
        if sampler:sampler.finish(max(.001,time.monotonic()-sampler.started))
        if broker:
            Path(str(profile_path)+".done").touch()
            try:broker.wait(timeout=5)
            except subprocess.TimeoutExpired:broker.kill();broker.wait()
        control.validate_message=original_validate;schedule_execution.MQTTBridge=original_bridge
        schedule_execution.execute=original_execute

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage",choices=["generation","mqtt"],default="generation")
    parser.add_argument("--vehicles",type=int,default=25000)
    parser.add_argument("--clients",type=int,default=500)
    parser.add_argument("--workers",type=int,default=1)
    parser.add_argument("--window-seconds",type=int,default=60)
    parser.add_argument("--wall-seconds",type=int,default=60)
    parser.add_argument("--rss-mib",type=int,default=4096)
    parser.add_argument("--publish-per-second",type=int,default=15000)
    parser.add_argument("--phase",choices=["aligned","spread"],default="aligned")
    parser.add_argument("--instrumentation",choices=["full","counts"],default="full")
    parser.add_argument("--emit-json",action="store_true")
    parser.add_argument("--sample-ms",type=int,default=250)
    parser.add_argument("--output",type=Path)
    parser.add_argument("--child",type=Path,help=argparse.SUPPRESS)
    args=parser.parse_args()
    if args.child:
        case=json.loads((args.child/"case.json").read_text())
        child(case,args.child);return
    case=validate({k:getattr(args,k) for k in ("stage","vehicles","clients","workers","window_seconds","wall_seconds","rss_mib","publish_per_second","phase","instrumentation","sample_ms")})
    LOCAL.mkdir(exist_ok=True)
    output=(args.output or LOCAL/("operations-"+args.stage+".json")).resolve()
    if not output.is_relative_to(LOCAL.resolve()):raise ValueError("보고서는 프로젝트 .local 아래에 저장하세요.")
    build(case["stage"])
    from scripts.measurement_process import ProcessTree
    with tempfile.TemporaryDirectory(prefix="operations-measure-",dir=LOCAL) as name:
        folder=Path(name);(folder/"case.json").write_text(json.dumps(case))
        tree=ProcessTree()
        try:
            with (folder/"child.log").open("w",encoding="utf-8") as log:
                p=tree.start([sys.executable,str(Path(__file__).resolve()),"--child",str(folder)],cwd=ROOT,stdout=log,stderr=log,env=dict(os.environ,PYTHONUTF8="1"))
                try:p.wait(timeout=case["wall_seconds"]+60)
                except subprocess.TimeoutExpired:raise RuntimeError("계측 프로세스 전체 시간 상한 초과")
            if p.returncode or not (folder/"result.json").exists():
                raise RuntimeError((folder/"child.log").read_text(encoding="utf-8",errors="replace")[-3000:] or "결과가 없습니다.")
            report=json.loads((folder/"result.json").read_text(encoding="utf-8"))
            output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
            if args.emit_json:
                print(json.dumps(report,ensure_ascii=False));return
            print(json.dumps(dict(output=str(output),state=report["result"]["job_state"],processed=report["result"]["processed_messages"],
                planned=report["result"]["planned_messages"],messages_per_second=round(report["observed_messages_per_second"],2),
                stop_reason=report["stop_reason"],mqtt_integrity=report["broker_integrity_verified"]),ensure_ascii=False))
        finally:tree.close()

if __name__=="__main__":main()
