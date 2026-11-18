package main

import (
	"math/rand"
	"time"
)

const journeyVersion = "combustion-journey-8"

// 가상 교통 에피소드. 통계 보정값이 아니라 시간대/도로와 회복을 연결하는 개발 모형이다.
func journeyFlow(g *Generation, road roadContext, t float64, rng *rand.Rand) (float64, string, float64) {
	hour := time.UnixMilli(g.StartTimeMS + int64(t*1000)).UTC().Add(9 * time.Hour).Hour()
	peak := hour >= 7 && hour < 9 || hour >= 17 && hour < 19
	risk := .08
	if peak {
		risk = .22
	}
	if road.Name == "고속 도로" {
		risk = .03
		if peak {
			risk = .12
		}
	}
	if g.Profile == "congested" {
		risk = .5
		if peak {
			risk = .65
		}
	}
	draw := rng.Float64()
	if draw < risk {
		return .18 + rng.Float64()*.32, "혼잡 구간 · 대기와 서행", 45 + rng.Float64()*135
	}
	if draw < risk+.22 {
		return .6 + rng.Float64()*.25, "앞차 흐름 · 부분 회복", 60 + rng.Float64()*150
	}
	return .92 + rng.Float64()*.08, "원활 구간 · 교통 회복", 120 + rng.Float64()*300
}
