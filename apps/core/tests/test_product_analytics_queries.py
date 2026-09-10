from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.core.middleware.safe_method_write import (
    SafeMethodDatabaseWriteMiddleware,
    sql_write_operation,
)
from apps.core.models import (
    OpsAuditLog,
    ProductUsageDailyActor,
    Tenant,
    TenantMembership,
)
from apps.core.product_analytics.views import ProductUsageOverviewView


class ProductUsageOverviewTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.platform = Tenant.objects.create(
            code="analytics-platform",
            name="Analytics Platform",
            is_active=True,
        )
        self.target = Tenant.objects.create(
            code="analytics-target",
            name="Analytics Target",
            is_active=True,
        )
        self.user = get_user_model().objects.create_superuser(
            username="analytics-platform-owner",
            password="test1234",
            tenant=self.platform,
        )
        TenantMembership.objects.create(
            tenant=self.platform,
            user=self.user,
            role="owner",
            is_active=True,
        )

    def daily(self, *, actor: str, event_type: str, count: int):
        now = timezone.now()
        ProductUsageDailyActor.objects.create(
            day=timezone.localdate(),
            tenant=self.target,
            actor_hash=actor,
            role="teacher",
            audience_group="teacher_staff",
            surface="teacher",
            feature_id="attendance.mark",
            screen_id="teacher.attendance.home",
            event_type=event_type,
            cta_id="attendance.save" if event_type.startswith("cta_") else "",
            action_id=(
                "attendance.save" if event_type.startswith("task_") else ""
            ),
            placement_id="teacher.page.primary",
            position_index=0,
            device_class="desktop",
            client_release="test-release",
            catalog_version="2026-07-29",
            synthetic=False,
            is_impersonated=False,
            count=count,
            first_at=now - timedelta(minutes=1),
            last_at=now,
        )

    def request(self, query: str):
        request = self.factory.get(
            f"/api/v1/core/dev/product-analytics/overview/{query}"
        )
        request.tenant = self.platform
        force_authenticate(request, user=self.user)
        with override_settings(OWNER_TENANT_ID=self.platform.id):
            return ProductUsageOverviewView.as_view()(request)

    def guarded_request(self, query: str):
        request = self.factory.get(
            f"/api/v1/core/dev/product-analytics/overview/{query}"
        )
        request.tenant = self.platform
        force_authenticate(request, user=self.user)
        view = SafeMethodDatabaseWriteMiddleware(
            ProductUsageOverviewView.as_view()
        )
        with override_settings(OWNER_TENANT_ID=self.platform.id):
            return view(request)

    def test_overview_get_retries_are_read_only(self):
        audit_count = OpsAuditLog.objects.count()

        with self.assertLogs(
            "apps.core.product_analytics.views",
            level="INFO",
        ) as captured_logs, CaptureQueriesContext(connection) as captured:
            responses = [self.guarded_request("?days=28") for _ in range(3)]

        self.assertEqual([response.status_code for response in responses], [200] * 3)
        self.assertEqual(
            [
                query["sql"]
                for query in captured.captured_queries
                if sql_write_operation(query["sql"]) is not None
            ],
            [],
        )
        self.assertEqual(OpsAuditLog.objects.count(), audit_count)
        self.assertEqual(
            sum(
                "product_analytics.overview_read" in message
                for message in captured_logs.output
            ),
            3,
        )

    def test_platform_admin_gets_role_feature_and_completion_metrics(self):
        for index in range(5):
            actor = f"{index:064d}"
            self.daily(actor=actor, event_type="screen_view", count=2)
            self.daily(actor=actor, event_type="screen_engaged", count=1)
            self.daily(actor=actor, event_type="task_start", count=1)
            self.daily(actor=actor, event_type="task_success", count=1)

        response = self.request(
            f"?days=28&tenant_id={self.target.id}&role=teacher&surface=teacher"
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data["suppressed"])
        self.assertEqual(response.data["summary"]["active_actors"], 5)
        self.assertEqual(response.data["summary"]["engagement_rate"], 0.5)
        self.assertEqual(response.data["summary"]["task_completion_rate"], 1.0)
        self.assertEqual(response.data["features"][0]["feature_id"], "attendance.mark")

    def test_small_single_tenant_cell_is_suppressed(self):
        self.daily(actor="a" * 64, event_type="screen_view", count=1)

        response = self.request(f"?days=28&tenant_id={self.target.id}")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["suppressed"])
        self.assertIsNone(response.data["summary"]["active_actors"])
        self.assertEqual(response.data["features"], [])

    def test_non_platform_tenant_is_forbidden(self):
        outsider = get_user_model().objects.create_user(
            username="analytics-non-platform",
            password="test1234",
            tenant=self.target,
        )
        TenantMembership.objects.create(
            tenant=self.target,
            user=outsider,
            role="owner",
            is_active=True,
        )
        request = self.factory.get(
            "/api/v1/core/dev/product-analytics/overview/?days=28"
        )
        request.tenant = self.target
        force_authenticate(request, user=outsider)

        with override_settings(OWNER_TENANT_ID=self.platform.id):
            response = ProductUsageOverviewView.as_view()(request)

        self.assertEqual(response.status_code, 403)
