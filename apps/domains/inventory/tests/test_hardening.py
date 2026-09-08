from __future__ import annotations

import json
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIRequestFactory

from apps.core.models import Tenant, TenantMembership
from apps.domains.inventory.models import InventoryFile, InventoryFolder
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


User = get_user_model()
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
        self.assertEqual(deleted_file.status_code, 204, deleted_file.content)
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
        self.assertEqual(deleted_folder.status_code, 204, deleted_folder.content)
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
        ), patch("apps.domains.inventory.views.delete_object_r2_storage") as delete_r2:
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
        ), patch(
            "apps.domains.inventory.views.inv_repo.inventory_file_create",
            side_effect=RuntimeError("database unavailable"),
        ), patch(
            "apps.domains.inventory.views.delete_object_r2_storage",
            side_effect=RuntimeError("storage unavailable"),
        ):
            response = FileUploadView.as_view()(request)

        self.assertEqual(response.status_code, 502)
        self.assertEqual(json.loads(response.content)["code"], "inventory_storage_cleanup_failed")


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

    def test_file_overwrite_backs_up_destination_before_copy_and_deletes_after_db_success(self):
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

        self.assertTrue(result["ok"])
        copy_sources = [call.kwargs["source_key"] for call in copy_r2.call_args_list]
        self.assertEqual(copy_sources[0], existing.r2_key)
        self.assertEqual(copy_sources[1], source.r2_key)
        delete_keys = [call.kwargs["key"] for call in delete_r2.call_args_list]
        self.assertIn(source.r2_key, delete_keys)
        self.assertNotIn(existing.r2_key, delete_keys)
        source.refresh_from_db()
        self.assertEqual(source.folder_id, self.target_folder.id)
        self.assertEqual(source.r2_key, existing.r2_key)
        self.assertFalse(InventoryFile.objects.filter(id=existing.id).exists())

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

    def test_folder_overwrite_copy_failure_restores_destination_and_keeps_db_rows(self):
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
        self.assertEqual(result["status"], 502)
        self.assertTrue(InventoryFolder.objects.filter(id=overwrite_folder.id).exists())
        self.assertTrue(InventoryFile.objects.filter(id=existing_child.id).exists())
        self.assertTrue(InventoryFile.objects.filter(id=source_child.id).exists())
        copy_sources = [call.kwargs["source_key"] for call in copy_r2.call_args_list]
        self.assertIn(existing_child.r2_key, copy_sources)
        delete_r2.assert_called()

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
