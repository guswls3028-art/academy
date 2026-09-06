from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.domains.messaging.alimtalk_content_builders import (
    MANUAL_EVENT_TO_TEMPLATE_TYPE,
    get_provider_template_contract,
)
from apps.domains.messaging.models import MessageTemplate


MANUAL_PREFLIGHT_TTL_SECONDS = 300
MANUAL_PREFLIGHT_PAYLOAD_KIND = "manual_send_preflight_v1"


@dataclass(frozen=True)
class ManualDeliveryIdentityError(Exception):
    code: str
    detail: str

    def __str__(self) -> str:
        return self.detail


def resolve_content_template_snapshot(tenant, data: dict[str, Any]):
    """Resolve an optional saved letter and reject a stale score-editor copy."""

    template_id = data.get("template_id")
    if not template_id:
        return None

    template = MessageTemplate.objects.filter(
        tenant=tenant,
        pk=template_id,
        retired_at__isnull=True,
    ).first()
    if not template:
        raise ManualDeliveryIdentityError(
            "content_template_not_found",
            "선택한 문구를 찾을 수 없습니다. 문구를 다시 선택해 주세요.",
        )
    if template.is_system:
        raise ManualDeliveryIdentityError(
            "content_template_system_preset_disabled",
            "시스템 기본 문구는 수동 발송에 자동 적용되지 않습니다. 이번 발송 문구를 직접 작성하거나 저장한 문구를 선택해 주세요.",
        )

    if (data.get("block_category") or "").strip() != "grades":
        return template

    if template.category != MessageTemplate.Category.GRADES:
        raise ManualDeliveryIdentityError(
            "content_template_category_mismatch",
            "수업 결과에는 성적 문구만 사용할 수 있습니다.",
        )

    supplied_version = (data.get("template_version") or "").strip()
    supplied_at = parse_datetime(supplied_version)
    if supplied_at is None or supplied_at != template.updated_at:
        raise ManualDeliveryIdentityError(
            "content_template_stale",
            "선택한 성적 문구가 변경되었습니다. 최신 문구를 다시 선택해 주세요.",
        )
    return template


def build_content_snapshot_sha256(*, text: str, replacements: list[dict] | None) -> str:
    canonical = json.dumps(
        {"text": text, "replacements": replacements or []},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _manual_request_body_sha256(data: dict[str, Any]) -> str:
    scheduled_send_at = data.get("scheduled_send_at")
    canonical = json.dumps(
        {
            "raw_body": str(data.get("raw_body") or "").strip(),
            "raw_subject": str(data.get("raw_subject") or "").strip(),
            "alimtalk_extra_vars": data.get("alimtalk_extra_vars") or {},
            "alimtalk_extra_vars_per_student": data.get(
                "alimtalk_extra_vars_per_student"
            )
            or {},
            "scheduled_send_at": (
                scheduled_send_at.isoformat()
                if hasattr(scheduled_send_at, "isoformat")
                else str(scheduled_send_at or "")
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_manual_preflight_identity(
    *,
    tenant,
    data: dict[str, Any],
    recipients,
    template_plan,
) -> dict[str, Any]:
    """Build the exact non-PII identity that preflight authorizes once."""

    from apps.domains.messaging.security import build_recipient_fingerprint

    recipient_scope = sorted(
        (
            {
                "student_id": int(recipient.student_id),
                "phone_fingerprint": build_recipient_fingerprint(recipient.phone),
            }
            for recipient in recipients
        ),
        key=lambda item: (item["student_id"], item["phone_fingerprint"]),
    )
    return {
        "tenant_id": int(tenant.id),
        "recipients": recipient_scope,
        "send_to": str(data.get("send_to") or "parent"),
        "body_sha256": _manual_request_body_sha256(data),
        "content_template_id": template_plan.content_template_id or "",
        "content_template_version": template_plan.content_template_version or "",
        "event_type": f"manual_{str(data.get('manual_event') or '').strip()}",
        "provider_template_id": template_plan.solapi_template_id or "",
        "provider_template_type": template_plan.template_type or "",
        "provider_template_version": template_plan.provider_template_version or "",
        "provider_template_structure_fingerprint": (
            template_plan.provider_template_structure_fingerprint or ""
        ),
        "provider_template_content_fingerprint": (
            template_plan.provider_template_content_fingerprint or ""
        ),
        "provider_template_header_fingerprint": (
            template_plan.provider_template_header_fingerprint or ""
        ),
    }


def issue_manual_send_preflight_identity(
    *,
    tenant,
    data: dict[str, Any],
    recipients,
    template_plan,
    actor_id: int | None,
) -> str:
    """Persist and sign one exact preflight authorization with a short TTL."""

    from apps.domains.messaging.models import NotificationPreviewToken
    from apps.domains.messaging.security import build_manual_preflight_signature

    token = uuid.uuid4()
    expires_at = timezone.now() + timedelta(seconds=MANUAL_PREFLIGHT_TTL_SECONDS)
    identity = build_manual_preflight_identity(
        tenant=tenant,
        data=data,
        recipients=recipients,
        template_plan=template_plan,
    )
    identity["expires_at"] = expires_at.isoformat()
    signature = build_manual_preflight_signature(
        token=str(token),
        identity=identity,
    )
    NotificationPreviewToken.objects.create(
        token=token,
        tenant=tenant,
        notification_type=identity["event_type"][:30],
        session_type="manual_send",
        session_id=0,
        send_to=identity["send_to"],
        payload={
            "kind": MANUAL_PREFLIGHT_PAYLOAD_KIND,
            "identity": identity,
        },
        created_by_id=actor_id,
        expires_at=expires_at,
    )
    return f"{token}.{signature}"


def consume_manual_send_preflight_identity(
    *,
    signed_token: str,
    tenant,
    data: dict[str, Any],
    recipients,
    template_plan,
    actor_id: int | None,
) -> ManualDeliveryIdentityError | None:
    """Atomically consume one signed preflight only when the request is unchanged."""

    from apps.domains.messaging.models import NotificationPreviewToken
    from apps.domains.messaging.security import verify_manual_preflight_signature

    try:
        token_text, signature = str(signed_token or "").rsplit(".", 1)
        token_uuid = uuid.UUID(token_text)
    except (AttributeError, ValueError):
        return ManualDeliveryIdentityError(
            "invalid_preflight_identity",
            "발송 전 확인 정보가 올바르지 않습니다. 다시 확인해 주세요.",
        )

    with transaction.atomic():
        token = (
            NotificationPreviewToken.objects.select_for_update()
            .filter(token=token_uuid, tenant=tenant)
            .first()
        )
        if token is None:
            return ManualDeliveryIdentityError(
                "preflight_identity_not_found",
                "발송 전 확인 정보를 찾을 수 없습니다. 다시 확인해 주세요.",
            )
        if token.used_at is not None:
            return ManualDeliveryIdentityError(
                "preflight_identity_used",
                "이미 사용된 발송 확인입니다. 중복 발송을 막기 위해 다시 확인해 주세요.",
            )
        if timezone.now() > token.expires_at:
            return ManualDeliveryIdentityError(
                "preflight_identity_expired",
                "발송 전 확인이 만료되었습니다. 다시 확인해 주세요.",
            )
        if token.created_by_id is not None and token.created_by_id != actor_id:
            return ManualDeliveryIdentityError(
                "preflight_identity_actor_mismatch",
                "다른 사용자가 확인한 발송은 실행할 수 없습니다.",
            )

        payload = token.payload if isinstance(token.payload, dict) else {}
        identity = payload.get("identity")
        if payload.get("kind") != MANUAL_PREFLIGHT_PAYLOAD_KIND or not isinstance(
            identity, dict
        ):
            return ManualDeliveryIdentityError(
                "invalid_preflight_identity",
                "발송 전 확인 정보가 올바르지 않습니다. 다시 확인해 주세요.",
            )
        if not verify_manual_preflight_signature(
            token=token_text,
            identity=identity,
            signature=signature,
        ):
            return ManualDeliveryIdentityError(
                "invalid_preflight_identity_signature",
                "발송 전 확인 정보가 변경되었습니다. 다시 확인해 주세요.",
            )

        current_identity = build_manual_preflight_identity(
            tenant=tenant,
            data=data,
            recipients=recipients,
            template_plan=template_plan,
        )
        expected_identity = {
            key: value for key, value in identity.items() if key != "expires_at"
        }
        if current_identity != expected_identity:
            return ManualDeliveryIdentityError(
                "preflight_identity_mismatch",
                "발송 대상·문구 또는 승인 봉투가 변경되었습니다. 다시 확인해 주세요.",
            )

        token.used_at = timezone.now()
        token.payload = {
            "redacted": True,
            "kind": MANUAL_PREFLIGHT_PAYLOAD_KIND,
        }
        token.save(update_fields=["used_at", "payload"])
    return None


def build_manual_delivery_identity(
    *,
    template_type: str,
    content_template,
    text: str,
    replacements: list[dict] | None,
) -> dict[str, Any]:
    """Return immutable non-PII identity carried by outbox, queue, and worker."""

    contract = get_provider_template_contract(template_type)
    if not contract:
        return {}
    payload: dict[str, Any] = {
        "provider_template_type": template_type,
        "provider_template_version": contract["template_version"],
        "provider_template_structure_fingerprint": contract["structure_fingerprint"],
        "content_snapshot_sha256": build_content_snapshot_sha256(
            text=text,
            replacements=replacements,
        ),
    }
    if contract.get("content_fingerprint"):
        payload["provider_template_content_fingerprint"] = contract["content_fingerprint"]
    if contract.get("header_fingerprint"):
        payload["provider_template_header_fingerprint"] = contract["header_fingerprint"]
    if content_template is not None:
        content_template_id = getattr(content_template, "id", None)
        content_template_updated_at = getattr(content_template, "updated_at", None)
        if content_template_id is not None:
            payload["content_template_id"] = content_template_id
        if content_template_updated_at is not None:
            payload["content_template_version"] = content_template_updated_at.isoformat()
    return payload


def validate_delivery_identity(payload: dict[str, Any], *, event_type: str) -> str:
    """Validate the exact provider envelope and content snapshot for any event."""

    if event_type.startswith("manual_"):
        manual_event = event_type.removeprefix("manual_")
        template_type = MANUAL_EVENT_TO_TEMPLATE_TYPE.get(manual_event)
    else:
        from apps.domains.messaging.alimtalk_content_builders import get_template_type

        template_type = get_template_type(event_type)
    contract = get_provider_template_contract(template_type or "")
    if not contract:
        return "event_provider_contract_missing"
    expected = {
        "template_id": contract["template_id"],
        "provider_template_type": template_type,
        "provider_template_version": contract["template_version"],
        "provider_template_structure_fingerprint": contract["structure_fingerprint"],
    }
    if contract.get("content_fingerprint"):
        expected["provider_template_content_fingerprint"] = contract["content_fingerprint"]
    if contract.get("header_fingerprint"):
        expected["provider_template_header_fingerprint"] = contract["header_fingerprint"]
    for key, value in expected.items():
        if payload.get(key) != value:
            return f"invalid_{key}"

    replacement_keys = tuple(
        str(item.get("key") or "")
        for item in (payload.get("alimtalk_replacements") or [])
        if isinstance(item, dict)
    )
    if replacement_keys != contract["variables"]:
        return "invalid_provider_template_variables"

    actual_snapshot = build_content_snapshot_sha256(
        text=str(payload.get("text") or ""),
        replacements=payload.get("alimtalk_replacements") or [],
    )
    if payload.get("content_snapshot_sha256") != actual_snapshot:
        return "invalid_content_snapshot_sha256"
    return ""
