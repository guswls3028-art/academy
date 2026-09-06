from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.core.models import OpsAuditLog, Tenant, TenantMembership
from apps.domains.messaging.models import MessageTemplate
from apps.domains.messaging.serializers import (
    MessageTemplateSerializer,
    SendMessageRequestSerializer,
)
from apps.domains.messaging.services.preflight import _resolve_template_for_manual_send
from apps.domains.messaging.views.info_views import (
    MessagingInfoView,
    TestCredentialsView as CredentialsDiagnosticView,
)
from apps.domains.messaging.views.template_views import (
    MessageTemplateDuplicateView,
    MessageTemplateSubmitReviewView,
    SolapiSyncTemplatesView,
)
from apps.domains.messaging import urls as messaging_urls


User = get_user_model()


class BusinessMessagingContractTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.tenant = Tenant.objects.create(
            code="business-msg",
            name="Business Messaging",
            is_active=True,
            kakao_pfid="TENANT-PFID",
            messaging_sender="01099998888",
            messaging_provider="ppurio",
            own_solapi_api_key="tenant-key",
            own_solapi_api_secret="tenant-secret",
            own_ppurio_api_key="ppurio-key",
            own_ppurio_account="ppurio-account",
        )
        self.user = User.objects.create_user(
            username="business-msg-owner",
            password="test1234",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=self.user, role="owner")

    def _request(self, method: str, path: str, data: dict | None = None):
        request = getattr(self.factory, method)(path, data=data or {}, format="json")
        force_authenticate(request, user=self.user)
        request.user = self.user
        request.tenant = self.tenant
        return request

    def test_messaging_info_hides_legacy_provider_fields_and_owner_controls_activation(self):
        response = MessagingInfoView.as_view()(self._request("get", "/api/v1/messaging/info/"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data["delivery_policy"],
            "verified_tenant_or_common_alimtalk",
        )
        self.assertNotIn("sms_allowed", response.data)
        self.assertEqual(response.data["messaging_provider"], "solapi")
        self.assertEqual(response.data["kakao_pfid"], "")
        self.assertEqual(response.data["own_solapi_api_key"], "")
        self.assertFalse(response.data["has_own_credentials"])
        self.assertFalse(response.data["custom_channel_registered"])
        self.assertTrue(response.data["tenant_messaging_enabled"])
        self.assertTrue(response.data["can_manage_messaging"])

        patch_response = MessagingInfoView.as_view()(
            self._request(
                "patch",
                "/api/v1/messaging/info/",
                {"tenant_messaging_enabled": False},
            )
        )
        self.assertEqual(patch_response.status_code, 200)
        self.assertFalse(patch_response.data["tenant_messaging_enabled"])
        self.assertTrue(patch_response.data["messaging_disabled"])
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.messaging_provider, "ppurio")
        self.assertEqual(self.tenant.kakao_pfid, "TENANT-PFID")
        self.assertFalse(self.tenant.messaging_is_active)
        audit = OpsAuditLog.objects.get(action="messaging.tenant_activation")
        self.assertEqual(audit.target_tenant_id, self.tenant.id)
        self.assertEqual(audit.payload, {"previous": True, "enabled": False})

    def test_teacher_cannot_change_tenant_messaging_activation(self):
        teacher = User.objects.create_user(
            username="business-msg-activation-teacher",
            password="test1234",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=teacher,
            role="teacher",
        )
        request = self.factory.patch(
            "/api/v1/messaging/info/",
            {"tenant_messaging_enabled": False},
            format="json",
        )
        force_authenticate(request, user=teacher)
        request.user = teacher
        request.tenant = self.tenant

        response = MessagingInfoView.as_view()(request)

        self.assertEqual(response.status_code, 403)
        self.tenant.refresh_from_db()
        self.assertTrue(self.tenant.messaging_is_active)

    def test_provider_template_mutation_endpoints_are_gone(self):
        template = MessageTemplate.objects.create(
            tenant=self.tenant,
            category="attendance",
            name="출석 문구",
            body="출석 안내",
        )
        submit_response = MessageTemplateSubmitReviewView.as_view()(
            self._request(
                "post",
                f"/api/v1/messaging/templates/{template.id}/submit-review/",
            ),
            pk=template.id,
        )
        sync_response = SolapiSyncTemplatesView.as_view()(
            self._request("post", "/api/v1/messaging/templates/sync-solapi/")
        )

        self.assertEqual(submit_response.status_code, 410)
        self.assertEqual(submit_response.data["code"], "new_kakao_template_disabled")
        self.assertEqual(sync_response.status_code, 410)
        self.assertEqual(sync_response.data["code"], "provider_template_sync_disabled")

    def test_duplicate_template_collapses_recursive_copy_names(self):
        source = MessageTemplate.objects.create(
            tenant=self.tenant,
            category="default",
            name="복사 - 복사 - 학부모 안내",
            body="안내 본문",
        )

        response = MessageTemplateDuplicateView.as_view()(
            self._request(
                "post",
                f"/api/v1/messaging/templates/{source.id}/duplicate/",
            ),
            pk=source.id,
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["name"], "복사 - 학부모 안내")
        self.assertNotIn("복사 - 복사 -", response.data["name"])

        long_source = MessageTemplate.objects.create(
            tenant=self.tenant,
            category="default",
            name="가" * 120,
            body="안내 본문",
        )
        long_response = MessageTemplateDuplicateView.as_view()(
            self._request(
                "post",
                f"/api/v1/messaging/templates/{long_source.id}/duplicate/",
            ),
            pk=long_source.id,
        )

        self.assertEqual(long_response.status_code, 201)
        self.assertTrue(long_response.data["name"].startswith("복사 - "))
        self.assertEqual(len(long_response.data["name"]), 120)

    def test_duplicate_template_rejects_overlong_explicit_name(self):
        source = MessageTemplate.objects.create(
            tenant=self.tenant,
            category="default",
            name="학부모 안내",
            body="안내 본문",
        )

        response = MessageTemplateDuplicateView.as_view()(
            self._request(
                "post",
                f"/api/v1/messaging/templates/{source.id}/duplicate/",
                {"name": "가" * 121},
            ),
            pk=source.id,
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("name", response.data)

    def test_unverified_self_credit_endpoint_is_not_routable(self):
        self.assertNotIn(
            "messaging-charge",
            {pattern.name for pattern in messaging_urls.urlpatterns},
        )

    def test_teacher_cannot_probe_common_provider_diagnostics(self):
        teacher = User.objects.create_user(
            username="business-msg-teacher",
            password="test1234",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=teacher,
            role="teacher",
        )
        request = self.factory.post(
            "/api/v1/messaging/test-credentials/",
            data={},
            format="json",
        )
        force_authenticate(request, user=teacher)
        request.user = teacher
        request.tenant = self.tenant

        response = CredentialsDiagnosticView.as_view()(request)

        self.assertEqual(response.status_code, 403)

    def test_manual_send_payload_is_bounded_before_recipient_lookup(self):
        too_many = SendMessageRequestSerializer(data={
            "student_ids": list(range(1, 202)),
            "send_to": "parent",
            "raw_body": "안내",
            "block_category": "attendance",
        })
        too_large = SendMessageRequestSerializer(data={
            "student_ids": [1],
            "send_to": "parent",
            "raw_body": "가" * 5001,
            "block_category": "attendance",
        })

        self.assertFalse(too_many.is_valid())
        self.assertIn("student_ids", too_many.errors)
        self.assertFalse(too_large.is_valid())
        self.assertIn("raw_body", too_large.errors)

    def test_payment_missing_sid_never_falls_back_to_attendance_envelope(self):
        template = MessageTemplate.objects.create(
            tenant=self.tenant,
            category="payment",
            name="결제 안내",
            body="결제 안내입니다.",
            solapi_template_id="STALE-DIRECT-SID",
            solapi_status="APPROVED",
        )

        plan = _resolve_template_for_manual_send(
            self.tenant,
            {
                "template_id": template.id,
                "raw_body": "결제 안내입니다.",
                "block_category": "attendance",
            },
        )

        self.assertFalse(plan.ok)
        self.assertEqual(plan.source, "unified_missing")
        self.assertTrue(plan.uses_unified_template)

    def test_saved_phrase_readiness_describes_envelope_not_legacy_solapi_status(self):
        attendance = MessageTemplate.objects.create(
            tenant=self.tenant,
            category="attendance",
            name="출석 안내",
            body="출석 안내",
        )
        payment = MessageTemplate.objects.create(
            tenant=self.tenant,
            category="payment",
            name="결제 안내",
            body="결제 안내",
            solapi_template_id="STALE-DIRECT-SID",
            solapi_status="APPROVED",
        )
        general = MessageTemplate.objects.create(
            tenant=self.tenant,
            category="default",
            name="일반 안내",
            body="일반 안내",
        )

        attendance_data = MessageTemplateSerializer(attendance).data
        payment_data = MessageTemplateSerializer(payment).data
        general_data = MessageTemplateSerializer(general).data

        self.assertEqual(attendance_data["alimtalk_readiness"], "ready")
        self.assertEqual(attendance_data["alimtalk_envelope_type"], "attendance")
        self.assertEqual(payment_data["alimtalk_readiness"], "provider_template_missing")
        self.assertEqual(payment_data["alimtalk_envelope_type"], "notice_payment")
        self.assertEqual(general_data["alimtalk_readiness"], "envelope_selection_required")
