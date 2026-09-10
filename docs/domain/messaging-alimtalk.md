# 메시징 도메인 SSOT (공용 카카오 알림톡 발송 시스템)

> 최종 갱신: 2026-08-29 (일반 강의 출결을 수동 preview→confirm 전용으로 복구)
> 근거: 코드 직접 확인. 추측 없음.

---

## 🚨 §0. 학원장 mental model (절대 원칙, 모든 알림톡 작업의 base)

이 박스를 **반드시 먼저 읽고** 알림톡/메시징 작업을 진행한다. 상세 발송 정책은 `backend/docs/ssot/messaging-policy.md`가 정본이다.

### 비유: 봉투와 편지

- **카카오 검수 통과 ITEM_LIST 4종 양식 = 장식이 다른 4가지 봉투** (header/highlight/item.list/prefix는 카카오에 박혀있어 변경 불가 = 봉투의 장식)
- **`#{선생님메모}` 한 자리 = 봉투 안에 들어가는 자유 편지** (학원장 자유 편집, 무제한 길이)

선생님이 양식 UI에서 하는 모든 행위 — 변수블록 추가/제거, 문구 변경, 새 카테고리 안내, 양식 디자인 — 은 **전부 `#{선생님메모}` 한 자리 안에서 일어남**. 봉투 자체는 장식, 변경 불가.

### 봉투 의미 매칭 (학원장이 카테고리 선택 = 봉투 선택)

| 봉투 | prefix (장식 고정) | 자동 슬롯 (장식) | 학원장 자유 편지 |
|---|---|---|---|
| **score** | `[성적표 안내]` | 학원이름/학생이름/강의명/차시명 | `#{선생님메모}` 안에 시험1~4/총점/숙제완성도 등 성적 상세를 치환 |
| **attendance** | `[출석 안내]` | 학원이름/학생이름/강의명/차시명/날짜/시간 | `#{선생님메모}` |
| **clinic_info** | `[클리닉 안내]` | 학원이름/학생이름/장소/날짜/시간 | `#{선생님메모}` |
| **clinic_change** | `[일정 변경 안내]` | 학원이름/학생이름/기존일정/변동사항/수정자 | `#{선생님메모}` |
| NONE notice_withdrawal | `[HakwonPlus] 퇴원 처리 완료` | 학원명/학생이름2 | **없음** (시스템 안내) |
| NONE notice_payment | `[HakwonPlus] 결제 완료 안내` | 학원명/학생이름2/사이트링크 | **없음** (시스템 안내) |

성적 관련 안내 → score 봉투. 클리닉 관련 → clinic_info 봉투. 출석·시험 일정·과제 안내 → attendance 봉투. 클리닉 일정 변경/취소 → clinic_change 봉투. **ITEM_LIST 봉투 안 편지(`#{선생님메모}`)는 무제한 자유.** NONE 봉투는 카카오 승인 본문 고정이라 편지 영역이 없다.

2026-07-08 Solapi 실등록 감사 기준 `notice_payment` 기존 SID는 provider 목록에 없어 현재 fail-closed다. 논리 매핑은 유지하지만 실제 승인 SID가 복구되기 전까지 결제 알림톡은 발송하지 않는다.

### 영구 금지 패턴 (응답/제안/백로그에 떠오르면 즉시 멈춤)

- ❌ "이 트리거는 양식이 없다 → 카카오 검수 신청하자"
- ❌ "notice/community/staff 카테고리에 매핑이 없으니 추가하자"
- ❌ "새로운 양식이 필요하니 Solapi에 등록하자 / 봉투 새로 만들자"
- ❌ "신규 알림톡 템플릿 만들자"
- ❌ "fallback 양식이 필요할 수도 있다" 추론
- ❌ NONE 양식 본문 미반영을 결함으로 분류 / 매핑 제거 시도
- ❌ 양식 추가 / 신규 검수를 backlog · P1 · P2 · 개선안으로 적기

### 학원장 톡 원문 (한 달 반 격분 끝에 박힌 mental model)

- "우리가 만드는 양식 수정은 사실 `#{선생님메모}` 안에만 들어가는거임 ㅇㅋ?"
- "선생님이 변수블록 추가 제거 문구변경등을 하는건 모두 `#{선생님메모}` 여기 안에서 일어나는거라고 이걸 이해시키는데에 1달 반이 걸렸다"
- "계속 양식이없다고 탬플릿 추가해야한다고 하는데 `#{선생님메모}` 여기에 박으면된다고 우리가 프로그램에서 선생님 통해 제작한 풀 템플릿을 그럼 무제한으로 쓸 수 있잖아"
- "`#{선생님메모}` 여기를 제외한건 그냥 일종의 껍데기 UI라고 생각해도됨. 그냥 장식마냥"
- "근데 이제 장식이 용도에 맞게 배치되어야하니까. 성적 관련된건 성적쪽 `#{선생님메모}` // 클리닉 관련된건 클리닉쪽 `#{선생님메모}`"
- "이 내용 전달이 너무 어려웠음 아무리 말해도 계속 탬플릿 추가하려고 하니까 미쳐버리는거임"

### How to apply (모든 알림톡 작업 진행 전 자기 검토)

1. 내가 양식 추가 / 카카오 검수 / 새 템플릿 / 새 봉투 권유하려 하나? → **멈춤**
2. 모든 새 안내 문구 / 카테고리 / 트리거 → 4종 봉투 중 의미 가까운 것 선택 + `#{선생님메모}` 본문 자유 작성으로 cover
3. 학원장이 명시적으로 "새 카카오 양식 검수 받자" 한 경우만 신규 검수 진행
4. NONE 자동발송 매핑(withdrawal_complete/payment_complete/payment_due_days_before) 제거 금지 — 시스템 안내 자동발송이 정상
5. UI에서 `#{선생님메모}` 본문 편집을 잠그지 않는다.

관련: `backend/docs/ssot/messaging-policy.md`, `backend/docs/domain/messaging.md`

---

## 1. 아키텍처 개요

### 발송 파이프라인

```
도메인 이벤트 (출결/클리닉/시험 등)
  -> send_event_notification()          [services.py:267]
    -> AutoSendConfig 조회 (enabled 확인)
    -> 통합 템플릿 매핑 (alimtalk_content_builders.py)
    -> enqueue_alimtalk()                    [services.py:111]
      -> MessagingSQSQueue.enqueue()    [sqs_queue.py:62]
        -> SQS (academy-v1-messaging-queue)
          -> 메시징 워커
            -> legacy SMS payload 차단
            -> 공용 Solapi provider dispatch (활성 verified tenant 채널은 마지막 단계에서 exact ID 치환)
              -> 공용 Solapi SDK
                -> 카카오 알림톡
```

### 수동 발송 파이프라인

```
관리자 UI (SendMessageModal)
  -> SendMessageView.post()             [views/send_views.py]
    -> 통합 승인 봉투 매핑 (CATEGORY_TO_TEMPLATE_TYPE)
    -> build_manual_replacements()       [alimtalk_content_builders.py]
    -> enqueue_alimtalk()                     [services.py:111]
      -> (이하 동일, legacy tenant PFID/provider fallback 없음)
```

### 시스템 필수 알림톡 파이프라인 (가입/비번)

```
가입 승인 / 계정 복구
  -> send_alimtalk_via_owner()          [apps/domains/messaging/policy.py]
    -> 오너 테넌트의 승인 템플릿 조회
    -> enqueue_alimtalk() (오너 tenant_id로)
      -> (이하 동일)
```

공개 로그인 화면의 아이디 찾기/비밀번호 찾기 정본은 `backend/docs/domain/account-recovery.md`다.

- 현재 공개 endpoint: `/api/v1/auth/account-recovery/dispatch/`
- 아이디 찾기: `registration_approved_student` / `registration_approved_parent` 템플릿 재사용, 비밀번호는 `변경되지 않음`
- 비밀번호 찾기: `password_reset_student` / `password_reset_parent`, 6자리 숫자 임시 비밀번호
- `password_find_otp`는 legacy OTP 경로 호환용이다.

### 각 단계의 역할

| 단계 | 파일 | 역할 |
|------|------|------|
| `send_event_notification` | services.py:267 | AutoSendConfig 조회, enabled/dry-run 확인, 공용 owner 템플릿 또는 unified 템플릿 resolve, 수신자 전화번호 추출 |
| `enqueue_alimtalk` | `services/queue_service.py` | 정책 검증(disabled/restricted/whitelist/알림톡 전용), owner tenant_id 정규화, SQS enqueue |
| `MessagingSQSQueue.enqueue` | sqs_queue.py:62 | SQS 메시지 구성, business_idempotency_key 생성, 큐 전송 |
| 메시징 워커 `main` | sqs_main.py:314 | SQS Long Polling, Redis 멱등 잠금, 예약 취소 확인, 잔액 검증/차감, 공급자별 발송, 로그 기록 |
| 알림톡 provider dispatch | `sqs_main.py` | 공용 시스템 PFID + 공용 provider로 알림톡 발송. Solapi fallback은 `disable_sms=True` |
| 비알림톡 boundary | `queue_service.py`, `sqs_queue.py`, `scheduled.py`, `sqs_main.py` | SMS/LMS는 `sms_disabled`, 그 밖의 명시 채널은 `unsupported_message_mode`로 닫고 provider를 호출하지 않음 |

운영 실사용 검증은 `scripts/v1/run-messaging-verify-send.ps1` → `messaging_verify_common_alimtalk`로만 수행한다. 이 경로는 통제번호 `01031217466`으로 `password_reset_student` owner exact approved template을 발송하고, 워커가 `NotificationLog.provider_message_id`를 남긴 뒤 성공으로 판정한다.

---

## 2. Solapi 통합 템플릿 4종

### 템플릿 목록

| 상수 | Solapi ID | 타입 | 등록 변수 |
|------|-----------|------|-----------|
| `SOLAPI_CLINIC_INFO` | `KA01TP2604061058318608Hy40ZnTFZT` | ITEM_LIST (clinic_info) | 학원이름, 학생이름, 클리닉장소, 클리닉날짜, 클리닉시간, 선생님메모, 사이트링크 |
| `SOLAPI_CLINIC_CHANGE` | `KA01TP260406110706969XS06XRZveEk` | ITEM_LIST (clinic_change) | 학원이름, 학생이름, 클리닉기존일정, 클리닉변동사항, 클리닉수정자, 선생님메모, 사이트링크 |
| `SOLAPI_SCORE` | `KA01TP260406105458211774JKJ3OU55` | ITEM_LIST (score) | 학원이름, 학생이름, 강의명, 차시명, 선생님메모, 사이트링크 |
| `SOLAPI_ATTENDANCE` | `KA01TP260406121126868FGddLmrDFUC` | ITEM_LIST (attendance) | 학원이름, 학생이름, 강의명, 차시명, 강의날짜, 강의시간, 선생님메모, 사이트링크 |

출처: `alimtalk_content_builders.py`, `TEMPLATE_TYPE_VARIABLES`, Solapi provider 실등록 감사(2026-07-08 21:56 KST)

### 활성화 플래그

`UNIFIED_TEMPLATES_ENABLED = True` (line 21). 카카오 검수 승인 완료 후 True로 설정됨. 미승인 상태에서 True로 두면 Solapi 발송 거부.

### Solapi 템플릿 본문 구조

```
#{선생님메모}
#{사이트링크}
```

백엔드에서 트리거별 메시지를 조립해 `#{선생님메모}` 값으로 전송. `#{사이트링크}`는 테넌트 URL.

### TEMPLATE_TYPE_VARIABLES 매핑

`alimtalk_content_builders.py:275-288`에 정의. 각 템플릿 타입에 Solapi로 전달해야 하는 **전체** 변수 목록. 누락하면 카카오 에러 3063.

### ITEM_LIST 변수 23자 제한

- `ITEM_LIST_VAR_MAX_LEN = 23` (line 291)
- 선생님메모, 사이트링크를 **제외**한 변수 값이 23자 초과 시 22자 + "..." 로 truncate (`alimtalk_content_builders.py:258-259`, `437-438`)

### 선생님메모 자동 보강 로직

`build_unified_replacements` (line 382-424), `build_manual_replacements` (line 227-246):

- template.body(선생님 편집 가능)에 핵심 변수(`#{학생이름}`, `#{클리닉장소}` 등)가 **하나도** 없으면
- 핵심 정보(학생이름 + 라벨:값 쌍)를 **자동으로** 선생님메모 앞에 추가
- 변수가 하나라도 있으면 선생님이 커스텀한 것으로 판단하여 자동 보강 안 함

---

## 3. 트리거 -> 템플릿 매핑 (TRIGGER_TO_TEMPLATE_TYPE)

출처: `alimtalk_content_builders.py` (2026-09-03 기준)

| 트리거 | 템플릿 타입 | 자동 발화 | 비고 |
|--------|------------|---|------|
| `clinic_reservation_created` | clinic_info | ✅ | |
| `clinic_reminder` | clinic_info | ✅ | EventBridge `academy-v1-send-clinic-reminders` → `send_clinic_reminders` |
| `clinic_check_in` | clinic_info | ✅ | |
| `clinic_check_out` | clinic_info | ✅ | 하원 전용 본문과 실제 하원 시각을 승인된 공용 클리닉 안내 봉투로 발송 |
| `clinic_absent` | clinic_info | ✅ | ⚠️ 결석 통보를 "[클리닉 안내]" prefix — 의미 검토 |
| `clinic_self_study_completed` | clinic_info | ✅ | 자율학습 완료 전용. 하원과 별도 이벤트 |
| `clinic_result_notification` | clinic_info | ✅ | ⚠️ "결과"를 "안내" prefix — 의미 검토 |
| `counseling_reservation_created` | clinic_info | ✅ | ⚠️ "상담"이 "[클리닉 안내]" prefix — 학부모 혼동 가능 |
| `clinic_reservation_changed` | clinic_change | ✅ | `clinic.services.lifecycle`가 기존일정/변동사항/수정자 변수 생성 |
| `clinic_cancelled` | clinic_change | ✅ | `clinic.services.lifecycle`가 기존일정/변동사항/수정자 변수 생성 |
| `check_in_complete` | attendance | manual | 일반 강의 출결 상태 저장과 분리된 출결 알림 preview→confirm만 허용 |
| `absent_occurred` | attendance | manual | 일반 강의 출결 상태 저장과 분리된 출결 알림 preview→confirm만 허용 |
| `lecture_session_reminder` | attendance | manual | minutes_before 스케줄러 미구현 |
| `exam_scheduled_days_before` | attendance | manual | 시험 예정 안내를 강의/차시 컨텍스트 봉투에 담음 |
| `exam_start_minutes_before` | attendance | manual | 시험 시작 안내를 강의/차시 컨텍스트 봉투에 담음 |
| `exam_not_taken` | attendance | manual | 미응시 안내를 강의/차시 컨텍스트 봉투에 담음 |
| `assignment_registered` | attendance | manual | 과제 등록 안내를 강의/차시 컨텍스트 봉투에 담음 |
| `assignment_due_hours_before` | attendance | manual | 과제 마감 안내를 강의/차시 컨텍스트 봉투에 담음 |
| `assignment_not_submitted` | attendance | manual | 과제 미제출 안내를 강의/차시 컨텍스트 봉투에 담음 |
| `retake_assigned` | clinic_info | manual | 재시험 안내를 보강/클리닉 컨텍스트 봉투에 담음 |
| `exam_score_published` | score | manual (정책: 저장≠발송) | "[성적표 안내]" prefix 의미 일치 |
| `monthly_report_generated` | score | manual | ⚠️ 월간 집계를 강의명/차시명 단일 변수에 매핑 — 의미 약함 |
| `withdrawal_complete` | notice_withdrawal | manual | NONE 고정 본문 시스템 안내 |
| `payment_complete` | notice_payment | manual | NONE 고정 본문 시스템 안내. 2026-07-08 현재 provider SID 누락으로 fail-closed |
| `payment_due_days_before` | notice_payment | manual | NONE 고정 본문 시스템 안내. 2026-07-08 현재 provider SID 누락으로 fail-closed |

### 의도적으로 매핑 제외된 트리거 (`alimtalk_content_builders.py` 주석)

다음 트리거들은 코드 자동 발화 (`IMPLEMENTED_AUTO_TRIGGERS`) 대상이지만 현재 4종 ITEM_LIST + 2종 NONE 봉투 중 의미상 적절한 매칭이 없어 통합 매핑을 의도적으로 제외:

| 트리거 | 매핑 제외 사유 |
|---|---|
| `video_encoding_complete` / `matchup_report_submitted` | "[성적표 안내]" prefix 의미 불일치 (강사 본인/owner/admin 알림) |
| `qna_answered` | 공용 ITEM_LIST 봉투와 의미가 맞지 않아 통합 매핑은 유지하지 않는다. 별도 owner exact 템플릿은 학생 이름과 사이트 링크만 치환하는 고정 문구이며, provider와 DB가 모두 `APPROVED`일 때만 발송한다. |
| `counsel_answered` | 한 때 TYPE_SCORE 재사용([v1.2.0 release](../releases/v1.2.0.md) §6) 이었으나 prefix 의미 불일치로 매핑 제거. test_alimtalk_content_builders.py:55-60 None assert 적용 |

### 매핑 X 트리거의 실제 발송 path

`build_unified_replacements` → trigger 매핑 X → `return []` (빈 replacements). 이후 `notification_service.send_event_notification` 기존 모드 진입:
1. tenant AutoSendConfig는 enabled/body memo 확인에만 사용
2. owner tenant (T1 hakwonplus) 의 같은 trigger AutoSendConfig template이 `solapi_status=APPROVED`이고 SID가 있을 때만 발송
3. owner exact approved template이 없으면 발송 차단 (`return False`)

→ 즉, 위 매핑 제외 trigger 들의 실제 운영 발송 여부 = owner tenant AutoSendConfig 의 별도 승인 template 등록 상태에 의존. **tenant template, 자유양식, 다른 trigger로 fallback하지 않는다.** Q&A 답변 알림은 전용 고정 문구의 검수 상태가 `PENDING`/`REJECTED`이면 fail-closed하며, `APPROVED` 확인 뒤에만 opt-in 테넌트에서 발송한다. 기존 답변을 소급 발송하지 않는다.

**참고:** `clinic_reservation_changed`와 `clinic_cancelled`는 `clinic_change` 템플릿을 사용하여 기존일정/변동사항/수정자 변수를 ITEM_LIST에 표시.

---

## 4. 자동 발송 트리거 전체 목록

### AutoSendConfig.Trigger choices

출처: `models.py:187-229`

| 그룹 | 트리거 | 라벨 | 정책 분류 |
|------|--------|------|-----------|
| A. 가입/등록 | `student_signup` | 가입 완료(레거시/미사용) | DISABLED |
| | `registration_approved_student` | 가입 안내(학생) | SYSTEM_AUTO |
| | `registration_approved_parent` | 가입 안내(학부모) | SYSTEM_AUTO |
| | `class_enrollment_complete` | 반 등록 완료 | DISABLED |
| | `enrollment_expiring_soon` | 등록 만료 예정 | DISABLED |
| | `withdrawal_complete` | 퇴원 처리 완료 | MANUAL_DEFAULT |
| B. 출결 | `lecture_session_reminder` | 수업 시작 N분 전 | MANUAL_DEFAULT |
| | `check_in_complete` | 입실 완료(일반 강의) | MANUAL_DEFAULT |
| | `absent_occurred` | 결석 발생(일반 강의) | MANUAL_DEFAULT |
| C. 시험 | `exam_scheduled_days_before` | 시험 예정 N일 전 | MANUAL_DEFAULT |
| | `exam_start_minutes_before` | 시험 시작 N분 전 | MANUAL_DEFAULT |
| | `exam_not_taken` | 시험 미응시 | MANUAL_DEFAULT |
| | `exam_score_published` | 성적 공개 | MANUAL_DEFAULT |
| | `retake_assigned` | 재시험 대상 지정 | MANUAL_DEFAULT |
| D. 과제 | `assignment_registered` | 과제 등록 | MANUAL_DEFAULT |
| | `assignment_due_hours_before` | 과제 마감 N시간 전 | MANUAL_DEFAULT |
| | `assignment_not_submitted` | 과제 미제출 | MANUAL_DEFAULT |
| E. 성적 | `monthly_report_generated` | 월간 성적 리포트 발송 | MANUAL_DEFAULT |
| F. 클리닉 | `clinic_reminder` | 클리닉 시작 N분 전 | AUTO_DEFAULT |
| | `clinic_reservation_created` | 클리닉 예약 완료 | AUTO_DEFAULT |
| | `clinic_reservation_changed` | 클리닉 예약 변경 | AUTO_DEFAULT |
| | `clinic_cancelled` | 클리닉 예약 취소 | AUTO_DEFAULT |
| | `clinic_check_in` | 클리닉 입실 | AUTO_DEFAULT |
| | `clinic_check_out` | 클리닉 하원 | AUTO_DEFAULT |
| | `clinic_absent` | 클리닉 결석 | AUTO_DEFAULT |
| | `clinic_self_study_completed` | 자율학습 완료 | AUTO_DEFAULT |
| | `clinic_result_notification` | 클리닉 대상 해소(완료) | AUTO_DEFAULT |
| | `counseling_reservation_created` | 상담 예약 완료 | AUTO_DEFAULT |
| G. 결제 | `payment_complete` | 결제 완료 | MANUAL_DEFAULT |
| | `payment_due_days_before` | 납부 예정일 N일 전 | MANUAL_DEFAULT |
| I. 비밀번호 | `password_find_otp` | 비밀번호 찾기 인증번호 (legacy OTP) | SYSTEM_AUTO |
| | `password_reset_student` | 비밀번호 재설정(학생) | SYSTEM_AUTO |
| | `password_reset_parent` | 비밀번호 재설정(학부모) | SYSTEM_AUTO |

정책 분류 출처: `policy.py:25-67`

### 정책 분류 의미

| 분류 | 의미 |
|------|------|
| SYSTEM_AUTO | 시스템 필수. 항상 자동. 사용자가 끌 수 없음 |
| AUTO_DEFAULT | 자동 기본값. 사용자가 끌 수 있음 |
| MANUAL_DEFAULT | 수동 기본값. preview -> confirm 필요. 사용자가 자동화 가능 |
| DISABLED | 현재 비활성 |

### 각 트리거의 코드 호출 위치

| 트리거 | 호출 파일:라인 | 비고 |
|--------|---------------|------|
| `clinic_reservation_created` | `clinic/services/lifecycle.py` + `clinic/views/participant_views.py` | create service 이벤트, view가 `on_commit` 발송 |
| `clinic_reservation_changed` | `clinic/services/lifecycle.py` + `clinic/views/participant_views.py` | change_booking service 이벤트 뒤 view가 발송 결과를 응답에 포함 |
| `clinic_cancelled` | `clinic/services/lifecycle.py` + `clinic/views/participant_views.py` | service가 이벤트 선택하고 view가 발송 결과를 응답에 포함 |
| `clinic_check_in` | `clinic/services/lifecycle.py` + `clinic/views/participant_views.py` | service가 이벤트 선택하고 view가 발송 결과를 응답에 포함 |
| `clinic_check_out` | `clinic/services/lifecycle.py` + `clinic/views/participant_views.py` | 정상/미등원 하원의 별도 checkout 이벤트 뒤 view가 선택 수신자 발송 결과를 응답에 포함 |
| `clinic_absent` | `clinic/services/lifecycle.py` + `clinic/views/participant_views.py` | service가 이벤트 선택하고 view가 발송 결과를 응답에 포함 |
| `clinic_self_study_completed` | `clinic/services/lifecycle.py` + `clinic/views/participant_views.py` | complete service 이벤트 뒤 view가 발송 결과를 응답에 포함 |
| `registration_approved_*` | 신규 학생 첫 ACTIVE 수강 확정 + 계정 변경 플로우 | `send_alimtalk_via_owner` 경유 |
| `password_find_otp` | legacy OTP 경로 | `send_alimtalk_via_owner` 경유 |
| `password_reset_*` | 현재 공개 비밀번호 찾기 + 관리자/선생님 재설정 | `send_alimtalk_via_owner` 경유 |

### 도메인 코드에서 send_event_notification 호출이 확인되지 않은 트리거

- `student_signup` (DISABLED, 레거시)
- `class_enrollment_complete` (DISABLED)
- `enrollment_expiring_soon` (DISABLED)
- `withdrawal_complete` (MANUAL_DEFAULT)
- `lecture_session_reminder` (MANUAL_DEFAULT, minutes_before=30)
- `check_in_complete`, `absent_occurred` (MANUAL_DEFAULT, 출결 알림 preview→confirm 전용)
- `exam_scheduled_days_before` ~ `retake_assigned` (MANUAL_DEFAULT, 시험 관련 5개)
- `assignment_registered` ~ `assignment_not_submitted` (MANUAL_DEFAULT, 과제 관련 3개)
- `monthly_report_generated` (MANUAL_DEFAULT)
- `clinic_reminder` (AUTO_DEFAULT, minutes_before=30, EventBridge 운영 스케줄 적용)
- `clinic_result_notification` (AUTO_DEFAULT)
- `counseling_reservation_created` (AUTO_DEFAULT)
- `payment_complete`, `payment_due_days_before` (MANUAL_DEFAULT)

**참고:** minutes_before가 있는 트리거는 스케줄러가 호출해야 하며, 현재 스케줄러 구현 상태는 별도 확인 필요.

---

## 5. 수동 발송 경로

### SendMessageView 알림톡 라우팅

출처: `views/send_views.py`, `services/preflight.py`

1. `message_mode == "alimtalk"`이면:
2. 현재 화면이 보낸 `block_category`를 `get_unified_for_manual_send()`로 먼저 매핑
3. 통합 ITEM_LIST 4종 또는 NONE 2종 매핑이 있으면 현재 화면의 Solapi 봉투를 사용하고, 저장 문구의 과거 카테고리는 편지 내용만 재사용한다. 단, 자체 승인 양식을 쓰는 `signup`과 고정 시스템 본문인 `payment`는 저장 템플릿 카테고리를 유지한다.
4. `signup` 카테고리면 자체 Solapi 템플릿 유지 (`SYSTEM_TEMPLATE_CATEGORIES`)
5. 현재 화면 카테고리에 봉투 매핑이 없을 때만 저장 템플릿의 `category`와 `name`을 fallback으로 사용
6. 그래도 매핑이 없으면 fail-closed. 자유양식/공지형 fallback은 사용하지 않는다.
7. 매핑된 봉투는 `build_manual_replacements()`로 실제 Solapi 등록 변수와 일치하는 replacements 세트를 빌드
8. preflight 응답의 `preview_recipients[].full_message_body`는 같은 replacements로 승인 봉투 전체 문구를 서버에서 렌더링한다.

### 발송 직전 실제 문구 미리보기 계약

- 최종 미리보기의 학원명, 학생명, 사이트 링크, 빈 값 `"-"`, ITEM_LIST 23자 절단은 실제 Solapi replacements와 동일한 서버 계산값을 사용한다.
- 저장 문구가 다른 카테고리에서 만들어졌더라도 현재 발송 화면의 봉투를 바꾸지 않는다. 예를 들어 성적 화면에서 클리닉 문구를 재사용해도 preflight와 실발송은 모두 `score` 봉투를 사용한다.
- `payment`처럼 본문과 봉투가 고정된 시스템 템플릿은 다른 진입 화면의 봉투로 fallback하지 않으며, 승인 SID가 없으면 발송을 차단한다.
- 클라이언트가 샘플 학원명·강의명·차시·날짜를 임의로 조립하지 않는다. `preview_recipients` 또는 `full_message_body`가 없거나 수신자 수와 맞지 않으면 발송을 fail-close한다.
- 전화번호는 마스킹해 반환하고, 유효한 학생/학부모 번호가 없는 대상은 `excluded`와 `exclude_reason`으로 구분한다.
- 여러 학생을 선택한 경우 학생 선택은 표시할 `full_message_body`만 바꾸며, 실제 발송 대상 집합은 바꾸지 않는다.
- 공용 `preview -> confirm` 경로도 `full_message_body`를 표시한다. 1회용 confirm 토큰에는 발송에 불필요한 이 표시 전용 필드를 저장하지 않는다.
- `grades` 수동 발송은 해석된 전체 학생 ID 집합과 `alimtalk_extra_vars_per_student` 키 집합이 정확히 같고, 모든 학생 항목에 비어 있지 않은 `_body_subst`가 있어야 한다. preflight와 confirm이 같은 검사를 수행하며, 한 명이라도 누락·초과·형식 오류이면 `grade_personalization_incomplete`로 실패 폐쇄한다.
- 성적 본문은 학생별 `_body_subst`만 사용한다. 공용 `raw_body`나 `alimtalk_extra_vars`에 들어온 첫 선택 학생의 점수는 다른 학생의 대체값이 될 수 없다. 이 경계는 예약 발송을 포함해 outbox 생성 전에 적용한다.

### CATEGORY_TO_TEMPLATE_TYPE 매핑

출처: `alimtalk_content_builders.py:167-178` (2026-07-08 기준)

| 카테고리 | 템플릿 타입 | 비고 |
|----------|------------|---|
| grades | score | "[성적표 안내]" prefix 의미 일치 |
| attendance | attendance | |
| lecture | attendance | |
| exam | attendance | 시험 일정/미응시 안내를 강의/차시 컨텍스트 봉투에 담음 |
| assignment | attendance | 과제 등록/마감/미제출 안내를 강의/차시 컨텍스트 봉투에 담음 |
| clinic | clinic_info (또는 clinic_change*) | |
| payment | notice_payment | NONE 고정 본문 시스템 안내. 2026-07-08 현재 provider SID 누락으로 fail-closed |

### 매핑 의도적 제외 카테고리

다음 카테고리는 코드 매핑 없음. `get_unified_for_category` → `(None, None)` 반환:

- **notice / community / staff / default / student**: 카카오 등록 양식 부재

→ 위 카테고리로 호출 시 `get_unified_for_category` 가 (None, None) 반환. 다른 카테고리/템플릿으로 fallback하지 않는다.

*clinic 카테고리: template_name에 "변경/취소/change/cancel/reschedule" 키워드가 있거나, extra_vars에 클리닉기존일정/클리닉변동사항/클리닉수정자가 있으면 clinic_change. 그 외 clinic_info. (`get_unified_for_category` line 163-189)

### 시스템 기본양식 (통합 승인 봉투 제외)

`SYSTEM_TEMPLATE_CATEGORIES = frozenset({"signup"})` (line 117)

signup 카테고리만 자체 Solapi 템플릿을 유지. 나머지 매핑 카테고리는 통합 ITEM_LIST 4종 또는 NONE 2종 승인 봉투로 라우팅.

### 수동 발송 제약

- `message_mode` 기본값: `"alimtalk"`
- `message_mode` 기본값은 알림톡이지만, 명시된 비알림톡 값은 알림톡으로 보정하지 않고 차단한다.
- 알림톡 모드이면 공용 승인 `solapi_template_id` 필수
- Rate limit: 업무 tenant별 rolling 1시간 500건 + 공유 공급자 계정 KST 일일 900건 기본 안전 한도
- 최대 200명 일괄 발송 (line 504-508)
- 발신번호/provider/API 키는 공용 Solapi 설정만 사용한다. PFID와 provider template ID는 worker가 검증된 tenant channel binding이 활성일 때만 마지막 단계에서 치환한다.

---

## 6. Provider/채널 정책

### 공용 provider와 검증된 tenant 채널

- owner tenant는 승인 채널 인프라의 소유자다. owner 학원의 고객용 `messaging_is_active=false`는 다른 업무 tenant로 전파하지 않으며, 공용 채널 경계에서 공유하는 차단은 테스트 tenant와 긴급 운영 hold뿐이다. 업무 tenant 자신의 고객 토글은 계속 발송 전에 fail-closed로 검사한다.

출처: `policy.py`, `queue_service.py`, `sqs_main.py`

- 실발송 provider/API 키/발신번호는 `OWNER_TENANT_ID` 공용 Solapi 설정만 사용한다.
- 기본 PFID는 공용 owner 채널이다. 새 `AlimtalkChannelBinding`이 `active`이고 요청된 공용 template ID에 대응하는 `AlimtalkTemplateBinding`이 `APPROVED`이며 양쪽 provider 전달 동작 지문이 같을 때만 해당 tenant PFID/template ID로 치환한다. 전달 동작 지문은 본문·버튼·강조 구조를 비교하고, 검수용 카테고리와 provider 응답의 빈 기본값은 제외한다.
- 과거 tenant `messaging_provider`, `kakao_pfid`, 자체 Solapi/Ppurio 키와 기존 MessageTemplate 승인 표시는 새 binding을 만들거나 활성화하는 근거로 사용하지 않는다.
- 공용 템플릿의 legacy `categoryCode=TE`는 신규 복제 때 현재 Kakao 6자리 분류로 의미에 맞게 변환한다. 변수와 아이템 구조로 가입·인증·예약·서비스 신청·피드백 중 하나를 정확히 결정할 수 없으면 등록 전에 fail-closed한다.
- `enqueue_alimtalk()`는 알림톡 payload의 `tenant_id`를 owner tenant로 정규화하고 원 업무 테넌트는 `source_tenant_id`로 남긴다.
- worker도 raw/legacy SQS payload의 `tenant_id`를 owner tenant로 재정규화한다. 공용 채널의 물리 발송·로그 tenant는 owner를 유지하지만, 단가·잔액 차감·환불은 `source_tenant_id`로 식별한 실제 업무 테넌트에 귀속한다. 모든 canonical payload의 tenant 결합은 producer HMAC과 durable outbox tenant로 검증하며 서로 모순되는 payload는 발송 전에 폐기한다.
- 신규 canonical payload는 `occurrence_key`를 싣는다. worker는 payload의 tenant/channel/event/target/recipient/occurrence/template로 business key를 다시 계산하며 producer key와 다르면 `invalid_business_idempotency_key`로 폐기한다. 따라서 유효한 signed key만 복사해 수신번호를 변경해도 provider로 진행하지 않는다.

### SQS tenant-binding 키와 배포 계약

- HMAC 키 SSOT는 API·worker SSM env에 동일하게 저장한 전용 `MESSAGING_TENANT_BINDING_KEY`다. Django `SECRET_KEY`나 공급사 키를 재사용하지 않는다.
- `MESSAGING_TENANT_BINDING_ENFORCED`의 코드 기본값은 `true`다. 기존 unsigned backlog가 있는 최초 도입 때만 명시적으로 `false`를 사용한다.
- 도입 순서: 전용 키를 두 env에 동기화 → enforcement=false로 signer API와 검증 worker 배포 → 모든 API producer가 새 이미지인지 확인하고 unsigned SQS backlog가 0이 될 때까지 drain → `scripts/v1/set-messaging-tenant-binding-enforcement.ps1 -Enabled true -RefreshMessagingWorker` → unsigned 거부 로그와 정상 signed canary 확인. 이 entrypoint는 shared production mutation lock 아래 exact worker refresh가 terminal `Successful`일 때까지 기다리며, 실패·취소·timeout이면 lock을 남겨 forward-convergence 전 다른 release를 차단한다.
- `false` 상태를 일반 운영 모드로 두지 않는다. release seal과 배포 검증은 최종 worker runtime 값이 `true`임을 확인한다.
- 키 순환은 새 키를 fallback으로 먼저 worker fleet에 배포하고, signer primary 전환 후 구 API/queue backlog를 drain한 다음 구 키를 제거하는 양방향 호환 순서로 수행한다. 검증 중에는 `MESSAGING_TENANT_BINDING_FALLBACK_KEYS`를 사용한다.
- 서명 누락(강제 모드) 또는 불일치 payload는 공급사 호출·과금 전에 terminal delete하며, owner `NotificationLog.failure_reason`에 `missing_tenant_binding_signature` 또는 `invalid_tenant_binding_signature`를 남긴다. 로그에는 수신번호·본문을 저장하지 않는다.

### 알림톡 채널 결정

출처: `policy.py`, `tenant_channels.py`, `sqs_main.py`

- `resolve_kakao_channel()`은 항상 `settings.SOLAPI_KAKAO_PF_ID` 공용 PFID를 반환한다.
- worker는 공용 template allowlist와 tenant-binding HMAC을 먼저 검증한 뒤 `resolve_alimtalk_delivery_route()`에서만 활성 tenant channel로 번역한다.
- `channel_source` API 값은 `common_owner`, `tenant_pending`, `tenant_verified`, `tenant_suspended` 중 하나이며 PFID 원문은 화면에 노출하지 않는다.
- 검수 중 tenant 채널은 공용 경로를 유지한다. 활성 tenant 채널에서 template mapping이 누락되거나 본문 지문이 달라지면 `tenant_suspended`로 표시하고 공용으로 fallback하지 않고 fail-closed한다.

### SMS 정책

출처: `policy.py`, `queue_service.py`, `sqs_main.py`

- SMS/LMS 실발송 전체 금지.
- `message_mode="sms"` 신규 enqueue는 `MessagingPolicyError(reason="sms_disabled")`.
- `message_mode="lms"`도 `sms_disabled`, 그 밖의 명시 비알림톡 값은 `unsupported_message_mode`로 차단한다.
- worker가 legacy SMS payload를 받으면 발송하지 않고 실패 로그로 닫는다.
- SMS/LMS 호환 callable과 `sms_allowed` API capability 필드는 제공하지 않는다.
- 운영 오류 알림에도 SMS 예외가 없으며 `check_dev_alerts`는 Slack webhook만 사용한다.
- 테스트 테넌트(9999): 모든 메시징 비활성

### 테넌트 자체 연동 키

출처: `policy.py`

legacy 설정 필드는 남아 있을 수 있으나 신규 실발송 경로에서 사용하지 않는다.
기존 `messaging_sender`·provider·자체 키 데이터는 삭제하지 않고 읽기 전용 이력으로 보존한다.

### 제한 테넌트

출처: `policy.py:83`

`RESTRICTED_MESSAGING_TENANTS = frozenset()` -- 현재 비어 있음 (림글리쉬 제한 해제 완료).

## 7. SQS 메시지 구조

출처: `sqs_queue.py:102-128`

| 필드 | 타입 | 설명 |
|------|------|------|
| `tenant_id` | int | 공용 owner 테넌트 ID |
| `source_tenant_id` | int/null | 원 업무 테넌트 ID. 발송 채널/provider 결정에는 사용하지 않음 |
| `to` | str | 수신 번호 (하이픈 제거됨) |
| `text` | str | legacy envelope 호환용 본문. 알림톡 fallback SMS에는 사용하지 않음 |
| `sender` | null | 공용 owner 발신 설정을 worker가 사용 |
| `created_at` | str (ISO) | 생성 시각 |
| `message_mode` | "alimtalk" | 발송 방식. SMS/LMS는 차단 |
| `reservation_id` | int (optional) | 예약 ID (워커에서 취소 여부 double check) |
| `alimtalk_replacements` | list[{key, value}] (optional) | 알림톡 템플릿 치환 변수 |
| `template_id` | str (optional) | Solapi 알림톡 템플릿 ID |
| `event_type` | str (optional) | 비즈니스 이벤트 유형 (30자 제한, 멱등성 키용) |
| `business_idempotency_key` | str | SHA-256 해시 (중복 발송 방지) |
| `tenant_binding_signature_version` | "v1" | 서명 material 스키마 버전 |
| `tenant_binding_signature` | str | 전용 shared key로 `tenant_id`·`source_tenant_id`·멱등성 키를 인증한 HMAC-SHA256 |

SQS 큐 이름: `academy-v1-messaging-queue` (SSOT, `sqs_queue.py:53`)
DLQ: `academy-v1-messaging-queue-dlq` (line 54)

---

## 8. 멱등성/중복 방지

### 3-Layer 중복 방지

| Layer | 위치 | 메커니즘 |
|-------|------|----------|
| Layer 1: Redis Lock | sqs_main.py:373 | `acquire_job_lock(f"messaging:{message_id}")` - SQS MessageId 기준. Redis 장애 시 fail-closed (메시지 SQS에 남겨 재시도) |
| Layer 2: DB Claim | `repositories_messaging.py` | `claim_notification_slot()` - business_idempotency_key 기준 UniqueConstraint. 공급사 호출 전 `processing`, 호출 경계부터 `sending`으로 분리 |
| Layer 3: Legacy Key Guard | `sqs_main.py` | producer key가 없는 legacy payload도 SQS MessageId+canonical payload SHA-256 키를 만들어 DB claim을 반드시 거침 |

### business_idempotency_key 구조

출처: `sqs_queue.py:22-34`

```
canonical = f"msg:{tenant_id}:{source_tenant_id}:{channel}:{event_type}:{target_type}:{target_id}:{recipient}:{occurrence_key}:{template_id}"
SHA-256(canonical) -> 64자 hex
```

- `tenant_id`: 공용 owner tenant
- `source_tenant_id`: 원 업무 tenant. owner 직접 발송이면 빈 문자열
- `channel`: "alimtalk"만 신규 발송 가능
- `event_type`: 트리거명 또는 "manual_send"
- `occurrence_key`: 도메인 호출자 지정 키. 수동 즉시/예약 발송은 DB dispatch UUID에서 만든 안정 키
- DB UniqueConstraint: `(tenant, message_mode, business_idempotency_key)` where `business_idempotency_key > ""` (models.py:64-68)

### 개인정보 없는 원천/수신자 추적

- `ScheduledNotification`과 `NotificationLog`는 같은 `business_idempotency_key`로 outbox→worker claim을 연결한다. 별도 중복 trace ID는 만들지 않는다.
- `recipient_fingerprint`는 정규화 수신번호를 전용 `MESSAGING_TENANT_BINDING_KEY`로 HMAC-SHA256한 64자 값이다. 원문 번호를 terminal payload나 진단 출력에 남기지 않고도 정확한 번호 입력으로 관련 행을 찾는다.
- `origin_type`/`origin_id`는 producer 종류와 job/batch/domain ID다. Excel 신규 학생은 `excel_import`와 AIJob job ID를 pending 계정 안내에 보존했다가 첫 ACTIVE 수강 outbox와 worker log로 전달한다.
- HMAC primary key 순환 중 진단은 `MESSAGING_TENANT_BINDING_FALLBACK_KEYS`로 만든 후보도 조회한다. fallback 제거 뒤에는 제거된 키로 만든 과거 지문을 새 번호 입력만으로 재계산할 수 없으므로 incident 보존 기간과 key rotation drain을 맞춘다.

### DB dispatch(outbox) 상태와 SQS 재시도

- 수동·시스템·영상·매치업·커뮤니티의 즉시 발송과 예약/지연 발송은 모두 `ScheduledNotification` 행을 먼저 만든다. `enqueue_alimtalk()`는 outbox drainer 전용 경계이며 product producer가 직접 호출하지 않는다.
- 상태는 `pending → dispatching → sent` 순서다. 여기서 `sent`/`sent_at`은 **SQS 접수 완료**이며 공급사 최종 발송 성공이 아니다.
- `dispatch_key`는 행마다 고유한 UUID이고 payload의 `occurrence_key=dispatch:<uuid>`로 고정된다. 폴러/프로세스가 재시작되어 같은 행을 다시 enqueue해도 business key는 바뀌지 않는다.
- SQS enqueue가 실패하면 영구 실패로 닫지 않는다. 30초부터 지수 백오프하며 최대 8회 시도 후 `failed`가 된다. 입력 누락, SMS 차단, `MessagingPolicyError`는 즉시 terminal `failed`다.
- `dispatching`이 5분 이상이면 죽은 폴러 claim으로 보고 회수한다. 외부 SQS 호출은 DB transaction과 row lock 밖에서 실행한다.
- `operations/status`는 `retry_waiting`, `dispatching`, `stale_dispatching`을 별도로 노출한다.
- tenant별 rolling 1시간 한도는 500건이다. 별도로 공유 provider 계정은 KST 00:00~24:00의 outbox attempt 예약과 outbox에 없는 legacy log를 합산해 `MESSAGING_PROVIDER_DAILY_DISPATCH_LIMIT`(기본 900)으로 제한한다. PostgreSQL transaction advisory lock 아래 tenant 간 claim을 직렬화하며 owner tenant 행의 존재 여부에는 의존하지 않는다. 한도 도달 신규 행은 `provider_daily_dispatch_quota_deferred`와 다음 날 00:05 KST `next_attempt_at`을 남긴다. 재시도 중 같은 일일 예약은 중복 집계하지 않는다.
- 수동 preflight와 `operations/status.rate_limit_provider_daily`는 전역 limit/used/remaining을 노출한다. 예약 발송은 예약 시점에 오늘 한도를 선점하지 않고 실제 due claim 시점에 검사한다.

### 워커 provider 상태와 exactly-once 경계

`NotificationLog.status` 상태 전이는 아래가 정본이다.

| 상태 | 의미 | 자동 재시도 |
|---|---|---|
| `processing` | business key 선점, 공급사 호출 전 | stale 또는 명시적 `retryable_failed`만 가능 |
| `sending` | 비멱등 공급사 호출 경계를 DB에 먼저 기록 | 금지 |
| `sent` | 공급사가 접수를 성공 응답하고 DB 확정 | 금지 |
| `retryable_failed` | 공급사 호출 전 확정된 일시 실패 | 가능 |
| `failed` | 설정/템플릿/권한 등 확정 실패 | 금지 |
| `ambiguous` | 공급사 호출 후 timeout/통신 오류로 접수 여부 불명 | 금지, 운영자 확인 |

- DB, SQS, 외부 공급사 사이에는 공통 transaction이 없고 공급사가 공식 idempotency 계약을 제공하지 않으므로 수학적인 exactly-once는 불가능하다.
- 클리닉 설정의 이력은 `scope=clinic`으로 clinic trigger만 조회한다. 예약 outbox는
  `scheduled/pending/dispatching/cancelled/failed`, worker log는
  `pending/dispatching/provider_accepted/failed/skipped`로 설명하고, 정책·템플릿·수신자
  차단은 실패 발송이 아니라 `skipped`로 표시한다.
- log 상세의 `verify_provider=true`는 저장된 provider group ID 하나만 read-only
  조회한다. Solapi `statusCode=2000`은 공급사 접수, `3000`은 통신사 결과 대기이며
  모든 메시지가 `statusCode=4000`일 때만 `delivered`다. 다른 terminal 코드는 `failed`, 조회 실패는
  `unavailable`이며 저장된 접수 근거를 전달 완료로 오인하지 않는다. 정확한 group ID는
  현재 tenant의 owner/admin/teacher/staff가 실제 수신자와 대조할 수 있으며 공급자
  자격증명이나 공급자 실패 원문은 반환하지 않는다.
- 중복 발송 방지를 우선해 `sending` 이후에는 워커 재시작, 같은 SQS 재전달, 다른 SQS MessageId의 중복 전달 모두 공급사를 다시 호출하지 않는다. 같은 SQS 메시지가 재전달되면 provider 응답 저장 실패/worker 중단으로 간주해 `sending→ambiguous`를 원자 반영하고 기존 차감액을 유지한다.
- 대가는 at-most-once 모호 구간이다. 현재 `sending` DB 기록부터 실제 발송 endpoint 호출 전까지 Solapi는 보통 짧은 client 준비 구간이고, Ppurio는 token preflight timeout 최대 10초를 포함한다. 호출 후 응답 모호 구간은 Ppurio request timeout 최대 15초다. 이 구간 crash/timeout은 `ambiguous`이며 자동 재발송하지 않는다.
- 크레딧 예약은 `NotificationLog.amount_deducted`와 tenant 잔액을 한 transaction에서 기록하고, 재선점/롤백도 멱등 처리한다. `ambiguous`는 실제 접수 가능성이 있어 자동 환불하지 않는다.

### _domain_object_id

도메인 코드에서 context에 `_domain_object_id`를 전달하여 occurrence_key로 사용. 예:

- `f"clinic_participant_{obj.pk}"` (`clinic/views/participant_views.py` create)
- `f"participant_{participant.pk}_{next_status}_{int(time.time())}"` (`clinic/services/lifecycle.py`)
- `f"booking_change_{new_booking.pk}"` (`clinic/views/participant_views.py`)
- 일반 강의 입실·결석 수동 발송은 preview token의 batch occurrence를 사용하며,
  출결 상태 저장은 occurrence나 outbox를 만들지 않는다.

---

## 9. 안전장치 체계

### Dry-Run

출처: `policy.py:141-173`

- 환경변수 `MESSAGING_DRY_RUN_TRIGGERS`:
  - `"*"`: 모든 이벤트 트리거 dry-run (가입/비번 제외)
  - `"check_in_complete,absent_occurred"`: 특정 트리거만
  - 비어있으면: dry-run 없음 (운영 모드)
- 가입/비번 관련 5개 트리거는 dry-run 대상에서 **항상 제외** (ALWAYS_LIVE_TRIGGERS, line 160-166)

### 수신자 denylist와 테스트 화이트리스트

출처: `policy.py:106-138`

- 환경변수 `MESSAGING_TEST_WHITELIST`: 콤마 구분 전화번호
- 설정 시: 해당 번호에만 실발송 (테스트 모드)
- 미설정 시: 모든 번호 허용 (운영 모드)
- 환경변수 `MESSAGING_RECIPIENT_DENYLIST`: 콤마 구분 전화번호. 하이픈 제거 후
  whitelist보다 먼저 적용하며 API enqueue, 워커 소비, 공용 Solapi 호출 직전에
  같은 번호를 다시 거부한다. 이미 큐에 들어간 메시지도 공급사에 전달하지 않는다.

### 테넌트 레벨 비활성화

- `Tenant.messaging_is_active`: 대표·관리자가 자동발송 화면의 **알림톡 전체 사용**에서 직접 바꾸는 고객 설정. 기본값은 `true`이며 `false`이면 수동·자동·계정 알림을 모두 중지한다. 변경은 `messaging.tenant_activation` 감사 로그에 남는다.
- `is_messaging_disabled(tenant_id)`: TEST_TENANT_ID(기본 9999), 고객 설정 off 또는 긴급 운영 hold이면 모든 메시징 스킵. 존재하지 않거나 DB 설정을 확인할 수 없는 tenant는 fail-closed다.
- 기존 ymath 중지 요청은 코드 상수가 아니라 `Tenant.messaging_is_active=false` 고객 설정으로 이관한다.
- `MESSAGING_DISABLED_TENANT_IDS`: 오발송·중복 확산 같은 긴급 사고에만 쓰는 런타임 env 콤마 구분 hold다. 온보딩 승인이나 고객 선호를 여기에 저장하지 않는다.
  운영 변경 후 API와 messaging worker가 새 env를 읽었는지 확인해야 하며, 최소한
  messaging worker는 재기동 전까지 기존 큐를 소비하게 두지 않는다.
- `is_messaging_restricted(tenant_id)`: RESTRICTED_MESSAGING_TENANTS에 포함되면 비계정 메시징 차단 (현재 비어있음)
- `AutoSendConfig.enabled`: 트리거별 on/off (models.py:249)

### 워커 레벨 안전장치

- 예약 취소 Double Check: `reservation_id` 있으면 발송 직전 DB 확인 (sqs_main.py:441-457)
- 잔액 검증: 발송 전 잔액 < 단가이면 스킵 (sqs_main.py:564-596)
- 테넌트 ID 필수: `tenant_id` 없으면 메시지 삭제 (sqs_main.py:414-425)
- 수신자 운영 차단: denylist 번호는 큐 입구에서 삭제하고 provider 호출 직전에 재확인
- `sending`/`ambiguous` 결과는 자동 재발송 금지. `operations/status.log_24h.action_required`(`sending + ambiguous`)와 `provider_outcome_ambiguous` risk로 확인
- Solapi `QuotaExceeded`·`NotEnoughBalance`는 provider ID·차감 없는 접수 전 거절이므로 terminal failed로 닫고 자동 재발송하지 않는다. 그 밖의 timeout/연결 단절은 기존처럼 ambiguous로 보존한다.
- 결제 `notice_payment`처럼 논리 매핑은 있으나 provider SID가 없는 유형은 preflight와 실제 send 모두 `unified_template_unavailable`로 fail-close. 과거 tenant template SID로 fallback 금지

### 제품 메시징 incident 진단

```powershell
python manage.py diagnose_messaging_incident `
  --tenant-id 11 `
  --recipient 010-0000-0000 `
  --origin-id <excel-job-id> `
  --since-hours 72 `
  --provider
```

- `--tenant-id`는 필수이며 `--recipient`와 `--origin-id`는 함께 또는 각각 사용할 수 있다. `--provider`는 exact `--recipient`가 있을 때만 허용한다.
- JSON은 outbox/log status·trigger·origin type, business-key 연결 누락 건수, provider ATA/statusCode/disableSms 집계만 반환한다. 전화번호, message body, 계정 값, target name, provider message/group ID, 입력 origin ID는 출력하지 않는다.
- provider 조회는 read-only다. `sent` outbox는 SQS 접수, `NotificationLog.status=sent`는 provider 접수 성공이므로 최종 공급자 상태는 `--provider` 집계를 함께 본다. `sending`/`ambiguous`는 자동 재발송하지 않는다.
- provider 조회는 Solapi SDK가 지원하는 KST 달력 날짜 문자열로 범위를 전달하고, 목록 API의 최대 page size인 500건씩 최대 10 page를 읽는다. 진단 실행 중 `httpx`/`httpcore`/`solapi` 요청 로그는 exact recipient가 URL에 노출되지 않도록 일시 억제하고, 완료 후 logger 상태를 복구한다. JSON의 `provider.window`에는 실제 조회한 KST 시작일·종료일만 남긴다.

### 금지 패턴

- signal/post_save에서 자동 발송
- bulk_create/bulk_update에서 자동 발송
- on_commit 콜백에서 행정 작업 결과로 자동 발송 (가입 안내 제외)
- AutoSendToggle을 행정 화면(출결/시험/과제)에 노출
- preview 없이 confirm 직접 호출
- 과거 날짜 이벤트에 대한 현재 시점 알림 발송

---

## 10. 카카오 ITEM_LIST 제약 사항

### 23자 제한

- ITEM_LIST 템플릿의 변수 값은 23자 이하 (카카오 정책)
- `ITEM_LIST_VAR_MAX_LEN = 23` (`alimtalk_content_builders.py:291`)
- 선생님메모와 사이트링크는 제한 미적용
- 초과 시 22자 + "..." 로 truncate

### 중복 필터 정책

- Solapi에 등록된 변수 **전체**를 보내야 함. 누락 시 에러.
- 값이 없는 변수는 `"-"` 로 전달 (`alimtalk_content_builders.py:262`, `441`)

### 카카오 에러 코드

| 에러 | 의미 | 발생 조건 |
|------|------|-----------|
| 3063 | 잘못된 파라미터 | 등록 변수 누락 또는 변수명 불일치 |
| 3043 | 아이템 하이라이트 불일치 | templateItem/templateItemHighlight 불일치 (카카오 공식 코드) |
| 3076 | 변수값 길이 초과 | 변수 값이 23자 초과 |

---

## 11. 공용 정본 + 검증 tenant 채널 알림톡 only

### send_event_notification 템플릿 resolve

출처: `services.py:309-320`

1. 현재 테넌트의 AutoSendConfig 조회: enabled/delay/본문 메모만 사용
2. 검수 템플릿은 명시 unified category 템플릿 또는 오너 테넌트(`OWNER_TENANT_ID`, 기본 1)의 exact trigger 승인 템플릿만 사용
3. tenant 문구, 다른 trigger, SMS로 fallback하지 않음. 공용 승인 정본이 없으면 fail-closed
4. worker provider 직전 단계에서만 활성 tenant channel의 승인·동일 지문 template ID로 번역

### send_alimtalk_via_owner

출처: `apps/domains/messaging/policy.py`의 `send_alimtalk_via_owner()`

- 모든 테넌트에서 가입/비번 관련 알림톡은 오너 테넌트의 exact trigger 승인 템플릿으로 발송
- `password_reset_*`, `password_find_otp`가 `registration_approved_*` 템플릿을 재활용하는 fallback 금지
- SMS fallback 없음. 과거 tenant PFID/provider/자체 키 사용 없음. 새 verified binding이 없으면 공용 채널, 활성 binding의 exact mapping이 없으면 발송 실패

---

## 12. 명시적 프로비저닝

`GET /api/v1/messaging/auto-send/`는 완전한 읽기 요청이다. 누락된
트리거는 `enabled=false`인 가상 기본값으로 응답하며 `AutoSendConfig`나
`MessageTemplate`을 만들지 않는다. 따라서 설정 화면 열기, 새로고침, 사전
가져오기가 자동발송을 켜거나 과거 트리거를 되살릴 수 없다.

기본 템플릿과 설정 행의 생성·갱신은 아래
`ProvisionDefaultTemplatesView` POST에서만 일어난다. `{academy_name}` 치환,
기본 본문, 자유양식 템플릿 처리도 이 명시적 변경 요청 안에서만 수행한다.

### 기본 템플릿 목록

출처: `default_templates.py:13-399`

**자동발송 트리거 템플릿** (27개):
registration_approved_student, registration_approved_parent, withdrawal_complete, lecture_session_reminder, check_in_complete, absent_occurred, exam_scheduled_days_before, exam_start_minutes_before, exam_not_taken, exam_score_published, retake_assigned, assignment_registered, assignment_due_hours_before, assignment_not_submitted, monthly_report_generated, clinic_reminder, clinic_reservation_created, clinic_reservation_changed, clinic_self_study_completed, clinic_cancelled, clinic_check_in, clinic_check_out, clinic_absent, clinic_result_notification, counseling_reservation_created, payment_complete, payment_due_days_before

**자유양식 템플릿** (7개):
freeform_general, freeform_grades, freeform_lecture, freeform_exam, freeform_assignment, freeform_payment, freeform_clinic

### ProvisionDefaultTemplatesView

출처: `views.py:977`

POST로 기존 기본 템플릿 리셋 가능. 이름이 기본값과 동일한 템플릿은 최신 기본값으로 덮어쓰기. 사용자가 새로 만든 템플릿은 유지. 새 자동발송 설정은 안전 기본값인 `enabled=false`로 생성한다.

---

## 13. 클리닉/출결 알림 발송 패턴

### _send_clinic_notification

출처: `clinic/views/participant_views.py` + `clinic/services/lifecycle.py`

`_send_clinic_notification(..., send_to="student|parent|both")`은 운영자가 선택한
수신자만 exact 승인 알림톡 경로에 요청하고 `requested/failed` 결과를 API에 돌려준다.
다른 trigger나 SMS/LMS fallback은 없다.

- 예약 생성은 `transaction.on_commit()` 뒤 발송하고, 상태·하원·완료·일정 변경은 서비스의 DB 전이가 끝난 뒤 view가 발송 결과를 응답에 포함한다.
- 학생 본인이 예약을 생성·변경·취소하면 클라이언트의 `send_to` 입력과 무관하게 학생+학부모(`both`)를 사용한다. 직원이 처리하는 등원·결석·하원·완료·재촉은 기존 수신자 선택을 유지한다.
- create/change_booking/status/checkout/complete 계열은 `clinic.services.lifecycle`이 서로 다른 `ClinicNotificationEvent`를 반환한다.
- clinic_info context 변수: 클리닉명, 장소, 날짜, 시간, _domain_object_id
- clinic_change context 변수: 클리닉명, 장소, 날짜, 시간, 클리닉기존일정, 클리닉변동사항, 클리닉수정자, _domain_object_id
- `clinic_check_in`/`clinic_absent` 트리거 전용 추가 변수: **도착시간** (상태 처리 시점의 `timezone.now()` → `HH:MM` 포맷). 선생님메모 본문에서 `#{도착시간}`으로 사용 가능.
- `clinic_check_out`은 실제 하원 시각과 `checkout_mode`, `participant_<id>_checkout` business object를 사용한다. 승인된 공용 `clinic_info` 봉투에 하원 전용 본문을 담되 `clinic_self_study_completed`의 `completed_at`과 멱등성 키를 공유하지 않는다.
- 2026-05-23 단말 확인: 실제 카카오톡 복붙 본문에는 자유 본문(`#{선생님메모}`)만 보이고 ITEM_LIST 변수는 별도 UI로 표시될 수 있다. 현재 템플릿 본문은 검수 통과한 자유 본문 정책을 유지한다. 기본 본문에 일정 정보를 중복 삽입하려면 기존 테넌트 템플릿 reset/overwrite 정책을 먼저 정해야 한다.

### 일반 강의 출결 수동 알림

출처: `messaging/views_notification.py`, `messaging/notification_dispatch.py`

- `attendance/views.py`의 개별 PATCH, 일괄 출석, 차시 명단 생성은
  `check_in_complete`/`absent_occurred` 발송이나 outbox 생성을 하지 않는다.
- 선생은 출결 화면의 별도 발송 동작으로 대상·수신자·문구를 preview하고,
  생성된 `NotificationPreviewToken`을 confirm해야만 durable outbox가 생긴다.
- preview와 confirm은 같은 테넌트·사용자·수신 범위를 다시 검증하고,
  사용된 token은 재사용할 수 없다.
- 이 경계는 일반 강의에만 적용한다. 클리닉 등원·결석의 자동/수동 정책과
  participant 상태 전이는 별도 계약이다.

---

## 14. MessageTemplate 모델

출처: `models.py:105-179`

### Category choices

DEFAULT, SIGNUP, ATTENDANCE, LECTURE, EXAM, ASSIGNMENT, GRADES, CLINIC, PAYMENT, NOTICE, COMMUNITY, STAFF

### 주요 필드

| 필드 | 설명 |
|------|------|
| `category` | 카테고리 (default/signup/attendance/...) |
| `name` | 템플릿 이름 (120자) |
| `subject` | 제목 (선택, 200자) |
| `body` | 본문 (`#{변수명}` 포함) |
| `solapi_template_id` | Solapi 알림톡 템플릿 ID |
| `solapi_status` | 검수 상태: ""(미신청), PENDING, APPROVED, REJECTED |
| `is_system` | 시스템 기본 양식 여부 (True면 수정/삭제 불가) |
| `is_user_default` | 사용자 지정 기본 양식 (tenant+category당 1개, UniqueConstraint) |

사용자 문구 복제 API는 원본이 이미 복제본이어도 이름의 `복사 -` 접두어를
한 번만 유지하고 120자 제한 안에서 잘라 저장한다. 과거 `(복사본)` 접미사가
중첩된 이름도 새 복제 시 한 번의 `복사 -` 이름으로 정규화한다. 본문·카테고리·
제목은 그대로 복제하며 시스템 문구 자체는 수정하지 않는다.

---

## 15. 코드 수정 시 주의사항

### 이 문서와 동기화해야 하는 코드 파일

| 파일 | 동기화 대상 |
|------|------------|
| `apps/support/messaging/alimtalk_content_builders.py` | 섹션 2, 3, 5 (템플릿 ID, 매핑, 변수) |
| `apps/support/messaging/models.py` | 섹션 4, 14 (Trigger choices, MessageTemplate) |
| `apps/support/messaging/default_templates.py` | 섹션 12 (기본 템플릿) |
| `apps/domains/messaging/policy.py` | 섹션 4, 6, 9 (정책 분류, 공급자, dry-run) |
| `apps/support/messaging/services.py` | 섹션 1, 11 (파이프라인, 폴백) |
| `apps/support/messaging/sqs_queue.py` | 섹션 7, 8 (메시지 구조, 멱등성) |
| `apps/worker/messaging_worker/sqs_main.py` | 섹션 1, 6, 8 (워커, 공급자 분기, 멱등성) |
| `apps/domains/clinic/views/participant_views.py` | 섹션 4, 13 (트리거 호출 위치, on_commit dispatch) |
| `apps/domains/clinic/services/lifecycle.py` | 섹션 4, 8, 13 (상태/완료 전이와 클리닉 이벤트 context) |
| `apps/domains/messaging/views_notification.py` | 섹션 5, 13 (출결 알림 preview→confirm API) |
| `apps/domains/messaging/notification_dispatch.py` | 섹션 5, 8, 13 (수동 발송 대상·멱등 outbox) |
| `apps/domains/attendance/views.py` | 섹션 4, 13 (일반 강의 출결 저장과 발송의 분리 경계) |

### TEMPLATE_TYPE_VARIABLES 변경 시

- Solapi에 등록된 변수와 **정확히 일치**해야 함
- 변수 추가/제거 시 Solapi 템플릿 재검수 필요
- 불일치 시 카카오 에러 3063 (잘못된 파라미터) 발생

### default_templates.py 변경 시

- 이미 프로비저닝된 테넌트의 DB 데이터는 자동 갱신되지 않음
- `ProvisionDefaultTemplatesView` POST로 기존 기본 템플릿 리셋 가능
- 사용자가 편집한 템플릿은 유지됨 (이름이 다르면 별도 생성)

### TRIGGER_TO_TEMPLATE_TYPE 변경 시

- 해당 트리거의 Solapi 템플릿 ID가 올바른지 확인
- `UNIFIED_TEMPLATES_ENABLED = True` 상태에서 미승인 템플릿 ID 사용 시 Solapi 발송 거부

### 새 트리거 추가 시 체크리스트

1. `models.py` AutoSendConfig.Trigger choices에 추가
2. `policy.py` TRIGGER_POLICY에 정책 분류 추가
3. `alimtalk_content_builders.py` TRIGGER_TO_TEMPLATE_TYPE에 매핑 추가
4. `default_templates.py` _TEMPLATE_DEFINITIONS에 기본 템플릿 추가
5. 도메인 코드에서 `send_event_notification()` 호출 추가
6. 이 문서 갱신

---

## 변경 이력

- 2026-03-28: 운영사고 수습 -- 입실 자동 발송 제거, 수동 발송 구조 도입
- 2026-03-28: 전역 AutoSendToggle 제거 (시험/과제/성적 화면)
- 2026-03-28: 비필수 트리거 전면 비활성화
- 2026-03-31: 뿌리오(ppurio) v3 API 전환
- 2026-04-09: SSOT 문서 전면 재작성 -- 코드 기반 16개 섹션 구조화
- 2026-08-29: 일반 강의 개별 출결 PATCH에 재유입된 입실·결석 자동 훅을 제거하고, 메시징 도메인의 출결 알림 preview→confirm만 허용하는 수동 전용 경계를 복구
