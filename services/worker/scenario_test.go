package main

import (
	"math"
	"reflect"
	"testing"
)

func scenarioRequest(duration int) Request {
	r := automaticRequest()
	r.Generation.ModelVersion = scenarioVersion
	r.Generation.Profile = "mixed"
	r.Generation.DurationSeconds = duration
	r, _ = normalize(r)
	return r
}

func TestScenarioLongDriveContinuityAndBounds(t *testing.T) {
	r := scenarioRequest(3600)
	out, err := process(r)
	if err != nil {
		t.Fatal(err)
	}
	if out.ModelVersion != scenarioVersion || out.Processed != 5406 || out.TripCount != 3 {
		t.Fatal("새 모델 집계 실패", out, err)
	}
	for i := 0; i < 3; i++ {
		p := getScenario(r, i)
		if len(p.Segments) < 50 {
			t.Fatal("장시간 상태 반복 부족")
		}
		last := motionTotals{}
		for j, s := range p.Segments {
			a := (s.V1 - s.V0) / (s.End - s.Start)
			if math.Abs(a) > 1.600001 || s.V0 < 0 || s.V1 < 0 || s.Event != "" {
				t.Fatal("정상 차량 범위/이벤트 위반", s)
			}
			if a > 0 && s.End-s.Start > 45 {
				t.Fatal("장시간 가속 구간 연장")
			}
			if j > 0 && (math.Abs(last.Distance-s.Before.Distance) > 1e-7 || math.Abs(last.X-s.Before.X) > 1e-7 || math.Abs(last.Y-s.Before.Y) > 1e-7 || math.Abs(p.Segments[j-1].V1-s.V0) > 1e-8) {
				t.Fatal("구간 불연속")
			}
			if math.Max(s.V0*s.V0, s.V1*s.V1)*math.Abs(s.Curvature) > 1.20001 {
				t.Fatal("정상 회전 횡가속 한도 위반")
			}
			last = advance(s, s.End-s.Start)
		}
		final := p.Segments[len(p.Segments)-1]
		if final.V1 > .001 || math.Abs(final.End-3600) > .001 {
			t.Fatal("종료 시 미정차")
		}
		trip := out.TripSummaries[i].Message["pld"].(map[string]any)
		if math.Abs(trip["dis"].(float64)-last.Distance) > 1e-7 || trip["fc"] != last.Fuel {
			t.Fatal("Trip과 내부 적분 불일치")
		}
	}
}

func TestScenarioReproductionIntervalAndAnomalyQuota(t *testing.T) {
	r := scenarioRequest(3600)
	r.Config.VehicleCount = 4
	r.Generation.AnomalyVehiclePercent = 50
	r.Generation.AnomalyEventsPerHour = 60
	r, _ = normalize(r)
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
	if !reflect.DeepEqual(a.TripSummaries[0].Message, b.TripSummaries[0].Message) {
		t.Fatal("보고 주기가 Trip을 바꿈")
	}
	if a.Scenario.EligibleVehicles != 2 || a.Scenario.ObservedVehicles > 2 || a.Scenario.ObservedVehicles < 1 || a.Scenario.Events < 1 {
		t.Fatal("대상 비율과 관측 집계 실패", a.Scenario)
	}
	selected := 0
	for i := 0; i < 4; i++ {
		p := getScenario(r, i)
		if p.Eligible {
			selected++
		}
		for _, s := range p.Segments {
			if !p.Eligible && s.Event != "" {
				t.Fatal("비대상 차량의 이상 이벤트")
			}
		}
	}
	if selected != 2 {
		t.Fatal("대상 차량 반올림/선정 실패")
	}
	repeat := scenarioRequest(3600)
	repeat.Config.VehicleCount = 4
	repeat.Generation.AnomalyVehiclePercent = 50
	repeat.Generation.AnomalyEventsPerHour = 60
	repeat.Config.IntervalMS = 4000
	repeat, _ = normalize(repeat)
	c, err := process(repeat)
	if err != nil || !reflect.DeepEqual(b.Trace, c.Trace) || !reflect.DeepEqual(b.Scenario, c.Scenario) {
		t.Fatal("seed 재현 실패")
	}
}

func TestScenarioVehicleTraceAndLegacy(t *testing.T) {
	r := scenarioRequest(600)
	out, err := process(r)
	if err != nil {
		t.Fatal(err)
	}
	r.Operation = "trace"
	r.TraceVehicle = 2
	r.TraceStartMS = 100000
	r.TraceUntilMS = 200000
	selected, err := process(r)
	if err != nil || len(selected.Trace) > 120 || selected.Trace[0].ElapsedSeconds < 100 || selected.Trace[len(selected.Trace)-1].ElapsedSeconds > 200 {
		t.Fatal("선택 차량/구간 경계 실패", err)
	}
	if reflect.DeepEqual(out.Trace, selected.Trace) {
		t.Fatal("차량 선택 미반영")
	}
	legacy := automaticRequest()
	legacyOut, err := process(legacy)
	if err != nil || legacyOut.ModelVersion != generatorVersion || legacyOut.Scenario != nil {
		t.Fatal("이전 모델 호환 실패")
	}
}

func TestTraceMessagePageAndBounds(t *testing.T) {
	r := routeRequest(600)
	r.Operation = "trace"
	r.TraceVehicle = 2
	r.TraceStartMS = 10000
	r.TraceUntilMS = 420000
	r.TraceOffset = 100
	r.TraceLimit = 100
	out, err := process(r)
	if err != nil || len(out.Samples) != 100 || out.Planned != 206 {
		t.Fatal("구간 페이지", out, err)
	}
	normalized, _ := normalize(r)
	expected := sampleAt(normalized, 1, 105, Assignment{"v000002", "sim-" + r.RunID + "-c000002"}, "sim")
	if !reflect.DeepEqual(out.Samples[0].Message, expected.Message) || out.Samples[0].Sequence != 106 {
		t.Fatal("원래 생성 주기 불일치")
	}
	r.TraceOffset = 10001
	if _, err := process(r); err == nil {
		t.Fatal("페이지 위치 상한 누락")
	}
	r.TraceOffset = 0
	r.TraceLimit = 101
	if _, err := process(r); err == nil {
		t.Fatal("페이지 크기 상한 누락")
	}
}
