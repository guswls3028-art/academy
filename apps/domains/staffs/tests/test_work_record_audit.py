from datetime import date, time
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.core.models import OpsAuditLog, Tenant, TenantMembership
from apps.domains.staffs.models import Staff, WorkMonthLock, WorkRecord, WorkType
from apps.domains.staffs.views import StaffViewSet, WorkRecordViewSet


User = get_user_model()


class WorkRecordAuditTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.tenant = Tenant.objects.create(
            code="work-record-audit",
            name="Work Record Audit",
            is_active=True,
        )
        self.owner = User.objects.create_user(
            username="work-record-audit-owner",
            password="test1234",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=self.owner,
            role="owner",
        )
        self.staff = Staff.objects.create(
            tenant=self.tenant,
            name="감사 대상",
            phone="",
        )
        self.other_staff = Staff.objects.create(
            tenant=self.tenant,
            name="이동 대상",
            phone="",
        )
        self.work_type = WorkType.objects.create(
            tenant=self.tenant,
            name="기본 근무",
            base_hourly_wage=15_000,
            is_active=True,
        )

    def _request(self, method, path, data=None):
        request = getattr(self.factory, method)(path, data or {}, format="json")
        request.tenant = self.tenant
        force_authenticate(request, user=self.owner)
        return request

    def _create_manual_record(self):
        request = self._request(
            "post",
            "/api/v1/staffs/work-records/",
            {
                "staff": self.staff.id,
                "work_type": self.work_type.id,
                "date": "2026-08-01",
                "start_time": "14:00:00",
                "end_time": "18:30:00",
                "break_minutes": 10,
                "break_total_seconds": 0,
                "current_break_started_at": None,
                "meal_minutes": 20,
                "adjustment_amount": 500,
            },
        )
        response = WorkRecordViewSet.as_view({"post": "create"})(request)
        self.assertEqual(response.status_code, 201, response.data)
        return WorkRecord.objects.get(pk=response.data["id"])

    def test_manager_manual_create_records_recovery_snapshot_without_pii(self):
        record = self._create_manual_record()

        audit = OpsAuditLog.objects.get(action="staff.work_record_created")
        self.assertEqual(audit.actor_user, self.owner)
        self.assertEqual(audit.target_tenant, self.tenant)
        self.assertEqual(
            audit.payload,
            {
                "source": "payroll_manager_manual",
                "work_record_id": record.id,
                "staff_id": self.staff.id,
                "work_type_id": self.work_type.id,
                "date": "2026-08-01",
                "start_time": "14:00:00",
                "end_time": "18:30:00",
                "break_minutes": 10,
                "break_total_seconds": 0,
                "current_break_started_at": None,
                "meal_minutes": 20,
                "work_hours": "4.00",
                "amount": 60500,
                "resolved_hourly_wage": 15000,
                "adjustment_amount": 500,
                "is_manually_edited": False,
                "created_local_date": str(timezone.localdate(record.created_at)),
            },
        )
        self.assertNotIn("memo", audit.payload)

    def test_server_clock_in_is_not_mislabeled_as_manager_manual_entry(self):
        self.staff.user = self.owner
        self.staff.save(update_fields=["user"])
        request = self._request(
            "post",
            f"/api/v1/staffs/{self.staff.id}/work-records/start-work/",
            {"work_type": self.work_type.id},
        )

        response = StaffViewSet.as_view({"post": "start_work"})(
            request,
            pk=self.staff.id,
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertFalse(
            OpsAuditLog.objects.filter(action="staff.work_record_created").exists()
        )

    def test_manager_manual_delete_preserves_before_snapshot_after_row_is_gone(self):
        record = self._create_manual_record()
        OpsAuditLog.objects.all().delete()

        request = self._request(
            "delete",
            f"/api/v1/staffs/work-records/{record.id}/",
        )
        response = WorkRecordViewSet.as_view({"delete": "destroy"})(
            request,
            pk=record.id,
        )

        self.assertEqual(response.status_code, 204, response.data)
        self.assertFalse(WorkRecord.objects.filter(pk=record.id).exists())
        audit = OpsAuditLog.objects.get(action="staff.work_record_deleted")
        self.assertEqual(audit.actor_user, self.owner)
        self.assertEqual(audit.target_tenant, self.tenant)
        self.assertEqual(audit.payload["source"], "payroll_manager_manual")
        self.assertEqual(audit.payload["work_record_id"], record.id)
        self.assertEqual(audit.payload["staff_id"], self.staff.id)
        self.assertEqual(audit.payload["date"], "2026-08-01")
        self.assertEqual(audit.payload["start_time"], "14:00:00")
        self.assertEqual(audit.payload["end_time"], "18:30:00")
        self.assertEqual(audit.payload["amount"], 60500)
        self.assertNotIn("memo", audit.payload)

    def test_manual_create_rolls_back_when_required_audit_cannot_be_written(self):
        with patch(
            "apps.core.models.OpsAuditLog.objects.create",
            side_effect=RuntimeError("audit unavailable"),
        ):
            request = self._request(
                "post",
                "/api/v1/staffs/work-records/",
                {
                    "staff": self.staff.id,
                    "work_type": self.work_type.id,
                    "date": "2026-08-01",
                    "start_time": "14:00:00",
                    "end_time": "18:30:00",
                },
            )
            response = WorkRecordViewSet.as_view({"post": "create"})(request)

        self.assertEqual(response.status_code, 503, response.data)
        self.assertFalse(WorkRecord.objects.exists())

    def test_manual_delete_rolls_back_when_required_audit_cannot_be_written(self):
        record = WorkRecord.objects.create(
            tenant=self.tenant,
            staff=self.staff,
            work_type=self.work_type,
            date=date(2026, 8, 1),
            start_time=time(14, 0),
            end_time=time(18, 30),
        )
        with patch(
            "apps.core.models.OpsAuditLog.objects.create",
            side_effect=RuntimeError("audit unavailable"),
        ):
            request = self._request(
                "delete",
                f"/api/v1/staffs/work-records/{record.id}/",
            )
            response = WorkRecordViewSet.as_view({"delete": "destroy"})(
                request,
                pk=record.id,
            )

        self.assertEqual(response.status_code, 503, response.data)
        self.assertTrue(WorkRecord.objects.filter(pk=record.id).exists())

    def test_manual_patch_records_exact_old_and_new_payroll_identity(self):
        record = WorkRecord.objects.create(
            tenant=self.tenant,
            staff=self.staff,
            work_type=self.work_type,
            date=date(2026, 8, 9),
            start_time=time(14, 0),
            end_time=time(18, 30),
            work_hours=Decimal("4.50"),
            amount=67_500,
            is_manually_edited=True,
        )
        request = self._request(
            "patch",
            f"/api/v1/staffs/work-records/{record.id}/",
            {
                "staff": self.other_staff.id,
                "date": "2026-08-01",
                "work_hours": "4.50",
                "amount": 70_000,
            },
        )

        response = WorkRecordViewSet.as_view({"patch": "partial_update"})(
            request,
            pk=record.id,
        )

        self.assertEqual(response.status_code, 200, response.data)
        audit = OpsAuditLog.objects.get(action="staff.work_record_updated")
        self.assertEqual(audit.payload["source"], "payroll_manager_manual")
        self.assertEqual(audit.payload["work_record_id"], record.id)
        self.assertEqual(
            audit.payload["fields"],
            ["amount", "date", "staff", "work_hours"],
        )
        self.assertEqual(
            audit.payload["old"],
            {
                "amount": "67500",
                "date": "2026-08-09",
                "staff": str(self.staff.id),
                "work_hours": "4.50",
            },
        )
        self.assertEqual(
            audit.payload["new"],
            {
                "amount": "70000",
                "date": "2026-08-01",
                "staff": str(self.other_staff.id),
                "work_hours": "4.50",
            },
        )

    def test_manual_patch_rolls_back_when_required_audit_cannot_be_written(self):
        record = WorkRecord.objects.create(
            tenant=self.tenant,
            staff=self.staff,
            work_type=self.work_type,
            date=date(2026, 8, 9),
            start_time=time(14, 0),
            end_time=time(18, 30),
            work_hours=Decimal("4.50"),
            amount=67_500,
            is_manually_edited=True,
        )
        with patch(
            "apps.core.models.OpsAuditLog.objects.create",
            side_effect=RuntimeError("audit unavailable"),
        ):
            request = self._request(
                "patch",
                f"/api/v1/staffs/work-records/{record.id}/",
                {
                    "staff": self.other_staff.id,
                    "date": "2026-08-01",
                    "work_hours": "4.50",
                    "amount": 70_000,
                },
            )
            response = WorkRecordViewSet.as_view({"patch": "partial_update"})(
                request,
                pk=record.id,
            )

        self.assertEqual(response.status_code, 503, response.data)
        record.refresh_from_db()
        self.assertEqual(record.staff_id, self.staff.id)
        self.assertEqual(record.date, date(2026, 8, 9))
        self.assertEqual(record.amount, 67_500)

    def test_manual_recalculate_records_exact_payroll_restore(self):
        record = WorkRecord.objects.create(
            tenant=self.tenant,
            staff=self.staff,
            work_type=self.work_type,
            date=date(2026, 8, 1),
            start_time=time(14, 0),
            end_time=time(18, 30),
            break_minutes=10,
            meal_minutes=20,
            work_hours=Decimal("9.50"),
            amount=200_000,
            resolved_hourly_wage=15_000,
            adjustment_amount=500,
            is_manually_edited=True,
        )
        request = self._request(
            "post",
            f"/api/v1/staffs/work-records/{record.id}/recalculate/",
        )

        response = WorkRecordViewSet.as_view({"post": "recalculate"})(
            request,
            pk=record.id,
        )

        self.assertEqual(response.status_code, 200, response.data)
        record.refresh_from_db()
        self.assertFalse(record.is_manually_edited)
        self.assertEqual(record.work_hours, Decimal("4.00"))
        self.assertEqual(record.amount, 60_500)
        audit = OpsAuditLog.objects.get(action="staff.work_record_updated")
        self.assertEqual(
            audit.payload,
            {
                "source": "payroll_manager_manual",
                "work_record_id": record.id,
                "fields": ["amount", "is_manually_edited", "work_hours"],
                "old": {
                    "amount": "200000",
                    "date": "2026-08-01",
                    "is_manually_edited": "True",
                    "staff": str(self.staff.id),
                    "work_hours": "9.50",
                },
                "new": {
                    "amount": "60500",
                    "date": "2026-08-01",
                    "is_manually_edited": "False",
                    "staff": str(self.staff.id),
                    "work_hours": "4.00",
                },
            },
        )

    def test_manual_recalculate_rolls_back_when_required_audit_cannot_be_written(self):
        record = WorkRecord.objects.create(
            tenant=self.tenant,
            staff=self.staff,
            work_type=self.work_type,
            date=date(2026, 8, 1),
            start_time=time(14, 0),
            end_time=time(18, 30),
            work_hours=Decimal("9.50"),
            amount=200_000,
            resolved_hourly_wage=15_000,
            is_manually_edited=True,
        )
        with patch(
            "apps.core.models.OpsAuditLog.objects.create",
            side_effect=RuntimeError("audit unavailable"),
        ):
            request = self._request(
                "post",
                f"/api/v1/staffs/work-records/{record.id}/recalculate/",
            )
            response = WorkRecordViewSet.as_view({"post": "recalculate"})(
                request,
                pk=record.id,
            )

        self.assertEqual(response.status_code, 503, response.data)
        record.refresh_from_db()
        self.assertTrue(record.is_manually_edited)
        self.assertEqual(record.work_hours, Decimal("9.50"))
        self.assertEqual(record.amount, 200_000)

    def test_manual_recalculate_keeps_locked_month_unchanged(self):
        record = WorkRecord.objects.create(
            tenant=self.tenant,
            staff=self.staff,
            work_type=self.work_type,
            date=date(2026, 8, 1),
            start_time=time(14, 0),
            end_time=time(18, 30),
            work_hours=Decimal("9.50"),
            amount=200_000,
            resolved_hourly_wage=15_000,
            is_manually_edited=True,
        )
        WorkMonthLock.objects.create(
            tenant=self.tenant,
            staff=self.staff,
            year=2026,
            month=8,
            is_locked=True,
            locked_by=self.owner,
        )
        request = self._request(
            "post",
            f"/api/v1/staffs/work-records/{record.id}/recalculate/",
        )

        response = WorkRecordViewSet.as_view({"post": "recalculate"})(
            request,
            pk=record.id,
        )

        self.assertEqual(response.status_code, 400, response.data)
        record.refresh_from_db()
        self.assertTrue(record.is_manually_edited)
        self.assertEqual(record.work_hours, Decimal("9.50"))
        self.assertEqual(record.amount, 200_000)
        self.assertFalse(OpsAuditLog.objects.exists())

    def test_manual_recalculate_cannot_reach_another_tenant_record(self):
        other_tenant = Tenant.objects.create(
            code="work-record-audit-other",
            name="Work Record Audit Other",
            is_active=True,
        )
        other_staff = Staff.objects.create(
            tenant=other_tenant,
            name="다른 테넌트 직원",
            phone="",
        )
        other_work_type = WorkType.objects.create(
            tenant=other_tenant,
            name="다른 테넌트 근무",
            base_hourly_wage=15_000,
            is_active=True,
        )
        record = WorkRecord.objects.create(
            tenant=other_tenant,
            staff=other_staff,
            work_type=other_work_type,
            date=date(2026, 8, 1),
            start_time=time(14, 0),
            end_time=time(18, 30),
            work_hours=Decimal("9.50"),
            amount=200_000,
            resolved_hourly_wage=15_000,
            is_manually_edited=True,
        )
        request = self._request(
            "post",
            f"/api/v1/staffs/work-records/{record.id}/recalculate/",
        )

        response = WorkRecordViewSet.as_view({"post": "recalculate"})(
            request,
            pk=record.id,
        )

        self.assertEqual(response.status_code, 404, response.data)
        record.refresh_from_db()
        self.assertTrue(record.is_manually_edited)
        self.assertEqual(record.work_hours, Decimal("9.50"))
        self.assertEqual(record.amount, 200_000)
        self.assertFalse(OpsAuditLog.objects.exists())
