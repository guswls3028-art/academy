from __future__ import annotations

from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.http import HttpRequest, JsonResponse
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.api.common.middleware import UnhandledExceptionMiddleware
from apps.api.common import middleware as incident_middleware
from apps.api.common.throttles import UserIncidentReportThrottle
from apps.core.management.commands.check_dev_alerts import (
    LEGACY_SMS_DELIVERY_ACTION,
    SLACK_DELIVERY_ACTION,
    rule_user_incidents,
)
from apps.core.models import OpsAuditLog, Tenant, TenantMembership, User
from apps.core.views.user_incidents import (
    UserIncidentReportView,
    sanitize_incident_route,
)


class UserIncidentReportViewTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            code="incident-tenant",
            name="Incident Tenant",
            is_active=True,
        )
        self.user = User.objects.create_user(
            username="incident-owner",
            password="1234",
            tenant=self.tenant,
        )
        TenantMembership.objects.create(
            tenant=self.tenant,
            user=self.user,
            role="owner",
            is_active=True,
        )
        self.factory = APIRequestFactory()

    def test_authenticated_member_report_is_tenant_scoped_and_sanitized(self):
        request = self.factory.post(
            "/api/v1/core/problem-reports/",
            {
                "source": "manual",
                "message": "학생 목록 저장 버튼이 동작하지 않습니다.",
                "route": "https://example.test/admin/students/123?phone=01012345678",
                "screen_size": "1366x768",
            },
            format="json",
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.user)

        response = UserIncidentReportView.as_view()(request)

        self.assertEqual(response.status_code, 201)
        log = OpsAuditLog.objects.get(pk=response.data["incident_id"])
        self.assertEqual(log.action, "user_incident.manual")
        self.assertEqual(log.target_tenant, self.tenant)
        self.assertEqual(log.actor_user, self.user)
        self.assertEqual(log.payload["route"], "/admin/students/:id")
        self.assertNotIn("phone", log.payload["route"])

    def test_non_member_is_denied(self):
        other_tenant = Tenant.objects.create(
            code="other-tenant",
            name="Other Tenant",
            is_active=True,
        )
        request = self.factory.post(
            "/api/v1/core/problem-reports/",
            {"source": "manual", "message": "문제", "route": "/admin"},
            format="json",
        )
        request.tenant = other_tenant
        force_authenticate(request, user=self.user)

        response = UserIncidentReportView.as_view()(request)

        self.assertEqual(response.status_code, 403)
        self.assertFalse(OpsAuditLog.objects.filter(action="user_incident.manual").exists())

    def test_frontend_exception_requires_error_name(self):
        request = self.factory.post(
            "/api/v1/core/problem-reports/",
            {
                "source": "frontend_exception",
                "message": "화면 오류",
                "route": "/admin/dashboard",
            },
            format="json",
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.user)

        response = UserIncidentReportView.as_view()(request)

        self.assertEqual(response.status_code, 400)

    def test_route_sanitizer_removes_ids_uuid_and_query(self):
        value = (
            "https://example.test/results/42/"
            "123e4567-e89b-12d3-a456-426614174000?student=홍길동"
        )
        self.assertEqual(
            sanitize_incident_route(value),
            "/results/:id/:uuid",
        )

    def test_manual_and_automatic_reports_use_separate_throttle_buckets(self):
        manual = SimpleNamespace(
            tenant=self.tenant,
            user=self.user,
            data={"source": "manual"},
        )
        automatic = SimpleNamespace(
            tenant=self.tenant,
            user=self.user,
            data={"source": "frontend_exception"},
        )

        throttle = UserIncidentReportThrottle()
        for _index in range(12):
            OpsAuditLog.objects.create(
                actor_user=self.user,
                target_tenant=self.tenant,
                action="user_incident.frontend_exception",
            )
        self.assertTrue(throttle.allow_request(manual, UserIncidentReportView()))
        self.assertFalse(throttle.allow_request(automatic, UserIncidentReportView()))

    def test_top_level_json_array_is_validation_error_not_server_error(self):
        request = self.factory.post(
            "/api/v1/core/problem-reports/",
            [],
            format="json",
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.user)

        response = UserIncidentReportView.as_view()(request)

        self.assertEqual(response.status_code, 400)


class BackendIncidentCaptureTests(TransactionTestCase):
    def setUp(self):
        incident_middleware._user_incident_sampled_at.clear()
        self.tenant = Tenant.objects.create(
            code="capture-tenant",
            name="Capture Tenant",
            is_active=True,
        )

    def _request(self, path="/api/v1/results/42/"):
        request = HttpRequest()
        request.method = "POST"
        request.path = path
        request.META["HTTP_USER_AGENT"] = "test-agent"
        request.tenant = self.tenant
        request.user = SimpleNamespace(is_authenticated=False)
        request.resolver_match = SimpleNamespace(route="api/v1/results/<int:pk>/")
        return request

    def test_5xx_response_records_metadata_only_incident(self):
        middleware = UnhandledExceptionMiddleware(
            get_response=lambda _request: JsonResponse({"detail": "error"}, status=500)
        )

        response = middleware(self._request())
        incident_middleware._user_incident_audit_queue.join()

        self.assertEqual(response.status_code, 500)
        log = OpsAuditLog.objects.get(action="user_incident.backend_5xx")
        self.assertEqual(log.target_tenant, self.tenant)
        self.assertEqual(log.payload["status"], 500)
        self.assertEqual(log.payload["route"], "api/v1/results/<int:pk>/")
        self.assertNotIn("body", log.payload)

    def test_health_5xx_is_not_recorded_as_user_incident(self):
        middleware = UnhandledExceptionMiddleware(
            get_response=lambda _request: JsonResponse({"detail": "error"}, status=500)
        )

        middleware(self._request("/health"))
        incident_middleware._user_incident_audit_queue.join()

        self.assertFalse(
            OpsAuditLog.objects.filter(action="user_incident.backend_5xx").exists()
        )

    def test_repeated_identical_5xx_is_sampled_before_database_write(self):
        middleware = UnhandledExceptionMiddleware(
            get_response=lambda _request: JsonResponse({"detail": "error"}, status=500)
        )
        middleware(self._request())
        middleware(self._request())
        incident_middleware._user_incident_audit_queue.join()

        self.assertEqual(
            OpsAuditLog.objects.filter(action="user_incident.backend_5xx").count(),
            1,
        )

    def test_identical_5xx_for_different_tenants_is_not_cross_suppressed(self):
        second_tenant = Tenant.objects.create(
            code="capture-tenant-2",
            name="Capture Tenant 2",
            is_active=True,
        )
        middleware = UnhandledExceptionMiddleware(
            get_response=lambda _request: JsonResponse({"detail": "error"}, status=500)
        )
        first = self._request()
        second = self._request()
        second.tenant = second_tenant
        middleware(first)
        middleware(second)
        incident_middleware._user_incident_audit_queue.join()

        self.assertEqual(
            OpsAuditLog.objects.filter(action="user_incident.backend_5xx").count(),
            2,
        )


class UserIncidentAlertRuleTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            code="alert-tenant",
            name="Alert Tenant",
            is_active=True,
        )

    def _backend_incident(self, *, tenant=None, status=500):
        return OpsAuditLog.objects.create(
            action="user_incident.backend_5xx",
            summary="GET route returned 500",
            target_tenant=tenant or self.tenant,
            payload={
                "source": "backend_5xx",
                "route": "api/v1/results/<int:pk>/",
                "method": "GET",
                "status": status,
                "exception_name": "ValueError",
            },
            result="failed",
        )

    def test_repeated_same_error_is_grouped_without_sensitive_details(self):
        self._backend_incident()
        self._backend_incident()

        result = rule_user_incidents()

        self.assertIsNotNone(result)
        self.assertEqual(result["total"], 2)
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["rows"][0]["count"], 2)
        self.assertNotIn("ValueError", str(result["rows"]))

    def test_legacy_sms_receipt_remains_read_only_dedup_history(self):
        self._backend_incident()
        result = rule_user_incidents()
        OpsAuditLog.objects.create(
            action=LEGACY_SMS_DELIVERY_ACTION,
            summary="Historical operator delivery",
            payload={
                "fingerprints": result["fingerprints"],
                "attempt_state": "delivered",
            },
            result="success",
        )

        self.assertIsNone(rule_user_incidents())

    @override_settings(DEV_ALERTS_WEBHOOK_URL="https://hooks.example.invalid/test")
    @patch(
        "apps.core.management.commands.check_dev_alerts._post_slack",
        return_value=True,
    )
    def test_command_records_successful_slack_delivery_and_deduplicates(self, post_mock):
        self._backend_incident()

        call_command(
            "check_dev_alerts",
            "--rule",
            "user_incidents",
            stdout=StringIO(),
        )

        post_mock.assert_called_once()
        receipt = OpsAuditLog.objects.get(action=SLACK_DELIVERY_ACTION)
        self.assertEqual(receipt.result, "success")
        self.assertEqual(len(receipt.payload["fingerprints"]), 1)
        self.assertIsNone(rule_user_incidents())

    @override_settings(DEV_ALERTS_WEBHOOK_URL="https://hooks.example.invalid/test")
    @patch(
        "apps.core.management.commands.check_dev_alerts._post_slack",
        return_value=False,
    )
    def test_failed_slack_delivery_does_not_consume_incidents(self, post_mock):
        self._backend_incident()

        with self.assertRaises(CommandError):
            call_command(
                "check_dev_alerts",
                "--rule",
                "user_incidents",
                stdout=StringIO(),
            )

        post_mock.assert_called_once()
        self.assertFalse(
            OpsAuditLog.objects.filter(action=SLACK_DELIVERY_ACTION).exists()
        )
        self.assertIsNotNone(rule_user_incidents())
        self.assertEqual(
            OpsAuditLog.objects.get(action="cron.check_dev_alerts").result,
            "failed",
        )

    @override_settings(DEV_ALERTS_WEBHOOK_URL="https://hooks.example.invalid/test")
    @patch("apps.core.management.commands.check_dev_alerts._post_slack")
    def test_dry_run_never_dispatches_or_consumes_incidents(self, post_mock):
        self._backend_incident()

        with self.assertRaisesMessage(CommandError, "were not delivered"):
            call_command(
                "check_dev_alerts",
                "--rule",
                "user_incidents",
                "--dry-run",
                stdout=StringIO(),
            )

        post_mock.assert_not_called()
        self.assertFalse(
            OpsAuditLog.objects.filter(action=SLACK_DELIVERY_ACTION).exists()
        )
        self.assertIsNotNone(rule_user_incidents())
        self.assertEqual(
            OpsAuditLog.objects.get(action="cron.check_dev_alerts").result,
            "failed",
        )


class DevAlertsWorkflowContractTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.workflow = (
            Path(__file__).resolve().parents[3]
            / ".github"
            / "workflows"
            / "dev-alerts-cron.yml"
        ).read_text(encoding="utf-8")

    def test_five_minute_dispatch_evaluates_user_and_messaging_incidents(self):
        self.assertIn(
            'EXTRA_ARGS="--rule user_incidents --rule messaging_delivery_health"',
            self.workflow,
        )
        self.assertIn('SCHEDULE_EXPRESSION: ${{ github.event.schedule }}', self.workflow)
        self.assertIn('"2 * * * *"', self.workflow)
        self.assertIn('FULL_RULES_INPUT: ${{ github.event.inputs.full_rules }}', self.workflow)

    def test_workflow_has_no_sms_or_external_signal_dispatch(self):
        lowered = self.workflow.lower()
        for forbidden in (
            "test_sms",
            "--test-sms",
            "dev_alerts_sms",
            "--external-signal",
            "messagetype.sms",
            "operator sms",
        ):
            self.assertNotIn(forbidden, lowered)

    def test_dry_run_never_dispatches_platform_push(self):
        self.assertIn('if [ "$DRY_RUN" = "true" ]; then', self.workflow)
        self.assertIn('PUSH_COMMAND=""', self.workflow)
        self.assertIn('CLEANUP_COMMAND=""', self.workflow)
        self.assertIn(
            "sh -c '${CLEANUP_COMMAND}${PUSH_COMMAND}python manage.py check_dev_alerts",
            self.workflow,
        )
