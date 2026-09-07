# 클리닉 예약 정책과 다중 시간대 계약

## 목적과 사용자 흐름

클리닉 예약은 기존 한 시간 단위의 `fixed_slot`과 하나의 긴 운영 세션에서 실제
체류 시간을 고르는 `time_range`를 함께 지원한다. 학생이나 교직원이 같은 날짜의
고정 세션 여러 개를 한 번에 선택하면, 각 시간대마다 독립적인
`SessionParticipant`를 만든다. 예를 들어 17:00–18:00과 18:00–19:00을 함께
선택하면 화면에는 17:00–19:00 이용으로 요약하지만 데이터와 정원은 두 세션에서
각각 관리한다.

- 학생은 자신의 계정에 연결된 학생 한 명과 같은 날짜의 시간대 여러 개를
  선택해 신청한다.
- 교직원은 활성 학생 여러 명과 같은 날짜의 시간대 여러 개를 선택해 일괄
  추가한다.
- 기존 단일 생성 `POST /clinic/participants/`와 일정 변경 API는 호환을 위해
  유지한다. 일정 변경은 계속 하나의 새 세션만 받는다.
- 시간대가 하나일 때만 `preferred_start_time`과 `preferred_end_time`을 받을 수
  있다. 여러 세션의 부분 구간을 하나의 희망 시간으로 추정하지 않는다.
- 세션의 `allow_multi_slot_booking`이 `false`이면 학생 한 명은 해당 날짜에
  `pending` 또는 `booked` 예약을 하나만 가질 수 있다. `true`인 세션끼리만
  같은 날짜의 여러 시간대를 예약할 수 있다.
- 여러 시간대를 한 요청으로 선택할 때는 시작 시각 순으로 앞 세션의 종료 시각과
  다음 세션의 시작 시각이 정확히 이어져야 한다. 중간 공백이나 겹침이 있으면
  참가자를 하나도 만들지 않고 `400`으로 실패한다.

프론트엔드 상호작용 계약은 학생 앱
`src/app_student/domains/clinic/README.md`와 선생님 앱
`src/app_teacher/domains/clinic/README.md`가 소유한다.

## API 계약

`POST /api/v1/clinic/participants/bulk-create/`

```json
{
  "session_ids": [701, 702],
  "student_ids": [801, 802],
  "student_request_memo": "두 시간 연속 참여"
}
```

성공하면 `201`과 생성된 참가자 목록을 돌려준다.

```json
{
  "count": 4,
  "participants": []
}
```

- `session_ids`는 중복 없는 1–20개이며 모두 현재 테넌트의 같은 날짜여야 한다.
- 학생 요청은 `student_ids`를 받지 않고 인증된 학생만 사용한다.
- 교직원 요청은 중복 없는 활성 학생 1–100명을 명시한다.
- 한 요청의 학생 × 시간대 조합은 최대 500개다.
- 학생 신청은 세션 대상 강의·학년·학교·정원 규칙을 기존 단일 예약과 동일하게
  적용한다. 교직원 추가도 기존 수동 추가 권한과 상태 규칙을 유지한다.
- 세션 또는 학생이 없거나 다른 테넌트에 속하면 존재를 추정하지 않고 실패한다.
- 여러 시간대를 선택했는데 하나라도 `allow_multi_slot_booking=false`이면 아무
  참가자도 만들지 않고 `409`로 실패한다.
- 같은 날짜의 허용 세션들이어도 서로 연속하지 않으면 아무 참가자도 만들지 않고
  `400`으로 실패한다.

## 세션 정책과 초기값

새 세션은 두 예약 방식 중 하나를 생성 시점 snapshot으로 고정한다.

- `fixed_slot`(기본값)은 기존처럼 세션 전체를 한 자리로 예약한다. 기존 tenant,
  session, participant는 migration에서 모두 이 값이므로 기존 동작과 데이터가
  바뀌지 않는다.
- `time_range`는 한 개의 긴 운영 세션 안에서 학생이 실제
  `booking_start_time`/`booking_end_time`을 선택한다. 시작·종료는 둘 다 있어야
  하고 세션 시작 기준 30분 또는 60분 간격, 세션 운영 범위, 최대 체류 시간을
  모두 만족해야 한다. `preferred_*` 희망 시간과는 별도 사실이다.
- tenant 기본값 `clinic_booking_mode`, `clinic_booking_interval_minutes`,
  `clinic_booking_max_stay_minutes`는 owner/admin만 바꾼다. 모든 직원 역할은 값을
  읽을 수 있고, session의 snapshot은 이후 기본값 변경에 따라 바뀌지 않는다.
- 활성 예약이 있는 session의 예약 방식·간격·최대 체류는 바꿀 수 없다.
  `time_range`는 다중 session 선택과 섞지 않으며 반복 생성도 한 날짜씩 한다.

`GET /api/v1/clinic/sessions/{id}/availability/`는 요청 tenant와 세션 대상 자격을
통과한 사용자에게 운영 범위, 간격, 최대 체류, 각 구간의 남은 정원만 반환한다.
참가자 신원은 반환하지 않는다. 시간 범위 정원은 session row lock 아래
`pending`/`booked`/`attended`의 반열린 구간(`[start, end)`) 겹침을 각 간격마다
검사한다. 따라서 10:00–11:00과 11:00–12:00은 겹치지 않으며 동시 요청도 같은
구간 정원을 초과할 수 없다. 고정 시간대 정원 계산은 기존 pending/booked 계약을
그대로 유지한다.

목록·상세·운영 tree의 `is_full`도 같은 구간별 계산을 사용한다. `time_range`는
모든 구간이 마감일 때만 전체 마감이고, `available_slots`는 구간 중 가장 큰 잔여
정원이다. 누적 예약 인원이 동시 정원을 넘더라도 열린 구간을 숨기지 않는다.
운영 tree는 예약 방식·간격·최대 체류 snapshot을 함께 내려주고, 화면은 시간 범위
수업의 누적 예약 인원과 동시 정원을 구분한다. 목록 조회는 tenant-scoped 활성
예약을 한 번에 prefetch하므로 세션 수나 시간 구간 수만큼 추가 쿼리가 발생하지 않는다.

`Tenant.clinic_allow_multi_slot_booking_default`는 새 세션의 기본값이고,
`Session.allow_multi_slot_booking`은 생성 시점에 그 값을 복사한 snapshot이다.
세션 생성 요청이 값을 명시하면 명시값이 우선하며, 이후 테넌트 기본값을 바꿔도
이미 생성된 세션은 바뀌지 않는다.

- 기본값은 `false`다.
- 초기 운영값은 `tchul=false`, `godmin=false`, `limglish=true`다.
- 기존 `limglish` 세션은 migration에서 `true`, 나머지 기존 세션은 `false`로
  설정한다.
- 세션을 `true`에서 `false`로 바꿔도 기존 참가자 행은 보존한다. 이후 같은 날짜의
  충돌하는 새 예약만 막는다.
- `cancelled`, `rejected`, `no_show`는 활성 충돌로 보지 않는다.

날짜 비교의 기준은 tenant로 격리된 `Session.date`다. 이 값은 클리닉 운영의
현지 날짜를 직접 저장하는 `DateField`이므로 서버 UTC 시각에서 날짜를 다시
추정하지 않는다. tenant나 session을 현재 요청 범위에서 확인할 수 없으면 다른
tenant를 추정하지 않고 실패 폐쇄한다.

### 일정 tree 날짜 범위 조회

교직원 전용 `GET /api/v1/clinic/sessions/tree/`는 기존 `year`+`month` 조회와
`date_from`+`date_to` inclusive 조회 중 정확히 하나만 받는다. 날짜 범위는 최대
31일이며 일부 누락, 역순, 잘못된 날짜, 월 조회와의 혼합을 `400`으로 거부한다.
범위는 tenant-scoped `Session.date`에 직접 적용하므로 8월 31일~9월 6일처럼
월말·월초를 건너도 양 끝 날짜를 모두 포함하고 서버 시각대 변환을 하지 않는다.

이 조회는 존재하는 세션 행을 반환한다. 참가자의 `cancelled` 여부는 세션 자체를
숨기지 않으며 예약·출결 수치는 상태별 집계로만 구분한다. 삭제된 세션 행과 다른
tenant의 세션은 반환하지 않는다. 이전 주 복사에 필요한 제목·날짜·시간·장소·정원,
대상 학년/학교유형/강의/반, 희망시간 접수, 다중 시간대, 예약 방식·간격·최대 체류
snapshot을 함께 반환하지만 참가자 명단은 반환하거나 복사하지 않는다.

## 원자성·동시성·알림

서비스는 학생을 ID 순으로 먼저 잠그고 세션을 날짜·시작 시각·ID 순으로 잠근다.
단일 생성, 학생 bulk, 교직원 bulk, 일정 변경이 모두 같은 학생 row lock을
사용하므로 서로 다른 세션을 향한 동시 요청도 같은 날짜 정책을 우회하지 못한다.
이후 기존 단일 참가자 생성 규칙을 학생 × 세션 조합마다 적용한다.
정원 마감, 비연속 시간대, 잘못된 대상, 권한 오류가 하나라도 발생하면 요청 전체를 롤백한다.
따라서 2명 × 2시간대 요청이 일부만 저장되는 상태는 없다.

이미 같은 학생·세션의 활성 예약이 있거나 같은 날짜 정책이 충돌하면 단일 생성과
bulk 모두 `409`로 거부하고 요청 전체를 롤백한다. 일정 변경은 기존 예약을 충돌
검사에서 제외한 뒤 새 세션을 확보하고 기존 행을 취소하므로 OFF 세션 간의 정상적인
1:1 변경은 허용한다. 알림은 트랜잭션 커밋 뒤 각 참가자별
`clinic_reservation_created` 이벤트로 요청하며, 승인된 알림톡 템플릿이 없으면
기존 메시징 정책대로 실패 폐쇄한다.

다중 예약은 기존 `SessionParticipant` 행들의 집합이므로 조회·출석·취소·패스카드
규칙을 그대로 따른다. 학생 직접 예약 생성·일정 변경·취소 알림은 기존 계약대로
학생과 학부모 모두에게 보내며, 교직원 수신자 선택 규칙은 변경하지 않는다.

### 자동 시작 리마인더

`send_clinic_reminders`는 기존 tenant의 `clinic_reminder` enabled 및
`minutes_before` 설정을 그대로 사용한다. `fixed_slot`은 기존 세션 시작시각과
세션+학생 중복방지 키를 유지한다. `time_range`만 실제 예약의
`booking_start_time`에서 설정된 분 수를 뺀 시점에 학생에게 보낸다. 긴 세션의
개방시각이나 희망시각으로 대체하지 않으며, 자동 수신자를 부모나 교사로 바꾸지 않는다.

- 기본 5분 지연 보정 창 안에 도달한 `booked` 예약만 평가한다. 예약 시작 이후나
  보정 창이 지난 과거 알림을 소급 발송하지 않는다.
- 범위 시작·종료가 없거나 세션 운영 범위를 벗어나거나, 학생·세션·예약 tenant가
  다르거나 학생이 삭제됐거나 하원한 경우에는 발송하지 않는다.
- 실제 예약별 `clinic_booking:<participant>:<session>:<date>:<start>` origin을
  durable outbox에 저장한다. 반복 tick은 같은 occurrence를 새로 만들지 않고,
  기존 세션 기준 알림 이력도 보존하여 시간 보정이 과거 발송의 재생이 되지 않게 한다.
- dispatcher와 worker의 공급자 호출 직전에 현재 예약·날짜·시작시각·tenant를
  다시 확인한다. 취소/이동/시간변경/삭제/시작시간 경과로 닫힌 occurrence는
  외부 발송하지 않는다. 공급자 전 차단은 확정 실패로 처리하고 중복 재시도하지 않는다.
  기존에 명시 설정된 참관 사본에도 같은 예약 생명주기를 적용한다.
- `--dry-run`은 동일한 due/dedup 건수를 계산하지만 outbox나 외부 전송을 만들지 않는다.
  수신 실패 및 SQS 재시도 정책은 [메시징 정책](../ssot/messaging-policy.md)을 따른다.

검증은 `apps/support/clinic/tests/test_clinic_reminder_service.py`,
`apps/domains/messaging/tests/test_scheduled_notifications.py`,
`tests/test_messaging_worker_failures.py`의 실제 시간범위/고정시간 호환,
재실행·취소·시간변경·tenant 격리·공급자 호출 0 회귀를 사용한다.

참가자와 보충 대상의 상태 변경은 일반 detail `PATCH/PUT/DELETE`로 허용하지 않는다.
예약 생성·일정 변경·상태 변경·완료·완료 취소·하원·오늘 계획 action만 각 service의
잠금과 감사 규칙을 통과한다. 일정 변경은 이전 참가자의 반복 알림을 취소하고, 이전
오늘 계획 행을 `booking_changed`로 닫은 뒤 새 세션의 대상 강의에도 유효한 항목만 새
참가자에게 원자적으로 이어 준다. 취소·거절은 오늘 계획을 각각
`booking_cancelled`/`booking_rejected`로 닫는다. 세션 삭제도 cascade 전에 해당
참가자의 미래 반복 알림을 취소·redact한다. 예약 알림 dispatcher는 lifecycle 정리가
누락된 과거 행도 현재 tenant의 `booked` 참가자인지 다시 확인하고 아니면 발송하지 않는다.

자율학습 완료와 완료 취소는 `completion_history`에 actor와 시각을 append-only로
남긴다. 완료 취소는 현재 `completed_at/by`만 비우며 기존 완료 감사와 이미 생성된
알림 이력을 삭제하지 않는다. 별도 승인된 정정 템플릿이 없으므로 다른 trigger를
대용하지 않는다.

## 등원 기록 없는 하원 감사 계약

`POST /api/v1/clinic/participants/{id}/checkout/`은 정상 등원 후 하원과, 현장에서
등원 처리를 놓친 예약 학생의 하원을 모두 기록한다. 두 경로 모두 기존
`checked_out_at`, `checked_out_by`를 사용하며 자율학습 `completed_at`은 건드리지
않는다.

- 정상 등원(`status=attended`, `checked_in_at` 존재)은 기존 빈 payload도 허용하고
  `checkout_mode=arrival_recorded`를 기록한다.
- 등원 기록이 없는 예약 확정 학생은 `confirm_without_arrival=true`와 현재
  `expected_session_id`, `expected_student_id`를 모두 보내야 한다. 이때
  `checkout_mode=arrival_not_recorded`를 기록하며 `status`나 `checked_in_at`을
  생성·추정하지 않는다.
- 예상 session/student가 바뀌면 `409`, 다른 tenant 참가자는 `404`, pending·취소·
  거절·결석·세션 미연결 행은 실패 폐쇄한다.
- 같은 참가자의 반복 하원 요청은 `200`으로 기존 결과를 돌려주고 하원 시각·처리자를
  다시 쓰지 않는다.
- 최초 하원은 `clinic_check_out` 이벤트를 만들고 직원이 선택한 학생/학부모/둘 다에게
  승인된 공용 `clinic_info` 봉투로 하원 전용 본문과 실제 시각을 요청한다. 같은 참가자의
  반복 하원은 기존 기록만 반환하며 알림을 다시 요청하지 않는다.

## 구현과 검증

- 입력·응답 직렬화: `apps/domains/clinic/serializers.py`
- 트랜잭션·잠금: `apps/domains/clinic/services/lifecycle.py`
- API 액션·커밋 후 알림: `apps/domains/clinic/views/participant_views.py`
- tenant/session 정책: `apps/core/models/tenant.py`, `apps/domains/clinic/models.py`
- 집중 API 회귀: `tests/test_clinic_multi_slot_booking_api.py`
- 월 경계·tenant·복사 설정 범위 회귀:
  `tests/test_clinic_session_tree_date_range_api.py`
- 시간 범위·권한·연락처·알림 이력 회귀: `tests/test_clinic_time_range_policy_api.py`
- 하원·등원 독립 회귀: `tests/test_clinic_operations_workflow_api.py`
- 상태 소유권·오늘 계획·패스카드·완료 감사 회귀:
  `apps/domains/progress/tests/test_generic_write_boundaries.py`,
  `tests/test_clinic_participant_plan_api.py`, `apps/domains/clinic/tests.py`

```powershell
$env:DJANGO_SETTINGS_MODULE='apps.api.config.settings.test'
python -m pytest tests/test_clinic_multi_slot_booking_api.py -q
python manage.py test tests.test_clinic_time_range_policy_api --noinput
python manage.py makemigrations --check --dry-run
python manage.py check --settings apps.api.config.settings.test
```

집중 회귀는 ON/OFF 단일·bulk 경로, 혼합 정책 원자성, 비활성 상태, ON→OFF,
일정 변경, tenant 격리, 초기 tenant/session 값과 PostgreSQL 동시 쓰기를 검증한다.
