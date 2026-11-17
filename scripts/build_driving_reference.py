"""EPA 공개 1Hz 시험 사이클의 비교용 집계 생성. 원문은 .local에만 보관한다."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import math
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
SOURCES = [("UDDS", "미국 도시 주행 시험 사이클", "https://www.epa.gov/sites/default/files/2015-10/uddscol.txt"), ("HWFET", "미국 고속 주행 시험 사이클", "https://www.epa.gov/sites/default/files/2015-10/hwycol.txt")]
EDGES = [0,20/3.6,40/3.6,60/3.6,80/3.6,100/3.6,math.inf]
LABELS = ["정차 (0)","0 초과~20 미만","20~40 미만","40~60 미만","60~80 미만","80~100 미만","100 이상"]

def summarize(raw):
    points = []
    for line in raw.decode("ascii").splitlines():
        fields = line.split()
        if len(fields)!=2:
            continue
        try:
            points.append((float(fields[0]),float(fields[1])*.44704))
        except ValueError:
            pass
    if len(points)<2 or points[0][0]!=0 or any(not math.isfinite(t) or not math.isfinite(v) for t,v in points) or any(t1-t0!=1 or v0<0 or v1<0 for (t0,v0),(t1,v1) in zip(points,points[1:])):
        raise ValueError("EPA 시간·속도 원문 형식을 확인하세요.")
    duration = points[-1][0]
    distance = idle = 0.
    speed = [0.]*7
    for (t0,v0),(t1,v1) in zip(points,points[1:]):
        dt=t1-t0
        distance+=(v0+v1)*dt/2
        if v0==v1==0:
            idle+=dt;speed[0]+=dt
        elif v0==v1:
            for i,(lo,hi) in enumerate(zip(EDGES,EDGES[1:]),1):
                if lo<=v0<hi:speed[i]+=dt
        else:
            a=(v1-v0)/dt
            for i,(lo,hi) in enumerate(zip(EDGES,EDGES[1:]),1):
                x,y=sorted(((lo-v0)/a,(hi-v0)/a))
                speed[i]+=max(0,min(dt,y)-max(0,x))
    return dict(vehicle_seconds=duration,distance_m=distance,average_speed_kmh=distance/duration*3.6,peak_speed_kmh=max(v for _,v in points)*3.6,idle_seconds=idle,idle_percent=idle/duration*100,speed_bins=[dict(label=label,seconds=seconds,percent=seconds/duration*100) for label,seconds in zip(LABELS,speed)])

def main():
    cache=ROOT/".local/driving-reference";cache.mkdir(parents=True,exist_ok=True)
    cycles=[]
    for name,description,url in SOURCES:
        with urllib.request.urlopen(url,timeout=30) as response:raw=response.read()
        (cache/(name+".txt")).write_bytes(raw)
        cycles.append(dict(name=name,description=description,url=url,sha256=hashlib.sha256(raw).hexdigest(),summary=summarize(raw)))
    output=dict(version="epa-reference-1",retrieved_at=datetime.now(timezone.utc).isoformat(),method="1Hz 목표 속도를 구간별 선형 보간, mph×0.44704=m/s, 속도 사다리꼴 적분, 양 끝 속도0인 구간만 정차로 집계. 한국 실차 대표성·합격 기준·모델 보정값이 아님.",source_page="https://www.epa.gov/vehicle-and-fuel-emissions-testing/dynamometer-drive-schedules",cycles=cycles)
    (ROOT/"configs/driving-reference.json").write_text(json.dumps(output,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    for cycle in cycles:print(cycle["name"],cycle["summary"]["vehicle_seconds"],cycle["summary"]["average_speed_kmh"],cycle["summary"]["idle_percent"])

if __name__=="__main__":main()
