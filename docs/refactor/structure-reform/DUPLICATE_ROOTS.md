# Duplicate Roots

**Status:** [VERIFIED] code/path audit plus [PROPOSED] canonical candidates  
**Captured:** 2026-05-22  
**Scope:** student-centered duplicate roots and adjacent high-risk paths

Risk scale:

- P0: tenant/data-loss/security risk
- P1: high user-visible drift or operational incident risk
- P2: medium drift or refactor blocker
- P3: cleanup/documentation risk

| Feature | Current paths | Actual callers/screens/APIs | Canonical candidate | Remove/integrate target | Risk | Tests needed |
|---|---|---|---|---|---|---|
| Student list/detail read | Backend `StudentViewSet.get_queryset`, `StudentDetailSerializer`; frontend admin `students.api.ts`; teacher `students/api.ts`; clinic/messaging/results direct reads | Admin students home/detail, teacher students list/detail, clinic add participant, messaging send, results grade pages | `students.selectors.list_students`, `get_student_detail`, `get_student_for_tenant` | Direct `Student.objects` reads in non-students domains; duplicated FE mappers | P1 | API contract snapshot, tenant isolation, admin/teacher/student profile visibility |
| Student profile read | `/students/{id}/`, `/students/me/`, `/student/me/`, `/core/me linkedStudents` | Admin/teacher detail, student profile, parent child switcher, auth bootstrap | `students.selectors.get_student_profile_context` | Manual DTO builders in `StudentProfileView` and `StudentViewSet.me` | P1 | Snapshot all four responses before convergence |
| Student create | `[PARTIAL ACCOUNT GRAPH + IMPORT + JSON BULK COMPLETED]` `StudentViewSet.create`, `bulk_create`, `approve_registration_request`, Excel import worker, E2E direct API setup | Admin create modal, teacher create sheet, signup approval, student Excel import, E2E fixtures | `students.services.create_student_account` for Parent/User/Student/Membership graph; `students.services.import_students_from_rows` / `resolve_student_import_row` / `resolve_student_import_conflicts` for import row orchestration | View-level user/parent/membership creation logic removed from active create/import/bulk roots; remaining single-create duplicate response shape stays view-owned compatibility | P1 | `[DONE]` account graph, parent notice, membership, welcome flag regression, frontend contract build, registration approval orchestration, import orchestration, JSON bulk/conflict orchestration |
| Student restore | `[COMPLETED]` `bulk_restore` and `bulk_resolve_conflicts` are compatibility facades | Deleted student page, conflict resolver, student import | `students.services.restore_student` | Local unmangle/reactivate/relink implementations removed from the active paths | P0 | `[DONE]` restore identity collision, tenant isolation, user/membership/parent behavior, no enrollment ghost reactivation |
| Student update | `StudentViewSet.perform_update`, `StudentViewSet.me`, `StudentProfileView.patch`, teacher `updateStudent`, admin `updateStudent` | Admin edit modal, teacher detail, student profile page | `students.services.update_student_profile` | Direct `student.save()` update branches in views | P1 | Parent relink, OMR recompute, school mapping, profile photo, id collision |
| Student schedule hidden state | `StudentSessionClearPastView`, `StudentSessionHideView`, `StudentSessionUnhideView` write `Student.schedule_hidden_*` | Student sessions page | `students.services.update_schedule_visibility` or student-app policy wrapper | Inline mutation in student-app session views | P2 | Student/parent tenant and child selection tests |
| Student soft delete | `[COMPLETED]` `StudentViewSet.destroy`, `bulk_delete` are compatibility HTTP facades | Admin/teacher delete, E2E cleanup | `students.services.soft_delete_student` plus enrollment/clinic lifecycle hooks | Direct duplicated user/membership/enrollment/clinic side effects removed from student views | P0 | `[DONE]` soft delete state machine, single/bulk routing, enrollment deactivation, clinic cancellation |
| Student permanent delete | `[COMPLETED ENTRYPOINT]` `bulk_permanent_delete`, `bulk_resolve_conflicts` delete action, purge/deleted duplicate commands are compatibility facades | Deleted students admin cleanup, conflict re-register, E2E cleanup, retention purge | `students.services.permanently_delete_students`; future domain hooks/events for cross-domain cleanup | Raw SQL removed from view/commands. Guarded destructive table graph still lives in student lifecycle service until domain hooks exist | P0 | `[DONE]` tenant isolation, fee/section FK cleanup, command routing, retained-account profile guard, production cleanup QA; `[TODO]` dry-run/domain hook extraction |
| Parent link/account | `[COMPLETED]` explicit creation and lookup-only recovery/restore paths are separated | Staff student create/edit, signup approval, parent app, account recovery | `parents.services.ensure_parent_account_for_student` for explicit creation; `find_parent_account` for reads | Deleted implicit-password facade and bulk password/account commands; student self-service cannot relink Parent | P1 | `[DONE]` direct/fixed or random password only, existing password preservation, login-ID collision denial, selected-child fail-closed coverage, enrollment/video/account notice regression |
| Password/account recovery | `[SEALED]` `/auth/account-recovery/dispatch/`, `/students/password_reset_send/`, `/students/send_existing_credentials/`; legacy `/students/password_find/request/verify` returns 410 | Auth recovery UI, admin password modal, teacher password modal, signup duplicate flow, E2E password reset | `apps/domains/students/services/account_recovery.py` plus `apps/core/services/password.py` SSOT | Public/duplicate/admin reset paths delegate to account recovery service; OTP 인증번호 path is sealed; account NotificationLog target metadata powers student detail status | P1 | `[DONE]` account recovery/password safety tests, account notification metadata; `[TODO]` production canary after deploy |
| Registration request approve | `[COMPLETED]` `RegistrationRequestViewSet.approve`, `bulk_approve`, auto-approve inside create are compatibility facades | Signup modal, admin requests page, teacher comms notifications | `students.services.approve_registration_request` | View-owned create/status logic removed; view keeps response shape and nonfatal message dispatch | P1 | `[DONE]` approval contract, auto-approve, duplicate fallback, parent password payload, notification-failure nonrollback |
| Excel student import | `[COMPLETED]` `/students/bulk_create_from_excel/`, `ExcelParsingService`, legacy `bulk_from_excel` facade | Admin/teacher student Excel upload, AI job status | `students.services.import_students_from_rows` called by student-registration worker | Student creation/password policy is isolated from enrollment Excel; teacher mobile no longer uploads with hidden `0000`/implicit welcome flag | P1 | `[DONE]` job payload contract, row validation, duplicate/restore, welcome flag, local/prod sheet rendering, production worker QA, AlimTalk log success |
| Excel enrollment | `[EXISTING-ONLY]` `/enrollments/lecture_enroll_from_excel/`, `ExcelParsingService`, `lecture_enroll_from_excel_rows` | Admin lecture/session Excel enrollment, AI job status | `enrollment.services.lecture_enroll_from_excel_rows` plus tenant-scoped active-student selector | Student ID exact first, else exact name+normalized parent phone; no student create/restore/password branch; missing or ambiguous rows are reported without account mutation | P0 | `[DONE]` tenant isolation, identifier priority, sibling suffix names, ambiguous fallback, mixed existing/missing rows, account credential invariants, retry payload, desktop/390px UI contract |
| Enrollment matrix | `/students/{id}/enrollment-matrix/`, `/toggle/` | Admin student enrollment matrix drawer | `[COMPLETED] enrollment.selectors` + `enrollment.services.lifecycle` public API called by students facade | Student domain view no longer mutates enrollment/session state directly; legacy URL remains as facade | P2 | `[DONE]` tenant isolation and session/exam/homework scope tests |
| Exam/homework assignment roster | `ExamEnrollmentManageView`, `HomeworkEnrollmentManageView`, `HomeworkAssignmentManageView` each built valid roster independently | Exam enrollment modal, homework enrollment/assignment screens | `[COMPLETED] enrollment.selectors.active_session_enrollments_for_session` | Per-view SessionEnrollment filtering with missing tenant/status/deleted checks | P1 | `[DONE]` cross-tenant corrupted row exclusion and inactive enrollment rejection |
| Attendance roster create | `[COMPLETED]` `AttendanceViewSet.bulk_create` delegates to `attendance.services.create_attendance_roster`; `bulk_create_session_enrollments` shares `ensure_session_roster_membership` | Attendance/lecture sessions flows, session-enrollment bulk API | `attendance.services.create_attendance_roster` + `ensure_session_roster_membership` | View-owned roster/enrollment/fee side effects removed from active roster create path | P1 | `[DONE]` cross-tenant, mixed-tenant, soft-deleted student rejection, serializer FK scoping, malformed ID 400s, roster idempotency, production API QA |
| Results student grades | `[COMPLETED]` `/results/admin/student-grades/` validates `student_id` and resolves active tenant student through `students.selectors` | Admin/teacher student detail grades | Current view-level grade aggregation plus `students.selectors.active_student_by_id`; later `results.selectors.get_student_grades` extraction | Direct Student existence check removed from this view | P2 | `[DONE]` malformed, missing/deleted/cross-tenant contract plus achievement contract |
| Clinic participants | `[COMPLETED WRITE SSOT]` Clinic participant/session/idcard HTTP paths now use `students.selectors` and `enrollment.selectors`; create, change_booking, status, complete, and uncomplete writes route through `clinic.services.lifecycle` | Clinic operations console, add participant, student booking/idcard | `[COMPLETED] clinic.services.create_participant`, `change_participant_booking`, `change_participant_status`, `complete_participant`, `uncomplete_participant`; `[NEXT] clinic event/outbox boundary` | View-owned participant write orchestration removed; notification dispatch still uses `transaction.on_commit` compatibility | P1 | `[DONE]` deleted/cross-tenant participant create rejection, transition state-machine tests, nullable-lock regression, create/change_booking service tests, production transition/AlimTalk QA; `[TODO]` event/outbox extraction |
| Messaging recipients | `[COMPLETED]` `SendMessageView` student/parent branch and manual notification preview resolve recipients through `messaging.services.recipients` | Messaging send, manual notification preview/confirm, event notifications | `messaging.services.recipients.resolve_student_message_recipients` with students selector DTO | Remaining event notification path still uses caller-supplied student objects until outbox/event extraction | P1 | `[DONE]` parent/student target selection, duplicate/missing/cross-tenant/deleted ID handling, raw phone redaction/token payload |
| Frontend student DTO mapping | `[PARTIAL]` Admin students path is now a compatibility facade over `src/shared/api/contracts/students`; teacher student surfaces use the shared contract; auth signup still imports the admin facade | Admin students, teacher students, student profile, signup modal | `src/shared/api/contracts/students` now; generated schema later | Remaining auth/admin-internal compatibility imports after role-app cleanup | P2 | `[DONE]` typecheck, focused role E2E; `[TODO]` auth signup contract snapshot |
| Frontend storage/inventory API wrappers | `[COMPLETED]` Admin storage path is now a compatibility facade over `src/shared/api/contracts/storage`; teacher storage and student inventory use shared contract directly | Admin storage/matchup, teacher storage, student inventory, storage quota | `src/shared/api/contracts/storage` | Role-app imports of admin storage API internals removed for this surface | P2 | `[DONE]` typecheck, admin storage integration E2E, teacher storage/student inventory E2E |
| Frontend fees API/status wrappers | `[COMPLETED]` Admin fees API and status paths are now compatibility facades over shared contracts/product labels; teacher fees imports shared directly and handles disabled-feature/403 states explicitly | Admin fees, teacher fees dashboard, teacher fees invoices | `src/shared/api/contracts/fees` and `src/shared/product/fees/feesStatus` | Role-app imports of admin fees API/status internals removed for this surface | P2 | `[DONE]` typecheck, build, focused teacher fees E2E with local feature-flag route override |
| Frontend tools timer API wrapper | `[COMPLETED]` Admin stopwatch timer API path is now a compatibility facade over the shared tools contract; teacher timer imports shared directly | Admin stopwatch/timer, teacher timer | `src/shared/api/contracts/tools` | Role-app import of admin tools timer API internals removed for this surface | P3 | `[DONE]` typecheck, focused teacher timer E2E, boundary snapshot 7→6 |
| Frontend exam enrollment API wrapper | `[COMPLETED]` Admin exam enrollment API path is now a compatibility facade over the shared exam enrollment contract; teacher exam detail and OMR pages import shared directly | Admin exam setup/enrollment, admin lecture score entry, teacher exam detail, teacher OMR | `src/shared/api/contracts/examEnrollments` | Role-app imports of admin exam enrollment internals removed for this surface | P2 | `[DONE]` typecheck, build, focused teacher OMR E2E, boundary snapshot 6→4 |
| Frontend tenant info API wrapper | `[COMPLETED]` Admin profile API re-exports the shared tenant info contract; teacher organization settings imports shared directly | Admin profile/settings, teacher organization settings | `src/shared/api/contracts/tenantInfo` | Role-app import of admin profile tenant info internals removed for this surface | P2 | `[DONE]` typecheck, build, focused teacher organization settings E2E, boundary snapshot 4→3 |
| Frontend submissions API/types wrappers | `[COMPLETED]` Admin submissions type and materials submissions API paths plus the teacher submissions API path are compatibility facades over the shared submissions contract; student submit and teacher submissions inbox import shared directly | Admin submissions/materials submissions, teacher submissions inbox, student submit page | `src/shared/api/contracts/submissions` | Role-app imports of admin submissions internals removed for this surface | P2 | `[DONE]` focused ESLint, typecheck, build, focused teacher/admin submissions E2E, boundary snapshot 3→1 |
| Frontend lecture sections API wrapper | `[COMPLETED]` Admin lecture sections API path is now a compatibility facade over the shared lecture sections contract; teacher clinic imports shared directly | Admin lecture section management, admin clinic section filters/create, admin session clinic tab/block, teacher clinic create sheet | `src/shared/api/contracts/lectureSections` | Last role-app import of admin lecture section internals removed | P2 | `[DONE]` focused ESLint, typecheck, focused teacher/admin clinic E2E, boundary snapshot 1→0 |
| Frontend enrollment API wrappers | `[COMPLETED]` admin lectures, exams, homework, clinic tab, and teacher score entry previously depended on role-local wrappers for `/enrollments/session-enrollments/` | Lecture score roster, exam creation/enrollment panel, homework assignment/enrollment panel, session clinic tab, teacher mobile score entry | `frontend/src/shared/api/contracts/sessionEnrollments.ts` with admin compatibility facades | Lectures/exams/homework session enrollment API files remain compatibility facades; active call sites now use shared contract where cross-domain import was unnecessary | P2 | `[DONE]` focused ESLint, frontend typecheck, boundary snapshot 43→41 |
| Legacy/wrong E2E route | `[COMPLETED] e2e/admin/dnb-lectures-sessions.spec.ts` no longer calls `/api/v1/students/students/` | DNB lecture/session E2E setup/cleanup | Current `/api/v1/students/` or shared E2E data helper | Wrong route removed from this helper; CI guard blocks reintroduction of `students/students`, `lectures/enrollments`, and direct enrollment-create routes in tracked source/E2E files | P2 | `[DONE]` `pnpm guard:legacy-api` |
| Student field naming | Backend `is_managed`, `uses_identifier`, `no_phone`, `omr_code`; frontend `active`, `noPhone`, synthetic phone, `studentPhone` | Admin/teacher create/edit; student/profile | Generated contract plus explicit UI form mapper | Implicit field semantics in each API client | P1 | Contract snapshot and mapper unit tests |

## Canonicalization Order

1. Introduce selectors for reads and replace non-students direct reads only when
   tests cover the caller.
2. Introduce services for create/update/lifecycle and route existing views
   through them without changing URLs.
3. Add deprecation logging for legacy paths before removal.
4. Converge frontend role-app APIs onto a shared contract after backend schema
   snapshots exist.
5. Remove legacy helpers only after route usage detection proves no caller
   remains.

## Required Explicit Findings

- Student creation/restoration is actually 6+ roots.
- Student profile update is actually 3 primary roots plus schedule visibility
  mutations.
- Password/account recovery is actually 4 API roots and must not be mixed with
  the current release while Phase 1 student canonicalization starts.
- The E2E lecture/session path used `/api/v1/students/students/`, not the current
  `/api/v1/students/`; the known tracked caller has been migrated.
- `noPhone/uses_identifier/omr_code`, `active/is_managed`, and parent initial
  password have semantic drift across backend and frontend.

## Implemented Convergence Notes

- 2026-05-23: Clinic participant transition writes converged on
  `apps.domains.clinic.services.lifecycle`. `set_status`, `complete`, and
  `uncomplete` now share service-level transition rules and row locking, with
  regression coverage for PostgreSQL's nullable outer-join lock behavior.
  Production QA verified no-show, completion, invalid terminal transition,
  uncomplete, successful AlimTalk logs, and zero disposable-data residue.
- 2026-05-23: Clinic participant creation and student booking-change writes
  also converged on `apps.domains.clinic.services.lifecycle`.
  `ParticipantViewSet.create` and `change_booking` are now transport facades;
  the service owns tenant/student/enrollment validation, row-locked capacity
  checks, duplicate checks, status defaults, and notification event context.
- 2026-05-23: Student Excel/import row orchestration converged on
  `apps.domains.students.services.import_students_from_rows` and
  `resolve_student_import_row`. Student-only Excel worker and lecture-enroll
  Excel now share tenant-scoped name+parent duplicate detection, deleted-student
  restore, school-level validation, phone/ps_number/omr derivation, and
  `create_student_account` graph creation without the worker depending on the
  `lecture_enroll` helper. `bulk_from_excel.py` and `lecture_enroll.py` remain
  compatibility facades. Teacher mobile Excel upload now opens a bottom sheet
  to confirm initial password and welcome AlimTalk flag instead of silently
  uploading with hardcoded `0000`.
- 2026-09-10: Parent account ownership was completed. Phone-derived and blank
  password fallbacks, the compatibility ensure facade, bulk account/password
  commands, and first-child selection fallback were removed. Staff create/edit
  now supplies an explicit password or verified signup hash, lookup/recovery/
  restore paths never create accounts, and public login identifiers cannot
  collide silently across student and parent accounts.
- 2026-08-23: Lecture/session Excel enrollment was split from the student
  creation/import resolver. It now matches only active students in the current
  tenant by exact student ID first, or by exact name and normalized parent
  phone when the ID is blank. Ambiguous fallback identities fail closed. It
  never creates or restores a student and never reads or mutates
  initial-password/account fields. Student registration Excel retains the
  explicit initial-password options.
- 2026-05-23: Production Excel worker QA completed. The QA pass also removed a
  DRF action route converter bug in `students/excel_job_status/<job_id>/` and
  a parser heuristic that dropped valid rows when a name exceeded 20
  characters despite a valid phone.
- 2026-05-23: JSON `bulk_create` and `bulk_resolve_conflicts` now route through
  the student import service. Active duplicate detection, delete-and-recreate,
  parent password notice mapping, and rollback-on-create-failure are covered by
  local tests and production API QA.
- 2026-05-23: Attendance roster creation now routes through
  `apps.domains.attendance.services.create_attendance_roster`. The shared
  `ensure_session_roster_membership` unit is also used by session-enrollment
  bulk create, so enrollment reactivation, fee auto-assignment, session roster
  creation, and attendance idempotency have one write boundary. Production API
  QA verified repeat registration, malformed ID 400s, matrix reflection, and
  zero disposable-data residue.
- 2026-05-22: Phase 2 enrollment matrix and assignment roster writes/read
  scope now converge through `apps.domains.enrollment.selectors` and
  `apps.domains.enrollment.services.lifecycle`.
- 2026-05-22: Phase 3 automatic notification queue payloads carry
  source/use-case/domain object/actor metadata for operational tracing.
- 2026-05-22: Phase 4 AI job gateway rejects missing tenant/source and
  payload tenant mismatch before SQS publish.
- 2026-05-22: Phase 5 first cleanup removed the known
  `/api/v1/students/students/` E2E helper route.
- 2026-05-22: Phase 5 frontend enrollment API cleanup created a canonical
  `app_admin/domains/enrollment/api/enrollments.ts` client. Lectures, exams,
  and homework session-enrollment wrappers now delegate to it instead of each
  normalizing the same endpoint independently.
- 2026-05-22: Session-enrollment fetch/bulk contract moved to
  `src/shared/api/contracts/sessionEnrollments.ts`. Admin enrollment remains a
  compatibility facade, lecture scores/exam creation/session clinic/teacher
  mobile score entry call the shared contract directly, and the previous exams
  `404/501 -> []` compatibility is preserved in the shared contract.
- 2026-05-22: Frontend CI runs `pnpm guard:legacy-api` to fail on reintroduced
  legacy `/students/students/`, `/lectures/enrollments/`, or direct
  `/enrollments/` create calls in tracked `src`/`e2e` TypeScript/JavaScript.
- 2026-05-22: Frontend shared purity slice moved theme, responsive view,
  clinic-target, video status/workbox, and session-progress contracts under
  `src/shared/*`. Admin role paths remain compatibility facades where needed;
  `shared -> app_*` imports became 0 and cross-app/admin role imports became 37.
- 2026-05-22: Operational notification counts moved to shared notification
  contracts/hooks. Admin notification modules are compatibility facades, teacher
  surfaces use `useTeacherPendingCounts`, and cross-app/admin role imports are
  now 30.
- 2026-05-22: Community post/reply/attachment contracts moved to shared
  community contracts. Student notices/community and teacher developer feedback
  no longer import admin community internals, patch notes are shared product
  data, and cross-app/admin role imports are now 24.
- 2026-05-22: Video access-mode/rule contracts moved to shared video contracts,
  reusable thumbnail rendering moved to `src/shared/media/video`, student video
  surfaces no longer import admin video internals for those contracts, and
  cross-app/admin role imports are now 21.
- 2026-05-22: Lecture/session attendance API moved to shared attendance
  contracts. Admin attendance API remains a compatibility facade, teacher
  attendance and lecture matrix surfaces use the shared contract directly,
  duplicate teacher matrix/export calls were removed, and cross-app/admin role
  imports are now 17.
- 2026-05-22: Storage/inventory API, student API contracts, and student Excel
  utilities moved to shared contract/product modules. Admin paths remain
  compatibility facades, teacher/student storage and student surfaces use shared
  contracts directly, and cross-app/admin role imports are now 9.
- 2026-05-22: Fees API and status/tone contracts moved to shared
  contract/product modules. Admin paths remain compatibility facades, teacher
  fees surfaces use shared contracts directly, local E2E uses a route-level
  feature-flag override only, and cross-app/admin role imports are now 7.
- 2026-05-22: Tools timer download API moved to the shared tools contract.
  Admin stopwatch timer API remains a compatibility facade, teacher timer uses
  the shared contract directly, and cross-app/admin role imports are now 6.
- 2026-05-22: Exam enrollment API moved to the shared exam enrollment contract.
  Admin exam enrollment API remains a compatibility facade, teacher exam detail
  and OMR pages use the shared contract directly, and cross-app/admin role
  imports are now 4.
- 2026-05-22: Tenant information API moved to the shared tenant info contract.
  Admin profile API re-exports the contract, teacher organization settings uses
  shared directly, and cross-app/admin role imports were 3 after that slice.
- 2026-05-22: Submission types and submission inbox/action APIs moved to the
  shared submissions contract. Admin submissions/materials API paths and the
  teacher submissions API path remain compatibility facades, student submit and
  teacher submissions inbox use shared directly, and cross-app/admin role
  imports were 1 after that slice.
- 2026-05-22: Lecture section and section-assignment APIs moved to the shared
  lecture sections contract. Admin lectures sections API remains a compatibility
  facade, teacher clinic imports shared directly, and cross-app/admin role
  imports are now 0.
- 2026-05-23: Parent welcome-password drift cleanup also aligned account
  Alimtalk queue metadata. Direct student welcome and registration approval
  paths now enqueue `registration_approved_student|parent` event metadata so
  `NotificationLog` masking and operations tracing use the same SYSTEM_AUTO
  trigger SSOT.
- 2026-05-23: Student restore lifecycle converged on
  `apps.domains.students.services.restore_student`. `bulk_restore`,
  `bulk_resolve_conflicts`, and lecture/enrollment deleted-student reuse now
  share ps_number unmangling, collision rejection, user reactivation,
  membership restoration, and parent relinking behavior without reactivating
  previous enrollments.
- 2026-05-23: Student permanent delete entrypoints converged on
  `apps.domains.students.services.permanently_delete_students`.
  `bulk_permanent_delete`, `bulk_resolve_conflicts` delete, deleted-duplicate
  cleanup, and 30-day purge now share tenant-scoped locking, fee/section FK
  cleanup, user retention checks, pending reset cleanup, and orphan account
  deletion. The remaining structural debt is replacing the service's guarded
  raw SQL graph with domain-owned cleanup hooks/events.
- 2026-05-23: Account recovery temporary password usability converged on
  `apps.core.services.password.TEMP_PASSWORD_LENGTH = 6`. Public login recovery
  still uses `/auth/account-recovery/dispatch/` and pending reset semantics,
  `NotificationLog` keeps credential bodies redacted, and production QA verified
  a successful real AlimTalk send plus cleanup. Device-visible body validation is
  pending user paste because logs intentionally do not store the secret body.
- 2026-05-23: Student creation account graph converged on
  `apps.domains.students.services.create_student_account`. Direct create, JSON
  bulk create, conflict delete-and-recreate, registration approval, lecture
  enrollment import, and student Excel worker now share Parent ensure, User
  creation/password assignment, Student creation, and active student
  `TenantMembership` creation. Validation, duplicate/deleted-student policy,
  response shape, and message dispatch intentionally remain at each caller until
  their orchestration contracts are snapshotted. Frontend teacher create now
  calls the shared student contract. The dead `send_welcome_message` API and
  worker option was removed; every new account stages one credential notice and
  the first ACTIVE enrollment owns its dispatch timing.
- 2026-05-23: Registration approval orchestration converged on
  `apps.domains.students.services.approve_registration_request`. Approve,
  bulk_approve, and auto-approve now share the same row lock, pending-state
  guard, signup password hash transfer, `pending -> approved` transition, and
  student account graph call. Approval notification dispatch remains outside
  the durable transaction and failures are logged without hiding the committed
  approval from the API caller.
- 2026-05-23 (superseded by 2026-09-10): Parent account welcome-password drift reduced. Parent creation
  paths now use `ensure_parent_account_for_student()` result metadata, so new
  parent accounts announced the then-current phone-derived password while existing
  parent accounts announce `변경되지 않음`. Student conflict restore no longer
  sends a new-password welcome message because restore does not reset passwords.
- 2026-05-22: Clinic participant/session/idcard active-student reads moved to
  `students.selectors` and `enrollment.selectors` for touched HTTP paths.
  `student_app.permissions.get_request_student` now returns only active
  tenant-scoped students, and tests cover deleted-student rejection through
  `student`, `enrollment_id`, student self-booking, and idcard fallback.
- 2026-05-22: Attendance roster create student validation moved from direct
  `Student.objects` lookup to `students.selectors`, and `AttendanceSerializer`
  now scopes session/enrollment FK querysets by request tenant. Tests cover
  cross-tenant, mixed-tenant, same-tenant deleted student rejection, and FK
  queryset scoping.
- 2026-05-22: Attendance production QA passed after deployment:
  `e2e/teacher/attendance-contract.spec.ts`, the deep operational smoke
  "강의 출결 매트릭스" path, API/console error capture, and mobile screenshots
  for teacher attendance check and matrix render.
- 2026-05-22: Messaging manual send and manual notification preview recipient
  reads moved to `messaging.services.recipients`, backed by
  `students.selectors.students_for_tenant(deleted="active")`. Tests cover
  student vs parent target phone, duplicate ID dedupe, same-tenant deleted
  student omission, cross-tenant omission, public preview redaction, and token
  payload raw phone preservation for sendable recipients.
- 2026-05-22: Messaging production QA passed after deployment without external
  send: production manual notification preview resolved a no-parent-phone
  active student once despite duplicate/missing IDs, returned no preview token,
  excluded the recipient for "전화번호 없음", and exposed no raw phone or
  replacement payload fields. The admin message page production smoke and
  mobile screenshot also passed with no console/API errors.
- 2026-05-22: Results admin student grades now parses `student_id` safely and
  uses `students.selectors.active_student_by_id` before enrollment/result reads.
  Tests cover malformed IDs, active same-tenant empty payload, cross-tenant
  rejection, same-tenant soft-deleted rejection, and the existing achievement
  contract.
- 2026-05-22: Results production QA passed after backend deployment
  (`8b7f0f24`, CI run `26296381780`, CI build note `83cf2554`) and frontend
  mobile follow-up deployment (`2d340855`, CI run `26299809665`). Production API
  checks covered active student `200`, malformed `student_id` `400`, and missing
  student `404`. Production UI checks covered the admin student detail overlay
  and grade tab smoke (`2 passed`) plus mobile visual capture at
  `C:\academy\_artifacts\results-prod-qa-20260522\admin-student-detail-grades-mobile-content-first-prod.png`.
  During QA, the mobile overlay asset/layout defect was fixed in frontend
  commits `756e6e3c`, `c4546fcf`, and `2d340855`; Cloudflare Pages upload
  resilience was fixed in `cf6f873b` and `8c428447`.
- 2026-05-22: Student soft delete side effects moved to
  `apps.domains.students.services.soft_delete_student`. The canonical service
  marks the student deleted, preserves unique `ps_number` semantics, unlinks
  parent, deactivates the login user and tenant membership, and delegates
  enrollment/clinic side effects to domain lifecycle hooks. `destroy` and
  `bulk_delete` are now route-level facades over that service. Local validation
  covered the full student test file (`24 passed`), stabilization/permanent
  delete/profile suites (`30 passed`), all student test modules (`85 passed`),
  `manage.py check`, and migration dry-run.
