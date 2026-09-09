from __future__ import annotations

import json
import threading
import unittest
from unittest.mock import patch

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import close_old_connections, connection
from django.test import TransactionTestCase
from rest_framework.test import APIRequestFactory

from academy.adapters.db.django import repositories_inventory as inv_repo
from apps.core.models import Tenant, TenantMembership
from apps.domains.inventory.models import InventoryFile
from apps.domains.inventory.views import FileUploadView
from apps.support.inventory.student_dependencies import (
    permanently_delete_students_for_storage,
    soft_delete_student_for_storage,
)


User = get_user_model()
Student = django_apps.get_model("students", "Student")
SubmissionStorageCleanupIntent = django_apps.get_model(
    "submissions",
    "SubmissionStorageCleanupIntent",
)


class TestStudentUploadLifecycleConcurrencyPostgres(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        if connection.vendor != "postgresql":
            raise unittest.SkipTest(
                "PostgreSQL is required for student upload lifecycle concurrency."
            )
        super().setUpClass()

    def setUp(self):
        self.factory = APIRequestFactory()
        self.tenant = Tenant.objects.create(
            code="inventory-upload-lifecycle-race",
            name="Inventory Upload Lifecycle Race",
            is_active=True,
        )
        self.staff = User.objects.create_user(
            username="inventory-upload-race-staff",
            password="test1234",
            tenant=self.tenant,
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=self.staff,
            role="teacher",
        )
        self.student_user = User.objects.create_user(
            username="inventory-upload-race-student",
            password="test1234",
            tenant=self.tenant,
        )
        self.student = Student.objects.create(
            tenant=self.tenant,
            user=self.student_user,
            ps_number="UPLOAD-RACE",
            name="업로드 학생",
            omr_code="93000001",
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=self.student_user,
            role="student",
        )

    def _upload_request(self):
        upload = SimpleUploadedFile(
            "race.pdf",
            b"%PDF-1.4\n% upload race\n%%EOF",
            content_type="application/pdf",
        )
        request = self.factory.post(
            "/storage/inventory/upload/",
            data={
                "scope": "student",
                "student_ps": self.student.ps_number,
                "file": upload,
            },
            format="multipart",
        )
        request.tenant = self.tenant
        return request

    def test_delete_commits_during_put_then_attach_fails_and_compensates_exact_key(self):
        put_started = threading.Event()
        release_put = threading.Event()
        upload_finished = threading.Event()
        upload_errors = []
        responses = []
        storage_objects = set()

        def fake_put(*, key, **kwargs):
            put_started.set()
            if not release_put.wait(timeout=10):
                raise TimeoutError("test did not release R2 PUT")
            storage_objects.add(key)

        def fake_delete(*, key):
            storage_objects.discard(key)

        def upload_worker():
            close_old_connections()
            try:
                response = FileUploadView.as_view()(self._upload_request())
                responses.append(response)
                upload_finished.set()
            except BaseException as exc:  # pragma: no cover - asserted below
                upload_errors.append(exc)
            finally:
                close_old_connections()

        with patch(
            "apps.domains.inventory.views.JWTAuthentication.authenticate",
            return_value=(self.staff, None),
        ), patch(
            "apps.domains.inventory.views.upload_fileobj_to_r2_storage",
            side_effect=fake_put,
        ), patch(
            "apps.domains.inventory.views.delete_object_r2_storage",
            side_effect=fake_delete,
        ):
            upload_thread = threading.Thread(target=upload_worker)
            upload_thread.start()
            self.assertTrue(put_started.wait(timeout=5))
            thread_student = Student.objects.get(pk=self.student.pk)
            soft_delete_student_for_storage(thread_student, tenant=self.tenant)
            result = permanently_delete_students_for_storage(
                tenant=self.tenant,
                student_ids=[self.student.id],
            )
            self.assertEqual(result.deleted_count, 1)
            release_put.set()
            upload_thread.join(timeout=10)

        self.assertFalse(upload_thread.is_alive())
        self.assertEqual(upload_errors, [])
        self.assertTrue(upload_finished.is_set())
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0].status_code, 409)
        self.assertEqual(
            json.loads(responses[0].content)["code"],
            "student_storage_owner_missing",
        )
        self.assertEqual(storage_objects, set())
        self.assertFalse(
            InventoryFile.objects.filter(
                tenant=self.tenant,
                student_ps="UPLOAD-RACE",
            ).exists()
        )

    def test_upload_attach_serializes_soft_and_permanent_delete_then_cleans_storage(self):
        attach_started = threading.Event()
        release_attach = threading.Event()
        delete_started = threading.Event()
        delete_finished = threading.Event()
        upload_errors = []
        delete_errors = []
        responses = []
        delete_results = []
        storage_objects = set()
        real_inventory_file_create = inv_repo.inventory_file_create

        def fake_put(*, key, **kwargs):
            storage_objects.add(key)

        def blocking_inventory_file_create(*args, **kwargs):
            attach_started.set()
            if not release_attach.wait(timeout=10):
                raise TimeoutError("test did not release inventory attach")
            return real_inventory_file_create(*args, **kwargs)

        def fake_delete(*, key):
            storage_objects.discard(key)

        def upload_worker():
            close_old_connections()
            try:
                responses.append(FileUploadView.as_view()(self._upload_request()))
            except BaseException as exc:  # pragma: no cover - asserted below
                upload_errors.append(exc)
            finally:
                close_old_connections()

        def delete_worker():
            close_old_connections()
            try:
                delete_started.set()
                thread_student = Student.objects.get(pk=self.student.pk)
                soft_delete_student_for_storage(thread_student, tenant=self.tenant)
                delete_results.append(
                    permanently_delete_students_for_storage(
                        tenant=self.tenant,
                        student_ids=[self.student.id],
                    )
                )
                delete_finished.set()
            except BaseException as exc:  # pragma: no cover - asserted below
                delete_errors.append(exc)
            finally:
                close_old_connections()

        with patch(
            "apps.domains.inventory.views.JWTAuthentication.authenticate",
            return_value=(self.staff, None),
        ), patch(
            "apps.domains.inventory.views.upload_fileobj_to_r2_storage",
            side_effect=fake_put,
        ), patch(
            "apps.domains.inventory.views.inv_repo.inventory_file_create",
            side_effect=blocking_inventory_file_create,
        ), patch(
            "apps.infrastructure.storage.r2.delete_object_r2_storage",
            side_effect=fake_delete,
        ):
            upload_thread = threading.Thread(target=upload_worker)
            upload_thread.start()
            self.assertTrue(attach_started.wait(timeout=5))
            delete_thread = threading.Thread(target=delete_worker)
            delete_thread.start()
            self.assertTrue(delete_started.wait(timeout=5))
            self.assertFalse(
                delete_finished.wait(timeout=1),
                "Student delete crossed an in-flight inventory attachment lock.",
            )
            release_attach.set()
            upload_thread.join(timeout=10)
            delete_thread.join(timeout=10)

        self.assertFalse(upload_thread.is_alive())
        self.assertFalse(delete_thread.is_alive())
        self.assertEqual(upload_errors, [])
        self.assertEqual(delete_errors, [])
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0].status_code, 200)
        self.assertEqual(len(delete_results), 1)
        self.assertEqual(delete_results[0].deleted_count, 1)
        self.assertEqual(storage_objects, set())
        self.assertFalse(Student.objects.filter(pk=self.student.pk).exists())
        self.assertFalse(InventoryFile.objects.filter(tenant=self.tenant).exists())
        intent = SubmissionStorageCleanupIntent.objects.get(
            bucket=SubmissionStorageCleanupIntent.Bucket.STORAGE,
        )
        self.assertEqual(intent.status, SubmissionStorageCleanupIntent.Status.CLEANED)
