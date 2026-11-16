package main

import (
	"context"
	"encoding/json"
	"errors"
	"time"
)

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
		out.Trace = append(out.Trace, TracePoint{float64(offsetMS(req, seq)) / 1000, sample.VehicleState, p["sp"].(float64), p["rpm"].(float64), p["cdit"].(float64)})
	}
	return nil
}

func reporter(started time.Time, progress func(Result)) func(*Result, bool) {
	last := time.Time{}
	return func(out *Result, force bool) {
		out.ElapsedMS = time.Since(started).Milliseconds()
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
