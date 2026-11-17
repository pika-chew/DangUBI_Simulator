package main

import (
	"context"
	"encoding/json"
	"reflect"
	"sync"
	"testing"
	"time"
)

func TestConcurrentSharedAutomaticPayloadOrder(t *testing.T) {
	_, b := concurrentRequest(t, 0)
	// 이 시험은 생성 주기를 실제 시간으로 실행해 종료 Trip까지 확인한다.
	r := automaticRequest()
	r.Operation = "mqtt"
	r.Generation.ModelVersion = eventVersion
	r.Generation.DurationSeconds = 10
	r.Config.PublishMode = "parallel"
	r.Config.MaxInFlight = 2
	r.Config.QueueCapacity = 3
	r.Config.ClientCount = 2
	r.Config.IntervalMS = 1000
	profileReq, _ := concurrentRequest(t, 0)
	r.MQTT = profileReq.MQTT
	// 첫 브로커는 사용하지 않으며 실제 발행 브로커로 바꾼다.
	b, r.MQTT = localBroker(t, false, false)
	r.MQTT.MaxMessages = 100
	r.MQTT.MaxDurationSeconds = 15
	r.MQTT.ConnectPerSecond = 5
	out, err := runMQTT(r)
	if err != nil || out.State != "mqtt_completed" || out.Puback != 36 || out.TripCount != 3 {
		t.Fatal(out, err)
	}
	normalized, _ := normalize(r)
	b.mu.Lock()
	defer b.mu.Unlock()
	byVehicle := map[float64][]map[string]any{}
	for _, m := range b.messages {
		tid := m["pld"].(map[string]any)["tid"].(float64)
		byVehicle[tid] = append(byVehicle[tid], m)
	}
	for index := 0; index < 3; index++ {
		messages := byVehicle[float64(index+1)]
		if len(messages) != 12 {
			t.Fatal("차량별 수량", index, len(messages))
		}
		for seq, m := range messages {
			expected, _ := automaticMessage(normalized, index, seq)
			data, _ := json.Marshal(expected)
			var decoded map[string]any
			json.Unmarshal(data, &decoded)
			if !reflect.DeepEqual(decoded, m) {
				t.Fatal("생성값/차량별 순서 변경", index, seq)
			}
		}
	}
}

func concurrentRequest(t *testing.T, delay time.Duration) (Request, *testBroker) {
	b, p := localBroker(t, false, false)
	b.mu.Lock()
	b.ackDelay = delay
	b.mu.Unlock()
	p.LocalBenchmark = true
	p.ConnectPerSecond = 100
	p.PublishPerSecond = 2000
	p.MaxMessages = 100
	p.MaxDurationSeconds = 10
	p.TimeoutMS = 1000
	r := request("dedicated", 3)
	r.Operation = "mqtt"
	r.MQTT = p
	r.Config.PublishMode = "parallel"
	r.Config.MaxInFlight = 2
	r.Config.QueueCapacity = 1
	r.Config.MessagesPerVehicle = 6
	r.Config.IntervalMS = 1
	return r, b
}
func TestConcurrentBoundsOrderAndBackpressure(t *testing.T) {
	r, b := concurrentRequest(t, 20*time.Millisecond)
	out, err := runMQTT(r)
	if err != nil || out.State != "mqtt_completed" || out.Puback != 18 || out.ActiveClients != 0 {
		t.Fatal(out, err)
	}
	s := out.Transmission
	if s.PeakQueued > 1 || s.PeakInFlight != 2 || s.Backpressure == 0 || s.Queued != 0 || s.InFlight != 0 || s.Discarded != 0 {
		t.Fatal("대기/동시성 상한", s)
	}
	if out.Performance.Ack.Count != 18 || out.Performance.Schedule.Count != 18 || out.Performance.Generation.Count != 18 {
		t.Fatal("집계", out.Performance)
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.peakHandling < 2 || b.peakHandling > 2 {
		t.Fatal("브로커 동시 처리", b.peakHandling)
	}
	last := map[string]float64{}
	counts := map[string]int{}
	for i, m := range b.messages {
		id := b.receivedIDs[i]
		ts := m["ts"].(float64)
		if counts[id] > 0 && ts <= last[id] {
			t.Fatal("Client 순서 위반")
		}
		last[id] = ts
		counts[id]++
	}
	for _, n := range counts {
		if n != 6 {
			t.Fatal("Client 누락")
		}
	}
}
func TestConcurrentGlobalRateLimit(t *testing.T) {
	r, b := concurrentRequest(t, 0)
	r.Config.MessagesPerVehicle = 2
	r.Config.QueueCapacity = 20
	r.Config.MaxInFlight = 3
	r.MQTT.PublishPerSecond = 10
	out, err := runMQTT(r)
	if err != nil || out.Puback != 6 {
		t.Fatal(out, err)
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	for i := 1; i < len(b.receivedAt); i++ {
		if b.receivedAt[i].Sub(b.receivedAt[i-1]) < 80*time.Millisecond {
			t.Fatal("전체 발행 속도 제한 위반")
		}
	}
}
func TestConcurrentPauseDrainsBeforeAcknowledging(t *testing.T) {
	r, _ := concurrentRequest(t, 40*time.Millisecond)
	r.Config.QueueCapacity = 10
	ctx, c := controlledContext()
	var once sync.Once
	pausedAck := -1
	out, err := runMQTTContext(ctx, r, func(out Result) {
		if out.Attempted == 1 {
			once.Do(func() { c.request(true) })
		}
		if out.ExecutionState == "paused" {
			if out.Transmission.InFlight != 0 || out.Attempted != out.Puback {
				t.Error("응답 집계 전 정지 완료")
			}
			if pausedAck < 0 {
				pausedAck = out.Puback
				go func() { time.Sleep(100 * time.Millisecond); c.request(false) }()
			} else if out.Puback != pausedAck {
				t.Error("정지 완료 후 집계 변화")
			}
		}
	})
	if err != nil || out.State != "mqtt_completed" || out.Puback != 18 || out.PausedMS < 90 || pausedAck != 1 {
		t.Fatal(out, err, pausedAck)
	}
}
func TestConcurrentStopAndUnconfirmedAccounting(t *testing.T) {
	for _, stop := range []bool{false, true} {
		r, b := concurrentRequest(t, 0)
		r.Config.QueueCapacity = 10
		r.MQTT.TimeoutMS = 150
		b.mu.Lock()
		b.noAck = true
		b.mu.Unlock()
		ctx, cancel := context.WithCancel(context.Background())
		out, err := runMQTTContext(ctx, r, func(out Result) {
			if stop && out.Attempted == 2 {
				cancel()
			}
		})
		cancel()
		if err != nil || out.Attempted != out.Puback+out.Unconfirmed || out.Unconfirmed == 0 || out.Transmission.InFlight != 0 || out.Transmission.Queued != 0 || out.ActiveClients != 0 || out.Processed != out.Attempted+out.Transmission.Discarded {
			t.Fatal("미확인/대기 정리", out, err)
		}
		if stop && out.State != "stopped" || !stop && out.State != "mqtt_failed" {
			t.Fatal(out.State)
		}
	}
}
func TestConcurrentFixedIDReconnectAndLifetimes(t *testing.T) {
	for _, lifetime := range []string{"always", "while_driving"} {
		r, b := concurrentRequest(t, 0)
		r.Config.MessagesPerVehicle = 2
		r.Config.IntervalMS = 200
		r.Config.Lifetime = lifetime
		r.Config.QueueCapacity = 10
		b.mu.Lock()
		b.closeOnce = true
		b.mu.Unlock()
		out, err := runMQTT(r)
		if err != nil || out.Puback != 6 || out.Reconnects < 1 || out.ActiveClients != 0 {
			t.Fatal(out, err)
		}
		b.mu.Lock()
		n := len(b.ids)
		b.mu.Unlock()
		if n != 3 {
			t.Fatal("ID 변경")
		}
	}
}
func TestTransmissionConfigLimits(t *testing.T) {
	for _, c := range []Config{{ClientCount: 2, PublishMode: "bad"}, {ClientCount: 2, PublishMode: "parallel", MaxInFlight: 3}, {ClientCount: 2, MaxInFlight: 2}, {ClientCount: 2, QueueCapacity: 1001}, {ClientCount: 2, MaxInFlight: -1}} {
		if validateTransmission(c) == nil {
			t.Fatal(c)
		}
	}
}
