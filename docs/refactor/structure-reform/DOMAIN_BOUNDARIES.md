# Domain Boundaries

**Status:** [VERIFIED] current ownership and dependency audit with [PROPOSED] interfaces  
**Captured:** 2026-05-22  
**Goal:** make feature entrypoints converge by domain responsibility, not by folder cosmetics.

Boundary rule for the structure reform:

- Models remain where migrations and Django app labels currently require them.
- Public read access goes through selectors.
- Public writes go through services/use-cases.
- Cross-domain side effects go through events/tasks or explicit domain hooks.
- Tenant must be passed explicitly for every tenant-scoped read/write. No
  fallback tenant is introduced.

## Students

| Item | Current state |
|---|---|
| Owned data | `Student`, `Tag`, `StudentRegistrationRequest`; identity/profile/lifecycle fields; student schedule visibility fields |
| External references | Enrollment, attendance, clinic, exams/results/submissions, homework, messaging, fees, video, community, inventory, auth/core |
| Public interface candidate | `students.selectors` for tenant-scoped reads; `students.services` for create/update/lifecycle/profile/import/registration approval |
| Forbidden dependency | Other domains importing `Student` internals for write logic; model save touching inventory; frontend role apps importing admin student internals instead of shared contracts |
| Tenant rule | Every student lookup must include tenant and deleted-state intent; no global user/student lookup unless explicitly platform-admin scoped |
| Current risk | Profile writes plus soft-delete/restore/permanent-delete entrypoints now have service facades; remaining risks are create/import/registration roots, schedule visibility, read DTO drift, model save inventory side effect, no-tenant repository helpers, and the permanent-delete service's guarded raw SQL graph |

## Parents

| Item | Current state |
|---|---|
| Owned data | `Parent`, parent `User`, parent membership linkage |
| External references | Students, core auth, account recovery, messaging |
| Public interface candidate | `parents.services.ensure_parent_account_for_student` for explicit creation, `find_parent_account` for lookup, `parents.selectors.get_parent_children(tenant, parent)` for child reads |
| Forbidden dependency | Student views deciding parent password/message semantics independently |
| Tenant rule | Parent phone lookup must include tenant |
| Current risk | Existing malformed or login-identifier-colliding legacy rows require exact staff repair; runtime creation and relink paths fail closed |

## Lectures / Classes / Sessions

| Item | Current state |
|---|---|
| Owned data | `Lecture`, `Section`, `SectionAssignment`, `Session` |
| External references | Enrollment, attendance, exams, homework, clinic, video, community |
| Public interface candidate | `lectures.selectors.get_session_for_tenant`, `lectures.services.manage_session` |
| Forbidden dependency | Other domains mutating lecture/session state without lecture service |
| Tenant rule | Lecture/session access must be tenant-scoped through lecture or session tenant path |
| Current risk | `/lectures/` prefix includes both lecture and attendance routes; session state participates in many domains |

## Enrollment

| Item | Current state |
|---|---|
| Owned data | `Enrollment`, `SessionEnrollment` |
| External references | Attendance, exams, homework, results, submissions, fees, clinic, video |
| Public interface candidate | `[COMPLETED] enrollment.selectors.enrollments_for_tenant`, `session_enrollments_for_tenant`, `active_session_enrollments_for_session`; `[COMPLETED] enrollment.services.lifecycle` for bulk enrollment/session create, status side effects, delete, and learning access toggle |
| Forbidden dependency | Students domain view directly owning enrollment-matrix toggles without enrollment service |
| Tenant rule | Enrollment and session enrollment queries must include tenant |
| Current risk | Student soft delete now delegates enrollment deactivation through the enrollment lifecycle service; enrollment matrix, assignment roster, and touched clinic enrollment reads are canonicalized. Remaining risk is event/hook extraction for permanent-delete cleanup |

## Attendance

| Item | Current state |
|---|---|
| Owned data | `Attendance` |
| External references | Enrollment/session, students for roster validation, messaging for notifications |
| Public interface | `attendance.services.create_attendance_roster`, `attendance.services.ensure_session_roster_membership`; future status event candidate: `attendance.events.AttendanceChanged` |
| Forbidden dependency | View-owned roster side effects beyond selector DTOs and attendance/enrollment services |
| Tenant rule | Attendance is tenant-scoped and should validate enrollment/session belong to same tenant |
| Current risk | Roster creation side effects are now service-owned and shared by attendance bulk create plus session-enrollment bulk create. Remaining risk is status-change/event extraction: notification triggers still live beside attendance HTTP mutation code. |

## Clinic

| Item | Current state |
|---|---|
| Owned data | Clinic `Session`, `SessionParticipant`, `Test`, clinic `Submission` |
| External references | Student, enrollment, lectures, messaging |
| Public interface candidate | `[COMPLETED] clinic.services.create_participant`, `change_participant_booking`, `change_participant_status`, `complete_participant`, `uncomplete_participant`; `[NEXT] clinic.events` / outbox boundary |
| Forbidden dependency | View-owned participant business decisions and students delete flow directly mutating clinic participants |
| Tenant rule | Participant/session/submission/test queries must include tenant and same-tenant active student/enrollment |
| Current risk | Clinic has a `Submission` model name that conflicts conceptually with assessment submissions. Participant create/change/status/complete/uncomplete writes are service-owned and row-locked where needed. Notification dispatch still uses the current `transaction.on_commit` compatibility boundary, so the next structural risk is event/outbox durability rather than view-owned participant business logic. |

## Exams

| Item | Current state |
|---|---|
| Owned data | `Exam`, `ExamEnrollment`, sheets, questions, answer keys, template bundles, exam assets |
| External references | Lectures/sessions, enrollment, submissions, results, AI/OMR |
| Public interface candidate | `exams.selectors.get_exam_contract`, `exams.services.manage_exam_enrollment` |
| Forbidden dependency | Results/submissions mutating exam-owned enrollment or asset state directly |
| Tenant rule | Exam access must include direct `tenant` or verified session/lecture tenant path |
| Current risk | Exam creation/update/recalculation and OMR-related flows are spread across many API views |

## Homework

| Item | Current state |
|---|---|
| Owned data | Homework policy, homework enrollment, homework assignment |
| External references | Lectures/sessions, enrollment, submissions, homework_results/results |
| Public interface candidate | `homework.services.assign_homework`, `homework.selectors.get_assignments` |
| Forbidden dependency | Importing homework_results viewset through `homework.views.__init__` as if same owner |
| Tenant rule | Homework policy/assignment/enrollment must include tenant and validated enrollment |
| Current risk | `homework` and `homeworks` prefixes split ownership; score viewset belongs to `homework_results` but is exported by `homework` |

## Submissions

| Item | Current state |
|---|---|
| Owned data | Assessment `Submission`, `SubmissionAnswer` |
| External references | Exams, homework, enrollment, results, AI/OMR |
| Public interface candidate | `submissions.services.submit`, `submissions.services.discard`, `submissions.events.SubmissionChanged` |
| Forbidden dependency | Results/exams directly editing submission internals outside service hooks |
| Tenant rule | Submission and answers must include tenant; target exam/homework must resolve under same tenant |
| Current risk | Multiple OMR/admin/manual edit paths; result recomputation side effects need explicit events |

## Results

| Item | Current state |
|---|---|
| Owned data | `Result`, `ExamResult`, `ExamAttempt`, `ResultItem`, `ResultFact`, score edit draft, wrong-note PDFs |
| External references | Submissions, exams, enrollment/students, lectures/sessions |
| Public interface candidate | `results.selectors.get_student_grades`, `results.services.recalculate_attempt`, `results.events.ResultPublished` |
| Forbidden dependency | Student/teacher/admin views calculating grade state independently |
| Tenant rule | Results are tenant-scoped through enrollment/exam/session and must prove same tenant |
| Current risk | Same grade/status values are computed across result views, serializers, and frontend state |

## Staff / Payroll

| Item | Current state |
|---|---|
| Owned data | `Staff`, `WorkType`, `StaffWorkType`, `WorkRecord`, work-month locks, payroll snapshots |
| Public read interface | `staffs.selectors.current_work_record_for_staff`, `work_records_for_staff_range`, `open_work_records_for_tenant`, `work_current_status` |
| Public write interface | `staffs.services.start_work_record`, `start_work_break`, `end_work_break`, `end_work_record` |
| Forbidden dependency | HTTP views implementing work-session state transitions, check-then-create clock-in, break accumulation, or payroll close calculations outside the service and DB constraint |
| Tenant rule | Staff, work type, and work record must belong to the same request tenant |
| Invariant | A staff member has at most one open (`end_time IS NULL`) work record; conflicts return HTTP 409 |

## Messaging

| Item | Current state |
|---|---|
| Owned data | Notification logs, preview tokens, templates, auto-send config, scheduled notifications |
| External references | Students, parents, staff, attendance, clinic, exams/results, community |
| Public interface candidate | `messaging.services.enqueue_notification`, `messaging.services.resolve_recipients`, event handlers |
| Forbidden dependency | Domain views sending directly in the middle of transaction without event/outbox contract |
| Tenant rule | Every message/template/log must include tenant; recipients resolved from tenant-scoped selector |
| Current risk | Manual send and manual preview recipient reads are selector-backed; direct sends and rollback logic are still repeated in student/password flows, and automatic notification queue payloads include source/use-case/domain object/actor metadata, but durable outbox is still future work |

## AI / Matchup / OMR

| Item | Current state |
|---|---|
| Owned data | AI jobs/results/config/usage; matchup documents/problems/hit reports/page state/layout fingerprints |
| External references | Inventory/files, exams/submissions, landing, tenants |
| Public interface candidate | `ai.services.enqueue_job`, `matchup.services.create_document_job`, `omr.services.submit_batch` |
| Forbidden dependency | Domain views reaching worker/infra gateways directly without job contract |
| Tenant rule | Jobs and documents must include tenant or explicit system/platform scope; no tenant fallback for worker payloads |
| Current risk | `dispatch_job` now rejects missing tenant/source and payload tenant mismatch; student Excel import still dispatches AI jobs from student view, and matchup has many tenant query paths plus public/iframe tenant resolution exceptions |

## Billing / Fees

| Item | Current state |
|---|---|
| Owned data | Billing surfaces plus fees `FeeTemplate`, `StudentFee`, `StudentInvoice`, `InvoiceItem`, `FeePayment` |
| External references | Students, enrollment, student app fees |
| Public interface candidate | `fees.services.assign_fee`, `fees.selectors.get_student_invoices` |
| Forbidden dependency | Student lifecycle deleting fee data directly without fees hook |
| Tenant rule | Fee/invoice/payment queries must include tenant and same-tenant student/enrollment |
| Current risk | Fee direct-FK cleanup is covered by `students.services.permanently_delete_students`, but the target boundary is still a fees-owned cleanup hook/event rather than student-owned SQL |

## Community

| Item | Current state |
|---|---|
| Owned data | Scope nodes, posts, mappings, replies, templates, likes, reports, notifications, attachments, blocks |
| External references | Students/users, lectures/sessions, messaging |
| Public interface candidate | `community.services.create_post`, `community.services.anonymize_or_unlink_author`, `community.events.ReplyCreated` |
| Forbidden dependency | Students permanent delete updating community internals from raw SQL long term; current compatibility service must keep explicit tenant predicates |
| Tenant rule | Community content and moderation state must include tenant; author lookup must not cross tenant |
| Current risk | Permanent delete currently nulls same-tenant student authors through the guarded lifecycle service; community-owned anonymize/unlink hook remains pending |

## Landing

| Item | Current state |
|---|---|
| Owned data | Public landing, consult requests, testimonials, tenant/program branding surfaces |
| External references | Tenant/core, community/matchup public preview in some flows |
| Public interface candidate | `landing.selectors.get_public_landing`, `landing.services.submit_consult` |
| Forbidden dependency | Product domains depending on landing internals for tenant resolution or public state |
| Tenant rule | Public forms still resolve a concrete tenant; no fallback tenant |
| Current risk | Public unauthenticated flows require strict tenant resolver and rate-limit contracts |

## Dependency Policy For Phase 1

- Allowed from other domains to students: public selector DTOs and service
  commands only.
- Forbidden from other domains to students: direct mutation of `Student`, direct
  reliance on `Student.save()` side effects, direct deleted-state filtering.
- Allowed from students to other domains: explicit domain hooks/events for
  enrollment deactivation, clinic participant cancellation, messaging
  notification, inventory identity rename.
- Forbidden from students to other domains: raw table surgery in views and model
  save imports.
- Allowed in frontend: role apps may use shared generated contracts or the
  current hand-authored shared contracts while schema generation is introduced.
- Forbidden in frontend: `app_teacher`, `app_student`, `shared`, or new role
  surfaces importing `@admin/domains/students/*` internals. Existing auth/admin
  compatibility facade callers need dedicated cleanup before deletion.
