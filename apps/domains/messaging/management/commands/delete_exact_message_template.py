import hashlib

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils.dateparse import parse_datetime

from apps.core.models import Tenant
from apps.domains.messaging.models import (
    AutoSendConfig,
    MessageTemplate,
    NotificationPreviewToken,
    ScheduledNotification,
)


def _parse_expected_bool(value: str) -> bool:
    normalized = str(value or "").strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise CommandError("expected_boolean_must_be_true_or_false")


class Command(BaseCommand):
    help = "CAS-delete one exact unreferenced saved message letter."

    def add_arguments(self, parser):
        parser.add_argument("--tenant-code", required=True)
        parser.add_argument("--template-id", required=True, type=int)
        parser.add_argument("--expected-name", required=True)
        parser.add_argument("--expected-category", required=True)
        parser.add_argument("--expected-is-system", required=True)
        parser.add_argument("--expected-is-user-default", required=True)
        parser.add_argument("--expected-solapi-template-id", required=True)
        parser.add_argument("--expected-solapi-status", required=True)
        parser.add_argument("--expected-created-at", required=True)
        parser.add_argument("--expected-updated-at", required=True)
        parser.add_argument("--expected-body-sha256", required=True)
        parser.add_argument("--expected-global-body-match-count", required=True, type=int)
        parser.add_argument("--apply", action="store_true")

    @transaction.atomic
    def handle(self, *args, **options):
        tenant = Tenant.objects.filter(code=options["tenant_code"]).first()
        if tenant is None:
            raise CommandError("tenant_not_found")

        template = (
            MessageTemplate.objects.select_for_update()
            .filter(tenant=tenant, pk=options["template_id"])
            .first()
        )
        if template is None:
            raise CommandError("template_not_found")

        expected_created_at = parse_datetime(options["expected_created_at"])
        expected_updated_at = parse_datetime(options["expected_updated_at"])
        if expected_created_at is None or expected_updated_at is None:
            raise CommandError("expected_timestamp_invalid")

        actual_sha = hashlib.sha256((template.body or "").encode("utf-8")).hexdigest()
        expected_sha = options["expected_body_sha256"].strip().lower()
        expected_fields = {
            "name": options["expected_name"],
            "category": options["expected_category"],
            "is_system": _parse_expected_bool(options["expected_is_system"]),
            "is_user_default": _parse_expected_bool(
                options["expected_is_user_default"]
            ),
            "solapi_template_id": options["expected_solapi_template_id"],
            "solapi_status": options["expected_solapi_status"],
            "created_at": expected_created_at,
            "updated_at": expected_updated_at,
            "body_sha256": expected_sha,
        }
        actual_fields = {
            "name": template.name,
            "category": template.category,
            "is_system": template.is_system,
            "is_user_default": template.is_user_default,
            "solapi_template_id": template.solapi_template_id,
            "solapi_status": template.solapi_status,
            "created_at": template.created_at,
            "updated_at": template.updated_at,
            "body_sha256": actual_sha,
        }
        mismatches = [
            field
            for field, expected in expected_fields.items()
            if actual_fields[field] != expected
        ]
        if mismatches:
            raise CommandError(f"template_cas_mismatch:{','.join(mismatches)}")

        global_body_matches = sum(
            1
            for body in MessageTemplate.objects.filter(
                category=MessageTemplate.Category.GRADES
            ).values_list("body", flat=True)
            if hashlib.sha256((body or "").encode("utf-8")).hexdigest()
            == expected_sha
        )
        if global_body_matches != options["expected_global_body_match_count"]:
            raise CommandError(
                "global_body_match_count_mismatch:"
                f"expected={options['expected_global_body_match_count']}:"
                f"actual={global_body_matches}"
            )

        template_id = template.id
        auto_refs = AutoSendConfig.objects.filter(template_id=template_id).count()
        scheduled_refs = ScheduledNotification.objects.filter(
            Q(payload__content_template_id=template_id)
            | Q(payload__template_id=template_id)
            | Q(payload__template_id=str(template_id))
        ).count()
        preview_refs = NotificationPreviewToken.objects.filter(
            Q(payload__content_template_id=template_id)
            | Q(payload__template_id=template_id)
            | Q(payload__template_id=str(template_id))
            | Q(payload__identity__content_template_id=template_id)
        ).count()
        if auto_refs or scheduled_refs or preview_refs:
            raise CommandError(
                "template_referenced:"
                f"auto={auto_refs}:scheduled={scheduled_refs}:preview={preview_refs}"
            )

        summary = (
            f"tenant={tenant.code} template_id={template_id} "
            f"body_sha256={actual_sha} matches={global_body_matches} "
            f"refs=auto:{auto_refs},scheduled:{scheduled_refs},preview:{preview_refs}"
        )
        if not options["apply"]:
            self.stdout.write(f"plan {summary}")
            return

        deleted_count, _ = template.delete()
        if deleted_count != 1:
            raise CommandError(f"unexpected_delete_count:{deleted_count}")
        if MessageTemplate.objects.filter(pk=template_id).exists():
            raise CommandError("template_delete_post_read_failed")
        self.stdout.write(self.style.SUCCESS(f"deleted {summary} post_read=0"))
