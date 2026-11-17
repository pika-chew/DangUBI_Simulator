package main

import (
	"math"
	"reflect"
	"testing"
)

func powertrainRequest(duration int, start string) Request {
	r := koreaRequest(duration, "dry")
	r.Generation.ModelVersion = powertrainVersion
	r.Generation.EngineStart = start
	r, _ = normalize(r)
	return r
}
func engineTestTrip(start string) (Request, *scenarioPlan) {
	r := powertrainRequest(150, start)
	segments := []motionSegment{
		{Start: 0, End: 10}, {Start: 10, End: 50, V0: 0, V1: 20},
		{Start: 50, End: 110, V0: 20, V1: 20}, {Start: 110, End: 130, V0: 20, V1: 0},
		{Start: 130, End: 150},
	}
	total := motionTotals{}
	for i := range segments {
		segments[i].Road = roadContext{Name: "도심 도로", Limit: 100}
		segments[i].Before = total
		total = advance(segments[i], segments[i].End-segments[i].Start)
	}
	p := &scenarioPlan{Segments: segments, Duration: 150, Warmup: 10, Style: "보통", Weather: "건조"}
	r.Generation.cache.plans[0] = p
	return r, p
}
func TestEngineGearFuelCutAndStop(t *testing.T) {
	warm, p := engineTestTrip("warm")
	cold, _ := engineTestTrip("cold")
	warmFinal, coldFinal := engineAt(warm, 0, 150), engineAt(cold, 0, 150)
	if warmFinal.CutSeconds <= 0 || coldFinal.CutSeconds != 0 || warmFinal.Fuel >= coldFinal.Fuel {
		t.Fatal("온도별 연료 차단·연료 보정", warmFinal, coldFinal)
	}
	if warmFinal.WarmSeconds != 0 || coldFinal.WarmSeconds <= 0 {
		t.Fatal("워밍업 정의 불일치")
	}
	shifted, rpmDrop := 0, false
	last := engineAt(warm, 0, 0)
	for tm := engineTick; tm <= 150; tm += engineTick {
		current := engineAt(warm, 0, tm)
		near(t, current.Fuel-last.Fuel, last.FuelRate*engineTick/3600)
		if last.FuelCut {
			near(t, current.Fuel, last.Fuel)
		}
		if current.Gear > last.Gear {
			shifted++
			if current.RPM < last.RPM {
				rpmDrop = true
			}
		}
		if tm < 150 && (current.RPM < 650 || current.RPM > 6000 || current.Load < 0 || current.Load > 100 || current.Throttle < 0 || current.Throttle > 95 || current.FuelRate < 0) {
			t.Fatal("엔진 값 범위", current)
		}
		last = current
	}
	if shifted < 3 || !rpmDrop {
		t.Fatal("기어 상승/RPM 하강이 없음", shifted)
	}
	idle := engineAt(warm, 0, 145)
	if idle.RPM <= 0 || idle.FuelRate <= 0 || idle.FuelCut || idle.Voltage <= getEnginePlan(warm, 0).Spec.RestVolts {
		t.Fatal("시동 유지 정차", idle)
	}
	if warmFinal.RPM != 0 || warmFinal.FuelRate != 0 || warmFinal.Gear != 0 {
		t.Fatal("종료 시동 OFF")
	}
	if len(p.Engine.States) > 17 {
		t.Fatal("전체 엔진 시계열을 보관함")
	}
	at := engineAt(warm, 0, 53.25)
	again := engineAt(warm, 0, 53.25)
	if at != again {
		t.Fatal("부분 조회 재현 실패")
	}
}
func TestEngineDemandAndGearCoupling(t *testing.T) {
	g := powertrainRequest(60, "warm").Generation
	spec := vehicleEngineSpec(g, 0)
	makePoint := func(accel, grade float64, gear int) engineState {
		p := &scenarioPlan{Duration: 10, Segments: []motionSegment{{Start: 0, End: 10, V0: 12, V1: 12 + accel*10, Grade: grade}}}
		return setEnginePoint(p, spec, engineState{Gear: gear, Coolant: 85}, 0)
	}
	cruise := makePoint(0, 0, 3)
	accelerate := makePoint(1, 0, 3)
	uphill := makePoint(0, .04, 3)
	if accelerate.Load <= cruise.Load || accelerate.FuelRate <= cruise.FuelRate || accelerate.Throttle <= cruise.Throttle || uphill.FuelRate <= cruise.FuelRate {
		t.Fatal("부하·연료 연관성", cruise, accelerate, uphill)
	}
	lowGear, highGear := makePoint(0, 0, 2), makePoint(0, 0, 4)
	if lowGear.RPM <= highGear.RPM {
		t.Fatal("동일 속도에서 기어에 따른 RPM 차이가 없음")
	}
	limited := limitPoweredEnd(spec, 20, 30, 3, .02)
	if limited >= 30 || roadPower(spec, limited, (limited-20)/3, .02) > (spec.RatedKW-1.2)*.9+1e-7 {
		t.Fatal("출력 상한", limited)
	}
	if spec != vehicleEngineSpec(g, 0) || spec == vehicleEngineSpec(g, 1) {
		t.Fatal("차량 특성 seed 고정/차량 편차")
	}
}
func TestPowertrainReportIntervalTripAndSchema(t *testing.T) {
	r := powertrainRequest(600, "warm")
	a, err := process(r)
	if err != nil {
		t.Fatal(err)
	}
	for i, trip := range a.TripSummaries {
		final := engineAt(r, i, 600)
		pld := trip.Message["pld"].(map[string]any)
		near(t, pld["fc"].(float64), final.Fuel)
		near(t, pld["fct"].(float64), final.CutSeconds)
		near(t, pld["ctp"].(float64), final.TemperatureIntegral/600)
		last := sampleAt(r, i, r.Config.MessagesPerVehicle-2, Assignment{VehicleID: trip.VehicleID}, "sim")
		near(t, last.Message["pld"].(map[string]any)["fc"].(float64), pld["fc"].(float64))
		if len(trip.Message) != 3 || pld["gear"] != nil || pld["engine_state"] != nil {
			t.Fatal("엔진 내부 정보가 OBD에 포함됨")
		}
	}
	r.Config.IntervalMS = 4000
	r, _ = normalize(r)
	b, err := process(r)
	if err != nil || !reflect.DeepEqual(a.TripSummaries[0].Message, b.TripSummaries[0].Message) {
		t.Fatal("보고 주기가 Trip/연료를 변경", err)
	}
	r.Generation.EngineStart = "unknown"
	if _, err := process(r); err == nil {
		t.Fatal("시작 상태 검증 누락")
	}
	r.Generation.EngineStart = "cold"
	r.Generation.ModelVersion = koreaVersion
	if _, err := process(r); err == nil {
		t.Fatal("구모델에 새 엔진 상태 허용")
	}
}
func TestPowertrainRainSpeedAndOutputBounds(t *testing.T) {
	for _, weather := range []string{"dry", "rain"} {
		r := powertrainRequest(3600, "cold")
		r.Generation.Weather = weather
		r.Generation.AnomalyVehiclePercent = 100
		r.Generation.AnomalyEventsPerHour = 60
		r, _ = normalize(r)
		for i := 0; i < 3; i++ {
			p := getScenario(r, i)
			spec := vehicleEngineSpec(r.Generation, i)
			for _, s := range p.Segments {
				if math.Max(s.V0, s.V1)*3.6 > s.Road.Limit+1e-5 {
					t.Fatal("도로 상한 위반")
				}
				a := (s.V1 - s.V0) / (s.End - s.Start)
				if a > 0 && roadPower(spec, s.V1, a, s.Grade) > (spec.RatedKW-1.2)*.9+1e-5 {
					t.Fatal("출력 상한 위반")
				}
			}
			lastFuel := 0.0
			for tm := 0.0; tm <= 3600; tm += 7.25 {
				s := engineAt(r, i, tm)
				if s.Fuel < lastFuel {
					t.Fatal("누적 연료 역행")
				}
				lastFuel = s.Fuel
			}
		}
	}
}
