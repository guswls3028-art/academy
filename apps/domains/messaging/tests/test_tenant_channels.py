from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from apps.core.models import Tenant
from apps.domains.messaging.models import (
    AlimtalkChannelBinding,
    AlimtalkTemplateBinding,
    NotificationLog,
)
from apps.domains.messaging.tenant_channels import (
    TenantAlimtalkRouteError,
    build_template_fingerprint,
    get_tenant_channel_status,
    resolve_alimtalk_delivery_route,
)
from academy.adapters.messaging.solapi_template_client import clone_kakao_template


def _template(template_id: str, *, status: str = "APPROVED", content: str = "안내"):
    return {
        "templateId": template_id,
        "name": "알림톡 안내",
        "status": status,
        "content": content,
        "categoryCode": "001001",
        "messageType": "BA",
        "emphasizeType": "NONE",
        "buttons": [],
        "quickReplies": [],
        "highlight": {},
        "item": {},
        "securityFlag": False,
    }


class SolapiTemplateCloneTests(SimpleTestCase):
    @patch("academy.adapters.messaging.solapi_template_client.requests.post")
    def test_clone_preserves_item_list_and_omits_empty_provider_defaults(self, post):
        post.return_value.status_code = 200
        post.return_value.json.return_value = {"templateId": "TENANT-TEMPLATE"}
        source = _template("SOURCE", content="#{선생님메모}")
        source.update({
            "emphasizeType": "ITEM_LIST",
            "header": "수업 안내",
            "highlight": {"title": "#{학원명}", "description": "#{학생이름}"},
            "item": {"list": [{"title": "일정", "description": "#{날짜}"}]},
            "imageId": None,
        })

        clone_kakao_template(
            api_key="key",
            api_secret="secret",
            channel_id="TENANT-CHANNEL",
            source_template=source,
            name="tenant copy",
        )

        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["channelId"], "TENANT-CHANNEL")
        self.assertEqual(payload["emphasizeType"], "ITEM_LIST")
        self.assertEqual(payload["header"], "수업 안내")
        self.assertEqual(payload["item"], source["item"])
        self.assertNotIn("imageId", payload)


class TenantChannelRoutingTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            code="tenant-channel",
            name="Tenant Channel",
            kakao_pfid="UNTRUSTED-LEGACY-PFID",
            messaging_provider="ppurio",
        )

    def _binding(self, status=AlimtalkChannelBinding.Status.ACTIVE):
        now = timezone.now()
        return AlimtalkChannelBinding.objects.create(
            tenant=self.tenant,
            channel_id="VERIFIED-CHANNEL",
            status=status,
            verified_at=now,
            last_synced_at=now,
        )

    def test_legacy_tenant_fields_never_change_delivery_route(self):
        route = resolve_alimtalk_delivery_route(
            source_tenant_id=self.tenant.id,
            common_channel_id="COMMON-CHANNEL",
            common_template_id="COMMON-TEMPLATE",
        )

        self.assertEqual(route.source, "common_owner")
        self.assertEqual(route.channel_id, "COMMON-CHANNEL")

    def test_pending_verified_channel_keeps_common_route(self):
        self._binding(AlimtalkChannelBinding.Status.PENDING_TEMPLATES)

        route = resolve_alimtalk_delivery_route(
            source_tenant_id=self.tenant.id,
            common_channel_id="COMMON-CHANNEL",
            common_template_id="COMMON-TEMPLATE",
        )

        self.assertEqual(route.source, "common_owner")

    def test_active_channel_requires_approved_exact_fingerprint_mapping(self):
        channel = self._binding()
        AlimtalkTemplateBinding.objects.create(
            channel=channel,
            source_template_id="COMMON-TEMPLATE",
            channel_template_id="TENANT-TEMPLATE",
            status="APPROVED",
            source_fingerprint="same",
            channel_fingerprint="same",
            last_synced_at=timezone.now(),
        )

        route = resolve_alimtalk_delivery_route(
            source_tenant_id=self.tenant.id,
            common_channel_id="COMMON-CHANNEL",
            common_template_id="COMMON-TEMPLATE",
        )

        self.assertEqual(route.source, "tenant_verified")
        self.assertEqual(route.channel_id, "VERIFIED-CHANNEL")
        self.assertEqual(route.template_id, "TENANT-TEMPLATE")

    def test_active_channel_fails_closed_when_mapping_is_missing(self):
        self._binding()

        with self.assertRaisesMessage(
            TenantAlimtalkRouteError,
            "tenant_channel_template_unavailable",
        ):
            resolve_alimtalk_delivery_route(
                source_tenant_id=self.tenant.id,
                common_channel_id="COMMON-CHANNEL",
                common_template_id="COMMON-TEMPLATE",
            )

    @patch(
        "apps.domains.messaging.tenant_channels.required_common_template_ids",
        return_value=("COMMON-TEMPLATE",),
    )
    def test_status_projection_distinguishes_pending_templates(self, _required):
        channel = self._binding(AlimtalkChannelBinding.Status.PENDING_TEMPLATES)
        channel.test_template_id = "TEST-TEMPLATE"
        channel.test_template_fingerprint = "f" * 64
        channel.save(update_fields=["test_template_id", "test_template_fingerprint"])

        status = get_tenant_channel_status(self.tenant.id)

        self.assertTrue(status["custom_channel_registered"])
        self.assertEqual(status["custom_channel_status"], "pending_templates")
        self.assertEqual(status["channel_source"], "tenant_pending")
        self.assertTrue(status["custom_channel_test_available"])
        self.assertNotIn("VERIFIED-CHANNEL", status["custom_channel_reference"])

    @patch(
        "apps.domains.messaging.tenant_channels.required_common_template_ids",
        return_value=("COMMON-TEMPLATE",),
    )
    def test_status_projection_suspends_an_active_channel_with_drift(self, _required):
        self._binding(AlimtalkChannelBinding.Status.ACTIVE)

        status = get_tenant_channel_status(self.tenant.id)

        self.assertEqual(status["custom_channel_status"], "suspended")
        self.assertEqual(status["channel_source"], "tenant_suspended")
        self.assertTrue(status["custom_channel_routing_blocked"])


@override_settings(
    SOLAPI_API_KEY="shared-key",
    SOLAPI_API_SECRET="shared-secret",
    SOLAPI_KAKAO_PF_ID="COMMON-CHANNEL",
    SOLAPI_SENDER="0212345678",
    OWNER_TENANT_ID=1,
    MESSAGING_TENANT_BINDING_KEY="test-binding-key",
)
class TenantChannelCommandTests(TestCase):
    def setUp(self):
        self.owner = Tenant.objects.create(id=1, code="owner", name="Owner")
        self.tenant = Tenant.objects.create(id=3, code="limglish", name="임근혁 영어")

    def test_worker_context_translates_only_at_the_provider_boundary(self):
        from apps.worker.messaging_worker.sqs_main import (
            _resolve_tenant_delivery_context,
        )

        now = timezone.now()
        channel = AlimtalkChannelBinding.objects.create(
            tenant=self.tenant,
            channel_id="NEW-CHANNEL",
            status="active",
            verified_at=now,
            last_synced_at=now,
        )
        AlimtalkTemplateBinding.objects.create(
            channel=channel,
            source_template_id="SOURCE-TEMPLATE",
            channel_template_id="TENANT-TEMPLATE",
            status="APPROVED",
            source_fingerprint="same",
            channel_fingerprint="same",
            last_synced_at=now,
        )

        context = _resolve_tenant_delivery_context(
            self.owner.id,
            self.tenant.id,
            "SOURCE-TEMPLATE",
        )

        self.assertEqual(context["billing_tenant_id"], self.tenant.id)
        self.assertEqual(context["channel"]["pf_id"], "NEW-CHANNEL")
        self.assertEqual(context["template_id"], "TENANT-TEMPLATE")
        self.assertEqual(context["channel_source"], "tenant_verified")

    @patch(
        "apps.domains.messaging.management.commands.configure_tenant_alimtalk_channel.required_common_template_ids",
        return_value=("SOURCE-TEMPLATE",),
    )
    @patch(
        "apps.domains.messaging.management.commands.configure_tenant_alimtalk_channel.list_kakao_templates"
    )
    def test_dry_run_does_not_trust_or_persist_legacy_fields(self, list_templates, _required):
        self.tenant.kakao_pfid = "LEGACY"
        self.tenant.messaging_provider = "ppurio"
        self.tenant.save(update_fields=["kakao_pfid", "messaging_provider"])
        source = _template("SOURCE-TEMPLATE")
        list_templates.side_effect = [[source], []]

        call_command(
            "configure_tenant_alimtalk_channel",
            tenant_code="limglish",
            channel_id="NEW-CHANNEL",
        )

        self.assertFalse(AlimtalkChannelBinding.objects.exists())
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.kakao_pfid, "LEGACY")
        self.assertEqual(self.tenant.messaging_provider, "ppurio")

    @patch(
        "apps.domains.messaging.management.commands.configure_tenant_alimtalk_channel.required_common_template_ids",
        return_value=("SOURCE-TEMPLATE",),
    )
    @patch(
        "apps.domains.messaging.management.commands.configure_tenant_alimtalk_channel.list_kakao_templates"
    )
    def test_configuration_rejects_an_unapproved_common_source(
        self,
        list_templates,
        _required,
    ):
        list_templates.side_effect = [
            [_template("SOURCE-TEMPLATE", status="REJECTED")],
            [],
        ]

        with self.assertRaisesMessage(
            CommandError,
            "common_provider_templates_unapproved=1",
        ):
            call_command(
                "configure_tenant_alimtalk_channel",
                tenant_code="limglish",
                channel_id="NEW-CHANNEL",
            )

    @patch(
        "apps.domains.messaging.management.commands.configure_tenant_alimtalk_channel.required_common_template_ids",
        return_value=("SOURCE-TEMPLATE",),
    )
    @patch(
        "apps.domains.messaging.management.commands.configure_tenant_alimtalk_channel.request_kakao_template_inspection"
    )
    @patch(
        "apps.domains.messaging.management.commands.configure_tenant_alimtalk_channel.clone_kakao_template"
    )
    @patch(
        "apps.domains.messaging.management.commands.configure_tenant_alimtalk_channel.list_kakao_templates"
    )
    def test_apply_clones_requests_review_and_persists_pending_binding(
        self,
        list_templates,
        clone_template,
        request_inspection,
        _required,
    ):
        source = _template("SOURCE-TEMPLATE", content="정본 안내")
        pending = _template("TENANT-TEMPLATE", status="INSPECTING", content="정본 안내")
        test_template = _template("TEST-TEMPLATE", content="인증번호 #{인증번호}")
        test_template.update({"name": "인증번호", "variables": [{"name": "인증번호"}]})
        list_templates.side_effect = [[source], [test_template], [pending, test_template]]
        clone_template.return_value = _template(
            "TENANT-TEMPLATE", status="PENDING", content="정본 안내"
        )
        request_inspection.return_value = pending

        call_command(
            "configure_tenant_alimtalk_channel",
            tenant_code="limglish",
            channel_id="NEW-CHANNEL",
            apply=True,
            activate_if_ready=True,
        )

        channel = AlimtalkChannelBinding.objects.get(tenant=self.tenant)
        self.assertEqual(channel.status, "pending_templates")
        self.assertEqual(channel.test_template_id, "TEST-TEMPLATE")
        mapping = channel.template_bindings.get(source_template_id="SOURCE-TEMPLATE")
        self.assertEqual(mapping.channel_template_id, "TENANT-TEMPLATE")
        self.assertEqual(mapping.status, "INSPECTING")
        clone_template.assert_called_once()
        request_inspection.assert_called_once()

    @patch(
        "apps.domains.messaging.management.commands.configure_tenant_alimtalk_channel.required_common_template_ids",
        return_value=("SOURCE-TEMPLATE",),
    )
    @patch(
        "apps.domains.messaging.management.commands.configure_tenant_alimtalk_channel.request_kakao_template_inspection"
    )
    @patch(
        "apps.domains.messaging.management.commands.configure_tenant_alimtalk_channel.clone_kakao_template"
    )
    @patch(
        "apps.domains.messaging.management.commands.configure_tenant_alimtalk_channel.list_kakao_templates"
    )
    def test_apply_resumes_inspection_for_an_existing_pending_copy(
        self,
        list_templates,
        clone_template,
        request_inspection,
        _required,
    ):
        source = _template("SOURCE-TEMPLATE", content="정본 안내")
        pending = _template("TENANT-TEMPLATE", status="PENDING", content="정본 안내")
        inspecting = {**pending, "status": "INSPECTING"}
        list_templates.side_effect = [[source], [pending], [inspecting]]
        request_inspection.return_value = inspecting

        call_command(
            "configure_tenant_alimtalk_channel",
            tenant_code="limglish",
            channel_id="NEW-CHANNEL",
            apply=True,
        )

        clone_template.assert_not_called()
        request_inspection.assert_called_once_with(
            api_key="shared-key",
            api_secret="shared-secret",
            template_id="TENANT-TEMPLATE",
            comment="학원 운영 알림톡 전용 템플릿입니다.",
        )
        mapping = AlimtalkTemplateBinding.objects.get(
            source_template_id="SOURCE-TEMPLATE"
        )
        self.assertEqual(mapping.status, "INSPECTING")

    @patch(
        "apps.domains.messaging.management.commands.send_tenant_alimtalk_channel_test.list_kakao_templates"
    )
    @patch("apps.worker.messaging_worker.config.load_config")
    @patch("apps.worker.messaging_worker.sqs_main.send_one_alimtalk")
    def test_live_test_is_alimtalk_only_and_idempotent(
        self,
        send_one,
        load_config,
        list_templates,
    ):
        live = _template("TEST-TEMPLATE", content="인증번호 #{인증번호}")
        live.update({"name": "인증번호", "variables": [{"name": "인증번호"}]})
        now = timezone.now()
        AlimtalkChannelBinding.objects.create(
            tenant=self.tenant,
            channel_id="NEW-CHANNEL",
            status="pending_templates",
            verified_at=now,
            last_synced_at=now,
            test_template_id="TEST-TEMPLATE",
            test_template_fingerprint=build_template_fingerprint(live),
        )
        list_templates.return_value = [live]
        load_config.return_value = SimpleNamespace(SOLAPI_SENDER="0212345678")
        send_one.return_value = {"status": "ok", "group_id": "provider-group"}

        kwargs = {
            "tenant_code": "limglish",
            "to": "0101234567",
            "idempotency_key": "test-once",
            "confirm_live": True,
        }
        call_command("send_tenant_alimtalk_channel_test", **kwargs)
        call_command("send_tenant_alimtalk_channel_test", **kwargs)

        send_one.assert_called_once()
        self.assertEqual(send_one.call_args.kwargs["pf_id"], "NEW-CHANNEL")
        self.assertEqual(send_one.call_args.kwargs["template_id"], "TEST-TEMPLATE")
        log = NotificationLog.objects.get(origin_type="tenant_channel_test")
        self.assertEqual(log.status, "sent")
        self.assertTrue(log.success)
        self.assertEqual(log.message_mode, "alimtalk")
        self.assertTrue(log.provider_message_id)
        self.assertNotIn("0101234567", log.recipient_summary)
