package main

import (
	"math"
	"reflect"
	"sort"
	"testing"
)

func TestJourneyTimeSpeedDistanceAndRecovery(t *testing.T) {
	for _, profile := range []string{"urban", "mixed", "highway", "congested"} {
		means, peaks := []float64{}, []float64{}
		for seed := int64(1); seed <= 24; seed++ {
			r := eventRequest(seed, "dry")
			r.Generation.ModelVersion = journeyVersion
			r.Generation.Profile = profile
			r.Generation.AnomalyVehiclePercent = 0
			r.Generation.cache = nil
			p := getScenario(r, 0)
			distance := 0.0
			for _, s := range p.Segments {
				seconds := s.End - s.Start
				if seconds <= 0 || s.V0 < 0 || s.V1 < 0 || s.Event != "" {
					t.Fatal("시간/속도/비대상 사건", s)
				}
				if math.Abs((s.V1-s.V0)/seconds) > 1.61 {
					t.Fatal("정상 가감속 한도", s)
				}
				distance += (s.V0 + s.V1) * seconds / 2
			}
			last := p.Segments[len(p.Segments)-1]
			total := advance(last, last.End-last.Start)
			if math.Abs(total.Distance-distance) > .00001 {
				t.Fatal("시간-속도 적분과 거리 불일치")
			}
			if math.Abs(last.End-p.Duration) > .00001 {
				t.Fatal("운행시간 누락")
			}
			means = append(means, total.Distance/p.Duration*3.6)
			peaks = append(peaks, total.Peak*3.6)
			clone := *r.Generation
			clone.cache = nil
			r.Generation = &clone
			if !reflect.DeepEqual(p, getScenario(r, 0)) {
				t.Fatal("부분 조회/seed 재현")
			}
		}
		sort.Float64s(means)
		sort.Float64s(peaks)
		t.Logf("%s / 1시간: 거리km·평균kmh P10 %.2f 중앙 %.2f P90 %.2f / 최고속도 중앙 %.2f", profile, means[2], means[12], means[21], peaks[12])
		if profile == "highway" && means[12] < 50 {
			t.Fatal("원활 고속 구간 미반영")
		}
		if profile == "congested" && peaks[12] < 40 {
			t.Fatal("정체를 운행 내내 강제함")
		}
	}
}
