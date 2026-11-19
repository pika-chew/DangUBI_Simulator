package main

import (
	"math"
	"reflect"
	"testing"
)

func TestManeuverClassificationAndCompletion(t *testing.T) {
	urban := roadContext{"도심 도로", 50, .35}
	launch := motionSegment{Start: 0, End: 2, V0: 0, V1: 6, Event: "급가속", Road: urban}
	stop := motionSegment{Start: 0, End: 3, V0: 10, V1: 0, Event: "급감속", Road: urban}
	turn := motionSegment{Start: 0, End: 2 * math.Pi, V0: 6, V1: 6, Curvature: 1. / 12, Event: "급회전", Reason: "저속 유턴", Road: urban}
	for _, tc := range []struct {
		s    motionSegment
		want string
		bit  int
	}{{launch, "급출발", 1}, {stop, "급정지", 64}, {turn, "급유턴", 8}} {
		if got := maneuverEvent(tc.s); got != tc.want {
			t.Fatalf("판정 %s want %s", got, tc.want)
		}
		if got := intervalEventMask([]motionSegment{tc.s}, 0, tc.s.End-.01); got != 0 {
			t.Fatal("완료 전 미래 이벤트 표시", got)
		}
		if got := intervalEventMask([]motionSegment{tc.s}, tc.s.End-.01, tc.s.End); got != tc.bit {
			t.Fatal("완료 구간 비트", got, tc.bit)
		}
		if got := intervalEventMask([]motionSegment{tc.s}, tc.s.End, tc.s.End+2); got != 0 {
			t.Fatal("완료 이후 반복", got)
		}
	}
	for _, tc := range []struct {
		s    motionSegment
		want string
	}{
		{motionSegment{Start: 0, End: 6, V0: 0, V1: 6, Road: urban}, ""},
		{motionSegment{Start: 0, End: 10, V0: 10, V1: 0, Road: urban}, ""},
		{motionSegment{Start: 0, End: 4, V0: 10, V1: 0, Event: "급감속", Road: urban}, "급감속"},
		{motionSegment{Start: 0, End: 2, V0: 0, V1: 3, Event: "급출발", Road: urban}, ""},
		{motionSegment{Start: 0, End: 2, V0: 10, V1: 4, Event: "급정지", Road: urban}, ""},
		{motionSegment{Start: 0, End: math.Pi, V0: 6, V1: 6, Curvature: 1. / 12, Event: "급회전", Reason: "저속 유턴", Road: urban}, ""},
		{motionSegment{Start: 0, End: 2 * math.Pi, V0: 6, V1: 6, Curvature: 1. / 12, Event: "급유턴", Road: roadContext{"고속 도로", 100, 0}}, ""},
		{motionSegment{Start: 0, End: 3 * math.Pi, V0: 4, V1: 4, Curvature: 1. / 12, Event: "급유턴", Road: urban}, ""},
	} {
		if got := maneuverEvent(tc.s); got != tc.want {
			t.Fatalf("일반/불완전 기동 오분류 %+v: %s", tc.s, got)
		}
	}
}

func TestJourneyGeneratesAllManeuversWithinExistingEligibility(t *testing.T) {
	counts := map[string]int{}
	for seed := int64(0); seed < 100; seed++ {
		r := eventRequest(seed, "dry")
		r.Generation.ModelVersion = journeyVersion
		r.Generation.Profile = "urban"
		r.Generation.cache = nil
		p := getScenario(r, 0)
		lastEnd := -30.
		for _, s := range p.Segments {
			if s.Event == "" {
				continue
			}
			if s.Start < lastEnd+30-1e-6 {
				t.Fatal("이벤트 회복 간격", seed, s, lastEnd)
			}
			lastEnd = s.End
			counts[s.Event]++
			if s.Event == "급출발" || s.Event == "급유턴" || s.Event == "급정지" {
				if maneuverEvent(s) != s.Event {
					t.Fatal("실제 움직임과 판정 불일치", s)
				}
				bit := map[string]int{"급출발": 1, "급유턴": 8, "급정지": 64}[s.Event]
				if intervalEventMask(p.Segments, s.End-.001, s.End)&bit == 0 {
					t.Fatal("실제 생성 em 누락", s)
				}
			}
		}
		g := *r.Generation
		g.cache = nil
		r.Generation = &g
		if !reflect.DeepEqual(p, getScenario(r, 0)) {
			t.Fatal("재생성 불일치")
		}
	}
	for _, kind := range []string{"급출발", "급유턴", "급정지"} {
		if counts[kind] == 0 {
			t.Fatal("기동 미생성", kind, counts)
		}
	}
	t.Log("고빈도 개발 조건의 발생 건수", counts)
	for _, percent := range []float64{0, 100} {
		r := eventRequest(42, "dry")
		r.Generation.ModelVersion = journeyVersion
		r.Generation.AnomalyVehiclePercent = percent
		if percent == 100 {
			r.Generation.AnomalyEventsPerHour = 0
		}
		r.Generation.cache = nil
		for _, s := range getScenario(r, 0).Segments {
			if s.Event != "" {
				t.Fatal("비대상/빈도0에서 이상 이벤트", s)
			}
		}
	}
}
