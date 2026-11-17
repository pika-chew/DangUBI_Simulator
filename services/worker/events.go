package main

import (
	"math"
	"math/rand"
)

const eventVersion = "combustion-events-6"

func isPowertrain(version string) bool {
	return version == powertrainVersion || version == eventVersion
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
