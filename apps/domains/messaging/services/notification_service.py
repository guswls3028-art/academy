# apps/support/messaging/services/notification_service.py
"""
이벤트 기반 알림 발송 — send_event_notification
"""

import logging

logger = logging.getLogger(__name__)


def send_clinic_reminder_for_students(*, session_id: int):
    from apps.support.clinic.session_dependencies import (
        send_clinic_reminder_for_students as _send_clinic_reminder_for_students,
    )

    return _send_clinic_reminder_for_students(session_id=session_id)


def send_due_clinic_reminders(
    *,
    now=None,
    tenant_id: int | None = None,
    window_minutes: int = 5,
    dry_run: bool = False,
) -> dict:
    from apps.support.clinic.session_dependencies import (
        send_due_clinic_reminders as _send_due_clinic_reminders,
    )

    return _send_due_clinic_reminders(
        now=now,
        tenant_id=tenant_id,
        window_minutes=window_minutes,
        dry_run=dry_run,
    )


def send_event_notification(
    tenant,
    trigger: str,
    student,
    send_to: str = "parent",  # "parent" | "student"
    context: dict = None,
    send_at=None,
) -> bool:
    """
    이벤트 기반 자동 알림톡 발송.
    AutoSendConfig에서 enabled 확인 → 템플릿 resolve → enqueue.

    Args:
        tenant: Tenant 인스턴스
        trigger: AutoSendConfig.Trigger 값 (예: "check_in_complete")
        student: Student 인스턴스 (name, phone, parent_phone 필요)
        send_to: "parent" (학부모) 또는 "student"
        context: 추가 치환 변수 dict (예: {"강의명": "수학A반", "차시명": "3차시"})

    Returns:
        bool: enqueue 성공 여부
    """
    from apps.domains.messaging.selectors import get_auto_send_config
    from apps.domains.messaging.policy import (
        get_owner_tenant_id,
        get_trigger_policy,
        is_messaging_disabled,
        MessagingPolicyError,
        is_event_dry_run,
    )
    from apps.domains.messaging.alimtalk_content_builders import (
        get_solapi_template_id as get_unified_tid,
        get_template_type,
        build_unified_replacements,
        get_provider_template_contract,
    )
    from .url_helpers import get_tenant_site_url

    if get_trigger_policy(trigger) == "DISABLED":
        logger.info(
            "send_event_notification skipped: trigger=%s policy disabled",
            trigger,
        )
        return False

    if is_messaging_disabled(tenant.id):
        logger.info("send_event_notification skipped: tenant_id=%s messaging disabled", tenant.id)
        return False

    # Dry-run 모드: 로그만 남기고 실발송 안 함
    if is_event_dry_run(trigger):
        student_name = getattr(student, "name", "?")
        logger.info(
            "send_event_notification DRY-RUN: trigger=%s tenant=%s student=%s send_to=%s (not sending)",
            trigger, tenant.id, student_name, send_to,
        )
        return False

    # 1) 현재 테넌트의 config 조회: enabled/delay/body memo만 사용한다.
    #    알림톡 검수 템플릿과 PFID는 공용 owner SSOT에서만 resolve한다.
    config = get_auto_send_config(tenant.id, trigger)
    if not config or not config.enabled:
        logger.debug(
            "send_event_notification skipped: trigger=%s tenant=%s (config not found or disabled)",
            trigger, tenant.id,
        )
        return False

    content_template = config.template
    if not content_template:
        logger.debug("send_event_notification skipped: trigger=%s no template linked", trigger)
        return False
    if int(getattr(content_template, "tenant_id", tenant.id)) != int(tenant.id):
        logger.error(
            "send_event_notification blocked: trigger=%s template tenant mismatch",
            trigger,
        )
        return False
    if getattr(content_template, "retired_at", None) is not None:
        logger.error(
            "send_event_notification blocked: trigger=%s content template retired",
            trigger,
        )
        return False

    owner_id = get_owner_tenant_id()

    effective_mode = (config.message_mode or "alimtalk").strip().lower()
    if effective_mode != "alimtalk":
        logger.error(
            "send_event_notification blocked: trigger=%s tenant=%s non-Alimtalk mode=%s",
            trigger, tenant.id, effective_mode,
        )
        return False

    # ── 통합 알림톡 템플릿 감지 ──
    # 트리거에 매핑된 통합 템플릿이 있으면 해당 ID 사용
    unified_type = get_template_type(trigger)
    unified_tid = get_unified_tid(trigger)
    provider_contract = get_provider_template_contract(unified_type or "") or {}
    if (
        not unified_type
        or not unified_tid
        or provider_contract.get("template_id") != unified_tid
        or not provider_contract.get("template_version")
        or not provider_contract.get("structure_fingerprint")
    ):
        logger.error(
            "send_event_notification BLOCKED: trigger=%s has no exact provider contract",
            trigger,
        )
        return False

    # 수신자 전화번호
    phone = None
    if send_to == "parent":
        phone = (getattr(student, "parent_phone", "") or "").replace("-", "").strip()
    else:
        phone = (getattr(student, "phone", "") or "").replace("-", "").strip()
    if not phone or len(phone) < 10:
        logger.debug(
            "send_event_notification skipped: trigger=%s no valid phone for send_to=%s",
            trigger, send_to,
        )
        return False

    name = (getattr(student, "name", "") or "").strip()
    academy_name = (getattr(tenant, "name", "") or "").strip()
    site_url = get_tenant_site_url(tenant) or ""

    # ── show_actual_time: ITEM_LIST 시간 필드를 실제 버튼 누른 시각으로 교체 ──
    if context and context.get("_actual_time") and getattr(config, "show_actual_time", False):
        context["시간"] = context["_actual_time"]

    content_body = (content_template.body or "").strip()
    replacements = build_unified_replacements(
        trigger=trigger,
        content_body=content_body,
        context=context or {},
        tenant_name=academy_name,
        student_name=name,
        site_url=site_url,
    )
    _enqueue_text = next(
        (r["value"] for r in replacements if r["key"] == "선생님메모"),
        content_body,
    )
    _alimtalk_tid = unified_tid

    sender = ""

    # 멱등성 키
    student_id = getattr(student, "id", None) or getattr(student, "pk", None)
    domain_object_id = (context or {}).get("_domain_object_id", "")
    if not domain_object_id:
        from django.utils import timezone as _tz
        domain_object_id = _tz.localtime().strftime("%Y%m%d")
    stable_occurrence = f"{trigger}:{domain_object_id}"
    source_domain = (context or {}).get("_source_domain", "")
    source_use_case = (context or {}).get("_source_use_case", "")
    actor_id = (context or {}).get("_actor_id", "")

    # ── 발송 모드 결정: 자동발송은 알림톡 단일 경로만 사용한다. ──
    _can_alimtalk = bool(_alimtalk_tid)

    if not _can_alimtalk:
        logger.warning(
            "send_event_notification: trigger=%s tenant=%s no available alimtalk channel (mode=%s)",
            trigger, tenant.id, effective_mode,
        )
        return False

    target_type = "parent" if send_to == "parent" else "student"
    payload = {
        "tenant_id": owner_id,
        "to": phone,
        "text": _enqueue_text,
        "sender": sender,
        "message_mode": "alimtalk",
        "template_id": _alimtalk_tid,
        "alimtalk_replacements": replacements,
        "event_type": trigger,
        "target_type": target_type,
        "target_id": student_id,
        "target_name": name,
        "source_tenant_id": tenant.id if int(tenant.id) != int(owner_id) else None,
        "occurrence_key": stable_occurrence,
        "source_domain": source_domain,
        "source_use_case": source_use_case,
        "domain_object_id": domain_object_id,
        "actor_id": actor_id,
    }
    from apps.domains.messaging.services.manual_delivery_identity import (
        build_manual_delivery_identity,
    )

    payload.update(
        build_manual_delivery_identity(
            template_type=unified_type,
            content_template=content_template,
            text=_enqueue_text,
            replacements=replacements,
        )
    )

    if send_at is not None:
        try:
            from apps.domains.messaging.scheduled import schedule_exact_notification_at

            schedule_exact_notification_at(
                tenant_id=tenant.id,
                trigger=trigger,
                send_at=send_at,
                payload=payload,
            )
            return True
        except Exception as exc:
            logger.exception(
                "send_event_notification exact scheduling failed: trigger=%s tenant=%s error=%s",
                trigger,
                tenant.id,
                exc,
            )
            return False

    delay_mode = (getattr(config, "delay_mode", "immediate") or "immediate").strip().lower()
    delay_value = getattr(config, "delay_value", None)
    if delay_mode not in ("immediate", "delay_minutes", "scheduled_hour"):
        logger.warning(
            "send_event_notification skipped: trigger=%s tenant=%s invalid delay_mode=%r",
            trigger, tenant.id, delay_mode,
        )
        return False
    if delay_mode in ("delay_minutes", "scheduled_hour"):
        if delay_value is None:
            logger.warning(
                "send_event_notification skipped: trigger=%s tenant=%s delay_mode=%s without delay_value",
                trigger, tenant.id, delay_mode,
            )
            return False
        try:
            delay_value = int(delay_value)
        except (TypeError, ValueError):
            logger.warning(
                "send_event_notification skipped: trigger=%s tenant=%s invalid delay_value=%r",
                trigger, tenant.id, delay_value,
            )
            return False
        if delay_mode == "delay_minutes" and delay_value < 0:
            logger.warning(
                "send_event_notification skipped: trigger=%s tenant=%s invalid delay_minutes=%s",
                trigger, tenant.id, delay_value,
            )
            return False
        if delay_mode == "scheduled_hour" and not 0 <= delay_value <= 23:
            logger.warning(
                "send_event_notification skipped: trigger=%s tenant=%s invalid scheduled_hour=%s",
                trigger, tenant.id, delay_value,
            )
            return False
        try:
            from apps.domains.messaging.scheduled import schedule_notification
            schedule_notification(
                tenant_id=tenant.id,
                trigger=trigger,
                delay_mode=delay_mode,
                delay_value=delay_value,
                payload=payload,
            )
            return True
        except Exception as exc:
            logger.exception(
                "send_event_notification schedule failed: trigger=%s tenant=%s delay_mode=%s error=%s",
                trigger, tenant.id, delay_mode, exc,
            )
            return False

    try:
        from apps.domains.messaging.models import ScheduledNotification
        from apps.domains.messaging.scheduled import dispatch_notification_now

        notification = dispatch_notification_now(
            tenant_id=tenant.id,
            trigger=trigger,
            payload=payload,
        )
        return notification.status in {
            ScheduledNotification.Status.SENT,
            ScheduledNotification.Status.PENDING,
        }
    except MessagingPolicyError as exc:
        logger.info(
            "send_event_notification policy error: trigger=%s tenant=%s mode=alimtalk reason=%s",
            trigger, tenant.id, exc.reason,
        )
    except Exception as exc:
        logger.exception(
            "send_event_notification failed: trigger=%s tenant=%s mode=alimtalk error=%s",
            trigger, tenant.id, exc,
        )
    return False
