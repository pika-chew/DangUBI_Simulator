package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/rand/v2"
	"regexp"
	"sync"
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
	ID         int64           `json:"id"`
	Command    string          `json:"command"`
	ClientID   string          `json:"client_id,omitempty"`
	VehicleID  string          `json:"vehicle_id,omitempty"`
	Message    map[string]any  `json:"message,omitempty"`
	MessageRaw json.RawMessage `json:"message_raw,omitempty"`
	DueAtMS    int64           `json:"due_at_ms,omitempty"`
}

type streamStats struct {
	PublishRateWaitMS      float64                     `json:"publish_rate_wait_ms"`
	PublishRateOvershootMS float64                     `json:"publish_rate_overshoot_ms"`
	Connected              int                         `json:"connected_clients"`
	Active                 int                         `json:"active_clients"`
	Peak                   int                         `json:"peak_active_clients"`
	Attempts               int                         `json:"publish_attempts"`
	Published              int                         `json:"mqtt_published"`
	Puback                 int                         `json:"puback_count"`
	Unconfirmed            int                         `json:"unconfirmed_messages"`
	Resent                 int                         `json:"resent_messages"`
	RetryWaits             int                         `json:"publish_retry_count"`
	Reconnects             int                         `json:"reconnect_count"`
	Connections            int                         `json:"connection_count"`
	Disconnections         int                         `json:"disconnect_count"`
	ScheduleDelay          int64                       `json:"schedule_delay_ms"`
	InitialConnectMS       *int64                      `json:"initial_connect_ms,omitempty"`
	PostConnectSchedule    DurationSummary             `json:"post_connect_schedule"`
	PublishWindows         map[int]DurationSummary     `json:"publish_windows,omitempty"`
	ParkingConnects        map[string][]parkingConnect `json:"parking_connects,omitempty"`
	ParkingFirstSends      map[string][]parkingSend    `json:"parking_first_sends,omitempty"`
	Performance            *PerformanceSummary         `json:"performance"`
}

type mqttSession struct {
	connectSlots                 chan struct{}
	clientConnect, clientPublish map[string]*pacer
	online                       map[string]bool
	lastScan                     time.Time
	parkingTrace                 *parkingTrace
	connectedAt                  time.Time
	postConnect                  durationMetric
	publishWindows               [12]durationMetric
	mu, connectMu, publishMu     sync.Mutex
	parked                       map[string]bool
	profile                      *MQTTProfile
	clients                      map[string]mqtt.Client
	vehicles                     map[string]string
	seen                         map[string]bool
	stats                        streamStats
	metrics                      performanceMetrics
	connectPacer, publishPacer   pacer
	publishLimiter               *publishLimiter
	started                      time.Time
	persistent                   bool
	envelope                     bool
	validID                      *regexp.Regexp
}

type parkingConnect struct {
	StartedMS  int64   `json:"started_ms"`
	DurationMS float64 `json:"duration_ms"`
	OfflineMS  int64   `json:"offline_ms"`
}

type parkingSend struct {
	ClientID    string  `json:"client_id"`
	TimestampMS float64 `json:"timestamp_ms"`
	DelayMS     int64   `json:"delay_ms"`
}

type parkingTrace struct {
	connects   map[string][]parkingConnect
	firstSends map[string][]parkingSend
	closedAt   map[string]time.Time
	lastTrip   map[string]float64
}

func copyParkingRecords[T any](records map[string][]T) map[string][]T {
	out := make(map[string][]T, len(records))
	for key, items := range records {
		out[key] = append([]T(nil), items...)
	}
	return out
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
	maxAssignments := 100
	if req.VehicleEnvelope {
		maxAssignments = 1000
	}
	if p.ExecutionProfile == "operations" {
		maxAssignments = 25000
	}
	if p.LocalBenchmark {
		maxAssignments = 1000
	}
	minimum := 1
	if req.Persistent {
		// 상시 운영은 운영 프로필에서만 허용하며 차량 0대로 시작할 수 있다.
		if p.ExecutionProfile != "operations" || len(req.Clients) < 1 || len(req.Clients) > p.MaxClients {
			return nil, errors.New("상시 운영 Client 풀을 확인하세요.")
		}
		minimum = 0
	} else if len(req.Clients) > 0 {
		return nil, errors.New("Client 풀 선언은 상시 운영에서만 사용합니다.")
	}
	if !validID.MatchString(req.RunID) || len(req.Assignments) < minimum || len(req.Assignments) > maxAssignments {
		return nil, errors.New("실행/차량 배정을 확인하세요.")
	}
	s := &mqttSession{profile: p, clients: map[string]mqtt.Client{}, vehicles: map[string]string{}, seen: map[string]bool{}, parked: map[string]bool{}, started: time.Now(), persistent: req.Persistent, envelope: req.Persistent || req.VehicleEnvelope, validID: validID}
	if p.MaxConnecting > 0 {
		s.connectSlots = make(chan struct{}, p.MaxConnecting)
	}
	if p.ExecutionProfile == "operations" {
		s.clientConnect = map[string]*pacer{}
		s.clientPublish = map[string]*pacer{}
		s.online = map[string]bool{}
	}
	s.connectPacer.step = time.Second / time.Duration(p.ConnectPerSecond)
	if parkingBenchmarkEnabled && p.LocalBenchmark && p.MaxDurationSeconds > 120 {
		s.parkingTrace = &parkingTrace{make(map[string][]parkingConnect), make(map[string][]parkingSend), make(map[string]time.Time), make(map[string]float64)}
	}
	s.publishPacer.step = time.Second / time.Duration(p.PublishPerSecond)
	if p.ExecutionProfile == "operations" && p.PublishPerSecond > 1000 {
		s.publishLimiter = newPublishLimiter(p.PublishPerSecond)
	}
	addClient := func(id string) {
		s.clients[id] = newMQTTClient(p, id, config)
		if p.ExecutionProfile == "operations" {
			s.clientConnect[id] = &pacer{step: time.Second}
			s.clientPublish[id] = &pacer{step: time.Second / 100}
		}
	}
	for _, id := range req.Clients {
		if !validID.MatchString(id) || s.clients[id] != nil {
			return nil, errors.New("상시 운영 Client 풀을 확인하세요.")
		}
		addClient(id)
	}
	for _, a := range req.Assignments {
		if !validID.MatchString(a.VehicleID) || !validID.MatchString(a.ClientID) || s.vehicles[a.VehicleID] != "" || (s.persistent && s.clients[a.ClientID] == nil) {
			return nil, errors.New("고정 배정표를 확인하세요.")
		}
		s.vehicles[a.VehicleID] = a.ClientID
		if s.clients[a.ClientID] == nil {
			s.clients[a.ClientID] = newMQTTClient(p, a.ClientID, config)
			if p.ExecutionProfile == "operations" {
				s.clientConnect[a.ClientID] = &pacer{step: time.Second}
				s.clientPublish[a.ClientID] = &pacer{step: time.Second / 100}
			}
		}
	}
	if len(s.clients) > p.MaxClients {
		return nil, errors.New("Client 상한을 초과했습니다.")
	}
	return s, nil
}

func (s *mqttSession) snapshot() streamStats {
	s.mu.Lock()
	defer s.mu.Unlock()
	// 운영 규모에서는 응답마다 전체 연결을 다시 훑지 않는다. 비동기 단절은 주기적으로 보정한다.
	if s.profile.ExecutionProfile != "operations" || time.Since(s.lastScan) >= 250*time.Millisecond {
		s.stats.Active = 0
		for id, c := range s.clients {
			open := c.IsConnectionOpen()
			if s.online != nil {
				s.online[id] = open
			}
			if open {
				s.stats.Active++
			}
		}
		s.lastScan = time.Now()
	}
	s.stats.Peak = max(s.stats.Peak, s.stats.Active)
	s.stats.Performance = s.metrics.summary(s.stats.Puback, time.Since(s.started))
	s.stats.PostConnectSchedule = s.postConnect.summary()
	if s.parkingTrace != nil {
		s.stats.ParkingConnects = copyParkingRecords(s.parkingTrace.connects)
		s.stats.ParkingFirstSends = copyParkingRecords(s.parkingTrace.firstSends)
	}
	if s.profile.LocalBenchmark {
		s.stats.PublishWindows = make(map[int]DurationSummary)
		for i := range s.publishWindows {
			if s.publishWindows[i].count > 0 {
				s.stats.PublishWindows[i*10] = s.publishWindows[i].summary()
			}
		}
	}
	return s.stats
}

// 각 명령의 실제 완료 카운터는 즉시 전달하고 상세 히스토그램은 주기적으로 전달한다.
func (s *mqttSession) counts() map[string]any {
	s.mu.Lock()
	defer s.mu.Unlock()
	v := s.stats
	return map[string]any{
		"connected_clients": v.Connected, "active_clients": v.Active, "peak_active_clients": v.Peak,
		"publish_attempts": v.Attempts, "mqtt_published": v.Published, "puback_count": v.Puback,
		"unconfirmed_messages": v.Unconfirmed, "reconnect_count": v.Reconnects,
		"resent_messages": v.Resent, "publish_retry_count": v.RetryWaits,
		"connection_count": v.Connections, "disconnect_count": v.Disconnections,
		"schedule_delay_ms": v.ScheduleDelay, "initial_connect_ms": v.InitialConnectMS,
	}
}

// dropConnection은 응답이 끊긴 연결을 닫아 다음 발행에서 새로 접속하게 한다(주차 해제가 아니므로 재접속으로 센다).
func (s *mqttSession) dropConnection(id string) {
	c := s.clients[id]
	if c == nil {
		return
	}
	c.Disconnect(0)
	until := time.Now().Add(time.Duration(s.profile.TimeoutMS) * time.Millisecond)
	for c.IsConnectionOpen() && time.Now().Before(until) {
		time.Sleep(time.Millisecond)
	}
	s.refreshClient(id)
}

func (s *mqttSession) disconnect(id string) {
	if c := s.clients[id]; c != nil {
		s.mu.Lock()
		s.parked[id] = true
		wasOpen := c.IsConnectionOpen()
		if wasOpen {
			s.stats.Disconnections++
		}
		s.mu.Unlock()
		c.Disconnect(0)
		// Paho의 해제 처리가 비동기로 끝날 수 있으므로 실제 닫힘을 확인한다.
		until := time.Now().Add(time.Duration(s.profile.TimeoutMS) * time.Millisecond)
		for c.IsConnectionOpen() && time.Now().Before(until) {
			time.Sleep(time.Millisecond)
		}
		if s.parkingTrace != nil && wasOpen && !c.IsConnectionOpen() {
			s.mu.Lock()
			s.parkingTrace.closedAt[id] = time.Now()
			s.mu.Unlock()
		}
		s.refreshClient(id)
	}
}
func (s *mqttSession) close() {
	if s.profile.ExecutionProfile == "operations" {
		until := time.Now().Add(time.Duration(s.profile.TimeoutMS) * time.Millisecond)
		for id, c := range s.clients {
			s.mu.Lock()
			s.parked[id] = true
			if c.IsConnectionOpen() {
				s.stats.Disconnections++
			}
			s.mu.Unlock()
			c.Disconnect(0)
		}
		for time.Now().Before(until) {
			open := false
			for _, c := range s.clients {
				if c.IsConnectionOpen() {
					open = true
					break
				}
			}
			if !open {
				break
			}
			time.Sleep(time.Millisecond)
		}
		s.mu.Lock()
		s.lastScan = time.Time{}
		s.mu.Unlock()
		return
	}
	for id := range s.clients {
		s.disconnect(id)
	}
	s.mu.Lock()
	s.lastScan = time.Time{}
	s.mu.Unlock()
}

func (s *mqttSession) refreshClient(id string) {
	if s.online == nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	open := s.clients[id].IsConnectionOpen()
	if open != s.online[id] {
		if open {
			s.stats.Active++
		} else {
			s.stats.Active--
		}
		s.online[id] = open
	}
	s.stats.Peak = max(s.stats.Peak, s.stats.Active)
}

func (s *mqttSession) connect(ctx context.Context, id string) error {
	c := s.clients[id]
	if c == nil {
		return errors.New("배정되지 않은 Client입니다.")
	}
	if c.IsConnectionOpen() {
		return nil
	}
	if s.connectSlots != nil {
		select {
		case s.connectSlots <- struct{}{}:
		case <-ctx.Done():
			return ctx.Err()
		}
		defer func() { <-s.connectSlots }()
	}
	before := func() error {
		if p := s.clientConnect[id]; p != nil {
			if err := p.wait(ctx); err != nil {
				return err
			}
		}
		s.connectMu.Lock()
		err := s.connectPacer.wait(ctx)
		s.connectMu.Unlock()
		if err != nil {
			return err
		}
		s.mu.Lock()
		defer s.mu.Unlock()
		if s.seen[id] && !s.parked[id] {
			s.stats.Reconnects++
		}
		// 주차 후 정상 재접속의 첫 시도와 장애 재시도를 구분한다.
		s.parked[id] = false
		return nil
	}
	connectStarted := time.Now()
	if err := connectMQTT(ctx, c, s.profile, before, func(d time.Duration) { s.mu.Lock(); s.metrics.connect.add(d); s.mu.Unlock() }); err != nil {
		return err
	}
	s.mu.Lock()
	if !s.seen[id] {
		s.stats.Connected++
		s.seen[id] = true
	}
	s.stats.Connections++
	if trace := s.parkingTrace; trace != nil && len(trace.connects[id]) < 2 {
		offline := int64(0)
		if closed := trace.closedAt[id]; !closed.IsZero() {
			offline = connectStarted.Sub(closed).Milliseconds()
		}
		trace.connects[id] = append(trace.connects[id], parkingConnect{
			connectStarted.Sub(s.started).Milliseconds(), float64(time.Since(connectStarted)) / float64(time.Millisecond), offline,
		})
	}
	if len(s.seen) == len(s.clients) && s.connectedAt.IsZero() {
		s.connectedAt = time.Now()
		elapsed := time.Since(s.started).Milliseconds()
		s.stats.InitialConnectMS = &elapsed
	}
	s.mu.Unlock()
	s.refreshClient(id)
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
		s.mu.Lock()
		id := s.vehicles[c.VehicleID]
		s.mu.Unlock()
		if id == "" {
			return errors.New("차량 또는 발행 상한을 확인하세요.")
		}
		identity := ""
		if s.envelope {
			identity = c.VehicleID
		}
		encoded, ty, timestamp, tid, err := streamPayloadFor(c, s.parkingTrace != nil, identity)
		if err != nil {
			return err
		}
		// 한 번의 발행 시도. 반환: (발행했는지, 다시 시도할 만한 실패인지, 오류)
		attempt := func(first bool) (bool, bool, error) {
			if err := s.connect(ctx, id); err != nil {
				return false, ctx.Err() == nil, err
			}
			if p := s.clientPublish[id]; p != nil {
				if err := p.wait(ctx); err != nil {
					return false, false, err
				}
			}
			// 모든 Client가 같은 속도 제한을 공유한다. 토큰 대기는 잠금 밖이다.
			s.publishMu.Lock()
			waitStarted := time.Now()
			expectedWait := max(time.Duration(0), time.Until(s.publishPacer.next))
			if s.publishLimiter != nil {
				expectedWait, err = s.publishLimiter.wait(ctx)
			} else {
				err = s.publishPacer.wait(ctx)
			}
			waited := time.Since(waitStarted)
			s.mu.Lock()
			s.stats.PublishRateWaitMS += float64(waited) / float64(time.Millisecond)
			s.stats.PublishRateOvershootMS += float64(max(time.Duration(0), waited-expectedWait)) / float64(time.Millisecond)
			s.mu.Unlock()
			if err != nil {
				s.publishMu.Unlock()
				return false, false, err
			}
			s.mu.Lock()
			if s.stats.Attempts >= s.profile.MaxMessages {
				s.mu.Unlock()
				s.publishMu.Unlock()
				return false, false, errors.New("발행 상한을 초과했습니다.")
			}
			s.stats.Attempts++
			if c.DueAtMS > 0 && first {
				delay := max(int64(0), time.Now().UnixMilli()-c.DueAtMS)
				if trace := s.parkingTrace; trace != nil && ty == 4 && len(trace.firstSends[c.VehicleID]) < 2 {
					if tid != trace.lastTrip[c.VehicleID] {
						trace.lastTrip[c.VehicleID] = tid
						trace.firstSends[c.VehicleID] = append(trace.firstSends[c.VehicleID], parkingSend{id, timestamp, delay})
					}
				}
				if s.profile.LocalBenchmark && s.parkingTrace == nil {
					window := min(len(s.publishWindows)-1, int(time.Since(s.started)/(10*time.Second)))
					s.publishWindows[window].add(time.Duration(delay) * time.Millisecond)
				}
				s.stats.ScheduleDelay = max(s.stats.ScheduleDelay, delay)
				s.metrics.schedule.add(time.Duration(delay) * time.Millisecond)
				if !s.connectedAt.IsZero() && c.DueAtMS >= s.connectedAt.UnixMilli() {
					s.postConnect.add(time.Duration(delay) * time.Millisecond)
				}
			}
			s.mu.Unlock()
			started := time.Now()
			token := s.clients[id].Publish(fmt.Sprintf("%s/%s/%s/obd", s.profile.TopicPrefix, runID, c.VehicleID), 1, false, encoded)
			s.publishMu.Unlock()
			err := waitToken(ctx, token, time.Duration(s.profile.TimeoutMS)*time.Millisecond)
			s.mu.Lock()
			defer s.mu.Unlock()
			s.metrics.ack.add(time.Since(started))
			if err != nil {
				return true, ctx.Err() == nil, err
			}
			s.stats.Published++
			s.stats.Puback++
			return true, false, nil
		}
		// 응답 지연·연결 끊김은 retry_window_seconds 동안 재접속 뒤 같은 메시지를 다시 보낸다(QoS1 중복 가능, 건수 기록).
		// 0이면 이전처럼 재발행하지 않고 멈춘다.
		window := time.Duration(s.profile.RetryWindowSeconds) * time.Second
		until := time.Now().Add(window)
		for n := 0; ; n++ {
			published, retry, err := attempt(n == 0)
			if err == nil {
				return nil
			}
			if !retry || window <= 0 || !time.Now().Before(until) {
				if published {
					s.mu.Lock()
					s.stats.Unconfirmed++
					s.mu.Unlock()
					if window > 0 {
						return fmt.Errorf("발행 응답을 %d초 동안 재시도했지만 확인하지 못했습니다.", int(window.Seconds()))
					}
					return errors.New("발행 응답이 미확인입니다. 자동 재발행하지 않습니다.")
				}
				return err
			}
			s.mu.Lock()
			if published {
				s.stats.Resent++
			}
			s.stats.RetryWaits++
			s.mu.Unlock()
			s.dropConnection(id)
			delay := min(30*time.Second, time.Second<<min(n, 5)) + time.Duration(rand.Int64N(int64(250*time.Millisecond)))
			if err := sleepUntil(ctx, time.Now().Add(delay)); err != nil {
				return err
			}
		}
	default:
		return errors.New("지원하지 않는 연결 명령입니다.")
	}
}

// 상시 운영에서만 실행 중 차량을 기존 Client에 붙이거나 뗀다. Client 소유는 바뀌지 않는다.
// 진행 중 발행은 이미 Client를 확정했으므로 떼어도 그 발행은 끝까지 처리된다.
func (s *mqttSession) reassign(c streamCommand) error {
	if !s.persistent {
		return errors.New("차량 배정 변경은 상시 운영에서만 사용합니다.")
	}
	if !s.validID.MatchString(c.VehicleID) {
		return errors.New("차량 식별자를 확인하세요.")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if c.Command == "attach" {
		if s.clients[c.ClientID] == nil || s.vehicles[c.VehicleID] != "" || len(s.vehicles) >= 25000 {
			return errors.New("붙일 차량·Client를 확인하세요.")
		}
		s.vehicles[c.VehicleID] = c.ClientID
		return nil
	}
	if s.vehicles[c.VehicleID] == "" || c.ClientID != "" {
		return errors.New("뗄 차량을 확인하세요.")
	}
	delete(s.vehicles, c.VehicleID)
	return nil
}

// 고정 Client마다 한 명령만 실행한다. 서로 다른 Client의 응답은 ID로 구분한다.
// 실패·EOF·중단 시 모든 진행 명령을 집계한 뒤 연결을 정리한다.
func serveMQTTStream(req Request, input io.Reader, w io.Writer) {
	encoder := json.NewEncoder(w)
	s, err := newMQTTSession(req)
	if err != nil {
		encoder.Encode(map[string]any{"ok": false, "error": err.Error()})
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(req.MQTT.MaxDurationSeconds)*time.Second)
	defer cancel()
	defer s.close()
	lastDetails := time.Time{}
	respond := func(id int64, err error, closed ...bool) {
		full := req.MQTT.ExecutionProfile != "operations" || id == 0 || err != nil || len(closed) > 0 || time.Since(lastDetails) >= 100*time.Millisecond
		var stats any
		if full {
			stats = s.snapshot()
			lastDetails = time.Now()
		} else {
			stats = s.counts()
		}
		reply := map[string]any{"id": id, "ok": err == nil, "stats": stats, "stats_complete": full}
		if id == 0 && err == nil && len(closed) == 0 {
			reply["capabilities"] = map[string]bool{"raw_message": true}
		}
		if len(closed) > 0 {
			reply["closed"] = closed[0]
		}
		if err != nil {
			reply["error"] = err.Error()
		}
		if encoder.Encode(reply) != nil {
			cancel()
		}
	}
	commands := make(chan streamCommand)
	go func() {
		defer close(commands)
		scanner := bufio.NewScanner(input)
		scanner.Buffer(make([]byte, 4096), 262145)
		for scanner.Scan() {
			if len(bytes.TrimSpace(scanner.Bytes())) == 0 {
				continue
			}
			var c streamCommand
			dec := json.NewDecoder(bytes.NewReader(scanner.Bytes()))
			dec.DisallowUnknownFields()
			if dec.Decode(&c) != nil {
				cancel()
				return
			}
			if dec.Decode(&struct{}{}) != io.EOF {
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
		cancel()
	}()
	type completion struct {
		id     int64
		client string
		err    error
	}
	completed := make(chan completion, len(s.clients))
	busy := map[string]bool{}
	active := 0
	lastID := int64(0)
	shutdown := func(id int64, err error) {
		cancel()
		for active > 0 {
			done := <-completed
			active--
			respond(done.id, done.err)
		}
		s.close()
		respond(id, err, true)
	}
	respond(0, nil)
	for {
		select {
		case <-ctx.Done():
			shutdown(0, errors.New("MQTT 실행이 중단되거나 제한시간이 끝났습니다."))
			return
		case done := <-completed:
			active--
			delete(busy, done.client)
			if done.err != nil {
				shutdown(done.id, done.err)
				return
			}
			respond(done.id, nil)
		case c, ok := <-commands:
			if !ok {
				shutdown(0, errors.New("MQTT 명령 입력이 종료되었습니다."))
				return
			}
			if c.ID <= lastID {
				shutdown(c.ID, errors.New("MQTT 명령 ID 순서를 확인하세요."))
				return
			}
			lastID = c.ID
			if c.Command == "close" {
				shutdown(c.ID, nil)
				return
			}
			if c.Command == "attach" || c.Command == "detach" {
				if err := s.reassign(c); err != nil {
					shutdown(c.ID, err)
					return
				}
				respond(c.ID, nil)
				continue
			}
			id := c.ClientID
			if c.Command == "publish" {
				id = s.vehicles[c.VehicleID]
			}
			if s.clients[id] == nil || busy[id] || (c.Command != "publish" && c.Command != "connect" && c.Command != "disconnect") {
				shutdown(c.ID, errors.New("Client별 명령 순서 또는 고정 배정을 확인하세요."))
				return
			}
			busy[id] = true
			active++
			go func(c streamCommand, id string) { completed <- completion{c.ID, id, s.command(ctx, req.RunID, c)} }(c, id)
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
			delay := time.Duration(1<<attempt) * 100 * time.Millisecond
			if p.ExecutionProfile == "operations" {
				delay = time.Duration(1<<attempt)*time.Second + time.Duration(rand.Int64N(int64(250*time.Millisecond)))
			}
			if err := sleepUntil(ctx, time.Now().Add(delay)); err != nil {
				return err
			}
		}
	}
	return errors.New("MQTT 접속 또는 재연결에 실패했습니다.")
}
