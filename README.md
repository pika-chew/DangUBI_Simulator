# DangUBI 차량 데이터 시뮬레이터

OBD 차량 데이터를 생성하고 MQTT 연결 풀 또는 차량별 연결로 전송하는 시뮬레이터입니다.

## 로컬 실행

준비: `python scripts/dev.py prepare`

실행: `python scripts/dev.py run`

브라우저: http://127.0.0.1:8080

[사용법·기능·제한](docs/USAGE.md)

## 구성

- `apps/web`: React + TypeScript 관리 화면
- `services/control`: FastAPI 제어 API
- `services/worker`: Go 생성·전송 Worker
- `contracts`: 개발용 OBD JSON Schema
- `scripts`: 개발 환경 준비·빌드·테스트

수동 OBD 입력·상태 기반 자동 주행·Trip 집계·고정 배정 확인과 TLS MQTT 소량 전송을 지원합니다. 기본값은 송신 없음이며, 실제 송신에는 서버의 인증서·토픽·시험 상한 설정과 화면 확인이 필요합니다. OBD 개발 프로필은 실제 수신 시스템의 규격과 확인이 필요합니다. 자동 주행은 실차 보정 전의 단일 운행 근사 모델입니다. 다중 운행·장시간 부하·분산 실행은 후속 범위입니다.

실행 시작 즉시 ID를 받고 진행 상태를 조회하거나 일시중지·재개·중단할 수 있습니다. 실제 시간/가상시간 실행을 선택하며, 진행·중단 결과는 로컬 SQLite에 기록합니다. 현재 단일 API 프로세스에서 비동기 실행 한 개만 지원합니다. 일시중지 중 시뮬레이션 상태를 보존하며 전체 안전 제한시간은 계속 적용합니다. 다중 운행과 대규모 장시간 실행은 후속입니다.

검증: `python scripts/dev.py test`
