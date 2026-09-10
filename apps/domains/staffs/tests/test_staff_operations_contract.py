import json
from datetime import date, datetime, time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIRequestFactory

from apps.core.models import Tenant, TenantMembership
from apps.domains.staffs.models import (
    ExpenseRecord,
    PayrollReferenceDeductionPolicy,
    PayrollSnapshot,
    Staff,
    StaffWorkType,
    WorkMonthLock,
    WorkRecord,
    WorkType,
)
from apps.domains.staffs.serializers import (
    StaffCreateUpdateSerializer,
    WorkRecordSerializer,
)
from apps.domains.staffs.views import (
    ExpenseRecordViewSet,
    PayrollSnapshotViewSet,
    StaffViewSet,
    WorkMonthLockViewSet,
    WorkRecordViewSet,
)
from apps.domains.staffs.views.helpers import can_access_staff_management
from apps.core.permissions import TenantResolvedAndPayrollManager


User = get_user_model()


class StaffOperationsContractTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="직원계약테스트",
            code="staff-contract-test",
        )
        self.owner = User.objects.create_user(
            username="staff-contract-owner",
            password="1234",
            name="원장",
        )
        TenantMembership.objects.create(
            tenant=self.tenant,
            user=self.owner,
            role="owner",
            is_active=True,
        )
        self.factory = APIRequestFactory()
        self.work_type = WorkType.objects.create(
            tenant=self.tenant,
            name="조교 근무",
            base_hourly_wage=12_000,
        )

    def _request(self, method, path, data=None):
        request = getattr(self.factory, method)(path, data or {}, format="json")
        request.tenant = self.tenant
        request.user = self.owner
        return request

    def _staff(self, name):
        return Staff.objects.create(
            tenant=self.tenant,
            name=name,
            phone="",
        )

    def test_reference_deduction_uses_legacy_won_rounding_and_reconciles_components(self):
        reference = PayrollReferenceDeductionPolicy.calculate(313_250)

        self.assertEqual(reference["business_income_tax"], 9_398)
        self.assertEqual(reference["local_income_tax"], 939)
        self.assertEqual(reference["deduction_total"], 10_337)
        self.assertEqual(reference["net_work_amount"], 302_913)
        self.assertEqual(
            reference["business_income_tax"] + reference["local_income_tax"],
            reference["deduction_total"],
        )

    def test_work_record_amount_uses_exact_paid_minutes_before_display_hour_rounding(self):
        staff = self._staff("분단위 정본 직원")
        work_type = WorkType.objects.create(
            tenant=self.tenant,
            name="분단위 근무",
            base_hourly_wage=15_000,
        )

        record = WorkRecord.objects.create(
            tenant=self.tenant,
            staff=staff,
            work_type=work_type,
            date=date(2026, 8, 15),
            start_time=time(15, 36),
            end_time=time(19, 28),
        )

        self.assertEqual(str(record.work_hours), "3.87")
        self.assertEqual(record.amount, 58_000)

    def test_memo_only_patch_preserves_existing_canonical_payroll_amount(self):
        staff = self._staff("기존 정산 보존 직원")
        work_type = WorkType.objects.create(
            tenant=self.tenant,
            name="기존 분단위 근무",
            base_hourly_wage=15_000,
        )
        record = WorkRecord.objects.create(
            tenant=self.tenant,
            staff=staff,
            work_type=work_type,
            date=date(2026, 8, 15),
            start_time=time(15, 36),
            end_time=time(19, 28),
            work_hours="3.87",
            amount=58_050,
            resolved_hourly_wage=15_000,
            is_manually_edited=True,
        )
        WorkRecord.objects.filter(pk=record.pk).update(
            is_manually_edited=False,
        )

        response = WorkRecordViewSet.as_view({"patch": "partial_update"})(
            self._request(
                "patch",
                f"/staffs/work-records/{record.id}/",
                {"memo": "확인 메모만 변경"},
            ),
            pk=record.id,
        )

        self.assertEqual(response.status_code, 200, response.data)
        record.refresh_from_db()
        self.assertEqual(record.memo, "확인 메모만 변경")
        self.assertEqual(str(record.work_hours), "3.87")
        self.assertEqual(record.amount, 58_050)

    def test_payroll_list_applies_staff_year_month_filters(self):
        selected = self._staff("선택 직원")
        other = self._staff("다른 직원")
        PayrollSnapshot.objects.create(
            tenant=self.tenant,
            staff=selected,
            year=2026,
            month=7,
            total_amount=100_000,
        )
        PayrollSnapshot.objects.create(
            tenant=self.tenant,
            staff=selected,
            year=2026,
            month=6,
            total_amount=200_000,
        )
        PayrollSnapshot.objects.create(
            tenant=self.tenant,
            staff=other,
            year=2026,
            month=7,
            total_amount=300_000,
        )

        request = self._request(
            "get",
            (
                "/staffs/payroll-snapshots/"
                f"?staff={selected.id}&year=2026&month=7"
            ),
        )
        response = PayrollSnapshotViewSet.as_view({"get": "list"})(request)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["staff"], selected.id)
        self.assertEqual(response.data["results"][0]["month"], 7)

    def test_month_work_record_list_is_not_cut_at_twenty_rows(self):
        staff = self._staff("다건 근무자")
        for day in range(1, 22):
            WorkRecord.objects.create(
                tenant=self.tenant,
                staff=staff,
                work_type=self.work_type,
                date=date(2026, 7, day),
                start_time=time(13, 0),
                end_time=time(14, 0),
            )

        request = self._request(
            "get",
            (
                "/staffs/work-records/"
                f"?staff={staff.id}&date_from=2026-07-01&date_to=2026-07-31"
            ),
        )
        response = WorkRecordViewSet.as_view({"get": "list"})(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 21)
        self.assertEqual(len(response.data["results"]), 21)

    def test_delete_rejects_staff_with_payroll_history(self):
        staff = self._staff("퇴사 보존")
        WorkRecord.objects.create(
            tenant=self.tenant,
            staff=staff,
            work_type=self.work_type,
            date=date(2026, 7, 1),
            start_time=time(13, 0),
            end_time=time(14, 0),
        )
        serializer = StaffCreateUpdateSerializer(
            staff,
            context={
                "request": SimpleNamespace(
                    tenant=self.tenant,
                    user=self.owner,
                )
            },
        )

        with self.assertRaisesMessage(ValidationError, "퇴사 처리"):
            serializer.delete(staff)

        self.assertTrue(Staff.objects.filter(pk=staff.pk).exists())
        self.assertTrue(WorkRecord.objects.filter(staff=staff).exists())

    def test_expense_create_cannot_forge_approved_state(self):
        staff = self._staff("비용 직원")
        request = self._request(
            "post",
            "/staffs/expense-records/",
            {
                "staff": staff.id,
                "date": "2026-07-01",
                "title": "교재 구입",
                "amount": 30_000,
                "status": "APPROVED",
            },
        )
        response = ExpenseRecordViewSet.as_view({"post": "create"})(request)

        self.assertEqual(response.status_code, 201)
        expense = ExpenseRecord.objects.get(pk=response.data["id"])
        self.assertEqual(expense.status, "PENDING")
        self.assertIsNone(expense.approved_at)
        self.assertIsNone(expense.approved_by_id)

    def test_expense_create_rejects_zero_amount(self):
        staff = self._staff("0원 비용 직원")
        request = self._request(
            "post",
            "/staffs/expense-records/",
            {
                "staff": staff.id,
                "date": "2026-07-01",
                "title": "잘못된 비용",
                "amount": 0,
            },
        )

        response = ExpenseRecordViewSet.as_view({"post": "create"})(request)

        self.assertEqual(response.status_code, 400)
        self.assertIn("amount", response.data)
        self.assertFalse(ExpenseRecord.objects.filter(staff=staff).exists())

    def test_expense_update_rejects_zero_amount_without_mutation(self):
        staff = self._staff("비용 수정 직원")
        expense = ExpenseRecord.objects.create(
            tenant=self.tenant,
            staff=staff,
            date=date(2026, 7, 1),
            title="교재 구입",
            amount=30_000,
        )
        request = self._request(
            "patch",
            f"/staffs/expense-records/{expense.id}/",
            {"amount": 0},
        )

        response = ExpenseRecordViewSet.as_view({"patch": "partial_update"})(
            request,
            pk=expense.id,
        )

        self.assertEqual(response.status_code, 400)
        expense.refresh_from_db()
        self.assertEqual(expense.amount, 30_000)

    def test_closed_work_record_cannot_start_break(self):
        staff = self._staff("종료 근무자")
        record = WorkRecord.objects.create(
            tenant=self.tenant,
            staff=staff,
            work_type=self.work_type,
            date=date(2026, 7, 1),
            start_time=time(13, 0),
            end_time=time(14, 0),
        )
        request = self._request(
            "post",
            f"/staffs/work-records/{record.id}/start_break/",
        )
        response = WorkRecordViewSet.as_view({"post": "start_break"})(
            request,
            pk=record.id,
        )

        self.assertEqual(response.status_code, 400)
        record.refresh_from_db()
        self.assertIsNone(record.current_break_started_at)

    def test_membership_role_wins_for_same_name_and_blank_phone(self):
        teacher_user = User.objects.create_user(
            username="blank-phone-teacher",
            password="1234",
        )
        assistant_user = User.objects.create_user(
            username="blank-phone-assistant",
            password="1234",
        )
        TenantMembership.objects.create(
            tenant=self.tenant,
            user=teacher_user,
            role="teacher",
            is_active=True,
        )
        TenantMembership.objects.create(
            tenant=self.tenant,
            user=assistant_user,
            role="staff",
            is_active=True,
        )
        teacher_staff = Staff.objects.create(
            tenant=self.tenant,
            user=teacher_user,
            name="동명이인",
            phone="",
        )
        assistant_staff = Staff.objects.create(
            tenant=self.tenant,
            user=assistant_user,
            name="동명이인",
            phone="",
        )

        request = self._request("get", "/staffs/")
        response = StaffViewSet.as_view({"get": "list"})(request)

        roles = {
            row["id"]: row["role"]
            for row in response.data["results"]
        }
        self.assertEqual(roles[teacher_staff.id], "TEACHER")
        self.assertEqual(roles[assistant_staff.id], "ASSISTANT")

    @patch(
        "apps.domains.staffs.views.staff."
        "teacher_repo.teacher_name_phone_keys_tenant"
    )
    def test_ambiguous_legacy_name_phone_role_fails_closed(
        self,
        teacher_keys_mock,
    ):
        first = self._staff("계정 없는 동명이인")
        second = self._staff("계정 없는 동명이인")
        teacher_keys_mock.return_value = {("계정 없는 동명이인", "")}

        request = self._request("get", "/staffs/")
        response = StaffViewSet.as_view({"get": "list"})(request)

        roles = {
            row["id"]: row["role"]
            for row in response.data["results"]
        }
        self.assertEqual(roles[first.id], "ASSISTANT")
        self.assertEqual(roles[second.id], "ASSISTANT")

    def test_work_record_allows_overnight_but_rejects_invalid_break(self):
        staff = self._staff("야간 근무자")
        overnight = WorkRecordSerializer(
            data={
                "staff": staff.id,
                "work_type": self.work_type.id,
                "date": "2026-07-01",
                "start_time": "22:00",
                "end_time": "01:00",
                "break_minutes": 30,
            },
            context={
                "request": SimpleNamespace(
                    tenant=self.tenant,
                    user=self.owner,
                )
            },
        )
        self.assertTrue(overnight.is_valid(), overnight.errors)

        invalid_break = WorkRecordSerializer(
            data={
                "staff": staff.id,
                "work_type": self.work_type.id,
                "date": "2026-07-01",
                "start_time": "22:00",
                "end_time": "23:00",
                "break_minutes": 60,
            },
            context={
                "request": SimpleNamespace(
                    tenant=self.tenant,
                    user=self.owner,
                )
            },
        )
        self.assertFalse(invalid_break.is_valid())
        self.assertIn("break_minutes", invalid_break.errors)

    def test_superuser_tenant_pointer_does_not_grant_payroll_access(self):
        superuser = User.objects.create_superuser(
            username="pointer-only-superuser",
            password="1234",
            tenant=self.tenant,
        )

        self.assertFalse(
            can_access_staff_management(superuser, self.tenant)
        )

    def test_self_clock_in_rejects_unassigned_work_type(self):
        user = User.objects.create_user(
            username="unassigned-clock-user",
            password="1234",
        )
        TenantMembership.objects.create(
            tenant=self.tenant,
            user=user,
            role="staff",
            is_active=True,
        )
        staff = Staff.objects.create(
            tenant=self.tenant,
            user=user,
            name="미배정 조교",
        )
        request = self.factory.post(
            f"/staffs/{staff.id}/work-records/start-work/",
            {"work_type": self.work_type.id},
            format="json",
        )
        request.tenant = self.tenant
        request.user = user

        response = StaffViewSet.as_view({"post": "start_work"})(
            request,
            pk=staff.id,
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(WorkRecord.objects.filter(staff=staff).exists())

    def test_staff_summary_allows_self_and_denies_other_staff(self):
        user = User.objects.create_user(
            username="self-payroll-summary-user",
            password="1234",
        )
        TenantMembership.objects.create(
            tenant=self.tenant,
            user=user,
            role="staff",
            is_active=True,
        )
        staff = Staff.objects.create(
            tenant=self.tenant,
            user=user,
            name="본인 급여 조교",
        )
        other = self._staff("다른 조교")
        WorkRecord.objects.create(
            tenant=self.tenant,
            staff=staff,
            work_type=self.work_type,
            date=date(2026, 8, 3),
            start_time=time(9, 0),
            end_time=time(10, 0),
        )
        ExpenseRecord.objects.create(
            tenant=self.tenant,
            staff=staff,
            date=date(2026, 8, 4),
            title="교통비",
            amount=3_000,
            status="APPROVED",
        )

        def request_for(target):
            request = self.factory.get(
                f"/staffs/{target.id}/summary/",
                {"date_from": "2026-08-01", "date_to": "2026-08-31"},
            )
            request.tenant = self.tenant
            request.user = user
            return StaffViewSet.as_view({"get": "summary"})(request, pk=target.id)

        own_response = request_for(staff)
        self.assertEqual(own_response.status_code, 200, own_response.data)
        self.assertEqual(own_response.data["work_amount"], 12_000)
        self.assertEqual(own_response.data["reference_deduction_total"], 396)
        self.assertEqual(own_response.data["reference_transfer_amount"], 14_604)
        self.assertEqual(request_for(other).status_code, 403)

    def test_self_end_work_rejects_adjustment_amount(self):
        user = User.objects.create_user(
            username="self-adjust-user",
            password="1234",
        )
        TenantMembership.objects.create(
            tenant=self.tenant,
            user=user,
            role="staff",
            is_active=True,
        )
        staff = Staff.objects.create(
            tenant=self.tenant,
            user=user,
            name="자가 조정 조교",
        )
        StaffWorkType.objects.create(
            tenant=self.tenant,
            staff=staff,
            work_type=self.work_type,
        )
        record = WorkRecord.objects.create(
            tenant=self.tenant,
            staff=staff,
            work_type=self.work_type,
            date=timezone.localdate(),
            start_time=time(9, 0),
        )
        request = self.factory.post(
            f"/staffs/work-records/{record.id}/end_work/",
            {"adjustment_amount": 10_000_000},
            format="json",
        )
        request.tenant = self.tenant
        request.user = user

        response = WorkRecordViewSet.as_view({"post": "end_work"})(
            request,
            pk=record.id,
        )

        self.assertEqual(response.status_code, 403)
        record.refresh_from_db()
        self.assertIsNone(record.end_time)
        self.assertEqual(record.adjustment_amount, 0)

    def test_manager_cannot_reset_owner_password(self):
        owner_staff = Staff.objects.create(
            tenant=self.tenant,
            user=self.owner,
            name="대표",
        )
        manager_user = User.objects.create_user(
            username="staff-manager",
            password="1234",
        )
        TenantMembership.objects.create(
            tenant=self.tenant,
            user=manager_user,
            role="staff",
            is_active=True,
        )
        Staff.objects.create(
            tenant=self.tenant,
            user=manager_user,
            name="관리 조교",
            is_manager=True,
        )
        request = self.factory.post(
            f"/staffs/{owner_staff.id}/change-password/",
            {"password": "5678"},
            format="json",
        )
        request.tenant = self.tenant
        request.user = manager_user

        response = StaffViewSet.as_view({"post": "change_password"})(
            request,
            pk=owner_staff.id,
        )

        self.assertEqual(response.status_code, 403)
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.check_password("1234"))

    def test_processed_expense_cannot_be_deleted(self):
        staff = self._staff("승인 비용 직원")
        expense = ExpenseRecord.objects.create(
            tenant=self.tenant,
            staff=staff,
            date=date(2026, 7, 1),
            title="승인 교재비",
            amount=30_000,
            status="APPROVED",
            approved_by=self.owner,
            approved_at=timezone.now(),
        )
        request = self._request(
            "delete",
            f"/staffs/expense-records/{expense.id}/",
        )

        response = ExpenseRecordViewSet.as_view({"delete": "destroy"})(
            request,
            pk=expense.id,
        )

        self.assertEqual(response.status_code, 400)
        self.assertTrue(ExpenseRecord.objects.filter(pk=expense.id).exists())

    def test_breaking_record_cannot_be_closed_by_generic_patch(self):
        staff = self._staff("휴게 근무자")
        record = WorkRecord.objects.create(
            tenant=self.tenant,
            staff=staff,
            work_type=self.work_type,
            date=date(2026, 7, 1),
            start_time=time(13, 0),
            current_break_started_at=timezone.now(),
        )
        request = self._request(
            "patch",
            f"/staffs/work-records/{record.id}/",
            {"end_time": "14:00"},
        )

        response = WorkRecordViewSet.as_view({"patch": "partial_update"})(
            request,
            pk=record.id,
        )

        self.assertEqual(response.status_code, 400)
        record.refresh_from_db()
        self.assertIsNone(record.end_time)
        self.assertIsNotNone(record.current_break_started_at)

    def test_new_monthly_pay_type_is_rejected(self):
        staff = self._staff("시급 직원")
        serializer = StaffCreateUpdateSerializer(
            staff,
            data={"pay_type": "MONTHLY"},
            partial=True,
            context={
                "request": SimpleNamespace(
                    tenant=self.tenant,
                    user=self.owner,
                )
            },
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("pay_type", serializer.errors)

    def test_payroll_snapshot_freezes_staff_name(self):
        staff = self._staff("마감 당시 이름")
        snapshot = PayrollSnapshot.objects.create(
            tenant=self.tenant,
            staff=staff,
            year=2026,
            month=7,
            total_amount=100_000,
        )

        staff.name = "변경된 이름"
        staff.save(update_fields=["name"])
        snapshot.refresh_from_db()

        self.assertEqual(snapshot.staff_name, "마감 당시 이름")

    def test_created_account_can_use_configured_password_without_recommendation(self):
        request = SimpleNamespace(tenant=self.tenant, user=self.owner)
        serializer = StaffCreateUpdateSerializer(
            data={
                "username": "new-assistant",
                "password": "1234",
                "name": "신규 조교",
                "role": "ASSISTANT",
            },
            context={"request": request},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        staff = serializer.save()

        self.assertIsNotNone(staff.user)
        self.assertTrue(staff.user.check_password("1234"))
        self.assertFalse(staff.user.must_change_password)

    def test_non_manager_staff_lacks_payroll_management_permission(self):
        user = User.objects.create_user(
            username="teacher-roster-reader",
            password="1234",
        )
        TenantMembership.objects.create(
            tenant=self.tenant,
            user=user,
            role="staff",
            is_active=True,
        )
        Staff.objects.create(
            tenant=self.tenant,
            user=user,
            name="일반 조교",
            is_manager=False,
        )
        request = self.factory.post("/teachers/", {}, format="json")
        request.tenant = self.tenant
        request.user = user

        self.assertFalse(
            TenantResolvedAndPayrollManager().has_permission(
                request,
                view=None,
            )
        )

    def test_legacy_monthly_staff_cannot_be_auto_closed(self):
        staff = self._staff("기존 월급 직원")
        Staff.objects.filter(pk=staff.pk).update(pay_type="MONTHLY")
        request = self._request(
            "post",
            "/staffs/work-month-locks/",
            {"staff": staff.id, "year": 2026, "month": 7},
        )

        response = WorkMonthLockViewSet.as_view({"post": "create"})(request)

        self.assertEqual(response.status_code, 400)
        self.assertIn("월급 직원", str(response.data))
        self.assertFalse(staff.payroll_snapshots.exists())

    def test_rejected_expense_is_immutable(self):
        staff = self._staff("반려 환급 직원")
        expense = ExpenseRecord.objects.create(
            tenant=self.tenant,
            staff=staff,
            date=date(2026, 7, 1),
            title="반려된 교재 선결제",
            amount=30_000,
            status="REJECTED",
            approved_by=self.owner,
            approved_at=timezone.now(),
        )
        request = self._request(
            "patch",
            f"/staffs/expense-records/{expense.id}/",
            {"amount": 90_000},
        )

        response = ExpenseRecordViewSet.as_view({"patch": "partial_update"})(
            request,
            pk=expense.id,
        )

        self.assertEqual(response.status_code, 400)
        expense.refresh_from_db()
        self.assertEqual(expense.amount, 30_000)

    def test_end_work_rejects_meal_time_covering_entire_shift(self):
        staff = self._staff("식사시간 검증 직원")
        record = WorkRecord.objects.create(
            tenant=self.tenant,
            staff=staff,
            work_type=self.work_type,
            date=date(2026, 7, 1),
            start_time=time(9, 0),
        )
        request = self._request(
            "post",
            f"/staffs/work-records/{record.id}/end_work/",
            {"meal_minutes": 60},
        )
        fixed_now = timezone.make_aware(datetime(2026, 7, 1, 10, 0))

        with patch(
            "apps.domains.staffs.views.work_record.timezone.now",
            return_value=fixed_now,
        ):
            response = WorkRecordViewSet.as_view({"post": "end_work"})(
                request,
                pk=record.id,
            )

        self.assertEqual(response.status_code, 400)
        record.refresh_from_db()
        self.assertIsNone(record.end_time)

    def test_clock_in_freezes_the_resolved_hourly_wage(self):
        staff = self._staff("단가 고정 직원")
        record = WorkRecord.objects.create(
            tenant=self.tenant,
            staff=staff,
            work_type=self.work_type,
            date=date(2026, 7, 1),
            start_time=time(9, 0),
        )
        self.assertEqual(record.resolved_hourly_wage, 12_000)

        self.work_type.base_hourly_wage = 20_000
        self.work_type.save(update_fields=["base_hourly_wage"])
        record.end_time = time(10, 0)
        record.save(recalculate_payroll=True)

        self.assertEqual(record.resolved_hourly_wage, 12_000)
        self.assertEqual(record.amount, 12_000)

    def test_admin_profile_edit_does_not_downgrade_membership(self):
        admin_user = User.objects.create_user(
            username="profile-admin",
            password="1234",
        )
        membership = TenantMembership.objects.create(
            tenant=self.tenant,
            user=admin_user,
            role="admin",
            is_active=True,
        )
        staff = Staff.objects.create(
            tenant=self.tenant,
            user=admin_user,
            name="기존 관리자",
        )
        serializer = StaffCreateUpdateSerializer(
            staff,
            data={"name": "수정 관리자", "role": "ASSISTANT"},
            partial=True,
            context={
                "request": SimpleNamespace(
                    tenant=self.tenant,
                    user=self.owner,
                )
            },
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        updated = serializer.save()
        membership.refresh_from_db()

        self.assertEqual(updated.name, "수정 관리자")
        self.assertEqual(membership.role, "admin")

    def test_staff_me_requires_choice_when_multiple_work_types_assigned(self):
        user = User.objects.create_user(
            username="multi-rate-staff",
            password="1234",
        )
        TenantMembership.objects.create(
            tenant=self.tenant,
            user=user,
            role="staff",
            is_active=True,
        )
        staff = Staff.objects.create(
            tenant=self.tenant,
            user=user,
            name="복수 유형 조교",
        )
        other_work_type = WorkType.objects.create(
            tenant=self.tenant,
            name="시험 감독",
            base_hourly_wage=15_000,
        )
        StaffWorkType.objects.create(
            tenant=self.tenant,
            staff=staff,
            work_type=self.work_type,
        )
        StaffWorkType.objects.create(
            tenant=self.tenant,
            staff=staff,
            work_type=other_work_type,
        )
        request = self.factory.get("/staffs/me/")
        request.tenant = self.tenant
        request.user = user

        response = StaffViewSet.as_view({"get": "me"})(request)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data["assigned_work_types"]), 2)
        self.assertNotIn("default_work_type_id", response.data)

    @patch("apps.domains.staffs.views.payroll_snapshot.dispatch_staffs_ai_job")
    def test_export_payload_fixes_snapshot_ids_and_ignores_later_hires(
        self,
        dispatch_mock,
    ):
        worked_staff = self._staff("1월 근무자")
        later_hire = self._staff("7월 입사자")
        WorkRecord.objects.create(
            tenant=self.tenant,
            staff=worked_staff,
            work_type=self.work_type,
            date=date(2026, 1, 10),
            start_time=time(9, 0),
            end_time=time(10, 0),
        )
        snapshot = PayrollSnapshot.objects.create(
            tenant=self.tenant,
            staff=worked_staff,
            year=2026,
            month=1,
            total_amount=12_000,
        )
        dispatch_mock.return_value = {"ok": True, "job_id": "job-1"}
        request = self._request(
            "post",
            "/staffs/payroll-snapshots/export-excel/",
            {"year": 2026, "month": 1},
        )

        response = PayrollSnapshotViewSet.as_view({"post": "export_excel"})(
            request
        )

        self.assertEqual(response.status_code, 202)
        payload = dispatch_mock.call_args.kwargs["payload"]
        self.assertEqual(payload["snapshot_ids"], [snapshot.id])
        self.assertTrue(payload["revision"])
        self.assertNotEqual(worked_staff.id, later_hire.id)

    def test_position_is_independent_from_account_role_and_management_access(self):
        serializer = StaffCreateUpdateSerializer(
            data={
                "username": "academy-director",
                "password": "1234",
                "name": "운영 실장",
                "position": "DIRECTOR",
                "role": "ASSISTANT",
                "is_manager": True,
            },
            context={
                "request": SimpleNamespace(
                    tenant=self.tenant,
                    user=self.owner,
                )
            },
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        staff = serializer.save()
        membership = TenantMembership.objects.get(
            tenant=self.tenant,
            user=staff.user,
        )

        self.assertEqual(staff.position, "DIRECTOR")
        self.assertTrue(staff.is_manager)
        self.assertEqual(membership.role, "staff")

        response = StaffViewSet.as_view({"get": "list"})(
            self._request("get", "/staffs/")
        )
        row = next(
            item
            for item in response.data["results"]
            if item["id"] == staff.id
        )
        self.assertEqual(row["position"], "DIRECTOR")
        self.assertEqual(row["position_label"], "실장")
        self.assertEqual(row["account_role"], "STAFF")
        self.assertTrue(row["can_manage_staff"])

    def test_admin_account_reports_effective_management_and_cannot_be_toggled(self):
        admin_user = User.objects.create_user(
            username="staff-admin-account",
            password="1234",
        )
        admin_membership = TenantMembership.objects.create(
            tenant=self.tenant,
            user=admin_user,
            role="admin",
            is_active=True,
        )
        staff = Staff.objects.create(
            tenant=self.tenant,
            user=admin_user,
            name="관리자 실장",
            position="DIRECTOR",
            is_manager=False,
        )

        list_response = StaffViewSet.as_view({"get": "list"})(
            self._request("get", "/staffs/")
        )
        row = next(
            item
            for item in list_response.data["results"]
            if item["id"] == staff.id
        )
        self.assertEqual(row["account_role"], "ADMIN")
        self.assertTrue(row["can_manage_staff"])

        patch_response = StaffViewSet.as_view(
            {"patch": "partial_update"}
        )(
            self._request(
                "patch",
                f"/staffs/{staff.id}/",
                {"is_manager": True},
            ),
            pk=staff.id,
        )
        self.assertEqual(patch_response.status_code, 400)
        staff.refresh_from_db()
        self.assertFalse(staff.is_manager)

        Staff.objects.filter(pk=staff.pk).update(is_manager=True)
        admin_membership.is_active = False
        admin_membership.save(update_fields=["is_active"])
        inactive_detail = StaffViewSet.as_view({"get": "retrieve"})(
            self._request("get", f"/staffs/{staff.id}/"),
            pk=staff.id,
        )
        self.assertEqual(inactive_detail.data["account_role"], "NONE")
        self.assertFalse(inactive_detail.data["can_manage_staff"])

    def test_payroll_overview_is_tenant_scoped_and_surfaces_close_blockers(self):
        ready = Staff.objects.create(
            tenant=self.tenant,
            name="정산 가능 실장",
            position="DIRECTOR",
        )
        StaffWorkType.objects.create(
            tenant=self.tenant,
            staff=ready,
            work_type=self.work_type,
        )
        WorkRecord.objects.create(
            tenant=self.tenant,
            staff=ready,
            work_type=self.work_type,
            date=date(2026, 8, 3),
            start_time=time(9, 0),
            end_time=time(10, 0),
        )
        WorkRecord.objects.create(
            tenant=self.tenant,
            staff=ready,
            work_type=self.work_type,
            date=date(2026, 8, 3),
            start_time=time(9, 0),
            end_time=time(10, 0),
            work_hours=1,
            amount=12_000,
            resolved_hourly_wage=12_000,
            is_manually_edited=True,
        )
        WorkRecord.objects.create(
            tenant=self.tenant,
            staff=ready,
            work_type=self.work_type,
            date=date(2026, 8, 6),
            start_time=time(8, 0),
            end_time=time(21, 0),
        )
        ExpenseRecord.objects.create(
            tenant=self.tenant,
            staff=ready,
            date=date(2026, 8, 4),
            title="교재 선결제",
            amount=3_000,
            status="APPROVED",
        )

        review = Staff.objects.create(
            tenant=self.tenant,
            name="확인 필요 조교",
            position="ASSISTANT",
        )
        ExpenseRecord.objects.create(
            tenant=self.tenant,
            staff=review,
            date=date(2026, 8, 5),
            title="대기 비용",
            amount=2_000,
            status="PENDING",
        )

        other_tenant = Tenant.objects.create(
            name="다른 학원",
            code="staff-overview-other",
        )
        Staff.objects.create(
            tenant=other_tenant,
            name="다른 학원 직원",
            position="DIRECTOR",
        )

        response = StaffViewSet.as_view(
            {"get": "payroll_overview"}
        )(
            self._request(
                "get",
                "/staffs/payroll-overview/?year=2026&month=8",
            )
        )

        self.assertEqual(response.status_code, 200, response.data)
        rows = {row["staff_id"]: row for row in response.data["rows"]}
        self.assertEqual(set(rows), {ready.id, review.id})
        self.assertEqual(rows[ready.id]["work_hours"], 15.0)
        self.assertEqual(rows[ready.id]["work_amount"], 180_000)
        self.assertEqual(rows[ready.id]["approved_expense_amount"], 3_000)
        self.assertEqual(rows[ready.id]["total_amount"], 183_000)
        self.assertEqual(rows[ready.id]["reference_deduction_total"], 5_940)
        self.assertEqual(rows[ready.id]["reference_net_work_amount"], 174_060)
        self.assertEqual(rows[ready.id]["reference_transfer_amount"], 177_060)
        self.assertEqual(rows[ready.id]["duplicate_work_record_count"], 2)
        self.assertEqual(rows[ready.id]["abnormal_long_work_record_count"], 1)
        self.assertEqual(rows[ready.id]["manually_edited_work_record_count"], 1)
        self.assertEqual(
            rows[ready.id]["work_type_breakdown"],
            [{
                "work_type_id": self.work_type.id,
                "work_type_name": "조교 근무",
                "color": "#4CAF50",
                "record_count": 3,
                "work_hours": 15.0,
                "work_amount": 180_000,
            }],
        )
        self.assertEqual(rows[ready.id]["settlement_status"], "OPEN")
        self.assertTrue(rows[ready.id]["can_close"])
        self.assertEqual(rows[review.id]["pending_expense_count"], 1)
        self.assertEqual(rows[review.id]["pending_expense_amount"], 2_000)
        self.assertEqual(rows[review.id]["settlement_status"], "NEEDS_REVIEW")
        self.assertFalse(rows[review.id]["can_close"])
        self.assertEqual(response.data["totals"]["total_amount"], 183_000)
        self.assertEqual(response.data["totals"]["advisory_issue_count"], 4)
        self.assertEqual(response.data["totals"]["reference_transfer_amount"], 177_060)
        self.assertEqual(response.data["totals"]["needs_review_count"], 1)

        WorkMonthLock.objects.create(
            tenant=self.tenant,
            staff=ready,
            year=2026,
            month=8,
        )
        PayrollSnapshot.objects.create(
            tenant=self.tenant,
            staff=ready,
            year=2026,
            month=8,
            work_hours=1,
            work_amount=12_000,
            approved_expense_amount=3_000,
            total_amount=15_000,
        )
        StaffWorkType.objects.filter(staff=ready).delete()

        closed_response = StaffViewSet.as_view(
            {"get": "payroll_overview"}
        )(
            self._request(
                "get",
                "/staffs/payroll-overview/?year=2026&month=8",
            )
        )
        closed_rows = {
            row["staff_id"]: row for row in closed_response.data["rows"]
        }
        self.assertEqual(
            closed_rows[ready.id]["settlement_status"],
            "CLOSED",
        )
        self.assertEqual(
            closed_response.data["totals"]["needs_review_count"],
            1,
        )

    def test_payroll_overview_rejects_invalid_month(self):
        response = StaffViewSet.as_view(
            {"get": "payroll_overview"}
        )(
            self._request(
                "get",
                "/staffs/payroll-overview/?year=2026&month=13",
            )
        )

        self.assertEqual(response.status_code, 400)

    def test_payroll_overview_openapi_contract_declares_query_and_response(self):
        schema_path = Path(__file__).resolve().parents[4] / "schema" / "openapi.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        operation = schema["paths"]["/api/v1/staffs/payroll-overview/"]["get"]

        required_query = {
            parameter["name"]
            for parameter in operation["parameters"]
            if parameter["in"] == "query" and parameter.get("required")
        }
        self.assertEqual(required_query, {"year", "month"})
        response_schema = operation["responses"]["200"]["content"][
            "application/json"
        ]["schema"]
        self.assertEqual(
            response_schema["$ref"],
            "#/components/schemas/StaffPayrollOverview",
        )
        row_properties = schema["components"]["schemas"][
            "StaffPayrollOverviewRow"
        ]["properties"]
        self.assertIn("reference_transfer_amount", row_properties)
        self.assertIn("work_type_breakdown", row_properties)
        self.assertIn("advisory_issue_count", row_properties)
