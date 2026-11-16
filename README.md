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

현재 첫 실행본은 실제 송신 없는 입력·생성·고정 배정 확인을 지원합니다. 실제 MQTT 전송 및 연결 관리는 개발 중입니다. OBD 개발 프로필은 실제 수신 시스템의 규격과 확인이 필요합니다.

검증: `python scripts/dev.py test`
