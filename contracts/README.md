# 공통 계약

OBD JSON Schema와 UI/API/Worker가 함께 사용할 계약의 위치입니다.

개발 기준은 OBD 타입 3/4입니다. 문서 표·예제의 차이는 실제 수신 규격과 확인하기 전 확정하지 않습니다. 특히 em, xyz, 단위·배율은 전송 호환성 확인 항목입니다. 제품 사용 안내는 ../docs/USAGE.md를 참고하세요.

상시 운영 전송 본문은 `envelopes.operations`를 따릅니다. 루트는 `{vehicleId, ty, ts, pld}` 순서이며 `vehicleId`는 송신 VIN(기본 가명 VIN, Vault 도입 전 임시 모드 `DANGUBI_OPERATION_VIN_MODE=synthetic_raw`에서는 시뮬레이터 합성 VIN)으로 토픽 `{prefix}/ops-main/{vehicleId}/obd`의 차량 식별자와 같습니다. 시험 실행 본문도 `envelopes.trial`에 따라 `{vehicleId, ty, ts, pld}`이며, `vehicleId`는 가명처리하지 않은 시험 VIN(공장 코드 `T`)으로 토픽 `{prefix}/{run_id}/{vehicleId}/obd`의 차량 식별자와 같습니다. `profiles`는 봉투 안의 `ty·ts·pld` 규격입니다.
