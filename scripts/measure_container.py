"""사용자 서버·AWS와 분리된 Linux 컨테이너에서 운영 MQTT 경로를 계측한다."""
import argparse
import json
from pathlib import Path
import subprocess
import uuid

ROOT=Path(__file__).resolve().parents[1]
LOCAL=ROOT/".local"


def run_arguments(image,name,instrumentation,window,wall,sample_ms=250):
    if instrumentation not in ("full","counts") or not 2<=window<=60 or not 10<=wall<=120 or not 250<=sample_ms<=1000:
        raise ValueError("계측 모드·시간 범위를 확인하세요.")
    return ["docker","--context","desktop-linux","run","--name",name,
        "--network","none","--read-only","--cap-drop","ALL","--security-opt","no-new-privileges",
        "--cpus","4","--memory","4g","--pids-limit","512",
        "--tmpfs","/app/.local:rw,nosuid,nodev,size=256m,uid=10001,gid=10001",
        "--tmpfs","/tmp:rw,nosuid,nodev,size=64m",image,
        "python","scripts/measure_operations.py","--stage","mqtt","--vehicles","25000",
        "--clients","500","--workers","1","--window-seconds",str(window),
        "--wall-seconds",str(wall),"--rss-mib","3072","--publish-per-second","15000",
        "--instrumentation",instrumentation,"--sample-ms",str(sample_ms),"--emit-json"]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image",default="dangubi-measure:6244df3-normalized")
    parser.add_argument("--instrumentation",choices=["full","counts"],default="full")
    parser.add_argument("--window-seconds",type=int,default=60)
    parser.add_argument("--wall-seconds",type=int,default=30)
    parser.add_argument("--sample-ms",type=int,default=250)
    parser.add_argument("--execution-profile",action="store_true",help="실행 스레드 함수 진단. 처리량 비교에 사용하지 않습니다.")
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    output=args.output.resolve()
    if not output.is_relative_to(LOCAL.resolve()):raise ValueError("보고서는 .local 아래에 저장하세요.")
    name="dangubi-measure-"+uuid.uuid4().hex[:12]
    command=run_arguments(args.image,name,args.instrumentation,args.window_seconds,args.wall_seconds,args.sample_ms)
    if args.execution_profile:
        position=command.index(args.image)
        command[position:position]=["--env","DANGUBI_MEASURE_EXECUTION_PROFILE=1"]
    docker=["docker","--context","desktop-linux"]
    image=json.loads(subprocess.check_output(docker+["image","inspect",args.image],text=True,encoding="utf-8"))[0]
    output.parent.mkdir(parents=True,exist_ok=True)
    try:
        result=subprocess.run(command,capture_output=True,text=True,encoding="utf-8",timeout=args.wall_seconds+90)
        output.with_suffix(".stderr.log").write_text(result.stderr[-12000:],encoding="utf-8")
        state=json.loads(subprocess.check_output(docker+["inspect",name],text=True,encoding="utf-8"))[0]
        if result.returncode:
            raise RuntimeError(f"컨테이너 종료 {result.returncode}, OOMKilled={state['State']['OOMKilled']}: "+result.stderr[-2500:])
        report=json.loads(result.stdout)
        report["container"]=dict(image_id=image["Id"],revision=image.get("Config",{}).get("Labels",{}).get("org.opencontainers.image.revision"),
            cpus=4,memory_bytes=4294967296,network="none",oom_killed=state["State"]["OOMKilled"],
            scope="동일 컨테이너의 제어기·생성/송신 Worker·루프백 mTLS 브로커. Windows 수치와 직접 비교하여 OS 효과로 해석하지 않음")
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps(dict(output=str(output),puback=report["result"]["puback_count"],
            puback_per_second=report["puback_messages_per_second"],count_consistent=report["observed_count_consistent"],
            integrity=report["broker_integrity_verified"],completed=report["completed_within_budget"]),ensure_ascii=False))
    finally:
        cleanup=subprocess.run(docker+["rm","-f",name],capture_output=True,timeout=15)
        if cleanup.returncode:raise RuntimeError("시험 컨테이너 정리를 확인하지 못했습니다: "+cleanup.stderr.decode("utf-8",errors="replace")[-1000:])


if __name__=="__main__":main()
