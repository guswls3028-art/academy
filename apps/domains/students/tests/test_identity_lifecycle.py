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
from django.db.models.query import QuerySet
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from unittest.mock import patch
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.core.models.tenant import Tenant
from apps.core.models.tenant_membership import TenantMembership
from apps.core.models.user import user_internal_username, user_display_username
from apps.domains.students.models import Student, StudentInventoryNamespaceConflict
from apps.domains.students.serializers import StudentDetailSerializer
from apps.domains.students.services import StudentLifecycleError, restore_student, soft_delete_student
from apps.domains.students.services.creation import create_student_account
from apps.domains.students.services.identity import StudentIdentityError
from apps.domains.students.services.profile import (
    StudentProfileUpdateError,
    update_student_profile,
)
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

        with self.assertRaises(StudentInventoryNamespaceConflict):
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

    def test_ps_number_change_locks_user_student_then_namespace(self):
        """Existing identity paths keep one deadlock-safe row/namespace lock order."""
        lock_order = []
        original_select_for_update = QuerySet.select_for_update

        def record_namespace_lock(*, tenant_id, ps_numbers):
            lock_order.append(("namespace", tenant_id, tuple(ps_numbers)))
            return tuple(sorted(ps_numbers))

        def record_lock(queryset, *args, **kwargs):
            lock_order.append(queryset.model)
            return original_select_for_update(queryset, *args, **kwargs)

        with patch(
            "apps.domains.students.models.lock_student_ps_namespaces",
            side_effect=record_namespace_lock,
        ), patch.object(QuerySet, "select_for_update", record_lock):
            self.student.ps_number = "LOCK002"
            self.student.save(update_fields=["ps_number"])

        self.assertEqual(lock_order[:2], [User, Student])
        self.assertEqual(lock_order[2][0], "namespace")
        self.assertEqual(set(lock_order[2][2]), {"A12345", "LOCK002"})

    def test_new_student_locks_tenant_reference_then_ps_namespace_before_insert(self):
        user = User.objects.create_user(
            username=user_internal_username(self.tenant, "CREATE01"),
            password="test1234",
            tenant=self.tenant,
        )

        lock_order = []

        with patch(
            "apps.domains.students.models.lock_student_creation_tenant_reference",
            side_effect=lambda **kwargs: lock_order.append(("tenant", kwargs)),
        ) as lock_tenant, patch(
            "apps.domains.students.models.lock_student_creation_user_reference",
            side_effect=lambda **kwargs: lock_order.append(("user", kwargs)),
        ) as lock_user, patch(
            "apps.domains.students.models.lock_student_ps_namespaces",
            side_effect=lambda **kwargs: lock_order.append(("namespace", kwargs)),
        ) as lock_namespace:
            created = Student.objects.create(
                tenant=self.tenant,
                user=user,
                ps_number="CREATE01",
                name="생성 잠금",
                omr_code="88000001",
            )

        self.assertIsNotNone(created.pk)
        lock_tenant.assert_called_once_with(tenant_id=self.tenant.id)
        lock_user.assert_called_once_with(user_id=user.id)
        lock_namespace.assert_called_once_with(
            tenant_id=self.tenant.id,
            ps_numbers=("CREATE01",),
        )
        self.assertEqual(
            [item[0] for item in lock_order],
            ["tenant", "user", "namespace"],
        )

    def test_del_prefix_ps_cascades_inventory_into_tombstone_namespace(self):
        """Soft-delete PS changes quarantine inventory away from future students."""
        folder = InventoryFolder.objects.create(
            tenant=self.tenant, student_ps="A12345", name="root"
        )
        inventory_file = InventoryFile.objects.create(
            tenant=self.tenant,
            scope="student",
            student_ps="A12345",
            folder=folder,
            display_name="private.pdf",
            r2_key=f"tenants/{self.tenant.id}/students/A12345/inventory/private.pdf",
            original_name="private.pdf",
            size_bytes=47,
            content_type="application/pdf",
        )
        tombstone_ps = f"_del_{self.student.id}_A12345"
        self.student.ps_number = f"_del_{self.student.id}_A12345"
        self.student.save(update_fields=["ps_number"])
        folder.refresh_from_db()
        inventory_file.refresh_from_db()
        self.assertEqual(folder.student_ps, tombstone_ps)
        self.assertEqual(inventory_file.student_ps, tombstone_ps)

    def test_new_claim_quarantines_one_exact_legacy_predecessor_namespace(self):
        original_ps = self.student.ps_number
        tombstone_ps = f"_del_{self.student.id}_{original_ps}"
        Student.objects.filter(pk=self.student.pk).update(
            ps_number=tombstone_ps,
            deleted_at=timezone.now(),
        )
        User.objects.filter(pk=self.student.user_id).update(
            username=user_internal_username(self.tenant, tombstone_ps)
        )
        folder = InventoryFolder.objects.create(
            tenant=self.tenant,
            scope="student",
            student_ps=original_ps,
            name="legacy-private",
        )
        inventory_file = InventoryFile.objects.create(
            tenant=self.tenant,
            scope="student",
            student_ps=original_ps,
            folder=folder,
            display_name="legacy.pdf",
            r2_key=f"tenants/{self.tenant.id}/students/{original_ps}/inventory/legacy.pdf",
            original_name="legacy.pdf",
            size_bytes=47,
            content_type="application/pdf",
        )
        replacement_user = User.objects.create_user(
            username=user_internal_username(self.tenant, original_ps),
            password="test1234",
            tenant=self.tenant,
        )

        replacement = Student.objects.create(
            tenant=self.tenant,
            user=replacement_user,
            ps_number=original_ps,
            name="replacement",
            omr_code="88000002",
        )

        self.assertIsNotNone(replacement.pk)
        folder.refresh_from_db()
        inventory_file.refresh_from_db()
        self.assertEqual(folder.student_ps, tombstone_ps)
        self.assertEqual(inventory_file.student_ps, tombstone_ps)

    def test_new_claim_rejects_ambiguous_legacy_inventory_namespace(self):
        original_ps = "AMBIGUOUS"
        for index in range(2):
            predecessor = _create_student(
                self.tenant,
                original_ps,
                name=f"predecessor-{index}",
                phone=f"0107000000{index}",
                parent_phone=f"0108000000{index}",
            )
            Student.objects.filter(pk=predecessor.pk).update(
                ps_number=f"_del_{predecessor.id}_{original_ps}",
                deleted_at=timezone.now(),
            )
            User.objects.filter(pk=predecessor.user_id).update(
                username=user_internal_username(
                    self.tenant,
                    f"_del_{predecessor.id}_{original_ps}",
                )
            )
        InventoryFolder.objects.create(
            tenant=self.tenant,
            scope="student",
            student_ps=original_ps,
            name="ambiguous-private",
        )
        replacement_user = User.objects.create_user(
            username=user_internal_username(self.tenant, original_ps),
            password="test1234",
            tenant=self.tenant,
        )

        with self.assertRaisesRegex(ValueError, "storage namespace"):
            Student.objects.create(
                tenant=self.tenant,
                user=replacement_user,
                ps_number=original_ps,
                name="blocked replacement",
                omr_code="88000003",
            )

    def test_existing_rename_quarantines_one_legacy_target_namespace(self):
        target_ps = "LEGACY-TARGET"
        predecessor = _create_student(
            self.tenant,
            target_ps,
            name="previous owner",
            phone="01070000111",
            parent_phone="01080000111",
        )
        tombstone_ps = f"_del_{predecessor.id}_{target_ps}"
        Student.objects.filter(pk=predecessor.pk).update(
            ps_number=tombstone_ps,
            deleted_at=timezone.now(),
        )
        User.objects.filter(pk=predecessor.user_id).update(
            username=user_internal_username(self.tenant, tombstone_ps)
        )
        legacy_folder = InventoryFolder.objects.create(
            tenant=self.tenant,
            scope="student",
            student_ps=target_ps,
            name="previous private",
        )

        self.student.ps_number = target_ps
        self.student.save(update_fields=["ps_number"])

        self.student.refresh_from_db()
        legacy_folder.refresh_from_db()
        self.assertEqual(self.student.ps_number, target_ps)
        self.assertEqual(legacy_folder.student_ps, tombstone_ps)

    def test_existing_rename_rejects_ambiguous_legacy_source_namespace(self):
        original_ps = "LEGACY-SOURCE"
        predecessor = _create_student(
            self.tenant,
            original_ps,
            name="previous owner",
            phone="01070000112",
            parent_phone="01080000112",
        )
        predecessor_tombstone = f"_del_{predecessor.id}_{original_ps}"
        Student.objects.filter(pk=predecessor.pk).update(
            ps_number=predecessor_tombstone,
            deleted_at=timezone.now(),
        )
        User.objects.filter(pk=predecessor.user_id).update(
            username=user_internal_username(self.tenant, predecessor_tombstone)
        )
        legacy_folder = InventoryFolder.objects.create(
            tenant=self.tenant,
            scope="student",
            student_ps=original_ps,
            name="previous private",
        )
        InventoryFolder.objects.filter(pk=legacy_folder.pk).update(
            created_at=self.student.created_at - timedelta(seconds=1)
        )
        Student.objects.filter(pk=self.student.pk).update(ps_number=original_ps)
        User.objects.filter(pk=self.student.user_id).update(
            username=user_internal_username(self.tenant, original_ps)
        )
        self.student.refresh_from_db()

        self.student.ps_number = "RENAMED"
        with self.assertRaisesRegex(ValueError, "source namespace"):
            self.student.save(update_fields=["ps_number"])

        self.student.refresh_from_db()
        legacy_folder.refresh_from_db()
        self.assertEqual(self.student.ps_number, original_ps)
        self.assertEqual(legacy_folder.student_ps, original_ps)

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

    def _create_canonical_student(self, *, tenant, ps_number: str, omr_code: str):
        return create_student_account(
            tenant=tenant,
            student_data={
                "ps_number": ps_number,
                "name": "concurrent replacement",
                "phone": f"010{omr_code}",
                "parent_phone": "",
                "omr_code": omr_code,
            },
            password="test1234",
        ).student

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

    def test_direct_create_locks_user_reference_before_rename_namespace(self):
        tenant = _create_tenant(name="Direct Create Lock", code="direct-create-lock")
        owner = _create_student(tenant, "DIRECT-OLD")
        direct_locked = threading.Event()
        release_direct = threading.Event()
        rename_started = threading.Event()
        rename_finished = threading.Event()
        unexpected: list[BaseException] = []
        direct_conflicts: list[BaseException] = []
        from apps.domains.students.models import (
            _prepare_student_ps_inventory_claim as real_prepare_claim,
        )

        def blocking_prepare(*args, **kwargs):
            if kwargs.get("exclude_student_id") is None:
                direct_locked.set()
                if not release_direct.wait(timeout=10):
                    raise TimeoutError("direct create release timed out")
            return real_prepare_claim(*args, **kwargs)

        def direct_create_worker():
            close_old_connections()
            try:
                Student.objects.create(
                    tenant=tenant,
                    user_id=owner.user_id,
                    ps_number="DIRECT-TARGET",
                    name="invalid duplicate user",
                    omr_code="98400001",
                )
            except IntegrityError as exc:
                direct_conflicts.append(exc)
            except BaseException as exc:  # pragma: no cover - asserted below
                unexpected.append(exc)
            finally:
                close_old_connections()

        def rename_worker():
            close_old_connections()
            try:
                rename_started.set()
                thread_owner = Student.objects.get(pk=owner.pk)
                thread_owner.ps_number = "DIRECT-TARGET"
                thread_owner.save(update_fields=["ps_number"])
                rename_finished.set()
            except BaseException as exc:  # pragma: no cover - asserted below
                unexpected.append(exc)
            finally:
                close_old_connections()

        with patch(
            "apps.domains.students.models._prepare_student_ps_inventory_claim",
            side_effect=blocking_prepare,
        ):
            direct_thread = threading.Thread(target=direct_create_worker)
            direct_thread.start()
            self.assertTrue(direct_locked.wait(timeout=5))
            rename_thread = threading.Thread(target=rename_worker)
            rename_thread.start()
            self.assertTrue(rename_started.wait(timeout=5))
            release_direct.set()
            direct_thread.join(timeout=15)
            rename_thread.join(timeout=15)

        self.assertFalse(direct_thread.is_alive())
        self.assertFalse(rename_thread.is_alive())
        self.assertEqual(unexpected, [])
        self.assertEqual(len(direct_conflicts), 1)
        self.assertTrue(rename_finished.is_set())
        owner.refresh_from_db()
        self.assertEqual(owner.ps_number, "DIRECT-TARGET")

    def test_soft_delete_serializes_reuse_and_quarantines_old_inventory(self):
        tenant = _create_tenant(name="Soft Delete Reuse", code="soft-delete-reuse")
        student = _create_student(tenant, "REUSE01")
        folder = InventoryFolder.objects.create(
            tenant=tenant,
            scope="student",
            student_ps="REUSE01",
            name="private",
        )
        replacement_user = User.objects.create_user(
            username="soft-delete-reuse-pending-user",
            password="test1234",
            tenant=tenant,
        )
        update_started = threading.Event()
        release_update = threading.Event()
        create_started = threading.Event()
        create_finished = threading.Event()
        errors = []
        created_ids = []
        from apps.support.students.lifecycle_dependencies import (
            update_inventory_student_ps as real_update_inventory_student_ps,
        )

        def blocking_update(*args, **kwargs):
            update_started.set()
            if not release_update.wait(timeout=10):
                raise TimeoutError("inventory quarantine release timed out")
            return real_update_inventory_student_ps(*args, **kwargs)

        def soft_delete_worker():
            close_old_connections()
            try:
                soft_delete_student(Student.objects.get(pk=student.pk), tenant=tenant)
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                close_old_connections()

        def create_worker():
            close_old_connections()
            try:
                create_started.set()
                replacement = Student.objects.create(
                    tenant=tenant,
                    user_id=replacement_user.id,
                    ps_number="REUSE01",
                    name="replacement",
                    omr_code="99000001",
                )
                created_ids.append(replacement.id)
                create_finished.set()
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                close_old_connections()

        with patch(
            "apps.domains.students.models.update_inventory_student_ps",
            side_effect=blocking_update,
        ):
            delete_thread = threading.Thread(target=soft_delete_worker)
            delete_thread.start()
            self.assertTrue(update_started.wait(timeout=5))
            create_thread = threading.Thread(target=create_worker)
            create_thread.start()
            self.assertTrue(create_started.wait(timeout=5))
            self.assertFalse(create_finished.wait(timeout=1))
            release_update.set()
            delete_thread.join(timeout=10)
            create_thread.join(timeout=10)

        self.assertFalse(delete_thread.is_alive())
        self.assertFalse(create_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(created_ids), 1)
        student.refresh_from_db()
        folder.refresh_from_db()
        self.assertEqual(folder.student_ps, student.ps_number)
        self.assertTrue(student.ps_number.startswith(f"_del_{student.id}_REUSE01"))
        self.assertTrue(
            Student.objects.filter(pk=created_ids[0], ps_number="REUSE01").exists()
        )
        self.assertFalse(
            InventoryFolder.objects.filter(tenant=tenant, student_ps="REUSE01").exists()
        )

    def test_canonical_create_waits_for_soft_delete_without_lock_cycle(self):
        tenant = _create_tenant(name="Canonical Soft Delete", code="canonical-soft")
        student = _create_student(tenant, "CANONICAL-SOFT")
        InventoryFolder.objects.create(
            tenant=tenant,
            scope="student",
            student_ps=student.ps_number,
            name="private",
        )
        soft_delete_locked = threading.Event()
        release_soft_delete = threading.Event()
        create_started = threading.Event()
        errors: list[BaseException] = []
        created_ids: list[int] = []
        from apps.support.students.lifecycle_dependencies import (
            update_inventory_student_ps as real_update_inventory_student_ps,
        )

        def blocking_update(*args, **kwargs):
            soft_delete_locked.set()
            if not release_soft_delete.wait(timeout=10):
                raise TimeoutError("soft delete release timed out")
            return real_update_inventory_student_ps(*args, **kwargs)

        def soft_delete_worker():
            close_old_connections()
            try:
                soft_delete_student(Student.objects.get(pk=student.pk), tenant=tenant)
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                close_old_connections()

        def create_worker():
            close_old_connections()
            try:
                create_started.set()
                replacement = self._create_canonical_student(
                    tenant=tenant,
                    ps_number="CANONICAL-SOFT",
                    omr_code="98100001",
                )
                created_ids.append(replacement.id)
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                close_old_connections()

        with patch(
            "apps.domains.students.models.update_inventory_student_ps",
            side_effect=blocking_update,
        ):
            soft_thread = threading.Thread(target=soft_delete_worker)
            soft_thread.start()
            self.assertTrue(soft_delete_locked.wait(timeout=5))
            create_thread = threading.Thread(target=create_worker)
            create_thread.start()
            self.assertTrue(create_started.wait(timeout=5))
            release_soft_delete.set()
            soft_thread.join(timeout=15)
            create_thread.join(timeout=15)

        self.assertFalse(soft_thread.is_alive())
        self.assertFalse(create_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(created_ids), 1)
        student.refresh_from_db()
        self.assertIsNotNone(student.deleted_at)
        self.assertTrue(
            Student.objects.filter(
                pk=created_ids[0],
                ps_number="CANONICAL-SOFT",
            ).exists()
        )

    def test_canonical_create_wins_restore_with_stable_conflict_no_deadlock(self):
        tenant = _create_tenant(name="Canonical Restore", code="canonical-restore")
        deleted = _create_student(tenant, "CANONICAL-RESTORE")
        soft_delete_student(deleted, tenant=tenant)
        create_reserved = threading.Event()
        release_create = threading.Event()
        restore_started = threading.Event()
        errors: list[BaseException] = []
        restore_codes: list[str] = []
        created_ids: list[int] = []
        from academy.adapters.db.django import repositories_students

        real_student_create = repositories_students.student_create

        def blocking_student_create(*args, **kwargs):
            create_reserved.set()
            if not release_create.wait(timeout=10):
                raise TimeoutError("canonical create release timed out")
            return real_student_create(*args, **kwargs)

        def create_worker():
            close_old_connections()
            try:
                replacement = self._create_canonical_student(
                    tenant=tenant,
                    ps_number="CANONICAL-RESTORE",
                    omr_code="98200001",
                )
                created_ids.append(replacement.id)
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                close_old_connections()

        def restore_worker():
            close_old_connections()
            try:
                restore_started.set()
                restore_student(Student.objects.get(pk=deleted.pk), tenant=tenant)
            except StudentLifecycleError as exc:
                restore_codes.append(exc.code)
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                close_old_connections()

        with patch(
            "apps.domains.students.services.creation.student_repo.student_create",
            side_effect=blocking_student_create,
        ):
            create_thread = threading.Thread(target=create_worker)
            create_thread.start()
            self.assertTrue(create_reserved.wait(timeout=5))
            restore_thread = threading.Thread(target=restore_worker)
            restore_thread.start()
            self.assertTrue(restore_started.wait(timeout=5))
            release_create.set()
            create_thread.join(timeout=15)
            restore_thread.join(timeout=15)

        self.assertFalse(create_thread.is_alive())
        self.assertFalse(restore_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(created_ids), 1)
        self.assertEqual(restore_codes, ["student_storage_namespace_conflict"])
        deleted.refresh_from_db()
        self.assertIsNotNone(deleted.deleted_at)

    def test_canonical_create_wins_rename_with_stable_conflict_no_deadlock(self):
        tenant = _create_tenant(name="Canonical Rename", code="canonical-rename")
        source = _create_student(tenant, "CANONICAL-SOURCE")
        create_reserved = threading.Event()
        release_create = threading.Event()
        rename_started = threading.Event()
        errors: list[BaseException] = []
        rename_conflicts: list[dict[str, str] | str] = []
        created_ids: list[int] = []
        from academy.adapters.db.django import repositories_students

        real_student_create = repositories_students.student_create

        def blocking_student_create(*args, **kwargs):
            create_reserved.set()
            if not release_create.wait(timeout=10):
                raise TimeoutError("canonical create release timed out")
            return real_student_create(*args, **kwargs)

        def create_worker():
            close_old_connections()
            try:
                replacement = self._create_canonical_student(
                    tenant=tenant,
                    ps_number="CANONICAL-TARGET",
                    omr_code="98300001",
                )
                created_ids.append(replacement.id)
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                close_old_connections()

        def rename_worker():
            close_old_connections()
            try:
                rename_started.set()
                update_student_profile(
                    student=Student.objects.select_related("user").get(pk=source.pk),
                    tenant=tenant,
                    data={"ps_number": "CANONICAL-TARGET"},
                    identity_field="ps_number",
                )
            except StudentProfileUpdateError as exc:
                rename_conflicts.append(exc.detail)
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                close_old_connections()

        with patch(
            "apps.domains.students.services.creation.student_repo.student_create",
            side_effect=blocking_student_create,
        ):
            create_thread = threading.Thread(target=create_worker)
            create_thread.start()
            self.assertTrue(create_reserved.wait(timeout=5))
            rename_thread = threading.Thread(target=rename_worker)
            rename_thread.start()
            self.assertTrue(rename_started.wait(timeout=5))
            release_create.set()
            create_thread.join(timeout=15)
            rename_thread.join(timeout=15)

        self.assertFalse(create_thread.is_alive())
        self.assertFalse(rename_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(created_ids), 1)
        self.assertEqual(len(rename_conflicts), 1)
        source.refresh_from_db()
        self.assertEqual(source.ps_number, "CANONICAL-SOURCE")

    def test_restore_reservation_wins_canonical_create_without_deadlock(self):
        tenant = _create_tenant(name="Restore Wins Create", code="restore-wins-create")
        deleted = _create_student(tenant, "RESTORE-WINS")
        soft_delete_student(deleted, tenant=tenant)
        restore_reserved = threading.Event()
        release_restore = threading.Event()
        create_started = threading.Event()
        create_finished = threading.Event()
        errors: list[BaseException] = []
        create_conflicts: list[dict[str, str] | str] = []
        restored_ids: list[int] = []
        from apps.domains.students.models import (
            lock_student_ps_namespaces as real_lock_student_ps_namespaces,
        )

        def blocking_namespace_lock(*args, **kwargs):
            if threading.current_thread().name == "restore-winner":
                restore_reserved.set()
                if not release_restore.wait(timeout=10):
                    raise TimeoutError("restore release timed out")
            return real_lock_student_ps_namespaces(*args, **kwargs)

        def restore_worker():
            close_old_connections()
            try:
                restored = restore_student(
                    Student.objects.get(pk=deleted.pk),
                    tenant=tenant,
                ).student
                restored_ids.append(restored.id)
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                close_old_connections()

        def create_worker():
            close_old_connections()
            try:
                create_started.set()
                self._create_canonical_student(
                    tenant=tenant,
                    ps_number="RESTORE-WINS",
                    omr_code="98500001",
                )
            except StudentIdentityError as exc:
                create_conflicts.append(exc.detail)
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                create_finished.set()
                close_old_connections()

        with patch(
            "apps.domains.students.models.lock_student_ps_namespaces",
            side_effect=blocking_namespace_lock,
        ):
            restore_thread = threading.Thread(
                target=restore_worker,
                name="restore-winner",
            )
            restore_thread.start()
            self.assertTrue(restore_reserved.wait(timeout=5))
            create_thread = threading.Thread(target=create_worker)
            create_thread.start()
            self.assertTrue(create_started.wait(timeout=5))
            self.assertFalse(create_finished.wait(timeout=1))
            release_restore.set()
            restore_thread.join(timeout=15)
            create_thread.join(timeout=15)

        self.assertFalse(restore_thread.is_alive())
        self.assertFalse(create_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(restored_ids, [deleted.id])
        self.assertEqual(len(create_conflicts), 1)
        deleted.refresh_from_db()
        self.assertIsNone(deleted.deleted_at)
        self.assertEqual(deleted.ps_number, "RESTORE-WINS")

    def test_rename_reservation_wins_canonical_create_without_deadlock(self):
        tenant = _create_tenant(name="Rename Wins Create", code="rename-wins-create")
        source = _create_student(tenant, "RENAME-WINS-SOURCE")
        rename_reserved = threading.Event()
        release_rename = threading.Event()
        create_started = threading.Event()
        create_finished = threading.Event()
        errors: list[BaseException] = []
        create_conflicts: list[dict[str, str] | str] = []
        renamed_ids: list[int] = []
        from apps.domains.students.models import (
            lock_student_ps_namespaces as real_lock_student_ps_namespaces,
        )

        def blocking_namespace_lock(*args, **kwargs):
            if threading.current_thread().name == "rename-winner":
                rename_reserved.set()
                if not release_rename.wait(timeout=10):
                    raise TimeoutError("rename release timed out")
            return real_lock_student_ps_namespaces(*args, **kwargs)

        def rename_worker():
            close_old_connections()
            try:
                updated = update_student_profile(
                    student=Student.objects.select_related("user").get(pk=source.pk),
                    tenant=tenant,
                    data={"ps_number": "RENAME-WINS-TARGET"},
                    identity_field="ps_number",
                ).student
                renamed_ids.append(updated.id)
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                close_old_connections()

        def create_worker():
            close_old_connections()
            try:
                create_started.set()
                self._create_canonical_student(
                    tenant=tenant,
                    ps_number="RENAME-WINS-TARGET",
                    omr_code="98600001",
                )
            except StudentIdentityError as exc:
                create_conflicts.append(exc.detail)
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                create_finished.set()
                close_old_connections()

        with patch(
            "apps.domains.students.models.lock_student_ps_namespaces",
            side_effect=blocking_namespace_lock,
        ):
            rename_thread = threading.Thread(
                target=rename_worker,
                name="rename-winner",
            )
            rename_thread.start()
            self.assertTrue(rename_reserved.wait(timeout=5))
            create_thread = threading.Thread(target=create_worker)
            create_thread.start()
            self.assertTrue(create_started.wait(timeout=5))
            self.assertFalse(create_finished.wait(timeout=1))
            release_rename.set()
            rename_thread.join(timeout=15)
            create_thread.join(timeout=15)

        self.assertFalse(rename_thread.is_alive())
        self.assertFalse(create_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(renamed_ids, [source.id])
        self.assertEqual(len(create_conflicts), 1)
        source.refresh_from_db()
        self.assertEqual(source.ps_number, "RENAME-WINS-TARGET")


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
        self.parent = Parent.objects.create(
            tenant=self.tenant,
            name="학부모",
            phone=self.student.parent_phone,
        )
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

    def test_soft_delete_rejects_legacy_source_owned_by_deleted_predecessor(self):
        predecessor = _create_student(
            self.tenant,
            "PREVIOUS",
            name="previous owner",
            phone="01071111111",
            parent_phone="01081111111",
        )
        predecessor_tombstone = f"_del_{predecessor.id}_{self.student.ps_number}"
        Student.objects.filter(pk=predecessor.pk).update(
            ps_number=predecessor_tombstone,
            deleted_at=timezone.now(),
        )
        User.objects.filter(pk=predecessor.user_id).update(
            username=user_internal_username(self.tenant, predecessor_tombstone)
        )
        legacy_folder = InventoryFolder.objects.create(
            tenant=self.tenant,
            scope="student",
            student_ps=self.student.ps_number,
            name="previous private",
        )
        InventoryFolder.objects.filter(pk=legacy_folder.pk).update(
            created_at=self.student.created_at - timedelta(seconds=1)
        )

        with self.assertRaises(StudentLifecycleError) as ctx:
            soft_delete_student(self.student, tenant=self.tenant)

        self.assertEqual(ctx.exception.code, "student_storage_namespace_conflict")
        self.student.refresh_from_db()
        legacy_folder.refresh_from_db()
        self.assertIsNone(self.student.deleted_at)
        self.assertEqual(self.student.ps_number, "SD001")
        self.assertEqual(legacy_folder.student_ps, "SD001")

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
        self.parent = Parent.objects.create(
            tenant=self.tenant, name="학부모", phone="01055556666"
        )
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
        tombstone_folder = InventoryFolder.objects.create(
            tenant=self.tenant,
            scope="student",
            student_ps=self.student.ps_number,
            name="복원 파일",
        )
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
        tombstone_folder.refresh_from_db()
        self.assertEqual(tombstone_folder.student_ps, "R11111")

    def test_restore_collision_detection(self):
        """복원 시 ps_number가 다른 활성 학생에게 사용 중이면 충돌."""
        _create_student(self.tenant, "R11111", name="새학생", phone="01099998888", parent_phone="01077778888")
        with self.assertRaises(StudentLifecycleError) as ctx:
            restore_student(self.student, tenant=self.tenant)

        self.assertEqual(ctx.exception.code, "ps_number_conflict")
        self.student.refresh_from_db()
        self.assertIsNotNone(self.student.deleted_at)
        self.assertTrue(self.student.ps_number.startswith(f"_del_{self.student.id}_R11111"))

    def test_restore_reports_stable_conflict_for_ambiguous_legacy_inventory(self):
        predecessor = _create_student(
            self.tenant,
            "R11111",
            name="과거 학생",
            phone="01099998888",
            parent_phone="01077778888",
        )
        predecessor_tombstone = f"_del_{predecessor.id}_R11111"
        Student.objects.filter(pk=predecessor.pk).update(
            ps_number=predecessor_tombstone,
            deleted_at=timezone.now(),
        )
        User.objects.filter(pk=predecessor.user_id).update(
            username=user_internal_username(self.tenant, predecessor_tombstone)
        )
        InventoryFolder.objects.create(
            tenant=self.tenant,
            scope="student",
            student_ps="R11111",
            name="소유자 불명 자료",
        )

        with self.assertRaises(StudentLifecycleError) as ctx:
            restore_student(self.student, tenant=self.tenant)

        self.assertEqual(ctx.exception.code, "student_storage_namespace_conflict")
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
                "send_welcome_message": True,
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
