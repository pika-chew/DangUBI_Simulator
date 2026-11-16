package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
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
	Operation  string         `json:"operation"`
	RunID      string         `json:"run_id"`
	Config     Config         `json:"config"`
	Message    map[string]any `json:"message"`
	MQTT       *MQTTProfile   `json:"mqtt,omitempty"`
	Generation *Generation    `json:"generation,omitempty"`
}
type Assignment struct {
	VehicleID string `json:"vehicle_id"`
	ClientID  string `json:"client_id"`
}
type Sample struct {
	VehicleID    string         `json:"vehicle_id"`
	ClientID     string         `json:"client_id"`
	Topic        string         `json:"topic"`
	Sequence     int            `json:"sequence"`
	Message      map[string]any `json:"message"`
	VehicleState string         `json:"vehicle_state,omitempty"`
}
type Result struct {
	RunID            string       `json:"run_id"`
	Transport        string       `json:"transport"`
	State            string       `json:"state"`
	AllocatedClients int          `json:"allocated_clients"`
	AssignmentCount  int          `json:"assignment_count"`
	Assignments      []Assignment `json:"assignments"`
	Planned          int          `json:"planned_messages"`
	Processed        int          `json:"processed_messages"`
	Bytes            int          `json:"payload_bytes"`
	Connected        int          `json:"connected_clients"`
	Published        int          `json:"mqtt_published"`
	Puback           int          `json:"puback_count"`
	Attempted        int          `json:"publish_attempts"`
	Unconfirmed      int          `json:"unconfirmed_messages"`
	Reconnects       int          `json:"reconnect_count"`
	ActiveClients    int          `json:"active_clients"`
	Errors           []string     `json:"errors"`
	Samples          []Sample     `json:"samples"`
	TripSummaries    []Sample     `json:"trip_summaries,omitempty"`
	Trace            []TracePoint `json:"trace,omitempty"`
	ModelVersion     string       `json:"model_version,omitempty"`
	MicrotripCount   int          `json:"microtrip_count"`
	TripCount        int          `json:"trip_count"`
}

func process(req Request) (Result, error) {
	var err error
	req, err = normalize(req)
	if err != nil {
		return Result{}, err
	}
	c := req.Config
	if c.VehicleCount < 1 || c.VehicleCount > 10000 || c.MessagesPerVehicle < 1 || c.VehicleCount*c.MessagesPerVehicle > 10000 || c.IntervalMS < 1 || c.IntervalMS > 86400000 {
		return Result{}, errors.New("차량 수·생성 건수·주기를 확인하세요. 한 실행은 최대 10,000건입니다.")
	}
	if req.Operation != "preview" && req.Operation != "dry_run" {
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
	if req.Operation == "preview" && req.Generation == nil {
		return out, nil
	}
	if req.Generation != nil {
		out.ModelVersion = generatorVersion
	}
	for seq := 0; seq < c.MessagesPerVehicle; seq++ {
		for i, a := range allocations {
			sample := sampleAt(req, i, seq, a, "sim")
			encoded, err := json.Marshal(sample.Message)
			if err != nil {
				return Result{}, err
			}
			if len(encoded) > 131072 {
				return Result{}, errors.New("메시지 크기가 128KiB를 초과합니다.")
			}
			out.Processed++
			out.Bytes += len(encoded)
			if sample.Message["ty"].(float64) == 3 {
				out.TripCount++
				if req.Generation != nil && len(out.TripSummaries) < 100 {
					out.TripSummaries = append(out.TripSummaries, sample)
				}
			} else {
				out.MicrotripCount++
			}
			if len(out.Samples) < 10 {
				out.Samples = append(out.Samples, sample)
			}
			if req.Generation != nil && i == 0 && seq < c.MessagesPerVehicle-1 && (seq%max(1, (c.MessagesPerVehicle-2+118)/119) == 0 || seq == c.MessagesPerVehicle-2) {
				p := sample.Message["pld"].(map[string]any)
				out.Trace = append(out.Trace, TracePoint{float64(offsetMS(req, seq)) / 1000, sample.VehicleState, p["sp"].(float64), p["rpm"].(float64), p["cdit"].(float64)})
			}
		}
	}
	if req.Operation == "preview" {
		out.Processed = 0
		out.Bytes = 0
		return out, nil
	}
	out.State = "dry_run_completed"
	return out, nil
}
func main() {
	dec := json.NewDecoder(io.LimitReader(os.Stdin, 1048577))
	dec.DisallowUnknownFields()
	var req Request
	if err := dec.Decode(&req); err != nil {
		fail("요청 JSON을 읽을 수 없습니다.")
		return
	}
	var extra any
	if err := dec.Decode(&extra); err != io.EOF {
		fail("요청은 JSON 객체 하나여야 합니다.")
		return
	}
	var result Result
	var err error
	if req.Operation == "mqtt" {
		result, err = runMQTT(req)
	} else {
		result, err = process(req)
	}
	if err != nil {
		fail(err.Error())
		return
	}
	json.NewEncoder(os.Stdout).Encode(result)
}
func fail(message string) {
	json.NewEncoder(os.Stdout).Encode(map[string]string{"error": message})
	os.Exit(1)
}
