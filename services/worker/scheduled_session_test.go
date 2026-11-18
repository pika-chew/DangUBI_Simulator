package main

import (
	"bytes"
	"encoding/json"
	"reflect"
	"testing"
)

func sessionInput(seconds int64) ScheduledInput {
	s := ScheduledInput{StartMS: 1794960000000, IntervalMS: 2000, Limit: 100, TID: 1, Seed: 42, Profile: "mixed", Lat: 37.5, Lon: 127, Odometer: 50000, Continuity: EngineContinuity{VehicleSeed: 42, Coolant: 80}}
	s.EndMS = s.StartMS + seconds*1000
	s.FromMS = s.StartMS
	s.UntilMS = s.EndMS
	return s
}
func TestScheduleSessionPagesMatchLegacyAndReuse(t *testing.T) {
	for _, seconds := range []int64{242, 600, 3600, 10800} {
		for _, force := range []bool{false, true} {
			s := sessionInput(seconds)
			s.ForceEnd = force
			s.AnomalyEligible = true
			s.AnomalyEventsPerHour = 6
			s.AnomalyIntensity = "medium"
			if force {
				s.UntilMS = s.StartMS + seconds*500
			}
			cache := scheduledCache{}
			count := 0
			for {
				old, e := scheduledOBD(Request{Scheduled: &s})
				if e != nil {
					t.Fatal(e)
				}
				current, e := scheduledPage(Request{Scheduled: &s}, &cache, true)
				if e != nil {
					t.Fatal(e)
				}
				a, b := old.Scheduled, current.Scheduled
				if !reflect.DeepEqual(a.Messages, b.Messages) || a.MicroCount != b.MicroCount || a.FinalOdometer != b.FinalOdometer || a.FinalLat != b.FinalLat || a.FinalLon != b.FinalLon || a.FinalCoolant != b.FinalCoolant || a.FinalHeading != b.FinalHeading || a.Synthetic != b.Synthetic {
					t.Fatal("기존 생성과 원본/연속 상태 불일치", seconds, s.Offset)
				}
				if len(b.Trace) != 0 {
					t.Fatal("실행 응답에 그래프 생성")
				}
				count++
				more := int64(s.Offset+len(b.Messages)) < b.MicroCount
				if !more {
					if !reflect.DeepEqual(a.Trip, b.Trip) {
						t.Fatal("종료 요약 불일치")
					}
					break
				}
				if b.Trip != nil {
					t.Fatal("마지막 페이지 전 요약 전달")
				}
				s.Offset += len(b.Messages)
			}
			if cache.misses != 1 || cache.hits != count-1 {
				t.Fatal("모션/엔진 재사용 누락", cache.misses, cache.hits, count)
			}
		}
	}
}
func TestScheduleSessionCacheIsolationAndEviction(t *testing.T) {
	c := scheduledCache{}
	for i := int64(0); i < scheduleCacheLimit+4; i++ {
		s := sessionInput(1)
		s.Seed += i
		if _, e := scheduledPage(Request{Scheduled: &s}, &c, true); e != nil {
			t.Fatal(e)
		}
		if len(c.entries) > scheduleCacheLimit || c.bytes > scheduleCacheBytes {
			t.Fatal("캐시 상한")
		}
	}
	if c.evictions != 4 {
		t.Fatal("캐시 정리 누락")
	}
	s := sessionInput(1)
	a, _ := scheduledPage(Request{Scheduled: &s}, &c, true)
	b, _ := scheduledOBD(Request{Scheduled: &s})
	if !reflect.DeepEqual(a.Scheduled.Messages, b.Scheduled.Messages) || c.misses != scheduleCacheLimit+5 {
		t.Fatal("축출 후 동일 생성")
	}
	s.Odometer++
	a, _ = scheduledPage(Request{Scheduled: &s}, &c, true)
	b, _ = scheduledOBD(Request{Scheduled: &s})
	if !reflect.DeepEqual(a.Scheduled.Messages, b.Scheduled.Messages) || c.misses != scheduleCacheLimit+6 {
		t.Fatal("초기 상태 캐시 충돌")
	}
}

func TestScheduleSessionThousandVehiclesReuseAndMatchOriginal(t *testing.T) {
	c := scheduledCache{}
	for pass := 0; pass < 2; pass++ {
		for vehicle := int64(0); vehicle < 1000; vehicle++ {
			s := sessionInput(12)
			s.Seed += vehicle
			s.TID += vehicle
			s.Limit = 1
			s.Offset = pass
			got, err := scheduledPage(Request{Scheduled: &s}, &c, true)
			if err != nil {
				t.Fatal(err)
			}
			if vehicle%97 == 0 {
				old, err := scheduledOBD(Request{Scheduled: &s})
				if err != nil || !reflect.DeepEqual(got.Scheduled.Messages, old.Scheduled.Messages) || got.Scheduled.FinalOdometer != old.Scheduled.FinalOdometer {
					t.Fatal("차량별 페이지 원문/상태 불일치", vehicle, pass)
				}
			}
			if c.bytes > scheduleCacheBytes || len(c.entries) > scheduleCacheLimit {
				t.Fatal("캐시 예산 초과")
			}
		}
	}
	if c.misses != 1000 || c.hits != 1000 || c.evictions != 0 {
		t.Fatal("다수 차량 운행 재사용 실패", c.misses, c.hits, c.evictions)
	}
}
func TestScheduleSessionProtocolIDsAndClose(t *testing.T) {
	for _, bad := range []bool{false, true} {
		var input, output bytes.Buffer
		enc := json.NewEncoder(&input)
		s := sessionInput(600)
		enc.Encode(map[string]any{"id": 1, "command": "page", "scheduled": s})
		id := 2
		if bad {
			id = 1
		}
		s.Offset = 100
		enc.Encode(map[string]any{"id": id, "command": "page", "scheduled": s})
		enc.Encode(map[string]any{"id": 3, "command": "close"})
		serveScheduleStream(&input, &output)
		dec := json.NewDecoder(&output)
		var replies []map[string]any
		for dec.More() {
			var r map[string]any
			if dec.Decode(&r) != nil {
				break
			}
			replies = append(replies, r)
		}
		if bad {
			if len(replies) != 3 || replies[2]["ok"] != false {
				t.Fatal("중복 ID 거절")
			}
			continue
		}
		if len(replies) != 4 || replies[3]["id"] != float64(3) || replies[3]["ok"] != true {
			t.Fatal("종료 확인")
		}
		stats := replies[2]["stats"].(map[string]any)
		if stats["plan_builds"] != float64(1) || stats["cache_hits"] != float64(1) {
			t.Fatal("프로토콜 재사용")
		}
		page := replies[1]["scheduled"].(map[string]any)
		if _, found := page["trace"]; found {
			t.Fatal("실행 응답 필드")
		}
	}
}

func TestScheduleSessionRealPlanByteBudget(t *testing.T) {
	c := scheduledCache{}
	for seed := int64(1); seed <= 8; seed++ {
		s := sessionInput(86400)
		s.Seed = seed
		if _, err := scheduledPage(Request{Scheduled: &s}, &c, true); err != nil {
			t.Fatal(err)
		}
		if c.bytes > scheduleCacheBytes {
			t.Fatal("모션·엔진 추정 보유량 초과")
		}
	}
	if len(c.entries) >= scheduleCacheLimit || c.evictions == 0 {
		t.Fatal("긴 운행의 바이트 예산으로 축출되지 않음", len(c.entries), c.bytes)
	}
}

func TestScheduleSessionResponseByteBudget(t *testing.T) {
	var in, out bytes.Buffer
	enc := json.NewEncoder(&in)
	s := sessionInput(600)
	enc.Encode(map[string]any{"id": 1, "command": "page", "scheduled": s, "byte_limit": 16384})
	enc.Encode(map[string]any{"id": 2, "command": "close"})
	serveScheduleStream(&in, &out)
	dec := json.NewDecoder(&out)
	var ready, reply map[string]any
	dec.Decode(&ready)
	dec.Decode(&reply)
	raw, _ := json.Marshal(reply)
	if len(raw)+1 > 16384 || reply["ok"] != true {
		t.Fatal("바이트 응답 상한")
	}
	page := reply["scheduled"].(map[string]any)
	messages := page["messages"].([]any)
	if len(messages) == 0 || len(messages) >= 100 || reply["next_offset"] != float64(len(messages)) {
		t.Fatal("바이트 기준 페이지 분할")
	}
	legacy, _ := scheduledOBD(Request{Scheduled: &s})
	expected, _ := json.Marshal(legacy.Scheduled.Messages[:len(messages)])
	actual, _ := json.Marshal(messages)
	if !bytes.Equal(expected, actual) {
		t.Fatal("작은 페이지 원본 불일치")
	}
}
