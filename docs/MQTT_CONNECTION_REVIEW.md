# MQTT 연결 구조 추가 검토

2026-11-16. AWS 최종 실행 환경 추후 선택 유지. 기존 연결 풀 채택은 유지하되 이번 질문을 철회나 차량별 연결 구현 승인으로 해석하지 않는다.

## 브로커와 연결 풀의 관계

AWS IoT Core는 여러 MQTT 클라이언트를 직접 수용하는 관리형 브로커다. 계정의 데이터 endpoint로 차량마다 고유 Client ID를 사용해 직접 연결할 수 있다. 하나의 논리적 서비스 endpoint이지 물리 서버 한 대라는 의미는 아니다. MQTT는 프로토콜, Client는 접속 주체, broker는 메시지를 받는 서비스다.

현재 풀 설계는 Worker 내부 차량 상태 → Worker 내부 여러 MQTT Client → IoT Core다. 가상 차량은 외부 장치가 아니라 Worker 안의 데이터·상태이므로 외부 차량의 MQTT 접속을 먼저 받는 중계 서버가 없다. 풀은 별도 브로커도 아니고 IoT Core가 차량별 MQTT 세션을 합치는 기능도 아니다. 차량을 대표해 발행하는 점에서 게이트웨이와 유사한 의미는 있지만 별도 네트워크 중계 홉은 없다.

차량 1,000개/풀 연결 10개이면 IoT Core에서는 MQTT Client 10개와 차량별 토픽·본문이 보인다. 차량별 직접 연결이면 Client 1,000개가 보인다. 두 경우 모두 Worker에서 IoT Core로 직접 접속한다. Worker 한 개가 다수의 독립 Client를 실행할 수 있으므로 차량 수만큼 서버를 만들 필요는 없다. 같은 Client ID를 중복 사용하면 기존 연결이 끊기므로 실행·Worker·차량 구분을 포함해야 한다.

근거: [AWS MQTT](https://docs.aws.amazon.com/iot/latest/developerguide/mqtt.html), [프로토콜](https://docs.aws.amazon.com/iot/latest/developerguide/protocols.html).

### 실제 송신 순서

1. Worker가 차량별 상태를 생성하고 각 메시지에 차량 식별 가능한 토픽을 배정한다.
2. 풀 Client들이 각자의 고유 Client ID와 허용된 인증서로 IoT Core에 TLS/MQTT 접속한다.
3. 차량→Client 매핑에 따라 전송 큐에 넣고 전체/연결별 limiter를 거쳐 Publish한다.
4. QoS 1이면 해당 Client 연결로 PUBACK를 받는다. 이 응답은 차량별 독립 인증이나 후속 저장 완료를 뜻하지 않는다.
5. 연결이 끊기면 해당 연결의 차량 전송에 영향을 준다. 제한된 큐·백오프·재시도로 처리하며 중복과 미확인 상태를 기록한다.

차량 ID(데이터 주체), Client ID(MQTT 세션 주체), 인증서(접속 자격)는 구별한다. 풀에서는 차량 ID와 Client ID가 일대일이 아니다. 차량별 독립 연결도 인증서를 공유하면 차량별 독립 자격 증명을 재현한 것은 아니므로 인증 재현 목표에 맞춰 별도로 구성해야 한다.

## 대안과 판단

| 방식 | 경로 | 프로젝트에 대한 판단 |
|---|---|---|
| 풀에서 MQTT 직접 발행 | 여러 차량 상태 → 소수 Client → IoT Core | 데이터량 중심 시험에 적합. 차량별 연결·인증 재현 불가. 기존 채택안. |
| 차량별 MQTT 직접 연결 | 차량 상태별 Client → 동일 IoT Core endpoint | 실제 접속·재연결 부하에 적합. 연결·메모리·인증서 운영 부담 증가. 추가 기능 승인 전 후보. |
| HTTPS Publish | Worker → IoT Data Publish API | MQTT 연결을 유지하지 않고 전송 가능. 차량 MQTT 세션 재현 불가. API 속도·계정 한도 별도 검토. |
| MQTT over WSS | Worker → WebSocket MQTT → IoT Core | 443 포트·SigV4 인증 대안. 여전히 MQTT Client와 연결 수를 관리하므로 풀/차량별 모델의 대체 축이 아님. |
| 실제 중계/브리지 | 단말 → 로컬 broker/Greengrass → IoT Core | 실제 게이트웨이 구조를 시험할 때 적합. 중계 관리·지연·병목이 추가되어 현재 필수 범위에는 불필요하다는 판단. |

HTTPS Publish는 송신 전용이며 MQTT 세션 시험과 동등하지 않다. X.509 MQTT, WSS/SigV4, HTTPS/SigV4는 인증 방식도 다르다. 서로 바꿀 때 해당 권한·SDK·응답 처리를 별도로 구현해야 한다.

Basic Ingest는 연결 풀 대체가 아니다. `$aws/rules/...` 토픽을 사용해 pub/sub broker를 우회하고 Rule로 보내는 수집 경로다. broker 동작이 목표인 경우 동등한 시험이 아니며 Rule 비용·권한도 필요하다. 현재 전송 범위에 자동 추가하지 않는다.

Shared subscriptions는 IoT Core가 수신 메시지를 여러 구독자에게 분배하는 기능이다. 차량 송신자의 여러 연결을 하나로 합치는 기능이 아니므로 연결 풀 대체로 사용하지 않는다. MQTT 5로 바꾸어도 차량별 세션을 합치는 문제가 해결되는 것은 아니다. 근거: [AWS MQTT shared subscriptions](https://docs.aws.amazon.com/iot/latest/developerguide/mqtt.html#shared-subscriptions).

근거: [Publish API](https://docs.aws.amazon.com/iot/latest/apireference/API_iotdata_Publish.html), [통신/인증](https://docs.aws.amazon.com/iot/latest/developerguide/protocols.html), [Greengrass bridge](https://docs.aws.amazon.com/greengrass/v2/developerguide/mqtt-bridge-component.html), [Basic Ingest](https://docs.aws.amazon.com/iot/latest/developerguide/iot-basic-ingest.html).

## 규모 관련 주의점

### Client 수와 연결 비용

2026-11-16 공식 요금 재확인: Client ID 문자열을 준비한 개수가 아니라 실제 연결 유지시간 합계가 Connectivity 과금 기준이다. 1분 단위로 측정한다. 동시 연결 한도는 계정·리전별 기본 500,000개, 일부 리전 100,000개이며 증액 요청 가능 항목이다. 서울은 현재 공식 표의 일부 리전 목록에 포함되지 않아 일반 기본값 500,000개에 해당하나 실제 계정 적용값은 별도 확인한다. 이것은 보유 가능한 ID 문자열 수나 영구 등록 가능 수가 아니다.

공식 가격 페이지의 미국 버지니아 북부 예시 단가 $0.08/1,000,000 연결·분을 적용하면 30일 상시 연결 시 20개=$0.06912, 1,000개=$3.456, 10,000개=$34.56, 100,000개=$345.60이다. 무료 사용량·세금·환율 제외, 연결 유지비만 계산했으며 서울 견적이 아니다. 메시지·Rule·로그·Worker 등의 비용은 별도다. 메시지 수와 크기가 같다면 연결 풀만으로 메시지 과금 단위가 감소하지 않는다.

근거: [연결·메시지 가격](https://aws.amazon.com/iot-core/pricing/), [계정별 동시 연결 할당량](https://docs.aws.amazon.com/general/latest/gr/iot-core.html).

공식 기본 할당량은 동시 연결 500,000개(일부 리전 100,000개), inbound Publish 20,000/s(일부 2,000/s), 연결당 Publish 100/s다. 이는 서로 다른 제한이며 실제 계정에서 확인해야 한다. 접속 시도 속도·대역폭·미확인 QoS 1 수 제한도 있다. 풀에서 차량별 연결로 바꾸어도 계정 수신 한도는 늘지 않는다. 여러 토픽을 사용해도 동일하다.

근거: [IoT Core 할당량](https://docs.aws.amazon.com/general/latest/gr/iot-core.html).

## 남은 검토 및 시점

| 항목 | 필요 시점 | 현재 처리/제안 |
|---|---|---|
| 시험 목표: 데이터량/차량 연결 수/둘 다 | MQTT 전송 모듈 상세 구현 전 | 기존 풀 기본 유지. 두 모드를 추가할지는 별도 결정. 브로커 중계는 추가하지 않음. |
| OBD ty·자료형·단위·시간·필수값 | 실제 수신 연동 전 | 개발은 표 기준 잠정 스키마. 규격 적합성 확정은 아님. |
| 토픽·차량 ID·Client ID·인증서 정책 | 첫 실제 AWS 연결 전 | 풀 Client가 여러 차량 토픽에 발행할 수 있어야 함. 실제 단말 인증 재현은 별도 범위. |
| 차량/활성 차량/연결 수, 평균·최대 TPS·기간·메시지 크기 | 부하 시험·자원 산정 전 | 독립 설정과 상한 제공. 목표 수치 미확정. |
| QoS·재시도·순서·중단 시 큐 처리 | 첫 전송 기능 검토 | QoS 1, retain=false, 묶음 없음은 잠정값. 중복·미확인 전송 표시. |
| 차량 프로필·경로·연료 모델·이벤트 범위 | 자동 생성 기능 구현 전 | 내연기관 기본 프로필은 잠정안. |
| AWS 계정/리전·시험 토픽·권한·예산 | 실제 연결/부하 전 | 실행 환경 확정과 구분. 개인 키는 별도 보안 저장소 사용. |
| 개발 도구와 GitHub 자동 기록 | 개발 기반 단계 | npm 준비 필요 기록 유지, 상시 감시는 아직 없음. |

전 항목을 지금 확정할 필요는 없다. 개발 기반과 송신 없는 입력·미리보기 흐름은 진행 가능하다. 사용자 입력이 필요한 것은 우선 시험 목적과 1차 규모다. 나머지는 기능 단계별 구체적 기본안을 검토하며 결정한다. 이번 작업은 공식 문서 조사와 설계 기록이며 연결·전송 실험을 수행하지 않았다.
