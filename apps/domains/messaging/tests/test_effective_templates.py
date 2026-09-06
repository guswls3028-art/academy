from __future__ import annotations

from django.test import TestCase, override_settings
from django.core.exceptions import ValidationError

from apps.core.models import Tenant
from apps.domains.messaging.effective_templates import (
    prime_effective_owner_templates,
    resolve_effective_template_status,
)
from apps.domains.messaging.models import AutoSendConfig, MessageTemplate
from apps.domains.messaging.serializers import AutoSendConfigSerializer


class EffectiveTemplateStatusTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            code="effective-msg",
            name="Effective Messaging",
            is_active=True,
        )
        self.owner_settings = override_settings(OWNER_TENANT_ID=self.tenant.id)
        self.owner_settings.enable()
        self.addCleanup(self.owner_settings.disable)

    def test_unified_trigger_is_effectively_approved_even_if_linked_template_pending(self):
        template = MessageTemplate.objects.create(
            tenant=self.tenant,
            name="Clinic reminder body",
            category=MessageTemplate.Category.DEFAULT,
            subject="Clinic",
            body="Clinic #{student}",
            solapi_template_id="legacy-pending",
            solapi_status="PENDING",
        )
        config = AutoSendConfig.objects.create(
            tenant=self.tenant,
            trigger="clinic_reminder",
            template=template,
            enabled=True,
            message_mode="alimtalk",
        )

        effective = resolve_effective_template_status(config)
        data = AutoSendConfigSerializer(config).data

        self.assertTrue(effective.is_approved)
        self.assertEqual(effective.source, "unified")
        self.assertEqual(effective.template_type, "clinic_info")
        self.assertEqual(data["template_solapi_status"], "PENDING")
        self.assertEqual(data["effective_template_solapi_status"], "APPROVED")
        self.assertEqual(data["effective_template_source"], "unified")
        self.assertTrue(data["effective_template_is_approved"])
        self.assertEqual(data["effective_template_type"], "clinic_info")

    def test_non_unified_trigger_does_not_use_linked_provider_template(self):
        template = MessageTemplate.objects.create(
            tenant=self.tenant,
            name="Matchup report",
            category=MessageTemplate.Category.DEFAULT,
            subject="Matchup",
            body="Matchup #{student}",
            solapi_template_id="tenant-pending",
            solapi_status="PENDING",
        )
        config = AutoSendConfig.objects.create(
            tenant=self.tenant,
            trigger="matchup_report_submitted",
            template=template,
            enabled=True,
            message_mode="alimtalk",
        )

        effective = resolve_effective_template_status(config)

        self.assertFalse(effective.is_approved)
        self.assertEqual(effective.source, "provider_contract_missing")
        self.assertEqual(effective.template_type, "")
        self.assertEqual(effective.solapi_template_id, "")
        self.assertEqual(effective.solapi_status, "")

    def test_mapped_trigger_without_provider_sid_does_not_fallback_to_linked_template(self):
        template = MessageTemplate.objects.create(
            tenant=self.tenant,
            name="Stale payment notice",
            category=MessageTemplate.Category.PAYMENT,
            subject="Payment",
            body="Payment",
            solapi_template_id="STALE-PAYMENT-SID",
            solapi_status="APPROVED",
        )
        config = AutoSendConfig.objects.create(
            tenant=self.tenant,
            trigger="payment_complete",
            template=template,
            enabled=True,
            message_mode="alimtalk",
        )

        effective = resolve_effective_template_status(config)

        self.assertFalse(effective.is_approved)
        self.assertEqual(effective.source, "unified_missing")
        self.assertEqual(effective.template_type, "notice_payment")
        self.assertEqual(effective.solapi_template_id, "")

    def test_provider_fallback_is_not_loaded_before_serialization(self):
        owner_template = MessageTemplate.objects.create(
            tenant=self.tenant,
            name="Owner exact notice",
            category=MessageTemplate.Category.DEFAULT,
            body="Owner exact notice",
            solapi_template_id="OWNER-EXACT-SID",
            solapi_status="APPROVED",
        )
        AutoSendConfig.objects.create(
            tenant=self.tenant,
            trigger="matchup_report_submitted",
            template=owner_template,
            enabled=True,
            message_mode="alimtalk",
        )
        child_tenant = Tenant.objects.create(
            code="effective-msg-child",
            name="Effective Messaging Child",
            is_active=True,
        )
        child_config = AutoSendConfig.objects.create(
            tenant=child_tenant,
            trigger="matchup_report_submitted",
            enabled=True,
            message_mode="alimtalk",
        )

        with self.assertNumQueries(0):
            primed = prime_effective_owner_templates([child_config])
        with self.assertNumQueries(0):
            effective = resolve_effective_template_status(primed[0])

        self.assertFalse(effective.is_approved)
        self.assertEqual(effective.source, "content_template_missing")
        self.assertEqual(effective.solapi_template_id, "")

    def test_cross_tenant_content_template_fails_model_and_runtime_contract(self):
        other_tenant = Tenant.objects.create(
            code="effective-msg-other",
            name="Other Messaging",
            is_active=True,
        )
        foreign_template = MessageTemplate.objects.create(
            tenant=other_tenant,
            name="Foreign content",
            category=MessageTemplate.Category.ATTENDANCE,
            body="Foreign",
        )
        config = AutoSendConfig(
            tenant=self.tenant,
            trigger="check_in_complete",
            template=foreign_template,
            enabled=True,
            message_mode="alimtalk",
        )

        with self.assertRaises(ValidationError):
            config.full_clean()
        config.save()
        effective = resolve_effective_template_status(config)
        self.assertFalse(effective.is_approved)
        self.assertEqual(effective.source, "content_template_tenant_mismatch")
