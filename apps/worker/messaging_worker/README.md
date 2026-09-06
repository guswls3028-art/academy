# Messaging Worker (SQS + Solapi)

SQS `academy-messaging-jobs` 수신 → 검증된 Solapi 알림톡만 발송. 예약 취소 시 발송 직전 Double Check로 스킵.

---

## 1. 인프라 (비용 절감)

- **인스턴스**: **t3.nano**(또는 가장 싼 스펙). CPU보다 네트워크 I/O만 쓰므로 고사양 불필요.
- **반드시 스팟 인스턴스(Spot Instance)** 로 구성 → 일반 온디맨드 대비 **70% 이상 절감**.

---

## 2. 환경변수

| 변수 | 필수 | 설명 |
|------|------|------|
| `SOLAPI_API_KEY` | ✅ | Solapi API 키 |
| `SOLAPI_API_SECRET` | ✅ | Solapi API 시크릿 |
| `SOLAPI_SENDER` | ✅ | 발신 번호 (예: 01012345678) |
| `SOLAPI_KAKAO_PF_ID` | - | 카카오 비즈니스 채널 ID (알림톡 사용 시) |
| `SOLAPI_KAKAO_TEMPLATE_ID` | - | 카카오 검수 완료 템플릿 ID. **ENV로 관리해 코드 수정 없이 교체** |
| `MESSAGING_SQS_QUEUE_NAME` | - | 기본값 `academy-messaging-jobs` |
| `AWS_REGION` | - | 기본값 `ap-northeast-2` |
| `MESSAGING_SQS_WAIT_SECONDS` | - | Long Polling 대기(기본 20) |
| `DJANGO_SETTINGS_MODULE` | - | 예약 취소 Double Check 시 설정 |
| `MESSAGING_DISABLED_TENANT_IDS` | - | 긴급 사고 전용 콤마 구분 업무 tenant ID. 고객 선호는 제품의 전체 사용 토글로 제어하고, 이 값은 API·워커 양쪽의 incident hold에만 사용 |
| `MESSAGING_RECIPIENT_DENYLIST` | - | 콤마 구분 수신번호. 큐 소비와 공급사 호출 직전에 재확인하여 영구 차단 |

---

## 3. SQS 운용 전략

- **Visibility Timeout**: 솔라피 타임아웃(약 5~10초)보다 넉넉히 **30~60초** (기본 60). 짧으면 중복 발송 대참사.
- **Dead Letter Queue (DLQ)**: **3번 재시도** 후 실패 메시지는 DLQ로 격리 → 미수신 원인 확인용 로그.
- **Long Polling**: `WaitTimeSeconds` **20초** → SQS 빈 쿼리 비용 최소화.

---

## 4. message_mode: 검증 채널 알림톡

| message_mode | 동작 |
|--------------|------|
| `alimtalk` | 공용 owner 채널 또는 활성 tenant binding으로 알림톡 발송. pf_id·template_id 필수. |
| 그 외 명시 값 | `sms_disabled` 또는 `unsupported_message_mode`로 실패 폐쇄. |

- **템플릿 관리**: 카카오 검수 끝난 **템플릿 ID**를 ENV(`SOLAPI_KAKAO_TEMPLATE_ID`) 또는 payload `template_id`로 전달.

---

## 5. 예약 취소 대응 (Revoke + Double Check)

- 사용자가 예약을 취소하면, 해당 예약의 발송은 **SQS에 이미 들어간 뒤**일 수 있음.
- **발송 로직 입구에서 Double Check 필수**: `reservation_id`가 있으면 `is_reservation_cancelled(reservation_id)` 호출. **취소 상태면 발송 스킵**하고 메시지 삭제.
- 워커 실행 시 `DJANGO_SETTINGS_MODULE` 설정 시 `django.setup()` 호출 → `apps.domains.messaging.services.is_reservation_cancelled()` 에서 `Reservation` 모델의 `status == 'CANCELLED'` 여부 조회.

---

## 6. 실행

```bash
pip install -r requirements/worker-messaging.txt
python scripts/create_sqs_resources.py ap-northeast-2

# 예약 취소 체크 필요 시
export DJANGO_SETTINGS_MODULE=apps.api.config.settings.prod
python -m apps.worker.messaging_worker.sqs_main
```

---

## 7. API/서비스에서 발송 요청

- **비동기 (권장)**
  `enqueue_alimtalk(..., message_mode="alimtalk", template_id="...", alimtalk_replacements=[...])` → 공용 알림톡만
  → SQS 적재, 워커가 owner tenant 잔액 검증·차감 후 공용 채널로 발송.
- **Raw/legacy SQS payload**
  worker 입구에서 `tenant_id`를 owner tenant로 강제 정규화한다. 원 업무 테넌트는
  `source_tenant_id`로만 보존하며, 예약 취소 같은 업무 조회에만 사용한다.
- **SMS/LMS**
  실발송 예외와 호환 callable이 없다. legacy SMS/LMS payload는 provider를 호출하지
  않고 `sms_disabled` 실패로 닫으며, 그 밖의 비지원 값도 알림톡으로 보정하지 않는다.
- **운영 수신자 차단**
  `MESSAGING_RECIPIENT_DENYLIST`는 하이픈을 제거해 비교한다. 이미 SQS에 들어간
  메시지도 워커 입구에서 삭제하고, Solapi 호출 직전 같은 정책을 다시 확인한다.
- **공급자 접수 전 거절**
  `QuotaExceeded`와 `NotEnoughBalance`는 provider ID·차감이 없는 확정 거절로
  terminal failed 처리한다. 중복 방지를 위해 worker가 자동 재발송하지 않는다.

API 키/시크릿은 **환경변수**로만 설정하고 코드에 노출하지 마세요.

---

## 8. 발신번호와 채널

Worker 발신번호는 **SOLAPI_SENDER** 환경변수만 사용합니다. provider/API 키도 공용
Solapi 설정으로 고정합니다. 기본 PFID는 공용 owner 채널이며, 새
`AlimtalkChannelBinding`이 active이고 공용 template ID와 매핑된 tenant template이
APPROVED·동일 본문 지문일 때만 해당 tenant PFID/template ID로 치환합니다. 과거
tenant 발신번호/PFID/provider/자체 키는 실발송에 사용하지 않습니다.

---

## 9. 발송 ID 저장 (민원 대응)

Solapi 응답의 **group_id**(및 messageId)를 발송 로그/예약 테이블에 저장해 두면, "문자 안 왔어요" 민원 시 Solapi 콘솔에서 해당 ID로 조회해 원인 파악이 가능합니다.

---

## 10. Fake Solapi (DEBUG / 테스트)

- **DEBUG=True** 또는 **SOLAPI_MOCK=true** 이면 실제 API를 호출하지 않고, 발송될 JSON만 콘솔에 예쁘게 로그합니다.
- 잔액 차감·템플릿 미승인 에러를 피하려면 개발/스트레스 테스트 시 이 모드로 실행하세요.
- `apps.domains.messaging.solapi_mock.MockSolapiMessageService` 사용.

## 11. 스트레스 테스트

```bash
# 100건 enqueue (워커는 별도 터미널에서 DEBUG=True 로 실행)
python scripts/stress_test_messaging_worker.py

# 100건 enqueue 후 워커 자동 실행, 큐가 비워질 때까지 대기
python scripts/stress_test_messaging_worker.py --run-worker
```

## 12. 알림톡 템플릿 변수 (미리 확정)

- `apps.domains.messaging.alimtalk_templates` 에 템플릿 변수명 상수 및 치환 헬퍼 정의.
- **변수명**: `name`, `date`, `time`, `clinic_name`, `place`, `link`, `title` (DB 필드와 매칭).
- 솔라피/카카오 콘솔에 등록할 템플릿 문구 예: `#{name}님, #{date} #{time} #{clinic_name} 예약이 완료되었습니다.`
- 치환 데이터 생성: `build_replacements(context)` / `template_context_from_reservation(reservation)` 사용.
