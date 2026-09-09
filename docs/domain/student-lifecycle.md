# 학생 생명주기 SSOT

**상태:** Active
**최종 점검:** 2026-09-10
**코드 기준:** `apps/domains/students/services/lifecycle.py`, `apps/domains/enrollment/services/lifecycle.py`, `apps/domains/students/views/student_views.py`

## 1. 상태

| 상태 | 판정 | 진입 |
|------|------|------|
| Active | `Student.deleted_at IS NULL` | 학생 생성 또는 복원 |
| Soft-deleted | `Student.deleted_at IS NOT NULL` | `soft_delete_student()` |
| Restored | 다시 Active | `restore_student()` |
| Permanently deleted | DB row 제거 | `permanently_delete_students()` |

학생 삭제/복원/영구삭제 신규 코드는 view나 management command에 직접 구현하지 않는다.
HTTP와 운영 명령은 생명주기 서비스를 호출하는 compatibility facade다.

서로 혼동하면 안 되는 상태 축은 다음과 같다.

| 축 | 저장값 | 의미 |
|---|---|---|
| 학생 삭제 | `Student.deleted_at` | 30일 복구 가능한 학생 계정·업무 접근 정지 |
| 계정 접근 | `User.is_active` + 현재 tenant의 `TenantMembership.is_active` | 현재 tenant 로그인 가능 여부 |
| 관리 대상 | `Student.is_managed` | 교직원 목록/업무 분류이며 로그인·수강 권한을 바꾸지 않음 |
| 강의 수강 | `Enrollment.status` | 강의별 `ACTIVE`/`PENDING`/`INACTIVE` 권한 |

## 2. Soft Delete

SSOT: `soft_delete_student(student, tenant=...)`

- `deleted_at`을 기록하고 `ps_number`를 `_del_{student.id}_{old}`로 보존한다.
- 같은 transaction과 student-PS namespace lock에서 학생 scope
  `InventoryFolder`/`InventoryFile.student_ps`도 같은 tombstone 번호로 옮긴다. R2 key의
  historical PS segment는 locator이므로 이때 object copy/rename은 하지 않는다. 새 학생이
  원래 번호를 재사용해도 이전 학생·학부모 metadata를 조회할 수 없다.
- tombstone namespace가 이미 모호하게 점유되어 안전하게 이동할 수 없으면 raw 예외 대신
  `student_storage_namespace_conflict`로 전체 soft delete transaction을 되돌린다.
- `Parent` 직접 연결을 끊는다.
- 순수 학생 계정이면 해당 테넌트의 `student` 멤버십을 비활성화하고, 남은 활성 멤버십이 없을 때만 `User.is_active=False`로 둔다.
- 같은 사용자에게 다른 테넌트 멤버십이나 같은 테넌트의 staff/teacher/admin/owner/parent 역할이 남아 있으면 전역 계정을 잠그지 않는다.
- 각 enrollment의 현재 상태를 `status_before_student_deletion`에 먼저 기록한 뒤
  `INACTIVE`로 일시 정지한다. enrollment, 차시 명단, 출결, 성적, 과제, 영상 진도
  행을 이동·복제·삭제하지 않는다.
- `status_before_student_deletion`은 lifecycle 내부의 일회성 DB marker이며 학생·수강
  API 응답이나 OpenAPI 계약에 노출하지 않는다.
- 자동 배정 수강료는 enrollment 비활성화와 같은 트랜잭션에서 비활성화한다.
- clinic 예약 취소는 clinic lifecycle hook을 통해 수행한다.
- 삭제된 학생은 수강 bulk 등록, enrollment 활성/대기 전환, 차시 명단 추가 경로에서
  fail-closed로 거절한다.

## 3. Restore

SSOT: `restore_student(student, tenant=..., profile_data=None)`

- `_del_` 접두사에서 원래 `ps_number`를 복원한다.
- 같은 테넌트 활성 학생과 아이디 충돌이 있으면 실패한다.
- 복원하는 학생의 tombstone Inventory metadata도 원래 번호로 함께 되돌린다. 원래 번호에
  다른 owner 또는 모호한 legacy metadata가 있으면 합치거나 추측하지 않고 복원을 실패
  폐쇄하며 `student_storage_namespace_conflict`를 반환한다.
- `User.is_active`, 학생 전화번호, 테넌트 멤버십, Parent 연결을 복원한다.
- `status_before_student_deletion`이 있는 enrollment만 삭제 전 상태로 복원하고 marker를
  비운다. 원래 `INACTIVE`였던 수강은 계속 `INACTIVE`, 원래 `PENDING`은 계속
  `PENDING`이다.
- 복원 시점에 `Lecture.is_active=False`이거나 `end_date`가 지난 강의는 삭제 전 상태가
  `ACTIVE`/`PENDING`이어도 `INACTIVE`로 유지한다.
- 활성으로 돌아온 enrollment의 수강료 연결은 다시 계산하지만, 기존 enrollment를
  복원하는 동작만으로 첫 계정 안내를 재발송하지 않는다.
- 복원은 비밀번호를 재발급하지 않는다. 가입 안내 알림톡도 새 비밀번호처럼 보내지 않는다.

`enrollment.0002_student_deletion_status_snapshot` 적용 전에 이미 삭제되어 있던 학생의
원래 수강 상태는 과거 `INACTIVE` 덮어쓰기로 복원할 수 없다. 마이그레이션은 이를
추측하지 않고 현재 수강을 `INACTIVE`, 삭제 전 상태 snapshot도 `INACTIVE`로 기록한다.
따라서 legacy 삭제 학생 복원은 계정과 데이터 연결을 복구하되 수강을 임의로 열지
않으며, 필요한 강의만 교직원이 명시적으로 재등록한다.

무중단 롤링 교체 중에는 마이그레이션이 설치한 PostgreSQL trigger가 구 런타임의
`QuerySet.update(status="INACTIVE")`도 보완한다. 학생이 이미 soft-deleted이고 기존
수강 상태가 `ACTIVE`/`PENDING`이며 marker가 비어 있을 때만 `OLD.status`를 marker에
원자 기록한다. 신 런타임의 명시적 dual-write가 있으면 trigger는 개입하지 않는다.
reverse migration은 같은 이름의 trigger와 function만 정확히 제거한다.

수강·차시 명단 batch write는 요청 순서와 무관하게 중복 제거한 Student ID 오름차순으로
먼저 잠근다. 차시 명단은 Student 행을 선점한 뒤 Enrollment 행도 오름차순으로 잠그고,
DB에서 다시 읽은 tenant/lecture/status만으로 등록 여부를 결정한다. API가 전달한 오래된
Enrollment 인스턴스의 상태는 권한 판정에 사용하지 않는다.

차시 명단 조회는 과거 명단 보존을 위해 비활성 수강 행도 반환할 수 있으며, 각 행의
현재 수강 상태를 `enrollment_status`로 함께 제공한다. 직전 차시 복사와 시험·과제
자동 배정처럼 현재 쓰기 대상을 만드는 소비자는 `ACTIVE` 행만 사용한다. 누락되거나
알 수 없는 상태는 활성으로 추측하지 않고 제외하며, 최종 write는 위 잠금 가드에서
다시 검증한다.

## 4. Permanent Delete

SSOT: `permanently_delete_students(tenant=..., student_ids=[...])`

현재 facade:

- `StudentViewSet.bulk_permanent_delete`
- `StudentViewSet.bulk_resolve_conflicts`의 delete 후 재등록 경로
- `StudentViewSet.deleted_duplicates_fix`
- `check_deleted_student_duplicates --fix`
- `purge_deleted_students`

정리 범위:

- enrollment 및 enrollment child
- lecture section assignment
- fees: `StudentFee`, `StudentInvoice`, `InvoiceItem`, `FeePayment`
- submissions/results/homework/progress/video/clinic/community의 학생 참조
  - `StudentReportedScore`, 학생 지원 세션, 비활성 수강·직접 영상 권한을
    학생 행 삭제 전에 같은 tenant 범위로 정리한다.
  - 수강이 먼저 삭제되어 `Submission.enrollment=NULL`인 제출도 삭제 대상
    학생 전용 계정의 tenant-scoped submission이면 포함한다. 같은 tenant의
    Parent/Staff/비학생 역할도 가진 계정은 `Submission.user`만으로 학생 owner를
    추측하지 않고 삭제 대상 enrollment로 직접 귀속된 submission만 포함한다.
    따라서 해당 계정이 다른 학생을 위해 만든 제출과 media는 보존한다.
    제출 도메인이 `SubmissionMedia`/legacy `file_key` cleanup intent와 child 행을
    먼저 기록한 뒤 부모 `Submission`을 삭제한다. OMR batch 이력은 보존하고 해당
    submission 참조만 `NULL`로 바꾼다.
  - `WrongNotePDF.file_path`도 같은 durable intent에 Storage 버킷 대상으로 기록한다.
    제출 object는 AI 버킷 대상이므로 같은 문자열 key여도 버킷을 혼동하지 않는다.
  - 학생 scope `InventoryFile`은 삭제 대상 학생의 현재 `ps_number`와 `_del_`에서
    복구한 원래 `ps_number` 중 다른 학생이 재사용하지 않은 값으로만 exact ID를
    조회한다. 잠긴 exact `InventoryFile` 행이 canonical owner이고 R2 key는 locator다.
    따라서 학생번호 변경 뒤 metadata의 `student_ps`와 key의 과거 PS segment가 달라도
    key가 `tenants/{tenant_id}/students/...`에 속하면 정리할 수 있다. 다른 tenant
    prefix는 `409`로 중단하고, 같은 key를 다른 canonical owner가 참조하면 보존한다.
    Storage cleanup intent를 먼저 기록하고, `StudentReportedScore`를 지운
    다음 intent가 생성된 InventoryFile metadata만 같은 transaction에서 삭제한다.
    commit 뒤 R2 삭제가 성공하면 intent는 `cleaned`, provider 실패면 재시도 가능한
    `failed`로 남는다. 같은 Storage key를 Matchup 등 다른 canonical owner가 참조하면
    intent와 metadata를 모두 만들거나 지우지 않고 보존한다.
  - 위 exact student scope에서 파일 삭제 뒤 비어 있는 `InventoryFolder`는 leaf부터
    정리한다. 다른 owner 때문에 파일이 보존된 folder tree와 다른 학생이 재사용한
    `ps_number`의 파일·폴더는 삭제하지 않는다.
- 삭제 대상 테넌트의 student 멤버십과 pending password reset
- 다른 활성 멤버십·Parent·Staff·staff-role 멤버십이 없는 orphan `User`

안전 규칙:

- 삭제 대상 학생은 반드시 같은 tenant의 soft-deleted 학생이어야 한다.
- PostgreSQL에서는 `academy:student-ps-namespace:v1:{tenant_id}:{ps_number}` transaction
  advisory lock이 학생번호 namespace의 생성·변경·Inventory attach·영구삭제 ownership
  판정을 직렬화한다. 기존 학생 identity 경로는 교착을 피하려고 항상
  `User row -> Student row -> sorted old/new namespace` 순서이고, 영구삭제도
  `Tenant -> User -> Student -> sorted current/original namespace` 순서다. 신규 Student
  insert는 target과 발견된 tombstone predecessor namespace를 정렬해 먼저 잠근다. 기존
  rename도 target claim이므로 같은 snapshot/re-read를 사용한다. exact predecessor가
  하나이면 legacy Inventory metadata를 predecessor tombstone으로 격리하고, 복수/모호한
  attribution이면 그 identity mutation만 실패한다. upload attach와 student-scope
  folder/file create·move·수정·삭제는 Student row를 잠그지 않고 namespace 뒤 활성 owner와
  최신 metadata scope를 다시 읽는다.
- 같은 사용자가 다른 테넌트나 같은 테넌트의 비학생 역할로 남아 있으면 User와 해당 멤버십을 보존한다.
- 보존되는 사용자가 과거 soft delete 때문에 비활성화되어 있고 활성 멤버십이 남아 있으면 재활성화한다.
- Student, Enrollment, Submission reverse graph의 tenant-bearing 직접 FK와 소유
  exam/session/lecture/video/batch tenant가 다르면 어떤 DB/R2 변경보다 먼저
  `409 cross_tenant_reference`로 중단한다. legacy OMR batch item의 nullable
  `tenant_id`는 parent batch가 exact tenant일 때만 허용하고 submission 참조만 비운다.
- 삭제 대상 object key를 같은 owning bucket의 다른 submission/media 또는 등록된
  inventory/exam/community/student/matchup/tools/public/video owner가 참조하면 R2 object를
  보존한다. 대상 tenant·행·버킷을 키 문자열만으로 추측하지 않는다.
- DB transaction은 object를 삭제하지 않는다. `(tenant, bucket, object_key)` cleanup
  intent가 DB 삭제와 함께 commit된 다음 callback이 처리한다. provider 실패는 raw
  예외 대신 `storage_delete_failed`를 남기며 pending/failed intent는 반복 실행해도
  완료 key를 다시 처리하지 않는다. 15분 지난 PROCESSING claim만 token으로 회수한다.
- 영구삭제 API 성공 응답은 `deleted`와
  `storage_cleanup: {pending, failed}`를 반환한다. 저장 namespace 불일치 또는 진행 중
  media upload는 `409 storage_cleanup_scope_mismatch`로 전체 mutation을 중단한다.
- `SubmissionMedia.UPLOADING`은 1시간 upload lease 동안 같은 `409`로 보호한다.
  lease가 지난 row는 중단된 업로드로 회수해 durable AI cleanup intent에 넣고 DB
  삭제를 진행한다. 늦게 끝난 PUT의 finalize는 row와 cleanup intent를 다시 잠가
  확인하며, 소유권을 잃었으면 row를 되살리지 않고 key를 동일 intent로 재정리한다.
- 현재 cross-domain 정리는 guarded raw SQL graph다. 장기 목표는 각 도메인 cleanup hook/event로 분해하는 것이다.

## 5. Retention 운영

- soft-deleted 학생은 30일 보관 후 purge 대상이다.
- 운영 스케줄은 EventBridge `academy-v1-purge-soft-deleted`: 매일 03:15 KST.
- 실행 명령은 API 컨테이너에서 `python manage.py purge_deleted_students`.
- cleanup intent만 재시도할 때는
  `python manage.py process_submission_storage_cleanup --limit 1000`을 사용한다.
  매일 purge 명령도 학생 대상 유무와 무관하게 pending/failed intent를 먼저·후에
  재시도한다.
- 수동 점검:

```powershell
python manage.py check_deleted_student_duplicates --dry-run
python manage.py check_deleted_student_duplicates --fix
python manage.py purge_deleted_students --dry-run
python manage.py purge_deleted_students
```

## 6. 검증 기준

- soft delete, restore, permanent delete는 학생 생명주기 테스트에 포함되어야 한다.
- soft delete/restore 변경 시 `ACTIVE`/`PENDING`/`INACTIVE` 보존, 삭제 중 종료된 강의
  비활성 유지, 계정 안내 미발송, 삭제 학생 수강/차시 등록 차단을 함께 검증한다.
- PostgreSQL에서는 구 런타임 형태의 상태 일괄갱신→신 런타임 복원과, 역순으로 겹치는
  수강/차시 batch write가 교착 없이 끝나는지 함께 검증한다.
- permanent delete 변경 시 최소 검증:
  - tenant isolation
  - cross-tenant User 보존 및 재활성화
  - same-tenant parent/staff/teacher 계정 보존
  - fee/section/video-comment dependency cleanup
  - reported score/support session/video entitlement cleanup
  - reported-score 증빙 InventoryFile의 Storage intent, post-commit R2 삭제, 빈 folder
    zero-readback, shared owner 및 재사용 `ps_number` 보존, PS 변경 전 historical key 정리
  - PostgreSQL create/rename과 permanent delete 경쟁에서 namespace 판정 직렬화 및
    신규 owner Inventory/R2 보존
  - soft delete/restore의 Inventory metadata tombstone 왕복, 재사용 학생과 sibling
    parent/student list·download 격리, 안전한 replacement 신규 파일의 정상 사용
  - 학생 Inventory PUT/attach와 soft/permanent delete 양 순서에서 attach 실패 exact
    보상 또는 committed metadata 기반 cleanup zero-readback
  - soft delete와 stale student-scope file/folder delete 경쟁에서 tombstone metadata/R2 보존
  - detached submission media R2/row cleanup과 공유 object 보존
  - AI/Storage bucket 분리, DB rollback 전 object 미삭제, 부분 실패 재시도
  - 오답노트 PDF의 동일한 post-commit cleanup과 scheduled retry
  - OMR batch submission 참조 nulling
  - `Student`/`Enrollment`/`Submission` reverse FK graph 및 storage owner registry drift
  - corrupt cross-tenant child reference 차단
  - purge/duplicate cleanup command routing
