from __future__ import annotations

from datetime import timedelta
import json
from unittest.mock import patch

from django.apps import apps
from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory

from apps.core.models import Tenant, TenantMembership
from apps.domains.inventory.models import InventoryFile, InventoryFolder
from apps.domains.inventory.r2_path import safe_filename
from apps.domains.inventory.services import delete_folder_recursive, move_file, move_folder
from apps.domains.inventory.views import (
    FileDeleteView,
    FileUploadView,
    FolderCreateView,
    FolderDeleteView,
    InventoryListView,
    PresignView,
    QuotaView,
)
from apps.domains.students.models import Student
from apps.support.inventory.storage_cleanup_dependencies import (
    compensate_unattached_storage_object,
    process_pending_storage_cleanup_intents,
    uncertain_storage_write_settle_delay,
)
from apps.support.inventory.student_dependencies import (
    student_storage_namespace_has_legacy_conflict,
)


User = get_user_model()
SubmissionStorageCleanupIntent = django_apps.get_model(
    "submissions",
    "SubmissionStorageCleanupIntent",
)
StudentReportedScore = django_apps.get_model("results", "StudentReportedScore")
Parent = apps.get_model("parents", "Parent")


class InventoryHardeningViewTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.tenant = Tenant.objects.create(code="inv-hard", name="Inventory Hardening", is_active=True)
        self.staff = User.objects.create_user(
            username="inv-hard-staff",
            password="test1234",
            tenant=self.tenant,
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=self.staff, role="teacher")
        self.student_user = User.objects.create_user(
            username="inv-hard-student",
            password="test1234",
            tenant=self.tenant,
        )
        self.student = Student.objects.create(
            tenant=self.tenant,
            user=self.student_user,
            ps_number="S001",
            omr_code="12345678",
            name="학생",
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=self.student_user,
            role="student",
        )
        self.parent_user = User.objects.create_user(
            username="inv-hard-parent",
            password="test1234",
            tenant=self.tenant,
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=self.parent_user,
            role="parent",
        )
        self.parent = Parent.objects.create(
            tenant=self.tenant,
            user=self.parent_user,
            name="학부모",
            phone="01099998888",
        )
        self.student.parent = self.parent
        self.student.save(update_fields=["parent"])
        self.other_student_user = User.objects.create_user(
            username="inv-hard-student-2",
            password="test1234",
            tenant=self.tenant,
        )
        self.other_student = Student.objects.create(
            tenant=self.tenant,
            user=self.other_student_user,
            ps_number="S002",
            omr_code="87654321",
            name="다른학생",
        )

    def _json_request(self, path, body, user):
        request = self.factory.post(path, data=json.dumps(body), content_type="application/json")
        request.tenant = self.tenant
        return request

    def _multipart_request(self, path, data):
        request = self.factory.post(path, data=data, format="multipart")
        request.tenant = self.tenant
        return request

    def _auth(self, user):
        return patch("apps.domains.inventory.views.JWTAuthentication.authenticate", return_value=(user, None))

    def assert_empty_no_content_response(self, response):
        self.assertEqual(response.status_code, 204, response.content)
        self.assertEqual(response.content, b"")
        self.assertIn(response.headers.get("Content-Length"), (None, "0"))

    def test_parent_selected_child_can_manage_inventory_and_staff_sees_same_projection(self):
        create_request = self.factory.post(
            "/storage/inventory/folders/",
            data=json.dumps({
                "scope": "student",
                "student_ps": self.student.ps_number,
                "name": "학부모 제출",
            }),
            content_type="application/json",
            HTTP_X_STUDENT_ID=str(self.student.id),
        )
        create_request.tenant = self.tenant
        with self._auth(self.parent_user):
            created = FolderCreateView.as_view()(create_request)
        self.assertEqual(created.status_code, 200, created.content)
        folder_id = int(json.loads(created.content)["id"])

        upload_request = self.factory.post(
            "/storage/inventory/upload/",
            data={
                "scope": "student",
                "student_ps": self.student.ps_number,
                "folder_id": str(folder_id),
                "file": SimpleUploadedFile(
                    "parent-note.pdf",
                    b"%PDF-parent-note",
                    content_type="application/pdf",
                ),
            },
            format="multipart",
            HTTP_X_STUDENT_ID=str(self.student.id),
        )
        upload_request.tenant = self.tenant
        with self._auth(self.parent_user), patch(
            "apps.domains.inventory.views.upload_fileobj_to_r2_storage"
        ) as upload_r2:
            uploaded = FileUploadView.as_view()(upload_request)
        self.assertEqual(uploaded.status_code, 200, uploaded.content)
        upload_r2.assert_called_once()
        file_id = int(json.loads(uploaded.content)["id"])

        for user in (self.parent_user, self.staff):
            list_request = self.factory.get(
                "/storage/inventory/",
                {"scope": "student", "student_ps": self.student.ps_number},
                HTTP_X_STUDENT_ID=str(self.student.id),
            )
            list_request.tenant = self.tenant
            with self._auth(user):
                listed = InventoryListView.as_view()(list_request)
            self.assertEqual(listed.status_code, 200, listed.content)
            self.assertEqual([int(row["id"]) for row in json.loads(listed.content)["files"]], [file_id])

        delete_file_request = self.factory.delete(
            (
                f"/storage/inventory/files/{file_id}/?scope=student"
                f"&student_ps={self.student.ps_number}"
            ),
            HTTP_X_STUDENT_ID=str(self.student.id),
        )
        delete_file_request.tenant = self.tenant
        with self._auth(self.parent_user), patch(
            "apps.domains.inventory.views.delete_object_r2_storage"
        ) as delete_r2:
            deleted_file = FileDeleteView.as_view()(delete_file_request, file_id=file_id)
        self.assert_empty_no_content_response(deleted_file)
        delete_r2.assert_called_once()

        delete_folder_request = self.factory.delete(
            (
                f"/storage/inventory/folders/{folder_id}/?scope=student"
                f"&student_ps={self.student.ps_number}"
            ),
            HTTP_X_STUDENT_ID=str(self.student.id),
        )
        delete_folder_request.tenant = self.tenant
        with self._auth(self.parent_user):
            deleted_folder = FolderDeleteView.as_view()(
                delete_folder_request,
                folder_id=folder_id,
            )
        self.assert_empty_no_content_response(deleted_folder)
        self.assertFalse(InventoryFolder.objects.filter(id=folder_id).exists())

    def test_parent_inventory_rejects_sibling_scope_without_storage_side_effect(self):
        request = self.factory.post(
            "/storage/inventory/upload/",
            data={
                "scope": "student",
                "student_ps": self.other_student.ps_number,
                "file": SimpleUploadedFile(
                    "wrong-child.pdf",
                    b"%PDF-wrong-child",
                    content_type="application/pdf",
                ),
            },
            format="multipart",
            HTTP_X_STUDENT_ID=str(self.student.id),
        )
        request.tenant = self.tenant
        with self._auth(self.parent_user), patch(
            "apps.domains.inventory.views.upload_fileobj_to_r2_storage"
        ) as upload_r2:
            response = FileUploadView.as_view()(request)
        self.assertEqual(response.status_code, 403, response.content)
        upload_r2.assert_not_called()
        self.assertFalse(InventoryFile.objects.filter(original_name="wrong-child.pdf").exists())

    def test_admin_upload_attach_uses_shared_admin_mutation_lock(self):
        request = self.factory.post(
            "/storage/inventory/upload/",
            data={
                "scope": "admin",
                "file": SimpleUploadedFile(
                    "admin-lock.pdf",
                    b"%PDF-admin-lock",
                    content_type="application/pdf",
                ),
            },
            format="multipart",
        )
        request.tenant = self.tenant

        with self._auth(self.staff), patch(
            "apps.domains.inventory.views.upload_fileobj_to_r2_storage"
        ), patch(
            "apps.domains.inventory.views.lock_student_ps_namespaces"
        ) as lock_namespace:
            response = FileUploadView.as_view()(request)

        self.assertEqual(response.status_code, 200, response.content)
        lock_namespace.assert_called_once_with(
            tenant_id=self.tenant.id,
            ps_numbers=("__admin_inventory_move__",),
        )

    def test_parent_inventory_rejects_unowned_and_cross_tenant_selected_children(self):
        foreign_tenant = Tenant.objects.create(
            code="inv-hard-foreign",
            name="Inventory Foreign",
            is_active=True,
        )
        foreign_user = User.objects.create_user(
            username="inv-hard-foreign-student",
            password="test1234",
            tenant=foreign_tenant,
        )
        foreign_student = Student.objects.create(
            tenant=foreign_tenant,
            user=foreign_user,
            ps_number="FOREIGN-001",
            omr_code="11223344",
            name="타학원학생",
        )

        for selected_student in (self.other_student, foreign_student):
            request = self.factory.post(
                "/storage/inventory/upload/",
                data={
                    "scope": "student",
                    "student_ps": selected_student.ps_number,
                    "file": SimpleUploadedFile(
                        "unowned-child.pdf",
                        b"%PDF-unowned-child",
                        content_type="application/pdf",
                    ),
                },
                format="multipart",
                HTTP_X_STUDENT_ID=str(selected_student.id),
            )
            request.tenant = self.tenant
            with self._auth(self.parent_user), patch(
                "apps.domains.inventory.views.upload_fileobj_to_r2_storage"
            ) as upload_r2:
                response = FileUploadView.as_view()(request)
            self.assertEqual(response.status_code, 403, response.content)
            upload_r2.assert_not_called()
        self.assertFalse(
            InventoryFile.objects.filter(original_name="unowned-child.pdf").exists()
        )

    def test_presign_requires_inventory_file_for_raw_r2_key(self):
        request = self._json_request(
            "/storage/inventory/presign/",
            {"r2_key": f"tenants/{self.tenant.id}/admin/inventory/orphan.pdf"},
            self.staff,
        )

        with self._auth(self.staff), patch("apps.domains.inventory.views.generate_presigned_get_url_storage") as presign:
            response = PresignView.as_view()(request)

        self.assertEqual(response.status_code, 404)
        presign.assert_not_called()

    def test_presign_by_file_id_uses_file_row_authorization(self):
        inv_file = InventoryFile.objects.create(
            tenant=self.tenant,
            scope="admin",
            student_ps="",
            folder=None,
            display_name="admin.pdf",
            original_name="admin.pdf",
            r2_key=f"tenants/{self.tenant.id}/admin/inventory/admin.pdf",
            content_type="application/pdf",
        )
        request = self._json_request(
            "/storage/inventory/presign/",
            {"file_id": inv_file.id, "r2_key": inv_file.r2_key},
            self.staff,
        )

        with self._auth(self.staff), patch(
            "apps.domains.inventory.views.generate_presigned_get_url_storage",
            return_value="https://example.test/signed",
        ) as presign:
            response = PresignView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content), {"url": "https://example.test/signed"})
        presign.assert_called_once_with(key=inv_file.r2_key, expires_in=3600)

    def test_student_cannot_presign_other_students_file(self):
        inv_file = InventoryFile.objects.create(
            tenant=self.tenant,
            scope="student",
            student_ps=self.other_student.ps_number,
            folder=None,
            display_name="other.pdf",
            original_name="other.pdf",
            r2_key=f"tenants/{self.tenant.id}/students/{self.other_student.ps_number}/inventory/other.pdf",
            content_type="application/pdf",
        )
        request = self._json_request("/storage/inventory/presign/", {"file_id": inv_file.id}, self.student_user)

        with self._auth(self.student_user), patch("apps.domains.inventory.views.generate_presigned_get_url_storage") as presign:
            response = PresignView.as_view()(request)

        self.assertEqual(response.status_code, 403)
        presign.assert_not_called()

    def test_file_delete_rejects_matchup_document_with_owner_pinned_problem(self):
        from apps.domains.matchup.models import MatchupDocument, MatchupProblem

        inv_file = InventoryFile.objects.create(
            tenant=self.tenant,
            scope="admin",
            student_ps="",
            folder=None,
            display_name="matchup.pdf",
            original_name="matchup.pdf",
            r2_key=f"tenants/{self.tenant.id}/admin/inventory/matchup.pdf",
            content_type="application/pdf",
        )
        doc = MatchupDocument.objects.create(
            tenant=self.tenant,
            inventory_file=inv_file,
            title="matchup",
            r2_key=inv_file.r2_key,
            original_name=inv_file.original_name,
            content_type=inv_file.content_type,
        )
        MatchupProblem.objects.create(
            tenant=self.tenant,
            document=doc,
            number=1,
            text="pinned",
            meta={"manual_owner_pinned": True},
        )
        request = self.factory.delete(f"/storage/inventory/files/{inv_file.id}/?scope=admin")
        request.tenant = self.tenant

        with self._auth(self.staff), patch(
            "apps.domains.matchup.services.cleanup_matchup_problem_images"
        ) as cleanup:
            response = FileDeleteView.as_view()(request, file_id=inv_file.id)

        self.assertEqual(response.status_code, 409)
        cleanup.assert_not_called()
        self.assertTrue(InventoryFile.objects.filter(id=inv_file.id).exists())

    def test_presign_rejects_inventory_row_with_cross_tenant_key_prefix(self):
        inv_file = InventoryFile.objects.create(
            tenant=self.tenant,
            scope="admin",
            student_ps="",
            folder=None,
            display_name="bad.pdf",
            original_name="bad.pdf",
            r2_key="tenants/999999/admin/inventory/bad.pdf",
            content_type="application/pdf",
        )
        request = self._json_request("/storage/inventory/presign/", {"file_id": inv_file.id}, self.staff)

        with self._auth(self.staff), patch("apps.domains.inventory.views.generate_presigned_get_url_storage") as presign:
            response = PresignView.as_view()(request)

        self.assertEqual(response.status_code, 403)
        presign.assert_not_called()

    def test_folder_create_rejects_cross_scope_parent(self):
        parent = InventoryFolder.objects.create(
            tenant=self.tenant,
            scope="admin",
            student_ps="",
            name="admin-parent",
        )
        request = self._json_request(
            "/storage/inventory/folders/",
            {"scope": "student", "student_ps": self.student.ps_number, "parent_id": parent.id, "name": "bad-child"},
            self.staff,
        )

        with self._auth(self.staff):
            response = FolderCreateView.as_view()(request)

        self.assertEqual(response.status_code, 403)
        self.assertFalse(InventoryFolder.objects.filter(tenant=self.tenant, name="bad-child").exists())

    def test_upload_rejects_missing_folder_id_instead_of_uploading_to_root(self):
        upload = SimpleUploadedFile("x.pdf", b"%PDF-1.4", content_type="application/pdf")
        request = self._multipart_request(
            "/storage/inventory/upload/",
            {"scope": "admin", "folder_id": "999999", "file": upload},
        )

        with self._auth(self.staff), patch("apps.domains.inventory.views.upload_fileobj_to_r2_storage") as upload_r2:
            response = FileUploadView.as_view()(request)

        self.assertEqual(response.status_code, 404)
        upload_r2.assert_not_called()
        self.assertFalse(InventoryFile.objects.filter(tenant=self.tenant, original_name="x.pdf").exists())

    def test_upload_rejects_cross_scope_folder(self):
        folder = InventoryFolder.objects.create(
            tenant=self.tenant,
            scope="student",
            student_ps=self.student.ps_number,
            name="student-folder",
        )
        upload = SimpleUploadedFile("x.pdf", b"%PDF-1.4", content_type="application/pdf")
        request = self._multipart_request(
            "/storage/inventory/upload/",
            {"scope": "admin", "folder_id": str(folder.id), "file": upload},
        )

        with self._auth(self.staff), patch("apps.domains.inventory.views.upload_fileobj_to_r2_storage") as upload_r2:
            response = FileUploadView.as_view()(request)

        self.assertEqual(response.status_code, 403)
        upload_r2.assert_not_called()

    def test_upload_rejects_invalid_matchup_promotion_before_storage_side_effects(self):
        upload = SimpleUploadedFile("x.txt", b"hello", content_type="text/plain")
        request = self._multipart_request(
            "/storage/inventory/upload/",
            {"scope": "admin", "promote_to_matchup": "true", "file": upload},
        )

        with self._auth(self.staff), patch("apps.domains.inventory.views.upload_fileobj_to_r2_storage") as upload_r2:
            response = FileUploadView.as_view()(request)

        self.assertEqual(response.status_code, 400)
        upload_r2.assert_not_called()
        self.assertFalse(InventoryFile.objects.filter(tenant=self.tenant, original_name="x.txt").exists())

    def test_upload_rejects_student_scope_matchup_promotion_before_storage_side_effects(self):
        upload = SimpleUploadedFile("x.pdf", b"%PDF-1.4", content_type="application/pdf")
        request = self._multipart_request(
            "/storage/inventory/upload/",
            {
                "scope": "student",
                "student_ps": self.student.ps_number,
                "promote_to_matchup": "true",
                "file": upload,
            },
        )

        with self._auth(self.staff), patch("apps.domains.inventory.views.upload_fileobj_to_r2_storage") as upload_r2:
            response = FileUploadView.as_view()(request)

        self.assertEqual(response.status_code, 400)
        upload_r2.assert_not_called()
        self.assertFalse(InventoryFile.objects.filter(tenant=self.tenant, original_name="x.pdf").exists())

    def test_staff_student_upload_revalidates_active_owner_before_r2_put(self):
        upload = SimpleUploadedFile("x.pdf", b"%PDF-1.4", content_type="application/pdf")
        request = self._multipart_request(
            "/storage/inventory/upload/",
            {
                "scope": "student",
                "student_ps": "MISSING-STUDENT",
                "file": upload,
            },
        )

        with self._auth(self.staff), patch(
            "apps.domains.inventory.views.upload_fileobj_to_r2_storage"
        ) as upload_r2:
            response = FileUploadView.as_view()(request)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(json.loads(response.content)["code"], "student_storage_owner_missing")
        upload_r2.assert_not_called()
        self.assertFalse(
            InventoryFile.objects.filter(
                tenant=self.tenant,
                student_ps="MISSING-STUDENT",
            ).exists()
        )

    def test_inventory_filename_uses_128_bit_random_suffix(self):
        with patch(
            "apps.domains.inventory.r2_path.secrets.token_hex",
            return_value="a" * 32,
        ) as token_hex:
            generated = safe_filename("lesson.pdf")

        token_hex.assert_called_once_with(16)
        self.assertTrue(generated.endswith(f"_{'a' * 32}.pdf"))

    def test_upload_failure_returns_stable_code_without_provider_detail(self):
        upload = SimpleUploadedFile("x.pdf", b"%PDF-1.4", content_type="application/pdf")
        request = self._multipart_request(
            "/storage/inventory/upload/",
            {"scope": "admin", "file": upload},
        )

        with self._auth(self.staff), patch(
            "apps.domains.inventory.views.upload_fileobj_to_r2_storage",
            side_effect=RuntimeError("provider-secret-detail"),
        ), patch(
            "apps.infrastructure.storage.r2.delete_object_r2_storage"
        ):
            response = FileUploadView.as_view()(request)

        payload = json.loads(response.content)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(payload["code"], "inventory_storage_upload_failed")
        self.assertNotIn("provider-secret-detail", payload["detail"])

    def test_uncertain_put_failure_compensates_and_keeps_durable_retry_intent(self):
        upload = SimpleUploadedFile("uncertain.pdf", b"%PDF-1.4", content_type="application/pdf")
        request = self._multipart_request(
            "/storage/inventory/upload/",
            {"scope": "admin", "file": upload},
        )
        stored_keys = set()

        def store_then_raise(*, key, **kwargs):
            stored_keys.add(key)
            raise TimeoutError("provider response lost")

        def exact_delete(*, key):
            stored_keys.discard(key)

        with self._auth(self.staff), patch(
            "apps.domains.inventory.views.upload_fileobj_to_r2_storage",
            side_effect=store_then_raise,
        ), patch(
            "apps.infrastructure.storage.r2.delete_object_r2_storage",
            side_effect=exact_delete,
        ):
            response = FileUploadView.as_view()(request)

        self.assertEqual(response.status_code, 502)
        self.assertEqual(json.loads(response.content)["code"], "inventory_storage_upload_failed")
        self.assertEqual(stored_keys, set())
        intent = SubmissionStorageCleanupIntent.objects.get(tenant=self.tenant)
        self.assertEqual(intent.status, SubmissionStorageCleanupIntent.Status.PENDING)
        self.assertFalse(InventoryFile.objects.filter(tenant=self.tenant).exists())

    def test_uncertain_write_cleanup_waits_for_late_provider_completion(self):
        key = f"tenants/{self.tenant.id}/admin/inventory/late-write.pdf"
        stored_keys: set[str] = set()

        def exact_delete(*, key):
            stored_keys.discard(key)

        with patch(
            "apps.infrastructure.storage.r2.delete_object_r2_storage",
            side_effect=exact_delete,
        ) as delete_r2:
            outcome = compensate_unattached_storage_object(
                tenant_id=self.tenant.id,
                key=key,
                uncertain_write=True,
            )
            self.assertEqual(outcome, "pending")
            self.assertEqual(delete_r2.call_count, 1)

            early = process_pending_storage_cleanup_intents()
            self.assertEqual(early.cleaned, 0)
            self.assertEqual(early.failed, 0)
            self.assertEqual(delete_r2.call_count, 1)

            # The provider finishes after the caller timed out and after the
            # first exact delete saw no object.
            stored_keys.add(key)
            intent = SubmissionStorageCleanupIntent.objects.get(
                tenant=self.tenant,
                object_key=key,
            )
            self.assertEqual(intent.status, SubmissionStorageCleanupIntent.Status.PENDING)
            SubmissionStorageCleanupIntent.objects.filter(pk=intent.pk).update(
                updated_at=(
                    timezone.now()
                    - uncertain_storage_write_settle_delay()
                    - timedelta(seconds=1)
                )
            )

            settled = process_pending_storage_cleanup_intents()

        self.assertEqual(settled.cleaned, 1)
        self.assertEqual(settled.failed, 0)
        self.assertEqual(stored_keys, set())
        intent.refresh_from_db()
        self.assertEqual(intent.status, SubmissionStorageCleanupIntent.Status.CLEANED)

    def test_unexpected_attach_failure_still_compensates_uploaded_object(self):
        upload = SimpleUploadedFile("attach-failure.pdf", b"%PDF-1.4", content_type="application/pdf")
        request = self._multipart_request(
            "/storage/inventory/upload/",
            {"scope": "admin", "file": upload},
        )

        with self._auth(self.staff), patch(
            "apps.domains.inventory.views.upload_fileobj_to_r2_storage"
        ) as upload_r2, patch(
            "apps.domains.inventory.views.inv_repo.inventory_file_aggregate_size",
            side_effect=[0, RuntimeError("attach read failed")],
        ), patch(
            "apps.infrastructure.storage.r2.delete_object_r2_storage"
        ) as delete_r2:
            response = FileUploadView.as_view()(request)

        self.assertEqual(response.status_code, 500)
        self.assertEqual(json.loads(response.content)["code"], "inventory_metadata_save_failed")
        delete_r2.assert_called_once_with(key=upload_r2.call_args.kwargs["key"])

    def test_upload_compensation_preserves_a_canonical_inventory_owner(self):
        owned_key = f"tenants/{self.tenant.id}/admin/inventory/already-owned.pdf"
        owner = InventoryFile.objects.create(
            tenant=self.tenant,
            scope="admin",
            student_ps="",
            folder=None,
            display_name="already-owned.pdf",
            original_name="already-owned.pdf",
            r2_key=owned_key,
            content_type="application/pdf",
        )

        with patch(
            "apps.infrastructure.storage.r2.delete_object_r2_storage"
        ) as delete_r2:
            outcome = compensate_unattached_storage_object(
                tenant_id=self.tenant.id,
                key=owned_key,
                uncertain_write=True,
            )

        self.assertEqual(outcome, "referenced")
        self.assertTrue(InventoryFile.objects.filter(pk=owner.pk).exists())
        self.assertFalse(
            SubmissionStorageCleanupIntent.objects.filter(
                tenant=self.tenant,
                object_key=owned_key,
            ).exists()
        )
        delete_r2.assert_not_called()

    def test_active_replacement_cannot_list_or_download_ambiguous_legacy_inventory(self):
        legacy_file = InventoryFile.objects.create(
            tenant=self.tenant,
            scope="student",
            student_ps=self.student.ps_number,
            display_name="previous-private.pdf",
            r2_key=f"tenants/{self.tenant.id}/students/S001/inventory/previous-private.pdf",
            original_name="previous-private.pdf",
            size_bytes=47,
            content_type="application/pdf",
        )
        InventoryFile.objects.filter(pk=legacy_file.pk).update(
            created_at=timezone.now() - timedelta(minutes=1)
        )
        Student.objects.filter(pk=self.student.pk).update(
            ps_number=f"_del_{self.student.id}_S001",
            deleted_at=timezone.now(),
            parent=None,
        )
        replacement_user = User.objects.create_user(
            username="inv-hard-replacement",
            password="test1234",
            tenant=self.tenant,
        )
        replacement = Student(
            tenant=self.tenant,
            user=replacement_user,
            ps_number="S001",
            omr_code="12345679",
            name="새 학생",
        )
        Student.objects.bulk_create([replacement])
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=replacement_user,
            role="student",
        )
        replacement_parent_user = User.objects.create_user(
            username="inv-hard-replacement-parent",
            password="test1234",
            tenant=self.tenant,
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=replacement_parent_user,
            role="parent",
        )
        replacement_parent = Parent.objects.create(
            tenant=self.tenant,
            user=replacement_parent_user,
            name="새 학부모",
            phone="01077778888",
        )
        Student.objects.filter(pk=replacement.pk).update(parent=replacement_parent)

        student_list = self.factory.get(
            "/storage/inventory/?scope=student&student_ps=S001"
        )
        student_list.tenant = self.tenant
        with self._auth(replacement_user):
            student_response = InventoryListView.as_view()(student_list)

        parent_list = self.factory.get(
            "/storage/inventory/?scope=student&student_ps=S001",
            HTTP_X_STUDENT_ID=str(replacement.pk),
        )
        parent_list.tenant = self.tenant
        with self._auth(replacement_parent_user):
            parent_response = InventoryListView.as_view()(parent_list)

        presign = self._json_request(
            "/storage/inventory/presign/",
            {"file_id": legacy_file.id},
            replacement_user,
        )
        with self._auth(replacement_user):
            presign_response = PresignView.as_view()(presign)

        for response in (student_response, parent_response, presign_response):
            self.assertEqual(response.status_code, 409)
            self.assertEqual(
                json.loads(response.content)["code"],
                "student_storage_namespace_conflict",
            )

        staff_list = self.factory.get(
            "/storage/inventory/?scope=student&student_ps=S001"
        )
        staff_list.tenant = self.tenant
        with self._auth(self.staff):
            staff_response = InventoryListView.as_view()(staff_list)
        self.assertEqual(staff_response.status_code, 200)
        self.assertEqual(len(json.loads(staff_response.content)["files"]), 1)

    def test_safe_replacement_claim_can_use_new_inventory_without_old_file_exposure(self):
        original_ps = self.student.ps_number
        tombstone_ps = f"_del_{self.student.id}_{original_ps}"
        legacy_file = InventoryFile.objects.create(
            tenant=self.tenant,
            scope="student",
            student_ps=original_ps,
            display_name="old-private.pdf",
            r2_key=f"tenants/{self.tenant.id}/students/{original_ps}/inventory/old-private.pdf",
            original_name="old-private.pdf",
            size_bytes=47,
            content_type="application/pdf",
        )
        Student.objects.filter(pk=self.student.pk).update(
            ps_number=tombstone_ps,
            deleted_at=timezone.now(),
            parent=None,
        )
        User.objects.filter(pk=self.student.user_id).update(username=f"deleted-{self.student.id}")
        replacement_user = User.objects.create_user(
            username="safe-replacement",
            password="test1234",
            tenant=self.tenant,
        )
        replacement = Student.objects.create(
            tenant=self.tenant,
            user=replacement_user,
            parent=self.parent,
            ps_number=original_ps,
            omr_code="12345679",
            name="새 학생",
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=replacement_user,
            role="student",
        )
        new_file = InventoryFile.objects.create(
            tenant=self.tenant,
            scope="student",
            student_ps=original_ps,
            display_name="new.pdf",
            r2_key=f"tenants/{self.tenant.id}/students/{original_ps}/inventory/new.pdf",
            original_name="new.pdf",
            size_bytes=48,
            content_type="application/pdf",
        )
        legacy_file.refresh_from_db()
        self.assertEqual(legacy_file.student_ps, tombstone_ps)
        self.assertGreaterEqual(new_file.created_at, replacement.created_at)
        self.assertFalse(
            student_storage_namespace_has_legacy_conflict(
                tenant_id=self.tenant.id,
                student_id=replacement.id,
                ps_number=original_ps,
            ),
            (
                f"replacement={replacement.created_at.isoformat()} "
                f"new_file={new_file.created_at.isoformat()}"
            ),
        )

        student_list = self.factory.get(
            f"/storage/inventory/?scope=student&student_ps={original_ps}"
        )
        student_list.tenant = self.tenant
        with self._auth(replacement_user):
            student_response = InventoryListView.as_view()(student_list)

        parent_list = self.factory.get(
            f"/storage/inventory/?scope=student&student_ps={original_ps}",
            HTTP_X_STUDENT_ID=str(replacement.id),
        )
        parent_list.tenant = self.tenant
        with self._auth(self.parent_user):
            parent_response = InventoryListView.as_view()(parent_list)

        presign = self._json_request(
            "/storage/inventory/presign/",
            {"file_id": new_file.id},
            replacement_user,
        )
        with self._auth(replacement_user), patch(
            "apps.domains.inventory.views.generate_presigned_get_url_storage",
            return_value="https://example.invalid/safe",
        ):
            presign_response = PresignView.as_view()(presign)

        for label, response in (
            ("student-list", student_response),
            ("parent-list", parent_response),
            ("student-presign", presign_response),
        ):
            self.assertEqual(response.status_code, 200, f"{label}: {response.content!r}")
        for response in (student_response, parent_response):
            files = json.loads(response.content)["files"]
            self.assertEqual([item["id"] for item in files], [str(new_file.id)])

    def test_all_plan_can_upload_and_reports_200gb_quota(self):
        upload = SimpleUploadedFile("all-plan.pdf", b"%PDF-1.4", content_type="application/pdf")
        request = self._multipart_request(
            "/storage/inventory/upload/",
            {"scope": "admin", "file": upload},
        )

        with self._auth(self.staff), patch(
            "apps.domains.inventory.views.upload_fileobj_to_r2_storage"
        ) as upload_r2:
            response = FileUploadView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        upload_r2.assert_called_once()
        self.assertTrue(
            InventoryFile.objects.filter(
                tenant=self.tenant,
                original_name="all-plan.pdf",
            ).exists()
        )

        quota_request = self.factory.get("/storage/quota/")
        quota_request.tenant = self.tenant
        with self._auth(self.staff):
            quota_response = QuotaView.as_view()(quota_request)

        self.assertEqual(quota_response.status_code, 200)
        quota = json.loads(quota_response.content)
        self.assertEqual(quota["plan"], "all")
        self.assertEqual(quota["limitBytes"], 200 * 1024**3)

    def test_upload_removes_exact_r2_object_when_metadata_create_fails(self):
        upload = SimpleUploadedFile("metadata-failure.pdf", b"%PDF-1.4", content_type="application/pdf")
        request = self._multipart_request(
            "/storage/inventory/upload/",
            {"scope": "admin", "file": upload},
        )

        with self._auth(self.staff), patch(
            "apps.domains.inventory.views.upload_fileobj_to_r2_storage"
        ) as upload_r2, patch(
            "apps.domains.inventory.views.inv_repo.inventory_file_create",
            side_effect=RuntimeError("database unavailable"),
        ), patch("apps.infrastructure.storage.r2.delete_object_r2_storage") as delete_r2:
            response = FileUploadView.as_view()(request)

        self.assertEqual(response.status_code, 500)
        self.assertEqual(json.loads(response.content)["code"], "inventory_metadata_save_failed")
        upload_r2.assert_called_once()
        uploaded_key = upload_r2.call_args.kwargs["key"]
        delete_r2.assert_called_once_with(key=uploaded_key)
        self.assertFalse(
            InventoryFile.objects.filter(
                tenant=self.tenant,
                original_name="metadata-failure.pdf",
            ).exists()
        )

    def test_upload_fails_closed_when_metadata_and_r2_cleanup_both_fail(self):
        upload = SimpleUploadedFile("cleanup-failure.pdf", b"%PDF-1.4", content_type="application/pdf")
        request = self._multipart_request(
            "/storage/inventory/upload/",
            {"scope": "admin", "file": upload},
        )

        with self._auth(self.staff), patch(
            "apps.domains.inventory.views.upload_fileobj_to_r2_storage"
        ) as upload_r2, patch(
            "apps.domains.inventory.views.inv_repo.inventory_file_create",
            side_effect=RuntimeError("database unavailable"),
        ), patch(
            "apps.infrastructure.storage.r2.delete_object_r2_storage",
            side_effect=RuntimeError("storage unavailable"),
        ):
            response = FileUploadView.as_view()(request)

        self.assertEqual(response.status_code, 502)
        self.assertEqual(json.loads(response.content)["code"], "inventory_storage_cleanup_failed")
        uploaded_key = upload_r2.call_args.kwargs["key"]
        intent = SubmissionStorageCleanupIntent.objects.get(
            tenant=self.tenant,
            bucket=SubmissionStorageCleanupIntent.Bucket.STORAGE,
            object_key=uploaded_key,
        )
        self.assertEqual(intent.status, SubmissionStorageCleanupIntent.Status.PENDING)


class InventoryHardeningMoveTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(code="inv-move-hard", name="Inventory Move Hardening", is_active=True)
        self.source_folder = InventoryFolder.objects.create(
            tenant=self.tenant,
            scope="admin",
            student_ps="",
            name="source",
        )
        self.target_folder = InventoryFolder.objects.create(
            tenant=self.tenant,
            scope="admin",
            student_ps="",
            name="target",
        )

    def _student_move_fixture(self, *, ps_number: str):
        user = User.objects.create_user(
            username=f"move-{ps_number.lower()}",
            password="test1234",
            tenant=self.tenant,
        )
        Student.objects.create(
            tenant=self.tenant,
            user=user,
            ps_number=ps_number,
            name="이동 학생",
            omr_code=f"9{len(ps_number):07d}",
        )
        source_folder = InventoryFolder.objects.create(
            tenant=self.tenant,
            scope="student",
            student_ps=ps_number,
            name="source",
        )
        target_folder = InventoryFolder.objects.create(
            tenant=self.tenant,
            scope="student",
            student_ps=ps_number,
            name="target",
        )
        source_file = InventoryFile.objects.create(
            tenant=self.tenant,
            scope="student",
            student_ps=ps_number,
            folder=source_folder,
            display_name="source.pdf",
            original_name="source.pdf",
            r2_key=(
                f"tenants/{self.tenant.id}/students/{ps_number}/"
                "inventory/source/source.pdf"
            ),
            content_type="application/pdf",
        )
        return source_folder, target_folder, source_file

    def test_student_file_move_stale_after_copy_records_exact_cleanup_intent(self):
        _, target_folder, source_file = self._student_move_fixture(
            ps_number="MOVE-FILE"
        )
        concurrent_key = (
            f"tenants/{self.tenant.id}/students/MOVE-FILE/"
            "inventory/concurrent/source.pdf"
        )

        def copy_then_change_metadata(*, source_key, dest_key):
            del source_key, dest_key
            InventoryFile.objects.filter(pk=source_file.pk).update(
                r2_key=concurrent_key
            )

        with patch(
            "apps.domains.inventory.services.copy_object_r2_storage",
            side_effect=copy_then_change_metadata,
        ) as copy_r2, patch(
            "apps.infrastructure.storage.r2.delete_object_r2_storage",
            side_effect=RuntimeError("storage unavailable"),
        ):
            result = move_file(
                tenant=self.tenant,
                scope="student",
                student_ps="MOVE-FILE",
                source_file_id=source_file.id,
                target_folder_id=target_folder.id,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 409)
        self.assertEqual(result["code"], "inventory_move_conflict")
        copied_key = copy_r2.call_args.kwargs["dest_key"]
        source_file.refresh_from_db()
        self.assertEqual(source_file.r2_key, concurrent_key)
        intent = SubmissionStorageCleanupIntent.objects.get(
            tenant=self.tenant,
            bucket=SubmissionStorageCleanupIntent.Bucket.STORAGE,
            object_key=copied_key,
        )
        self.assertEqual(intent.status, SubmissionStorageCleanupIntent.Status.PENDING)

    def test_student_folder_move_revalidates_topology_and_original_keys_after_copy(self):
        source_folder, target_folder, source_file = self._student_move_fixture(
            ps_number="MOVE-FOLDER"
        )
        concurrent_key = (
            f"tenants/{self.tenant.id}/students/MOVE-FOLDER/"
            "inventory/concurrent/source.pdf"
        )

        def copy_then_change_metadata(*, source_key, dest_key):
            del source_key, dest_key
            InventoryFile.objects.filter(pk=source_file.pk).update(
                r2_key=concurrent_key
            )

        with patch(
            "apps.domains.inventory.services.copy_object_r2_storage",
            side_effect=copy_then_change_metadata,
        ) as copy_r2, patch(
            "apps.infrastructure.storage.r2.delete_object_r2_storage",
            side_effect=RuntimeError("storage unavailable"),
        ):
            result = move_folder(
                tenant=self.tenant,
                scope="student",
                student_ps="MOVE-FOLDER",
                source_folder_id=source_folder.id,
                target_folder_id=target_folder.id,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 409)
        self.assertEqual(result["code"], "inventory_move_conflict")
        copied_key = copy_r2.call_args.kwargs["dest_key"]
        source_folder.refresh_from_db()
        source_file.refresh_from_db()
        self.assertIsNone(source_folder.parent_id)
        self.assertEqual(source_file.r2_key, concurrent_key)
        intent = SubmissionStorageCleanupIntent.objects.get(
            tenant=self.tenant,
            bucket=SubmissionStorageCleanupIntent.Bucket.STORAGE,
            object_key=copied_key,
        )
        self.assertEqual(intent.status, SubmissionStorageCleanupIntent.Status.PENDING)

    def test_folder_move_uses_fresh_key_when_sanitized_paths_collide(self):
        left_root = InventoryFolder.objects.create(
            tenant=self.tenant,
            scope="admin",
            name="left?",
        )
        right_root = InventoryFolder.objects.create(
            tenant=self.tenant,
            scope="admin",
            name="left*",
        )
        child = InventoryFolder.objects.create(
            tenant=self.tenant,
            scope="admin",
            parent=left_root,
            name="child",
        )
        source_file = InventoryFile.objects.create(
            tenant=self.tenant,
            scope="admin",
            folder=child,
            display_name="same.pdf",
            original_name="same.pdf",
            r2_key=(
                f"tenants/{self.tenant.id}/admin/inventory/"
                "left_/child/same-token.pdf"
            ),
            content_type="application/pdf",
        )

        with patch(
            "apps.domains.inventory.services.copy_object_r2_storage"
        ) as copy_r2, patch(
            "apps.infrastructure.storage.r2.delete_object_r2_storage"
        ) as delete_r2:
            result = move_folder(
                tenant=self.tenant,
                scope="admin",
                student_ps="",
                source_folder_id=child.id,
                target_folder_id=right_root.id,
            )

        self.assertTrue(result["ok"], result)
        fresh_key = copy_r2.call_args.kwargs["dest_key"]
        self.assertNotEqual(fresh_key, source_file.r2_key)
        delete_r2.assert_called_once_with(key=source_file.r2_key)
        child.refresh_from_db()
        source_file.refresh_from_db()
        self.assertEqual(child.parent_id, right_root.id)
        self.assertEqual(source_file.r2_key, fresh_key)

    def test_file_move_commits_and_records_cleanup_when_old_key_delete_fails(self):
        source_file = InventoryFile.objects.create(
            tenant=self.tenant,
            scope="admin",
            folder=self.source_folder,
            display_name="cleanup.pdf",
            original_name="cleanup.pdf",
            r2_key=(
                f"tenants/{self.tenant.id}/admin/inventory/"
                "source/cleanup-token.pdf"
            ),
            content_type="application/pdf",
        )

        with patch(
            "apps.domains.inventory.services.copy_object_r2_storage"
        ), patch(
            "apps.infrastructure.storage.r2.delete_object_r2_storage",
            side_effect=RuntimeError("storage unavailable"),
        ):
            result = move_file(
                tenant=self.tenant,
                scope="admin",
                student_ps="",
                source_file_id=source_file.id,
                target_folder_id=self.target_folder.id,
            )

        self.assertTrue(result["ok"], result)
        source_file.refresh_from_db()
        self.assertEqual(source_file.folder_id, self.target_folder.id)
        self.assertNotEqual(
            source_file.r2_key,
            f"tenants/{self.tenant.id}/admin/inventory/source/cleanup-token.pdf",
        )
        intent = SubmissionStorageCleanupIntent.objects.get(
            tenant=self.tenant,
            bucket=SubmissionStorageCleanupIntent.Bucket.STORAGE,
            object_key=(
                f"tenants/{self.tenant.id}/admin/inventory/"
                "source/cleanup-token.pdf"
            ),
        )
        self.assertEqual(intent.status, SubmissionStorageCleanupIntent.Status.PENDING)

    def test_folder_move_rechecks_current_ancestry_before_db_handoff(self):
        source_file = InventoryFile.objects.create(
            tenant=self.tenant,
            scope="admin",
            folder=self.source_folder,
            display_name="cycle.pdf",
            original_name="cycle.pdf",
            r2_key=(
                f"tenants/{self.tenant.id}/admin/inventory/"
                "source/cycle-token.pdf"
            ),
            content_type="application/pdf",
        )

        def copy_then_make_target_descendant(*, source_key, dest_key):
            del source_key, dest_key
            InventoryFolder.objects.filter(pk=self.target_folder.pk).update(
                parent=self.source_folder
            )

        with patch(
            "apps.domains.inventory.services._inventory_namespace_snapshot",
            return_value=(tuple(), tuple()),
        ), patch(
            "apps.domains.inventory.services.copy_object_r2_storage",
            side_effect=copy_then_make_target_descendant,
        ) as copy_r2, patch(
            "apps.infrastructure.storage.r2.delete_object_r2_storage"
        ) as delete_r2:
            result = move_folder(
                tenant=self.tenant,
                scope="admin",
                student_ps="",
                source_folder_id=self.source_folder.id,
                target_folder_id=self.target_folder.id,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 409)
        self.assertEqual(result["code"], "inventory_move_conflict")
        copied_key = copy_r2.call_args.kwargs["dest_key"]
        delete_r2.assert_called_once_with(key=copied_key)
        self.source_folder.refresh_from_db()
        self.target_folder.refresh_from_db()
        source_file.refresh_from_db()
        self.assertIsNone(self.source_folder.parent_id)
        self.assertEqual(self.target_folder.parent_id, self.source_folder.id)
        self.assertNotEqual(source_file.r2_key, copied_key)

    def _attach_owner_pinned_matchup_document(self, inv_file: InventoryFile):
        from apps.domains.matchup.models import MatchupDocument, MatchupProblem

        doc = MatchupDocument.objects.create(
            tenant=self.tenant,
            inventory_file=inv_file,
            title=inv_file.display_name,
            r2_key=inv_file.r2_key,
            original_name=inv_file.original_name,
            content_type=inv_file.content_type,
        )
        MatchupProblem.objects.create(
            tenant=self.tenant,
            document=doc,
            number=1,
            text="pinned",
            meta={"manual_owner_pinned": True},
        )
        return doc

    def test_recursive_folder_delete_rejects_owner_pinned_matchup_document(self):
        inv_file = InventoryFile.objects.create(
            tenant=self.tenant,
            scope="admin",
            student_ps="",
            folder=self.source_folder,
            display_name="matchup.pdf",
            original_name="matchup.pdf",
            r2_key=f"tenants/{self.tenant.id}/admin/inventory/source/matchup.pdf",
            content_type="application/pdf",
        )
        self._attach_owner_pinned_matchup_document(inv_file)

        with patch("apps.domains.matchup.services.cleanup_matchup_problem_images") as cleanup:
            result = delete_folder_recursive(
                tenant=self.tenant,
                folder=self.source_folder,
                scope="admin",
                student_ps="",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 409)
        cleanup.assert_not_called()
        self.assertTrue(InventoryFolder.objects.filter(id=self.source_folder.id).exists())
        self.assertTrue(InventoryFile.objects.filter(id=inv_file.id).exists())

    def test_file_overwrite_rejects_owner_pinned_matchup_destination(self):
        source = InventoryFile.objects.create(
            tenant=self.tenant,
            scope="admin",
            student_ps="",
            folder=self.source_folder,
            display_name="same.pdf",
            original_name="same.pdf",
            r2_key=f"tenants/{self.tenant.id}/admin/inventory/source/same.pdf",
            content_type="application/pdf",
        )
        existing = InventoryFile.objects.create(
            tenant=self.tenant,
            scope="admin",
            student_ps="",
            folder=self.target_folder,
            display_name="same.pdf",
            original_name="same.pdf",
            r2_key=f"tenants/{self.tenant.id}/admin/inventory/target/same.pdf",
            content_type="application/pdf",
        )
        self._attach_owner_pinned_matchup_document(existing)

        with patch("apps.domains.inventory.services.copy_object_r2_storage") as copy_r2, patch(
            "apps.domains.inventory.services.delete_object_r2_storage"
        ) as delete_r2:
            result = move_file(
                tenant=self.tenant,
                scope="admin",
                student_ps="",
                source_file_id=source.id,
                target_folder_id=self.target_folder.id,
                on_duplicate="overwrite",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 409)
        self.assertEqual(result["code"], "protected_matchup_document")
        copy_r2.assert_not_called()
        delete_r2.assert_not_called()
        self.assertTrue(InventoryFile.objects.filter(id=source.id).exists())
        self.assertTrue(InventoryFile.objects.filter(id=existing.id).exists())

    def test_file_overwrite_rechecks_matchup_protection_after_copy(self):
        source = InventoryFile.objects.create(
            tenant=self.tenant,
            scope="admin",
            folder=self.source_folder,
            display_name="late-protected.pdf",
            original_name="late-protected.pdf",
            r2_key=f"tenants/{self.tenant.id}/admin/inventory/source/late-protected.pdf",
            content_type="application/pdf",
        )
        existing = InventoryFile.objects.create(
            tenant=self.tenant,
            scope="admin",
            folder=self.target_folder,
            display_name="late-protected.pdf",
            original_name="late-protected.pdf",
            r2_key=f"tenants/{self.tenant.id}/admin/inventory/target/late-protected.pdf",
            content_type="application/pdf",
        )

        def copy_then_protect(*, source_key, dest_key):
            del source_key, dest_key
            self._attach_owner_pinned_matchup_document(existing)

        with patch(
            "apps.domains.inventory.services.copy_object_r2_storage",
            side_effect=copy_then_protect,
        ) as copy_r2, patch(
            "apps.infrastructure.storage.r2.delete_object_r2_storage"
        ) as delete_r2:
            result = move_file(
                tenant=self.tenant,
                scope="admin",
                student_ps="",
                source_file_id=source.id,
                target_folder_id=self.target_folder.id,
                on_duplicate="overwrite",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "protected_matchup_document")
        source.refresh_from_db()
        existing.refresh_from_db()
        self.assertEqual(source.folder_id, self.source_folder.id)
        self.assertEqual(existing.folder_id, self.target_folder.id)
        copied_key = copy_r2.call_args.kwargs["dest_key"]
        delete_r2.assert_called_once_with(key=copied_key)

    def test_file_move_rechecks_source_reported_score_after_copy(self):
        source_folder, target_folder, source_file = self._student_move_fixture(
            ps_number="LATE-SOURCE"
        )
        student = Student.objects.get(
            tenant=self.tenant,
            ps_number="LATE-SOURCE",
        )

        def copy_then_protect(*, source_key, dest_key):
            del source_key, dest_key
            StudentReportedScore.objects.create(
                tenant=self.tenant,
                student=student,
                evidence_file=source_file,
                source="school_exam",
                academic_year=2026,
                semester=1,
                exam_round="first",
                subject="수학",
                score="92.00",
                max_score="100.00",
            )

        with patch(
            "apps.domains.inventory.services.copy_object_r2_storage",
            side_effect=copy_then_protect,
        ) as copy_r2, patch(
            "apps.infrastructure.storage.r2.delete_object_r2_storage"
        ) as delete_r2:
            result = move_file(
                tenant=self.tenant,
                scope="student",
                student_ps="LATE-SOURCE",
                source_file_id=source_file.id,
                target_folder_id=target_folder.id,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "reported_score_evidence_protected")
        source_file.refresh_from_db()
        self.assertEqual(source_file.folder_id, source_folder.id)
        copied_key = copy_r2.call_args.kwargs["dest_key"]
        delete_r2.assert_called_once_with(key=copied_key)

    def test_file_overwrite_uses_fresh_key_then_cleans_replaced_objects(self):
        source = InventoryFile.objects.create(
            tenant=self.tenant,
            scope="admin",
            student_ps="",
            folder=self.source_folder,
            display_name="same.pdf",
            original_name="same.pdf",
            r2_key=f"tenants/{self.tenant.id}/admin/inventory/source/same.pdf",
            content_type="application/pdf",
        )
        existing = InventoryFile.objects.create(
            tenant=self.tenant,
            scope="admin",
            student_ps="",
            folder=self.target_folder,
            display_name="same.pdf",
            original_name="same.pdf",
            r2_key=f"tenants/{self.tenant.id}/admin/inventory/target/same.pdf",
            content_type="application/pdf",
        )

        with patch("apps.domains.inventory.services.copy_object_r2_storage") as copy_r2, patch(
            "apps.infrastructure.storage.r2.delete_object_r2_storage"
        ) as delete_r2:
            result = move_file(
                tenant=self.tenant,
                scope="admin",
                student_ps="",
                source_file_id=source.id,
                target_folder_id=self.target_folder.id,
                on_duplicate="overwrite",
            )

        self.assertTrue(result["ok"])
        copy_r2.assert_called_once()
        self.assertEqual(copy_r2.call_args.kwargs["source_key"], source.r2_key)
        fresh_key = copy_r2.call_args.kwargs["dest_key"]
        self.assertNotEqual(fresh_key, existing.r2_key)
        delete_keys = [call.kwargs["key"] for call in delete_r2.call_args_list]
        self.assertIn(source.r2_key, delete_keys)
        self.assertIn(existing.r2_key, delete_keys)
        source.refresh_from_db()
        self.assertEqual(source.folder_id, self.target_folder.id)
        self.assertEqual(source.r2_key, fresh_key)
        self.assertFalse(InventoryFile.objects.filter(id=existing.id).exists())

    def test_file_overwrite_uncertain_copy_preserves_canonical_destination(self):
        source = InventoryFile.objects.create(
            tenant=self.tenant,
            scope="admin",
            student_ps="",
            folder=self.source_folder,
            display_name="same.pdf",
            original_name="same.pdf",
            r2_key=f"tenants/{self.tenant.id}/admin/inventory/source/same.pdf",
            content_type="application/pdf",
        )
        existing = InventoryFile.objects.create(
            tenant=self.tenant,
            scope="admin",
            student_ps="",
            folder=self.target_folder,
            display_name="same.pdf",
            original_name="same.pdf",
            r2_key=f"tenants/{self.tenant.id}/admin/inventory/target/same.pdf",
            content_type="application/pdf",
        )

        with patch(
            "apps.domains.inventory.services.copy_object_r2_storage",
            side_effect=TimeoutError("ambiguous provider timeout"),
        ) as copy_r2, patch(
            "apps.infrastructure.storage.r2.delete_object_r2_storage"
        ) as delete_r2:
            result = move_file(
                tenant=self.tenant,
                scope="admin",
                student_ps="",
                source_file_id=source.id,
                target_folder_id=self.target_folder.id,
                on_duplicate="overwrite",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 502)
        fresh_key = copy_r2.call_args.kwargs["dest_key"]
        self.assertNotEqual(fresh_key, existing.r2_key)
        delete_r2.assert_called_once_with(key=fresh_key)
        source.refresh_from_db()
        existing.refresh_from_db()
        self.assertEqual(source.folder_id, self.source_folder.id)
        self.assertEqual(existing.folder_id, self.target_folder.id)
        intent = SubmissionStorageCleanupIntent.objects.get(
            tenant=self.tenant,
            bucket=SubmissionStorageCleanupIntent.Bucket.STORAGE,
            object_key=fresh_key,
        )
        self.assertEqual(intent.status, SubmissionStorageCleanupIntent.Status.PENDING)

    def test_folder_overwrite_rejects_owner_pinned_matchup_destination(self):
        InventoryFile.objects.create(
            tenant=self.tenant,
            scope="admin",
            student_ps="",
            folder=self.source_folder,
            display_name="child.pdf",
            original_name="child.pdf",
            r2_key=f"tenants/{self.tenant.id}/admin/inventory/source/child.pdf",
            content_type="application/pdf",
        )
        overwrite_folder = InventoryFolder.objects.create(
            tenant=self.tenant,
            scope="admin",
            student_ps="",
            parent=self.target_folder,
            name=self.source_folder.name,
        )
        existing_child = InventoryFile.objects.create(
            tenant=self.tenant,
            scope="admin",
            student_ps="",
            folder=overwrite_folder,
            display_name="child.pdf",
            original_name="child.pdf",
            r2_key=f"tenants/{self.tenant.id}/admin/inventory/target/source/child.pdf",
            content_type="application/pdf",
        )
        self._attach_owner_pinned_matchup_document(existing_child)

        with patch("apps.domains.inventory.services.copy_object_r2_storage") as copy_r2, patch(
            "apps.domains.inventory.services.delete_object_r2_storage"
        ) as delete_r2:
            result = move_folder(
                tenant=self.tenant,
                scope="admin",
                student_ps="",
                source_folder_id=self.source_folder.id,
                target_folder_id=self.target_folder.id,
                on_duplicate="overwrite",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 409)
        self.assertEqual(result["code"], "protected_matchup_document")
        copy_r2.assert_not_called()
        delete_r2.assert_not_called()
        self.assertTrue(InventoryFolder.objects.filter(id=self.source_folder.id).exists())
        self.assertTrue(InventoryFolder.objects.filter(id=overwrite_folder.id).exists())
        self.assertTrue(InventoryFile.objects.filter(id=existing_child.id).exists())

    def test_folder_overwrite_rechecks_reported_score_after_copy(self):
        source_folder, target_folder, source_file = self._student_move_fixture(
            ps_number="LATE-SCORE"
        )
        overwrite_folder = InventoryFolder.objects.create(
            tenant=self.tenant,
            scope="student",
            student_ps="LATE-SCORE",
            parent=target_folder,
            name=source_folder.name,
        )
        existing_child = InventoryFile.objects.create(
            tenant=self.tenant,
            scope="student",
            student_ps="LATE-SCORE",
            folder=overwrite_folder,
            display_name="existing.pdf",
            original_name="existing.pdf",
            r2_key=(
                f"tenants/{self.tenant.id}/students/LATE-SCORE/"
                "inventory/target/source/existing.pdf"
            ),
            content_type="application/pdf",
        )
        student = Student.objects.get(
            tenant=self.tenant,
            ps_number="LATE-SCORE",
        )

        def copy_then_protect(*, source_key, dest_key):
            del source_key, dest_key
            StudentReportedScore.objects.create(
                tenant=self.tenant,
                student=student,
                evidence_file=existing_child,
                source="school_exam",
                academic_year=2026,
                semester=1,
                exam_round="first",
                subject="수학",
                score="90.00",
                max_score="100.00",
            )

        with patch(
            "apps.domains.inventory.services.copy_object_r2_storage",
            side_effect=copy_then_protect,
        ) as copy_r2, patch(
            "apps.infrastructure.storage.r2.delete_object_r2_storage"
        ) as delete_r2:
            result = move_folder(
                tenant=self.tenant,
                scope="student",
                student_ps="LATE-SCORE",
                source_folder_id=source_folder.id,
                target_folder_id=target_folder.id,
                on_duplicate="overwrite",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "reported_score_evidence_protected")
        source_folder.refresh_from_db()
        source_file.refresh_from_db()
        self.assertIsNone(source_folder.parent_id)
        self.assertEqual(source_file.folder_id, source_folder.id)
        self.assertTrue(InventoryFolder.objects.filter(pk=overwrite_folder.pk).exists())
        self.assertTrue(InventoryFile.objects.filter(pk=existing_child.pk).exists())
        copied_key = copy_r2.call_args.kwargs["dest_key"]
        delete_r2.assert_called_once_with(key=copied_key)

    def test_folder_overwrite_uncertain_copy_preserves_canonical_destination(self):
        source_child = InventoryFile.objects.create(
            tenant=self.tenant,
            scope="admin",
            student_ps="",
            folder=self.source_folder,
            display_name="child.pdf",
            original_name="child.pdf",
            r2_key=f"tenants/{self.tenant.id}/admin/inventory/source/child.pdf",
            content_type="application/pdf",
        )
        overwrite_folder = InventoryFolder.objects.create(
            tenant=self.tenant,
            scope="admin",
            student_ps="",
            parent=self.target_folder,
            name=self.source_folder.name,
        )
        existing_child = InventoryFile.objects.create(
            tenant=self.tenant,
            scope="admin",
            student_ps="",
            folder=overwrite_folder,
            display_name="child.pdf",
            original_name="child.pdf",
            r2_key=f"tenants/{self.tenant.id}/admin/inventory/target/source/child.pdf",
            content_type="application/pdf",
        )

        def copy_side_effect(*, source_key, dest_key):
            if source_key == source_child.r2_key:
                raise RuntimeError("copy failed")

        with patch("apps.domains.inventory.services.copy_object_r2_storage", side_effect=copy_side_effect) as copy_r2, patch(
            "apps.infrastructure.storage.r2.delete_object_r2_storage"
        ) as delete_r2:
            result = move_folder(
                tenant=self.tenant,
                scope="admin",
                student_ps="",
                source_folder_id=self.source_folder.id,
                target_folder_id=self.target_folder.id,
                on_duplicate="overwrite",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 502)
        self.assertTrue(InventoryFolder.objects.filter(id=overwrite_folder.id).exists())
        self.assertTrue(InventoryFile.objects.filter(id=existing_child.id).exists())
        self.assertTrue(InventoryFile.objects.filter(id=source_child.id).exists())
        copy_r2.assert_called_once()
        fresh_key = copy_r2.call_args.kwargs["dest_key"]
        self.assertNotEqual(fresh_key, existing_child.r2_key)
        delete_r2.assert_called_once_with(key=fresh_key)
        intent = SubmissionStorageCleanupIntent.objects.get(
            tenant=self.tenant,
            bucket=SubmissionStorageCleanupIntent.Bucket.STORAGE,
            object_key=fresh_key,
        )
        self.assertEqual(intent.status, SubmissionStorageCleanupIntent.Status.PENDING)

    def test_folder_overwrite_duplicate_detection_is_scope_limited(self):
        source_folder = InventoryFolder.objects.create(
            tenant=self.tenant,
            scope="admin",
            student_ps="",
            parent=self.target_folder,
            name="shared",
        )
        student_folder = InventoryFolder.objects.create(
            tenant=self.tenant,
            scope="student",
            student_ps="S001",
            parent=None,
            name="shared",
        )

        with patch("apps.domains.inventory.services.copy_object_r2_storage"), patch(
            "apps.domains.inventory.services.delete_object_r2_storage"
        ):
            result = move_folder(
                tenant=self.tenant,
                scope="admin",
                student_ps="",
                source_folder_id=source_folder.id,
                target_folder_id=None,
                on_duplicate="overwrite",
            )

        self.assertTrue(result["ok"])
        self.assertTrue(InventoryFolder.objects.filter(id=student_folder.id).exists())
        source_folder.refresh_from_db()
        self.assertIsNone(source_folder.parent_id)
