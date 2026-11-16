package main

import "testing"

func request(mode string, clients int) Request {
	return Request{Operation: "dry_run", RunID: "test-01", Config: Config{VehicleCount: 3, ClientCount: clients, MappingMode: mode, MessagesPerVehicle: 2, IntervalMS: 2000, Lifetime: "always"}, Message: map[string]any{"ty": float64(4), "ts": float64(1000), "pld": map[string]any{"tid": float64(7), "lat": 37.5, "lon": 127.0, "clt": float64(1000)}}}
}
func TestSharedAndDedicated(t *testing.T) {
	shared, err := process(request("shared", 2))
	if err != nil {
		t.Fatal(err)
	}
	if shared.Assignments[0].ClientID != shared.Assignments[2].ClientID || shared.Assignments[0].ClientID == shared.Assignments[1].ClientID {
		t.Fatal("공유 배정이 올바르지 않음")
	}
	dedicated, err := process(request("dedicated", 3))
	if err != nil {
		t.Fatal(err)
	}
	ids := map[string]bool{}
	for _, a := range dedicated.Assignments {
		ids[a.ClientID] = true
	}
	if len(ids) != 3 {
		t.Fatal("전용 배정 충돌")
	}
	if shared.Processed != 6 || dedicated.Processed != 6 || shared.Published != 0 || shared.Connected != 0 || shared.Puback != 0 {
		t.Fatal("송신 없는 결과 수량 불일치")
	}
	if shared.Samples[3].Message["ts"] != float64(3000) {
		t.Fatal("가상시간 갱신 실패")
	}
	if request("shared", 2).Message["ts"] != float64(1000) {
		t.Fatal("원본 변경")
	}
}
func TestInvalidAndBounded(t *testing.T) {
	for _, req := range []Request{request("dedicated", 2), request("shared", 4), request("unknown", 1)} {
		if _, err := process(req); err == nil {
			t.Fatal("잘못된 설정 허용")
		}
	}
	req := request("shared", 2)
	req.Config.MessagesPerVehicle = 10000
	if _, err := process(req); err == nil {
		t.Fatal("건수 제한 누락")
	}
	req = request("shared", 2)
	req.Operation = "mqtt"
	if _, err := process(req); err == nil {
		t.Fatal("실제 송신 경로 허용")
	}
	req = request("shared", 2)
	req.Operation = "preview"
	out, err := process(req)
	if err != nil || out.Processed != 0 {
		t.Fatal("미리보기가 처리 실행")
	}
}
