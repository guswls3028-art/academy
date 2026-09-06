import hashlib
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from apps.core.models import Tenant
from apps.domains.messaging.models import AutoSendConfig, MessageTemplate


class DeleteExactMessageTemplateCommandTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            code="delete-letter",
            name="Delete Letter",
            is_active=True,
        )
        self.template = MessageTemplate.objects.create(
            tenant=self.tenant,
            category=MessageTemplate.Category.GRADES,
            name="잘못 저장된 성적 문구",
            body="잘못된 자동 선택 문구",
            is_system=False,
            is_user_default=False,
        )

    def command_options(self):
        self.template.refresh_from_db()
        return {
            "tenant_code": self.tenant.code,
            "template_id": self.template.id,
            "expected_name": self.template.name,
            "expected_category": self.template.category,
            "expected_is_system": "false",
            "expected_is_user_default": "false",
            "expected_solapi_template_id": "",
            "expected_solapi_status": "",
            "expected_created_at": self.template.created_at.isoformat(),
            "expected_updated_at": self.template.updated_at.isoformat(),
            "expected_body_sha256": hashlib.sha256(
                self.template.body.encode("utf-8")
            ).hexdigest(),
            "expected_global_body_match_count": 1,
        }

    def test_plan_is_read_only_and_reports_zero_references(self):
        stdout = StringIO()
        call_command(
            "delete_exact_message_template",
            stdout=stdout,
            **self.command_options(),
        )

        self.assertTrue(MessageTemplate.objects.filter(pk=self.template.id).exists())
        self.assertIn("plan tenant=delete-letter", stdout.getvalue())
        self.assertIn("refs=auto:0,scheduled:0,preview:0", stdout.getvalue())

    def test_any_reference_blocks_delete(self):
        AutoSendConfig.objects.create(
            tenant=self.tenant,
            trigger=AutoSendConfig.Trigger.EXAM_SCORE_PUBLISHED,
            template=self.template,
        )

        with self.assertRaisesMessage(CommandError, "template_referenced:auto=1"):
            call_command(
                "delete_exact_message_template",
                apply=True,
                **self.command_options(),
            )
        self.assertTrue(MessageTemplate.objects.filter(pk=self.template.id).exists())

    def test_apply_hard_deletes_only_the_exact_unreferenced_row(self):
        stdout = StringIO()
        call_command(
            "delete_exact_message_template",
            apply=True,
            stdout=stdout,
            **self.command_options(),
        )

        self.assertFalse(MessageTemplate.objects.filter(pk=self.template.id).exists())
        self.assertIn("deleted tenant=delete-letter", stdout.getvalue())
        self.assertIn("post_read=0", stdout.getvalue())

    def test_cas_mismatch_fails_without_delete(self):
        options = self.command_options()
        options["expected_name"] = "다른 이름"

        with self.assertRaisesMessage(CommandError, "template_cas_mismatch:name"):
            call_command("delete_exact_message_template", apply=True, **options)
        self.assertTrue(MessageTemplate.objects.filter(pk=self.template.id).exists())
