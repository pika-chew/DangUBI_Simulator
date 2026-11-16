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

검증: `python scripts/dev.py test`
