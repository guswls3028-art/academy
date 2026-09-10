# backend/docs

Backend 문서의 단일 진입점. 현재 동작 정본, 운영 절차, 리팩토링 계획, 과거 기록을 분리한다.

제품 전체 목표 아키텍처는 워크스페이스 루트 `ARCHITECTURE.md`에 둔다. 현재 실측과 실행 계획은 이 저장소의 [refactor/](refactor/)에서 관리한다.

## 진실 우선순위

충돌 시 아래 순서로 판단한다.

1. 실행 코드: `apps/`, `academy/`, `scripts/v1/`, `.github/workflows/`
2. 실행 SSOT: [ssot/](ssot/)
3. 현재 정책 문서: [architecture/](architecture/), [domain/](domain/), [infrastructure/](infrastructure/), [operations/](operations/)
4. 진행/예정 설계: [refactor/](refactor/)
5. 과거 기록: [releases/](releases/), [reports/](reports/)

## 폴더 트리

```text
backend/docs/
  README.md
  ssot/             # 코드/스크립트/CI가 직접 의존하는 정본
  architecture/     # 레이어, 모듈 경계, ADR, 큰 설계 결정
  domain/           # 현재 도메인 규칙과 상태머신
  infrastructure/   # AWS/Cloudflare/RDS/SQS/R2 구조와 예산
  operations/       # 배포, 장애대응, 운영 절차, 테넌트 셋업
    runbooks/       # 절차형 운영 runbook
    tenants/        # 테넌트별/도메인별 셋업
  refactor/         # 예정 리팩토링, 백로그, migration plan
  reports/          # 자동/수동 검증 보고서와 사고 기록
    history/        # audit/drift 스냅샷
    incidents/      # 사고 보고서
  releases/         # 봉인 릴리즈 기록, append-only
```

## 폴더 의미

| 폴더 | 의미 | 변경 방식 |
|------|------|-----------|
| [ssot/](ssot/) | 코드/스크립트/CI가 경로 그대로 읽는 정본 | 의존 코드와 동시 변경 |
| [architecture/](architecture/) | 레이어 책임, 배치 규칙, ADR | 큰 결정 시 갱신 |
| [domain/](domain/) | 도메인별 현재 정책·상태·불변 규칙 | 기능/정책 변경 시 갱신 |
| [infrastructure/](infrastructure/) | 인프라 구조, 용량, 비용, 자원 경계 | 인프라 변경 시 갱신 |
| [operations/](operations/) | 배포/운영/장애/테넌트 절차 | 실제 운영 절차 변경 시 갱신 |
| [refactor/](refactor/) | 예정 리팩토링과 백로그 | 완료 후 정본 문서로 흡수 |
| [reports/](reports/) | 자동 보고서, 감사, 사고 기록 | 자동 생성 또는 append-only |
| [releases/](releases/) | 봉인 릴리즈 기록 | append-only |

## 핵심 단축 경로

| 용도 | 경로 |
|------|------|
| CURRENT 릴리즈 | [releases/README.md](releases/README.md)의 CURRENT 행 |
| 실행 파라미터 | [ssot/params.yaml](ssot/params.yaml) |
| 레이어/코드 배치 | [architecture/hexagonal-cutover-policy.md](architecture/hexagonal-cutover-policy.md) |
| OpenAPI·프런트 생성 타입 계약 | [architecture/api-schema-contract.md](architecture/api-schema-contract.md) |
| GET/HEAD/OPTIONS 무변경 경계 | [architecture/safe-http-mutation-boundary.md](architecture/safe-http-mutation-boundary.md) |
| 시험 생성·혼합 채점·오답노트 | [domain/exam-grading.md](domain/exam-grading.md) |
| 데이터 목록 정렬·필터·페이지네이션 | [domain/data-list-ordering.md](domain/data-list-ordering.md) |
| 출결 명단 정렬·페이지네이션 | [domain/attendance.md](domain/attendance.md) |
| 강의 정규 수업·보강 유형과 이름 | [domain/lecture-sessions.md](domain/lecture-sessions.md) |
| 과제 만점·합격 정책·성적 저장 | [domain/homework-grading.md](domain/homework-grading.md) |
| 학생별 누적 성적·정규/보강 범위 | [domain/student-performance-console.md](domain/student-performance-console.md) |
| 학생 성적표 오답 상태·학원별 성장 그래프 구성 | [domain/student-grade-report.md](domain/student-grade-report.md) |
| 교직원 저장소·학생 인벤토리 파일과 R2 보상 정리 | [domain/inventory-storage.md](domain/inventory-storage.md) |
| 보강·클리닉 등원 예정 운영 | [domain/arrival-operations.md](domain/arrival-operations.md) |
| 알림톡 발송 기록의 역할별 조회·상태·개인정보 경계 | [domain/messaging-delivery-log.md](domain/messaging-delivery-log.md) |
| 시험 원본→회차 범위 HWPX 오답노트 계획 | [refactor/exam-wrong-note-hwpx-plan.md](refactor/exam-wrong-note-hwpx-plan.md) |
| OMR 출력·인식 | [domain/omr.md](domain/omr.md) |
| 배포 아키텍처 | [infrastructure/deployment-architecture.md](infrastructure/deployment-architecture.md) |
| 배포 경로 비교 | [operations/deployment-modes.md](operations/deployment-modes.md) |
| 컨테이너 이미지 보안 | [operations/container-image-security.md](operations/container-image-security.md) |
| 상시 개발 런타임 | [operations/persistent-development-runtime.md](operations/persistent-development-runtime.md) |
| 수동 정식 배포 | [operations/formal-deploy.md](operations/formal-deploy.md) |
| 동시 Codex 세션 격리·정리 | [operations/concurrent-codex-sessions.md](operations/concurrent-codex-sessions.md) |
| 변경 위험 라우팅·교차 저장소 릴리스 증거 | [operations/change-risk-and-release-bundle.md](operations/change-risk-and-release-bundle.md) |
| 운영 canary·E2E 잔재 정리 | [operations/production-canary.md](operations/production-canary.md) |
| 개발자 문의 운영함 | [operations/dev-console-inbox.md](operations/dev-console-inbox.md) |
| 읽기 전용 상태 모순 검사·전달 영수증 | [operations/state-integrity-monitor.md](operations/state-integrity-monitor.md) |
| 제품 사용 분석 | [domain/product-usage-analytics.md](domain/product-usage-analytics.md) |
| DB 확장·테넌트 분리 판단 | [infrastructure/database-scaling-and-tenant-isolation.md](infrastructure/database-scaling-and-tenant-isolation.md) |
| 강사 AI 문제 풀이 (Beta) | [domain/teacher-problem-solver.md](domain/teacher-problem-solver.md) |
| 선생앱 학생 업무 도우미 (Beta) | [domain/teacher-ops-assistant.md](domain/teacher-ops-assistant.md) |
| PPT 문제 생성·문항 크롭 | [domain/ppt-question-generator.md](domain/ppt-question-generator.md) |
| 문제 리뷰 리포트 작성·PDF/PPTX 출력 | [domain/problem-review-report.md](domain/problem-review-report.md) |
| 타이머 Windows 배포·서명·PWA 대체 경계 | [domain/timer-distribution.md](domain/timer-distribution.md) |
| 교사 제공 참고자료 인벤토리·보안·품질 경계 | [domain/teacher-provided-source-materials.md](domain/teacher-provided-source-materials.md) |
| 운영 runbook | [operations/runbooks/](operations/runbooks/) |

## 작성 규칙

- 현재 규칙은 `domain/`, `architecture/`, `infrastructure/`, `operations/` 중 하나에 둔다.
- 예정/제안/백로그는 `refactor/`에 둔다. 구현 완료 후 현재 정본 문서로 흡수한다.
- 사고/감사/검증 기록은 `reports/`에 둔다. 현재 정책처럼 서술하지 않는다.
- 봉인 릴리즈는 `releases/`에 두고 append-only로 관리한다.
- 한 주제는 한 파일에 둔다. 같은 내용을 여러 문서에 복제하지 않는다.
- 파일명은 kebab-case를 기본으로 하며, 기존 한국어 운영 문서명은 유지할 수 있다.

## 기능 변경 기록 계약

기능을 추가·수정·삭제·대체하는 작업은 같은 작업 안에서 해당 기능의 현재
정본 문서를 갱신해야 완료된다. 별도 작업 일지보다 `domain/`,
`architecture/`, `infrastructure/`, `operations/`의 소유 문서가 현재
상태를 설명하고, 시간순 변경 내역은 Git 이력이 담당한다. 소유 문서가 없으면
적절한 폴더에 만들고 이 진입점이나 가장 가까운 상위 문서에서 연결한다.

기능 문서에는 해당되는 범위에서 다음 내용을 남긴다.

- 기능의 목적, 사용자/운영자 역할, 진입점
- 처음부터 끝까지의 정상 흐름과 주요 상태 전이
- 권한, 테넌트, 데이터 보존 등 깨지면 안 되는 불변 규칙
- API, 이벤트, 작업 큐, 저장 데이터의 소유 경계와 프론트엔드 연결 문서
- 빈 상태, 오류, 재시도, 중복 요청 등 실패 경계
- 구현 위치를 찾을 수 있는 안정적인 모듈 경로와 집중 검증 명령/테스트
- 삭제·대체 시 그 이유, 대체/마이그레이션 경로, 호환 범위, 기존 데이터 처리

코드 내부 정리처럼 제품 동작이 보존되는 변경은 기능 문서를 억지로 고치지
않아도 된다. 대신 작업 완료 보고에 동작이 바뀌지 않았다는 판단 근거와 이를
입증한 테스트를 명시한다. 날짜형 보고서, 계획서, 에이전트 메모만으로 현재
기능을 설명해서는 안 된다.
