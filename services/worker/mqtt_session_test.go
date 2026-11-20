package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"testing"
	"time"
)

func TestOperationsStreamCompactCountersAndFinalDetails(t *testing.T) {
	_, profile := localBroker(t, false, false)
	profile.ExecutionProfile = "operations"
	profile.MaxConnecting = 100
	profile.ConnectPerSecond, profile.PublishPerSecond = 1000, 15000
	req := request("dedicated", 1)
	req.MQTT = profile
	req.Assignments = []Assignment{{"v000001", "detail-c1"}}
	input, send := io.Pipe()
	output, receive := io.Pipe()
	defer input.Close()
	defer send.Close()
	defer output.Close()
	go func() { defer receive.Close(); serveMQTTStream(req, input, receive) }()
	encoder, decoder := json.NewEncoder(send), json.NewDecoder(output)
	read := func() map[string]any {
		var reply map[string]any
		if err := decoder.Decode(&reply); err != nil || reply["ok"] != true {
			t.Fatal(err, reply)
		}
		return reply
	}
	initial := read()
	if initial["stats_complete"] != true || initial["capabilities"].(map[string]any)["raw_message"] != true {
		t.Fatal("초기 상세 누락")
	}
	encoder.Encode(streamCommand{ID: 1, Command: "connect", ClientID: "detail-c1"})
	read()
	compact := 0
	for i := 1; i <= 20; i++ {
		command := streamCommand{ID: int64(i + 1), Command: "publish", VehicleID: "v000001", Message: req.Message}
		if i%2 == 1 {
			command.MessageRaw, _ = json.Marshal(req.Message)
			command.Message = nil
		}
		encoder.Encode(command)
		reply := read()
		stats := reply["stats"].(map[string]any)
		if reply["id"] != float64(i+1) || stats["puback_count"] != float64(i) {
			t.Fatal("개별 완료 집계", reply)
		}
		if reply["stats_complete"] == false {
			compact++
			if _, ok := stats["performance"]; ok {
				t.Fatal("상세 반복 전달")
			}
		}
	}
	encoder.Encode(streamCommand{ID: 22, Command: "close"})
	last := read()
	stats := last["stats"].(map[string]any)
	if compact == 0 || last["closed"] != true || last["stats_complete"] != true || stats["active_clients"] != float64(0) {
		t.Fatal("최종 상세/정리", last)
	}
	perf := stats["performance"].(map[string]any)
	if perf["ack"].(map[string]any)["count"] != float64(20) {
		t.Fatal("최종 표본 누락", perf)
	}
}

func TestScaleSessionIsLoopbackOnlyAndNormalAssignmentsStayLimited(t *testing.T) {
	_, p := localBroker(t, false, false)
	p.LocalBenchmark = true
	p.MaxClients = 1000
	p.MaxMessages = 10000
	p.MaxDurationSeconds = 120
	p.ConnectPerSecond = 200
	p.PublishPerSecond = 5000
	if err := p.validate(); err != nil {
		t.Fatal(err)
	}
	url := p.BrokerURL
	p.BrokerURL = "ssl://example.com:8883"
	if p.validate() == nil {
		t.Fatal("외부 확장 연결 허용")
	}
	p.BrokerURL = url
	req := request("shared", 1)
	req.MQTT = p
	req.Assignments = nil
	for i := 0; i < 1000; i++ {
		req.Assignments = append(req.Assignments, Assignment{fmt.Sprintf("v%06d", i), "scale-c1"})
	}
	s, err := newMQTTSession(req)
	if err != nil {
		t.Fatal(err)
	}
	s.close()
	p.LocalBenchmark = false
	p.MaxClients = 20
	p.MaxMessages = 100
	p.MaxDurationSeconds = 30
	p.ConnectPerSecond = 5
	p.PublishPerSecond = 70
	req.Assignments = req.Assignments[:101]
	if _, err := newMQTTSession(req); err == nil {
		t.Fatal("일반 배정 상한 변경")
	}
}

func TestParkingProfileRequiresTaggedLoopbackAndSmallLimits(t *testing.T) {
	_, profile := localBroker(t, false, false)
	profile.LocalBenchmark = true
	profile.MaxDurationSeconds = 1880
	profile.MaxClients = 8
	profile.MaxMessages = 128
	if valid := profile.validate() == nil; valid != parkingBenchmarkEnabled {
		t.Fatalf("빌드별 장시간 허용 불일치: %v", valid)
	}
	for name, change := range map[string]func(*MQTTProfile){
		"no flag":      func(p *MQTTProfile) { p.LocalBenchmark = false },
		"external":     func(p *MQTTProfile) { p.BrokerURL = "ssl://example.com:8883" },
		"duration":     func(p *MQTTProfile) { p.MaxDurationSeconds++ },
		"clients":      func(p *MQTTProfile) { p.MaxClients++ },
		"messages":     func(p *MQTTProfile) { p.MaxMessages++ },
		"connect rate": func(p *MQTTProfile) { p.ConnectPerSecond++ },
		"publish rate": func(p *MQTTProfile) { p.PublishPerSecond++ },
	} {
		t.Run(name, func(t *testing.T) {
			p := *profile
			change(&p)
			if p.validate() == nil {
				t.Fatal("시험 프로필 범위 초과 허용")
			}
		})
	}
}

func TestParkingTraceKeepsBothSessionsAndFirstMicrotrips(t *testing.T) {
	if !parkingBenchmarkEnabled {
		t.Skip("장시간 시험 전용 Worker")
	}
	_, profile := localBroker(t, false, false)
	profile.LocalBenchmark = true
	profile.MaxDurationSeconds = 1880
	req := request("dedicated", 1)
	req.MQTT = profile
	req.Assignments = []Assignment{{"v000001", "park-c1"}}
	s, err := newMQTTSession(req)
	if err != nil {
		t.Fatal(err)
	}
	defer s.close()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	for trip := 1; trip <= 2; trip++ {
		if err := s.connect(ctx, "park-c1"); err != nil {
			t.Fatal(err)
		}
		for seq := 0; seq < 2; seq++ {
			message := map[string]any{"ty": float64(4), "ts": float64(100*trip + seq), "pld": map[string]any{"tid": float64(trip)}}
			if err := s.command(ctx, "parking-run", streamCommand{Command: "publish", VehicleID: "v000001", Message: message, DueAtMS: time.Now().UnixMilli()}); err != nil {
				t.Fatal(err)
			}
		}
		s.disconnect("park-c1")
		time.Sleep(5 * time.Millisecond)
	}
	stats := s.snapshot()
	connects := stats.ParkingConnects["park-c1"]
	sends := stats.ParkingFirstSends["v000001"]
	if len(connects) != 2 || connects[1].OfflineMS < 5 || stats.Reconnects != 0 {
		t.Fatalf("주차 연결 기록 불일치: %+v", stats)
	}
	if len(sends) != 2 || sends[0].TimestampMS != 100 || sends[1].TimestampMS != 200 ||
		sends[0].ClientID != "park-c1" || sends[1].ClientID != "park-c1" {
		t.Fatalf("운행별 첫 발행 기록 불일치: %+v", sends)
	}
}

func TestMQTTStreamParallelCancellation(t *testing.T) {
	for _, mode := range []string{"stop", "eof", "duplicate_client", "oversized", "operations_stop", "operations_eof"} {
		t.Run(mode, func(t *testing.T) {
			broker, profile := localBroker(t, true, false)
			profile.TimeoutMS = 2000
			if mode == "operations_stop" || mode == "operations_eof" {
				profile.ExecutionProfile = "operations"
				profile.MaxConnecting = 2
				profile.PublishPerSecond = 15000
			}
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
			case "stop", "operations_stop":
				enc.Encode(streamCommand{Command: "stop"})
			case "eof", "operations_eof":
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
