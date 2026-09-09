# 과제 만점·합격 정책·성적 저장 — SSOT

## 목적과 사용자 흐름

원장·선생님·채점 권한이 있는 직원이 차시마다 여러 과제를 만들고, 과제별
문제 수에 맞는 만점과 과제별 합격 기준을 설정한 뒤 학생 점수를 입력하는
현재 계약이다.

1. **강의 → 차시 → 과제**에서 과제를 만들고 `점수형` 또는 `완료형` 채점
   방식을 고른다. 점수형은 과제별 만점을 지정한다.
2. 각 과제의 **과제별 합격 기준**에서 퍼센트 또는 원점수 커트라인을 지정한다.
3. **차시 → 성적**에서 학생별 점수를 입력한다. 성적표 분모와 합격률 계산은
   해당 과제의 만점을 사용한다.
4. 만점이나 정책이 바뀌면 기존 1차 점수의 합격·클리닉 판정을 다시 계산한다.
5. 워크북형 과제는 **자산**에서 문제+해설 통합 파일, 문제 파일, 또는 문제·해설
   두 파일을 등록하고 문항·원본 해설을 검수한 뒤 **결과**에서 학생별 O/X/복습을
   기록한다.

프런트 화면 계약은
[frontend/docs/HOMEWORK-SCORING.md](https://github.com/guswls3028-art/academy-frontend/blob/main/docs/HOMEWORK-SCORING.md)가
소유한다.

## 두 설정의 소유 범위

| 설정 | 범위 | 저장·응답 계약 |
|------|------|----------------|
| 채점 방식 | 과제별 | `Homework.grading_mode`: `SCORE` 또는 `COMPLETION` |
| 과제 만점 | 과제별 | `Homework.meta.default_max_score`, API의 `max_score` |
| 합격 기준 | 과제별 | `Homework.cutline_mode`, `cutline_value`, `round_unit_percent` |
| 기존 기본 정책 | 차시 공통 fallback | `HomeworkPolicy`; 개별 기준이 없는 기존 과제만 사용 |

과제 만점은 과제마다 다를 수 있다. 과제 생성·복사·설정 변경과 성적표 조회는
모두 API의 `max_score`를 사용하며, 값이 없는 기존 과제만 100점을 호환
기본값으로 읽는다. 클라이언트가 점수 저장 요청에 다른 `max_score`를 보내거나
생략해도 서버는 `Homework`의 현재 만점을 정본으로 사용한다.

`HomeworkScore.max_score`는 점수 입력 시점의 판정 스냅샷이다. 과제 만점을
바꾸거나 같은 만점을 명시적으로 다시 저장하면 현재 1차 점수 스냅샷과
`passed`, `clinic_required`, `ClinicLink`를 함께 동기화한다. 학생이 입력한
점수와 재시도 이력은 보존한다. 정책을 저장할 때도 각 과제의 현재 만점을
스냅샷에 먼저 반영한 뒤 판정과 링크를 한 transaction에서 갱신한다.
PostgreSQL에서는 `HomeworkScore` 본체 행만 잠그고, 선택적인 차시 정책 JOIN은
판정 입력으로 읽기만 한다. 따라서 정책이 없는 차시도 outer join 잠금 오류 없이
동일한 재계산·클리닉 동기화 계약을 따른다. 과제 부분 수정도 `Homework` 본체만
잠가 nullable `source_exam`·`sheet` 조회 조인을 잠금 대상에서 제외한다.

## 점수형과 완료형

- `SCORE`는 기존 계약이다. `0/30`처럼 점수 또는 수행 개수를 저장하고 과제별
  만점·커트라인으로 합격을 계산한다. 기존 과제와 템플릿은 기본적으로 이 방식이다.
- `COMPLETION`은 완료/미완료만 기록한다. 서버 저장값은 완료 `1`, 미완료 `0`,
  미입력 `null`이며 만점·원점수 기준을 모두 `1`로 정규화한다. 다른 수치나
  클라이언트가 보낸 임의 만점은 `400`으로 거부한다.
- 완료형도 별도 상태 테이블을 만들지 않는다. 기존 `HomeworkScore.passed`,
  `clinic_required`와 `ClinicLink` 동기화를 그대로 사용해 학생 상세, 성적표,
  클리닉 대상이 같은 판정을 읽는다.
- 관리자 클리닉 대상 목록도 링크를 만든 과제의 유효 합격 기준을 그대로 읽는다.
  과제별 기준이 있으면 차시 기본값보다 우선하며 `COUNT`는 원점수, `PERCENT`는
  퍼센트 값과 해당 과제 만점으로 환산한 표시 기준을 함께 반환한다. 시험 점수
  호환 필드에는 과제 값을 복제하지 않아 화면이 같은 과제를 시험·과제로 두 번
  설명하지 않는다.
- 학생 상세의 과제 목록은 **배정 행을 기준**으로 만들고 기존 점수 행을 합친다.
  따라서 아직 `HomeworkScore`가 없는 과제도 빠지지 않는다. 배정 뒤 제출 행과
  점수가 모두 없거나 명시적으로 `meta.status=NOT_SUBMITTED`가 기록된 결과는
  `미제출`, 제출 행은 있지만 점수가 없는 결과는 `검사 전`으로 구분한다.
  제출 여부는 tenant·enrollment·homework가 모두 일치하는 `Submission`을 한 번에
  조회하며 generic target id만으로 다른 tenant의 제출을 인정하지 않는다.
  삭제·시스템 과제와
  다른 tenant·다른 학생 배정은 포함하지 않는다. 과거 배정 행 없이 점수만 남은
  정상 legacy 결과는 호환을 위해 계속 표시한다.
- 차시에서 과제를 제거하면 배정과 자동 클리닉 연결만 해제하고 기존 점수·제출
  이력은 감사 목적으로 보존한다. `removed_from_session_at`이 있는 과제는 보존된
  점수 행이 있더라도 관리자 학생 상세와 학생·학부모 성적 요약 같은 현재 성적
  화면에서는 노출하지 않는다. 명시적인 교직원 시도 이력 조회만 보존 이력을
  다룰 수 있으며, 같은 제목의 과제를 다시 만들더라도 현재 화면에 두 건으로
  합쳐지지 않는다.
- 과제 대상자 편집기는 현재 차시의 **활성** 수강생만 후보로 보여 주고 그 후보
  범위만 완전 치환한다. 화면에 보이지 않는 퇴원·비활성 수강생의 기존 배정 행은
  삭제하지 않으므로 과거 제출·점수와의 관계가 끊기지 않는다. 저장은 과제 본행을
  잠근 transaction에서 수행해 같은 과제에 대한 동시 완전 치환이 섞이지 않게 한다.
- 호환용 차시 단위 `HomeworkEnrollment` 편집도 같은 이력 보존 규칙을 따른다.
  `PUT /homework/enrollments/?session_id=`는 현재 활성 후보의 추가·제외 차이만
  반영해 숨은 비활성 행과 변경 없는 행의 생성 시각을 보존한다. 차시 본행을
  `FOR NO KEY UPDATE`로 잠가 동시 완전 치환을 직렬화하되 자식 행 FK 확인과
  교착하지 않으며, 마지막으로 잠금을 얻은 요청을 정본으로 삼는다.
- 관리자 학생 상세는 과제 행에 `session_order`, `session_regular_order`,
  `session_type`, `session_date`, `display_order`를 함께 반환한다. 강의를 교체해
  차시 번호가 다시 1부터 시작해도 응답은
  실제 수업일 최신순으로 정렬한다. 같은 날짜에서는 차시 번호 내림차순, 강의명,
  과제 표시 순서, 과제 ID 내림차순을 안정 동률 기준으로 사용하며 날짜가 없는 과거
  행은 마지막에 둔다. 이 정렬은 점수 수정 시각과 무관하다. 학생·학부모 요약도
  tenant 범위의 `Submission`을 확인해 제출했지만
  아직 채점하지 않은 과제를 `검사 전`으로, 제출 자체가 없는 과제를 `미제출`로
  구분한다.
- 결과 행이 하나라도 생긴 과제는 채점 방식을 바꿀 수 없다. 기존 숫자 점수나
  교사 기록을 새 의미로 재해석하지 않으며, 다른 방식이 필요하면 새 과제를 만든다.
- 템플릿 저장·템플릿 불러오기·다른 차시 복사는 `grading_mode`를 보존한다.

## 정책과 만점의 불변 규칙

새로 만들거나 설정 화면에서 저장한 과제는 각자의 기준을 사용한다. 기존 과제에
개별 기준이 없으면 배포 전과 동일하게 차시 `HomeworkPolicy`를 기본값으로 읽는다.
차시 정책을 바꿔도 이미 개별 기준이 있는 과제는 덮어쓰지 않는다.

과제 수정 API는 요청에 포함된 필드만 바꾼다. 따라서 차시 기본 기준을 쓰는
과제에서 제목이나 제출기한만 수정할 때 `max_score` 또는 커트라인 필드를
보내지 않으면 기본 기준 상속과 기존 판정은 그대로 유지한다. 만점 또는
커트라인 필드가 실제 요청에 포함된 경우에만 해당 동기화·재판정 경로를
실행한다.

- `PERCENT`: 각 과제의 `score / max_score`를 기준으로 판정하므로 만점이
  서로 다른 과제에 권장한다. 퍼센트 반올림 단위는 기존 정책을 따른다.
- `COUNT`: 원점수 커트라인이며 해당 과제의 만점을 초과할 수 없다. 차시 기본
  정책을 사용하는 기존 과제에는 기존의 최저 만점 검증을 유지한다.
- 새 과제의 만점은 그 과제에 적용되는 원점수 커트라인보다 낮을 수 없다.
- 기존 과제의 만점은 그 과제에 적용되는 원점수 커트라인이나 이미 입력된 최고 점수보다
  낮출 수 없다. 검증 실패 시 일부 값이나 판정을 쓰지 않고 `400`으로 거부한다.
- 점수는 0 이상 해당 과제 만점 이하여야 한다. 초과 점수는 `400`으로
  거부하고 기존 점수를 보존한다.
- tenant와 차시 범위는 모든 조회·쓰기에서 필수이며 기본 tenant나
  cross-tenant fallback을 사용하지 않는다.

## 워크북 원본과 문항별 채점

과제는 별도 분리 엔진을 만들지 않고 `Homework.source_exam`이 가리키는 비노출
regular `Exam`을 원본 구조 소유자로 사용한다. 이 시험은 활성 시험 목록이나 학생
성적에 노출하지 않고 세션에도 연결하지 않는다. HWP/HWPX 미주 번호와 원본 그림,
PDF 분리, 교직원 검수·승인 계약은 시험 원본과 동일하다. 특히 미주 전체 이미지를
선생님 해설 정본으로 보존하고, 문제 영역 크롭은 승인 전 문항별로 조정한다.
실행·스크립트·브라우저 실행 형식을 제외한 모든 안전한 원본 형식을 보관하며,
자동 분리 미지원 형식도 PDF 변환을 강제하지 않고 직접 문항·해설 등록으로 이어진다.
원본 시험을 멱등 생성할 때는 `Homework` 본행만 `FOR UPDATE OF`로 잠근다.
nullable `source_exam`·`sheet` 조회 조인은 잠금 대상에서 제외하므로 PostgreSQL에서도
빈 워크북의 첫 원본 생성이 `500`으로 실패하지 않는다.

학생별 문항 표시는 1차 `HomeworkScore.meta.question_marks`에 문항 번호별로 저장한다.
기존 점수, 미제출 상태와 임의 확장 메타는 덮어쓰지 않는다.

| 화면 | `is_correct` | `include_in_wrong_note` | 통합 오답노트 |
|------|--------------|-------------------------|---------------|
| O | true | false | 제외 |
| X | false | true | 포함 |
| O·복습 | true | true | 포함 |
| 미입력 | 키 없음 | 키 없음 | 제외 |

X를 나중에 다시 맞힌 뒤에도 남기려면 O·복습으로 바꾼다. 대상 학생 배정,
워크북 문항 번호와 tenant가 모두 일치해야 전체 변경을 저장하며, 다른 학생·과제
또는 현재 원본에 없는 번호는 `400`으로 거부한다.

## API와 호환 경계

### 학생 사진·동영상 다건 제출

- 학생 과제 하나에는 활성 `Submission`을 정확히 한 행만 유지한다. 사진·동영상은
  순서가 있는 `SubmissionMedia` 자식 행이며, 파일마다 tenant, 서버가 정한 저장
  식별자, 안전한 원본 표시명, 종류·MIME·용량·순서·상태·오류·시각을 보존한다.
  부모는 기존 `homework_image`·`homework_video` source와 active uniqueness를 그대로
  사용하므로 구 API 인스턴스와 겹쳐 실행돼도 두 번째 활성 부모를 만들지 않는다.
  클라이언트가 tenant·사용자·object key를 지정하거나 응답에서 bucket key를 읽는
  경로는 없다.
- 한 과제는 활성 파일 20개, 파일당 100MB, 활성 파일 합계 500MB까지다. JPG/JPEG,
  PNG, GIF, WebP, HEIC/HEIF, AVIF, MP4/M4V/MOV, WebM을 허용하고, 확장자 또는
  브라우저 MIME이 이 중 하나면 원본을 먼저 보존한다.
  휴대폰별 MIME·파일 signature 차이는 제출을 막지 않으며 교사가 원본을 직접
  확인한다. tenant의 활성 학생, 본인 활성 수강, 현재 과제 배정은 모두 맞아야
  목록·업로드·삭제할 수 있고 학부모는 변경할 수 없다.
- 브라우저가 만든 `client_file_id`는 tenant 안에서 유일하며 같은 fingerprint로
  재시도할 때 같은 자식 행과 저장 키를 쓴다. 이미 성공한 동일 fingerprint는 새
  행으로 복제하지 않는다. 한 파일의 object-store 저장이 실패하면 그 행만
  `failed`와 오류 시각을 기록하고 `503`을 반환한다. 같은 묶음의 앞서 성공한
  파일·행은 rollback하지 않는다.
- object 저장 뒤 최종 DB 상태 갱신이 실패해도 object를 추측 삭제하지 않는다. 이미
  커밋된 자식 행의 deterministic 저장 키에 남겨 같은 `client_file_id` 재시도가
  동일 키를 덮어쓰고 상태를 복구하게 한다. 실패 상태 기록도 best-effort로 시도하고
  API는 성공으로 응답하지 않는다. 따라서 DB 장애 중 이름 없는 orphan을 새로 만들지
  않으며 감사 행과 object identity의 연결이 유지된다.
- 학생 업로드·삭제와 교사의 일반 점수 저장·직접 확인 완료는 tenant 안의 canonical
  `HomeworkAssignment` 행을 먼저 잠근다. 공통 잠금 순서는
  `HomeworkAssignment` → `HomeworkScore`/`AssessmentCorrection` 또는
  `Submission`/`SubmissionMedia`다. 학생 쓰기는 assignment 잠금 뒤 현재
  완료/통과 상태를 다시 확인한다. 교사 완료가 먼저 커밋됐으면 같은
  `client_file_id` 재시도를 포함한 upload/delete를 object 저장이나 행 변경 전에
  `409 HOMEWORK_MEDIA_REVIEWED`로 거부한다. 학생 변경이 먼저 잠금을 얻었으면 교사는
  그 transaction이 끝날 때까지 기다린 뒤 확정된 media set만 검수 근거로 기록한다.
- 업로드 transaction은 R2 원본 저장과 최종 `uploaded` 상태 확정까지 assignment
  잠금을 유지한다. object 저장 실패는 transaction 안에서 해당 deterministic 행을
  `failed`로 만든 뒤 transaction을 커밋하고 `503`을 반환하므로 같은
  `client_file_id`와 object key로 재시도할 수 있다. 다른 파일의 앞선 성공은
  rollback하지 않고 object를 추측 삭제하지 않는다.
- 교직원 제출 목록은 학생별 현재 활성 media set의 timestamp 비의존 SHA-256인
  `media_set_fingerprint`를 반환한다. 직접 확인 완료는 같은 assignment 잠금 안에서
  다시 계산한 값을 `AssessmentCorrection.source_fingerprint`에 저장한다. 활성 legacy
  object와 제거되지 않은 child의 identity, 내용 fingerprint, 순서, 종류·MIME·용량,
  저장 상태를 포함하고 soft-remove된 파일은 제외한다.
- 학생 삭제는 행과 object를 즉시 없애지 않고 `removed_at`, `removed_by`,
  `removed` 상태를 기록한다. soft-delete object는 감사·복구 근거로 보존하며, 향후
  정리도 tenant·행·보존기간을 확정한 별도 exact-target 작업에서만 수행한다. 이번
  expand migration은 child table만 만들며 기존 행, constraint, object를 바꾸거나
  지우지 않는다.
- 기존 `homework_image`·`homework_video` 단건 `Submission.file_key`는 그대로
  보존한다. 새 목록에서는 `legacy-{submission_id}`인 파일 하나로 투영하고, soft
  remove는 기존 행의 `meta`에 감사 시각을 기록한다. 구 단건 제출 생성 API도
  계속 동작한다. 새 child가 하나도 성공하지 않았거나 마지막 활성 child가 제거된
  다건 부모는 성적 화면에서 `제출`로 계산하지 않는다.
- 교직원 제출 목록은 자식 파일별 상태·오류를 반환하되 저장 키는 반환하지 않는다.
  미리보기는 같은 tenant의 교직원 또는 소유 학생만 서버가 10분짜리 서명 URL을
  발급하며, 실패·업로드 중·삭제 파일은 준비되지 않은 것으로 거부한다.
- 사진·동영상 숙제 제출은 자동검사 대기함이나 AI 작업으로 보내지 않는다. 부모
  `Submission`은 `submitted` 상태와 원본 파일을 그대로 보존하고, 교사는 과제 상세의
  제출관리에서 직접 미리본 뒤 확인 완료를 기록한다. 공용 제출함은 시험 OMR 처리만
  다루며 기존 숙제 제출 행·파일·점수는 삭제하거나 변환하지 않는다.
- 직접 확인은 기존 `AssessmentCorrection`을 사용해 점수와 독립적으로 저장한다.
  확인 완료·취소는 `expected_updated_at`으로 다른 화면의 판정을 덮어쓰지 않는다.
  학생 파일 잠금과 제출관리의 `teacher_reviewed` 표시는 같은 현재 상태를 사용한다.
  최신 `HomeworkScore.passed=true`이거나 점수 행 없이 직접 확인이 완료된 과제만
  완료/통과로 잠근다. 미입력, `NOT_SUBMITTED`, 미완료/불합격, 이전 통과 뒤 최신
  재시도가 불합격인 과제는 과거 검수 이력과 무관하게 학생이 파일을 추가·삭제해
  보완할 수 있다. 점수 행이 있으면 최신 시도 결과가 직접 확인보다 우선한다.

PostgreSQL barrier 회귀는
`apps/domains/results/tests/test_assessment_correction_concurrency_pg.py`에서 교사 선행
거부와 학생 upload/delete 선행 직렬화를 검증한다. 학생 upload → reload → 교직원
목록/fingerprint 확인 → 직접 완료 → 같은 파일 retry 및 delete 거부 journey는
`apps/domains/submissions/tests/test_homework_media_submission.py`가 검증한다.

| Method | Path | 역할 |
|--------|------|------|
| GET/POST | `/submissions/submissions/homework/{homework_id}/media/` | 본인 파일 목록·파일 하나 업로드. 여러 파일은 독립 요청으로 부분 성공을 보존 |
| DELETE | `/submissions/submissions/homework/{homework_id}/media/{media_id}/` | 미제출·미완료·불합격인 본인 파일 soft remove. 완료/통과 상태는 잠금 |
| GET | `/submissions/submissions/homework/{homework_id}/media/{media_id}/preview/` | 권한 확인 뒤 짧은 미리보기 URL 발급 |
| GET | `/submissions/submissions/homework/{homework_id}/` | 교직원 학생별 제출, ordered `files`, 직접 검수 상태 목록 |
| PATCH | `/results/admin/sessions/{session_id}/score-correction/` | `source_type=homework`인 교사 확인 완료·취소. 점수는 변경하지 않음 |

| Method | Path | 역할 |
|--------|------|------|
| GET/POST | `/homeworks/` | 차시 과제 목록·생성, `max_score` 반환·입력 |
| GET/PATCH | `/homeworks/{id}/` | 과제별 만점·합격 기준 조회/변경과 1차 점수 동기화 |
| POST | `/homeworks/{id}/source-exam/` | 워크북 분리용 비노출 원본 시험을 멱등 생성 |
| GET/PATCH | `/homeworks/{id}/question-grading/` | 배정 학생×워크북 문항 채점표 조회와 O/X/복습 저장 |
| GET/PATCH | `/homework/policies/` | 개별 기준 없는 기존 과제의 차시 기본 정책 |
| PATCH | `/homework/scores/quick/` | 성적표 셀 점수 저장. 서버 만점을 정본으로 사용 |
| GET | `/results/session-scores/` | 성적표 과제 메타와 각 셀에 과제별 만점 반환 |

`/homeworks/` 응답은 원시 오버라이드와 함께 `effective_cutline_mode`,
`effective_cutline_value`, `effective_round_unit_percent`,
`uses_session_cutline_default`를 반환한다. 기존 과제에 `default_max_score`가 없으면 100으로 읽는다. 이미 과제 메타에는
만점이 있지만 과거 점수 스냅샷이 100인 경우에도 성적표 조회는 즉시 과제
만점을 분모로 반환한다. 다음 점수 저장 또는 만점 재저장에서는 스냅샷과
판정을 정본 값으로 복구한다.

`/homeworks/`, 학생별 성적 요약, 차시 성적표 메타는 모두 `grading_mode`를
반환한다. 학생 상세의 단건 수정도 `/homework/scores/quick/`을 사용하며 먼저
기존 점수 편집 lease를 짧게 획득한다. 다른 화면이 같은 차시를 수정 중이면
`409 SCORE_EDIT_LOCKED`로 실패하고 값을 덮어쓰지 않는다.
학생 상세와 학생·학부모 성적 요약 응답은 배정된 각 과제의 `grading_mode`,
`meta_status`, 정본 `max_score`, 차시 유형·순서 메타데이터를 반환한다. 완료형은
완료 여부, 점수형은 `점수/만점`을 표시하며 `NOT_SUBMITTED`와 미검사 `null`은
모두 숫자 0으로 바꾸지 않는다.

학생 상세는 `HomeworkAssignment` 도입 전 생성된 1차 `HomeworkScore`도 과거
이력으로 보존한다. `/homework/scores/quick/`은 새 미배정 점수 생성은 계속
거부하지만, 같은 tenant의 정확한 차시·수강·과제에 이미 존재하는 레거시 1차
점수행은 그 행을 잠근 뒤 수정할 수 있다. 따라서 화면에 정상 이력으로 노출된
과제가 배정행 부재만으로 저장 불가능해지지 않으며, 다른 tenant·차시·수강생이나
점수행조차 없는 미배정 대상은 기존처럼 실패한다.

차시 성적표의 편집 초안은 브라우저 편집기별로 유지한다. 같은 계정을 공유한
여러 화면도 `X-Score-Editor-Client`로 구분한다. 시험과 과제 변경은 각각
`(enrollment_id, exam_id, 입력 종류, question_id)`와
`(enrollment_id, homework_id)`의 정확한 셀 집합이 겹칠 때만 충돌한다. 셀 집합이
서로 겹치지 않으면 계정·기기와 관계없이 여러 직원·화면이 동시에 저장할 수 있다.
각 편집기는 현재 선택한 성적 셀을 `active_cell`로 갱신하며, 다른 편집기는 응답의
`active_editors`에서 편집자 이름과 선택 셀을 읽는다. 같은 셀만
`409 SCORE_EDIT_LOCKED`로 차단하고 빈 초안은 다른 직원의 입력을 막지 않는다.
충돌 응답이 와도 클라이언트의 로컬 초안은 삭제하거나 서버값으로 초기화하지 않는다.
과제 부분 저장은 현재 요청 셀에 대한 활성 초안
소유권을 다시 확인한 뒤 반영하므로 서로 다른 셀의 안전한 동시 작업과 같은 셀의
덮어쓰기 방지가 함께 유지된다. 선택 표시와 lease는 마지막 갱신 후 2분이 지나면
자동 만료된다. 같은 계정의 새 브라우저 편집기는 만료된 빈 lease에 예전 선택 셀이나
무효화 표시만 남아 있어도 변경값이 없으면 안전하게 재사용한다. 변경을 담은 같은
계정의 활성 또는 만료 초안은 새 기기의 조회에 복구 대상으로 노출하고, 사용자가
복원을 명시한 경우에만 새 client lease로 인계한다. 활성 초안은 정확한 같은
차시·tenant·계정에서 최신 변경 초안 하나만 노출하며, 빈 초안이나 무효화된 초안은
새 기기 복구 대상으로 선택하지 않는다. 이전 기기 행은 `SAME_ACCOUNT_HANDOFF`로
무효화하되 payload를 보존한다. 다른 계정·tenant는 조회하거나 인계할 수 없다.

교사 모바일의 단건 점수 수정은 실제 대상 셀을 빈 lease보다 먼저 초안으로 등록한다.
같은 계정의 다른 화면에 그 셀과 충돌하는 초안이 있으면 모바일 화면으로 편집권을
인계하고 기존 초안은 `SAME_ACCOUNT_HANDOFF`로 무효화하되 입력값은 복구용으로
보존한다. 따라서 새 기기의 저장은 기기 차이만으로 `409`가 되지 않고, 이전 화면은
이후 저장에서 stale 안내와 복원 절차를 거친다. 다른 계정의 충돌 초안은 인계할 수
없으며 기존 `409 SCORE_EDIT_LOCKED` 경계를 유지한다.

차시 성적표 조회는 각 과제 점수 블록에 셀 version인 `updated_at`을 반환한다.
새 클라이언트는 `/homework/scores/quick/`에 `expected_updated_at`을 함께 보내며,
빈 셀을 처음 저장할 때는 `null`을 보낸다. 서버는 해당 점수 행을 잠근 뒤 현재
version과 일치할 때만 저장한다. 다른 화면이 같은 셀을 먼저 저장했거나 빈 셀로
보던 사이 행이 생성됐으면 점수를 바꾸지 않고 `409 SCORE_CELL_CONFLICT`와
`server_value(score, max_score, meta_status, updated_at)`를 반환한다. 필드를 보내지
않는 기존 클라이언트는 단계적 전환을 위해 기존 저장 동작을 유지한다. 프론트는
편집 중 성적표를 주기적으로 다시 읽되 active/dirty 셀의 로컬 초안을 덮지 않는다.
깨끗한 셀만 원격 저장값으로 수렴하며, 충돌한 셀은 서버값과 내 입력을 함께 보여
준 뒤 최신 서버 version을 기준으로 한 명시적 재적용에서만 다시 저장한다.

무중단 전환은 두 릴리스로 수행했다. 첫 호환 릴리스는 `results.0020`의
`client_id`와 브라우저별 유일조건을 적용하되 앱이 빈 client 슬롯 하나만 사용했고,
구버전 API도 컬럼 기본값 `""`으로 같은 사용자에게 두 번째 행을 만들지 않았다.
모든 API 인스턴스가 호환 릴리스로 교체된 뒤 앱이 브라우저별 행을 사용하도록
전환했다. 따라서 롤링 배포 중 구버전·신버전 API가 겹쳐도 기존 초안과 새 초안이
서로 덮어쓰지 않는다.

현재 운영 설정 화면은 조회 응답의 `updated_at`을
`X-Expected-Updated-At` 헤더로 보낸다. 서버는 과제 행을 잠근 뒤 같은
버전일 때만 저장하며, 다른 화면이 먼저 저장했으면 기존 입력을 덮어쓰지
않고 `409`, `code=stale_resource`, 현재 `updated_at`을 반환한다. 헤더가
없는 기존 클라이언트는 호환을 위해 기존 동작을 유지한다.

## 구현 위치와 검증

- 만점 정본·직렬화: `apps/domains/homework_results/models/homework.py`,
  `serializers/homework.py`
- 만점 변경 검증·동기화: `services/max_score_sync.py`,
  `views/homework_view.py`
- 점수 저장: `views/homework_score_viewset.py`
- 개별/기본 정책 계산: `apps/domains/homework/utils/homework_policy.py`,
  `apps/domains/homework/serializers/core.py`,
  `services/policy_recalc.py`
- 성적표 조회: `apps/domains/results/views/session_scores_view.py`
- 학생 상세 과제 합성: `apps/domains/results/views/admin_student_grades_view.py`,
  `apps/support/results/admin_student_grades_dependencies.py`
- 학생·학부모 성적 요약: `apps/support/student_app/results_summary.py`
- 워크북 원본·채점표: `views/homework_view.py`,
  `tests/test_workbook_source_and_grading.py`
- 학생 제출 media 모델·검증·재시도: `apps/domains/submissions/models/submission.py`,
  `services/homework_media.py`, `views/homework_submission_media_view.py`
- 다건·부분 실패·권한 회귀:
  `apps/domains/submissions/tests/test_homework_media_submission.py`
- Ymath 시험·워크북 통합 실자료 UAT:
  `../operations/runbooks/ymath-real-source-qa.md`,
  `scripts/ymath_realuse_scenario.py`

집중 회귀 검증:

```powershell
.venv\Scripts\python.exe -m pytest `
  apps/domains/homework/tests/test_homework_policy_api.py `
  apps/domains/homework_results/tests/test_homework_quick_patch_scope.py `
  apps/domains/results/tests/test_session_scores_roster_scope.py `
  -q --reuse-db
```
