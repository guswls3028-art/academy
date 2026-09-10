"""
Staff 도메인 운영 안정화 테스트.
- Staff-Teacher 연동 (이름/전화 변경, 비활성화/재활성화, 동시 변경)
- Staff 삭제 정책 (Owner 삭제 방지, Teacher cascade)
- Staff role 판별
- 비밀번호 변경
"""
from datetime import date, time

from django.test import TestCase, RequestFactory
from django.contrib.auth import get_user_model
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.core.models.tenant import Tenant
from apps.core.models.tenant_membership import TenantMembership
from apps.core.models.ops_audit import OpsAuditLog
from apps.domains.staffs.models import (
    ExpenseRecord,
    PayrollSnapshot,
    Staff,
    StaffWorkType,
    WorkMonthLock,
    WorkRecord,
    WorkType,
)
from apps.domains.staffs.serializers import StaffCreateUpdateSerializer, StaffListSerializer
from apps.domains.staffs.views import StaffViewSet
from apps.domains.staffs.views.helpers import can_access_staff_management
from apps.domains.staffs.views.work_month_lock import WorkMonthLockViewSet
from academy.adapters.db.django import repositories_teachers as teacher_repo

User = get_user_model()


def _make_tenant(name="테스트학원"):
    return Tenant.objects.create(name=name, code=name.lower().replace(" ", ""))


def _make_request(tenant, user=None):
    factory = RequestFactory()
    request = factory.get("/")
    request.tenant = tenant
    if user:
        request.user = user
    return request


def _create_staff_teacher(tenant, name="김강사", phone="01011112222"):
    """Staff(TEACHER role) + Teacher 레코드를 함께 생성."""
    user = User.objects.create_user(
        username=f"t{tenant.id}_{name}",
        password="test1234",
        name=name,
        phone=phone,
        tenant=tenant,
    )
    user.is_staff = True
    user.save(update_fields=["is_staff"])

    staff = Staff.objects.create(
        tenant=tenant,
        user=user,
        name=name,
        phone=phone,
    )
    teacher_repo.teacher_create(tenant, name, phone or "", is_active=True)
    TenantMembership.objects.create(
        tenant=tenant,
        user=user,
        role="teacher",
        is_active=True,
    )
    return staff


class TestStaffTeacherNameSync(TestCase):
    """이슈 2: Staff 이름 변경 시 Teacher 레코드 동기화."""

    def setUp(self):
        self.tenant = _make_tenant()
        self.staff = _create_staff_teacher(self.tenant, name="김강사", phone="01011112222")

    def test_name_change_syncs_teacher(self):
        """Staff 이름 변경 → Teacher.name도 변경됨."""
        request = _make_request(self.tenant, self.staff.user)
        serializer = StaffCreateUpdateSerializer(
            self.staff,
            data={"name": "김선생"},
            partial=True,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        serializer.update(self.staff, serializer.validated_data)

        teacher = teacher_repo.teacher_filter_tenant(self.tenant).get(phone="01011112222")
        self.assertEqual(teacher.name, "김선생")

    def test_phone_change_syncs_teacher(self):
        """Staff 전화번호 변경 → Teacher.phone도 변경됨."""
        request = _make_request(self.tenant, self.staff.user)
        serializer = StaffCreateUpdateSerializer(
            self.staff,
            data={"phone": "01099998888"},
            partial=True,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        serializer.update(self.staff, serializer.validated_data)

        teacher = teacher_repo.teacher_filter_tenant(self.tenant).get(name="김강사")
        self.assertEqual(teacher.phone, "01099998888")

    def test_name_and_phone_change_together(self):
        """이름 + 전화번호 동시 변경."""
        request = _make_request(self.tenant, self.staff.user)
        serializer = StaffCreateUpdateSerializer(
            self.staff,
            data={"name": "박강사", "phone": "01033334444"},
            partial=True,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        serializer.update(self.staff, serializer.validated_data)

        self.assertFalse(teacher_repo.teacher_filter_tenant(self.tenant).filter(name="김강사").exists())
        teacher = teacher_repo.teacher_filter_tenant(self.tenant).get(name="박강사")
        self.assertEqual(teacher.phone, "01033334444")


class TestStaffTeacherDeactivateSync(TestCase):
    """이슈 3: 이름 변경 + 비활성화 동시 요청 시 Teacher 비활성화도 정확히 동작."""

    def setUp(self):
        self.tenant = _make_tenant()
        self.staff = _create_staff_teacher(self.tenant, name="이강사", phone="01055556666")

    def test_deactivate_syncs_teacher(self):
        """Staff 비활성화 → Teacher.is_active=False."""
        request = _make_request(self.tenant, self.staff.user)
        serializer = StaffCreateUpdateSerializer(
            self.staff,
            data={"is_active": False},
            partial=True,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        serializer.update(self.staff, serializer.validated_data)

        teacher = teacher_repo.teacher_filter_tenant(self.tenant).get(name="이강사")
        self.assertFalse(teacher.is_active)

    def test_reactivate_syncs_teacher(self):
        """Staff 재활성화 → Teacher.is_active=True."""
        self.staff.is_active = False
        self.staff.save(update_fields=["is_active"])
        teacher_repo.teacher_update_is_active_by_name_phone(
            self.tenant, "이강사", "01055556666", False,
        )

        request = _make_request(self.tenant, self.staff.user)
        serializer = StaffCreateUpdateSerializer(
            self.staff,
            data={"is_active": True, "role": "TEACHER"},
            partial=True,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        serializer.update(self.staff, serializer.validated_data)

        teacher = teacher_repo.teacher_filter_tenant(self.tenant).get(name="이강사")
        self.assertTrue(teacher.is_active)

    def test_name_change_and_deactivate_simultaneously(self):
        """이름 변경 + 비활성화 동시 → Teacher가 새 이름으로 비활성화됨."""
        request = _make_request(self.tenant, self.staff.user)
        serializer = StaffCreateUpdateSerializer(
            self.staff,
            data={"name": "최강사", "is_active": False},
            partial=True,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        serializer.update(self.staff, serializer.validated_data)

        # 구 이름 Teacher는 없어야 함
        self.assertFalse(teacher_repo.teacher_filter_tenant(self.tenant).filter(name="이강사").exists())
        # 새 이름 Teacher가 비활성 상태여야 함
        teacher = teacher_repo.teacher_filter_tenant(self.tenant).get(name="최강사")
        self.assertFalse(teacher.is_active)


class TestStaffRoleDetection(TestCase):
    """Staff role 판별: Teacher 존재 시 TEACHER, 없으면 ASSISTANT."""

    def setUp(self):
        self.tenant = _make_tenant()

    def test_role_teacher_when_teacher_exists(self):
        """Teacher 레코드가 있으면 role=TEACHER."""
        staff = _create_staff_teacher(self.tenant, name="강사A", phone="01077778888")
        request = _make_request(self.tenant, staff.user)
        serializer = StaffListSerializer(staff, context={"request": request})
        self.assertEqual(serializer.data["role"], "TEACHER")

    def test_role_assistant_when_no_teacher(self):
        """Teacher 레코드가 없으면 role=ASSISTANT."""
        staff = Staff.objects.create(tenant=self.tenant, name="조교A", phone="01099990000")
        request = _make_request(self.tenant)
        serializer = StaffListSerializer(staff, context={"request": request})
        self.assertEqual(serializer.data["role"], "ASSISTANT")

    def test_role_stays_teacher_after_name_change(self):
        """이름 변경 후에도 Teacher 연동이 유지되어 role=TEACHER."""
        staff = _create_staff_teacher(self.tenant, name="원래이름", phone="01012340000")
        request = _make_request(self.tenant, staff.user)

        # 이름 변경
        update_ser = StaffCreateUpdateSerializer(
            staff,
            data={"name": "변경이름"},
            partial=True,
            context={"request": request},
        )
        update_ser.is_valid(raise_exception=True)
        update_ser.update(staff, update_ser.validated_data)

        staff.refresh_from_db()
        list_ser = StaffListSerializer(staff, context={"request": request})
        self.assertEqual(list_ser.data["role"], "TEACHER")


class TestStaffManagementPermissions(TestCase):
    """직원관리 민감 권한과 직원 본인 출퇴근 권한을 분리한다."""

    def setUp(self):
        self.factory = APIRequestFactory()
        self.tenant = _make_tenant()
        self.staff = _create_staff_teacher(self.tenant, name="일반강사", phone="01012121212")
        self.work_type = WorkType.objects.create(
            tenant=self.tenant,
            name="기본",
            base_hourly_wage=10000,
            is_active=True,
        )
        StaffWorkType.objects.create(
            tenant=self.tenant,
            staff=self.staff,
            work_type=self.work_type,
        )

    def _start_work(self, staff, user, work_type_id):
        from apps.domains.staffs.views import StaffViewSet

        request = self.factory.post(
            f"/staffs/{staff.id}/work-records/start-work/",
            {"work_type": work_type_id},
            format="json",
        )
        force_authenticate(request, user=user)
        request.tenant = self.tenant
        view = StaffViewSet.as_view({"post": "start_work"})
        return view(request, pk=staff.id)

    def test_non_manager_teacher_cannot_access_staff_management(self):
        self.assertFalse(can_access_staff_management(self.staff.user, self.tenant))

    def test_legacy_manager_teacher_cannot_access_staff_management(self):
        self.staff.is_manager = True
        self.staff.save(update_fields=["is_manager"])

        self.assertFalse(can_access_staff_management(self.staff.user, self.tenant))

    def test_non_manager_assistant_cannot_access_staff_management(self):
        assistant_user = User.objects.create_user(
            username=f"t{self.tenant.id}_assistant",
            password="test1234",
            name="일반조교",
            tenant=self.tenant,
        )
        assistant = Staff.objects.create(
            tenant=self.tenant,
            user=assistant_user,
            name="일반조교",
            phone="01045454545",
            is_manager=False,
        )
        TenantMembership.objects.create(
            tenant=self.tenant,
            user=assistant_user,
            role="staff",
            is_active=True,
        )

        self.assertFalse(can_access_staff_management(assistant.user, self.tenant))

    def test_legacy_manager_assistant_cannot_access_staff_management(self):
        assistant_user = User.objects.create_user(
            username=f"t{self.tenant.id}_manager_assistant",
            password="test1234",
            name="관리조교",
            tenant=self.tenant,
        )
        assistant = Staff.objects.create(
            tenant=self.tenant,
            user=assistant_user,
            name="관리조교",
            phone="01056565656",
            is_manager=True,
        )
        TenantMembership.objects.create(
            tenant=self.tenant,
            user=assistant_user,
            role="staff",
            is_active=True,
        )

        self.assertFalse(can_access_staff_management(assistant.user, self.tenant))

    def test_admin_membership_can_access_staff_management_without_staff_profile(self):
        admin_user = User.objects.create_user(
            username=f"t{self.tenant.id}_admin",
            password="test1234",
            name="운영자",
            tenant=self.tenant,
        )
        TenantMembership.objects.create(
            tenant=self.tenant,
            user=admin_user,
            role="admin",
            is_active=True,
        )

        self.assertTrue(can_access_staff_management(admin_user, self.tenant))

    def test_non_manager_teacher_can_start_own_work_with_assigned_work_type(self):
        response = self._start_work(self.staff, self.staff.user, self.work_type.id)

        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(
            WorkRecord.objects.filter(
                tenant=self.tenant,
                staff=self.staff,
                work_type=self.work_type,
                end_time__isnull=True,
            ).exists()
        )

    def test_non_manager_staff_can_read_only_own_canonical_work_history(self):
        own_record = WorkRecord.objects.create(
            tenant=self.tenant,
            staff=self.staff,
            work_type=self.work_type,
            date=date(2026, 8, 19),
            start_time=time(13, 0),
            end_time=time(15, 0),
        )
        other_staff = _create_staff_teacher(
            self.tenant,
            name="다른강사",
            phone="01034343434",
        )
        WorkRecord.objects.create(
            tenant=self.tenant,
            staff=other_staff,
            work_type=self.work_type,
            date=date(2026, 8, 19),
            start_time=time(9, 0),
            end_time=time(10, 0),
        )

        request = self.factory.get(
            f"/staffs/{self.staff.id}/work-records/",
            {"date_from": "2026-08-01", "date_to": "2026-08-31"},
        )
        force_authenticate(request, user=self.staff.user)
        request.tenant = self.tenant
        response = StaffViewSet.as_view({"get": "work_records"})(
            request,
            pk=self.staff.id,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["id"], own_record.id)
        self.assertEqual(response.data["results"][0]["work_type_name"], "기본")
        self.assertEqual(response.data["results"][0]["amount"], 20_000)

        summary_request = self.factory.get(
            f"/staffs/{self.staff.id}/summary/",
            {"date_from": "2026-08-01", "date_to": "2026-08-31"},
        )
        force_authenticate(summary_request, user=self.staff.user)
        summary_request.tenant = self.tenant
        summary = StaffViewSet.as_view({"get": "summary"})(
            summary_request,
            pk=self.staff.id,
        )
        self.assertEqual(summary.status_code, 200, summary.data)
        self.assertEqual(summary.data["work_hours"], 2)
        self.assertEqual(summary.data["work_amount"], 20_000)

        other_request = self.factory.get(
            f"/staffs/{other_staff.id}/work-records/",
            {"date_from": "2026-08-01", "date_to": "2026-08-31"},
        )
        force_authenticate(other_request, user=self.staff.user)
        other_request.tenant = self.tenant
        denied = StaffViewSet.as_view({"get": "work_records"})(
            other_request,
            pk=other_staff.id,
        )
        self.assertEqual(denied.status_code, 403, denied.data)

        denied_summary = StaffViewSet.as_view({"get": "summary"})(
            other_request,
            pk=other_staff.id,
        )
        self.assertEqual(denied_summary.status_code, 403, denied_summary.data)

    def test_current_work_exposes_selected_type_and_clock_in_wage(self):
        record = WorkRecord.objects.create(
            tenant=self.tenant,
            staff=self.staff,
            work_type=self.work_type,
            date=date(2026, 8, 19),
            start_time=time(13, 0),
        )
        request = self.factory.get(
            f"/staffs/{self.staff.id}/work-records/current/"
        )
        force_authenticate(request, user=self.staff.user)
        request.tenant = self.tenant

        response = StaffViewSet.as_view({"get": "work_current"})(
            request,
            pk=self.staff.id,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["work_record_id"], record.id)
        self.assertEqual(response.data["work_type"], self.work_type.id)
        self.assertEqual(response.data["work_type_name"], "기본")
        self.assertEqual(response.data["hourly_wage"], 10_000)

    def test_start_work_rejects_cross_tenant_work_type(self):
        other_tenant = _make_tenant(name="다른학원")
        other_work_type = WorkType.objects.create(
            tenant=other_tenant,
            name="다른근무",
            base_hourly_wage=99999,
            is_active=True,
        )

        response = self._start_work(self.staff, self.staff.user, other_work_type.id)

        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(WorkRecord.objects.filter(staff=self.staff).exists())

    def test_non_manager_teacher_cannot_start_other_staff_work(self):
        other_staff = _create_staff_teacher(self.tenant, name="다른강사", phone="01034343434")

        response = self._start_work(other_staff, self.staff.user, self.work_type.id)

        self.assertEqual(response.status_code, 403, response.data)
        self.assertFalse(WorkRecord.objects.filter(staff=other_staff).exists())


class TestWorkMonthLockFilters(TestCase):
    """월마감 조회는 직원/연/월 필터로 정확히 좁혀진다."""

    def setUp(self):
        self.factory = APIRequestFactory()
        self.tenant = _make_tenant()
        self.manager = _create_staff_teacher(self.tenant, name="관리강사", phone="01067676767")
        TenantMembership.objects.filter(
            tenant=self.tenant,
            user=self.manager.user,
        ).update(role="admin")
        self.staff_a = _create_staff_teacher(self.tenant, name="강사A", phone="01078787878")
        self.staff_b = _create_staff_teacher(self.tenant, name="강사B", phone="01089898989")
        WorkMonthLock.objects.create(
            tenant=self.tenant,
            staff=self.staff_a,
            year=2026,
            month=7,
            is_locked=True,
            locked_by=self.manager.user,
        )
        WorkMonthLock.objects.create(
            tenant=self.tenant,
            staff=self.staff_a,
            year=2026,
            month=6,
            is_locked=True,
            locked_by=self.manager.user,
        )
        WorkMonthLock.objects.create(
            tenant=self.tenant,
            staff=self.staff_b,
            year=2026,
            month=7,
            is_locked=True,
            locked_by=self.manager.user,
        )

    def _list_locks(self, params):
        request = self.factory.get("/staffs/work-month-locks/", params)
        request.tenant = self.tenant
        force_authenticate(request, user=self.manager.user)
        view = WorkMonthLockViewSet.as_view({"get": "list"})
        return view(request)

    def _close_month(self, *, month, payload_overrides=None):
        payload = {"staff": self.staff_a.id, "year": 2026, "month": month}
        payload.update(payload_overrides or {})
        request = self.factory.post(
            "/staffs/work-month-locks/",
            payload,
            format="json",
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.manager.user)
        return WorkMonthLockViewSet.as_view({"post": "create"})(request)

    def test_filters_by_staff_year_and_month(self):
        response = self._list_locks(
            {"staff": self.staff_a.id, "year": 2026, "month": 7}
        )

        self.assertEqual(response.status_code, 200, response.data)
        rows = response.data.get("results", response.data)
        ids = [row["staff"] for row in rows]
        months = [row["month"] for row in rows]
        self.assertEqual(ids, [self.staff_a.id])
        self.assertEqual(months, [7])

    def test_repeated_month_close_is_idempotent(self):
        first = self._close_month(month=8)
        second = self._close_month(month=8)

        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(first.data["id"], second.data["id"])
        self.assertEqual(
            PayrollSnapshot.objects.filter(
                tenant=self.tenant,
                staff=self.staff_a,
                year=2026,
                month=8,
            ).count(),
            1,
        )

    def test_month_close_rejects_boolean_and_non_integer_identifiers(self):
        for payload in (
            {"staff": True},
            {"staff": "abc"},
            {"year": True},
            {"month": 8.5},
        ):
            with self.subTest(payload=payload):
                response = self._close_month(month=8, payload_overrides=payload)
                self.assertEqual(response.status_code, 400, response.data)

        self.assertFalse(
            WorkMonthLock.objects.filter(
                tenant=self.tenant,
                staff=self.staff_a,
                year=2026,
                month=8,
            ).exists()
        )

    def test_month_close_rejects_year_outside_supported_range(self):
        for year in (2019, 2101):
            with self.subTest(year=year):
                response = self._close_month(
                    month=8,
                    payload_overrides={"year": year},
                )
                self.assertEqual(response.status_code, 400, response.data)

    def test_month_lock_patch_and_delete_are_method_not_allowed(self):
        lock = WorkMonthLock.objects.get(
            tenant=self.tenant,
            staff=self.staff_a,
            year=2026,
            month=7,
        )
        view = WorkMonthLockViewSet.as_view({"get": "retrieve"})
        patch_request = self.factory.patch(
            f"/staffs/work-month-locks/{lock.id}/",
            {"is_locked": False},
            format="json",
        )
        patch_request.tenant = self.tenant
        force_authenticate(patch_request, user=self.manager.user)
        delete_request = self.factory.delete(
            f"/staffs/work-month-locks/{lock.id}/"
        )
        delete_request.tenant = self.tenant
        force_authenticate(delete_request, user=self.manager.user)

        patch_response = view(patch_request, pk=lock.id)
        delete_response = view(delete_request, pk=lock.id)

        self.assertEqual(patch_response.status_code, 405)
        self.assertEqual(delete_response.status_code, 405)
        lock.refresh_from_db()
        self.assertTrue(lock.is_locked)

    def test_close_rejects_open_work_record_without_creating_artifacts(self):
        work_type = WorkType.objects.create(
            tenant=self.tenant,
            name="월마감 근무",
            base_hourly_wage=10_000,
        )
        WorkRecord.objects.create(
            tenant=self.tenant,
            staff=self.staff_a,
            work_type=work_type,
            date="2026-08-10",
            start_time="09:00",
        )

        response = self._close_month(month=8)

        self.assertEqual(response.status_code, 400, response.data)
        self.assertTrue(response.data["open_work_record_ids"])
        self.assertFalse(
            WorkMonthLock.objects.filter(
                staff=self.staff_a,
                year=2026,
                month=8,
            ).exists()
        )
        self.assertFalse(
            PayrollSnapshot.objects.filter(
                staff=self.staff_a,
                year=2026,
                month=8,
            ).exists()
        )

    def test_close_rejects_pending_expense(self):
        ExpenseRecord.objects.create(
            tenant=self.tenant,
            staff=self.staff_a,
            date="2026-08-10",
            title="미처리 교통비",
            amount=10_000,
            status="PENDING",
        )

        response = self._close_month(month=8)

        self.assertEqual(response.status_code, 400, response.data)
        self.assertTrue(response.data["pending_expense_ids"])
        self.assertFalse(
            WorkMonthLock.objects.filter(
                staff=self.staff_a,
                year=2026,
                month=8,
            ).exists()
        )

    def test_close_rejects_incomplete_closed_work_record(self):
        work_type = WorkType.objects.create(
            tenant=self.tenant,
            name="불완전 근무",
            base_hourly_wage=10_000,
        )
        record = WorkRecord.objects.create(
            tenant=self.tenant,
            staff=self.staff_a,
            work_type=work_type,
            date="2026-08-10",
            start_time="09:00",
        )
        WorkRecord.objects.filter(pk=record.pk).update(end_time="18:00")

        response = self._close_month(month=8)

        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(
            [int(value) for value in response.data["incomplete_work_record_ids"]],
            [record.id],
        )
        self.assertFalse(
            PayrollSnapshot.objects.filter(
                staff=self.staff_a,
                year=2026,
                month=8,
            ).exists()
        )

    def test_open_record_in_other_month_does_not_block_close(self):
        work_type = WorkType.objects.create(
            tenant=self.tenant,
            name="다른 달 근무",
            base_hourly_wage=10_000,
        )
        WorkRecord.objects.create(
            tenant=self.tenant,
            staff=self.staff_a,
            work_type=work_type,
            date="2026-09-01",
            start_time="09:00",
        )

        response = self._close_month(month=8)

        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(
            PayrollSnapshot.objects.filter(
                staff=self.staff_a,
                year=2026,
                month=8,
            ).exists()
        )

    def test_legacy_unlocked_row_with_snapshot_requires_reconciliation(self):
        lock = WorkMonthLock.objects.create(
            tenant=self.tenant,
            staff=self.staff_a,
            year=2026,
            month=8,
            is_locked=False,
            locked_by=self.manager.user,
        )
        PayrollSnapshot.objects.create(
            tenant=self.tenant,
            staff=self.staff_a,
            year=2026,
            month=8,
            generated_by=self.manager.user,
        )

        response = self._close_month(month=8)

        self.assertEqual(response.status_code, 400, response.data)
        lock.refresh_from_db()
        self.assertFalse(lock.is_locked)
        self.assertTrue(
            OpsAuditLog.objects.filter(
                action="payroll.month_lock_reconciliation_required",
                target_tenant=self.tenant,
            ).exists()
        )


class TestStaffDeletePolicy(TestCase):
    """Staff 삭제 정책 테스트."""

    def setUp(self):
        self.tenant = _make_tenant()

    def test_delete_removes_teacher(self):
        """Staff 삭제 → 대응 Teacher도 삭제."""
        staff = _create_staff_teacher(self.tenant, name="삭제대상", phone="01000001111")
        request = _make_request(self.tenant, staff.user)
        serializer = StaffCreateUpdateSerializer(
            staff, context={"request": request},
        )
        serializer.delete(staff)

        self.assertFalse(Staff.objects.filter(id=staff.id).exists())
        self.assertFalse(
            teacher_repo.teacher_filter_tenant(self.tenant).filter(name="삭제대상").exists()
        )

    def test_owner_cannot_be_deleted(self):
        """Owner는 삭제 불가."""
        owner_user = User.objects.create_user(
            username=f"t{self.tenant.id}_owner",
            password="test1234",
            name="원장",
        )
        TenantMembership.objects.create(
            tenant=self.tenant, user=owner_user, role="owner", is_active=True,
        )
        staff = Staff.objects.create(
            tenant=self.tenant, user=owner_user, name="원장", phone="",
        )

        from rest_framework.exceptions import ValidationError
        serializer = StaffCreateUpdateSerializer(
            staff, context={"request": _make_request(self.tenant, owner_user)},
        )
        with self.assertRaises(ValidationError):
            serializer.delete(staff)


class TestStaffPasswordChange(TestCase):
    """비밀번호 변경 엔드포인트 (실제 view 호출)."""

    def setUp(self):
        self.tenant = _make_tenant()
        self.staff = _create_staff_teacher(self.tenant, name="비번테스트", phone="01022223333")
        self.actor = User.objects.create_user(
            username=f"t{self.tenant.id}_password_owner",
            password="test1234",
            name="대표",
        )
        TenantMembership.objects.create(
            tenant=self.tenant,
            user=self.actor,
            role="owner",
            is_active=True,
        )

    def _call_change_password(self, staff_id, password_data):
        from rest_framework.test import APIRequestFactory
        from apps.domains.staffs.views import StaffViewSet
        factory = APIRequestFactory()
        request = factory.post(f"/staffs/{staff_id}/change-password/", password_data, format="json")
        request.tenant = self.tenant
        request.user = self.actor
        view = StaffViewSet.as_view({"post": "change_password"})
        return view(request, pk=staff_id)

    def test_password_change_works(self):
        """비밀번호 변경 후 새 비밀번호로 인증 가능."""
        self.staff.user.must_change_password = True
        self.staff.user.save(update_fields=["must_change_password"])
        before_token_version = self.staff.user.token_version

        resp = self._call_change_password(self.staff.id, {"password": "new_password_123"})
        self.assertEqual(resp.status_code, 200)
        self.staff.user.refresh_from_db()
        self.assertTrue(self.staff.user.check_password("new_password_123"))
        self.assertFalse(self.staff.user.must_change_password)
        self.assertEqual(self.staff.user.token_version, before_token_version + 1)

    def test_password_too_short(self):
        """4자 미만 비밀번호는 거부."""
        resp = self._call_change_password(self.staff.id, {"password": "ab"})
        self.assertEqual(resp.status_code, 400)

    def test_password_empty(self):
        """빈 비밀번호는 거부."""
        resp = self._call_change_password(self.staff.id, {"password": ""})
        self.assertEqual(resp.status_code, 400)

    def test_password_no_user(self):
        """계정 없는 Staff는 비밀번호 변경 불가."""
        staff_no_user = Staff.objects.create(
            tenant=self.tenant, name="계정없음", phone="01099990000",
        )
        resp = self._call_change_password(staff_no_user.id, {"password": "test1234"})
        self.assertEqual(resp.status_code, 400)
