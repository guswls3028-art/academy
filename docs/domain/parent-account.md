# 학부모 계정 SSOT

**상태:** Active
**최종 점검:** 2026-09-07
**코드 기준:** `apps/domains/parents/services/__init__.py`, `apps/domains/parents/models.py`, `apps/domains/student_app/permissions.py`, `apps/domains/submissions/views/homework_submission_media_view.py`, `apps/api/common/auth_jwt.py`

## 1. 계정 생성 규칙

| 항목 | 현재 규칙 |
|------|-----------|
| 내부 username | `p_{tenant_id}_{parent_phone}` |
| 로그인 입력값 | 학부모 전화번호 |
| 초기 비밀번호 | 직원이 학생 등록에 입력한 초기 비밀번호. 독립 ensure/복구는 학부모 전화번호 숫자 기준 마지막 4자리 |
| 변경 권장 | `must_change_password=True`; 로그인·API 사용은 차단하지 않음 |
| 이름 | 기존 Parent 이름 우선, 없으면 `{학생이름} 학부모` |
| 역할 | `TenantMembership.role = "parent"` |
| 생성 시점 | 학생 생성 SSOT 또는 계정복구 중 parent ensure 호출 |

`PARENT_DEFAULT_PASSWORD = "0000"` 상수는 외부 import 호환용 deprecated 값이다. 신규 코드에서 초기 비밀번호로 사용하지 않는다.
입력은 하이픈/공백을 제거한 뒤 `010` 11자리여야 하며, 짧거나 잘못된 번호를
`0000` 또는 다른 공용 비밀번호로 보정하지 않고 요청을 실패시킨다.

## 2. 생성/연결 플로우

```
학생 등록 또는 legacy 학부모 계정 복구
  -> ensure_parent_account_for_student(tenant, parent_phone, student_name, initial_password?)
    -> Parent(tenant + phone) 조회
    -> Parent 없음:
         User(username=p_{tenant_id}_{phone}, phone=phone, tenant=tenant) 생성
         password=입력된 학생 초기 비밀번호 또는 parent_initial_password(phone)
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
값을 staging한다. 가입 신청 승인은 학생이 제출한 hash만 보유하므로 학부모 평문을
재사용하지 않고 전화번호 뒤 4자리 fallback을 적용한다. 기존 학부모 계정의
비밀번호는 새 자녀를 등록해도 변경하지 않는다.

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

`ensure_parent_for_student()`는 기존 호출부 호환용 facade다. 알림톡·운영 안내처럼
비밀번호 안내 문구가 필요한 신규 경로는 반드시 `ensure_parent_account_for_student()`
결과의 `password_for_notice`를 사용한다.

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
- legacy Parent row에 user가 없으면, 복구 과정에서 `ensure_parent_for_student()`로 계정을 생성/연결한다.

## 5. 첫 수강 확정 계정 안내 알림톡

학부모 계정 안내는 `registration_approved_parent` 트리거를 사용한다. 학생 마스터 생성이나 가입 승인만으로는 발송하지 않고, 변경 후 생성된 학생의 첫 ACTIVE 수강이 확정된 뒤 한 번만 발송한다.
이 트리거는 `SYSTEM_AUTO`이므로 legacy `AutoSendConfig.enabled=False`가 남아 있어도 발송을 막지 않는다. 다만 공용 owner의 exact APPROVED 학부모 템플릿이 없으면 발송하지 않고 pending 안내값을 유지한다. 두 번째 이후 수강 등록은 현재 비밀번호를 재발송하지 않는다.

| 변수 | 값 |
|------|----|
| `#{학부모아이디}` | 학부모 전화번호 |
| `#{학부모비밀번호}` | 직접 학생 등록 시 입력한 초기 비밀번호, 독립 생성 시 전화번호 뒤 4자리, 아이디 찾기 시 `변경되지 않음` |
| `#{학생아이디}` | 학생 `ps_number` |
| `#{학생비밀번호}` | 가입 승인/학생 안내 값 또는 `변경되지 않음` |
| `#{비밀번호안내}` | 상황별 안내 문구 |

계정/비밀번호 복구 발송 정책은 `send_alimtalk_via_owner()`를 따른다. SMS fallback과 템플릿 fallback은 없다.
첫 수강 확정 계정 안내 경로는 큐 payload에
`event_type=registration_approved_student|registration_approved_parent`를 실어
운영 로그가 계정성 알림으로 분류되게 한다. 이 분류는
`NotificationLog.message_body` 보안 마스킹의 기준이므로 신규 가입 안내 발송 경로에서
생략하면 안 된다. 학생/학부모 비밀번호 안내값은 수강 전까지 별도 암호문으로 보관하며, 유효 수신자 전체의 durable outbox가 확보되면 즉시 제거한다.

## 6. 유지보수 명령

누락된 legacy Parent/User 계정은 다음 명령으로 테넌트 하나씩 복구한다. 기본은
dry-run이며 실제 실행은 같은 tenant code를 두 번 명시해야 한다. 유효한 010 11자리
학부모 번호의 누락 계정만 `ensure_parent_account_for_student()`로 생성하고, 이미
연결된 학부모 User의 비밀번호는 절대 바꾸지 않는다.

```
python manage.py ensure_parent_accounts_for_students --tenant <code>
python manage.py ensure_parent_accounts_for_students --tenant <code> --execute --confirm <code>
```

`apps/domains/parents/management/commands/reset_all_parent_passwords.py`는 legacy 일괄 정비용 명령이다.
전체 테넌트 대상 실행은 금지하며, 대상 학원과 dry-run row 수를 먼저 고정해야 한다.

```
python manage.py reset_all_parent_passwords --tenant-code <code>
python manage.py reset_all_parent_passwords --tenant-code <code> --apply --confirm-count <count>
```

`--apply`가 없으면 항상 dry-run이다. 실제 실행은 현재 대상 수가
`--confirm-count`와 정확히 일치할 때만 진행한다. 출력은 초기 비밀번호와 동일한
전화번호 끝 네 자리를 노출하지 않는다. 실행된 계정은 `token_version` 증가,
`must_change_password=True` 변경 권장 상태, 기존 pending reset 폐기를 함께 적용한다. 신규 초기
비밀번호 정책은 이 명령이 아니라 `parent_initial_password()`가 SSOT다.

집중 검증:

```powershell
python manage.py test apps.domains.parents.tests.test_account_creation
python manage.py test apps.domains.parents.tests.test_account_creation_concurrency_pg
python manage.py test apps.domains.parents.tests.test_password_reset_command
```

두 번째 테스트는 PostgreSQL row/unique-lock 동작을 검증하므로 SQLite에서는
skip이 정답이다.
