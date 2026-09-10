# PPT 문제 생성기

## 목적과 진입점

PPT 문제 생성기는 관리자·교사가 수업용 이미지 묶음 또는 PDF 시험지를
슬라이드로 변환하는 도구다. 프런트엔드 정식 경로는
`/workspace/tools/ppt`이고, 테넌트 스태프만
`POST /api/v1/tools/ppt/generate/`를 호출할 수 있다. 테넌트가 없거나
권한이 부족하면 작업을 만들지 않는다.

이미지 모드는 사용자가 정한 순서와 슬라이드별 반전 설정을 보존해 이미지 한
장당 슬라이드 한 장을 만든다. `채움` 배치는 이미지를 슬라이드 밖으로 밀어내지
않고 PPTX의 중앙 crop 값으로 가로 또는 세로 초과분을 실제로 자른다. 따라서
생성 파일을 편집하거나 재사용해도 그림 개체 경계는 슬라이드 안에 남는다.
PDF 모드는 먼저 문항 단위 크롭을 시도하고,
텍스트 계층이 없는 스캔 PDF는 이미지 세그멘테이션을 시도한다. 두 방식으로도
문항을 찾지 못한 경우에만 페이지 단위로 변환하고 응답의 `mode=page`로
프런트엔드가 이를 알린다.

## 문항 크롭 불변 규칙

- 문항 번호, 본문, 표·그림·보기처럼 답에 필요한 시각 자료를 같은 슬라이드에
  보존한다. 좌표를 줄이는 것보다 내용 누락 방지가 우선이다.
- 2단 PDF에서 `[7~8]` 같은 공통 자료가 한쪽 열에 있고 실제 시작 문항인
  `7.`이 반대쪽 열에 있으면, 중복 번호로 어느 한쪽을 버리지 않는다. 공통 자료
  영역과 반대쪽 실제 문항 영역을 하나의 표시 크롭으로 합친다.
- 이 교차 열 결합에서도 실제 문항 본문 좌표는 `body_bbox`, 합친 수업용 표시
  좌표는 `display_bbox`·`context_bbox`, 감사 좌표는 `audit_bbox`로 구분한다.
  다음 문항의 시작점과 페이지 하단 folio를 넘지 않는다.
- 일반 2단 문항은 자기 열을 벗어나 인접 문항을 포함하지 않는다. 공통 자료나
  큰 시각 자료라는 명시적 신호가 있을 때만 필요한 범위를 확장한다.
- 자동 분할이 불확실하거나 유효 슬라이드가 0장이면 빈 PPTX를 반환하지 않고
  안전한 페이지 단위 변환으로 되돌린다.
- 이미지 모드의 `맞춤`은 비율을 유지하며 전부 보이고, `채움`은 비율을 유지한
  중앙 crop, `늘림`은 슬라이드 크기에 맞춘 변형이다. `채움`의 picture 좌표는
  슬라이드 경계와 같고 초과분은 OOXML crop 속성으로만 제거한다.

## 비동기 작업과 저장 경계

API는 업로드를 `tenants/<tenant_id>/tools/ppt/tmp/` 아래에 두고
`ppt_generation` 작업을 Tools queue로 보낸다. Tools worker가 원본을 임시
디렉터리에 내려받아 분할·렌더·PPTX 조립을 수행한 뒤 로컬 임시 디렉터리를
정리한다. 결과는 `tenants/<tenant_id>/tools/ppt/`에 저장하고 1시간 유효한
다운로드 URL, 파일명, 슬라이드 수, 바이트 수를 작업 결과로 반환한다.

진행 상태는 파일 다운로드, 문항 분리/PPT 생성, 파일 저장, 완료 순서로 기록한다.
작업 실패 시 내부 경로나 저장 키를 사용자 화면에 노출하지 않고 일반화된 오류로
종료한다. 다른 테넌트의 업로드나 결과를 찾는 기본값·fallback은 없다.

## 구현과 검증

주요 소유 코드는 다음과 같다.

- 업로드·권한·작업 발행: `apps/domains/tools/ppt/views.py`
- 작업 실행·R2 결과: `academy/application/use_cases/ai/pipelines/ppt_handler.py`
- PDF 계획·렌더 크롭: `academy/application/use_cases/tools/generate_ppt.py`
- 문항·공통 자료 좌표: `academy/domain/tools/question_splitter.py`
- PPTX 배치: `academy/adapters/tools/pptx_writer.py`

집중 회귀는 다음 명령으로 확인한다.

```powershell
python -m pytest tests/test_ppt_pdf_question_plan.py tests/test_pptx_writer_crop.py tests/test_question_splitter_t2_fixes.py -q
```

실사용 검증은 2단 PDF에서 공통 자료와 반대쪽 시작 문항이 함께 보이는지,
후속 문항 순서와 총 슬라이드 수가 유지되는지, 생성 PPTX를 다시 열 수 있는지
확인한다. 배포는 Tools worker를 포함한 정식 backend release를 사용하고 개발
런타임·격리 preproduction의 Excel/PPT/R2 smoke를 모두 통과해야 한다.
