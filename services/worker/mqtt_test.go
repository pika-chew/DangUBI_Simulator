package main

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/binary"
	"encoding/json"
	"encoding/pem"
	"hash"
	"math"
	"math/big"
	"net"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/eclipse/paho.mqtt.golang/packets"
)

type testBroker struct {
	listener        net.Listener
	mu              sync.Mutex
	sockets         []net.Conn
	ids             map[string]bool
	published       int
	messages        []map[string]any
	noAck           bool
	closeOnce       bool
	closed          bool
	ackDelay        time.Duration
	slowSuffix      string
	discardPayload  bool
	handling        int
	peakHandling    int
	receivedAt      []time.Time
	receivedIDs     []string
	receivedTopics  []string
	measureDigest   bool
	fingerprint     [32]byte
	lastTimestamp   map[string]float64
	lastKind        map[string]float64
	invalidMeasured int
	outOfOrder      int
}

func makeCertificate(t *testing.T, template, parent *x509.Certificate, key, parentKey *ecdsa.PrivateKey) []byte {
	t.Helper()
	der, err := x509.CreateCertificate(rand.Reader, template, parent, &key.PublicKey, parentKey)
	if err != nil {
		t.Fatal(err)
	}
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
}
func localBroker(t *testing.T, noAck, closeOnce bool) (*testBroker, *MQTTProfile) {
	t.Helper()
	dir := t.TempDir()
	now := time.Now()
	caKey, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	ca := &x509.Certificate{SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "simulator-test-ca"}, NotBefore: now.Add(-time.Hour), NotAfter: now.Add(time.Hour), IsCA: true, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature}
	caPEM := makeCertificate(t, ca, ca, caKey, caKey)
	caFile := filepath.Join(dir, "ca.pem")
	os.WriteFile(caFile, caPEM, 0600)
	key, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	keyDER, _ := x509.MarshalECPrivateKey(key)
	keyPEM := pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: keyDER})
	server := &x509.Certificate{SerialNumber: big.NewInt(2), DNSNames: []string{"localhost"}, IPAddresses: []net.IP{net.ParseIP("127.0.0.1")}, NotBefore: now.Add(-time.Hour), NotAfter: now.Add(time.Hour), KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}}
	client := &x509.Certificate{SerialNumber: big.NewInt(3), NotBefore: now.Add(-time.Hour), NotAfter: now.Add(time.Hour), KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth}}
	serverCert, _ := tls.X509KeyPair(makeCertificate(t, server, ca, key, caKey), keyPEM)
	certFile := filepath.Join(dir, "client.crt")
	keyFile := filepath.Join(dir, "client.key")
	os.WriteFile(certFile, makeCertificate(t, client, ca, key, caKey), 0600)
	os.WriteFile(keyFile, keyPEM, 0600)
	roots := x509.NewCertPool()
	roots.AppendCertsFromPEM(caPEM)
	listener, err := tls.Listen("tcp", "127.0.0.1:0", &tls.Config{Certificates: []tls.Certificate{serverCert}, MinVersion: tls.VersionTLS12, ClientCAs: roots, ClientAuth: tls.RequireAndVerifyClientCert})
	if err != nil {
		t.Fatal(err)
	}
	b := &testBroker{listener: listener, ids: map[string]bool{}, noAck: noAck, closeOnce: closeOnce}
	go func() {
		for {
			conn, err := listener.Accept()
			if err != nil {
				return
			}
			b.mu.Lock()
			if b.closed {
				conn.Close()
				b.mu.Unlock()
				return
			}
			b.sockets = append(b.sockets, conn)
			b.mu.Unlock()
			go b.handle(conn)
		}
	}()
	t.Cleanup(func() {
		b.mu.Lock()
		b.closed = true
		b.listener.Close()
		for _, c := range b.sockets {
			c.Close()
		}
		b.mu.Unlock()
	})
	return b, &MQTTProfile{Enabled: true, ContractReviewed: true, BrokerURL: "ssl://" + listener.Addr().String(), CAFile: caFile, CertFile: certFile, KeyFile: keyFile, TopicPrefix: "sim-test", MaxClients: 5, MaxMessages: 20, MaxDurationSeconds: 10, ConnectPerSecond: 5, PublishPerSecond: 70, TimeoutMS: 500, ReconnectAttempts: 0}
}
func (b *testBroker) handle(conn net.Conn) {
	defer conn.Close()
	clientID := ""
	for {
		packet, err := packets.ReadPacket(conn)
		if err != nil {
			return
		}
		switch p := packet.(type) {
		case *packets.ConnectPacket:
			clientID = p.ClientIdentifier
			b.mu.Lock()
			b.ids[p.ClientIdentifier] = true
			b.mu.Unlock()
			ack := packets.NewControlPacket(packets.Connack).(*packets.ConnackPacket)
			ack.ReturnCode = 0
			ack.Write(conn)
		case *packets.PublishPacket:
			b.mu.Lock()
			b.published++
			b.handling++
			b.peakHandling = max(b.peakHandling, b.handling)
			if !b.discardPayload {
				b.receivedAt = append(b.receivedAt, time.Now())
				b.receivedIDs = append(b.receivedIDs, clientID)
				b.receivedTopics = append(b.receivedTopics, p.TopicName)
			}
			var message map[string]any
			if (!b.discardPayload || b.measureDigest) && json.Unmarshal(p.Payload, &message) == nil {
				if !b.discardPayload {
					b.messages = append(b.messages, message)
				}
				if b.measureDigest {
					b.measureMessage(p.TopicName, message)
				}
			}
			closeNow := b.closeOnce
			ackDelay := b.ackDelay
			if b.slowSuffix != "" && !strings.HasSuffix(clientID, b.slowSuffix) {
				ackDelay = 0
			}
			noAck := b.noAck
			b.closeOnce = false
			b.mu.Unlock()
			if ackDelay > 0 {
				time.Sleep(ackDelay)
			}
			b.mu.Lock()
			b.handling--
			b.mu.Unlock()
			if !noAck {
				ack := packets.NewControlPacket(packets.Puback).(*packets.PubackPacket)
				ack.MessageID = p.MessageID
				ack.Write(conn)
			}
			if closeNow {
				return
			}
		case *packets.PingreqPacket:
			packets.NewControlPacket(packets.Pingresp).Write(conn)
		case *packets.DisconnectPacket:
			return
		}
	}
}

// JSON 수치 표기 차이를 없앤 개발 측정용 지문. 원문을 보관하지 않는다.
func measurementValue(h hash.Hash, v any) {
	switch x := v.(type) {
	case nil:
		h.Write([]byte{'n'})
	case bool:
		if x {
			h.Write([]byte{'t'})
		} else {
			h.Write([]byte{'f'})
		}
	case float64:
		h.Write([]byte{'d'})
		if x == 0 {
			x = 0
		}
		binary.Write(h, binary.BigEndian, math.Float64bits(x))
	case string:
		h.Write([]byte{'s'})
		binary.Write(h, binary.BigEndian, uint32(len([]byte(x))))
		h.Write([]byte(x))
	case []any:
		h.Write([]byte{'a'})
		binary.Write(h, binary.BigEndian, uint32(len(x)))
		for _, v := range x {
			measurementValue(h, v)
		}
	case map[string]any:
		h.Write([]byte{'o'})
		binary.Write(h, binary.BigEndian, uint32(len(x)))
		keys := make([]string, 0, len(x))
		for k := range x {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		for _, k := range keys {
			measurementValue(h, k)
			measurementValue(h, x[k])
		}
	}
}

func (b *testBroker) measureMessage(topic string, m map[string]any) {
	if b.lastTimestamp == nil {
		b.lastTimestamp = map[string]float64{}
		b.lastKind = map[string]float64{}
	}
	ts, tok := m["ts"].(float64)
	ty, yok := m["ty"].(float64)
	if !tok || !yok || len(m) != 3 || (ty != 3 && ty != 4) {
		b.invalidMeasured++
		return
	}
	if _, ok := b.lastTimestamp[topic]; !ok && len(b.lastTimestamp) >= 1000 {
		b.invalidMeasured++
		return
	}
	if previous, ok := b.lastTimestamp[topic]; ok && (ts < previous || b.lastKind[topic] == 3) {
		b.outOfOrder++
	}
	b.lastTimestamp[topic] = ts
	b.lastKind[topic] = ty
	h := sha256.New()
	measurementValue(h, m)
	digest := h.Sum(nil)
	carry := 0
	for i := 31; i >= 0; i-- {
		n := int(b.fingerprint[i]) + int(digest[i]) + carry
		b.fingerprint[i] = byte(n)
		carry = n >> 8
	}
}
func TestMQTTTLSModes(t *testing.T) {
	for _, mode := range []string{"shared", "dedicated"} {
		t.Run(mode, func(t *testing.T) {
			broker, profile := localBroker(t, false, false)
			clients := 2
			if mode == "dedicated" {
				clients = 3
			}
			req := request(mode, clients)
			req.Operation = "mqtt"
			req.MQTT = profile
			req.Config.IntervalMS = 30
			req.Config.Lifetime = "while_driving"
			out, err := runMQTT(req)
			if err != nil {
				t.Fatal(err)
			}
			if out.State != "mqtt_completed" || out.Puback != 6 || out.Published != 6 || out.Connected != clients || out.ActiveClients != 0 {
				t.Fatalf("실제 TLS 연결·발행 결과 불일치: %+v", out)
			}
			if out.Performance == nil || out.Performance.Ack.Count != 6 || out.Performance.Generation.Count != 6 || out.Performance.Schedule.Count != 6 || out.Performance.Connect.Count != int64(clients) || out.Performance.AckPerSecond <= 0 {
				t.Fatal("전송 성능 집계 누락", out.Performance)
			}
			broker.mu.Lock()
			defer broker.mu.Unlock()
			if broker.published != 6 || len(broker.ids) != clients {
				t.Fatal("브로커 관측 수량 불일치")
			}
		})
	}
}

func TestAutomaticMQTTIncludesFinalTrip(t *testing.T) {
	for _, version := range []string{scenarioVersion, routeVersion, koreaVersion, powertrainVersion, eventVersion} {
		t.Run(version, func(t *testing.T) { testAutomaticMQTTIncludesFinalTrip(t, version) })
	}
}

func testAutomaticMQTTIncludesFinalTrip(t *testing.T, version string) {
	broker, profile := localBroker(t, false, false)
	profile.MaxMessages = 100
	profile.MaxDurationSeconds = 15
	req := automaticRequest()
	req.Generation.ModelVersion = version
	req.Operation = "mqtt"
	req.MQTT = profile
	req.Config.VehicleCount = 2
	req.Config.ClientCount = 1
	req.Config.IntervalMS = 1000
	req.Config.Lifetime = "while_driving"
	req.Generation.DurationSeconds = 10
	out, err := runMQTT(req)
	if err != nil {
		t.Fatal(err)
	}
	if out.Puback != 24 || out.State != "mqtt_completed" || out.Connected != 1 || out.TripCount != 2 {
		t.Fatalf("자동 MQTT 전송 실패: %+v", out)
	}
	broker.mu.Lock()
	defer broker.mu.Unlock()
	if len(broker.messages) != 24 {
		t.Fatal("실제 발행 건수 불일치")
	}
	for _, m := range broker.messages {
		if len(m) != 3 || m["ty"] == nil || m["ts"] == nil || m["pld"] == nil {
			t.Fatal("OBD 밖 정보가 MQTT에 포함됨", m)
		}
		for _, key := range []string{"context", "road", "reason", "driver_style", "weather", "event", "gear", "engine_state", "wheel_power_kw", "fuel_rate_lph", "coolant_c"} {
			if m["pld"].(map[string]any)[key] != nil {
				t.Fatal("내부 상황이 payload에 포함됨", key)
			}
		}
	}

	for i := 22; i < 24; i++ {
		m := broker.messages[i]
		if m["ty"] != float64(3) || m["ts"] != float64(req.Generation.StartTimeMS+10000) {
			t.Fatal("마지막 Trip 또는 운행 종료 시각 누락")
		}
	}
}
func TestMQTTUnconfirmedAndLimits(t *testing.T) {
	_, profile := localBroker(t, true, false)
	req := request("shared", 2)
	req.Operation = "mqtt"
	req.MQTT = profile
	req.Config.IntervalMS = 1
	out, err := runMQTT(req)
	if err != nil {
		t.Fatal(err)
	}
	if out.State != "mqtt_failed" || out.Unconfirmed != 1 || out.Puback != 0 || out.Attempted != 1 {
		t.Fatal("미확인 메시지를 성공으로 집계함")
	}
	profile.MaxMessages = 1
	if _, err := runMQTT(req); err == nil {
		t.Fatal("실제 송신 상한 누락")
	}
	profile.MaxMessages = 20
	profile.BrokerURL = "tcp://localhost:1883"
	if _, err := runMQTT(req); err == nil {
		t.Fatal("암호화 없는 연결 허용")
	}
}
func TestMQTTReconnectSameID(t *testing.T) {
	broker, profile := localBroker(t, false, true)
	req := request("shared", 1)
	req.Config.VehicleCount = 1
	req.Config.IntervalMS = 300
	req.Operation = "mqtt"
	req.MQTT = profile
	out, err := runMQTT(req)
	if err != nil {
		t.Fatal(err)
	}
	if out.Puback != 2 || out.Reconnects < 1 {
		t.Fatalf("고정 ID 재연결 실패: %+v", out)
	}
	broker.mu.Lock()
	defer broker.mu.Unlock()
	if len(broker.ids) != 1 {
		t.Fatal("재연결 시 ID 변경")
	}
}

func TestMQTTRejectsWrongCAAndHonorsDeadline(t *testing.T) {
	_, profile := localBroker(t, false, false)
	req := request("shared", 1)
	req.Config.VehicleCount = 1
	req.Operation = "mqtt"
	req.MQTT = profile
	profile.CAFile = profile.CertFile
	out, err := runMQTT(req)
	if err != nil {
		t.Fatal(err)
	}
	if out.State != "mqtt_failed" || out.Connected != 0 || out.Published != 0 {
		t.Fatal("인증서 검증 실패를 허용함")
	}
	_, profile = localBroker(t, true, false)
	profile.MaxDurationSeconds = 1
	profile.TimeoutMS = 5000
	req.MQTT = profile
	started := time.Now()
	out, err = runMQTT(req)
	if err != nil {
		t.Fatal(err)
	}
	if out.State != "mqtt_failed" || time.Since(started) > 3*time.Second || out.Unconfirmed != 1 {
		t.Fatal("시험 제한시간이 적용되지 않음")
	}
}

// 환경변수를 지정한 경우에만 실행하는 화면 통합 시험용 로컬 브로커.
func TestMQTTBrowserFixture(t *testing.T) {
	path := os.Getenv("DANGUBI_BROWSER_TEST_PROFILE")
	if path == "" {
		t.Skip("화면 통합 시험에서만 실행")
	}
	_, profile := localBroker(t, false, false)
	profile.MaxDurationSeconds = 15
	profile.TimeoutMS = 3000
	data, _ := json.Marshal(profile)
	if err := os.WriteFile(path, data, 0600); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(3 * time.Minute)
	for time.Now().Before(deadline) {
		if _, err := os.Stat(path + ".done"); err == nil {
			return
		}
		time.Sleep(100 * time.Millisecond)
	}
	t.Fatal("화면 통합 시험 종료 신호 대기시간 초과")
}
