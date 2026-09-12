# refactor

예정 리팩토링, 백로그, migration plan을 두는 작업 대기실.

## 문서

| 파일 | 상태 | 내용 |
|------|------|------|
| [structure-reform/REFACTOR_ROADMAP.md](structure-reform/REFACTOR_ROADMAP.md) | active | 프로젝트 구조 리팩토링 실행 로드맵 |
| [structure-reform/STRUCTURE_AUDIT.md](structure-reform/STRUCTURE_AUDIT.md) | verified | 학생 중심 duplicate root 및 경계 감사 |
| [structure-reform/DOMAIN_BOUNDARIES.md](structure-reform/DOMAIN_BOUNDARIES.md) | verified/proposed | 도메인별 현재 책임과 공개 인터페이스 후보 |
| [structure-reform/DUPLICATE_ROOTS.md](structure-reform/DUPLICATE_ROOTS.md) | verified/proposed | 중복 진입점과 canonical 후보 |
| [structure-reform/PRE_PROMOTION_STRUCTURE_PLAN.md](structure-reform/PRE_PROMOTION_STRUCTURE_PLAN.md) | proposed | 운영 홍보 직전 배포·작업트리·구조조정 착수 계획 |
| [roadmap.md](roadmap.md) | phase-0 baseline | 대규모 구조 리팩토링 Phase 0 가드레일 로드맵 |
| [inventory.md](inventory.md) | verified/proposed | 현재 구조 실측과 병목 inventory |
| [phase-0-guardrails.md](phase-0-guardrails.md) | proposed | 코드 이동 전 안전망 구축 계획 |
| [validation-matrix.md](validation-matrix.md) | proposed | 단계별 검증 매트릭스 |
| [hardening-plan.md](hardening-plan.md) | active | 기존 사용자 흐름 완결성·데이터 안전·UX·구조·운영 하드닝 실행 계획 |
| [failure-transparency-stabilization.md](failure-transparency-stabilization.md) | partially implemented / runtime-unverified | 자동승인 수리·CI 확인, 나머지 4개 후보 미해결; 실화면·운영 미검증 |
| [matchup-segmentation-risk-backlog.md](matchup-segmentation-risk-backlog.md) | proposed | 매치업 문항분리 숨은 버그·잠재 리스크와 실행 단위 |
| [exam-wrong-note-hwpx-plan.md](exam-wrong-note-hwpx-plan.md) | proposed | 시험 원본 검수·문항 정본 저장·회차 범위 학생별 HWPX 오답노트 단계와 수용 기준 |
| [backlog-student-grade-comparison.md](backlog-student-grade-comparison.md) | backlog | 학생 성적 비교 시스템 |

## 작성 규칙

- 아직 현재 동작이 아닌 계획은 여기에 둔다.
- 구현이 끝나면 관련 정본 문서(`domain/`, `architecture/`, `operations/`, `infrastructure/`)로 흡수하고 이 문서는 완료/보관 여부를 결정한다.
- 대규모 리팩토링 문서는 목표, 대상 경로, compatibility boundary, 검증 기준, rollback/cleanup 기준을 포함한다.
- `structure-reform/` 문서가 현재 대규모 구조 리팩토링의 active 작업대다. 완료된 slice는 현행 정본 문서로 흡수하거나 `reports/history/`에 감사 기록으로 보관한다.
