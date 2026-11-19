package main

import (
	"math"
	"math/rand"
)

const trafficVersion = "combustion-traffic-7"

const eventVersion = "combustion-events-6"

func isPowertrain(version string) bool {
	return version == powertrainVersion || version == eventVersion || version == trafficVersion || version == journeyVersion
}

// 절대 운행 시간의 포아송 후보 시계. 대기/변속/회복 중 놓친 후보는 적립하지 않는다.
// 빈도는 사건 횟수 보장이 아닌 시간당 후보의 기댓값이다.
type eventClock struct {
	rng        *rand.Rand
	rate, next float64
}

func newEventClock(seed int64, rate float64) *eventClock {
	c := &eventClock{rng: rand.New(rand.NewSource(seed)), rate: rate, next: math.Inf(1)}
	c.step()
	return c
}
func (c *eventClock) step() {
	if c.rate > 0 {
		if math.IsInf(c.next, 1) {
			c.next = 0
		}
		c.next += c.rng.ExpFloat64() * 3600 / c.rate
	}
}
func (c *eventClock) due(t float64) bool {
	ready := false
	for c.next <= t+1e-7 {
		ready = math.Abs(c.next-t) < 1e-6
		c.step()
	}
	return ready
}

// 강도·판정 문턱·회복시간은 실차 통계가 아닌 개발 가정이다.
func eventStrength(level string, wet bool, rng *rand.Rand) (accel, brake, lateral float64) {
	factor := map[string]float64{"low": 0, "medium": .5, "high": 1}[level]
	if level == "" {
		factor = .5
	}
	accel, brake, lateral = 3.05+.55*factor, 3.2+1.2*factor, 3.05+.75*factor
	jitter := .98 + .04*rng.Float64()
	accel *= jitter
	brake *= jitter
	lateral *= jitter
	if wet {
		accel *= .9
		brake *= .9
		lateral *= .9
	}
	return
}

func chooseEvent(rng *rand.Rand, road roadContext, v, flow float64, turn bool, accel, brake, lateral float64) string {
	kinds := []string{}
	openRoad := road.Name == "도심 도로" || road.Name == "간선 도로" || road.Name == "고속 도로"
	if openRoad && !turn && flow >= .65 && v >= .5 && road.Limit/3.6-v >= 4 && accel >= 3 {
		kinds = append(kinds, "급가속")
	}
	if v >= 8 && (flow < 1 || road.StopChance > 0) && brake >= 3 {
		kinds = append(kinds, "급감속")
	}
	if turn && (road.Name == "도심 도로" || road.Name == "간선 도로") && v >= 5 && lateral >= 3 {
		kinds = append(kinds, "급회전")
	}
	if len(kinds) == 0 {
		return ""
	}
	return kinds[rng.Intn(len(kinds))]
}

// 일정 운행의 추가 기동 후보. 기존 후보 시계/대상 차량/회복 간격 안에서만 선택한다.
func chooseJourneyEvent(rng *rand.Rand, road roadContext, v, flow float64, turn bool, accel, brake, lateral float64) string {
	kinds := []string{}
	open := road.Name == "도심 도로" || road.Name == "간선 도로" || road.Name == "외곽 간선 · 제한표지" || road.Name == "지정 간선 · 제한표지" || road.Name == "고속 도로"
	if open && !turn && flow >= .65 && road.Limit/3.6-v >= 4 && accel >= 3 {
		kinds = append(kinds, "급가속") // 저속 출발 여부는 실제 생성 후 판정한다.
	}
	if v >= 8 && (flow < 1 || road.StopChance > 0) && brake >= 3 {
		kinds = append(kinds, "급감속")
		if road.Name == "도심 도로" && !turn {
			kinds = append(kinds, "급정지")
		}
	}
	if turn && road.Name == "도심 도로" && v >= 5 && lateral >= 3 {
		kinds = append(kinds, "급회전")
		// 실제 지도상의 유턴 허용 지점이 아닌 합성 도심 교차로 상황이다.
		if flow >= .65 && v <= 30/3.6 {
			kinds = append(kinds, "급유턴")
		}
	}
	if len(kinds) == 0 {
		return ""
	}
	return kinds[rng.Intn(len(kinds))]
}
