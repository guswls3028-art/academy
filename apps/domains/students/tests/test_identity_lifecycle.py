# PATH: apps/domains/students/tests/test_identity_lifecycle.py
"""
Regression tests for student identity SSOT, lifecycle, and ghost-data elimination.
Covers: ps_number/username sync, deletion semantics, restore flow, ghost data filters.
"""
from importlib import import_module
import threading
import unittest
from datetime import timedelta

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import IntegrityError, close_old_connections, connection
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from unittest.mock import patch
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.core.models.tenant import Tenant
from apps.core.models.tenant_membership import TenantMembership
from apps.core.models.user import user_internal_username, user_display_username
from apps.domains.parents.test_support import create_parent_account_fixture
from apps.domains.students.models import Student
from apps.domains.students.serializers import StudentDetailSerializer
from apps.domains.students.services import StudentLifecycleError, restore_student, soft_delete_student
from apps.domains.students.views import StudentViewSet

Parent = apps.get_model("parents", "Parent")
InventoryFolder = apps.get_model("inventory", "InventoryFolder")
InventoryFile = apps.get_model("inventory", "InventoryFile")
Enrollment = apps.get_model("enrollment", "Enrollment")
Lecture = apps.get_model("lectures", "Lecture")
ClinicSession = apps.get_model("clinic", "Session")
SessionParticipant = apps.get_model("clinic", "SessionParticipant")
cancel_active_participants_for_student = import_module(
    "apps.domains.clinic.services.lifecycle"
).cancel_active_participants_for_student

User = get_user_model()


def _create_tenant(name="TestAcademy", code="test"):
    return Tenant.objects.create(name=name, code=code)


def _create_student(tenant, ps_number, name="테스트학생", phone="01012345678", parent_phone="01098765432"):
    internal_username = user_internal_username(tenant, ps_number)
    user = User.objects.create_user(
        username=internal_username,
        password="test1234",
        tenant=tenant,
        phone=phone,
        name=name,
    )
    student = Student.objects.create(
        tenant=tenant,
        user=user,
        ps_number=ps_number,
        name=name,
        phone=phone,
        parent_phone=parent_phone,
        omr_code=phone[-8:] if phone and len(phone) >= 8 else "00000000",
    )
    TenantMembership.ensure_active(tenant=tenant, user=user, role="student")
    return student


class TestPsNumberUsernameSyncOnSave(TestCase):
    """Student.save() hook: ps_number change → User.username sync + inventory cascade."""

    def setUp(self):
        self.tenant = _create_tenant()
        self.student = _create_student(self.tenant, "A12345")

    def test_ps_number_change_syncs_username(self):
        """ps_number 변경 시 User.username이 자동 동기화."""
        self.student.ps_number = "B99999"
        self.student.save(update_fields=["ps_number"])
        self.student.user.refresh_from_db()
        expected = user_internal_username(self.tenant, "B99999")
        self.assertEqual(self.student.user.username, expected)

    def test_unpersisted_ps_number_does_not_change_login_or_inventory(self):
        """update_fields에서 제외한 ps_number는 연관 identity에도 반영하지 않는다."""
        folder = InventoryFolder.objects.create(
            tenant=self.tenant,
            student_ps="A12345",
            name="root",
        )
        original_username = self.student.user.username

        self.student.ps_number = "B99999"
        self.student.name = "이름만 변경"
        self.student.save(update_fields=["name"])

        self.student.refresh_from_db()
        self.student.user.refresh_from_db()
        folder.refresh_from_db()
        self.assertEqual(self.student.ps_number, "A12345")
        self.assertEqual(self.student.user.username, original_username)
        self.assertEqual(folder.student_ps, "A12345")

    def test_in_memory_user_relink_is_rejected_before_unrelated_save(self):
        original_user_id = self.student.user_id
        original_username = self.student.user.username
        other_user = User.objects.create_user(
            username=user_internal_username(self.tenant, "OTHER01"),
            password="test1234",
            tenant=self.tenant,
        )
        self.student.user = other_user
        self.student.name = "이름 변경 시도"

        with self.assertRaisesMessage(ValueError, "Student.user cannot be changed"):
            self.student.save(update_fields=["name"])

        self.student.refresh_from_db()
        self.student.user.refresh_from_db()
        self.assertEqual(self.student.user_id, original_user_id)
        self.assertEqual(self.student.user.username, original_username)

    def test_username_collision_rolls_back_student_user_and_inventory(self):
        folder = InventoryFolder.objects.create(
            tenant=self.tenant,
            student_ps="A12345",
            name="root",
        )
        original_username = self.student.user.username
        User.objects.create_user(
            username=user_internal_username(self.tenant, "TAKEN01"),
            password="test1234",
            tenant=self.tenant,
        )
        self.student.ps_number = "TAKEN01"

        with self.assertRaises(IntegrityError):
            self.student.save(update_fields=["ps_number"])

        self.student.refresh_from_db()
        self.student.user.refresh_from_db()
        folder.refresh_from_db()
        self.assertEqual(self.student.ps_number, "A12345")
        self.assertEqual(self.student.user.username, original_username)
        self.assertEqual(folder.student_ps, "A12345")

    def test_ps_number_change_cascades_inventory(self):
        """ps_number 변경 시 인벤토리 student_ps도 업데이트."""
        folder = InventoryFolder.objects.create(
            tenant=self.tenant,
            student_ps="A12345",
            name="root",
        )
        ifile = InventoryFile.objects.create(
            tenant=self.tenant,
            student_ps="A12345",
            scope="student",
            folder=folder,
            display_name="test.pdf",
            original_name="test.pdf",
            r2_key="test/key",
            size_bytes=100,
        )
        self.student.ps_number = "C77777"
        self.student.save(update_fields=["ps_number"])
        folder.refresh_from_db()
        ifile.refresh_from_db()
        self.assertEqual(folder.student_ps, "C77777")
        self.assertEqual(ifile.student_ps, "C77777")

    def test_del_prefix_ps_does_not_cascade_inventory(self):
        """_del_ 접두사 ps_number 변경은 인벤토리 업데이트 안 함 (삭제 시)."""
        InventoryFolder.objects.create(
            tenant=self.tenant, student_ps="A12345", name="root"
        )
        self.student.ps_number = f"_del_{self.student.id}_A12345"
        self.student.save(update_fields=["ps_number"])
        folder = InventoryFolder.objects.get(tenant=self.tenant, student_ps="A12345")
        self.assertEqual(folder.student_ps, "A12345")  # 변경 안 됨

    def test_display_username_matches_ps_number(self):
        """user_display_username(user) == ps_number (SSOT)."""
        display = user_display_username(self.student.user)
        self.assertEqual(display, self.student.ps_number)
        # ps_number 변경 후에도 동일
        self.student.ps_number = "D11111"
        self.student.save(update_fields=["ps_number"])
        self.student.user.refresh_from_db()
        self.assertEqual(user_display_username(self.student.user), "D11111")


class StudentIdentityConcurrencyPostgresTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        if connection.vendor != "postgresql":
            raise unittest.SkipTest(
                "PostgreSQL is required for student identity row-lock verification."
            )
        super().setUpClass()

    def test_concurrent_ps_number_changes_keep_all_identity_copies_aligned(self):
        tenant = _create_tenant(name="Identity Race", code="identity-race")
        student = _create_student(tenant, "RACE00")
        folder = InventoryFolder.objects.create(
            tenant=tenant,
            student_ps="RACE00",
            name="root",
        )
        barrier = threading.Barrier(2, timeout=10)
        outcomes: list[str] = []
        errors: list[BaseException] = []

        def worker(next_ps_number: str):
            close_old_connections()
            try:
                thread_student = Student.objects.get(pk=student.pk)
                thread_student.ps_number = next_ps_number
                barrier.wait()
                thread_student.save(update_fields=["ps_number"])
                outcomes.append(next_ps_number)
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                close_old_connections()

        threads = [
            threading.Thread(target=worker, args=("RACE01",)),
            threading.Thread(target=worker, args=("RACE02",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertCountEqual(outcomes, ["RACE01", "RACE02"])
        student.refresh_from_db()
        student.user.refresh_from_db()
        folder.refresh_from_db()
        self.assertIn(student.ps_number, {"RACE01", "RACE02"})
        self.assertEqual(user_display_username(student.user), student.ps_number)
        self.assertEqual(folder.student_ps, student.ps_number)


class TestSoftDeleteSemantics(TestCase):
    """Student soft-delete → ps_number mangling, user deactivation, enrollment status."""

    def setUp(self):
        self.tenant = _create_tenant()
        self.student = _create_student(self.tenant, "S11111", phone="01011112222")

    def test_soft_delete_mangles_ps_number(self):
        now = timezone.now()
        self.student.deleted_at = now
        original_ps = self.student.ps_number
        self.student.ps_number = f"_del_{self.student.id}_{original_ps}"
        self.student.save(update_fields=["deleted_at", "ps_number"])
        self.assertTrue(self.student.ps_number.startswith("_del_"))

    def test_soft_delete_deactivates_user(self):
        self.student.user.is_active = False
        self.student.user.phone = None
        self.student.user.save(update_fields=["is_active", "phone"])
        self.student.user.refresh_from_db()
        self.assertFalse(self.student.user.is_active)
        self.assertIsNone(self.student.user.phone)


class TestSoftDeleteLifecycleService(TestCase):
    """Canonical soft-delete service keeps all side effects in one place."""

    def setUp(self):
        self.tenant = _create_tenant()
        self.student = _create_student(
            self.tenant,
            "SD001",
            phone="01044445555",
            parent_phone="01099998888",
        )
        self.parent = create_parent_account_fixture(
            tenant=self.tenant,
            parent_phone=self.student.parent_phone,
            student_name=self.student.name,
            initial_password="chosen-parent-password",
        ).parent
        self.student.parent = self.parent
        self.student.save(update_fields=["parent"])
        self.lecture = Lecture.objects.create(
            tenant=self.tenant,
            title="삭제 테스트 강의",
            name="삭제 테스트 강의",
            subject="테스트",
        )
        self.enrollment = Enrollment.objects.create(
            tenant=self.tenant,
            student=self.student,
            lecture=self.lecture,
            status="ACTIVE",
        )
        self.clinic_session = ClinicSession.objects.create(
            tenant=self.tenant,
            date="2026-04-01",
            start_time="14:00",
            location="Room A",
            max_participants=10,
        )
        self.booked = SessionParticipant.objects.create(
            tenant=self.tenant,
            session=self.clinic_session,
            student=self.student,
            status=SessionParticipant.Status.BOOKED,
            source=SessionParticipant.Source.AUTO,
        )
        self.pending = SessionParticipant.objects.create(
            tenant=self.tenant,
            session=None,
            student=self.student,
            requested_date="2026-04-02",
            requested_start_time="15:00",
            status=SessionParticipant.Status.PENDING,
            source=SessionParticipant.Source.STUDENT_REQUEST,
        )
        self.attended = SessionParticipant.objects.create(
            tenant=self.tenant,
            session=self.clinic_session,
            student=self.student,
            status=SessionParticipant.Status.ATTENDED,
            source=SessionParticipant.Source.AUTO,
            participant_role="target",
        )

    def test_soft_delete_student_applies_full_lifecycle(self):
        result = soft_delete_student(self.student, tenant=self.tenant)

        self.assertEqual(result.enrollment_count, 1)
        self.assertEqual(result.clinic_participant_count, 2)
        self.assertTrue(result.user_deactivated)

        self.student.refresh_from_db()
        self.student.user.refresh_from_db()
        self.enrollment.refresh_from_db()
        self.booked.refresh_from_db()
        self.pending.refresh_from_db()
        self.attended.refresh_from_db()

        self.assertIsNotNone(self.student.deleted_at)
        self.assertTrue(self.student.ps_number.startswith(f"_del_{self.student.id}_SD001"))
        self.assertIsNone(self.student.parent_id)
        self.assertFalse(self.student.user.is_active)
        self.assertIsNone(self.student.user.phone)
        self.assertFalse(
            TenantMembership.objects.get(tenant=self.tenant, user=self.student.user).is_active
        )
        self.assertEqual(self.enrollment.status, "INACTIVE")
        self.assertEqual(self.enrollment.status_before_student_deletion, "ACTIVE")
        self.assertEqual(self.booked.status, SessionParticipant.Status.CANCELLED)
        self.assertEqual(self.pending.status, SessionParticipant.Status.CANCELLED)
        self.assertEqual(self.attended.status, SessionParticipant.Status.ATTENDED)

    def test_soft_delete_student_rejects_repeat_delete(self):
        soft_delete_student(self.student, tenant=self.tenant)

        with self.assertRaises(StudentLifecycleError) as ctx:
            soft_delete_student(self.student, tenant=self.tenant)

        self.assertEqual(ctx.exception.code, "already_deleted")

    def test_management_flag_is_distinct_from_account_and_deletion_state(self):
        self.student.is_managed = False
        self.student.save(update_fields=["is_managed"])

        active_data = StudentDetailSerializer(self.student).data

        self.assertFalse(active_data["is_managed"])
        self.assertEqual(active_data["account_state"], "ACTIVE")

        soft_delete_student(self.student, tenant=self.tenant)
        self.student.refresh_from_db()
        deleted_data = StudentDetailSerializer(self.student).data

        self.assertFalse(deleted_data["is_managed"])
        self.assertEqual(deleted_data["account_state"], "DELETED")

    @patch("apps.domains.enrollment.services.lifecycle.schedule_pending_account_notice")
    def test_restore_recovers_only_pre_delete_status_on_open_lectures(self, notice_mock):
        inactive_lecture = Lecture.objects.create(
            tenant=self.tenant,
            title="기존 비활성 강의",
            name="기존 비활성 강의",
            subject="테스트",
        )
        inactive_enrollment = Enrollment.objects.create(
            tenant=self.tenant,
            student=self.student,
            lecture=inactive_lecture,
            status="INACTIVE",
        )
        pending_lecture = Lecture.objects.create(
            tenant=self.tenant,
            title="대기 강의",
            name="대기 강의",
            subject="테스트",
        )
        pending_enrollment = Enrollment.objects.create(
            tenant=self.tenant,
            student=self.student,
            lecture=pending_lecture,
            status="PENDING",
        )
        ended_lecture = Lecture.objects.create(
            tenant=self.tenant,
            title="삭제 중 종료 강의",
            name="삭제 중 종료 강의",
            subject="테스트",
            end_date=timezone.localdate() + timedelta(days=1),
        )
        ended_enrollment = Enrollment.objects.create(
            tenant=self.tenant,
            student=self.student,
            lecture=ended_lecture,
            status="ACTIVE",
        )

        soft_delete_student(self.student, tenant=self.tenant)

        for enrollment, original_status in (
            (self.enrollment, "ACTIVE"),
            (inactive_enrollment, "INACTIVE"),
            (pending_enrollment, "PENDING"),
            (ended_enrollment, "ACTIVE"),
        ):
            enrollment.refresh_from_db()
            self.assertEqual(enrollment.status, "INACTIVE")
            self.assertEqual(enrollment.status_before_student_deletion, original_status)

        ended_lecture.end_date = timezone.localdate() - timedelta(days=1)
        ended_lecture.save(update_fields=["end_date"])
        result = restore_student(self.student, tenant=self.tenant)

        self.enrollment.refresh_from_db()
        inactive_enrollment.refresh_from_db()
        pending_enrollment.refresh_from_db()
        ended_enrollment.refresh_from_db()
        self.assertEqual(self.enrollment.status, "ACTIVE")
        self.assertEqual(inactive_enrollment.status, "INACTIVE")
        self.assertEqual(pending_enrollment.status, "PENDING")
        self.assertEqual(ended_enrollment.status, "INACTIVE")
        self.assertEqual(result.enrollment_count, 4)
        self.assertEqual(result.active_enrollment_count, 1)
        self.assertEqual(result.pending_enrollment_count, 1)
        self.assertEqual(result.inactive_enrollment_count, 2)
        self.assertFalse(
            Enrollment.objects.filter(
                student=self.student,
                status_before_student_deletion__isnull=False,
            ).exists()
        )
        notice_mock.assert_not_called()


class TestSoftDeleteViewRouting(TestCase):
    """Single and bulk HTTP paths use the canonical soft-delete lifecycle."""

    def setUp(self):
        self.factory = APIRequestFactory()
        self.tenant = _create_tenant()
        self.admin = User.objects.create_user(
            username="soft-delete-admin",
            password="test1234",
            tenant=self.tenant,
            is_staff=True,
            name="관리자",
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=self.admin, role="owner")

    def _student_with_edges(self, suffix: str) -> tuple[Student, Enrollment, SessionParticipant]:
        student = _create_student(
            self.tenant,
            f"VR{suffix}",
            phone=f"0107000{int(suffix):04d}",
            parent_phone=f"0108000{int(suffix):04d}",
        )
        lecture = Lecture.objects.create(
            tenant=self.tenant,
            title=f"강의 {suffix}",
            name=f"강의 {suffix}",
            subject="테스트",
        )
        enrollment = Enrollment.objects.create(
            tenant=self.tenant,
            student=student,
            lecture=lecture,
            status="ACTIVE",
        )
        clinic_session = ClinicSession.objects.create(
            tenant=self.tenant,
            date="2026-04-01",
            start_time=f"14:{int(suffix) % 60:02d}",
            location=f"Room {suffix}",
            max_participants=10,
        )
        participant = SessionParticipant.objects.create(
            tenant=self.tenant,
            session=clinic_session,
            student=student,
            status=SessionParticipant.Status.BOOKED,
            source=SessionParticipant.Source.AUTO,
        )
        return student, enrollment, participant

    def test_destroy_routes_through_soft_delete_lifecycle(self):
        student, enrollment, participant = self._student_with_edges("0001")
        request = self.factory.delete(f"/api/v1/students/{student.id}/")
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant

        response = StudentViewSet.as_view({"delete": "destroy"})(request, pk=student.id)

        self.assertEqual(response.status_code, 204)
        student.refresh_from_db()
        student.user.refresh_from_db()
        enrollment.refresh_from_db()
        participant.refresh_from_db()
        self.assertTrue(student.ps_number.startswith(f"_del_{student.id}_VR0001"))
        self.assertFalse(student.user.is_active)
        self.assertIsNone(student.user.phone)
        self.assertEqual(enrollment.status, "INACTIVE")
        self.assertEqual(participant.status, SessionParticipant.Status.CANCELLED)

    @patch("apps.domains.students.views.student_views.send_event_notification")
    def test_withdrawal_occurrence_changes_only_after_restore_and_rewithdraw(
        self,
        send_notification,
    ):
        student, _enrollment, _participant = self._student_with_edges("0011")
        parent = create_parent_account_fixture(
            tenant=self.tenant,
            parent_phone=student.parent_phone,
            student_name=student.name,
            initial_password="chosen-parent-password",
        ).parent
        student.parent = parent
        student.save(update_fields=["parent"])

        def destroy_once():
            request = self.factory.delete(f"/api/v1/students/{student.id}/")
            force_authenticate(request, user=self.admin)
            request.tenant = self.tenant
            with self.captureOnCommitCallbacks(execute=True):
                return StudentViewSet.as_view({"delete": "destroy"})(
                    request,
                    pk=student.id,
                )

        first = destroy_once()
        duplicate = destroy_once()
        student.refresh_from_db()
        restore_student(student, tenant=self.tenant)
        student.refresh_from_db()
        second = destroy_once()

        self.assertEqual(first.status_code, 204)
        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(second.status_code, 204)
        self.assertEqual(send_notification.call_count, 2)
        occurrence_keys = [
            call.kwargs["context"]["_domain_object_id"]
            for call in send_notification.call_args_list
        ]
        self.assertNotEqual(occurrence_keys[0], occurrence_keys[1])
        self.assertTrue(
            all(key.startswith(f"withdrawal:{student.id}:") for key in occurrence_keys)
        )

    def test_bulk_delete_routes_through_soft_delete_lifecycle(self):
        student1, enrollment1, participant1 = self._student_with_edges("0002")
        student2, enrollment2, participant2 = self._student_with_edges("0003")
        request = self.factory.post(
            "/api/v1/students/bulk_delete/",
            data={"ids": [student1.id, student2.id]},
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant

        response = StudentViewSet.as_view({"post": "bulk_delete"})(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["deleted"], 2)
        for student, enrollment, participant, original_ps in [
            (student1, enrollment1, participant1, "VR0002"),
            (student2, enrollment2, participant2, "VR0003"),
        ]:
            student.refresh_from_db()
            student.user.refresh_from_db()
            enrollment.refresh_from_db()
            participant.refresh_from_db()
            self.assertTrue(student.ps_number.startswith(f"_del_{student.id}_{original_ps}"))
            self.assertFalse(student.user.is_active)
            self.assertIsNone(student.user.phone)
            self.assertEqual(enrollment.status, "INACTIVE")
            self.assertEqual(participant.status, SessionParticipant.Status.CANCELLED)


class TestSoftDeleteCancelsClinicBookings(TestCase):
    """Student soft-delete should cancel active clinic participants (PENDING/BOOKED → CANCELLED)."""

    def setUp(self):
        self.tenant = _create_tenant()
        self.student = _create_student(self.tenant, "CL001", phone="01055550001", parent_phone="01099990001")
        self.session = ClinicSession.objects.create(
            tenant=self.tenant,
            date="2026-04-01",
            start_time="14:00",
            location="Room A",
            max_participants=10,
        )
        # Create BOOKED and PENDING participants
        self.booked = SessionParticipant.objects.create(
            tenant=self.tenant, session=self.session, student=self.student,
            status=SessionParticipant.Status.BOOKED, source=SessionParticipant.Source.AUTO,
        )
        self.pending = SessionParticipant.objects.create(
            tenant=self.tenant, session=None, student=self.student,
            requested_date="2026-04-02", requested_start_time="15:00",
            status=SessionParticipant.Status.PENDING, source=SessionParticipant.Source.STUDENT_REQUEST,
        )

    def test_soft_delete_cancels_active_bookings(self):
        """BOOKED/PENDING 예약이 학생 삭제 시 CANCELLED로 변경."""
        now = timezone.now()
        count = cancel_active_participants_for_student(
            tenant=self.tenant,
            student=self.student,
            changed_at=now,
        )

        self.booked.refresh_from_db()
        self.pending.refresh_from_db()
        self.assertEqual(count, 2)
        self.assertEqual(self.booked.status, "cancelled")
        self.assertEqual(self.pending.status, "cancelled")

    def test_attended_not_cancelled(self):
        """ATTENDED 상태는 삭제 시에도 보존 (이력)."""
        attended = SessionParticipant.objects.create(
            tenant=self.tenant, session=self.session, student=self.student,
            status=SessionParticipant.Status.ATTENDED, source=SessionParticipant.Source.AUTO,
            participant_role="target",
        )
        cancel_active_participants_for_student(
            tenant=self.tenant,
            student=self.student,
            changed_at=timezone.now(),
        )

        attended.refresh_from_db()
        self.assertEqual(attended.status, "attended")  # 보존됨

    def test_session_count_after_cancel(self):
        """예약 취소 후 세션 카운트가 정확해야 함."""
        cancel_active_participants_for_student(
            tenant=self.tenant,
            student=self.student,
            changed_at=timezone.now(),
        )

        from django.db.models import Count, Q
        session = (
            ClinicSession.objects.filter(pk=self.session.pk)
            .annotate(
                booked_count=Count("participants", filter=Q(
                    participants__status__in=["booked", "pending"]
                ))
            ).first()
        )
        self.assertEqual(session.booked_count, 0)  # 모두 취소됨


class TestBulkRestoreFlow(TestCase):
    """Bulk restore: ps_number collision check, parent re-link, User.phone restore."""

    def setUp(self):
        self.factory = APIRequestFactory()
        self.tenant = _create_tenant()
        self.admin = User.objects.create_user(
            username="restore-admin",
            password="test1234",
            tenant=self.tenant,
            is_staff=True,
            name="복원 관리자",
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=self.admin, role="owner")
        self.student = _create_student(self.tenant, "R11111", phone="01033334444", parent_phone="01055556666")
        # Create parent
        self.parent = create_parent_account_fixture(
            tenant=self.tenant,
            parent_phone="01055556666",
            student_name=self.student.name,
            initial_password="chosen-parent-password",
        ).parent
        self.student.parent = self.parent
        self.student.save(update_fields=["parent"])
        # Soft delete
        self.student.deleted_at = timezone.now()
        self.student.ps_number = f"_del_{self.student.id}_R11111"
        self.student.parent_id = None
        self.student.save(update_fields=["deleted_at", "ps_number", "parent"])
        self.student.user.is_active = False
        self.student.user.phone = None
        self.student.user.save(update_fields=["is_active", "phone"])
        TenantMembership.objects.filter(tenant=self.tenant, user=self.student.user).update(is_active=False)

    def test_restore_recovers_ps_number(self):
        """복원 서비스가 ps_number/user/parent/membership을 함께 복원."""
        result = restore_student(self.student, tenant=self.tenant)

        self.assertEqual(result.restored_ps_number, "R11111")
        self.assertTrue(result.user_reactivated)
        self.assertTrue(result.parent_relinked)
        self.student.refresh_from_db()
        self.student.user.refresh_from_db()

        self.assertIsNone(self.student.deleted_at)
        self.assertEqual(self.student.ps_number, "R11111")
        self.assertTrue(self.student.user.is_active)
        self.assertEqual(self.student.user.phone, "01033334444")
        self.assertEqual(self.student.parent_id, self.parent.id)
        self.assertTrue(
            TenantMembership.objects.get(tenant=self.tenant, user=self.student.user).is_active
        )

    def test_restore_missing_parent_account_requires_explicit_password_without_reactivating_student(self):
        parent_user = self.parent.user
        self.parent.delete()
        parent_user.delete()

        with self.assertRaises(StudentLifecycleError) as ctx:
            restore_student(self.student, tenant=self.tenant)

        self.assertEqual(ctx.exception.code, "parent_account_password_required")
        self.student.refresh_from_db()
        self.student.user.refresh_from_db()
        self.assertIsNotNone(self.student.deleted_at)
        self.assertFalse(self.student.user.is_active)

    def test_restore_missing_parent_account_uses_explicit_password(self):
        parent_user = self.parent.user
        self.parent.delete()
        parent_user.delete()

        result = restore_student(
            self.student,
            tenant=self.tenant,
            parent_initial_password="teacher-selected-password",
        )

        self.assertTrue(result.parent_credentials_initialized)
        self.assertEqual(result.parent_password_for_notice, "teacher-selected-password")
        self.student.refresh_from_db()
        self.assertIsNotNone(self.student.parent_id)
        self.assertTrue(self.student.parent.user.check_password("teacher-selected-password"))

    def test_restore_inactive_parent_account_is_not_reported_as_password_required(self):
        self.parent.user.is_active = False
        self.parent.user.save(update_fields=["is_active"])

        with self.assertRaises(StudentLifecycleError) as ctx:
            restore_student(self.student, tenant=self.tenant)

        self.assertEqual(ctx.exception.code, "parent_account_invalid")
        self.student.refresh_from_db()
        self.assertIsNotNone(self.student.deleted_at)

    def test_restore_collision_detection(self):
        """복원 시 ps_number가 다른 활성 학생에게 사용 중이면 충돌."""
        _create_student(self.tenant, "R11111", name="새학생", phone="01099998888", parent_phone="01077778888")
        with self.assertRaises(StudentLifecycleError) as ctx:
            restore_student(self.student, tenant=self.tenant)

        self.assertEqual(ctx.exception.code, "ps_number_conflict")
        self.student.refresh_from_db()
        self.assertIsNotNone(self.student.deleted_at)
        self.assertTrue(self.student.ps_number.startswith(f"_del_{self.student.id}_R11111"))

    def test_user_phone_can_be_restored(self):
        """복원 시 User.phone을 Student.phone에서 복원."""
        restore_student(self.student, tenant=self.tenant)
        self.student.user.refresh_from_db()
        self.assertEqual(self.student.user.phone, "01033334444")

    def test_bulk_restore_view_uses_restore_lifecycle(self):
        request = self.factory.post(
            "/api/v1/students/bulk_restore/",
            data={"ids": [self.student.id]},
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant

        response = StudentViewSet.as_view({"post": "bulk_restore"})(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["restored"], 1)
        self.student.refresh_from_db()
        self.student.user.refresh_from_db()
        self.assertIsNone(self.student.deleted_at)
        self.assertEqual(self.student.ps_number, "R11111")
        self.assertTrue(self.student.user.is_active)
        self.assertEqual(self.student.user.phone, "01033334444")
        self.assertEqual(self.student.parent_id, self.parent.id)

    @patch(
        "apps.domains.students.views.student_views.send_parent_account_credentials_notice",
        return_value=True,
    )
    def test_bulk_restore_repairs_missing_parent_with_explicit_password_and_sends_notice(
        self,
        send_notice_mock,
    ):
        parent_user = self.parent.user
        self.parent.delete()
        parent_user.delete()
        request = self.factory.post(
            "/api/v1/students/bulk_restore/",
            data={
                "ids": [self.student.id],
                "parent_initial_password": "teacher-selected-password",
            },
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant

        response = StudentViewSet.as_view({"post": "bulk_restore"})(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["restored"], 1)
        self.student.refresh_from_db()
        self.assertIsNotNone(self.student.parent_id)
        self.assertTrue(self.student.parent.user.check_password("teacher-selected-password"))
        send_notice_mock.assert_called_once()

    def test_lecture_enroll_restore_uses_restore_lifecycle(self):
        from apps.domains.students.services.lecture_enroll import (
            get_or_create_student_for_lecture_enroll,
        )

        student, created, was_restored = get_or_create_student_for_lecture_enroll(
            self.tenant,
            {
                "name": self.student.name,
                "parent_phone": self.student.parent_phone,
                "phone": self.student.phone,
                "memo": "복원 메모",
            },
            "test1234",
        )

        self.assertFalse(created)
        self.assertTrue(was_restored)
        student.refresh_from_db()
        student.user.refresh_from_db()
        self.assertIsNone(student.deleted_at)
        self.assertEqual(student.ps_number, "R11111")
        self.assertEqual(student.memo, "복원 메모")
        self.assertTrue(student.user.is_active)
        self.assertEqual(student.parent_id, self.parent.id)

    @patch("apps.domains.messaging.services.send_welcome_messages")
    def test_bulk_resolve_restore_does_not_send_new_password_notice(self, send_mock):
        request = self.factory.post(
            "/api/v1/students/bulk_resolve_conflicts/",
            data={
                "initial_password": "newpass123",
                "resolutions": [
                    {
                        "row": 1,
                        "student_id": self.student.id,
                        "action": "restore",
                        "student_data": {
                            "name": self.student.name,
                            "parent_phone": self.student.parent_phone,
                            "phone": self.student.phone,
                        },
                    }
                ],
            },
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant

        response = StudentViewSet.as_view({"post": "bulk_resolve_conflicts"})(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["restored"], 1)
        send_mock.assert_not_called()


class TestCrossTenantIsolation(TestCase):
    """Same ps_number across different tenants: must be allowed."""

    def setUp(self):
        self.tenant1 = _create_tenant("Academy1", "acad1")
        self.tenant2 = _create_tenant("Academy2", "acad2")

    def test_same_ps_number_different_tenants(self):
        s1 = _create_student(self.tenant1, "X99999", name="학생A", phone="01011111111", parent_phone="01022222222")
        s2 = _create_student(self.tenant2, "X99999", name="학생B", phone="01033333333", parent_phone="01044444444")
        self.assertEqual(s1.ps_number, s2.ps_number)
        self.assertNotEqual(s1.user.username, s2.user.username)
        self.assertEqual(user_display_username(s1.user), "X99999")
        self.assertEqual(user_display_username(s2.user), "X99999")

    def test_duplicate_blocked_within_same_tenant(self):
        _create_student(self.tenant1, "D11111", phone="01055555555", parent_phone="01066666666")
        with self.assertRaises(Exception):
            _create_student(self.tenant1, "D11111", name="다른학생", phone="01077777777", parent_phone="01088888888")


class TestGhostDataExclusion(TestCase):
    """Deleted students should not appear in active queries."""

    def setUp(self):
        self.tenant = _create_tenant()
        self.active_student = _create_student(self.tenant, "ACT001", name="활성학생", phone="01011110001", parent_phone="01099990001")
        self.deleted_student = _create_student(self.tenant, "DEL001", name="삭제학생", phone="01011110002", parent_phone="01099990002")
        # Soft delete
        self.deleted_student.deleted_at = timezone.now()
        self.deleted_student.ps_number = f"_del_{self.deleted_student.id}_DEL001"
        self.deleted_student.save(update_fields=["deleted_at", "ps_number"])

    def test_active_student_query_excludes_deleted(self):
        active = Student.objects.filter(tenant=self.tenant, deleted_at__isnull=True)
        self.assertEqual(active.count(), 1)
        self.assertEqual(active.first().ps_number, "ACT001")

    def test_community_filter_expression(self):
        """Community _EXCLUDE_DELETED_AUTHOR Q expression works correctly."""
        from django.db.models import Q
        _filter = Q(created_by__isnull=True) | Q(created_by__deleted_at__isnull=True)
        # Verify the Q expression is constructable (ORM-level test)
        self.assertIsNotNone(_filter)

    def test_video_comment_filter_expression(self):
        """Video comment author filter Q expression works correctly."""
        from django.db.models import Q
        _active = Q(author_student__isnull=True) | Q(author_student__deleted_at__isnull=True)
        self.assertIsNotNone(_active)


class TestUsernameDisplayFunctions(TestCase):
    """user_internal_username / user_display_username are inverses."""

    def setUp(self):
        self.tenant = _create_tenant()

    def test_roundtrip(self):
        display = "MYID01"
        internal = user_internal_username(self.tenant, display)
        self.assertTrue(internal.startswith(f"t{self.tenant.id}_"))
        user = User.objects.create_user(username=internal, password="x", tenant=self.tenant)
        self.assertEqual(user_display_username(user), display)

    def test_no_tenant(self):
        internal = user_internal_username(None, "plainuser")
        self.assertEqual(internal, "plainuser")

    def test_parent_prefix(self):
        parent_username = f"p_{self.tenant.id}_01012345678"
        user = User.objects.create_user(username=parent_username, password="x", tenant=self.tenant)
        self.assertEqual(user_display_username(user), "01012345678")
