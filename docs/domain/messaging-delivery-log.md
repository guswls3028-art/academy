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
- `owner`, `admin`, `staff`, `teacher`는 현재 tenant에서 허용된 운영자다. 새
  로그는 이름과 실제 수신 전화번호를 `recipient_summary`에 기록하고, 이 역할들은
  목록에서 수신자와 정확한 공급자 메시지 ID를 확인한다. 상세에서는 이미 저장된
  비민감 본문도 확인한다. 수신자와 공급자 접수 근거를 서로 대조할 수 없는
  마스킹은 운영 권한을 막는 것으로 취급한다.
- 과거에 마스킹 요약만 저장된 로그는 현재 연락처로 추정하거나 다시 쓰지 않는다.
  새 로그부터 정확한 수신 전화가 남으며 기존 로그의 원본 부족은 그대로 표시한다.
- 목록 응답은 역할과 무관하게 본문을 포함하지 않는다. 상세 모달을 열 때만
  개별 상세 API가 호출된다.
- 등록 자격증명, 비밀번호 초기화·찾기처럼 민감한 메시지 유형은 저장 시점에
  보안 안내문으로 대체된다. 높은 권한도 원문을 복원할 수 없다.
- 공급자 실패 원문은 전화번호, 이메일, IP 등 개인정보를 포함할 수 있어
  반환하지 않는다. API는 안전한 실패 코드와 일반화된 설명만 투영한다.
- `origin_id_prefix`는 영문·숫자·콜론·밑줄·하이픈만 허용하며 현재 tenant 안에서만
  적용한다. 클리닉 운영 화면은 `clinic_participant:{id}:` 접두사로 한 참가자의
  처리 이력을 폴링한다. 응답의 `origin_id`는 전화번호가 아닌 domain 식별자다.

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
- `provider_message_reference`: 호환용 마스킹 참조
- `provider_message_id`: 현재 tenant의 허용된 모든 직원에게 제공하는 정확한 접수 증거
- `failure_code`, `failure_reason`: 개인정보가 제거된 실패 안내

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
비노출을 고정한다.
