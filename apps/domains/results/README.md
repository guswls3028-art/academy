Results Domain SSOT
===================

이 문서는 `apps/domains/results/`의 현재 책임 경계를 설명한다.
코드와 문서가 충돌하면 변경자는 먼저 실제 코드 경로를 재측정하고,
문서를 갱신한 뒤 코드를 수정한다.

Scope
-----

`results` 도메인은 학원 시험(Exam) 결과와, 증빙 확인이 필요한 학생 제출 외부 성적을 소유한다.

- `Enrollment`가 결과의 주체다. `Result`는 `student_id`가 아니라 `enrollment_id`를 키로 사용한다.
- 숙제 결과는 `apps/domains/homework_results/`가 소유한다.
- 진척/클리닉 화면처럼 시험과 숙제를 함께 보여줘야 하는 경우는 각 도메인의 결과를 읽어 조합한다.
- 시험지/문항/정답 정의는 `apps/domains/exams/`가 소유한다.
- 답안 원본은 `apps/domains/submissions/`가 소유한다.

Canonical Records
-----------------

운영 화면과 통계의 기준은 아래 네 모델이다.

- `ExamAttempt`: 시험 시도, 대표 시도, 재응시 상태
- `ResultFact`: append-only 원시 이벤트
- `ResultItem`: 문항 단위 채점 스냅샷
- `Result`: 학생/학부모/관리자 화면의 대표 결과 스냅샷

`ExamResult`는 SSOT가 아니다. 이 모델은 과거 `Submission` OneToOne 채점 계약,
임시 점수 상태 확인, 오래된 API 호환을 위해 유지하는 legacy compatibility
snapshot이다. 신규 기능은 `Result` 계열 모델을 기준으로 작성한다.

Student-submitted School / Mock Scores
--------------------------------------

`StudentReportedScore`는 학생이 성적표 원본과 함께 자발적으로 제출한 학교 내신·모의고사
성적의 검수 상태를 소유한다. 학원 시험 `Result`와는 별도 기록이며 서로 대체하지 않는다.

- 학교 내신은 학년도·1/2학기 아래 1차(중간)·2차(기말) 지필평가와 수행평가,
  학교별 기타 평가를 함께 수용한다. 수행/기타 평가는 성적표 기재 시험명과 시험일을
  필수로 저장해 학교별 3차 지필평가 같은 실제 명칭을 잃지 않는다.
- 한 `StudentReportedScore`는 한 과목의 성적이지만, 한 `InventoryFile` 원본에 최대
  20과목을 연결해 학생은 성적표 한 장을 한 번만 올린다. 생성·묶음 검수는 원자적으로
  처리하고 과목별 상승선은 `subject_summaries`에서 계속 분리한다.
- 학교 성적은 기존 9등급과 2025학년도 고1부터 적용되는 5등급을 구분하며,
  성취도(A~E), 과목 평균, 표준편차, 수강자 수도 원본에 표시된 경우 보존한다.
- 모의고사는 교육청 전국연합학력평가와 평가원 수능 모의평가를 구분한다. 시행 월은
  1~12월에서 성적표에 적힌 값을 저장한다. 평가원 하반기 모의평가 월처럼 공식 일정이
  학년도별로 바뀔 수 있으므로 6월·9월 같은 고정 allowlist를 계약으로 두지 않는다.
- 학생 입력값은 `pending`으로 생성되며, 교직원이 원본을 확인해 `verified`로 바꾼
  값만 누적 통계에 포함한다. 등급이 입력된 원본은 관리자가 5/9등급 체계를 명시적으로
  재확인해야 승인된다. `rejected`와 사유 필수 `voided`는 통계에서 제외한다.
- 동일 학생·시험 분류·과목의 정정 제출을 승인하면 기존 승인값은 대체 상태로 반려한다.
  수행·기타 평가는 같은 이름이 반복될 수 있으므로 시험일까지 같을 때만 정정본으로 본다.
- `evidence_file`은 학생 인벤토리 원본과 다대일로 연결한다. `pending`/`verified` 동안은
  단건·재귀 삭제와 덮어쓰기를 차단하며 DB 제약도 활성 상태의 null 증빙을 거부한다.
  `rejected`/`voided` 뒤에는 원본만 명시적으로 삭제할 수 있다. R2 삭제가 실패하면 DB
  연결을 유지하고 502로 재시도를 요구하며, adapter 부재는 503으로 중단한다. 검수 이력이
  있는 원본은 이동·재귀 폴더 삭제·덮어쓰기에서 제외해 반드시 단건 삭제를 거친다. 성적 행·
  검수자·사유는 원본과 분리된 감사 기록으로 남는다.
- 관리자 성적 콘솔은 300ms 지연 검색·강의·학년·출처·과목·득점구간·추세·정렬 조건을
  서버에 전달하고 최대 100명 단위 학생 페이지와 20장 단위 검토 큐 페이지를 사용한다.
  동일 조건의 1분 자동 갱신은 관련 tenant 데이터의 건수·최종 수정 시각이 같을 때만
  5분 버전 캐시를 사용하며, 새 시험·승인·학생 변경 시 즉시 새 키로 재계산한다.
- 업로드·검수·성적 콘솔 조합은 cross-domain support인
  `apps/support/results/student_reported_scores.py`와
  `apps/support/results/student_performance_console.py`가 담당한다.
- 운영 왕복 검증은 `scripts/post_deploy_smoke/reported_score_chain.py --cleanup-remote`로
  제출→묶음 승인→차트 반영→통계 제외→증빙 삭제→UUID가 붙은 `[E2E-*]` 감사행 정리까지
  수행한다. 업로드 응답이 불명확해도 exact marker 기반 `--recover-active` 정리가 실행된다.

Scoring Flow
------------

기본 OMR 채점 흐름은 다음 순서를 따른다.

1. `apps/domains/submissions/services/grading_dispatcher.py`
2. `apps/domains/results/services/grading_service.py::grade_submission`
3. `apps/domains/results/services/exam_grading_service.py::auto_grade_objective`
4. `apps/domains/results/services/sync_result_from_submission.py::sync_result_from_exam_submission`
5. progress dispatch

`ExamGradingService`는 legacy `ExamResult` 객관식 스냅샷만 만든다.
학생/관리자 화면에 노출되는 대표 결과는 `sync_result_from_exam_submission`
단계에서 `Result` / `ResultItem`으로 동기화된다.

Wrong-note PDF / HWPX
---------------------

- 오답노트의 조회 기준은 append-only `ResultFact`가 아니라 현재 대표 결과의
  `ResultItem(is_correct=False)`이다. 재채점·대표 시도 변경 뒤 이미 맞힌 문항을
  과거 오답 이벤트 때문에 다시 노출하지 않는다.
- 교직원은 성적 상세의 **오답노트 만들기**에서 현재 시험 또는 수강 강의 전체를
  선택한다. 누적 범위는 정규 회차 순서로 묶고 PDF 또는 HWPX를 고른다. 앞쪽
  문제지는 정답을 노출하지 않고 뒤쪽 `정답 및 해설`에 교사 원본 해설을 싣는다.
- 문제 이미지는 시험 설정의 답안 등록 → 이미지 등록에서 `ExamQuestion.image_key`로
  저장한다. 해설 이미지는 `QuestionExplanation.image_key`로 분리해 유지한다.
- `POST /results/wrong-notes/documents/`는 tenant 범위의 `WrongNotePDF`와 AI job을
  transaction에서 기록한 뒤 tools worker 큐에 발행한다. 발행 성공은
  `202 PENDING`, 발행 실패는 두 job을 `FAILED`로 닫고 `503`을 반환한다.
  worker가 선택한 PDF/HWPX를 R2에 저장하고 callback이 `PENDING|RUNNING`에서만
  `DONE` 또는 `FAILED`를 원자적으로 확정한다. stale retry로 닫힌 이전 job과 이미
  종결된 job은 늦거나 중복된 callback에도 상태·파일 경로가 변하지 않는다.
  상태 API는 형식·파일명에 맞는 attachment presigned URL을 반환하고 기존 PDF
  경로는 호환 별칭으로 유지한다.
  운영 형태의 positive/retry/duplicate/tenant 회귀는
  `scripts/v1/run-wrong-note-development-canary.ps1`이 격리 development의 실제
  HTTP·Tools SQS worker·R2 download/reload와 cleanup 0으로 검증한다.
- 조회·생성·다운로드는 교직원 전용이다. 한 학원에서 한 번에 한 문서만 만들고,
  생성은 최대 100문항·90초로 제한한다. 현재 범위는 단일 시험 또는
  `lecture_id + from_session_order + 선택적 to_session_order`이며 양끝을 포함한다.
  범위를 넘으면 현재 시험으로 좁혀 다시 만든다.
- R2 이미지는 10MB·2천만 픽셀 상한과 제한 읽기/타임아웃을 적용하고 한 장씩
  처리한다. 학생 영구 삭제는 진행 중인 PDF가 있으면 중단하며, 저장된 PDF 객체를
  먼저 제거한 뒤 삭제를 계속한다.

Excel Result Import
-------------------

- `GET /api/v1/results/admin/exams/{exam_id}/result-import/template/`: 응시 대상 학생과 실제 문항 번호가 채워진 `.xlsx` 양식
- `POST /api/v1/results/admin/exams/{exam_id}/result-import/`: 업로드 파일 미리검증
- 같은 POST에 `apply=true`: 미리검증과 동일한 계약으로 원자적 반영
- 정답은 빈칸/`O`, 오답은 `X`로 표시한다. 전 문항이 공란인 행은 만점과 결시를 구분할 수 없으므로 `응시 여부` 열에서 `응시` 또는 `결시`를 명시해야 하며, 선택 전에는 미리보기와 반영을 차단한다. 기존 양식은 정답 문항 하나를 `O`로 표시해 만점을 확인할 수도 있다.
- 결시는 대표 `ExamAttempt.meta.status=NOT_SUBMITTED`로 저장한다. 상세 인원 기록에는 남기되 점수·석차·문항/평균·합불 통계에 0점으로 넣지 않는다. 현재 대표 결과 기준 누적 미응시 횟수는 이후 전용 양식과 성적 화면의 학생 이름 음영으로 이어지며, 정상 점수를 다시 반영하면 해당 시험의 결시 상태가 해제된다.
- 학생은 `수강등록ID` 우선, 없으면 같은 시험 roster 안에서 연락처와 이름으로만 확정한다.
- 저장 결과는 `Result`/`ResultItem` 최신 snapshot과 변경 문항의 append-only `ResultFact(source=excel_import)`에 기록한다.

OMR Score Shape
---------------

OMR 배점 구조의 SSOT는 `apps/support/omr/score_shape.py`다.

- 객관식과 실제 서술형 배점은 `ExamQuestion.score`와 sheet/template 구조로 계산한다.
- 0점 서술형은 장식용 서술형으로 취급한다.
- 장식용 서술형은 OMR 레이아웃에는 남을 수 있지만 `Result.max_score`와 채점 분모에는 들어가지 않는다.
- 20문항 객관식 + 5문항 장식용 서술형 시험지는 objective max 100, subjective max 0으로 계산되어야 한다.

Manual Scoring
--------------

현행 수동 성적 입력은 view/service 단위로 나뉜 관리자 API가 처리한다.

- `admin_exam_total_score_view.py`
- `admin_exam_objective_score_view.py`
- `admin_exam_subjective_score_view.py`
- `admin_exam_item_score_view.py`

죽은 legacy serializer/service override 경로는 사용하지 않는다. 수동 입력은 반드시
`Result`, `ResultItem`, `ExamAttempt.meta`를 일관되게 갱신해야 하며,
objective + subjective 합산과 문항별 만점 검증을 깨면 안 된다.

직접 채점표는 `GET/POST /results/admin/exams/{exam_id}/manual-grading/`의
조회 → 미리보기(`apply=false`) → 원자적 확정(`apply=true`) 순서를 사용한다.
화면의 **전원 결시로 설정**은 서버 쓰기 없이 현재 초안의 응시 상태만 바꾸며,
제출한 학생을 다시 응시로 전환해 정오를 입력한 뒤 기존 미리보기·확정 절차를
거친다. 결시 확정은 `ExamAttempt.meta.status=NOT_SUBMITTED`로 저장하고 문항
결과를 제거한다. 결시 행은 상세 기록에는 남지만 점수·석차·백분위·응시자 평균
및 추이 집계에서는 제외하며, 조회 응답 조립 단계에서도 이 값을 다시 차단한다.
합산 점수 PATCH와 직접 채점표 확정은 같은 전이를 적용한다. 대표 `Result`의
`total_score`와 `objective_score`를 0으로 맞추고 `ResultItem`을 원자적으로 제거하며,
기존 실패 `ClinicLink`는 물리 삭제나 통과 처리 대신 감사 가능한
`resolution_type=NOT_SUBMITTED`로 닫는다. 연결된 오늘 계획 선택은 해제하지만 이미
예약·등원·완료된 클리닉 참여 사실은 보존한다. 동일 결시 요청은 성공하는 no-op이고
감사 사실을 중복 생성하지 않으며, 이후 명시적 점수 입력은 결시 표식을 해제하고
새 점수에 맞는 클리닉 판정을 다시 만들 수 있다. 결시 전환 자체는 알림을 보내지 않는다.

클리닉 운영 목록의 `GET /results/admin/clinic-targets/`는 점수 미달과
`NOT_SUBMITTED`를 구분한다. 과제 미제출은 기존 source-specific `ClinicLink`를
`reason=missing`, `homework_score`, `homework_cutline`, `meta_status`와 함께
반환한다. 시험 미응시는 점수 실패가 아니므로 조회가 미해결 `ClinicLink`를
자동 생성하지 않는다. 대신 같은 tenant의 활성 수강, 실제 차시 roster, 시험 연결,
가장 최근 `Result`의 명시적 `ExamAttempt.meta.status=NOT_SUBMITTED`가 모두 일치할 때만
`clinic_link_id=null`인 판정 대기 행으로 투영한다. 단순히 점수가 없다는 이유로
미응시를 추정하지 않으며, 이후 채점 결과가 생기면 과거 미응시 표식은 다시
대상으로 노출하거나 면제할 수 없다.

결석 등으로 면제할 때는
`POST /results/admin/clinic-targets/waive-missing/`에 정확한 차시·수강·시험 ID와
2~500자의 사유를 보낸다. 서버는 위 미응시 조건을 다시 잠금·검증하고 그때만
source-specific `ClinicLink`를 만든 뒤 `WAIVED`로 해소한다. 반복 요청은 같은
이력을 반환하고 다른 tenant·roster·정상 점수는 실패 폐쇄한다.
`include_resolved=true`는 해소 이력을 함께 반환하며 기본 조회는 현재 미해결과
판정 대기만 반환한다.

관리자 대상 응답은 각 `ClinicLink`의 `resolution_evidence`, append-only
`resolution_history`, `linked_bookings`를 함께 투영한다. `linked_bookings`는 활성
`SessionParticipantPlanItem` FK만 권위 연결로 사용하며 이름·날짜·시험명으로
참가자를 추정하지 않는다. 각 항목은 같은 tenant·같은 학생·일치하거나 비어 있는
수강 관계를 다시 확인한 뒤 참가자/클리닉 세션 ID, 세션 날짜·시작·종료·장소,
참가 상태, 학생 작성 `student_request_memo`, 교직원 `staff_memo`, 선택 provenance를
반환한다. 작성 출처가 불명확한 legacy `SessionParticipant.memo`는 읽거나 반환하지
않는다. tenant context가 없는 `GET /results/admin/clinic-targets/`는 빈 성공 대신
`403 {"detail":"Tenant required","code":"TENANT_REQUIRED"}`로 실패 폐쇄한다.
권한 없는 사용자도 403이며 GET은 링크·계획·해소 이력을 변경하지 않는다. 집중
회귀는 `apps/domains/results/tests/test_admin_clinic_targets_contract.py`가 정확한
연결만 포함하고 교차 tenant/corrupt 연결과 legacy memo를 제외하는지 함께 검증한다.

클리닉 읽기 경계는 `ClinicLink.tenant`만 신뢰하지 않는다. 링크의 enrollment,
student, enrollment lecture, session lecture가 모두 요청 tenant에 속해야 하며,
다형 `source_type/source_id`가 현재 다른 tenant의 시험·과제를 가리키면 현재 목록과
해소 이력에서 모두 제외한다. 삭제된 같은 tenant 원본의 해소 이력은 보존한다.
일반 `ClinicLinkViewSet`, 관리자 대상 목록, 차시 대상 ID와 하이라이트 projection이
같은 관계 검증으로 실패 폐쇄한다. 회귀 검증은
`apps/domains/clinic/tests.py::MultiTenantIsolationTest::test_clinic_target_service_rejects_mismatched_link_relations`가
손상된 link tenant, enrollment graph와 resolved source ID를 함께 확인하고,
`apps/domains/progress/tests/test_drift_and_resolution.py::DriftResolutionTest::test_clinic_link_viewset_rejects_mismatched_enrollment_tenant`가
일반 API 경계를 확인한다.

학생 이름의 `name_highlight_clinic_target`은 별도 출석 플래그가 아니라 학생
패스카드의 `CLINIC_REQUIRED` 상태를 관리자 화면에 투영한 값이다. 현재 미해결
과락 링크가 있어도 오늘·미래 `booked` 또는 `completed_at`이 없는 `attended`가
학생에게 하나라도 있으면 패스카드와 모든 강의의 노란 하이라이트가 함께
`BOOKING_CONFIRMED`/`false`가 된다. `pending`은 하이라이트를 끄지 않는다.
클리닉 수강완료 뒤 링크가 남아 있으면 즉시 `CLINIC_REQUIRED`/`true`로 돌아가며,
모든 링크가 해소되면 `PASSED`/`false`다. 현재 대상 링크의 원본·완료 진행 상태와
예약 판정은 패스카드, 대상자 목록, 성적표, 참가자 직렬화가 같은 공용 계산을 쓴다.
`pending`/`booked` 행도 `completed_at`이 남아 있으면 새 예약이 아니므로 패스카드를
보호하지 않는다. 따라서 완료 후 출석 상태만 `booked`로 정정해도
`BOOKING_CONFIRMED`/`can_leave=true`로 되돌아가지 않는다. `linked_bookings`는 취소·
거절 참가자를 방어적으로 제외하며, 일정 변경 service가 유효한 오늘 계획을 새 참가자
행으로 옮긴 뒤에만 새 예약을 권위 연결로 반환한다.

Session Assessment Inspection
-----------------------------

차시 성적 드로어의 시험 오답 확인과 과제 검사는 점수 합불을 덮어쓰지 않는 별도
교사 확인 기록이다.

- 조회는 `GET /results/admin/sessions/{session_id}/scores/`, 저장은
  `PATCH /results/admin/sessions/{session_id}/score-correction/`가 담당한다.
- 시험 저장은 조회와 같은 최신 대표 `Result`를 선택하고 그 `Result` 행만 잠근다.
  nullable `Result.attempt`를 `select_related()`한 채 `FOR UPDATE`하지 않는다.
  PostgreSQL은 nullable outer join의 반대편 잠금을 거부하기 때문이다.
- 과제는 종이 검사처럼 점수가 없어도 `PENDING`/`COMPLETED`와 메모를 저장할 수 있다.
  시험은 유효한 점수와 만점이 있는 비만점 결과에서만 오답 확인 상태를 바꾼다.
- `COMPLETED`는 원점수·`passed`·제출 row를 수정하지 않는다. 대신
  `AssessmentCorrection`을 교사 결정 정본으로 저장하고 source-specific
  `ClinicLink`를 사유·사용자·source fingerprint가 있는 `MANUAL_OVERRIDE`로 해소한다.
  따라서 25점 시험과 점수 없는 과제도 원자료 그대로 재시험/자동 Clinic 대상에서
  제외된다. 해제는 같은 링크를 미해소로 돌려 현재 원자료로 재평가한다.
- 시험의 기본 성적은 `ExamAttempt(attempt_index=1)`에 보존된
  `meta.initial_snapshot`이다. 2차 이상 재시험이 대표 `Result`를 갱신해도 차시 성적표,
  학생·학부모 성적표와 상세, 관리자 학생 상담, 시험/차시/강의 요약 및 운영 분석은
  1차 점수·만점·미응시 상태를 사용한다. 재시험 점수는 같은 원시험의 `attempts` 보충
  이력과 최종 보완 성취에만 사용하며 1차 평균·석차·추이·합불을 덮어쓰지 않는다.
- 2차 이상 시도가 최신 `ResultItem`을 덮어 1차 문항 상세를 확실히 복원할 수 없으면,
  1차 원점수 옆에 재시험 문항을 섞지 않고 문항 상세를 비운다. 재시험 점수와 차수는
  `ExamAttempt` 이력에서 별도로 표시한다.
- 첫 시도 스냅샷이 없는 모델 도입 전 데이터는 현재 `Result`로 실패 폐쇄 호환한다.
  별도 `Exam`으로 이미 만든 과거 재시험은 원시험과 안전하게 연결할 식별자가 없으므로
  자동 합치거나 삭제하지 않고 독립 시험 기록으로 유지한다. 이후 운영은 원시험의
  재시험 이력을 사용한다.
- 완료 저장은 2자 이상의 사유와 `expected_updated_at`을 요구한다. 다른 탭이 먼저
  저장한 경우 `409 ASSESSMENT_CORRECTION_CONFLICT`와 최신 시각을 반환하며, 일반
  학생·학부모와 다른 tenant/roster/source는 실패 폐쇄한다. `EXAM_PASS`와
  `HOMEWORK_PASS` 같은 사실 근거는 교사 토글보다 강하므로 해제하지 않는다.
- `COMPLETED` 당시 시험의 대표 결과·대표 시도·점수·문항별 답안/정오/배점을
  SHA-256 내용 지문으로 저장한다. 같은 값을 다시 동기화해 `updated_at`만 바뀐
  경우에는 완료를 유지하고, 실제 점수나 답안 내용이 바뀐 경우에만 조회 시
  `PENDING`으로 돌려 교사가 변경된 결과를 재확인하게 한다. 지문 도입 전의 기존
  완료 기록은 교사 입력을 보존해 완료로 읽고, 다음 수동 저장부터 지문을 기록한다.
  내용이 바뀐 시험의 stale `MANUAL_OVERRIDE`는 성취도/진행 파이프라인에서도 제외하고
  링크를 다시 열어 원점수 기준 Clinic 판정을 복원한다.
- tenant 차시 roster 밖 학생, 다른 차시 평가, 만점 시험의 수동 변경은 실패 폐쇄한다.
- 성적표와 세션 요약은 여러 시험의 대표 `Result`를
  `(target_id, enrollment_id)` 단위로 한 번에 선택한다. 시험 열 수만큼 대표 결과
  쿼리를 반복하지 않으며, 세션 점수·시험별 요약·재시험 통계는 성적표와 같은
  출석 우선 차시 roster만 집계한다. 같은 테넌트·강의 학생이라도 해당 차시 roster가
  아니면 제외한다. 세션 참여자·통과율은 tenant가 일치하는 `SessionProgress`, 클리닉
  비율은 그 참여자와 교집합인 미해결 자동 `ClinicLink`를 기준으로 한다. 손상된
  교차 테넌트 FK는 어느 모수에도 섞지 않는다.
- 시험별 요약에서 `pass_score <= 0`은 합격 기준 미설정이다. 점수가 있어도 합격이나
  불합격으로 세지 않으며 두 건수와 합격률을 0으로 유지한다.
- 하나의 시험이 여러 강의 차시에 연결되면 결과의 `Enrollment.lecture_id`와
  `ExamLecturePolicy`를 결합해 강의별 유효 합격·귀가 기준을 적용한다. 정책이 없는
  기존 연결만 `Exam.pass_score`를 상속한다. 시험 결과·요약·문항 통계의 선택
  `lecture_id`는 시험에 실제 연결된 요청 tenant 강의만 허용하며, 강의별 석차 코호트도
  그 강의의 결과로 제한한다. 전체 조회는 연결 강의 결과를 합치되 행별 기준을 섞지 않는다.
- 대표 시도가 `NOT_SUBMITTED`인 결과는 보관하되 점수 평균·최저·최고와 시험별
  합격·불합격 건수에서 제외한다. 손상 데이터에 점수가 남아 있어도 결시를 0점이나
  유효 점수로 되살리지 않는다.
- 시험별 합격률의 분모는 결시를 포함한 `participant_count`가 아니라 실제 채점 결과가
  있는 합격·불합격 인원이다. 결시는 참여 이력에는 남지만 합격률을 낮추지 않는다.
- 관리자 시험 요약은 요청 테넌트의 최신 결과만 조회한다. 채점 중·실패 시도는 참여
  이력에는 남겨도 평균·최저·최고·합불 집계에 넣지 않으며, 시도 모델 도입 전의
  attempt 없는 레거시 결과만 완료 결과로 호환한다.
- 회귀 검증은
  `apps/domains/results/tests/test_session_scores_roster_scope.py`와
  `test_assessment_lifecycle_ssot.py`가 저장·재열기·메모·roster, 잠금 SQL의 nullable
  outer join 부재, 다중 시험 단일 대표 결과 조회, 차시 roster·테넌트 집계 경계와
  합격 기준 미설정 처리를 함께 확인한다.

학생 카드의 상태 projection과 학원별 성장 그래프 구성은
`docs/domain/student-grade-report.md`가 소유한다.

Aggregation
-----------

집계/해석은 `apps/domains/results/aggregations/` 또는 명시된 BFF view에서만 수행한다.
모델, serializer, 단순 CRUD view에 새로운 집계 로직을 넣지 않는다.

운영 성적 분석의 canonical BFF는
`apps/support/results/enterprise_analytics.py`다.
이 서비스는 `Result`, `ResultFact`, `ResultItem`, `Submission`을 함께 읽어
성적 분포, 기간별 추이, 수동 성적 입력, 자동채점 사용량을 tenant scope 안에서 집계한다.
성적 분포·평균·합격률과 월별 점수 추이는 1차 점수 projection을 사용하고, 2차 이상
재시험은 시도/보완 이력으로만 남긴다.
`[E2E-*]`, `LOCAL-DEMO`, `DEMO-*` 구조화 prefix로 식별되는 합성 시험은 기본 분석에서 제외한다.
`주간 테스트`, `Level Test`처럼 실제 운영에서 쓰는 일반 시험명은 분석에 포함한다.
노출 엔드포인트는 교사용 `GET /results/admin/analytics/`,
학생/학부모용 `GET /student/grades/analytics/`이며, 학생/학부모는 선택된 학생 1명만 조회한다.
학생/학부모 분석의 `date_range.days`는 시험 수·득점률·합격률·미응시·오답·과제
지표 전체에 동일하게 적용되며, 기간을 판정할 기록 시각이 없는 행은 기간 분석에서 제외한다.

학생별 누적 시험 추이는 관리자·선생 공용 BFF
`GET /results/admin/student-grades/?student_id=<id>`와 학생·학부모 공용 BFF
`GET /student/grades/`의 동일한 `exam_trend`, `exam_summary` 계약을 사용한다.
학생·학부모 응답은 `get_request_student`가 확정한 본인/선택 자녀의 활성 수강만
대상으로 하며, 잘못된 자녀 헤더는 다른 자녀로 fallback하지 않는다. 한 점은 동일
시험의 재응시가 아니라 서로 다른 정규
시험의 대표 `Result` 한 건이다. 유효한 점수가 입력된 시험만 정규 차시 날짜·순서 기준으로
`1회차..N회차`가 자동 부여되며, 만점이 다른 시험은 `score_pct`로 정규화한다.
`NOT_SUBMITTED`는 목록에는 남지만 0점으로 바꾸지 않고 추이·평균 분모에서 제외한다.
보관된 정규 시험 결과는 이력에 유지하고 `archived=true`로 구분한다.
여러 강의에 연결된 시험은 해당 `Result.enrollment`의 강의 차시만 사용하고 시스템 강의는
제외한다. 음수·비유한 점수나 0 이하 만점은 추이에서 제외하며 가산점에 따른 100% 초과는 유지한다.

학생·학부모 BFF는 `Exam.student_results_published=true`인 결과만 목록·추이·요약·분석에
포함한다. 비공개 시험도 교직원 BFF, 채점 기록, 통계 원본에는 유지한다. 개별 학생 결과
조회는 비공개일 때 점수·문항·석차를 반환하지 않고 서버가 계산한 재응시 가능 여부만
제한 응답으로 내려 중복 제출을 막는다.

주의할 예외:

- `session_scores_view.py`는 실사용 BFF라서 시험 결과와 숙제 제출 상태를 함께 읽을 수 있다.
  이 경우에도 숙제 결과를 results 도메인이 소유한다는 뜻은 아니다.

Change Rules
------------

- 신규 채점 규칙은 먼저 OMR score shape와 Result 동기화 경로에 반영한다.
- `ExamResult`에 새 기능을 추가하지 않는다. 호환 때문에 유지할 뿐이다.
- 레거시 import나 죽은 serializer/view/service를 되살리지 않는다.
- 배점, tenant scope, submission scope, representative attempt, manual score 합산을 바꾸는 변경은 focused test와 운영 검증 대상이다.
- "이미 완성" 같은 선언보다 재현 가능한 검증 결과를 우선한다.
