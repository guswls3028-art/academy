from __future__ import annotations

import threading
import unittest

import pytest
from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection
from django.test import TransactionTestCase

from apps.core.models import Tenant, TenantMembership
from apps.domains.parents.models import Parent
from apps.domains.parents.services import ensure_parent_account_for_student


pytestmark = pytest.mark.django_db(transaction=True)


class ParentAccountCreationConcurrencyPostgresTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        if connection.vendor != "postgresql":
            raise unittest.SkipTest("PostgreSQL is required for parent account concurrency verification.")
        super().setUpClass()

    def test_concurrent_students_share_one_parent_account(self):
        tenant = Tenant.objects.create(name="Parent Race", code="parent-race")
        barrier = threading.Barrier(2, timeout=10)
        outcomes: list[tuple[int, bool]] = []
        errors: list[Exception] = []

        def worker(student_name: str):
            close_old_connections()
            try:
                thread_tenant = Tenant.objects.get(pk=tenant.pk)
                barrier.wait()
                result = ensure_parent_account_for_student(
                    tenant=thread_tenant,
                    parent_phone="01098765432",
                    student_name=student_name,
                    initial_password="explicit-password",
                )
                outcomes.append((result.parent.id, result.credentials_initialized))
            except Exception as exc:
                errors.append(exc)
            finally:
                close_old_connections()

        threads = [
            threading.Thread(target=worker, args=("첫째",)),
            threading.Thread(target=worker, args=("둘째",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(len({parent_id for parent_id, _created in outcomes}), 1)
        self.assertEqual(sum(1 for _parent_id, created in outcomes if created), 1)
        parent = Parent.objects.get(tenant=tenant, phone="01098765432")
        self.assertEqual(
            get_user_model().objects.filter(username=f"p_{tenant.id}_01098765432").count(),
            1,
        )
        self.assertEqual(
            TenantMembership.objects.filter(
                tenant=tenant,
                user=parent.user,
                role="parent",
                is_active=True,
            ).count(),
            1,
        )
