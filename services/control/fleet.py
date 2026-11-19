"""전체 조건으로 차량 명부와 일정을 배정한다. 실차 통계가 아닌 개발 모델."""
from datetime import datetime, timedelta
import hashlib
import math
import random
from typing import Literal

from pydantic import Field, model_validator
from services.control.schedule import Strict, Measurement, ScheduleRequest, KST, instant, plan
from services.control.journey_schedule import varied_day
from services.control.schedule_obd import ScheduledOBDRequest, generate, key_for


class PatternMix(Strict):
    commute: int = Field(default=60, ge=0, le=100)
    daytime: int = Field(default=20, ge=0, le=100)
    flexible: int = Field(default=20, ge=0, le=100)

    @model_validator(mode="after")
    def total(self):
        if self.commute + self.daytime + self.flexible != 100:
            raise ValueError("생활패턴 비율의 합은 100%여야 합니다.")
        return self


class RoadMix(Strict):
    urban: int = Field(default=50, ge=0, le=100)
    highway: int = Field(default=20, ge=0, le=100)
    congested: int = Field(default=10, ge=0, le=100)
    mixed: int = Field(default=20, ge=0, le=100)

    @model_validator(mode="after")
    def total(self):
        if sum(self.model_dump().values()) != 100:
            raise ValueError("도로 구성 비율의 합은 100%여야 합니다.")
        return self


class FleetRequest(Strict):
    include_em: bool = False
    execution_profile: Literal["small_test", "operations"] = "small_test"
    fleet_version: Literal["fleet-plan-1", "fleet-plan-2", "fleet-plan-3", "fleet-plan-4"] = "fleet-plan-1"
    vehicle_count: int = Field(default=20, ge=1, le=25000)
    measurement: Measurement
    seed: int = Field(default=42, ge=-2147483648, le=2147483647)
    interval_ms: int = Field(default=2000, ge=100, le=86400000)
    daily_driving_minutes: int | None = Field(default=None, ge=10, le=480)
    trip_min_minutes: int | None = Field(default=None, ge=2, le=240)
    trip_max_minutes: int | None = Field(default=None, ge=2, le=240)
    vehicle_variation_percent: int | None = Field(default=None, ge=0, le=50)
    daily_variation_percent: int = Field(default=25, ge=0, le=50)
    departure_jitter_minutes: int = Field(default=30, ge=0, le=120)
    day_off_probability: float = Field(default=0.2, ge=0, le=1)
    weekend_activity_probability: float = Field(default=0.5, ge=0, le=1)
    anomaly_vehicle_percent: float = Field(default=5, ge=0, le=100)
    anomaly_events_per_hour: float = Field(default=2, ge=0, le=60)
    anomaly_intensity: Literal["low", "medium", "high"] = "medium"
    pattern_mix: PatternMix = Field(default_factory=PatternMix)
    road_mix: RoadMix = Field(default_factory=RoadMix)

    @model_validator(mode="after")
    def feasible(self):
        if self.execution_profile=="small_test" and self.vehicle_count>100:
            raise ValueError("소량 시험은 최대100대입니다. 운영 프로필을 선택하세요.")
        if self.fleet_version == "fleet-plan-4":
            if self.vehicle_variation_percent is not None:
                raise ValueError("새 배정은 차량 간 운행 시간 편차를 사용하지 않습니다. 해당 항목을 제거하세요.")
        else:
            self.vehicle_variation_percent = 20 if self.vehicle_variation_percent is None else self.vehicle_variation_percent
        if self.fleet_version in ("fleet-plan-3", "fleet-plan-4"):
            if self.daily_driving_minutes is not None:
                raise ValueError("새 시간 범위 배정에는 하루 목표 시간을 사용하지 않습니다. 최소·최대 운행 시간을 지정하세요.")
            self.trip_min_minutes = 3 if self.trip_min_minutes is None else self.trip_min_minutes
            self.trip_max_minutes = (120 if self.fleet_version == "fleet-plan-4" else 90) if self.trip_max_minutes is None else self.trip_max_minutes
            if self.trip_min_minutes > self.trip_max_minutes:
                raise ValueError("최소 운행 시간은 최대 운행 시간 이하여야 합니다.")
            return self
        self.daily_driving_minutes = 120 if self.daily_driving_minutes is None else self.daily_driving_minutes
        maximum = self.daily_driving_minutes * (1 + self.vehicle_variation_percent / 100) * (1 + self.daily_variation_percent / 100)
        if maximum > 600:
            raise ValueError("운행 시간 목표와 두 편차의 최대 조합은 하루 600분 이하여야 합니다. 목표나 편차를 줄이세요.")
        return self


class FleetOBDRequest(Strict):
    fleet: FleetRequest
    checkpoint_epoch: int | None = Field(default=None, ge=0)
    force_end_trip: bool = False


def rng(req, *parts):
    value = "|".join(map(str, (req.fleet_version, req.seed, *parts)))
    return random.Random(int.from_bytes(hashlib.sha256(value.encode()).digest(), "big"))


def allocation(req):
    ids = [f"v{i:06d}" for i in range(1, req.vehicle_count + 1)]
    ranked = sorted(ids, key=lambda v: rng(req, "pattern", v).random())
    weights = req.pattern_mix.model_dump()
    exact = {key: req.vehicle_count * weight / 100 for key, weight in weights.items()}
    counts = {key: math.floor(n) for key, n in exact.items()}
    for key in sorted(weights, key=lambda k: (-(exact[k] - counts[k]), k))[:req.vehicle_count - sum(counts.values())]:
        counts[key] += 1
    patterns, index = {}, 0
    for key, count in counts.items():
        for vehicle in ranked[index:index+count]:
            patterns[vehicle] = key
        index += count
    target_count = math.floor(req.vehicle_count * req.anomaly_vehicle_percent / 100 + 0.5)
    eligible = set(sorted(ids, key=lambda v: rng(req, "anomaly", v).random())[:target_count])
    result = {}
    for vehicle in ids:
        fixed = rng(req, "vehicle", vehicle)
        variation = (req.vehicle_variation_percent or 0) / 100
        kind = patterns[vehicle]
        first = fixed.randint(420, 540) if kind == "commute" else fixed.randint(480, 600) if kind == "daytime" else fixed.randint(360, 600)
        result[vehicle] = {
            "pattern": kind,
            "anomaly_eligible": vehicle in eligible,
            "daily_target_minutes": (req.daily_driving_minutes or 0) * fixed.uniform(1-variation, 1+variation),
            "first_departure_minute": first,
            "evening_departure_minute": fixed.randint(1020, 1140),
            "road_profile": fixed.choices(list(req.road_mix.model_dump()), weights=list(req.road_mix.model_dump().values()))[0],
        }
        if req.fleet_version=="fleet-plan-2":
            personal=rng(req,"commute-length",vehicle)
            result[vehicle]["commute_minutes"]=max(5,min(105,result[vehicle]["daily_target_minutes"]/2*personal.lognormvariate(-.18,.55)))
        if req.fleet_version in ("fleet-plan-3", "fleet-plan-4"):
            del result[vehicle]["daily_target_minutes"]
            personal=rng(req,"commute-length",vehicle)
            low, high=req.trip_min_minutes,req.trip_max_minutes
            # 운행 1회의 길이를 범위 안에서 선택한다. 하루 합계 목표는 만들지 않는다.
            result[vehicle]["commute_minutes"]=personal.triangular(low,high,low+(high-low)*.3)
            if req.fleet_version=="fleet-plan-3":
                result[vehicle]["duration_preference"]=personal.uniform(1-variation,1+variation)
    return result


def iter_vehicle_days(req, vehicle, fixed):
    first = datetime.fromtimestamp(instant(req.measurement.start) / 1000, KST).date() - timedelta(days=2)
    last = datetime.fromtimestamp(instant(req.measurement.end) / 1000, KST).date() + timedelta(days=1)
    for n in range((last-first).days+1):
        day = first + timedelta(days=n)
        daily = rng(req, vehicle, day.isoformat(), "day")
        weekend = day.isoweekday() >= 6
        # 미운행과 주말 참여 여부는 별도 난수 키: 한 항목의 수정이 시간 배정을 흔들지 않는다.
        if rng(req, vehicle, day.isoformat(), "off").random() < req.day_off_probability:
            continue
        if weekend and rng(req, vehicle, day.isoformat(), "weekend").random() >= req.weekend_activity_probability:
            continue
        kind = "flexible" if weekend else fixed["pattern"]
        if req.fleet_version in ("fleet-plan-2", "fleet-plan-3", "fleet-plan-4"):
            yield from varied_day(req,day,fixed,daily,kind,KST)
            continue
        count = 2 if kind == "commute" else daily.randint(2, 3) if kind == "daytime" else daily.randint(1, 3)
        delta = req.daily_variation_percent / 100
        budget = max(count, round(fixed["daily_target_minutes"] * daily.uniform(1-delta, 1+delta)))
        weights = [daily.uniform(.8, 1.2) for _ in range(count)]
        durations = [max(1, int(budget * weight / sum(weights))) for weight in weights[:-1]]
        durations.append(budget-sum(durations))
        start = fixed["first_departure_minute"] + daily.randint(-req.departure_jitter_minutes, req.departure_jitter_minutes)
        if weekend:
            start += 60
        # 첫 출발은 04~13시, 최대 운행600분+주차60분으로 자정 안에 배치 가능하다.
        available = 1440-start-budget
        gaps = [30] * (count-1)
        extra = max(0, available-sum(gaps))
        if kind == "commute":
            desired = fixed["evening_departure_minute"] + daily.randint(-req.departure_jitter_minutes, req.departure_jitter_minutes)
            gaps[0] += min(extra, max(0, desired-start-durations[0]-30))
        else:
            for i in range(len(gaps)):
                add = min(extra, daily.randint(0, 120 if kind == "flexible" else 60))
                gaps[i] += add
                extra -= add
        midnight = datetime(day.year, day.month, day.day, tzinfo=KST)
        for i, duration in enumerate(durations):
            departure = midnight + timedelta(minutes=start)
            arrival = departure + timedelta(minutes=duration)
            if not 0 <= departure.timestamp()*1000 < arrival.timestamp()*1000 <= 4102444800000:
                raise ValueError("일정 문맥을 포함한 운행은 1970~2100년 범위여야 합니다.")
            yield {"start": departure.isoformat(), "end": arrival.isoformat(), "road_profile": fixed["road_profile"]}
            start += duration + (gaps[i] if i < len(gaps) else 0)


def selected_schedule(req, vehicles):
    return ScheduleRequest(mode="manual", measurement=req.measurement, seed=req.seed, interval_ms=req.interval_ms, include_em=req.include_em, vehicles=vehicles)


def expand(req):
    # 전체 확장은 재현 비교용으로 유지한다. 제품 조회는 선택 차량/날짜 생성 경로를 사용한다.
    assigned = allocation(req)
    vehicles = [{"vehicle_id":v,"trips":list(iter_vehicle_days(req,v,fixed))} for v,fixed in assigned.items()]
    return selected_schedule(req,vehicles), assigned


def preview(req, offset=0, limit=100, vehicle_id=None):
    assigned = allocation(req)
    if req.execution_profile=="operations":
        from services.control.fleet_stream import preview as stream_preview
        return stream_preview(req,assigned,iter_vehicle_days,offset,limit,vehicle_id)
    schedule = selected_schedule(req,[{"vehicle_id":v,"trips":[]} for v in assigned])
    def provide(_req, vehicle):
        return ((instant(t["start"]),instant(t["end"]),t["road_profile"]) for t in iter_vehicle_days(req,vehicle.vehicle_id,assigned[vehicle.vehicle_id]))
    data = plan(schedule, offset, limit, vehicle_id, trip_provider=provide)
    data["fleet_version"] = req.fleet_version
    data["anomaly_target_count"] = sum(v["anomaly_eligible"] for v in assigned.values())
    for vehicle in data["vehicles"]:
        vehicle.update(assigned[vehicle["vehicle_id"]])
    data["warnings"].extend([
        "전체 조건으로 배정한 개발용 합성 일정이며 한국 실차 통계로 보정하지 않았습니다.",
        "일일 목표는 운행하는 날의 전체 운행 합계입니다. 미운행·관측 경계·정수 분 반올림으로 관측 평균은 달라집니다.",
        "설정 자동화이며 대규모 전송 지원 확대가 아닙니다. 계획100대/10000운행 상한을 유지하며 장기 상태는 제한된 요청 단위로 나누어 준비합니다.",
    ])
    return data


def obd(body, vehicle_id, trip_key, worker, start_ms, end_ms, offset, limit, checkpoint_store=None, session=None):
    assigned = allocation(body.fleet)
    if vehicle_id not in assigned:
        raise ValueError("계획에 없는 차량입니다.")
    fixed = assigned[vehicle_id]
    trips = []
    for trip in iter_vehicle_days(body.fleet,vehicle_id,fixed):
        trips.append(trip)
        if key_for(vehicle_id,instant(trip["start"]),instant(trip["end"]),trip["road_profile"]) == trip_key:
            break
    else:
        raise ValueError("계획에 없는 운행입니다.")
    schedule = selected_schedule(body.fleet,[{"vehicle_id":vehicle_id,"trips":trips}])
    output = generate(ScheduledOBDRequest(schedule=schedule, force_end_trip=body.force_end_trip, checkpoint_epoch=body.checkpoint_epoch), vehicle_id, trip_key, worker, start_ms, end_ms, offset, limit,
                      anomaly={"anomaly_eligible": fixed["anomaly_eligible"], "anomaly_events_per_hour": body.fleet.anomaly_events_per_hour, "anomaly_intensity": body.fleet.anomaly_intensity}, checkpoint_store=checkpoint_store, cache_context=body.fleet.model_dump(), session=session)
    output["vehicle_assignment"] = fixed
    output["fleet_version"] = body.fleet.fleet_version
    return output
