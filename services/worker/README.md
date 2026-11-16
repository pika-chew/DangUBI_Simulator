# 생성·전송 Worker

Go Worker의 고정 배정·생성 확인·Eclipse Paho TLS MQTT 소량 전송 구현입니다.

현재 구현: stdin JSON 입력, 공유/전용 Client 배정, 차량별 입력값 복제·시각 갱신, TLS 인증, 제한된 연결·발행 속도, QoS 1 PUBACK, 동일 ID 재연결, 첫/마지막 메시지에 따른 연결 유지와 정리. 송신 없는 확인을 기본으로 합니다. 실행법은 ../../docs/USAGE.md를 참고하세요.
코어 로직은 ECS/Fargate API에 직접 의존하지 않습니다. AWS 전송은 접속 설정이 준비된 이후 확인합니다.
