"""Community DELETE → DB/outbox → exact mocked storage cleanup integration."""

from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import connection, connections, transaction
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.core.models import Tenant, TenantMembership
from apps.domains.community.api.views.post_views import PostViewSet
from apps.domains.community.models import PostAttachment, PostEntity
from apps.domains.inventory.models import InventoryFile
from apps.domains.submissions.models import SubmissionStorageCleanupIntent
from apps.domains.submissions.services.lifecycle import process_submission_storage_cleanup_intents


class CommunityDeleteResponseTests(SimpleTestCase):
    def test_pending_outer_transaction_does_not_claim_completed_204(self):
        with patch("apps.domains.community.services.deletion.storage_cleanup_status", return_value={
            "pending": 1, "failed": 0, "cleaned": 0,
        }):
            with patch("django.db.transaction.get_connection", return_value=SimpleNamespace(in_atomic_block=True)):
                response = PostViewSet._content_deleted_response(1, {"posts": 0, "attachments": 1}, (1,))
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.data["storage_cleanup"], {"pending": 1, "failed": 0, "cleaned": 0})

    def test_pending_and_failed_counts_are_disjoint(self):
        with patch("apps.domains.community.services.deletion.storage_cleanup_status", return_value={
            "pending": 1, "failed": 2, "cleaned": 3,
        }):
            response = PostViewSet._content_deleted_response(1, {"posts": 1, "attachments": 6}, (1, 2, 3, 4, 5, 6))
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.data["deleted"]["r2_objects"], 3)
        self.assertEqual(response.data["storage_cleanup"]["pending"] + response.data["storage_cleanup"]["failed"], 3)


class CommunityDeleteFixtures:
    def setUp(self):
        self.tenant = Tenant.objects.create(code="community-delete", name="Synthetic community")
        self.user = get_user_model().objects.create_user(
            username="community-delete", tenant=self.tenant, password="test-only",
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=self.user, role="owner")
        self.factory = APIRequestFactory()
        self.deleted_keys = []
        self.fail_keys = set()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch(
            "apps.infrastructure.storage.r2.delete_object_r2_storage", side_effect=self._delete_object,
        ))
        self.stack.enter_context(patch(
            "apps.infrastructure.storage.r2.generate_presigned_get_url_storage",
            return_value="https://synthetic.invalid/file",
        ))

    def _delete_object(self, *, key):
        self.deleted_keys.append(key)
        if key in self.fail_keys:
            raise RuntimeError("synthetic provider failure")

    def _post(self, **kwargs):
        return PostEntity.objects.create(
            tenant=self.tenant, title="Synthetic post", content="Preserve unrelated content",
            post_type="board", author_role="staff", status="published", **kwargs,
        )

    def _attachment(self, post, name="file.pdf", *, key=None, tenant=None):
        return PostAttachment.objects.create(
            tenant=tenant or self.tenant, post=post, original_name=name, size_bytes=8,
            content_type="application/pdf",
            r2_key=key or f"tenants/{self.tenant.id}/community/posts/{post.id}/{name}",
        )

    def _request(self, post_id, *, attachment_id=None, method="delete"):
        suffix = f"attachments/{attachment_id}/" if attachment_id is not None else ""
        request = getattr(self.factory, method)(f"/api/v1/community/posts/{post_id}/{suffix}")
        force_authenticate(request, user=self.user)
        request.tenant = self.tenant
        action = "retrieve" if method == "get" else "delete_attachment" if suffix else "destroy"
        kwargs = {"pk": post_id}
        if attachment_id is not None:
            kwargs["att_id"] = attachment_id
        return PostViewSet.as_view({method: action})(request, **kwargs)

    def _upload(self, post_id):
        request = self.factory.post(
            f"/api/v1/community/posts/{post_id}/attachments/",
            {"files": [SimpleUploadedFile("same.pdf", b"same content", content_type="application/pdf")],
             "idempotency_key": "same-exact-batch"}, format="multipart",
        )
        force_authenticate(request, user=self.user)
        request.tenant = self.tenant
        return PostViewSet.as_view({"post": "upload_attachments"})(request, pk=post_id)


class CommunityDeleteDurabilityTests(CommunityDeleteFixtures, TestCase):
    def test_attachment_outer_rollback_preserves_metadata_and_never_calls_provider(self):
        post = self._post()
        att = self._attachment(post)
        with self.captureOnCommitCallbacks(execute=True):
            with self.assertRaisesMessage(RuntimeError, "outer rollback"):
                with transaction.atomic():
                    response = self._request(post.id, attachment_id=att.id)
                    self.assertEqual(response.status_code, 502)
                    self.assertEqual(response.data["storage_cleanup"], {"pending": 1, "failed": 0, "cleaned": 0})
                    raise RuntimeError("outer rollback")
        self.assertEqual(self.deleted_keys, [])
        self.assertTrue(PostAttachment.objects.filter(pk=att.id).exists())
        self.assertFalse(SubmissionStorageCleanupIntent.objects.exists())

    def test_attachment_db_delete_failure_preserves_original_and_never_calls_provider(self):
        post = self._post()
        att = self._attachment(post)
        with patch.object(PostAttachment, "delete", side_effect=RuntimeError("db failure")):
            with self.assertRaisesMessage(RuntimeError, "db failure"):
                self._request(post.id, attachment_id=att.id)
        self.assertEqual(self.deleted_keys, [])
        self.assertTrue(PostAttachment.objects.filter(pk=att.id).exists())
        self.assertFalse(SubmissionStorageCleanupIntent.objects.exists())

    def test_post_cascade_records_exact_intents_before_outer_commit(self):
        post = self._post()
        attachments = [self._attachment(post, name) for name in ("one.pdf", "two.pdf")]
        other = self._attachment(self._post(), "keep.pdf")
        keys = sorted(att.r2_key for att in attachments)
        with self.captureOnCommitCallbacks(execute=True):
            with transaction.atomic():
                response = self._request(post.id)
                self.assertEqual(response.status_code, 502)
                self.assertEqual(response.data["storage_cleanup"], {"pending": 2, "failed": 0, "cleaned": 0})
                self.assertEqual(self.deleted_keys, [])
                self.assertFalse(PostEntity.objects.filter(pk=post.id).exists())
                self.assertCountEqual(
                    SubmissionStorageCleanupIntent.objects.values_list("object_key", flat=True), keys,
                )
        self.assertEqual(sorted(self.deleted_keys), keys)
        self.assertEqual(SubmissionStorageCleanupIntent.objects.filter(status="cleaned").count(), 2)
        self.assertTrue(PostAttachment.objects.filter(pk=other.id).exists())

    def test_post_outer_rollback_preserves_cascade_and_no_intents(self):
        post = self._post()
        att = self._attachment(post)
        with self.captureOnCommitCallbacks(execute=True):
            with self.assertRaisesMessage(RuntimeError, "outer rollback"):
                with transaction.atomic():
                    self._request(post.id)
                    raise RuntimeError("outer rollback")
        self.assertTrue(PostEntity.objects.filter(pk=post.id).exists())
        self.assertTrue(PostAttachment.objects.filter(pk=att.id).exists())
        self.assertFalse(SubmissionStorageCleanupIntent.objects.exists())
        self.assertEqual(self.deleted_keys, [])

    def test_intent_save_failure_rolls_back_post_and_attachments(self):
        post = self._post()
        att = self._attachment(post)
        with patch.object(SubmissionStorageCleanupIntent, "save", side_effect=RuntimeError("intent failure")):
            with self.assertRaisesMessage(RuntimeError, "intent failure"):
                self._request(post.id)
        self.assertTrue(PostEntity.objects.filter(pk=post.id).exists())
        self.assertTrue(PostAttachment.objects.filter(pk=att.id).exists())
        self.assertEqual(self.deleted_keys, [])

    def test_foreign_namespace_refuses_attachment_and_post_before_mutation(self):
        post = self._post()
        att = self._attachment(post, key=f"tenants/{self.tenant.id + 1}/community/foreign.pdf")
        for attachment_id in (att.id, None):
            self.assertEqual(self._request(post.id, attachment_id=attachment_id).status_code, 409)
        self.assertTrue(PostAttachment.objects.filter(pk=att.id).exists())
        self.assertTrue(PostEntity.objects.filter(pk=post.id).exists())
        self.assertEqual(self.deleted_keys, [])

    def test_corrupt_foreign_tenant_child_blocks_post_cascade(self):
        foreign = Tenant.objects.create(code="community-foreign", name="Synthetic foreign")
        post = self._post()
        att = self._attachment(post, tenant=foreign)
        self.assertEqual(self._request(post.id).status_code, 409)
        self.assertTrue(PostAttachment.objects.filter(pk=att.id).exists())
        self.assertTrue(PostEntity.objects.filter(pk=post.id).exists())
        self.assertEqual(self.deleted_keys, [])

    def test_cross_tenant_surviving_owner_preserves_legacy_exact_object(self):
        foreign = Tenant.objects.create(code="community-foreign", name="Synthetic foreign")
        post = self._post()
        att = self._attachment(post, key="legacy/community/shared.pdf")
        owner = InventoryFile.objects.create(
            tenant=foreign, scope="admin", display_name="Shared", original_name="shared.pdf", r2_key=att.r2_key,
        )
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self._request(post.id, attachment_id=att.id).status_code, 204)
        self.assertFalse(PostAttachment.objects.filter(pk=att.id).exists())
        self.assertTrue(InventoryFile.objects.filter(pk=owner.id).exists())
        self.assertEqual(self.deleted_keys, [])
        self.assertFalse(SubmissionStorageCleanupIntent.objects.exists())

    def test_support_attachment_and_post_cascade_keep_submitted_records(self):
        post = self._post(support_kind="bug")
        att = self._attachment(post)
        self.assertEqual(self._request(post.id, attachment_id=att.id).status_code, 403)
        # Support posts are intentionally absent from the general CRUD queryset.
        self.assertEqual(self._request(post.id).status_code, 404)
        self.assertTrue(PostAttachment.objects.filter(pk=att.id).exists())
        self.assertTrue(PostEntity.objects.filter(pk=post.id).exists())
        self.assertEqual(self.deleted_keys, [])

    def test_upload_holds_post_then_exact_key_lock_before_provider_put(self):
        post = self._post()
        seen = []
        from apps.domains.submissions.services.lifecycle import lock_storage_object_keys

        def lock_keys(**kwargs):
            self.assertTrue(connection.in_atomic_block)
            seen.append("key")
            return lock_storage_object_keys(**kwargs)

        def put(**kwargs):
            self.assertEqual(seen, ["key"])
            self.assertTrue(connection.in_atomic_block)
            seen.append("put")

        with patch("apps.domains.community.api.views.post_views.lock_storage_object_keys", side_effect=lock_keys):
            with patch("apps.infrastructure.storage.r2.upload_fileobj_to_r2_storage", side_effect=put):
                self.assertEqual(self._upload(post.id).status_code, 201)
        self.assertEqual(seen, ["key", "put"])
        self.assertEqual(PostAttachment.objects.filter(post=post).count(), 1)


class CommunityDeleteCommittedTests(CommunityDeleteFixtures, TransactionTestCase):
    def test_post_normal_autocommit_success_returns_204_and_cleans_exact_keys(self):
        post = self._post()
        att = self._attachment(post)
        response = self._request(post.id)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.deleted_keys, [att.r2_key])
        self.assertEqual(SubmissionStorageCleanupIntent.objects.get().status, "cleaned")
        self.assertEqual(self._request(post.id, method="get").status_code, 404)

    def test_attachment_provider_failure_reports_deleted_truth_and_retry_converges(self):
        post = self._post()
        att = self._attachment(post)
        self.fail_keys.add(att.r2_key)
        response = self._request(post.id, attachment_id=att.id)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.data["code"], "community_storage_cleanup_pending")
        self.assertEqual(response.data["deleted"], {"posts": 0, "attachments": 1, "r2_objects": 0})
        self.assertEqual(response.data["storage_cleanup"], {"pending": 0, "failed": 1, "cleaned": 0})
        self.assertNotIn(att.r2_key, str(response.data))
        self.assertFalse(PostAttachment.objects.filter(pk=att.id).exists())
        reload = self._request(post.id, method="get")
        self.assertEqual(reload.status_code, 200)
        self.assertEqual(reload.data["attachments"], [])
        self.assertEqual(reload.data["content"], post.content)
        intent = SubmissionStorageCleanupIntent.objects.get(object_key=att.r2_key)
        self.fail_keys.clear()
        process_submission_storage_cleanup_intents(intent_ids=[intent.id])
        intent.refresh_from_db()
        self.assertEqual(intent.status, "cleaned")
        self.assertEqual(self.deleted_keys, [att.r2_key, att.r2_key])
        process_submission_storage_cleanup_intents(intent_ids=[intent.id])
        self.assertEqual(self.deleted_keys, [att.r2_key, att.r2_key])

    def test_post_partial_provider_failure_keeps_only_failed_key_retryable(self):
        post = self._post()
        first, second = self._attachment(post, "one.pdf"), self._attachment(post, "two.pdf")
        self.fail_keys.add(second.r2_key)
        response = self._request(post.id)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.data["deleted"], {"posts": 1, "attachments": 2, "r2_objects": 1})
        self.assertEqual(response.data["storage_cleanup"], {"pending": 0, "failed": 1, "cleaned": 1})
        self.assertEqual(self._request(post.id, method="get").status_code, 404)
        self.assertFalse(PostAttachment.objects.filter(post_id=post.id).exists())
        self.fail_keys.clear()
        process_submission_storage_cleanup_intents()
        self.assertEqual(self.deleted_keys.count(first.r2_key), 1)
        self.assertEqual(self.deleted_keys.count(second.r2_key), 2)
        self.assertEqual(SubmissionStorageCleanupIntent.objects.filter(status="cleaned").count(), 2)

    def test_unshared_legacy_attachment_normal_success_and_reload(self):
        post = self._post()
        att = self._attachment(post, key="legacy/community/original.pdf")
        self.assertEqual(self._request(post.id, attachment_id=att.id).status_code, 204)
        self.assertEqual(self.deleted_keys, [att.r2_key])
        self.assertEqual(self._request(post.id, method="get").data["attachments"], [])
        self.assertEqual(SubmissionStorageCleanupIntent.objects.get().status, "cleaned")

    def test_existing_purge_consumer_drains_community_intent_without_student_targets(self):
        post = self._post()
        att = self._attachment(post)
        self.fail_keys.add(att.r2_key)
        self.assertEqual(self._request(post.id, attachment_id=att.id).status_code, 502)
        self.fail_keys.clear()
        output = StringIO()
        call_command("purge_deleted_students", stdout=output)
        self.assertEqual(SubmissionStorageCleanupIntent.objects.get().status, "cleaned")
        self.assertIn("cleaned=1 failed=0 deferred=0", output.getvalue())
        self.assertEqual(self.deleted_keys, [att.r2_key, att.r2_key])


@skipUnless(connection.vendor == "postgresql", "requires PostgreSQL row/advisory locks")
class CommunityDeleteConcurrencyTests(CommunityDeleteFixtures, TransactionTestCase):
    def _failed_reupload_fixture(self):
        post = self._post()
        with patch("apps.infrastructure.storage.r2.upload_fileobj_to_r2_storage"):
            self.assertEqual(self._upload(post.id).status_code, 201)
        att = PostAttachment.objects.get(post=post)
        self.fail_keys.add(att.r2_key)
        self.assertEqual(self._request(post.id, attachment_id=att.id).status_code, 502)
        self.fail_keys.clear()
        self.deleted_keys.clear()
        return post, att.r2_key, SubmissionStorageCleanupIntent.objects.get().id

    def _thread(self, function, pid_holder=None):
        connections.close_all()
        try:
            if pid_holder is not None:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    pid_holder.append(cursor.fetchone()[0])
            return function()
        finally:
            connections.close_all()

    def _assert_waiting_on_key(self, pid_holder):
        deadline = monotonic() + 10
        while monotonic() < deadline:
            if pid_holder:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = %s AND locktype = 'advisory' AND NOT granted)",
                        [pid_holder[0]],
                    )
                    if cursor.fetchone()[0]:
                        return
            sleep(0.02)
        self.fail("concurrent operation did not wait on the exact storage key lock")

    def test_cleanup_waits_for_reupload_commit_then_preserves_new_owner(self):
        post, key, intent_id = self._failed_reupload_fixture()
        entered, release = Event(), Event()
        processor_pid = []

        def put(**kwargs):
            self.assertEqual(kwargs["key"], key)
            entered.set()
            self.assertTrue(release.wait(15))

        with patch("apps.infrastructure.storage.r2.upload_fileobj_to_r2_storage", side_effect=put):
            with ThreadPoolExecutor(max_workers=2) as pool:
                upload = pool.submit(self._thread, lambda: self._upload(post.id))
                try:
                    self.assertTrue(entered.wait(10))
                    cleanup = pool.submit(self._thread, lambda: process_submission_storage_cleanup_intents(
                        intent_ids=[intent_id],
                    ), processor_pid)
                    self._assert_waiting_on_key(processor_pid)
                    self.assertEqual(self.deleted_keys, [])
                finally:
                    release.set()
                self.assertEqual(upload.result(timeout=10).status_code, 201)
                self.assertEqual(cleanup.result(timeout=10).deferred, 1)
        self.assertEqual(PostAttachment.objects.get(post=post).r2_key, key)
        self.assertEqual(self.deleted_keys, [])

    def test_reupload_waits_for_cleanup_then_replaces_exact_object(self):
        post, key, intent_id = self._failed_reupload_fixture()
        entered, release = Event(), Event()
        upload_pid, object_present = [], set()

        def delete(*, key):
            entered.set()
            self.assertTrue(release.wait(15))
            object_present.discard(key)

        def put(**kwargs):
            object_present.add(kwargs["key"])

        with patch("apps.infrastructure.storage.r2.delete_object_r2_storage", side_effect=delete):
            with patch("apps.infrastructure.storage.r2.upload_fileobj_to_r2_storage", side_effect=put):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    cleanup = pool.submit(self._thread, lambda: process_submission_storage_cleanup_intents(intent_ids=[intent_id]))
                    try:
                        self.assertTrue(entered.wait(10))
                        upload = pool.submit(self._thread, lambda: self._upload(post.id), upload_pid)
                        self._assert_waiting_on_key(upload_pid)
                    finally:
                        release.set()
                    self.assertEqual(cleanup.result(timeout=10).cleaned, 1)
                    self.assertEqual(upload.result(timeout=10).status_code, 201)
        self.assertEqual(object_present, {key})
        self.assertEqual(PostAttachment.objects.get(post=post).r2_key, key)
