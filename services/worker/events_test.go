package main

import (
	"math"
	"math/rand"
	"reflect"
	"testing"
)

func TestEventClockTimeAndSkippedOpportunities(t *testing.T) {
	a, b := newEventClock(42, 60), newEventClock(42, 60)
	count := 0
	for a.next < 3600 {
		tm := a.next
		if !a.due(tm) {
			t.Fatal("시각 누락")
		}
		count++
	}
	if count < 30 || count > 100 {
		t.Fatal("시간당 후보 수", count)
	}
	first := b.next
	if b.due(first + 1) {
		t.Fatal("놓친 기회를 뒤늦게 실행")
	}
	if b.next <= first+1 {
		t.Fatal("후보 소진 실패")
	}
	if newEventClock(1, 0).due(3600) {
		t.Fatal("빈도0")
	}
	// 여러 차량/seed 평균에서1시간 후보 기대값을 확인한다. 실제 이벤트 목표가 아니다.
	n := 0
	for seed := int64(0); seed < 1000; seed++ {
		c := newEventClock(seed, 12)
		for c.next < 3600 {
			n++
			c.step()
		}
	}
	if math.Abs(float64(n)/1000-12) > .5 {
		t.Fatal("후보 기대값", n)
	}
}

func TestEventConditionsAndIntensity(t *testing.T) {
	rng := rand.New(rand.NewSource(1))
	for i := 0; i < 100; i++ {
		if chooseEvent(rng, roadContext{"고속 도로", 100, 0}, 20, 1, true, 3.3, 3.8, 3.4) != "" {
			t.Fatal("본선에 회전/감속 생성")
		}
		if chooseEvent(rng, roadContext{"도심 도로", 50, .3}, 0, 1, true, 3.3, 3.8, 3.4) != "" {
			t.Fatal("정차 중 이벤트")
		}
		if chooseEvent(rng, roadContext{"보호구역 · 도심 도로", 30, .3}, 3, 1, true, 3.3, 3.8, 3.4) != "" {
			t.Fatal("보호 저속구간 이벤트")
		}
	}
	loA, loB, loL := eventStrength("low", false, rand.New(rand.NewSource(2)))
	hiA, hiB, hiL := eventStrength("high", false, rand.New(rand.NewSource(2)))
	wetA, wetB, wetL := eventStrength("high", true, rand.New(rand.NewSource(2)))
	if hiA <= loA || hiB <= loB || hiL <= loL || wetA >= hiA || wetB >= hiB || wetL >= hiL {
		t.Fatal("강도/강우")
	}
}

func eventRequest(seed int64, weather string) Request {
	r := powertrainRequest(3600, "warm")
	r.Generation.ModelVersion = eventVersion
	r.Generation.AnomalyIntensity = "high"
	r.Generation.AnomalyVehiclePercent = 100
	r.Generation.AnomalyEventsPerHour = 60
	r.Generation.Seed = seed
	r.Generation.Weather = weather
	r, _ = normalize(r)
	return r
}

func TestTimedEventsPhysicalRulesAndReplay(t *testing.T) {
	counts := map[string]int{}
	for _, weather := range []string{"dry", "rain"} {
		for seed := int64(0); seed < 40; seed++ {
			r := eventRequest(seed, weather)
			r.Generation.Profile = []string{"urban", "highway", "congested", "mixed"}[seed%4]
			r, _ = normalize(r)
			for i := 0; i < 3; i++ {
				p := getScenario(r, i)
				lastEnd := -30.0
				for _, s := range p.Segments {
					if math.Max(s.V0, s.V1)*3.6 > s.Road.Limit+1e-5 {
						t.Fatal("속도 상한", seed, s)
					}
					if s.Event == "" {
						continue
					}
					counts[s.Event]++
					if s.Start < lastEnd+30-1e-6 || s.End-s.Start < .5-1e-6 {
						t.Fatal("회복/사건 길이", s, lastEnd)
					}
					lastEnd = s.End
					a := (s.V1 - s.V0) / (s.End - s.Start)
					switch s.Event {
					case "급가속":
						if a < 3-1e-6 || s.Curvature != 0 {
							t.Fatal("급가속", s)
						}
					case "급감속":
						if a > -3+1e-6 || s.V0 < 8 {
							t.Fatal("급감속", s)
						}
					case "급회전":
						if s.Road.Name != "도심 도로" && s.Road.Name != "간선 도로" || s.V0*s.V0*math.Abs(s.Curvature) < 3 {
							t.Fatal("급회전", s)
						}
					}
				}
			}
		}
	}
	for _, kind := range []string{"급가속", "급감속", "급회전"} {
		if counts[kind] == 0 {
			t.Fatal("이벤트 미발생", kind)
		}
	}
	r := eventRequest(42, "dry")
	a, _ := process(r)
	r.Config.IntervalMS = 4000
	r, _ = normalize(r)
	b, _ := process(r)
	if !reflect.DeepEqual(a.Scenario, b.Scenario) {
		t.Fatal("보고주기가 사건/Trip 변경")
	}
	for i := range a.TripSummaries {
		if !reflect.DeepEqual(a.TripSummaries[i].Message, b.TripSummaries[i].Message) {
			t.Fatal("보고주기가 Trip 변경")
		}
	}
	r.Generation.AnomalyVehiclePercent = 0
	r, _ = normalize(r)
	if out, _ := process(r); out.Scenario.Events != 0 {
		t.Fatal("비대상 사건")
	}
	r.Generation.AnomalyVehiclePercent = 100
	r.Generation.AnomalyEventsPerHour = 0
	r, _ = normalize(r)
	if out, _ := process(r); out.Scenario.Events != 0 {
		t.Fatal("빈도0 사건")
	}
}
