"""상시 운영 차량의 운행 조회. 운행 이력 한 행(초기 상태·당시 설정)으로 그 운행만 다시 생성한다.

- 메시지 원문을 저장하지 않으므로 조회 결과는 '재생성'이다. 브로커 수신 기록이 아니다.
- 생성 입력은 OperationGenerator._payload와 같다(같은 Worker면 같은 본문). 당시 Worker와 다르면 경고한다.
- 원본 VIN은 쓰지 않는다. 생성 키는 내부 차량키, 표시는 가명 VIN.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

from services.control.operation_plan import vehicle_key

STATUS_LABELS = {"sending": "전송 중", "completed": "종료 Trip 확인", "interrupted": "중단 · 복구 대기", "lost": "손실 기록 후 제외"}


def payload(trip: dict, low: int, high: int, offset: int, limit: int) -> dict:
    settings, initial, seed = trip["settings"], trip["initial"], trip["seed"]
    vehicle, key = vehicle_key(trip["vehicle_id"]), trip["trip_key"]
    return dict(start_ms=trip["start_ms"], end_ms=trip["end_ms"], from_ms=low, until_ms=high,
                interval_ms=settings["interval_ms"], include_em=True, offset=offset, limit=limit, tid=int(key[:12], 16),
                seed=int(hashlib.sha256(f"{seed}|{key}".encode()).hexdigest()[:7], 16), profile=trip["road_profile"],
                lat=initial["lat"], lon=initial["lon"], odometer_m=initial["odometer_m"],
                continuity=dict(vehicle_seed=int(hashlib.sha256(f"{seed}|{vehicle}".encode()).hexdigest()[:7], 16),
                                coolant_c=initial["coolant_c"], heading_rad=initial["heading_rad"]),
                force_end=False, anomaly_eligible=trip["anomaly_eligible"],
                anomaly_events_per_hour=settings["anomaly_events_per_hour"], anomaly_intensity=settings["anomaly_intensity"])


def delivery(trip: dict) -> dict:
    """전송 증거 요약. 생성(송신 없음)과 PUBACK을 구분하고, 복구 구간은 중복 가능으로 표시한다."""
    mqtt = trip["transport"] == "mqtt"
    confirmed = trip["confirmed_ts"]
    if trip["status"] == "completed":
        text = "종료 Trip PUBACK 확인 · 브로커 수신 확인(후속 저장 성공은 아님)" if mqtt else "생성 완료 · 송신 없는 생성이라 외부 전송 없음"
    elif trip["status"] == "sending":
        text = ("전송 중 · " if mqtt else "생성 중 · ") + ("확인된 메시지 없음" if confirmed is None else "최근 체크포인트까지 확인")
    elif trip["status"] == "interrupted":
        text = "운영이 비정상 종료돼 멈춘 운행 · 다음 시작 때 확인된 메시지 다음부터 이어 보냄"
    else:
        text = "정리 보류에서 손실 기록 후 제외 · 확인 시각 이후 메시지는 보내지 않음"
    return {"status": trip["status"], "status_label": STATUS_LABELS[trip["status"]], "transport": trip["transport"],
            "confirmed_ts": confirmed, "description": text, "recoveries": trip["recoveries"],
            "duplicate_risk_after_ms": trip["resume_after_ms"] if trip["recoveries"] else None}


def generate(trip: dict, worker: Path, start_ms, end_ms, offset, limit, search_events=False, event_mask=0) -> dict:
    a, b = trip["start_ms"], trip["end_ms"]
    low = a if start_ms is None else start_ms
    high = b if end_ms is None else end_ms
    if not a <= low <= high <= b:
        raise ValueError("조회 구간은 선택 운행의 시작~종료 안에서 시작 ≤ 종료로 지정하세요.")
    request = payload(trip, low, high, offset, limit)
    if search_events:
        request.update(search_events=True, event_mask=event_mask)
    worker_hash = hashlib.sha256(Path(worker).read_bytes()).hexdigest()
    process = subprocess.run([str(worker)], input=json.dumps({"operation": "schedule_obd", "scheduled": request}).encode(),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    result = json.loads(process.stdout)
    if process.returncode or "scheduled" not in result:
        raise ValueError(result.get("error", "운행 OBD 재생성에 실패했습니다."))
    output = result["scheduled"]
    same_worker = trip["worker_hash"] == worker_hash if trip["worker_hash"] else None
    warnings = ["메시지 원문은 저장하지 않습니다. 저장된 초기 상태와 당시 설정으로 같은 운행을 다시 생성한 결과이며 브로커 수신 기록이 아닙니다."]
    if not trip.get("identity_recorded"):
        warnings.append("이 운행에 송신 식별자 기록이 없어 현재 VIN 모드를 기준으로 vehicleId를 표시합니다.")
    if same_worker is False:
        warnings.insert(0, "현재 Worker가 이 운행을 보낸 당시 Worker와 다릅니다. 재생성 값이 실제로 보낸 값과 다를 수 있습니다.")
    elif same_worker is None:
        warnings.insert(0, "이 운행의 당시 Worker 지문이 없어 재생성 값과 실제 전송 값의 일치를 확인할 수 없습니다.")
    return {"status": "generated_local_only", "vehicle_id": trip["vehicle_id"], "pseudonym": trip["pseudonym"], "vehicleId": trip.get("vehicleId"), "trip_key": trip["trip_key"],
            "model": output["model"], "event_search": output.get("event_search"), "initial_state": trip["initial"],
            "messages": output["messages"], "trace": output.get("trace", []), "microtrip_count": output["microtrip_count"],
            "has_more": offset + len(output["messages"]) < output["microtrip_count"], "trip": output.get("trip"),
            "synthetic_trip": output["synthetic_trip"], "same_worker": same_worker, "delivery": delivery(trip),
            "settings": {k: trip["settings"][k] for k in ("interval_ms", "anomaly_events_per_hour", "anomaly_intensity", "transport")},
            "warnings": warnings}
