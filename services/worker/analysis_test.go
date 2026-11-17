package main

import (
	"math"
	"reflect"
	"testing"
)

func near(t *testing.T, a, b float64) {
	t.Helper()
	if math.Abs(a-b) > 1e-6 {
		t.Fatalf("불일치: %.12f != %.12f", a, b)
	}
}
func TestAnalysisExactDurationAndStops(t *testing.T) {
	a := roadContext{Name: "A"}
	b := roadContext{Name: "B"}
	segments := []motionSegment{
		{Start: 0, End: 5, Road: a}, {Start: 5, End: 10, Road: a},
		{Start: 10, End: 30, V0: 0, V1: 20, Road: a, Event: "급가속", Reason: "앞차 속도 유지"},
		{Start: 30, End: 40, V0: 20, V1: 20, Road: a},
		{Start: 40, End: 60, V0: 20, V1: 0, Road: b, Reason: "혼잡·앞차 감속"},
		{Start: 60, End: 70, Road: b},
	}
	s := summarizeSegments(segments, 0, 70, true)
	near(t, s.VehicleSeconds, 70)
	near(t, s.Distance, 600)
	near(t, s.Idle, 20)
	if s.Stops != 2 || s.LongestStop != 10 || s.EventCount != 1 || s.EligibleVehicles != 1 {
		t.Fatal("정차/이벤트", s)
	}
	near(t, s.Speed[1].Seconds, 100.0/9)
	near(t, s.Speed[4].Seconds, 50.0/3)
	for _, bins := range [][]TimeBin{s.Speed, s.Acceleration, s.Traffic} {
		sum := 0.0
		percent := 0.0
		for _, b := range bins {
			sum += b.Seconds
			percent += b.Percent
		}
		near(t, sum, 70)
		near(t, percent, 100)
	}
	if len(s.Roads) != 2 || s.Roads[0].Entries != 1 || s.Roads[0].Longest != 40 {
		t.Fatal("연속 도로", s.Roads)
	}
	near(t, s.EligibleEventRate, 3600.0/70)
	left := summarizeSegments(segments, 0, 11, true)
	right := summarizeSegments(segments, 11, 70, true)
	if left.EventCount+right.EventCount != 1 {
		t.Fatal("경계 이벤트 중복")
	}
	near(t, left.Distance+right.Distance, s.Distance)
	clipped := summarizeSegments(segments, 2, 8, false)
	if clipped.Stops != 1 {
		t.Fatal("인접 정차 병합")
	}
	near(t, clipped.Idle, 6)
}
func TestAnalysisMatchesTripAndIntervalInvariant(t *testing.T) {
	for _, version := range []string{generatorVersion, scenarioVersion, routeVersion} {
		r := scenarioRequest(600)
		r.Generation.ModelVersion = version
		r.Generation.Profile = "urban"
		r, _ = normalize(r)
		generated, err := process(r)
		if err != nil {
			t.Fatal(err)
		}
		r.Operation = "analysis"
		r.TraceUntilMS = 600000
		r.AnalysisProcessed = generated.Processed
		out, err := process(r)
		if err != nil {
			t.Fatal(err)
		}
		distance, idle := 0.0, 0.0
		for _, trip := range generated.TripSummaries {
			pld := trip.Message["pld"].(map[string]any)
			distance += pld["dis"].(float64)
			idle += pld["idt"].(float64)
		}
		near(t, out.Analysis.Summary.Distance, distance)
		near(t, out.Analysis.Summary.Idle, idle)
		near(t, out.Analysis.Summary.VehicleSeconds, 1800)
		r.Config.IntervalMS = 4000
		r, _ = normalize(r)
		r.AnalysisProcessed = r.Config.VehicleCount * r.Config.MessagesPerVehicle
		second, err := process(r)
		if err != nil || !reflect.DeepEqual(out.Analysis.Summary, second.Analysis.Summary) {
			t.Fatal("보고 주기에 따라 통계 변경", version, err)
		}
	}
}
func TestAnalysisPartialAndBoundaries(t *testing.T) {
	r := routeRequest(600)
	r.Operation = "analysis"
	r.AnalysisProcessed = 5
	r.TraceUntilMS = 600000
	out, err := process(r)
	if err != nil {
		t.Fatal(err)
	}
	near(t, out.Analysis.Summary.VehicleSeconds, 4)
	if out.Analysis.Total != 2 {
		t.Fatal("생성 전 차량을 분석함")
	}
	r.AnalysisVehicle = 3
	r.TraceUntilMS = 2000
	if _, err := process(r); err == nil {
		t.Fatal("미래 구간 분석")
	}
	r.AnalysisProcessed = 0
	if _, err := process(r); err == nil {
		t.Fatal("미생성 차량 미래 분석")
	}
	r.AnalysisVehicle = 0
	r.AnalysisProcessed = 0
	out, err = process(r)
	if err != nil || out.Analysis.Summary.VehicleSeconds != 0 {
		t.Fatal("빈 분석", err)
	}
}
func TestAnalysisFleetPageAndEligibility(t *testing.T) {
	r := routeRequest(60)
	r.Config.VehicleCount = 60
	r.Generation.AnomalyVehiclePercent = 25
	r.Generation.AnomalyEventsPerHour = 60
	r, _ = normalize(r)
	r.Operation = "analysis"
	r.TraceUntilMS = 60000
	r.AnalysisProcessed = r.Config.VehicleCount * r.Config.MessagesPerVehicle
	out, err := process(r)
	if err != nil {
		t.Fatal(err)
	}
	a := out.Analysis
	if a.Summary.EligibleVehicles != 15 || a.TargetEligible != 15 || a.Total != 60 || len(a.Vehicles) != 50 || a.Summary.EventVehicles > 15 {
		t.Fatal("분석 대상·페이지", a)
	}
	r.AnalysisOffset = 50
	b, err := process(r)
	if err != nil || len(b.Analysis.Vehicles) != 10 || b.Analysis.Vehicles[0].VehicleID != "v000051" || !reflect.DeepEqual(a.Summary, b.Analysis.Summary) {
		t.Fatal("분석 페이지", err)
	}
	near(t, a.Summary.VehicleSeconds, 3600)
	r.AnalysisVehicle = 1
	r.TraceStartMS = 10000
	r.TraceUntilMS = 20000
	r.AnalysisOffset = 0
	one, err := process(r)
	if err != nil {
		t.Fatal(err)
	}
	near(t, one.Analysis.Summary.VehicleSeconds, 10)
}

func TestAnalysisAccelerationBoundary(t *testing.T) {
	s := summarizeSegments([]motionSegment{{Start: 0, End: 20.0 / 3, V0: 10, V1: 0}}, 0, 20.0/3, false)
	near(t, s.Acceleration[1].Seconds, 0)
	near(t, s.Acceleration[2].Seconds, 20.0/3)
}
