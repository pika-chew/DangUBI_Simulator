package main

import (
	"context"
	"encoding/json"
	"os"
	"testing"
	"time"
)

func TestMQTTSessionFixedAssignmentAndPark(t *testing.T) {
	broker, profile := localBroker(t, false, false)
	req := request("shared", 1)
	req.MQTT = profile
	req.Assignments = []Assignment{{"v000001", "sim-stream-c1"}, {"v000002", "sim-stream-c1"}}
	s, err := newMQTTSession(req)
	if err != nil {
		t.Fatal(err)
	}
	defer s.close()
	for i := 0; i < 2; i++ {
		if err := s.command(context.Background(), "stream", streamCommand{Command: "connect", ClientID: "sim-stream-c1"}); err != nil {
			t.Fatal(err)
		}
		for _, v := range []string{"v000001", "v000002"} {
			if err := s.command(context.Background(), "stream", streamCommand{Command: "publish", VehicleID: v, Message: req.Message}); err != nil {
				t.Fatal(err)
			}
		}
		if err := s.command(context.Background(), "stream", streamCommand{Command: "disconnect", ClientID: "sim-stream-c1"}); err != nil {
			t.Fatal(err)
		}
	}
	stats := s.snapshot()
	if stats.Connected != 1 || stats.Connections != 2 || stats.Disconnections != 2 || stats.Active != 0 || stats.Reconnects != 0 || stats.Puback != 4 {
		t.Fatal(stats)
	}
	broker.mu.Lock()
	defer broker.mu.Unlock()
	if broker.published != 4 || len(broker.ids) != 1 {
		t.Fatal("브로커 수신/배정 불일치")
	}
}

func TestMQTTSessionUnconfirmedDoesNotReplay(t *testing.T) {
	broker, profile := localBroker(t, true, false)
	req := request("shared", 1)
	req.MQTT = profile
	req.Assignments = []Assignment{{"v000001", "sim-stream-c1"}}
	s, err := newMQTTSession(req)
	if err != nil {
		t.Fatal(err)
	}
	defer s.close()
	err = s.command(context.Background(), "stream", streamCommand{Command: "publish", VehicleID: "v000001", Message: req.Message})
	if err == nil || s.stats.Attempts != 1 || s.stats.Unconfirmed != 1 || s.stats.Puback != 0 {
		t.Fatal("미확인 처리", err, s.stats)
	}
	broker.mu.Lock()
	defer broker.mu.Unlock()
	if broker.published != 1 {
		t.Fatal("자동 재발행")
	}
}

// API 통합 시험이 요청한 경우에만 일회성 로컬 mTLS 브로커를 연다.
func TestScheduleMQTTFixture(t *testing.T) {
	path := os.Getenv("DANGUBI_SCHEDULE_PROFILE")
	if path == "" {
		t.Skip("일정 전송 통합 시험 전용")
	}
	fault := os.Getenv("DANGUBI_SCHEDULE_FAULT")
	broker, p := localBroker(t, fault == "no_ack", fault == "disconnect_once")
	p.MaxClients = 5
	p.MaxMessages = 100
	p.MaxDurationSeconds = 30
	p.TimeoutMS = 500
	p.ReconnectAttempts = 2
	if fault == "delay_ack" {
		broker.mu.Lock()
		broker.ackDelay = 300 * time.Millisecond
		broker.mu.Unlock()
	}
	raw, _ := json.Marshal(p)
	if err := os.WriteFile(path+".tmp", raw, 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(path+".tmp", path); err != nil {
		t.Fatal(err)
	}
	for until := time.Now().Add(120 * time.Second); time.Now().Before(until); {
		if _, err := os.Stat(path + ".done"); err == nil {
			broker.mu.Lock()
			report := map[string]any{"received": broker.published, "unique_clients": len(broker.ids), "connections": len(broker.sockets)}
			broker.mu.Unlock()
			raw, _ := json.Marshal(report)
			os.WriteFile(path+".report", raw, 0600)
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatal("로컬 시험 종료 대기 초과")
}
