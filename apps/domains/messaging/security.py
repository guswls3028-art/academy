from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any

from django.core.exceptions import ImproperlyConfigured

TENANT_BINDING_SIGNATURE_VERSION = "v1"
DELIVERY_IDENTITY_SIGNATURE_VERSION = "v2"
MANUAL_PREFLIGHT_SIGNATURE_VERSION = "v1"
RECIPIENT_FINGERPRINT_VERSION = "v1"

_DELIVERY_IDENTITY_FIELDS = (
    "event_type",
    "template_id",
    "provider_template_type",
    "provider_template_version",
    "provider_template_structure_fingerprint",
    "provider_template_content_fingerprint",
    "provider_template_header_fingerprint",
    "content_template_id",
    "content_template_version",
    "content_snapshot_sha256",
)

SENSITIVE_NOTIFICATION_TYPES = frozenset(
    {
        "registration_approved_student",
        "registration_approved_parent",
        "password_find_otp",
        "password_reset_student",
        "password_reset_parent",
    }
)

SENSITIVE_MESSAGE_PLACEHOLDER = "[보안] 계정/인증 알림 본문은 저장하지 않습니다."

_LEGACY_PARENT_PHONE_TARGET = re.compile(r"^(parent:[^:]+):([^:]+)$")

_SENSITIVE_LINE_MARKERS = (
    "비밀번호",
    "임시비밀번호",
    "임시 비밀번호",
    "인증번호",
    "password",
    "pw",
)


def normalize_recipient_phone(value: Any) -> str:
    """Return the digits-only recipient representation used at every boundary."""

    return re.sub(r"\D", "", str(value or ""))


def build_recipient_fingerprint(value: Any, *, key: str | None = None) -> str:
    """Build a stable, non-reversible lookup key without retaining phone PII."""

    from django.conf import settings

    normalized = normalize_recipient_phone(value)
    if not normalized:
        return ""
    resolved_key = str(
        key
        if key is not None
        else getattr(settings, "MESSAGING_TENANT_BINDING_KEY", "")
        or ""
    ).strip()
    if not resolved_key:
        raise ImproperlyConfigured("MESSAGING_TENANT_BINDING_KEY is required")
    material = (
        f"messaging-recipient-fingerprint:{RECIPIENT_FINGERPRINT_VERSION}:"
        f"{normalized}"
    ).encode("utf-8")
    return hmac.new(
        resolved_key.encode("utf-8"),
        material,
        hashlib.sha256,
    ).hexdigest()


def build_recipient_fingerprint_candidates(value: Any) -> tuple[str, ...]:
    """Include rotation fallbacks so incident lookup survives an active key rotation."""

    from django.conf import settings

    keys = (
        str(getattr(settings, "MESSAGING_TENANT_BINDING_KEY", "") or "").strip(),
        *tuple(
            str(item).strip()
            for item in getattr(
                settings,
                "MESSAGING_TENANT_BINDING_FALLBACK_KEYS",
                (),
            )
            if str(item).strip()
        ),
    )
    fingerprints = [
        build_recipient_fingerprint(value, key=item)
        for item in keys
        if item
    ]
    return tuple(dict.fromkeys(item for item in fingerprints if item))


def build_tenant_binding_signature(
    *,
    tenant_id: int,
    source_tenant_id: int | None,
    business_idempotency_key: str,
) -> str:
    """Authenticate the delivery/billing tenant binding on an SQS payload."""
    from django.conf import settings

    key = str(getattr(settings, "MESSAGING_TENANT_BINDING_KEY", "") or "").strip()
    if not key:
        raise ImproperlyConfigured("MESSAGING_TENANT_BINDING_KEY is required")
    source_part = "" if source_tenant_id is None else str(int(source_tenant_id))
    material = (
        f"messaging-tenant-binding:{TENANT_BINDING_SIGNATURE_VERSION}:"
        f"{int(tenant_id)}:{source_part}:"
        f"{business_idempotency_key}"
    ).encode("utf-8")
    return hmac.new(
        key.encode("utf-8"),
        material,
        hashlib.sha256,
    ).hexdigest()


def verify_tenant_binding_signature(
    *,
    signature: str,
    tenant_id: int,
    source_tenant_id: int | None,
    business_idempotency_key: str,
) -> bool:
    from django.conf import settings

    source_part = "" if source_tenant_id is None else str(int(source_tenant_id))
    material = (
        f"messaging-tenant-binding:{TENANT_BINDING_SIGNATURE_VERSION}:"
        f"{int(tenant_id)}:{source_part}:{business_idempotency_key}"
    ).encode("utf-8")
    primary = str(getattr(settings, "MESSAGING_TENANT_BINDING_KEY", "") or "").strip()
    fallbacks = tuple(
        str(key).strip()
        for key in getattr(settings, "MESSAGING_TENANT_BINDING_FALLBACK_KEYS", ())
        if str(key).strip()
    )
    if not primary:
        raise ImproperlyConfigured("MESSAGING_TENANT_BINDING_KEY is required")
    supplied = str(signature or "")
    return any(
        hmac.compare_digest(
            supplied,
            hmac.new(key.encode("utf-8"), material, hashlib.sha256).hexdigest(),
        )
        for key in (primary, *fallbacks)
    )


def _delivery_identity_material(payload: dict[str, Any]) -> bytes:
    identity = {
        field: payload.get(field) if payload.get(field) is not None else ""
        for field in _DELIVERY_IDENTITY_FIELDS
    }
    canonical = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        f"messaging-delivery-identity:{DELIVERY_IDENTITY_SIGNATURE_VERSION}:"
        f"{canonical}"
    ).encode("utf-8")


def build_delivery_identity_signature(
    payload: dict[str, Any],
    *,
    key: str | None = None,
) -> str:
    """Authenticate an immutable provider-envelope and content snapshot identity."""

    from django.conf import settings

    resolved_key = str(
        key
        if key is not None
        else getattr(settings, "MESSAGING_TENANT_BINDING_KEY", "")
        or ""
    ).strip()
    if not resolved_key:
        raise ImproperlyConfigured("MESSAGING_TENANT_BINDING_KEY is required")
    return hmac.new(
        resolved_key.encode("utf-8"),
        _delivery_identity_material(payload),
        hashlib.sha256,
    ).hexdigest()


def verify_delivery_identity_signature(
    payload: dict[str, Any],
    *,
    signature: str,
) -> bool:
    """Verify v2 identity with the active key and configured rotation fallbacks."""

    from django.conf import settings

    primary = str(getattr(settings, "MESSAGING_TENANT_BINDING_KEY", "") or "").strip()
    fallbacks = tuple(
        str(key).strip()
        for key in getattr(settings, "MESSAGING_TENANT_BINDING_FALLBACK_KEYS", ())
        if str(key).strip()
    )
    if not primary:
        raise ImproperlyConfigured("MESSAGING_TENANT_BINDING_KEY is required")
    supplied = str(signature or "")
    material = _delivery_identity_material(payload)
    return any(
        hmac.compare_digest(
            supplied,
            hmac.new(key.encode("utf-8"), material, hashlib.sha256).hexdigest(),
        )
        for key in (primary, *fallbacks)
    )


def _manual_preflight_material(*, token: str, identity: dict[str, Any]) -> bytes:
    canonical = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        f"messaging-manual-preflight:{MANUAL_PREFLIGHT_SIGNATURE_VERSION}:"
        f"{token}:{canonical}"
    ).encode("utf-8")


def build_manual_preflight_signature(
    *,
    token: str,
    identity: dict[str, Any],
    key: str | None = None,
) -> str:
    """Sign the exact one-time manual-send identity returned by preflight."""

    from django.conf import settings

    resolved_key = str(
        key
        if key is not None
        else getattr(settings, "MESSAGING_TENANT_BINDING_KEY", "")
        or ""
    ).strip()
    if not resolved_key:
        raise ImproperlyConfigured("MESSAGING_TENANT_BINDING_KEY is required")
    return hmac.new(
        resolved_key.encode("utf-8"),
        _manual_preflight_material(token=token, identity=identity),
        hashlib.sha256,
    ).hexdigest()


def verify_manual_preflight_signature(
    *,
    token: str,
    identity: dict[str, Any],
    signature: str,
) -> bool:
    """Verify a preflight identity across configured signing-key rotation."""

    from django.conf import settings

    primary = str(getattr(settings, "MESSAGING_TENANT_BINDING_KEY", "") or "").strip()
    fallbacks = tuple(
        str(key).strip()
        for key in getattr(settings, "MESSAGING_TENANT_BINDING_FALLBACK_KEYS", ())
        if str(key).strip()
    )
    if not primary:
        raise ImproperlyConfigured("MESSAGING_TENANT_BINDING_KEY is required")
    supplied = str(signature or "")
    material = _manual_preflight_material(token=token, identity=identity)
    return any(
        hmac.compare_digest(
            supplied,
            hmac.new(key.encode("utf-8"), material, hashlib.sha256).hexdigest(),
        )
        for key in (primary, *fallbacks)
    )


def sanitize_message_body_for_log(
    message_body: str,
    *,
    notification_type: str = "",
) -> str:
    """Return a NotificationLog-safe body with account secrets removed."""

    body = str(message_body or "")
    if not body:
        return ""

    if notification_type in SENSITIVE_NOTIFICATION_TYPES:
        return SENSITIVE_MESSAGE_PLACEHOLDER

    redacted_lines: list[str] = []
    for line in body.splitlines():
        lowered = line.lower()
        if not any(marker in lowered for marker in _SENSITIVE_LINE_MARKERS):
            redacted_lines.append(line)
            continue

        separator = "：" if "：" in line else ":"
        if separator in line:
            prefix = line.split(separator, 1)[0].rstrip()
            redacted_lines.append(f"{prefix}{separator} [보안상 숨김]")
        else:
            redacted_lines.append("[보안상 민감정보 줄 숨김]")
    return "\n".join(redacted_lines)


def sanitize_notification_target_id(target_id: Any) -> str:
    """Remove a phone suffix written by the legacy parent-account key format."""

    value = str(target_id or "")
    match = _LEGACY_PARENT_PHONE_TARGET.fullmatch(value)
    if match:
        phone_digits = re.sub(r"\D", "", match.group(2))
        if 10 <= len(phone_digits) <= 15:
            return match.group(1)
    return value


def is_sensitive_notification(
    *,
    trigger: str = "",
    payload: object = None,
) -> bool:
    """Return whether a durable delivery row can contain an account secret."""

    event_type = payload.get("event_type", "") if isinstance(payload, dict) else ""
    return trigger in SENSITIVE_NOTIFICATION_TYPES or event_type in SENSITIVE_NOTIFICATION_TYPES


def redact_terminal_delivery_payload(*, trigger: str, payload: object) -> object:
    """Drop recipient PII once a durable delivery cannot be retried.

    Retryable rows deliberately retain the complete provider payload. Non-object
    malformed rows are returned verbatim so incident responders do not lose the
    original forensic evidence.
    """

    if not isinstance(payload, dict):
        return payload

    redacted: dict[str, Any] = {"redacted": True}
    for key in (
        "tenant_id",
        "source_tenant_id",
        "event_type",
        "target_type",
        "target_id",
        "occurrence_key",
        "message_mode",
        "template_id",
        "provider_template_type",
        "provider_template_version",
        "provider_template_structure_fingerprint",
        "provider_template_content_fingerprint",
        "provider_template_header_fingerprint",
        "delivery_identity_version",
        "content_template_id",
        "content_template_version",
        "content_snapshot_sha256",
        "recipient_fingerprint",
        "origin_type",
        "origin_id",
        "request_id",
        "batch_id",
        "sender_staff_id",
        "trace_identity_version",
        "source_domain",
        "source_use_case",
        "domain_object_id",
    ):
        value = payload.get(key)
        if value not in (None, ""):
            redacted[key] = value
    if "target_id" in redacted:
        redacted["target_id"] = sanitize_notification_target_id(
            redacted["target_id"]
        )
    return redacted


def redact_consumed_preview_payload(payload: object) -> dict[str, Any]:
    """Keep only non-recipient metadata after a preview token is consumed."""

    source = payload if isinstance(payload, dict) else {}
    return {
        "redacted": True,
        "recipients": [],
        "notification_type": str(source.get("notification_type") or ""),
        "send_to": str(source.get("send_to") or ""),
    }
