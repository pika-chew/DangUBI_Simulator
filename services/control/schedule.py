"""일정만 계획한다. OBD 생성·MQTT·실행 DB와 분리한다."""
from datetime import date, datetime, timedelta, timezone
import hashlib
import random
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from services.control.operating import TRIAL_VEHICLES

KST = timezone(timedelta(hours=9))


def instant(value: str) -> int:
    if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,3})?(?:Z|[+-]\d\d:\d\d)", value):
        raise ValueError("시각은 UTC offset을 포함한 ISO8601 형식(최대 밀리초)이어야 합니다.")
    return round(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Measurement(Strict):
    start: str
    end: str
    timezone: Literal["Asia/Seoul"] = "Asia/Seoul"

    @model_validator(mode="after")
    def bounds(self):
        a, b = instant(self.start), instant(self.end)
        if not 0 <= a < b <= 4102444800000 or b-a > 30*86400000:
            raise ValueError("측정 기간은 1970~2100년 범위의 양수 기간·최대30일입니다.")
        return self


class Trip(Strict):
    start: str
    end: str
    road_profile: Literal["urban", "highway", "congested", "mixed"] = "mixed"

    @model_validator(mode="after")
    def bounds(self):
        a, b = instant(self.start), instant(self.end)
        if not 0 <= a < b <= 4102444800000 or b-a > 86400000:
            raise ValueError("운행은 양수 기간·최대24시간이어야 합니다.")
        return self


class Slot(Strict):
    start_time: str
    duration_min_minutes: int = Field(ge=1, le=1440)
    duration_max_minutes: int = Field(ge=1, le=1440)
    departure_jitter_minutes: int = Field(default=0, ge=0, le=120)
    road_profile: Literal["urban", "highway", "congested", "mixed"] = "mixed"

    @model_validator(mode="after")
    def bounds(self):
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", self.start_time) or self.duration_min_minutes > self.duration_max_minutes:
            raise ValueError("출발 HH:MM과 운행 길이 범위를 확인하세요.")
        return self


class Pattern(Strict):
    weekdays: list[int] = Field(default_factory=lambda: [1,2,3,4,5], max_length=7)
    day_off_probability: float = Field(default=0, ge=0, le=1)
    baseline_jitter_minutes: int = Field(default=0, ge=0, le=120)
    slots: list[Slot] = Field(max_length=16)

    @model_validator(mode="after")
    def days(self):
        if len(set(self.weekdays)) != len(self.weekdays) or any(d < 1 or d > 7 for d in self.weekdays):
            raise ValueError("요일은 월요일1~일요일7이며 중복 없이 지정하세요.")
        return self


class Vehicle(Strict):
    vehicle_id: str = Field(pattern=r"^v[0-9]{6}$")
    trips: list[Trip] | None = Field(default=None, max_length=1000)
    pattern: Pattern | None = None
    overrides: dict[str, list[Trip]] = Field(default_factory=dict, max_length=32)

    @model_validator(mode="after")
    def exceptions(self):
        for day, trips in self.overrides.items():
            if date.fromisoformat(day).isoformat() != day or len(trips) > 16:
                raise ValueError("날짜 예외는 YYYY-MM-DD·하루 최대16회입니다.")
            if any(datetime.fromtimestamp(instant(t.start)/1000, KST).date().isoformat() != day for t in trips):
                raise ValueError("날짜 예외의 출발일은 해당 한국 날짜여야 합니다.")
        return self


class ScheduleRequest(Strict):
    include_em: bool = True
    schedule_version: Literal["schedule-plan-1"] = "schedule-plan-1"
    mode: Literal["manual", "patterned"]
    seed: int = Field(default=42, ge=-2147483648, le=2147483647)
    interval_ms: int = Field(default=2000, ge=100, le=86400000)
    measurement: Measurement
    vehicles: list[Vehicle] = Field(min_length=1, max_length=TRIAL_VEHICLES)

    @model_validator(mode="after")
    def modes(self):
        self.include_em = True
        if sum(len(v.trips or []) for v in self.vehicles) > 10000:
            raise ValueError("직접 지정 운행은 실행 전체 최대10,000회입니다.")
        if len({v.vehicle_id for v in self.vehicles}) != len(self.vehicles):
            raise ValueError("차량 ID가 중복되었습니다.")
        for v in self.vehicles:
            if self.mode == "manual" and (v.trips is None or v.pattern is not None or v.overrides):
                raise ValueError("직접 지정은 trips 목록만 사용합니다. 미운행은 빈 목록입니다.")
            if self.mode == "patterned" and (v.trips is not None or v.pattern is None):
                raise ValueError("자동 일정은 pattern과 날짜별 overrides를 사용합니다.")
        return self


def rng_for(req, *parts):
    digest = hashlib.sha256((str(req.seed)+"|"+req.schedule_version+"|"+"|".join(parts)).encode()).digest()
    return random.Random(int.from_bytes(digest, "big"))


def iter_vehicle_trips(req, vehicle):
    if req.mode == "manual":
        for t in sorted(vehicle.trips,key=lambda t:(instant(t.start),instant(t.end),t.road_profile)):
            yield instant(t.start),instant(t.end),t.road_profile
        return
    import heapq
    pattern=vehicle.pattern
    first=datetime.fromtimestamp(instant(req.measurement.start)/1000,KST).date()-timedelta(days=2)
    last=datetime.fromtimestamp(instant(req.measurement.end)/1000,KST).date()+timedelta(days=1)
    baseline=rng_for(req,vehicle.vehicle_id,"baseline").randint(-pattern.baseline_jitter_minutes,pattern.baseline_jitter_minutes)
    pending=[]
    for n in range((last-first).days+1):
        day=first+timedelta(days=n)
        if day.isoformat() in vehicle.overrides:
            for t in vehicle.overrides[day.isoformat()]:heapq.heappush(pending,(instant(t.start),instant(t.end),t.road_profile))
        elif day.isoweekday() in pattern.weekdays and rng_for(req,vehicle.vehicle_id,day.isoformat(),"off").random()>=pattern.day_off_probability:
            for index,slot in enumerate(pattern.slots):
                rng=rng_for(req,vehicle.vehicle_id,day.isoformat(),str(index))
                hour,minute=map(int,slot.start_time.split(":"))
                a=datetime(day.year,day.month,day.day,hour,minute,tzinfo=KST)+timedelta(minutes=baseline+rng.randint(-slot.departure_jitter_minutes,slot.departure_jitter_minutes))
                duration=rng.randint(slot.duration_min_minutes,slot.duration_max_minutes)
                start=round(a.timestamp()*1000)
                heapq.heappush(pending,(start,start+duration*60000,slot.road_profile))
        # 다음 날짜의 가장 이른 출발 이전만 출력한다. 기준/날짜별 편차 각각 최대120분을 고려한다.
        boundary=round((datetime(day.year,day.month,day.day,tzinfo=KST)+timedelta(days=1,hours=-4)).timestamp()*1000)
        while pending and pending[0][0]<boundary:yield heapq.heappop(pending)
    while pending:yield heapq.heappop(pending)


def vehicle_trips(req,vehicle):
    return list(iter_vehicle_trips(req,vehicle))


def observed_row(vehicle,a,b,profile,start,end,interval,version="schedule-plan-1"):
    if b<start or a>end:return None
    key=hashlib.sha256(f"{version}|{vehicle}|{a}|{b}|{profile}".encode()).hexdigest()
    first=max(0,(start-a+interval-1)//interval)
    last=(min(end,b-1)-a)//interval
    return {"vehicle_id":vehicle,"trip_key":key,"tid":int(key[:12],16),"start_ms":a,"end_ms":b,"road_profile":profile,"observed_start_ms":max(a,start),"observed_end_ms":min(b,end),"started_before_window":a<start,"continues_after_window":b>end,"microtrip_count":max(0,last-first+1),"trip_count":int(start<=b<=end)}


def plan(req: ScheduleRequest, offset=0, limit=100, vehicle_id=None, trip_provider=None):
    if vehicle_id is not None and not any(v.vehicle_id == vehicle_id for v in req.vehicles):
        raise ValueError("계획에 없는 차량입니다.")
    start, end = instant(req.measurement.start), instant(req.measurement.end)
    rows, inactive, summaries, total, micros, completed = [], [], [], 0, 0, 0
    seen = set()
    for vehicle in sorted(req.vehicles, key=lambda v:v.vehicle_id):
        trips = sorted(trip_provider(req, vehicle) if trip_provider else vehicle_trips(req, vehicle))
        previous = None
        observed, driving_ms = 0, 0
        for a, b, profile in trips:
            if previous is not None and a-previous < 1800000:
                raise ValueError(f"{vehicle.vehicle_id}: 운행 중첩 또는 최소 주차30분 미달입니다. 자동 편차/날짜 예외도 확인하세요.")
            previous = b
            if b < start or a > end:
                continue
            total += 1
            if total > 10000:
                raise ValueError("관측 범위의 일정은 최대10,000회입니다. 차량이나 운행 슬롯을 줄이세요.")
            observed += 1
            driving_ms += max(0, min(b,end)-max(a,start))
            key = hashlib.sha256(f"{req.schedule_version}|{vehicle.vehicle_id}|{a}|{b}|{profile}".encode()).hexdigest()
            tid = int(key[:12], 16)
            if tid in seen:
                raise ValueError("운행 번호 충돌입니다. 계획을 다시 설정하세요.")
            seen.add(tid)
            first = max(0, (start-a+req.interval_ms-1)//req.interval_ms)
            last = (min(end,b-1)-a)//req.interval_ms
            count = max(0, last-first+1)
            trip_count = int(start <= b <= end)
            micros += count
            completed += trip_count
            rows.append({"vehicle_id":vehicle.vehicle_id,"trip_key":key,"tid":tid,"start_ms":a,"end_ms":b,"road_profile":profile,"observed_start_ms":max(a,start),"observed_end_ms":min(b,end),"started_before_window":a<start,"continues_after_window":b>end,"microtrip_count":count,"trip_count":trip_count})
        if not observed:
            inactive.append(vehicle.vehicle_id)
        summaries.append({"vehicle_id":vehicle.vehicle_id,"observed_trip_count":observed,"driving_ms":driving_ms,"parking_ms":end-start-driving_ms})
    rows.sort(key=lambda r:(r["start_ms"],r["vehicle_id"],r["trip_key"]))
    matched = [r for r in rows if vehicle_id is None or r["vehicle_id"] == vehicle_id]
    return {"schedule_version":req.schedule_version,"status":"plan_only","minimum_parking_seconds":1800,"measurement_start_ms":start,"measurement_end_ms":end,"vehicle_count":len(req.vehicles),"observed_trip_count":len(rows),"vehicles":summaries,"inactive_vehicle_ids":inactive,"microtrip_count":micros,"trip_count":completed,"planned_messages":micros+completed,"offset":offset,"limit":limit,"trips":matched[offset:offset+limit],"matched_trip_count":len(matched),"has_more":offset+limit<len(matched),"warnings":["일정 미리보기는 계획만 반환합니다. OBD 조회와 전체 일정 MQTT 전송은 별도 API에서 실행합니다.","자동 일정은 개발용 계획이며 실제 교통에 따른 도착 시각 계산은 후속입니다."]}
