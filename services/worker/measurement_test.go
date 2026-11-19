package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"math"
	"os"
	"strconv"
	"testing"
	"time"
)

func TestMeasurementVehicleTrackingBoundaries(t *testing.T) {
	for _, limit := range []int{1000, 25000} {
		b := testBroker{}
		if limit == 25000 {
			b.maxMeasuredTopics = limit
		}
		m := map[string]any{"ts": float64(1), "ty": float64(4), "pld": map[string]any{}}
		for i := 0; i <= limit; i++ {
			b.measureMessage(strconv.Itoa(i), m)
		}
		if len(b.lastTimestamp) != limit || b.invalidMeasured != 1 {
			t.Fatalf("차량 추적 한도 %d: 저장 %d 오류 %d", limit, len(b.lastTimestamp), b.invalidMeasured)
		}
	}
}

func TestMeasurementFingerprintMatchesPythonAndDetectsOrder(t *testing.T) {
	m := map[string]any{"ty": float64(4), "ts": float64(1), "pld": map[string]any{"sp": float64(1.5), "flags": []any{true, nil, "한"}}}
	h := sha256.New()
	measurementValue(h, m)
	if hex.EncodeToString(h.Sum(nil)) != "bd88e197fcbbc651353e7db126e41c922dd7544b86a08c0082f45eda8aae25ce" {
		t.Fatal("언어 간 지문 불일치")
	}
	b := testBroker{}
	b.measureMessage("v1", m)
	b.measureMessage("v1", m)
	if b.fingerprint == [32]byte{} || b.outOfOrder != 0 {
		t.Fatal("중복 집계 또는 순서 검사")
	}
	m["ty"] = float64(3)
	b.measureMessage("v1", m)
	m["ty"] = float64(4)
	b.measureMessage("v1", m)
	if b.outOfOrder != 1 {
		t.Fatal("종료 후 발행 누락")
	}
	b.measureMessage("v2", map[string]any{"ty": float64(9)})
	if b.invalidMeasured != 1 {
		t.Fatal("잘못된 본문 검사")
	}
}

func TestParkingMeasurementAllowsNextTripAndRejectsEarlyOrReversedMessages(t *testing.T) {
	for _, tc := range []struct {
		name string
		next float64
		kind float64
		bad  int
	}{
		{"next trip", 1802000, 4, 0},
		{"premature", 1799999, 4, 1},
		{"reversed", -1, 4, 1},
		{"second summary", 1802000, 3, 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			b := testBroker{connectionCounts: make(map[string]int)}
			msg := func(ts, kind float64) map[string]any {
				return map[string]any{"ts": ts, "ty": kind, "pld": map[string]any{}}
			}
			b.measureMessage("v1", msg(1000, 4))
			b.measureMessage("v1", msg(2000, 3))
			b.measureMessage("v1", msg(2000+tc.next, tc.kind))
			if b.outOfOrder != tc.bad {
				t.Fatalf("순서 오류 수: %d, 기대값: %d", b.outOfOrder, tc.bad)
			}
		})
	}
}

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
	b.measureDigest = os.Getenv("DANGUBI_MEASURE_DIGEST") == "1"
	if os.Getenv("DANGUBI_MEASURE_OPERATIONS") == "1" {
		b.maxMeasuredTopics = 25000
	}
	if os.Getenv("DANGUBI_MEASURE_PARKING") == "1" || os.Getenv("DANGUBI_MEASURE_COMPRESSED") == "1" {
		b.connectionCounts = make(map[string]int)
	}
	if os.Getenv("DANGUBI_MEASURE_COMPRESSED") == "1" {
		b.topicClients = make(map[string]string)
	}
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
	timeout := 150 * time.Second
	if os.Getenv("DANGUBI_MEASURE_PARKING") == "1" {
		timeout = 1910 * time.Second
	}
	for deadline := time.Now().Add(timeout); time.Now().Before(deadline); {
		if _, err := os.Stat(path + ".done"); err == nil {
			b.mu.Lock()
			report := map[string]any{"received": b.published, "unique_client_ids": len(b.ids)}
			if b.topicClients != nil {
				report["checked_client_topics"] = len(b.topicClients)
				report["client_changes"] = b.clientChanges
			}
			if b.connectionCounts != nil {
				counts := make(map[string]int, len(b.connectionCounts))
				for id, count := range b.connectionCounts {
					counts[id] = count
				}
				report["connections_by_client"] = counts
			}
			if b.measureDigest {
				report["fingerprint"] = hex.EncodeToString(b.fingerprint[:])
				report["out_of_order"] = b.outOfOrder
				report["invalid_messages"] = b.invalidMeasured
				report["vehicle_topics"] = len(b.lastTimestamp)
			}
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
