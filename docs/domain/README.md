# domain

도메인별 현재 정책·규칙·상태머신 SSOT. 코드와 다르면 코드를 확인한 뒤 문서를 갱신한다.

## 핵심 정책

| 파일 | 도메인 | 핵심 |
|------|--------|------|
| [state-transitions.md](state-transitions.md) | 시험/클리닉/과제 | 상태머신과 결과 경계 |
| [messaging.md](messaging.md) | 메시징 | 메시징 SSOT 인덱스 |
| [messaging-alimtalk.md](messaging-alimtalk.md) | 알림톡 | 4종 ITEM_LIST 봉투 + 본문 자유 정책 |
| [account-first-use.md](account-first-use.md) | 로그인 | 신규 계정 첫 접속 안내와 1회 완료 상태 |
| [account-recovery.md](account-recovery.md) | 로그인 | 아이디/비밀번호 찾기 |
| [parent-account.md](parent-account.md) | 학부모 | 학부모 계정 생성/로그인 |
| [student-core.md](student-core.md) | 학생 | 학생 중심 계정·식별자·연결 도메인 통합 SSOT |
| [direct-video-access.md](direct-video-access.md) | 영상 | 수강 등록 없이 정확한 학생·영상 한 건만 여는 감사·회수 계약 |
| [public-video-session.md](public-video-session.md) | 영상 | 공개 영상 공간의 무쓰기 조회·명시적 준비·구형 클라이언트 호환 |
| [student-support-audit.md](student-support-audit.md) | 학생 지원 | 교직원 학생 화면 대리보기와 학생 활동 감사 계약 |
| [student-creation.md](student-creation.md) | 학생 | 생성 계정 그래프 |
| [student-lifecycle.md](student-lifecycle.md) | 학생 | 삭제/복원/영구삭제 생명주기 |
| [staff-operations.md](staff-operations.md) | 직원 | 권한·퇴사·근태·비용·월마감·정산 표시 계약 |
| [fees.md](fees.md) | 수납 | 학원별 비목·월 청구·수납·취소 후 재청구 계약 |
| [lecture-sessions.md](lecture-sessions.md) | 강의 | 정규 수업·보강 유형, 차시 번호, 보강 이름과 API 경계 |
| [homework-grading.md](homework-grading.md) | 과제 | 과제별 만점, 차시 공통 합격 정책, 성적·클리닉 동기화 |
| [student-performance-console.md](student-performance-console.md) | 성적 | 학생별 누적 성적과 정규·보강 결과 범위 |
| [student-grade-report.md](student-grade-report.md) | 성적 | 학생 카드 오답 완료 상태와 학원별 성장 그래프 섹션 구성 |
| [arrival-operations.md](arrival-operations.md) | 출결·클리닉 | 보강 예정 저장과 클리닉 예약을 합친 오늘·내일 등원 운영 현황 |
| [clinic-booking.md](clinic-booking.md) | 클리닉 | 세션별 같은 날 다중 시간대 예약 정책·원자성·동시성 |

## 도메인별

| 파일 | 도메인 |
|------|--------|
| [community.md](community.md) | 커뮤니티 |
| [matchup.md](matchup.md) | 매치업 사용자 흐름 |
| [omr.md](omr.md) | OMR v15 인식 시스템 |
| [problem-studio.md](problem-studio.md) | 문제 제작/한글 이관 |
| [teacher-mobile.md](teacher-mobile.md) | 선생앱 모바일 설계 |

## 다른 폴더로 분리된 문서

| 주제 | 위치 |
|------|------|
| 레이어/코드 배치 정책 | [../architecture/hexagonal-cutover-policy.md](../architecture/hexagonal-cutover-policy.md) |
| 운영 baseline | [../operations/operations-baseline.md](../operations/operations-baseline.md) |
| 결제 오픈 체크리스트 | [../operations/billing-go-live-checklist.md](../operations/billing-go-live-checklist.md) |
| RDS connection budget | [../infrastructure/connection-budget.md](../infrastructure/connection-budget.md) |
| 학생 성적 비교 백로그 | [../refactor/backlog-student-grade-comparison.md](../refactor/backlog-student-grade-comparison.md) |
| DB 인증 장애 기록 | [../reports/incidents/incident-2026-03-23-db-auth-failure.md](../reports/incidents/incident-2026-03-23-db-auth-failure.md) |

## 작성 규칙

- 현재 운영 중인 도메인 규칙만 둔다.
- 계획/백로그/제안은 `../refactor/`에 둔다.
- 사고와 감사 기록은 `../reports/`에 둔다.
- 운영 절차는 `../operations/`에 둔다.
