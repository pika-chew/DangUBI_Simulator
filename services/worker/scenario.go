package main

import (
	"math"
	"math/rand"
	"sort"
)

const scenarioVersion = "combustion-scenario-2"

type scenarioCache struct {
	plans    map[int]*scenarioPlan
	eligible map[int]bool
}
type roadContext struct {
	Name              string
	Limit, StopChance float64
}

var localRoads = []roadContext{{"골목·좁은 도로", 20, .25}, {"보호·주의 구간", 30, .25}, {"도심 도로", 50, .5}, {"간선 도로", 70, .2}}

type motionTotals struct{ Distance, Fuel, Idle, Cut, Peak, X, Y, Heading float64 }
type motionSegment struct {
	Start, End, V0, V1, Grade, Curvature float64
	Road                                 roadContext
	Reason, Event                        string
	Before                               motionTotals
}
type scenarioPlan struct {
	Engine         *enginePlan
	Segments       []motionSegment
	Warmup         float64
	Eligible       bool
	Style, Weather string
	Duration       float64
}
type ScenarioSummary struct {
	EligibleVehicles int `json:"eligible_vehicles"`
	ObservedVehicles int `json:"observed_vehicles"`
	Events           int `json:"events"`
}
type MotionContext struct {
	Road         string  `json:"road"`
	Reason       string  `json:"reason"`
	SpeedLimit   float64 `json:"speed_limit_kmh"`
	Grade        float64 `json:"grade_percent"`
	Acceleration float64 `json:"acceleration_mps2"`
	Lateral      float64 `json:"lateral_mps2"`
	Heading      float64 `json:"heading_deg"`
	Event        string  `json:"event,omitempty"`
	Eligible     bool    `json:"eligible"`
	Style        string  `json:"driver_style"`
	Weather      string  `json:"weather"`
	Gear         int     `json:"gear,omitempty"`
	EngineState  string  `json:"engine_state,omitempty"`
	WheelPower   float64 `json:"wheel_power_kw,omitempty"`
	FuelRate     float64 `json:"fuel_rate_lph,omitempty"`
	Coolant      float64 `json:"coolant_c,omitempty"`
}

func modelVersion(g *Generation) string {
	if g.ModelVersion == "" {
		return generatorVersion
	}
	return g.ModelVersion
}

func getScenario(req Request, index int) *scenarioPlan {
	if req.Generation.ModelVersion == routeVersion || req.Generation.ModelVersion == koreaVersion || isPowertrain(req.Generation.ModelVersion) {
		return getRouteScenario(req, index)
	}
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
	t, v := 0.0, 0.0
	totals := motionTotals{Heading: rng.Float64() * 2 * math.Pi}
	accel := map[string]float64{"신중": .8, "보통": 1.2, "적극": 1.6}[p.Style]
	decel := 1.5
	preference := .8 + rng.Float64()*.18
	if p.Weather == "젖은 노면" {
		accel *= .8
		decel *= .8
		preference *= .9
	}
	appendSegment := func(dt, end float64, road roadContext, reason string, grade, curvature float64, event string) {
		dt = min(dt, p.Duration-t)
		if dt <= 1e-8 {
			return
		}
		s := motionSegment{Start: t, End: t + dt, V0: v, V1: end, Grade: grade, Curvature: curvature, Road: road, Reason: reason, Event: event, Before: totals}
		p.Segments = append(p.Segments, s)
		totals = advance(s, dt)
		t += dt
		v = end
	}
	warm := min(8+rng.Float64()*10, p.Duration*.12)
	appendSegment(warm, 0, localRoads[2], "시동·웜업", 0, 0, "")
	p.Warmup = warm
	for t < p.Duration-1e-7 {
		left := p.Duration - t
		if left <= v/decel+3 {
			brake := min(left, v/decel)
			if brake > 1e-7 {
				appendSegment(brake, max(0, v-decel*brake), localRoads[2], "운행 종료 감속", 0, 0, "")
			}
			appendSegment(p.Duration-t, 0, localRoads[2], "운행 종료 대기", 0, 0, "")
			break
		}
		road := localRoads[rng.Intn(len(localRoads))]
		if (g.Profile == "highway" && rng.Float64() < .8) || (g.Profile == "mixed" && rng.Float64() < .35) {
			road = roadContext{"고속 도로", 100, 0}
		}
		if g.Profile == "congested" {
			road = roadContext{"혼잡 도로", 40, .85}
		}
		grade := 0.0
		if rng.Float64() < .25 {
			grade = (rng.Float64()*2 - 1) * .05
		}
		target := road.Limit / 3.6 * preference
		reason := "원활한 흐름"
		flow := rng.Float64()
		if g.Profile == "congested" || flow < .25 {
			target *= .1 + rng.Float64()*.45
			reason = "혼잡·앞차 감속"
		} else if flow < .5 {
			target *= .65 + rng.Float64()*.2
			reason = "앞차 속도 유지"
		}
		target *= min(1, 1-grade*1.5)
		target = min(target, decel*max(0, p.Duration-t-3)/3) // 끝에서 급정지하지 않도록 여유 확보
		event := ""
		if p.Eligible && eventRNG.Float64() < g.AnomalyEventsPerHour*35/3600 {
			event = []string{"급가속", "급감속", "급회전"}[eventRNG.Intn(3)]
		}
		if event == "급가속" && target-v < 4 {
			target = min(road.Limit/3.6, max(target, v+6))
		}
		if event == "급감속" {
			target = max(0, v-6-eventRNG.Float64()*4)
		}
		curvature := 0.0
		turn := road.Name != "고속 도로" && rng.Float64() < .18
		uturn := turn && rng.Float64() < .15
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
			entering.Limit = max(road.Limit, v*3.6)
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
			if g.Profile == "congested" {
				stopReason = "정체 정차"
			} else if rng.Float64() < .12 {
				stopReason = "잠시 볼일 · 시동 유지"
			}
			appendSegment(min(6+rng.Float64()*25, max(0, p.Duration-t-5)), 0, road, stopReason, grade, 0, "")
		}
	}
	g.cache.plans[index] = p
	return p
}

func advance(s motionSegment, dt float64) motionTotals {
	a := (s.V1 - s.V0) / (s.End - s.Start)
	end := max(0, s.V0+a*dt)
	d := (s.V0 + end) * dt / 2
	out := s.Before
	out.Distance += d
	out.Peak = max(out.Peak, s.V0, end)
	if s.V0 == 0 && s.V1 == 0 {
		out.Idle += dt
	}
	if a < -.05 && s.V0 > 1 {
		out.Cut += dt
	} else {
		out.Fuel += .8*dt/3600 + d*.00007*(1+max(0, s.Grade)*8) + max(0, end*end-s.V0*s.V0)*.000015
	}
	h := s.Before.Heading
	if math.Abs(s.Curvature) > 1e-12 {
		out.X += (math.Sin(h+s.Curvature*d) - math.Sin(h)) / s.Curvature
		out.Y += (math.Cos(h) - math.Cos(h+s.Curvature*d)) / s.Curvature
	} else {
		out.X += d * math.Cos(h)
		out.Y += d * math.Sin(h)
	}
	out.Heading = h + s.Curvature*d
	return out
}

func scenarioAt(req Request, index int, t float64) (motionTotals, float64, MotionContext, int) {
	p := getScenario(req, index)
	i := sort.Search(len(p.Segments), func(i int) bool { return p.Segments[i].End > t })
	if i == len(p.Segments) {
		i--
	}
	s := p.Segments[i]
	dt := max(0, min(t, s.End)-s.Start)
	totals := advance(s, dt)
	v := s.V0 + (s.V1-s.V0)*dt/(s.End-s.Start)
	context := MotionContext{Road: s.Road.Name, Reason: s.Reason, SpeedLimit: s.Road.Limit, Grade: s.Grade * 100, Acceleration: (s.V1 - s.V0) / (s.End - s.Start), Lateral: v * v * math.Abs(s.Curvature), Heading: math.Mod(totals.Heading*180/math.Pi+36000, 360), Eligible: p.Eligible, Style: p.Style, Weather: p.Weather}
	if s.Event != "" && dt > 0 {
		context.Event = s.Event
	}
	events := 0
	for _, seg := range p.Segments {
		if seg.Event != "" && t >= seg.Start+min(1, seg.End-seg.Start) {
			events++
		}
	}
	return totals, v, context, events
}

func scenarioMessage(req Request, index, seq int) (map[string]any, string) {
	g := req.Generation
	t := float64(offsetMS(req, seq)) / 1000
	total, v, c, _ := scenarioAt(req, index, t)
	lat := g.StartLat + total.Y/6371000*180/math.Pi
	lon := math.Mod(g.StartLon+total.X/(6371000*math.Cos(g.StartLat*math.Pi/180))*180/math.Pi+540, 360) - 180
	ts := float64(g.StartTimeMS + offsetMS(req, seq))
	ty := float64(4)
	state := "정속"
	if v < .01 {
		state = "정차·공회전"
	} else if c.Acceleration > .05 {
		state = "가속"
	} else if c.Acceleration < -.05 {
		state = "감속"
	}
	if c.Reason == "시동·웜업" {
		state = "웜업·공회전"
	}
	if t >= float64(g.DurationSeconds) {
		state = "운행 종료·시동 OFF"
		v = 0
	}
	pld := map[string]any{"tid": float64(index + 1)}
	if seq == req.Config.MessagesPerVehicle-1 {
		ty = 3
		state = "Trip 집계"
		warm := min(t, 180)
		coolant := (25*warm + 65*warm*warm/360 + (t-warm)*90) / max(1, t)
		for k, value := range map[string]any{"stt": float64(g.StartTimeMS), "edt": ts, "dis": total.Distance, "tdis": g.InitialOdometerM + total.Distance, "fc": total.Fuel, "stlat": g.StartLat, "stlon": g.StartLon, "edlat": lat, "edlon": lon, "ctp": coolant, "coe": total.Fuel * 2310, "fct": total.Cut, "hsts": total.Peak * 3.6, "mesp": total.Distance / max(1, t) * 3.6, "idt": total.Idle, "btv": 12.6, "gnv": 14.2, "wut": getScenario(req, index).Warmup, "dtvt": t - total.Idle} {
			pld[k] = value
		}
	} else {
		rpm, load, throttle := 800.0, 18.0, 3.0
		if v > .01 {
			rpm = 900 + v*3.6*26
			load = max(5, min(95, 22+v*1.2+c.Acceleration*12+c.Grade*2))
			throttle = max(0, min(95, load*.6+max(0, c.Acceleration)*10))
		}
		if t >= float64(g.DurationSeconds) {
			rpm = 0
			load = 0
			throttle = 0
		}
		for k, value := range map[string]any{"lat": lat, "lon": lon, "clt": ts, "sp": v * 3.6, "rpm": math.Round(rpm), "cdit": total.Distance, "fc": total.Fuel, "el": load, "vv": 12.6, "tpos": throttle} {
			pld[k] = value
		}
	}
	return map[string]any{"ty": ty, "ts": ts, "pld": pld}, state
}
