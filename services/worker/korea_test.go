package main

import (
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"math"
	"strings"
	"testing"
)

func koreaRequest(duration int, weather string) Request {
	r := routeRequest(duration)
	r.Generation.ModelVersion = koreaVersion
	r.Generation.Weather = weather
	r, _ = normalize(r)
	return r
}
func TestKoreaRoadSpeedAndWeather(t *testing.T) {
	for _, weather := range []string{"dry", "rain"} {
		for _, profile := range []string{"urban", "mixed", "highway", "congested"} {
			for seed := int64(0); seed < 30; seed++ {
				r := koreaRequest(3600, weather)
				r.Generation.Profile = profile
				r.Generation.Seed = seed
				for i := 0; i < 3; i++ {
					p := getScenario(r, i)
					expected := "건조"
					if weather == "rain" {
						expected = "강우·젖은 노면"
					}
					if p.Weather != expected {
						t.Fatal("차량 간 날씨 불일치")
					}
					for _, s := range p.Segments {
						factor := 1.0
						if weather == "rain" {
							factor = .8
						}
						base := map[string]float64{"도심 도로": 50, "간선 도로": 50, "골목·좁은 도로": 20, "보호구역 · 도심 도로": 30, "진출입로": 50, "고속 도로": 100}[s.Road.Name]
						if math.Abs(s.Road.Limit-base*factor) > 1e-8 || math.Max(s.V0, s.V1)*3.6 > s.Road.Limit+1e-5 {
							t.Fatal("구간 속도 상한 위반", weather, profile, seed, s)
						}
						if strings.Contains(s.Reason, "유턴") {
							t.Fatal("허용 미확인 유턴 생성")
						}
						if s.Road.Name == "고속 도로" && (strings.Contains(s.Reason, "신호") || strings.Contains(s.Reason, "볼일") || s.Curvature != 0) {
							t.Fatal("본선 상황 위반")
						}
					}
				}
			}
		}
	}
}
func TestKoreaAnomalyRespectsWeatherCapAndReplay(t *testing.T) {
	r := koreaRequest(600, "rain")
	r.Generation.AnomalyVehiclePercent = 100
	r.Generation.AnomalyEventsPerHour = 60
	r, _ = normalize(r)
	out, err := process(r)
	if err != nil {
		t.Fatal(err)
	}
	if out.Scenario == nil || out.Scenario.Events == 0 {
		t.Fatal("강우 이상 이벤트 시험에 실제 이벤트가 없습니다.")
	}
	for i := 0; i < 3; i++ {
		p := getScenario(r, i)
		for _, s := range p.Segments {
			if math.Max(s.V0, s.V1)*3.6 > s.Road.Limit+1e-5 {
				t.Fatal("이벤트가 강우 상한을 무시함", s)
			}
		}
	}
	audit := auditRequest{Request: r, AnalysisProcessed: out.Processed}
	audit.TraceUntilMS = 600000
	analysis, err := processAudit(audit)
	if err != nil {
		t.Fatal(err)
	}
	distance := 0.0
	for _, trip := range out.TripSummaries {
		distance += trip.Message["pld"].(map[string]any)["dis"].(float64)
	}
	near(t, analysis.Analysis.Summary.Distance, distance)
	r.Generation.Weather = "snow"
	if _, err := process(r); err == nil {
		t.Fatal("미지원 날씨 허용")
	}
	r.Generation.ModelVersion = routeVersion
	r.Generation.Weather = "rain"
	if _, err := process(r); err == nil {
		t.Fatal("구버전 날씨 설정을 조용히 무시함")
	}
}

func TestPreviousModelsKeepPublishedOutput(t *testing.T) {
	expected := map[string]string{
		koreaVersion:     "4be852b58ca24333a961bc62e7a1b47af57fc92f0981b8afe3e194780d09c8a9",
		generatorVersion: "9c357de35f770dcece11412078f209c1e5d791f0fe432f3f7fc2d548898cea01",
		scenarioVersion:  "912301a9856ba79113b007454bda87d5fba780021fff66fdacc97c68692806a3",
		routeVersion:     "11be3113125b44d8ec1af51412f1301908a177570cf74c5b7761cf22902d3542",
	}
	// v1~v3은0a8fe29, v4는df19242 배포 코드의 3대·600초·도심·seed42 결과 기준값이다.
	for version, hash := range expected {
		r := scenarioRequest(600)
		r.Generation.ModelVersion = version
		r.Generation.Profile = "urban"
		out, err := process(r)
		if err != nil {
			t.Fatal(err)
		}
		data, _ := json.Marshal(map[string]any{"samples": out.Samples, "trip_summaries": out.TripSummaries, "trace": out.Trace, "scenario": out.Scenario})
		if fmt.Sprintf("%x", sha256.Sum256(data)) != hash {
			t.Fatal("구버전 결과가 변경됨", version)
		}
	}
}
func TestKoreaInternalDriveAudit(t *testing.T) {
	for _, profile := range []string{"urban", "highway", "congested", "mixed"} {
		seconds, distance, idle := 0.0, 0.0, 0.0
		for seed := int64(0); seed < 30; seed++ {
			r := koreaRequest(3600, "dry")
			r.Generation.Profile = profile
			r.Generation.Seed = seed
			p := getScenario(r, 0)
			s := summarizeSegments(p.Segments, 0, 3600, p.Eligible)
			seconds += s.VehicleSeconds
			distance += s.Distance
			idle += s.Idle
		}
		t.Logf("개발 점검 · %s 30대×1시간 · 평균 %.2f km/h · 정차 %.2f%% (한국 실차 적합 판정 아님)", profile, distance/seconds*3.6, idle/seconds*100)
	}
}

func TestAnalysisExcludedFromRuntime(t *testing.T) {
	r := koreaRequest(600, "dry")
	r.Operation = "analysis"
	if _, err := process(r); err == nil {
		t.Fatal("개발 분석이 실행 Worker에 노출됨")
	}
}
