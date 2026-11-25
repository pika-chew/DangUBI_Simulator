"""운영 계획의 페이지와 합계만 계산한다. 전체 운행/메시지 목록은 만들지 않는다."""
import heapq
import sqlite3
from services.control.schedule import instant, observed_row


def preview(req, assigned, days, offset=0, limit=100, vehicle_id=None):
    if vehicle_id is not None and vehicle_id not in assigned:
        raise ValueError("계획에 없는 차량입니다.")
    start,end=instant(req.measurement.start),instant(req.measurement.end)
    population=len(assigned)
    anomaly_count=sum(v["anomaly_eligible"] for v in assigned.values())
    if vehicle_id is not None:assigned={vehicle_id:assigned[vehicle_id]}
    summaries,inactive=[],[]
    inactive_count=0
    totals=dict(observed_trip_count=0,microtrip_count=0,trip_count=0,matched_trip_count=0)
    # 전역 Trip 번호 중복 검사는 임시 디스크 색인으로 수행하고 연결 종료 시 삭제한다.
    ids=sqlite3.connect("")
    ids.execute("PRAGMA cache_size=-2048")
    ids.execute("CREATE TABLE ids (tid INTEGER PRIMARY KEY)")
    def rows():
        nonlocal inactive_count
        for vehicle,fixed in assigned.items():
            observed=driving=0
            previous=None
            for trip in days(req,vehicle,fixed):
                a,b=instant(trip["start"]),instant(trip["end"])
                if previous is not None and a-previous<1800000:
                    raise ValueError(f"{vehicle}: 운행 중첩 또는 최소 주차30분 미달입니다.")
                previous=b
                row=observed_row(vehicle,a,b,trip["road_profile"],start,end,req.interval_ms)
                if row is None:continue
                try:ids.execute("INSERT INTO ids VALUES (?)",(row["tid"],))
                except sqlite3.IntegrityError:raise ValueError("운행 번호 충돌입니다. 계획을 다시 설정하세요.") from None
                observed+=1;driving+=max(0,min(b,end)-max(a,start))
                totals["observed_trip_count"]+=1
                totals["microtrip_count"]+=row["microtrip_count"];totals["trip_count"]+=row["trip_count"]
                if vehicle_id is None or vehicle_id==vehicle:
                    totals["matched_trip_count"]+=1
                    yield row
            if not observed:
                inactive_count+=1
                if len(inactive)<100:inactive.append(vehicle)
            if len(summaries)<100:
                summaries.append(dict(vehicle_id=vehicle,observed_trip_count=observed,driving_ms=driving,
                                      parking_ms=end-start-driving,**fixed))
    try:
        page=heapq.nsmallest(offset+limit,rows(),key=lambda r:(r["start_ms"],r["vehicle_id"],r["trip_key"]))[offset:]
    except sqlite3.Error:
        raise ValueError("운영 계획의 임시 색인을 사용할 수 없습니다. 임시 디렉터리와 디스크 여유를 확인하세요.") from None
    finally:ids.close()
    return dict(schedule_version="schedule-plan-1",fleet_version=req.fleet_version,execution_profile=req.execution_profile,
                status="plan_only",minimum_parking_seconds=1800,measurement_start_ms=start,measurement_end_ms=end,
                vehicle_count=len(assigned),fleet_vehicle_count=population,summary_scope="selected_vehicle" if vehicle_id else "fleet",
                vehicles=summaries,inactive_vehicle_ids=inactive,inactive_vehicle_count=inactive_count,vehicle_list_limit=100,**totals,
                planned_messages=totals["microtrip_count"]+totals["trip_count"],offset=offset,limit=limit,trips=page,
                has_more=offset+len(page)<totals["matched_trip_count"],anomaly_target_count=anomaly_count,
                warnings=["운영 계획은 순차 합계와 요청한 페이지만 보관합니다. 실제 처리량과 장기 안정성은 별도 검증 대상입니다.",
                          "개발용 합성 일정이며 한국 실차 통계로 보정하지 않았습니다."])
