package main

import (
	"math"
	"reflect"
	"testing"
)

func TestTrafficVariesWithoutChangingLegacy(t *testing.T) {
	for _, profile := range []string{"urban", "mixed", "highway", "congested"} {
		r := eventRequest(42, "dry")
		r.Generation.ModelVersion = trafficVersion
		r.Generation.Profile = profile
		r.Generation.AnomalyVehiclePercent = 0
		r.Generation.cache = nil
		p := getScenario(r, 0)
		varying, moving := 0, 0
		peak := 0.0
		for _, s := range p.Segments {
			if s.End <= s.Start {
				t.Fatal("시간 역전")
			}
			if s.V0 > 1 && s.V1 > 1 {
				moving++
				if math.Abs(s.V1-s.V0) > .01 {
					varying++
				}
			}
			if s.Event != "" {
				t.Fatal("비대상 이벤트")
			}
			if math.Abs((s.V1-s.V0)/(s.End-s.Start)) > 1.61 {
				t.Fatal("정상 가감속 초과", s)
			}
			peak = math.Max(peak, math.Max(s.V0, s.V1)*3.6)
		}
		if moving == 0 || float64(varying)/float64(moving) < .6 {
			t.Fatal("반복 정속", profile, varying, moving)
		}
		if profile == "highway" && peak < 70 {
			t.Fatal("고속 흐름 미반영", peak)
		}
		clone := *r.Generation
		clone.cache = nil
		r.Generation = &clone
		if !reflect.DeepEqual(p, getScenario(r, 0)) {
			t.Fatal("seed 재현 실패")
		}
	}
}
