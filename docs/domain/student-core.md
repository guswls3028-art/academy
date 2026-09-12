# Student Domain Core SSOT

**Status:** Active
**Last checked:** 2026-08-30 KST
**Truth basis:** code inspection of `apps/domains/students/`, `apps/core/views/account_recovery.py`, `apps/core/services/password.py`, `apps/domains/results/services/submission_scope_guard.py`, `apps/domains/results/services/student_result_service.py`, and frontend shared student contracts.

This document is the integration SSOT for the student domain. More specific
documents still own their detailed contracts:

- creation and import: `student-creation.md`
- staff support preview and activity evidence: `student-support-audit.md`
- deletion/restore/permanent delete: `student-lifecycle.md`
- login ID and password recovery: `account-recovery.md`
- parent account graph: `parent-account.md`
- OMR scoring: `omr.md`
- messaging and Alimtalk: `messaging-alimtalk.md`

## 0. Product Rule

Student is the product spine. A feature that displays, grades, notifies,
assigns, books, or reports learning content must treat the student account
graph and tenant-scoped enrollment graph as first-class constraints.

Broad promotion or expansion launch must not proceed on a "screen loads" signal
alone. The release gate must prove the chain from student identity to the
consumer role that sees the final state.

Student state is not one boolean. `deleted_at`, tenant account access,
`is_managed`, and each `Enrollment.status` are independent axes. `is_managed`
only controls staff management classification and must never be described or
implemented as login suspension. Deletion/restore and enrollment state transfer
are owned by `student-lifecycle.md`.

Tenant-scoped student, lecture-enrollment, and session-enrollment lists use
student name ascending with stable ID tie-breakers unless the user explicitly
chooses another supported ordering. Search, deleted-state, lecture, and session
scope are applied before this global ordering and pagination. PostgreSQL uses
the explicit `C` collation for the student-name expression so the production
database locale cannot replace Unicode codepoint 가나다 order; SQLite keeps its
equivalent native binary ordering. The shared list rules are owned by
`data-list-ordering.md`.

## 1. Canonical Student Graph

The durable student graph is:

```text
Tenant
  -> User
  -> Student
  -> TenantMembership(role="student")
  -> Parent link, when parent_phone is present
```

Canonical creator:

- `apps/domains/students/services/creation.py::create_student_account()`

Owned by the creator:

- ensure or link the parent account;
- create the student user;
- create the student row;
- create or reactivate student tenant membership;
- return the parent password phrase for notices.

Not owned by the creator:

- duplicate/deleted-student decisions;
- serializer/API response shape;
- Excel/R2/AI worker dispatch;
- Alimtalk dispatch;
- registration status transition.

## 2. Identity Invariants

Canonical identity helper:

- `apps/domains/students/services/identity.py`

Required invariants:

- `tenant` must be resolved by the caller. No tenant fallback.
- active student means `Student.deleted_at IS NULL`.
- `Student.user` is required.
- `Student.ps_number` is tenant-unique and is the student login display ID.
- When a student phone is supplied, it is the requested login ID and any collision
  fails explicitly. A generated ID is used only when both the explicit ID and
  student phone are absent; a collision never silently changes the saved ID.
- A student login display ID must also be unique across every active login
  identity in the tenant, including parent accounts. If a no-phone student's
  requested ID equals the parent phone, creation and profile changes reject the
  collision instead of silently assigning another ID. A generated student ID is
  used only when the caller leaves the ID empty. This prevents a shared
  initial password from matching both the student and parent account and
  making login ambiguous.
- `Student.ps_number` and the inventory copies that scope student files use the
  same 50-character storage boundary; a valid student identity must not fail
  when it is mirrored into inventory metadata.
- internal username mirrors `ps_number` through `user_internal_username(tenant, ps_number)`.
- Persisting a `ps_number` change locks the account and student rows and updates
  the internal username, inventory copies, and student row in one transaction.
  A save whose `update_fields` excludes `ps_number` must not mutate either
  identity mirror. The persisted `Student.user_id` selects the account lock
  before the student row lock; an in-memory attempt to relink the account or
  tenant fails closed. Username collisions roll back every identity copy.
- student phone is optional; parent phone is required on creation/import/signup.
- phone fields are normalized to numeric `010XXXXXXXX` 11-digit strings.
- Public JWT login NFKC-normalizes and trims the submitted identifier. It removes
  spaces, hyphens, dots, and parentheses only when the compact value is exactly an
  `010` 11-digit mobile identifier; punctuation in custom IDs remains significant.
- an exact student-phone/parent-phone match means the student has no phone. The
  Excel parser, JSON import, signup approval, single create, and profile write
  store that value only as `parent_phone`; `Student.phone` and `User.phone` are
  empty and `uses_identifier=True`. The parent account remains the sole owner
  of the shared recipient number.
- `User.phone` mirrors `Student.phone`; profile changes update or clear both so
  account and notification paths cannot retain a stale student recipient.
- malformed student phone is rejected. Do not silently convert it to identifier mode.
- if student phone exists, `omr_code` is the last 8 digits of student phone.
- if student phone is absent, `omr_code` is the last 8 digits of parent phone.
- no fake student phone is created only to satisfy downstream code.
- `uses_identifier=True` means the student has no student phone and is identified by the account/OMR identifier flow.
- when staff first records a distinct real student phone for an identifier-mode
  account, the same transaction updates `Student.phone`, `User.phone`,
  `omr_code`, `uses_identifier`, `Student.ps_number`, and the internal username.
  A phone-owned login ID also follows a later phone change; an explicitly
  custom login ID remains unchanged. Passwords are never reset by this update,
  and the existing SYSTEM_AUTO account notice reports `변경되지 않음`.

Current canonical entry points:

| Flow | Canonical path |
|---|---|
| admin single create | `StudentCreateSerializer` -> `create_student_account()` |
| JSON bulk create | `import_students_from_rows()` |
| Excel/worker import | `ExcelParsingService` -> `import_students_from_rows()` |
| lecture/enrollment Excel import | `resolve_student_import_row()` |
| signup approval | `approve_registration_request()` -> reuse exact active identity or `create_student_account(password_hash=...)` |
| admin/student profile write | `update_student_profile()`; student self-service cannot change `parent_phone`/Parent linkage |
| deleted conflict restore/delete | `restore_student()` / `permanently_delete_students()` through import conflict resolver |

New-student JSON/Excel import resolves each row inside the requested tenant in
this order:

1. A non-empty normalized student phone that matches exactly one active
   `Student` resolves that existing student.
2. Otherwise, an exact name plus normalized parent phone that matches exactly
   one active `Student` resolves that existing student. The name is opaque:
   suffixes and markers such as `김지우a`, `김지우b`, `김지우1`, `김지우2`,
   or parentheses are ordinary name text and are neither stripped nor folded.
3. The existing deleted-student name-plus-parent restore policy applies only
   to a unique deleted candidate. A deleted phone collision that is not that
   restore candidate remains an explicit conflict.
4. Only when those same-tenant candidates are absent is a new account graph
   created.

Multiple candidates at any lookup boundary fail the row closed instead of
choosing the first record. No cross-tenant fallback exists. Resolving an active
duplicate never rewrites the existing student's name, phone, parent phone,
login ID, password, token version, or pending account notice. A parent phone is
required but may be shared by siblings or twins; the student phone is optional.
The worker result must expose every source row as created, duplicate, restored,
or failed so a partial result cannot look like silent omission.
`created` remains the backward-compatible count, while `created_rows`,
`duplicates`, `restored`, and `failed` carry the Excel row and opaque student
name for the staff result dialog. Created/duplicate/restored rows may also carry
the same-tenant student ID for internal navigation, but the browser's persisted
result projection omits IDs, phones, and credentials. Known validation failures
include an allowlisted `reason_code` and user-safe message. Unexpected exceptions
are logged with their internal detail but return only the generic
`processing_error` reason; raw exception text must never enter the job result.

Student list search keeps its queryset tenant-scoped before applying filters.
A complete `010` mobile number in the general search or explicit student/parent
phone filter is compared exactly after removing presentation separators from
both the query and stored fields. Thus hyphenated and digit-only forms are
equivalent without introducing a normalized substring or cross-tenant fallback.
Non-phone general search continues to use the existing name, PS/OMR, school,
and major text fields.

Legacy active rows whose student phone exactly equals the parent phone are
audited and repaired with the dry-run-first command below. Execution requires
the complete current candidate ID set and a tenant/count confirmation, fails
closed for pending account notices or active account outboxes, and changes no
login ID, password, parent account, enrollment, or notification:

```powershell
python manage.py repair_shared_student_parent_phones --tenant <tenant-code>
python manage.py repair_shared_student_parent_phones --tenant <tenant-code> --student-ids <id,id> --execute --confirm <tenant-code>:<count>
```

Student and enrollment Excel uploads accept only the `.xlsx` extension. The
API validates a non-empty bounded upload, a supported browser MIME (including
Windows Hancom HCell's `application/haansoftxlsx`, empty MIME, and generic
`application/octet-stream`), the ZIP signature, `[Content_Types].xml`, and
`xl/workbook.xml` before R2 upload. MIME is client metadata and never replaces
the file-content checks. A renamed file, unrelated MIME, disguised ZIP, or
corrupt workbook fails closed, and the worker parser remains the final workbook
and row-validation boundary.

Student app profile photos use only the tenant-scoped R2 key returned by
`profile_photo_key(tenant_id, student_id, unique_id, ext)` as the readable
production state. An R2 upload or DB key-save failure returns retryable 503 and
must not fall back to a local `ImageField` that production cannot serve. After
the new key is saved, the exact replaced R2 object is deleted best-effort; a DB
save failure cleans the newly uploaded key best-effort and preserves the old DB
key. Invalid content type, image magic bytes, or files larger than 10 MiB are
rejected before upload.

## 2.1 Tenant Custom Student Fields

Teacher-specific profile columns are a tenant-scoped extension of the canonical
`Student` row, not a second student identity or profile table.

- `StudentCustomFieldDefinition` owns each tenant's label, stable generated key,
  value type, Excel aliases/options, order, and active state.
- `Student.custom_fields` stores values by the stable definition key. Display
  label changes therefore do not migrate or orphan student values.
- definitions and values are always resolved through `request.tenant`; a key
  from another tenant or an unknown/inactive key is rejected on write.
- supported value types are `text`, `number`, `date`, and `select`.
- deactivation is non-destructive. It hides the field from active forms and
  tables but never removes definitions or existing student values. Reactivation
  exposes the preserved values again.
- profile updates merge submitted active custom values into the existing JSON
  object. Core fields and inactive custom values are preserved.
- canonical core Excel headers keep precedence. Unknown Excel headers are
  preserved by `ExcelParsingService` and the student import service maps them
  only when they match an active definition label or alias.
- renaming a definition retains its previous label as an Excel alias.
- frontend table visibility remains a per-browser teacher preference; it does
  not change tenant data or the shared definition.

Changing this boundary requires tenant-isolation, rename/alias, deactivate/
reactivate, single-create/update, and Excel round-trip tests. Do not introduce
per-teacher physical database columns or repurpose core student columns.

## 3. Signup, ID Recovery, Password Recovery

Detailed SSOT: `account-recovery.md` and `student-creation.md`.

Current rules:

- signup create requires write-only `initial_password` and
  `password_confirmation`. The serializer compares the exact untrimmed values
  before any request, Student, User, or membership write. A mismatch is a
  field-keyed 400 and creates nothing. Confirmation is not a model field and
  neither its plaintext nor a second hash is persisted or logged.
- a matching signup request stores one Django password hash only;
  `initial_password_plain` must remain empty.
- signup approval uses the original password hash and tells the student
  "가입 신청 시 입력한 비밀번호" instead of exposing plaintext.
- signup approval status transition and student creation are atomic in
  `approve_registration_request()`.
- PostgreSQL approval acquires deterministic transaction locks for the
  same-tenant student phone, exact name+parent phone, and requested login ID
  before candidate lookup or account creation. Distinct pending request rows
  for one identity therefore cannot create parallel Student/Parent/User graphs.
- signup approval re-resolves the request against the current tenant before
  creating anything. One exact active student graph is reused in place: the
  request is linked and approved without changing the existing Student,
  Parent, User, membership, `ps_number`, password, `token_version`, or pending
  account-notice state. The approval result reports both passwords as
  `변경되지 않음`.
- multiple active matches or a mismatched tenant/phone/account graph fails
  closed with 409. A deleted match is never restored automatically: the staff
  approval response returns only same-tenant candidates whose exact recovery
  identity matches, and staff must select one candidate through
  `POST /students/registration_requests/{id}/resolve_deleted/`.
- explicit deleted recovery locks Parent, related Users in ID order, Student,
  then memberships, rechecks the selected same-tenant deleted graph, and
  restores that exact Student/User. It applies the confirmed signup login ID
  and the already stored signup password hash, invalidates old tokens, and
  reactivates the existing student membership. An active collision, foreign
  tenant or membership, changed candidate, missing graph edge, or processed
  request returns 409 with no recovery write. Repeating a successful request
  also returns 409 and does not reapply password, token, profile, or history
  changes.
- recovery never clears or repoints an earlier registration audit link.
  `StudentRegistrationRequest.student` is a nullable non-unique ForeignKey with
  plural reverse relation `Student.registration_requests`; the expand-only
  migration preserves every existing link and permits the new recovery request
  to reference the same Student. Enrollment rows and links remain unchanged;
  their statuses are restored only by the canonical student lifecycle from the
  deletion snapshot. Original `ACTIVE`/`PENDING`/`INACTIVE` states return when
  the lecture still accepts restoration, ended or inactive lectures stay
  `INACTIVE`, and the internal marker is consumed and cleared. This recovery
  endpoint does not enqueue an account notice or message.
- active reuse also requires active Student and Parent
  login users with canonical internal usernames and phone mirrors plus active
  same-tenant `student` and `parent` memberships. A reused Student must already
  have a linked Parent User; only the new-student path may let the existing
  parent ensure contract repair a Parent whose `user_id` is empty. A phone match
  in another tenant is never reused or treated as a same-tenant identity.
- `godmin` and `tchul` do not use student self-registration. Public create and
  duplicate-check APIs and direct approval all fail closed with 403; existing
  login and account-recovery flows remain available.
- approval Alimtalk failure does not hide an already committed approval.
- public ID/password recovery uses `/api/v1/auth/account-recovery/dispatch/`.
- legacy public OTP password-find endpoints are sealed with 410 Gone.
- public password recovery creates `PendingPasswordReset` and changes the real
  password only when the temporary password is used to log in.
- unknown, ambiguous, and successful public recovery responses must be generic.
- public recovery sends only to the verified phone supplied by the user.
- staff/teacher password reset through `/students/password_reset_send/` is a
  privileged path:
  - authenticated active owner/admin/teacher/staff membership is required for
    `temp_password`; account-notice opt-out parameters are not part of the
    contract and cannot suppress SYSTEM_AUTO account notices;
  - student target may resolve by `student_ps_number` or verified student phone;
  - parent target resolves by student name + parent phone;
  - password changes immediately;
  - pending reset is cleared;
  - Alimtalk delivery failure rolls back the password and pending-reset state.
- password minimum length remains 4. Do not raise it.

Focused verification:

```powershell
python -m pytest apps/domains/students/tests/test_registration_approval_identity.py -v
python -m pytest apps/domains/students/tests/test_registration_request_history_migration.py -v
$env:DJANGO_SETTINGS_MODULE='apps.api.config.settings.test_pg'
python -m pytest apps/domains/students/tests/test_registration_approval_concurrency_pg.py -v
```
- automatic temporary password generation is 6 numeric digits for user handling,
  not a minimum-length policy change.

## 4. Alimtalk Boundaries

Detailed SSOT: `messaging-alimtalk.md`.

Student account Alimtalk is system-critical but still fail-closed:

- `send_alimtalk_via_owner()` is the canonical account-notification dispatcher.
- account triggers use exact approved owner templates.
- no SMS fallback.
- `password_reset_*` must not fall back to `registration_approved_*`.
- any student/parent account ID or password change sends a SYSTEM_AUTO account
  notice. ID-only changes, parent phone relinks, and first-time student phone
  registration use `registration_approved_*` with password phrase
  `변경되지 않음`.
- student creation and registration approval stage, but do not send, the initial
  account notice. The first confirmed active enrollment dispatches it once.
- staged student and parent password notice values are encrypted separately and
  removed only after all expected durable outbox rows exist.
- first-enrollment notices use service-returned parent password phrases:
  - new parent account: parent initial password phrase;
  - existing parent account: `변경되지 않음`.
- account notification logs are linked back through `source_tenant_id`,
  `target_type="account"`, and stable target IDs.
- student detail UI may show account-notification status metadata, never the
  message body or temporary password.
- preexisting students have no pending notice marker, so adding another lecture
  after this behavior ships does not back-send a historical welcome message.

## 5. Student-Linked Content Rule

Any feature connected to learning content must resolve student scope through the
tenant and the appropriate roster object. Direct student lookup is not enough
when the feature is tied to a class/session/exam/homework/clinic context.

Required chain:

```text
Tenant
  -> active Student
  -> active Enrollment
  -> SessionEnrollment / ExamEnrollment / HomeworkEnrollment / Clinic participant
  -> Submission / Result / Achievement / Notification / Student app projection
```

Canonical active-enrollment selector for student-facing projections:

- `apps/domains/enrollment/selectors.py::active_enrollments_for_student()`
- `apps/domains/enrollment/selectors.py::active_enrollments_for_students()`
- `apps/domains/enrollment/selectors.py::active_enrollment_ids_for_student()`

Student-facing exam lists/detail/submission, exam result detail, grades summary,
homework summary, video visibility/progress, dashboard scope, clinic remediation,
wrong-note/PDF, exam-attempt history, attendance summaries, schedule mutations,
and future linked-content reads must use this selector or a narrower selector
with the same tenant + active student + active enrollment constraints.
Public video's synthetic system lecture is the only intentional exception; it
must call the same selector with `include_system=True` and keep the exception
local to public-video accounting.

Rules:

- no cross-tenant fallback;
- no deleted-student fallback;
- no inactive enrollment fallback for scored/submitted content;
- when `student_id` and `enrollment_id` are both present, cross-check both;
- when only `student_id` is present and a roster context exists, resolve the
  active enrollment with explicit ordering or fail with a user-visible error;
- OMR candidate matching is same-tenant, active roster only;
- ambiguous OMR/phone/name matches must go to manual review, not silent choice;
- student-facing projections must be verified from the student role after admin
  or teacher writes the state.

## 6. Connected Domains

| Domain | Student contract |
|---|---|
| OMR automatic grading | candidate set is same-tenant active roster; identifier is phone/parent-phone last 8 digits; unmatched/ambiguous scans remain reviewable facts |
| Results/exam scores | submission, exam, enrollment, and tenant must match before score/result writes |
| Clinic | clinic target and remediation state must resolve through enrollment/session context; `/clinic/idcard/` returns every unresolved target across active enrollments in newest-first order, with the actual source title, nullable dedicated scope, session label, and a separate tenant-local reservation/passcard projection |
| Homework | assignment/submission rows must carry tenant-scoped enrollment identity |
| Attendance | attendance status that affects secession/enrollment must call the lifecycle path, not mutate student rows directly |
| Video/progress | student visibility and progress must use tenant-scoped enrollment/session access |
| QnA/community/counseling | student author/target must be tenant-scoped and not inferred from display name |
| Messaging | recipients come from the verified student/parent phone in the resolved student graph |

Clinic source projection never guesses from an uploaded filename or copies a free-form
description into scope. `source_title` is the tenant-scoped Exam/Homework title, and
`source_scope` stays `null` until that source domain owns a dedicated unit/range value;
clients render the missing scope explicitly instead of hiding the rest of the target.
The passcard keeps the unresolved `ClinicLink` verdict in `current_result` and returns
the separate `passcard_state` (`PASSED`, `CLINIC_REQUIRED`, or
`BOOKING_CONFIRMED`). A future-or-today `booked` reservation yields
`BOOKING_CONFIRMED` without resolving any `ClinicLink`. After check-in, `attended`
keeps that state across local-date changes until `completed_at` is recorded, but only
for assessments on or before the clinic's scheduled date. Every confirming or visible
booking must be on or after the latest live unresolved assessment session date across
the student's active enrollments. A session without a date uses the tenant-local date
of its `ClinicLink.created_at`; an undated source must not permit unlimited reuse of
old attendance. Same-date clinics remain supported because the assessment schedule
owns a date, not an exam-end timestamp. An older attended clinic (including one with
checkout recorded but no completion) cannot confirm a newer assessment. The API
selects the next qualifying booking instead; no attendance, booking, score, or
`ClinicLink` history is rewritten by this read projection. A future booking scheduled
before a future assessment is also insufficient. Resolved, removed, or completed
sources do not move the cutoff. Completion
ends the booking state immediately: if any link is still unresolved, the student
returns to `CLINIC_REQUIRED` until the next confirmed booking; if every link has been
resolved, the state is `PASSED`. `cancelled`, `rejected`, and `no_show` never confirm
the booking state. `pending` remains visible as `승인 대기` but stays
`CLINIC_REQUIRED`. `valid_bookings` is sorted by effective date, start time, status
priority, and participant id. For an unresolved student, `current_booking` is the first
confirmed or in-progress item when one exists; otherwise it is the first visible
pending item. The projection includes only status and schedule fields, not student or
parent PII. Manual clinic PDFs remain independently authored content and do not
inherit this projection.

`name_highlight_clinic_target` is the administrative projection of the same
student-level state, not an independent attendance flag. It is `true` only while
the student's passcard is `CLINIC_REQUIRED`. A confirmed future/today booking or an
incomplete `attended` clinic that meets the same assessment-date cutoff changes the
passcard to `BOOKING_CONFIRMED` and removes
the yellow name highlight from every unresolved enrollment for that student.
`pending` does not remove it. When clinic work receives `completed_at`, unresolved
links make both the passcard and yellow highlight return immediately; resolving all
links makes the passcard `PASSED` and keeps the highlight off. All projections use
tenant-scoped student and enrollment relationships and fail closed on missing data.

Regression entry: `apps.domains.clinic.tests.StudentClinicPermissionAPITest` exercises
old attendance, a future booking before the assessment, same-day positive booking,
reload consistency, and a later requirement in another active lecture. Both idcard
and `compute_clinic_highlight_map` must agree while source and attendance rows remain
unchanged.

### Staff student-support session and ended-lecture boundary

The staff support-preview and student activity contract is owned by
`student-support-audit.md`. Support tokens revalidate the operator's active
staff access on every request and never create a student login event. Exact
homework, video, and result opens are stored in the same `OpsAuditLog` activity
stream as screen-level evidence; records begin at feature release and are not
inferred retroactively.

An ended lecture (`Lecture.is_active=False`) is not current learning access even
when its `Enrollment.status` remains `ACTIVE`. Student session, exam, homework
submission target, video list, playback, and progress paths must require an
active lecture. Historical grades and video progress use the separate readonly
history selector and may include ended lectures; they must not return a playable
session or media URL.

### Parent-selected learning submissions

A parent may submit an online exam or homework media only for the active linked
student named by `X-Student-Id`. The server never guesses a child when that header
is absent and never falls back after an invalid, unlinked, deleted, or cross-tenant
student ID. Enrollment, active lecture, and exact exam/homework assignment checks
still run before any submission row or object-store write.

The resulting `Submission.user` is the selected student's User so grades, pending
work, teacher review, and reload projections remain identical to a student-authored
submission. When the authenticated actor is the parent, the submission metadata
records `submitted_by_user_id`.

The same exact selected-child rule applies to community question/counsel writes,
student inventory and reported-score evidence, and video progress. These writes
persist to the selected child's ordinary rows so the parent, student, and staff
reload projections agree. Reported scores preserve the parent as `submitted_by`;
community posts preserve `author_role=parent`. A parent video progress POST no
longer returns an unsaved echo. Missing, stale, sibling-selected, unlinked, deleted,
or cross-tenant child context fails before mutation. Profile, account identity,
password, and administrator-only settings remain outside delegated learning access.

## 7. Minimum Change Gate

When a change touches any of these surfaces, run the smallest focused set that
proves the touched path, then broaden when the behavior crosses domains.

Always for student identity/account changes:

```powershell
cd C:\academy\backend
python -m pytest apps\domains\students\tests\test_student_identity_convergence.py apps\domains\students\tests\test_registration_password_safety.py apps\domains\students\tests\test_password_reset_safety.py apps\domains\students\tests\test_account_recovery.py -v --tb=short -x
python manage.py check --settings apps.api.config.settings.test
python manage.py makemigrations --check --dry-run --settings apps.api.config.settings.test
```

Add for OMR/results/submission changes:

```powershell
cd C:\academy\backend
python -m pytest apps\domains\student_app\tests\test_grades_summary_homework.py apps\domains\results\tests\test_submission_scope_guard.py apps\support\omr\tests\test_candidate_matching.py -v --tb=short -x
```

Add for student video/progress access changes:

```powershell
cd C:\academy\backend
python -m pytest tests\test_student_video_progress_enrollment_resolution.py -v --tb=short -x
python -m pytest tests\test_video_access_security.py -v --tb=short -x
python -m pytest apps\domains\students\tests\test_student_support.py -v --tb=short -x
```

### Student video playback controls

Tenant-resolved staff can change the default `allow_skip`, `max_speed`, and
`show_watermark` values through the ordinary video detail PATCH. An actual
default-policy change locks the video row and increments `Video.policy_version`
exactly once; title/order-only or no-op PATCH requests do not increment it. This
lets the student's periodic access check reject a stale playback grant while
avoiding needless interruption when the effective defaults did not change.

For one session, staff can update skip and/or speed defaults for selected videos
with `POST /api/v1/media/videos/bulk-policy/`. The JSON body contains the exact
positive `session_id`, one to 500 unique positive `video_ids`, and at least one
of `allow_skip` or `max_speed`. The action first locks the tenant-owned session
and all active requested video rows in deterministic order. Every ID must be an
active video in that exact tenant and session; a missing, deleted, duplicate,
cross-session, or cross-tenant ID rejects the whole request with no writes.
Only rows whose saved value changes are written, and each changed row increments
`policy_version` once. Student-level `VideoAccess` overrides continue to take
precedence over these video defaults.

`FREE_REVIEW` and `PROCTORED_CLASS` decide whether monitored playback sessions
and event writes are required. They do not erase the teacher's saved video
controls. In every non-blocked mode, `Video.allow_skip=True` (or a student-level
`allow_skip_override=True`) keeps free seeking even when an `ONLINE` attendance
still requires monitored playback. `FREE_REVIEW` also always permits free
seeking because the student is not completing a monitored substitute-attendance
watch. In review mode, `Video.max_speed` and `Video.show_watermark` remain the
effective playback-rate and watermark policy.

For a first `PROCTORED_CLASS` watch with skipping disabled, arbitrary forward
seeking is blocked, but the student may move forward through the server-approved
`budgeted_forward` control in fixed 10-second steps. The per-video allowance is
the smaller of 20% of the encoded duration and 30 minutes.
`VideoProgress.forward_skip_seconds_used` is the
server-owned cumulative counter, so refreshes, devices, tabs, and new playback
sessions do not reset it. `POST
/api/v1/student/video/videos/{video_id}/forward-skip/` locks the selected
enrollment before granting at most one step; the final grant may be shorter than
10 seconds and concurrent requests cannot exceed the allowance. Approved jumps
advance the ordinary playback position and therefore count as permitted course
progress. Backward 10-second movement remains available. Arbitrary progress-bar
forward movement and direct unapproved seeks still fail closed and enter the
proctored event stream.

The budget is unavailable when the duration is missing, and an explicit
`VideoAccess.block_seek` still blocks all seeking. Parents may watch through the
existing selected-child contract but cannot consume the student's persisted
skip allowance. Once completed `VideoProgress` or
`VideoAccess.proctored_completed_at` changes the video to `FREE_REVIEW`, free
seeking is restored; ordinary offline/review students receive the same free
seeking without needing a prior per-video completion row. No review-mode rule
overrides an explicit `block_seek=True`.
The student player must consume the nested `policy` returned by
`POST /api/v1/student/video/videos/{video_id}/playback/`; the flat video fields
are display metadata, not a second policy source.

The cross-repository rollout is backend-first: the backend adds the explicit
POST bootstrap and makes a GET without `access_check=true` fail closed with
HTTP 405. The frontend then switches to POST against that sealed contract.
There is no compatibility fallback from POST to the legacy mutating GET;
read-only access checks remain GET throughout the rollout.

Tenant-wide public-library videos intentionally have no enrollment-specific
`access_mode`. Their effective playback policy is therefore resolved as
`FREE_REVIEW`; the response keeps the public video's flat `access_mode=null`
compatibility field while the nested policy remains complete and executable,
including free seeking without a per-enrollment skip budget.

For active enrollments, lightweight read-only
`GET .../playback/?access_check=true` uses the same effective
access-mode resolver as playback issuance. Explicit offline `PROCTORED_CLASS`
and online `FREE_REVIEW` overrides therefore return the exact mode and
monitoring flag that playback enforces, while `BLOCKED` remains a 403.

### Exact video access after enrollment deactivation

An inactive enrollment does not inherit video access from a fee, invoice,
payment, attendance row, or legacy `VideoAccess` override. When an operator has
separate authorization to preserve one already-earned video, the only exception
is an explicit `InactiveVideoEntitlement` for the same tenant, student,
enrollment, and video. The entitlement records its access mode, source and
source reference, nonblank reason, actor reference, grant time, optional expiry,
and revoke actor/reason/time. Revoked grants remain as history; a partial unique
constraint permits only one current grant for the exact enrollment and video.

The exception is valid only while the student, user, and student membership are
active; the enrollment is `INACTIVE`; the lecture remains active; the video is
`READY`; and the enrollment still has an exact `SessionEnrollment` for the
video's session. Tenant, student, enrollment, lecture, session, and video must
all agree. Expired or revoked grants fail closed. An explicit `BLOCKED` video
access row wins over the entitlement. Legacy `VideoAccess` rows never make an
inactive enrollment eligible on their own.
Soft-deleted videos (`deleted_at` set) are ineligible even when their retained
foreign-key row or R2 object still exists: student home/session surfaces omit
the video and thumbnail, playback/write resolution fails closed, and staff
history reports the entitlement as `INELIGIBLE`.

The target must be Academy-hosted revocable media. YouTube embeds cannot be
bounded or revoked by the Academy CDN contract, so staff grant returns HTTP 400
with `code=video_source_unsupported`, and a legacy/direct YouTube entitlement
row still fails closed at runtime.

Student video home and session-video responses expose only the exact entitled
session and video. They do not reopen future sessions, exams, homework,
attendance, fees, social actions, or the rest of the lecture. Playback,
required progress and forward-skip writes, and lightweight
`?access_check=true` revalidation use the same access context. Likes and
comments remain active-enrollment-only and return 403 for an inactive
enrollment even when that exact video is entitled. The access check returns
only `ok`, effective `access_mode`,
`monitoring_enabled`, and `policy_version`; it does not create playback
sessions, activity records, or view counts. A full `POST .../playback/`
bootstrap rechecks the
lecture, enrollment, video, and effective mode under row locks before recording
activity or view count. Every mode receives a bounded current-access token;
only `PROCTORED_CLASS` creates a monitored playback session. For ordinary
active playback, the current-access token and monitored session lease remain
bounded to `VIDEO_PLAYBACK_TTL_SECONDS`; the signed HLS URL is separately
bounded to the encoded video duration plus that grace period. Heartbeat extends
the monitored lease, while token renewal rotates the short current-access
credential. Keeping the media URL, token, and session lease separate prevents
the player source from being replaced every 10 minutes while preserving
revocation checks and 30-second access revalidation.

Before a current-access token expires, the client renews it through
`POST /api/v1/media/playback/renew/`. Renewal revalidates the exact current
tenant, user, student, enrollment or direct entitlement, lecture, session,
video, access mode, and policy version. It returns a new token and expiry while
preserving the existing monitored `VideoPlaybackSession`; it does not create a
new playback session or increment view/activity counters. Ordinary active
enrollment media keeps its existing duration-bounded HLS URL. Direct and
inactive-entitlement media receive a newly signed URL bounded by the renewed
short expiry. A revoked, expired, cross-scope, ended-lecture, changed-policy, or
inactive monitored session fails closed instead of being recreated by refresh.

For inactive entitlements only, the playback token, HLS URL, and thumbnail URL
expire at the earliest of the current access TTL (600 seconds by default) and
`expires_at`; configuration drift above 600 seconds is clamped back to 600 for
this inactive-only path. The locked playback grant is authoritative: HLS and
thumbnail signatures are rebuilt after that grant using its exact expiry. A
single absolute expiry from the locked clock is reused for the monitored session,
playback token, response, HLS URL, and thumbnail URL. A scheduler delay or a
concurrent replacement with a shorter entitlement cannot extend any one of
those artifacts. CDN workers
validate only the URL expiry and HMAC; they have no entitlement callback.
Therefore revoke immediately blocks new access checks, playback grants, and
token validation, while an already issued CDN URL can remain usable until its
bounded expiry (at most the access TTL). This contract does not claim immediate
revocation of an already issued CDN signature. For active, system/public, and
other ordinary playback, the HLS signature lasts at most the encoded video
duration plus `VIDEO_PLAYBACK_TTL_SECONDS`; the playback token remains
short-lived and renewable. Legacy READY rows without duration retain the
historical 24-hour media ceiling. This bounds already-issued ordinary media
after a lecture close without extending the monitored session lease or token
lifetime.
Signed URL query parameters and playback tokens are bearer credentials and are
never logged; playback logs retain only video id, safe status/path metadata,
and expiry timestamps.

Inactive-entitlement progress and forward-skip writes lock the exact lecture,
session, enrollment, video policy version, and entitlement in the same order,
then revalidate immediately before mutation. If revoke or another policy change
commits after the request's initial access check, the stale write returns 403
and creates or updates no progress or skip-budget row. Ordinary active
enrollment write behavior remains unchanged.

Staff manage this exception through tenant-resolved video administration:

- `GET /api/v1/media/inactive-video-entitlements/` lists auditable grants and
  accepts exact `student_id`, `enrollment_id`, and `video_id` filters;
- `POST /api/v1/media/inactive-video-entitlements/` grants or idempotently
  refreshes one exact current entitlement;
- `POST /api/v1/media/inactive-video-entitlements/{id}/revoke/` revokes it
  idempotently.

Grant and revoke lock the scoped rows and validate the current graph again
inside the transaction. They do not increment the video-wide
`Video.policy_version`: exact entitlement row state and expiry are revalidated
directly, so unrelated ACTIVE students' existing tokens and monitored sessions
remain valid.
An operator may stage a grant while the exact enrollment is still `ACTIVE` so a
withdrawal transition can be completed without an access gap; the staged row is
reported as `STAGED` and has no effect on ordinary active-enrollment policy. It
becomes eligible only after that same enrollment is `INACTIVE` and every other
inactive-entitlement guard passes.
The list reports `INELIGIBLE` instead of `ACTIVE` if the enrollment is inactive
but a current account, lecture, video, session-scope, tenant, or blocking guard
no longer passes.
They do not alter student, enrollment, attendance, session-enrollment, fee,
invoice, payment, exam, or homework state and do not enqueue notifications.
`STAFF_AUTHORIZATION` is an explicit manual source; it must not be presented as
proof that an invoice or payment exists. Both a database CHECK constraint and
runtime scope validation reject every other stored source, so a direct import
cannot turn a payment-derived or unknown value into learning access.

If playback policy cannot be resolved for the selected active enrollment or
the exact inactive entitlement, the request fails closed rather than choosing
another enrollment or tenant.

An `ACTIVE` enrollment alone does not keep a paid lecture open after the
lecture is ended. When a regular `Lecture.is_active` becomes false, student and
parent operational reads remove its sessions, current exams, video library,
playback and progress writes immediately. The tenant-wide `is_system` public
library remains available. Published exam and homework history stays readable
from the grades contract so ending a lecture revokes learning access without
erasing the student's record.

Regular lecture deactivation locks the lecture row and changes its state in the
same transaction that marks every ACTIVE monitored playback session for that
lecture `REVOKED`. A concurrent playback grant uses the same lecture-first lock,
so it either commits before close and is revoked by that close transaction, or
observes the committed close and issues no token/session. Refresh and heartbeat
reject existing regular-lecture tokens after close. Client disposal may still
flush final Redis violation/total counters, but its ACTIVE-only status update
cannot downgrade a server `REVOKED` row to `ENDED`. System-library lectures are
excluded from this close-time revocation compatibility path.

Add for frontend account/student UI changes:

```powershell
cd C:\academy\frontend
pnpm typecheck
pnpm guard:legacy-api
pnpm build
pnpm exec playwright test e2e\auth\account-recovery-modal.spec.ts --reporter=list
```

Launch-readiness and broader real-use gates are tracked in
`../refactor/student-domain-launch-readiness.md`.

## 8. Do Not

- do not add a second student identity helper in serializers/views/frontend only;
- do not store plaintext signup or temporary passwords;
- do not reset a public user's actual password before delivery/activation;
- do not use a stored different phone when the public user proved another phone;
- do not send account notices through SMS fallback;
- do not expose account existence in public recovery responses;
- do not let OMR, clinic, homework, results, or QnA choose a student by name alone;
- do not treat admin-side success as complete until the student/parent-facing
  projection is checked when that projection exists.
