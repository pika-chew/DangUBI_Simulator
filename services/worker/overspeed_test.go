package main

import "testing"

func TestOverspeedThresholdWindowAndCombination(t *testing.T) {
	road := roadContext{"도심 도로", 50, .3}
	s := motionSegment{Start: 0, End: 2, V0: 15, V1: 25, Road: road, Event: "급가속"}
	if got := intervalEventMask([]motionSegment{s}, 0, 1); got != 128|32 {
		t.Fatal("과속과 급가속 결합", got)
	}
	if got := intervalEventMask([]motionSegment{s}, 2, 3); got != 0 {
		t.Fatal("종료 이후 과속 반복", got)
	}
	for _, kmh := range []float64{49, 50, 50.005} {
		s.V0 = kmh / 3.6
		s.V1 = s.V0
		s.Event = ""
		if got := intervalEventMask([]motionSegment{s}, 0, 2); got != 0 {
			t.Fatal("경계 이하 과속", kmh, got)
		}
	}
	s.V0 = 50.02 / 3.6
	s.V1 = s.V0
	if got := intervalEventMask([]motionSegment{s}, 0, 2); got != 128 {
		t.Fatal("과속 누락", got)
	}
	s.Road.Limit = 0
	if got := intervalEventMask([]motionSegment{s}, 0, 2); got != 0 {
		t.Fatal("제한속도 미정 구간에서 과속", got)
	}
}

func TestOverspeedCrossingDoesNotReportFuture(t *testing.T) {
	s := motionSegment{Start: 0, End: 10, V0: 10, V1: 20, Road: roadContext{"간선 도로", 54, .1}}
	a, b, ok := overspeedSpan(s)
	if !ok || a <= 5 || b != 10 {
		t.Fatal(a, b, ok)
	}
	if got := intervalEventMask([]motionSegment{s}, 0, 5); got != 0 {
		t.Fatal("초과 전 미래 판정", got)
	}
	if got := intervalEventMask([]motionSegment{s}, 5, 6); got != 128 {
		t.Fatal("보고 사이 초과 누락", got)
	}
	s.V0, s.V1 = 20, 10
	a, b, ok = overspeedSpan(s)
	if !ok || a != 0 || b >= 5 {
		t.Fatal(a, b, ok)
	}
	if got := intervalEventMask([]motionSegment{s}, 5, 6); got != 0 {
		t.Fatal("과속 해제 이후 판정", got)
	}
}

func TestMandatoryOverspeedMessageAndSearchAgree(t *testing.T) {
	r := Request{Config: Config{VehicleCount: 1, ClientCount: 1, MessagesPerVehicle: 11, IntervalMS: 2000},
		Generation: &Generation{ModelVersion: scenarioVersion, DurationSeconds: 20, StartTimeMS: 100000, Profile: "urban", StartLat: 37.5, StartLon: 127}}
	segment := motionSegment{Start: 0, End: 20, V0: 20, V1: 20, Road: roadContext{"도심 도로", 50, .3}}
	r.Generation.cache = &scenarioCache{plans: map[int]*scenarioPlan{0: {Segments: []motionSegment{segment}, Duration: 20}}}
	message := func(seq int) map[string]any { m, _ := automaticMessage(r, 0, seq); return m }
	if em := message(1)["pld"].(map[string]any)["em"]; em != 128 {
		t.Fatal("include_em 기본false여도 필수", em)
	}
	if _, ok := message(10)["pld"].(map[string]any)["em"]; ok {
		t.Fatal("Trip에 em 추가")
	}
	s := &ScheduledInput{StartMS: 100000, IntervalMS: 2000, Limit: 100, EventMask: 128}
	result := searchEventMessages(s, r, 0, 9, message)
	if !result.Complete || len(result.Rows) != 9 || result.Zero != 1 {
		t.Fatal(result)
	}
	for _, row := range result.Rows {
		if row.Message["pld"].(map[string]any)["em"] != 128 || len(row.Evidence) != 1 || row.Evidence[0].Kind != "과속" || row.Evidence[0].Limit != 50 {
			t.Fatal(row)
		}
	}
}
