package main

import (
	"context"
	"reflect"
	"sync"
	"testing"
	"time"
)

func controlledContext() (context.Context, *executionControl) {
	c := newExecutionControl()
	return context.WithValue(context.Background(), controlKey{}, c), c
}

func TestPausePreservesRemainingInterval(t *testing.T) {
	ctx, c := controlledContext()
	started := time.Now()
	var timestamps []time.Time
	var resumeAt time.Time
	var once sync.Once
	go func() { time.Sleep(50 * time.Millisecond); c.request(true) }()
	for seq := 0; seq < 3; seq++ {
		err := waitSchedule(ctx, started.Add(time.Duration(seq)*300*time.Millisecond), func(force bool) {
			_, paused, _ := c.snapshot()
			if paused {
				once.Do(func() { go func() { time.Sleep(350 * time.Millisecond); resumeAt = time.Now(); c.request(false) }() })
			}
		})
		if err != nil {
			t.Fatal(err)
		}
		timestamps = append(timestamps, time.Now())
	}
	paused, _, _ := c.snapshot()
	if paused < 340*time.Millisecond || time.Since(started) < 940*time.Millisecond {
		t.Fatal("정지 시간이 보존되지 않음")
	}
	if timestamps[1].Sub(resumeAt) < 210*time.Millisecond || timestamps[2].Sub(timestamps[1]) < 270*time.Millisecond {
		t.Fatal("남은 주기를 건너뛰거나 몰아서 실행함")
	}
}

func TestPausePreservesAutomaticTripAndSeed(t *testing.T) {
	req := automaticRequest()
	baseline, err := process(req)
	if err != nil {
		t.Fatal(err)
	}
	ctx, c := controlledContext()
	c.request(true)
	var once sync.Once
	out, err := processContext(ctx, req, func(r Result) {
		if r.ExecutionState == "paused" {
			once.Do(func() { go func() { time.Sleep(100 * time.Millisecond); c.request(false) }() })
		}
	})
	if err != nil || out.PausedMS < 80 || !reflect.DeepEqual(out.Samples, baseline.Samples) || !reflect.DeepEqual(out.TripSummaries, baseline.TripSummaries) || !reflect.DeepEqual(out.Assignments, baseline.Assignments) {
		t.Fatal("재개 후 데이터·집계·배정이 바뀜", err)
	}
}

func TestDeadlineDuringPause(t *testing.T) {
	base, c := controlledContext()
	c.request(true)
	ctx, cancel := context.WithTimeout(base, 150*time.Millisecond)
	defer cancel()
	req := request("shared", 1)
	out, err := processContext(ctx, req, nil)
	if err != nil || out.State != "stopped" || out.Processed != 0 || out.PausedMS < 100 || out.SimulationTimeMS != 0 {
		t.Fatal("정지 중 제한시간/미생성 집계 실패", out, err)
	}
}

func TestMQTTPauseKeepsConnectionAndLimit(t *testing.T) {
	broker, profile := localBroker(t, false, false)
	profile.MaxDurationSeconds = 1
	req := request("shared", 1)
	req.Config.VehicleCount = 1
	req.Config.MessagesPerVehicle = 3
	req.Config.IntervalMS = 100
	req.Operation = "mqtt"
	req.MQTT = profile
	ctx, c := controlledContext()
	var once sync.Once
	out, err := runMQTTContext(ctx, req, func(r Result) {
		if r.Puback == 1 {
			once.Do(func() { c.request(true) })
		}
	})
	if err != nil || out.State != "mqtt_failed" || out.Puback != 1 || out.Attempted != 1 || out.ActiveClients != 0 || out.PausedMS < 500 {
		t.Fatalf("정지 중 발행 또는 제한시간 실패: %+v %v", out, err)
	}
	broker.mu.Lock()
	defer broker.mu.Unlock()
	if broker.published != 1 || len(broker.ids) != 1 {
		t.Fatal("정지 중 연결·발행 수가 변함")
	}
}
