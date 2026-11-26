package main

import (
	"context"
	"testing"
	"time"
)

// 응답이 한 번 누락되면 재접속 뒤 같은 메시지를 다시 보내 확인을 받는다(중복 가능, 건수 기록).
func TestPublishRetryResendsAfterMissingPuback(t *testing.T) {
	broker, profile := localBroker(t, false, false)
	broker.mu.Lock()
	broker.dropAcks = 1
	broker.mu.Unlock()
	profile.TimeoutMS = 300
	profile.RetryWindowSeconds = 20
	req := request("shared", 1)
	req.MQTT = profile
	req.Assignments = []Assignment{{"v000001", "sim-stream-c1"}}
	s, err := newMQTTSession(req)
	if err != nil {
		t.Fatal(err)
	}
	defer s.close()
	if err := s.command(context.Background(), "stream", streamCommand{Command: "publish", VehicleID: "v000001", Message: req.Message}); err != nil {
		t.Fatal("재전송 후에도 실패", err)
	}
	if s.stats.Attempts != 2 || s.stats.Puback != 1 || s.stats.Resent != 1 || s.stats.Unconfirmed != 0 || s.stats.Reconnects != 1 {
		t.Fatal("재전송 집계", s.stats.Attempts, s.stats.Puback, s.stats.Resent, s.stats.Unconfirmed, s.stats.Reconnects)
	}
	broker.mu.Lock()
	defer broker.mu.Unlock()
	if broker.published != 2 {
		t.Fatal("브로커 수신(원본+재전송)", broker.published)
	}
}

// 재시도 시간 안에 끝내 확인하지 못하면 미확인 1건으로 멈춘다.
func TestPublishRetryGivesUpAfterWindow(t *testing.T) {
	_, profile := localBroker(t, true, false)
	profile.TimeoutMS = 200
	profile.RetryWindowSeconds = 2
	req := request("shared", 1)
	req.MQTT = profile
	req.Assignments = []Assignment{{"v000001", "sim-stream-c1"}}
	s, err := newMQTTSession(req)
	if err != nil {
		t.Fatal(err)
	}
	defer s.close()
	started := time.Now()
	err = s.command(context.Background(), "stream", streamCommand{Command: "publish", VehicleID: "v000001", Message: req.Message})
	if err == nil || s.stats.Unconfirmed != 1 || s.stats.Puback != 0 || s.stats.Resent < 1 {
		t.Fatal("재시도 종료", err, s.stats.Unconfirmed, s.stats.Resent)
	}
	if elapsed := time.Since(started); elapsed < 2*time.Second || elapsed > 10*time.Second {
		t.Fatal("재시도 시간", elapsed)
	}
}
