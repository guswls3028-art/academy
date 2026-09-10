# 역할별 데이터·행위 권한

이 문서는 Academy의 역할별 최소 데이터·행위 경계를 한곳에서 찾기 위한 정본이다.
세부 계산·상태 전이는 각 도메인 문서가 소유한다. 모든 허용은 요청에서 확정된
활성 `TenantMembership`과 단일 `request.tenant` 안에서만 성립하며, 다른 tenant나
다른 학생으로 대체하지 않는다.

## 역할 × 데이터 × 행위

| 데이터·행위 | 학생 | 보호자 | 강사 | 조교·staff | 대표·admin |
|---|---|---|---|---|---|
| 시험 결과·회차 점수 | 공개된 본인 결과 조회 | 연결된 선택 자녀의 공개 결과 조회 | 현재 tenant 운영 조회·입력 | 현재 tenant 운영 조회·입력 | 현재 tenant 전체 운영 |
| 등수·집단 통계 | 제공되는 경우 본인 등수와 익명 집단 규모·평균 | 선택 자녀의 같은 정보 | 학생 식별 결과와 운영 통계 | 학생 식별 결과와 운영 통계 | 학생 식별 결과와 운영 통계 |
| 다른 학생 식별 점수 | 불가 | 불가 | 현재 tenant 업무 범위에서 가능 | 현재 tenant 업무 범위에서 가능 | 현재 tenant에서 가능 |
| 학생 연락처·출결·클리닉·과제 | 본인용 화면만 | 연결 자녀용 화면만 | 현재 tenant 운영 가능 | 현재 tenant 운영 가능 | 현재 tenant 운영·정책 관리 |
| 알림톡 처리 로그 | 불가 | 불가 | 실제 수신 전화·비민감 본문·정확한 공급사 접수 ID 조회 | 강사와 동일 | 강사와 동일 |
| 본인 근태·근무액 | 해당 없음 | 해당 없음 | 연결 Staff 본인만 | 연결 Staff 본인만 | 본인 및 tenant 전체 관리 |
| 동료 시급·비용·정산·급여 내보내기 | 불가 | 불가 | 불가 | 불가 | 가능 |
| 직원 계정·직위·급여 설정 | 불가 | 불가 | 불가 | 불가 | 가능 |
| tenant 결제·소유자 설정·비밀·공급자 자격증명 | 불가 | 불가 | 불가 | 불가 | 필요한 관리 API만 가능. 비밀 원문 응답은 불가 |
| 다른 tenant 데이터 | 불가 | 불가 | 불가 | 불가 | 불가 |

## 식별 정보와 비밀의 구분

- 현재 tenant에서 학생 운영을 수행하는 강사·조교에게 학생 이름과 연락처는 업무
  식별 정보다. 이를 일률적으로 마스킹해 실제 대상이나 전달 결과를 확인하지 못하게
  하면 권한 오류로 취급한다.
- 다른 학생·다른 tenant의 식별 정보, 동료 직원의 급여·보상, 계정 비밀,
  access/refresh token, 공급자 자격증명과 공급자 실패 원문은 운영 편의를 이유로
  넓히지 않는다.
- 계정·인증 알림 본문은 저장 단계에서 보안 안내문으로 대체한다. 역할을 높여도
  원문을 복원하지 않는다.

## 실행 소유 경계

- 학생·보호자 선택: `apps/domains/student_app/permissions.py`의
  `get_request_student`; 명시한 `X-Student-Id`가 미소유·비정상이면 기본 자녀로
  대체하지 않고 `403`으로 닫는다.
- 학생 성적: `apps/support/student_app/results_summary.py`와
  `apps/domains/results/views/student_exam_result_view.py`; 공개 여부와 활성 수강,
  현재 tenant, 본인 학생 ID를 함께 제한한다.
- 교직원 운영: `TenantResolvedAndStaff`와 각 도메인의 tenant-scoped queryset.
- 메시지 로그: `apps/domains/messaging/views/log_views.py`,
  `apps/worker/messaging_worker/sqs_main.py`.
- 직원·급여: `apps/core/permissions.py`의 `can_manage_staff_payroll`과
  `apps/domains/staffs/views/`.

## 회귀 검증

```powershell
$env:DJANGO_SETTINGS_MODULE='apps.api.config.settings.test'
C:\academy\backend\.venv\Scripts\python.exe manage.py test apps.domains.staffs.tests.test_staff_teacher_sync apps.domains.staffs.tests.test_staff_operations_contract.StaffOperationsContractTests apps.domains.messaging.tests.test_notification_log_redaction.NotificationLogRedactionTests apps.domains.student_app.tests.test_parent_exam_child_selection.ParentExamChildSelectionTests apps.domains.student_app.tests.test_grades_summary_homework.MyGradesSummaryHomeworkTests -v 1
C:\academy\backend\.venv\Scripts\python.exe -m pytest tests/test_messaging_worker_failures.py -q
```

프런트 라우트·표시 계약은 `frontend/docs/ROLE-DATA-ACTION-POLICY.md`가 소유한다.
