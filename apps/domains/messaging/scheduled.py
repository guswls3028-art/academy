# apps/support/messaging/scheduled.py
"""
예약/지연 발송 — ScheduledNotification 생성 + 처리.

delay_mode:
  - delay_minutes: 이벤트 발생 후 N분 뒤 발송
  - scheduled_hour: 다음 지정 시각(KST)에 발송 (예: 7 → 오전 7시)
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from django.db import connection, transaction
from django.db.models import Min, Q
from django.utils import timezone as dj_tz

from apps.domains.messaging.security import redact_terminal_delivery_payload

logger = logging.getLogger(__name__)

# 한국 시간대 (UTC+9)
KST = timezone(timedelta(hours=9))

# SQS send_message 자체는 짧은 호출이므로 5분 이상 dispatching이면 프로세스가
# 종료되었거나 응답 확정 전에 끊긴 것으로 본다. 같은 dispatch key로 재등록해도
# worker의 business idempotency key가 동일하므로 공급사 중복 호출로 이어지지 않는다.
DISPATCH_CLAIM_TIMEOUT = timedelta(minutes=5)
MAX_ENQUEUE_ATTEMPTS = 8
BASE_RETRY_DELAY_SECONDS = 30
MAX_RETRY_DELAY_SECONDS = 60 * 60
HOURLY_SEND_LIMIT = 500
QUOTA_MIN_RETRY_DELAY = timedelta(seconds=30)
DISABLED_ACCOUNT_NOTICE_RETRY_DELAY = timedelta(minutes=15)
REQUIRED_ACCOUNT_NOTICE_TRIGGERS = frozenset(
    {"registration_approved_student", "registration_approved_parent"}
)

# PostgreSQL transaction-scoped advisory lock namespace for the one shared
# provider account.  It does not depend on an owner-tenant row existing, and
# PostgreSQL releases it automatically on commit/rollback or connection loss.
_PROVIDER_DAILY_LOCK_NAMESPACE = 0x4D534747  # "MSGG"
_PROVIDER_DAILY_LOCK_KEY = 0x4441494C  # "DAIL"


class MessagingHourlyQuotaExceeded(Exception):
    """The business tenant has no reservation capacity in the rolling hour."""


def _acquire_provider_daily_dispatch_lock() -> None:
    """Serialize account-wide daily reservations on the production database."""

    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)",
            [_PROVIDER_DAILY_LOCK_NAMESPACE, _PROVIDER_DAILY_LOCK_KEY],
        )


@dataclass(frozen=True)
class _DispatchClaim:
    notification_id: int
    business_tenant_id: int
    trigger: str
    payload: dict
    attempt_count: int
    claimed_at: datetime


def _create_scheduled_notification_unlocked(
    *,
    tenant_id: int,
    trigger: str,
    send_at: datetime,
    payload: dict,
):
    from apps.domains.messaging.models import ScheduledNotification

    dispatch_key = uuid4()
    durable_payload = dict(payload)
    durable_payload["occurrence_key"] = (
        durable_payload.get("occurrence_key") or f"dispatch:{dispatch_key.hex}"
    )
    from apps.domains.messaging.services.queue_service import (
        build_enqueued_business_key,
    )

    business_key = build_enqueued_business_key(
        tenant_id=tenant_id,
        payload=durable_payload,
    )
    from apps.domains.messaging.security import build_recipient_fingerprint

    recipient_fingerprint = build_recipient_fingerprint(durable_payload.get("to"))
    origin_type = str(
        durable_payload.get("origin_type")
        or durable_payload.get("source_use_case")
        or durable_payload.get("source_domain")
        or ""
    ).strip()[:64]
    origin_id = str(
        durable_payload.get("origin_id")
        or durable_payload.get("domain_object_id")
        or ""
    ).strip()[:128]
    if recipient_fingerprint:
        durable_payload["recipient_fingerprint"] = recipient_fingerprint
    if origin_type:
        durable_payload["origin_type"] = origin_type
    if origin_id:
        durable_payload["origin_id"] = origin_id
    return ScheduledNotification.objects.create(
        tenant_id=tenant_id,
        trigger=trigger,
        send_at=send_at,
        payload=durable_payload,
        dispatch_key=dispatch_key,
        business_idempotency_key=business_key,
        recipient_fingerprint=recipient_fingerprint,
        origin_type=origin_type,
        origin_id=origin_id,
        status=ScheduledNotification.Status.PENDING,
    )


def create_notification_outboxes(
    *,
    tenant_id: int,
    notifications: list[dict],
) -> list[object]:
    """Atomically persist outbox rows; dispatch-time claiming enforces quota.

    Future reservations must not consume the current rolling-hour allowance.
    Immediate rows are also persisted first, then reserve capacity under the
    business Tenant lock in ``_claim_due_notifications``.
    """

    if not notifications:
        return []
    with transaction.atomic():
        from apps.domains.messaging.observers import (
            build_messaging_observer_payloads,
            get_messaging_observer_recipients,
        )

        observer_recipients = get_messaging_observer_recipients(tenant_id)
        original_outboxes = []
        for item in notifications:
            original_outbox = _create_scheduled_notification_unlocked(
                tenant_id=tenant_id,
                trigger=item["trigger"],
                send_at=item["send_at"],
                payload=item["payload"],
            )
            original_outboxes.append(original_outbox)
            for observer_payload in build_messaging_observer_payloads(
                original_outbox=original_outbox,
                recipients=observer_recipients,
            ):
                _create_scheduled_notification_unlocked(
                    tenant_id=tenant_id,
                    trigger=item["trigger"],
                    send_at=item["send_at"],
                    payload=observer_payload,
                )
        return original_outboxes


def _create_scheduled_notification(
    *,
    tenant_id: int,
    trigger: str,
    send_at: datetime,
    payload: dict,
):
    return create_notification_outboxes(
        tenant_id=tenant_id,
        notifications=[
            {"trigger": trigger, "send_at": send_at, "payload": payload},
        ],
    )[0]


def create_immediate_notification(*, tenant_id: int, trigger: str, payload: dict):
    """Persist one immediate notification without invoking an external queue."""
    return _create_scheduled_notification(
        tenant_id=tenant_id,
        trigger=trigger,
        send_at=dj_tz.now(),
        payload=payload,
    )


def schedule_exact_notification_at(
    *, tenant_id: int, trigger: str, send_at: datetime, payload: dict
):
    """Persist one exact aware send time without invoking an external queue."""
    if dj_tz.is_naive(send_at):
        raise ValueError("send_at must be timezone-aware")
    if send_at <= dj_tz.now():
        raise ValueError("send_at must be in the future")
    return _create_scheduled_notification(
        tenant_id=tenant_id,
        trigger=trigger,
        send_at=send_at,
        payload=payload,
    )


def schedule_notification(
    tenant_id: int,
    trigger: str,
    delay_mode: str,
    delay_value: int,
    payload: dict,
) -> object:
    """
    ScheduledNotification 레코드 생성.

    Args:
        tenant_id: 테넌트 ID
        trigger: AutoSendConfig 트리거명
        delay_mode: "delay_minutes" | "scheduled_hour"
        delay_value: 분 수 또는 시각(0-23)
        payload: enqueue_alimtalk kwargs (JSON 직렬화 가능해야 함)
    """
    now = dj_tz.now()

    if delay_mode == "delay_minutes":
        if delay_value < 0:
            raise ValueError("delay_minutes must be greater than or equal to 0")
        send_at = now + timedelta(minutes=delay_value)
    elif delay_mode == "scheduled_hour":
        if not 0 <= delay_value <= 23:
            raise ValueError("scheduled_hour must be between 0 and 23")
        send_at = _next_kst_hour(now, delay_value)
    else:
        raise ValueError(f"unknown delay_mode: {delay_mode}")

    notification = _create_scheduled_notification(
        tenant_id=tenant_id,
        trigger=trigger,
        send_at=send_at,
        payload=payload,
    )
    logger.info(
        "schedule_notification: created trigger=%s tenant=%s send_at=%s delay_mode=%s delay_value=%s",
        trigger, tenant_id, send_at.isoformat(), delay_mode, delay_value,
    )
    return notification


def schedule_notification_at(
    *,
    tenant_id: int,
    trigger: str,
    send_at: datetime,
    payload: dict,
):
    """
    지정 시각 예약 발송 레코드 생성.

    수동 발송 UI에서 직접 선택한 wall-clock 시각을 저장할 때 사용한다.
    """
    if dj_tz.is_naive(send_at):
        send_at = dj_tz.make_aware(send_at, dj_tz.get_current_timezone())
    if send_at <= dj_tz.now():
        raise ValueError("send_at must be in the future")

    notification = _create_scheduled_notification(
        tenant_id=tenant_id,
        trigger=trigger,
        send_at=send_at,
        payload=payload,
    )
    logger.info(
        "schedule_notification_at: created trigger=%s tenant=%s send_at=%s",
        trigger, tenant_id, send_at.isoformat(),
    )
    return notification


def dispatch_notification_now(
    *,
    tenant_id: int,
    trigger: str,
    payload: dict,
):
    """Persist an immediate dispatch before the first SQS enqueue attempt."""
    from apps.domains.messaging.models import ScheduledNotification

    notification = create_immediate_notification(
        tenant_id=tenant_id,
        trigger=trigger,
        payload=payload,
    )

    def _dispatch_after_commit() -> None:
        try:
            notification_ids = list(
                ScheduledNotification.objects.filter(
                    Q(pk=notification.id)
                    | Q(
                        origin_type="messaging_observer",
                        origin_id=f"outbox:{notification.id}",
                    )
                )
                .order_by("id")
                .values_list("id", flat=True)
            )
            process_due_notifications(
                batch_size=len(notification_ids),
                notification_ids=notification_ids,
            )
        except Exception:
            logger.exception(
                "post-commit notification dispatch failed; durable outbox remains: id=%s",
                notification.id,
            )

    # Django executes this immediately when no transaction is active.  Inside
    # a business transaction it runs only after the outermost commit, so a
    # rollback cannot leave an SQS message without its domain write/outbox.
    transaction.on_commit(_dispatch_after_commit)
    notification.refresh_from_db()
    if notification.status not in {
        ScheduledNotification.Status.SENT,
        ScheduledNotification.Status.PENDING,
        ScheduledNotification.Status.FAILED,
    }:
        logger.warning(
            "dispatch_notification_now left unexpected status=%s notif_id=%s",
            notification.status,
            notification.id,
        )
    return notification


def _next_kst_hour(now: datetime, hour: int) -> datetime:
    """
    다음 도래하는 KST 시각 계산.
    예: 현재 KST 10:30, hour=7 → 다음 날 07:00 KST
    예: 현재 KST 05:00, hour=7 → 오늘 07:00 KST
    """
    now_kst = now.astimezone(KST)
    target = now_kst.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now_kst:
        target += timedelta(days=1)
    return target


def _retry_delay(attempt_count: int) -> timedelta:
    exponent = max(0, int(attempt_count) - 1)
    seconds = min(
        BASE_RETRY_DELAY_SECONDS * (2 ** exponent),
        MAX_RETRY_DELAY_SECONDS,
    )
    return timedelta(seconds=seconds)


def _terminal_payload_error(payload: object, *, trigger: str = "") -> str:
    if not isinstance(payload, dict):
        return "invalid_payload_not_object"
    if not payload.get("tenant_id"):
        return "invalid_payload_missing_tenant_id"
    if not str(payload.get("to") or "").strip():
        return "invalid_payload_missing_recipient"
    if not str(payload.get("text") or "").strip():
        return "invalid_payload_missing_text"
    from apps.domains.messaging.observers import observer_copy_block_reason

    observer_block = observer_copy_block_reason(trigger=trigger, payload=payload)
    if observer_block:
        return observer_block
    mode = str(payload.get("message_mode") or "alimtalk").strip().lower()
    from apps.domains.messaging.policy import get_message_mode_block_reason

    return get_message_mode_block_reason(mode)


def _terminal_policy_error(*, business_tenant_id: int) -> str:
    """Return durable policy blocks that cannot succeed through queue retries."""
    from apps.domains.messaging.policy import is_messaging_disabled

    if is_messaging_disabled(business_tenant_id):
        return "business_tenant_messaging_disabled"
    return ""


def _ensure_dispatch_occurrence(payload: dict, dispatch_key: UUID) -> dict:
    durable_payload = dict(payload)
    durable_payload["occurrence_key"] = (
        durable_payload.get("occurrence_key") or f"dispatch:{dispatch_key.hex}"
    )
    return durable_payload


def _ensure_notification_dispatch_identity(notification) -> None:
    """Normalize rows inserted by an old binary during a rolling deployment."""
    if not isinstance(notification.payload, dict):
        raise ValueError("invalid_payload_not_object")
    update_fields: list[str] = []
    if notification.dispatch_key is None:
        notification.dispatch_key = uuid4()
        update_fields.append("dispatch_key")
    payload = _ensure_dispatch_occurrence(notification.payload, notification.dispatch_key)
    if payload != notification.payload:
        notification.payload = payload
        update_fields.append("payload")
    if not notification.business_idempotency_key:
        from apps.domains.messaging.services.queue_service import (
            build_enqueued_business_key,
        )

        notification.business_idempotency_key = build_enqueued_business_key(
            tenant_id=notification.tenant_id,
            payload=payload,
        )
        update_fields.append("business_idempotency_key")
    from apps.domains.messaging.security import build_recipient_fingerprint

    if not notification.recipient_fingerprint:
        notification.recipient_fingerprint = build_recipient_fingerprint(
            payload.get("to")
        )
        if notification.recipient_fingerprint:
            update_fields.append("recipient_fingerprint")
    if not notification.origin_type:
        notification.origin_type = str(
            payload.get("origin_type")
            or payload.get("source_use_case")
            or payload.get("source_domain")
            or ""
        ).strip()[:64]
        if notification.origin_type:
            update_fields.append("origin_type")
    if not notification.origin_id:
        notification.origin_id = str(
            payload.get("origin_id")
            or payload.get("domain_object_id")
            or ""
        ).strip()[:128]
        if notification.origin_id:
            update_fields.append("origin_id")
    if update_fields:
        notification.save(update_fields=update_fields)


def _claim_due_notifications(
    *,
    batch_size: int,
    now: datetime,
    notification_ids: list[int] | None = None,
) -> tuple[list[_DispatchClaim], int, int, int]:
    from apps.core.models import Tenant
    from apps.domains.messaging.models import ScheduledNotification
    from apps.domains.messaging.selectors import (
        get_provider_daily_dispatch_limit,
        get_provider_daily_notification_usage,
        get_provider_day_window,
        notification_logs_for_business_tenant,
    )

    stale_before = now - DISPATCH_CLAIM_TIMEOUT
    due_filter = (
        Q(
            status=ScheduledNotification.Status.PENDING,
            send_at__lte=now,
        )
        & (Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
    ) | (
        Q(status=ScheduledNotification.Status.DISPATCHING)
        & (Q(last_attempt_at__isnull=True) | Q(last_attempt_at__lte=stale_before))
    )
    due_qs = ScheduledNotification.objects.filter(due_filter).order_by("send_at", "id")
    if notification_ids is not None:
        due_qs = due_qs.filter(id__in=notification_ids)
    if connection.features.has_select_for_update:
        due_qs = due_qs.select_for_update(
            skip_locked=connection.features.has_select_for_update_skip_locked
        )

    claims: list[_DispatchClaim] = []
    terminal_count = 0
    deferred_count = 0
    reconciled_count = 0
    with transaction.atomic():
        due = list(due_qs[: max(1, int(batch_size))])
        if not due:
            return claims, terminal_count, deferred_count, reconciled_count
        tenant_ids = sorted({notification.tenant_id for notification in due})
        # Lock order is global provider account first, then tenant ids. This
        # serializes the shared daily reservation without coupling delivery to
        # a configurable owner-tenant row, while tenant rows retain the rolling
        # hourly quota boundary.
        _acquire_provider_daily_dispatch_lock()
        tenants = {
            tenant.id: tenant
            for tenant in Tenant.objects.select_for_update()
            .filter(id__in=tenant_ids)
            .order_by("id")
        }
        provider_daily_limit = get_provider_daily_dispatch_limit()
        provider_daily_usage = get_provider_daily_notification_usage(now=now)
        provider_day_start, provider_day_end = get_provider_day_window(now=now)
        provider_release_at = provider_day_end + timedelta(minutes=5)
        cutoff = now - timedelta(hours=1)
        usage_by_tenant: dict[int, int] = {}
        release_at_by_tenant: dict[int, datetime] = {}
        for tenant_id in tenant_ids:
            tenant = tenants[tenant_id]
            reserved_qs = ScheduledNotification.objects.filter(
                tenant_id=tenant_id,
                last_attempt_at__gte=cutoff,
            )
            reserved_keys = list(
                reserved_qs.exclude(business_idempotency_key="")
                .values_list("business_idempotency_key", flat=True)
            )
            legacy_qs = (
                notification_logs_for_business_tenant(tenant)
                .filter(sent_at__gte=cutoff)
                .exclude(business_idempotency_key__in=reserved_keys)
            )
            # Outbox attempts are the durable SSOT. Direct-send legacy logs are
            # added only when their business key is not represented by an outbox;
            # blank legacy keys remain conservatively disjoint.
            usage_by_tenant[tenant_id] = reserved_qs.count() + legacy_qs.count()
            oldest_reserved = reserved_qs.aggregate(value=Min("last_attempt_at"))["value"]
            oldest_legacy = legacy_qs.aggregate(value=Min("sent_at"))["value"]
            oldest = min(
                value
                for value in (oldest_reserved, oldest_legacy)
                if value is not None
            ) if oldest_reserved is not None or oldest_legacy is not None else now
            release_at_by_tenant[tenant_id] = max(
                now + QUOTA_MIN_RETRY_DELAY,
                oldest + timedelta(hours=1, seconds=1),
            )

        for notification in due:
            if _clinic_participant_reminder_is_stale(notification):
                notification.status = ScheduledNotification.Status.CANCELLED
                notification.next_attempt_at = None
                notification.error_message = "clinic_participant_closed"
                notification.payload = redact_terminal_delivery_payload(
                    trigger=notification.trigger,
                    payload=notification.payload,
                )
                notification.save(update_fields=[
                    "status",
                    "next_attempt_at",
                    "error_message",
                    "payload",
                ])
                terminal_count += 1
                continue
            terminal_error = _terminal_payload_error(
                notification.payload,
                trigger=notification.trigger,
            )
            if not terminal_error:
                terminal_error = _terminal_policy_error(
                    business_tenant_id=notification.tenant_id,
                )
            if (
                terminal_error == "business_tenant_messaging_disabled"
                and notification.trigger in REQUIRED_ACCOUNT_NOTICE_TRIGGERS
            ):
                notification.status = ScheduledNotification.Status.PENDING
                notification.next_attempt_at = (
                    now + DISABLED_ACCOUNT_NOTICE_RETRY_DELAY
                )
                notification.error_message = terminal_error
                notification.save(
                    update_fields=["status", "next_attempt_at", "error_message"]
                )
                deferred_count += 1
                continue
            if terminal_error:
                terminal_payload = redact_terminal_delivery_payload(
                    trigger=notification.trigger,
                    payload=notification.payload,
                )
                notification.status = ScheduledNotification.Status.FAILED
                notification.next_attempt_at = None
                notification.error_message = terminal_error
                update_fields = ["status", "next_attempt_at", "error_message"]
                if terminal_payload != notification.payload:
                    notification.payload = terminal_payload
                    update_fields.append("payload")
                notification.save(update_fields=update_fields)
                terminal_count += 1
                continue

            _ensure_notification_dispatch_identity(notification)
            if (
                notification.business_idempotency_key
                and notification_logs_for_business_tenant(
                    tenants[notification.tenant_id]
                )
                .filter(
                    business_idempotency_key=notification.business_idempotency_key
                )
                .exists()
            ):
                notification.status = ScheduledNotification.Status.SENT
                notification.sent_at = now
                notification.next_attempt_at = None
                notification.error_message = ""
                terminal_payload = redact_terminal_delivery_payload(
                    trigger=notification.trigger,
                    payload=notification.payload,
                )
                update_fields = [
                    "status",
                    "sent_at",
                    "next_attempt_at",
                    "error_message",
                ]
                if terminal_payload != notification.payload:
                    notification.payload = terminal_payload
                    update_fields.append("payload")
                notification.save(
                    update_fields=update_fields
                )
                reconciled_count += 1
                continue
            if notification.attempt_count >= MAX_ENQUEUE_ATTEMPTS:
                notification.status = ScheduledNotification.Status.FAILED
                notification.next_attempt_at = None
                notification.error_message = "enqueue_attempts_exhausted"
                terminal_payload = redact_terminal_delivery_payload(
                    trigger=notification.trigger,
                    payload=notification.payload,
                )
                update_fields = ["status", "next_attempt_at", "error_message"]
                if terminal_payload != notification.payload:
                    notification.payload = terminal_payload
                    update_fields.append("payload")
                notification.save(update_fields=update_fields)
                terminal_count += 1
                continue

            payload = notification.payload

            already_daily_reserved = bool(
                notification.last_attempt_at
                and provider_day_start <= notification.last_attempt_at < provider_day_end
            )
            if (
                not already_daily_reserved
                and provider_daily_usage >= provider_daily_limit
            ):
                notification.next_attempt_at = provider_release_at
                notification.error_message = "provider_daily_dispatch_quota_deferred"
                notification.save(
                    update_fields=["next_attempt_at", "error_message"]
                )
                deferred_count += 1
                continue

            already_reserved = bool(
                notification.last_attempt_at
                and notification.last_attempt_at >= cutoff
            )
            tenant_usage = usage_by_tenant[notification.tenant_id]
            if not already_reserved and tenant_usage >= HOURLY_SEND_LIMIT:
                notification.next_attempt_at = release_at_by_tenant[
                    notification.tenant_id
                ]
                notification.error_message = "hourly_dispatch_quota_deferred"
                notification.save(
                    update_fields=["next_attempt_at", "error_message"]
                )
                deferred_count += 1
                continue

            notification.payload = payload
            notification.status = ScheduledNotification.Status.DISPATCHING
            notification.attempt_count += 1
            notification.last_attempt_at = now
            notification.next_attempt_at = None
            notification.error_message = ""
            notification.save(
                update_fields=[
                    "payload",
                    "status",
                    "attempt_count",
                    "last_attempt_at",
                    "next_attempt_at",
                    "error_message",
                ]
            )
            if not already_reserved:
                usage_by_tenant[notification.tenant_id] += 1
            if not already_daily_reserved:
                provider_daily_usage += 1
            claims.append(
                _DispatchClaim(
                    notification_id=notification.id,
                    business_tenant_id=notification.tenant_id,
                    trigger=notification.trigger,
                    payload=payload,
                    attempt_count=notification.attempt_count,
                    claimed_at=now,
                )
            )
    return claims, terminal_count, deferred_count, reconciled_count


def _clinic_participant_reminder_is_stale(notification) -> bool:
    if notification.trigger != "clinic_reminder":
        return False
    origin_id = str(notification.origin_id or "")
    payload = getattr(notification, "payload", None)
    if isinstance(payload, dict) and str(payload.get("domain_object_id") or "").startswith("clinic_booking:"):
        # Explicit observer copies keep their own provenance but the same booking.
        origin_id = str(payload["domain_object_id"])
    if origin_id.startswith("clinic_booking:"):
        from apps.domains.clinic.contracts import is_clinic_booking_reminder_active

        return not is_clinic_booking_reminder_active(
            tenant_id=notification.tenant_id, origin_id=origin_id,
        )
    prefix = "clinic_participant:"
    marker = ":manual_reminder:"
    if not origin_id.startswith(prefix) or marker not in origin_id:
        return False
    participant_text = origin_id[len(prefix):].split(marker, 1)[0]
    try:
        participant_id = int(participant_text)
    except (TypeError, ValueError):
        return True

    from apps.domains.clinic.contracts import is_clinic_participant_reminder_active

    return not is_clinic_participant_reminder_active(
        participant_id=participant_id,
        tenant_id=notification.tenant_id,
    )


def process_due_notifications(
    batch_size: int = 100,
    *,
    notification_ids: list[int] | None = None,
) -> dict:
    """
    send_at <= now이고 status=pending인 알림을 처리.
    management command에서 주기적으로 호출.

    Returns:
        {"processed": int, "sent": int, "retried": int, "failed": int}
    """
    from apps.domains.messaging.models import ScheduledNotification
    from apps.domains.messaging.services import enqueue_alimtalk

    now = dj_tz.now()
    claims, terminal_count, deferred_count, reconciled_count = _claim_due_notifications(
        batch_size=batch_size,
        now=now,
        notification_ids=notification_ids,
    )
    stats = {
        "processed": len(claims) + terminal_count + reconciled_count,
        "sent": reconciled_count,
        "retried": 0,
        "failed": terminal_count,
        "deferred": deferred_count,
    }

    # 외부 SQS 호출을 DB transaction/row lock 바깥에서 수행한다. dispatching
    # 상태가 프로세스 crash를 기록하며, stale claim은 같은 occurrence_key로만 재시도한다.
    for claim in claims:
        terminal_error = _terminal_payload_error(
            claim.payload,
            trigger=claim.trigger,
        )
        try:
            if terminal_error:
                raise ValueError(terminal_error)
            enqueue_payload = dict(claim.payload)
            enqueue_payload.pop("recipient_fingerprint", None)
            enqueued = enqueue_alimtalk(
                **enqueue_payload,
                trusted_business_tenant_id=claim.business_tenant_id,
            )
            if not enqueued:
                raise RuntimeError("enqueue_alimtalk returned false")
        except Exception as exc:
            from apps.domains.messaging.policy import MessagingPolicyError

            is_terminal = bool(terminal_error) or isinstance(exc, MessagingPolicyError)
            if claim.attempt_count >= MAX_ENQUEUE_ATTEMPTS:
                is_terminal = True
            error_message = str(exc)[:500]
            if claim.attempt_count >= MAX_ENQUEUE_ATTEMPTS and not terminal_error:
                error_message = f"enqueue_attempts_exhausted: {error_message}"[:500]

            update_values = {
                "error_message": error_message,
                "next_attempt_at": None,
            }
            if is_terminal:
                update_values["status"] = ScheduledNotification.Status.FAILED
                terminal_payload = redact_terminal_delivery_payload(
                    trigger=claim.trigger,
                    payload=claim.payload,
                )
                if terminal_payload != claim.payload:
                    update_values["payload"] = terminal_payload
            else:
                update_values["status"] = ScheduledNotification.Status.PENDING
                update_values["next_attempt_at"] = now + _retry_delay(
                    claim.attempt_count
                )
            updated = ScheduledNotification.objects.filter(
                id=claim.notification_id,
                status=ScheduledNotification.Status.DISPATCHING,
                last_attempt_at=claim.claimed_at,
            ).update(**update_values)
            if updated:
                stats["failed" if is_terminal else "retried"] += 1
            logger.error(
                "process_due_notifications: %s notif_id=%s trigger=%s attempt=%s error=%s",
                "terminal failure" if is_terminal else "retry scheduled",
                claim.notification_id,
                claim.trigger,
                claim.attempt_count,
                exc,
            )
            continue

        terminal_payload = redact_terminal_delivery_payload(
            trigger=claim.trigger,
            payload=claim.payload,
        )
        update_values = {
            "status": ScheduledNotification.Status.SENT,
            "sent_at": dj_tz.now(),
            "next_attempt_at": None,
            "error_message": "",
        }
        if terminal_payload != claim.payload:
            update_values["payload"] = terminal_payload
        updated = ScheduledNotification.objects.filter(
            id=claim.notification_id,
            status=ScheduledNotification.Status.DISPATCHING,
            last_attempt_at=claim.claimed_at,
        ).update(**update_values)
        if updated:
            stats["sent"] += 1

    if stats["processed"]:
        logger.info("process_due_notifications: %s", stats)
    return stats
