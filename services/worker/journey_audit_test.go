package main

import (
	"math"
	"sort"
	"testing"
	"time"
)

// 제품 분석 화면 없이 시간·속도·거리와 연속 주행을 내부 구간 전체에서 검증한다.
func TestJourneyScenarioMatrix(t *testing.T) {
	for _, minutes := range []int{5, 20, 60, 180} {
		for _, profile := range []string{"urban", "mixed", "highway", "congested"} {
			means, peaks, idles, stops, steady := []float64{}, []float64{}, []float64{}, []float64{}, []float64{}
			for _, hour := range []int{8, 14, 18} {
				for seed := int64(1); seed <= 24; seed++ {
					r := eventRequest(seed, "dry")
					r.Generation.ModelVersion = journeyVersion
					r.Generation.Profile = profile
					r.Generation.DurationSeconds = minutes * 60
					r.Generation.StartTimeMS = time.Date(2026, 11, 18, hour, 0, 0, 0, time.FixedZone("KST", 9*3600)).UnixMilli()
					r.Generation.AnomalyVehiclePercent = 0
					r.Generation.cache = nil
					p := getScenario(r, 0)
					distance, clock, velocity := 0.0, 0.0, 0.0
					for _, s := range p.Segments {
						dt := s.End - s.Start
						if dt <= 0 || math.Abs(s.Start-clock) > 1e-7 || math.Abs(s.V0-velocity) > 1e-7 {
							t.Fatal("운행 구간 불연속", minutes, profile, seed, s)
						}
						if math.Abs((s.V1-s.V0)/dt) > 1.61 || s.Event != "" {
							t.Fatal("정상 운행에 과도한 가감속/이벤트", s)
						}
						if s.V0 < 0 || s.V1 < 0 || math.IsNaN(s.V1) {
							t.Fatal("속도 범위", s)
						}
						distance += (s.V0 + s.V1) * dt / 2
						clock, velocity = s.End, s.V1
					}
					if math.Abs(clock-float64(minutes*60)) > 1e-6 || velocity > .001 {
						t.Fatal("종료 시간/정차 불일치", minutes, profile, seed, clock, velocity)
					}
					stats := summarizeSegments(p.Segments, 0, p.Duration, false)
					if math.Abs(stats.Distance-distance) > .00001 || math.Abs(stats.Average-distance/p.Duration*3.6) > .00001 {
						t.Fatal("시간-속도 적분과 거리/평균 불일치")
					}
					means = append(means, stats.Average)
					peaks = append(peaks, stats.Peak)
					idles = append(idles, stats.IdlePercent)
					stops = append(stops, float64(stats.Stops)*3600/p.Duration)
					steadySeconds := 0.0
					for _, s := range p.Segments {
						dt := s.End - s.Start
						if math.Min(s.V0, s.V1) > 0 && math.Abs((s.V1-s.V0)/dt) < .1 {
							steadySeconds += dt
						}
					}
					steady = append(steady, steadySeconds/p.Duration*100)
				}
			}
			for _, values := range [][]float64{means, peaks, idles, stops, steady} {
				sort.Float64s(values)
			}
			t.Logf("%d분 %s n=%d 평균속도P10/중앙/P90 %.2f/%.2f/%.2f 거리중앙km %.2f 최고속도중앙 %.2f 정차시간%% %.1f 정차횟수/시간 %.1f 저가감속주행%% %.1f", minutes, profile, len(means), means[7], means[36], means[64], means[36]*float64(minutes)/60, peaks[36], idles[36], stops[36], steady[36])
		}
	}
}

func TestScheduledTraceBoundAndRawConsistency(t *testing.T) {
	for _, seconds := range []int64{240, 242, 480, 482, 1200, 3600, 10800} {
		s := ScheduledInput{StartMS: 1794960000000, IntervalMS: 2000, Limit: 100, TID: 1, Seed: 42, Profile: "mixed", Lat: 37.5, Lon: 127, Odometer: 50000, Continuity: EngineContinuity{VehicleSeed: 42, Coolant: 80}}
		s.EndMS = s.StartMS + seconds*1000
		s.FromMS = s.StartMS
		s.UntilMS = s.EndMS
		result, err := scheduledOBD(Request{Scheduled: &s})
		if err != nil {
			t.Fatal(err)
		}
		out := result.Scheduled
		if len(out.Trace) > 120 {
			t.Fatalf("%d초: 상태표 %d점으로 상한120 초과", seconds, len(out.Trace))
		}
		trip := out.Trip["pld"].(map[string]any)
		if trip["edt"].(float64)-trip["stt"].(float64) != float64(seconds*1000) {
			t.Fatal("Trip 전체 운행시간")
		}
		if math.Abs(trip["mesp"].(float64)-trip["dis"].(float64)/float64(seconds)*3.6) > .00001 {
			t.Fatal("Trip 평균/거리")
		}
		previous := -1.0
		for _, point := range out.Trace {
			if point.Distance < previous {
				t.Fatal("누적거리 감소")
			}
			previous = point.Distance
			query := s
			query.FromMS = s.StartMS + int64(math.Round(point.ElapsedSeconds*1000))
			query.UntilMS = query.FromMS
			query.Limit = 1
			page, e := scheduledOBD(Request{Scheduled: &query})
			if e != nil {
				t.Fatal(e)
			}
			if len(page.Scheduled.Messages) != 1 {
				t.Fatal("상태표에 대응하는 원본 없음")
			}
			message := page.Scheduled.Messages[0]
			raw := message["pld"].(map[string]any)
			if message["ty"] != float64(4) || message["ts"].(float64) >= float64(s.EndMS) {
				t.Fatal("Microtrip/종료 시각")
			}
			if point.Distance != raw["cdit"].(float64) || point.Speed != raw["sp"].(float64) {
				t.Fatal("상태표와 Microtrip 불일치")
			}
			if len(message) != 3 {
				t.Fatal("OBD 외 메타정보 혼입")
			}
		}
		s.UntilMS = s.EndMS - 1000
		partial, e := scheduledOBD(Request{Scheduled: &s})
		if e != nil || partial.Scheduled.Trip != nil {
			t.Fatal("종료 전 정상 Trip 발생")
		}
	}
}
