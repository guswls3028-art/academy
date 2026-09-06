from __future__ import annotations

from collections import Counter

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.core.models import Tenant
from apps.domains.messaging.models import (
    AlimtalkChannelBinding,
    AlimtalkTemplateBinding,
)
from apps.domains.messaging.solapi_template_client import (
    clone_kakao_template,
    list_kakao_templates,
    request_kakao_template_inspection,
)
from apps.domains.messaging.tenant_channels import (
    build_template_fingerprint,
    required_common_template_ids,
)


class Command(BaseCommand):
    help = "Verify and configure a tenant Kakao channel on the shared Solapi account."

    def add_arguments(self, parser):
        parser.add_argument("--tenant-code", required=True)
        parser.add_argument("--channel-id", required=True)
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--activate-if-ready", action="store_true")

    def handle(self, *args, **options):
        tenant_code = options["tenant_code"].strip()
        channel_id = options["channel_id"].strip()
        apply = bool(options["apply"])
        if not tenant_code or not channel_id:
            raise CommandError("tenant-code and channel-id are required")

        tenants = list(Tenant.objects.filter(code=tenant_code)[:2])
        if len(tenants) != 1:
            raise CommandError(f"tenant_match_count={len(tenants)}")
        tenant = tenants[0]
        if not tenant.is_active:
            raise CommandError("tenant_inactive")

        api_key = str(getattr(settings, "SOLAPI_API_KEY", "") or "").strip()
        api_secret = str(getattr(settings, "SOLAPI_API_SECRET", "") or "").strip()
        common_channel_id = str(
            getattr(settings, "SOLAPI_KAKAO_PF_ID", "") or ""
        ).strip()
        if not api_key or not api_secret or not common_channel_id:
            raise CommandError("common_solapi_configuration_missing")
        if channel_id == common_channel_id:
            raise CommandError("tenant_channel_must_differ_from_common_channel")

        source_templates = list_kakao_templates(
            api_key,
            api_secret,
            common_channel_id,
        )
        destination_templates = list_kakao_templates(
            api_key,
            api_secret,
            channel_id,
        )
        required_ids = required_common_template_ids()
        source_by_id = {
            str(item.get("templateId") or item.get("id") or "").strip(): item
            for item in source_templates
        }
        missing_source = [item for item in required_ids if item not in source_by_id]
        if missing_source:
            raise CommandError(
                f"common_provider_templates_missing={len(missing_source)}"
            )
        unapproved_source = [
            item
            for item in required_ids
            if str(source_by_id[item].get("status") or "").upper() != "APPROVED"
        ]
        if unapproved_source:
            raise CommandError(
                f"common_provider_templates_unapproved={len(unapproved_source)}"
            )

        existing = AlimtalkChannelBinding.objects.filter(tenant=tenant).first()
        if existing and existing.channel_id != channel_id:
            raise CommandError("tenant_already_bound_to_different_channel")
        other_tenant = AlimtalkChannelBinding.objects.filter(
            channel_id=channel_id,
        ).exclude(tenant=tenant).exists()
        if other_tenant:
            raise CommandError("channel_already_bound_to_another_tenant")

        created = 0
        inspected = 0
        destination_by_fingerprint = self._destination_by_fingerprint(
            destination_templates
        )
        for source_id in required_ids:
            source = source_by_id[source_id]
            fingerprint = build_template_fingerprint(source)
            destination = destination_by_fingerprint.get(fingerprint)
            if destination is None and apply:
                destination = clone_kakao_template(
                    api_key=api_key,
                    api_secret=api_secret,
                    channel_id=channel_id,
                    source_template=source,
                    name=self._tenant_template_name(tenant.name, source),
                )
                created += 1
                destination_by_fingerprint[fingerprint] = destination

            if (
                apply
                and destination is not None
                and str(destination.get("status") or "").upper() == "PENDING"
            ):
                destination_id = str(
                    destination.get("templateId") or destination.get("id") or ""
                ).strip()
                if not destination_id:
                    raise CommandError("provider_clone_missing_template_id")
                inspected_template = request_kakao_template_inspection(
                    api_key=api_key,
                    api_secret=api_secret,
                    template_id=destination_id,
                    comment="학원 운영 알림톡 전용 템플릿입니다.",
                )
                inspected += 1
                destination_by_fingerprint[fingerprint] = (
                    inspected_template or destination
                )

        if apply:
            destination_templates = list_kakao_templates(
                api_key,
                api_secret,
                channel_id,
            )
            destination_by_fingerprint = self._destination_by_fingerprint(
                destination_templates
            )

        mappings = []
        for source_id in required_ids:
            source = source_by_id[source_id]
            fingerprint = build_template_fingerprint(source)
            destination = destination_by_fingerprint.get(fingerprint)
            if destination:
                mappings.append((source_id, fingerprint, destination))

        approved = sum(
            1
            for _, _, item in mappings
            if str(item.get("status") or "").upper() == "APPROVED"
        )
        status_counts = Counter(
            str(item.get("status") or "UNKNOWN").upper()
            for _, _, item in mappings
        )
        test_template = self._select_test_template(destination_templates)

        if apply:
            now = timezone.now()
            with transaction.atomic():
                channel, _ = AlimtalkChannelBinding.objects.update_or_create(
                    tenant=tenant,
                    defaults={
                        "channel_id": channel_id,
                        "status": (
                            AlimtalkChannelBinding.Status.ACTIVE
                            if options["activate_if_ready"]
                            and approved == len(required_ids)
                            else AlimtalkChannelBinding.Status.PENDING_TEMPLATES
                        ),
                        "verified_at": now,
                        "last_synced_at": now,
                        "test_template_id": (
                            str(test_template.get("templateId") or "").strip()
                            if test_template
                            else ""
                        ),
                        "test_template_fingerprint": (
                            build_template_fingerprint(test_template)
                            if test_template
                            else ""
                        ),
                    },
                )
                required_set = set(required_ids)
                channel.template_bindings.exclude(
                    source_template_id__in=required_set
                ).delete()
                for source_id, fingerprint, destination in mappings:
                    AlimtalkTemplateBinding.objects.update_or_create(
                        channel=channel,
                        source_template_id=source_id,
                        defaults={
                            "channel_template_id": str(
                                destination.get("templateId")
                                or destination.get("id")
                                or ""
                            ).strip(),
                            "status": str(
                                destination.get("status") or ""
                            ).upper(),
                            "source_fingerprint": fingerprint,
                            "channel_fingerprint": build_template_fingerprint(
                                destination
                            ),
                            "last_synced_at": now,
                        },
                    )

        mode = "APPLY" if apply else "DRY_RUN"
        self.stdout.write(
            " ".join(
                [
                    f"mode={mode}",
                    f"tenant_id={tenant.id}",
                    "channel_verified=1",
                    f"required={len(required_ids)}",
                    f"matched={len(mappings)}",
                    f"approved={approved}",
                    f"created={created}",
                    f"inspection_requested={inspected}",
                    f"test_template_ready={int(test_template is not None)}",
                    "statuses="
                    + ",".join(
                        f"{key}:{status_counts[key]}"
                        for key in sorted(status_counts)
                    ),
                ]
            )
        )

    @staticmethod
    def _destination_by_fingerprint(templates: list[dict]) -> dict[str, dict]:
        ranked = sorted(
            templates,
            key=lambda item: (
                str(item.get("status") or "").upper() != "APPROVED",
                str(item.get("templateId") or item.get("id") or ""),
            ),
        )
        return {
            build_template_fingerprint(item): item
            for item in reversed(ranked)
        }

    @staticmethod
    def _tenant_template_name(tenant_name: str, source: dict) -> str:
        source_name = str(source.get("name") or "알림톡 안내").strip()
        return f"[{tenant_name.strip()}] {source_name}"[:100]

    @staticmethod
    def _select_test_template(templates: list[dict]) -> dict | None:
        candidates = []
        for item in templates:
            variables = {
                str(value.get("name") or "").strip()
                for value in item.get("variables") or []
                if isinstance(value, dict)
            }
            if (
                str(item.get("status") or "").upper() == "APPROVED"
                and variables == {"인증번호"}
                and str(item.get("messageType") or "BA").upper() == "BA"
                and str(item.get("emphasizeType") or "NONE").upper() == "NONE"
            ):
                candidates.append(item)
        if not candidates:
            return None
        return sorted(
            candidates,
            key=lambda item: (
                str(item.get("name") or "") != "인증번호",
                str(item.get("templateId") or item.get("id") or ""),
            ),
        )[0]
