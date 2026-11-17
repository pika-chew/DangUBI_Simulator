"""측정 일정의 한 운행을 기존 Go 물리 모델로 재생성한다. 전송/DB 저장 없음."""
import hashlib
import json
import math
from pathlib import Path
import subprocess
import time

from pydantic import Field
from services.control.schedule import Strict, ScheduleRequest, instant, plan, vehicle_trips


class ScheduledOBDRequest(Strict):
    schedule: ScheduleRequest
    initial_lat: float = Field(default=37.5, ge=-85, le=85)
    initial_lon: float = Field(default=127.0, ge=-180, le=180)
    initial_odometer_m: float = Field(default=500000, ge=0, le=1e9)
    initial_coolant_c: float = Field(default=20, ge=-40, le=130)
    force_end_trip: bool = False


def key_for(vehicle, a, b, profile):
    return hashlib.sha256(f"schedule-plan-1|{vehicle}|{a}|{b}|{profile}".encode()).hexdigest()


def generate(body, vehicle_id, trip_key, worker: Path, start_ms, end_ms, offset, limit):
    req = body.schedule
    plan(req)  # 기존 일정·주차·건수 검증을 그대로 적용한다.
    vehicle = next((v for v in req.vehicles if v.vehicle_id == vehicle_id), None)
    if vehicle is None:
        raise ValueError("계획에 없는 차량입니다.")
    trips = sorted(vehicle_trips(req, vehicle))
    index = next((i for i,(a,b,p) in enumerate(trips) if key_for(vehicle_id,a,b,p)==trip_key),None)
    if index is None:
        raise ValueError("계획에 없는 운행입니다.")
    chain = trips[:index+1]
    if len(chain)>16 or sum(b-a for a,b,_ in chain)>48*3600000:
        raise ValueError("초기 상태 복원은 선행 운행 포함16회·누적48시간까지입니다. 체크포인트 연동은 후속입니다.")
    if any((b-a)%1000 for a,b,_ in chain):
        raise ValueError("현재 OBD 연동은 운행 길이가 정수 초인 일정만 지원합니다.")
    a,b,profile=chain[-1]
    low=max(a,instant(req.measurement.start))
    high=min(b,instant(req.measurement.end))
    if start_ms is not None: low=max(low,start_ms)
    if end_ms is not None: high=min(high,end_ms)
    if low>high:
        raise ValueError("선택 운행과 관측/조회 구간이 겹치지 않습니다.")
    if body.force_end_trip and high<b and high!=instant(req.measurement.end):
        raise ValueError("시험용 강제 Trip은 측정 끝에 아직 진행 중인 운행에만 요청할 수 있습니다.")
    state={"lat":body.initial_lat,"lon":body.initial_lon,"odometer_m":body.initial_odometer_m}
    coolant,heading=body.initial_coolant_c,0.0
    vehicle_seed=int(hashlib.sha256(f"{req.seed}|{vehicle_id}".encode()).hexdigest()[:7],16)
    deadline=time.monotonic()+30
    previous_end=None
    selected_initial=None
    for i,(departure,arrival,road) in enumerate(chain):
        if previous_end is not None:
            # 외기20℃·냉각 시정수2시간의 개발 근사. 실차/외기 조회값이 아니다.
            coolant=20+(coolant-20)*math.exp(-(departure-previous_end)/7200000)
        initial=dict(state,coolant_c=coolant,heading_rad=heading)
        selected=i==len(chain)-1
        key=key_for(vehicle_id,departure,arrival,road)
        payload={"start_ms":departure,"end_ms":arrival,"from_ms":low if selected else arrival,"until_ms":high if selected else arrival,"interval_ms":req.interval_ms,"offset":offset if selected else 0,"limit":limit if selected else 1,"tid":int(key[:12],16),"seed":int(hashlib.sha256(f"{req.seed}|{key}".encode()).hexdigest()[:7],16),"profile":road,**state,"continuity":{"vehicle_seed":vehicle_seed,"coolant_c":coolant,"heading_rad":heading},"force_end":body.force_end_trip if selected else False}
        remaining=deadline-time.monotonic()
        if remaining<=0: raise ValueError("상태 복원 제한시간을 초과했습니다.")
        process=subprocess.run([str(worker)],input=json.dumps({"operation":"schedule_obd","scheduled":payload}).encode(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=min(15,remaining))
        result=json.loads(process.stdout)
        if process.returncode or "scheduled" not in result:
            raise ValueError(result.get("error","일정 OBD 생성 실패"))
        output=result["scheduled"]
        state={"lat":output["final_lat"],"lon":output["final_lon"],"odometer_m":output["final_odometer_m"]}
        coolant,heading=output["final_coolant_c"],output["final_heading_rad"]
        previous_end=arrival
        if selected: selected_initial=initial
    return {"status":"generated_local_only","vehicle_id":vehicle_id,"trip_key":trip_key,"model":output["model"],"initial_state":selected_initial,"messages":output["messages"],"trace":output["trace"],"microtrip_count":output["microtrip_count"],"has_more":offset+len(output["messages"])<output["microtrip_count"],"trip":output.get("trip"),"synthetic_trip":output["synthetic_trip"],"trip_scope":"측정 끝까지 잘린 시험 요약" if output["synthetic_trip"] else "실제 운행 전체 요약", "warnings":["로컬 생성·조회이며 MQTT 발행/DB 저장은 없습니다.","첫 계획 운행부터 상태를 복원합니다. 자동 일정은 관측 기간의 앞뒤 문맥을 초기 기준으로 사용합니다.","연속성은 현재 일정 요청 기준입니다. 측정 기간 자체를 바꾸면 초기 문맥이 바뀔 수 있습니다.","공유 교통·목적지 길찾기·실차 냉각 보정은 아직 적용하지 않았습니다."]}
