"""격리 컨테이너의 생성·종료·볼륨 보존 검사. 외부 MQTT 접속 없음."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
PROBE = '''
import json, os, time, urllib.request
from pathlib import Path
from services.control.runtime import RuntimePaths
from services.control.checkpoints import CheckpointStore
def api(path, body=None):
    data=None if body is None else json.dumps(body).encode()
    request=urllib.request.Request('http://127.0.0.1:8080'+path,data=data,headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(request,timeout=15) as response:
        return json.load(response)
assert os.getuid()!=0
health=api('/api/health')
assert health['worker_ready'] and not health['mqtt_ready'] and health['active_job_id'] is None
assert '<html' in urllib.request.urlopen('http://127.0.0.1:8080/',timeout=5).read().decode().lower()
paths=RuntimePaths.from_env(Path('/app'))
assert paths.database.parent==paths.checkpoints.parent==Path('/data')
cache=CheckpointStore(paths.checkpoints)
if __PHASE__ == 'create':
    assert api('/api/runs')==[]
    body={'config':{'vehicle_count':3,'mapping_mode':'shared','client_count':2,'messages_per_vehicle':2,
          'interval_ms':2000,'connection_lifetime':'always','publish_mode':'serial','max_in_flight':1,'queue_capacity':100},
          'message':api('/api/schema')['profiles']['4']['example'],'transport':'dry_run','confirm_mqtt':False,'realtime':False}
    run=api('/api/runs',body)
    assert run['result']['processed_messages']==6 and run['result']['mqtt_published']==0
    fleet={'execution_profile':'operations','fleet_version':'fleet-plan-4','vehicle_count':3,
           'measurement':{'start':'2026-11-19T00:00:00+09:00','end':'2026-11-20T00:00:00+09:00','timezone':'Asia/Seoul'},
           'seed':42,'interval_ms':60000,'trip_min_minutes':3,'trip_max_minutes':3,
           'day_off_probability':0,'weekend_activity_probability':1}
    plan=api('/api/workspace/plans',{'title':'container operations smoke','fleet':fleet})
    operation=api('/api/workspace/execute',{'plan_key':plan['key'],'transport':'dry_run',
                  'confirm_mqtt':False,'realtime':False,'mapping_mode':'shared','client_count':2})
    deadline=time.monotonic()+20
    while time.monotonic()<deadline:
        result=api('/api/jobs/'+operation['id'])['result']
        if result['job_state'] not in {'starting','running','stopping'}:break
        time.sleep(.1)
    assert result['job_state']=='completed',result
    assert result['processed_messages']==result['planned_messages'] and result['trip_count']>0
    assert result['generation_session']['batch_requests']>0 and result['execution_cleanup_confirmed']
    state={'lat':37.5,'lon':127.0,'odometer_m':1000,'coolant_c':80,'heading_rad':0}
    cache.put('container-smoke',0,1000,state,cache.stats()['epoch'])
    body['realtime']=True
    body['config']['messages_per_vehicle']=30
    job=api('/api/jobs',body)
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        result=api('/api/jobs/'+job['id'])['result']
        if result['processed_messages']>0:break
        time.sleep(.1)
    else:raise AssertionError('Worker did not start')
    print(json.dumps({'run_id':run['id'],'job_id':job['id'],'operations_job_id':operation['id']}))
else:
    records=api('/api/runs')
    assert len(records)==3
    full=[r for r in records if r['result'].get('processed_messages')==6 and 'job_state' not in r['result']]
    stopped=[r for r in records if r['result'].get('job_state')=='stopped']
    assert len(full)==len(stopped)==1
    completed=[r for r in records if r['result'].get('job_state')=='completed']
    assert len(completed)==1 and completed[0]['result']['generation_session']['batch_requests']>0
    assert not stopped[0]['result']['forced_termination']
    epoch,resumed=cache.resume('container-smoke',1)
    assert resumed is not None and cache.stats()['entries']==1
    print(json.dumps({'record_count':len(records),'cache_entries':1,'shutdown':'stopped'}))
'''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--context")
    args = parser.parse_args(argv)
    if not shutil.which("docker"):
        print("Docker 명령이 필요합니다.", file=sys.stderr)
        return 1
    docker = ["docker"] + (["--context", args.context] if args.context else [])
    suffix = uuid4().hex[:12]
    volume, container = f"dangubi-check-data-{suffix}", f"dangubi-check-{suffix}"

    def run(*parts, **kwargs):
        return subprocess.run(docker + list(parts), cwd=ROOT, check=True, **kwargs)

    def start():
        run("run", "--detach", "--name", container, "--network", "none", "--read-only",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m", "--stop-timeout", "30",
            "--mount", f"type=volume,source={volume},target=/data", args.image, stdout=subprocess.DEVNULL)
        for _ in range(60):
            health = subprocess.run(docker + ["inspect", "--format", "{{.State.Health.Status}}", container],
                                    capture_output=True, text=True)
            if health.returncode == 0 and health.stdout.strip() == "healthy":
                return
            time.sleep(1)
        raise RuntimeError("컨테이너가 준비되지 않았습니다.")

    volume_created = False
    try:
        run("volume", "create", volume, stdout=subprocess.DEVNULL)
        volume_created = True
        start()
        run("exec", "-i", container, "python", "-", input=PROBE.replace("__PHASE__", "'create'").encode())
        run("stop", "--time", "30", container, stdout=subprocess.DEVNULL)
        code = run("inspect", "--format", "{{.State.ExitCode}}", container, capture_output=True, text=True).stdout.strip()
        # Uvicorn은 정리 후 SIGTERM을 다시 발생시킬 수 있다. 다음 단계의 저장 상태도 함께 검사한다.
        if code not in {"0", "143"}:
            raise RuntimeError(f"컨테이너가 정상 종료되지 않았습니다: {code}")
        run("rm", container, stdout=subprocess.DEVNULL)
        start()
        run("exec", "-i", container, "python", "-", input=PROBE.replace("__PHASE__", "'verify'").encode())
        print("컨테이너 생성·정상 종료·교체 후 실행 기록/조회 캐시 보존 확인 완료")
        return 0
    except (subprocess.CalledProcessError, OSError, RuntimeError) as error:
        print(f"컨테이너 확인 실패: {error}", file=sys.stderr)
        subprocess.run(docker + ["logs", "--tail", "40", container], cwd=ROOT)
        return 1
    finally:
        # 이 검사에서 만든 고유 이름의 자원만 정리한다. 기존 컨테이너·사용자 데이터는 건드리지 않는다.
        subprocess.run(docker + ["rm", "--force", container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if volume_created:
            subprocess.run(docker + ["volume", "rm", volume], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    sys.exit(main())
