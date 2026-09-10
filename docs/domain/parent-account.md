# 학부모 계정 SSOT

**상태:** Active
**최종 점검:** 2026-09-10
**코드 기준:** `apps/domains/parents/services/__init__.py`, `apps/domains/parents/models.py`, `apps/domains/student_app/permissions.py`, `apps/domains/submissions/views/homework_submission_media_view.py`, `apps/api/common/auth_jwt.py`

## 1. 계정 생성 규칙

| 항목 | 현재 규칙 |
|------|-----------|
| 내부 username | `p_{tenant_id}_{parent_phone}` |
| 로그인 입력값 | 학부모 전화번호 |
| 초기 비밀번호 | 직원이 직접 입력했거나 `random`을 명시 선택해 생성된 학생 초기 비밀번호. 가입 신청은 학생이 제출한 검증된 password hash |
| 변경 권장 | `must_change_password=True`; 로그인·API 사용은 차단하지 않음 |
| 이름 | 기존 Parent 이름 우선, 없으면 `{학생이름} 학부모` |
| 역할 | `TenantMembership.role = "parent"` |
| 생성 시점 | 학생 생성 SSOT에서 명시적 비밀번호/hash가 전달된 경우만 |

입력은 하이픈/공백을 제거한 뒤 `010` 11자리여야 하며, 짧거나 잘못된 번호를
전화번호 일부나 공용 비밀번호로 보정하지 않고 요청을 실패시킨다.
동일 테넌트의 학생·직원 등 다른 활성 계정이 그 학부모 번호를 로그인
아이디로 이미 사용하면 Parent/User를 새로 만들거나 연결하지 않고 충돌을
명시적으로 반환한다. 서로 다른 내부 username으로 저장된다는 이유로 복수
공개 로그인 후보를 만들지 않는다.

## 2. 생성/연결 플로우

```
학생 등록
  -> ensure_parent_account_for_student(tenant, parent_phone, student_name, initial_password | initial_password_hash)
    -> Parent(tenant + phone) 조회
    -> Parent 없음:
         User(username=p_{tenant_id}_{phone}, phone=phone, tenant=tenant) 생성
         password=호출자가 명시한 초기 비밀번호 또는 검증된 가입 password hash
         must_change_password=True
         Parent 생성
         TenantMembership(parent) 활성화
         result.password_for_notice = 실제 설정한 초기 비밀번호
    -> Parent 있음 + user 없음:
         기존 Parent.name 보존
         User 생성/연결
         TenantMembership(parent) 활성화
         result.password_for_notice = 실제 설정한 초기 비밀번호
    -> Parent 있음 + user 있음:
         기존 Parent 반환
         TenantMembership(parent) 활성 상태 보정
         result.password_for_notice = "변경되지 않음"
```

학생을 단건·JSON·Excel로 직접 등록하면서 4자 이상의 초기 비밀번호를 입력하면,
새 학생 계정과 새 학부모 계정에 같은 값을 설정하고 각각의 첫 수강 안내에도 같은
값을 staging한다. 가입 신청 승인은 학생이 제출한 검증된 hash를 새 학생과 새
학부모 계정에 동일하게 적용하고 안내에는 `가입 신청 시 입력한 비밀번호`라고
표시한다. 기존 학부모 계정의 비밀번호는 새 자녀를 등록해도 변경하지 않는다.

직원이 학생 수정에서 학부모 번호를 바꾸면 기존 학부모 계정은 비밀번호 변경 없이
연결한다. 해당 번호의 계정이 없다면 `parent_initial_password` 4자 이상이 있어야
새 계정을 만들며, 없으면 학생 수정 전체를 rollback한다.
번호가 바뀌지 않았더라도 Parent/User 또는 학생 연결이 누락된 경우에는 같은 직원
수정 화면에서 해당 번호와 명시적 초기 비밀번호를 다시 제출해 한 계정만 복구한다.
비밀번호가 없는 미완성 User만 그 입력으로 자격증명을 초기화한다. 이미 사용 가능한
비밀번호가 있는 User는 프로필 연결만 복구하고 입력값으로 비밀번호를 덮어쓰지 않는다.
학생 본인 프로필에서는 학부모 번호와 Parent 연결을 바꿀 수 없다.
잘못된 연결이 성적·출결·영상 권한으로 이어지지 않도록 직원이 exact 학생을
확인한 뒤 관리자/교사 학생 수정 흐름에서만 변경한다.

학생 전화번호가 학부모 전화번호와 정확히 같으면 그 값은 학부모 연락처로만
취급한다. 학생 계정 그래프는 별도 `ps_number`로 유지하되 `Student.phone`과
`User.phone`은 비우므로, 학부모 번호가 학생 수신처나 학생 전화 로그인 ID로
중복 등록되지 않는다. 이후 직원이 실제 학생 번호를 입력하는 전환 규칙과 legacy
교정 명령은 [student-core.md](student-core.md)가 정본이다.

동일 테넌트에서 같은 학부모 번호를 가진 학생 둘을 동시에 등록할 수 있다.
서비스는 Parent row를 잠그고, 신규 row 경합은 DB의 User username 및
`uniq_parent_phone_per_tenant` 유일 제약 충돌 후 한 번 재조회한다. 두 요청은
같은 Parent/User/Membership을 반환하며 초기 비밀번호를 실제로 만든 요청만
`user_created=True`와 안내용 초기 비밀번호를 갖는다. 기존 User/Parent의
비밀번호는 idempotent ensure 중 다시 설정하지 않는다.

학생 생성 경로의 Parent/User/Student/Membership 계정 그래프는
[student-creation.md](student-creation.md)가 정본이다.

### 2.1 선택 자녀 학습 대리행위

학부모는 학생 앱에서 활성 연결 자녀를 명시적으로 선택한 뒤 그 자녀의 온라인 시험
답안과 과제 사진·영상을 제출할 수 있다. 요청은 항상 `X-Student-Id`를 포함해야 하며,
서버는 해당 자녀가 현재 테넌트에서 이 학부모에게 연결돼 있는지와 활성 수강·시험 또는
과제 배정을 모두 다시 검증한다. 헤더 누락, 연결되지 않은 학생, 다른 테넌트 학생,
다른 자녀의 수강 ID는 다른 자녀로 보정하지 않고 쓰기 전에 거절한다.

`GET /api/v1/core/me/`는 현재 테넌트의 활성 연결 자녀 전체를 ID 오름차순의
`linkedStudents` 배열로만 반환한다. 첫 행을 기본 자녀로 의미화하던
`linkedStudentId`, `linkedStudentName` 단수
필드는 제거됐다. 자녀가 한 명이면 프론트가 그 유일한 ID를 확정할 수 있지만, 여러 명이면
저장된 유효한 직접 선택 또는 새 사용자 선택 전까지 학생 범위 API를 호출하지 않는다.

대리 제출의 `Submission.user`는 학부모가 아니라 선택 자녀의 로그인 User다. 따라서
성적·미제출 상태·교사 검수함 같은 후속 투영은 학생 본인 제출과 동일하게 이어진다.
실제 요청자가 자녀 User와 다르면 `Submission.meta.submitted_by_user_id`에 학부모 User
ID를 남긴다. 학생이 먼저 만든 기존 과제 제출에 학부모가 파일을 추가하는 경우에도
같은 메타데이터를 보완한다. 과제 파일 삭제의 `removed_by`도 실제 요청자를 기록한다. 완료·통과 후
변경 잠금과 파일 형식·용량·중복·재시도 규칙은 학생 제출과 동일하다.

선택 자녀의 질문·상담 작성·수정·삭제와 첨부 관리, 학생 인벤토리 폴더/파일 관리,
성적표 제출, 영상 진행률 저장도 같은 `X-Student-Id` 검증을 사용한다. 생성된 글·파일·
성적·영상 진행률은 자녀 데이터로 조회되며 실제 학부모 작성 글은 `author_role=parent`,
성적표는 `submitted_by` 학부모를 보존한다. 누락·미소유·삭제·다른 tenant 자녀나 payload의
다른 학생 식별자는 쓰기 전에 실패 폐쇄한다. 자녀 프로필은 읽기 전용이며 학부모
비밀번호 변경은 학부모 자신의 계정에만 적용한다. 학생 비밀번호·아이디·프로필 및
관리자 설정은 위임 범위가 아니다.

계정 생성은 `ensure_parent_account_for_student()` 한 경로만 사용한다. 공개 계정 복구는
`find_parent_account()`로 조회만 하며 계정을 만들지 않는다. 교직원의 삭제 학생 복원은
정상 계정을 그대로 연결하고, 누락·사용불가 계정에 한해서만 화면에서 받은 명시적 초기
비밀번호로 같은 생성 경로를 호출한다. 임의 비밀번호를 만들거나 전화번호 뒤 4자리를
사용하지 않는다.

## 3. 로그인

```
POST /api/v1/token/
Headers: X-Tenant-Code: {tenant_code}
Body: { "username": "{학부모전화번호}", "password": "{비밀번호}" }
```

테넌트 바인딩은 JWT 발급 과정에서 검증된다. 내부 username(`p_{tenant_id}_{phone}`)은 공개 로그인 입력값이 아니다.

## 4. 계정 복구와의 관계

공개 로그인 화면의 아이디/비밀번호 찾기는 [account-recovery.md](account-recovery.md)가 정본이다.

- 학부모 아이디 찾기: 학생 이름 + 등록 학부모 전화번호가 유일하게 일치할 때 전화번호로 아이디 안내를 보낸다.
- 학부모 비밀번호 찾기: 동일 검증 후 6자리 숫자 임시 비밀번호를 pending reset으로 발급한다. 실제 비밀번호 변경과 `must_change_password=True` 적용은 학부모가 임시 비밀번호로 로그인할 때 수행한다.
- Parent/User 계정이 없거나 연결이 불완전하면 공개 복구에서 생성하지 않고 generic 성공 응답으로 닫는다. 직원이 exact 학생을 확인한 뒤 명시적 초기 비밀번호로 별도 복구해야 한다.

## 5. 첫 수강 확정 계정 안내 알림톡

학부모 계정 안내는 `registration_approved_parent` 트리거를 사용한다. 학생 마스터 생성이나 가입 승인만으로는 발송하지 않고, 변경 후 생성된 학생의 첫 ACTIVE 수강이 확정된 뒤 한 번만 발송한다.
이 트리거는 `SYSTEM_AUTO`이므로 legacy `AutoSendConfig.enabled=False`가 남아 있어도 발송을 막지 않는다. 다만 공용 owner의 exact APPROVED 학부모 템플릿이 없으면 발송하지 않고 pending 안내값을 유지한다. 두 번째 이후 수강 등록은 현재 비밀번호를 재발송하지 않는다.

| 변수 | 값 |
|------|----|
| `#{학부모아이디}` | 학부모 전화번호 |
| `#{학부모비밀번호}` | 직접 입력/명시 생성된 임시 비밀번호, 가입 신청 시 입력값 안내 문구, 아이디 찾기·기존 계정 연결 시 `변경되지 않음` |
| `#{학생아이디}` | 학생 `ps_number` |
| `#{학생비밀번호}` | 가입 승인/학생 안내 값 또는 `변경되지 않음` |
| `#{비밀번호안내}` | 상황별 안내 문구 |

계정/비밀번호 복구 발송 정책은 `send_alimtalk_via_owner()`를 따른다. SMS fallback과 템플릿 fallback은 없다.
학부모 본인이 비밀번호를 변경해 특정 자녀 문맥이 없는 경우에는 학부모 계정 정보만
안내한다. 연결 목록의 첫 행이나 최근 행을 골라 학생 이름·아이디를 채우지 않는다.
첫 수강 확정 계정 안내 경로는 큐 payload에
`event_type=registration_approved_student|registration_approved_parent`를 실어
운영 로그가 계정성 알림으로 분류되게 한다. 이 분류는
`NotificationLog.message_body` 보안 마스킹의 기준이므로 신규 가입 안내 발송 경로에서
생략하면 안 된다. 학생/학부모 비밀번호 안내값은 수강 전까지 별도 암호문으로 보관하며, 유효 수신자 전체의 durable outbox가 확보되면 즉시 제거한다.

## 6. 운영 복구

전화번호에서 비밀번호를 파생하거나 누락 계정을 일괄 생성·초기화하는 management
command는 제공하지 않는다. 누락된 legacy Parent/User는 tenant, 학생, 학부모 번호,
기존 연결을 exact하게 확인한 뒤 한 계정씩 명시적 초기 비밀번호로 복구하고, 계정
안내 알림톡과 실제 로그인을 확인한다. 기존 계정이면 비밀번호를 추측해 덮어쓰지
않고 직원 비밀번호 재설정 화면에서 입력한 값으로만 변경한다.

집중 검증:

```powershell
python manage.py test apps.domains.parents.tests.test_account_creation
python manage.py test apps.domains.parents.tests.test_account_creation_concurrency_pg
```

두 번째 테스트는 PostgreSQL row/unique-lock 동작을 검증하므로 SQLite에서는
skip이 정답이다.
