from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from django.db.models import F


_TEMPLATE_FINGERPRINT_FIELDS = (
    "content",
    "categoryCode",
    "buttons",
    "quickReplies",
    "messageType",
    "emphasizeType",
    "header",
    "highlight",
    "item",
    "extra",
    "ad",
    "emphasizeTitle",
    "emphasizeSubtitle",
    "securityFlag",
    "imageId",
)


class TenantAlimtalkRouteError(RuntimeError):
    """A verified tenant channel cannot safely serve the requested template."""


@dataclass(frozen=True)
class AlimtalkDeliveryRoute:
    channel_id: str
    template_id: str
    source: str


def build_template_fingerprint(template: dict[str, Any]) -> str:
    """Hash only provider fields that affect an Alimtalk template's behavior."""

    canonical = {
        field: template.get(field)
        for field in _TEMPLATE_FINGERPRINT_FIELDS
    }
    material = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def required_common_template_ids() -> tuple[str, ...]:
    """Return the currently supported common-channel template identifiers."""

    from apps.domains.messaging.alimtalk_content_builders import (
        TEMPLATE_TYPE_TO_SOLAPI_ID,
    )
    from apps.domains.messaging.models import AutoSendConfig
    from apps.domains.messaging.policy import get_owner_tenant_id

    template_ids = {
        str(value or "").strip()
        for value in TEMPLATE_TYPE_TO_SOLAPI_ID.values()
        if str(value or "").strip()
    }
    template_ids.update(
        str(value or "").strip()
        for value in AutoSendConfig.objects.filter(
            tenant_id=get_owner_tenant_id(),
            template__solapi_status="APPROVED",
        ).values_list("template__solapi_template_id", flat=True)
        if str(value or "").strip()
    )
    return tuple(sorted(template_ids))


def resolve_alimtalk_delivery_route(
    *,
    source_tenant_id: int,
    common_channel_id: str,
    common_template_id: str,
) -> AlimtalkDeliveryRoute:
    """Translate a validated common template to an active tenant channel."""

    from apps.domains.messaging.models import (
        AlimtalkChannelBinding,
        AlimtalkTemplateBinding,
    )

    common_channel_id = str(common_channel_id or "").strip()
    common_template_id = str(common_template_id or "").strip()
    if not common_channel_id or not common_template_id:
        raise TenantAlimtalkRouteError("common_alimtalk_route_unavailable")

    channel = AlimtalkChannelBinding.objects.filter(
        tenant_id=int(source_tenant_id),
        status=AlimtalkChannelBinding.Status.ACTIVE,
        verified_at__isnull=False,
    ).first()
    if channel is None:
        return AlimtalkDeliveryRoute(
            channel_id=common_channel_id,
            template_id=common_template_id,
            source="common_owner",
        )

    template = AlimtalkTemplateBinding.objects.filter(
        channel=channel,
        source_template_id=common_template_id,
        status="APPROVED",
    ).first()
    if (
        template is None
        or not template.channel_template_id.strip()
        or not template.source_fingerprint
        or template.source_fingerprint != template.channel_fingerprint
    ):
        raise TenantAlimtalkRouteError("tenant_channel_template_unavailable")

    return AlimtalkDeliveryRoute(
        channel_id=channel.channel_id,
        template_id=template.channel_template_id,
        source="tenant_verified",
    )


def mask_channel_reference(channel_id: str) -> str:
    channel_id = str(channel_id or "").strip()
    if not channel_id:
        return ""
    return f"채널 ····{channel_id[-4:]}"


def get_tenant_channel_status(tenant_id: int) -> dict[str, Any]:
    """Build the tenant-facing channel projection without exposing raw IDs."""

    from apps.domains.messaging.models import AlimtalkChannelBinding

    required_ids = set(required_common_template_ids())
    channel = AlimtalkChannelBinding.objects.filter(tenant_id=int(tenant_id)).first()
    if channel is None:
        return {
            "channel_source": "common_owner",
            "custom_channel_status": "not_configured",
            "custom_channel_registered": False,
            "custom_channel_reference": "",
            "custom_channel_approved_templates": 0,
            "custom_channel_required_templates": len(required_ids),
            "custom_channel_test_available": False,
            "custom_channel_last_test_status": "",
            "custom_channel_last_tested_at": None,
            "custom_channel_routing_blocked": False,
        }

    approved_ids = set(
        channel.template_bindings.filter(
            status="APPROVED",
            source_fingerprint=F("channel_fingerprint"),
        ).values_list("source_template_id", flat=True)
    )
    ready = required_ids.issubset(approved_ids)
    effective_status = channel.status
    if channel.status == AlimtalkChannelBinding.Status.ACTIVE and not ready:
        effective_status = AlimtalkChannelBinding.Status.SUSPENDED
    return {
        "channel_source": (
            "tenant_verified"
            if effective_status == AlimtalkChannelBinding.Status.ACTIVE
            else (
                "tenant_pending"
                if effective_status == AlimtalkChannelBinding.Status.PENDING_TEMPLATES
                else "tenant_suspended"
            )
        ),
        "custom_channel_status": effective_status,
        "custom_channel_registered": True,
        "custom_channel_reference": mask_channel_reference(channel.channel_id),
        "custom_channel_approved_templates": len(required_ids & approved_ids),
        "custom_channel_required_templates": len(required_ids),
        "custom_channel_test_available": bool(
            channel.test_template_id and channel.test_template_fingerprint
        ),
        "custom_channel_last_test_status": channel.last_test_status,
        "custom_channel_last_tested_at": channel.last_tested_at,
        "custom_channel_routing_blocked": bool(
            channel.status == AlimtalkChannelBinding.Status.ACTIVE and not ready
        ),
    }
