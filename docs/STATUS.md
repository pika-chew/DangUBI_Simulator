# 현재 작업 현황

갱신일: 2026-11-16 (Asia/Seoul)

## 단계

**2단계 수동 입력·미리보기·API→Go Worker의 첫 로컬 실행본 구현 완료 / 실제 MQTT 송신 전**

2026-11-16 최신 구현: React/TypeScript 입력 화면, 공통 개발 JSON Schema, FastAPI 제어 API, Go Worker 생성 확인, 공유/전용 고정 배정, 로컬 실행 기록을 구현했다. npm과 Python 의존성을 프로젝트 내부에 설치하고 lock 파일에 버전을 고정했다. 자세한 사용법과 미구현 범위는 FIRST_RUN.md를 기준으로 한다.

2026-11-16 최신 확정: 공통 연결 엔진 및 연결 풀/차량별 전체 연결 비교를 1차 구현 범위에 포함한다. 연결 수·배정 방식·배정표는 실행 전 설정 후 고정, 실행 중 증감·재배정은 후속 기능이다. 고정된 유지 정책에 따른 접속/해제와 장애 재연결은 허용한다. DATA_FLOW.md에 흐름도를 작성하고 DESIGN·DECISIONS·IMPLEMENTATION·CONFIGURABLE_CONNECTION_MODEL을 일치시켰다. 이번 작업은 설계 기록 수정이며 기능 구현·AWS 전송은 하지 않았다.

2026-11-16 후속 확정: 코드 스택과 연결 풀 채택, AWS 최종 환경 추후 선택. 개발 기반 구성 시작. 기존 추가 검토 기록은 AWS_OPTIONS.md에 보관한다.

## 완료

- 빈 로컬 작업 폴더와 빈 GitHub 저장소 확인.
- 제공된 Notion 제안서 1~9절을 열려 있는 브라우저에서 읽음.
- Smart Fleet OBD와 AWS 공식 문서 확인. 표/예제 충돌과 MQTT 제한 기록.
- DESIGN v0.1, SOURCES, DECISIONS, WORKFLOW, README, AGENTS 작성.

## 실제 확인과 한계

- 첫 로컬 실행본: TypeScript/Vite 빌드, Go 필수 테스트, API 통합 테스트 10개 통과. 브라우저 입력·배정·Worker 처리 확인. 실제 MQTT 시험은 아직 하지 않음.
- 로컬 Git 초기화 및 GitHub origin 연결 완료. 초기 커밋 112a107을 main에 push 완료. 상시 감시 장치는 아직 설정하지 않음.
- VS Code 프로젝트 workspace 열기 명령 실행 완료. AWS 자원 생성·배포·실제 Publish는 수행하지 않음.
- Notion 원문은 수정하지 않음. 현재 설계 초안의 기준 문서는 로컬 DESIGN.md.
- 공유 기록은 같은 폴더/저장소를 읽는 작업에서 적용된다. 모든 별도 채팅을 자동 동기화하는 서비스는 아직 없음.

## 다음 작업

첫 실행본 사용자 검토 → 실제 MQTT 전송 어댑터·연결 수명 관리 구현. 실제 IoT 시험 전 수신 규격·토픽·접속 정보를 확인하고, AWS 부하 실행 전 목표·예산을 확정한다. 상태 기반 자동 생성과 실제 TPS 제어는 후속 단계다.

## 이번 산출물과 확인

- 이번 구현 산출물: apps/web React 화면·package-lock, services/control FastAPI·requirements.lock·통합 테스트, services/worker Go 엔진·테스트, contracts/obd-development.json, scripts/dev.py와 VS Code 작업. .local에 실행 기록·빌드 결과를 격리했다. 실제 연결·발행·PUBACK 0을 화면에서 구분하며 연결 유지 정책은 현재 기록만 한다.

- 사용자 확정 사항을 작업 규칙에도 반영하고 DATA_FLOW.md를 가로 방향(LR)으로 정리했다. 연결 설정 고정과 허용된 실행 중 설정 변경의 경계를 DESIGN에 명시했다. 문서 간 범위를 대조했으며 이번 산출물은 설계 흐름도다. 본체 구현·AWS 전송은 수행하지 않았다.

- 공통 연결 엔진 검토안 작성: CONFIGURABLE_CONNECTION_MODEL.md. 차량과 Client 배정 분리, 실행별 Client 수 지정으로 공유/전용 시험 가능성, 일대일 배정 조건·인증 제약·실시간 재배정 위험을 기록했다. 실행 전 고정 설정 우선은 제안이며 추가 구현 승인으로 처리하지 않음. AWS 실제 시험 없음.

- 차량 수·연결 방식별 비용 동작 설명: SIMULATOR_OPERATION_COST.md에 차량 상태/Client/Worker 분리, 생성·발행·Client ID 수명 관리의 예정 구조와 1,000/10,000대 동일 72시간 연결 비교를 기록했다. 공식 IoT/Fargate 문서 재확인 및 산술 대조. 기능 구현 또는 AWS 배포는 수행하지 않았다.

- 차량 수·연결 방식별 비용 영향 설명을 COST_ESTIMATE_SEOUL.md에 추가했다. 메시지·연결 사용량 비용과 배정 자원 실행비를 구분하고, 계산기의 Worker 수가 차량 설정에 자동 연동되지 않는 점을 명시했다. 공식 요금 원칙 재확인, 자원 요구는 실측 전 설계 판단.

- 비용 계산기 비교표 가독성 개선: 상시/운행 연동별로 풀·차량별 연결을 묶고 연결 수·개당 유지시간·연결비를 명시했다. 동시 운행/분산 운행/직접 지정 배치를 추가했으며, 풀 가동 72시간 때문에 두 행이 같아지는 이유를 동적으로 표시한다. 기본 조건·요금은 보존했다. 글자·대비·간격 확대, 전체 너비 비교표 적용. Node 회귀검증과 브라우저 동시/분산 전환 확인, comparison-preview.png 기록.

- 30일 이내 조건 조정형 비용 계산기 구현: tools/cost-calculator/cost-calculator.html, seoul-cost-config.json, requests.http 및 사용법 작성. 하루/전체 운행시간, 풀/전체 연결, 상시/운행 연동/직접 지정 유지, 메시지 크기·주기, 계단 단가, 비교 보관·JSON 입출력 지원. 시뮬레이터 본체나 AWS 자원은 변경하지 않음.
- 계산기 확인: Node 검증 통과(기존 견적·5KB 경계·구간 단가·유지시간·잘못된 입력). 브라우저에서 기본 결과·전체 기간 전환·31일 입력 차단·비교 보관·JSON 내보내기/불러오기 확인. 내보낸 JSON을 원본과 대조해 일치 확인. 로컬 HTTP 미리보기 127.0.0.1:8766 사용, 서버 없이 HTML 직접 열기도 가능하도록 기본 설정을 내장함. 실제 AWS 청구 검증은 아님.

- 서울 1만 대·3일 견적 작성: COST_ESTIMATE_SEOUL.md. 공식 AWSIoT·AmazonECS 서울 가격 파일 조회, 단위별 계산 결과 보관. 기본 하루 2시간 해석에서 IoT 약 $129.68, 참고 실행 예산 약 $185(가정·예비비 포함). AWS 환경 선택·실측·실제 지출은 없음.

- Client 연결 한도·비용 추가 확인: 연결 수와 유지시간 기준 과금, 기본 동시 연결 한도, 미국 리전 예시 계산을 MQTT_CONNECTION_REVIEW.md에 기록. 실제 계정 한도 조회·서울 견적·AWS 전송은 수행하지 않음.

- 연결 설명 보완: 기존 MQTT_CONNECTION_REVIEW.md를 읽고 송신 순서·차량/Client/인증 구분·shared subscriptions의 적용 범위를 추가했다. 공식 자료 조사이며 실제 연결 시험은 수행하지 않았다. 기존 미커밋 변경은 보존했다.

- 추가 조사: MQTT_CONNECTION_REVIEW.md 작성. IoT Core의 다중 Client 직접 수용과 내부 풀의 차이, 대안, 남은 설정 시점 정리. 공식 문서와 현행 작업 기록 확인. 코드 변경·AWS 실제 접속·전송 시험은 수행하지 않음.

- 폴더 apps/web, services/control, services/worker, contracts 구성. 실행 코드는 아직 없음.
- VS Code workspace/tasks, .editorconfig, .gitignore, 환경 점검 도구 작성.
- 환경 점검 도구는 설치 여부만 검사한다. npm 누락 시 정상적으로 실패 상태를 반환해야 한다.
- Windows 기본 정책상 PowerShell 파일 실행이 차단되어 환경 점검 도구를 Python으로 변경했다. 시스템 실행 정책은 바꾸지 않았다.
