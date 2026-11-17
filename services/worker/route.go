package main

import (
	"math"
	"math/rand"
)

const routeVersion = "combustion-route-3"
const koreaVersion = "combustion-korea-4"

type routePart struct {
	End  float64
	Road roadContext
}

// 도로 연결 구조를 참고한 합성 경로. 시간/확률은 실측 한국 통계가 아닌 개발 가정이다.
func buildRoute(g *Generation, rng *rand.Rand) []routePart {
	duration := float64(g.DurationSeconds)
	urban := roadContext{"도심 도로", 50, .35}
	arterial := roadContext{"간선 도로", 70, .12}
	if g.ModelVersion == koreaVersion || g.ModelVersion == powertrainVersion {
		arterial.Limit = 50
	}
	alley := roadContext{"골목·좁은 도로", 20, .2}
	ramp := roadContext{"진출입로", 50, 0}
	highway := roadContext{"고속 도로", 100, 0}
	var out []routePart
	t := 0.0
	add := func(dt float64, road roadContext) { t = min(duration, t+dt); out = append(out, routePart{t, road}) }
	// 고속 중심 운행도 일반도로 접근·진출입로·일반도로 도착을 거친다.
	useHighway := duration >= 900 && (g.Profile == "highway" || (g.Profile == "mixed" && rng.Float64() < .55))
	if useHighway {
		approach := 30 + rng.Float64()*30
		access := 40 + rng.Float64()*50
		entry := 100 + rng.Float64()*50
		exit := 100 + rng.Float64()*50
		egress := 40 + rng.Float64()*50
		arrival := 30 + rng.Float64()*30
		add(approach, urban)
		add(access, arterial)
		add(entry, ramp)
		add(duration-approach-access-entry-exit-egress-arrival, highway)
		add(exit, ramp)
		add(egress, arterial)
		add(duration-t, urban)
		return out
	}
	current := urban
	lastZoneEnd := -600.0
	for t < duration {
		dt := min(duration-t, 120+rng.Float64()*300)
		// 보호구역은 도심 도로의 일부다. 모든 차량에 나타나도록 강제하지 않는다.
		if current.Name == urban.Name && t-lastZoneEnd >= 600 && dt >= 120 && rng.Float64() < .2 {
			add(40, current)
			zoneTime := 20 + rng.Float64()*40
			add(zoneTime, roadContext{"보호구역 · 도심 도로", 30, .35})
			lastZoneEnd = t
			add(dt-40-zoneTime, current)
		} else {
			add(dt, current)
		}
		if current.Name == urban.Name {
			if rng.Float64() < .3 {
				current = alley
			} else {
				current = arterial
			}
		} else {
			current = urban
		}
	}
	return out
}

func routeAt(route []routePart, t float64) routePart {
	for _, part := range route {
		if t < part.End-1e-8 {
			return part
		}
	}
	return route[len(route)-1]
}

func getRouteScenario(req Request, index int) *scenarioPlan {
	g := req.Generation
	if g.cache == nil {
		g.cache = &scenarioCache{plans: map[int]*scenarioPlan{}}
	}
	if p, ok := g.cache.plans[index]; ok {
		return p
	}
	if g.cache.eligible == nil {
		g.cache.eligible = map[int]bool{}
		count := int(math.Round(float64(req.Config.VehicleCount) * g.AnomalyVehiclePercent / 100))
		permutation := rand.New(rand.NewSource(g.Seed ^ 0x5eed1234)).Perm(req.Config.VehicleCount)
		for _, i := range permutation[:count] {
			g.cache.eligible[i] = true
		}
	}
	rng := rand.New(rand.NewSource(g.Seed + int64(index)*1000003))
	eventRNG := rand.New(rand.NewSource(g.Seed + int64(index)*1000003 + 0xabc123))
	p := &scenarioPlan{Duration: float64(g.DurationSeconds), Eligible: g.cache.eligible[index], Style: []string{"신중", "보통", "적극"}[rng.Intn(3)], Weather: "건조"}
	if rng.Float64() < .15 {
		p.Weather = "젖은 노면"
	}
	korean := g.ModelVersion == koreaVersion || g.ModelVersion == powertrainVersion
	weatherFactor := 1.0
	if korean {
		p.Weather = "건조"
		if g.Weather == "rain" {
			p.Weather = "강우·젖은 노면"
			weatherFactor = .8
		}
	}
	effectiveRoad := func(road roadContext) roadContext { road.Limit *= weatherFactor; return road }
	route := buildRoute(g, rand.New(rand.NewSource(g.Seed+int64(index)*1000003+0x726f7574)))
	routeUntil := p.Duration
	currentRoad := effectiveRoad(route[0].Road)
	flowUntil, flowFactor, grade := 0.0, 1.0, 0.0
	flowReason := "원활한 흐름"
	contextRNG := rand.New(rand.NewSource(g.Seed + int64(index)*1000003 + 0x666c6f77))
	spec := engineSpec{}
	if g.ModelVersion == powertrainVersion {
		spec = vehicleEngineSpec(g, index)
	}
	t, v := 0.0, 0.0
	totals := motionTotals{Heading: rng.Float64() * 2 * math.Pi}
	accel := map[string]float64{"신중": .8, "보통": 1.2, "적극": 1.6}[p.Style]
	decel := 1.5
	preference := .8 + rng.Float64()*.18
	if p.Weather == "젖은 노면" || p.Weather == "강우·젖은 노면" {
		accel *= .8
		decel *= .8
		if !korean {
			preference *= .9
		}
	}
	appendSegment := func(dt, end float64, road roadContext, reason string, grade, curvature float64, event string) {
		requested := dt
		dt = min(dt, p.Duration-t, routeUntil-t)
		if requested > dt && requested > 0 {
			end = v + (end-v)*dt/requested
		}
		if dt <= 1e-8 {
			return
		}
		if g.ModelVersion == powertrainVersion {
			end = limitPoweredEnd(spec, v, end, dt, grade)
			if event == "급가속" && (end-v)/dt < 3 {
				event = ""
			}
		}
		s := motionSegment{Start: t, End: t + dt, V0: v, V1: end, Grade: grade, Curvature: curvature, Road: road, Reason: reason, Event: event, Before: totals}
		p.Segments = append(p.Segments, s)
		totals = advance(s, dt)
		t += dt
		v = end
	}
	warm := min(8+rng.Float64()*10, p.Duration*.12)
	appendSegment(warm, 0, currentRoad, "시동·웜업", 0, 0, "")
	p.Warmup = warm
	for t < p.Duration-1e-7 {
		part := routeAt(route, t)
		routeUntil = part.End
		currentRoad = effectiveRoad(part.Road)
		left := p.Duration - t
		if left <= v/decel+3 {
			routeUntil = p.Duration // 종료 감속은 마지막 수 초의 경계보다 우선한다.
			brake := min(left, v/decel)
			if brake > 1e-7 {
				appendSegment(brake, max(0, v-decel*brake), currentRoad, "운행 종료 감속", 0, 0, "")
			}
			appendSegment(p.Duration-t, 0, currentRoad, "운행 종료 대기", 0, 0, "")
			break
		}
		road := currentRoad
		if t >= flowUntil {
			flowUntil = t + 90 + contextRNG.Float64()*210
			flowFactor, flowReason = 1, "원활한 흐름"
			flow := contextRNG.Float64()
			if g.Profile == "congested" || flow < .15 {
				flowFactor, flowReason = .15+contextRNG.Float64()*.4, "혼잡·앞차 감속"
			} else if flow < .4 {
				flowFactor, flowReason = .65+contextRNG.Float64()*.2, "앞차 속도 유지"
			}
			grade = (contextRNG.Float64()*2 - 1) * .025
		}
		// 같은 교통 상황 안에서 작은 속도 편차만 허용한다.
		target := road.Limit / 3.6 * preference * flowFactor * (.95 + rng.Float64()*.05)
		reason := flowReason
		if flowFactor < .6 {
			road.StopChance = .35
		}
		if road.Name == "진출입로" {
			road.StopChance = 0
		}
		if routeUntil < p.Duration && routeUntil-t <= 60 {
			next := routeAt(route, routeUntil)
			target = min(target, next.Road.Limit*weatherFactor/3.6)
		}
		target *= min(1, 1-grade*1.5)
		target = min(target, decel*max(0, p.Duration-t-3)/3) // 끝에서 급정지하지 않도록 여유 확보
		event := ""
		if p.Eligible && eventRNG.Float64() < g.AnomalyEventsPerHour*35/3600 {
			event = []string{"급가속", "급감속", "급회전"}[eventRNG.Intn(3)]
			if road.Name == "고속 도로" && event == "급회전" {
				event = ""
			}
		}
		if event == "급가속" && target-v < 4 {
			target = min(road.Limit/3.6, max(target, v+6))
		}
		if event == "급감속" {
			target = max(0, v-6-eventRNG.Float64()*4)
		}
		curvature := 0.0
		turn := road.Name != "고속 도로" && rng.Float64() < .18
		uturn := turn && (road.Name == "도심 도로" || road.Name == "골목·좁은 도로") && rng.Float64() < .08
		if korean {
			uturn = false
		} // 실제 경로의 유턴 허용 지점을 알 수 없으므로 기본 생성하지 않는다.
		turnSign := rng.Intn(2)
		radius := 30 + rng.Float64()*70
		if uturn {
			radius = 9 + rng.Float64()*5
		}
		if turn || event == "급회전" {
			turn = true
			lateral := 1.2
			if event == "급회전" {
				lateral = 3.4
			}
			target = min(target, math.Sqrt(lateral*radius))
			if target > 1 {
				curvature = 1 / radius
				if turnSign == 0 {
					curvature = -curvature
				}
			}
		}
		a := accel
		if target < v {
			a = decel
		}
		if event == "급가속" && target > v {
			a = 3.3
		}
		if event == "급감속" && target < v {
			a = 3.8
		}
		transition := math.Abs(target-v) / a
		if transition > 0 {
			// 감속 중에는 이전 구간 속도까지 허용하는 진입 구간으로 표시한다.
			entering := road
			if !korean {
				entering.Limit = max(road.Limit, v*3.6)
			}
			realEvent := ""
			if event == "급가속" && target > v || event == "급감속" && target < v {
				realEvent = event
			}
			appendSegment(transition, target, entering, "목표 속도 전환 · "+reason, grade, 0, realEvent)
		}
		hold := 8 + rng.Float64()*32
		hold = min(hold, max(0, p.Duration-t-v/decel-2))
		turnReason := reason
		if turn && curvature != 0 {
			angle := math.Pi / 2
			turnReason = "커브·교차로 회전"
			if korean {
				turnReason = "도로 커브"
				if road.Name == "진출입로" {
					turnReason = "진출입로 커브"
				}
			}
			if uturn {
				angle = math.Pi
				turnReason = "저속 유턴"
			}
			hold = min(hold, angle/math.Abs(curvature)/max(v, .1))
		}
		realEvent := ""
		if curvature != 0 && v*v*math.Abs(curvature) >= 3 {
			realEvent = "급회전"
		}
		appendSegment(max(.1, hold), v, road, turnReason, grade, curvature, realEvent)
		if rng.Float64() < road.StopChance && p.Duration-t > v/decel+8 {
			appendSegment(v/decel, 0, road, "정차 전 감속", grade, 0, "")
			stopReason := "신호 대기"
			if flowFactor < .6 {
				stopReason = "정체 정차"
			} else if road.Name == "골목·좁은 도로" && rng.Float64() < .12 {
				stopReason = "잠시 볼일 · 시동 유지"
			}
			appendSegment(min(6+rng.Float64()*25, max(0, p.Duration-t-5)), 0, road, stopReason, grade, 0, "")
		}
	}
	g.cache.plans[index] = p
	return p
}
