from __future__ import annotations

import hashlib
import secrets
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError
from django.utils import timezone

from apps.core.models import Tenant
from apps.domains.messaging.models import AlimtalkChannelBinding, NotificationLog
from apps.domains.messaging.policy import (
    check_recipient_allowed,
    get_owner_tenant_id,
    is_messaging_disabled,
)
from apps.domains.messaging.security import (
    build_recipient_fingerprint,
    normalize_recipient_phone,
)
from apps.domains.messaging.solapi_template_client import list_kakao_templates
from apps.domains.messaging.tenant_channels import build_template_fingerprint


class Command(BaseCommand):
    help = "Send one idempotent live Alimtalk through a verified tenant channel."

    def add_arguments(self, parser):
        parser.add_argument("--tenant-code", required=True)
        parser.add_argument("--to", required=True)
        parser.add_argument("--idempotency-key", required=True)
        parser.add_argument("--confirm-live", action="store_true")

    def handle(self, *args, **options):
        tenant_code = options["tenant_code"].strip()
        recipient = normalize_recipient_phone(options["to"])
        operator_key = options["idempotency_key"].strip()
        if len(recipient) not in {10, 11}:
            raise CommandError("invalid_recipient_format")
        if not operator_key or len(operator_key) > 128:
            raise CommandError("invalid_idempotency_key")

        tenants = list(Tenant.objects.filter(code=tenant_code)[:2])
        if len(tenants) != 1:
            raise CommandError(f"tenant_match_count={len(tenants)}")
        tenant = tenants[0]
        if not tenant.is_active or is_messaging_disabled(tenant.id):
            raise CommandError("tenant_messaging_unavailable")
        if not check_recipient_allowed(recipient):
            raise CommandError("recipient_blocked_by_policy")

        channel = AlimtalkChannelBinding.objects.filter(
            tenant=tenant,
            verified_at__isnull=False,
        ).first()
        if channel is None or not channel.test_template_id:
            raise CommandError("tenant_channel_test_template_unavailable")

        api_key = str(getattr(settings, "SOLAPI_API_KEY", "") or "").strip()
        api_secret = str(getattr(settings, "SOLAPI_API_SECRET", "") or "").strip()
        templates = list_kakao_templates(
            api_key,
            api_secret,
            channel.channel_id,
            status_filter="APPROVED",
        )
        live_template = next(
            (
                item
                for item in templates
                if str(item.get("templateId") or item.get("id") or "").strip()
                == channel.test_template_id
            ),
            None,
        )
        if (
            live_template is None
            or build_template_fingerprint(live_template)
            != channel.test_template_fingerprint
        ):
            raise CommandError("tenant_channel_test_template_drift")

        business_key = hashlib.sha256(
            f"tenant-channel-test:{tenant.id}:{operator_key}".encode("utf-8")
        ).hexdigest()
        owner_id = int(get_owner_tenant_id())
        existing = NotificationLog.objects.filter(
            tenant_id=owner_id,
            message_mode="alimtalk",
            business_idempotency_key=business_key,
        ).first()
        if existing:
            self.stdout.write(
                " ".join(
                    [
                        "idempotent_replay=1",
                        f"status={existing.status}",
                        f"success={int(existing.success)}",
                        f"provider_evidence={int(bool(existing.provider_message_id))}",
                    ]
                )
            )
            return

        if not options["confirm_live"]:
            self.stdout.write(
                "mode=DRY_RUN channel_verified=1 template_approved=1 recipient_allowed=1"
            )
            return

        now = timezone.now()
        try:
            log = NotificationLog.objects.create(
                tenant_id=owner_id,
                source_tenant=tenant,
                success=False,
                status="sending",
                claimed_at=now,
                amount_deducted=Decimal("0"),
                recipient_summary="연동 테스트 수신자",
                template_summary="개별 채널 연동 테스트",
                failure_reason="",
                message_body="",
                message_mode="alimtalk",
                business_idempotency_key=business_key,
                recipient_fingerprint=build_recipient_fingerprint(recipient),
                origin_type="tenant_channel_test",
                origin_id=operator_key,
                notification_type="channel_connection_test",
                target_type="tenant",
                target_id=str(tenant.id),
                target_name=tenant.name,
            )
        except IntegrityError:
            raise CommandError("channel_test_idempotency_conflict")

        from apps.worker.messaging_worker.config import load_config
        from apps.worker.messaging_worker.sqs_main import (
            _is_non_retryable_send_failure,
            send_one_alimtalk,
        )

        config = load_config()
        result = send_one_alimtalk(
            config,
            to=recipient,
            sender=config.SOLAPI_SENDER,
            pf_id=channel.channel_id,
            template_id=channel.test_template_id,
            replacements=[
                {
                    "key": "인증번호",
                    "value": f"{secrets.randbelow(1_000_000):06d}",
                }
            ],
            text="",
        )
        provider_id = str(result.get("group_id") or "").strip()
        if result.get("status") == "ok" and provider_id:
            log.status = "sent"
            log.success = True
            log.provider_message_id = provider_id
            channel.last_test_status = AlimtalkChannelBinding.TestStatus.SENT
        else:
            reason = str(result.get("reason") or "")
            definitely_rejected = _is_non_retryable_send_failure(reason)
            log.status = "failed" if definitely_rejected else "ambiguous"
            log.failure_reason = (
                "provider_rejected"
                if definitely_rejected
                else "provider_result_unconfirmed"
            )
            log.provider_message_id = provider_id
            channel.last_test_status = (
                AlimtalkChannelBinding.TestStatus.FAILED
                if definitely_rejected
                else AlimtalkChannelBinding.TestStatus.AMBIGUOUS
            )
        log.save(
            update_fields=[
                "status",
                "success",
                "provider_message_id",
                "failure_reason",
            ]
        )
        channel.last_tested_at = timezone.now()
        channel.save(
            update_fields=["last_test_status", "last_tested_at", "updated_at"]
        )
        self.stdout.write(
            " ".join(
                [
                    "mode=LIVE",
                    f"status={log.status}",
                    f"success={int(log.success)}",
                    f"provider_evidence={int(bool(log.provider_message_id))}",
                    "message_mode=alimtalk",
                    "sms_fallback=0",
                ]
            )
        )
        if not log.success:
            raise CommandError(log.failure_reason)
