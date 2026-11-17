package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"time"
)

type Config struct {
	VehicleCount       int    `json:"vehicle_count"`
	MappingMode        string `json:"mapping_mode"`
	ClientCount        int    `json:"client_count"`
	MessagesPerVehicle int    `json:"messages_per_vehicle"`
	IntervalMS         int64  `json:"interval_ms"`
	Lifetime           string `json:"connection_lifetime"`
}
type Request struct {
	Operation    string         `json:"operation"`
	RunID        string         `json:"run_id"`
	Config       Config         `json:"config"`
	Message      map[string]any `json:"message"`
	MQTT         *MQTTProfile   `json:"mqtt,omitempty"`
	Generation   *Generation    `json:"generation,omitempty"`
	TraceVehicle int            `json:"trace_vehicle,omitempty"`
	TraceStartMS int64          `json:"trace_start_ms,omitempty"`
	TraceUntilMS int64          `json:"trace_until_ms,omitempty"`
	TraceOffset  int            `json:"trace_offset,omitempty"`
	TraceLimit   int            `json:"trace_limit,omitempty"`
	Realtime     bool           `json:"realtime,omitempty"`
}
type Assignment struct {
	VehicleID string `json:"vehicle_id"`
	ClientID  string `json:"client_id"`
}
type Sample struct {
	VehicleID      string         `json:"vehicle_id"`
	ClientID       string         `json:"client_id"`
	Topic          string         `json:"topic"`
	Sequence       int            `json:"sequence"`
	Message        map[string]any `json:"message"`
	Context        *MotionContext `json:"context,omitempty"`
	EventsObserved int            `json:"events_observed,omitempty"`
	VehicleState   string         `json:"vehicle_state,omitempty"`
}
type Result struct {
	RunID             string           `json:"run_id"`
	Transport         string           `json:"transport"`
	State             string           `json:"state"`
	AllocatedClients  int              `json:"allocated_clients"`
	AssignmentCount   int              `json:"assignment_count"`
	Assignments       []Assignment     `json:"assignments"`
	Planned           int              `json:"planned_messages"`
	Processed         int              `json:"processed_messages"`
	Bytes             int              `json:"payload_bytes"`
	Connected         int              `json:"connected_clients"`
	Published         int              `json:"mqtt_published"`
	Puback            int              `json:"puback_count"`
	Attempted         int              `json:"publish_attempts"`
	Unconfirmed       int              `json:"unconfirmed_messages"`
	Reconnects        int              `json:"reconnect_count"`
	ActiveClients     int              `json:"active_clients"`
	Errors            []string         `json:"errors"`
	Samples           []Sample         `json:"samples"`
	TripSummaries     []Sample         `json:"trip_summaries,omitempty"`
	Trace             []TracePoint     `json:"trace,omitempty"`
	ModelVersion      string           `json:"model_version,omitempty"`
	MicrotripCount    int              `json:"microtrip_count"`
	TripCount         int              `json:"trip_count"`
	ElapsedMS         int64            `json:"elapsed_ms"`
	Remaining         int              `json:"remaining_messages"`
	Unsent            int              `json:"unsent_messages"`
	ScheduleDelayMS   int64            `json:"schedule_delay_ms"`
	PeakActiveClients int              `json:"peak_active_clients"`
	PausedMS          int64            `json:"paused_ms"`
	ActiveElapsedMS   int64            `json:"active_elapsed_ms"`
	SimulationTimeMS  int64            `json:"simulation_time_ms"`
	Scenario          *ScenarioSummary `json:"scenario,omitempty"`
	events            map[int]int
	ExecutionState    string `json:"execution_state"`
}

func process(req Request) (Result, error) { return processContext(context.Background(), req, nil) }

func processContext(ctx context.Context, req Request, progress func(Result)) (Result, error) {
	var err error
	req, err = normalize(req)
	if err != nil {
		return Result{}, err
	}
	c := req.Config
	if c.VehicleCount < 1 || c.VehicleCount > 10000 || c.MessagesPerVehicle < 1 || c.VehicleCount*c.MessagesPerVehicle > 10000 || c.IntervalMS < 1 || c.IntervalMS > 86400000 {
		return Result{}, errors.New("차량 수·생성 건수·주기를 확인하세요. 한 실행은 최대 10,000건입니다.")
	}
	if req.Operation != "preview" && req.Operation != "dry_run" && req.Operation != "plan" && req.Operation != "trace" {
		return Result{}, errors.New("송신 없는 확인만 지원합니다.")
	}
	if c.Lifetime != "always" && c.Lifetime != "while_driving" {
		return Result{}, errors.New("연결 유지 정책을 확인하세요.")
	}
	if req.RunID == "" || len(req.RunID) > 48 {
		return Result{}, errors.New("실행 ID가 올바르지 않습니다.")
	}
	for _, r := range req.RunID {
		if !(r >= 'a' && r <= 'z' || r >= '0' && r <= '9' || r == '-') {
			return Result{}, errors.New("실행 ID에 허용되지 않은 문자가 있습니다.")
		}
	}
	clients := c.ClientCount
	switch c.MappingMode {
	case "dedicated":
		if clients != c.VehicleCount {
			return Result{}, errors.New("차량별 전체 연결은 차량 수와 Client 수가 같아야 합니다.")
		}
	case "shared":
		if clients < 1 || clients > c.VehicleCount {
			return Result{}, errors.New("풀 Client 수는 1 이상 차량 수 이하여야 합니다.")
		}
	default:
		return Result{}, errors.New("배정 방식을 확인하세요.")
	}
	if req.Generation == nil {
		pld, ok := req.Message["pld"].(map[string]any)
		if !ok {
			return Result{}, errors.New("pld 객체가 필요합니다.")
		}
		ty, ok := req.Message["ty"].(float64)
		if !ok || ty != 3 && ty != 4 {
			return Result{}, errors.New("개발용 OBD 타입은 3 또는 4입니다.")
		}
		ts, ok := req.Message["ts"].(float64)
		if !ok || ts < 0 || ts > 9000000000000000 {
			return Result{}, errors.New("ts 시각을 확인하세요.")
		}
		if _, ok := pld["tid"].(float64); !ok {
			return Result{}, errors.New("숫자 tid가 필요합니다.")
		}
		if ty == 4 {
			if _, ok := pld["clt"].(float64); !ok {
				return Result{}, errors.New("Microtrip에 clt가 필요합니다.")
			}
		}
	}
	allocations := make([]Assignment, c.VehicleCount)
	for i := 0; i < c.VehicleCount; i++ {
		slot := i % clients
		allocations[i] = Assignment{fmt.Sprintf("v%06d", i+1), fmt.Sprintf("sim-%s-c%06d", req.RunID, slot+1)}
	}
	limit := len(allocations)
	if limit > 100 {
		limit = 100
	}
	out := Result{RunID: req.RunID, Transport: "dry_run", State: "preview", AllocatedClients: clients, AssignmentCount: c.VehicleCount, Assignments: allocations[:limit], Planned: c.VehicleCount * c.MessagesPerVehicle, Samples: []Sample{}}
	if req.Operation == "trace" {
		if req.Generation == nil || req.TraceVehicle < 1 || req.TraceVehicle > c.VehicleCount || req.TraceStartMS < 0 || req.TraceStartMS > req.TraceUntilMS || req.TraceUntilMS < 0 || req.TraceUntilMS > int64(req.Generation.DurationSeconds)*1000 {
			return Result{}, errors.New("차량·조회 구간을 확인하세요.")
		}
		if req.TraceOffset < 0 || req.TraceOffset > 10000 || req.TraceLimit < 0 || req.TraceLimit > 100 {
			return Result{}, errors.New("조회 페이지 크기와 위치를 확인하세요.")
		}
		out.ModelVersion = modelVersion(req.Generation)
		count := min(c.MessagesPerVehicle-2, int(req.TraceUntilMS/c.IntervalMS))
		first := int((req.TraceStartMS + c.IntervalMS - 1) / c.IntervalMS)
		stride := max(1, (count-first+118)/119)
		index := req.TraceVehicle - 1
		out.Planned = max(0, count-first+1)
		for seq := first + req.TraceOffset; seq <= count && len(out.Samples) < req.TraceLimit; seq++ {
			out.Samples = append(out.Samples, sampleAt(req, index, seq, allocations[index], "sim"))
		}
		for seq := first; seq <= count; seq++ {
			if (seq-first)%stride != 0 && seq != count {
				continue
			}
			sample := sampleAt(req, index, seq, allocations[index], "sim")
			p := sample.Message["pld"].(map[string]any)
			out.Trace = append(out.Trace, TracePoint{ElapsedSeconds: float64(offsetMS(req, seq)) / 1000, State: sample.VehicleState, Speed: p["sp"].(float64), RPM: p["rpm"].(float64), Distance: p["cdit"].(float64), Context: sample.Context})
		}
		return out, nil
	}
	if req.Operation == "plan" || req.Operation == "preview" && req.Generation == nil {
		return out, nil
	}
	if req.Generation != nil {
		out.ModelVersion = modelVersion(req.Generation)
	}
	started := time.Now()
	control := controlFrom(ctx)
	report := reporter(started, progress, control)
	out.State = "running"
	report(&out, true)
	for seq := 0; seq < c.MessagesPerVehicle; seq++ {
		if req.Realtime && req.Operation != "preview" {
			if err := waitSchedule(ctx, started.Add(time.Duration(offsetMS(req, seq))*time.Millisecond), func(force bool) { report(&out, force) }); err != nil {
				out.State = "stopped"
				report(&out, true)
				return out, nil
			}
		}
		for i, a := range allocations {
			if err := control.gate(ctx, func(force bool) { report(&out, force) }); err != nil {
				out.State = "stopped"
				report(&out, true)
				return out, nil
			}
			sample := sampleAt(req, i, seq, a, "sim")
			if req.Realtime {
				out.ScheduleDelayMS = max(out.ScheduleDelayMS, time.Since(started).Milliseconds()-pauseMilliseconds(control)-offsetMS(req, seq))
			}
			if err := recordSample(&out, req, sample, i, seq); err != nil {
				return Result{}, err
			}
			report(&out, false)
		}
	}
	if req.Operation == "preview" {
		out.Processed = 0
		out.Bytes = 0
		out.State = "preview"
		out.Remaining = out.Planned
		return out, nil
	}
	out.State = "dry_run_completed"
	report(&out, true)
	return out, nil
}
func main() {
	reader := bufio.NewReaderSize(os.Stdin, 1048577)
	first, err := reader.ReadSlice('\n')
	if len(first) > 1048576 || (err != nil && err != io.EOF) {
		fail("요청 크기를 확인하세요.")
		return
	}
	// 첫 요청은 한 줄 JSON이다. 기존 EOF 종료 요청도 허용한다.
	dec := json.NewDecoder(io.MultiReader(bytes.NewReader(first), reader))
	dec.DisallowUnknownFields()
	var req Request
	if err := dec.Decode(&req); err != nil {
		fail("요청 JSON을 읽을 수 없습니다.")
		return
	}
	managed := os.Getenv("DANGUBI_MANAGED") == "1"
	ctx, cancel := context.WithCancel(context.Background())
	control := newExecutionControl()
	ctx = context.WithValue(ctx, controlKey{}, control)
	defer cancel()
	var progress func(Result)
	if managed {
		encoder := json.NewEncoder(os.Stdout)
		progress = func(out Result) { encoder.Encode(map[string]any{"kind": "progress", "result": out}) }
		go func() {
			// 제어 API가 사라져 stdin이 닫혀도 실행을 멈춘다.
			for {
				var command struct {
					Command string `json:"command"`
				}
				if err := dec.Decode(&command); err != nil {
					cancel()
					return
				}
				if command.Command == "pause" {
					control.request(true)
				}
				if command.Command == "resume" {
					control.request(false)
				}
				if command.Command == "stop" {
					cancel()
					return
				}
			}
		}()
	} else {
		var extra any
		if err := dec.Decode(&extra); err != io.EOF {
			fail("요청은 JSON 객체 하나여야 합니다.")
			return
		}
	}
	var result Result
	if req.Operation == "mqtt" {
		result, err = runMQTTContext(ctx, req, progress)
	} else {
		result, err = processContext(ctx, req, progress)
	}
	if err != nil {
		fail(err.Error())
		return
	}
	if managed {
		json.NewEncoder(os.Stdout).Encode(map[string]any{"kind": "result", "result": result})
	} else {
		json.NewEncoder(os.Stdout).Encode(result)
	}
}
func fail(message string) {
	json.NewEncoder(os.Stdout).Encode(map[string]string{"error": message})
	os.Exit(1)
}
