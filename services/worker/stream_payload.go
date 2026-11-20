package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"unicode/utf8"
)

// 준비한 본문은 루트 형식을 확인한 뒤 그대로 발행한다. 일반 map 입력은 유지한다.
func streamPayload(c streamCommand, trace bool) ([]byte, float64, float64, float64, error) {
	fail := func() ([]byte, float64, float64, float64, error) {
		return nil, 0, 0, 0, errors.New("OBD 본문을 확인하세요.")
	}
	if len(c.MessageRaw) == 0 {
		m := c.Message
		ty, ok := m["ty"].(float64)
		ts, tok := m["ts"].(float64)
		payload, pok := m["pld"].(map[string]any)
		raw, err := json.Marshal(m)
		if !ok || !tok || !pok || (ty != 3 && ty != 4) || len(m) != 3 || err != nil || len(raw) > 131072 {
			return fail()
		}
		tid, _ := payload["tid"].(float64)
		return raw, ty, ts, tid, nil
	}
	if c.Message != nil || len(c.MessageRaw) > 131072 || !utf8.Valid(c.MessageRaw) {
		return fail()
	}
	raw := c.MessageRaw
	decoder := json.NewDecoder(bytes.NewReader(raw))
	start, err := decoder.Token()
	if err != nil || start != json.Delim('{') {
		return fail()
	}
	seen := map[string]bool{}
	var ty, ts, tid float64
	for decoder.More() {
		key, err := decoder.Token()
		if err != nil {
			return fail()
		}
		name, ok := key.(string)
		if !ok || seen[name] {
			return fail()
		}
		seen[name] = true
		switch name {
		case "ty":
			if decoder.Decode(&ty) != nil || (ty != 3 && ty != 4) {
				return fail()
			}
		case "ts":
			var number *float64
			if decoder.Decode(&number) != nil || number == nil {
				return fail()
			}
			ts = *number
		case "pld":
			var payload json.RawMessage
			if decoder.Decode(&payload) != nil {
				return fail()
			}
			body := bytes.TrimSpace(payload)
			if len(body) == 0 || body[0] != '{' {
				return fail()
			}
			if trace {
				var fields map[string]json.RawMessage
				if json.Unmarshal(payload, &fields) != nil {
					return fail()
				}
				_ = json.Unmarshal(fields["tid"], &tid)
			}
		default:
			return fail()
		}
	}
	end, err := decoder.Token()
	if err != nil || end != json.Delim('}') || len(seen) != 3 {
		return fail()
	}
	if decoder.Decode(new(any)) != io.EOF {
		return fail()
	}
	return raw, ty, ts, tid, nil
}
