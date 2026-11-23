package main

import (
	"encoding/json"
	"io"
	"testing"
)

func TestPersistentPayloadRequiresLeadingMatchingVehicleID(t *testing.T) {
	alias := "PS64C36B1HDA81N43"
	valid := `{"vehicleId":"PS64C36B1HDA81N43","ty":4,"ts":1000,"pld":{"tid":7}}`
	raw, ty, ts, _, err := streamPayloadFor(streamCommand{MessageRaw: json.RawMessage(valid)}, false, alias)
	if err != nil || string(raw) != valid || ty != 4 || ts != 1000 {
		t.Fatal("상시 운영 본문을 원문 그대로 통과시켜야 함", err)
	}
	for name, body := range map[string]string{
		"다른 차량":        `{"vehicleId":"PSOTHER","ty":4,"ts":1000,"pld":{}}`,
		"맨 앞이 아님":      `{"ty":4,"vehicleId":"PS64C36B1HDA81N43","ts":1000,"pld":{}}`,
		"누락":           `{"ty":4,"ts":1000,"pld":{}}`,
		"문자열 아님":       `{"vehicleId":7,"ty":4,"ts":1000,"pld":{}}`,
		"추가 필드":        `{"vehicleId":"PS64C36B1HDA81N43","ty":4,"ts":1000,"pld":{},"vin":"x"}`,
		"중복 vehicleId": `{"vehicleId":"PS64C36B1HDA81N43","vehicleId":"PS64C36B1HDA81N43","ty":4,"ts":1000,"pld":{}}`,
	} {
		if _, _, _, _, err := streamPayloadFor(streamCommand{MessageRaw: json.RawMessage(body)}, false, alias); err == nil {
			t.Fatal("거부해야 함:", name)
		}
	}
	// 필드 순서를 보장할 수 없는 map 입력은 상시 운영에서 받지 않는다.
	if _, _, _, _, err := streamPayloadFor(streamCommand{Message: map[string]any{"vehicleId": alias, "ty": float64(4), "ts": float64(1), "pld": map[string]any{}}}, false, alias); err == nil {
		t.Fatal("map 입력 거부")
	}
	// 시험 실행 계약은 그대로다: vehicleId가 있으면 거부한다.
	if _, _, _, _, err := streamPayload(streamCommand{MessageRaw: json.RawMessage(valid)}, false); err == nil {
		t.Fatal("시험 실행 본문에 vehicleId 거부")
	}
}

func persistentStream(t *testing.T, req Request) (*json.Encoder, func() map[string]any, func()) {
	t.Helper()
	input, send := io.Pipe()
	output, receive := io.Pipe()
	done := make(chan struct{})
	go func() { defer close(done); defer receive.Close(); serveMQTTStream(req, input, receive) }()
	decoder := json.NewDecoder(output)
	read := func() map[string]any {
		var reply map[string]any
		if err := decoder.Decode(&reply); err != nil {
			t.Fatal(err)
		}
		return reply
	}
	return json.NewEncoder(send), read, func() { send.Close(); <-done; output.Close(); input.Close() }
}

func TestPersistentSessionAttachPublishDetach(t *testing.T) {
	broker, profile := localBroker(t, false, false)
	profile.ExecutionProfile = "operations"
	profile.MaxConnecting = 10
	req := Request{Operation: "mqtt_stream", RunID: "ops-main", MQTT: profile, Persistent: true, Clients: []string{"sim-ops-main-c001", "sim-ops-main-c002"}}
	encoder, read, stop := persistentStream(t, req)
	defer stop()
	if r := read(); r["ok"] != true {
		t.Fatal("차량 0대로 시작해야 함", r)
	}
	expect := func(id int64, ok bool) {
		t.Helper()
		r := read()
		if r["id"] != float64(id) || r["ok"] != ok {
			t.Fatal(id, r)
		}
	}
	alias := "PS64C36B1HDA81N43"
	encoder.Encode(streamCommand{ID: 1, Command: "attach", VehicleID: alias, ClientID: "sim-ops-main-c002"})
	expect(1, true)
	body := json.RawMessage(`{"vehicleId":"` + alias + `","ty":4,"ts":1000,"pld":{"tid":7}}`)
	encoder.Encode(streamCommand{ID: 2, Command: "publish", VehicleID: alias, MessageRaw: body})
	expect(2, true)
	broker.mu.Lock()
	got := broker.messages[len(broker.messages)-1]
	broker.mu.Unlock()
	if got["vehicleId"] != alias {
		t.Fatal("발행 본문에 가명 VIN", got)
	}
	encoder.Encode(streamCommand{ID: 3, Command: "detach", VehicleID: alias})
	expect(3, true)
	// 뗀 차량의 발행은 세션 오류로 끝난다.
	encoder.Encode(streamCommand{ID: 4, Command: "publish", VehicleID: alias, MessageRaw: body})
	if r := read(); r["ok"] != false {
		t.Fatal("뗀 차량 발행 거부", r)
	}
}

func TestPersistentSessionRejectsInvalidSetupAndReassignment(t *testing.T) {
	_, profile := localBroker(t, false, false)
	small := *profile
	if _, err := newMQTTSession(Request{RunID: "ops-main", MQTT: &small, Persistent: true, Clients: []string{"c1"}}); err == nil {
		t.Fatal("운영 프로필이 아니면 상시 운영 거부")
	}
	profile.ExecutionProfile = "operations"
	profile.MaxConnecting = 10
	profile.MaxClients = 1
	for name, req := range map[string]Request{
		"Client 없음":    {RunID: "ops-main", MQTT: profile, Persistent: true},
		"Client 상한":    {RunID: "ops-main", MQTT: profile, Persistent: true, Clients: []string{"c1", "c2"}},
		"Client 중복":    {RunID: "ops-main", MQTT: profile, Persistent: true, Clients: []string{"c1", "c1"}},
		"선언 밖 배정":      {RunID: "ops-main", MQTT: profile, Persistent: true, Clients: []string{"c1"}, Assignments: []Assignment{{"v1", "c9"}}},
		"시험에 Client 풀": {RunID: "run", MQTT: profile, Clients: []string{"c1"}, Assignments: []Assignment{{"v1", "c1"}}},
	} {
		if _, err := newMQTTSession(req); err == nil {
			t.Fatal("거부해야 함:", name)
		}
	}
	fixed, err := newMQTTSession(Request{RunID: "run", MQTT: profile, Assignments: []Assignment{{"v1", "c1"}}})
	if err != nil || fixed.reassign(streamCommand{Command: "attach", VehicleID: "v2", ClientID: "c1"}) == nil {
		t.Fatal("시험 실행은 실행 중 배정 고정", err)
	}
	s, err := newMQTTSession(Request{RunID: "ops-main", MQTT: profile, Persistent: true, Clients: []string{"c1"}})
	if err != nil {
		t.Fatal(err)
	}
	for name, c := range map[string]streamCommand{
		"없는 Client":  {Command: "attach", VehicleID: "v1", ClientID: "c9"},
		"잘못된 ID":     {Command: "attach", VehicleID: "v/1", ClientID: "c1"},
		"없는 차량 떼기":   {Command: "detach", VehicleID: "v1"},
		"떼기에 Client": {Command: "detach", VehicleID: "v1", ClientID: "c1"},
	} {
		if s.reassign(c) == nil {
			t.Fatal("거부해야 함:", name)
		}
	}
	if s.reassign(streamCommand{Command: "attach", VehicleID: "v1", ClientID: "c1"}) != nil || s.reassign(streamCommand{Command: "attach", VehicleID: "v1", ClientID: "c1"}) == nil {
		t.Fatal("중복 붙이기 거부")
	}
}

func TestTrialEnvelopeRequiresVINVehicleIDWithFixedAssignments(t *testing.T) {
	broker, profile := localBroker(t, false, false)
	vin := "0DSY7UZL3ST000001"
	req := Request{Operation: "mqtt_stream", RunID: "trial-run", MQTT: profile, VehicleEnvelope: true, Assignments: []Assignment{{vin, "sim-trial-run-c001"}}}
	encoder, read, stop := persistentStream(t, req)
	defer stop()
	if r := read(); r["ok"] != true {
		t.Fatal(r)
	}
	encoder.Encode(streamCommand{ID: 1, Command: "publish", VehicleID: vin, MessageRaw: json.RawMessage(`{"vehicleId":"` + vin + `","ty":4,"ts":1000,"pld":{"tid":7}}`)})
	if r := read(); r["ok"] != true {
		t.Fatal("시험 VIN 본문 발행", r)
	}
	broker.mu.Lock()
	got := broker.messages[len(broker.messages)-1]
	broker.mu.Unlock()
	if got["vehicleId"] != vin {
		t.Fatal(got)
	}
	// 시험 실행도 실행 중 배정은 고정이며 vehicleId 없는 본문은 거부한다.
	encoder.Encode(streamCommand{ID: 2, Command: "publish", VehicleID: vin, MessageRaw: json.RawMessage(`{"ty":4,"ts":2000,"pld":{"tid":7}}`)})
	if r := read(); r["ok"] != false {
		t.Fatal("vehicleId 누락 거부", r)
	}
}
