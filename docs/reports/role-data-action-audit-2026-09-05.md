# 역할별 데이터·행위 감사 — 2026-09-05

## 범위와 판정 방법

학생, 보호자, 강사, 조교·staff, 대표·admin의 결과·연락처·출결·클리닉·과제,
메시지 전달 증거, 직원·급여 경계를 backend permission/queryset/serializer와
frontend route/표시 계약에서 교차 대조했다. 실행 코드와 실패 우선 회귀를 기준으로
판정했고 실제 알림톡 발송·재시도는 수행하지 않았다.

## 확인된 불일치

| ID | 심각도 | 실행 영향 | 원인 | 교정·회귀 |
|---|---|---|---|---|
| RP-01 | P1 privacy | `is_manager=true`인 강사·조교가 동료의 시급·근무금액·환급·월 정산을 조회하고 Excel/PDF를 내보낼 수 있었음 | `can_manage_staff_payroll`과 serializer가 `teacher`, `staff`를 민감 권한으로 승격했고 frontend도 서버 boolean만 신뢰하며 권한 토글을 제공 | 권한·`can_manage_staff`를 현재 tenant의 `owner`, `admin`으로 제한. 새 `is_manager` 쓰기와 무효 토글/내보내기 열을 제거하고 오래된 true flag도 역할 guard에서 거부. backend 역할·serializer 회귀와 390px Playwright로 고정 |
| RP-02 | P1 operations | 허용된 강사·조교가 알림톡 수신자를 중복 이름 사이에서 식별하거나 공급사 접수 ID와 대조하기 어려웠음 | worker가 전화번호를 앞 4자리+마스킹으로 저장하고 log projection/mobile UI가 staff·teacher의 이름·본문·provider ID를 다시 제한 | 새 로그에 실제 수신 전화 저장, tenant staff에게 정확한 recipient/provider evidence와 비민감 상세 본문 제공. 계정 비밀·공급자 오류 원문은 계속 제거. backend 역할 회귀, worker 회귀, teacher/staff 390px Playwright로 고정 |
| RP-03 | P1 operations | 같은 학생에 대해 학생·보호자를 함께 선택해도 최종 확인창에 마스킹 전화가 하나만 보여 두 실제 목적지를 확인하기 어려웠음 | preflight의 학생·보호자 행을 `student_id` 하나로 합쳐 표시했음. 실제 발송은 `send_to`와 `student_id`를 함께 사용해 두 대상으로 올바르게 분리됨 | 별도 frontend PR #424가 `send_to`별 마스킹 전화 표시를 보존. raw phone과 라우팅은 바꾸지 않고 두 목적지 표시, 390px overflow, 이후 학생 단독 payload 회귀로 고정 |

## 확인된 정상 경계

| 경계 | 실행 근거 | 결과 |
|---|---|---|
| 학생 본인 성적 | `results/student_exam_result_view.py`, `student_app/results_summary.py` | 본인·현재 tenant·활성 수강·공개 결과만 조회하며 본인 등수와 익명 집단 크기/평균만 반환 |
| 보호자 자녀 선택 | `student_app/permissions.py`, `test_parent_exam_child_selection.py` | 연결된 활성 자녀만 허용. 잘못된 명시 자녀는 fallback 없이 `403` |
| 교직원 일반 운영 | `TenantResolvedAndStaff`와 results/attendance/clinic/lecture/student queryset | 현재 tenant의 강사·조교 모두 학생 운영 경로 사용 가능 |
| 클리닉 정책 설정 | `clinic/capabilities.py`, `clinic/views/settings_views.py` | 학생 운영은 전 직원, 예약 정책 변경은 owner/admin으로 분리 |
| 메시지 비밀 | `messaging/security.py`, notification log 회귀 | 계정·인증 본문은 저장 전에 대체되고 provider 실패 원문·전화/IP는 응답하지 않음 |
| cross-tenant | 각 tenant-scoped selector/queryset 및 기존 격리 회귀 | 모든 역할에서 fail closed |

## 겹침·보류

- 학생 결과 알림톡 수신 정책과 RP-03 표시 수정은 별도 변경의 정확한 소유 범위이므로
  이 감사에서 해당 send/preflight/modal 파일을 수정하지 않았다. RP-03은 frontend
  PR #424의 exact head `b0b88ede9`에서 회귀를 포함해 교정됐다.
- safe-method 전체 저장소 계약의 별도 변경도 소유권을 넘겨받지 않았다. 이번에
  수정한 로그 조회와 급여 권한 판정에는 GET 데이터 쓰기를 추가하지 않았다.
- 정식 배포는 기존 zlib 이미지 보안 gate와 release serialization을 통과해야 한다.
  이 감사는 해당 gate를 우회하지 않는다.

## 실패 우선·회귀 증거

- 실패 우선: 강사·조교의 `is_manager=true`가 payroll permission을 통과하는 두 역할
  사례, staff 로그의 `body_visibility=restricted`, worker의 수신 전화 마스킹, 390px
  급여 메뉴·로그 표시가 각각 기존 동작에서 실패했다.
- backend: 직원 sync/운영, 로그 redaction, 보호자 자녀 선택, 학생 성적 요약을 합친
  Django 121건과 worker pytest 19건이 통과했다. Ruff, Django check,
  `makemigrations --check --dry-run`, safe-method static/unit/runtime 계약도 통과했다.
- frontend: 직원 운영·teacher/staff 모바일 로그·admin 로그 30건이 Chromium
  `--retries=0`에서 통과했고 strict browser defect는 0건이었다. typecheck, 전체 lint,
  build/bundle budget, OpenAPI type, legacy/deployment/runtime-recovery guard가 통과했다.

## 개발 런타임 QA 경계

이 branch의 backend 후보는 이미지 보안 gate와 release 직렬화보다 앞서 개발
런타임에 게시하지 않는다. 직전 공식 backend release run의 candidate scan 실패로
persistent development job 자체가 skipped였고, frontend same-artifact real-use는
main push에서만 실행된다. 따라서 이번 branch에서 합성 tenant Setup이나 메시지
발송·재시도는 시작하지 않았으며 남은 QA tenant도 만들지 않았다. 두 PR이 직렬화된
release queue에서 해당 gate를 통과한 뒤에만 owner/admin/teacher/staff 화면과 정확한
Cleanup 0을 공식 same-artifact 절차로 봉인한다.
