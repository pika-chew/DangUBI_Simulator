package main

import (
	"context"
	"encoding/json"
	"io"
	"os"
	"testing"
	"time"
)

func TestMQTTStreamParallelCancellation(t *testing.T) {
	for _, mode := range []string{"stop", "eof", "duplicate_client", "oversized"} {
		t.Run(mode, func(t *testing.T) {
			broker, profile := localBroker(t, true, false)
			profile.TimeoutMS = 2000
			req := request("dedicated", 2)
			req.MQTT = profile
			req.Assignments = []Assignment{{"v000001", "stream-a"}, {"v000002", "stream-b"}}
			input, send := io.Pipe()
			output, receive := io.Pipe()
			done := make(chan struct{})
			go func() { defer close(done); defer receive.Close(); serveMQTTStream(req, input, receive) }()
			defer input.Close()
			defer send.Close()
			defer output.Close()
			enc := json.NewEncoder(send)
			dec := json.NewDecoder(output)
			var reply struct {
				ID     int64       `json:"id"`
				OK     bool        `json:"ok"`
				Closed bool        `json:"closed"`
				Stats  streamStats `json:"stats"`
			}
			if err := dec.Decode(&reply); err != nil || !reply.OK {
				t.Fatal("초기 응답", err, reply)
			}
			// 먼저 연결해 접속 대기와 PUBACK 대기를 구별한다.
			for i, a := range req.Assignments {
				enc.Encode(streamCommand{ID: int64(i + 1), Command: "connect", ClientID: a.ClientID})
				if err := dec.Decode(&reply); err != nil || !reply.OK {
					t.Fatal("연결", err, reply)
				}
			}
			for i, a := range req.Assignments {
				enc.Encode(streamCommand{ID: int64(i + 3), Command: "publish", VehicleID: a.VehicleID, Message: req.Message})
			}
			until := time.Now().Add(time.Second)
			for {
				broker.mu.Lock()
				n := broker.published
				broker.mu.Unlock()
				if n == 2 {
					break
				}
				if time.Now().After(until) {
					t.Fatal("Client 병행 발행 실패", n)
				}
				time.Sleep(time.Millisecond)
			}
			switch mode {
			case "stop":
				enc.Encode(streamCommand{Command: "stop"})
			case "eof":
				send.Close()
			case "duplicate_client":
				enc.Encode(streamCommand{ID: 5, Command: "publish", VehicleID: "v000001", Message: req.Message})
			case "oversized":
				go func() { send.Write(make([]byte, 262146)) }()
			}
			for {
				if err := dec.Decode(&reply); err != nil {
					t.Fatal("정리 응답", err)
				}
				if reply.Closed {
					break
				}
			}
			if reply.OK || reply.Stats.Active != 0 || reply.Stats.Attempts != 2 || reply.Stats.Unconfirmed != 2 || reply.Stats.Puback != 0 {
				t.Fatal("중단 최종 집계", reply)
			}
			select {
			case <-done:
			case <-time.After(time.Second):
				t.Fatal("송신 종료 지연")
			}
			broker.mu.Lock()
			n := broker.published
			broker.mu.Unlock()
			if n != 2 {
				t.Fatal("미확인 자동 재발행", n)
			}
		})
	}
}

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
	if fault == "split_ack" {
		broker.mu.Lock()
		broker.ackDelay = 400 * time.Millisecond
		broker.slowSuffix = "-c001"
		broker.mu.Unlock()
		p.TimeoutMS = 1200
		p.PublishPerSecond = 10
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
			report := map[string]any{"received": broker.published, "unique_clients": len(broker.ids), "connections": len(broker.sockets), "received_at": broker.receivedAt, "client_ids": broker.receivedIDs, "topics": broker.receivedTopics, "messages": broker.messages, "peak_handling": broker.peakHandling}
			broker.mu.Unlock()
			raw, _ := json.Marshal(report)
			os.WriteFile(path+".report", raw, 0600)
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatal("로컬 시험 종료 대기 초과")
}
