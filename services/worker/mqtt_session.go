package main

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"regexp"
	"time"

	mqtt "github.com/eclipse/paho.mqtt.golang"
)

// 기존 소량 실행과 일정 실행이 같은 TLS/세션 옵션을 사용한다.
func newMQTTClient(p *MQTTProfile, id string, config *tls.Config) mqtt.Client {
	timeout := time.Duration(p.TimeoutMS) * time.Millisecond
	options := mqtt.NewClientOptions().AddBroker(p.BrokerURL).SetClientID(id).SetTLSConfig(config.Clone()).SetProtocolVersion(4).SetCleanSession(true).SetAutoReconnect(false).SetConnectRetry(false).SetConnectTimeout(timeout).SetWriteTimeout(timeout).SetKeepAlive(30 * time.Second).SetPingTimeout(timeout)
	return mqtt.NewClient(options)
}

type streamCommand struct {
	Command   string         `json:"command"`
	ClientID  string         `json:"client_id,omitempty"`
	VehicleID string         `json:"vehicle_id,omitempty"`
	Message   map[string]any `json:"message,omitempty"`
}

type streamStats struct {
	Connected      int                 `json:"connected_clients"`
	Active         int                 `json:"active_clients"`
	Peak           int                 `json:"peak_active_clients"`
	Attempts       int                 `json:"publish_attempts"`
	Published      int                 `json:"mqtt_published"`
	Puback         int                 `json:"puback_count"`
	Unconfirmed    int                 `json:"unconfirmed_messages"`
	Reconnects     int                 `json:"reconnect_count"`
	Connections    int                 `json:"connection_count"`
	Disconnections int                 `json:"disconnect_count"`
	Performance    *PerformanceSummary `json:"performance"`
}

type mqttSession struct {
	parked                     map[string]bool
	profile                    *MQTTProfile
	clients                    map[string]mqtt.Client
	vehicles                   map[string]string
	seen                       map[string]bool
	stats                      streamStats
	metrics                    performanceMetrics
	connectPacer, publishPacer pacer
	started                    time.Time
}

func newMQTTSession(req Request) (*mqttSession, error) {
	p := req.MQTT
	if err := p.validate(); err != nil {
		return nil, err
	}
	config, err := p.tlsConfig()
	if err != nil {
		return nil, err
	}
	validID := regexp.MustCompile(`^[a-zA-Z0-9_-]{1,128}$`)
	if !validID.MatchString(req.RunID) || len(req.Assignments) < 1 || len(req.Assignments) > 100 {
		return nil, errors.New("실행/차량 배정을 확인하세요.")
	}
	s := &mqttSession{profile: p, clients: map[string]mqtt.Client{}, vehicles: map[string]string{}, seen: map[string]bool{}, parked: map[string]bool{}, started: time.Now()}
	s.connectPacer.step = time.Second / time.Duration(p.ConnectPerSecond)
	s.publishPacer.step = time.Second / time.Duration(p.PublishPerSecond)
	for _, a := range req.Assignments {
		if !validID.MatchString(a.VehicleID) || !validID.MatchString(a.ClientID) || s.vehicles[a.VehicleID] != "" {
			return nil, errors.New("고정 배정표를 확인하세요.")
		}
		s.vehicles[a.VehicleID] = a.ClientID
		if s.clients[a.ClientID] == nil {
			s.clients[a.ClientID] = newMQTTClient(p, a.ClientID, config)
		}
	}
	if len(s.clients) > p.MaxClients {
		return nil, errors.New("Client 상한을 초과했습니다.")
	}
	return s, nil
}

func (s *mqttSession) snapshot() streamStats {
	s.stats.Active = 0
	for _, c := range s.clients {
		if c.IsConnectionOpen() {
			s.stats.Active++
		}
	}
	s.stats.Peak = max(s.stats.Peak, s.stats.Active)
	s.stats.Performance = s.metrics.summary(s.stats.Puback, time.Since(s.started))
	return s.stats
}

func (s *mqttSession) disconnect(id string) {
	if c := s.clients[id]; c != nil {
		s.parked[id] = true
		if c.IsConnectionOpen() {
			s.stats.Disconnections++
		}
		c.Disconnect(0)
		// Paho의 해제 처리가 비동기로 끝날 수 있으므로 실제 닫힘을 확인한다.
		until := time.Now().Add(time.Duration(s.profile.TimeoutMS) * time.Millisecond)
		for c.IsConnectionOpen() && time.Now().Before(until) {
			time.Sleep(time.Millisecond)
		}
	}
}
func (s *mqttSession) close() {
	for id := range s.clients {
		s.disconnect(id)
	}
}

func (s *mqttSession) connect(ctx context.Context, id string) error {
	c := s.clients[id]
	if c == nil {
		return errors.New("배정되지 않은 Client입니다.")
	}
	if c.IsConnectionOpen() {
		return nil
	}
	before := func() error {
		if err := s.connectPacer.wait(ctx); err != nil {
			return err
		}
		if s.seen[id] && !s.parked[id] {
			s.stats.Reconnects++
		}
		// 주차 후 정상 재접속의 첫 시도와 장애 재시도를 구분한다.
		s.parked[id] = false
		return nil
	}
	if err := connectMQTT(ctx, c, s.profile, before, s.metrics.connect.add); err != nil {
		return err
	}
	if !s.seen[id] {
		s.stats.Connected++
		s.seen[id] = true
	}
	s.stats.Connections++
	s.snapshot()
	return nil
}

func (s *mqttSession) command(ctx context.Context, runID string, c streamCommand) error {
	switch c.Command {
	case "connect":
		return s.connect(ctx, c.ClientID)
	case "disconnect":
		if s.clients[c.ClientID] == nil {
			return errors.New("배정되지 않은 Client입니다.")
		}
		s.disconnect(c.ClientID)
		return nil
	case "publish":
		id := s.vehicles[c.VehicleID]
		if id == "" || s.stats.Attempts >= s.profile.MaxMessages {
			return errors.New("차량 또는 발행 상한을 확인하세요.")
		}
		m := c.Message
		ty, ok := m["ty"].(float64)
		_, payload := m["pld"].(map[string]any)
		_, timestamp := m["ts"].(float64)
		encoded, err := json.Marshal(m)
		if !ok || (ty != 3 && ty != 4) || !payload || !timestamp || len(m) != 3 || err != nil || len(encoded) > 131072 {
			return errors.New("OBD 본문을 확인하세요.")
		}
		if err = s.connect(ctx, id); err != nil {
			return err
		}
		if err = s.publishPacer.wait(ctx); err != nil {
			return err
		}
		s.stats.Attempts++
		started := time.Now()
		err = waitToken(ctx, s.clients[id].Publish(fmt.Sprintf("%s/%s/%s/obd", s.profile.TopicPrefix, runID, c.VehicleID), 1, false, encoded), time.Duration(s.profile.TimeoutMS)*time.Millisecond)
		s.metrics.ack.add(time.Since(started))
		if err != nil {
			s.stats.Unconfirmed++
			return errors.New("발행 응답이 미확인입니다. 자동 재발행하지 않습니다.")
		}
		s.stats.Published++
		s.stats.Puback++
		return nil
	default:
		return errors.New("지원하지 않는 연결 명령입니다.")
	}
}

// 한 명령·한 응답, 발행은 PUBACK까지 한 건만 처리한다. stdin EOF도 연결을 정리한다.
func serveMQTTStream(req Request, dec *json.Decoder, w io.Writer) {
	encoder := json.NewEncoder(w)
	s, err := newMQTTSession(req)
	if err != nil {
		encoder.Encode(map[string]any{"ok": false, "error": err.Error()})
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(req.MQTT.MaxDurationSeconds)*time.Second)
	defer cancel()
	defer s.close()
	respond := func(err error) {
		reply := map[string]any{"ok": err == nil, "stats": s.snapshot()}
		if err != nil {
			reply["error"] = err.Error()
		}
		encoder.Encode(reply)
	}
	commands := make(chan streamCommand)
	go func() {
		defer close(commands)
		for {
			var c streamCommand
			if dec.Decode(&c) != nil {
				cancel()
				return
			}
			if c.Command == "stop" {
				cancel()
				return
			}
			select {
			case commands <- c:
			case <-ctx.Done():
				return
			}
		}
	}()
	respond(nil)
	for {
		select {
		case <-ctx.Done():
			s.close()
			respond(errors.New("MQTT 실행이 중단되거나 제한시간이 끝났습니다."))
			return
		case c, ok := <-commands:
			if !ok {
				return
			}
			if c.Command == "close" {
				s.close()
				respond(nil)
				return
			}
			err := s.command(ctx, req.RunID, c)
			if err != nil {
				s.close()
			}
			respond(err)
			if err != nil {
				return
			}
		}
	}
}

// 재연결·속도 제한·응답 대기를 기존 소량 전송과 일정 전송에서 공통으로 사용한다.
func connectMQTT(ctx context.Context, c mqtt.Client, p *MQTTProfile, before func() error, measure func(time.Duration)) error {
	for attempt := 0; attempt <= p.ReconnectAttempts; attempt++ {
		if err := before(); err != nil {
			return err
		}
		started := time.Now()
		err := waitToken(ctx, c.Connect(), time.Duration(p.TimeoutMS)*time.Millisecond)
		measure(time.Since(started))
		if err == nil {
			return nil
		}
		c.Disconnect(0)
		if attempt < p.ReconnectAttempts {
			if err := sleepUntil(ctx, time.Now().Add(time.Duration(1<<attempt)*100*time.Millisecond)); err != nil {
				return err
			}
		}
	}
	return errors.New("MQTT 접속 또는 재연결에 실패했습니다.")
}
