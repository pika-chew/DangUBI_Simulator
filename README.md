# DangUBI 차량 데이터 시뮬레이터

현재 단계: **수동 입력 → API → Go Worker의 첫 로컬 실행본 구현 완료**. 송신 없는 생성 확인과 풀/차량별 고정 배정을 지원합니다. 실제 MQTT 전송·자동 주행·부하 실행은 후속입니다.

`python scripts/dev.py run` 실행 후 http://127.0.0.1:8080 에서 확인합니다. [첫 실행본 사용법·검증·제한](docs/FIRST_RUN.md)을 참고하세요.

VS Code에서 `DangUBI.code-workspace`를 열고 ‘프로젝트: 개발 환경 확인’ 작업을 실행할 수 있습니다.

- [설계 초안](docs/DESIGN.md)
- [서울 리전 비용 계산기](tools/cost-calculator/cost-calculator.html) · [설정 JSON](tools/cost-calculator/seoul-cost-config.json) · [사용법](tools/cost-calculator/README.md)
- [MQTT 연결 구조·대안 및 남은 검토](docs/MQTT_CONNECTION_REVIEW.md)
- [연결 풀·AWS 환경 비교와 결정 시점](docs/AWS_OPTIONS.md)
- [현재 작업 현황](docs/STATUS.md)
- [결정 및 검토 항목](docs/DECISIONS.md)
- [공식 근거 자료](docs/SOURCES.md)
- [컨텍스트 공유 및 버전관리 방안](docs/WORKFLOW.md)
- [구현 착수 계획과 남은 확인 항목](docs/IMPLEMENTATION.md)

대상 GitHub: https://github.com/pika-chew/DangUBI_Simulator

로컬 Git을 초기화하고 origin을 위 저장소로 연결했습니다. 상시 자동 감시·동기화는 아직 설정하지 않았습니다.
