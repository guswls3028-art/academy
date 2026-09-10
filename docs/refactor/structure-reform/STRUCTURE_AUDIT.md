# Structure Reform Audit

**Status:** [VERIFIED] codebase audit with [INFERRED] refactor risk notes  
**Captured:** 2026-05-22  
**Scope:** backend `C:\academy\backend`, frontend `C:\academy\frontend`  
**Excluded:** current account-recovery/password/Alimtalk release implementation changes, `dnfm`, `dnfm-group`

This audit is Phase 0 material. It does not approve a large rewrite. It records
where feature entrypoints currently split, where tenant/data integrity is not
structurally enforced, and which small refactor should start Phase 1.

## 1. Current Structure Summary

[VERIFIED]

- `C:\academy` is a workspace, not a git repository. `backend/` and `frontend/`
  are separate git repositories.
- Backend root API routing lives in `apps/api/v1/urls.py`.
- Backend product domains currently live under `apps/domains/`.
- Frontend role surfaces live under `src/app_admin`, `src/app_teacher`,
  `src/app_student`, plus `auth`, `core`, `shared`, `landing`.
- Current refactor inventory measured 27 backend domain directories,
  117 backend cross-domain imports, 632 backend cross-domain internal imports,
  0 frontend cross-app imports, and 0 `shared -> app_*` imports.

Backend API prefixes relevant to this audit:

| Prefix | Include | Notes |
|---|---|---|
| `/students/` | `apps.domains.students.urls` | Admin/teacher student management, registration, password/reset helpers |
| `/student/` | `apps.domains.student_app.urls` | Student/parent app BFF profile, sessions, results, video, fees |
| `/lectures/` | `apps.domains.lectures.urls` and `apps.domains.attendance.urls` | Shared prefix for lecture and attendance endpoints |
| `/results/` | `apps.domains.results.urls` | Admin/student grade surfaces |
| `/homework/` | `apps.domains.homework.urls` | Homework policy/enrollment/assignment and homework score route import |
| `/homeworks/` | `apps.domains.homework_results.urls` | Homework result surface |
| `/clinic/` | `apps.domains.clinic.urls` | Clinic sessions, participants, submissions |
| `/messaging/` | `apps.domains.messaging.urls` | Templates, config, logs, send, previews |
| `/auth/` | `apps.core.auth_urls` | Auth and current account-recovery dispatch |
| `/jobs/` | `apps.domains.ai.urls` | AI job status/queue API |

## 2. Student Domain Canonical Source

[VERIFIED]

The canonical persisted student source is `apps.domains.students.models.Student`.
It owns:

- tenant membership context through `tenant`, `user`, and `TenantMembership`;
- identity fields: `ps_number`, `omr_code`, `name`, `phone`, `parent_phone`;
- parent relation: `parent`;
- school/profile fields: gender, grade, school type and school names/classes,
  address, memo, profile photo keys;
- lifecycle fields: `is_managed`, `deleted_at`;
- student-app schedule visibility fields: `schedule_hidden_before`,
  `schedule_hidden_ids`;
- tags through `Tag` and the join table.

[VERIFIED]

Tenant-scoped student selectors and profile/lifecycle services now exist for the
touched paths. Profile update, soft delete, restore, permanent-delete, and the
student account creation graph have canonical service boundaries while keeping
deployed URLs stable.

[INFERRED]

Student create still has split orchestration behavior: validation, single-create
duplicate response shape, API response shapes, import job dispatch, and approval
notifications remain caller-owned. The durable account graph, registration
approval transition, Excel/import row orchestration, and JSON bulk/conflict row
orchestration are now centralized. Schedule visibility, read DTO convergence,
and domain-event cleanup still have split behavior across views, serializers,
service helpers, repository wrappers, frontend mapping functions, and E2E
helpers. Therefore a patch outside the canonicalized paths is not guaranteed to
reach every surface.

## 3. Student Data Read Paths

[VERIFIED]

| Surface | Current path | Current implementation |
|---|---|---|
| Admin student list/detail | `/api/v1/students/`, `/api/v1/students/{id}/` | `StudentViewSet.get_queryset` plus `StudentSerializer` / `StudentDetailSerializer` |
| Teacher student list/detail | same `/students/` API | `src/app_teacher/domains/students/api.ts`, using `src/shared/api/contracts/students` |
| Student/parent profile | `/api/v1/student/me/` | `StudentProfileView`, manual response DTO |
| Student legacy self profile | `/api/v1/students/me/` | `StudentViewSet.me`, separate GET/PATCH shape |
| Auth current user | `/api/v1/core/me/` | `UserSerializer.linkedStudents`, reads `parent.students` |
| Student app sessions/results/dashboard | `/api/v1/student/*` | BFF views aggregate enrollment, attendance, exams, results, video, fees |
| Messaging send targets | `/api/v1/messaging/send/` | `SendMessageView`, direct `Student.objects.filter(...)` |
| Clinic/attendance/results helpers | domain-specific endpoints | Clinic participant/session/idcard, attendance roster, and results admin student-grades touched paths use student/enrollment selectors |

Important finding: this feature is actually at least 7 read roots when "student
identity/profile as shown to users" is counted across admin, teacher, student
app, auth, messaging, clinic/attendance/results, and E2E helper paths.

## 4. Student Data Write Paths

[VERIFIED]

| Feature | Current write roots | Evidence |
|---|---:|---|
| Single student create | 1 primary, plus compatibility surfaces | `StudentViewSet.create` validates/responds and calls `create_student_account` for Parent/User/Student/Membership |
| Bulk JSON create | 1 service root plus compatibility dispatch | `StudentViewSet.bulk_create` validates the transport payload and calls `import_students_from_rows` |
| Excel/import create | 1 service root plus compatibility dispatch | `bulk_create_from_excel` dispatches worker; `StudentViewSet.excel_job_status` exposes the regex job route; `ExcelParsingService` calls `import_students_from_rows`; legacy `bulk_from_excel.py` delegates to the import service |
| Lecture enrollment existing-student match | 0 student write roots | `lecture_enroll_from_excel_rows` calls the tenant-scoped active-student identity selector; it does not create, restore, or mutate account credentials |
| Registration approve | 1 service root plus compatibility HTTP facades | `approve_registration_request` owns approval state and account graph transaction; views own response shape and nonfatal approval-message dispatch |
| Admin/student profile update | 3 roots | `StudentViewSet.perform_update`, `StudentViewSet.me`, `StudentProfileView.patch` |
| Schedule visibility | 3 action roots | `StudentSessionClearPastView`, `StudentSessionHideView`, `StudentSessionUnhideView` mutate Student fields |
| Soft delete / restore / permanent delete | 6 roots | `destroy`, `bulk_delete`, `bulk_restore`, `bulk_permanent_delete`, duplicate fix, purge command |
| Password/account recovery | 4 API roots | `/auth/account-recovery/dispatch/`, `/students/password_reset_send/`, `/students/password_find/*`, `/students/send_existing_credentials/` |

Required report statement: this function is actually 6+ roots for student
creation/restoration and 4 roots for password/account recovery.

## 5. Why Student Patches Drift

[VERIFIED]

- `StudentProfileView.patch`, `StudentViewSet.perform_update`, and
  `StudentViewSet.me` now route writes through
  `students.services.profile.update_student_profile`, so parent relink and OMR
  recompute drift has been removed for those paths.
- The remaining profile risk is response DTO drift: admin, legacy self-profile,
  student-app profile, and auth bootstrap still expose different read shapes.
- `Student.save()` updates `User.username` and inventory `student_ps` when
  `ps_number` changes. This means the students model directly modifies another
  domain's internal data.
- Frontend admin `mapStudent` computes `active` from `is_managed`, but deletion,
  user active state, enrollment status, and `is_managed` are different backend
  states.
- Frontend admin `updateStudent` with `noPhone` sends a synthetic phone and
  `omr_code`, while backend treats `omr_code` as read-only on update and computes
  it from phone/parent phone.
- Parent password meaning is now explicit: new parent users require a staff-entered
  password or the validated registration-request password hash. Existing parent
  passwords are never replaced during create, restore, relink, or import. Phone-tail
  derivation, compatibility ensure commands, and hidden account creation were removed.
  Parent relink and missing-account repair return explicit state and require successful
  account-notice delivery before their transaction can commit.

Required report statement: this field is used as backend meaning A and frontend
meaning B in multiple places. `uses_identifier/no_phone/omr_code` is the clearest
case: backend uses it to determine identity and OMR derivation, while frontend
uses `noPhone` to synthesize a phone-like value and attempts to send `omr_code`.

## 6. Duplicate Root And Legacy Risks

[VERIFIED]

- Admin student API is now a compatibility facade over the shared student
  contract. Teacher student surfaces use the shared contract, while some auth
  and admin-internal callers still go through the admin facade.
- `auth/pages/SignupModal.tsx` imports admin student API internals for public
  signup helpers.
- `auth/api/recovery.api.ts` calls `/auth/account-recovery/dispatch/`, while
  admin/teacher/public student APIs still call `/students/password_*` or
  `/students/send_existing_credentials/`.
- E2E `e2e/admin/dnb-lectures-sessions.spec.ts` calls
  `/api/v1/students/students/`, while the deployed current route is
  `/api/v1/students/`.

Required report statement: this screen/test is not using the latest API. The E2E
lecture/session helper uses a legacy or wrong `/students/students/` route and
must be detected before refactoring deletes or renames any student path.

## 7. Tenant And Data Integrity Risks

[VERIFIED]

High-risk patterns found:

- `academy/adapters/db/django/repositories_students.py` contains helpers that can
  run without tenant, including `tag_all(tenant=None)`,
  `user_create_user(tenant=None)`, `tag_get(tenant=None)`,
  `student_filter_tenant_deleted()`, `user_filter_username(username)`,
  `user_filter_phone(phone)`, `user_filter_phone_exists(phone, tenant=None)`,
  `student_filter_deleted_dup_groups()`, and
  `student_filter_deleted_before_cutoff(cutoff)`.
- `apps/core/serializers.py` reads `parent.students.filter(...)` without an
  explicit tenant filter for `linkedStudents`; it relies on the parent relation
  invariant rather than a tenant-scoped selector.
- `students.services.lifecycle.permanently_delete_students` uses guarded raw SQL
  to delete/update data in results, submissions, homework, attendance,
  enrollment, exams, video, progress, fees, clinic, community, students, core
  membership, and accounts user tables. `bulk_permanent_delete`, conflict
  delete, deleted-duplicate cleanup, and purge commands are compatibility
  facades over the service.
- Some raw delete/update clauses rely on already tenant-scoped student/user IDs
  rather than repeating tenant in every table operation.
- Management commands for deleted student duplicate cleanup and purge operate on
  broad querysets and need explicit tenant/run-mode guardrails.

Required report statement: tenant filter is not structurally guaranteed in these
helper paths. Some callers pass tenant correctly, but the helper signatures still
allow unsafe no-tenant usage.

## 8. Domain Boundary Collapse Points

[VERIFIED]

- Students model save logic imports and updates inventory models.
- Student permanent delete no longer edits internal tables from views/commands,
  but the canonical lifecycle service still owns a guarded cross-domain SQL
  graph across assessment, academics, clinic, media, fees, community, and core.
  Domain hooks/events are still the target boundary.
- Community, fees, messaging, results, and parts of enrollment still import or
  query `Student` directly in views/serializers/services. Touched clinic
  participant/session/idcard and attendance roster paths now use public
  student/enrollment selectors instead of direct `Student.objects` /
  `Enrollment.objects` reads.
- `homework.views.__init__` imports `HomeworkScoreViewSet` from
  `homework_results`, so two homework-related roots are coupled at URL/export
  level.
- `student_app` is a BFF surface but lives as a domain and performs aggregation
  across sessions, attendance, exams, results, video, and fees.
- Frontend auth and some admin-internal surfaces still import admin student
  compatibility paths. Teacher student/storage and student inventory surfaces
  now use shared contracts directly.

Required report statement: these domains are directly modifying or reading other
domains' internal models/tables rather than going through public selectors,
services, or events.

## 9. Bug Traceability Problems

[INFERRED from verified roots]

- Student lifecycle state is still spread across `Student.deleted_at`,
  `Student.is_managed`, `User.is_active`, `TenantMembership.is_active`,
  `Enrollment.status`, clinic participant status, and messaging side effects.
- Soft delete, restore, permanent-delete, account graph creation, and
  registration approval entrypoints now have explicit service use-cases.
  Create/import row orchestration, schedule visibility, and domain-event cleanup
  remain split.
- Notifications are fired inside view/service flows rather than through a
  durable domain event/outbox contract.
- Logs exist in some destructive paths, but a uniform
  `tenant/actor/source/use_case/correlation_id` convention is not present across
  student create/update/delete/recover/import.
- Current tests cover important patches, but they mostly target specific
  endpoints. They do not yet assert that one canonical student update is visible
  through admin, teacher, student app, auth linked students, messaging recipient,
  and E2E UI surfaces.

## 10. Implemented Reform Slices

[COMPLETED 2026-05-22]

- Phase 1 student profile canonicalization introduced tenant-scoped student
  selectors and `students.services.profile.update_student_profile`.
- Phase 2 enrollment canonicalization introduced
  `apps.domains.enrollment.selectors` and
  `apps.domains.enrollment.services.lifecycle`.
- Student enrollment matrix still uses the existing student URL, but the view is
  now a facade over the enrollment domain. Session/exam/homework toggles are
  validated against tenant + student + lecture + active enrollment in one use
  case.
- Exam/homework assignment screens now use the enrollment active-session roster
  selector instead of recomputing roster scope independently.
- Phase 3 notification queue payloads now include source/use-case/domain object
  metadata; attendance passes actor metadata for status-change sends.
- Phase 4 `dispatch_job` now blocks missing tenant/source and payload
  tenant-mismatch before creating an AI job.
- Phase 5 frontend shared-contract cleanup moved community contracts, patch
  notes data, video access/rule contracts, reusable video thumbnail UI,
  lecture/session attendance API, storage/inventory API, student API contracts,
  student Excel utilities, fees API/status contracts, the tools timer download
  contract, the exam enrollment contract, the tenant info contract, submissions
  API/types, and lecture section APIs out of admin internals. Frontend
  cross-app/admin role imports are now 0.
- Clinic participant/session/idcard active-student reads now use
  `students.selectors` and `enrollment.selectors`. Deleted students are blocked
  from participant create through `student`, `enrollment_id`, and student
  self-booking; deleted student idcard requests return the empty safe payload.
- Clinic participant status, complete, and uncomplete writes now use
  `apps.domains.clinic.services.lifecycle`. The HTTP view is a facade over the
  service and keeps only serializer response shaping plus `transaction.on_commit`
  notification dispatch.
- Clinic participant creation and booking-change writes now use the same
  lifecycle service. Tenant/student/enrollment validation, row-locked capacity
  checks, duplicate checks, status defaults, and notification event contexts are
  no longer view-owned compatibility paths.
- Attendance roster create now uses
  `apps.domains.attendance.services.create_attendance_roster`; the shared
  `ensure_session_roster_membership` unit also backs session-enrollment bulk
  create. Active tenant-scoped student validation, enrollment reactivation,
  auto-assigned fee reactivation, session roster creation, attendance
  idempotency, and malformed ID 400s are covered by local and production QA.
- Student lifecycle services now cover soft delete, restore, and permanent
  delete entrypoints. Permanent delete remains a compatibility service with a
  guarded raw SQL graph, but `bulk_permanent_delete`, conflict delete,
  duplicate cleanup, and purge commands no longer own separate destructive
  logic.
- Account recovery temporary password generation now has one SSOT at
  `apps.core.services.password.TEMP_PASSWORD_LENGTH = 6`. Public login recovery
  uses the canonical dispatch endpoint and production AlimTalk logging confirms
  successful send while preserving credential-body redaction. Legacy student
  password endpoints remain compatibility roots and still need deprecation
  logging before removal.
- Student account graph creation now has one SSOT at
  `apps.domains.students.services.create_student_account`. Direct create, JSON
  bulk create, conflict delete-and-recreate, registration approval, lecture
  enrollment import, and student Excel import all share Parent ensure, User
  password/hash assignment, Student creation, and student TenantMembership
  activation. Frontend teacher create routes through the shared student
  contract, admin Excel upload forwards the welcome toggle, and worker payload
  booleans are parsed explicitly.
- Registration approval orchestration now has one SSOT at
  `apps.domains.students.services.approve_registration_request`. Approve,
  bulk_approve, and auto-approve share the same row lock, pending-state guard,
  signup password hash transfer, `pending -> approved` transition, and student
  account graph call. Approval AlimTalk dispatch remains a view compatibility
  boundary and is nonfatal after the durable transaction commits.

## 11. Phase 1 Recommendation

[COMPLETED INITIAL SLICE]

Phase 1 should start with `students` canonical read/write path, not with file
movement:

1. Add `students/selectors.py` for tenant-scoped reads.
2. Add `students/services.py` or a small `students/services/*` set for
   create/update/profile/lifecycle use-cases.
3. Route existing views through the new services without changing URLs.
4. Add deprecation logging around legacy/self/profile/password helper paths that
   are not removed yet.
5. Move frontend admin/teacher/student/auth API calls toward a shared student
   contract after backend contract snapshots exist.

This area is high-value because student identity/profile is the source used by
attendance, grades, clinic, messaging, storage, signup, and student app UX. It
is also bounded enough to start with tests and compatibility layers rather than
physical package moves.
