package main

import (
	"reflect"
	"testing"
)

func TestEventSearchMatchesEveryActualMicrotripAndPages(t *testing.T) {
	for _, interval := range []int64{100, 700, 2000, 70000} {
		s := sessionInput(1800)
		s.IncludeEM = true
		s.AnomalyEligible = true
		s.AnomalyEventsPerHour = 60
		s.AnomalyIntensity = "high"
		s.Profile = "urban"
		s.IntervalMS = interval
		s.FromMS += 12345 // 관측 시작이 보고 주기 경계가 아닌 경우에도 같은 메시지를 검색한다.
		cache := scheduledCache{}
		expected := []map[string]any{}
		total := int64(0)
		for offset := 0; ; {
			s.SearchEvents = false
			s.Offset = offset
			s.Limit = 100
			result, err := scheduledPage(Request{Scheduled: &s}, &cache, true)
			if err != nil {
				t.Fatal(err)
			}
			total = result.Scheduled.MicroCount
			for _, m := range result.Scheduled.Messages {
				if m["pld"].(map[string]any)["em"].(int) != 0 {
					expected = append(expected, m)
				}
			}
			offset += len(result.Scheduled.Messages)
			if int64(offset) >= total {
				break
			}
		}
		for _, filter := range []int{0, 1, 8, 64, 128, 1 | 8 | 64 | 128} {
			found := []map[string]any{}
			nonzero, zero := int64(0), int64(0)
			cursor := int64(0)
			for attempts := 0; ; attempts++ {
				if attempts > 2000 {
					t.Fatal("검색 페이지 진행 실패")
				}
				s.SearchEvents = true
				s.EventMask = filter
				s.Offset = int(cursor)
				s.Limit = 7
				result, err := scheduledPage(Request{Scheduled: &s}, &cache, true)
				if err != nil {
					t.Fatal(err)
				}
				p := result.Scheduled.EventSearch
				if len(p.Rows) > 7 || p.Total != total || p.NextOffset < cursor || p.Scanned != p.NextOffset-cursor {
					t.Fatal("검색 경계", p)
				}
				nonzero += p.Nonzero
				zero += p.Zero
				for _, row := range p.Rows {
					found = append(found, row.Message)
					if len(row.Evidence) == 0 {
						t.Fatal("판정 근거 누락")
					}
					for _, e := range row.Evidence {
						if e.EndMS > int64(row.Message["ts"].(float64)) {
							t.Fatal("미래 판정 근거 노출")
						}
					}
				}
				if p.Complete {
					break
				}
				if p.NextOffset <= cursor {
					t.Fatal("검색 진행 없음")
				}
				cursor = p.NextOffset
			}
			want := []map[string]any{}
			for _, m := range expected {
				if filter == 0 || m["pld"].(map[string]any)["em"].(int)&filter != 0 {
					want = append(want, m)
				}
			}
			if !reflect.DeepEqual(found, want) {
				t.Fatalf("주기%d/필터%d: 검색%d 원문%d", interval, filter, len(found), len(want))
			}
			if nonzero != int64(len(expected)) || zero+nonzero != total {
				t.Fatal("전체 필드 통계 불일치", nonzero, zero, total)
			}
		}
	}
}

func TestEventSearchDistinguishesDisabledEmptyAndZero(t *testing.T) {
	s := sessionInput(4)
	s.SearchEvents = true
	a, err := scheduledOBD(Request{Scheduled: &s})
	if err != nil {
		t.Fatal(err)
	}
	if p := a.Scheduled.EventSearch; p.Missing != 0 || p.Zero != 2 || !p.Complete {
		t.Fatal(p)
	}
	s.IncludeEM = true
	b, err := scheduledOBD(Request{Scheduled: &s})
	if err != nil {
		t.Fatal(err)
	}
	if p := b.Scheduled.EventSearch; p.Missing != 0 || p.Zero != 2 || !p.Complete {
		t.Fatal(p)
	}
	s.FromMS = s.EndMS
	c, err := scheduledOBD(Request{Scheduled: &s})
	if err != nil {
		t.Fatal(err)
	}
	if p := c.Scheduled.EventSearch; p.Total != 0 || p.Missing != 0 || p.Zero != 0 || !p.Complete {
		t.Fatal(p)
	}
	s.EventMask = 128 // bit7 = 과속
	if _, err := scheduledOBD(Request{Scheduled: &s}); err != nil {
		t.Fatal("과속 bit7 거부", err)
	}
	s.EventMask = 256 // bit8 이상은 없다
	if _, err := scheduledOBD(Request{Scheduled: &s}); err == nil {
		t.Fatal("bit8 허용")
	}
}
