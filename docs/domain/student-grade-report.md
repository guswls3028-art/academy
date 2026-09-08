# 학생 성적표 표시와 구성

학생·학부모가 교사의 오답 확인 상태를 성적 보드에서 직접 확인하고, 학원 관리자는
성장 그래프에 필요한 정보와 순서를 학원 단위로 정하는 현재 계약이다. 프론트엔드
동선과 반응형 계약은 `frontend/docs/STUDENT-GRADE-REPORT.md`를 함께 따른다.

## 사용자와 진입점

- 학생·학부모: `GET /api/v1/student/grades/`를 통해 공개된 시험 카드의 오답 확인
  상태와 성장 그래프 구성을 읽고, `GET /api/v1/student/results/me/exams/{exam_id}/`의
  공개된 시험 상세에서도 같은 상태를 확인한다. 응답의 `lecture_options`는 선택한 학생의 활성
  비시스템 수강 강좌만 포함하며, 아직 공개 점수가 없는 강좌도 학생이 강좌별 빈 상태를
  확인할 수 있도록 유지한다. 프론트엔드는 한 강좌면 현재 강좌 티켓을, 둘 이상이면 선택
  상자를 표시한다. 시험별 `student_results_published=false` 결과는
  목록·추이·분석에서 제외한다.
- 시험 등수와 상위 비율은 교직원 결과 화면과 같은 1차 점수 표준 공동 순위를 쓴다.
  동점 다음 등수는 동점 인원만큼 건너뛰며(`1, 2, 2, 4`), 미응시자는 응시 인원과
  순위 계산에서 제외한다.
- 시험 카드·회차별 추이·요약·학생 상세의 기본 점수와 만점도 같은 1차 점수를 쓴다.
  현재 대표 `Result.attempt_id`가 1차 attempt이면, 자동 채점 직후의
  `initial_snapshot`보다 같은 시도에 확정된 canonical `Result.total_score`와
  `max_score`를 우선한다. 재시험이 대표가 된 뒤에만 보존된 1차 snapshot을 사용한다.
  2차 이상 재시험 점수와 교사 PASS는 원점수를 바꾸지 않는 보충 이력/최종 보완 상태다.
  재시험이 최신 문항 스냅샷을 덮어 1차 문항별 결과를 안전하게 복원할 수 없으면,
  학생 상세는 재시험 문항을 1차 점수와 섞지 않고 문항 분석을 비운다.
  별도 시험으로 이미 등록된 과거 재시험 데이터는 원시험 연결 근거가 없으므로 자동
  병합하지 않고 기존 독립 시험 카드로 보존한다.
- 학원 관리자: 관리자 또는 교사 모바일 앱의 **설정 → 학원 정보 → 학생 성적표**에서
  `GET/PATCH /api/v1/core/student-grade-report-layout/`을 사용한다.
- 구성 수정 권한은 같은 테넌트의 `owner`와 `admin`이다. 일반 교사·직원,
  학생·학부모는 실패 폐쇄한다.

## 오답 확인과 교사 최종 처리 상태

학생 시험 카드의 `correction_status`는 차시 성적표에서 교사가 저장한
`AssessmentCorrection`을 읽은 값이다. 프론트엔드는 점수나 오답 개수로 상태를
재구성하지 않는다.

- 유효한 비만점 시험에 현재 완료 기록이 없으면 `PENDING`이다.
- 교사가 완료했고 대표 결과·대표 시도·점수·문항별 답안/정오/배점의 내용 지문이
  같으면 `COMPLETED`다.
- 완료 뒤 점수나 답안 내용이 바뀌면 `PENDING`으로 읽는다. `updated_at`만 바뀐
  재저장은 완료를 유지한다.
- 만점 시험은 `NOT_REQUIRED`, 미응시·무효 점수는 `null`이다.
- 시험과 수강 강의의 차시를 하나로 확정할 수 없으면 다른 차시의 교사 확인 기록을
  추정하지 않고 `null`을 반환한다.
- 학생 응답에는 상태만 포함한다. 교사 메모와 완료 시각은 노출하지 않는다.
- 성적 보드는 `전체·확인 필요·처리됨`으로 시험을 필터링한다. 시험 상세는 같은 상태를
  설명 문장과 함께 표시하되 학생·학부모에게 수정 권한을 주지 않는다.

Ymath는 `Program.feature_flags.assessment_status_display=wrong_completion`을
테넌트 표시 정본으로 사용한다. 시험 카드·상세·홈·클리닉 상태 카드에서는
`PENDING`을 **오답 미완료**, `COMPLETED`와 `NOT_REQUIRED`를 **오답 완료**로
보이고 PASS·합격·보강 합격 뱃지를 함께 노출하지 않는다. `null`은 완료로
간주하지 않는다. 이 정책은 API의 내부 `achievement`, 원점수, 재시험과
`ClinicLink` 이력을 보존한 채 표시만 단순화하며, 다른 테넌트의 성취 표현을
바꾸지 않는다.

구현은 `apps/domains/results/services/assessment_correction_status.py`의 내용 지문과
상태 판정을 교사용 차시 성적표와 학생 성적 요약이 함께 사용한다.

시험은 원점수의 `is_pass=false`를 보존해도 현재 교사 완료가 유효하면 최종 성취를
`REMEDIATED`로 읽어 재시험 필요 카드와 실패 통계에서 제외한다. 실제 점수·답안이
바뀌면 지문 검증이 이 최종 처리를 무효화해 다시 확인 대상으로 돌린다.

과제는 `teacher_resolved=true`를 별도로 반환한다. 점수 없는 미제출 과제를 교사가
현장에서 확인해 완료한 경우에도 `score=null`과 원래 `passed`/제출 사실은 바꾸지
않고 `achievement=REMEDIATED`로 표시한다. 학생·학부모 성적 카드에는 **교사 확인
완료**로 보이며, 대시보드의 미통과·제출 화면의 재제출 대상·과제 통과 통계에서는
완료로 센다. 교사 메모는 학생/학부모 응답에 포함하지 않는다. 교사가 완료를 해제하면
`teacher_resolved=false`와 현재 원자료의 `FAIL`/`NOT_SUBMITTED` 상태가 다시 표시된다.
과제의 1차 성적·성취 이력과 파일 변경 가능성은 서로 다른 계약이다. 응답의
`submission_media_locked=true`는 최신 재시도 통과 또는 점수 행 없는 교사 완료로
과제 파일 POST/DELETE가 잠겼음을 뜻한다. 제출 화면은 이 값을 사용해 대상을 제외하며,
`passed`는 계속 1차 성적을 나타낸다.

## 성장 그래프 구성

표시 순서와 노출 여부는 `Program.ui_config.student_grade_report_layout`에 저장한다.
런타임에서 YMath 또는 다른 테넌트 코드를 분기하지 않는다.

| 섹션 ID | 의미 |
|---|---|
| `score_trend` | 회차별 성적 추이 |
| `score_comparison` | 평균·통과율·전체 평균 비교 |
| `lecture_average` | 강좌별 평균 득점률 |
| `improvement_priority` | 반복 오답·약점 시험·자동 해석 문구 |
| `exam_summary` | 시험 평균·합격률·시험 수·평균 등수 |
| `rank_position` | 상·중·하위권 위치 분석 |
| `weakest_lecture` | 약점 강좌 안내 |
| `homework_summary` | 과제 채점·평균·합격률 |

`score_comparison_metrics`는 성적 비교 섹션 안의 `average_score`, `pass_rate`,
`status` 표시 여부를 각각 소유한다. 섹션을 유지하면서 필요 없는 요약 항목만 숨길 수
있고, 전체 평균 비교 그래프는 유지된다. 과거 저장값이나 구버전 PATCH에 이 키가 없으면
기존 저장값을 보존하며, 최초 기본값은 세 항목 모두 표시다.

PATCH는 모든 알려진 섹션을 중복 없이 포함하고 `visible`을 boolean으로 보내야 하며,
하나 이상을 표시해야 한다. 읽을 때 알 수 없는 과거 섹션은 버리고 새 섹션은 기본 표시
상태로 보완한다. 저장은 다른 `ui_config` 키를 보존하고
`student_grade_report.layout.update` 감사 로그를 남긴다.

기본값은 기존 화면과 같은 전체 표시다. 데이터 마이그레이션은 YMath의 최초 구성만
`score_trend`, `score_comparison`, `lecture_average` 표시로 저장하고, 성적 비교에서는
평균 득점률만 표시하며 통과율과 상태는 숨긴다. 이는 편집 가능한 초깃값이므로 관리자는
같은 API에서 나머지 섹션과 세부 항목을 다시 켜거나 순서를 변경할 수 있다.

## 실패와 호환

- `Program`이 비정상적으로 없으면 학생 조회는 전체 표시 기본값을 사용하고, 설정 저장은
  `400`으로 중단한다. 조회 중 새 `Program`을 만들지 않는다.
- 과거 또는 수동 수정으로 `Program.ui_config`가 JSON 객체가 아니면 조회는 전체 표시
  기본값을 사용한다. 관리자가 저장하거나 YMath 초기화 명령을 실행하면 비정상 값을
  빈 객체로 복구한 뒤 현재 성적표 구성만 저장한다.
- 수강이 없는 학생도 `report_layout`은 받아 학원 구성이 안정적으로 유지된다.
- 구버전 응답처럼 `report_layout`이 없으면 프론트엔드는 전체 표시 기본값을 사용한다.
- 비공개 시험은 교직원 화면과 원본 결과를 보존하지만 학생·학부모 응답과 그 응답을
  사용하는 알림·대시보드에서는 점수나 존재를 노출하지 않는다.
- 분석 API 실패는 오답 상태, 회차별 성적, 다른 독립 섹션을 지우지 않는다.
- 구버전 API처럼 `lecture_options`가 없으면 프론트엔드는 공개된 `exam_trend`의 강좌
  메타데이터로만 선택 목록을 복구한다. 이 호환 경로는 다른 학생이나 비활성 강좌를
  추정하지 않는다.

## 검증

- `apps/core/tests/test_student_grade_report_layout.py`: owner/admin 권한, 일반 교사 차단,
  정규화, 검증, 다른 `ui_config` 보존
- `apps/domains/student_app/tests/test_grades_summary_homework.py`: 학생·학부모 응답의 현재
  시험 완료 상태, 점수 변경 뒤 미완료 전이, 점수 없는 교사 완료 과제와 메모 비노출,
  테넌트 구성 projection
- `apps/domains/results/tests/test_session_scores_roster_scope.py`: 교사용 저장·재조회와 같은
  지문 판정 유지
