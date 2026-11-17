package main

import (
	"errors"
	"fmt"
	"math"
	"math/rand"
	"sort"
	"strings"
)

type TimeBin struct {
	Label   string  `json:"label"`
	Seconds float64 `json:"seconds"`
	Percent float64 `json:"percent"`
	Count   int     `json:"count"`
}
type RoadExposure struct {
	Name    string  `json:"name"`
	Seconds float64 `json:"seconds"`
	Percent float64 `json:"percent"`
	Entries int     `json:"entries"`
	Longest float64 `json:"longest_seconds"`
}
type DriveStats struct {
	VehicleSeconds    float64        `json:"vehicle_seconds"`
	Distance          float64        `json:"distance_m"`
	Average           float64        `json:"average_speed_kmh"`
	MovingAverage     float64        `json:"moving_average_speed_kmh"`
	Peak              float64        `json:"peak_speed_kmh"`
	Idle              float64        `json:"idle_seconds"`
	IdlePercent       float64        `json:"idle_percent"`
	Stops             int            `json:"stop_episodes"`
	LongestStop       float64        `json:"longest_stop_seconds"`
	Speed             []TimeBin      `json:"speed_bins"`
	Acceleration      []TimeBin      `json:"acceleration_bins"`
	StopDurations     []TimeBin      `json:"stop_duration_bins"`
	Roads             []RoadExposure `json:"roads"`
	Traffic           []TimeBin      `json:"traffic_labels"`
	Events            map[string]int `json:"events"`
	EventCount        int            `json:"event_count"`
	EventRate         float64        `json:"events_per_vehicle_hour"`
	EligibleSeconds   float64        `json:"eligible_vehicle_seconds"`
	EligibleEventRate float64        `json:"events_per_eligible_hour"`
	AnalyzedVehicles  int            `json:"analyzed_vehicles"`
	EligibleVehicles  int            `json:"eligible_vehicles"`
	EventVehicles     int            `json:"event_vehicles"`
}
type VehicleDriveStats struct {
	VehicleID   string  `json:"vehicle_id"`
	Start       float64 `json:"start_seconds"`
	End         float64 `json:"end_seconds"`
	Seconds     float64 `json:"vehicle_seconds"`
	Average     float64 `json:"average_speed_kmh"`
	IdlePercent float64 `json:"idle_percent"`
	Stops       int     `json:"stop_episodes"`
	Events      int     `json:"event_count"`
	Eligible    bool    `json:"eligible"`
}
type AnalysisReport struct {
	Version            string              `json:"analysis_version"`
	Model              string              `json:"model_version"`
	Processed          int                 `json:"processed_snapshot"`
	ConfiguredVehicles int                 `json:"configured_vehicles"`
	TargetEligible     int                 `json:"target_eligible_vehicles"`
	TargetFrequency    float64             `json:"configured_events_per_hour"`
	Summary            DriveStats          `json:"summary"`
	Vehicles           []VehicleDriveStats `json:"vehicles"`
	Offset             int                 `json:"vehicle_offset"`
	Total              int                 `json:"vehicle_total"`
}

var speedEdges = []float64{0, 20 / 3.6, 40 / 3.6, 60 / 3.6, 80 / 3.6, 100 / 3.6, math.Inf(1)}
var accelEdges = []float64{math.Inf(-1), -3, -1.5, -.05, .05, 1.5, 3, math.Inf(1)}

func bins(labels ...string) []TimeBin {
	out := make([]TimeBin, len(labels))
	for i, label := range labels {
		out[i].Label = label
	}
	return out
}
func emptyStats() DriveStats {
	return DriveStats{Speed: bins("정차 (0)", "0 초과~20 미만", "20~40 미만", "40~60 미만", "60~80 미만", "80~100 미만", "100 이상"), Acceleration: bins("-3 미만", "-3~-1.5 미만", "-1.5~-0.05 미만", "-0.05~0.05 미만", "0.05~1.5 미만", "1.5~3 미만", "3 이상"), StopDurations: bins("10초 미만", "10~30초 미만", "30~60초 미만", "60초 이상"), Traffic: bins("혼잡·정체 표시", "앞차 표시", "기타 표시"), Roads: []RoadExposure{}, Events: map[string]int{}}
}
func finishStats(s *DriveStats) {
	if s.VehicleSeconds > 0 {
		s.Average = s.Distance / s.VehicleSeconds * 3.6
		s.IdlePercent = s.Idle / s.VehicleSeconds * 100
		s.EventRate = float64(s.EventCount) * 3600 / s.VehicleSeconds
		for _, set := range [][]TimeBin{s.Speed, s.Acceleration, s.Traffic} {
			for i := range set {
				set[i].Percent = set[i].Seconds / s.VehicleSeconds * 100
			}
		}
		for i := range s.Roads {
			s.Roads[i].Percent = s.Roads[i].Seconds / s.VehicleSeconds * 100
		}
	}
	if s.VehicleSeconds-s.Idle > 1e-8 {
		s.MovingAverage = s.Distance / (s.VehicleSeconds - s.Idle) * 3.6
	}
	if s.EligibleSeconds > 0 {
		s.EligibleEventRate = float64(s.EventCount) * 3600 / s.EligibleSeconds
	}
	for i := range s.StopDurations {
		if s.Stops > 0 {
			s.StopDurations[i].Percent = float64(s.StopDurations[i].Count) / float64(s.Stops) * 100
		}
	}
}

// 선형 속도 구간에서 각 속도대의 체류시간을 직접 적분한다.
func speedBandTime(v0, v1, dt, lo, hi float64) float64 {
	if math.Abs(v1-v0) < 1e-12 {
		if v0 >= lo && v0 < hi {
			return dt
		}
		return 0
	}
	a := (v1 - v0) / dt
	x, y := (lo-v0)/a, (hi-v0)/a
	if x > y {
		x, y = y, x
	}
	return max(0, min(dt, y)-max(0, x))
}
func summarizeSegments(segments []motionSegment, start, end float64, eligible bool) DriveStats {
	out := emptyStats()
	roads := map[string]*RoadExposure{}
	stop, roadRun := 0.0, 0.0
	previousRoad := ""
	flushStop := func() {
		if stop <= 0 {
			return
		}
		out.Stops++
		out.LongestStop = max(out.LongestStop, stop)
		j := 3
		if stop < 10 {
			j = 0
		} else if stop < 30 {
			j = 1
		} else if stop < 60 {
			j = 2
		}
		out.StopDurations[j].Count++
		out.StopDurations[j].Seconds += stop
		stop = 0
	}
	for _, s := range segments {
		left, right := max(start, s.Start), min(end, s.End)
		if right <= left {
			continue
		}
		dt := right - left
		a := (s.V1 - s.V0) / (s.End - s.Start)
		v0, v1 := s.V0+a*(left-s.Start), s.V0+a*(right-s.Start)
		out.VehicleSeconds += dt
		out.Distance += (v0 + v1) * dt / 2
		out.Peak = max(out.Peak, v0*3.6, v1*3.6)
		if s.V0 == 0 && s.V1 == 0 {
			out.Idle += dt
			out.Speed[0].Seconds += dt
			stop += dt
		} else {
			flushStop()
			for i := 0; i < len(speedEdges)-1; i++ {
				out.Speed[i+1].Seconds += speedBandTime(v0, v1, dt, speedEdges[i], speedEdges[i+1])
			}
		}
		binAcceleration := math.Round(a*1e9) / 1e9 // 경계의 부동소수점 잡음만 정리한다.
		for i := 0; i < len(accelEdges)-1; i++ {
			if binAcceleration >= accelEdges[i] && binAcceleration < accelEdges[i+1] {
				out.Acceleration[i].Seconds += dt
				break
			}
		}
		name := s.Road.Name
		if roads[name] == nil {
			roads[name] = &RoadExposure{Name: name}
		}
		road := roads[name]
		road.Seconds += dt
		if name != previousRoad {
			road.Entries++
			roadRun = 0
		}
		roadRun += dt
		road.Longest = max(road.Longest, roadRun)
		previousRoad = name
		traffic := 2
		if strings.Contains(s.Reason, "혼잡") || strings.Contains(s.Reason, "정체") {
			traffic = 0
		} else if strings.Contains(s.Reason, "앞차") {
			traffic = 1
		}
		out.Traffic[traffic].Seconds += dt
		onset := s.Start + min(1, s.End-s.Start)
		if s.Event != "" && onset > start && onset <= end {
			out.Events[s.Event]++
			out.EventCount++
		}
	}
	flushStop()
	names := make([]string, 0, len(roads))
	for name := range roads {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		out.Roads = append(out.Roads, *roads[name])
	}
	if out.VehicleSeconds > 0 {
		out.AnalyzedVehicles = 1
		if eligible {
			out.EligibleVehicles = 1
			out.EligibleSeconds = out.VehicleSeconds
		}
		if out.EventCount > 0 {
			out.EventVehicles = 1
		}
	}
	finishStats(&out)
	return out
}
func mergeStats(total *DriveStats, s DriveStats) {
	total.VehicleSeconds += s.VehicleSeconds
	total.Distance += s.Distance
	total.Peak = max(total.Peak, s.Peak)
	total.Idle += s.Idle
	total.Stops += s.Stops
	total.LongestStop = max(total.LongestStop, s.LongestStop)
	total.EligibleSeconds += s.EligibleSeconds
	total.AnalyzedVehicles += s.AnalyzedVehicles
	total.EligibleVehicles += s.EligibleVehicles
	total.EventVehicles += s.EventVehicles
	total.EventCount += s.EventCount
	for event, n := range s.Events {
		total.Events[event] += n
	}
	for _, pair := range [][2][]TimeBin{{total.Speed, s.Speed}, {total.Acceleration, s.Acceleration}, {total.StopDurations, s.StopDurations}, {total.Traffic, s.Traffic}} {
		for i := range pair[0] {
			pair[0][i].Seconds += pair[1][i].Seconds
			pair[0][i].Count += pair[1][i].Count
		}
	}
	for _, r := range s.Roads {
		found := false
		for i := range total.Roads {
			if total.Roads[i].Name == r.Name {
				total.Roads[i].Seconds += r.Seconds
				total.Roads[i].Entries += r.Entries
				total.Roads[i].Longest = max(total.Roads[i].Longest, r.Longest)
				found = true
				break
			}
		}
		if !found {
			total.Roads = append(total.Roads, r)
		}
	}
}
func analysisSegments(req Request, index int) ([]motionSegment, bool) {
	if modelVersion(req.Generation) != generatorVersion {
		p := getScenario(req, index)
		return p.Segments, p.Eligible
	}
	// v1 생성식을 바꾸지 않고 같은 seed와 속도 변화 지점을 분석한다.
	g := req.Generation
	duration := float64(g.DurationSeconds)
	target := map[string]float64{"urban": 50, "highway": 95, "congested": 20}[g.Profile]
	variation := .9 + rand.New(rand.NewSource(g.Seed+int64(index)*1000003)).Float64()*.2
	peak := min(target, .72*duration/1.1) * variation / 3.6
	out := []motionSegment{}
	for j := range phaseNames {
		out = append(out, motionSegment{Start: phaseTimes[j] * duration, End: phaseTimes[j+1] * duration, V0: peak * phaseSpeeds[j], V1: peak * phaseSpeeds[j+1], Road: roadContext{Name: "이전 모델 · 도로 구분 없음"}, Reason: phaseNames[j]})
	}
	return out, false
}
func analyzeDrive(req auditRequest) (*AnalysisReport, error) {
	if req.Generation == nil || req.AnalysisVehicle < 0 || req.AnalysisVehicle > req.Config.VehicleCount || req.AnalysisProcessed < 0 || req.AnalysisProcessed > req.Config.VehicleCount*req.Config.MessagesPerVehicle || req.AnalysisOffset < 0 || req.AnalysisOffset > 10000 || req.TraceStartMS < 0 || req.TraceUntilMS < req.TraceStartMS || req.TraceUntilMS > int64(req.Generation.DurationSeconds)*1000 {
		return nil, errors.New("운행 분석의 차량·생성 범위·페이지를 확인하세요.")
	}
	report := &AnalysisReport{Version: "drive-analysis-1", Model: modelVersion(req.Generation), Processed: req.AnalysisProcessed, ConfiguredVehicles: req.Config.VehicleCount, TargetFrequency: req.Generation.AnomalyEventsPerHour, Summary: emptyStats(), Vehicles: []VehicleDriveStats{}, Offset: req.AnalysisOffset}
	if report.Model != generatorVersion {
		report.TargetEligible = int(math.Round(float64(req.Config.VehicleCount) * req.Generation.AnomalyVehiclePercent / 100))
	}
	for i := 0; i < req.Config.VehicleCount; i++ {
		if req.AnalysisVehicle != 0 && req.AnalysisVehicle != i+1 {
			continue
		}
		n := req.AnalysisProcessed - (i + 1)
		if n < 0 {
			if req.AnalysisVehicle != 0 && req.TraceUntilMS > 0 {
				return nil, errors.New("아직 생성되지 않은 운행은 분석할 수 없습니다.")
			}
			continue
		}
		last := n / req.Config.VehicleCount
		end := min(float64(req.TraceUntilMS)/1000, float64(last)*float64(req.Config.IntervalMS)/1000, float64(req.Generation.DurationSeconds))
		start := float64(req.TraceStartMS) / 1000
		if req.AnalysisVehicle != 0 && end < float64(req.TraceUntilMS)/1000 {
			return nil, errors.New("아직 생성되지 않은 운행은 분석할 수 없습니다.")
		}
		if end <= start {
			continue
		}
		segments, eligible := analysisSegments(req.Request, i)
		s := summarizeSegments(segments, start, end, eligible)
		if req.Generation.cache != nil {
			delete(req.Generation.cache.plans, i)
		}
		mergeStats(&report.Summary, s)
		if report.Total >= req.AnalysisOffset && len(report.Vehicles) < 50 {
			report.Vehicles = append(report.Vehicles, VehicleDriveStats{fmt.Sprintf("v%06d", i+1), start, end, s.VehicleSeconds, s.Average, s.IdlePercent, s.Stops, s.EventCount, eligible})
		}
		report.Total++
	}
	sort.Slice(report.Summary.Roads, func(i, j int) bool { return report.Summary.Roads[i].Name < report.Summary.Roads[j].Name })
	finishStats(&report.Summary)
	return report, nil
}

// 아래 집계는 개발 시험 전용이다. 실행 Worker나 공개 API에는 포함하지 않는다.
type auditRequest struct {
	Request
	AnalysisVehicle   int
	AnalysisProcessed int
	AnalysisOffset    int
}
type auditResult struct{ Analysis *AnalysisReport }

func normalizeAudit(req auditRequest) (auditRequest, error) {
	normalized, err := normalize(req.Request)
	req.Request = normalized
	return req, err
}
func processAudit(req auditRequest) (auditResult, error) {
	req, err := normalizeAudit(req)
	if err != nil {
		return auditResult{}, err
	}
	report, err := analyzeDrive(req)
	return auditResult{report}, err
}
