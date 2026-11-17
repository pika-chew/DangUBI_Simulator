package main

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"math"
	"time"
)

// 내부 집계는 매 응답마다 유지하되 화면용 전체 JSON은 상태 전환/250ms마다 보낸다.
// 최종 result는 main에서 별도로 항상 출력한다.
func managedProgress(w io.Writer, now func() time.Time) func(Result) {
	encoder := json.NewEncoder(w)
	var last time.Time
	var state, execution string
	return func(out Result) {
		t := now()
		if last.IsZero() || out.State != state || out.ExecutionState != execution || t.Sub(last) >= 250*time.Millisecond {
			encoder.Encode(map[string]any{"kind": "progress", "result": out})
			last, state, execution = t, out.State, out.ExecutionState
		}
	}
}

func sleepReporting(ctx context.Context, target time.Time, tick func()) error {
	timer := time.NewTimer(max(0, time.Until(target)))
	defer timer.Stop()
	ticker := time.NewTicker(250 * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-timer.C:
			return ctx.Err()
		case <-ticker.C:
			tick()
		}
	}
}

func recordSample(out *Result, req Request, sample Sample, index, seq int) error {
	data, err := json.Marshal(sample.Message)
	if err != nil {
		return err
	}
	if len(data) > 131072 {
		return errors.New("메시지 크기가 128KiB를 초과합니다.")
	}
	if sample.Context != nil {
		if out.Scenario == nil {
			out.Scenario = &ScenarioSummary{EligibleVehicles: int(math.Round(float64(req.Config.VehicleCount) * req.Generation.AnomalyVehiclePercent / 100))}
			out.events = map[int]int{}
		}
		before := out.events[index]
		if sample.EventsObserved > before {
			out.Scenario.Events += sample.EventsObserved - before
			if before == 0 {
				out.Scenario.ObservedVehicles++
			}
			out.events[index] = sample.EventsObserved
		}
	}
	out.SimulationTimeMS = offsetMS(req, seq)
	out.Processed++
	out.Bytes += len(data)
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
	if req.Generation != nil && index == 0 && seq < req.Config.MessagesPerVehicle-1 && (seq%max(1, (req.Config.MessagesPerVehicle-2+118)/119) == 0 || seq == req.Config.MessagesPerVehicle-2) {
		p := sample.Message["pld"].(map[string]any)
		out.Trace = append(out.Trace, TracePoint{ElapsedSeconds: float64(offsetMS(req, seq)) / 1000, State: sample.VehicleState, Speed: p["sp"].(float64), RPM: p["rpm"].(float64), Distance: p["cdit"].(float64), Context: sample.Context})
	}
	return nil
}

func reporter(started time.Time, progress func(Result), control *executionControl) func(*Result, bool) {
	last := time.Time{}
	return func(out *Result, force bool) {
		out.ElapsedMS = time.Since(started).Milliseconds()
		paused, isPaused, _ := control.snapshot()
		out.PausedMS = paused.Milliseconds()
		out.ActiveElapsedMS = max(0, out.ElapsedMS-out.PausedMS)
		out.ExecutionState = "running"
		if isPaused {
			out.ExecutionState = "paused"
		}
		out.Remaining = out.Planned - out.Processed
		if out.Transport == "mqtt" {
			out.Unsent = out.Planned - out.Attempted
		}
		if progress != nil && (force || time.Since(last) >= 250*time.Millisecond) {
			progress(*out)
			last = time.Now()
		}
	}
}
