package main

import (
	"reflect"
	"testing"
)

func TestMicrotripEventIntervalAndBits(t *testing.T) {
	segments := []motionSegment{
		{Start: 0, End: 2},
		{Start: 2, End: 3, Event: "급가속"},
		{Start: 3, End: 4, Event: "급회전", Curvature: .1},
		{Start: 4, End: 5, Event: "급회전", Curvature: -.1},
		{Start: 5, End: 6, Event: "급감속"},
		{Start: 6, End: 10},
	}
	for _, tc := range []struct {
		from, until float64
		want        int
	}{
		{0, 0, 0}, {0, 2, 32}, {2, 2.5, 32}, {3, 3.5, 2},
		{4, 4.5, 4}, {5, 5.5, 16}, {0, 6, 54}, {6, 10, 0},
	} {
		if got := intervalEventMask(segments, tc.from, tc.until); got != tc.want {
			t.Fatalf("(%v,%v] got %d want %d", tc.from, tc.until, got, tc.want)
		}
	}
}

func TestMicrotripMandatoryEMAndCachedPages(t *testing.T) {
	s := sessionInput(1800)
	s.AnomalyEligible = true
	s.AnomalyEventsPerHour = 60
	s.AnomalyIntensity = "high"
	s.Limit = 100
	cache := scheduledCache{}
	found := false
	for offset := 0; offset < 900; offset += 100 {
		s.Offset = offset
		s.IncludeEM = false
		off, err := scheduledPage(Request{Scheduled: &s}, &cache, true)
		if err != nil {
			t.Fatal(err)
		}
		s.IncludeEM = true
		on, err := scheduledPage(Request{Scheduled: &s}, &cache, true)
		if err != nil {
			t.Fatal(err)
		}
		// 조회 경로/페이지 재생성과 실행 세션이 같은 em을 반환해야 한다.
		view, err := scheduledOBD(Request{Scheduled: &s})
		if err != nil {
			t.Fatal(err)
		}
		if !reflect.DeepEqual(on.Scheduled.Messages, view.Scheduled.Messages) {
			t.Fatal("조회/실행 메시지 불일치")
		}
		for i, m := range on.Scheduled.Messages {
			p := m["pld"].(map[string]any)
			mask, ok := p["em"].(int)
			if !ok || mask < 0 || mask > 383 || mask&128 != 0 {
				t.Fatal("비트마스크 없음/범위 초과")
			}
			found = found || mask != 0
			if !reflect.DeepEqual(m, off.Scheduled.Messages[i]) {
				t.Fatal("em 이외의 주행 데이터 변경")
			}
		}
		if !reflect.DeepEqual(on.Scheduled.Trip, off.Scheduled.Trip) || on.Scheduled.FinalOdometer != off.Scheduled.FinalOdometer || on.Scheduled.FinalCoolant != off.Scheduled.FinalCoolant {
			t.Fatal("Trip/최종 상태 변경")
		}
	}
	if !found {
		t.Fatal("이상 이벤트가 있는 운행에서 em이 모두0")
	}
}
