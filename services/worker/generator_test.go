package main

import (
	"encoding/json"
	"math"
	"reflect"
	"testing"
)

func automaticRequest() Request {
	req := request("shared", 2)
	req.Message = nil
	req.Generation = &Generation{Profile: "urban", Seed: 42, StartTimeMS: 1794783600000, DurationSeconds: 60, StartLat: 37.5, StartLon: 127, InitialOdometerM: 500000}
	return req
}

func TestAutomaticInvariantsAndTrip(t *testing.T) {
	req, err := normalize(automaticRequest())
	if err != nil {
		t.Fatal(err)
	}
	out, err := process(req)
	if err != nil {
		t.Fatal(err)
	}
	if out.Processed != 96 || out.MicrotripCount != 93 || out.TripCount != 3 || len(out.TripSummaries) != 3 || out.ModelVersion != generatorVersion {
		t.Fatalf("계획·요약 불일치: %+v", out)
	}
	states := map[string]bool{}
	previousDistance, previousSpeed := 0.0, 0.0
	for seq := 0; seq < req.Config.MessagesPerVehicle-1; seq++ {
		m, state := automaticMessage(req, 0, seq)
		p := m["pld"].(map[string]any)
		speed, distance := p["sp"].(float64), p["cdit"].(float64)
		if distance < previousDistance || math.Abs(speed-previousSpeed)/3.6/2 > 2.000001 {
			t.Fatal("거리 역행 또는 가속 한계 초과")
		}
		if m["ts"] != float64(req.Generation.StartTimeMS)+float64(seq*2000) || p["clt"] != m["ts"] {
			t.Fatal("가상 시각 불연속")
		}
		if p["el"].(float64) < 0 || p["el"].(float64) > 100 || p["rpm"].(float64) < 0 {
			t.Fatal("차량 값 범위 이탈")
		}
		states[state] = true
		previousDistance = distance
		previousSpeed = speed
	}
	for _, state := range []string{"웜업·공회전", "가속", "정속", "감속", "정차·공회전", "운행 종료·시동 OFF"} {
		if !states[state] {
			t.Fatal("상태 누락", state)
		}
	}
	p := out.TripSummaries[0].Message["pld"].(map[string]any)
	peak := p["hsts"].(float64)
	if math.Abs(p["dis"].(float64)-.55*60*peak/3.6) > 1e-8 || p["dis"] != previousDistance || p["idt"] != 12.0 || p["fct"] != 12.0 || p["wut"] != 6.0 {
		t.Fatal("속도 적분·Trip 집계 불일치")
	}
	if p["tdis"].(float64) != 500000+previousDistance || p["stt"] != float64(req.Generation.StartTimeMS) || p["edt"] != float64(req.Generation.StartTimeMS+60000) {
		t.Fatal("누적거리·운행 구간 불일치")
	}
	if p["mesp"].(float64) != previousDistance/60*3.6 || p["fc"].(float64) <= 0 {
		t.Fatal("평균속도·연료 집계 실패")
	}
	last, _ := automaticMessage(req, 0, req.Config.MessagesPerVehicle-2)
	if last["pld"].(map[string]any)["rpm"] != 0.0 || last["pld"].(map[string]any)["vv"] != 12.6 {
		t.Fatal("시동 OFF 값 실패")
	}
}

func TestAutomaticReproductionAndReportInterval(t *testing.T) {
	a, _ := normalize(automaticRequest())
	b, _ := normalize(automaticRequest())
	for seq := 0; seq < a.Config.MessagesPerVehicle; seq++ {
		m, _ := automaticMessage(a, 0, seq)
		n, _ := automaticMessage(b, 0, seq)
		if !reflect.DeepEqual(m, n) {
			t.Fatal("seed 재현 실패")
		}
	}
	b.Config.IntervalMS = 1000
	b, _ = normalize(b)
	ma, _ := automaticMessage(a, 0, a.Config.MessagesPerVehicle-1)
	mb, _ := automaticMessage(b, 0, b.Config.MessagesPerVehicle-1)
	if !reflect.DeepEqual(ma, mb) {
		t.Fatal("보고 주기가 Trip 집계를 바꿈")
	}
	b.Generation.Seed++
	changed, _ := automaticMessage(b, 0, 10)
	same, _ := automaticMessage(a, 0, 5)
	if reflect.DeepEqual(changed, same) {
		t.Fatal("seed 편차 없음")
	}
	first, _ := automaticMessage(a, 0, 10)
	second, _ := automaticMessage(a, 1, 10)
	if first["pld"].(map[string]any)["sp"] == second["pld"].(map[string]any)["sp"] {
		t.Fatal("차량별 편차 없음")
	}
}

func TestAutomaticBoundsAndPreview(t *testing.T) {
	for _, mutate := range []func(*Request){
		func(r *Request) { r.Config.IntervalMS = 700 }, func(r *Request) { r.Generation.Profile = "invalid" }, func(r *Request) { r.Generation.StartLat = 90 }, func(r *Request) { r.Config.VehicleCount = 10000 },
	} {
		r := automaticRequest()
		mutate(&r)
		if _, err := process(r); err == nil {
			t.Fatal("자동 생성 범위 검사 누락")
		}
	}
	r := automaticRequest()
	r.Operation = "preview"
	out, err := process(r)
	if err != nil || out.Processed != 0 || out.Planned != 96 || len(out.Samples) == 0 || len(out.TripSummaries) != 3 {
		t.Fatal("자동 미리보기 불일치")
	}
	r = automaticRequest()
	r.Config.IntervalMS = 100
	r.Generation.DurationSeconds = 300
	out, err = process(r)
	if err != nil || len(out.Trace) > 120 || out.Trace[len(out.Trace)-1].ElapsedSeconds != 300 {
		t.Fatal("표시용 기록 상한·종료 누락")
	}
	// JSON 경계에서도 숫자 타입이 유효하게 직렬화되어야 한다.
	if _, err = json.Marshal(out); err != nil {
		t.Fatal(err)
	}
}
