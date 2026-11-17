package main

import (
	"math"
	"math/rand"
	"reflect"
	"strings"
	"testing"
)

func routeRequest(duration int) Request {
	r := scenarioRequest(duration)
	r.Generation.ModelVersion = routeVersion
	r, _ = normalize(r)
	return r
}

func TestRouteTopologyAndZonePlacement(t *testing.T) {
	allowed := map[string]map[string]bool{
		"도심 도로":        {"간선 도로": true, "골목·좁은 도로": true, "보호구역 · 도심 도로": true},
		"간선 도로":        {"도심 도로": true, "진출입로": true},
		"골목·좁은 도로":     {"도심 도로": true},
		"보호구역 · 도심 도로": {"도심 도로": true},
		"진출입로":         {"간선 도로": true, "고속 도로": true},
		"고속 도로":        {"진출입로": true},
	}
	for _, profile := range []string{"urban", "highway", "mixed", "congested"} {
		for seed := int64(0); seed < 100; seed++ {
			r := routeRequest(3600)
			r.Generation.Profile = profile
			route := buildRoute(r.Generation, rand.New(rand.NewSource(seed)))
			prev := ""
			last := 0.0
			zoneEnd := -600.0
			for _, part := range route {
				if part.End <= last || part.End > 3600 {
					t.Fatal("경로 시각", part)
				}
				if prev != "" && prev != part.Road.Name && !allowed[prev][part.Road.Name] {
					t.Fatal("연결 불가", prev, part)
				}
				if strings.HasPrefix(part.Road.Name, "보호구역") {
					if last-zoneEnd < 600 || part.End-last > 60.001 {
						t.Fatal("보호구역 간격/길이", part)
					}
					zoneEnd = part.End
				}
				if profile == "highway" && part.Road.Name == "고속 도로" && part.End-last < 2999.99 {
					t.Fatal("고속 유지 부족")
				}
				prev, last = part.Road.Name, part.End
			}
			if last != 3600 {
				t.Fatal("경로 미완료")
			}
		}
	}
}

func TestRouteMotionContinuityAndContext(t *testing.T) {
	for _, duration := range []int{10, 60, 600, 900, 3600} {
		for seed := int64(0); seed < 20; seed++ {
			r := routeRequest(3600)
			r.Generation.DurationSeconds = duration
			r.Config.IntervalMS = 100
			r.Generation.Seed = seed
			// 계획 검사는 메시지 적재 없이 수행한다.
			p := getScenario(r, 0)
			for i, s := range p.Segments {
				if s.End <= s.Start || math.Abs((s.V1-s.V0)/(s.End-s.Start)) > 1.60001 {
					t.Fatal("시간/가속 한도", s)
				}
				if math.Max(s.V0*s.V0, s.V1*s.V1)*math.Abs(s.Curvature) > 1.20001 {
					t.Fatal("회전 한도", s)
				}
				if s.Road.Name == "고속 도로" && (strings.Contains(s.Reason, "신호") || strings.Contains(s.Reason, "볼일") || strings.Contains(s.Reason, "유턴") || s.Curvature != 0) {
					t.Fatal("고속 상황 불일치", s)
				}
				if i > 0 {
					previous := p.Segments[i-1]
					before := advance(previous, previous.End-previous.Start)
					if math.Abs(previous.End-s.Start) > 1e-7 || math.Abs(previous.V1-s.V0) > 1e-7 || math.Abs(before.Distance-s.Before.Distance) > 1e-7 || math.Abs(before.X-s.Before.X) > 1e-7 {
						t.Fatal("연속성 위반", s)
					}
				}
			}
			last := p.Segments[len(p.Segments)-1]
			if math.Abs(last.End-float64(duration)) > 1e-6 || last.V1 > 1e-6 {
				t.Fatal("종료 정차", last)
			}
		}
	}
}

func TestRouteReplayAndTripIntegration(t *testing.T) {
	r := routeRequest(3600)
	a, err := process(r)
	if err != nil {
		t.Fatal(err)
	}
	r.Config.IntervalMS = 4000
	r, _ = normalize(r)
	b, err := process(r)
	if err != nil {
		t.Fatal(err)
	}
	if a.ModelVersion != routeVersion || !reflect.DeepEqual(a.TripSummaries[0].Message, b.TripSummaries[0].Message) {
		t.Fatal("주기 독립 적분")
	}
	r, _ = normalize(r)
	c, _ := process(r)
	if !reflect.DeepEqual(b.Trace, c.Trace) {
		t.Fatal("seed 재현")
	}
	p := getScenario(r, 0)
	last := p.Segments[len(p.Segments)-1]
	total := advance(last, last.End-last.Start)
	trip := b.TripSummaries[0].Message["pld"].(map[string]any)
	if math.Abs(trip["dis"].(float64)-total.Distance) > 1e-7 {
		t.Fatal("Trip 거리")
	}
}

func TestRouteDistributionComparison(t *testing.T) {
	for _, version := range []string{scenarioVersion, routeVersion} {
		seconds, zone, highway, longest, idle := 0.0, 0.0, 0.0, 0.0, 0.0
		events := 0
		for seed := int64(0); seed < 30; seed++ {
			r := routeRequest(3600)
			r.Generation.ModelVersion = version
			r.Generation.Seed = seed
			r.Generation.Profile = "highway"
			p := getScenario(r, 0)
			consecutive := 0.0
			for _, s := range p.Segments {
				dt := s.End - s.Start
				seconds += dt
				if strings.Contains(s.Road.Name, "보호") {
					zone += dt
				}
				if s.Road.Name == "고속 도로" {
					highway += dt
					consecutive += dt
					longest = max(longest, consecutive)
				} else {
					consecutive = 0
				}
				if s.V0 == 0 && s.V1 == 0 {
					idle += dt
				}
				if s.Event != "" {
					events++
				}
			}
		}
		t.Logf("%s 30대×1시간 고속: 고속비중 %.2f%% 보호비중 %.2f%% 공회전 %.2f%% 최장고속 %.1fs 이벤트%d", version, 100*highway/seconds, 100*zone/seconds, 100*idle/seconds, longest, events)
	}
}

func TestRouteAnomalyEligibility(t *testing.T) {
	r := routeRequest(3600)
	r.Config.VehicleCount = 4
	r.Generation.AnomalyVehiclePercent = 50
	r.Generation.AnomalyEventsPerHour = 60
	r, _ = normalize(r)
	out, err := process(r)
	if err != nil {
		t.Fatal(err)
	}
	if out.Scenario.EligibleVehicles != 2 || out.Scenario.ObservedVehicles > 2 || out.Scenario.Events == 0 {
		t.Fatal("이상 대상/관측 집계", out.Scenario)
	}
	for i := 0; i < 4; i++ {
		p := getScenario(r, i)
		for _, s := range p.Segments {
			if !p.Eligible && s.Event != "" {
				t.Fatal("비대상 이상 발생", s)
			}
			if s.Road.Name == "고속 도로" && s.Event == "급회전" {
				t.Fatal("고속 급회전 후보", s)
			}
		}
	}
}
