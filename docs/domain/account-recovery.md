# 계정 복구 SSOT

**상태:** Active
**최종 점검:** 2026-09-10
**코드 기준:** `apps/core/views/account_recovery.py`, `apps/domains/students/services/account_recovery.py`, `apps/domains/students/views/account_notification_views.py`, `apps/core/services/password.py`, `apps/api/common/auth_jwt.py`, `apps/domains/students/views/password_views.py`

## 1. 정본 경로

공개 로그인 화면의 아이디 찾기/비밀번호 찾기는 하나의 계정복구 경로를 사용한다.

```
POST /api/v1/auth/account-recovery/dispatch/
{
  "mode": "username" | "password",
  "target": "student" | "parent",
  "student_name": "...",
  "phone": "01012345678"
}
```

프론트 정본:

- `frontend/src/auth/pages/AccountRecoveryModal.tsx`
- `frontend/src/auth/api/recovery.api.ts`

백엔드 정본:

- HTTP entry: `apps/core/views/account_recovery.py`
- domain service: `apps/domains/students/services/account_recovery.py`
- password SSOT: `apps/core/services/password.py`

## 2. 보안/테넌트 원칙

- `AllowAny` 엔드포인트지만 `TenantResolved`가 필수다.
- `AlimtalkEndpointThrottle`을 적용한다.
- 요청 전화번호는 숫자만 남긴 뒤 `010`으로 시작하는 11자리만 허용한다.
- 조회 실패, 동명이인/공유번호 등 다건 매칭, 성공, 계정 확인 후 알림톡 예약 실패 모두 공개 응답은 generic 200 message로 통일한다.
- API 응답에 아이디나 비밀번호를 직접 반환하지 않는다.
- 안내 발송은 검증된 전화번호로만 수행한다. 이메일은 사용하지 않는다.
- legacy 공개 호환 경로도 정본 계정복구 서비스로 위임하며, 사용자 존재 여부를 노출하지 않는다. OTP 인증번호 방식은 410으로 봉인한다.
- 공개 요청의 알림톡 예약 실패는 전화번호/이름을 남기지 않는 서버 warning과 메시징 운영 로그로 추적한다. 공개 API에서 503이나 내부 실패 원인을 반환하지 않는다. 인증된 staff reset/계정 안내는 운영자가 재시도 판단을 해야 하므로 400/503을 유지한다.

## 3. 매칭 규칙

### 가입 신청의 삭제 이력 복구

학생 셀프 회원가입은 `initial_password`와 write-only
`password_confirmation`의 exact 일치를 서버에서 먼저 검증한다. 불일치하면
가입신청을 포함해 Student/User/membership 쓰기가 0건이며, 확인값 원문·별도 해시는
저장하거나 로그로 남기지 않는다. 일치하면 기존처럼 `initial_password`의 강한
Django hash 하나만 가입신청에 저장한다.

일반 승인은 삭제 학생을 자동 선택하지 않는다. 현재 테넌트에서 exact 복원
식별값이 맞는 삭제 후보를 인증된 staff에게만 제시하고, staff가 한 후보를 명시해
`POST /api/v1/students/registration_requests/{id}/resolve_deleted/`에
`student_id`를 보낸다. 서비스는 Parent → Users(sorted) → Student → membership
순서로 잠근 뒤 후보와 요청을 다시 비교한다. active/cross-tenant/변경·누락 graph와
이미 처리된 재시도는 409로 닫힌다.

성공 시 새 Student/User를 만들지 않고 선택한 기존 계정을 복원해 이번 신청의
로그인 ID와 저장된 가입 hash를 채택하고 기존 토큰을 폐기한다. 과거와 현재의 모든
`StudentRegistrationRequest.student` 연결은 같은 Student에 그대로 남고 Enrollment
행과 연결 이력도 그대로 남는다. Enrollment 상태 복원은 별도 구현하지 않고 학생
lifecycle 정본을 따른다. 삭제 전 `ACTIVE`/`PENDING`/`INACTIVE` snapshot을 복원하되,
종료되거나 비활성인 강의는 `INACTIVE`로 닫고 내부 snapshot marker는 한 번 사용한 뒤
비운다. 이 명시적 복구 자체는 알림톡, notification/outbox, provider
send/retry/requeue를 생성하지 않는다.

### 학생 대상

학생 이름 + 학생 전화번호 또는 학부모 전화번호가 현재 테넌트의 active student와 유일하게 일치해야 한다.

```
Student(tenant, deleted_at is null, name__iexact)
  where phone == 요청번호 or parent_phone == 요청번호
  exactly one match
  student.user exists
```

아이디 안내에는 학생 `ps_number`를 우선 사용하고, 없으면 user display username을 사용한다.

### 학부모 대상

학생 이름 + 등록 학부모 전화번호가 현재 테넌트의 active student와 유일하게 일치해야 한다.

```
Student(tenant, deleted_at is null, name__iexact, parent_phone == 요청번호)
  exactly one match
  Parent(tenant, phone)와 연결 User가 이미 존재
```

학부모 공개 아이디는 전화번호다.

## 4. mode별 동작

### `mode=username`

- 비밀번호를 변경하지 않는다.
- 학생은 `registration_approved_student`, 학부모는 `registration_approved_parent` 트리거를 재사용한다.
- 비밀번호 변수에는 `변경되지 않음`을 넣는다.
- `#{비밀번호안내}`로 비밀번호를 잊었으면 비밀번호 찾기를 사용하라고 안내한다.

### `mode=password`

- `generate_temp_password()`로 6자리 숫자 임시 비밀번호를 만든다.
- 공개 계정복구는 즉시 비밀번호를 바꾸지 않고 `PendingPasswordReset`에 임시 비밀번호 해시를 저장한다.
- 학생은 `password_reset_student`, 학부모는 `password_reset_parent` 트리거로 발송한다.
- enqueue 실패 시 이번 요청의 pending reset을 되돌린다. 기존 pending reset이 없으면 삭제하고, 이미 발급된 기존 pending reset이 있으면 복원한다.
- 사용자가 알림톡의 임시 비밀번호로 로그인하면 `TenantAwareTokenObtainPairSerializer`가 pending reset을 소비하고 그때 `force_reset_password()`를 적용한다.
- pending reset이 소비되면 실제 비밀번호가 임시 비밀번호로 바뀌고 `token_version`이 증가하며 `must_change_password=True`가 된다.
- 비활성/로그인 불가 계정은 pending reset을 소비하지 않는다.
- pending reset이 만료되거나 워커/공급자 단계에서 발송 실패하더라도 기존 비밀번호는 그대로 유지된다.

임시 비밀번호 길이/형식 SSOT:

```
apps/core/services/password.py
TEMP_PASSWORD_LENGTH = 6
generate_temp_password() -> 숫자 6자리
```

비밀번호 최소 길이는 현재 `apps/core/services/password.py`와 계정 정책에 따라 4자를 유지한다. 자동 임시 비밀번호가 6자리인 것은 알림톡을 보고 직접 입력하는 학부모/학생 사용성을 위한 운영 정책이며 최소 길이 상향이 아니다.

## 5. Pending reset 안전 구조

공개 계정복구의 상품 안전 목표는 "알림톡을 못 받은 사용자를 기존 계정에서도 잠그지 않는 것"이다.

| 단계 | 상태 |
|------|------|
| 요청 성공 + enqueue 성공 | 임시 비밀번호 해시가 `PendingPasswordReset`에 저장됨. 기존 비밀번호는 계속 유효 |
| 사용자가 임시 비밀번호로 로그인 | pending reset 소비 → 실제 비밀번호 변경 → `must_change_password=True` |
| enqueue 실패 | 이번 요청의 pending reset 롤백. 기존 pending reset이 있으면 복원. 기존 비밀번호 유지 |
| 워커/공급자 단계 실패 | pending reset은 남아 있을 수 있으나 기존 비밀번호는 유지. 운영 확인은 NotificationLog/공급자 로그로 판단 |
| 만료 후 임시 비밀번호 로그인 | pending reset 삭제 후 일반 로그인 실패 처리 |

보관 원칙:

- plaintext 임시 비밀번호는 DB에 저장하지 않는다. pending reset은 Django password hash만 저장한다.
- `NotificationLog.message_body`는 계정/인증 트리거 본문을 저장하지 않고 보안 placeholder로 마스킹한다.
- 사용자별 pending reset은 1개만 유지한다. 새 요청은 이전 pending reset을 대체한다.
- 공개 임시비밀번호 요청은 대상 User row를 잠근 트랜잭션 안에서 pending reset과 알림톡 예약을 함께 처리한다. 같은 계정의 동시 요청은 순서대로 처리되며, 발송 실패 요청이 다른 요청의 성공한 pending reset을 복원/덮어쓰지 않는다.
- 관리자/선생님 또는 학생/학부모 본인이 인증된 상태에서 수행하는 학생/학부모 비밀번호 변경은 즉시 reset 경로를 사용한다.
- 관리자/선생님 reset은 4자 이상 임시 비밀번호를 반드시 직접 입력한다. 빈값을 자동 생성하거나 전화번호에서 파생하지 않는다.
- 본인 변경은 `change_password_with_notice()`가 User row를 잠근 뒤 현재 비밀번호를 다시 확인한다. 따라서 같은 기존 비밀번호를 사용한 동시 요청은 하나만 성공한다.
- 본인 변경과 staff reset은 비밀번호 hash, `must_change_password`, `token_version`, pending reset 정리, durable 알림톡 예약을 한 DB 트랜잭션으로 처리한다. 알림톡 예약 실패 시 이 상태가 모두 이전 값으로 돌아가며 현재 세션도 유지된다.
- 이미 발급된 pending 임시 비밀번호는 본인 비밀번호 변경이나 staff/owner 강제
  초기화가 성공하는 즉시 폐기한다. 알림톡 예약 실패로 상위 트랜잭션이
  롤백되면 기존 pending reset도 함께 복구된다.
- pending 임시 비밀번호의 로그인 소비는 User row를 먼저 잠근 뒤 pending row를
  다시 확인한다. 따라서 staff 초기화와 동시에 로그인해도 이전 pending 값이
  더 늦게 새 비밀번호를 덮어쓸 수 없다.
- 운영 복구 명령 `fix_user_password`도 같은 초기화 정본을 사용한다. 기존
  계정은 `token_version` 증가와 pending reset 폐기를 함께 적용하고, 신규·기존
  모두 다음 로그인에서 본인 비밀번호 변경을 권장한다. 로그인과 다른 API는
  계속 사용할 수 있다. `--cleanup-bare` 삭제는
  대체 계정 비밀번호·membership 갱신과 같은 트랜잭션이며, 학원 조회나 후속
  갱신이 실패하면 명령도 실패하고 삭제를 포함한 변경 전체를 롤백한다.

### 로그인 비밀번호 입력 보존

로그인 비밀번호는 본인 변경·관리자 초기화·pending 임시 비밀번호에 저장된 입력과
정확히 같은 값으로 검증한다. `TenantAwareTokenObtainPairSerializer`는 SimpleJWT가
동적으로 생성한 password 필드의 `trim_whitespace=False`를 설정해 앞뒤 공백·탭·
유니코드 공백을 제거하지 않는다. 로그인 ID의 정규화와 비밀번호 처리는 별개다.
기존 비밀번호 hash나 계정 상태는 수정하지 않으며, 입력값을 trim한 뒤 재시도하는
fallback도 두지 않는다. 공백이 없는 비밀번호 앞뒤에 공백을 추가한 입력은 거절한다.
실제 JWT 발급 → 본인 변경 → 기존 access/refresh 거절 → 새 비밀번호 재로그인 →
사용자/학생 정보 조회 및 발송 실패 롤백은
`apps/api/common/tests/test_password_login_roundtrip.py`에서 검증한다.

## 5.1 Refresh token 계정 상태 검증

`POST /api/v1/token/refresh/`는 `TenantAwareTokenRefreshView`가 소유한다. 서명과
만료 검증만 하는 기본 SimpleJWT refresh view를 직접 사용하지 않는다.

- refresh token에 `user_id`, `tenant_id`, `token_version` claim이 모두 있어야 한다.
- User와 Tenant가 활성이고, claim의 `token_version`이 현재 User 값과 같아야 한다.
- claim tenant에 유효한 `TenantMembership`과 역할별 학생/학부모 profile이 있어야 한다.
- 비밀번호 변경, 계정 비활성화, membership 제거로 위 조건이 깨지면 refresh는 401로 닫힌다. 새 access/refresh token을 발급하거나 기존 refresh token을 회전시키지 않는다.
- 프론트 세션 종료/복귀 계약은 `frontend/docs/ACCOUNT-CREDENTIAL-FLOWS.md`가 소유한다.

## 6. 알림톡 발송

계정복구 발송은 `send_alimtalk_via_owner()`를 사용한다.

- 오너 테넌트의 승인된 알림톡 템플릿으로 발송한다.
- SMS fallback은 없다.
- `password_reset_*`, `password_find_otp` 템플릿이 승인되지 않았으면 발송 실패가 정답이다. 가입 승인 템플릿 등 다른 trigger로 fallback하지 않는다.
- 테스트 테넌트의 메시징 disabled 상태에서는 실제 발송을 건너뛰고 성공으로 간주한다. 비밀번호 복구는 전달되지 않는 임시 비밀번호를 만들지 않기 위해 pending reset도 만들지 않는다.
- 계정 아이디 변경, 학부모 전화번호 변경/계정 연결, 학생 전화번호 최초 등록은 `account_notifications.py`를 통해 `registration_approved_*` exact trigger로 발송한다. 비밀번호가 바뀌지 않은 경우 비밀번호 변수는 `변경되지 않음`이다.

### 인증된 관리자·선생님의 아이디 안내

```
GET  /api/v1/students/<student_id>/account-notifications/?limit=5
POST /api/v1/students/<student_id>/account-notifications/
{
  "target": "student" | "parent"
}
```

- 현재 테넌트의 인증된 staff만 사용하고, `<student_id>`를 정확히 조회한다. 다른 테넌트 학생은 404로 닫힌다.
- GET은 최근 계정 알림톡 이력만 조회하고, POST는 선택한 학생 또는 학부모 계정의 아이디를 다시 안내한다.
- POST는 `registration_approved_student`/`registration_approved_parent` 승인 템플릿을 재사용하며 비밀번호 변수는 `변경되지 않음`으로 보낸다.
- 아이디 안내는 현재 비밀번호, `must_change_password`, `token_version`, `PendingPasswordReset`, 이미 발급된 세션을 절대 변경하지 않는다. 비밀번호 초기화는 별도 화면과 staff reset 경로로만 수행한다.
- 학생 대상은 학생 전화번호를 우선하고, 없으면 기존 학부모 전화번호 fallback 계약을 따른다. 학부모 대상은 등록된 학부모 전화번호와 연결된 학부모 계정을 사용한다.
- 잘못된 target·수신처/계정 없음은 400, 발송 실패는 503으로 응답하고 비밀번호 상태는 그대로 유지한다.

## 7. Legacy compatibility

아래 경로는 기존 호출처 호환을 위해 남아 있다. 신규 공개 복구 UI는 추가하지 않는다.

| 경로 | 현재 용도 |
|------|-----------|
| `/api/v1/students/password_find/request/` | legacy OTP 봉인: 410 Gone |
| `/api/v1/students/password_find/verify/` | legacy OTP 봉인: 410 Gone |
| `/api/v1/students/password_reset_send/` | 관리자/선생님 비밀번호 재설정 + 공개 비밀번호 복구 호환. 모두 정본 서비스 위임 |
| `/api/v1/students/send_existing_credentials/` | legacy 중복가입 자격 안내 호환 |
| `/api/v1/students/<id>/account-notifications/` | GET: 학생 상세 최근 계정 알림톡 상태, POST: 학생/학부모 아이디 안내 |

legacy 공개 호환 규칙:

- 비인증/비staff `password_reset_send` 요청은 정본 비밀번호 복구 서비스로 위임하며 pending reset을 사용한다.
- `send_existing_credentials` 요청은 정본 비밀번호 복구 서비스로 위임하며 pending reset을 사용한다.
- 인증된 관리자/선생님 `password_reset_send` 요청은 정본 계정복구 서비스의 staff reset 경로로 위임한다. `temp_password` 4자 이상이 필수이며 비밀번호는 즉시 변경하고, 알림톡 발송 실패 시 비밀번호와 pending reset을 롤백한다.
- 공개 호환 요청의 발송 대상은 요청자가 증명한 전화번호다. 저장된 다른 학생/학부모 번호로 대체 발송하지 않는다.
- 조회 실패, 다건 매칭, parent side-effect 차단 케이스는 generic 200으로 응답하고 비밀번호를 변경하지 않는다.
- `temp_password`는 인증된 관리자/선생님 요청에서만 즉시 변경값으로 사용한다. `skip_notify` 옵션은 제거됐으며 계정 알림을 끄는 경로는 없다.

계정 알림톡 로그:

- `NotificationLog.source_tenant_id`, `target_type`, `target_id`, `target_name`으로 오너 대리발송 로그를 원 테넌트/학생에 다시 연결한다.
- 학생 계정 target id는 `student:<student_id>`, 학부모 계정 target id는 `parent:<student_id>:<parent_phone>` 형식이다.
- 학생 상세 화면은 본문/임시 비밀번호를 표시하지 않고 발송 시각, 종류, 성공/실패, 마스킹 수신자만 표시한다.

이 경로를 수정하는 작업은 정본 서비스(`apps/domains/students/services/account_recovery.py`, `apps/domains/students/services/account_notifications.py`, `apps/core/services/password.py`)로 위임하거나, 호출처가 사라진 뒤 제거한다. 새 기능은 legacy view에 직접 추가하지 않는다.

## 8. 상품/파괴 테스트 기준

- 단위/통합: `python -m pytest apps\domains\students\tests\test_account_recovery.py apps\domains\students\tests\test_password_reset_safety.py -v --tb=short -x`
- 로그인 활성화: pending 임시 비밀번호로 `/api/v1/token/` 로그인이 성공하고 `must_change_password=True` 토큰이 발급되며 일반 API도 차단되지 않는지 확인한다.
- 기존 비밀번호 보호: 발송 실패, unknown account, ambiguous match, 워커/공급자 실패 상황에서 기존 비밀번호가 유지되는지 확인한다.
- 개인정보 보호: unknown/ambiguous/success/delivery failure 공개 응답은 status와 generic message로 구분되지 않아야 한다.
- 본인 변경 원자성: 알림톡 예약 실패 전후의 비밀번호, `must_change_password`, `token_version`이 모두 동일해야 한다.
- 동시 본인 변경: PostgreSQL에서 같은 기존 비밀번호로 동시에 요청해도 하나만 성공하고 `token_version`은 한 번만 증가해야 한다.
- 동시 공개 복구: PostgreSQL에서 같은 계정의 두 요청이 User row lock으로 직렬화되고 마지막으로 발송 예약된 pending 비밀번호만 유효해야 한다.
- refresh 폐쇄: stale `token_version`, 비활성 User/Tenant/membership/profile의 refresh가 401이며 회전 token을 반환하지 않아야 한다.
- staff 아이디 안내: 선택 target에 대한 exact trigger/마스킹 수신자를 확인하고, 발송 전후 비밀번호·`must_change_password`·`token_version`·pending reset이 유지되는지 검증한다.
- staff 테넌트 경계: 다른 테넌트의 `<student_id>`는 404이고 발송 부수효과가 없어야 한다.
- 반복 요청: 새 pending reset이 이전 pending reset을 대체해야 한다.
- 실패 복원: 이미 발급된 pending reset이 있을 때 다음 발송이 실패하면 기존 pending reset이 유지되어야 한다.
- 실발송: 운영 설정에서 실제 알림톡 enqueue와 워커 발송 성공을 확인해야 한다.
- 단말 확인: 실사용 번호로 받은 알림톡 본문을 확인해야 상품 QA가 닫힌다.
- 테스트 데이터는 `[E2E-{timestamp}]` 태그를 사용하고 cleanup한다.
