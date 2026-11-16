# Client 수를 조정하는 공통 연결 엔진 검토안

2026-11-16 사용자 확정: 공통 연결 엔진, 연결 풀과 차량별 전체 연결 비교를 1차 구현에 포함한다. 연결 수·배정은 실행 전 설정하고 실행 중 고정한다. 실행 중 증감·재배정은 후속 기능이다. AWS 환경 추후 선택은 유지한다. 기능 구현·AWS 시험은 아직 수행하지 않았다.

차량 ID와 MQTT Client ID는 별도다. 차량→Client 배정은 애플리케이션에서 관리할 수 있다. 단, Client ID는 CONNECT로 정해지는 접속 식별자이므로 연결 도중 이름만 바꾸는 기능과 혼동하지 않는다. MQTT 인증/토픽 정책과 수신 시스템이 차량을 식별하는 방식에 따라 자유도가 제한된다.

공통 엔진은 차량 상태 엔진 → 차량별 전송 큐 → 배정 관리자 → MQTT 세션 관리자 → IoT Core로 구성한다. 배정표는 run/vehicle/client/worker/epoch를 기록한다. 공유 모드에서는 차량마다 담당 Client를 고정하고 균등 배분한다. 전용 모드에서는 차량과 Client를 명시적으로 일대일 배정한다. Client 수가 차량 수와 같다는 조건만으로 전용 모드가 성립하지 않으며 hash 배정 충돌도 피해야 한다.

제안 설정: vehicle_count, mapping_mode(shared/dedicated), client_count(공유 모드의 실행 전체 목표치), connection_lifetime(always/while_driving/keep_after_stop), connect_rate_limit, reconnect 정책, 메시지 속도·미확인 요청·큐 제한, credential_profile. 전용 모드는 차량 수만큼 Client 정의를 만들되 실제 연결 수는 수명 정책과 운행 상태에 따라 달라진다. 목표 Client 수와 실제 연결 성공/연결 중/실패/유휴 수를 분리해 표시한다. Worker마다 전체 client_count를 복제하지 않고 실행 전체 수량을 나눠 소유한다.

차량 10,000대에서 공유 100연결과 전용 10,000연결을 같은 생성 엔진으로 시험할 수 있다. 데이터 생성량은 동일 주기로 유지한다. 같은 seed·기준 시각·운행/연결 유지 조건으로 비교하고, 연결 수 감소로 전송 한도가 낮아질 때 큐가 무한히 증가하지 않게 실패·지연을 표시한다.

초기 확정 범위는 실행 시작 전에 연결 수·배정 모드를 설정하고 실행 동안 고정하는 것이다. 고정된 유지 정책에 따른 접속/해제와 같은 담당 Client의 장애 재연결은 포함한다. 설정 Client 수와 실제 동시 연결 수는 구분한다. 새 실행으로 풀/전용을 비교한다. 실행 중 증감은 후속 기능: 해당 차량 생성 일시정지 또는 제한 큐 → 기존 전송/PUBACK 정리 → timeout 미확인 기록 → epoch로 소유권 인계 → 새 연결 전송 재개 → 이전 연결 정리. 무손실·중복 없음은 보장하지 않는다. 연결 장애·QoS 1 재전송·체크포인트 복구에서 중복 가능하다.

주요 문제: 연결당/계정 Publish 상한, 대량 CONNECT와 재연결 폭주, 연결 전환 시 순서 역전·중복·미확인, 동일 Client ID 접속 충돌, 토픽/인증서/Thing 정책과 차량 식별 불일치, Worker 메모리·TLS·소켓 한계, 장애 영향 차량 수 차이, 유휴 연결 비용, 비교 조건 불일치. 인증서 공유는 차량별 독립 인증 재현과 구분한다. 인증서 개인 키는 배정표나 로그에 기록하지 않는다.

근거: [AWS MQTT](https://docs.aws.amazon.com/iot/latest/developerguide/mqtt.html), [AWS 할당량](https://docs.aws.amazon.com/general/latest/gr/iot-core.html), [Thing policy variables](https://docs.aws.amazon.com/iot/latest/developerguide/thing-policy-variables.html). 2026-11-16 재확인.
