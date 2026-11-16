# 근거 자료

확인일: 2026-11-16. 공식 문서의 일반 제한과 실제 계정에 적용된 할당량은 다를 수 있다. 설계 추천·예시 수치는 자료에 있는 사실과 구분하여 DESIGN에 표시했다.

| ID | 자료 | 적용 내용 |
|---|---|---|
| N1 | [Notion 설계제안서](https://app.notion.com/p/4195f194fbbf80e18f13dd4925f29637) | 브라우저에서 1~9절 확인. 제어/Worker 분리, 수동·상태 생성, 트래픽·연결·복구 방향 반영. 파이프라인 검증 제외. 연결 도구는 404여서 열려 있는 문서를 읽음. |
| R1 | [프로젝트 저장소](https://github.com/pika-chew/DangUBI_Simulator) | API에서 빈 저장소임을 확인. 참조 구현 코드는 없음. |
| S1 | [Smart Fleet 메시지 규격](https://smart-fleet-docs.readthedocs.io/ko/latest/message/) | 7.2.2 OBD 필드와 표/예제 불일치. 수신 계약 확정 필요. |
| S2 | [AWS IoT Core 할당량](https://docs.aws.amazon.com/general/latest/gr/iot-core.html) | 계정·연결별 발행 제한, payload 제한, 동시 연결·대역폭 한도. |
| S3 | [AWS IoT MQTT](https://docs.aws.amazon.com/iot/latest/developerguide/mqtt.html) | MQTT 지원과 QoS 동작. |
| S4 | [통신 프로토콜 및 인증](https://docs.aws.amazon.com/iot/latest/developerguide/protocols.html) | TLS·포트·인증 방식. |
| S5 | [ECS Fargate](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/AWS_Fargate.html) | 컨테이너 실행·자원·네트워크·IAM 설정. |
| S6 | [IoT Core 가격](https://aws.amazon.com/iot-core/pricing/) / [Fargate 가격](https://aws.amazon.com/fargate/pricing/) | 메시지 5KB 과금 단위와 자원 비용 구조. 리전별 금액은 미산정. |
| S7 | [EventBridge 스케줄 유형](https://docs.aws.amazon.com/scheduler/latest/UserGuide/schedule-types.html) | 예약 시각 60초 정밀도. 초 단위 부하는 내부 스케줄러로 처리. |
| S8 | [Eclipse Paho MQTT Go](https://github.com/eclipse-paho/paho.mqtt.golang) | Go MQTT 3.1.1 클라이언트 후보. |

원문을 프로젝트 승인으로 취급하지 않는다. 모든 기술 선택과 성능 기준은 사용자 검토 전 제안이다.

2026-11-16 추가 확인: [ECS 실행 환경](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ecs-configuration.html), [ECS 가격](https://aws.amazon.com/ecs/pricing/), [EC2 가격](https://aws.amazon.com/ec2/pricing/on-demand/), [VPC 가격](https://aws.amazon.com/vpc/pricing/), [Fargate Spot와 전환 제약](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-capacity-providers.html), [Batch](https://docs.aws.amazon.com/batch/latest/userguide/what-is-batch.html), [Lambda timeout](https://docs.aws.amazon.com/lambda/latest/dg/configuration-timeout.html). 코드 스택은 후속 사용자 피드백으로 확정되었고 AWS 환경 선택은 미확정이다.
