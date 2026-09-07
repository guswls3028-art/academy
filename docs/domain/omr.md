# OMR 자동채점 시스템 — SSOT

## 개요

학원 시험의 객관식은 OMR 버블로 수집해 AI 워커로 자동 채점하고, 서술형은 같은 답안지의 넓은 작성칸에 적어 교사가 수동 채점하는 시스템.

## SSOT 구조

| 구성요소 | SSOT 파일 | 역할 |
|----------|-----------|------|
| **시험지 계약** | `backend/academy/domain/omr/contract.py` | OMR이 읽을 객관식 경계와 표시할 서술형 경계, 문제 ID 매핑, 계약 fingerprint |
| **계약 빌더** | `backend/apps/support/omr/contract_builder.py` | `Sheet`/`ExamQuestion`/`AnswerKey`에서 `OMRSheetContract` 생성 |
| **답안지 렌더링** | `backend/apps/domains/assets/omr/renderer/pdf_renderer.py` | 인쇄/PDF용 OMR 답안지. 표시용 영역은 채점 계약과 분리 |
| **좌표 메타** | `backend/apps/domains/assets/omr/services/meta_generator.py` | mm 단위 버블/ROI 좌표 (AI 워커용) |
| **답안 검출** | `backend/academy/adapters/ai/omr/engine.py` | 스캔 이미지에서 마킹 감지 |
| **식별자 검출** | `backend/academy/adapters/ai/omr/identifier.py` | 전화번호 뒤 8자리 감지 |
| **결과 반영** | `backend/apps/domains/submissions/services/ai_omr_result_mapper.py` | 워커 결과를 답안/fact/학생매칭 상태로 반영 |
| **인식 fact** | `backend/apps/domains/submissions/models/omr_fact.py` | 인식 run, 문항별 감지값, 학생 매칭 fact 저장 |

## OMR v2 계약 구조

OMR은 더 이상 `total_questions` 하나를 여러 서비스가 각자 해석하지 않는다. 모든 런타임 경로는 먼저 `OMRSheetContract`를 만든 뒤 그 계약을 기준으로 렌더링, 워커 payload, 답안 저장, 인식 fact, 채점 준비도를 판단한다.

```mermaid
flowchart LR
    A["Sheet / ExamQuestion / AnswerKey"] --> B["OMRSheetContract"]
    B --> C["PDF renderer defaults"]
    B --> D["AI worker payload + template_meta"]
    B --> E["qnum -> ExamQuestion.id mapping"]
    D --> F["AI worker recognition"]
    F --> G["SubmissionAnswer compatibility rows"]
    F --> H["OMRRecognitionRun / OMRDetectedAnswer facts"]
    G --> I["grading readiness"]
    H --> I
    I --> J["ExamResult / Result sync"]
```

### 계약 원칙
- `total_questions`는 시험 전체 문항 수다.
- `choice_count`는 객관식 문항 수다.
- `essay_count`는 서술형 문항 수다. 필드명은 기존 API/데이터 계약 호환을 위해 유지한다.
- `ExamQuestion.question_kind`가 모든 문항에 있으면 문항 번호별 `choice`/`essay` 값이 경계보다 우선한다. 따라서 `1 객관식 / 2 서술형 / 3 객관식`처럼 섞인 순서를 그대로 보존한다.
- 인쇄 영역은 객관식 버블과 서술형 빈 작성칸을 물리적으로 나누고 각 행에 시험의 실제 문항 번호를 표시한다. AI 좌표와 자동판정은 객관식에만 존재한다.
- 수학 `0~999` 정답키도 종이 OMR에서는 숫자 버블을 만들거나 인식하지 않는다. 기존 답안키와 결과 데이터는 보존하고 온라인 응시 자동채점은 유지하되, 종이 OMR 서답형은 교사가 수동 채점한다.
- `essay_count=0`인 객관식 전용 시험은 객관식 40문항 이하에서 사용자가 기본 5줄짜리 `서술형 작성 공간`을 표시하거나 숨길 수 있다. 41문항부터는 3열 객관식 레이아웃을 우선해 자동으로 숨긴다.
- 표시 선택은 요청의 `include_optional_essay_area`에만 존재하는 렌더링 옵션이다. `OMRSheetContract`, 워커 payload, 정답키, 좌표 메타, 답안 저장, 채점에는 포함하지 않는다.
- 독립 `/tools/omr`와 시험 연결 OMR은 모두 `essay_count` 호환 필드를 서술형 기입란 수로만 사용한다. 해당 영역에는 숫자 버블이나 인식 좌표를 만들지 않으며 AI 자동판정 대상에 포함하지 않는다.
- OMR 자동인식 답안 저장은 계약에 등록된 객관식 번호만 허용한다.
- 워커가 일반 서술형 번호를 보내면 그 값은 fact로는 남기되 `SubmissionAnswer`로 저장하지 않는다.
- 워커 원본 답안 수와 계약상 `auto_detect_count`가 다르면 `ANSWER_COUNT_MISMATCH`로 수동 검토가 필요하다.
- `OMRRecognitionRun.contract_snapshot`은 인식 당시 계약의 fingerprint, 객관식/서술형 경계, `auto_detect_count`를 저장한다.

### 레거시 호환
- 모든 문항에 `question_kind`가 저장된 신규 시험은 문항별 유형을 우선한다.
- `Sheet.choice_count`/`essay_count`가 있으면 그 값을 우선한다.
- deprecated `POST /exams/{id}/generate-omr/`도 시험 소유권은
  `Exam.tenant`로 판정하므로 차시가 없는 같은 tenant 템플릿을 지원하고 다른
  tenant 시험은 노출하지 않는다.
- 레거시 생성 요청의 명시적 `mc_count=0`/`essay_count=0`은 기본값으로
  바꾸지 않는다. 숫자가 아니거나 각 최대 범위를 벗어난 값과 5지선다 외
  `n_choices`는 500이 아니라 필드별 `400` 검증 오류를 반환한다.
- 좌표 메타와 OMR 자산 목록의 쿼리도 같은 방식으로 `question_count`,
  `essay_count`, `n_choices`, `exam_id`를 검증해 잘못된 숫자는 필드별 `400`으로
  실패하고 내부 `ValueError`를 노출하지 않는다.
- 구형 sheet처럼 경계가 없으면 `AnswerKey`를 기준으로 객관식 답(`1~5`, `2,3`, `2|4`, `①` 등)과 비객관식 텍스트를 구분해 경계를 추론한다.
- 추론도 불가능하면 기존 호환을 위해 `total_questions` 전체를 객관식으로 본다.

### 검증 매트릭스
- 객관식 전용: `12/0`, `20/0`, `30/0`은 표시용 `서술형 작성 공간`을 선택할 수 있고, `60/0`은 자동으로 숨긴다. 표시 공간은 채점에 포함되지 않는다.
- 혼합형: `20/5`는 q1~q20 객관식만 자동 인식하고 q21~q25는 번호가 붙은 빈 서술형 작성칸으로 렌더링
- 임의 순서 혼합형: `1 choice / 2 essay / 3 choice`는 객관식 번호 `[1,3]`만 인식하고 서술형 번호 `[2]`는 수동 채점
- 서술형 전용: `0/20`까지 한 페이지에 생성할 수 있으며 모든 행은 버블 없는 작성칸
- 레거시 추론: `①`, `2,3`, `2|4`, 단답형 텍스트 혼합 answer key
- 워커 이상 응답: 원본 답안 수와 계약 객관식 수 불일치 시 `ANSWER_COUNT_MISMATCH`
- 워커 이상 응답: 원본 답안 수가 맞아도 중복 문항 때문에 고유 객관식 번호 수가 부족하면 `ANSWER_COUNT_MISMATCH`
- 운영 완료 판정: 배포 후 deploy verification, 운영 sheet 분포 read-only audit, Tenant 2 실제 OMR canary 재인식/재채점 또는 rollback 검증

## 레이아웃 (A4 Landscape, 297×210mm)

```
┌──────────────────────────────────────────────────────────┐
│ L-mark                                           L-mark │
│                                                          │
│  ┌─Left(62mm)──┐  3mm  ┌─MC(44mm)─┐ 2.5 ┌─MC(44mm)─┐  │
│  │ [Logo]      │       │ 1  12345 │     │16  12345 │  │
│  │ 시험명      │       │ 2  12345 │     │17  12345 │  │
│  │             │       │ ...      │     │ ...      │  │
│  │─────────────│       │15  12345 │     │30  12345 │  │
│  │ 성명        │       └──────────┘     └──────────┘  │
│  │─────────────│                                       │
│  │ 전화8자리   │       ┌─서술형 답안─────────────────┐│
│  │ [XXXX-XXXX] │       │ 1  [넓은 빈 작성칸]         ││
│  │ [0-9 버블]  │       │ 2  [넓은 빈 작성칸]         ││
│  │─────────────│       │ ...                          ││
│  │ 작성안내    │       └──────────────────────────────┘│
│  └─────────────┘                                       │
│                                                          │
│ L-mark                                           L-mark │
└──────────────────────────────────────────────────────────┘
```

## 좌표 체계 (meta_generator.py)

### 페이지 상수
- 페이지: 297mm × 210mm
- 마진: 좌10, 상9, 우10, 하6 (mm)
- 좌측 패널: 62mm 폭
- 객관식 컬럼: 44mm 폭 (고정), 최대 3컬럼
- 컬럼 간격: 2.5mm

### 버블
- 쌀톨형 세로 타원: 3.6mm × 5.2mm
- 선택지: "1"~"5" (숫자)
- 식별번호: "0"~"9" (세로 10행 × 가로 8열)

### 식별자 (전화번호 뒤 8자리)
- 4자리 - 4자리 구조
- 기입칸 8개 + 아래 0~9 버블 그리드
- 학생 본인 휴대폰 번호 우선, 없으면 부모님 번호

## 채점 대상 SSOT

- 시험에 `ExamEnrollment`가 하나라도 있으면 그 명시 명단이 OMR·수동 채점 대상의
  정본이다. 비대상 학생은 같은 차시에 있어도 후보·성적·미응시·클리닉 판정에
  포함하지 않는다.
- 명시 대상자가 전혀 없는 기존 시험만 연결 차시의 `SessionEnrollment` roster를
  호환 대상으로 사용하며, 실제 OMR 매칭/수동 입력 시 `ExamEnrollment`를 만든다.
- 후보 학생은 항상 같은 tenant, 활성 `Enrollment`, 삭제되지 않은 학생으로 제한한다. 다른 tenant나 시험이 연결되지 않은 차시의 학생으로 fallback하지 않는다.
- 성적탭 row 모수는 차시 출석/수강 roster다. 각 시험 점수 셀은 명시 대상자에게만
  보이고, 명시 명단이 없는 기존 시험에 한해 차시 roster 전체에 보인다.
- 오인식/미식별 스캔은 `Submission`의 수동 검토 상태와 답안 보정 API를 통해 보정한다. 원본 운영 데이터를 임의로 수정하지 않고, 검토자가 선택적으로 답안/점수를 확정한다.
- 학생이 이미 후보로 연결된 fuzzy match도 답안 변경 여부와 무관하게 같은 수동 보정 API에서 현재 `enrollment_id`를 명시해 확정한다. 이 요청은 수동 검토 표시를 해제하고 매칭 fact, 문항별 `ResultFact`, canonical `Result`, legacy `ExamResult`를 한 transaction에서 동기화한다.
- 객관식 전용 시험에 OMR과 동일한 수기 결과가 먼저 저장돼 있으면 별도 재응시로 만들지 않는다. tenant·시험·submission·enrollment가 일치하는 현재 `confirmed` OMR 매칭 fact가 있고, 완료된 1차 대표 수기 attempt의 문항 집합·답안·정오·문항 점수·만점·총점이 새 OMR 계산값과 모두 정확히 같을 때만 그 attempt를 OMR submission에 연결해 재사용한다. 누락/추가 문항, 답·점수 불일치, 수기 외 source, 서술형이 포함된 시험은 이 경로를 거부하고 기존 재응시 보호를 유지한다. 수기 `ResultFact`는 보존하고 OMR 동기화 fact를 append-only로 추가한다.
- 제출함 원본 미리보기는 같은 tenant의 교직원이 `Submission` id로 요청한다. 서버가 해당 row의 AI 버킷 객체 소유권과 `tenants/{tenant_id}/` 경계를 확인한 뒤 15분짜리 GET URL만 반환하며, 목록 응답이나 클라이언트 요청에는 원본 객체 키를 노출하지 않는다. 파일 없음·다른 tenant·잘못된 키는 404, 서명 실패는 503으로 fail-closed 한다.

## 운영 UX SSOT

- 선생/원장은 **강의 > 차시 > 성적** 화면에서 OMR을 등록한다. 별도 도구 화면은 OMR 양식 생성/출력용 보조 도구이며, 차시 채점의 주 동선이 아니다.
- 성적 화면의 주 CTA는 `OMR 스캔 등록`이다. 시험이 1개면 바로 업로드 모달을 열고, 여러 개면 시험 선택만 거쳐 같은 업로드 모달로 진입한다.
- 시험 설정/제출관리 화면은 OMR 스캔을 직접 등록하지 않는다. 등록이 필요하면 성적 탭으로 이동시키고, 해당 화면은 출력/대상자/제출 확인/재채점 보조 역할만 맡는다.
- `수강생 일괄배정`은 자동 materialize 실패나 운영 보정용 보조 기능이다. 초보 사용자 기본 흐름에서는 숨기고 더보기 메뉴에 둔다.
- 업로드 화면은 "파일 선택 -> 등록 시작 -> 성적표/드로어에서 결과 확인"으로 읽혀야 한다. OMR 스캔 등록을 위해 사용자가 여러 화면을 이해해야 하는 설계를 만들지 않는다.
- 등록 전 파일 목록은 썸네일, 위·아래 순서 이동, 개별 삭제, 이미지 좌·우 90도 회전을 제공한다. 재접수 batch는 기존 ordinal을 학생 답안 순서로 보존하므로 순서 이동만 잠그고, 이미지 회전과 삭제는 명시적으로 허용한다.
- 학생 상세 드로어와 선택형 시험명은 OMR 스캔 썸네일/정렬된 미리보기와 수동 답안 보정 진입점을 제공해야 한다. 자동 인식이 틀릴 수 있음을 전제로, 보정은 OMR 검토에서 선택적으로 수행하고 직접 정오 입력 표가 자동채점 결과를 덮어쓰지 않는다.
- 자동 식별 후보와 답안이 이미 채워졌고 검토 사유가 `IDENTIFIER_*`뿐인 행은 비활성 `변경 사항 없음`으로 막지 않는다. 화면은 원본과 현재 학생을 확인하라는 설명과 **현재 학생으로 확정하고 점수 표시** 동작을 제공한다. 학생 연결이 없을 때는 검색·선택을 요구하고, 답안·정렬·문항 검토 사유가 하나라도 섞이면 학생 확인 동작으로 우회하지 않는다.
- 검토 화면의 회전은 원본 기준 미리보기다. 운영자가 `이 방향으로 다시 읽기`를 확정하면 원본 object를 복사하지 않고 같은 key를 참조하는 새 immutable Submission을 만들고, `client_request_id`로 멱등 dispatch한다. 기존 제출은 새 판독을 비교·채택하기 전까지 보존한다.
- 성적표의 `OMR_REVIEW_REQUIRED`는 최신 유효 OMR만 사용한다. `FAILED`/`SUPERSEDED` 과거 제출의 검토 플래그는 최신 정상 제출을 덮지 않는다. 커트라인 통과와 오답 보완 상태도 분리해, 통과 학생은 행 판정이 `통과`이고 시험 상세에만 `보완 필요`가 남는다.

### 직접 채점 결과 엑셀 가져오기

OMR 사용이 어려운 혼합형 시험은 같은 성적 저장 경로에 엑셀 정오표를 넣을 수 있다.
시험 생성 방식, 화면 직접 채점, Ymath `0` 표식, 원자적 확정과 오답노트
연동의 전체 계약은 [시험 생성·혼합 채점·오답노트](exam-grading.md)가
소유한다. 이 절은 OMR과 엑셀 가져오기가 만나는 경계만 설명한다.

- 진입점: **시험 → 채점·결과 → 엑셀로 채점 결과 넣기**
- 전용 양식은 시험 응시 대상 학생과 실제 `ExamQuestion.number` 열을 포함한다.
- 다른 차시에서 시험을 복사하면 문항·배점·정답키 구조도 독립 snapshot으로 함께 복사되어 같은 전용 양식을 사용할 수 있다.
- 정답 표시는 빈칸 또는 `O`, 오답 표시는 `X`다. 업로드 전에 학생 매칭·문항 수·표시값·예상 점수를 미리 검증하고, 사용자가 **결과 반영**을 눌러야 저장한다.
- 기존 채점표도 이름 또는 연락처와 숫자 문항 헤더가 있으면 읽는다. 동명이인/공용 연락처처럼 한 명으로 확정할 수 없는 행은 fail-closed하고 `수강등록ID`를 요구한다.
- 객관식/서술형 순서와 관계없이 문항 번호로 `ResultItem`을 갱신하며, 변경된 문항은 append-only `ResultFact(source=excel_import)`로 남긴다.
- 업로드 전체를 한 transaction으로 처리하므로 한 행이라도 저장 단계에서 실패하면 일부 학생만 반영하지 않는다.
- 학생 마스터와 수강 정보는 조회·매칭만 하며 수정하지 않는다. 차시 roster fallback 학생은 기존 수동 채점/OMR 정책과 동일하게 `ExamEnrollment`만 materialize한다.

## 자동채점 파이프라인

```
1. 선생님: OMR 답안지 인쇄/PDF 생성
2. 학생: 답안지에 마킹 (사인펜)
3. 선생님: 스캔 파일 업로드 (batch upload)
4. 시스템:
   a. `OMRSheetContract` 생성 → 객관식/서술형 경계와 fingerprint 확정
   b. 명시된 `manual_rotation_degrees`를 원본에 먼저 적용한 뒤 `warp.py` → A4 landscape로 보정 (자동 90/180/270도 회전 포함)
   c. `identifier.py` → 전화번호 8자리 추출 → 학생 매칭 fact 기록
   d. `engine.py` → 계약상 객관식 버블만 감지
   e. `ai_omr_result_mapper.py` → 답안 projection + 인식 fact 저장
   f. `grading_readiness.py` → 학생 매칭/답안 수/수동검토 조건 확인
   g. `ExamGradingService` → 정답 대조 → `ExamResult` 생성
5. 선생님: 결과 확인, 필요 시 수동 보정
```

AI 워커는 API 전용 DRF 패키지를 설치하지 않는다. 따라서 결과 매퍼와 콜백의
import 경로는 worker settings와 worker 전용 requirements만으로 부팅되어야 한다.
콜백 반영이 실패하면 원본 스캔과 SQS 메시지를 삭제하지 않고 재시도하며, 빌드와
`tests/test_worker_entrypoint_imports.py`가 DRF 없는 결과 매퍼 import를 검증한다.
그 사이 30분 상태 복구가 `stuck:<status>_timeout`으로 실패 처리했더라도, 같은
submission의 저장된 DONE 결과에 답안이 있고 기존 답안이 없으면 해당 실패만
`ANSWERS_READY`로 재개해 채점한다. 다른 실패 사유나 기존 답안은 자동으로
덮어쓰지 않는다. 자동 채점 동기화는 최신 `Result`/`ResultItem`뿐 아니라 문항
통계의 append-only 원본인 `ResultFact`도 같은 transaction에서 문항별로 남긴다.
따라서 점수는 보이지만 문항 분석만 비는 부분 성공 상태를 허용하지 않는다.
수동 검토가 필요하지 않은 OMR은 이 동기화 직후 legacy `ExamResult`도 `FINAL`로
확정한 뒤 진행도와 수업 분석을 갱신한다. 수동 검토 표시가 있는 OMR만 DRAFT를
유지한다.

단일정답 문항에서 워커가 강한 복수마킹을 `status=ok, marking=multi`로 보내더라도
정답과 완전히 일치하는 다중정답 키가 아니면 `ANSWER_SCORE_AMBIGUOUS`로 검토를
요구한다. 재채점은 DONE 제출을 `ANSWERS_READY`로 되돌리는 데서 끝나지 않고
`force_regrade`로 FINAL `ExamResult`, canonical `Result`/`ResultItem`, 진행도까지 실제
다시 계산한다.

## 대량 등록 작업과 진행 상태 계약

100장까지의 OMR 파일 선택은 브라우저 메모리가 아니라 서버의
`OmrUploadBatch`/`OmrUploadBatchItem`이 접수 상태의 정본이다.

1. 클라이언트는 선택한 파일에 미지원 형식이나 0바이트 파일이 하나라도 섞이면
   유효 파일만 조용히 줄이지 않고 선택 전체를 중단한다. 이 검증을 통과한 뒤에만
   시험·차시와 총 파일 수로 batch를 생성한다.
2. 서버는 `1..total_count` ordinal을 원자적으로 만들고, 클라이언트는 그 batch id와
   ordinal을 붙여 파일 전체를 한 multipart 요청으로 보낸다.
3. 각 ordinal은 별도 transaction에서 `Submission`에 연결된다. 이미 연결된 ordinal을
   다시 보내면 서버가 item row를 먼저 잠그고 파일 검증보다 앞서 `RECEIVED`를 확인해
   no-op 처리한다. 따라서 같은 ordinal의 늦은 잘못된 파일이나 동시 재전송이 기존
   Submission 연결을 실패 상태로 되돌리지 않고, 워커도 중복 dispatch하지 않는다.
   같은 tenant·시험에서 다른 batch/ordinal로 동일 바이트가 다시 오면 SHA-256과 DB
   conditional unique constraint로 경합까지 직렬화하고 `duplicate`로 완료 처리한다.
   새 Submission/R2 object/worker dispatch를 만들지 않으며 파일명이 같아도 바이트가
   다르면 정상 신규 접수다.
4. 요청 응답이 끊겨도 detail GET의 `pending_admission_ordinals`와
   `admission_failed_ordinals`로 미접수 파일만 다시 선택한다. 이미 성공한 ordinal은
   재전송하지 않는다.
5. batch 진행 상태는 기존 Submission/AI worker 상태를 집계한다. 파일 수신은
   `received`, AI 작업 중은 `processing`, 채점 완료는 `completed`, 학생 확인 필요는
   `needs_identification`, 처리 실패는 `failed`로 서로 구분한다. 업로드 성공을 AI 완료로
   표시하지 않는다.
6. initialize/upload/list/detail/retry 응답의 `items`는 총수와 같은 ordinal 원장을
   순서대로 반환한다. 각 행의 안정 식별자는 `{batch_uuid}:{ordinal}`이고 admission 상태,
   통합 표시 상태, 연결된 Submission/중복 Submission id, 식별 상태와 안전한 실패
   코드·메시지만 포함한다. Submission 생성 전에 빈 파일이 거부되어도 해당 ordinal은
   `failure_code=empty_file`인 `failed` 행으로 남아 목록·새로고침에서 사라지지 않는다.

Batch와 item에는 tenant, 생성 직원, 시험/차시/강의 id, 총수, ordinal, Submission 연결,
동일 파일 판정용 SHA-256, 안전한 실패 코드만 저장한다. 파일명, 학생 이름·전화번호, R2 raw key는 batch 모델이나
batch API 응답에 저장·노출하지 않는다. 실제 원본과 학생 매칭은 기존 tenant-scoped
Submission 계약을 그대로 사용한다.
도입 전 item은 migration에서 tenant/exam scope만 backfill하고 기존 R2 원본을 다시 읽지
않으므로 hash는 빈 값으로 보존한다. rolling 배포 중 구버전 API가 만드는 item도 새
scope/hash 열을 모르므로 null일 수 있으며 conditional constraint 대상에서 제외된다. 신규
API는 세 값을 항상 채운다. 기존 제출·점수는 바꾸지 않으며 cross-batch 동일 파일 차단은
도입 후 신규 API로 접수되는 파일부터 적용한다.

파일을 R2에 올린 뒤 Submission metadata 저장, item 연결, dispatch 중 하나라도 실패해
DB transaction이 rollback되면 서버는 그 요청에서 생성한 exact object key만 즉시 보상
삭제한다. 보상 삭제 자체가 실패하면 원래 실패를 유지한 채 구조화 로그를 남긴다. 실패
item 전환도 row lock 아래에서 현재 상태를 다시 확인하며, 이미 `RECEIVED`와 Submission
연결이 확정된 item은 지우지 않는다. Legacy multipart는 sheet/session 해석까지 성공한
뒤에만 implicit batch를 만들므로 잘못된 sheet 요청이 pending batch를 남기지 않는다.

업로드 OpenAPI는 multipart의 legacy/durable 및 단일/다중 파일 형태를 `anyOf` 대안으로
명시한다. 둘 모두 `file` 또는 `files` 중 하나가 필수이며, durable 대안은 `batch_id`와
`item_ordinals`도 필수다. 각 대안은 미선언 필드를 거부하므로 durable 식별자 중 한쪽만
섞인 payload가 legacy 요청으로 오인되어 schema 검증을 통과하지 않는다.

목록과 상세 GET은 같은 tenant의 batch 생성 직원에게만 열리고 최근 7일 작업만 목록으로
복구한다. 제출관리 화면은 Submission 목록과 별도로 이 장별 원장을 표시하므로 admission
실패처럼 Submission이 없는 행도 총수·ordinal·실패 이유와 함께 보인다. 이 GET들은
`completion_notice_claimed_at`을 포함해 어떤 값도 쓰지 않는다.
완료 알림 소유권은 별도 `claim-completion` POST가 batch row를 잠근 transaction 안에서
획득하며, terminal 이후 최초 호출만 `notify=true`, 이후 호출과 동시 탭은 `false`다.
처리 중 claim은 409로 실패한다.

재시도 POST는 요청된 ordinal만 처리한다. 원본 key가 남아 있는 실패 Submission은 기존
retry lifecycle로 다시 dispatch한다. Batch item을 ordinal 순으로 잠근 뒤 연결된 Submission을
id 순으로 함께 잠그고 최신 상태와 file key를 재확인하므로 worker callback이나 다른 admin
retry가 만든 새 상태를 stale 객체가 덮어쓰지 않는다. 잠금 뒤 전이 조건이 달라졌거나 lifecycle
전이가 거부되면 해당 ordinal은 `skipped_ordinals`로 반환하며 500이나 중복 dispatch를 만들지
않는다. 아직 파일을 받지 못했거나 admission 단계에서 실패한 ordinal은
`requires_file_ordinals`로 반환해 명시적 파일 재선택을 요구한다. 다른 tenant, 다른 생성 직원,
다른 시험 batch는 404/403으로 fail-closed 한다.

같은 시험 응시의 스캔 교체는 재시험이 아니다. 중복 후보를 채택하거나 명시적 덮어쓰기를
확정하면 대표 `ExamAttempt`의 id/index를 유지한 채 `submission_id`만 새 immutable 스캔
event로 다시 연결하고 `omr_scan_replacements`에 이전/신규 submission, actor, 시각, 사유를
남긴다. 그 뒤 이전 DONE 제출을 SUPERSEDED로 전환하고 강제 재채점한다. 실제 재시험 정책과
최대 응시 횟수는 이 좁은 교체 경로에 소비되지 않는다.

## 문항 구성

| 문항 수 | 컬럼 분할 |
|---------|----------|
| 1~20 | 1컬럼 |
| 21~40 | 2컬럼 (균등 분할) |
| 41~60 | 3컬럼 (균등 분할) |

서술형은 최대 20문항의 별도 영역이며 각 번호에 넓은 빈 작성칸을 제공한다. 시험 문항 번호는 전체 문항 기준으로 유지하지만 서술형은 워커 payload, 인식 좌표, 자동판정에 포함하지 않는다. 객관식 전용 시험의 선택적 `서술형 작성 공간`도 문항 번호·배점·채점 대상이 아니다.

## 프론트엔드 연동

### 성적 탭
- `/admin/lectures/{lectureId}/sessions/{sessionId}/scores`: OMR 스캔 등록 주 동선
- `SessionOmrUploadAction.tsx`: 시험 선택 + 스캔 업로드 모달

### 시험 탭
- `ExamPolicyPanel.tsx`: 답안 등록 후 "OMR 답안지 출력" 버튼 자동 노출
- `ExamSubmissionsPanel.tsx`: 제출 목록/파일 확인. 스캔 등록은 성적 탭으로 이동.
- `ExamBulkActionsPanel.tsx`: 재채점 실행. 스캔 등록은 성적 탭으로 이동.
- 시험별 현행 preview/PDF API를 사용해 인식 좌표가 보장된 답안지만 생성
- 미리보기와 PDF는 같은 테넌트 로고 원본을 사용한다. 업로드 로고는 미리보기의 서명 URL과 동일한 Admin 버킷 원본 키를 PDF 렌더러가 직접 읽고, 원본이 없을 때만 테넌트 정적 로고와 기본 로고 순서로 대체한다.

### 도구 탭
- `/admin/tools/omr`: OMR 생성기 (독립 도구)
- 시험명/강의명/차시명/문항수 설정 → 미리보기 → 인쇄

### 생성 요청 파라미터
```
{
  "exam_title": "시험명",
  "lecture_name": "강의명",
  "session_name": "차시명",
  "mc_count": 30,
  "essay_count": 0,
  "choice_question_numbers": [1, 3],
  "essay_question_numbers": [2],
  "n_choices": 5,
  "include_optional_essay_area": false
}
```

`include_optional_essay_area`를 생략하면 기존처럼 표시를 우선한다. 실제 서술형 문항이 있으면 이 값과 무관하게 답안 영역을 항상 표시한다.

HTML 미리보기와 PDF 다운로드는 동일한 테넌트 로고 우선순위를 사용한다. 업로드된
HTTPS 로고를 PDF 서버가 가져오지 못하면 `renderer/logos/{tenant.code}.png`로
폴백하고, 그것도 없을 때만 OMR 기본 로고를 쓴다. `godmin.png`는 프론트의
`public/tenants/godmin/logo.png`와 같은 바이트 자산으로 봉인되어 미리보기의
`min.t` 로고와 다운로드 PDF가 달라지지 않는다.

기존 공개 `/omr-sheet`·`/omr-sheet.html`은 더 이상 별도 답안지를 렌더링하지 않고 인증된 `/admin/tools/omr` 생성기로 이동한다. 정적 HTML 복제본은 최신 좌표 계약과 분리될 수 있으므로 인쇄·인식 입력으로 사용하지 않는다.

## API 엔드포인트

| Method | Path | 설명 | 상태 |
|--------|------|------|------|
| GET | `/exams/{id}/omr/defaults/` | OMR 기본값(시험명, 문항수 등) 조회 | **현행** |
| POST | `/exams/{id}/omr/preview/` | OMR 미리보기 렌더링 | **현행** |
| POST | `/exams/{id}/omr/pdf/` | OMR PDF 생성·다운로드 | **현행** |
| POST | `/exams/{id}/generate-omr/` | OMR 메타 + URL 반환 | ⚠️ **deprecated** |
| GET | `/assets/omr/objective/meta/` | 좌표 메타 조회 | 현행 |
| POST | `/submissions/exams/{id}/omr/batch/` | 스캔 파일 일괄 업로드 | 현행 |
| POST | `/submissions/exams/{id}/omr/batches/` | 총수·차시를 고정한 durable batch/ordinal 생성 | 현행 |
| GET | `/submissions/omr/batches/` | 로그인 직원의 최근 7일 batch 목록(완전 read-only) | 현행 |
| GET | `/submissions/omr/batches/{batch_id}/` | batch 접수/AI 처리 집계(완전 read-only) | 현행 |
| POST | `/submissions/omr/batches/{batch_id}/retry/` | 실패 또는 미접수 ordinal만 재시도 | 현행 |
| POST | `/submissions/omr/batches/{batch_id}/claim-completion/` | terminal 완료 알림 1회 소유권 획득 | 현행 |
| GET | `/results/admin/exams/{id}/result-import/template/` | 학생·문항이 채워진 결과 입력 엑셀 다운로드 | 현행 |
| POST | `/results/admin/exams/{id}/result-import/` | 엑셀 미리검증, `apply=true`일 때 결과 반영 | 현행 |

## 버전 이력

| 버전 | 날짜 | 변경 |
|------|------|------|
| v17.2 | 2026-08-30 | 이미 연결된 fuzzy match를 답안 변경 없이 현재 학생으로 확정하는 OMR 검토 동작을 추가. 객관식 전용 수기 결과가 OMR의 학생·문항·답안·정오·점수와 완전히 같을 때만 기존 attempt를 원자적으로 연결하며, 불일치·혼합형은 기존 재응시 보호로 fail-closed. |
| v17.1 | 2026-08-30 | OMR의 미사용 0~999 숫자 버블을 제거하고 모든 비객관식 문항을 번호가 붙은 서술형 빈 작성칸으로 통일. 종이 OMR AI 인식·자동판정은 객관식만 수행하며 기존 답안키/성적 데이터와 온라인 숫자 단답 자동채점은 유지. |
| v17 | 2026-08-27 | 1~100장 OMR 접수를 durable batch/ordinal로 먼저 만들고 기존 Submission/AI worker 상태를 집계. 모달 종료·SPA 이동·새로고침 뒤에도 접수/처리/완료/식별필요/실패를 복구하며, 성공 ordinal 중복 생성 없이 미접수·실패 ordinal만 재시도. GET은 read-only이고 별도 row-lock POST만 완료 알림을 정확히 1회 claim. batch 계약에는 파일명·학생 PII·raw key를 저장하거나 응답하지 않음. |
| v16.9 | 2026-08-22 | OMR 결과 콜백에서 HTTP 전용 DRF import를 지연해 AI 워커 전용 이미지에서도 답안 반영이 완료되도록 복구. 콜백 지연 중 상태 복구가 `stuck:*_timeout`으로 표시한 제출은 저장된 DONE 답안이 있고 기존 답안이 없을 때만 재개. 자동 채점이 문항별 `ResultFact`를 원자적으로 저장하고 수동 검토 불필요 OMR을 `FINAL`로 확정해 점수·문항 통계·수업 분석이 함께 보이도록 보장. DRF 없는 결과 매퍼 import를 테스트와 이미지 빌드에서 차단 게이트로 검증. |
| v16.8 | 2026-08-22 | OMR PDF 원격 로고 조회 실패 시 godmin의 정적 테넌트 로고로 폴백해 HTML 미리보기와 다운로드 표지를 일치시킴. |
| v16.7 | 2026-08-14 | 레거시 생성 API도 `Exam.tenant` 소유권, 명시적 0문항, 필드별 400 검증 계약을 유지하고 세션 없는 템플릿을 지원. 좌표 메타와 자산 목록의 잘못된 숫자 쿼리도 필드별 400으로 거부. |
| v16.6 | 2026-07-22 | 전체 문항 수와 문항별 객관식/단답형 유형을 저장. 임의 혼합 순서를 실제 번호로 PDF/HTML/좌표 메타/워커 payload/채점에 전달하고 기존 앞-객관식/뒤-단답형 시험은 자동 호환. |
| v16.5 | 2026-07-22 | OMR을 쓰지 않는 혼합형 시험도 기존 X 표시 채점표 또는 전용 양식으로 학생별 문항 정오를 미리검증 후 일괄 반영. 학생/문항 번호 기반 매칭, tenant/roster 경계, 원자적 저장과 변경 fact 기록 추가. |
| v16.4 | 2026-07-20 | 0~999 숫자 단답을 백·십·일 자리 버블로 출력·인식하고 정답키와 대조해 자동 채점. 일반 서술형과 표시용 단답 공간은 자동 인식에서 제외. 학생 온라인 답안도 같은 범위로 제한·정규화. |
| v16.3 | 2026-07-20 | 객관식-only의 표시용 단답형 공간을 선택 가능하게 하고 41문항 이상은 자동 숨김. 단답형-only 20문항 생성 지원. 표시 옵션은 인식/채점 계약과 분리. 좌표가 분기된 공개 정적 생성기는 현행 관리자 생성기로 수렴. |
| v16.2 | 2026-07-09 | OMR 사용자 출력물과 UI 표시명을 `단답형`으로 정리. 내부 `essay_count` 계약은 호환을 위해 유지. |
| v16.1 | 2026-06-02 | 객관식 전용 OMR에 표시용 작성 공간을 렌더링하되, 계약/워커 payload/채점은 `essay_count=0`을 유지하도록 명시. |
| v16 | 2026-06-02 | `OMRSheetContract`를 런타임 SSOT로 승격. payload, 문서 기본값, 답안 저장, recognition fact, grading readiness가 같은 객관식/단답형 계약을 공유. |
| v15.1 | 2026-05-26 | 차시 성적 화면 OMR 등록을 주 동선으로 고정. 시험 선택/업로드/보정 UX와 `SessionEnrollment` roster 기준 채점 정책을 SSOT에 명시. |
| v14 | 2026-04 | reportlab 기반 `pdf_renderer.py`로 재구현. `/omr/defaults/`, `/omr/preview/`, `/omr/pdf/` 3종 엔드포인트 추가. `generate-omr/`은 deprecated. |
| v7 | 2026-03-19 | HTML SSOT 기반 전면 재설계. 기존 v245_final.py 삭제. |
| v245_final | ~ 2026-03-18 | 구 reportlab 기반 PDF 렌더러 (삭제됨) |
