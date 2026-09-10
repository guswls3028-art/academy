# Teacher operations assistant (beta)

The teacher mobile app accepts one to five images plus a current natural-language request. Images are data, never system instructions. OCR runs in the API container with local Tesseract (`kor+eng`); original bytes and OCR text are not persisted or audited.

## Safety contract

- Tenant is fixed by the authenticated staff session and every lookup is tenant-scoped.
- Existing students are resolved before creation with exact name plus normalized student or parent phone; school is conflict evidence. Name-only, phone conflict, or multiple candidates blocks. A missing phone is repaired through `update_student_profile(..., identity_field="ps_number")`, without a duplicate account or credential reset.
- A new student account or genuinely missing Parent/User requires a staff-entered initial password of at least four characters in the review receipt. The confirm API never derives a password from either phone number. Existing account passwords are never overwritten by that field.
- A new student may omit the student phone. The canonical account service then issues a dedicated login ID, and the confirm receipt returns that exact saved ID for staff readback.
- Lecture candidates are limited to the active date range. An exact regular session is required.
- Video-class requests ensure Enrollment, SessionEnrollment, and exact `Attendance=ONLINE`. Completion requires the access resolver to return `PROCTORED_CLASS`; roster rows alone are not success and no `FREE_REVIEW` override is created.
- Wrong-enrollment correction is explicit and removes only a locked enrollment whose attendances are pristine `UNSET` rows and which has no learning, payment, or user-authored dependency.
- Analyze returns a signed 30-minute proposal. Confirm re-locks exact targets, rejects drift before mutation, and uses the proposal nonce as the idempotency receipt.
- Account creation/linking, enrollment, attendance, notice enqueue, provider receipt, and real playback verification remain separate result states.
- A Parent/User repaired for an existing student receives only the parent credential notice in the same atomic confirm operation; delivery failure rolls back the repair. A newly created student still stages the normal student/parent notices until the first active enrollment.
- Initial notices use only approved `registration_approved_student` and `registration_approved_parent` Alimtalk templates. Missing templates block; there is no SMS/LMS fallback. Provider acceptance requires successful `NotificationLog` rows in `alimtalk` mode with provider IDs and no failure, and does not prove Kakao read.

## Audit and verification

`OpsAuditLog` stores only execution/scoped database IDs, action flags, row counts, and image hash prefixes. Never store raw phone numbers, passwords, JWTs, playback tokens, signed URLs, provider payloads, OCR text, or image bytes.

Focused tests live in `apps/domains/teacher_app/tests/test_ops_assistant.py`. Release verification uses a disposable QA tenant for login -> video home -> exact lecture/session -> playback -> CDN GET, ends only the canary proctored session, and confirms zero residue.
