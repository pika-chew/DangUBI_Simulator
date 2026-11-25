"""운행 집중 시간대: 정한 시간대에 동시에 운행하는 차량이 자연스럽게 늘도록 하루 운행을 조정한다.

- 차량·날짜마다 독립적으로 결정한다. 그날 운행이 있지만 시간대에 운행하지 않는 차량을 확률 p로 골라,
  시간대에 가장 가까운 운행 하나의 출발 시각만 옮긴다(운행 길이 유지). 옮긴 운행은 시간대 안의 한 시점을
  지나며 그 시점이 운행의 15~85% 위치가 되도록 출발을 정하므로 출발·도착이 흩어진다.
- 하루 범위(04:00~24:00), 다른 운행과의 최소 주차 30분, 다른 집중 시간대에 걸친 운행은 건드리지 않는다.
  운행이 없는 날(미운행·주말 불참)은 바꾸지 않는다. 기본 일정이 이미 목표 이상이면 줄이지 않는다.
- p는 날짜별로 운영 차량 전체에서 목표(구간 평균 동시 운행 비율)에 가장 가깝게 맞춘다(calibrate).
- 같은 입력이면 항상 같은 결과다(결정적). 차량 난수는 차량·날짜·시간대별 키를 쓴다.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from services.control.schedule import KST

FOCUS_VERSION = "traffic-focus-1"
TOLERANCE_PERCENT = 5
BIN_MS = 600000
PARKING_MS = 1800000
DAY_START_MINUTE = 240
CANDIDATE_TRIPS = 3
PLACEMENT_TRIES = 4
WEEKDAY_LABELS = {1: "월", 2: "화", 3: "수", 4: "목", 5: "금", 6: "토", 7: "일"}


def minute_of(text: str) -> int:
    hours, minutes = text.split(":")
    return int(hours) * 60 + int(minutes)


class FocusWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    end: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$|^24:00$")
    target_percent: int = Field(ge=1, le=100)
    shape: Literal["peak", "flat"] = "peak"
    weekdays: list[int] = Field(default_factory=lambda: [1, 2, 3, 4, 5], min_length=1, max_length=7)

    @field_validator("weekdays")
    @classmethod
    def valid_days(cls, value):
        if any(d < 1 or d > 7 for d in value) or len(set(value)) != len(value):
            raise ValueError("요일은 1(월)~7(일)을 중복 없이 고르세요.")
        return sorted(value)

    @model_validator(mode="after")
    def span(self):
        a, b = minute_of(self.start), minute_of(self.end)
        if a < DAY_START_MINUTE:
            raise ValueError("집중 시간대는 04:00 이후에 시작해야 합니다(하루 운행 배정 범위).")
        if not 10 <= b - a <= 240:
            raise ValueError("집중 시간대는 시작 < 종료이며 10분~4시간입니다.")
        return self


class TrafficFocus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    windows: list[FocusWindow] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def separate(self):
        spans = sorted((minute_of(w.start), minute_of(w.end)) for w in self.windows)
        if any(b[0] < a[1] for a, b in zip(spans, spans[1:])):
            raise ValueError("집중 시간대끼리 겹칠 수 없습니다.")
        return self


Trip = tuple  # (start_ms, end_ms, road_profile)
RngFor = Callable[[str, int], "random.Random"]  # (용도, 시간대 번호) → 결정적 난수


def midnight_ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=KST).timestamp() * 1000)


def active_windows(focus: TrafficFocus | None, day: date):
    """그날 적용되는 시간대 (번호, 설정, 시작ms, 종료ms)."""
    if focus is None:
        return []
    base = midnight_ms(day)
    return [(i, w, base + minute_of(w.start) * 60000, base + minute_of(w.end) * 60000)
            for i, w in enumerate(focus.windows) if day.isoweekday() in w.weekdays]


def coverage(trips, w0: int, w1: int) -> float:
    """구간 평균 동시 운행에 대한 한 차량의 기여(0~1): 구간 중 운행한 시간 비율."""
    return min(1.0, sum(max(0, min(b, w1) - max(a, w0)) for a, b, _ in trips) / (w1 - w0))


def _overlaps(a, b, w0, w1):
    return a < w1 and b > w0


def apply_window(trips, window, w0, w1, p, rng_for: RngFor, index, other_windows, locked):
    """한 시간대에 대해 차량 하루 운행을 조정한다. 바꾸지 않으면 같은 목록을 돌려준다."""
    if not trips or any(_overlaps(a, b, w0, w1) for a, b, _ in trips):
        return trips
    if rng_for("pick", index).random() >= p:
        return trips
    place = rng_for("place", index)
    base = midnight_ms(datetime.fromtimestamp(w0 / 1000, KST).date())
    lower, upper = base + DAY_START_MINUTE * 60000, base + 1440 * 60000
    center = (w0 + w1) / 2
    order = sorted(range(len(trips)), key=lambda i: (abs((trips[i][0] + trips[i][1]) / 2 - center), i))
    for i in order[:CANDIDATE_TRIPS]:
        a, b, profile = trips[i]
        if (a, b) in locked or any(_overlaps(a, b, x0, x1) for x0, x1 in other_windows):
            continue
        duration = b - a
        others = [t for j, t in enumerate(trips) if j != i]
        for _ in range(PLACEMENT_TRIES):
            point = place.triangular(w0, w1, center) if window.shape == "peak" else place.uniform(w0, w1)
            start = int(round((point - place.uniform(.15, .85) * duration) / 60000)) * 60000
            end = start + duration
            if start < lower or end > upper:
                continue
            if any(not (end + PARKING_MS <= oa or start >= ob + PARKING_MS) for oa, ob, _ in others):
                continue
            locked.add((start, end))
            return sorted(others + [(start, end, profile)])
    return trips


def adjust(trips, focus: TrafficFocus | None, day: date, ps: dict, rng_for: RngFor, upto: int | None = None):
    """하루 운행에 시간대 조정을 순서대로 적용한다(upto: 그 번호 전 시간대까지만)."""
    windows = active_windows(focus, day)
    if not windows or not trips:
        return list(trips)
    spans = [(w0, w1) for _, _, w0, w1 in windows]
    locked: set = set()
    current = sorted(trips)
    for index, window, w0, w1 in windows:
        if upto is not None and index >= upto:
            break
        others = [s for s in spans if s != (w0, w1)]
        current = apply_window(current, window, w0, w1, ps.get(index, 0.0), rng_for, index, others, locked)
    return current


def calibrate(vehicles: list, focus: TrafficFocus, day: date) -> dict:
    """vehicles: [(기본 하루 운행, rng_for)] — 운영 차량 전체(미운행 차량 포함).
    시간대별 p와 달성 보고, 10분 단위 기본/조정 동시 운행 비율을 낸다."""
    count = max(1, len(vehicles))
    windows = active_windows(focus, day)
    ps: dict[int, float] = {}
    reports = []
    for index, window, w0, w1 in windows:
        others = [(x0, x1) for i, _, x0, x1 in windows if i != index]
        gains, base_sum, active, infeasible = [], 0.0, 0, 0
        for trips, rng_for in vehicles:
            before = adjust(trips, focus, day, ps, rng_for, upto=index)
            c0 = coverage(before, w0, w1)
            base_sum += c0
            if before:
                active += 1
            if before and c0 == 0:
                after = apply_window(before, window, w0, w1, 1.0, rng_for, index, others, set())
                gain = coverage(after, w0, w1)
                if gain > 0:
                    gains.append((rng_for("pick", index).random(), gain))
                else:
                    infeasible += 1
        target = window.target_percent / 100
        baseline = base_sum / count
        maximum = (base_sum + sum(g for _, g in gains)) / count
        need = (target - baseline) * count
        p, added = 0.0, 0.0
        if need > 0:
            # 목표에 가장 가까운 지점에서 멈춘다(넘기는 쪽이 더 멀면 그 차량은 옮기지 않음).
            gains.sort()
            p = 1.0
            for u, gain in gains:
                if added + gain >= need:
                    p = min(1.0, u + 1e-12) if added + gain - need <= need - added else u
                    break
                added += gain
        achieved = (base_sum + sum(g for u, g in gains if u < p)) / count
        ps[index] = p
        reasons = []
        if baseline >= target:
            reasons.append("기본 일정이 이미 목표 이상입니다(줄이지 않음).")
        if maximum < target:
            reasons.append(f"그날 운행하는 차량이 {active / count:.0%}뿐이라 더 올릴 수 없습니다." if active / count < target
                           else "운행 길이를 바꾸지 않아 짧은 운행은 구간 일부만 채우고, 주차 30분·하루 범위 제약도 있어 더 올릴 수 없습니다.")
            if infeasible:
                reasons.append(f"제약 때문에 옮기지 못한 차량 {infeasible:,}대")
        if abs(achieved - target) * 100 > TOLERANCE_PERCENT and maximum >= target > baseline:
            reasons.append(f"차량이 {count:,}대라 한 대가 약 {100 / count:.1f}%p를 차지해 목표에 더 가깝게 맞출 수 없습니다.")
        reports.append({"index": index, "start": window.start, "end": window.end, "shape": window.shape,
                        "target_percent": window.target_percent, "baseline_percent": round(baseline * 100, 1),
                        "planned_percent": round(achieved * 100, 1), "maximum_percent": round(maximum * 100, 1),
                        "active_vehicle_percent": round(active / count * 100, 1), "probability": p,
                        "within_tolerance": abs(achieved - target) * 100 <= TOLERANCE_PERCENT, "reasons": reasons})
    return {"version": FOCUS_VERSION, "date": day.isoformat(), "vehicles": len(vehicles), "ps": {str(k): v for k, v in ps.items()},
            "windows": reports, "bins": bins(vehicles, focus, day, ps)}


def bins(vehicles, focus, day, ps) -> dict:
    """하루 10분 단위(144칸) 평균 동시 운행 비율(%) — 기본과 조정."""
    base = midnight_ms(day)
    n = 1440 // 10
    totals = {"baseline": [0.0] * n, "adjusted": [0.0] * n}
    for trips, rng_for in vehicles:
        for key, plan in (("baseline", trips), ("adjusted", adjust(trips, focus, day, ps, rng_for))):
            for a, b, _ in plan:
                first, last = max(0, (a - base) // BIN_MS), min(n - 1, (b - 1 - base) // BIN_MS)
                for k in range(int(first), int(last) + 1):
                    lo, hi = base + k * BIN_MS, base + (k + 1) * BIN_MS
                    totals[key][k] += max(0, min(b, hi) - max(a, lo)) / BIN_MS
    count = max(1, len(vehicles))
    return {key: [round(v * 100 / count, 2) for v in values] for key, values in totals.items()}


def window_at(report: dict, at_ms: int, day: date):
    """보고서에서 at_ms가 속한 시간대와 그 칸의 계획 비율."""
    base = midnight_ms(day)
    for item in report["windows"]:
        w0, w1 = base + minute_of(item["start"]) * 60000, base + minute_of(item["end"]) * 60000
        if w0 <= at_ms < w1:
            return item, report["bins"]["adjusted"][min(143, (at_ms - base) // BIN_MS)]
    return None, None


def days_between(first: date, last: date):
    day = first
    while day <= last:
        yield day
        day += timedelta(days=1)
