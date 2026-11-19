# DangUBI 차량 데이터 시뮬레이터

OBD 차량 데이터를 생성하고 MQTT 연결 풀 또는 차량별 연결로 전송하는 시뮬레이터입니다.

## 로컬 실행

준비: `python scripts/dev.py prepare`

실행: `python scripts/dev.py run`

브라우저: http://127.0.0.1:8080

[사용법·기능·제한](docs/USAGE.md)

## 컨테이너

Linux 컨테이너 빌드: `python scripts/container.py build --image dangubi-simulator:local-check`

격리 실행 확인: `python scripts/smoke_container.py --image dangubi-simulator:local-check`

데이터 볼륨 연결과 ECR 업로드는 [컨테이너 사용 안내](docs/USAGE.md#컨테이너-실행과-ecr-이미지-관리)를 참고하세요. AWS 실행 환경은 별도로 선택하며 현재 제품의 전송 상한을 유지합니다.

## 구성

- `apps/web`: React + TypeScript 관리 화면
- `services/control`: FastAPI 제어 API
- `services/worker`: Go 생성·전송 Worker
- `contracts`: 개발용 OBD JSON Schema
- `scripts`: 개발 환경 준비·빌드·테스트

수동 OBD 입력·상태 기반 자동 주행·Trip 집계·고정 배정 확인과 TLS MQTT 소량 전송을 지원합니다. 기본값은 송신 없음이며, 실제 송신에는 서버의 인증서·토픽·시험 상한 설정과 화면 확인이 필요합니다. OBD 개발 프로필은 실제 수신 시스템의 규격과 확인이 필요합니다. 자동 주행 v2는 도로·교통 조건·운전자·노면에 따른 상태 반복, 회전 경로와 경사, 이상 대상 비율/발생 집계를 지원합니다. 차량 번호·시간 구간별 그래프를 조회할 수 있습니다. 실차 보정 전 단일 운행 근사 모델이며 v1 결과와 구분합니다. 일정 기반 다중 운행과 운영 프로필의25,000대·30일 설정/순차 생성 경로를 지원합니다. 최대 동시 전송량·장기 안정성 실측과 분산 실행은 별도 단계입니다.

실행 시작 즉시 ID를 받고 진행 상태를 조회하거나 일시중지·재개·중단할 수 있습니다. 실제 시간/가상시간 실행을 선택하며, 진행·중단 결과는 로컬 SQLite에 기록합니다. 현재 단일 API 프로세스에서 비동기 실행 한 개만 지원합니다. 일시중지 중 시뮬레이션 상태를 보존하며 전체 안전 제한시간은 계속 적용합니다. 운영 일정은 차량 배정을 한 번 계산하고 제한된 메시지 큐와 차량별 마지막 상태로 처리합니다. 실제 AWS 용량과30일 연속 안정성을 보장하지는 않습니다.

검증: `python scripts/dev.py test`
