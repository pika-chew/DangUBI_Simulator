# 제어 API

Python FastAPI 제어 API입니다. 개발 스키마 검증, Go Worker 프로세스 전달, 고정 실행 설정과 로컬 기록 조회를 구현했습니다.

현재는 dry_run만 지원합니다. 실제 MQTT 전송을 수행하지 않습니다. 실행법과 API 검증은 ../../docs/USAGE.md를 참고하세요.
실행 관리와 데이터 생성·MQTT 처리를 분리합니다.
