package main

import (
	"context"
	"fmt"
	"testing"
	"time"
)

func TestOperatingProfileAndLargeFixedAssignments(t *testing.T) {
	_, p := localBroker(t, false, false)
	p.ExecutionProfile = "operations"
	p.MaxConnecting = 2
	p.MaxClients = 25000
	p.MaxMessages = 1000000000000
	p.MaxDurationSeconds = 2592600
	p.ConnectPerSecond = 1000
	p.PublishPerSecond = 15000
	if err := p.validate(); err != nil {
		t.Fatal(err)
	}
	req := request("shared", 1)
	req.MQTT = p
	for i := 1; i <= 25000; i++ {
		req.Assignments = append(req.Assignments, Assignment{fmt.Sprintf("v%06d", i), fmt.Sprintf("pool-%03d", i%500)})
	}
	s, err := newMQTTSession(req)
	if err != nil {
		t.Fatal(err)
	}
	defer s.close()
	if len(s.clients) != 500 || len(s.vehicles) != 25000 || cap(s.connectSlots) != 2 {
		t.Fatal("운영 배정/접속 상한")
	}
	if s.clientPublish["pool-001"].step != 10*time.Millisecond || s.clientConnect["pool-001"].step != time.Second {
		t.Fatal("Client별 속도 제한")
	}
	p.ExecutionProfile = "small_test"
	if p.validate() == nil {
		t.Fatal("소량 설정의 상한 우회")
	}
	p.ExecutionProfile = "operations"
	p.LocalBenchmark = true
	if p.validate() == nil {
		t.Fatal("운영/격리 시험 혼용")
	}
}

func TestOperatingConnectPublishAndCancelledAdmission(t *testing.T) {
	_, p := localBroker(t, false, false)
	p.ExecutionProfile = "operations"
	p.MaxConnecting = 1
	p.ConnectPerSecond = 1000
	p.PublishPerSecond = 15000
	req := request("shared", 1)
	req.MQTT = p
	req.Assignments = []Assignment{{"v000001", "pool-1"}}
	s, err := newMQTTSession(req)
	if err != nil {
		t.Fatal(err)
	}
	defer s.close()
	s.connectSlots <- struct{}{}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if s.connect(ctx, "pool-1") == nil {
		t.Fatal("접속 대기 취소 누락")
	}
	<-s.connectSlots
	if err = s.command(context.Background(), req.RunID, streamCommand{Command: "publish", VehicleID: "v000001", Message: req.Message}); err != nil {
		t.Fatal(err)
	}
	if stats := s.snapshot(); stats.Puback != 1 || stats.Active != 1 || stats.Peak != 1 {
		t.Fatal(stats)
	}
	s.close()
	if stats := s.snapshot(); stats.Active != 0 {
		t.Fatal("운영 최종 연결 정리", stats)
	}
}
