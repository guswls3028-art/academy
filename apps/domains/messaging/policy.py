# apps/domains/messaging/policy.py
"""
메시징 발송 정책 및 채널 resolver — 단일 진입점.

SSOT: 실발송은 공용 Solapi 계정의 검증된 알림톡만 사용한다.
- SMS/LMS 실발송 금지.
- 과거 tenant PFID/provider/자체 키 사용 금지.
- 기본은 공용 owner 채널이다. 새 운영자 검증 binding만 worker 마지막 단계에서 사용한다.
- 알림톡 템플릿 fallback 금지. 공용 승인 정본 또는 동일 지문의 tenant 승인본만 사용한다.
"""

import logging

from django.conf import settings

logger = logging.getLogger(__name__)


CLINIC_NOTIFICATION_TRIGGERS = frozenset(
    {
        "clinic_reminder",
        "clinic_reservation_created",
        "clinic_reservation_changed",
        "clinic_cancelled",
        "clinic_check_in",
        "clinic_check_out",
        "clinic_absent",
        "clinic_self_study_completed",
        "clinic_result_notification",
    }
)


# ──────────────────────────────────────────
# 트리거 정책 분류 — SSOT
# ──────────────────────────────────────────
# SYSTEM_AUTO: 시스템 필수 안내. 항상 자동. 사용자가 끌 수 없음.
# AUTO_DEFAULT: 자동 기본값. 사용자가 설정에서 끌 수 있음.
# MANUAL_DEFAULT: 수동 기본값. preview → confirm 필요. 사용자가 자동화 가능.
# DISABLED: 현재 비활성. 정책상 의미 없는 트리거.

TRIGGER_POLICY = {
    # SYSTEM_AUTO — 시스템 필수 안내 메시지
    "registration_approved_student": "SYSTEM_AUTO",
    "registration_approved_parent": "SYSTEM_AUTO",
    "password_find_otp": "SYSTEM_AUTO",
    "password_reset_student": "SYSTEM_AUTO",
    "password_reset_parent": "SYSTEM_AUTO",

    # AUTO_DEFAULT — 학생 행동에 대한 즉시 통보 (클리닉)
    "clinic_reservation_created": "AUTO_DEFAULT",
    "clinic_reservation_changed": "AUTO_DEFAULT",
    "clinic_cancelled": "AUTO_DEFAULT",
    "clinic_check_in": "AUTO_DEFAULT",
    "clinic_check_out": "AUTO_DEFAULT",
    "clinic_absent": "AUTO_DEFAULT",
    "clinic_reminder": "AUTO_DEFAULT",
    "clinic_self_study_completed": "AUTO_DEFAULT",
    "clinic_result_notification": "AUTO_DEFAULT",
    "counseling_reservation_created": "AUTO_DEFAULT",

    # MANUAL_DEFAULT — 선생 검토 필요 (preview → confirm)
    "exam_score_published": "MANUAL_DEFAULT",
    "exam_not_taken": "MANUAL_DEFAULT",
    "retake_assigned": "MANUAL_DEFAULT",
    "assignment_not_submitted": "MANUAL_DEFAULT",
    "assignment_registered": "MANUAL_DEFAULT",
    "assignment_due_hours_before": "MANUAL_DEFAULT",
    "withdrawal_complete": "MANUAL_DEFAULT",
    "check_in_complete": "MANUAL_DEFAULT",
    "absent_occurred": "MANUAL_DEFAULT",
    "monthly_report_generated": "MANUAL_DEFAULT",
    "exam_scheduled_days_before": "MANUAL_DEFAULT",
    "exam_start_minutes_before": "MANUAL_DEFAULT",
    "lecture_session_reminder": "MANUAL_DEFAULT",
    "payment_complete": "MANUAL_DEFAULT",
    "payment_due_days_before": "MANUAL_DEFAULT",
    # urgent_notice: 카카오 알림톡 정책 위반으로 제거

    # 영상 — 인코딩 완료 시 업로더(스태프)에게 알림
    "video_encoding_complete": "AUTO_DEFAULT",

    # 매치업 — 강사가 적중 보고서 학원 제출 시 owner/admin 알림 (기본 OFF)
    "matchup_report_submitted": "AUTO_DEFAULT",

    # 커뮤니티 — 학생/학부모 즉시 통보
    "qna_answered": "AUTO_DEFAULT",
    "counsel_answered": "AUTO_DEFAULT",

    # DISABLED — 현재 정책상 비활성
    "class_enrollment_complete": "DISABLED",
    "enrollment_expiring_soon": "DISABLED",
    "student_signup": "DISABLED",

}


def get_trigger_policy(trigger: str) -> str:
    """트리거의 정책 분류 반환. 미등록 트리거는 DISABLED."""
    return TRIGGER_POLICY.get(trigger, "DISABLED")


TENANT_OPT_IN_AUTO_TRIGGERS: frozenset = frozenset([
    "matchup_report_submitted",
    "qna_answered",
    "counsel_answered",
])


TEMPLATE_READY_OPT_IN_AUTO_TRIGGERS: frozenset = frozenset([
    "video_encoding_complete",
    "matchup_report_submitted",
    "qna_answered",
    "counsel_answered",
])


def requires_tenant_auto_send_opt_in(trigger: str) -> bool:
    """True면 테넌트가 자기 AutoSendConfig row를 명시적으로 켜야 한다."""
    return trigger in TENANT_OPT_IN_AUTO_TRIGGERS


def requires_template_ready_opt_in(trigger: str) -> bool:
    """True면 승인된 알림톡 템플릿 연결 전에는 자동 ON 금지."""
    return trigger in TEMPLATE_READY_OPT_IN_AUTO_TRIGGERS


def is_auto_send_enabled_by_default(trigger: str) -> bool:
    """자동발송 config 생성 시 기본 enabled 값."""
    return (
        get_trigger_implementation_status(trigger) == "implemented"
        and not requires_tenant_auto_send_opt_in(trigger)
        and not requires_template_ready_opt_in(trigger)
    )


# ──────────────────────────────────────────
# 자동 발화 구현 여부 — SSOT (운영자 가시성)
# ──────────────────────────────────────────
# 코드에서 실제 send_event_notification / send_alimtalk_via_owner / 워커 콜백으로
# 발화되는 트리거 목록. 여기 없는 트리거는 AutoSendConfig가 enabled=True여도
# 자동 발송이 일어나지 않음 (수동 발송 모달에서만 동작).
# 새 자동 발화 코드 추가 시 반드시 여기에도 등록할 것.
IMPLEMENTED_AUTO_TRIGGERS: frozenset = frozenset([
    # 가입/등록 (SYSTEM_AUTO)
    "registration_approved_student",
    "registration_approved_parent",
    "password_find_otp",
    "password_reset_student",
    "password_reset_parent",
    # 출결 (즉시 발화)
    "check_in_complete",
    "absent_occurred",
    # 클리닉/상담 (즉시 발화)
    "clinic_reservation_created",
    "clinic_reservation_changed",
    "clinic_cancelled",
    "clinic_check_in",
    "clinic_check_out",
    "clinic_absent",
    "clinic_reminder",  # management command: send_clinic_reminders
    "clinic_self_study_completed",
    "clinic_result_notification",
    # 시험/과제/퇴원/결제 (즉시 발화)
    # exam_score_published 제거 (2026-05-12): 정책 SSOT "저장과 발송은 분리" — 점수 저장은 알림 트리거 아님.
    # 학원장이 "수업결과 발송" 버튼으로 SendMessageModal 또는 manual-notification preview/confirm 통해 명시 발송.
    "withdrawal_complete",
    "payment_complete",
    # assignment_not_submitted: management command exists, but no production schedule.
    # Keep it manual_only until a scheduler is intentionally wired and verified.
    # 영상
    "video_encoding_complete",
    # 매치업 보고서 (강사 → 학원 owner/admin)
    "matchup_report_submitted",
    # 커뮤니티
    "qna_answered",
    "counsel_answered",
])


def get_trigger_implementation_status(trigger: str) -> str:
    """
    트리거의 자동 발화 구현 상태.
    - "implemented": 코드 발화 지점 존재. enabled=True 시 자동 발송 동작.
    - "manual_only": 자동 발화 미구현. 수동 발송 모달에서만 사용 가능.
    - "disabled": 정책상 비활성. 발송 자체 차단.
    """
    if get_trigger_policy(trigger) == "DISABLED":
        return "disabled"
    if trigger in IMPLEMENTED_AUTO_TRIGGERS:
        return "implemented"
    return "manual_only"


# ──────────────────────────────────────────
# 테넌트별 메시징 제한
# ──────────────────────────────────────────
def get_disabled_messaging_tenant_ids() -> frozenset[int]:
    """긴급 장애 확산 방지용 운영 hold 테넌트 ID.

    고객 선호나 온보딩 상태는 이 환경변수에 넣지 않는다. 고객이 직접 바꾸는
    제품 설정은 Tenant.messaging_is_active가 소유한다.
    """
    import os

    ids: set[int] = set()
    raw = os.environ.get("MESSAGING_DISABLED_TENANT_IDS", "").strip()
    if raw:
        for item in raw.split(","):
            try:
                ids.add(int(item.strip()))
            except (TypeError, ValueError):
                logger.warning("Ignoring invalid MESSAGING_DISABLED_TENANT_IDS item=%r", item)
    return frozenset(ids)


# 제한된 테넌트: 가입/등록/비번 관련 알림톡만 발송 가능.
# 이 트리거들은 send_alimtalk_via_owner / send_welcome_messages /
# send_registration_approved_messages에서 OWNER_TENANT_ID로 발송되므로
# enqueue_alimtalk 단에서 tenant_id 기준 차단 시 자동 우회됨.
RESTRICTED_MESSAGING_TENANTS: frozenset = frozenset()


def is_messaging_restricted(tenant_id: int) -> bool:
    """해당 테넌트의 비계정(non-account) 메시징이 제한되어 있는지 여부."""
    return int(tenant_id) in RESTRICTED_MESSAGING_TENANTS


def get_owner_tenant_id() -> int:
    """공용 알림톡 발송/템플릿을 소유하는 tenant ID."""
    return getattr(settings, "OWNER_TENANT_ID", 1)


def get_test_tenant_id() -> int:
    """로컬 기능 테스트용 tenant ID. 이 tenant에서는 알림톡·문자 발송 없이 기능만 동작."""
    return getattr(settings, "TEST_TENANT_ID", 9999)


def is_messaging_disabled(tenant_id: int) -> bool:
    """True면 해당 tenant의 모든 알림톡 enqueue·발송을 스킵한다."""
    tid = int(tenant_id)
    if tid == get_test_tenant_id() or tid in get_disabled_messaging_tenant_ids():
        return True

    # 호출 경로에는 API와 SQS worker가 모두 포함된다. 매 호출마다 DB의 최신
    # 고객 설정을 읽어 별도 프로세스 캐시 때문에 재활성화가 늦어지지 않게 한다.
    try:
        from apps.core.models import Tenant

        active = Tenant.objects.filter(pk=tid).values_list(
            "messaging_is_active", flat=True
        ).first()
    except Exception:
        logger.exception("Failed to read tenant messaging setting tenant_id=%s", tid)
        return True
    # 존재하지 않는 테넌트는 fail-closed다.
    return active is not True


def is_messaging_ops_held(tenant_id: int) -> bool:
    """사용자 설정과 무관한 긴급 운영 hold 여부."""
    return int(tenant_id) in get_disabled_messaging_tenant_ids()


def is_messaging_runtime_held(tenant_id: int) -> bool:
    """공용 채널 런타임 자체를 막는 테스트/긴급 운영 보호 여부."""
    tid = int(tenant_id)
    return tid == get_test_tenant_id() or is_messaging_ops_held(tid)


def get_messaging_disabled_reason(tenant_id: int) -> str:
    """Return a stable user-facing reason when messaging is operationally paused."""
    if not is_messaging_disabled(tenant_id):
        return ""
    if int(tenant_id) == get_test_tenant_id():
        return "테스트 학원에서는 실제 알림톡을 발송하지 않습니다."
    if is_messaging_ops_held(tenant_id):
        return "긴급 장애 확산 방지를 위한 운영 보호가 적용되어 있습니다. 담당자에게 즉시 문의해 주세요."
    return "이 학원의 알림톡 전체 사용이 꺼져 있습니다. 대표 또는 관리자가 다시 켤 수 있습니다."


def get_messaging_test_whitelist() -> frozenset[str]:
    """
    메시징 테스트 모드 수신자 허용 번호.
    MESSAGING_TEST_WHITELIST 환경변수에 콤마 구분 번호가 있으면 해당 번호만 실발송 허용.
    비어 있거나 미설정이면 테스트 모드 비활성 (모든 번호 허용).
    """
    import os
    raw = os.environ.get("MESSAGING_TEST_WHITELIST", "").strip()
    if not raw:
        return frozenset()
    return frozenset(n.replace("-", "").strip() for n in raw.split(",") if n.strip())


def get_messaging_recipient_denylist() -> frozenset[str]:
    """Return normalized recipients that must never reach a provider call."""
    import os

    raw = os.environ.get("MESSAGING_RECIPIENT_DENYLIST", "").strip()
    if not raw:
        return frozenset()
    return frozenset(
        n.replace("-", "").strip()
        for n in raw.split(",")
        if n.replace("-", "").strip()
    )


def check_recipient_allowed(to: str) -> bool:
    """
    수신 번호가 발송 허용 대상인지 확인.
    운영 차단목록은 테스트 whitelist보다 먼저 적용한다.
    테스트 모드(MESSAGING_TEST_WHITELIST 설정됨)에서는 whitelist에 있는 번호만 허용.
    운영 모드(미설정)에서는 모든 번호 허용.

    Returns:
        True if allowed, False if blocked
    """
    normalized = (to or "").replace("-", "").strip()
    denylist = get_messaging_recipient_denylist()
    if normalized in denylist:
        logger.warning(
            "recipient_guard: blocked sending to %s (operational denylist)",
            normalized[:4] + "****",
        )
        return False

    whitelist = get_messaging_test_whitelist()
    if not whitelist:
        return True  # 운영 모드: 모든 번호 허용
    if normalized in whitelist:
        return True
    logger.warning(
        "recipient_guard: blocked sending to %s (not in whitelist, %d entries)",
        normalized[:4] + "****", len(whitelist),
    )
    return False


def is_event_dry_run(trigger: str) -> bool:
    """
    특정 이벤트 트리거가 dry-run 모드인지 확인.
    dry-run이면 로그만 남기고 실제 발송하지 않음.

    환경변수 MESSAGING_DRY_RUN_TRIGGERS:
    - "*" : 모든 이벤트 트리거 dry-run (가입/비번 제외)
    - "check_in_complete,absent_occurred" : 특정 트리거만 dry-run
    - 비어있으면 dry-run 없음 (운영 모드)

    가입 안내/비밀번호 관련 트리거는 dry-run 대상에서 제외:
    registration_approved_*, password_find_otp, password_reset_*
    """
    import os
    raw = os.environ.get("MESSAGING_DRY_RUN_TRIGGERS", "").strip()
    if not raw:
        return False

    # 가입/비밀번호/결제·인증성 관련은 항상 실발송 (dry-run 제외)
    ALWAYS_LIVE_TRIGGERS = frozenset([
        "registration_approved_student",
        "registration_approved_parent",
        "password_find_otp",
        "password_reset_student",
        "password_reset_parent",
        "payment_complete",
        "payment_failed",
        "billing_card_registered",
        "billing_card_failed",
    ])
    if trigger in ALWAYS_LIVE_TRIGGERS:
        return False

    if raw == "*":
        return True
    dry_triggers = frozenset(t.strip() for t in raw.split(",") if t.strip())
    return trigger in dry_triggers


class MessagingPolicyError(Exception):
    """메시징 정책 위반."""
    def __init__(self, message: str, reason: str = "policy"):
        super().__init__(message)
        self.reason = reason


def get_message_mode_block_reason(raw_mode: str | None) -> str:
    """Return a terminal reason for explicit non-Alimtalk delivery modes."""
    mode = (raw_mode or "alimtalk").strip().lower()
    if mode == "alimtalk":
        return ""
    if mode in {"sms", "lms"}:
        return "sms_disabled"
    return "unsupported_message_mode"


def resolve_kakao_channel(tenant_id: int) -> dict:
    """
    알림톡 발송 시 사용할 카카오 채널(PF ID) 결정.

    SSOT: tenant별 kakao_pfid는 실발송에 사용하지 않는다.
    모든 알림톡은 시스템 기본 PFID만 사용한다.
    """
    default_pf_id = (getattr(settings, "SOLAPI_KAKAO_PF_ID", None) or "").strip()
    return {"pf_id": default_pf_id or "", "use_default": True}


def send_alimtalk_via_owner(
    trigger: str,
    to: str,
    replacements: dict[str, str],
    *,
    source_tenant_id: int | None = None,
    log_target_type: str = "account",
    log_target_id: int | str | None = None,
    log_target_name: str = "",
    origin_type: str = "system_account",
    origin_id: str | None = None,
) -> bool:
    """
    오너 테넌트의 승인된 알림톡 템플릿으로 발송.
    모든 테넌트에서 학생 인증 관련 알림톡은 이 함수를 사용.
    SMS fallback / 템플릿 fallback / tenant별 채널 fallback 없음.

    Args:
        trigger: AutoSendConfig trigger (예: "password_find_otp")
        to: 수신자 전화번호 (01012345678)
        replacements: 템플릿 치환 맵 (예: {"인증번호": "123456"})

    Returns:
        True if enqueue 성공
    """
    from apps.domains.messaging.models import ScheduledNotification
    from apps.domains.messaging.scheduled import dispatch_notification_now
    from apps.domains.messaging.selectors import get_auto_send_config

    owner_id = get_owner_tenant_id()

    # 공용 owner는 채널 인프라 소유자다. owner 학원의 고객용 off 설정이 다른
    # 학원의 발송까지 전역 중지시키면 안 되며, 테스트/긴급 hold만 공유한다.
    if is_messaging_runtime_held(owner_id):
        logger.info("send_alimtalk_via_owner: owner channel runtime held")
        return True  # 테스트 환경에서는 성공 간주
    business_tenant_id = int(source_tenant_id or owner_id)
    if is_messaging_disabled(business_tenant_id):
        logger.info(
            "send_alimtalk_via_owner: messaging disabled for business tenant=%s",
            business_tenant_id,
        )
        return False

    config = get_auto_send_config(owner_id, trigger)
    t = config.template if config else None
    solapi_id = (t.solapi_template_id or "").strip() if t else ""

    if (
        not t
        or int(getattr(t, "tenant_id", owner_id)) != int(owner_id)
        or not solapi_id
        or t.solapi_status != "APPROVED"
    ):
        logger.error(
            "send_alimtalk_via_owner: no exact approved owner template trigger=%s owner=%s",
            trigger, owner_id,
        )
        return False

    # 본문 치환 — text와 variables 모두 정확히 일치해야 카카오 검증 통과
    text = (t.body or "").strip()
    alimtalk_replacements = []
    for key, value in replacements.items():
        placeholder = f"#{{{key}}}"
        text = text.replace(placeholder, str(value))
        alimtalk_replacements.append({"key": key, "value": str(value)})

    target_name = (
        replacements.get("학생이름")
        or replacements.get("학부모이름")
        or replacements.get("이름")
        or ""
    )
    target_id = (
        replacements.get("학생아이디")
        or replacements.get("학부모아이디")
        or replacements.get("아이디")
        or ""
    )

    try:
        resolved_target_id = (
            log_target_id if log_target_id is not None else target_id
        )
        notification = dispatch_notification_now(
            tenant_id=business_tenant_id,
            trigger=trigger,
            payload={
                "tenant_id": owner_id,
                "to": to,
                "text": text,
                "message_mode": "alimtalk",
                "template_id": solapi_id,
                "alimtalk_replacements": alimtalk_replacements,
                "event_type": trigger,
                "target_type": log_target_type or "account",
                "target_id": resolved_target_id,
                "target_name": log_target_name or target_name,
                "source_tenant_id": source_tenant_id,
                "origin_type": str(origin_type or "system_account")[:64],
                "origin_id": str(
                    resolved_target_id if origin_id is None else origin_id
                )[:128],
            },
        )
        return notification.status in {
            ScheduledNotification.Status.PENDING,
            ScheduledNotification.Status.SENT,
        }
    except Exception as exc:
        logger.error("send_alimtalk_via_owner: outbox failed trigger=%s error=%s", trigger, exc)
        return False
