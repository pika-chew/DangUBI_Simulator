"""상시 운영 운행 계획. 측정 구간 없이 차량별로 끝없이 이어지는 날짜별 운행을 만든다.

- 같은 명부 seed·차량·날짜면 항상 같은 운행이 나온다(결정적). 복구 때 같은 운행을 다시 만든다.
- 시작 규칙: 등록(운영 편입) 다음 가상 날짜부터 운행한다.
- 재개 규칙: 건너뛴 운행은 따라잡지 않고(R1), 시작 시각이 기준 시각과 직전 종료+30분 이후인
  첫 운행부터(R2), 진행 중이었어야 할 운행의 중간부터 시작하지 않는다(R3).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import hashlib
import random
from types import SimpleNamespace

from pydantic import BaseModel, ConfigDict, Field

from services.control.journey_schedule import varied_day
from services.control.schedule import KST

PLAN_VERSION = "operations-plan-1"
MIN_PARKING_MS = 1800000
IDLE_DAY_LIMIT = 60


class OperationSettings(BaseModel):
    """운영 시작 전에 정하고 세션 동안 고정한다. 다음 시작부터 바꿀 수 있다."""
    model_config = ConfigDict(extra="forbid")
    transport: str = Field(default="dry_run", pattern="^(dry_run|mqtt)$")
    interval_ms: int = Field(default=2000, ge=1000, le=60000)
    client_count: int = Field(default=500, ge=1, le=25000)
    duration_cap_days: int = Field(default=7, ge=1, le=30)
    daily_variation_percent: int = Field(default=25, ge=0, le=50)
    departure_jitter_minutes: int = Field(default=30, ge=0, le=120)
    day_off_probability: float = Field(default=0.2, ge=0, le=1)
    weekend_activity_probability: float = Field(default=0.5, ge=0, le=1)
    anomaly_events_per_hour: float = Field(default=2, ge=0, le=60)
    anomaly_intensity: str = Field(default="medium", pattern="^(low|medium|high)$")
    generation_workers: int = Field(default=1, ge=1, le=4)


def vehicle_key(vehicle_id: int) -> str:
    """생성 키·Trip 번호에 쓰는 내부 식별자. 원본 VIN·가명 VIN을 쓰지 않는다."""
    return f"op{vehicle_id:06d}"


def day_of(ms: int) -> date:
    return datetime.fromtimestamp(ms / 1000, KST).date()


def rng(seed, *parts):
    value = "|".join(map(str, (PLAN_VERSION, seed, *parts)))
    return random.Random(int.from_bytes(hashlib.sha256(value.encode()).digest(), "big"))


def trip_key(vehicle: str, a: int, b: int, profile: str) -> str:
    return hashlib.sha256(f"{PLAN_VERSION}|{vehicle}|{a}|{b}|{profile}".encode()).hexdigest()


def trip_row(vehicle: str, a: int, b: int, profile: str, interval_ms: int, resume_after_ms: int | None = None) -> dict:
    """BoundedSchedule가 읽는 운행 행. resume_after_ms가 있으면 그 시각 이후 메시지부터 보낸다(복구)."""
    key = trip_key(vehicle, a, b, profile)
    first = a if resume_after_ms is None else min(b, max(a, resume_after_ms + 1))
    return {"vehicle_id": vehicle, "trip_key": key, "tid": int(key[:12], 16), "start_ms": a, "end_ms": b,
            "road_profile": profile, "observed_start_ms": first, "observed_end_ms": b,
            "started_before_window": first > a, "continues_after_window": False, "interval_ms": interval_ms}


def day_trips(settings: OperationSettings, seed, vehicle: str, fixed: dict, day: date):
    """하루치 운행 (시작ms, 종료ms, 도로). 미운행·주말 참여는 기존 배정과 같은 방식이다."""
    if rng(seed, vehicle, day.isoformat(), "off").random() < settings.day_off_probability:
        return []
    weekend = day.isoweekday() >= 6
    if weekend and rng(seed, vehicle, day.isoformat(), "weekend").random() >= settings.weekend_activity_probability:
        return []
    req = SimpleNamespace(fleet_version="fleet-plan-4", trip_min_minutes=fixed["trip_min_minutes"],
                          trip_max_minutes=fixed["trip_max_minutes"], daily_variation_percent=settings.daily_variation_percent,
                          departure_jitter_minutes=settings.departure_jitter_minutes)
    kind = "flexible" if weekend else fixed["pattern"]
    out = []
    for trip in varied_day(req, day, fixed, rng(seed, vehicle, day.isoformat(), "day"), kind, KST):
        a = int(datetime.fromisoformat(trip["start"]).timestamp() * 1000)
        b = int(datetime.fromisoformat(trip["end"]).timestamp() * 1000)
        out.append((a, b, trip["road_profile"]))
    return sorted(out)


def upcoming(settings: OperationSettings, seed, vehicle_id: int, fixed: dict, start_day: date,
             not_before_ms: int, last_end_ms: int | None):
    """기준 시각 이후의 운행을 끝없이 낸다. 장기간 운행이 없으면(설정상 미운행 100% 등) 멈춘다."""
    vehicle = vehicle_key(vehicle_id)
    floor = not_before_ms if last_end_ms is None else max(not_before_ms, last_end_ms + MIN_PARKING_MS)
    day = max(start_day, day_of(floor))
    idle = 0
    previous_end = last_end_ms
    while idle < IDLE_DAY_LIMIT:
        found = False
        for a, b, profile in day_trips(settings, seed, vehicle, fixed, day):
            if a < floor:
                continue
            if previous_end is not None and a - previous_end < MIN_PARKING_MS:
                continue
            found = True
            previous_end = b
            yield trip_row(vehicle, a, b, profile, settings.interval_ms)
        idle = 0 if found else idle + 1
        day += timedelta(days=1)
