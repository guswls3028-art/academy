# 시험 생성·혼합 채점·오답노트 — SSOT

## 목적과 사용자

원장·선생님·채점 권한이 있는 직원이 원본 시험지와 채점 방식을 한 번에
등록하고, 선택형 OMR과 답변형 직접 채점을 같은 시험 결과로 확정하는
현재 계약이다. 저장된 문항 결과는 성적 통계, 합격 판정, 클리닉 진행도,
학생별 오답노트가 함께 사용한다.

- 관리자 화면 사용법:
  [frontend/docs/USER-GUIDE-ADMIN.md](https://github.com/guswls3028-art/academy-frontend/blob/main/docs/USER-GUIDE-ADMIN.md)
- OMR 출력·인식 계약: [omr.md](omr.md)
- 주요 진입점: **차시 → 시험 → 시험 추가**, **시험 → 설정 → 강의 추가**,
  **시험 → 채점·결과**

시험 목록은 요청 tenant의 자료만 반환하며 기본 페이지네이터의 `page_size`
요청을 최대 500건까지 적용한다. 같은 시각에 생성된 시험도
`created_at DESC, id DESC`로 안정되게 정렬해 새로고침이나 다음 페이지에서
순서가 바뀌거나 문항 업로드 대상을 놓치지 않는다. 강의 필터는 한 시험이 같은
강의의 여러 차시에 연결돼도 `Exam` 한 행만 반환한다. M:N 조인 행을 그대로
페이지 수와 목록으로 노출하지 않고 요청 tenant 범위의 연결 존재 여부로 판정한다.
잘못된 cross-tenant 차시 연결은 강의 필터 일치로 인정하지 않는다.

출제 소유 관계는 URL과 요청 tenant에서 정본을 찾는다. `Question.sheet`와
`Sheet.exam`은 조회 응답에 포함되고 create payload에서는 필수 writable ID이지만,
일반 update payload로 기존 문항이나 sheet를 다른 부모에 옮길 수 없다. 문항 생성은
요청 tenant의 실제 Sheet를 서버가 다시 조회하고, Sheet 생성도 요청 tenant의 실제
template Exam을 다시 조회한다. 응시 이력 `ExamAttempt`는 제출·수동채점 서비스가
만드는 append-only 감사/결과 기록이므로 generic API는 조회만 제공하고
POST/PATCH/DELETE를 허용하지 않는다.
Create serializer의 부모 PK 조회도 요청 tenant 범위로 제한하여 foreign-existing ID와
존재하지 않는 ID가 같은 validation 오류를 반환하며, 다른 tenant 객체의 존재 여부를
API 오류 모양으로 구분할 수 없다.

## 시험 채점 계약

`Exam`이 시험 생성 시 아래 계약을 소유한다.

| 필드 | 값 | 의미 |
|------|----|------|
| `grading_mode` | `choice` | 선택형. OMR 결과가 주 채점 경로다. |
|  | `written` | 답변형. 모든 문항을 직접 채점한다. |
|  | `mixed` | 혼합형. 선택형 결과는 OMR로 보존하고 답변형만 직접 채점한다. |
| `manual_grading_method` | `correctness` | 답변형을 정오로 입력한다. |
|  | `score` | 답변형을 문항별 부분점수로 입력한다. |
| `choice_question_count` | 0 이상의 정수 | 원본 자동 분리 시 앞에서부터 선택형으로 만들 문항 수. 혼합형은 1 이상이어야 한다. |
| `segmentation_status` | `none`, `processing`, `review_required`, `ready`, `failed`, `conversion_required` | 원본 문항 분리와 교직원 검수 상태다. |
| `student_results_published` | `true`, `false` | 학생·학부모 성적 공개 여부다. 기본값 `true`는 기존 시험 노출을 유지한다. |

운영 설정은 `PATCH /exams/{id}/`에서 한 transaction으로 저장한다. 조회와 잠금은
반드시 `Exam.tenant` 소유권으로 범위를 제한한 단일 시험 행에 적용한다. 차시 연결은
소유권 fallback이 아니며 다른 tenant의 시험을 노출하지 않는다. 이 경계는 PostgreSQL의
행 잠금과 호환되어야 하므로 `DISTINCT` 결과 자체를 `SELECT FOR UPDATE`로 잠그지 않는다.

시험 응시 대상 `PUT`은 현재 차시의 활성 수강 등록 ID만 받아 완전 치환한다. 시험과
차시가 모두 요청 tenant에 속하는지 확인한 뒤 시험 본행을 잠그므로, 같은 시험의
대상자 저장 두 건이 동시에 들어와 두 선택의 합집합이 남지 않는다. 마지막으로 잠금을
얻은 완전 치환 요청이 정본이며 다른 tenant나 연결되지 않은 차시 ID는 거부한다.

### 시험 생성·대상 변경과 저장된 진척

정규 시험 생성과 대상자 추가·제거는 응시 대상 변경과 기존 `SessionProgress`
갱신을 **같은 트랜잭션**에서 끝낸다. 결과가 없는 새 대상 시험도 canonical
계산에 포함하므로 이전 합격값이 남지 않는다. `on_commit` best-effort 콜백은
사용하지 않는다. 계산·저장 실패 시 시험 생성/대상 치환과 부분 갱신이 모두
롤백되고 API는 실패한다. 같은 대상 목록의 반복 저장은 파생값·최초완료시각을
다시 쓰지 않는다. 이 경로는 과거의 모든 불일치를 자동 보정하는 backfill이 아니다.

영향 범위는 변경 전후 **유효 대상 `(enrollment, session)` 집합의 차이**다.
시험의 모든 연결 차시를 수강 등록의 같은 tenant·강의로 제한한다. 명시 대상이
0명인 기존 시험의 roster 호환 규칙도 같은 canonical helper로 평가하므로,
명시 목록의 첫 생성/마지막 제거가 다른 연결 강의에 주는 영향도 빠뜨리지 않는다.
inactive/삭제 학생과 cross-tenant/잘못된 강의 관계는 갱신하지 않는다.
시험 치환은 시험 본행 잠금으로 직렬화하고, 생성은 차시 본행의 non-key 잠금으로
직렬화한다. 실제 영향 enrollment를 ID 순서로 non-key 잠근 후 진척 행을 잠가,
서로 다른 시험 생성·치환과 정상 채점 재계산이 같은 진척을 덮어쓰지 않게 한다.
FK key-share와 양립하는 잠금으로 공유 차시/강의의 교착 위험도 제한한다.

| 파생 필드 | 계산 및 보존 계약 |
|---|---|
| `SessionProgress.exam_attempted`, `exam_aggregate_score`, `exam_passed`, `exam_meta` | 기존 `SessionProgressCalculator`의 시험 범위·명시 대상·미응시·집계·강의별 합격 기준을 그대로 공유한다. |
| `completed`, `completed_at` | 기존 video/homework 값과 시험 합격의 AND. 최초 완료시각은 기존 계산기와 동일하게 한 번만 설정하고 완료 취소에도 지우지 않는다. |
| `calculated_at`, `updated_at` | 실제 파생값이 달라진 행만 갱신한다. 출결·영상·과제 필드와 수동 `meta`는 보존한다. |
| `LectureProgress` | 영향 수강 등록의 기존 canonical 강의 집계와 같은 위험수준 규칙을 사용한다. `meta`와 소유 강의는 보존하며 `RiskLog`를 만들지 않는다. |

전체 `ProgressPipelineService`는 클리닉 자동 생성·해소 부작용을 가지므로 이
대상자 편집 경로에서 호출하지 않는다. `Result`, `ResultFact`, `ExamAttempt`,
`ClinicLink`/해소 이력, `AssessmentCorrection`, 발송 로그·예약·outbox는 바꾸지 않는다.
신규 roster에 아직 진척 행이 없다면 시험 생성만으로 출결·과제 값을 추측해 행을
만들지 않는다. 누락 수는 구조화 로그에 남고, roster/시험 조회는 실제 새 시험을
반환하며 세션 요약의 participant_count는 기존 진척 행 수(없으면 0)를 유지한다.
첫 실제 점수 입력은 기존 채점 파이프라인에서 진척을 생성한다. 조회 GET은 쓰지 않는다.

검증: `apps/domains/progress/tests/test_exam_target_projection_pg.py`의 PostgreSQL
API 생성→대상 편집→요약 조회, 진척 부재→최초 채점, rollback/중복/commit 후 예외,
동시 치환·생성 교차, 다중 연결·legacy 전환·tenant 격리, 원본/수동/발송 불변 테스트.

### 하나의 시험을 여러 강의에서 운영

정규 `Exam` 하나를 여러 강의의 차시에 연결해 문제·정답·원본과 채점 결과 구조를
공유할 수 있다. `Exam.sessions`가 연결 정본이며, 강의별 합격·귀가 기준은
`ExamLecturePolicy(exam, lecture, pass_score)`가 소유한다. 정책 행이 없는 기존 연결은
`Exam.pass_score`를 기본값으로 사용한다. 강의별 값은 0 이상이고 시험 만점 이하여야
하며, 한 강의의 값을 바꿔도 다른 강의와 시험 기본값을 바꾸지 않는다.

- `GET /exams/{exam_id}/lecture-assignments/`는 연결 강의를 묶어 차시, 강의별 유효
  기준, 활성 roster 수, 시험 선택 인원과 전체 합산 인원을 반환한다.
- `POST /exams/{exam_id}/lecture-assignments/`는 요청 tenant의 활성 비시스템 차시만
  연결한다. 연결 시 해당 차시의 현재 활성 `SessionEnrollment`를 `ExamEnrollment`에
  중복 없이 합쳐 즉시 OMR·수동 채점 대상이 되게 한다.
- 정규 시험 최초 생성(`POST /exams/`)도 선택한 최초 차시의 같은 tenant·강의에 속한
  활성 `SessionEnrollment`를 트랜잭션 안에서 `ExamEnrollment`에 중복 없이 만든다.
  따라서 `source_exam_id`로 기존 시험을 불러온 뒤 다른 강의를 추가해도 최초 강의와
  추가 강의의 roster 합집합이 실제 채점 대상 정본과 일치한다.
- `PATCH /exams/{exam_id}/lecture-assignments/`는 시험에 실제 연결된 강의의 기준만
  변경하고 진척 파이프라인을 다시 실행한다. 다른 tenant 차시·강의는 실패 폐쇄한다.
- 학생·학부모 성적 이력, 교직원 학생 이력, 세션 성적 메타와 행, 세션 요약, 운영
  analytics는 모두 결과 수강 강의의 유효 기준을 사용한다. 하나의 응답 안에서 기본
  기준과 강의별 기준을 섞지 않는다.
- 공유 시험의 진척 재계산은 `Result.enrollment`가 같은 tenant의 실제
  `ExamEnrollment`이고, 그 수강 등록의 `lecture_id`와 시험 연결 차시의 강의가
  일치할 때만 실행한다. 결과 수강생 전체와 연결 차시 전체를 곱하지 않는다. 같은
  강의에 연결 차시가 여러 개면 그 강의의 차시만 모두 계산하며, 맞는 연결 차시가
  없으면 다른 강의 차시를 빌려오지 않고 실패 폐쇄한다.
- 기존 강의 할당을 반복 저장하거나 채점·OMR 제출에서 진척을 다시 계산해도
  `SessionProgress`는 실제 수강 강의의 차시에만 유지되고, 수강 등록당 하나인
  `LectureProgress`의 강의 소유권은 바뀌거나 중복 생성되지 않는다.
- 공유 시험에서 한 강의의 마지막 연결 차시를 제거하면 그 강의 수강생의 기존
  `ExamEnrollment`는 이력 보존용으로 남을 수 있지만, 학생 시험 목록·상세·문항·제출과
  grading scope는 현재 시험 연결 차시와 수강 강의가 일치하지 않으면 실패 폐쇄한다.
- 시험에 `ExamEnrollment`가 하나라도 있으면 그 명단이 시험별 대상자 정본이다.
  같은 차시의 다른 학생은 해당 시험의 성적 셀·미응시·불합격·진척·클리닉 판정에
  포함하지 않고 학생·학부모 성적 이력에도 노출하지 않으며, 수동 채점이나 제출로
  대상자 행을 우회 생성할 수도 없다. 과거에 잘못 생성된 비대상 `Result`도 이 읽기
  경계에서 제외한다. 명시 대상자가 전혀 없는 기존 시험만 연결 차시 roster를 호환
  경로로 사용한다.
- 대상자 완전 치환 뒤 제거된 학생의 기존 `Result`와 append-only fact는 보존하되,
  세션 시험 요약·운영 analytics·오답노트와 unresolved 자동 `ClinicLink` 읽기에서는
  즉시 제외한다. 데이터 보존은 현재 대상 노출 권한이 아니다.

시험별 성적과 문항 통계는 선택 `lecture_id`로 **전체 / 강의별** 조회할 수 있다.
전체 조회도 각 결과의 `Enrollment.lecture_id`에 맞는 기준으로 합불을 계산하며,
강의별 조회의 석차·평균 코호트는 선택 강의 학생만 포함한다. 학생 결과와 세션 성적,
진척·클리닉 생성/해소, 재시험과 클리닉 대상 화면도 같은 강의별 유효 기준을 사용한다.
한 시험이 같은 강의의 차시 여러 개에 연결된 경우 roster는 enrollment ID 합집합으로
계산한다. 차시를 하나로 확정해야 하는 오답 확인 상태는 기존처럼 후보가 정확히 하나일
때만 제공해 다른 차시 기록을 빌려오지 않는다.

합격 점수 `0`은 클리닉 합격 기준을 사용하지 않는 유효한 값이다. 만점은 0보다 커야
하고 합격 점수는 0 이상이면서 만점을 넘을 수 없다. 재응시를 켜면 최대 응시 횟수는
2회 이상이어야 한다. API와 모델 검증이 같은 범위를 강제한다.
학생 온라인 제출은 `(exam, enrollment)`의 응시 대상 행을 먼저 잠그고 canonical
`ExamAttempt.attempt_index`로 최대 횟수를 확인한다. 한도 초과는 현재 `DONE` 제출을
`SUPERSEDED`로 바꾸거나 새 `SUBMITTED` 행을 만들기 전에 `409`로 끝나므로, 늦거나
중복된 요청이 마지막 유효 응시를 역행시키지 않는다.

문항이 생성된 뒤에도 `grading_mode`와 `manual_grading_method`는 시험
설정에서 바꿀 수 있다. 이 전환은 문항, 정답, 기존 OMR·직접 입력 결과를
삭제하거나 다시 계산하지 않고 이후 사용할 채점 화면과 수정 가능 범위만
바꾼다. `choice`에서 `written/correctness`로 바꾸면 기존 문항 전체가
정오표 입력 대상이 되고, 다시 `choice`로 바꾸면 기존 결과는 보존된 채
정오표가 잠기고 OMR 흐름을 사용한다.

반면 `choice_question_count`는 실제 혼합형 문항 구조의 경계이므로 문항
생성 뒤에는 바꿀 수 없다. 이미 문항 또는 성적이 있는 운영 시험에 새
원본을 올려 자동으로 덮어쓰는 것도 금지한다.

단, 과거 데이터에서 `choice_question_count=0`인데 이미 저장된 시험지의
`choice_count`가 1 이상이고 전체 문항 수보다 작은 경우는 실제 문항 구조와
같은 값으로 한 번 복구할 수 있다. 이 예외는 혼합형 전환의 막힌 상태를
해소하기 위한 것이며, 저장된 시험지와 다른 경계로 바꾸는 요청은 계속
거부한다.

기존 시험의 문항별 `question_kind`가 있으면 직접 채점 가능 여부는 실제
문항 유형을 기준으로 결정한다. 따라서 기존 답안 등록 화면에서 만든
임의 순서 혼합형도 번호와 유형을 유지한다.

## 학생 성적 공개

교직원은 시험 설정의 **학생 성적 공개**에서 시험별 공개 여부를 바꾼다.
비공개는 응시·제출·채점·교직원 성적표·통계를 그대로 유지하고 학생 및 학부모의
성적 목록, 누적 추이, 분석, 개별 결과에서만 해당 시험 결과를 제외한다. 정답 공개
정책인 `answer_visibility`와는 별도다. 성적이 비공개인 동안에는 점수, 문항별 정오,
석차와 분석을 직렬화하지 않는다.

비공개 시험의 개별 결과 API는 재응시 정책만 제한 응답으로 반환한다. 이를 통해
학생 화면이 결과 비공개를 미응시로 오인해 추가 제출을 허용하지 않는다. 학생·학부모
선택 범위와 tenant 검증은 공개 여부와 관계없이 먼저 실패 폐쇄한다. 공개로 다시
바꾸면 저장된 기존 결과를 재계산하거나 이동하지 않고 같은 결과가 즉시 조회된다.

## 원본 시험지 등록과 자동 분리

1. 정규 시험을 만들며 시험명, 만점, 커트라인, 채점 방식과 혼합형 선택
   문항 수를 확정한다.
2. 같은 tenant의 빈 시험 ID와 필수 `file`(문제지), 선택 `answer_file`(정답지),
   선택 `explanation_file`(해설지)을 `POST /exams/pdf-extract/`에 보낸다. 세 파일의
   역할은 파일명이나 업로드 순서로 추정하지 않는다.
3. 원본은 tenant 전용 storage R2의 `tenants/{tenant}/exams/pdf-extract/` 경로에
   `problem_source` 자산으로 저장한다. 이 경로의 다운로드 URL도 storage
   버킷에서 서명하며 AI 버킷으로 잘못 라우팅하지 않는다. PDF는 같은 객체를
   기존 배포용 `problem_pdf` 메타데이터에도 연결해 브라우저가 큰 파일을 두 번
   전송하지 않는다.
4. PDF, 이미지, HWP 5.x 또는 HWPX는 `question_segmentation` 작업으로 전달하고
   `segmentation_status=processing`으로 바꾼다.
5. worker는 문제 번호와 정답·해설의 명시 번호만 연결한다. 성공 콜백은 정본을
   바로 쓰지 않고 `ExamQuestionProposal`에 문제, 원본 번호, 인식 정답,
   해설 이미지·텍스트와 출처, `recognized|partial|unrecognized` 상태와 누락·불일치
   번호를 저장한 뒤 `review_required`로 바꾼다.
6. 교직원은 **문항·정답·해설 맞춤 확인**에서 문제, 인식 정답과 선생님 원본
   해설을 나란히 보고 번호·정답 수정, 수록/제외를 검수한다. 승인 전에는 채점
   문항과 정답이 생기지 않는다.
7. 승인은 빈 운영 시험을 다시 잠그고 선택 번호가 양수·고유한지 확인한 뒤
   문항, 정답과 `source_file` 해설을 한 transaction에서 만든다. 구버전 화면이
   정답 필드를 보내지 않으면 인식 후보를 지우지 않고 보존한다. 그때만
   `ready`와 유사문항 인덱싱으로 진행한다.

확정된 시험을 템플릿으로 저장한 뒤 **이전 시험 가져오기**를 하거나 **다른
차시에서 복사**하면 문항 번호·배점·정답뿐 아니라 확정된 문제 이미지,
`region_meta`, 교사 해설 텍스트와 해설 이미지, 출처·매칭 신뢰도와 시험 원본
자산도 독립 시험 스냅샷에 보존한다. `review_required` 상태의
`ExamQuestionProposal`은 아직 채점 정본이 아니므로 가져오지 않는다. 먼저 원본
시험에서 검수를 확정해야 한다.

문항 작업은 공통 dispatcher가 확정한 페이지별 박스와 원본 문항 번호를
그대로 사용한다. worker pipeline이 같은 PDF를 별도 `question_splitter`로
다시 자르지 않으므로 그래프·도형까지 확장한 표시 영역, cross-page 번호
검증과 scan fallback 결과가 실제 저장 문항 이미지에도 동일하게 반영된다.
날짜와 `Hyper`/`Routine|Remake`/`복습 Test` 표제가 함께 있는 짧은 학원
복습지 표지는 범위 회차(`1. 평면좌표` 등)를 문항 번호로 오인하지 않고
비문항 페이지로 제외한다. 같은 헤더가 반복되더라도 실제 문제 본문이 있는
페이지는 이 짧은 표지 조건에 포함되지 않는다.

문제 뒤에 `정답 및 해설`, `정답과 풀이`처럼 명시적인 교사용 구간이 붙은
PDF는 앞선 페이지에서 문항이 확인된 경우 그 표제 페이지부터 문서 끝까지를
문항 후보에서 제외한다. 이 구간은 버리지 않고 표제가 반복되지 않는 다음
페이지까지 번호별 해설로 읽는다. 분리된 실제 문항 번호와 일치하는 해설만
콜백 결과에 포함하며 번호가 있는 해설 영역은 이미지로도 보존한다. legacy
`boxes`에는 제외된 해설 박스를 섞지 않는다. 추출 텍스트는 교사가 쓴 내용을
읽은 것이며 새 해설을 생성하지 않는다.

검수 화면은 이미지가 없는 자동 인식 텍스트를 **원본 자동 인식 · 검수 필요**로
명시한다. 이 텍스트는 교사가 `원본과 같을 때만 해설로 저장`을 직접 선택하지
않으면 승인 시 `QuestionExplanation`에 쓰지 않는다. 원본 해설 이미지가 있으면
이미지를 우선 표시·보존하며, 시스템이 별도 AI 풀이를 생성한 것으로 표현하지
않는다.

실행 파일·스크립트·브라우저 실행 형식을 제외한 모든 안전한 자료 원본은
확장자와 관계없이 받고 최대 크기는 파일당 50MB다. 원본은 tenant 전용 자산으로
형식 그대로 보존한다. PDF, PNG, JPG/JPEG, HWP/HWPX는 자동 문항 분리를
시도하고, 다른 형식은 `conversion_required` 호환 상태로 저장해 시험 상세에서
문항·해설을 직접 등록·검수한다. 이 상태는 PDF 재업로드를 요구하지 않는다.
업로드 화면은 한 진입점에서 다음 자료 역할을 독립적으로 받는다.

- **문제지(필수)**: 답 표시가 없는 학생용 원본. `problem_source`로 보존한다.
- **정답지(선택)**: 객관식 기호·단답 답을 번호별로 읽어 `answer_source`로 보존한다.
- **해설지(선택)**: 교사 풀이 원본을 `teacher_explanation_source`로 보존한다.
- 정답·해설이 문제지 뒤에 붙은 통합 원본도 지원하지만, 별도 파일이 있으면 역할이
  명시된 별도 원본을 우선 번호 연결한다. PDF·이미지·HWP/HWPX 조합은 모두 같은
  paired-source 계약을 사용한다.

OCR 텍스트만으로 수식·도형·필기 해설을 대체하지 않는다. 번호가 인식된 정답·해설
페이지는 렌더링 원본 이미지도 함께 보존하고, 인식 텍스트는 검색·초기 입력 후보로만
사용한다. worker는 교사 문장을 생성·요약·재작성하지 않는다. 일부 번호만 맞으면
`partial`로 명시하고 맞은 항목만 후보에 붙이며, 누락/불일치 목록을 검수 화면에
보인다. 스캔 PDF에 검색 가능한 학원명·워터마크만 섞인 경우에도 그 짧은 내장 텍스트를
정답·해설 본문으로 오인하지 않고, 이미지에 남은 번호별 원문을 OCR로 보강한다. 같은
번호가 내장 텍스트와 OCR에 모두 있으면 내장 텍스트를 우선하며 OCR은 누락 번호만
추가한다. 작업 실패나 `review_required` 상태에서는 같은 세 역할로 재업로드할 수 있다.
재처리는 새 원본 자산을 먼저 연결한 뒤 더 이상 어떤 `ExamAsset`도 참조하지 않는
이전 객체만 삭제한다. 다중 업로드가 중간 실패하면 이번 요청이 올렸지만 DB에
참조되지 않은 객체만 정리하고, 기존 및 이미 연결된 원본은 보존한다.

템플릿의 문제지·OMR 첨부 API도 같은 안전 원본 정책을 사용한다. DOCX/PPTX 등
비-PDF 원본을 보존하되 실행 파일·스크립트·브라우저 실행 형식은 R2 쓰기 전에
거부한다.

HWP 5.x는 OLE 본문 레코드와 번호별 미주, EqEdit 수식, BinData를 직접 읽고,
HWPX는 ZIP 패키지의 본문 문단과 미주·수식·원본 BinData 참조를 읽는다. 단일
HWP/HWPX에서는 **미주 번호를 경계와 연결 키로 사용**해 본문의 문자·수식·삽화로
답 표시 없는 문제 이미지를 재현한다. legacy HWP의 picture control은 한 미주에
표지나 이웃 문항 그림이 함께 들어갈 수 있어 번호별 범위의 정본으로 신뢰하지
않는다. 번호별 ParaText와 EqEdit로 재현한 `본문·수식`을 기본 해설로 쓰고,
picture control 결과가 다르면 삭제하지 않은 채 `삽입 그림 직접 확인` 후보로
별도 보존한다. 교직원이 검수 화면에서 직접 대조해 선택한 경우에만 그 삽입
그림을 정본 해설로 확정한다. 미주 해설 이미지를 상단 비율로 잘라 문제로
재사용하지 않는다.
수식 조판 모듈을 사용할 수 없는 제한 환경에서는 문항을 버리지 않고 EqEdit 원문을
읽기 쉬운 텍스트로 재현하며, 원본 파일과 교사 검수 경계는 그대로 유지한다.
EqEdit가 공백 없이 저장한 비교식(`0letheta`)도 공식 토큰 경계만 해석해
`0≤θ`로 재현하며, 일반 영문 문자열 안의 `le`는 비교 연산자로 바꾸지 않는다.

번호가 있는 모든 미주의 안전한 해설 재현과 모든 본문 문제를 함께 재현한 경우에만
문제+해설 통합 자료로 처리한다. 일부 미주 그림 또는 본문 문항이 빠지면 부분
그림을 성공으로 오인하지 않고 작업 결과를 `conversion_required`로 닫는다.
원본은 보존되며 교직원은 문항·해설을 직접 등록해 검수할 수 있다. PDF 재업로드는
선택 사항이며 문장·수식·필기 내용을 AI가 다시 쓰거나 생성하지 않는다.

문제지·정답지·해설지가 분리된 Ymath 자료는 자동 번호 연결을 원할 때 **답 표시가
없는 학생용 문제지 PDF/이미지**를 주 파일로, 같은 시험의 정답지와 **선생님 해설
HWP/HWPX**를 각 선택 역할로 함께 올린다. 그 밖의 안전한 형식 조합도 거부하지
않고 세 역할 원본을 각각 보존하며, 지원되는 문제 원본의 문항 분리는 계속 진행하고
자동 인식이 불가능한 정답·해설은 직접 연결·검수한다.
세 원본은 각각 `problem_source`, `answer_source`,
`teacher_explanation_source` 자산으로 보존하고, worker는 학생용 원본에서 자른 문제와
정답·HWP 미주 해설을 원본 문항 번호로만 연결한다. 한 문항도 번호가 맞지 않으면
성공으로 가장하지 않고 부분/미인식 상태를 반환한다. 일부만 맞으면 맞은 후보만
붙이며 최종 확정 전 검수 화면에서 정답·해설 유무를 확인한다. 문제 파일에 필기
정답이 직접 그려진 경우에는 크롭으로
깨끗한 문제를 복원할 수 없으므로 답 표시가 없는 별도 문제 원본을 사용하는 편이
안전하다.

`ExamAsset.file_type`은 업로드에서 확인한 전체 MIME 값을 보존하며 최대 255자를
수용한다. DOCX 등 표준 office MIME을 잘라 저장하거나 확장자만으로 대체하지 않는다.

짝 파일의 legacy HWP 미주가 하나의 완성 그림이 아니라 편집 가능한 문자·EqEdit
수식·일부 삽화로 작성된 경우에도 원문을 버리지 않는다. worker는 번호별 ParaText,
공식 EqEdit script와 BinData 삽화를 문서 순서로 직접 읽고, 수식은 결정론적인
조판기로 렌더링해 `선생님 원문(문자·수식·삽화 재현)` 검수 이미지를 만든다.
AI가 해설을 생성하거나 문장을 바꾸지 않으며 원 HWP 자산도 그대로 보존한다.
번호가 있는 모든 미주를 재현하지 못하면 부분 이미지만 성공으로 올리지 않고
실패한다. 단일 HWP/HWPX도 같은 실패 폐쇄 원칙을 따르며, 본문 문제와 미주
해설을 각각 완성하지 못하면 원본 보관 상태로 닫고 직접 등록·검수를 안내한다.

이미 확정된 legacy HWP 시험에서 picture control 범위 오류가 확인된 경우에는
일반 재분석으로 교사 결과를 덮어쓰지 않는다. 운영자는
`repair_hwp_source_explanations --tenant-id <id> --exam-id <id>`의 dry-run으로
원본 해시, 정규 시험·ready 상태, source-file 출처, 승인 문항 번호와 안전 재현
번호의 완전 일치를 먼저 확인하고 `--apply`한다. 적용은 새 객체 업로드와 byte
readback 뒤 transaction에서만 정본 키를 바꾸며, 이전 키·원본 SHA-256·교체 키를
문항 `region_meta.explanation_repair`와 검수 attachment에 남긴다. 이전 객체는
삭제하지 않아 롤백과 교사 대조가 가능하다. 수동 편집 해설, 일부 문항 불일치,
다른 tenant 경로, 부분 복구 상태는 모두 실패 폐쇄한다.

과거 `hwp_endnote`/`hwpx_endnote` 검수 후보는 배포 중이던 작업을 잃지 않도록
8~98% 문제 영역 조절을 계속 지원한다. 새 단일 HWP/HWPX 후보는
`hwp_body_endnote`/`hwpx_body_endnote`로 기록하고 이미 분리된 본문 문제를
보여주므로 해설 이미지 크롭 조절을 제공하지 않는다. 어느 경로에서도 원본
미주 해설 이미지는 바꾸지 않는다.

tenant가 없거나 다른 tenant의 시험이면 거부한다. 이미 분리 중이면
`409`, 문항 또는 성적이 있어 잠긴 운영 시험이면 `409`, 실행·스크립트·브라우저
실행 형식이나 50MB 초과 파일이면 `400`을 반환한다.

## 직접 채점 표

### 학생별 결과 목록

**시험 → 채점·결과 → 학생별 결과**는 1차 점수 석차와 현재 최종점수를
구분한다. `GET /results/admin/exams/{exam_id}/results/`의 `ranking_score`가
`rank`의 기준이다. 현재 대표 `Result.attempt_id`가 1차 attempt와 같으면 서술형
확정분까지 반영된 canonical `Result.total_score`/`max_score`가
`ranking_score`와 `final_score`의 공통 원본이다. 현재 대표가 재시험 attempt이면
보존된 1차 snapshot을 두 필드에 사용해 재시험 점수가 1차 결과 목록을 덮지 않는다.
등수도 이 화면에 표시하는 같은 점수 집합으로 다시 계산한다.

1차 `ExamAttempt.meta.initial_snapshot`이 없는 과거 행은
`backfill_initial_snapshot`으로 복구한다. 이 명령은 append-only 1차 attempt의
`meta.total_score`를 현재 대표 `Result`보다 우선한다. 재응시가 이미 있어도 1차
attempt 메타가 남아 있으면 정확한 원점수를 보존하며, 두 출처가 모두 없지는 않은지
실행 전 dry-run 합계로 확인한다. 1차 메타가 없고 재응시 뒤 현재 Result만 남은
경우에만 근사 복구로 표시한다. dry-run은 행 잠금을 잡거나 데이터를 쓰지 않는다.

수동 채점이나 엑셀 반영은 Submission이 없어도 확정 점수가 있으므로 `완료`다.
결시는 `NOT_SUBMITTED`이며 두 점수와 석차를 표시하지 않는다. 응답은 기본
등수순이다. 동점자는 같은 등수이며 다음 등수는 동점 인원만큼 건너뛰는 표준 공동
순위(`1, 2, 2, 4`)를 사용하고, 동률 행 순서는 학생명과 enrollment ID로
안정화한다. 전체 목록 계약은
[data-list-ordering.md](data-list-ordering.md)를 따른다.

수동 점수 입력으로 만든 `ExamAttempt.submission_id=0`은 실제 제출 ID가 아니라
offline placeholder다. DB의 `unique_submission_per_attempt` 제약과 운영
`check_integrity` 감사 모두 이 sentinel을 중복 제출 판정에서 제외하며, 양수인 실제
submission ID만 유일성을 검사한다. `NULL`은 클리닉 직접 입력 경계로 동일하게
제외한다. 무결성 감사는 전체 `ExamResult.manual_overrides`를 순회하므로 뒤쪽 행의
`max_score` 누락도 표본 제한 없이 보고한다.

### 성적 탭 오답 확인 요약

`GET /results/admin/sessions/{session_id}/scores/`의 시험별
`correction_status`는 점수 합불과 클리닉 대상 판정에서 독립된
교사 오답 확인 상태다. 만점이 아닌 시험은 `PENDING` 또는
`COMPLETED`, 만점은 `NOT_REQUIRED`, 미응시·미채점은 `null`을
내려준다. 완료 후 점수나 답안 내용이 바뀌면 source fingerprint가
달라져 다시 `PENDING`이 되며, timestamp만 바뀐 재저장은 완료를
유지한다.

프런트의 결과 카드 우측 상태는 원점수·원제출과 별도로 `교사 통과` 또는
`교사 완료`를 명시한다. 교사가 2자 이상의 사유로 `COMPLETED`를 저장하면
`AssessmentCorrection`이 결정 정본이고, 같은 source의 `ClinicLink`는
`MANUAL_OVERRIDE`로 해소되어 재시험/자동 Clinic 미통과 대상에서 제외된다.
시험 25점과 과제 `score=null` 같은 원자료는 바꾸지 않는다. 해제하면 같은 링크를
미해소로 되돌려 현재 점수·제출 상태로 즉시 재평가한다. 사실상 재시험/과제 통과인
`EXAM_PASS`/`HOMEWORK_PASS` 해소는 더 강한 근거이므로 덮거나 다시 열지 않는다.

저장은 `expected_updated_at` 낙관적 동시성 토큰을 받고 충돌 시 `409`와 최신 시각을
반환한다. tenant·차시 roster·평가 연결·권한을 잠금 안에서 재검증하며, 점수 행이 없는
과제도 배정 행을 잠가 최초 판정 생성 경쟁을 직렬화한다. 사유·사용자·source fingerprint와
append-only 해소 이력을 남긴다. 시험 결과 내용이 바뀌면 교사
통과는 stale로 읽혀 `PENDING` 및 Clinic 재평가로 돌아가며 timestamp-only 재저장은
유지한다.

PostgreSQL 최초 생성 경쟁은
`apps/domains/results/tests/test_assessment_correction_concurrency_pg.py`가 검증한다.

성적표 마지막 열은 이 상태를 집계하여 완료 현황을 보여줄 수 있다.
`Program.feature_flags`의
`score_summary_column_default=exam_wrong`은 해당 테넌트의 기본 표시만
바꿀 뿐이며 그 표시 설정 자체는 `ClinicLink`나 `clinic_required`를 변경하지
않는다. Ymath에는 이 기본값을 적용하고 다른 학원은 기존
종합 판정을 유지한다. 직원이 표시 옵션을 명시적으로 바꾸면
테넌트·사용자별 브라우저 설정이 기본값보다 우선한다.

Ymath의 `Program.feature_flags.assessment_status_display=wrong_completion`은
교사·학생 화면의 시험 판정 표현을 `오답 미완료`와 `오답 완료`로 제한한다.
`PENDING`은 `오답 미완료`, `COMPLETED`와 만점의 `NOT_REQUIRED`는
`오답 완료`로 표시하며, 미응시·미채점 또는 차시를 하나로 확정할 수 없는
`null`은 완료로 추정하지 않고 `채점 대기`로 유지한다. 교사의 완료/해제는
기존 `score-correction` PATCH와 2자 이상 확인 근거를 그대로 사용한다.
이 플래그는 표시와 그 안전한 액션 문구만 바꾸며 `Result`, `ExamAttempt`,
`AssessmentCorrection`, `ClinicLink`, 원점수·문항 답안·재시험 이력을
삭제하거나 마이그레이션하지 않는다. 다른 테넌트의 통과·보강 합격 계약도
그대로 유지한다.

교사 모바일의 **성적 입력**은 같은 차시 성적표 응답을 사용해 학생 이름 검색,
`확인 필요·처리됨·채점 대기` 필터와 처리율을 표시하고, 비만점 결과의 완료 여부를
`PATCH /results/admin/sessions/{session_id}/score-correction/`로 바로 바꾼다. 점수
초안이 남은 행은 점수를 먼저 저장해야 상태를 바꿀 수 있다. 시험별 **성적 조회**의
`GET /results/admin/exams/{exam_id}/results/`도 `correction_status`와
`correction_session_id`를 제공한다. 시험과 수강 강의에 연결된 차시가 정확히 하나일
때만 두 값을 연결하며, 여러 차시가 후보면 다른 기록을 추정하지 않고 `null`로
실패 폐쇄한다.

### 정오 입력

| 화면 표시 | 저장 의미 | 점수 | 오답노트 |
|-----------|-----------|------|----------|
| `O` | 정답 | 문항 만점 | 제외 |
| `X` | 오답 | 0점 | 포함 |
| `오답노트` | 정답이지만 복습 지정 | 문항 만점 | 포함 |

일반 화면은 세 번째 상태를 `오답노트` 또는 좁은 셀에서 `노트`로 표시한다.
기본 단축키와 기존 Ymath 엑셀의 숫자 `0`은 호환 입력이며 숫자 점수가
아니다.
`ResultItem.is_correct=true`,
`ResultItem.include_in_wrong_note=true`로 저장한다.

**전원 결시로 설정**은 조회된 채점표의 로컬 초안만 일괄 변경한다. 조교는
일부 답안만 먼저 받은 경우 전원을 결시로 놓고 제출한 학생만 응시로 되돌린 뒤
정오를 입력할 수 있다. 이 동작은 실행 취소와 전체 초기화가 가능하며,
`apply=false` 미리보기를 거쳐 `apply=true`를 누르기 전에는 결과를 쓰지 않는다.
확정된 결시는 `NOT_SUBMITTED`로 남고 점수·석차·백분위·응시자 평균과 추이에서
제외된다.
세션 시험 요약의 `participant_count`와 합격률 분모도 점수가 확정된 응시자만 포함한다.

### 점수 입력

- 0점부터 해당 문항 만점까지 입력한다.
- 총점·선택형·답변형·문항별 점수 API는 JSON boolean과 `NaN`, 양·음의
  `Infinity`를 숫자로 받지 않는다. 유한한 정수·실수만 저장하며 검증 실패는
  `Result`, `ResultFact`, `ResultItem`을 만들거나 바꾸기 전에 `400`으로 닫는다.
- 선택형 배점 합계가 0인 답변형 전용 시험에는 양수 선택형 점수를 입력할 수 없고,
  답변형 배점 합계가 0인 시험에도 양수 답변형 점수를 입력할 수 없다. 명시적 0을
  시험 총점으로 대체하지 않는다.
- 오답노트 문서의 수강·강의·시험 ID와 시작·종료 회차는 명시적 0을 누락값이나
  기본 회차로 바꾸지 않는다. 1 미만 또는 역전된 범위는 문서 작업과 큐 발행 전에
  `400`으로 거부한다.
- 문항 만점과 같으면 정답, 그보다 낮으면 오답으로 판정한다.
- 부분점수와 0점 문항은 오답노트에 포함한다.
- 만점 문항도 **복습**을 켜면 정답으로 유지하면서 오답노트에 포함한다.

### 선택형·혼합형과 결시

- `choice` 시험의 정오와 점수는 OMR 자동채점 결과로 조회할 수 있지만
  직접 채점 표에서는 수정할 수 없다. 인식 오류는 OMR 검토에서 학생 답안을
  보정한 뒤 기존 재채점 경로로 정오·점수·통계를 다시 계산한다.
- `written` 시험은 모든 문항을 수정할 수 있다.
- `mixed` 시험은 `question_kind=essay` 문항만 수정할 수 있다. 선택형
  `ResultItem`은 OMR 값으로 잠기며, 선택형 OMR 결과가 완전하지 않으면
  답변형 성적 확정을 거부한다.
- 학생을 `absent`로 확정하면 `NOT_SUBMITTED` attempt로 저장하고 점수,
  평균, 석차, 합불, 문항 통계와 시험 요약의 응시자 수에서 0점 응시자로 계산하지 않는다.
- 이 전환은 같은 transaction에서 대표 결과의 `total_score`와 `objective_score`를
  0으로 정규화하고 문항 snapshot을 제거한다. 이전 실패 클리닉 링크는
  `resolution_type=NOT_SUBMITTED` 감사 이력으로 닫고 오늘 대상 선택만 무효화한다.
  이미 예약·등원·완료된 클리닉 참여 사실은 취소하지 않으며 전환 알림도 보내지
  않는다. 동일 요청 재시도는 no-op 성공하고 이후 점수 입력은 합법적인 명시 재개다.
  진도와 클리닉 파생 판정은 현재 대표 시도의 상태만 사용하므로, 과거 비대표
  `NOT_SUBMITTED` 시도는 정상 재응시 점수를 다시 미응시로 되돌리지 않는다.
- 일반 시험 재계산은 submission 다음 수강·결과·attempt 순서로 잠근 뒤
  `NOT_SUBMITTED`를 다시 확인한다. 수동 채점도 수강·결과·attempt 순서를 사용하므로
  두 경로가 역순 잠금으로 교착하지 않는다. 명시적인 응시 재개 없이 결시 attempt를
  reopen하거나 점수·`ResultItem`·오답·클리닉을 다시 만들지 않는다. 재계산과 결시
  확정이 동시에 실행되면 먼저 잠금을
  얻은 작업 뒤에 두 번째 작업이 최신 상태를 다시 읽어 최종 결시 상태를 보존한다.

AI OMR 성공 콜백은 인식 fact를 저장한 뒤 같은 worker 프로세스에서 채점과
`Result` 동기화를 닫는다. 이 동기화는 문항별 최신 `ResultItem`과 append-only
`ResultFact`를 같은 transaction에서 함께 저장하므로 점수 목록과 문항 분석이
부분 성공으로 갈라지지 않는다. 수동 검토가 필요하지 않은 OMR은 legacy
`ExamResult`도 `FINAL`로 확정한 다음 진행도와 수업 분석을 갱신하며, 검토 필요
표시가 있는 OMR만 DRAFT로 유지한다. CPU/GPU AI worker 이미지는 API 전용 DRF를 설치하지
않으므로 이 경로의 점수 편집 임대 무효화는 Django ORM만 의존하는
`score_edit_lease_state`를 사용한다. worker 이미지 빌드와
`tests/test_worker_entrypoint_imports.py`는 DRF가 없는 환경에서
`grading_service` import가 성공해야 통과한다.

문항 순서는 유형별 블록으로 재정렬하지 않는다. 예를 들어
`1 객관식 / 2 숫자 단답형 / 3 객관식`은 그대로 반환한다. 각 문항에는
`kind`와 함께 다음 `answer_type`을 제공한다.

- `choice`: 선택지 답안을 쓰는 객관식
- `numeric_short_answer`: 수학 시험에서 정답지가 `0~999` 정수인 단답형
- `written`: 그 밖의 답변형·서술형

`answer_type`은 표시와 입력 안내용이며, 수정 가능 여부는 기존
`editable`과 `entry_method` 계약을 따른다. 따라서 자동채점된 문항도
정오표에서 결과를 볼 수 있고, `choice` 전체와 `mixed` 선택형은 조회만
가능하다. 자동채점 답안 보정은 직접 채점 표가 아니라 OMR 검토가 소유한다.

채점 표의 문항 머리글에서는 직접 채점 가능한 문항의 배점을 함께
수정할 수 있다. 요청은 현재 배점을 `expected_question_scores`, 변경
배점을 `question_scores`로 함께 보내며, 유효 배점 합계와 시험 단위
가감점 합계가 시험 만점과 0.01점 이내로 일치해야 한다. 미리보기는
변경 배점으로 점수만 다시 계산하고 문항을 쓰지 않으며, 확정 때 학생
결과와 배점을 같은 transaction에서 반영한다. 현재 배점이 기대값과
다르면 stale 변경으로 거부한다.

문항이 하나도 없는 시험의 조회는 오류 대신 빈 `questions`를 반환한다.
관리자 화면은 이 상태에서 기존 객관식 답안 등록과 문항 수 기반 빠른
시작을 제공한다. 빠른 시작은 시험 자체 문항 구조를 먼저 만든 뒤 같은
채점 표를 다시 불러온다.

## 확인과 확정의 분리

`GET /results/admin/exams/{exam_id}/manual-grading/`은 tenant 안의 시험
대상 학생, 실제 문항, 기존 결과와 `expected_version`, 시험 만점,
현재 문항 배점 합계와 가감점 합계를 읽어 채점 표를 만든다.

관리자 화면은 수정 가능한 문항 배점을 문항 순서대로 쉼표·공백·줄바꿈으로
한 번에 입력할 수 있다. 입력 개수, 0 이상의 숫자, 고정 문항과 시험 가감점을
포함한 최종 합계가 시험 만점과 같은지를 클라이언트에서 먼저 검증한다. 적용은
개별 헤더 입력과 같은 `question_scores`/`expected_question_scores` 계약을 사용하며,
서버의 현재 배점 비교와 원자적 확정 검증을 우회하지 않는다.

같은 URL의 `POST`는 두 단계다.

1. `apply`가 없거나 false면 학생·문항·점수·결시·덮어쓰기 여부를
   검증하고 예상 결과만 반환한다. 이 단계는 DB를 변경하지 않는다.
2. 오류가 없는 동일 payload에 `apply=true`를 보내면 한 transaction에서
   전부 확정한다.

확정 시 `Result`, `ResultItem`, `ExamAttempt`를 갱신하고 실제 변경 문항은
append-only `ResultFact(source=manual_grid)`로 남긴다. transaction
commit 후 진행도 파이프라인을 요청한다.

각 학생의 `expected_version`이 현재 `Result.updated_at`과 다르면 다른
화면에서 결과가 바뀐 것으로 보고 전체 확정을 중단한다. 성적 편집 lease가
충돌하거나 한 학생이라도 대상·문항·값 검증에 실패해도 일부 행만 저장하지
않는다.

성적 편집 lease는 동일 시험을 공유하는 세션 묶음을 ID 순서로 잠가 서로
다른 화면의 쓰기를 직렬화한다. 세션 PK를 바꾸지 않으므로 PostgreSQL
`FOR NO KEY UPDATE`를 사용한다. 이는 편집 충돌 차단은 유지하면서
`SessionProgress`와 임시저장처럼 세션 FK를 쓰는 transaction의 지연 FK
검사와 교착하지 않게 한다. 운영 회귀 검증은 실제 PostgreSQL에서 첫
편집자가 세션 잠금과 FK 쓰기를 보유한 동안 두 번째 편집자가 같은 잠금을
기다리는 두-thread 시나리오로 수행한다.

다른 조교의 빈 초안은 시험 편집권을 선점하지 않는다. 시험 합산·객관식·주관식·
문항 입력도 `(enrollment_id, exam_id, 입력 종류, question_id)`의 정확한 셀 단위로
점유한다. 계정이나 기기가 같거나 달라도 같은 셀의 선택·미저장 변경만
`409 SCORE_EDIT_LOCKED`로 막고, 다른 학생·시험·입력 종류의 셀은 동시에 편집할 수
있다. 클라이언트는 다른 편집자의 `active_cell`만 비활성화하고 나머지 성적표와
로컬 초안을 유지한다. 과제를 포함한 공통 초안·복구 계약은
[homework-grading.md](homework-grading.md)가 소유한다.

권한은 인증된 같은 tenant의 teacher/admin으로 제한한다. 시험과 학생
후보 조회는 tenant와 차시 roster를 벗어나지 않으며 기본 tenant나
cross-tenant fallback을 사용하지 않는다.

## 기존 엑셀 채점표 호환

`GET /results/admin/exams/{exam_id}/result-import/template/`에서 전용 양식을
받고, `POST /results/admin/exams/{exam_id}/result-import/`에서 미리보기
후 `apply=true`로 확정한다. 직접 채점 표와 동일한 결과·통계 경로를 쓴다.

- 일반 양식은 빈칸 또는 `O`를 정답, `X`를 오답으로 읽는다.
- Ymath 양식은 문항 셀의 `.`을 오답, 숫자 `0`을 정답·복습 지정으로
  읽는다. 응시 여부 열의 `.`은 결시다.
- 모든 문항이 빈 행은 응시 여부가 확인되어야 만점과 결시를 구분한다.
- 여러 시트가 서로 다른 학생 집합이면 안전하게 합친다. 같은 학생이
  겹치는 후보 시트가 둘 이상이면 임의 선택하지 않고 오류로 중단한다.
- 동명이인이나 공용 연락처처럼 한 학생으로 확정할 수 없으면 전용 양식의
  `수강등록ID`를 요구한다.
- 미리보기는 쓰지 않고, 확정은 전체 transaction으로 반영한다.

## 학생별 오답 엑셀 내보내기

교직원은 시험의 **채점·결과 → 통계 → 학생별 틀린 문항 (엑셀)**에서
현재 사이트에 저장된 대표 성적의 오답 기록을 `.xlsx`로 내려받는다.
`GET /results/admin/exams/{exam_id}/wrong-note-export/`는 인증된 같은 tenant의
teacher/admin만 허용하며, 시험 roster 밖의 수강 등록이나 다른 tenant의 시험을
추정하거나 합치지 않는다.

파일은 오답 또는 복습 지정 문항이 있는 학생만 한 행씩 싣는다. `is_correct=false`는
**오답**, `is_correct=true && include_in_wrong_note=true`는 **복습 지정**으로 분리하고,
학교·이름·강의·총점·문항 번호·문항별 점수·학생 답안·사이트 최종 저장 시각을 함께
기록한다. 현재 `Result` 대표 snapshot만 읽기 때문에 재채점으로 해소된 과거 오답은
다시 나타나지 않는다. 내보낼 현재 기록이 없으면 빈 파일을 성공으로 반환하지 않고
사용자에게 안내 가능한 검증 오류로 실패한다. 파일은 조회용이며 업로드나 성적 수정은
기존 정오표/엑셀 가져오기 계약이 계속 소유한다. 학생 정보가 포함된 응답은
`Cache-Control: private, no-store`로 반환한다.

## 수업 분석 리포트 Excel

교직원은 시험의 **채점·결과 → 이번 수업에서 바로 결정할 것 → 수업 분석
리포트 (엑셀)**에서 1차 성적을 수업 전달용 `.xlsx`로 내려받는다.
`GET /results/admin/exams/{exam_id}/analysis-export/`는 인증된 같은 tenant의
teacher/admin만 허용하고, 시험 roster와 현재 대표 `Result` 밖의 학생을
추정하거나 다른 tenant의 통계를 섞지 않는다.

리포트는 다음 네 시트를 한 파일로 제공한다.

- **수업 브리핑**: 확정 응시·미응시/미채점, 평균·중앙값·상위 10%·표준편차,
  1차 합격률, 보충 완료 인원, 만점 대비 점수 분포, 보충 우선 문항과 수업 행동 제안
- **문항 우선순위**: `ResultFact` 문항 통계의 정답률이 낮은 순서와
  공통 재설명·재풀이·유사 문항·개별 확인 제안
- **학생별 등수**: 서버 석차와 같은 1차 점수, 학교·강의, 득점률·평균 대비,
  합격/보충 대상/보충 완료·복원 가능한 1차 결과의 오답 문항
- **학생별 답안**: 1차 `ResultFact` 또는 아직 대표인 1차 `ResultItem`의 학생 답안과
  정오 상태. 1차 문항을 안전하게 복원할 수 없으면 재시험 답안을 섞지 않고 미입력 처리

점수 분포와 편차 판단은 시험 만점이 100점이 아니어도 득점률로 정규화한다.
미응시, 채점 중, 채점 실패, 미확정 결과는 분석 모수에서 제외한다. 점수 브리핑과
분포·학생별 등수는 현재 대표 결과가 1차 시도이면 서술형 확정분을 포함한 canonical
`Result.total_score`와 `Result.max_score`를 사용한다. 따라서 자동 채점 직후 저장된 초기 snapshot보다
교사가 같은 1차 시도에 확정한 총점이 우선한다. 현재 대표 결과가 재시험이면 보존된
1차 snapshot을 사용해 재시험 점수가 1차 분석을 덮지 않으며, 학생별 답안과 오답
문항도 복원 가능한 1차 결과만 사용한다. 문항 통계도
채점 완료된 1차 시도만 집계해 재시험 횟수가 응시자 수처럼 중복되지
않는다. `pass_score <= 0`은 합격 기준 미설정이므로 합격·미달을 만들지 않고 기준
설정을 안내한다. 합격 판정은 1차 점수와 결과 수강 강의의 유효 기준
(`ExamLecturePolicy.pass_score`, 없으면 `Exam.pass_score`)을 사용하되,
`MANUAL_OVERRIDE`나 재시험 통과로 해소된 학생은 원점수를 바꾸지 않고 **보충 완료**로
분리한다. 공동 등수는 같은 점수에 같은 등수를 주고 다음 등수는 동점 인원만큼
건너뛰는 competition rank를 따른다. 응시 5명 미만은 표본 확인을
우선하고, 정답률 30%/50%와 만점 대비 표준편차 20%를 수업 제안의 설명 가능한
경계로 사용한다. 이 제안은 조회·전달용이며 컷, 합격 판정, 재시험 정책이나
성적 데이터를 자동 수정하지 않는다. 학생 답안과 이름은 Excel 수식 주입을
막고, 응답은 `Cache-Control: private, no-store`로 반환한다.

## 오답노트와 통계

오답노트 대상은 `ResultItem.is_correct=false` 또는
`include_in_wrong_note=true`다. 따라서 오답노트/복습 지정 문항은 점수와
정답률에는 정답으로 남으면서 학생 오답노트에는 포함된다. 재채점으로
오답도 아니고 복습 지정도 아닌 상태가 되면 누적 오답노트에서 빠진다.

결시를 제외한 확정 결과는 기존 시험 요약, 문항 통계, 합격 판정과
진행도 파이프라인이 읽는다. 선택형·답변형·혼합형이 별도 통계 저장소를
만들지 않는다.

현재 교직원 화면은 단일 시험 또는 수강 강의의 시작~종료 회차를 선택하고
최대 100문항의 PDF 또는 HWPX를 만든다. 시작과 종료 회차는 모두 포함하며 종료 회차를
비우면 시작 회차부터 현재까지 누적한다. 조회와 PDF 생성은 동일한
`from_session_order`/`to_session_order` 규칙을 사용하고 `1 <= from <= to`를
검증한다. 같은 시험이 여러 회차에 연결되어도 문항은 한 번만 싣고, 선택
범위 안의 가장 이른 회차를 표시한다.

학생 상세의 **통합 오답노트**는 같은 학생의 여러 수강 강의에서 시험과
워크북을 각각 선택해 한 문서로 묶는다. 선택 원본은
`{type: exam|homework, id, enrollment_id}`로 고정하며, 서버가 모든 enrollment의
student·tenant와 각 시험의 강의 연결 또는 과제 배정을 다시 검증한다. 워크북은
`HomeworkScore.meta.question_marks`에서 X 또는 O·복습 문항을 읽고 연결된 비노출
원본 시험의 문제·선생님 해설 이미지를 사용한다. 기존 단일 시험·회차 API는
`source_selection=[]`일 때 그대로 동작한다.

여기서 회차는 화면 배치 순서인 `Session.order`가 아니라 정규 수업 번호인
`Session.regular_order`다. 보강(`session_type=SUPPLEMENT`)은 정규 회차 범위에
포함하지 않는다. 따라서 정규 수업 사이에 보강을 삽입하거나 카드를
재배치해도 `2~3회차`는 정규 2차시와 3차시만 뜻하며, 조회 결과의
`session_order`도 같은 정규 수업 번호를 반환한다.

API는 호환 이름을 유지한 `WrongNotePDF`와 tools worker job을 tenant 범위에서
기록해 비동기로 생성하고, 완료 뒤 형식별 R2 attachment URL을 반환한다. 출력
job에는 선택한 시작·종료 회차, `pdf|hwpx` 형식과 요청 시점의
`source_fingerprint`를 저장한다. 선택형 job은 정확한 `source_selection`도 저장한다.
fingerprint는 원본 유형·ID·수강 등록, 대표 오답, 답안·점수, 문항·해설
내용과 저장 객체 식별자를 SHA-256으로 묶으며 만료형 조회 URL은 제외한다.
기존 단일 수강 job에는 선택형 출처 필드를 추가하지 않아 롤링 배포 전후의
fingerprint가 동일하며, 새 통합 선택 job만 원본 유형·ID·수강 등록을 포함한다.
조회 응답의 fingerprint를 생성 요청에 보내면 서버가 최신 목록과 비교하고,
이미 재채점되었거나 문항·해설이 바뀌었으면 job을 만들지 않고 `409`로 최신
조회부터 다시 하도록 안내한다. queue payload도 같은 fingerprint를 담고 worker는
렌더 직전에 다시 계산해 불일치하면 파일을 만들지 않는다. 배포 전 생성된 빈
fingerprint job은 기존 동작으로 처리한다. 5분 이상 갱신되지 않은 `PENDING` 또는
`RUNNING` job은 새 요청 transaction에서 `FAILED`로 닫은 뒤 새 generation을 만든다.
callback은 tenant와 job ID를 다시 검증하고 `PENDING|RUNNING`에서만
`DONE|FAILED`로 compare-and-set한다. 따라서 이미 `FAILED`로 닫힌 이전 generation의
늦거나 중복된 성공·실패 callback은 상태, 오류, 파일 경로를 되살리거나 덮어쓰지
않으며 다운로드에는 새 generation이 확정한 파일만 노출된다. 이 규칙은 기존
운영 row를 일괄 수정하지 않고 이후 callback 전이에만 적용한다. 두 형식 모두
앞쪽은 답이 없는 문제와 풀이 공간, 뒤쪽은 분리 표지 뒤의 정답 및 선생님 원본
해설이다. 문제 구간은 Ymath 학생 배포 양식을 기준으로 세로 A4의 좌우 2단을
동일 폭으로 쓰고, 바깥 여백 16mm와 단 사이 8mm를 둔다. 원본 문항 하나를
한 단 안에 종횡비를 유지해 온전히 배치하며 왼쪽에서 오른쪽 순으로 두 문항을
한 쪽에 싣는다. 홀수 마지막 문항의 오른쪽 단은 비워 두고, 남는 단 높이는
풀이 공간으로 사용한다. 해설 구간은 필기와 수식의 가독성을 위해 다시 A4
1단·한 문항씩 배치한다. HWPX는 문제·선생님 필기 해설 원본을 이미지로
보존하면서 제목, 정답,
`내 풀이 메모`, `추가 메모`를 한글 문단으로 제공한다. 이 문단은 한글에서 직접
편집할 수 있다. 픽셀 원본의 수식·필기는 충실도를 훼손할 수 있는 OCR 변환을 하지
않으므로 편집 가능한 한글 수식·필기 개체로 재구성한다고 약속하지 않는다.
HWPX 문제 구간은 한 구역의 2단 흐름과 명시적 단 나눔으로 문항마다 다음 단을
시작하고, 해설 조각은 한 구역·한 쪽으로 만든다. 모든 원본 이미지를
`Contents/content.hpf`의 embedded BinData로 등록해 두 번째 이미지부터 누락되는
한글 렌더 실패를 막는다. PDF와 HWPX가 같은 2단 문제 순서를 사용하며 기존 PDF
API는 호환 별칭으로 유지한다.

문제 쪽은 원본 종횡비에 따라 이미지 높이를 결정하고 남은 면적을 줄이 있는
풀이 공간으로 확장한다. 해설 쪽은 흰 여백을 제거한 뒤 A4에서 읽을 수 없는
세로로 긴 원본을 저밀도 행 경계에서 여러 쪽으로 나누며, 내용이 거의 없는
조각은 버리고 작은 마지막 조각은 앞쪽과 다시 합친다. PDF와 HWPX 모두 원본
문제·필기 해설을 임의 생성하거나 재서술하지 않는다.

## API 요약

시험 운영 설정을 수정하는 현재 화면은 조회 응답의 `updated_at`을
`X-Expected-Updated-At` 헤더로 보낸다. 서버는 해당 시험 행을 잠근 뒤 같은
버전일 때만 저장하고, 다른 화면이 먼저 저장했으면 `409`와
`code=stale_resource`, 현재 `updated_at`을 반환한다. 헤더가 없는 기존
클라이언트는 호환을 위해 기존 동작을 유지한다. 성공 응답은 부분 수정
필드만이 아니라 전체 `Exam` 표현을 반환하므로 클라이언트 캐시가 누락
필드를 기본값으로 오인하지 않는다.

| Method | Path | 역할 |
|--------|------|------|
| POST | `/exams/` | 시험과 채점 계약 생성 |
| PATCH | `/exams/{id}/` | 채점 방식·학생 성적 공개 전환. 문항·정답·기존 결과는 보존 |
| POST | `/exams/pdf-extract/` | 필수 문제지 `file`, 선택 정답지 `answer_file`, 선택 해설지 `explanation_file` 원본 보관 및 번호 맞춤 요청 |
| GET | `/exams/{id}/segmentation-review/` | 문항·인식 정답·원본 해설, partial/누락/불일치, 만료형 원본 URL 조회 |
| POST | `/exams/{id}/segmentation-review/approve/` | 번호·정답·제외·해설 variant를 반영해 빈 시험 문항·AnswerKey·원본 해설 확정 |
| GET | `/results/admin/exams/{id}/manual-grading/` | 직접 채점 표와 버전 조회 |
| POST | `/results/admin/exams/{id}/manual-grading/` | 직접 채점 미리보기 또는 원자적 확정 |
| GET | `/results/wrong-notes/sources/?student_id=` | 학생의 모든 강의에서 선택 가능한 시험·워크북과 수록 문항 수 조회 |
| POST | `/results/wrong-notes/preview/` | 선택 원본의 통합 문항과 fingerprint 미리보기 |
| POST | `/results/wrong-notes/documents/` | 기존 enrollment 범위 또는 학생 `source_selection` 통합 문서 job 생성 |
| GET | `/results/admin/exams/{id}/result-import/template/` | 시험 전용 엑셀 양식 다운로드 |
| POST | `/results/admin/exams/{id}/result-import/` | 엑셀 미리보기 또는 원자적 확정 |
| GET | `/results/admin/exams/{id}/analysis-export/` | 수업 브리핑·분포·문항 우선순위·등수·답안 XLSX 다운로드 |
| GET | `/results/admin/exams/{id}/wrong-note-export/` | 현재 대표 성적의 학생별 오답·복습 지정 기록 XLSX 다운로드 |
| GET | `/results/wrong-notes` | 학생의 현재 대표 오답·복습 문항과 안정적인 `source_fingerprint` 조회. `from_session_order`~선택적 `to_session_order` 포함 |
| POST | `/results/wrong-notes/documents/` | `output_format=pdf|hwpx`, 회차 범위와 선택적 `source_fingerprint` 검증 뒤 비동기 문서 job 생성 |
| GET | `/results/wrong-notes/documents/{job_id}/` | 형식·파일명·상태와 attachment URL 조회 |
| POST/GET | `/results/wrong-notes/pdf/...` | 기존 PDF 클라이언트 호환 별칭 |

## 집중 검증

```powershell
python manage.py test `
  apps.domains.exams.tests.test_exam_policy_update `
  apps.domains.exams.tests.test_guided_exam_source_workflow `
  apps.support.results.tests.test_manual_exam_grading `
  --settings apps.api.config.settings.test

python -m pytest tests/test_pdf_question_pipeline_regression.py tests/test_hwp_endnote_images.py -q

python -m pytest tests/results/test_exam_result_excel_import.py -q

python manage.py test `
  apps.domains.results.tests.test_wrong_note_service `
  apps.domains.results.tests.test_security_regression `
  --settings apps.api.config.settings.test

pwsh scripts/v1/run-wrong-note-development-canary.ps1 `
  -ExpectedReleaseId <release-id> `
  -ExpectedApiDigest <api-digest> `
  -ExpectedToolsDigest <tools-digest> `
  -RunId <run-id> -RunAttempt <attempt> -Ci
```

검증은 PDF/HWP/HWPX 처리 상태, 검수 전 proposal, 승인·번호 변경·제외·tenant 차단,
압축 HWP 이미지, HWPX 미주 원본과 일부 미주 그림 누락의 실패 폐쇄, Ymath
실자료, 잠긴 시험 보호, 정오·부분점수,
공통 dispatcher 크롭 재사용, 짧은 복습지 표지 제외,
후행 정답·해설의 문항 제외와 연속 해설 추출,
오답노트와 기존 `0` 호환 의미, 선택형 자동채점 정오 조회·직접 수정 차단과 OMR 보정 경계,
객관식·숫자 단답형이 섞인 원래 순서와 `answer_type`, 문항 배점
합계·stale 배점 거부, 과거 혼합형 경계 복구, 시험 설정 stale version 거부와
전체 PATCH 응답, 혼합형 OMR 보존, stale result version 거부,
다중 시트 선택, tenant 차단, 양끝을 포함하는 회차 범위, 다중 회차 시험의
중복 제거, 오답노트 포함과 PDF/HWPX 문제·해설 분리, worker/R2 상태를 포함한다.
마지막 canary는 disposable 두 tenant에서 실제 SQS 소비와 terminal message 삭제,
PDF download/reload, cross-tenant denial을 확인한 뒤 tenant/user/AI job/audit/R2
잔여가 모두 0이어야 통과한다.
Ymath 전체 원본을 운영 데이터 없이 persistent development에서 재현하는 절차와
합격 기준은 [Ymath 실자료 원본 전수 검증](../operations/runbooks/ymath-real-source-qa.md)을
따른다. `scripts/exam_source_bundle.py`, `scripts/exam_source_hwp_qa.py`,
`scripts/ymath_realuse_scenario.py`가 각각 원본 인벤토리, 미주 구조/미리보기,
실제 HTTP 시나리오와 재시작 상태를 소유한다.
