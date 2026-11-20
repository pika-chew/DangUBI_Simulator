package main

import (
	"bytes"
	"encoding/json"
	"reflect"
	"strings"
	"testing"
)

func TestStreamRawPayloadPreservesBytesAndLegacyValues(t *testing.T) {
	for _, raw := range []string{
		`{"ty":4,"ts":1795129202000,"pld":{"tid":123,"sp":3.25,"em":256}}`,
		` { "pld":{"tid":123,"xyz":"한글","sp":1e-7}, "ts":2, "ty":4.0 } `,
		`{"ty":3,"ts":3,"pld":{"tid":123}}`,
	} {
		for _, trace := range []bool{false, true} {
			prepared, ty, ts, tid, err := streamPayload(streamCommand{MessageRaw: json.RawMessage(raw)}, trace)
			if err != nil || !bytes.Equal(prepared, []byte(raw)) {
				t.Fatal("원문 유지", err)
			}
			var m map[string]any
			json.Unmarshal([]byte(raw), &m)
			legacy, lty, lts, ltid, err := streamPayload(streamCommand{Message: m}, trace)
			var a, b any
			json.Unmarshal(prepared, &a)
			json.Unmarshal(legacy, &b)
			if err != nil || !reflect.DeepEqual(a, b) || ty != lty || ts != lts || trace && tid != ltid {
				t.Fatal("기존 경로 값 불일치", err)
			}
		}
	}
}

func TestStreamRawPayloadRejectsMalformedAndAmbiguousBodies(t *testing.T) {
	for _, raw := range []string{
		`null`, `[]`, `{}`, `{"ty":4,"ts":null,"pld":{}}`,
		`{"ty":true,"ts":1,"pld":{}}`, `{"ty":5,"ts":1,"pld":{}}`,
		`{"ty":4,"ts":1,"pld":[]}`, `{"ty":4,"ts":1,"pld":null}`,
		`{"ty":4,"ts":1,"pld":{},"extra":1}`,
		`{"ty":4,"ty":3,"ts":1,"pld":{}}`,
		`{"ty":4,"ts":1,"pld":{}} {}`,
		`{"ty":4,"ts":1e400,"pld":{}}`,
		"{\"ty\":4,\"ts\":1,\"pld\":{\"x\":\"\xff\"}}",
		`{"ty":4,"ts":1,"pld":{"x":"` + strings.Repeat("x", 131072) + `"}}`,
	} {
		if _, _, _, _, err := streamPayload(streamCommand{MessageRaw: json.RawMessage(raw)}, false); err == nil {
			t.Fatal("잘못된 본문 허용", raw[:min(len(raw), 60)])
		}
	}
	if _, _, _, _, err := streamPayload(streamCommand{MessageRaw: json.RawMessage(`{"ty":4,"ts":1,"pld":{}}`), Message: map[string]any{}}, false); err == nil {
		t.Fatal("두 본문 동시 허용")
	}
}

func TestStreamRawPayloadOwnsDecodedFrame(t *testing.T) {
	frame := []byte(`{"id":1,"command":"publish","message_raw":{"ty":4,"ts":1,"pld":{}}}`)
	var command streamCommand
	if err := json.Unmarshal(frame, &command); err != nil {
		t.Fatal(err)
	}
	for i := range frame {
		frame[i] = 'x'
	}
	raw, _, _, _, err := streamPayload(command, false)
	if err != nil || string(raw) != `{"ty":4,"ts":1,"pld":{}}` {
		t.Fatal("수신 버퍼 재사용이 본문을 변경함", err)
	}
}
