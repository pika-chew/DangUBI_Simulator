package main

import (
	"math"
	"sort"
	"strings"
)

// 문턱값은 기존 모형의 강한 가감속/횡가속도 3m/s²를 잇는 개발 기준이다.
// 일반 출발/정차/유턴에 새 이벤트를 붙이지 않고, 이상 이벤트 후보만 세분한다.
func maneuverEvent(s motionSegment) string {
	dt := s.End - s.Start
	if dt < .5 {
		return ""
	}
	a := (s.V1 - s.V0) / dt
	switch s.Event {
	case "급가속", "급출발":
		if a >= 3-1e-8 && s.V0 <= 5/3.6 && s.V1 >= 20/3.6 {
			return "급출발"
		}
		if s.Event == "급출발" {
			return ""
		}
	case "급감속", "급정지":
		if a <= -3+1e-8 && s.V0 >= 20/3.6 && s.V1 <= 1/3.6 {
			return "급정지"
		}
		if s.Event == "급정지" {
			return ""
		}
	case "급회전", "급유턴":
		if s.Event == "급유턴" || strings.Contains(s.Reason, "유턴") {
			angle := math.Abs(s.Curvature) * (s.V0 + s.V1) / 2 * dt
			lateral := math.Min(s.V0*s.V0, s.V1*s.V1) * math.Abs(s.Curvature)
			if s.Road.Name == "도심 도로" && angle >= 150*math.Pi/180-1e-8 && angle <= 210*math.Pi/180+1e-8 && lateral >= 3-1e-8 {
				return "급유턴"
			}
			return ""
		}
	}
	return s.Event
}

func completedManeuver(event string) bool {
	return event == "급출발" || event == "급유턴" || event == "급정지"
}

// 시작 빈 구간은0. 새 기동은 완료를 포함한 보고에, 기존 지속 이벤트는 겹치는 구간에 표시한다.
// em 출력 선택 자체는 판정/주행 난수열에 관여하지 않는다.
func intervalEventMask(segments []motionSegment, from, until float64) int {
	if until <= from {
		return 0
	}
	mask := 0
	first := sort.Search(len(segments), func(i int) bool { return segments[i].End > from })
	for i := first; i < len(segments) && segments[i].Start <= until; i++ {
		s := segments[i]
		if a, b, ok := overspeedSpan(s); ok && math.Max(a, from) < math.Min(b, until) {
			mask |= 1 << 7 // 과속: bit7(128). 예약 비트 없이 em은 bit0~7(0~255)
		}
		event := maneuverEvent(s)
		// 출발 속도/정지 도달/유턴 각도는 실제 기동 완료 시점에 확정한다.
		if completedManeuver(event) && s.End > until {
			continue
		}
		switch event {
		case "급출발":
			mask |= 1 << 0
		case "급유턴":
			mask |= 1 << 3
		case "급정지":
			mask |= 1 << 6
		case "급가속":
			mask |= 1 << 5
		case "급감속":
			mask |= 1 << 4
		case "급회전":
			// 모형의 양의 곡률은 동쪽 기준 반시계 방향, 즉 좌회전이다.
			if s.Curvature > 0 {
				mask |= 1 << 1
			} else if s.Curvature < 0 {
				mask |= 1 << 2
			}
		}
	}
	return mask
}

// 과속은 기존 기동 라벨/대상 비율과 별개로 실제 속도를 모형의 적용 제한속도와 비교한다.
// 경계의 부동소수점 오차를 제외하기 위해 0.01km/h를 초과해야 하며 별도 지속시간 문턱은 없다.
func overspeedSpan(s motionSegment) (float64, float64, bool) {
	if s.End <= s.Start || s.Road.Limit <= 0 {
		return 0, 0, false
	}
	limit := (s.Road.Limit + .01) / 3.6
	if math.Max(s.V0, s.V1) <= limit {
		return 0, 0, false
	}
	a, b := s.Start, s.End
	if s.V1 != s.V0 {
		cross := s.Start + (limit-s.V0)/(s.V1-s.V0)*(s.End-s.Start)
		if s.V0 <= limit {
			a = math.Max(a, cross)
		}
		if s.V1 <= limit {
			b = math.Min(b, cross)
		}
	}
	return a, b, a < b
}
