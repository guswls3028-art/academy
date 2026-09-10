# 학생 생성 SSOT

**상태:** Active  
**최종 점검:** 2026-09-10
**코드 기준:** `apps/domains/students/services/creation.py`, `apps/domains/students/services/registration_approval.py`, `apps/domains/students/services/import_students.py`, `apps/domains/students/services/import_passwords.py`, `apps/domains/students/services/custom_fields.py`, `apps/domains/students/views/student_views.py`, `apps/domains/students/views/registration_views.py`, `apps/domains/students/services/lecture_enroll.py`, `apps/domains/students/services/bulk_from_excel.py`

## 1. 책임 경계

학생 생성의 계정 그래프 SSOT는 `create_student_account()`다.

이 서비스가 소유하는 것:

- `ensure_parent_account_for_student()` 호출과 Parent 연결
- 학생 `User` 생성
- 학생 비밀번호 설정 또는 가입 신청의 기존 password hash 이전
- `Student` 생성
- `TenantMembership(role="student")` 활성화
- 학부모 안내용 비밀번호 문구 반환
- 첫 수강 전 학생·학부모 계정 안내값 암호화 staging

이 서비스가 소유하지 않는 것:

- serializer/API 입력 검증
- 활성/삭제 학생 중복 정책
- 삭제 학생 복원 또는 delete-and-recreate 결정
- 가입 신청 상태 전이(`approve_registration_request()`가 소유)
- Excel/R2/AI job dispatch
- HTTP 응답 모양

가입 신청 승인의 durable orchestration SSOT는 `approve_registration_request()`다. 이 서비스는 `pending -> approved` 전이와 학생 계정 생성 그래프 호출을 하나의 트랜잭션으로 처리한다. 승인만으로 알림톡을 보내지 않으며, 첫 수강 확정 후 발송할 비밀번호 안내 문구를 암호화해 학생에 staging한다.

학생 등록 Excel/import/JSON bulk row orchestration SSOT는 `import_students_from_rows()`, `resolve_student_import_row()`, `resolve_student_import_conflicts()`다. 이 서비스는 학생 등록 행의 중복/복원/생성 판단, school_level_mode 검증, 계정 그래프 호출, 첫 수강용 계정 안내 staging, delete-and-recreate conflict resolution을 소유한다. R2 업로드, AI job dispatch, HTTP 응답 모양은 여전히 view/worker compatibility boundary다. 강의/차시 Excel 수강등록은 이 생성 경계를 호출하지 않고 `lecture_enroll_from_excel_rows()`가 같은 테넌트의 활성 학생만 조회한다. 학생번호가 있으면 같은 tenant의 exact `ps_number`를 최우선 사용하며, 공란일 때만 exact 이름과 숫자로 정규화한 학부모 전화번호 조합을 사용한다. `김지우a/b/1/2` 같은 이름 suffix는 서로 다른 이름이고, 형제·쌍둥이가 학부모 전화번호만 공유하는 것은 중복이 아니다. 학생 전화번호는 이 매칭 조건이 아니다. 후보 행을 잠근 상태에서 정확히 한 명일 때만 등록하며 같은 fallback 식별자의 활성 학생이 복수이면 임의 선택하지 않는다. 학생-only Excel과 강의/차시 Excel 파일은 dispatch 직전에 tenant별 R2 Excel key로 업로드한다. Dispatch가 동기적으로 거절되거나 예외로 끝나면 각 view는 자신이 방금 업로드한 exact key를 즉시 삭제하고, cleanup 자체가 실패해도 원래 dispatch 실패를 성공으로 바꾸지 않는다. DB commit 뒤 SQS publish가 거절되어 최초 HTTP 응답이 이미 수락된 경우에도 gateway가 `source_domain=enrollment`, 일치하는 tenant/bucket, `excel/<tenant>/<32자 hex>.xlsx` 생성 규칙을 모두 만족하는 exact key만 삭제한다. 다른 Excel job이나 임의 key는 이 보상을 적용하지 않는다. 정상 publish가 수락된 파일의 후속 삭제는 worker lifecycle이 계속 소유한다.

Excel 신규 학생 초기 비밀번호 정책 SSOT는 `build_student_import_password_policy()`다. 이 정책은 학생 등록 Excel에만 적용한다. 선택지는 `fixed`(공통 4자 이상 직접 입력)와 `random`(학생별 6자리 임시 비밀번호)뿐이다. 전화번호에서 비밀번호를 파생하는 방식은 API와 worker 모두 거절한다. 강의/차시 Excel 수강등록은 비밀번호 입력을 받거나 job payload에서 복구하지 않는다. 모든 Excel 신규 계정은 첫 로그인에서 비밀번호 변경을 권장하되 원래 화면과 API 사용을 막지 않는다. `fixed` 입력값과 `random` 결과는 서버 비밀키로 암호화해 AI job/result DB에 저장하고, 작업 종료 시 입력값은 제거한다. 평문 legacy job payload는 복구하지 않는다. 랜덤 결과는 스태프 전용 tenant-scoped 상태 조회에서 완료 후 한 시간 동안만 복호화하며 Redis에는 평문을 캐시하지 않는다. 학생 생성과 암호화된 작업 완료 결과는 같은 DB 트랜잭션으로 커밋한다.

Excel 충돌 검토에서 삭제 학생을 복원할 때 Parent/User가 누락됐다면, 같은 확인
요청에 이미 포함된 명시적 초기 비밀번호로만 한 계정을 원자적으로 복구한다. 복원
실패 시 계정 생성도 rollback한다. 새 Parent/User가 생기면 해당 학부모 계정 안내만
같은 작업에서 발송하며, 발송 실패도 복원과 계정 생성을 모두 rollback한다. 일반
일괄 복원은 비밀번호를 추측하거나 계정을 만들지 않는다.
충돌 검토의 `delete` 선택은 기존 학생 영구삭제와 새 계정 생성을 한 트랜잭션으로
묶고, 새 계정은 검토 화면의 명시적 로그인 ID와 초기 비밀번호를 그대로 사용한다.
응답은 생성·복원된 exact 학생 ID와 상태를 `resolved`에 반환해 클라이언트가 다시
읽어 확인할 수 있게 한다.

교사 사진 업무 도우미의 신규 학생 확정도 검토 행마다 직원이 4자 이상 초기
비밀번호를 직접 입력해야 한다. 사진·OCR·학생/학부모 전화번호에서 비밀번호를
만들지 않으며, 비밀번호는 서명된 분석 proposal이나 실행 감사 로그에 넣지 않는다.

Excel 파서의 학생 행 판별은 유효한 학부모/학생 전화번호가 있으면 이름 50자까지 허용한다. 긴 이름을 무조건 비학생 행으로 버리면 실제 외국 이름, 관리 접두어, QA 태그가 있는 정상 행이 `등록할 학생 데이터가 없습니다.`로 실패할 수 있다.

학생-only Excel 등록은 구조 오류(파일 손상, 헤더 없음)는 작업 전체를 실패시키되, 학생 행의 전화번호·학교/학년·맞춤 컬럼·중복 충돌 같은 행 단위 오류는 `failed[]`에 실제 Excel 행 번호와 사유를 기록하고 정상 행을 계속 처리한다. 작업 결과의 `total`은 정상 처리 대상과 행 단위 실패를 모두 포함한다. 강의/차시 Excel 수강등록은 명부에서 찾지 못한 행을 생성·복원하지 않고 건너뛰며 `not_found_students_count`로, 같은 식별자의 활성 학생이 복수인 행은 `ambiguous_students_count`로 반환한다. 매칭된 학생이 하나도 없으면 학생 선등록 또는 중복 명부 정리를 안내하고 작업 전체를 실패시킨다. 일부 행만 제외되면 작업박스가 등록·명부 없음·명부 중복 수를 구분해 경고한다.

Excel 파서는 active sheet에 고정하지 않고 표지/안내 시트를 건너뛰어 학생
헤더와 실제 전화번호 행이 가장 강한 worksheet를 선택한다. 학생/보호자
연락처 의미 표식을 일반 `연락처` 별칭보다 먼저 적용하며, 학생 번호만 있는
열을 학부모 번호로 승격하지 않는다. 한 명짜리 명단도 유효한 010 번호가
하나뿐이고 학생 번호로 표시되지 않았을 때는 학부모 연락처로 처리한다.
복수의 일반 전화 열처럼 규칙만으로 모호한 경우에만 마스킹된 샘플을 AI에
보내고, 신뢰도 0.8 미만 또는 AI 장애 시 추측하지 않고 구조 오류로 실패한다.
동일한 품질의 명단 시트가 여러 개면 활성 시트를 명시적으로 선택한 경우만
그 시트를 사용한다. 표지 시트가 활성 상태라 어느 명단이 대상인지 알 수 없으면
시트 이름을 포함한 구조 오류로 실패해 일부 명단을 조용히 누락하지 않는다.

테넌트 맞춤 학생 컬럼은 `StudentCustomFieldDefinition`의 안정적인 `key`와
`Student.custom_fields` JSON 값으로 저장한다. Excel 파서는 기존 핵심 헤더를
먼저 매핑하고, 나머지 헤더/값을 `_extra_columns`로 import 서비스까지
전달한다. import 서비스만 현재 테넌트의 활성 컬럼 라벨/별칭을 해석한다.
정의 이름을 바꾸면 이전 이름은 별칭으로 남으며, 컬럼 숨김은 기존 값을
삭제하지 않는다. 맞춤 컬럼이 없는 테넌트의 기존 Excel/JSON 계약은
변경되지 않는다.

계정 안내 암호문은 `Student.pending_account_notice_*`에만 보관한다. 신규 학생의 첫 ACTIVE 수강이 커밋된 뒤 `apps/domains/students/services/account_notice.py`가 계정 안내 outbox를 만들고, 학생·학부모 등 모든 유효 수신자 outbox가 확보된 경우에만 암호문과 대기 시각을 제거한다.

## 2. 현재 진입점

| 진입점 | 위치 | 생성 그래프 처리 |
|--------|------|----------------|
| 단건 생성 | `StudentViewSet.create` | `create_student_account(password=...)` |
| JSON 일괄 생성 | `StudentViewSet.bulk_create` -> `import_students_from_rows` | 학생 도메인 import row SSOT로 중복/복원/생성 판단 |
| 충돌 delete-and-recreate | `StudentViewSet.bulk_resolve_conflicts` -> `resolve_student_import_conflicts` | 영구삭제+재생성 conflict resolution을 학생 도메인 import row SSOT로 처리 |
| 가입 신청 승인 | `approve_registration_request` + view facade | `pending -> approved`와 `create_student_account(password_hash=reg.initial_password)`를 atomic 처리 |
| 강의/차시 Excel 기존 학생 매칭 | `lecture_enroll_from_excel_rows` -> `active_student_by_import_identity` | 동일 테넌트의 활성 학생을 학생번호 exact 우선, 없으면 exact 이름+정규화 학부모 전화번호로 조회하고 수강 등록; 생성·복원·계정 변경 없음 |
| 학생 Excel worker | `ExcelParsingService` -> `import_students_from_rows` | 학생 도메인 import row SSOT로 생성, 첫 수강 전 계정 안내값만 staging |

## 3. 불변 조건

- `tenant`는 반드시 caller가 resolve해서 전달한다. tenant fallback은 만들지 않는다.
- `student_data.ps_number`는 caller 또는 serializer가 확정한다.
- `password`와 `password_hash` 중 정확히 하나만 전달한다.
- 학생 전화번호가 비어 있어도 학생 `User`와 `TenantMembership(student)`는 생성된다. 학부모 계정과 공유 계정이 되는 것이 아니다.
- raw 초기 비밀번호를 받는 직접/Excel 생성에서 학부모가 새로 생성되면 학생과 같은 초기 비밀번호를 설정·안내한다.
- 가입 신청처럼 password hash가 전달되는 생성은 검증된 같은 hash를 새 학생·새 학부모 계정에 적용한다.
- 기존 학부모 계정이면 안내 문구는 `변경되지 않음`이다.
- 학생 마스터 생성, 가입 승인, 학생-only Excel/JSON 등록은 알림톡을 발송하지 않는다.
- 신규 학생 생성 시 학생 초기 비밀번호 안내값과 `parent_password_for_notice`를 서로 다른 암호문으로 staging한다. 평문 비밀번호는 DB에 저장하지 않는다.
- 첫 ACTIVE 수강 확정 후 계정 안내는 SYSTEM_AUTO다. 생성 API와 작업 payload에는 발송 여부 옵션이 없으며 발송 시점을 호출자가 바꿀 수 없다.
- 첫 수강 outbox가 완전하지 않으면 암호문을 보존해 동일 수강 요청에서 재시도한다. outbox가 모두 확보되면 암호문을 즉시 지우므로 같은 수강 재시도와 이후 추가 수강은 조용하다.
- 마이그레이션 기본값은 빈 값이므로 배포 전부터 있던 학생은 새 수강을 추가해도 과거 가입 안내를 받지 않는다.
- 학생 전화번호를 나중에 최초 등록하면 기존 학생 계정의 아이디 안내를 새 학생 번호로 발송한다. 비밀번호 변수는 `변경되지 않음`이다.
- 정상 학부모 계정의 복원은 비밀번호를 재발급하지 않고 계정 안내도 다시 보내지 않는다.
- 공개 계정복구는 누락 학부모 계정을 생성하지 않고 generic 응답으로 닫는다. 교직원 삭제
  학생 복원은 누락·사용불가 학부모 계정에만 명시 입력한 초기 비밀번호를 적용하고
  알림톡을 예약한다. 비밀번호 미입력이나 알림톡 예약 실패는 해당 복원 전체를 rollback한다.
- Excel 비밀번호 방식이 학생별로 달라지는 경우 학생별 암호문에 해당 값을 staging한다.
- 강의/차시 Excel 수강등록은 기존 학생의 `User.username`, 비밀번호 hash, `token_version`, 학생번호와 pending credential 암호문을 변경하지 않는다. 다른 테넌트·삭제 학생·미등록 학생도 생성하거나 복원하지 않는다.
- 첫 수강 계정 안내 실패는 이미 커밋된 학생/수강 생성을 API 실패로 되돌리지 않는다. 암호문을 보존하고 재시도한다.

## 4. Frontend 계약

- 학생 생성 API 호출은 `src/shared/api/contracts/students.ts`의 `createStudent()`가 canonical mapper다.
- teacher 모바일 생성 시트는 role-local raw `/students/` POST를 쓰지 않고 shared contract를 호출한다.
- admin/teacher Excel 업로드에는 계정 안내 on/off 옵션이 없다. 학생-only 등록에서는 발송하지 않고 첫 수강 확정 시 SYSTEM_AUTO 계정 안내가 발송된다.
- admin/teacher Excel 업로드는 빈 `fixed` 직접 입력을 기본으로 표시하고, 사용자가 `fixed` 또는 `random`을 명시 선택해야 등록할 수 있다.
- teacher 모바일 Excel 업로드도 파일 선택 직후 즉시 업로드하지 않는다. `StudentListPage`의 Excel import bottom sheet에서 초기 비밀번호 방식을 명시 확정한 뒤 shared upload contract를 호출한다.
- 학생-only Excel 업로드는 일부 행에 오류가 있어도 등록 버튼을 허용한다. 완료 작업에는 신규/복원/중복/실패 수와 실패한 실제 Excel 행 번호·이름·사유를 표시한다.
- `random` 작업 완료 시 작업박스에서 비밀번호 목록을 자동 다운로드하며, 완료 항목의 `비밀번호 목록` 버튼으로 다시 받을 수 있다.
- 강의/차시 Excel 수강등록 화면은 초기 비밀번호 방식을 묻지 않으며 `password_mode`·`initial_password`를 전송하지 않는다. 미등록 학생은 학생 등록 화면에서 먼저 등록해야 한다.
- admin의 맞춤 컬럼 관리에서 만든 활성 컬럼은 단건 등록/수정, 목록 컬럼
  선택, 상세, teacher 모바일, Excel 양식/내보내기에 동일한 안정 키로
  투영한다.

## 5. 검증 기준

학생 생성 경로 변경 시 최소 검증:

- `python -m pytest apps\domains\students\tests -q`
- `python -m pytest apps\domains\messaging\tests\test_messaging_service.py -q`
- `python manage.py check --settings apps.api.config.settings.test`
- `python manage.py makemigrations --check --dry-run --settings apps.api.config.settings.test`
- frontend focused ESLint for touched student files
- `pnpm typecheck`
- `pnpm build`
- `pnpm guard:legacy-api`

Excel 파서 변경은 표지+명단 다중 시트, 영문/한글 헤더, 한 명짜리 명단,
학생/보호자 전화 분리, 학생 전화만 있는 파일과 복수 명단 ambiguity의
fail-closed 회귀를 포함한다.

운영 QA는 최소 하나의 disposable 학생을 명부에 생성해 알림톡이 발송되지 않음을 확인하고, 첫 ACTIVE 수강 확정 후 계정 안내 알림톡 발송과 로그인을 확인해야 한다. 이어서 수강·강의와 학생을 cleanup(soft delete + permanent delete)하고 잔여 데이터가 없는지 확인한다.
