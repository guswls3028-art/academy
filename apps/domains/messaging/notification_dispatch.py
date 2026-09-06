# apps/support/messaging/notification_dispatch.py
"""
수동 알림 발송 서비스 — preview → confirm 2단계 발송.

자동 트리거 발송이 아닌 선생의 명시적 수동 발송만 지원.
저장과 발송을 완전 분리.
"""

import json
import logging
import uuid
from datetime import timedelta
from typing import Optional

from academy.adapters.db.django import repositories_enrollment as enroll_repo
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

PREVIEW_TOKEN_TTL_SECONDS = 300  # 5분
MAX_PREVIEW_TOKEN_PAYLOAD_BYTES = 2_000_000
MAX_PREVIEW_MESSAGE_BODY_LENGTH = 5_000


def _is_alimtalk_mode(raw_mode: str | None) -> bool:
    """Missing legacy values mean Alimtalk; explicit non-Alimtalk values fail closed."""
    return (raw_mode or "alimtalk").strip().lower() == "alimtalk"


def _provider_identity_fields(template_type: str, template_id: str, content_template) -> dict:
    """Return the exact configured provider contract carried to confirm/worker."""
    from apps.domains.messaging.alimtalk_content_builders import (
        get_provider_template_contract,
    )

    contract = get_provider_template_contract(template_type) or {}
    if not contract or str(contract.get("template_id") or "") != str(template_id or ""):
        return {}
    result = {
        "provider_template_type": template_type,
        "provider_template_version": str(contract.get("template_version") or ""),
        "provider_template_structure_fingerprint": str(
            contract.get("structure_fingerprint") or ""
        ),
    }
    if contract.get("content_fingerprint"):
        result["provider_template_content_fingerprint"] = str(
            contract["content_fingerprint"]
        )
    if contract.get("header_fingerprint"):
        result["provider_template_header_fingerprint"] = str(
            contract["header_fingerprint"]
        )
    if content_template is not None:
        result["content_template_id"] = int(content_template.id)
        result["content_template_version"] = content_template.updated_at.isoformat()
    return result


def build_attendance_preview(
    tenant,
    session_id: int,
    notification_type: str,
    send_to: str = "parent",
) -> dict:
    """
    출결 알림 미리보기 데이터 생성.

    Args:
        tenant: Tenant 인스턴스
        session_id: Session ID
        notification_type: "check_in" | "absent"
        send_to: "parent" | "student"

    Returns:
        {
            "recipients": [{"student_id", "student_name", "phone", "status", "message_body", "excluded", "exclude_reason"}],
            "total_count": int,
            "excluded_count": int,
            "message_template_body": str,
            "notification_type": str,
        }
    """
    from apps.domains.messaging.selectors import get_auto_send_config
    from apps.domains.messaging.services import get_tenant_site_url

    session = enroll_repo.get_session_with_lecture_by_id_for_tenant(session_id, tenant)
    if not session:
        return {"error": "세션을 찾을 수 없습니다.", "recipients": [], "total_count": 0, "excluded_count": 0}

    # 트리거 매핑
    trigger_map = {
        "check_in": "check_in_complete",
        "absent": "absent_occurred",
    }
    trigger = trigger_map.get(notification_type)
    if not trigger:
        return {"error": f"지원하지 않는 알림 유형: {notification_type}", "recipients": [], "total_count": 0, "excluded_count": 0}

    # 테넌트 config는 이번 발송의 내용만 소유한다. 공급사 봉투는 아래의
    # 정확한 업무 이벤트 계약에서만 가져온다.
    config = get_auto_send_config(tenant.id, trigger)
    if not config:
        return {"error": "발송 설정이 없습니다.", "recipients": [], "total_count": 0, "excluded_count": 0}
    content_template = config.template if config else None
    if content_template and content_template.tenant_id != tenant.id:
        return {"error": "발송 템플릿의 테넌트가 일치하지 않습니다.", "recipients": [], "total_count": 0, "excluded_count": 0}
    template = content_template
    if not _is_alimtalk_mode(config.message_mode if config else "alimtalk"):
        return {"error": "알림톡 전용 설정이 아닙니다.", "recipients": [], "total_count": 0, "excluded_count": 0}
    effective_mode = "alimtalk"
    # 업무 이벤트에 등록된 승인 봉투 외에는 사용하지 않는다.
    from apps.domains.messaging.alimtalk_content_builders import (
        get_solapi_template_id as get_unified_tid,
        get_template_type,
        build_manual_replacements,
        render_alimtalk_preview_text,
    )
    unified_type = get_template_type(trigger)
    unified_tid = get_unified_tid(trigger)
    if not unified_type or not unified_tid:
        return {"error": "승인된 알림톡 템플릿이 없습니다.", "recipients": [], "total_count": 0, "excluded_count": 0}
    solapi_template_id = unified_tid
    provider_identity = _provider_identity_fields(
        unified_type,
        solapi_template_id,
        template,
    )
    if not provider_identity.get("provider_template_version"):
        return {"error": "승인된 알림톡 템플릿 계약이 없습니다.", "recipients": [], "total_count": 0, "excluded_count": 0}

    # Manual notification dispatch is alimtalk-only; approved template required.
    if not template or not (template.body or "").strip():
        return {"error": "발송 템플릿이 없습니다.", "recipients": [], "total_count": 0, "excluded_count": 0}
    if len(template.body or "") > MAX_PREVIEW_MESSAGE_BODY_LENGTH:
        return {"error": "알림톡 문구가 허용 길이를 초과했습니다.", "recipients": [], "total_count": 0, "excluded_count": 0}
    # 출결 상태별 필터
    if notification_type == "check_in":
        status_filter = ["PRESENT", "LATE", "ONLINE", "SUPPLEMENT"]
    else:  # absent
        status_filter = ["ABSENT"]

    attendances = enroll_repo.attendance_filter_session_tenant_statuses(
        session_id,
        tenant,
        status_filter,
    )

    academy_name = (tenant.name or "").strip()
    site_url = get_tenant_site_url(tenant) or ""
    now = timezone.localtime()

    recipients = []
    for att in attendances:
        student = att.enrollment.student
        name = (student.name or "").strip()
        name_2 = name[-2:] if len(name) >= 2 else name  # 성(첫 글자) 제외 = 이름만

        if send_to == "parent":
            phone = (student.parent_phone or "").replace("-", "").strip()
        else:
            phone = (student.phone or "").replace("-", "").strip()

        excluded = False
        exclude_reason = ""
        if not phone or len(phone) < 10:
            excluded = True
            exclude_reason = "전화번호 없음" if not phone else "전화번호 형식 오류"

        # 본문 치환
        body = (template.body or "").strip()
        # 반 정보 (section_mode일 때)
        section = getattr(att.session, "section", None)
        section_label = ""
        if section:
            prefix = "클리닉 " if section.section_type == "CLINIC" else ""
            section_label = f"{prefix}{section.label}반"

        # 클리닉 세션은 lecture 없음 — 안전 가드.
        _lecture = getattr(att.session, "lecture", None)
        _lecture_title = (getattr(_lecture, "title", "") or "") if _lecture else "클리닉"
        context = {
            "학원명": academy_name,
            "학생이름": name,
            "학생이름2": name_2,
            "사이트링크": site_url,
            "강의명": _lecture_title,
            "차시명": att.session.title or att.session.display_label,
            "반이름": section_label,
            "날짜": str(att.session.date) if att.session.date else now.strftime("%Y-%m-%d"),
            "시간": now.strftime("%H:%M"),
        }
        for k, v in context.items():
            body = body.replace(f"#{{{k}}}", v)

        alimtalk_reps = build_manual_replacements(
            template_type=unified_type,
            content_body=(template.body or "").strip(),
            context=context,
            tenant_name=academy_name,
            student_name=name,
            site_url=site_url,
        )
        full_message_body = render_alimtalk_preview_text(unified_type, alimtalk_reps)

        recipients.append({
            "student_id": student.id,
            "student_name": name,
            "phone": phone[:3] + "****" + phone[-4:] if len(phone) >= 7 else phone,
            "phone_raw": phone,
            "status": att.status,
            "message_body": body,
            "full_message_body": full_message_body,
            "excluded": excluded,
            "exclude_reason": exclude_reason,
            "alimtalk_replacements": alimtalk_reps,
        })

    sendable = [r for r in recipients if not r["excluded"]]

    return {
        "recipients": recipients,
        "total_count": len(sendable),
        "excluded_count": len(recipients) - len(sendable),
        "message_template_body": (template.body or "").strip(),
        "notification_type": notification_type,
        "session_title": session.title or session.display_label,
        "lecture_title": session.lecture.title or "",
        "solapi_template_id": solapi_template_id,
        "message_mode": effective_mode,
        **provider_identity,
    }


def build_student_list_preview(
    tenant,
    trigger: str,
    student_ids: list[int],
    send_to: str = "parent",
    context_per_student: dict = None,
    shared_context: dict = None,
) -> dict:
    """
    학생 ID 목록 기반 범용 알림 미리보기.
    시험 성적 공개, 퇴원 안내, 과제 미제출 등 모든 MANUAL_DEFAULT에 사용.
    """
    from apps.domains.messaging.selectors import get_auto_send_config
    from apps.domains.messaging.services import get_tenant_site_url
    from apps.domains.messaging.services.recipients import resolve_student_message_recipients

    config = get_auto_send_config(tenant.id, trigger)
    if not config:
        return {"error": "발송 설정이 없습니다.", "recipients": [], "total_count": 0, "excluded_count": 0}
    content_template = config.template if config else None
    if content_template and content_template.tenant_id != tenant.id:
        return {"error": "발송 템플릿의 테넌트가 일치하지 않습니다.", "recipients": [], "total_count": 0, "excluded_count": 0}
    template = content_template
    if not _is_alimtalk_mode(config.message_mode if config else "alimtalk"):
        return {"error": "알림톡 전용 설정이 아닙니다.", "recipients": [], "total_count": 0, "excluded_count": 0}
    effective_mode = "alimtalk"
    # 업무 이벤트에 등록된 승인 봉투 외에는 사용하지 않는다.
    from apps.domains.messaging.alimtalk_content_builders import (
        get_solapi_template_id as get_unified_tid,
        get_template_type,
        build_manual_replacements,
        render_alimtalk_preview_text,
    )
    unified_type = get_template_type(trigger)
    unified_tid = get_unified_tid(trigger)
    if not unified_type or not unified_tid:
        return {"error": "승인된 알림톡 템플릿이 없습니다.", "recipients": [], "total_count": 0, "excluded_count": 0}
    solapi_template_id = unified_tid
    provider_identity = _provider_identity_fields(
        unified_type,
        solapi_template_id,
        template,
    )
    if not provider_identity.get("provider_template_version"):
        return {"error": "승인된 알림톡 템플릿 계약이 없습니다.", "recipients": [], "total_count": 0, "excluded_count": 0}

    if not template or not (template.body or "").strip():
        return {"error": "발송 템플릿이 없습니다.", "recipients": [], "total_count": 0, "excluded_count": 0}
    if len(template.body or "") > MAX_PREVIEW_MESSAGE_BODY_LENGTH:
        return {"error": "알림톡 문구가 허용 길이를 초과했습니다.", "recipients": [], "total_count": 0, "excluded_count": 0}
    resolved_recipients = resolve_student_message_recipients(
        tenant,
        student_ids,
        send_to=send_to,
    )

    academy_name = (tenant.name or "").strip()
    site_url = get_tenant_site_url(tenant) or ""
    context_per_student = context_per_student or {}
    shared_context = shared_context or {}

    recipients = []
    for resolved in resolved_recipients:
        name = resolved.student_name
        name_2 = name[-2:] if len(name) >= 2 else name  # 성(첫 글자) 제외 = 이름만
        phone = resolved.phone

        excluded = False
        exclude_reason = ""
        if not phone or len(phone) < 10:
            excluded = True
            exclude_reason = "전화번호 없음" if not phone else "전화번호 형식 오류"

        ctx = {
            "학원명": academy_name,
            "학생이름": name,
            "학생이름2": name_2,
            "사이트링크": site_url,
            **shared_context,
            **context_per_student.get(resolved.student_id, {}),
        }

        body = (template.body or "").strip()
        for k, v in ctx.items():
            body = body.replace(f"#{{{k}}}", str(v))
        if len(body) > MAX_PREVIEW_MESSAGE_BODY_LENGTH:
            return {
                "error": "생성된 알림톡 본문이 허용 길이를 초과했습니다.",
                "recipients": [],
                "total_count": 0,
                "excluded_count": 0,
            }

        alimtalk_reps = build_manual_replacements(
            template_type=unified_type,
            content_body=(template.body or "").strip(),
            context=ctx,
            tenant_name=academy_name,
            student_name=name,
            site_url=site_url,
        )
        full_message_body = render_alimtalk_preview_text(unified_type, alimtalk_reps)

        recipients.append({
            "student_id": resolved.student_id,
            "student_name": name,
            "phone": phone[:3] + "****" + phone[-4:] if len(phone) >= 7 else phone,
            "phone_raw": phone,
            "message_body": body,
            "full_message_body": full_message_body,
            "excluded": excluded,
            "exclude_reason": exclude_reason,
            "alimtalk_replacements": alimtalk_reps,
        })

    sendable = [r for r in recipients if not r["excluded"]]
    return {
        "recipients": recipients,
        "total_count": len(sendable),
        "excluded_count": len(recipients) - len(sendable),
        "message_template_body": (template.body or "").strip(),
        "notification_type": trigger,
        "solapi_template_id": solapi_template_id,
        "message_mode": effective_mode,
        **provider_identity,
    }


def create_preview_token(
    tenant,
    preview_data: dict,
    staff_id: Optional[int],
    session_type: str,
    session_id: int,
    notification_type: str,
    send_to: str,
) -> str:
    """preview 데이터를 토큰에 저장하고 UUID 반환."""
    from apps.domains.messaging.models import NotificationPreviewToken

    token_uuid = uuid.uuid4()
    expires = timezone.now() + timedelta(seconds=PREVIEW_TOKEN_TTL_SECONDS)

    # 발송 가능한 수신자만 payload에 저장
    sendable = [
        {
            key: value
            for key, value in recipient.items()
            if key != "full_message_body"
        }
        for recipient in preview_data.get("recipients", [])
        if not recipient.get("excluded")
    ]
    payload = {
        "recipients": sendable,
        "solapi_template_id": preview_data.get("solapi_template_id", ""),
        "message_mode": preview_data.get("message_mode", "alimtalk"),
        "message_template_body": preview_data.get("message_template_body", ""),
        "notification_type": notification_type,
        "send_to": send_to,
    }
    for key in (
        "provider_template_type",
        "provider_template_version",
        "provider_template_structure_fingerprint",
        "provider_template_content_fingerprint",
        "provider_template_header_fingerprint",
        "content_template_id",
        "content_template_version",
    ):
        value = preview_data.get(key)
        if value not in (None, ""):
            payload[key] = value
    payload_bytes = len(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    if payload_bytes > MAX_PREVIEW_TOKEN_PAYLOAD_BYTES:
        raise ValueError("notification preview payload exceeds storage limit")

    NotificationPreviewToken.objects.create(
        token=token_uuid,
        tenant=tenant,
        notification_type=notification_type,
        session_type=session_type,
        session_id=session_id,
        send_to=send_to,
        payload=payload,
        created_by_id=staff_id,
        expires_at=expires,
    )

    return str(token_uuid)


def consume_preview_token(token_str: str, tenant) -> dict:
    """
    토큰을 소비하고 payload 반환.
    실패 시 {"error": "..."} 반환.
    """
    from apps.domains.messaging.models import NotificationPreviewToken

    try:
        token_uuid = uuid.UUID(token_str)
    except (ValueError, AttributeError):
        return {"error": "유효하지 않은 토큰 형식입니다."}

    token = NotificationPreviewToken.objects.filter(
        token=token_uuid,
        tenant=tenant,
    ).order_by("-created_at", "-id").first()

    if not token:
        return {"error": "토큰을 찾을 수 없습니다."}

    if token.used_at is not None:
        return {"error": "이미 사용된 토큰입니다. 중복 발송이 방지되었습니다."}

    if timezone.now() > token.expires_at:
        return {"error": "토큰이 만료되었습니다. 미리보기를 다시 실행해주세요."}

    # Atomic consume (race condition 방지). 발송에 필요한 원문은 호출자에게만
    # 반환하고, 소비된 토큰에는 수신자/본문을 남기지 않는다.
    from apps.domains.messaging.models import NotificationPreviewToken as NPT
    from apps.domains.messaging.security import redact_consumed_preview_payload

    now = timezone.now()
    batch_id = uuid.uuid4()
    delivery_payload = token.payload
    updated = NPT.objects.filter(
        token=token_uuid,
        tenant=tenant,
        used_at__isnull=True,
    ).update(
        used_at=now,
        batch_id=batch_id,
        payload=redact_consumed_preview_payload(delivery_payload),
    )

    if updated == 0:
        return {"error": "이미 사용된 토큰입니다. 중복 발송이 방지되었습니다."}

    return {
        "payload": delivery_payload,
        "batch_id": str(batch_id),
        "staff_id": token.created_by_id,
        "notification_type": token.notification_type,
    }


def execute_notification_batch(
    tenant,
    payload: dict,
    batch_id: str,
    staff_id: Optional[int],
    *,
    process: bool = True,
) -> dict:
    """
    payload의 수신자 목록에 대해 알림톡 발송 실행.
    """
    from apps.domains.messaging.policy import check_recipient_allowed
    from apps.domains.messaging.scheduled import (
        create_notification_outboxes,
        process_due_notifications,
    )

    recipients = payload.get("recipients", [])
    solapi_template_id = payload.get("solapi_template_id", "")
    raw_message_mode = str(payload.get("message_mode") or "alimtalk").strip().lower()
    notification_type = payload.get("notification_type", "")
    send_to = payload.get("send_to", "parent")
    target_type = "parent" if send_to == "parent" else "student"
    provider_identity = {
        key: payload[key]
        for key in (
            "provider_template_type",
            "provider_template_version",
            "provider_template_structure_fingerprint",
            "provider_template_content_fingerprint",
            "provider_template_header_fingerprint",
            "content_template_id",
            "content_template_version",
        )
        if payload.get(key) not in (None, "")
    }

    _can_alimtalk = raw_message_mode == "alimtalk" and bool(solapi_template_id)
    modes_to_send = ["alimtalk"] if _can_alimtalk else []

    if not modes_to_send:
        logger.warning(
            "batch %s: no available channel (mode=%s, can_alimtalk=%s)",
            batch_id, raw_message_mode, _can_alimtalk,
        )
        return {
            "batch_id": batch_id,
            "sent_count": 0,
            "pending_count": 0,
            "accepted_count": 0,
            "failed_count": len([r for r in recipients if not r.get("excluded")]),
            "blocked_count": 0,
            "error": "발송 가능한 채널이 없습니다.",
        }

    failed = 0
    blocked = 0
    outbox_specs = []

    for r in recipients:
        phone = r.get("phone_raw", "")
        if not phone or r.get("excluded"):
            continue

        if not check_recipient_allowed(phone):
            blocked += 1
            logger.info("batch %s: blocked recipient %s (whitelist)", batch_id, phone[:4] + "****")
            continue

        canonical_event_type = {
            "check_in": "check_in_complete",
            "absent": "absent_occurred",
        }.get(notification_type, notification_type)
        delivery_identity = dict(provider_identity)
        if delivery_identity.get("provider_template_version"):
            from apps.domains.messaging.services.manual_delivery_identity import (
                build_content_snapshot_sha256,
            )

            delivery_identity["content_snapshot_sha256"] = build_content_snapshot_sha256(
                text=str(r.get("message_body") or ""),
                replacements=r.get("alimtalk_replacements") or [],
            )
        for mode in modes_to_send:
            outbox_specs.append(
                {
                    "trigger": canonical_event_type,
                    "send_at": timezone.now(),
                    "payload": {
                        "tenant_id": tenant.id,
                        "to": phone,
                        "text": r.get("message_body", ""),
                        "message_mode": mode,
                        "template_id": solapi_template_id if mode == "alimtalk" else None,
                        "alimtalk_replacements": r.get("alimtalk_replacements", []) if mode == "alimtalk" else None,
                        "event_type": canonical_event_type,
                        "target_type": target_type,
                        "target_id": r.get("student_id"),
                        "target_name": r.get("student_name", ""),
                        "occurrence_key": f"batch_{batch_id}",
                        **delivery_identity,
                    },
                }
            )

    outboxes = create_notification_outboxes(
        tenant_id=tenant.id,
        notifications=outbox_specs,
    )
    outbox_ids = [notification.id for notification in outboxes]
    if outboxes and process:
        process_due_notifications(
            batch_size=len(outboxes),
            notification_ids=outbox_ids,
        )
        from apps.domains.messaging.models import ScheduledNotification

        statuses = list(
            ScheduledNotification.objects.filter(
                id__in=outbox_ids
            ).values_list("status", flat=True)
        )
        failed = sum(
            1 for value in statuses if value == ScheduledNotification.Status.FAILED
        )
        sent = sum(
            1 for value in statuses if value == ScheduledNotification.Status.SENT
        )
        pending = sum(
            1
            for value in statuses
            if value in {
                ScheduledNotification.Status.PENDING,
                ScheduledNotification.Status.DISPATCHING,
            }
        )
    elif outboxes:
        sent = 0
        pending = len(outboxes)
    else:
        sent = 0
        pending = 0

    logger.info(
        "notification batch completed: batch_id=%s type=%s sent=%d failed=%d blocked=%d staff=%s",
        batch_id, notification_type, sent, failed, blocked, staff_id,
    )

    result = {
        "batch_id": batch_id,
        "sent_count": sent,
        "pending_count": pending,
        "accepted_count": sent + pending,
        "failed_count": failed,
        "blocked_count": blocked,
    }
    if not process:
        result["_outbox_ids"] = outbox_ids
    return result


def consume_preview_token_and_execute(token_str: str, tenant) -> dict:
    """Consume a preview token only after its durable outbox batch exists."""
    from apps.domains.messaging.models import NotificationPreviewToken
    from apps.domains.messaging.security import redact_consumed_preview_payload

    try:
        token_uuid = uuid.UUID(token_str)
    except (ValueError, AttributeError):
        return {"error": "유효하지 않은 토큰 형식입니다.", "status": 400}

    with transaction.atomic():
        token = (
            NotificationPreviewToken.objects.select_for_update()
            .filter(token=token_uuid, tenant=tenant)
            .order_by("pk")
            .first()
        )
        if token is None:
            return {"error": "토큰을 찾을 수 없습니다.", "status": 400}
        if token.used_at is not None:
            return {
                "error": "이미 사용된 토큰입니다. 중복 발송이 방지되었습니다.",
                "status": 400,
            }
        if timezone.now() > token.expires_at:
            return {
                "error": "토큰이 만료되었습니다. 미리보기를 다시 실행해주세요.",
                "status": 400,
            }

        batch_id = uuid.uuid4()
        delivery_payload = token.payload
        batch_result = execute_notification_batch(
            tenant=tenant,
            payload=delivery_payload,
            batch_id=str(batch_id),
            staff_id=token.created_by_id,
            process=False,
        )
        if "error" in batch_result:
            return {"error": batch_result["error"], "status": 400}
        token.used_at = timezone.now()
        token.batch_id = batch_id
        token.payload = redact_consumed_preview_payload(delivery_payload)
        token.save(update_fields=["used_at", "batch_id", "payload"])

    outbox_ids = batch_result.pop("_outbox_ids", [])
    if outbox_ids:
        from apps.domains.messaging.models import ScheduledNotification
        from apps.domains.messaging.scheduled import process_due_notifications

        process_due_notifications(
            batch_size=len(outbox_ids),
            notification_ids=outbox_ids,
        )
        statuses = list(
            ScheduledNotification.objects.filter(id__in=outbox_ids).values_list(
                "status",
                flat=True,
            )
        )
        batch_result["failed_count"] = sum(
            1 for value in statuses if value == ScheduledNotification.Status.FAILED
        )
        batch_result["sent_count"] = sum(
            1 for value in statuses if value == ScheduledNotification.Status.SENT
        )
        batch_result["pending_count"] = sum(
            1
            for value in statuses
            if value in {
                ScheduledNotification.Status.PENDING,
                ScheduledNotification.Status.DISPATCHING,
            }
        )
        batch_result["accepted_count"] = (
            batch_result["sent_count"] + batch_result["pending_count"]
        )
    return {"batch_result": batch_result}
