# 현재 작업 현황

갱신일: 2026-11-16 (Asia/Seoul)

## 단계

**2단계 개발 기반 구성 진행 / 시뮬레이터 기능 구현 전**

2026-11-16 후속 확정: 코드 스택과 연결 풀 채택, AWS 최종 환경 추후 선택. 개발 기반 구성 시작. 기존 추가 검토 기록은 AWS_OPTIONS.md에 보관한다.

## 완료

- 빈 로컬 작업 폴더와 빈 GitHub 저장소 확인.
- 제공된 Notion 제안서 1~9절을 열려 있는 브라우저에서 읽음.
- Smart Fleet OBD와 AWS 공식 문서 확인. 표/예제 충돌과 MQTT 제한 기록.
- DESIGN v0.1, SOURCES, DECISIONS, WORKFLOW, README, AGENTS 작성.

## 실제 확인과 한계

- 문서 파일 생성 및 필수 항목 포함 여부 확인. 실행 코드가 없어 기능 테스트는 수행하지 않음.
- 로컬 Git 초기화 및 GitHub origin 연결 완료. 초기 커밋 112a107을 main에 push 완료. 상시 감시 장치는 아직 설정하지 않음.
- VS Code 프로젝트 workspace 열기 명령 실행 완료. AWS 자원 생성·배포·실제 Publish는 수행하지 않음.
- Notion 원문은 수정하지 않음. 현재 설계 초안의 기준 문서는 로컬 DESIGN.md.
- 공유 기록은 같은 폴더/저장소를 읽는 작업에서 적용된다. 모든 별도 채팅을 자동 동기화하는 서비스는 아직 없음.

## 다음 작업

개발 도구 준비(npm 확인 필요) → 수동 입력·미리보기·Worker 전달 구현. 실제 IoT 전송 전 수신 규격·토픽·접속 정보를 확인하고, AWS 부하 실행 전 목표·예산을 확정한다. 자세한 단계는 IMPLEMENTATION.md를 따른다.

## 이번 산출물과 확인

- 폴더 apps/web, services/control, services/worker, contracts 구성. 실행 코드는 아직 없음.
- VS Code workspace/tasks, .editorconfig, .gitignore, 환경 점검 도구 작성.
- 환경 점검 도구는 설치 여부만 검사한다. npm 누락 시 정상적으로 실패 상태를 반환해야 한다.
- Windows 기본 정책상 PowerShell 파일 실행이 차단되어 환경 점검 도구를 Python으로 변경했다. 시스템 실행 정책은 바꾸지 않았다.
