# apps/support/messaging/selectors.py

from datetime import timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from apps.domains.messaging.models import AutoSendConfig, NotificationLog
from apps.domains.messaging.scheduled import HOURLY_SEND_LIMIT

KST = ZoneInfo("Asia/Seoul")
DEFAULT_PROVIDER_DAILY_DISPATCH_LIMIT = 900


def get_provider_daily_dispatch_limit() -> int:
    """Return the provider-account KST calendar-day safety ceiling."""

    return max(
        1,
        int(
            getattr(
                settings,
                "MESSAGING_PROVIDER_DAILY_DISPATCH_LIMIT",
                DEFAULT_PROVIDER_DAILY_DISPATCH_LIMIT,
            )
        ),
    )


def get_provider_day_window(*, now=None):
    """Return the current provider quota window as timezone-aware KST bounds."""

    current = (now or timezone.now()).astimezone(KST)
    start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def get_provider_daily_notification_usage(*, now=None) -> int:
    """Count global outbox reservations plus disjoint legacy provider attempts."""

    from apps.domains.messaging.models import ScheduledNotification

    start, end = get_provider_day_window(now=now)
    outbox_qs = ScheduledNotification.objects.filter(
        last_attempt_at__gte=start,
        last_attempt_at__lt=end,
    )
    outbox_keys = list(
        outbox_qs.exclude(business_idempotency_key="").values_list(
            "business_idempotency_key",
            flat=True,
        )
    )
    legacy_log_count = (
        NotificationLog.objects.filter(sent_at__gte=start, sent_at__lt=end)
        .exclude(business_idempotency_key__in=outbox_keys)
        .count()
    )
    return outbox_qs.count() + legacy_log_count


def notification_logs_for_business_tenant(tenant):
    """Return logs owned by the tenant that caused the business event.

    Provider delivery is normalized to the common owner tenant.  For those
    proxy sends ``source_tenant`` is the sole business owner; a physical owner
    tenant must not gain visibility merely because it supplied the channel.
    """
    tenant_id = int(getattr(tenant, "id", tenant))
    return NotificationLog.objects.filter(
        Q(source_tenant_id=tenant_id)
        | Q(source_tenant_id__isnull=True, tenant_id=tenant_id)
    )


def get_hourly_notification_usage(tenant, *, now=None) -> int:
    """Count dispatch attempts plus legacy direct-send logs in the rolling hour."""
    from apps.domains.messaging.models import ScheduledNotification

    cutoff = (now or timezone.now()) - timedelta(hours=1)
    tenant_id = int(getattr(tenant, "id", tenant))
    outbox_count = ScheduledNotification.objects.filter(
        tenant_id=tenant_id,
        last_attempt_at__gte=cutoff,
    ).count()
    outbox_keys = list(
        ScheduledNotification.objects.filter(
            tenant_id=tenant_id,
            last_attempt_at__gte=cutoff,
        )
        .exclude(business_idempotency_key="")
        .values_list("business_idempotency_key", flat=True)
    )
    legacy_log_count = (
        notification_logs_for_business_tenant(tenant)
        .filter(sent_at__gte=cutoff)
        .exclude(business_idempotency_key__in=outbox_keys)
        .count()
    )
    return outbox_count + legacy_log_count


def get_auto_send_config(tenant_id: int, trigger: str) -> Optional[AutoSendConfig]:
    """테넌트·트리거별 자동발송 설정 조회.
    enabled 여부와 무관하게 config 행 자체를 반환.
    → 호출자가 config.enabled를 체크하여 비활성이면 스킵.
    → config 행이 존재하면(비활성 포함) 호출자가 그 상태를 그대로 존중한다.
    """
    return AutoSendConfig.objects.filter(
        tenant_id=tenant_id,
        trigger=trigger,
    ).select_related("template").order_by("id").first()


def get_all_auto_send_configs(tenant_id: int) -> dict[str, Optional[AutoSendConfig]]:
    """테넌트의 모든 자동발송 설정 조회. trigger -> config."""
    configs = AutoSendConfig.objects.filter(tenant_id=tenant_id).select_related("template")
    return {c.trigger: c for c in configs}
