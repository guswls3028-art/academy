# 메시징 도메인 SSOT 인덱스

**상태:** Active
**최종 점검:** 2026-09-06
**목적:** 오래된 메시징 표가 여러 문서에 평행 진실로 남는 것을 막기 위한 현재 SSOT 진입점.

## 1. 권위 순서

| 영역 | 정본 |
|------|------|
| 트리거 정책 분류 | `apps/domains/messaging/policy.py`의 `TRIGGER_POLICY` |
| 자동 발화 구현 여부 | `apps/domains/messaging/policy.py`의 `IMPLEMENTED_AUTO_TRIGGERS` |
| 기본 템플릿 정의 | `apps/domains/messaging/default_templates.py` |
| 알림톡 템플릿/봉투 정책 | [messaging-alimtalk.md](messaging-alimtalk.md) |
| 운영 정책 표 | `backend/docs/ssot/messaging-policy.md` |
| 계정 복구 알림톡 | [account-recovery.md](account-recovery.md) |
| 수동 알림 컨텍스트 소스 | `apps/support/messaging/manual_context_sources.py` |

낡은 이벤트 표, Solapi ID 표, 구현 예정 목록을 이 파일에 다시 복제하지 않는다. 위 정본 중 하나를 갱신하고 이 인덱스에는 경로만 남긴다.

## 2. 현재 핵심 정책

- 신규 카카오 알림톡 템플릿 검수/등록을 기본 제안하지 않는다. 기존 4종 ITEM_LIST 봉투 + `#{선생님메모}` 자유 본문 정책을 우선 적용한다.
- 모든 실발송은 공용 오너 알림톡만 사용한다. SMS/LMS, tenant별 PFID, tenant별 알림톡 provider는 신규 발송 경로에서 사용하지 않는다.
- SMS/LMS 예외는 없다. `check_dev_alerts`는 운영 룰을 평가해 설정된 Slack webhook으로만 알리며, SMS 설정·테스트·외부 신호 발송 옵션은 존재하지 않는다. 운영 절차는 `docs/operations/runbooks/incidents.md`가 정본이다.
- 신규 발송 경계는 `enqueue_alimtalk()` 하나다. 명시된 비알림톡 `message_mode`는 알림톡으로 보정하지 않고 차단하며, 기존 로그와 테넌트별 공급자/발신번호/키 값은 삭제하지 않고 이력 데이터로 보존한다.
- 계정 관련 시스템 알림(가입 승인, 아이디 찾기, 비밀번호 찾기)은 `send_alimtalk_via_owner()`를 통해 오너 테넌트 exact trigger 승인 템플릿으로 발송한다.
- 알림톡 템플릿 fallback은 금지한다. exact business event의 공용 승인 SID·버전·구조·변수 계약이 없으면 발송하지 않으며 카테고리·저장 문구·최신/기본 행으로 추론하지 않는다.
- 공용 트리거 운영 실발송 검증은 `scripts/v1/run-messaging-verify-send.ps1` → `messaging_verify_common_alimtalk`을 사용한다. 수동 UI 경로 검증은 프론트의 `e2e/stability/controlled-real-alimtalk-send.spec.ts`를 사용한다. 둘 다 수신번호를 `01031217466` 하나로 강제하며, 한 검증에서는 한 경로만 1회 실행하고 `NotificationLog.provider_message_id`와 공급사 최종 성공을 확인한다.
- `password_find_otp`는 legacy OTP 경로용 트리거다. 공개 로그인 화면의 현재 정본은 `/api/v1/auth/account-recovery/dispatch/`다.
- 수동/자동 발송 UX와 템플릿 본문 자유 정책은 [messaging-alimtalk.md](messaging-alimtalk.md)와 `backend/docs/ssot/messaging-policy.md`를 우선한다.
- 클리닉 변경 알림처럼 도메인 상태에서 파생되는 수동 발송 변수/대상자는 프론트에서 재구현하지 않고 `context_source`로 백엔드 정본에 위임한다.
- `context_source`가 만든 변수 키는 서버 계산값이 정본이다. 요청 `context`/`context_per_student`가 같은 키를 보내면 미리보기 API에서 거부한다.
- 수동 발송의 최종 카카오 미리보기는 preflight의 `preview_recipients[].full_message_body`가 정본이다. 이 값은 실제 Solapi replacements와 같은 서버 계산값으로 만들며, 클라이언트 샘플 문구로 대체하지 않는다.
- 수동 발송은 학생과 보호자를 독립적으로 선택하며 어느 한쪽을 정책상 기본 수신자로 강제하지 않는다. preflight는 tenant·actor·수신자·본문·저장 문구 버전·event·provider identity를 1회용으로 서명한다.
- 활성 owner/admin/teacher/staff(조교)가 같은 수동 발송 흐름을 사용한다. 공용 메시징 설정 변경 권한은 owner/admin으로 분리한다.

## 3. 변경 규칙

메시징 코드를 바꾸면 다음을 함께 확인한다.

1. `policy.py`의 정책 분류와 구현 여부가 실제 호출 경로와 맞는가.
2. `default_templates.py`의 변수명이 Solapi 승인 변수와 맞는가.
3. [messaging-alimtalk.md](messaging-alimtalk.md)의 봉투/편지 정책과 충돌하지 않는가.
4. [account-recovery.md](account-recovery.md)의 계정복구 발송 흐름과 충돌하지 않는가.
5. 수동 발송 컨텍스트가 도메인 상태에서 파생된다면 `manual_context_sources.py` 또는 해당 도메인 서비스가 정본인가.
6. `context_source` 기반 변수 키가 클라이언트 입력으로 덮이지 않는가.
7. 최종 미리보기가 실제 Solapi replacements 기반 서버 문구를 사용하고, 계약 누락 시 fail-close하는가.
8. 오래된 표나 legacy 안내를 추가하지 않았는가.

## 4. 정리 이력

- 2026-05-21: 2026-04-08 기준의 장문 이벤트 표를 제거하고 SSOT 인덱스로 전환. 최신 정책은 `policy.py`, `messaging-alimtalk.md`, `messaging-policy.md`, `account-recovery.md`로 분리.
- 2026-06-06: 공용 오너 알림톡 only 및 fallback 금지 정책을 현재 SSOT에 반영. provider id 로그와 통제번호 전용 운영 검증 명령 추가.
- 2026-07-26: 수동 발송의 서버 정본 `full_message_body`, 학생별 최종 카카오 미리보기, 통제번호 UI 실발송 경로를 반영.
- 2026-08-20: 운영자 SMS 예외를 폐기하고 제품·운영의 모든 휴대전화 실발송을 공용 카카오 알림톡으로만 고정.
- 2026-08-21: SMS 호환 callable·capability 필드·이름이 남은 throttle을 제거하고 비알림톡 입력을 API부터 worker까지 실패 폐쇄하도록 정리.
