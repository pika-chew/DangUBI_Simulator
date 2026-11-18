package main

import (
	"errors"
	"math"
)

type EngineContinuity struct {
	VehicleSeed int64   `json:"vehicle_seed"`
	Coolant     float64 `json:"coolant_c"`
	Heading     float64 `json:"heading_rad"`
}
type ScheduledInput struct {
	AnomalyEligible      bool             `json:"anomaly_eligible"`
	AnomalyEventsPerHour float64          `json:"anomaly_events_per_hour"`
	AnomalyIntensity     string           `json:"anomaly_intensity"`
	StartMS              int64            `json:"start_ms"`
	EndMS                int64            `json:"end_ms"`
	FromMS               int64            `json:"from_ms"`
	UntilMS              int64            `json:"until_ms"`
	IntervalMS           int64            `json:"interval_ms"`
	Offset               int              `json:"offset"`
	Limit                int              `json:"limit"`
	TID                  int64            `json:"tid"`
	Seed                 int64            `json:"seed"`
	Profile              string           `json:"profile"`
	Lat                  float64          `json:"lat"`
	Lon                  float64          `json:"lon"`
	Odometer             float64          `json:"odometer_m"`
	Continuity           EngineContinuity `json:"continuity"`
	ForceEnd             bool             `json:"force_end"`
}
type ScheduledOutput struct {
	EventsUntilQueryEnd int              `json:"events_until_query_end"`
	Model               string           `json:"model"`
	MicroCount          int64            `json:"microtrip_count"`
	Messages            []map[string]any `json:"messages"`
	Trace               []TracePoint     `json:"trace"`
	Trip                map[string]any   `json:"trip,omitempty"`
	Synthetic           bool             `json:"synthetic_trip"`
	FinalLat            float64          `json:"final_lat"`
	FinalLon            float64          `json:"final_lon"`
	FinalOdometer       float64          `json:"final_odometer_m"`
	FinalCoolant        float64          `json:"final_coolant_c"`
	FinalHeading        float64          `json:"final_heading_rad"`
}

func scheduledOBD(input Request) (Result, error) {
	s := input.Scheduled
	if s == nil {
		return Result{}, errors.New("일정 운행 입력이 필요합니다.")
	}
	span := s.EndMS - s.StartMS
	finite := func(v float64) bool { return !math.IsNaN(v) && !math.IsInf(v, 0) }
	if s.StartMS < 0 || s.EndMS > 4102444800000 || span < 1000 || span > 86400000 || span%1000 != 0 || s.IntervalMS < 100 || s.IntervalMS > 86400000 || s.FromMS < s.StartMS || s.UntilMS > s.EndMS || s.FromMS > s.UntilMS || s.Offset < 0 || s.Offset > 1000000 || s.Limit < 1 || s.Limit > 100 || s.TID < 1 || s.TID > 9007199254740991 || !finite(s.Lat) || !finite(s.Lon) || !finite(s.Odometer) || !finite(s.Continuity.Coolant) || !finite(s.Continuity.Heading) || math.Abs(s.Lat) > 85 || math.Abs(s.Lon) > 180 || s.Odometer < 0 || s.Odometer > 1e12 || s.Continuity.Coolant < -40 || s.Continuity.Coolant > 130 {
		return Result{}, errors.New("운행 시각·상태·페이지 범위를 확인하세요.")
	}
	if s.Profile != "urban" && s.Profile != "highway" && s.Profile != "congested" && s.Profile != "mixed" {
		return Result{}, errors.New("도로 프로필을 확인하세요.")
	}
	if !finite(s.AnomalyEventsPerHour) || s.AnomalyEventsPerHour < 0 || s.AnomalyEventsPerHour > 60 || (s.AnomalyIntensity != "" && s.AnomalyIntensity != "low" && s.AnomalyIntensity != "medium" && s.AnomalyIntensity != "high") {
		return Result{}, errors.New("이상 이벤트 빈도·강도를 확인하세요.")
	}
	n := int((span + s.IntervalMS - 1) / s.IntervalMS)
	r := Request{Config: Config{VehicleCount: 1, ClientCount: 1, MessagesPerVehicle: n + 1, IntervalMS: s.IntervalMS}, Generation: &Generation{ModelVersion: journeyVersion, Seed: s.Seed, Profile: s.Profile, StartTimeMS: s.StartMS, DurationSeconds: int(span / 1000), StartLat: s.Lat, StartLon: s.Lon, InitialOdometerM: s.Odometer, Weather: "dry", Continuity: &s.Continuity}}
	if s.AnomalyEligible {
		r.Generation.AnomalyVehiclePercent = 100
	}
	r.Generation.AnomalyEventsPerHour = s.AnomalyEventsPerHour
	r.Generation.AnomalyIntensity = s.AnomalyIntensity
	message := func(seq int) map[string]any {
		m, _ := automaticMessage(r, 0, seq)
		m["pld"].(map[string]any)["tid"] = float64(s.TID)
		return m
	}
	// 기존 helper의 마지막 순번은Trip이며 종료Microtrip은 새 조회에서 요청하지 않는다.
	fullTrip := message(n)
	first := max(int64(0), (s.FromMS-s.StartMS+s.IntervalMS-1)/s.IntervalMS)
	last := min(int64(n-1), (s.UntilMS-s.StartMS)/s.IntervalMS)
	if s.ForceEnd && s.UntilMS < s.EndMS {
		last = min(last, (s.UntilMS-s.StartMS-1)/s.IntervalMS)
		if s.UntilMS == s.StartMS {
			last = -1
		}
	}
	out := &ScheduledOutput{Model: "schedule-obd-3/journey-8", MicroCount: max(int64(0), last-first+1), Messages: []map[string]any{}, Trace: []TracePoint{}}
	_, _, _, out.EventsUntilQueryEnd = scenarioAt(r, 0, float64(s.UntilMS-s.StartMS)/1000)
	for seq := first + int64(s.Offset); seq <= last && len(out.Messages) < s.Limit; seq++ {
		out.Messages = append(out.Messages, message(int(seq)))
	}
	stride := max(int64(1), (last-first+120)/120)
	for seq := first; seq <= last; seq += stride {
		m := message(int(seq))
		p := m["pld"].(map[string]any)
		t := float64(seq*s.IntervalMS) / 1000
		_, label := automaticMessage(r, 0, int(seq))
		_, _, context, _ := scenarioAt(r, 0, t)
		addEngineContext(r, 0, t, &context)
		out.Trace = append(out.Trace, TracePoint{ElapsedSeconds: t, State: label, Context: &context, Speed: p["sp"].(float64), RPM: p["rpm"].(float64), Distance: p["cdit"].(float64)})
	}
	if s.UntilMS == s.EndMS {
		out.Trip = fullTrip
	} else if s.ForceEnd {
		t := float64(s.UntilMS-s.StartMS) / 1000
		totals, _, _, _ := scenarioAt(r, 0, t)
		engine := engineAt(r, 0, t)
		out.Trip = message(n)
		p := out.Trip["pld"].(map[string]any)
		lat := s.Lat + totals.Y/6371000*180/math.Pi
		lon := math.Mod(s.Lon+totals.X/(6371000*math.Cos(s.Lat*math.Pi/180))*180/math.Pi+540, 360) - 180
		out.Trip["ts"] = float64(s.UntilMS)
		p["edt"] = float64(s.UntilMS)
		p["dis"] = totals.Distance
		p["tdis"] = s.Odometer + totals.Distance
		p["fc"] = engine.Fuel
		p["coe"] = engine.Fuel * 2310
		p["idt"] = totals.Idle
		p["fct"] = engine.CutSeconds
		p["wut"] = engine.WarmSeconds
		p["ctp"] = engine.TemperatureIntegral / max(1, t)
		p["mesp"] = totals.Distance / max(1, t) * 3.6
		p["hsts"] = totals.Peak * 3.6
		p["dtvt"] = max(0, t-totals.Idle)
		p["edlat"] = lat
		p["edlon"] = lon
		out.Synthetic = true
	}
	p := fullTrip["pld"].(map[string]any)
	end := engineAt(r, 0, float64(span)/1000)
	totals, _, _, _ := scenarioAt(r, 0, float64(span)/1000)
	out.FinalLat = p["edlat"].(float64)
	out.FinalLon = p["edlon"].(float64)
	out.FinalOdometer = p["tdis"].(float64)
	out.FinalCoolant = end.Coolant
	out.FinalHeading = totals.Heading
	return Result{Scheduled: out}, nil
}
