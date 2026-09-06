# 메시징/알림톡 운영 정책 SSOT (2026-09-05 갱신)

## 정책 분류 체계

코드 SSOT: `apps/domains/messaging/policy.py` → `TRIGGER_POLICY` dict

### SYSTEM_AUTO — 시스템 필수 안내 (항상 자동, 사용자가 끌 수 없음)
| Trigger | 설명 | 수신자 | 발송 순간 |
|---------|------|--------|----------|
| registration_approved_student | 가입/계정 아이디 안내(학생) | 학생(학생 번호 없으면 학부모) | 신규 학생의 첫 ACTIVE 수강 확정, 학생 아이디 변경, 학생 전화번호 최초 등록 시 |
| registration_approved_parent | 가입/계정 안내(학부모) | 학부모 | 신규 학생의 첫 ACTIVE 수강 확정, 학부모 전화번호 변경/계정 연결 시 |
| password_find_otp | 비밀번호 찾기 OTP (legacy compatibility) | 요청자 | legacy OTP 요청 시 |
| password_reset_student | 비밀번호 변경/재설정(학생) | 학생(학생 번호 없으면 학부모) | 관리자/선생님/본인 비밀번호 변경 또는 재설정 시 |
| password_reset_parent | 비밀번호 변경/재설정(학부모) | 학부모 | 관리자/선생님/본인 비밀번호 변경 또는 재설정 시 |

### AUTO_DEFAULT — 학생 행동 즉시 통보 (자동 기본 on, 선생이 설정에서 끌 수 있음)
| Trigger | 설명 | 수신자 | 발송 순간 |
|---------|------|--------|----------|
| clinic_reservation_created | 클리닉 예약 완료 | 학생이 직접 예약하면 학생+학부모 | 예약 생성(booked/pending) 시 |
| clinic_reservation_changed | 클리닉 예약 변경 | 학생이 직접 변경하면 학생+학부모 | 예약 변경 시 |
| clinic_cancelled | 클리닉 예약 취소 | 학생이 직접 취소하면 학생+학부모 | 상태 → cancelled |
| clinic_check_in | 클리닉 등원/지각 등원 | 직원 선택(학생/학부모/둘 다) | 상태 → attended, 실제 `checked_in_at` 기록 시 |
| clinic_check_out | 클리닉 하원 | 직원 선택(학생/학부모/둘 다) | 정상 등원 또는 명시 확인한 미등원 하원의 별도 `checked_out_at` 기록 시. 승인된 공용 `clinic_info` 봉투에 하원 전용 본문을 담아 요청 |
| clinic_absent | 클리닉 결석 | 직원 선택(학생/학부모/둘 다) | 확인 팝업 승인 후 상태 → no_show, 1회만 요청 |
| clinic_reminder | 클리닉 시작 N분 전 또는 스태프 재촉 | 자동은 학생, 수동은 직원 선택(학생/학부모/둘 다) | EventBridge `academy-v1-send-clinic-reminders` → `send_clinic_reminders`; 수동은 1회 또는 사용자 간격 반복, 같은 날 최대 22:00. 명시적 재클릭은 매번 독립 occurrence |
| clinic_self_study_completed | 클리닉 자율학습 완료 | 학부모 | 자율학습 완료(`completed_at`) 시. 하원과 별도 이벤트 |
| clinic_result_notification | 클리닉 결과 알림 | 학부모 | 클리닉 결과 확정 시 |
| counseling_reservation_created | 상담 예약 완료 | 학부모 | 상담 예약 시 |
| video_encoding_complete | 영상 인코딩 완료 | 스태프(업로더) | 인코딩 완료 시 |

### MANUAL_DEFAULT — 선생 검토 필요 (수동 기본, preview→confirm 또는 설정에서 자동화 가능)
| Trigger | 설명 | 수신자 | 발송 순간 |
|---------|------|--------|----------|
| exam_score_published | 성적 공개 | 학부모 | 선생이 수동 발송 |
| exam_not_taken | 시험 미응시 | 학부모 | 선생이 수동 발송 |
| retake_assigned | 재시험 배정 | 학부모 | 선생이 수동 발송 |
| assignment_not_submitted | 과제 미제출 | 학부모 | 선생이 수동 발송. 배치 명령은 있으나 운영 스케줄 미등록이므로 자동발화는 `manual_only` |
| assignment_registered | 과제 등록 알림 | 학부모 | 선생이 수동 발송 |
| assignment_due_hours_before | 과제 마감 N시간 전 | 학부모 | 스케줄러 미구현, `manual_only` |
| withdrawal_complete | 퇴원 안내 | 학부모 | 선생이 수동 발송 |
| check_in_complete | 일반 강의 입실 | 학부모 | 선생이 출결 알림 preview→confirm으로 수동 발송. 출결 상태 저장은 발송하지 않음 |
| absent_occurred | 일반 강의 결석 | 학부모 | 선생이 출결 알림 preview→confirm으로 수동 발송. 출결 상태 저장은 발송하지 않음 |
| monthly_report_generated | 월간 리포트 생성 | 학부모 | 선생이 수동 발송 |
| exam_scheduled_days_before | 시험 D-N 리마인더 | 학부모 | 스케줄러 미구현, `manual_only` |
| exam_start_minutes_before | 시험 시작 N분 전 | 학부모 | 스케줄러 미구현, `manual_only` |
| lecture_session_reminder | 수업 리마인더 | 학부모 | 스케줄러 미구현, `manual_only` |
| payment_complete | 결제 완료 | 학부모 | 결제 확정 시 |
| payment_due_days_before | 결제 예정 D-N | 학부모 | 스케줄러 미구현, `manual_only` |

### DISABLED — 비활성 (정책상 의미 없는 트리거)
| Trigger | 사유 |
|---------|------|
| class_enrollment_complete | 수강등록=행정작업, 알림 의미 없음 |
| enrollment_expiring_soon | 미구현 |
| student_signup | 레거시 |

## 핵심 원칙
1. **저장과 발송은 분리.** 학생 마스터 생성·가입 승인만으로는 발송하지 않는다. 신규 학생의 첫 `Enrollment.status=ACTIVE` 확정은 실제 수업 참여 확정이므로 계정 안내를 발송한다.
2. **SYSTEM_AUTO 외에는 사용자가 투명하게 보고 테넌트별로 통제 가능.** 한 학원의 선택을 다른 학원이나 공통 기본값에 복사하지 않는다.
3. **일반 강의와 클리닉 정책 절대 분리.**
4. **숨겨진 자동 발송 금지.** 모든 발송 경로가 설정 콘솔에 노출.
5. **검증 채널 알림톡 only.** 제품·고객·운영 경로 모두 SMS/LMS를 실발송하지 않는다. 공급자와 인증 키는 공용 Solapi만 사용한다. 기본은 공용 owner 채널이며, 운영자가 공급자 실조회로 새 `AlimtalkChannelBinding`을 만든 tenant만 본문 지문이 같은 승인 템플릿 매핑을 통해 자기 카카오 채널을 사용한다. 과거 `Tenant.kakao_pfid`, tenant provider, 자체 API 키는 읽지 않는다. 운영 오류 알림은 Slack webhook만 사용한다.
6. **fallback 금지.** exact trigger의 공용 승인 템플릿 또는 명시 unified category 템플릿이 없으면 발송하지 않는다.
7. **비알림톡 입력 실패 폐쇄.** SMS/LMS와 알 수 없는 `message_mode`를 알림톡으로 보정하지 않는다. 신규 코드에는 SMS 발송·enqueue 호환 callable이나 `sms_allowed` capability를 만들지 않는다.
8. **클리닉 하원과 학습 완료 분리.** `clinic_check_out`은 `checked_out_at`과 `checkout_mode`, `clinic_self_study_completed`는 `completed_at`을 소유한다. `arrival_not_recorded`는 등원 상태/시각을 만들지 않는다. 하원은 승인된 공용 `clinic_info` 봉투에 하원 전용 본문·실제 시각을 담고, 다른 trigger나 SMS/LMS로 대체하지 않는다.
9. **성적 알림 수신자는 발송자가 선택하며 미확정 상태를 추정하지 않는다.** `grades` 수동 발송과 `exam_score_published`·`monthly_report_generated` 미리보기는 `send_to=parent`와 `send_to=student`를 모두 허용한다. 화면에서는 학생과 보호자를 독립적으로 선택할 수 있고, 둘 다 선택하면 각 수신자 경로를 별도로 사전검사한 뒤 발송한다. 점수가 `null`이면 교사가 `NOT_SUBMITTED`를 명시한 경우에만 미응시·미제출로 표시하고, 그 외 미입력 항목이 하나라도 있으면 성적 발송 진입점에서 실패 폐쇄한다. 미입력을 0점·불합격·보충 필요로 변환하지 않는다. `grades` 수동 발송은 서버가 해석한 전체 수신 학생 ID와 `alimtalk_extra_vars_per_student`의 키가 정확히 일치하고 각 학생의 비어 있지 않은 `_body_subst`가 있을 때만 허용한다. 누락·초과·잘못된 키나 본문이 있으면 공용 `raw_body` 또는 첫 학생의 전역 성적값으로 대체하지 않고 미리보기와 confirm을 모두 차단한다.

## 클리닉 일정 알림 활성화

학생의 예약 생성·일정 변경·예약 취소 세 이벤트만 일괄 활성화할 때는 먼저
`python manage.py activate_clinic_schedule_notifications`로 변경 계획을 확인하고,
동일 명령에 `--apply`를 붙여 적용한다. 이 명령은 `Tenant.is_active=True`이면서
`Tenant.messaging_is_active=True`인 테넌트만 대상으로 아래 세 config만 생성하거나
ON 한다.

- `clinic_reservation_created`
- `clinic_reservation_changed`
- `clinic_cancelled`

다른 `AutoSendConfig`, 학원 전체 메시징 스위치, 메시지 본문과 발송 시점은 변경하지
않는다. 기존 템플릿이 연결되어 있으면 그대로 보존하고, 누락된 경우에만 해당
트리거의 기본 클리닉 템플릿을 생성·연결한다. 대상 config 중 비알림톡 모드가 한
건이라도 있거나 공용 승인 알림톡 매핑이 없으면 전체 적용을 실패 폐쇄한다. 설정
활성화 자체는 메시지를 발송하거나 과거 이벤트를 재발송하지 않는다.

일반 강의의 개별·일괄 출결 상태 저장과 차시 명단 생성은 알림 발송과 분리한다.
`PRESENT`/`ABSENT` 전환은 `check_in_complete`/`absent_occurred` outbox를 만들지
않으며, 일반 강의 입실·결석 안내는 메시징 도메인의 출결 알림
preview→confirm 경로에서 선생이 명시적으로 확인한 경우에만 생성한다.

## 검증 채널 라우팅 정책

- 모든 알림톡 큐 payload는 계속 `OWNER_TENANT_ID`와 공용 template ID로 정규화하며, 원 업무 테넌트는 서명된 `source_tenant_id`로 보존한다.
- worker는 공용 template 허용 검증을 먼저 끝낸 뒤 provider 호출 직전에만 `AlimtalkChannelBinding`을 조회한다. binding이 `active`이고 같은 공용 template ID의 `AlimtalkTemplateBinding`이 `APPROVED`이며 source/channel 본문 지문이 같을 때만 tenant PFID/template ID로 치환한다.
- 검수 중(`pending_templates`)에는 공용 owner 채널을 계속 사용한다. 활성 binding의 템플릿 매핑이 없거나 지문이 달라졌으면 API/UI에 `suspended`로 투영하고 공용으로 조용히 되돌리지 않고 fail-closed한다.
- tenant별 AutoSendConfig는 enabled/delay/본문 메모 등 업무 설정으로만 사용한다. 과거 `Tenant.kakao_pfid`, `messaging_provider`, 자체 Solapi/Ppurio 키와 MessageTemplate 승인 흔적은 새 binding의 출처가 될 수 없다.
- 공급자·API 키·발신번호는 공용 Solapi 설정만 사용하며 tenant별 credential/provider 분기는 복구하지 않는다.
- `send_alimtalk_via_owner()`는 `OWNER_TENANT_ID`의 exact trigger AutoSendConfig에 연결된 APPROVED 템플릿만 사용한다.
- `password_reset_*` 또는 `password_find_otp`가 `registration_approved_*` 템플릿으로 대체되는 fallback은 금지한다.
- 2026-07-08 Solapi 실등록 감사 기준 `notice_payment` SID는 provider에 없으므로 결제 트리거는 논리 매핑을 유지하되 fail-closed다.
- Community/Q&A 외부 알림톡은 owner의 exact `qna_answered` 고정 문구 템플릿만 사용한다. 학생 이름과 사이트 링크 외 자유문구를 넣지 않으며, provider와 DB가 모두 `APPROVED`가 아니면 발송하지 않는다. 자유양식·출석·성적 봉투로 fallback하지 않고 기존 답변도 소급 발송하지 않는다.

## 안전장치 체계
1. **Tenant.messaging_is_active** — 대표·관리자가 화면에서 직접 제어하는 학원 전체 on/off. 신규·기존 사용 중 학원은 기본 on이며 개인 고객의 선호를 코드나 운영 환경변수에 넣지 않는다.
2. **AutoSendConfig.enabled** — 트리거별 DB on/off (설정 콘솔에서 제어)
3. **TRIGGER_POLICY** — 코드 레벨 정책 분류 (SYSTEM_AUTO는 토글 비활성화)
4. **is_event_dry_run()** — MESSAGING_DRY_RUN_TRIGGERS 환경변수로 dry-run
5. **check_recipient_allowed()** — `MESSAGING_RECIPIENT_DENYLIST`의 운영 차단번호를 우선 거부하고, 테스트 환경에서는 `MESSAGING_TEST_WHITELIST`로 추가 제한한다. API enqueue와 워커 소비 입구에서 검사하며 공용 Solapi 호출 직전에도 다시 검사한다.
6. **NotificationPreviewToken** — preview→confirm 핸드셰이크 (1회용, 5분 TTL). confirm 성공 즉시 수신자/본문을 비우며, 1분 주기 `process_scheduled_notifications`가 만료 행을 회당 500건 정리한다. 수동 대량 정리는 `python manage.py purge_expired_notification_preview_tokens [--dry-run]`을 사용한다.
7. **멱등성 키** — business_idempotency_key (trigger + student_id + 날짜)
8. **일반 강의 출결 수동 발송 경계** — 출결 상태 PATCH·일괄 출석·차시 명단 생성은 자동 outbox를 만들지 않는다. 입실·결석 안내는 `NotificationPreviewToken`을 사용하는 출결 알림 preview→confirm 경로만 허용한다.
9. **계정 알림 event metadata** — `registration_approved_*`, `password_*` 발송은 큐 payload에 원 trigger를 `event_type`으로 싣는다. `NotificationLog.message_body` 보안 마스킹과 운영 추적은 이 값에 의존한다.
   신규 학생 계정 생성 시 초기 안내값은 암호화해 대기시키고, 첫 ACTIVE 수강 확정 후 계정 안내 outbox가 모두 확보되면 즉시 제거한다. 학생/학부모 계정 안내, 아이디 변경, 비밀번호 변경, 학생 전화번호 최초 등록은 SYSTEM_AUTO이며 legacy `send_welcome_message`/`skip_notify` 입력으로 끄지 않는다.
   `registration_approved_student|parent`의 첫 수강 계정 안내는 legacy `AutoSendConfig.enabled` 값으로 끌 수 없다. 공용 owner의 exact APPROVED 템플릿이 없을 때만 fail-closed하며, 학생과 학부모의 유효 수신번호가 다르면 각 계정 안내 outbox를 모두 확보해야 한다.
10. **DB dispatch/outbox** — 수동·시스템·영상·매치업·커뮤니티의 즉시 발송과 `AutoSendConfig.delay_mode` 예약 발송은 모두 `ScheduledNotification`을 먼저 저장한다. product producer의 `enqueue_alimtalk()` 직접 호출은 금지한다. `dispatch_key`에서 안정 occurrence key를 만들고 `pending → dispatching → sent(SQS 접수)`로 전이한다.
11. **SQS enqueue 복구** — transient enqueue 실패는 30초 지수 백오프, 최대 8회 재시도한다. `dispatching` 5분 stale claim도 같은 dispatch key로 회수한다. 입력 오류와 일반 알림의 업무 tenant 전체 발송 중지는 SQS 호출·재시도 없이 즉시 terminal `failed`로 전이해 payload를 제거하며, transient 재시도 소진도 terminal `failed`다. 첫 수강 계정 안내 2종은 운영 중지 중 15분 간격 `pending`으로 보류해 일회용 비밀번호를 잃지 않는다.
12. **provider 호출 경계** — 워커는 공급사 호출 전에 `NotificationLog.status=sending`을 영속화한다. `sending` 이후 crash/중복 SQS는 공급사를 다시 호출하지 않는다. 같은 SQS 메시지 재전달은 `sending→ambiguous`로 원자 승격하며 차감액을 유지한다. timeout처럼 접수 여부가 불명확한 결과와 함께 operations의 `action_required`로 운영 확인한다.
13. **provider 결과/크레딧 추적** — 성공 응답 group/message id는 `provider_message_id`에 저장한다. 크레딧 예약/롤백은 NotificationLog와 함께 멱등 처리하며 `ambiguous`는 자동 환불하지 않는다.
13-A. **접수와 최종 전달 분리** — `NotificationLog.status=sent`와 provider ID는 공급사 접수 근거다. 권한 있는 교직원이 상세에서 명시적으로 최종 상태를 확인할 때만 저장된 group ID로 Solapi 메시지 목록을 read-only 조회한다. provider `2000`은 접수, `3000`은 통신사 결과 대기, 모든 메시지의 `4000`만 최종 전달 성공이며 다른 terminal 코드는 실패다. 조회 오류는 접수 상태를 전달 성공으로 승격하지 않는다. provider 호출 경계 뒤 응답을 받지 못해 ID가 없는 `ambiguous`는 저장된 원문 오류의 종류와 관계없이 사용자 projection을 `provider_unconfirmed`로 유지한다. 조회 식별자가 실제로 없으므로 최종 상태는 추측하지 않고 확인 불가로 표시하며, 자동 재발송·환불·상태 강제변경 없이 operations의 전체 미확정 수에 남긴다.
14. **outbox 개인정보 보존 최소화** — `ScheduledNotification.payload` 원문은 재시도 가능한 `pending/dispatching` 동안에만 보존한다. `sent/cancelled/terminal failed` 전이 시 수신번호, 본문, 치환값, 이름을 제거하고 전달 식별 메타데이터만 남긴다. 비-object legacy payload는 포렌식 원형을 보존한 채 terminal failed로 격리한다.
15. **계정 target key 무전화번호 원칙** — 학생/학부모 계정 알림의 `target_id`는 `student:{student_id}`, `parent:{student_id}`, `parent-account:{parent_id}`만 사용한다. 저장 어댑터와 API는 legacy `parent:{student_id}:{phone}` suffix를 제거한다.
16. **첫 수강 계정 안내 멱등성** — 변경 배포 후 생성된 학생만 암호화된 pending 안내를 가진다. 학생-only 등록·가입 승인·학생 Excel 등록은 발송하지 않는다. 수강 bulk 등록, 수강 Excel 등록, `PENDING|INACTIVE → ACTIVE` 전이에서 커밋 후 pending 안내를 확인하며, 계정 target 기반 outbox가 이미 있으면 재사용한다. 모든 유효 수신자 outbox 확보 전에는 암호문을 유지하고 동일 수강 요청으로 재시도한다. 기존 학생과 두 번째 이후 수강은 pending 값이 없으므로 발송하지 않는다.
17. **업무 tenant 긴급 중지** — `MESSAGING_DISABLED_TENANT_IDS`는 확인된 오발송·중복 확산 같은 긴급 사고에만 원 업무 tenant 기준으로 API enqueue와 워커 소비를 모두 중단한다. 일반 알림은 terminal 처리하지만 `registration_approved_student|parent` outbox는 암호화된 업무 payload를 `pending`으로 유지하고, 이미 SQS에 들어간 동일 이벤트는 12시간 visibility 연장으로 보류한다. 해제 전에는 outbox·SQS·DLQ·provider pending과 연락처 정본을 확인한다. 화면은 이 운영 hold를 고객 설정과 구분해 표시하며 고객 토글이 이를 덮어쓰지 못한다.
18. **공용 owner와 고객 설정 분리** — 공용 owner tenant는 승인 채널 인프라의 소유자일 뿐 고객별 전체 사용 설정의 전역 기준이 아니다. owner 학원이 자기 알림톡을 꺼도 다른 업무 tenant의 발송·계정 복구는 계속되며, owner 경계에서 전역으로 공유되는 차단은 테스트 tenant와 긴급 운영 hold뿐이다.
19. **공급자 계정 일일 브레이크** — 시간당 tenant 한도와 별개로 모든 업무 tenant가 공유하는 공급자 계정에 KST 날짜 기준 `MESSAGING_PROVIDER_DAILY_DISPATCH_LIMIT`(기본 900) 한도를 적용한다. `ScheduledNotification.last_attempt_at` 예약과 outbox가 없는 legacy `NotificationLog`를 중복 없이 합산하며, 한도에 도달한 신규 outbox는 실패/폐기하지 않고 다음 날 00:05 KST로 이월한다. PostgreSQL transaction advisory lock이 tenant 간 동시 claim을 직렬화하며 owner tenant 행의 존재 여부에는 의존하지 않는다. 공급자가 `QuotaExceeded` 또는 `NotEnoughBalance`로 접수 전 거절하면 provider ID·차감이 없는 확정 실패로 닫고 `ambiguous`로 남기지 않는다. 자동 재발송은 하지 않는다.
20. **개인정보 없는 incident trace** — outbox와 worker log는 원문 번호 대신 `MESSAGING_TENANT_BINDING_KEY` HMAC `recipient_fingerprint`를 저장하고, `origin_type`/`origin_id`로 Excel job·수동 batch·domain object를 연결한다. terminal payload에는 이 비식별 메타데이터와 기존 dispatch/business key만 남긴다. 키 순환 중 조회는 fallback key 지문도 함께 계산한다.
20-A. **tenant 채널 등록·검수·테스트** — `configure_tenant_alimtalk_channel`은 dry-run 기본이며 tenant code를 정확히 1건으로 확정하고 공용 Solapi 계정에서 새 channel ID가 실제 조회될 때만 새 binding을 기록한다. 현재 공용 승인 템플릿의 본문·버튼·강조 구조를 복제하고 별도 검수를 요청한다. legacy `TE` 카테고리는 현재 Kakao 6자리 분류(가입, 인증, 예약, 서비스 신청, 피드백)로 의미에 따라 명시 변환하며, 검수 분류값과 provider의 빈 응답 기본값은 전달 동작 지문에서 제외한다. 모두 `APPROVED`이고 전달 동작 지문이 같을 때만 `--activate-if-ready`로 활성화한다. `send_tenant_alimtalk_channel_test`는 binding에 기록된 승인 `인증번호` 템플릿을 live로 다시 조회하고 denylist/whitelist와 idempotency key를 확인한 뒤 정확히 1건만 보낸다. 테스트도 `disable_sms=True`, PII-free recipient fingerprint, `NotificationLog.status`와 provider ID를 남기며 raw 번호·본문·provider ID를 출력하지 않는다. provider adapter 로그의 channel ID도 마지막 네 자리만 남긴다.
21. **Excel 계정 안내 provenance** — Excel로 신규 학생을 만든 job ID는 암호화 pending 계정 안내와 함께 저장한다. 첫 ACTIVE 수강에서 `origin_type=excel_import`, `origin_id=<AIJob job_id>`를 학생/학부모 outbox로 전달하고, 모든 유효 outbox 확보 뒤 비밀번호 암호문과 provenance를 함께 제거한다.
22. **canonical payload 무결성** — 신규 SQS payload는 `occurrence_key`를 명시하고 worker가 수신자·event·target·template을 다시 조합한 business key와 producer key가 같은지 확인한다. signed key를 복사한 뒤 수신자 등을 바꾼 payload는 `invalid_business_idempotency_key`로 공급자 호출 전에 폐기한다.
23. **공급자 잔액/재시도 감시** — 5분 주기 `check_dev_alerts`는 사용자 오류와 함께 최근 30분 `NotEnoughBalance` 확정 거절·미확정 건 및 Solapi 공용 잔액을 검사한다. 잔액이 `MESSAGING_PROVIDER_LOW_BALANCE_ALERT_THRESHOLD`(기본 10,000원) 미만이거나 잔액 조회가 실패하면 개인정보 없이 Slack으로 경고한다. 이 운영 경고도 SMS/LMS를 사용하지 않는다.
24. **2026-08-22 첫 수강 계정 안내 복구** — `repair_failed_first_enrollment_notices`는 tenant 11과 reviewed student allowlist `3656,4102,4103,4104,4105`만 받는 incident 전용 dry-run 기본 명령이다. 적용 직전 공용 PFID·발신번호·live 승인 template SID/body, exact 학생·학부모 placeholder envelope 9건, provider 잔액과 main queue·DLQ 0을 개인정보 없이 transaction 밖에서 먼저 읽는다. 실제 apply transaction은 `SET LOCAL lock_timeout='5s'`를 방어 설정한 뒤 pending reset → Student → ScheduledNotification → NotificationLog 순서로 `SHARE ROW EXCLUSIVE NOWAIT` table lock을 먼저 얻고, 그 뒤에만 tenant·학생·모든 parent linkage(비활성·soft-deleted·cross-tenant 포함)·학부모·계정·수강·기존 outbox/log·owner template을 `NO KEY UPDATE NOWAIT`로 한 번 authoritative 조회한다. 선행 DML 또는 row lock이 있으면 대기·교착 없이 `recovery_quiescence_unavailable`로 전체 중단하며 자동 재시도하지 않고, 후발 DML은 복구 transaction 뒤에 직렬화된다. external main/DLQ 0 확인 뒤 apply lock 안에서 committed `ScheduledNotification.status=dispatching` 전역 0을 DB-only로 다시 확인하며, claim이 있으면 같은 operator error로 중단한다. lock 내부에는 DB authoritative 검증·DML·`on_commit` 등록만 두고 SQS/provider network 호출은 하지 않는다. SQS readback client는 transaction 밖 preflight에서 요청당 2초·SDK 재시도 0으로 제한하며, commit 뒤 queue/provider 완료는 별도 운영 readback에서 확인한다. 3656의 broad first-enrollment history 집합은 `registration_approved_student|parent` trigger의 outbox `{1174,1654,1759}`와 log `{4570,5060,5145}`가 정확히 일치해야 하며 해당 trigger의 추가·누락 행은 모두 fail-closed다. 같은 target의 별도 `password_reset_*` history는 이 incident 집합에서 제외해 불변 보존하지만, first-enrollment trigger 범위의 pending·later sent·provider acceptance·cross-tenant 이력은 계속 차단한다. `1174↔4570`은 dispatch `e3b6c52e-1890-4ee9-b549-60d789a8507b`, business `f1645e709a33ffa71c1687743eccf169774a583f02fd1995f06736c434788a69`, blank origin, exact `provider_quota_exceeded_not_accepted` failed/no-provider/차감 0 이력이다. 이 exact 1174 reviewed pair에서 legacy payload의 blank origin 두 key가 없을 때만 missing을 blank로 정규화하며, 명시적 빈 문자열 외의 falsey 값은 거절한다. row origin과 nonblank 1654/1759 payload origin은 계속 exact key/value를 요구한다. `1654↔5060`은 dispatch `3055120a-c519-487e-b4ac-20b8057bc588`, business `6403f10f32e0633115ffd041b1e188822abb4c7bdded5b8ab66277dcfb40bcbb`, `system_account/student:3656`, 운영 DB에 기록된 exact literal backslash+n `NotEnoughBalance` ambiguous/no-provider/차감 0 이력이며 실제 LF로 정규화하지 않는다. `1759↔5145`는 dispatch `707ce6d8-756d-4a1f-86ff-1c5eb26811de`, business `ac83900afd8620f05e14a4d37fa33054367d63446bfd9a9e4564707dd051e4b0`, `credential_incident/godmin-20260822`, sent/success/provider-present/차감 0 canonical 학부모 이력이다. 세 쌍 모두 row tenant·trigger·status·error·keys·origin, payload source/event/target/mode/origin, log owner/source/type/target/mode/status/result/keys/origin이 exact여야 하며 history를 범용 상태나 느슨한 개수 조건으로 인정하지 않는다. 4102–4105의 failed pair outbox도 row tenant, source/event/target/mode/origin, dispatch/business key와 exact failed reason을 모두 만족해야 하며 잘못된 tenant 행도 target 기준 broad 조회에서 숨기지 않는다. generic ambiguous/processing/sending/비정형 provider evidence는 거절한다. 공유 학부모 계정과 canonical 학부모 성공 이력은 불변이고, 4102–4105는 parent를 가리키는 다른 Student row가 하나도 없을 때만 pair 회전한다. locked 계정은 실제 렌더 ID와 일치하고 usable password·active·미로그인·token version 2·pending 0이어야 한다. exact 결과는 회전 9계정과 `origin_type=recovery`, Alimtalk-only outbox 9건이다. 후보·템플릿·수신자·공유관계 drift, 안내 생성 false 또는 예상 수 불일치는 outer transaction 전체를 rollback하고 `on_commit` callback/SQS enqueue는 0이어야 한다. 비밀번호·전화번호·사용자명은 출력하지 않고 기존 failed/ambiguous/sent 이력은 수정·삭제하지 않는다.
25. **공용 발신번호 런타임 동치** — `/academy/api/env`와 `/academy/workers/env`의 `SOLAPI_API_KEY`, `SOLAPI_API_SECRET`, `SOLAPI_SENDER`는 서로 exact여야 하며 sender는 같은 자격으로 조회한 공급자의 유일한 ACTIVE 번호여야 한다. `reconcile_common_alimtalk_sender.py`는 번호를 입력·출력하지 않는 dry-run 기본 운영 도구다. apply는 clean latest main과 성공 manifest를 요구하고 shared production lock을 얻은 뒤 두 SSM 문서의 sender 한 key만 메모리에서 변경한다. Messaging과 API만 launch-before-terminate로 갱신하고 모든 InService 컨테이너의 sender를 일회성 HMAC으로 확인하며, main queue·DLQ 0과 API health까지 확인한 뒤에만 lock을 반환한다. refresh 전 부분 실패는 원문 파일 백업 없이 메모리의 exact SSM 값으로 rollback하고, refresh 시작 뒤 실패는 inactive 값으로 되돌리지 않고 lock을 유지해 forward-converge한다.
26. **공용 owner 런타임 명시성** — 코드의 호환 기본값은 owner tenant 1이지만 production `/academy/api/env`와 `/academy/workers/env`에는 `OWNER_TENANT_ID`가 같은 고정 JSON 문자열로 명시되어야 한다. 숫자·null·문자열 강제 변환은 명시 상태로 인정하지 않는다. `reconcile_common_alimtalk_owner_tenant.py`는 owner 값을 입력·출력하지 않는 dry-run 기본 운영 도구이며 absent 또는 exact 기본값만 허용한다. apply는 shared production lock 아래 누락된 owner key만 worker→API 순서로 추가하고 SOLAPI sender를 포함한 다른 key/value를 exact 보존한다. 이미 SSM이 명시돼도 InService runtime HMAC, API health, main queue·DLQ 0을 확인하며 runtime 값이 stale 또는 missing이면 같은 Messaging→API terminal refresh로 forward-converge한다. SSM version/raw/KMS와 최종 lock ownership까지 확인한 뒤에만 lock을 반환한다. refresh 전 부분 실패는 lock ownership을 다시 증명한 뒤 exact 원문으로 rollback하고, write 뒤 lock loss에서는 unowned 보상 write를 하지 않는다. refresh·readback·concurrency·rollback 불확실성은 lock을 유지한다. 이 도구는 sender-only 정합화 도구를 대체하거나 확장하지 않는다.
27. **클리닉 T-30 중복 방지** — `clinic_reminder`는 enabled tenant의 `minutes_before`(기본 30분) 시점에 `booked` 학생만 대상으로 한다. 고정시간 방식은 기존 세션 시작시각과 `clinic_session:<session_id>:reminder` origin+학생 target을 보존한다. 시간범위 방식은 실제 예약 시작시각과 `clinic_booking:<participant>:<session>:<date>:<start>` origin을 사용한다. EventBridge가 5분 보정 창에서 매분 실행되어도 이미 durable `ScheduledNotification`에 있는 occurrence는 다시 만들지 않으며, 시간범위 보정은 기존 세션 기준 이력을 재생하지 않는다. 범위 예약은 dispatcher와 공급자 호출 직전에도 취소·날짜·시각·tenant를 재검증한다. 이 검증은 기존 명시 참관 사본에도 적용한다. 공급사 잔액의 확정 미접수 거절은 audited retry에서도 같은 business key와 단일 `NotificationLog`만 사용하며, provider id는 접수 증거이고 최종 전달은 13-A의 별도 상태 확인을 따른다. 수동 `clinic.manual_reminder`는 별도 occurrence이므로 자동 T-30 dedup과 섞이지 않는다. 상세 시간범위 계약은 [클리닉 예약](../domain/clinic-booking.md#자동-시작-리마인더)을 따른다.
28. **클리닉 수동 재촉의 예약 생명주기 결속** — 예약 취소·거절·일정 변경과 세션 삭제는 같은 DB transaction에서 이전 participant의 미발송 수동 `clinic_reminder`를 `cancelled`로 끝내고 `next_attempt_at`과 수신 payload를 제거한다. dispatcher도 claim 직전에 origin의 tenant/participant가 여전히 활성 학생의 `booked` 예약인지 확인하므로, 이미 due가 된 stale 행도 외부 발송하지 않는다. 합법적인 새 예약에는 새 participant occurrence를 사용하며 이전 occurrence를 재개하지 않는다.
28. **테넌트별 참관 수신자** — 명시적으로 설정된 `MessagingObserver`는 해당 업무 tenant에서 새로 생성되는 비계정 알림톡의 동일 본문·승인 봉투 사본을 별도 outbox로 받는다. durable outbox의 `trigger` 또는 payload `event_type`이 `registration_approved_student`, `registration_approved_parent`, `password_find_otp`, `password_reset_student`, `password_reset_parent` 중 하나이면 아이디·비밀번호·OTP를 포함할 수 있으므로 참관 사본을 무조건 만들지 않는다. 두 값이 어긋나는 legacy/malformed producer도 중앙 `is_sensitive_notification()` 판정으로 fail-closed한다. 원 수신자 outbox와 UI 접수 건수는 바꾸지 않고, 허용된 사본은 `target_type=messaging_observer`, `origin_type=messaging_observer`, `origin_id=outbox:<원본 ID>`로 추적한다. 원 수신번호와 같은 참관 번호 및 참관자 간 중복 번호는 한 번만 발송한다. 발송 순간에도 활성 사용자, 활성 `owner/admin/staff` tenant membership, `010` 11자리 전화번호를 모두 요구한다. 즉시 발송의 참관 사본은 원본과 같은 commit 후 처리 묶음에 포함한다. 참관 사본에는 학생 이름·출결 등 개인정보가 포함될 수 있으므로 owner의 명시적 승인 아래 `python manage.py set_messaging_observers --tenant-id <id> --user-id <id> ... --apply --ack-sensitive-content`로만 전체 집합을 교체한다. 중단은 `--clear --apply`로 즉시 적용하며 기존 로그와 outbox는 감사 이력으로 보존한다.

## 운영 검증

- 배포 후 실발송 검증은 `pwsh scripts/v1/run-messaging-verify-send.ps1 -AwsProfile default`만 사용한다.
- 이 스크립트는 API 인스턴스에서 `messaging_verify_common_alimtalk`을 실행하며, 수신번호는 통제번호 `01031217466` 하나만 허용한다.
- 검증 트리거는 owner exact approved template(`password_reset_student` 기본)을 사용한다. SMS/LMS, tenant별 PFID/provider, 템플릿 fallback을 쓰지 않는다.
- 성공 판정은 SQS enqueue가 아니라 워커가 만든 `NotificationLog.status=sent`, `message_mode=alimtalk`, `tenant_id=OWNER_TENANT_ID`, `provider_message_id` 기록까지다.
- 제품 메시징 사고는 `python manage.py diagnose_messaging_incident --tenant-id <id> --recipient <번호> [--origin-id <job-id>] [--since-hours 72] [--provider]`로 조회한다. 출력은 상태/트리거/연결 건수와 공급자 type/status 집계만 포함하고 번호·본문·비밀번호·provider ID·입력한 origin ID를 출력하지 않는다.
- 잔액 충전/자동충전 뒤 audited recovery가 기존 이력을 보존하며 `sent`와 provider id까지 닫혔는지 확인한다. `ambiguous`는 접수 여부가 불명확하므로 자동 재발송하지 않고 공급자 대사 후 수동 조치한다.

## 변경 이력
- 2026-09-03: 하원 알림을 승인된 공용 `clinic_info` 봉투로 복구하고, 기존 테넌트에 하원 전용 본문/config를 기본 ON으로 프로비저닝했다. 정상 하원과 명시 확인한 미등원 하원 모두 직원이 수신자를 선택하며, 중복 하원은 재발송하지 않는다.
- 2026-08-29: 일반 강의 개별 출결 PATCH에 재유입된 입실·결석 자동 훅을 제거하고, 출결 알림 preview→confirm 수동 발송만 허용하는 회귀 계약을 복구
- 2026-08-23: 참관 사본에서 계정 아이디·비밀번호·OTP를 포함할 수 있는 5개 계정 트리거를 fail-closed로 제외했다. durable trigger와 payload event type을 중앙 민감도 판정으로 함께 검사해 legacy mismatch도 차단하며, 기존 감사 로그와 outbox는 보존한다.
- 2026-08-23: tenant membership으로 재검증되는 명시적 참관 수신자를 추가했다. 참관 사본은 원본과 분리된 outbox/provider 로그로 추적하며 동일 번호를 중복 발송하지 않는다.
- 2026-08-23: API와 Messaging worker의 공용 Solapi 설정이 서로 같아도 공급자의 유일 ACTIVE sender와 다르면 발송 전 fail-closed하도록 운영 정합화 경계를 추가했다. sender-only 교정은 shared lock, 두 SSM exact 보존, API+Messaging 제한 refresh, HMAC runtime readback을 모두 통과해야 한다.
- 2026-08-22: 숨은 테넌트별 코드 차단을 제거하고 대표·관리자가 직접 바꾸는 전체 알림톡 설정을 발송 정책에 연결했다. 환경변수 hold는 긴급 사고 전용으로 분리해 화면에 명시하며, 공급자 quota/잔액 접수 전 거절은 확정 실패로 종결한다.
- 2026-08-22: Solapi `NotEnoughBalance` 확정 거부를 미확정 결과와 분리하고, 5분 주기 저잔액/거절 Slack 경보를 추가했다.
- 2026-08-22: 운영 중지 중 첫 수강 계정 안내는 terminal 삭제하지 않고 outbox/SQS에 보류한다. 과거 유실 복구는 tenant 11의 reviewed 5명만 대상으로 공유 학부모 1명 불변, 학생 5+비공유 학부모 4 회전, Alimtalk outbox 9건을 한 transaction에서 보장하는 audited dry-run 명령으로 제한했다.
- 2026-08-23: 클리닉 등원·지각·결석·하원을 분리하고 직원별 수신자 선택, 1회/반복 재촉의 22시 상한, 하원 exact-template fail-closed 계약을 고정했다. `completed_at`은 자율학습 완료로 보존한다.
- 2026-08-22: 클리닉 T-30은 세션+학생별 durable outbox로 반복 scheduler tick을 제거하고, 확정 잔액 거절의 같은 로그 회복과 provider id 완료 증거를 고정했다.
- 2026-08-21: `enqueue_sms`/`send_sms`/provider SMS 호환 callable, `sms_allowed` API 필드, SMS 이름의 계정 알림 throttle을 제거했다. 명시된 비알림톡 `message_mode`는 API·outbox·SQS·worker 경계에서 알림톡으로 보정하지 않고 terminal fail-closed하도록 고정했다. 기존 로그·테넌트 설정 데이터는 이력으로 보존한다.
- 2026-08-20: 플랫폼 운영자 장애 SMS 예외와 활성화 스크립트·워크플로 입력·provider 호출 코드를 제거했다. 운영 오류는 Slack으로만 알리고, 기존 SMS audit action은 이력 조회와 짧은 중복 억제 기간에만 읽는다.
- 2026-08-20: 공유 공급자 KST 일일 900건 기본 브레이크, 수신번호 HMAC 지문, Excel job provenance, canonical business-key 재검증, 개인정보 없는 단일 incident 진단 명령을 추가했다.
- 2026-08-19: 신규 학생 계정 안내 발송 시점을 학생 마스터 생성/가입 승인에서 첫 ACTIVE 수강 확정으로 이동. 초기 안내값은 암호화 보관하고 전체 계정 outbox 확보 후 제거하며, 기존 학생·추가 수강·동일 요청 재시도는 중복 발송하지 않도록 고정.
- 2026-08-19: 잘못 등록된 외부 수신번호와 대량 계정 알림 사고 대응을 위해 운영 수신번호 denylist를 API enqueue·워커 소비·Solapi 호출 직전의 세 경계에 적용하고, tenant 긴급 중지의 재기동/해제 조건을 명시.
- 2026-07-26: 제품 메시징과 분리된 운영자 장애 SMS 단일 예외를 명시. 고정 통제번호에는 플랫폼 발급 테넌트 코드/내부 ID와 통제된 장애 분류/건수만 90-byte 집계로 보내며 owner 수정 테넌트명·사용자 본문·경로·개인정보는 금지. 발송 전 attempt receipt와 접수 직후 group ID를 저장하고, 공급사 미확정은 보존 기간 동안 자동 재발송하지 않는 at-most-once 보류와 실행당 10건 공정 순환 재조회, 확정 실패 5분 cooldown, 시간당 12회 상한으로 중복·폭주를 제한.
- 2026-07-13: 즉시/예약 수동 발송 DB dispatch, SQS enqueue 지수 백오프, stale dispatch 회수, provider `processing→sending→sent|failed|ambiguous` 경계를 도입. 공급사 exactly-once 미지원 구간은 중복 방지를 우선해 자동 재발송하지 않고 operations risk로 노출. `notice_payment` SID 누락은 preflight/send/operations에서 명시적으로 fail-close. 종단 outbox/사용된 preview payload의 PII를 즉시 제거하고 만료 preview token 자동 purge 및 전화번호 없는 계정 target key를 적용.
- 2026-07-08: Solapi provider 실등록 상태와 코드 변수표를 재대조. score ITEM_LIST 등록 변수는 학원이름/학생이름/강의명/차시명/선생님메모/사이트링크 6개로 고정하고, 시험1~4/총점/숙제완성도는 선생님메모 내부 치환 값으로만 사용한다. `notice_payment` SID 누락 상태를 fail-closed로 고정. manual default/community 자유양식 fallback과 Q&A 출석 봉투 fallback을 제거.
- 2026-06-06: SMS/LMS 및 tenant별 알림톡 채널/provider 사용을 금지하고, exact 공용 승인 템플릿 없으면 fail-closed하도록 정책 갱신. 운영 검증 수신번호를 `01031217466`으로 고정하고 provider id 로그를 추가.
- 2026-05-25: `clinic_reminder` 운영 EventBridge 연결. `process_scheduled_notifications` 운영 스케줄 추가. 운영 스케줄이 없는 `assignment_not_submitted`는 자동발화 구현상태에서 제외해 원장 화면 혼선 방지.
- 2026-05-23: 학생 등록 welcome/가입 승인 알림도 `registration_approved_student|parent` event metadata를 큐에 싣도록 정렬. 계정성 알림 로그 마스킹 기준을 문서화.
- 2026-05-21: 공개 로그인 화면 계정복구 SSOT를 `/api/v1/auth/account-recovery/dispatch/`로 정리. `password_find_otp`는 legacy OTP 경로로 명시.
- 2026-04-10: 당시 코드 기준 `clinic_check_out`을 `clinic_self_study_completed`로 통합했으나, 2026-08-23 실제 하원과 학습 완료의 의미 분리로 폐기된 결정이다.
- 2026-03-28: 정책 확정 — 4분류 체계 (SYSTEM_AUTO/AUTO_DEFAULT/MANUAL_DEFAULT/DISABLED)
- 2026-03-28: 클리닉 트리거 세분화 (cancelled, check_in, check_out, absent)
- 2026-03-28: 설정 콘솔 재정렬 (정책 배지, 템플릿 읽기 전용, DISABLED 숨김)
- 2026-03-28: 일반 강의 출결 자동 발송 코드 완전 제거
- 2026-03-28: 행정 화면 AutoSendToggle 전면 제거
