package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
	"strings"
	"time"

	mqtt "github.com/eclipse/paho.mqtt.golang"
)

// 접속 정보는 서버에서만 받으며 결과·실행 기록에는 포함하지 않는다.
type MQTTProfile struct {
	LocalBenchmark     bool   `json:"local_benchmark,omitempty"`
	ExecutionProfile   string `json:"execution_profile,omitempty"`
	MaxConnecting      int    `json:"max_connecting,omitempty"`
	Enabled            bool   `json:"enabled"`
	ContractReviewed   bool   `json:"contract_reviewed"`
	BrokerURL          string `json:"broker_url"`
	CAFile             string `json:"ca_file"`
	CertFile           string `json:"cert_file"`
	KeyFile            string `json:"key_file"`
	TopicPrefix        string `json:"topic_prefix"`
	MaxClients         int    `json:"max_clients"`
	MaxMessages        int    `json:"max_messages"`
	MaxDurationSeconds int    `json:"max_duration_seconds"`
	ConnectPerSecond   int    `json:"connect_per_second"`
	PublishPerSecond   int    `json:"publish_per_second"`
	TimeoutMS          int    `json:"timeout_ms"`
	ReconnectAttempts  int    `json:"reconnect_attempts"`
	// 발행 응답 지연·연결 끊김 때 같은 메시지를 다시 보내며 버티는 시간(초). 0이면 재발행하지 않는다.
	RetryWindowSeconds int `json:"retry_window_seconds"`
}

func (p *MQTTProfile) validate() error {
	if p == nil || !p.Enabled || !p.ContractReviewed {
		return errors.New("서버의 MQTT 활성화와 수신 규격 확인이 필요합니다.")
	}
	u, err := url.Parse(p.BrokerURL)
	if err != nil || u.Scheme != "ssl" || u.Hostname() == "" || u.Port() == "" || u.User != nil || u.Path != "" || u.RawQuery != "" || u.Fragment != "" {
		return errors.New("MQTT 주소는 ssl://호스트:포트 형식이어야 합니다.")
	}
	maxClients, maxMessages, maxSeconds, maxConnect, maxPublish := 20, 100, 30, 5, 70
	if p.MaxConnecting < 0 || p.MaxConnecting > 1000 {
		return errors.New("접속 동시 처리 상한을 확인하세요.")
	}
	if p.ExecutionProfile != "" && p.ExecutionProfile != "small_test" && p.ExecutionProfile != "operations" {
		return errors.New("실행 프로필을 확인하세요.")
	}
	if p.ExecutionProfile == "operations" {
		if p.LocalBenchmark || p.MaxConnecting < 1 || p.MaxConnecting > 1000 {
			return errors.New("운영 접속 동시 처리 상한을 확인하세요.")
		}
		maxClients, maxMessages, maxSeconds, maxConnect, maxPublish = 25000, 1000000000000, 2592600, 1000, 20000
	}
	if p.LocalBenchmark {
		if u.Hostname() != "127.0.0.1" && u.Hostname() != "::1" {
			return errors.New("로컬 측정은 루프백 IP에만 연결할 수 있습니다.")
		}
		maxClients, maxMessages, maxSeconds, maxConnect, maxPublish = 1000, 10000, 120, 200, 5000
		if parkingBenchmarkEnabled && p.MaxDurationSeconds > 120 {
			maxClients, maxMessages, maxSeconds, maxConnect, maxPublish = 8, 128, 1880, 5, 70
		}
	}
	if p.MaxClients < 1 || p.MaxClients > maxClients || p.MaxMessages < 1 || p.MaxMessages > maxMessages || p.MaxDurationSeconds < 1 || p.MaxDurationSeconds > maxSeconds || p.ConnectPerSecond < 1 || p.ConnectPerSecond > maxConnect || p.PublishPerSecond < 1 || p.PublishPerSecond > maxPublish || p.TimeoutMS < 100 || p.TimeoutMS > 5000 || p.ReconnectAttempts < 0 || p.ReconnectAttempts > 3 || p.RetryWindowSeconds < 0 || p.RetryWindowSeconds > 3600 {
		return errors.New("소량 MQTT 시험의 연결·건수·시간·속도 상한을 확인하세요.")
	}
	if p.TopicPrefix == "" || len(p.TopicPrefix) > 80 || strings.ContainsAny(p.TopicPrefix, "+#\x00") || strings.HasPrefix(p.TopicPrefix, "$") || strings.Count(p.TopicPrefix, "/") > 3 || strings.HasSuffix(p.TopicPrefix, "/") {
		return errors.New("시험 토픽 접두어를 확인하세요.")
	}
	return nil
}

func (p *MQTTProfile) tlsConfig() (*tls.Config, error) {
	roots := x509.NewCertPool()
	data, err := os.ReadFile(p.CAFile)
	if err != nil || !roots.AppendCertsFromPEM(data) {
		return nil, errors.New("CA 파일을 읽거나 확인할 수 없습니다.")
	}
	cert, err := tls.LoadX509KeyPair(p.CertFile, p.KeyFile)
	if err != nil {
		return nil, errors.New("인증서와 개인 키를 읽거나 확인할 수 없습니다.")
	}
	return &tls.Config{MinVersion: tls.VersionTLS12, RootCAs: roots, Certificates: []tls.Certificate{cert}}, nil
}

type pacer struct {
	next time.Time
	step time.Duration
}

func (p *pacer) wait(ctx context.Context) error {
	delay := time.Until(p.next)
	if delay > 0 {
		t := time.NewTimer(delay)
		defer t.Stop()
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-t.C:
		}
	}
	if ctx.Err() != nil {
		return ctx.Err()
	}
	p.next = time.Now().Add(p.step)
	return nil
}
func waitToken(ctx context.Context, token mqtt.Token, limit time.Duration) error {
	timer := time.NewTimer(limit)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return context.DeadlineExceeded
	case <-token.Done():
		return token.Error()
	}
}
func sleepUntil(ctx context.Context, target time.Time) error {
	delay := time.Until(target)
	if delay <= 0 {
		return ctx.Err()
	}
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

func runMQTT(req Request) (Result, error) { return runMQTTContext(context.Background(), req, nil) }

func runMQTTContext(parent context.Context, req Request, progress func(Result)) (Result, error) {
	var err error
	req, err = normalize(req)
	if err != nil {
		return Result{}, err
	}
	profile := req.MQTT
	if err := profile.validate(); err != nil {
		return Result{}, err
	}
	copyReq := req
	copyReq.Operation = "plan"
	out, err := process(copyReq)
	if err != nil {
		return Result{}, err
	}
	if out.AllocatedClients > profile.MaxClients || out.Planned > profile.MaxMessages {
		return Result{}, errors.New("서버에 설정된 실제 송신 상한을 초과했습니다.")
	}
	// 입력은 송신 전에 검증하며 인증서 검증을 비활성화하지 않는다.
	tlsConfig, err := profile.tlsConfig()
	if err != nil {
		return Result{}, err
	}
	ctx, cancel := context.WithTimeout(parent, time.Duration(profile.MaxDurationSeconds)*time.Second)
	defer cancel()
	out.Transport = "mqtt"
	out.Transmission = &TransmissionSummary{Mode: "serial", MaxInFlight: 1}
	if req.Config.PublishMode == "parallel" {
		out.Transmission.Mode = "parallel"
		out.Transmission.MaxInFlight = effectiveInFlight(req.Config)
		out.Transmission.QueueCapacity = effectiveQueueCapacity(req.Config)
	}
	out.State = "mqtt_running"
	if req.Generation != nil {
		out.ModelVersion = modelVersion(req.Generation)
	}
	started := time.Now()
	control := controlFrom(ctx)
	metrics := performanceMetrics{}
	baseReport := reporter(started, progress, control)
	report := func(out *Result, force bool) {
		out.Performance = metrics.summary(out.Puback, time.Since(started))
		baseReport(out, force)
	}
	report(&out, true)
	out.Published = 0
	out.Puback = 0
	out.Attempted = 0
	out.Errors = []string{}
	for _, samples := range [][]Sample{out.Samples, out.TripSummaries} {
		for i := range samples {
			samples[i].Topic = fmt.Sprintf("%s/%s/%s/obd", profile.TopicPrefix, req.RunID, samples[i].VehicleID)
		}
	}
	clients := map[string]mqtt.Client{}
	seen := map[string]bool{}
	remaining := map[string]int{}
	for _, a := range out.Assignments {
		remaining[a.ClientID] += req.Config.MessagesPerVehicle
	}
	connectPacer := pacer{step: time.Second / time.Duration(profile.ConnectPerSecond)}
	publishPacer := pacer{step: time.Second / time.Duration(profile.PublishPerSecond)}
	timeout := time.Duration(profile.TimeoutMS) * time.Millisecond
	connect := func(id string, client mqtt.Client) error {
		before := func() error {
			if req.Config.PublishMode == "parallel" && out.Transmission.InFlight > 0 {
				if control.pauseRequested() {
					return errPausePending
				}
			} else {
				if err := control.gate(ctx, func(force bool) { report(&out, force) }); err != nil {
					return err
				}
			}
			if err := connectPacer.wait(ctx); err != nil {
				return err
			}
			if req.Config.PublishMode == "parallel" && out.Transmission.InFlight > 0 && control.pauseRequested() {
				return errPausePending
			}
			if seen[id] {
				out.Reconnects++
			}
			return nil
		}
		if err := connectMQTT(ctx, client, profile, before, metrics.connect.add); err != nil {
			return err
		}
		if !seen[id] {
			out.Connected++
			seen[id] = true
		}
		out.ActiveClients = 0
		for _, c := range clients {
			if c.IsConnectionOpen() {
				out.ActiveClients++
			}
		}
		out.PeakActiveClients = max(out.PeakActiveClients, out.ActiveClients)
		report(&out, true)
		return nil
	}

	for _, a := range out.Assignments {
		if _, ok := clients[a.ClientID]; ok {
			continue
		}
		clients[a.ClientID] = newMQTTClient(profile, a.ClientID, tlsConfig)
	}
	fail := func(message string) {
		if errors.Is(ctx.Err(), context.Canceled) {
			out.State = "stopped"
		} else {
			out.State = "mqtt_failed"
			out.Errors = append(out.Errors, message)
		}
	}
	finish := func() Result {
		for _, client := range clients {
			client.Disconnect(0)
		}
		out.ActiveClients = 0
		report(&out, true)
		return out
	}
	if req.Config.Lifetime == "always" {
		for _, a := range out.Assignments {
			client := clients[a.ClientID]
			if seen[a.ClientID] {
				continue
			}
			if err := connect(a.ClientID, client); err != nil {
				fail("MQTT 접속을 완료하지 못했습니다. 인증·정책·접속 주소·시험 제한을 확인하세요.")
				return finish(), nil
			}
		}
	}
	scheduledStart := time.Now().Add(-time.Duration(pauseMilliseconds(control)) * time.Millisecond)
	if req.Config.PublishMode == "parallel" {
		return runConcurrentMQTT(ctx, cancel, req, &out, &metrics, clients, remaining, scheduledStart, connect, report, fail, finish)
	}
	for seq := 0; seq < req.Config.MessagesPerVehicle; seq++ {
		if err := waitSchedule(ctx, scheduledStart.Add(time.Duration(offsetMS(req, seq))*time.Millisecond), func(force bool) { report(&out, force) }); err != nil {
			fail("시험 제한시간을 초과했습니다.")
			return finish(), nil
		}
		for i, a := range out.Assignments {
			client := clients[a.ClientID]
			if !client.IsConnectionOpen() {
				if err := connect(a.ClientID, client); err != nil {
					fail("MQTT 연결 또는 재연결에 실패했습니다.")
					return finish(), nil
				}
			}
			if err := publishPacer.wait(ctx); err != nil {
				fail("시험 제한시간을 초과했습니다.")
				return finish(), nil
			}
			if err := control.gate(ctx, func(force bool) { report(&out, force) }); err != nil {
				fail("실행을 중단했습니다.")
				return finish(), nil
			}
			generationStarted := time.Now()
			sample := sampleAt(req, i, seq, a, profile.TopicPrefix)
			if err := recordSample(&out, req, sample, i, seq); err != nil {
				return finish(), err
			}
			encoded, _ := json.Marshal(sample.Message)
			metrics.generation.add(time.Since(generationStarted))
			delayMS := max(int64(0), time.Since(scheduledStart).Milliseconds()-pauseMilliseconds(control)-offsetMS(req, seq))
			out.ScheduleDelayMS = max(out.ScheduleDelayMS, delayMS)
			metrics.schedule.add(time.Duration(delayMS) * time.Millisecond)
			topic := sample.Topic
			if ctx.Err() != nil {
				fail("실행을 중단했습니다.")
				return finish(), nil
			}
			out.Attempted++
			out.Transmission.InFlight = 1
			out.Transmission.PeakInFlight = 1
			report(&out, true)
			ackStarted := time.Now()
			err := waitToken(ctx, client.Publish(topic, 1, false, encoded), timeout)
			metrics.ack.add(time.Since(ackStarted))
			out.Transmission.InFlight = 0
			if err != nil {
				out.Unconfirmed++
				fail("발행 응답을 확인하지 못했습니다. 중복 가능성이 있어 자동 재발행하지 않습니다.")
				return finish(), nil
			}
			out.Published++
			out.Puback++
			report(&out, true)
			remaining[a.ClientID]--
			if req.Config.Lifetime == "while_driving" && remaining[a.ClientID] == 0 {
				client.Disconnect(0)
			}
			out.ActiveClients = 0
			for _, c := range clients {
				if c.IsConnectionOpen() {
					out.ActiveClients++
				}
			}
			report(&out, false)
		}
	}
	out.State = "mqtt_completed"
	return finish(), nil
}
