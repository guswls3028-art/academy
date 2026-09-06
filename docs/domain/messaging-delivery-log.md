# 알림톡 발송 기록 조회

## 목적과 범위

`GET /api/v1/messaging/log/`와 `GET /api/v1/messaging/log/{id}/`는 현재
테넌트의 직원이 알림톡 처리 이력을 확인하는 읽기 전용 경로다. 실제 발송,
일반 재시도, 재큐잉, 예약 취소, 발송자나 공급자 선택은 이 API의 책임이 아니다.
제품 메시지는 알림톡 단일 채널이며 SMS/LMS 대체 발송은 제공하지 않는다.
조회 결과는 `message_mode=alimtalk`과 채널 필드가 없던 과거 호환 기록만
포함한다. 명시적인 비알림톡 기록을 현재 알림톡 처리 이력으로 오인해 표시하지
않는다.

소유 구현은 `apps/domains/messaging/views/log_views.py`와
`apps/domains/messaging/models.py`에 있고, 프런트 화면 계약은
`frontend/docs/MESSAGING-OPERATIONS.md`에 있다.

## 테넌트와 역할 경계

- 인증된 현재 직원과 `request.tenant`가 모두 있어야 하며, 결과는 항상 현재
  테넌트로 필터링한다. 테넌트를 추정하거나 다른 테넌트로 대체하지 않는다.
- `owner`, `admin`만 상세 조회에서 이미 저장된 본문 투영과 정확한 공급자
  메시지 ID를 받을 수 있다.
- `staff`, `teacher`는 본문을 받지 않는다. 공급자 증거는 존재 여부와 뒤쪽
  일부만 남긴 마스킹 참조로 제한한다.
- 목록 응답은 역할과 무관하게 본문을 포함하지 않는다. 상세 모달을 열 때만
  개별 상세 API가 호출된다.
- 등록 자격증명, 비밀번호 초기화·찾기처럼 민감한 메시지 유형은 저장 시점에
  보안 안내문으로 대체된다. 높은 권한도 원문을 복원할 수 없다.
- 공급자 실패 원문은 전화번호, 이메일, IP 등 개인정보를 포함할 수 있어
  반환하지 않는다. API는 안전한 실패 코드와 일반화된 설명만 투영한다.
- `origin_id_prefix`는 영문·숫자·콜론·밑줄·하이픈만 허용하며 현재 tenant 안에서만
  적용한다. 클리닉 운영 화면은 `clinic_participant:{id}:` 접두사로 한 참가자의
  처리 이력을 폴링한다. 응답의 `origin_id`는 전화번호가 아닌 domain 식별자다.

## 직접 발송 요청의 정확한 추적

수동 발송 UI는 사용자가 한 번 **발송하기**를 누를 때 UUID `client_request_id`를
하나 만든다. 학생과 보호자를 함께 선택해 두 API 요청이 생겨도 같은 request ID를
사용하고, 서버는 각 API 접수마다 별도 UUID `batch_id`를 발급한다. outbox, SQS,
worker log는 `request_id`, `batch_id`, `origin_type=manual_send`,
`origin_id=request_id`를 그대로 보존한다. 신규 trace payload는 worker가 UUID,
origin과 `occurrence_key=request:{request_id}:batch:{batch_id}`의 exact 일치를 다시
검증하며 drift가 있으면 공급자를 호출하지 않는다.

목록 API는 다음 exact filter와 같은 이름의 projection을 제공한다.

- `request_id=<UUID>`: `origin_type=manual_send`와 exact `origin_id`가 함께 맞는 로그만
  반환한다. 참관 사본 등 다른 provenance는 같은 correlation 값이 있어도 섞지 않는다.
- `batch_id=<UUID>`: 한 번의 학생 또는 보호자 API 접수에서 생성된 로그만 반환한다.
- `origin_type`, `origin_id`: 허용 문자 검증 뒤 현재 tenant에서 exact 일치시킨다.

형식이 잘못된 exact filter는 빈 목록으로 숨기지 않고 `400`을 반환한다. 직접 발송
응답의 `accepted_count=0`은 성공 응답이 아니며, 유효 번호가 하나도 없으면
`422 no_valid_phone`, 큐·예약 접수가 전부 실패하면 `503 message_not_accepted`다. 화면은
즉시 발송 접수 뒤 `request_id` 필터가 붙은 발송 내역으로 이동해 같은 요청만 다시
불러온다.

## 상태와 시각 의미

`NotificationLog.status`의 의미를 보존한다.

| 상태 | 운영 의미 |
|---|---|
| `processing` | 로그가 생성되어 발송 준비 중 |
| `sending` | 작업자가 처리 중이며 공급자 접수 결과 확인 중 |
| `sent` | 공급자가 알림톡 요청을 접수함. 카카오톡 열람 완료를 뜻하지 않음 |
| `retryable_failed` | 자동 재시도 가능한 임시 실패 |
| `failed` | 종료된 실패 |
| `ambiguous` | 공급자 결과를 확정할 수 없어 수동 확인 필요. 중복 방지를 위해 자동 재발송하지 않음 |

목록의 `status` 필터는 정확한 상태 외에 `active`(`processing`, `sending`,
`retryable_failed`)와 `attention`(`ambiguous`) 그룹을 지원한다. 기존
`success`와 `failure` 필터의 호환 의미는 유지한다.

`sent_at`은 현재 모델 계약상 **발송 완료 시각이 아니라 로그 행 생성 시각**이다.
`claimed_at`은 작업자가 행을 claim한 시각이다. 현재 모델에는 공급자 처리 완료
시각이 없으므로 화면이나 API에서 임의로 합성하지 않는다.

## 본문과 공급자 증거 투영

응답은 다음 필드로 표시 가능 범위를 명시한다.

- `message_body_included`: 이 응답에 본문이 실제 포함됐는지 여부
- `body_visibility`: `available`, `sensitive_redacted`, `restricted`,
  `not_recorded` 중 하나
- `provider_evidence`: 공급자 식별자가 기록됐는지를 나타내는 boolean
- `provider_message_reference`: 모든 허용 역할에 제공 가능한 마스킹 참조
- `provider_message_id`: `owner`, `admin`에게만 제공하는 정확한 증거
- `failure_code`, `failure_reason`: 개인정보가 제거된 실패 안내

`success=true`와 `status=sent`의 목록 projection은 `provider_delivery_status=
provider_accepted`다. provider ID의 노출 여부나 boolean만으로 `delivered`를 만들지
않는다. `delivered`와 최종 `failed`는 저장된 provider ID로 상세의 read-only 최종
상태 확인을 수행한 응답에서만 나타난다.

`message_body`가 비어 있으면 원문을 추정하거나 템플릿에서 재구성하지 않는다.

## 클리닉 실패 재시도

`POST /api/v1/clinic/participants/{id}/retry-notification/`은 현재 tenant의 직원이
해당 참가자 접두사에 속한 `failed` 또는 `retryable_failed` 알림톡만 다시 요청하는
좁은 경로다. `sent`, `processing`, `sending`, `ambiguous`는 중복 위험 때문에
거부한다. 저장된 원본 outbox의 승인 template, 수신 역할, 치환값과 대상 학생 ID가
현재 참가자와 정확히 맞아야 하며 원본이 없으면 추정하지 않는다. 새 원천 ID는
`clinic_participant:{participant_id}:retry:{log_id}`로 고정되어 같은 실패 로그를
여러 번 눌러도 outbox는 하나만 생성된다. SMS/LMS 전환은 없다.

## 검증

```powershell
$env:DJANGO_SETTINGS_MODULE='apps.api.config.settings.test'
C:\academy\backend\.venv\Scripts\python.exe manage.py test apps.domains.messaging.tests.test_notification_log_redaction.NotificationLogRedactionTests -v 1
C:\academy\backend\.venv\Scripts\python.exe manage.py test tests.test_clinic_time_range_policy_api -v 1
```

테스트는 테넌트 격리, 역할별 본문·공급자 증거, 민감 본문 비복원, 실패 원문
비노출과 request/batch/origin exact 필터·projection을 고정한다.
