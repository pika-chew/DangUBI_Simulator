package main

import (
	"encoding/json"
	"math"
	"os"
	"testing"
	"time"
)

func TestDurationSummary(t *testing.T) {
	m := durationMetric{}
	if m.summary().Count != 0 {
		t.Fatal("빈 집계")
	}
	for i := 1; i <= 100; i++ {
		m.add(time.Duration(i) * time.Microsecond)
	}
	s := m.summary()
	if s.Count != 100 || math.Abs(s.MeanMS-.0505) > 1e-9 || s.MaxMS != .1 || s.P95UpperMS != .128 {
		t.Fatal(s)
	}
}
func TestLocalMeasurementLimits(t *testing.T) {
	_, p := localBroker(t, false, false)
	p.LocalBenchmark = true
	p.MaxClients = 100
	p.MaxMessages = 5000
	p.MaxDurationSeconds = 120
	p.ConnectPerSecond = 100
	p.PublishPerSecond = 2000
	if err := p.validate(); err != nil {
		t.Fatal(err)
	}
	p.BrokerURL = "ssl://example.com:8883"
	if p.validate() == nil {
		t.Fatal("외부 측정 허용")
	}
	p.BrokerURL = "ssl://localhost:8883"
	if p.validate() == nil {
		t.Fatal("DNS 주소 측정 허용")
	}
	p.LocalBenchmark = false
	if p.validate() == nil {
		t.Fatal("기존 MQTT 상한 완화")
	}
}

// 측정 스크립트만 실행하는 일회성 루프백 브로커. 전체 payload를 보관하지 않는다.
func TestMQTTMeasurementFixture(t *testing.T) {
	path := os.Getenv("DANGUBI_MEASURE_PROFILE")
	if path == "" {
		t.Skip("로컬 성능 측정 전용")
	}
	fault := os.Getenv("DANGUBI_MEASURE_FAULT")
	b, p := localBroker(t, fault == "no_ack" || fault == "stop_wait", fault == "disconnect_once")
	b.mu.Lock()
	b.discardPayload = true
	if fault == "delay_ack" {
		b.ackDelay = 20 * time.Millisecond
	}
	b.mu.Unlock()
	p.LocalBenchmark = true
	p.MaxClients = 100
	p.MaxMessages = 5000
	p.MaxDurationSeconds = 120
	p.ConnectPerSecond = 100
	p.PublishPerSecond = 2000
	p.TimeoutMS = 1000
	p.ReconnectAttempts = 2
	data, _ := json.Marshal(p)
	if err := os.WriteFile(path+".tmp", data, 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(path+".tmp", path); err != nil {
		t.Fatal(err)
	}
	for deadline := time.Now().Add(150 * time.Second); time.Now().Before(deadline); {
		if _, err := os.Stat(path + ".done"); err == nil {
			b.mu.Lock()
			report := map[string]any{"received": b.published, "unique_client_ids": len(b.ids)}
			b.mu.Unlock()
			data, _ = json.Marshal(report)
			if err := os.WriteFile(path+".report", data, 0600); err != nil {
				t.Fatal(err)
			}
			return
		}
		time.Sleep(25 * time.Millisecond)
	}
	t.Fatal("측정 종료 대기시간 초과")
}
