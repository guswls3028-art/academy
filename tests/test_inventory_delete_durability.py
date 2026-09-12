from contextlib import ExitStack
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIRequestFactory

from apps.core.models import Tenant, TenantMembership
from apps.domains.inventory.models import InventoryFile, InventoryFolder
from apps.domains.inventory.views import FileDeleteView, FolderDeleteView, InventoryListView
from apps.domains.submissions.models import SubmissionStorageCleanupIntent
from apps.domains.submissions.services.lifecycle import process_submission_storage_cleanup_intents


class InventoryDeleteFixtures:
    def setUp(self):
        self.tenant = Tenant.objects.create(code="inventory-delete-durable", name="Inventory deletion")
        self.user = get_user_model().objects.create_user(
            username="inventory-delete-durable", tenant=self.tenant, password="test-only",
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=self.user, role="teacher")
        self.factory = APIRequestFactory()
        self.deleted_keys = []
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch(
            "apps.domains.inventory.views.JWTAuthentication.authenticate",
            return_value=(self.user, None),
        ))
        for target in (
            "apps.domains.inventory.views.delete_object_r2_storage",
            "apps.domains.inventory.services.delete_object_r2_storage",
            "apps.domains.matchup.services.delete_object_r2_storage",
            "apps.infrastructure.storage.r2.delete_object_r2_storage",
        ):
            self.stack.enter_context(patch(target, side_effect=self._delete_object))

    def _delete_object(self, *, key):
        self.deleted_keys.append(key)

    def _file(self, name="file.pdf", *, folder=None):
        return InventoryFile.objects.create(
            tenant=self.tenant, scope="admin", folder=folder,
            display_name=name, original_name=name,
            r2_key=f"tenants/{self.tenant.id}/admin/inventory/{name}",
        )

    def _delete_file(self, file_id):
        request = self.factory.delete(f"/storage/inventory/files/{file_id}/?scope=admin")
        request.tenant = self.tenant
        return FileDeleteView.as_view()(request, file_id=file_id)

    def _delete_folder(self, folder_id):
        request = self.factory.delete(
            f"/storage/inventory/folders/{folder_id}/?scope=admin&recursive=true",
        )
        request.tenant = self.tenant
        return FolderDeleteView.as_view()(request, folder_id=folder_id)

    def _listed_files(self):
        request = self.factory.get("/storage/inventory/?scope=admin")
        request.tenant = self.tenant
        response = InventoryListView.as_view()(request)
        self.assertEqual(response.status_code, 200)
        return json.loads(response.content)["files"]


class InventoryDeleteDurabilityTests(InventoryDeleteFixtures, TestCase):
    def test_file_outer_rollback_keeps_metadata_and_calls_no_provider(self):
        file = self._file()
        with self.captureOnCommitCallbacks(execute=True):
            with self.assertRaisesMessage(RuntimeError, "outer rollback"):
                with transaction.atomic():
                    self._delete_file(file.id)
                    raise RuntimeError("outer rollback")
        self.assertEqual(self.deleted_keys, [])
        self.assertTrue(InventoryFile.objects.filter(pk=file.id).exists())
        self.assertFalse(SubmissionStorageCleanupIntent.objects.exists())

    def test_file_db_delete_failure_calls_no_provider_and_keeps_original(self):
        file = self._file()
        with patch.object(InventoryFile, "delete", side_effect=RuntimeError("db delete failed")):
            with self.assertRaisesMessage(RuntimeError, "db delete failed"):
                self._delete_file(file.id)
        self.assertEqual(self.deleted_keys, [])
        self.assertTrue(InventoryFile.objects.filter(pk=file.id).exists())
        self.assertFalse(SubmissionStorageCleanupIntent.objects.exists())

    def test_file_records_intent_with_db_transition_and_deletes_only_after_commit(self):
        file = self._file()
        key = file.r2_key
        with self.captureOnCommitCallbacks(execute=True):
            with transaction.atomic():
                response = self._delete_file(file.id)
                self.assertEqual(response.status_code, 204, response.content)
                self.assertEqual(response.content, b"")
                self.assertEqual(self.deleted_keys, [])
                self.assertFalse(InventoryFile.objects.filter(pk=file.id).exists())
                intent = SubmissionStorageCleanupIntent.objects.get(object_key=key)
                self.assertEqual(intent.tenant_id, self.tenant.id)
                self.assertEqual(intent.bucket, "storage")
                self.assertEqual(intent.status, "pending")
        self.assertEqual(self.deleted_keys, [key])
        intent.refresh_from_db()
        self.assertEqual(intent.status, "cleaned")
        self.assertEqual(self._listed_files(), [])

    def test_recursive_folder_outer_rollback_preserves_originals_and_no_provider(self):
        folder = InventoryFolder.objects.create(tenant=self.tenant, scope="admin", name="folder")
        file = self._file(folder=folder)
        with self.captureOnCommitCallbacks(execute=True):
            with self.assertRaisesMessage(RuntimeError, "outer rollback"):
                with transaction.atomic():
                    self._delete_folder(folder.id)
                    raise RuntimeError("outer rollback")
        self.assertEqual(self.deleted_keys, [])
        self.assertTrue(InventoryFolder.objects.filter(pk=folder.id).exists())
        self.assertTrue(InventoryFile.objects.filter(pk=file.id).exists())
        self.assertFalse(SubmissionStorageCleanupIntent.objects.exists())

    def test_recursive_partial_provider_failure_has_retryable_intent_and_converges(self):
        folder = InventoryFolder.objects.create(tenant=self.tenant, scope="admin", name="folder")
        first, second = self._file("first.pdf", folder=folder), self._file("second.pdf", folder=folder)
        with patch(
            "apps.infrastructure.storage.r2.delete_object_r2_storage",
            side_effect=[None, RuntimeError("provider unavailable")],
        ), self.captureOnCommitCallbacks(execute=True):
            response = self._delete_folder(folder.id)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(InventoryFolder.objects.filter(pk=folder.id).exists())
        intents = SubmissionStorageCleanupIntent.objects.filter(
            tenant=self.tenant, object_key__in=[first.r2_key, second.r2_key],
        )
        self.assertEqual(intents.count(), 2)
        self.assertEqual(intents.filter(status="cleaned").count(), 1)
        self.assertEqual(intents.filter(status="failed", last_error="storage_delete_failed").count(), 1)
        with patch("apps.infrastructure.storage.r2.delete_object_r2_storage") as retry:
            result = process_submission_storage_cleanup_intents(intent_ids=list(intents.values_list("id", flat=True)))
        self.assertEqual(result.cleaned, 1)
        retry.assert_called_once_with(key=second.r2_key)
        self.assertEqual(intents.filter(status="cleaned").count(), 2)
        self.assertEqual(self._listed_files(), [])

    def test_owner_attached_before_cleanup_keeps_exact_object(self):
        from apps.domains.matchup.models import MatchupProblem

        file = self._file()
        key = file.r2_key
        with self.captureOnCommitCallbacks(execute=True):
            self._delete_file(file.id)
            MatchupProblem.objects.create(
                tenant=self.tenant, number=1, text="retained owner", image_key=key,
            )
        self.assertEqual(self.deleted_keys, [])
        intent = SubmissionStorageCleanupIntent.objects.get(object_key=key)
        self.assertEqual(intent.status, "pending")
        self.assertEqual(intent.last_error, "object_key_still_referenced")

    def test_intent_record_failure_rolls_back_metadata_without_provider(self):
        file = self._file()
        with patch.object(SubmissionStorageCleanupIntent, "save", side_effect=RuntimeError("intent save failed")):
            with self.assertRaisesMessage(RuntimeError, "intent save failed"):
                self._delete_file(file.id)
        self.assertTrue(InventoryFile.objects.filter(pk=file.id).exists())
        self.assertFalse(SubmissionStorageCleanupIntent.objects.exists())
        self.assertEqual(self.deleted_keys, [])

    def test_foreign_object_namespace_is_rejected_before_any_delete(self):
        file = self._file()
        file.r2_key = "tenants/999999/admin/inventory/foreign.pdf"
        file.save(update_fields=["r2_key"])
        response = self._delete_file(file.id)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(json.loads(response.content)["code"], "inventory_delete_scope_invalid")
        self.assertTrue(InventoryFile.objects.filter(pk=file.id).exists())
        self.assertFalse(SubmissionStorageCleanupIntent.objects.exists())
        self.assertEqual(self.deleted_keys, [])

    def test_exact_owned_legacy_key_remains_deletable_after_commit(self):
        file = self._file()
        file.r2_key = "legacy/inventory/original.pdf"
        file.save(update_fields=["r2_key"])
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self._delete_file(file.id).status_code, 204)
            self.assertEqual(self.deleted_keys, [])
        self.assertEqual(self.deleted_keys, [file.r2_key])

    def test_foreign_surviving_owner_protects_exact_legacy_key(self):
        from apps.domains.matchup.models import MatchupProblem

        file = self._file()
        file.r2_key = "legacy/shared/original.pdf"
        file.save(update_fields=["r2_key"])
        foreign = Tenant.objects.create(code="inventory-delete-foreign", name="Foreign")
        owner = MatchupProblem.objects.create(tenant=foreign, number=1, image_key=file.r2_key)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self._delete_file(file.id).status_code, 204)
        self.assertEqual(self.deleted_keys, [])
        self.assertTrue(MatchupProblem.objects.filter(pk=owner.id).exists())
        self.assertFalse(SubmissionStorageCleanupIntent.objects.exists())

    def test_cross_tenant_cascade_is_rejected_before_any_delete(self):
        folder = InventoryFolder.objects.create(tenant=self.tenant, scope="admin", name="folder")
        foreign = Tenant.objects.create(code="inventory-delete-foreign", name="Foreign")
        file = InventoryFile.objects.create(
            tenant=foreign, scope="admin", folder=folder, display_name="foreign.pdf",
            r2_key=f"tenants/{foreign.id}/admin/inventory/foreign.pdf",
        )
        response = self._delete_folder(folder.id)
        self.assertEqual(response.status_code, 409)
        self.assertTrue(InventoryFile.objects.filter(pk=file.id).exists())
        self.assertTrue(InventoryFolder.objects.filter(pk=folder.id).exists())
        self.assertFalse(SubmissionStorageCleanupIntent.objects.exists())
        self.assertEqual(self.deleted_keys, [])

    def test_matchup_cascade_records_all_exact_image_keys_after_commit(self):
        from apps.domains.matchup.models import MatchupDocument, MatchupProblem, ProblemSegmentationProposal

        file = self._file()
        prefix = f"tenants/{self.tenant.id}/matchup/"
        keys = [prefix + name for name in ("problem.png", "public.png", "page.png", "proposal.png")]
        document = MatchupDocument.objects.create(
            tenant=self.tenant, inventory_file=file, r2_key=file.r2_key,
            title="Synthetic document", meta={"page_image_keys": [keys[2]]},
        )
        problem = MatchupProblem.objects.create(
            tenant=self.tenant, document=document, number=1, image_key=keys[0],
            meta={"public_cleanup": {"public_image_key": keys[1]}},
        )
        proposal = ProblemSegmentationProposal.objects.create(
            tenant=self.tenant, document=document, image_key=keys[3],
        )
        with self.captureOnCommitCallbacks(execute=True):
            response = self._delete_file(file.id)
            self.assertEqual(response.status_code, 204)
            self.assertEqual(self.deleted_keys, [])
            self.assertEqual(SubmissionStorageCleanupIntent.objects.count(), 5)
        self.assertCountEqual(self.deleted_keys, [file.r2_key, *keys])
        self.assertFalse(MatchupDocument.objects.filter(pk=document.id).exists())
        self.assertFalse(MatchupProblem.objects.filter(pk=problem.id).exists())
        self.assertFalse(ProblemSegmentationProposal.objects.filter(pk=proposal.id).exists())

    def test_surviving_json_image_owners_keep_shared_keys(self):
        from apps.domains.matchup.models import MatchupDocument, MatchupProblem

        first, second = self._file("public.png"), self._file("page.png")
        owner_file = self._file("owner.pdf")
        document = MatchupDocument.objects.create(
            tenant=self.tenant, inventory_file=owner_file, r2_key=owner_file.r2_key,
            title="Surviving owner", meta={"page_image_keys": [second.r2_key]},
        )
        problem = MatchupProblem.objects.create(
            tenant=self.tenant, document=document, number=1,
            meta={"public_cleanup": {"public_image_key": first.r2_key}},
        )
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self._delete_file(first.id).status_code, 204)
            self.assertEqual(self._delete_file(second.id).status_code, 204)
        self.assertEqual(self.deleted_keys, [])
        self.assertFalse(SubmissionStorageCleanupIntent.objects.exists())
        self.assertTrue(MatchupDocument.objects.filter(pk=document.id).exists())
        self.assertTrue(MatchupProblem.objects.filter(pk=problem.id).exists())


class InventoryDeleteCommittedResponseTests(InventoryDeleteFixtures, TransactionTestCase):
    def test_file_success_is_empty_204_after_exact_cleanup_and_reload(self):
        file = self._file()
        response = self._delete_file(file.id)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        self.assertEqual(self.deleted_keys, [file.r2_key])
        self.assertEqual(SubmissionStorageCleanupIntent.objects.get(object_key=file.r2_key).status, "cleaned")
        self.assertEqual(self._listed_files(), [])

    def test_folder_success_reports_actual_cleanup_then_empty_public_list(self):
        folder = InventoryFolder.objects.create(tenant=self.tenant, scope="admin", name="folder")
        file = self._file(folder=folder)
        response = self._delete_folder(folder.id)
        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertEqual(payload["deleted"], {"folders": 1, "files": 1, "matchup_docs": 0, "r2_objects": 1})
        self.assertEqual(payload["storage_cleanup"], {"pending": 0, "failed": 0, "cleaned": 1})
        self.assertEqual(self.deleted_keys, [file.r2_key])
        self.assertEqual(self._listed_files(), [])

    def test_file_provider_failure_reports_deleted_and_pending_then_retry_cleans(self):
        file = self._file()
        with patch("apps.infrastructure.storage.r2.delete_object_r2_storage", side_effect=RuntimeError("unavailable")):
            response = self._delete_file(file.id)
        self.assertEqual(response.status_code, 502)
        payload = json.loads(response.content)
        self.assertIs(payload["deleted"], True)
        self.assertEqual(payload["code"], "inventory_storage_cleanup_pending")
        self.assertIn("목록에서 삭제되었습니다", payload["detail"])
        self.assertEqual(payload["storage_cleanup"], {"pending": 0, "failed": 1, "cleaned": 0})
        self.assertEqual(self._listed_files(), [])
        intent = SubmissionStorageCleanupIntent.objects.get(object_key=file.r2_key)
        result = process_submission_storage_cleanup_intents(intent_ids=[intent.id])
        self.assertEqual(result.cleaned, 1)
        self.assertEqual(self.deleted_keys, [file.r2_key])
        self.assertEqual(self._listed_files(), [])

    def test_folder_partial_cleanup_reports_committed_counts_and_list_is_empty(self):
        folder = InventoryFolder.objects.create(tenant=self.tenant, scope="admin", name="folder")
        self._file("first.pdf", folder=folder)
        self._file("second.pdf", folder=folder)
        with patch("apps.infrastructure.storage.r2.delete_object_r2_storage", side_effect=[None, RuntimeError("unavailable")]):
            response = self._delete_folder(folder.id)
        self.assertEqual(response.status_code, 502)
        payload = json.loads(response.content)
        self.assertEqual(payload["deleted"], {"folders": 1, "files": 2, "matchup_docs": 0, "r2_objects": 1})
        self.assertEqual(payload["storage_cleanup"], {"pending": 0, "failed": 1, "cleaned": 1})
        self.assertEqual(self._listed_files(), [])
        self.assertFalse(InventoryFolder.objects.filter(pk=folder.id).exists())
