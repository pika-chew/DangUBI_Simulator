package main

import (
	"context"
	"testing"
	"time"
)

func TestRealtimeStopKeepsPartialResult(t *testing.T) {
	req := automaticRequest()
	req.Realtime = true
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { time.Sleep(100 * time.Millisecond); cancel() }()
	started := time.Now()
	out, err := processContext(ctx, req, nil)
	if err != nil || out.State != "stopped" || out.Processed != 3 || out.Remaining != 93 || out.TripCount != 0 || len(out.TripSummaries) != 0 || time.Since(started) > time.Second {
		t.Fatalf("대기 중 중단·부분 집계 실패: %+v %v", out, err)
	}
	if len(out.Assignments) != 3 || out.Assignments[0].ClientID != out.Assignments[2].ClientID {
		t.Fatal("중단으로 고정 배정이 바뀜")
	}
}

func TestMQTTStopDuringPubackWait(t *testing.T) {
	broker, profile := localBroker(t, true, false)
	profile.TimeoutMS = 5000
	profile.MaxDurationSeconds = 10
	req := request("shared", 1)
	req.Config.VehicleCount = 1
	req.Operation = "mqtt"
	req.MQTT = profile
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() {
		for {
			broker.mu.Lock()
			seen := broker.published > 0
			broker.mu.Unlock()
			if seen {
				cancel()
				return
			}
			select {
			case <-ctx.Done():
				return
			case <-time.After(10 * time.Millisecond):
			}
		}
	}()
	started := time.Now()
	out, err := runMQTTContext(ctx, req, nil)
	if err != nil || out.State != "stopped" || out.Attempted != 1 || out.Unconfirmed != 1 || out.Puback != 0 || out.ActiveClients != 0 || out.Processed != 1 || time.Since(started) > 2*time.Second {
		t.Fatalf("응답 대기 중단 실패: %+v %v", out, err)
	}
}
