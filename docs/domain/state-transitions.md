# State Transition SSOT (Single Source of Truth)

**Version:** V1.1.2
**Created:** 2026-03-19
**Status:** ACTIVE

이 문서는 Academy SaaS 프로젝트의 모든 핵심 도메인 상태 전이를 정의하는 실행 가능한 기준선이다.
코드와 이 문서가 불일치하면 **코드를 수정**한다.

---

## Table of Contents

- [A. 도메인 목록 및 우선순위](#a-도메인-목록-및-우선순위)
- [B. 도메인별 상태전이 SSOT](#b-도메인별-상태전이-ssot)
  - [B1. Submission (시험/숙제 제출)](#b1-submission)
  - [B2. Video (영상 인코딩)](#b2-video)
  - [B3. VideoTranscodeJob (인코딩 작업)](#b3-videotranscodejob)
  - [B4. ExamAttempt (시험 응시)](#b4-examattempt)
  - [B5. ExamResult (시험 결과)](#b5-examresult)
  - [B6. Clinic SessionParticipant (클리닉 예약)](#b6-clinic-sessionparticipant)
  - [B7. Exam (시험 라이프사이클)](#b7-exam)
  - [B8. Homework (숙제 라이프사이클)](#b8-homework)
  - [B9. Enrollment (수강 등록)](#b9-enrollment)
  - [B10. StudentRegistrationRequest (학생 등록 신청)](#b10-studentregistrationrequest)
  - [B11. AIJobModel (AI 작업)](#b11-aijobmodel)
  - [B12. Invoice (청구서)](#b12-invoice)
  - [B13. PaymentTransaction (결제)](#b13-paymenttransaction)
  - [B14. Program.subscription_status (구독)](#b14-subscription-status)
  - [B15. VideoPlaybackSession (재생 세션)](#b15-videoplaybacksession)
  - [B16. PostEntity (커뮤니티 게시글)](#b16-postentity)
  - [B17. WrongNotePDF (오답노트 PDF)](#b17-wrongnotepdf)
  - [B18. NotificationLog (알림 발송)](#b18-notificationlog)
  - [B19. Attendance (출석)](#b19-attendance)
- [C. 현재 구현 불일치 목록](#c-현재-구현-불일치-목록)
- [D. 우선 수정 대상](#d-우선-수정-대상)
- [E. 불변조건 (Cross-Domain)](#e-불변조건-cross-domain)
- [F. 미검증 리스크](#f-미검증-리스크)

---

## A. 도메인 목록 및 우선순위

| 우선순위 | 도메인 | 상태 수 | 위험도 | 근거 |
|---------|--------|---------|--------|------|
| P0 | Submission | 9 | FIXED | `transition.py` SSOT 적용, EXTRACTING 포함, stuck recovery/cascade도 전이 가드 통과 |
| P0 | ExamResult | 2 | FIXED | `select_for_update` + FINAL early return으로 DRAFT 회귀 차단 |
| P0 | StudentRegistrationRequest | 3 | FIXED | approve/reject 레이스는 `select_for_update`로 보강됨 |
| P1 | Video | 5 | HIGH | 관리 명령어 SFU 미사용 |
| P1 | VideoTranscodeJob | 7 | FIXED | cancel/mark_dead/fail_retry 종단 상태 가드 적용 |
| P1 | ExamAttempt | 4 | FIXED | sync_result attempt 조회에 `select_for_update` 적용 |
| P1 | AIJobModel | 9 | FIXED | job_save_failed atomic + `select_for_update` + terminal guard 적용 |
| P2 | Clinic SessionParticipant | 6 | FIXED | change_booking이 lifecycle service의 lock + allowed-from 가드 사용 |
| P2 | Enrollment | 3 | LOW | 단순 3상태, 제한된 변이 |
| P2 | Invoice | 6 | N/A | 미구현 (모델만 존재) |
| P2 | PaymentTransaction | 5 | N/A | 미구현 |
| P3 | Exam | 3 | LOW | DRAFT 레거시, 실질 OPEN/CLOSED |
| P3 | Homework | 3 | LOW | Exam과 동일 패턴 |
| P3 | Program.subscription_status | 4 | LOW | 런타임 변이 코드 없음 |
| P3 | VideoPlaybackSession | 4 | LOW | 잘 구현됨 |
| P3 | PostEntity | 3 | LOW | 단순 발행 흐름 |
| P3 | WrongNotePDF | 4 | LOW | 비동기 생성 작업 |
| P3 | NotificationLog | 3 | LOW | SQS 워커 관리 |
| P3 | Attendance | 10 | LOW | 분류 값, 상태기계 아님 |

---

## B. 도메인별 상태전이 SSOT

---

### B1. Submission

**모델:** `apps/domains/submissions/models/submission.py`
**상태 필드:** `status` (CharField, indexed)

#### 상태 목록

| 상태 | 의미 | 진입 조건 | 이탈 조건 |
|------|------|----------|----------|
| `SUBMITTED` | 제출됨, 처리 대기 | 사용자 제출 / FAILED 재시도 | 디스패치 시작 |
| `DISPATCHED` | 처리 파이프라인에 전달됨 | dispatcher가 선점 | AI/OMR 처리 시작 |
| `EXTRACTING` | 답안 추출 중 | AI/OMR 이미지 분석 시작 | 추출 완료 또는 실패 |
| `ANSWERS_READY` | 답안 준비 완료, 채점 대기 | 추출 완료 / 수동 입력 / 식별 완료 | 채점 시작 |
| `GRADING` | 채점 진행 중 | 채점 서비스 시작 | 채점 완료 또는 실패 |
| `DONE` | 처리 완료 | 채점 성공 | 종단 상태 |
| `FAILED` | 처리 실패 | 파이프라인 어디서든 오류 | 재시도(→SUBMITTED) |
| `NEEDS_IDENTIFICATION` | 학생 식별 필요 | AI/OMR이 학생 매칭 실패 | 수동 매칭 완료(→ANSWERS_READY) |
| `SUPERSEDED` | 재응시로 대체됨 | 새 제출이 기존 제출을 대체 | 종단 상태 |

사진·동영상 숙제 제출은 이 자동 처리 상태기의 사용자 경로에서 제외한다. 업로드 뒤
`SUBMITTED`와 파일을 보존하고 AI dispatch·공용 제출함에는 넣지 않으며, 과제 상세에서
교사가 직접 확인한다. 교사 확인은 `AssessmentCorrection`이 소유하고 점수 입력은
`HomeworkScore`가 계속 소유하므로, 숙제 파일 확인만을 위해 `DONE`으로 위장 전이하지
않는다. 기존 제출·점수·파일은 이 경계 변경으로 삭제하거나 재작성하지 않는다.

#### 허용 전이 (STATUS_FLOW) — 검증 엔진: `transition.py`

런타임 호출부는 `transition.py`를 직접 호출하지 않는다. 상태 변경 public API는
`apps/domains/submissions/services/lifecycle.py`이며, 이 facade가
`mark_answers_ready`, `fail_submission`, `reopen_for_regrade`,
`supersede_done_submissions` 같은 의미 있는 함수로 STATUS_FLOW를 통과한다.
`scripts/lint/check_submission_lifecycle_boundary.py`가 deploy lint gate에서
런타임 코드의 `transition.py` 직접 import 회귀를 차단한다.

```
SUBMITTED      → {DISPATCHED, ANSWERS_READY, GRADING, FAILED}
DISPATCHED     → {EXTRACTING, ANSWERS_READY, NEEDS_IDENTIFICATION, FAILED}
EXTRACTING     → {ANSWERS_READY, NEEDS_IDENTIFICATION, FAILED}
ANSWERS_READY  → {GRADING}
GRADING        → {DONE, FAILED}
FAILED         → {SUBMITTED}
NEEDS_IDENTIFICATION → {ANSWERS_READY, FAILED}
DONE           → {SUPERSEDED}
SUPERSEDED     → {} (종단)
```

**EXTRACTING**: 워커가 본격 추출 중임을 나타내는 진행 상태다. 현재 생성
경로가 이 상태를 항상 사용하지는 않지만, pending inbox / stuck recovery /
cascade discard가 실제 운영 row를 다루므로 STATUS_FLOW에 포함한다.

**Admin Override (manual_edit 전용):**
```
DONE/FAILED/SUBMITTED/DISPATCHED/NEEDS_IDENTIFICATION → {ANSWERS_READY}
```
GRADING/SUPERSEDED에서는 admin_override도 차단.

#### 금지 전이

- `DONE → *` (`SUPERSEDED` 제외, 확정 결과 보존)
- `SUPERSEDED → *` (종단 상태)
- `SUBMITTED → DONE` (채점 없이 완료 금지)
- `GRADING → SUBMITTED` (재시도는 FAILED 경유 필수)

#### 전이 주체

| 전이 | 주체 |
|------|------|
| → SUBMITTED | 사용자(제출) / 관리자(재시도) |
| → DISPATCHED | dispatcher 서비스 |
| → EXTRACTING | AI/OMR 워커 |
| → ANSWERS_READY | AI/OMR 워커 / 관리자(수동입력) |
| → GRADING | grader 서비스 |
| → DONE | grader 서비스 |
| → FAILED | 모든 처리 단계 (오류 시) |
| → NEEDS_IDENTIFICATION | AI/OMR 워커 |
| → SUPERSEDED | attempt 서비스 (재응시 시) |

#### 실패 상태 & 재시도

- **FAILED:** 재시도 가능. FAILED → SUBMITTED 전이 후 재처리.
- **재시도 주체:** 관리자 (수동)
- **재시도 제한:** 없음 (무한 재시도 가능 — 제한 추가 권장)
- **자동 재시도:** 없음 (수동만)

#### UI 허용 액션

| 상태 | Admin | Student |
|------|-------|---------|
| SUBMITTED | 취소 | 대기 표시 |
| DISPATCHED | 상태 확인 | (미노출) |
| EXTRACTING | 상태 확인 | (미노출) |
| ANSWERS_READY | 수동 채점, 답안 편집 | (미노출) |
| GRADING | 상태 확인 | (미노출) |
| DONE | 결과 확인, 재채점 | 결과 확인 |
| FAILED | 재시도, 상세 확인 | (미노출, 결과 없음으로 표시) |
| NEEDS_IDENTIFICATION | 학생 매칭 | (미노출) |
| SUPERSEDED | 이력 확인 | (미노출) |

#### 백엔드 불변조건

1. **lifecycle public API 강제:** 런타임 상태 변경은 `services/lifecycle.py` 함수로만 수행하며 내부에서 `STATUS_FLOW`를 통과해야 함
2. **원자성:** 모든 상태 변경은 `@transaction.atomic` + `select_for_update` 내에서 수행
3. **테넌트 격리:** `submission.tenant_id` 일치 검증 필수
4. **단일 활성 제출:** `unique_active_submission_per_target` 제약조건 준수
5. **DONE/SUPERSEDED 불변:** 종단 상태에 도달한 submission은 절대 상태 변경 불가

#### 로그/감사 포인트

- 모든 상태 전이 시 `error_message` 기록 (실패 시)
- ResultFact append-only 로그 (채점 결과)

#### E2E 테스트 시나리오

1. 정상 플로우: SUBMITTED → DISPATCHED → ANSWERS_READY → GRADING → DONE
2. AI 실패 → FAILED → 재시도 → SUBMITTED → ... → DONE
3. 식별 실패 → NEEDS_IDENTIFICATION → 수동 매칭 → ANSWERS_READY → DONE
4. 재응시: 기존 DONE → SUPERSEDED, 새 SUBMITTED 생성
5. 금지 전이 시도 시 ValidationError 발생 확인
6. 동시 상태 변경 시 select_for_update 경합 확인

---

### B2. Video

**모델:** `apps/support/video/models.py`
**상태 필드:** `status` (CharField, indexed)

#### 상태 목록

| 상태 | 의미 | 진입 조건 | 이탈 조건 |
|------|------|----------|----------|
| `PENDING` | 업로드 대기 | presigned URL 생성 | upload-complete 호출 |
| `UPLOADED` | 업로드 완료, 인코딩 대기 | S3 파일 확인 + ffprobe | 작업 생성 및 처리 시작 |
| `PROCESSING` | 인코딩 진행 중 | 워커가 작업 시작 | 인코딩 완료 또는 실패 |
| `READY` | 사용 가능 | HLS 출력 완료 | 종단 상태 (soft-delete만 가능) |
| `FAILED` | 실패 | 최대 재시도 초과 / 복구 불가 | 재시도(→UPLOADED) |

#### 허용 전이 (호환/관리용)

```
PENDING    → {UPLOADED, FAILED}
UPLOADED   → {PROCESSING, READY, FAILED}
PROCESSING → {READY, FAILED, UPLOADED}
FAILED     → {UPLOADED}
READY      → {} (종단, soft-delete만)
```

#### 금지 전이

- `READY → *` (종단 상태)
- `FAILED → PROCESSING` (UPLOADED 경유 필수)
- `PENDING → PROCESSING` (UPLOADED 경유 필수)
- `PENDING → READY` (인코딩 없이 완료 불가)

#### 전이 주체

| 전이 | 주체 |
|------|------|
| → PENDING | upload/init API |
| → UPLOADED | upload/complete API / recover_stuck_videos / retry API |
| → PROCESSING | (레거시 — 현재 job.state=RUNNING으로 대체) |
| → READY | job_complete() |
| → FAILED | job_mark_dead() / recover_stuck_videos |

#### 실패 상태 & 재시도

- **FAILED:** 재시도 가능. retry API → UPLOADED → 새 작업 생성
- **재시도 제한:** `VIDEO_MAX_JOBS_PER_VIDEO` (기본 10)
- **자동 재시도:** `enqueue_uploaded_videos` cron (1시간 간격)
- **수동 복구:** `recover_stuck_videos` (자동 cron 미설정, PENDING 정체 복구 필요 시 운영자가 실행)

#### UI 허용 액션

| 상태 | Admin | Student |
|------|-------|---------|
| PENDING | 업로드 재시도 (file_key 있을 때) | (미노출 또는 "업로드 중") |
| UPLOADED | 대기 표시, 재시도 | "처리 중" 오버레이 필요 |
| PROCESSING | 진행률 표시, 재시도 | "처리 중" 오버레이 |
| READY | 재생, 삭제, 설정 변경 | 재생 |
| FAILED | 재시도, 삭제, 오류 상세 | "실패" 표시, 재생 불가 |

#### 백엔드 불변조건

1. **1 Video = 1 Active Job:** DynamoDB lock + DB unique constraint
2. **테넌트 동시 실행 제한:** `VIDEO_TENANT_MAX_CONCURRENT`
3. **원자성:** job_complete, job_mark_dead는 Video.status + Job.state를 동일 트랜잭션에서 변경
4. **Soft-delete 보존:** deleted_at ≠ null인 영상은 180일간 복구 가능

#### E2E 테스트 시나리오

1. 정상: PENDING → UPLOADED → (Job:QUEUED→RUNNING→SUCCEEDED) → READY
2. 업로드 실패: PENDING → (타임아웃) → 운영자 `recover_stuck_videos` 실행 → FAILED
3. 인코딩 실패: PROCESSING → Job:RETRY_WAIT → (5회 실패) → Job:DEAD → FAILED
4. 재시도: FAILED → retry API → UPLOADED → 새 Job → READY
5. 동시성: 같은 영상에 동시 retry → DDB lock으로 1개만 성공

---

### B3. VideoTranscodeJob

**모델:** `apps/support/video/models.py`
**상태 필드:** `state` (CharField, indexed)

#### 상태 목록

| 상태 | 의미 | 진입 조건 | 이탈 조건 |
|------|------|----------|----------|
| `QUEUED` | 대기 중 | 작업 생성 | 워커 선점 |
| `RUNNING` | 실행 중 | 워커가 선점 | 완료/실패/하트비트 만료 |
| `SUCCEEDED` | 완료 | 인코딩 성공 | 종단 상태 |
| `FAILED` | 실패 (재시도 가능할 수 있음) | 인코딩 오류 | 종단 상태 |
| `RETRY_WAIT` | 재시도 대기 | 실패 + attempt_count < max | 다시 QUEUED로 선점 |
| `DEAD` | 격리 (최종 실패) | max attempts 초과 / 수동 | 종단 상태 |
| `CANCELLED` | 취소됨 | 사용자/시스템 취소 | 종단 상태 |

#### 허용 전이

```
QUEUED     → {RUNNING, CANCELLED, DEAD}
RUNNING    → {SUCCEEDED, FAILED, RETRY_WAIT, CANCELLED, DEAD}
RETRY_WAIT → {RUNNING, DEAD, CANCELLED}
SUCCEEDED  → {} (종단)
FAILED     → {} (종단)
DEAD       → {} (종단)
CANCELLED  → {} (종단)
```

#### 금지 전이

- `SUCCEEDED → *` (종단 상태, 절대 덮어쓰기 금지)
- `DEAD → *` (종단 상태)
- `CANCELLED → *` (종단 상태)
- `FAILED → *` (종단 상태)

#### 전이 주체

| 전이 | 주체 |
|------|------|
| → QUEUED | video encoding service |
| → RUNNING | AWS Batch worker (`batch_main.job_set_running`) — daemon 폐기 2026-05-10 |
| → SUCCEEDED | worker (job_complete) |
| → RETRY_WAIT | worker (job_fail_retry) / scan_stuck_video_jobs / reconcile_batch_video_jobs (Batch FAILED 자동 재제출) |
| → DEAD | scan_stuck (MAX_ATTEMPTS) / reconcile (RECONCILE_MAX_ATTEMPTS) / retry API (stale detection) / cleanup_orphan (Video soft-delete) |
| → CANCELLED | retry API (기존 작업 취소) / 영상 삭제 |

#### 백엔드 불변조건

1. **종단 상태 불변:** SUCCEEDED, FAILED, DEAD, CANCELLED에 도달한 job은 state 변경 불가
2. **하트비트:** RUNNING 상태 job은 60초마다 하트비트 갱신 필수
3. **정체 감지:** scan_stuck_video_jobs가 하트비트 만료 RUNNING job을 RETRY_WAIT로 전이
4. **조회 전용 감지:** `detect_stuck_videos --dry-run`은 상태, 작업, `VideoOpsEvent`를 포함한 어떤 데이터도 기록하지 않는다. 옵션 없는 일반 감지는 정체가 있을 때 `VIDEO_STUCK_DETECTED` 운영 이벤트를 기록하며, 실제 복구는 `--repair`가 있고 `--dry-run`이 없을 때만 수행한다.
5. **조회 전용 정합성 점검:** `reconcile_batch_video_jobs --dry-run`은 DB, Redis의 단일 실행 락·연속 not-found 카운터, AWS Batch 작업을 변경하지 않는다. 현재 Redis 카운터와 AWS 상태를 읽어 실제 실행의 판단만 미리 보여 주며, AWS 조회 실패 운영 이벤트도 실제 실행에서만 기록한다. 로그도 `reconcile_dry_run`으로 구분하고 잠금을 실제 획득한 것처럼 기록하지 않는다. `--skip-lock` 실제 실행은 별도 `reconcile_lock_skipped` 경고를 남긴다.

---

### B4. ExamAttempt

**모델:** `apps/domains/results/models/exam_attempt.py`
**상태 필드:** `status` (CharField)

#### 상태 목록

| 상태 | 의미 | 진입 조건 | 이탈 조건 |
|------|------|----------|----------|
| `pending` | 생성됨, 채점 대기 | attempt 생성 | 채점 시작 |
| `grading` | 채점 진행 중 | grader가 채점 시작 | 채점 완료/실패 |
| `done` | 채점 완료 | 채점 성공 | 종단 상태 |
| `failed` | 채점 실패 | 채점 오류 | 재채점(→grading) |

#### 허용 전이

```
pending  → {grading}
grading  → {done, failed}
failed   → {grading}
done     → {} (종단)
```

#### 금지 전이

- `done → *` (종단 상태)
- `pending → done` (grading 경유 필수)
- `pending → failed` (grading 경유 필수)

#### 불변조건

1. **Append-only:** 기존 attempt 수정 금지 (새 attempt 생성)
2. **is_representative:** 하나의 (exam_id, enrollment_id)에 대해 정확히 1개만 True
3. **attempt_index:** 동일 (exam_id, enrollment_id)에서 단조 증가
4. **원자성:** 모든 상태 변경은 `@transaction.atomic` 내에서 수행

---

### B5. ExamResult

**모델:** `apps/domains/results/models/exam_result.py`
**상태 필드:** `status` (CharField)

#### 상태 목록

| 상태 | 의미 | 진입 조건 | 이탈 조건 |
|------|------|----------|----------|
| `DRAFT` | 초안 (편집 가능) | 채점 결과 생성 | 확정 |
| `FINAL` | 확정 (불변) | finalize() 호출 | 종단 상태 |

#### 허용 전이

```
DRAFT → {FINAL}
FINAL → {} (종단, 절대 불변)
```

#### 금지 전이

- **`FINAL → DRAFT` (절대 금지)** — 확정된 결과는 어떤 경우에도 DRAFT로 돌아갈 수 없음
- **`FINAL → FINAL` (멱등, 허용)** — finalize() 중복 호출은 무해

#### 불변조건

1. **FINAL 불변성:** status=FINAL인 ExamResult의 score, breakdown, manual_overrides는 수정 불가
2. **finalized_at:** FINAL 전이 시 반드시 설정
3. **학생 노출:** Student API는 status=FINAL인 결과만 반환해야 함
4. **운영 정합성 검사:** `check_integrity`의 `manual_overrides` 누락 카운트는
   임의 샘플이 아니라 비어 있지 않은 전체 JSON 행을 500행 단위로 스트리밍해
   검사한다. 데이터가 200건을 넘어도 뒤쪽의 `max_score` 누락을 숨기지 않는다.

---

### B6. Clinic SessionParticipant

**모델:** `apps/domains/clinic/models.py`
**상태 필드:** `status` (CharField)

#### 상태 목록

| 상태 | 의미 | 진입 조건 | 이탈 조건 |
|------|------|----------|----------|
| `pending` | 예약 대기 | 학생 신청 | 승인/거절 |
| `booked` | 예약 확정·미등원 | 관리자 승인 / 직접 등록 | 등원/결석/취소 |
| `attended` | 등원 완료 | 체크인 또는 결석 정정 | 하원 또는 스태프 상태 정정 |
| `no_show` | 결석 | 사전 연락 또는 현장 확인 | 지각 등원/일반 등원 정정, 보충 일정 생성 |
| `cancelled` | 취소됨 | 학생/관리자 취소 | 종단 상태 |
| `rejected` | 거절됨 | 관리자 거절 | 종단 상태 |

#### 허용 전이

```python
# apps/domains/clinic/services/lifecycle.py
STAFF_STATUS_TRANSITIONS = {
    "pending": {"booked", "rejected", "cancelled"},
    "booked": {"attended", "no_show", "cancelled"},
    "attended": {"booked", "no_show"},
    "no_show": {"booked", "attended"},
    "rejected": set(),
    "cancelled": set(),
}

STUDENT_STATUS_TRANSITIONS = {
    "pending": {"cancelled"},
    "booked": set(),
    "attended": set(),
    "no_show": set(),
    "rejected": set(),
    "cancelled": set(),
}
```

등원, 하원, 자율학습 완료는 서로 분리된 use-case다.

```python
COMPLETE_ALLOWED_STATUSES = {"attended"}
```

`complete`는 이미 `attended`인 참가자에게만 `completed_at`을 기록한다. 따라서
`pending`, `booked`, `no_show`, `cancelled`, `rejected` 참가자는 완료 처리할 수 없다.
`uncomplete`는 완료 시각만 취소하고 출석 상태는 유지한다. `checked_in_at`은 실제
등원 시각, `is_late`는 지각 여부, `checked_out_at`은 실제 하원 시각이다. 하원은
`attended + checked_in_at`에서만 한 번 허용하며 하원 후 상태 정정은 실패 폐쇄한다.
`completed_at`/`completed_by`는 시험·과제 보충 및 자율학습 완료 이력이므로 출석
정정이나 하원에서 지우거나 하원 시각으로 재사용하지 않는다.

#### 금지 전이

- `pending → attended` (booked 경유 필수)
- `pending → no_show` (booked 경유 필수)
- `booked → pending` (승인 취소 없음)
- `booked → rejected` (승인된 예약 거절 없음)
- `cancelled/rejected → *` (상태 전이 종단)
- `booked/no_show → checkout` (등원 시각 없음)
- `checked_out_at != null → 상태 정정/중복 checkout`

#### UI 허용 액션

| 상태 | Admin | Student |
|------|-------|---------|
| pending | 승인(→booked), 거절(→rejected), 취소(→cancelled) | 취소(→cancelled) |
| booked | 등원/지각 등원(→attended), 재촉, 결석 확인(→no_show), 하원 비활성 | (변경 불가, "확정" 표시) |
| attended | 하원(`checked_out_at`), 자율학습 완료(`completed_at`)를 별도 처리 | (미노출) |
| no_show | 등원/지각 등원 정정(→attended), 기존 일정 이동 또는 새 일정 생성 | (미노출) |
| cancelled | 종단 | (미노출) |
| rejected | 종단 | (미노출) |

#### 희망 시간과 메모 소유권

- `Session.allow_time_preference=true`인 일정에서만 학생이
  `preferred_start_time`과 `preferred_end_time`을 함께 요청할 수 있다. 두 시각은
  세션 운영 범위 안이고 시작이 종료보다 빨라야 한다. 이 값은 희망사항이며
  `Session.start_time`·`duration_minutes`, 참가자 상태, 출결 시각을 바꾸지 않는다.
- `SessionParticipant.student_request_memo`만 학생·학부모 요청으로 노출한다.
  인증된 학생의 예약 생성·일정 변경 또는 교직원이 출처를 확인해 이 전용 필드로
  입력한 값만 저장한다. 기존 `memo`는 작성 주체를 증명할 수 없는 레거시 운영
  필드이므로 이 필드로 이관하지 않으며 학생·학부모 응답에서 제거한다.
- `SessionParticipant.staff_memo`는 교직원 인수인계 전용이다. staff 응답에서만
  반환하며 학생·학부모 응답에서는 필드를 제거한다. 교직원은
  `PATCH /clinic/participants/{id}/staff-memo/`로 요청 메모와 독립적으로 저장한다.
- 일반 참가자 `PUT/PATCH`는 `preferred_start_time`, `preferred_end_time`,
  `student_request_memo`를 수정할 수 없고 해당 키가 들어오면 400으로 거부한다.
  희망 시간과 학생 요청은 검증된 예약 생성·일정 변경 경로에서만 기록한다.
- 희망 시간과 두 메모는 예약 승인, 현장 출결, `completed_at`, ClinicLink 해소,
  오늘 할 범위에 영향을 주지 않는다. 관련 값이 잘못되어도 기존 예약 시간을
  추정하거나 자동 확정하지 않고 요청 전체를 거부한다.

결석은 학생이 사전 연락했거나 당일 불참이 확정된 경우에 사용한다. 화면은 Enter로
확정하고 Esc로 취소할 수 있는 확인 단계를 거친 뒤 1회 알림톡을 요청하고, 결석
기록을 보존한 채 기존 세션으로 이동하거나 새 클리닉 일정을 만들 수 있게 한다.
30분 뒤 도착한 학생처럼 결석 판정을 정정하는 경우 `no_show → attended`와
`is_late=true`를 함께 기록한다.

단일 `booked` 참가자 재촉은
`POST /clinic/participants/{id}/remind/`가 소유하며, 현재 테넌트의 해당 학생에게
승인된 공용 `clinic_reminder` 알림톡만 발송한다. 비활성 설정·누락 템플릿·유효하지
않은 학생 전화번호는 실패 폐쇄하고 성공으로 표시하지 않는다. 운영자가 같은
참가자에게 재촉을 다시 누른 경우는 각각 별도의 수동 요청으로 기록·큐잉하며,
분 단위 중복 제거로 두 번째 요청을 삼키지 않는다. 반복 재촉은 10분 이상 사용자
간격, 같은 날짜의 종료 시각, 최대 22:00, 최대 12개 미래 occurrence를 서버에서
검증한다. 등원·결석·하원 시 해당 참가자의 미래 수동 재촉을 취소하고 payload의
수신번호·본문을 제거한다.

#### 현장 등원 중 목록

`GET /clinic/participants/?onsite_date=YYYY-MM-DD`는 현재 테넌트에서 지정 날짜의
등원 후 미하원 참가자를 기존 page/page_size 응답으로 반환한다. 필터는 pagination
전에 전체 queryset에 적용하며 정렬은 `checked_in_at ASC`, 클리닉 세션
`start_time ASC`, 참가자 `id ASC`로 고정한다. `ordering` query가 함께 와도 현장
운영 순서를 바꾸지 않는다.

- participant, session, student가 모두 현재 tenant에 속하고 `session.date`가
  요청 날짜와 정확히 같아야 한다.
- `status=attended`, `checked_in_at IS NOT NULL`, `checked_out_at IS NULL`을 모두
  만족해야 한다. 세션이 없거나, 등원 시각이 없거나, 이미 하원했거나, tenant 관계가
  깨진 stale/corrupt 행은 노출하지 않는다.
- `is_late`, `completed_at`, ClinicLink, `planned_clinic_link_ids`는 현장 포함 판정에
  영향을 주지 않는다. 현장 상태, 학습 완료, 보충 품목, today-plan은 독립 계약이다.
- `onsite_date`가 비었거나 YYYY-MM-DD 유효 날짜가 아니면 400으로 실패 폐쇄한다.

집중 회귀는 `tests/test_clinic_participant_onsite_filter_api.py`가 tenant/corrupt 행
배제, 고정 정렬, 20건을 넘는 결과의 pagination 경계와 상태 독립성을 검증한다.

#### 오늘 할 범위 계획

`SessionParticipantPlanItem`은 한 클리닉 참가자에게서 오늘 처리할 미해결
`ClinicLink` 선택을 저장한다. `PUT
/clinic/participants/{id}/planned-clinic-links/`는 staff-only 전체 교체이며 요청은
`{"planned_clinic_link_ids": [1, 2]}`다. 응답과 참가자 projection은 현재 유효한
ID를 오름차순 `planned_clinic_link_ids`로 돌려준다.

- 같은 테넌트·같은 학생의 활성 수강에 속하고, 원 수업 차시와 수강 강의가
  일치하며, `is_auto=true`, `resolved_at=null`인 링크만 허용한다.
- 클리닉 세션에 `target_lectures`가 있으면 그 강의의 링크만 허용한다. 중복,
  다른 학생/테넌트, 비활성 수강, 해소된 링크, 세션 범위 밖 링크가 하나라도 있으면
  전체 요청을 400으로 거부하고 기존 선택을 유지한다.
- 참가자 행을 `SELECT FOR UPDATE`한 뒤 링크를 ID 순으로 잠그고 전체 교체한다.
  PostgreSQL 동시 교체도 활성 중복 없이 직렬화하며, 활성 행에는 부분 unique
  constraint를 둔다.
- 선택과 제거는 생성 시각·선택 직원·제거 시각·제거 직원·사유를 남긴다. staff
  교체는 빠진 행을 `staff_replace`로 종료하고 새 선택을 추가한다. 물리 삭제로
  이력을 덮어쓰지 않는다.
- ClinicLink가 시험/과제 통과, 수동 해소, 면제, 원본 제거, 다음 차수 이월로
  닫히면 활성 계획 행도 `clinic_link_resolved:<resolution_type>`로 감사 종료한다.
  따라서 projection에 stale ID가 보이지 않고, unresolve가 과거 선택을 임의로
  되살리지 않는다.
- 등원·지각·결석·하원, `completed_at` 자율학습 완료는 계획을 해소하거나 지우지
  않는다. 계획 선택, 현장 상태, 학습 완료, ClinicLink 해소는 네 개의 독립 계약이다.
- 교직원 `GET /results/admin/clinic-targets/`의 예약 context는 활성
  `SessionParticipantPlanItem`을 역방향으로 따라간 결과만 사용한다. 같은 tenant와
  같은 학생, 일치하거나 비어 있는 참가자 수강 관계를 방어적으로 재검증하며
  이름·날짜·제목 유사성으로 연결하지 않는다. projection에는 정확한 plan item,
  participant, clinic session 식별자와 세션 날짜·시간·장소·참가 상태,
  `student_request_memo`, staff-only `staff_memo`, 선택 provenance가 포함된다.
  출처 불명 legacy participant `memo`는 이 경계에서 항상 제외한다.
- 위 조회는 tenant context가 없으면 200 빈 배열이 아니라 `403 TENANT_REQUIRED`로
  실패 폐쇄한다. 따라서 운영 화면은 권한/tenant 오류를 “모두 처리됨”으로 표시하지
  않고 명시적 오류와 재시도를 제공해야 한다.

집중 검증은 `tests/test_clinic_participant_plan_api.py`가 API 유효성·전체 롤백·
감사 종료·상태 독립성 및 PostgreSQL 동시 교체를 확인하고,
`apps/domains/results/tests/test_admin_clinic_targets_contract.py`가 역방향 read
projection과 tenant/권한 실패 폐쇄를 확인한다.

---

### B7. Exam

**모델:** `apps/domains/exams/models/exam.py`
**상태 필드:** `status` (CharField, indexed)

#### 상태 목록

| 상태 | 의미 |
|------|------|
| `DRAFT` | 초안 (레거시 — 사실상 사용 안 함) |
| `OPEN` | 진행 중 (기본값) |
| `CLOSED` | 마감 |

#### 허용 전이

```
DRAFT  → {OPEN}
OPEN   → {CLOSED}
CLOSED → {OPEN}  (재개방 허용)
```

#### 비고

- DRAFT는 레거시. 새 시험은 항상 OPEN으로 생성됨.
- 신규 운영 UI는 시험 전체 종료/재개방을 주 조작 단위로 쓰지 않는다.
- 성적 탭과 클리닉 판정의 SSOT는 학생별 제출/시도/Result/Achievement 상태다.
- 실질적 응시 가능 판단에서 status는 호환 보조 필드이며, 일정은 `open_at`/`close_at`와 별도 정책을 따른다.
- `answer_visibility`: HIDDEN / AFTER_CLOSED / ALWAYS (시험 상태와 독립적으로 정답 공개 제어)

---

### B8. Homework

**모델:** `apps/domains/homework_results/models/homework.py`
**상태 필드:** `status` (CharField, indexed)

Exam과 동일한 DRAFT / OPEN / CLOSED 호환 필드를 가진다. 신규 운영 UI의 주 조작 단위는 과제 전체 종료가 아니라 학생별 HomeworkScore/Submission/Achievement 상태다.

---

### B9. Enrollment

**모델:** `apps/domains/enrollment/models.py`
**상태 필드:** `status` (CharField)

#### 상태 목록

| 상태 | 의미 |
|------|------|
| `ACTIVE` | 수강 중 (기본값) |
| `INACTIVE` | 비활성 (퇴원/중단) |
| `PENDING` | 대기 (등록 승인 전) |

#### 허용 전이

```
PENDING  → {ACTIVE, INACTIVE}
ACTIVE   → {INACTIVE}
INACTIVE → {ACTIVE}  (재등록)
```

---

### B10. StudentRegistrationRequest

**모델:** `apps/domains/students/models.py`
**상태 필드:** `status` (CharField, indexed)

#### 상태 목록

| 상태 | 의미 |
|------|------|
| `pending` | 대기 (기본값) |
| `approved` | 승인됨 (학생 계정 생성) |
| `rejected` | 거절됨 |

#### 허용 전이

```
pending  → {approved, rejected}
approved → {} (종단)
rejected → {} (종단)
```

#### 금지 전이

- `approved → *` (이미 학생 생성됨, 롤백 불가)
- `rejected → approved` (재신청 필요)

#### 불변조건

1. **원자성 필수:** approve와 reject는 `select_for_update`로 동시 실행 방지
2. **approved → 학생 생성:** `students.services.registration_approval.approve_registration_request()`가 `pending -> approved` 전이와 학생 계정 생성을 같은 트랜잭션에서 처리
3. **발송 분리:** 승인은 계정 안내값만 암호화 staging하고 알림톡을 보내지 않음. 첫 ACTIVE 수강 확정 후 outbox를 만들며, 실패해도 이미 커밋된 승인/학생/수강 생성을 되돌리지 않음

---

### B11. AIJobModel

**모델:** `apps/domains/ai/models.py`
**상태 필드:** `status` (CharField)

#### 상태 목록

| 상태 | 의미 | 활성 여부 |
|------|------|----------|
| `PENDING` | 대기 중 | 활성 |
| `VALIDATING` | 입력 검증 중 | (미사용 — orphan) |
| `RUNNING` | 실행 중 | 활성 |
| `DONE` | 완료 | 종단 |
| `FAILED` | 실패 | 종단 |
| `REJECTED_BAD_INPUT` | 입력 거부 | 종단 |
| `FALLBACK_TO_GPU` | GPU 폴백 | (미사용 — orphan) |
| `RETRYING` | 재시도 중 | (미사용 — orphan) |
| `REVIEW_REQUIRED` | 검토 필요 | (미사용 — orphan) |

#### 허용 전이 (실제 사용되는 것만)

```
PENDING → {RUNNING}
RUNNING → {DONE, FAILED}
```

#### 불변조건

1. **종단 상태 불변:** DONE, FAILED, REJECTED_BAD_INPUT에 도달한 job은 상태 변경 불가
2. **하트비트:** RUNNING 상태에서 lease 만료 감지

---

### B12. Invoice

**모델:** `apps/billing/models.py`
**상태 필드:** `status` (CharField, indexed)

#### 상태 목록

| 상태 | 의미 |
|------|------|
| `SCHEDULED` | 예정 |
| `PENDING` | 결제 대기 |
| `PAID` | 결제 완료 |
| `FAILED` | 결제 실패 |
| `OVERDUE` | 연체 |
| `VOID` | 무효 |

#### 허용 전이

```
SCHEDULED → {PENDING, VOID}
PENDING   → {PAID, FAILED}
FAILED    → {PENDING, OVERDUE, VOID}
OVERDUE   → {PAID, VOID}
PAID      → {} (종단)
VOID      → {} (종단)
```

**전이 SSOT:** `apps/billing/services/invoice_service.py`

- `process_billing`은 결제기한에 도달한 `INVOICE_REQUEST`를
  `SCHEDULED → PENDING`으로 전환한다. 자동카드는 결제 실행 직전에 같은
  전이를 거친다.
- 수동 입금 확인은 `PENDING/FAILED/OVERDUE`에서만 허용한다. `FAILED`는
  `PENDING`을 경유하며, `PAID` 전이와 수동 `PaymentTransaction(SUCCESS)`
  기록은 한 트랜잭션으로 처리한다.
- API는 `can_mark_paid`, `payment_blocked_reason`, `is_terminal`을 노출해
  UI가 상태기계를 추측하지 않게 한다.

#### 계좌이체 입금 신고

`BankTransferNotice`는 고객의 신고와 실제 수납을 분리한다.

```
SUBMITTED → {CONFIRMED, REJECTED}
REJECTED  → SUBMITTED (동일 청구서 재신고)
CONFIRMED → {} (종단)
```

- 고객은 자기 테넌트의 `INVOICE_REQUEST`이면서 `PENDING/OVERDUE`인
  청구서만 신고할 수 있다. 금액은 클라이언트 입력이 아니라
  `Invoice.total_amount`에서 고정한다.
- 플랫폼 superuser만 입금을 확인하거나 반려한다.
- 확인은 `Program → Invoice → BankTransferNotice` 순서로 잠그며,
  `PAID`, 수동 `PaymentTransaction(SUCCESS)`, 구독 갱신,
  `TaxInvoiceIssue.READY`를 한 트랜잭션으로 처리한다.
- 한 청구서에는 입금 신고 1행만 존재하며 반려된 행만 재신고할 수 있다.

---

### B13. PaymentTransaction

**모델:** `apps/billing/models.py`
**현재 상태:** 자동결제와 Toss webhook이 `PENDING → SUCCESS/FAILED`,
환불 webhook이 `SUCCESS/PARTIALLY_REFUNDED → PARTIALLY_REFUNDED/REFUNDED`를
처리한다. 수동 입금은 처음부터 `SUCCESS`인 대조 레코드를 원자적으로 만든다.
동시 웹훅이 같은 idempotency key를 먼저 만든 복구 경로는
`PaymentTransaction` 자체 행만 잠근다. 선택 관계인 `invoice=NULL` 레거시·경합 행을
`select_related`하더라도 PostgreSQL nullable outer join 전체에 `FOR UPDATE`를
적용하지 않으므로 복구 요청이 500으로 실패하지 않는다.

---

### B14. Subscription Status

**모델:** `apps/core/models/program.py`
**상태 필드:** `subscription_status` (CharField, indexed)

#### 상태 목록

| 상태 | 의미 |
|------|------|
| `active` | 활성 구독 |
| `expired` | 만료 |
| `grace` | 유예 기간 |

해지 예약은 상태가 아니라 `cancel_at_period_end` 플래그다.

#### 허용 전이

```
active  → {grace, expired}
grace   → {active, expired}
expired → {active}
```

**전이 SSOT:** `apps/billing/services/subscription_service.py`

- `BILLING_GRACE_PERIOD_DAYS`(기본 7일)는 유료 기간 종료일 다음 날부터
  적용하며, `service_access_expires_at = subscription_expires_at + grace days`다.
- `process_billing`이 `active → grace → expired`를 수행하고
  `sync_subscription`은 누락 복구 안전망이다. 장기간 배치가 누락돼 유예
  종료일까지 지난 `active` 레코드는 한 실행에서 두 전이를 연속 수행하며,
  dry-run도 이 전체 체인을 예고한다.
- `audit_billing_fields --strict`는 `active`/`grace` 저장 상태가 실효 접근
  기간보다 뒤처진 경우를 릴리스 차단 오류로 보고한다.
- 402 접근 판정과 구독 API의 `days_remaining`은 유예 상태에서 실제 서비스
  접근 종료일을 기준으로 한다.

---

### B15. VideoPlaybackSession

**모델:** `apps/support/video/models.py`
**상태 필드:** `status` (CharField, indexed)

#### 상태 목록

| 상태 | 의미 |
|------|------|
| `ACTIVE` | 재생 중 |
| `ENDED` | 정상 종료 |
| `REVOKED` | 위반으로 차단 |
| `EXPIRED` | 타임아웃 만료 |

#### 허용 전이

```
ACTIVE  → {ENDED, REVOKED, EXPIRED}
ENDED   → {} (종단)
REVOKED → {} (종단)
EXPIRED → {} (종단)
```

---

### B16. PostEntity

**모델:** `apps/domains/community/models/post.py`
**상태 필드:** `status` (CharField)

| 상태 | 의미 |
|------|------|
| `draft` | 임시 저장 |
| `published` | 게시됨 |
| `archived` | 보관됨 |

---

### B17. WrongNotePDF

**모델:** `apps/domains/results/models/wrong_note_pdf.py`
**상태 필드:** `status` (CharField)

| 상태 | 의미 |
|------|------|
| `PENDING` | 생성 대기 |
| `RUNNING` | 생성 중 |
| `DONE` | 완료 |
| `FAILED` | 실패 |

---

### B18. NotificationLog

**모델:** `apps/support/messaging/models.py`
**상태 필드:** `status` (CharField)

| 상태 | 의미 |
|------|------|
| `processing` | 워커 선점, 발송 중 |
| `sent` | 발송 완료 |
| `failed` | 발송 실패 |

#### 불변조건

- 3-layer idempotency: Redis lock + DB UniqueConstraint + transport dedup

---

### B19. Attendance

**모델:** `apps/domains/attendance/models.py`
**상태 필드:** `status` (CharField)

| 상태 | 의미 |
|------|------|
| `UNSET` | 미입력 |
| `PRESENT` | 출석 |
| `LATE` | 지각 |
| `ONLINE` | 온라인 출석 |
| `SUPPLEMENT` | 보강 |
| `EARLY_LEAVE` | 조퇴 |
| `ABSENT` | 결석 |
| `RUNAWAY` | 이탈 |
| `MATERIAL` | 교재만 수령 |
| `INACTIVE` | 비활성 |
| `SECESSION` | 퇴원 |

**비고:** 차시 학생 일괄 배정과 수강 Excel 등록이 새 출결 행을 만들 때는
`UNSET`으로 시작한다. 기존 출결은 마이그레이션하지 않으며, 등원 뒤 교직원이
현장·영상·결석 등 실제 상태를 선택한다. **전체 출석**은 명시적으로 실행한
경우에만 `PRESENT`로 바꾼다. 이 일괄 작업은 바뀐 행의 직전 상태를 담은
10분 유효 서명 토큰을 반환하며, `bulk_undo_present`가 같은 테넌트·차시의
행을 잠근 뒤 현재 상태가 모두 `PRESENT`일 때만 직전 상태를 원자적으로
복구한다. 대상 중 하나라도 삭제·퇴원되었거나 현재 상태가 `PRESENT`가 아니면
전체 복구를 거부하고
교직원에게 현재 상태 확인을 요구한다. 기존 `PRESENT`, `SECESSION`, 비활성
수강등록은 일괄 변경과 복구 대상에 포함하지 않는다.

`ABSENT`는 이 차시에 현장·영상 학습 요청이 모두 없는 실제 결석이며 시험·과제
todo 대상이 아니다. `ONLINE`은 영상 요청 상태이므로 `PRESENT`와 함께 todo
대상이다. 성적 roster 자체는 모든 출결 상태를 유지하지만 실제 결석의 아직
수행하지 않은 시험·과제 칸과 현재 클리닉 투영만 제외한다. 기존 응시·제출·점수,
수동 판정, 대상 행과 클리닉 이력은 출결 전환으로 삭제하지 않는다.

개별 PATCH/PUT과 전체 현장 출석은 출결 행을 잠근 같은 트랜잭션 안에서 마지막
상태를 저장한다. `ABSENT → ONLINE/PRESENT` 등 대상 상태 복구는 활성 정규
시험·과제 대상을 멱등하게 보강하고 진행도를 재계산한다. `ONLINE/PRESENT →
ABSENT`는 원본을 삭제하지 않고 조회·쓰기 경계에서 todo를 실패 폐쇄한다. 출결
전환은 메시지나 provider 요청을 자동 생성하지 않는다. 세부 데이터·API 계약은
[`attendance.md`](attendance.md#시험과제-학습-todo-경계)가 소유한다.

대부분은 상태기계가 아닌 분류 값이다. 단,
`SECESSION`은 단순 출결
분류가 아니라 퇴원 처리 workflow로 동작한다. 전환 시 `confirm_secession:
true`가 필요하며 수강등록 비활성화, 자동 수납 비활성화, 시험/과제 대상
제거가 함께 수행된다. `SECESSION` 진입 후 일반 PATCH/PUT으로 출석 분류를
되돌릴 수 없으며, 재등록은 수강등록 lifecycle을 통해 새로 처리한다.

**검증:** `apps/domains/attendance/tests/test_bulk_present_undo.py`는 직전 상태
정확 복원, 일부 행 재수정 시 전체 거부, 테넌트 경계, 토큰 변조 거부,
변경 없음 응답을 증명한다.
`apps/domains/attendance/tests/test_learning_todo_eligibility_pg.py`는 상태별 대상,
이력 보존, 복구 멱등성, 클리닉 투영과 PostgreSQL 동시 전환을 증명한다.

---

## C. 현재 구현 불일치 목록

### C1. CRITICAL — Submission can_transit() 미적용 → **FIXED**

- **해결:** `apps/domains/submissions/services/transition.py` 전이표/guard와 `services/lifecycle.py` 런타임 facade 분리
- **적용:** live write path는 `mark_*`, `fail_submission`, `retry_failed_submission`, `supersede_*`, `reopen_for_regrade` 같은 의미 있는 lifecycle 함수로 라우팅
- **추가 수정:**
  - grader.py FAILED no-op 버그 수정 (transaction rollback → FAILED 미persist 문제)
  - manual_edit SUPERSEDED→ANSWERS_READY 차단
  - STATUS_FLOW 현실화 (SUBMITTED→GRADING/ANSWERS_READY 추가, DONE→SUPERSEDED 추가)
  - EXTRACTING 상태를 STATUS_FLOW에 포함해 stuck recovery / cascade discard도 전이 가드를 통과
  - 데드코드 정리 (progress/ai_omr_result_mapper.py)
- **테스트:** 전이/라이프사이클/멱등/cascade/stuck recovery 테스트로 검증

### C2. CRITICAL — ExamResult FINAL→DRAFT 회귀 → **FIXED**

- **해결:** `exam_grading_service.py`는 기존 `ExamResult`를 `select_for_update()`로 잠근 뒤 `FINAL`이면 early return한다.
- **보강:** `sync_result_from_submission.py`의 legacy snapshot sync도 같은 방식으로 `FINAL`을 보존한다.
- **효과:** 자동 재채점/동기화 재호출이 확정 결과를 DRAFT로 되돌리지 않는다.

### C3. HIGH — StudentRegistrationRequest approve/reject 레이스 → **FIXED**

- **해결:** approve/auto-approve/bulk_approve 경로는
  `apps/domains/students/services/registration_approval.py`의
  `approve_registration_request()`를 통해 `select_for_update`, 상태 재확인,
  계정 생성, `approved` 전이를 atomic 처리한다. reject/bulk_reject 경로는
  view compatibility layer에서 `select_for_update`와 상태 재확인을 사용한다.
- **비고:** 새 레이스가 발견되면 이 항목을 새 증거와 함께 다시 열고, 현재 fixed 기록은 변경 이력으로 남긴다.

### C4. HIGH — VideoTranscodeJob 종단 상태 덮어쓰기 → **FIXED**

- **해결:** `repositories_video.py`의 `job_fail_retry()`, `job_cancel()`, `job_mark_dead()`가 terminal 상태를 보호한다.
- **검증 기준:** `SUCCEEDED` / `DEAD` / `CANCELLED` 등 종단 상태는 retry/cancel/dead 업데이트 대상에서 제외한다.

### C5. MEDIUM — ExamAttempt sync_result 원자성 부재 → **FIXED**

- **해결:** `sync_result_from_submission.py`가 atomic 블록 안에서 기존 `ExamAttempt`를 `select_for_update()`로 조회한다.
- **효과:** grader/sync 동시 실행 시 기존 attempt row의 상태와 snapshot 업데이트가 같은 잠금 경로를 탄다.

### C6. MEDIUM — Clinic change_booking 전이 우회 → **FIXED**

- **해결:** `participant_views.py`는 `clinic/services/lifecycle.py::change_participant_booking()`에 위임한다.
- **보강:** 서비스는 기존 예약을 `select_for_update()`로 잠그고, `PENDING` 예약만 변경 허용하며, cancel 직전 `PENDING/BOOKED` allowed-from을 재확인한다.

### C7. MEDIUM — AIJobModel job_save_failed SFU 부재 → **FIXED**

- **해결:** `repositories_ai.py::job_save_failed()`가 atomic 블록에서 `select_for_update()` 후 terminal 상태를 재확인한다.
- **효과:** 이미 `DONE` / `FAILED` / `REJECTED_BAD_INPUT`인 job은 publish-failure path가 덮어쓰지 않는다.

### C8. LOW — Frontend UPLOADED 상태 미처리 (Student)

- **위치:** `frontend/src/student/` (비디오 관련 컴포넌트)
- **문제:** Student 앱에서 UPLOADED 상태 영상에 "처리 중" 오버레이가 표시되지 않음
- **영향:** 학생이 UPLOADED 영상을 재생 가능한 것으로 오인 (실제로는 isPlayable 체크로 차단)

### C9. LOW — Frontend 클리닉 rejected 미표시 (Student)

- **위치:** `frontend/src/student/domains/clinic/pages/ClinicPage.tsx`
- **문제:** rejected 예약이 API에서 반환되지만 UI에 표시되지 않음
- **영향:** 학생이 거절 사유를 확인할 수 없음

### C10. LOW — Frontend 402 SubscriptionExpiredOverlay (Student)

- **위치:** `frontend/src/student/`
- **문제:** 402 이벤트는 발생하지만 Student 앱에 오버레이 컴포넌트가 없음
- **영향:** 구독 만료 시 학생 앱에서 적절한 안내가 표시되지 않음

### C11. INFO — AIJobModel Orphan 상태

- **위치:** `apps/domains/ai/models.py`
- **Orphan:** VALIDATING, FALLBACK_TO_GPU, RETRYING, REVIEW_REQUIRED — 런타임 코드에서 설정되지 않음

---

## D. 우선 수정 대상

| # | 불일치 | 위험도 | 수정 상태 | 수정 내용 |
|---|--------|--------|----------|----------|
| 1 | C2: ExamResult FINAL→DRAFT | CRITICAL | **FIXED** | `exam_grading_service.py`: FINAL 결과 재채점 시 early return |
| 2 | C1: Submission can_transit | CRITICAL | **FIXED** | `transition.py` SSOT 생성, live write path 리팩터링, EXTRACTING recovery/cascade 가드 포함 |
| 3 | C3: Registration race | HIGH | **FIXED** | `students/services/registration_approval.py`: approve 상태 전이+학생 생성 atomic 처리; `registration_views.py`: reject/bulk_reject 잠금 유지 |
| 4 | C4: Job terminal overwrite | HIGH | **FIXED** | `repositories_video.py`: job_cancel/mark_dead/fail_retry에 종단 상태 가드 |
| 5 | C5: ExamAttempt atomic | MEDIUM | **FIXED** | `sync_result_from_submission.py`: select_for_update 추가 |
| 6 | C6: Clinic booking bypass | MEDIUM | **FIXED** | `clinic/views/participant_views.py`: change_booking에 CANCEL_ALLOWED_FROM 가드 |
| 7 | C7: AI job SFU | MEDIUM | **FIXED** | `repositories_ai.py`: job_save_failed에 atomic + select_for_update + 종단 상태 가드 |

---

## E. 불변조건 (Cross-Domain)

### E1. 테넌트 격리 (절대 불변)

- 모든 상태 변경 시 `obj.tenant_id == request.tenant.id` 검증 필수
- 상태 전이 실패 시 다른 테넌트/사용자/객체로 fallback 절대 금지
- 워커에서 tenant_id는 job/submission 생성 시 고정, 런타임 변경 불가

### E2. 종단 상태 보호

- 종단 상태에 도달한 객체는 상태 변경 불가:
  - Submission: DONE, SUPERSEDED
  - ExamResult: FINAL
  - VideoTranscodeJob: SUCCEEDED, FAILED, DEAD, CANCELLED
  - ExamAttempt: done
  - VideoPlaybackSession: ENDED, REVOKED, EXPIRED

### E3. 원자성

- 모든 상태 전이는 `@transaction.atomic` 내에서 수행
- 동시 변이 가능성이 있는 객체는 `select_for_update` 사용

### E4. 운영 데이터 보호

- Tenant 1 외의 모든 테넌트 데이터는 운영 데이터
- 상태 변이 실패 시에도 데이터 삭제/초기화 금지

---

## F. 미검증 리스크

### F1. Submission EXTRACTING producer adoption

- STATUS_FLOW에 정의되어 있지만 코드에서 설정되지 않음
- AI/OMR 파이프라인이 이 상태를 건너뛰고 DISPATCHED → ANSWERS_READY로 직행
- **리스크:** 낮음. stuck recovery / cascade discard는 EXTRACTING row를 처리할 수 있다.
- **후속:** 새 워커 단계가 EXTRACTING을 명시적으로 쓰기 시작하면 dispatcher/worker entry에서 lifecycle facade로 진입시킨다.

### F2. Submission SUPERSEDED producer coverage

- STATUS_FLOW는 DONE → SUPERSEDED를 허용한다.
- 학생 재응시 / 중복 OMR 채택 등 producer가 이 전이를 사용한다.
- **리스크:** 낮음. 새 supersede producer가 추가되면 반드시 `supersede_submission()` 또는 `supersede_done_submissions()`를 사용한다.

### F3. Video 관리 명령어 SFU 부재

- `enqueue_uploaded_videos`, `recover_stuck_videos`, `force_complete_videos`가 `select_for_update` 없이 상태 변경
- 워커와 동시 실행 시 레이스 컨디션 가능
- **리스크:** 낮음 (cron 간격이 워커 처리 시간보다 길어 실제 충돌 확률 낮음)

### F4. Billing 세금계산서 외부 발행 연동

- Invoice/PaymentTransaction 런타임 상태 전이와 Toss 결제는 구현됨.
- 계좌이체 고객은 사업자 정보를 저장하고 발행을 요청할 수 있다. 입금 확인 전
  `REQUESTED`, 확인 후 `READY`, 운영자가 홈택스에서 실제 발행하고 국세청
  승인번호를 기록하면 `ISSUED`가 된다.
- `TaxInvoiceIssue`의 외부 전자세금계산서 자동 발행 provider 연동은 별도
  범위다. `READY`는 국세청 발행 완료를 의미하지 않는다.
- 카드 자동결제와 전자세금계산서 발행을 같은 provider 기능으로 간주하지 않는다.
  발행사, `청구`/`영수` 시점, 카드 증빙과의 중복 정책을 오너와 세무대리인이
  확정한 뒤 `INVOICE_REQUEST` 경로에만 provider 전송을 붙인다.
- Toss `BILLING_DELETED` 웹훅은 원문 빌링키를 저장·로그하지 않고 SHA-256
  fingerprint로 활성 키를 찾아 `is_active=false`로 전이한다. 이후 배치는
  해당 키로 결제하지 않으며 원장이 새 카드를 등록해야 한다.

### F5. Subscription 배치 운영 의존성

- `process_billing`이 active → grace → expired 자동 전이를 수행하고,
  `sync_subscription`이 누락을 복구한다.
- **잔여 운영 리스크:** EventBridge/SSM 배치가 모두 중단되면 DB 상태 표시는
  늦어질 수 있다. 접근 판정은 날짜를 함께 검사해 과거 active를 허용하지 않는다.

### F6. ExamResult DRAFT 노출 가능성

- Student API가 DRAFT 결과를 필터링하는지 확인 필요
- 만약 필터링하지 않으면 채점 중인 미확정 결과가 학생에게 노출될 수 있음

### F7. Homework/Exam status DRAFT 레거시

- DRAFT 상태가 "레거시"로 표기되어 있지만 enum에서 제거되지 않음
- 새로 생성되는 시험/숙제는 OPEN이 기본이므로 DRAFT가 불필요
- **리스크:** 없음 (하위 호환성), 정리 권장

### F8. Clinic Submission vs Domains Submission 혼동

- `apps/domains/clinic/models.py`에 독립적인 Submission 모델이 존재 (pending/passed/failed)
- `apps/domains/submissions/models/submission.py`의 Submission과 이름이 동일하지만 완전히 다른 모델
- **리스크:** 코드 가독성. 네이밍 정리 권장 (ClinicSubmission 등)
