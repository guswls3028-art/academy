# SSOT 문서: backend/docs/domain/messaging.md, backend/docs/domain/messaging-alimtalk.md
"""
Messaging Worker - SQS 기반 검증 채널 알림톡 발송

SQS academy-messaging-jobs 에서 수신 → Solapi 공용 또는 검증 tenant 채널 발송.
SMS/LMS payload는 legacy 호환용 실패 로그만 남기고 실발송하지 않는다.
SQS Long Polling + SIGTERM/SIGINT graceful shutdown 패턴.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import signal
import sys
import time
from typing import Callable, Optional

from libs.queue import get_queue_client, QueueUnavailableError
from libs.redis.idempotency import acquire_job_lock, release_job_lock, RedisLockUnavailableError

from apps.worker.messaging_worker.config import load_config

# 트리거명 → 사용자 친화적 라벨 (발송 내역 표시용)
_TRIGGER_LABELS: dict[str, str] = {
    "check_in_complete": "입실 완료",
    "absent_occurred": "결석 발생",
    "lecture_session_reminder": "수업 시작 알림",
    "exam_score_published": "성적 공개",
    "exam_not_taken": "시험 미응시",
    "retake_assigned": "재시험 대상",
    "exam_scheduled_days_before": "시험 예정 안내",
    "exam_start_minutes_before": "시험 시작 알림",
    "assignment_not_submitted": "과제 미제출",
    "assignment_registered": "과제 등록",
    "assignment_due_hours_before": "과제 마감 알림",
    "monthly_report_generated": "월간 리포트",
    "withdrawal_complete": "퇴원 처리",
    "payment_complete": "결제 완료",
    "payment_due_days_before": "납부 예정 안내",
    "clinic_reminder": "클리닉 시작 알림",
    "clinic_reservation_created": "클리닉 예약",
    "clinic_reservation_changed": "클리닉 변경",
    "clinic_cancelled": "클리닉 취소",
    "clinic_check_in": "클리닉 입실",
    "clinic_check_out": "클리닉 하원",
    "clinic_absent": "클리닉 결석",
    "clinic_self_study_completed": "클리닉 자율학습 완료",
    "clinic_result_notification": "클리닉 결과",
    "counseling_reservation_created": "상담 예약",
    "video_encoding_complete": "영상 인코딩 완료",
    "registration_approved_student": "가입 안내(학생)",
    "registration_approved_parent": "가입 안내(학부모)",
    "password_find_otp": "비밀번호 찾기",
    "password_reset_student": "비밀번호 재설정(학생)",
    "password_reset_parent": "비밀번호 재설정(학부모)",
}


def _safe_payload_shape(payload) -> dict[str, object]:
    """Return non-sensitive metadata for malformed worker payload logs."""
    return {
        "payload_type": type(payload).__name__,
        "keys": (
            sorted(str(key) for key in payload.keys())[:30]
            if isinstance(payload, dict)
            else []
        ),
    }


def _get_template_summary(event_type: str, template_id: str, message_mode: str) -> str:
    """발송 내역에 표시할 사람이 읽을 수 있는 요약."""
    # manual_ 접두어 제거 (수동 발송)
    trigger = event_type.removeprefix("manual_") if event_type else ""
    label = _TRIGGER_LABELS.get(trigger, "")
    if label:
        mode = "알림톡" if message_mode == "alimtalk" else "비지원 채널 차단"
        return f"{label} ({mode})"
    if message_mode == "alimtalk" and template_id:
        return f"알림톡"
    return "비지원 채널 차단"


def _recipient_summary(target_name: str, recipient: str) -> str:
    """Keep the exact recipient phone for tenant-scoped delivery operations."""

    name = str(target_name or "").strip()
    phone = "".join(character for character in str(recipient or "") if character.isdigit())
    return " ".join(part for part in (name, phone) if part)


def _video_encoding_block_reason(event_type: str, message_mode: str, template_id: str) -> str:
    """영상 인코딩 완료는 알림톡 승인 템플릿 없이 발송하지 않는다."""
    if event_type != "video_encoding_complete":
        return ""
    if message_mode != "alimtalk":
        return "video_encoding_complete_non_alimtalk_blocked"
    if not (template_id or "").strip():
        return "video_encoding_complete_template_required"
    return ""

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [MESSAGING-WORKER] %(message)s",
)
logger = logging.getLogger("messaging_worker")

_NON_RETRYABLE_SEND_FAILURES = (
    "clinic_booking_closed",
    "alimtalk_failed_or_rejected",
    "alimtalk_requires_pf_id_and_template_id",
    "InvalidParameterValue",
    "TemplateNotApproved",
    "to_pf_template_required",
    "to_text_sender_required",
    "sender_required",
    "to_and_text_required",
    "solapi_not_installed",
    "sms_disabled",
    "ppurio_not_configured",
    "ppurio_token_failed",
    "ppurio_token_rejected",
    "ppurio_token_client_error",
    "invalid_sender_profile_format",
)


def _normalize_worker_tenants(
    raw_tenant_id,
    raw_source_tenant_id,
    *,
    owner_tenant_id: int,
) -> tuple[int, int | None]:
    """Return (send_tenant_id, source_tenant_id) for common-owner Alimtalk."""
    send_tenant_id = int(raw_tenant_id)
    try:
        source_tenant_id = int(raw_source_tenant_id) if raw_source_tenant_id is not None else None
    except (TypeError, ValueError):
        source_tenant_id = None

    if send_tenant_id != int(owner_tenant_id):
        if source_tenant_id is not None and source_tenant_id != send_tenant_id:
            raise ValueError("legacy_business_tenant_mismatch")
        source_tenant_id = send_tenant_id
        send_tenant_id = int(owner_tenant_id)
    return send_tenant_id, source_tenant_id


def _has_valid_worker_tenant_binding(data: dict) -> bool:
    """Require every canonical queue payload to authenticate tenant billing."""
    from apps.domains.messaging.security import verify_tenant_binding_signature

    try:
        tenant_id = int(data.get("tenant_id"))
        raw_source = data.get("source_tenant_id")
        source_tenant_id = int(raw_source) if raw_source is not None else None
    except (TypeError, ValueError):
        return False
    business_key = str(data.get("business_idempotency_key") or "")
    signature = str(data.get("tenant_binding_signature") or "")
    if not business_key or not signature:
        return False
    return verify_tenant_binding_signature(
        signature=signature,
        tenant_id=tenant_id,
        source_tenant_id=source_tenant_id,
        business_idempotency_key=business_key,
    )


def _worker_tenant_binding_error(data: dict) -> str:
    """Return a terminal binding error, honoring the explicit rollout gate."""
    from django.conf import settings

    signature = str(data.get("tenant_binding_signature") or "")
    if signature:
        if not _has_valid_worker_tenant_binding(data):
            return "invalid_tenant_binding_signature"
        if data.get("occurrence_key"):
            from apps.domains.messaging.sqs_queue import (
                build_business_idempotency_key,
            )

            expected_key = build_business_idempotency_key(
                tenant_id=int(data.get("tenant_id")),
                source_tenant_id=(
                    int(data["source_tenant_id"])
                    if data.get("source_tenant_id") is not None
                    else None
                ),
                channel=str(data.get("message_mode") or "alimtalk").strip().lower(),
                event_type=str(data.get("event_type") or "manual_send"),
                target_type=str(data.get("target_type") or ""),
                target_id=str(data.get("target_id") or ""),
                recipient=str(data.get("to") or ""),
                occurrence_key=str(data.get("occurrence_key") or ""),
                template_id=str(data.get("template_id") or ""),
            )
            if expected_key != str(data.get("business_idempotency_key") or ""):
                return "invalid_business_idempotency_key"
        return ""
    if bool(getattr(settings, "MESSAGING_TENANT_BINDING_ENFORCED", False)):
        return "missing_tenant_binding_signature"
    return ""


def _is_non_retryable_send_failure(reason: str) -> bool:
    return any(item in (reason or "") for item in _NON_RETRYABLE_SEND_FAILURES)


def _assert_clinic_reminder_delivery_active(data: dict, *, business_tenant_id: int) -> None:
    """A queued range reminder cannot outlive its exact active booking."""
    if data.get("event_type") != "clinic_reminder":
        return
    origin = str(data.get("domain_object_id") or data.get("origin_id") or "")
    if not origin.startswith("clinic_booking:"):
        return
    from apps.domains.clinic.contracts import is_clinic_booking_reminder_active

    if not is_clinic_booking_reminder_active(tenant_id=business_tenant_id, origin_id=origin):
        raise ValueError("clinic_booking_closed")


def _resolve_worker_business_key(data: dict, message_id: str | None) -> str:
    """Return the producer key or a deterministic guard for legacy payloads."""
    producer_key = str(data.get("business_idempotency_key") or "").strip()
    if producer_key:
        return producer_key[:64]
    canonical_payload = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    legacy_material = f"legacy:{message_id or ''}:{canonical_payload}"
    return hashlib.sha256(legacy_material.encode("utf-8")).hexdigest()


def _send_failure_disposition(
    reason: str,
    *,
    provider_send_started: bool,
    definitely_not_accepted: bool = False,
    provider_retryable: bool | None = None,
) -> str:
    """Classify failure without retrying an outcome-ambiguous provider call."""
    if definitely_not_accepted:
        return "retry" if provider_retryable else "terminal"
    if _is_non_retryable_send_failure(reason):
        return "terminal"
    if provider_send_started:
        return "ambiguous"
    return "retry"


def _classify_definitely_rejected_provider_exception(exc: Exception) -> str:
    """Return a stable reason only for Solapi failures known to precede acceptance.

    These provider responses contain no message/group id and deduct no amount. Treating
    them as ambiguous strands the log forever even though the provider definitely
    rejected the request. Other exceptions remain ambiguous after the boundary.
    """
    material = " ".join(
        [exc.__class__.__name__, *(str(item) for item in getattr(exc, "args", ())), str(exc)]
    ).replace("_", "").replace(" ", "").lower()
    if "notenoughbalance" in material:
        return "provider_not_enough_balance"
    if "quotaexceeded" in material:
        return "provider_quota_exceeded"
    return ""


_REQUIRED_ACCOUNT_NOTICE_EVENTS = frozenset(
    {"registration_approved_student", "registration_approved_parent"}
)
_DISABLED_ACCOUNT_NOTICE_VISIBILITY_SECONDS = 12 * 60 * 60


def _should_defer_disabled_tenant_message(event_type: str) -> bool:
    return str(event_type or "").strip() in _REQUIRED_ACCOUNT_NOTICE_EVENTS


def _resolve_tenant_delivery_context(
    tenant_id: int,
    source_tenant_id: int | None = None,
    common_template_id: str = "",
) -> dict:
    """Separate billing ownership and resolve the verified delivery channel."""
    from apps.domains.messaging.credit_services import get_tenant_messaging_info
    from apps.domains.messaging.policy import get_owner_tenant_id, resolve_kakao_channel
    from apps.domains.messaging.tenant_channels import resolve_alimtalk_delivery_route

    delivery_tenant_id = int(tenant_id)
    if delivery_tenant_id != int(get_owner_tenant_id()):
        raise RuntimeError("non_owner_delivery_tenant_blocked")
    delivery_info = get_tenant_messaging_info(delivery_tenant_id)
    if not delivery_info or not delivery_info.get("tenant_is_active"):
        raise RuntimeError("provider_tenant_missing_or_inactive")
    billing_tenant_id = int(source_tenant_id or delivery_tenant_id)
    billing_info = (
        delivery_info
        if billing_tenant_id == delivery_tenant_id
        else get_tenant_messaging_info(billing_tenant_id)
    )
    if not billing_info or not billing_info.get("tenant_is_active"):
        raise RuntimeError("business_tenant_missing_or_inactive")

    common_channel = resolve_kakao_channel(delivery_tenant_id)
    if not isinstance(common_channel, dict):
        raise RuntimeError("messaging_channel_resolution_failed")
    route = resolve_alimtalk_delivery_route(
        source_tenant_id=billing_tenant_id,
        common_channel_id=str(common_channel.get("pf_id") or ""),
        common_template_id=common_template_id,
    )
    return {
        "billing_info": billing_info,
        "billing_tenant_id": billing_tenant_id,
        "channel": {
            "pf_id": route.channel_id,
        },
        "template_id": route.template_id,
        "channel_source": route.source,
    }


def _allowed_common_template_ids(event_type: str) -> set[str]:
    """Return the common-channel envelope IDs allowed for this business event."""
    from apps.domains.messaging.alimtalk_content_builders import (
        TEMPLATE_TYPE_TO_SOLAPI_ID,
        get_solapi_template_id,
        get_template_type,
    )

    trigger = (event_type or "").strip()
    unified_ids = {
        str(template_id).strip()
        for template_id in TEMPLATE_TYPE_TO_SOLAPI_ID.values()
        if str(template_id or "").strip()
    }
    if trigger == "manual_send" or trigger.startswith("manual_"):
        return unified_ids

    unified_type = get_template_type(trigger)
    if unified_type:
        template_id = (get_solapi_template_id(trigger) or "").strip()
        return {template_id} if template_id else set()

    from apps.domains.messaging.models import AutoSendConfig
    from apps.domains.messaging.policy import get_owner_tenant_id

    owner_id = int(get_owner_tenant_id())
    config = (
        AutoSendConfig.objects.select_related("template")
        .filter(tenant_id=owner_id, trigger=trigger)
        .first()
    )
    template = config.template if config else None
    if (
        not template
        or int(template.tenant_id) != owner_id
        or template.solapi_status != "APPROVED"
    ):
        return set()
    template_id = (template.solapi_template_id or "").strip()
    return {template_id} if template_id else set()


# 메시지 발송 구간별 진행률 (n/4): 업로드 마법사처럼 단계별 0~100% 제공
MESSAGING_STEP_TOTAL = 4
MESSAGING_STEPS = [
    (1, "checking", "예약확인"),
    (2, "validating", "잔액확인"),
    (3, "sending", "발송"),
    (4, "done", "완료"),
]
_STEP_DISPLAY = {step_name: display for _, step_name, display in MESSAGING_STEPS}


def _record_progress(
    job_id: str,
    step: str,
    percent: int,
    step_index: int | None = None,
    step_percent: int | None = None,
    tenant_id: str | None = None,  # ✅ 추가: tenant_id 전달
) -> None:
    """Redis 진행률 기록 (우하단 실시간 프로그래스바용). 구간별 진행률 지원."""
    try:
        from academy.adapters.cache.redis_progress_adapter import RedisProgressAdapter
        extra = {"percent": percent}
        if step_index is not None:
            extra.update({
                "step_index": step_index,
                "step_total": MESSAGING_STEP_TOTAL,
                "step_name": step,
                "step_name_display": _STEP_DISPLAY.get(step, step),
                "step_percent": step_percent if step_percent is not None else 100,
            })
        # ✅ tenant_id 전달 (tenant namespace 키 사용)
        tenant_id_str = str(tenant_id) if tenant_id else None
        RedisProgressAdapter().record_progress(job_id, step, extra, tenant_id=tenant_id_str)
    except Exception as e:
        logger.debug("Redis progress record skip: %s", e)


_shutdown = False
_current_receipt_handle: Optional[str] = None


def _handle_signal(sig, frame):
    """SIGTERM/SIGINT: graceful shutdown. In-flight 메시지는 delete하지 않음 → visibility 만료 후 재노출되어 안전 재처리."""
    global _shutdown, _current_receipt_handle
    logger.info(
        "Received signal, initiating graceful shutdown... | current_job=%s",
        "processing" if _current_receipt_handle else "idle",
    )
    _shutdown = True


def _get_solapi_client(cfg):
    """DEBUG=True 또는 SOLAPI_MOCK=true 이면 Mock (로그만), 아니면 실제 Solapi."""
    if os.environ.get("SOLAPI_MOCK", "").lower() in ("true", "1", "yes") or os.environ.get("DEBUG", "").lower() in ("true", "1", "yes"):
        from apps.domains.messaging.solapi_mock import MockSolapiMessageService
        return MockSolapiMessageService(api_key=cfg.SOLAPI_API_KEY, api_secret=cfg.SOLAPI_API_SECRET)
    from solapi import SolapiMessageService
    return SolapiMessageService(api_key=cfg.SOLAPI_API_KEY, api_secret=cfg.SOLAPI_API_SECRET)


def send_one_alimtalk_ppurio(
    to: str,
    sender: str,
    pf_id: str,
    template_id: str,
    replacements: Optional[list] = None,
    *,
    api_key: str = "",
    account: str = "",
    before_provider_call: Optional[Callable[[], bool]] = None,
) -> dict:
    """뿌리오 알림톡 1건 발송. Solapi send_one_alimtalk과 동일 인터페이스."""
    try:
        from apps.domains.messaging.ppurio_client import send_ppurio_alimtalk
        return send_ppurio_alimtalk(
            to=to, sender=sender, pf_id=pf_id,
            template_id=template_id, replacements=replacements,
            api_key=api_key, account=account,
            before_provider_call=before_provider_call,
        )
    except Exception as e:
        logger.exception("ppurio alimtalk failed to=%s****", (to or "")[:4])
        return {
            "status": "error",
            "reason": str(e)[:500],
            "provider_called": False,
        }


def _build_variables_dict(replacements: Optional[list]) -> Optional[dict]:
    """Convert [{"key": "학생이름2", "value": "길동"}, ...] → {"#{학생이름2}": "길동", ...}"""
    if not replacements:
        return None
    variables = {}
    for r in replacements:
        if isinstance(r, dict) and "key" in r and "value" in r:
            key = str(r["key"])
            val = str(r["value"])
            if not key.startswith("#{"):
                key = f"#{{{key}}}"
            variables[key] = val
    return variables if variables else None


def send_one_alimtalk_own_solapi(
    to: str, sender: str, pf_id: str, template_id: str,
    replacements: Optional[list] = None,
    *, api_key: str, api_secret: str, text: str = "",
    before_provider_call: Optional[Callable[[], bool]] = None,
) -> dict:
    """테넌트 자체 솔라피 키로 알림톡 1건 발송."""
    try:
        from solapi.model import RequestMessage
        from solapi.model.kakao.kakao_option import KakaoOption
        from solapi import SolapiMessageService
    except ImportError:
        return {
            "status": "error",
            "reason": "solapi_not_installed",
            "provider_called": False,
        }
    to = (to or "").replace("-", "").strip()
    if not to or not pf_id or not template_id:
        return {
            "status": "error",
            "reason": "to_pf_template_required",
            "provider_called": False,
        }
    provider_called = False
    try:
        client = SolapiMessageService(api_key=api_key, api_secret=api_secret)
        variables = _build_variables_dict(replacements)
        kakao_option = KakaoOption(pf_id=pf_id, template_id=template_id, variables=variables, disable_sms=True)
        message = RequestMessage(from_=sender, to=to, text=text or " ", kakao_options=kakao_option)
        if before_provider_call is not None and not before_provider_call():
            return {
                "status": "error",
                "reason": "provider_boundary_claim_failed",
                "provider_called": False,
            }
        provider_called = True
        response = client.send(message)
        group_id = getattr(getattr(response, "group_info", None), "group_id", None)
        count = getattr(getattr(response, "group_info", None), "count", None)
        if count is not None and getattr(count, "registered_success", 0) == 0:
            return {
                "status": "error",
                "reason": "alimtalk_failed_or_rejected",
                "group_id": group_id,
                "provider_called": True,
                "provider_outcome": "rejected",
                "definitely_not_accepted": True,
                "provider_retryable": False,
            }
        logger.info("send_alimtalk_own ok to=%s**** group_id=%s", to[:4], group_id)
        return {
            "status": "ok",
            "group_id": group_id,
            "provider_called": True,
            "provider_outcome": "accepted",
        }
    except Exception as e:
        logger.warning("alimtalk_own failed to=%s****: %s", to[:4], e)
        rejected_reason = _classify_definitely_rejected_provider_exception(e)
        return {
            "status": "error",
            "reason": rejected_reason or str(e)[:500],
            "provider_called": provider_called,
            **(
                {
                    "provider_outcome": "rejected",
                    "definitely_not_accepted": True,
                    "provider_retryable": False,
                }
                if rejected_reason
                else {}
            ),
        }


def send_one_alimtalk(
    cfg,
    to: str,
    sender: str,
    pf_id: str,
    template_id: str,
    replacements: Optional[list] = None,
    text: str = "",
    before_provider_call: Optional[Callable[[], bool]] = None,
) -> dict:
    """
    Solapi 알림톡 1건 발송.
    replacements: [{"key": "학생이름2", "value": "길동"}, ...] — 템플릿 #{학생이름2}, #{날짜}, #{클리닉명} 등 치환.
    text: Solapi 요청에 필요한 알림톡 본문 필드. SMS fallback은 비활성화된다.
    """
    try:
        from solapi.model import RequestMessage
        from solapi.model.kakao.kakao_option import KakaoOption
    except ImportError:
        return {
            "status": "error",
            "reason": "solapi_not_installed",
            "provider_called": False,
        }
    to = (to or "").replace("-", "").strip()
    if not to or not pf_id or not template_id:
        return {
            "status": "error",
            "reason": "to_pf_template_required",
            "provider_called": False,
        }
    from apps.domains.messaging.policy import check_recipient_allowed

    if not check_recipient_allowed(to):
        return {
            "status": "error",
            "reason": "recipient_blocked_by_policy",
            "provider_called": False,
            "provider_outcome": "rejected",
            "definitely_not_accepted": True,
            "provider_retryable": False,
        }
    provider_called = False
    try:
        client = _get_solapi_client(cfg)
        variables = _build_variables_dict(replacements)
        kakao_option = KakaoOption(pf_id=pf_id, template_id=template_id, variables=variables, disable_sms=True)
        message = RequestMessage(
            from_=sender,
            to=to,
            text=text or " ",
            kakao_options=kakao_option,
        )
        if before_provider_call is not None and not before_provider_call():
            return {
                "status": "error",
                "reason": "provider_boundary_claim_failed",
                "provider_called": False,
            }
        # The queue payload may have been accepted before an operations block
        # was added. Recheck immediately before crossing the provider boundary.
        if not check_recipient_allowed(to):
            return {
                "status": "error",
                "reason": "recipient_blocked_by_policy",
                "provider_called": False,
                "provider_outcome": "rejected",
                "definitely_not_accepted": True,
                "provider_retryable": False,
            }
        provider_called = True
        response = client.send(message)
        group_id = getattr(getattr(response, "group_info", None), "group_id", None)
        count = getattr(getattr(response, "group_info", None), "count", None)
        if count is not None and getattr(count, "registered_success", 0) == 0:
            reason = "alimtalk_failed_or_rejected"
            logger.warning("alimtalk no success to=%s****", to[:4])
            return {
                "status": "error",
                "reason": reason,
                "group_id": group_id,
                "provider_called": True,
                "provider_outcome": "rejected",
                "definitely_not_accepted": True,
                "provider_retryable": False,
            }
        logger.info("send_alimtalk ok to=%s**** group_id=%s", to[:4], group_id)
        return {
            "status": "ok",
            "group_id": group_id,
            "provider_called": True,
            "provider_outcome": "accepted",
        }
    except Exception as e:
        logger.warning("alimtalk failed to=%s****: %s", to[:4], e)
        rejected_reason = _classify_definitely_rejected_provider_exception(e)
        return {
            "status": "error",
            "reason": rejected_reason or str(e)[:500],
            "provider_called": provider_called,
            **(
                {
                    "provider_outcome": "rejected",
                    "definitely_not_accepted": True,
                    "provider_retryable": False,
                }
                if rejected_reason
                else {}
            ),
        }


def main() -> int:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Django context: 예약/유저 등 DB 조회가 필요할 때 ORM 사용 가능하도록
    if os.environ.get("DJANGO_SETTINGS_MODULE"):
        import django
        from django.apps import apps as django_apps

        if not django_apps.ready:
            django.setup()
        logger.info("Django setup done (ORM available)")

        # DB 연결 검증 (startup validation): 연결 불가 시 즉시 종료
        try:
            from django.db import connection as db_conn
            db_conn.ensure_connection()
            logger.info("DB connection validated at startup")
        except Exception as e:
            logger.error("DB connection failed at startup: %s. Exiting.", e)
            return 1

    cfg = load_config()
    queue_client = get_queue_client()

    # Long Polling 10~20초: 빈 큐에 반복 요청 방지 → AWS 비용·CPU 절약
    logger.info(
        "Messaging Worker started | queue=%s | wait_time=%ss",
        cfg.MESSAGING_SQS_QUEUE_NAME,
        cfg.SQS_WAIT_TIME_SECONDS,
    )

    consecutive_errors = 0
    max_consecutive_errors = 10

    try:
        while not _shutdown:
            try:
                # Heartbeat — 매 polling cycle 1회. 실패는 silent (워커 차단 X).
                try:
                    from apps.shared.utils.heartbeat import beat as _beat
                    _beat("messaging")
                except Exception:
                    pass
                try:
                    raw = queue_client.receive_message(
                        queue_name=cfg.MESSAGING_SQS_QUEUE_NAME,
                        wait_time_seconds=cfg.SQS_WAIT_TIME_SECONDS,
                    )
                except QueueUnavailableError as e:
                    logger.warning(
                        "SQS unavailable (AWS credentials invalid or missing?). Waiting 60s. %s",
                        e,
                    )
                    time.sleep(60)
                    continue
                if not raw:
                    continue

                body = raw.get("Body", "")
                receipt_handle = raw.get("ReceiptHandle")
                message_id = raw.get("MessageId") or receipt_handle
                if not receipt_handle:
                    logger.error("Message missing ReceiptHandle")
                    continue

                job_id = f"messaging:{message_id}"
                try:
                    lock_acquired = acquire_job_lock(job_id)
                except RedisLockUnavailableError:
                    # Redis 장애: 메시지를 SQS에 남겨 재시도 (fail-closed)
                    logger.warning("Redis unavailable, leaving message in SQS for retry job_id=%s", job_id)
                    time.sleep(10)  # back-off to avoid tight loop during Redis outage
                    continue
                if not lock_acquired:
                    # 중복 메시지: 다른 워커가 처리 중일 수 있으므로 즉시 삭제하지 않고 재시도 기회 유지
                    logger.info("Duplicate in-flight message detected, keep for retry job_id=%s", job_id)
                    continue

                global _current_receipt_handle
                _msg_deleted = False
                try:
                    if isinstance(body, str):
                        try:
                            data = json.loads(body)
                        except json.JSONDecodeError:
                            logger.error("Invalid JSON in message body")
                            queue_client.delete_message(
                                queue_name=cfg.MESSAGING_SQS_QUEUE_NAME,
                                receipt_handle=receipt_handle,
                            )
                            _msg_deleted = True
                            continue
                    else:
                        data = body

                    if not isinstance(data, dict) or "to" not in data or "text" not in data:
                        payload_shape = _safe_payload_shape(data)
                        logger.error(
                            "Invalid message format: message_id=%s payload_type=%s keys=%s",
                            message_id,
                            payload_shape["payload_type"],
                            payload_shape["keys"],
                        )
                        queue_client.delete_message(
                            queue_name=cfg.MESSAGING_SQS_QUEUE_NAME,
                            receipt_handle=receipt_handle,
                        )
                        _msg_deleted = True
                        continue

                    tenant_id = data.get("tenant_id")
                    if tenant_id is None:
                        logger.error(
                            "Message missing tenant_id (required for tenant isolation), "
                            "deleting message_id=%s",
                            message_id,
                        )
                        queue_client.delete_message(
                            queue_name=cfg.MESSAGING_SQS_QUEUE_NAME,
                            receipt_handle=receipt_handle,
                        )
                        _msg_deleted = True
                        continue

                    try:
                        raw_tenant_id_int = int(tenant_id)
                    except (TypeError, ValueError):
                        logger.error(
                            "Message has invalid tenant_id=%r, deleting message_id=%s",
                            tenant_id,
                            message_id,
                        )
                        queue_client.delete_message(
                            queue_name=cfg.MESSAGING_SQS_QUEUE_NAME,
                            receipt_handle=receipt_handle,
                        )
                        _msg_deleted = True
                        continue

                    # 로컬 기능 테스트용 tenant(9999): 발송·차감 없이 스킵, 메시지 삭제 후 진행
                    if raw_tenant_id_int == cfg.TEST_TENANT_ID:
                        logger.info(
                            "Message skipped: tenant_id=%s is test tenant (messaging disabled)",
                            raw_tenant_id_int,
                        )
                        queue_client.delete_message(
                            queue_name=cfg.MESSAGING_SQS_QUEUE_NAME,
                            receipt_handle=receipt_handle,
                        )
                        _msg_deleted = True
                        continue

                    raw_tenant_id = raw_tenant_id_int
                    source_tenant_id_raw = data.get("source_tenant_id")
                    tenant_binding_error = _worker_tenant_binding_error(data)
                    if tenant_binding_error:
                        logger.error(
                            "Message has invalid or missing tenant binding signature; deleting message_id=%s reason=%s",
                            message_id,
                            tenant_binding_error,
                        )
                        if os.environ.get("DJANGO_SETTINGS_MODULE"):
                            try:
                                from decimal import Decimal
                                from academy.adapters.db.django.repositories_messaging import create_notification_log
                                from apps.domains.messaging.policy import get_owner_tenant_id

                                create_notification_log(
                                    tenant_id=int(get_owner_tenant_id()),
                                    success=False,
                                    amount_deducted=Decimal("0"),
                                    recipient_summary="차단된 큐 페이로드",
                                    failure_reason=tenant_binding_error,
                                    message_body="",
                                    message_mode="alimtalk",
                                    sqs_message_id=message_id,
                                    notification_type=str(data.get("event_type") or "queue_security"),
                                )
                            except Exception as exc:
                                logger.warning("tenant binding failure log creation failed: %s", exc)
                        queue_client.delete_message(
                            queue_name=cfg.MESSAGING_SQS_QUEUE_NAME,
                            receipt_handle=receipt_handle,
                        )
                        _msg_deleted = True
                        continue
                    if not data.get("tenant_binding_signature"):
                        logger.warning(
                            "Unsigned legacy messaging payload accepted during rollout message_id=%s",
                            message_id,
                        )
                    try:
                        tenant_id, source_tenant_id_msg = _normalize_worker_tenants(
                            raw_tenant_id,
                            source_tenant_id_raw,
                            owner_tenant_id=cfg.OWNER_TENANT_ID,
                        )
                    except (TypeError, ValueError) as exc:
                        logger.error(
                            "Message has contradictory tenant binding; deleting message_id=%s reason=%s",
                            message_id,
                            exc,
                        )
                        queue_client.delete_message(
                            queue_name=cfg.MESSAGING_SQS_QUEUE_NAME,
                            receipt_handle=receipt_handle,
                        )
                        _msg_deleted = True
                        continue
                    business_tenant_id = source_tenant_id_msg or tenant_id
                    if source_tenant_id_msg == cfg.TEST_TENANT_ID:
                        logger.info(
                            "Message skipped: source_tenant_id=%s is test tenant (messaging disabled)",
                            source_tenant_id_msg,
                        )
                        queue_client.delete_message(
                            queue_name=cfg.MESSAGING_SQS_QUEUE_NAME,
                            receipt_handle=receipt_handle,
                        )
                        _msg_deleted = True
                        continue
                    try:
                        from apps.domains.messaging.policy import is_messaging_disabled

                        if is_messaging_disabled(business_tenant_id):
                            event_type_for_policy = str(
                                data.get("event_type") or ""
                            ).strip()
                            logger.info(
                                "Message skipped: business_tenant_id=%s messaging disabled",
                                business_tenant_id,
                            )
                            if _should_defer_disabled_tenant_message(
                                event_type_for_policy
                            ):
                                queue_client.change_message_visibility(
                                    queue_name=cfg.MESSAGING_SQS_QUEUE_NAME,
                                    receipt_handle=receipt_handle,
                                    visibility_timeout=(
                                        _DISABLED_ACCOUNT_NOTICE_VISIBILITY_SECONDS
                                    ),
                                )
                                continue
                            queue_client.delete_message(
                                queue_name=cfg.MESSAGING_SQS_QUEUE_NAME,
                                receipt_handle=receipt_handle,
                            )
                            _msg_deleted = True
                            continue
                    except Exception as exc:
                        logger.warning(
                            "Messaging disabled policy check failed for business_tenant_id=%s; keeping message for retry: %s",
                            business_tenant_id,
                            exc,
                        )
                        continue
                    if int(raw_tenant_id) != int(tenant_id):
                        logger.warning(
                            "Messaging worker normalized tenant_id=%s to owner=%s source_tenant_id=%s",
                            raw_tenant_id,
                            tenant_id,
                            source_tenant_id_msg,
                        )

                    # 예약 취소 Double Check: 발송 직전 한 번 더 확인
                    reservation_id = data.get("reservation_id")
                    tenant_id_str = str(tenant_id) if tenant_id else None
                    if reservation_id is not None and os.environ.get("DJANGO_SETTINGS_MODULE"):
                        _record_progress(job_id, "checking", 10, step_index=1, step_percent=100, tenant_id=tenant_id_str)
                        try:
                            from apps.domains.messaging.services import is_reservation_cancelled
                            if is_reservation_cancelled(int(reservation_id), tenant_id=business_tenant_id):
                                logger.info("reservation_id=%s cancelled, skip send", reservation_id)
                                queue_client.delete_message(
                                    queue_name=cfg.MESSAGING_SQS_QUEUE_NAME,
                                    receipt_handle=receipt_handle,
                                )
                                _msg_deleted = True
                                _current_receipt_handle = None
                                continue
                        except Exception as e:
                            logger.warning("reservation check failed: %s", e)
                    else:
                        _record_progress(job_id, "checking", 10, step_index=1, step_percent=100, tenant_id=tenant_id_str)

                    _current_receipt_handle = receipt_handle

                    to = str(data.get("to", "")).replace("-", "").strip()
                    text = str(data.get("text", ""))
                    from apps.domains.messaging.policy import check_recipient_allowed
                    from apps.domains.messaging.security import (
                        build_recipient_fingerprint,
                    )

                    recipient_fingerprint_msg = build_recipient_fingerprint(to)
                    origin_type_msg = str(
                        data.get("origin_type")
                        or data.get("source_use_case")
                        or data.get("source_domain")
                        or ""
                    ).strip()[:64]
                    origin_id_msg = str(
                        data.get("origin_id")
                        or data.get("domain_object_id")
                        or ""
                    ).strip()[:128]

                    if not check_recipient_allowed(to):
                        logger.warning(
                            "Message skipped: recipient=%s rejected by policy",
                            to[:4] + "****" if to else "missing",
                        )
                        if os.environ.get("DJANGO_SETTINGS_MODULE"):
                            try:
                                from decimal import Decimal
                                from academy.adapters.db.django.repositories_messaging import (
                                    create_notification_log,
                                )

                                create_notification_log(
                                    tenant_id=int(tenant_id),
                                    success=False,
                                    amount_deducted=Decimal("0"),
                                    recipient_summary="차단된 수신자",
                                    failure_reason="recipient_blocked_by_policy",
                                    message_body="",
                                    message_mode=str(
                                        data.get("message_mode") or "alimtalk"
                                    ),
                                    sqs_message_id=message_id,
                                    notification_type=str(
                                        data.get("event_type") or ""
                                    ),
                                    source_tenant_id=source_tenant_id_msg,
                                    target_type=str(data.get("target_type") or ""),
                                    target_id=str(data.get("target_id") or ""),
                                    target_name=str(data.get("target_name") or ""),
                                    business_idempotency_key=_resolve_worker_business_key(
                                        data,
                                        message_id,
                                    ),
                                    recipient_fingerprint=recipient_fingerprint_msg,
                                    origin_type=origin_type_msg,
                                    origin_id=origin_id_msg,
                                )
                            except Exception as exc:
                                logger.warning(
                                    "recipient policy block log creation failed: %s",
                                    exc,
                                )
                        queue_client.delete_message(
                            queue_name=cfg.MESSAGING_SQS_QUEUE_NAME,
                            receipt_handle=receipt_handle,
                        )
                        _msg_deleted = True
                        _current_receipt_handle = None
                        continue
                    # 공용 채널 계약: 큐 payload의 발신번호는 신뢰하지 않는다.
                    sender = ""
                    target_name = (data.get("target_name") or "").strip()
                    target_type_msg = (data.get("target_type") or "").strip()
                    target_id_msg = str(data.get("target_id") or "").strip()
                    message_mode = (
                        data.get("message_mode") or "alimtalk"
                    ).strip().lower()
                    alimtalk_replacements = data.get("alimtalk_replacements") or []
                    template_id_msg = data.get("template_id") or ""
                    event_type_msg = (data.get("event_type") or "").strip()[:30]
                    video_encoding_block_reason = _video_encoding_block_reason(
                        event_type_msg,
                        message_mode,
                        str(template_id_msg or ""),
                    )
                    template_id_normalized = str(template_id_msg or "").strip()
                    template_policy_block_reason = ""
                    if os.environ.get("DJANGO_SETTINGS_MODULE"):
                        allowed_template_ids = _allowed_common_template_ids(event_type_msg)
                        if template_id_normalized not in allowed_template_ids:
                            template_policy_block_reason = "common_template_not_allowed"
                    channel_policy_block_reason = ""
                    if message_mode != "alimtalk":
                        from apps.domains.messaging.policy import (
                            get_message_mode_block_reason,
                        )
                        channel_policy_block_reason = get_message_mode_block_reason(
                            message_mode
                        )
                    policy_block_reason = (
                        channel_policy_block_reason
                        or video_encoding_block_reason
                        or template_policy_block_reason
                    )
                    if policy_block_reason:
                        logger.error(
                            "Messaging worker blocked delivery: tenant=%s mode=%s reason=%s",
                            tenant_id,
                            message_mode,
                            policy_block_reason,
                        )
                        if os.environ.get("DJANGO_SETTINGS_MODULE"):
                            try:
                                from decimal import Decimal
                                from academy.adapters.db.django.repositories_messaging import create_notification_log

                                create_notification_log(
                                    tenant_id=int(tenant_id),
                                    success=False,
                                    amount_deducted=Decimal("0"),
                                    recipient_summary=_recipient_summary(target_name, to),
                                    failure_reason=policy_block_reason,
                                    message_body=text[:2000],
                                    message_mode=message_mode,
                                    sqs_message_id=message_id,
                                    notification_type=event_type_msg,
                                    source_tenant_id=source_tenant_id_msg,
                                    target_type=target_type_msg,
                                    target_id=target_id_msg,
                                    target_name=target_name,
                                )
                            except Exception as e:
                                logger.warning("create_notification_log failed: %s", e)
                        queue_client.delete_message(
                            queue_name=cfg.MESSAGING_SQS_QUEUE_NAME,
                            receipt_handle=receipt_handle,
                        )
                        _msg_deleted = True
                        _current_receipt_handle = None
                        continue
                    source_domain_msg = (data.get("source_domain") or "").strip()[:50]
                    source_use_case_msg = (data.get("source_use_case") or "").strip()[:80]
                    domain_object_id_msg = (data.get("domain_object_id") or "").strip()[:120]
                    actor_id_msg = (data.get("actor_id") or "").strip()[:50]
                    if source_domain_msg or source_use_case_msg or domain_object_id_msg or actor_id_msg:
                        logger.info(
                            "Messaging source context: tenant=%s event=%s source=%s use_case=%s object=%s actor=%s",
                            tenant_id,
                            event_type_msg,
                            source_domain_msg,
                            source_use_case_msg,
                            domain_object_id_msg,
                            actor_id_msg,
                        )

                    # 테넌트별 잔액·PFID·발신번호·단가·공급자 (Django 있을 때만)
                    info = None
                    billing_tenant_id = None
                    base_price = "0"
                    pf_id_tenant = ""
                    channel_source = "common_owner"
                    if tenant_id is not None and os.environ.get("DJANGO_SETTINGS_MODULE"):
                        from apps.domains.messaging.tenant_channels import (
                            TenantAlimtalkRouteError,
                        )

                        try:
                            from apps.domains.messaging.credit_services import (
                                deduct_credits,
                                rollback_credits,
                            )
                            context = _resolve_tenant_delivery_context(
                                int(tenant_id),
                                source_tenant_id_msg,
                                template_id_normalized,
                            )
                            info = context["billing_info"]
                            billing_tenant_id = context["billing_tenant_id"]
                            base_price = info["base_price"]
                            channel = context["channel"]
                            pf_id_tenant = (channel.get("pf_id") or "").strip()
                            channel_source = context["channel_source"]
                            template_id_normalized = context["template_id"]
                        except TenantAlimtalkRouteError as exc:
                            from decimal import Decimal

                            logger.error(
                                "Tenant Alimtalk route blocked before provider call: "
                                "tenant=%s source_tenant=%s reason=%s",
                                tenant_id,
                                source_tenant_id_msg,
                                exc,
                            )
                            try:
                                create_notification_log(
                                    tenant_id=int(tenant_id),
                                    success=False,
                                    amount_deducted=Decimal("0"),
                                    recipient_summary=(
                                        f"{target_name} " if target_name else ""
                                    )
                                    + (to[:4] + "****" if to else ""),
                                    template_summary=event_type_msg,
                                    failure_reason=str(exc),
                                    message_body="",
                                    message_mode="alimtalk",
                                    sqs_message_id=message_id,
                                    notification_type=event_type_msg,
                                    source_tenant_id=source_tenant_id_msg,
                                    target_type=target_type_msg,
                                    target_id=target_id_msg,
                                    target_name=target_name,
                                    business_idempotency_key=str(
                                        data.get("business_idempotency_key") or ""
                                    ),
                                    recipient_fingerprint=recipient_fingerprint_msg,
                                    origin_type=origin_type_msg,
                                    origin_id=origin_id_msg,
                                )
                            except Exception:
                                logger.exception(
                                    "Failed to record blocked tenant channel route"
                                )
                                continue
                            queue_client.delete_message(
                                queue_name=cfg.MESSAGING_SQS_QUEUE_NAME,
                                receipt_handle=receipt_handle,
                            )
                            _msg_deleted = True
                            _current_receipt_handle = None
                            continue
                        except Exception:
                            logger.exception(
                                "Messaging tenant/provider resolution failed before claim; "
                                "leaving SQS message for retry: tenant=%s source_tenant=%s",
                                tenant_id,
                                source_tenant_id_msg,
                            )
                            continue
                    else:
                        logger.error(
                            "Messaging tenant context unavailable before provider call; "
                            "leaving SQS message for retry"
                        )
                        continue

                    # 공유 Solapi 계정의 검증된 발신번호 하나만 사용한다.
                    sender = (cfg.SOLAPI_SENDER or "").strip()

                    # 알림톡 사용 시: 검증된 tenant route 또는 공용 owner route
                    pf_id = pf_id_tenant or cfg.SOLAPI_KAKAO_PF_ID
                    template_id = template_id_normalized

                    # Business-level atomic claim (Layer 2: DB dedup via unique constraint)
                    business_key = _resolve_worker_business_key(data, message_id)
                    claim_log_id = None
                    if business_key and tenant_id is not None and os.environ.get("DJANGO_SETTINGS_MODULE"):
                        try:
                            from academy.adapters.db.django.repositories_messaging import claim_notification_slot
                            claimed, claim_log_id = claim_notification_slot(
                                tenant_id=int(tenant_id),
                                message_mode=message_mode or "alimtalk",
                                business_idempotency_key=business_key,
                                sqs_message_id=message_id,
                                recipient_summary=_recipient_summary(target_name, to),
                                source_tenant_id=source_tenant_id_msg,
                                target_type=target_type_msg,
                                target_id=target_id_msg,
                                target_name=target_name,
                                recipient_fingerprint=recipient_fingerprint_msg,
                                origin_type=origin_type_msg,
                                origin_id=origin_id_msg,
                            )
                            if not claimed:
                                if claim_log_id is not None:
                                    logger.warning(
                                        "Business dedup: key=%s still processing for same SQS message; leaving for retry/DLQ (tenant=%s log_id=%s)",
                                        business_key[:16], tenant_id, claim_log_id,
                                    )
                                    continue
                                logger.info(
                                    "Business dedup: key=%s already claimed, skipping (tenant=%s)",
                                    business_key[:16], tenant_id,
                                )
                                queue_client.delete_message(
                                    queue_name=cfg.MESSAGING_SQS_QUEUE_NAME,
                                    receipt_handle=receipt_handle,
                                )
                                _msg_deleted = True
                                _current_receipt_handle = None
                                continue
                        except Exception as e:
                            logger.exception(
                                "Business claim failed; leaving SQS message for retry: tenant=%s key=%s",
                                tenant_id, business_key[:16],
                            )
                            # DB claim is the durable idempotency guard. If it is unavailable,
                            # do not fail open and risk duplicate delivery; let SQS redeliver.
                            continue
                    else:
                        # Legacy DB-level dedup: Redis 장애 복구 후 재처리 시 이미 발송된 메시지 스킵
                        if tenant_id is not None and os.environ.get("DJANGO_SETTINGS_MODULE"):
                            try:
                                from apps.domains.messaging.models import NotificationLog as _NL
                                if _NL.objects.filter(
                                    sqs_message_id=message_id, success=True
                                ).exists():
                                    logger.info(
                                        "DB dedup: message_id=%s already sent successfully, skipping",
                                        message_id,
                                    )
                                    queue_client.delete_message(
                                        queue_name=cfg.MESSAGING_SQS_QUEUE_NAME,
                                        receipt_handle=receipt_handle,
                                    )
                                    _msg_deleted = True
                                    _current_receipt_handle = None
                                    continue
                            except Exception as e:
                                logger.warning("DB dedup check failed (proceeding): %s", e)

                    # 서비스 계약은 알림톡 전용이다. 비지원 모드는
                    # 크레딧을 예약하기 전에 영구 실패로 닫는다.
                    if message_mode != "alimtalk":
                        from decimal import Decimal
                        from academy.adapters.db.django.repositories_messaging import (
                            create_notification_log,
                            finalize_notification,
                        )

                        if claim_log_id is not None:
                            finalize_notification(
                                claim_log_id,
                                success=False,
                                amount_deducted=Decimal("0"),
                                failure_reason=channel_policy_block_reason,
                                message_body=text[:2000],
                                notification_type=event_type_msg,
                            )
                        else:
                            create_notification_log(
                                tenant_id=int(tenant_id),
                                success=False,
                                amount_deducted=Decimal("0"),
                                recipient_summary=_recipient_summary(target_name, to),
                                failure_reason=channel_policy_block_reason,
                                message_body=text[:2000],
                                message_mode=message_mode,
                                sqs_message_id=message_id,
                                notification_type=event_type_msg,
                                source_tenant_id=source_tenant_id_msg,
                                target_type=target_type_msg,
                                target_id=target_id_msg,
                                target_name=target_name,
                            )
                        queue_client.delete_message(
                            queue_name=cfg.MESSAGING_SQS_QUEUE_NAME,
                            receipt_handle=receipt_handle,
                        )
                        _msg_deleted = True
                        _current_receipt_handle = None
                        continue

                    # 잔액 검증 및 차감 (Django + info 있을 때, 단가 > 0)
                    deducted = False

                    def _rollback_deducted_credits() -> None:
                        if not deducted or billing_tenant_id is None:
                            return
                        if claim_log_id is not None:
                            from apps.domains.messaging.credit_services import (
                                rollback_notification_credits,
                            )

                            rollback_notification_credits(
                                notification_log_id=claim_log_id,
                                billing_tenant_id=int(billing_tenant_id),
                            )
                            return
                        from apps.domains.messaging.credit_services import rollback_credits

                        rollback_credits(int(billing_tenant_id), base_price)

                    try:
                        if info and float(base_price) > 0 and billing_tenant_id is not None:
                            _record_progress(job_id, "validating", 30, step_index=2, step_percent=0, tenant_id=tenant_id_str)
                            from apps.domains.messaging.credit_services import deduct_credits
                            if claim_log_id is not None:
                                from apps.domains.messaging.credit_services import (
                                    reserve_notification_credits,
                                )

                                reserve_notification_credits(
                                    notification_log_id=claim_log_id,
                                    billing_tenant_id=int(billing_tenant_id),
                                    amount=base_price,
                                )
                            else:
                                deduct_credits(int(billing_tenant_id), base_price)
                            deducted = True
                            _record_progress(job_id, "validating", 50, step_index=2, step_percent=100, tenant_id=tenant_id_str)
                        else:
                            _record_progress(job_id, "validating", 50, step_index=2, step_percent=100, tenant_id=tenant_id_str)
                    except ValueError as e:
                        if str(e) != "insufficient_balance":
                            raise
                        from decimal import Decimal
                        from academy.adapters.db.django.repositories_messaging import (
                            create_notification_log,
                            finalize_notification,
                        )

                        logger.warning(
                            "business_tenant_id=%s insufficient_balance base_price=%s, skip send",
                            billing_tenant_id,
                            base_price,
                        )
                        if claim_log_id is not None:
                            finalize_notification(
                                claim_log_id,
                                success=False,
                                amount_deducted=Decimal("0"),
                                failure_reason="insufficient_balance",
                                message_body=text[:2000],
                                notification_type=event_type_msg,
                            )
                        else:
                            create_notification_log(
                                tenant_id=int(tenant_id),
                                success=False,
                                amount_deducted=Decimal("0"),
                                recipient_summary=_recipient_summary(target_name, to),
                                failure_reason="insufficient_balance",
                                message_body=text[:2000],
                                message_mode=message_mode,
                                sqs_message_id=message_id,
                                notification_type=event_type_msg,
                                source_tenant_id=source_tenant_id_msg,
                                target_type=target_type_msg,
                                target_id=target_id_msg,
                                target_name=target_name,
                            )
                        queue_client.delete_message(
                            queue_name=cfg.MESSAGING_SQS_QUEUE_NAME,
                            receipt_handle=receipt_handle,
                        )
                        _msg_deleted = True
                        _current_receipt_handle = None
                        continue
                    except Exception as e:
                        logger.exception("deduct_credits failed: %s", e)
                        _current_receipt_handle = None
                        _msg_deleted = True  # 발송 전 실패 → 중복 위험 없음, 잠금 해제하여 재시도 허용
                        consecutive_errors += 1
                        continue

                    def _dispatch_alimtalk(
                        to_,
                        sender_,
                        pf_id_,
                        template_id_,
                        replacements_,
                        text_="",
                        before_provider_call=None,
                    ):
                        logger.info(
                            "alimtalk via verified route: tenant=%s channel_source=%s",
                            tenant_id,
                            channel_source,
                        )
                        return send_one_alimtalk(
                            cfg, to=to_, sender=sender_, pf_id=pf_id_,
                            template_id=template_id_, replacements=replacements_, text=text_,
                            before_provider_call=before_provider_call,
                        )

                    provider_send_started = False

                    def _start_provider_send() -> bool:
                        nonlocal provider_send_started
                        if provider_send_started:
                            return True
                        _assert_clinic_reminder_delivery_active(
                            data, business_tenant_id=business_tenant_id,
                        )
                        if claim_log_id is not None:
                            try:
                                from academy.adapters.db.django.repositories_messaging import (
                                    mark_notification_sending,
                                )

                                if not mark_notification_sending(claim_log_id):
                                    logger.warning(
                                        "Provider send boundary claim failed: tenant=%s log_id=%s",
                                        tenant_id,
                                        claim_log_id,
                                    )
                                    return False
                            except Exception:
                                logger.exception(
                                    "Provider send boundary claim errored: tenant=%s log_id=%s",
                                    tenant_id,
                                    claim_log_id,
                                )
                                return False
                        provider_send_started = True
                        return True

                    try:
                        _record_progress(job_id, "sending", 70, step_index=3, step_percent=0, tenant_id=tenant_id_str)
                        if pf_id and template_id:
                            result = _dispatch_alimtalk(
                                to, sender, pf_id, template_id,
                                alimtalk_replacements if isinstance(alimtalk_replacements, list) else None,
                                text_=text,
                                before_provider_call=_start_provider_send,
                            )
                        else:
                            result = {
                                "status": "error",
                                "reason": "alimtalk_requires_pf_id_and_template_id",
                            }
                        _record_progress(job_id, "sending", 90, step_index=3, step_percent=100, tenant_id=tenant_id_str)

                        send_succeeded = result.get("status") == "ok"
                        raw_reason = str(result.get("reason") or "send_failed")[:500]
                        failure_disposition = (
                            "sent"
                            if send_succeeded
                            else _send_failure_disposition(
                                raw_reason,
                                provider_send_started=provider_send_started,
                                definitely_not_accepted=bool(
                                    result.get("definitely_not_accepted")
                                ),
                                provider_retryable=result.get("provider_retryable"),
                            )
                        )
                        result_persisted = True

                        # 성공 시 로그, 확정 실패/공급사 호출 전 실패만 크레딧 롤백.
                        # 공급사 호출이 시작된 뒤의 통신 오류는 실제 접수 여부를 알 수
                        # 없으므로 ambiguous로 남기고 자동 재발송/자동 환불하지 않는다.
                        if tenant_id is not None and os.environ.get("DJANGO_SETTINGS_MODULE"):
                            try:
                                from decimal import Decimal
                                from academy.adapters.db.django.repositories_messaging import (
                                    create_notification_log, finalize_notification,
                                )
                                provider_message_id = str(
                                    result.get("group_id")
                                    or result.get("messagekey")
                                    or result.get("refkey")
                                    or ""
                                )[:128]
                                charged_amount = (
                                    Decimal(str(base_price)) if deducted else Decimal("0")
                                )
                                if send_succeeded:
                                    if claim_log_id is not None:
                                        finalize_notification(
                                            claim_log_id,
                                            success=True,
                                            amount_deducted=charged_amount,
                                            template_summary=_get_template_summary(event_type_msg, template_id, message_mode),
                                            message_body=text[:2000],
                                            provider_message_id=provider_message_id,
                                            notification_type=event_type_msg,
                                        )
                                    else:
                                        create_notification_log(
                                            tenant_id=int(tenant_id),
                                            success=True,
                                            amount_deducted=charged_amount,
                                            recipient_summary=_recipient_summary(target_name, to),
                                            template_summary=_get_template_summary(event_type_msg, template_id, message_mode),
                                            message_body=text[:2000],
                                            message_mode=message_mode,
                                            sqs_message_id=message_id,
                                            provider_message_id=provider_message_id,
                                            notification_type=event_type_msg,
                                            source_tenant_id=source_tenant_id_msg,
                                            target_type=target_type_msg,
                                            target_id=target_id_msg,
                                            target_name=target_name,
                                        )
                                else:
                                    if deducted and failure_disposition != "ambiguous":
                                        _rollback_deducted_credits()
                                    # 솔라피 IP 미허용 등으로 실패 시 발송 내역 비고에 안내 문구 추가
                                    if any(
                                        x in (raw_reason or "").lower()
                                        for x in ("forbidden", "허용되지 않은 ip", "unauthorized ip", "unauthorized)")
                                    ):
                                        failure_reason = (
                                            "솔라피 IP 미등록: SOLAPI 콘솔(console.solapi.com)에서 "
                                            "설정 > 허용 IP에 이 서버의 나가는 IP를 추가해 주세요. "
                                        ) + (raw_reason or "")[:400]
                                    else:
                                        failure_reason = raw_reason
                                    if claim_log_id is not None:
                                        failure_status = {
                                            "terminal": "failed",
                                            "retry": "retryable_failed",
                                            "ambiguous": "ambiguous",
                                        }[failure_disposition]
                                        finalize_notification(
                                            claim_log_id,
                                            success=False,
                                            amount_deducted=(
                                                charged_amount
                                                if failure_disposition == "ambiguous"
                                                else Decimal("0")
                                            ),
                                            failure_reason=failure_reason[:500],
                                            message_body=text[:2000],
                                            provider_message_id=provider_message_id,
                                            notification_type=event_type_msg,
                                            failure_status=failure_status,
                                            provider_definitely_not_accepted=bool(
                                                result.get("definitely_not_accepted")
                                            ),
                                        )
                                    else:
                                        create_notification_log(
                                            tenant_id=int(tenant_id),
                                            success=False,
                                            amount_deducted=Decimal("0"),
                                            recipient_summary=_recipient_summary(target_name, to),
                                            failure_reason=failure_reason[:500],
                                            message_body=text[:2000],
                                            message_mode=message_mode,
                                            sqs_message_id=message_id,
                                            provider_message_id=provider_message_id,
                                            notification_type=event_type_msg,
                                            source_tenant_id=source_tenant_id_msg,
                                            target_type=target_type_msg,
                                            target_id=target_id_msg,
                                            target_name=target_name,
                                        )
                            except Exception as e:
                                logger.exception("NotificationLog/rollback failed: %s", e)
                                result_persisted = False

                        if not result_persisted:
                            logger.warning(
                                "Send result persistence failed; leaving SQS message for guarded retry: tenant=%s log_id=%s",
                                tenant_id,
                                claim_log_id,
                            )
                            consecutive_errors += 1
                        elif send_succeeded:
                            _record_progress(job_id, "done", 100, step_index=4, step_percent=100, tenant_id=tenant_id_str)
                            queue_client.delete_message(
                                queue_name=cfg.MESSAGING_SQS_QUEUE_NAME,
                                receipt_handle=receipt_handle,
                            )
                            _msg_deleted = True
                            consecutive_errors = 0
                        else:
                            if failure_disposition == "terminal":
                                logger.error(
                                    "send permanently failed (non-retryable), deleting message: reason=%s to=%s tenant=%s",
                                    raw_reason, to[:4] if to else "?", tenant_id,
                                )
                                queue_client.delete_message(
                                    queue_name=cfg.MESSAGING_SQS_QUEUE_NAME,
                                    receipt_handle=receipt_handle,
                                )
                                _msg_deleted = True
                                consecutive_errors = 0
                            elif failure_disposition == "ambiguous":
                                logger.error(
                                    "send outcome ambiguous after provider call; suppressing automatic resend: reason=%s to=%s tenant=%s log_id=%s",
                                    raw_reason,
                                    to[:4] if to else "?",
                                    tenant_id,
                                    claim_log_id,
                                )
                                queue_client.delete_message(
                                    queue_name=cfg.MESSAGING_SQS_QUEUE_NAME,
                                    receipt_handle=receipt_handle,
                                )
                                _msg_deleted = True
                                consecutive_errors = 0
                            else:
                                logger.warning(
                                    "send failed before provider call, message will retry: %s",
                                    raw_reason,
                                )
                                consecutive_errors += 1
                                if consecutive_errors >= max_consecutive_errors:
                                    logger.error("Too many consecutive errors (%s), exiting", consecutive_errors)
                                    return 1

                        _current_receipt_handle = None
                    except Exception:
                        if deducted and not provider_send_started:
                            try:
                                _rollback_deducted_credits()
                                logger.info("Rolled back credits for tenant_id=%s after send exception", tenant_id)
                            except Exception as rb_err:
                                logger.exception("rollback_credits failed after send exception: %s", rb_err)
                        raise

                    if _shutdown:
                        logger.info("Graceful shutdown: exiting")
                        break
                finally:
                    # 중복 발송 방지: 성공(메시지 삭제)·파싱 오류·검증 실패 등
                    # delete_message가 호출된 경우에만 잠금 해제.
                    # 발송 실패 시 잠금 유지 → SQS VisibilityTimeout(900s)
                    # 내 재전달 시 acquire_job_lock이 차단하여 중복 발송 방지.
                    # 잠금 TTL(1800s) > VisibilityTimeout(900s) 이므로 안전.
                    if _msg_deleted:
                        # 메시지가 이미 삭제됨(성공·파싱오류) → 잠금 해제
                        release_job_lock(job_id)
                    # else: 발송 실패 → 잠금 유지 (TTL 만료까지 중복 차단)

            except KeyboardInterrupt:
                break
            except QueueUnavailableError:
                # 이미 내부 try에서 처리하지만, 다른 경로로 올 수 있음
                time.sleep(60)
                continue
            except Exception as e:
                logger.exception("Unexpected error in main loop: %s", e)
                consecutive_errors += 1
                if consecutive_errors >= max_consecutive_errors:
                    return 1
                time.sleep(5)

        logger.info("Messaging Worker shutdown complete")
        return 0

    except Exception:
        logger.exception("Fatal error in Messaging Worker")
        return 1


if __name__ == "__main__":
    sys.exit(main())
