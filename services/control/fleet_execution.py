"""실행 전 배정 한 번, 차량별 현재 운행/종료 상태 한 개로 순차 생성한다."""
import hashlib
import math
import threading
from services.control.fleet import allocation, iter_vehicle_days
from services.control.schedule import instant, observed_row
from services.control.schedule_obd import key_for
from services.control.generation_session import GenerationFailure


class FleetExecution:
    def __init__(self, req, worker, force_end=False):
        self.req,self.worker,self.force_end=req,worker,force_end
        self.assigned=allocation(req)
        self.start,self.end=instant(req.measurement.start),instant(req.measurement.end)
        self.cursors={}
        self.lock=threading.Lock()
        self.preceding=0;self.pages=0

    def trips(self, vehicle):
        for t in iter_vehicle_days(self.req,vehicle,self.assigned[vehicle]):
            row=observed_row(vehicle,instant(t["start"]),instant(t["end"]),t["road_profile"],self.start,self.end,self.req.interval_ms)
            if row is not None:yield row

    def stats(self):
        with self.lock:
            return dict(entries=len(self.cursors),max_entries=len(self.assigned),epoch=0,
                        allocation_builds=1,page_requests=self.pages,preceding_trips=self.preceding,mode="sequential_vehicle_state")

    def generate(self, vehicle, key, offset, session=None, limit=100):
        if session is None:raise GenerationFailure("운영 생성은 실행 전용 Worker 세션이 필요합니다.")
        session.worker_digest(self.worker)
        with self.lock:
            cursor=self.cursors.get(vehicle)
            if cursor is None:
                cursor=dict(trips=iter(iter_vehicle_days(self.req,vehicle,self.assigned[vehicle])),current=None,
                            final=dict(lat=37.5,lon=127.0,odometer_m=500000,coolant_c=20.,heading_rad=0.),last_end=None,index=0)
                self.cursors[vehicle]=cursor
        fixed=self.assigned[vehicle]
        for _ in range(16):
            if cursor["current"] is None or cursor["key"]!=key:
                if offset!=0:raise GenerationFailure("운영 운행의 페이지 순서를 확인하세요.")
                trip=next(cursor["trips"],None)
                if trip is None:raise GenerationFailure("계획에 없는 운행입니다.")
                a,b=instant(trip["start"]),instant(trip["end"])
                if (b-a)%1000:raise GenerationFailure("현재 OBD 연동은 정수 초 운행을 지원합니다.")
                initial=dict(cursor["final"])
                if cursor["last_end"] is not None:
                    if a-cursor["last_end"]<1800000:raise GenerationFailure("최소 주차30분을 확인하세요.")
                    initial["coolant_c"]=20+(initial["coolant_c"]-20)*math.exp(-(a-cursor["last_end"])/7200000)
                cursor.update(current=trip,key=key_for(vehicle,a,b,trip["road_profile"]),initial=initial,a=a,b=b)
            a,b=cursor["a"],cursor["b"]
            selected=cursor["key"]==key
            # 이미 관측 범위에 진입한 운행을 건너뛰거나 되감지 않는다.
            if not selected and observed_row(vehicle,a,b,cursor["current"]["road_profile"],self.start,self.end,self.req.interval_ms) is not None:
                raise GenerationFailure("운영 운행은 계획의 순서대로 생성해야 합니다.")
            payload=dict(start_ms=a,end_ms=b,from_ms=max(a,self.start) if selected else b,
                         until_ms=min(b,self.end) if selected else b,interval_ms=self.req.interval_ms,include_em=self.req.include_em,
                         offset=offset if selected else 0,limit=limit if selected else 1,
                         tid=int(cursor["key"][:12],16),seed=int(hashlib.sha256(f"{self.req.seed}|{cursor['key']}".encode()).hexdigest()[:7],16),
                         profile=cursor["current"]["road_profile"],**{k:cursor["initial"][k] for k in ("lat","lon","odometer_m")},
                         continuity=dict(vehicle_seed=int(hashlib.sha256(f"{self.req.seed}|{vehicle}".encode()).hexdigest()[:7],16),
                                         coolant_c=cursor["initial"]["coolant_c"],heading_rad=cursor["initial"]["heading_rad"]),
                         force_end=self.force_end if selected else False,anomaly_eligible=fixed["anomaly_eligible"],
                         anomaly_events_per_hour=self.req.anomaly_events_per_hour,anomaly_intensity=self.req.anomaly_intensity)
            output=session.page(payload)
            session.worker_digest(self.worker)
            cursor["final"]=dict(lat=output["final_lat"],lon=output["final_lon"],odometer_m=output["final_odometer_m"],
                                 coolant_c=output["final_coolant_c"],heading_rad=output["final_heading_rad"])
            cursor["last_end"]=b
            if selected:
                with self.lock:self.pages+=1
                return dict(status="generated_local_only",messages=output["messages"],has_more=offset+len(output["messages"])<output["microtrip_count"],
                            trip=output.get("trip"),synthetic_trip=output["synthetic_trip"],initial_state=cursor["initial"])
            cursor["current"]=None;cursor["index"]+=1
            with self.lock:self.preceding+=1
        return dict(status="preparing",restoration=dict(completed_preceding_trips=cursor["index"]))
