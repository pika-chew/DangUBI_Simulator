# 생성·전송 Worker

Go Worker의 고정 배정·송신 없는 생성 확인 구현입니다. Eclipse Paho MQTT 어댑터는 다음 단계이며 아직 설치·연결하지 않았습니다.

현재 구현: stdin JSON 입력, 공유/전용 Client 배정, 차량별 입력값 복제·가상 시각 갱신, 수량·샘플 결과 출력. 네트워크 연결과 TLS·재연결은 아직 없습니다. 실행법은 ../../docs/FIRST_RUN.md를 참고하세요.
코어 로직은 ECS/Fargate API에 직접 의존하지 않습니다. AWS 전송은 접속 설정이 준비된 이후 확인합니다.
