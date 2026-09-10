from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.core.exceptions import ObjectDoesNotExist
from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.core.models import Tenant, TenantMembership
from apps.domains.students.models import Student, StudentRegistrationRequest
from apps.domains.students.services import registration_approval
from apps.domains.students.services.lifecycle import (
    StudentLifecycleError,
    permanently_delete_students,
    restore_student,
)
from apps.domains.students.views import student_views
from apps.domains.students.views.student_views import StudentViewSet
from apps.support.students.namespace_lock import (
    lock_student_creation_tenant_reference,
)


pytestmark = pytest.mark.django_db(transaction=True)
User = get_user_model()
Parent = django_apps.get_model("parents", "Parent")


class TestLifecycleTenantGateConcurrencyPostgres(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        if connection.vendor != "postgresql":
            raise unittest.SkipTest(
                "PostgreSQL is required for lifecycle tenant-gate verification."
            )
        super().setUpClass()

    def _deleted_multirole_student(self, *, suffix: str):
        tenant = Tenant.objects.create(
            name=f"Tenant Gate {suffix}",
            code=f"tenant-gate-{suffix}",
            is_active=True,
        )
        parent_phone = f"01081{int(suffix):06d}"
        shared_user = User.objects.create_user(
            username=f"p_{tenant.id}_{parent_phone}",
            password="test1234",
            tenant=tenant,
            phone=parent_phone,
        )
        parent = Parent.objects.create(
            tenant=tenant,
            user=shared_user,
            name="공유 학부모",
            phone=parent_phone,
        )
        TenantMembership.ensure_active(
            tenant=tenant,
            user=shared_user,
            role="parent",
        )
        student = Student.objects.create(
            tenant=tenant,
            user=shared_user,
            parent=parent,
            ps_number=f"GATE-{suffix}",
            name=f"삭제학생 {suffix}",
            phone=f"01082{int(suffix):06d}",
            parent_phone=parent_phone,
            omr_code=f"82{int(suffix):06d}",
            school_type="HIGH",
            grade=1,
        )
        Student.objects.filter(pk=student.pk).update(
            deleted_at=timezone.now(),
            ps_number=f"_del_{student.id}_GATE-{suffix}",
        )
        student.refresh_from_db()
        return tenant, parent, student

    def _start_action_first(
        self,
        *,
        gate_target: str,
        action,
        delete,
    ) -> tuple[list[BaseException], list[int]]:
        action_has_tenant_gate = threading.Event()
        release_action = threading.Event()
        delete_started = threading.Event()
        delete_finished = threading.Event()
        errors: list[BaseException] = []
        delete_counts: list[int] = []

        def blocking_gate(*, tenant_id: int):
            lock_student_creation_tenant_reference(tenant_id=tenant_id)
            if threading.current_thread().name == "tenant-gated-action":
                action_has_tenant_gate.set()
                if not release_action.wait(timeout=10):
                    raise TimeoutError("tenant-gated action release timed out")

        def action_worker():
            close_old_connections()
            try:
                action()
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                close_old_connections()

        def delete_worker():
            close_old_connections()
            try:
                delete_started.set()
                delete_counts.append(delete())
                delete_finished.set()
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                close_old_connections()

        with patch(gate_target, side_effect=blocking_gate):
            action_thread = threading.Thread(
                target=action_worker,
                name="tenant-gated-action",
            )
            action_thread.start()
            self.assertTrue(action_has_tenant_gate.wait(timeout=5))

            delete_thread = threading.Thread(target=delete_worker)
            delete_thread.start()
            self.assertTrue(delete_started.wait(timeout=5))
            self.assertFalse(
                delete_finished.wait(timeout=1),
                "Permanent delete crossed the caller's Tenant KEY SHARE gate.",
            )
            release_action.set()
            action_thread.join(timeout=15)
            delete_thread.join(timeout=15)

        self.assertFalse(action_thread.is_alive())
        self.assertFalse(delete_thread.is_alive())
        return errors, delete_counts

    def _start_delete_first(
        self,
        *,
        tenant,
        student_id: int,
        gate_target: str,
        action,
    ) -> tuple[list[BaseException], int]:
        action_reached_gate = threading.Event()
        action_finished = threading.Event()
        errors: list[BaseException] = []

        def observed_gate(*, tenant_id: int):
            action_reached_gate.set()
            return lock_student_creation_tenant_reference(tenant_id=tenant_id)

        def action_worker():
            close_old_connections()
            try:
                action()
            except BaseException as exc:  # expected outcome is asserted by caller
                errors.append(exc)
            finally:
                action_finished.set()
                close_old_connections()

        with transaction.atomic():
            deleted_count = permanently_delete_students(
                tenant=tenant,
                student_ids=[student_id],
            ).deleted_count
            with patch(gate_target, side_effect=observed_gate):
                action_thread = threading.Thread(target=action_worker)
                action_thread.start()
                self.assertTrue(action_reached_gate.wait(timeout=5))
                self.assertFalse(
                    action_finished.wait(timeout=1),
                    "Caller crossed permanent delete's Tenant FOR UPDATE lock.",
                )

        action_thread.join(timeout=15)
        self.assertFalse(action_thread.is_alive())
        return errors, deleted_count

    def test_restore_first_blocks_permanent_delete_without_deadlock(self):
        tenant, _parent, student = self._deleted_multirole_student(suffix="101")

        def restore_action():
            restore_student(
                Student.objects.get(pk=student.pk),
                tenant=Tenant.objects.get(pk=tenant.pk),
            )

        def delete_action():
            return permanently_delete_students(
                tenant=Tenant.objects.get(pk=tenant.pk),
                student_ids=[student.pk],
            ).deleted_count

        errors, delete_counts = self._start_action_first(
            gate_target=(
                "apps.domains.students.services.lifecycle."
                "lock_student_creation_tenant_reference"
            ),
            action=restore_action,
            delete=delete_action,
        )

        self.assertEqual(errors, [])
        self.assertEqual(delete_counts, [0])
        student.refresh_from_db()
        self.assertIsNone(student.deleted_at)

    def test_permanent_delete_first_blocks_restore_without_deadlock(self):
        tenant, _parent, student = self._deleted_multirole_student(suffix="102")

        def restore_action():
            try:
                restore_student(
                    Student.objects.get(pk=student.pk),
                    tenant=Tenant.objects.get(pk=tenant.pk),
                )
            except (ObjectDoesNotExist, StudentLifecycleError):
                return

        errors, deleted_count = self._start_delete_first(
            tenant=tenant,
            student_id=student.pk,
            gate_target=(
                "apps.domains.students.services.lifecycle."
                "lock_student_creation_tenant_reference"
            ),
            action=restore_action,
        )

        self.assertEqual(errors, [])
        self.assertEqual(deleted_count, 1)
        self.assertFalse(Student.objects.filter(pk=student.pk).exists())

    def _registration_for_shared_parent(self, *, suffix: str):
        tenant, parent, student = self._deleted_multirole_student(suffix=suffix)
        registration = StudentRegistrationRequest.objects.create(
            tenant=tenant,
            status=StudentRegistrationRequest.PENDING,
            initial_password=make_password("signup-password"),
            initial_password_plain="",
            name=f"신규승인학생 {suffix}",
            username=f"APPROVAL-{suffix}",
            parent_phone=parent.phone,
            phone=f"01083{int(suffix):06d}",
            school_type="HIGH",
            high_school="잠금순서고",
            origin_middle_school="잠금순서중",
            grade=1,
            gender="M",
            address="서울",
        )
        return tenant, student, registration

    def test_registration_approval_first_blocks_permanent_delete_without_deadlock(self):
        tenant, student, registration = self._registration_for_shared_parent(
            suffix="103"
        )
        approved_ids: list[int] = []

        def approval_action():
            result = registration_approval.approve_registration_request(
                tenant=Tenant.objects.get(pk=tenant.pk),
                registration_id=registration.pk,
            )
            approved_ids.append(result.student.pk)

        def delete_action():
            return permanently_delete_students(
                tenant=Tenant.objects.get(pk=tenant.pk),
                student_ids=[student.pk],
            ).deleted_count

        errors, delete_counts = self._start_action_first(
            gate_target=(
                "apps.domains.students.services.registration_approval."
                "lock_student_creation_tenant_reference"
            ),
            action=approval_action,
            delete=delete_action,
        )

        self.assertEqual(errors, [])
        self.assertEqual(delete_counts, [1])
        self.assertEqual(len(approved_ids), 1)
        self.assertFalse(Student.objects.filter(pk=student.pk).exists())
        self.assertTrue(Student.objects.filter(pk=approved_ids[0]).exists())

    def test_permanent_delete_first_blocks_registration_approval_without_deadlock(self):
        tenant, student, registration = self._registration_for_shared_parent(
            suffix="104"
        )
        approved_ids: list[int] = []

        def approval_action():
            result = registration_approval.approve_registration_request(
                tenant=Tenant.objects.get(pk=tenant.pk),
                registration_id=registration.pk,
            )
            approved_ids.append(result.student.pk)

        errors, deleted_count = self._start_delete_first(
            tenant=tenant,
            student_id=student.pk,
            gate_target=(
                "apps.domains.students.services.registration_approval."
                "lock_student_creation_tenant_reference"
            ),
            action=approval_action,
        )

        self.assertEqual(errors, [])
        self.assertEqual(deleted_count, 1)
        self.assertEqual(len(approved_ids), 1)
        self.assertFalse(Student.objects.filter(pk=student.pk).exists())
        self.assertTrue(Student.objects.filter(pk=approved_ids[0]).exists())

    def _admin_profile_action(
        self,
        *,
        tenant_id: int,
        student_id: int,
        parent_phone: str,
    ) -> None:
        serializer = SimpleNamespace(
            instance=Student.objects.select_related("user").get(pk=student_id),
            validated_data={"parent_phone": parent_phone},
        )
        view = StudentViewSet()
        view.request = SimpleNamespace(tenant=Tenant.objects.get(pk=tenant_id))
        with patch(
            "apps.domains.students.services.account_notifications."
            "send_parent_account_credentials_notice",
            return_value=True,
        ):
            view.perform_update(serializer)

    def test_admin_profile_update_first_blocks_permanent_delete_without_deadlock(self):
        tenant, _parent, student = self._deleted_multirole_student(suffix="105")
        new_parent_phone = "01084100105"

        def profile_action():
            self._admin_profile_action(
                tenant_id=tenant.pk,
                student_id=student.pk,
                parent_phone=new_parent_phone,
            )

        def delete_action():
            return permanently_delete_students(
                tenant=Tenant.objects.get(pk=tenant.pk),
                student_ids=[student.pk],
            ).deleted_count

        errors, delete_counts = self._start_action_first(
            gate_target=(
                "apps.domains.students.views.student_views."
                "lock_student_creation_tenant_reference"
            ),
            action=profile_action,
            delete=delete_action,
        )

        self.assertEqual(errors, [])
        self.assertEqual(delete_counts, [1])
        self.assertFalse(Student.objects.filter(pk=student.pk).exists())

    def test_permanent_delete_first_blocks_admin_profile_update_without_deadlock(self):
        tenant, _parent, student = self._deleted_multirole_student(suffix="106")
        profile_conflicts: list[object] = []

        def profile_action():
            try:
                self._admin_profile_action(
                    tenant_id=tenant.pk,
                    student_id=student.pk,
                    parent_phone="01084100106",
                )
            except ValidationError as exc:
                profile_conflicts.append(exc.detail)

        errors, deleted_count = self._start_delete_first(
            tenant=tenant,
            student_id=student.pk,
            gate_target=(
                "apps.domains.students.views.student_views."
                "lock_student_creation_tenant_reference"
            ),
            action=profile_action,
        )

        self.assertEqual(errors, [])
        self.assertEqual(deleted_count, 1)
        self.assertEqual(len(profile_conflicts), 1)
        self.assertFalse(Student.objects.filter(pk=student.pk).exists())
