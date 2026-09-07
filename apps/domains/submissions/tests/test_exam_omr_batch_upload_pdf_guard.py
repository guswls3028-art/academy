from __future__ import annotations

import json
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch
from uuid import UUID

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import close_old_connections, connection, transaction
from django.test import TestCase, TransactionTestCase
from jsonschema import Draft4Validator
from rest_framework.test import APIRequestFactory, force_authenticate

from academy.adapters.tools.pymupdf_renderer import create_blank_pdf_bytes
from apps.core.models import Tenant, TenantMembership
from apps.domains.exams.models import Exam, Sheet
from apps.domains.submissions.models import OmrUploadBatch, OmrUploadBatchItem, Submission
from apps.domains.submissions.services.lifecycle import (
    retry_failed_submission as retry_failed_submission_lifecycle,
)
from apps.domains.submissions.services.transition import InvalidTransitionError
from apps.domains.submissions.views.exam_omr_batch_upload_view import (
    ExamOMRBatchInitializeView,
    ExamOMRBatchUploadView,
    OmrUploadBatchCompletionClaimView,
    OmrUploadBatchDetailView,
    OmrUploadBatchListView,
    OmrUploadBatchRetryView,
)


User = get_user_model()


def _pdf_bytes(page_count: int) -> bytes:
    return create_blank_pdf_bytes(page_count=page_count)


class ExamOmrMultipartOpenApiTests(unittest.TestCase):
    def test_openapi_accepts_valid_legacy_and_durable_multipart_requests(self):
        schema_path = Path(__file__).resolve().parents[4] / "schema" / "openapi.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        request_schema = schema["paths"][
            "/api/v1/submissions/submissions/exams/{exam_id}/omr/batch/"
        ]["post"]["requestBody"]["content"]["multipart/form-data"]["schema"]

        alternatives = request_schema.get("anyOf") or request_schema.get("oneOf")
        required_sets = {
            row["title"]: set(row["required"])
            for row in alternatives
        }
        self.assertEqual(
            required_sets,
            {
                "Legacy OMR single-file upload": {"file"},
                "Legacy OMR multi-file upload": {"files"},
                "Durable OMR batch single-file upload": {
                    "batch_id",
                    "item_ordinals",
                    "file",
                },
                "Durable OMR batch multi-file upload": {
                    "batch_id",
                    "item_ordinals",
                    "files",
                },
            },
        )

        validator = Draft4Validator(request_schema)
        batch_id = "00000000-0000-4000-8000-000000000001"
        valid_payloads = {
            "legacy single": {"file": "binary"},
            "legacy multi": {"files": ["binary-1", "binary-2"]},
            "durable single": {
                "batch_id": batch_id,
                "item_ordinals": [1],
                "file": "binary",
            },
            "durable multi": {
                "batch_id": batch_id,
                "item_ordinals": [1, 2],
                "files": ["binary-1", "binary-2"],
            },
        }
        for label, payload in valid_payloads.items():
            with self.subTest(payload=label):
                self.assertEqual(list(validator.iter_errors(payload)), [])

        malformed_payloads = {
            "missing upload": {},
            "durable missing ordinals": {"batch_id": batch_id, "file": "binary"},
            "durable missing batch": {"item_ordinals": [1], "file": "binary"},
            "durable missing file": {"batch_id": batch_id, "item_ordinals": [1]},
        }
        for label, payload in malformed_payloads.items():
            with self.subTest(payload=label):
                self.assertNotEqual(list(validator.iter_errors(payload)), [])


class ExamOMRBatchUploadPdfGuardTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.tenant = Tenant.objects.create(code="omr-upload", name="OMR Upload")
        self.user = User.objects.create_user(
            username="omr-upload-admin",
            password="pass1234!",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=self.user, role="admin")
        self.exam = Exam.objects.create(
            tenant=self.tenant,
            title="OMR Exam",
            exam_type=Exam.ExamType.TEMPLATE,
        )
        Sheet.objects.create(exam=self.exam, name="MAIN", total_questions=30)

    def _post(self, upload_file):
        request = self.factory.post(
            f"/api/v1/submissions/submissions/exams/{self.exam.id}/omr/batch/",
            data={"files": [upload_file]},
            format="multipart",
        )
        force_authenticate(request, user=self.user)
        request.tenant = self.tenant
        view = ExamOMRBatchUploadView.as_view()
        return view(request, exam_id=self.exam.id)

    def _initialize(self, total_count: int):
        request = self.factory.post(
            f"/api/v1/submissions/submissions/exams/{self.exam.id}/omr/batches/",
            data={"total_count": total_count},
            format="json",
        )
        force_authenticate(request, user=self.user)
        request.tenant = self.tenant
        return ExamOMRBatchInitializeView.as_view()(request, exam_id=self.exam.id)

    def _upload_to_batch(self, batch_id: UUID | str, uploads, ordinals):
        request = self.factory.post(
            f"/api/v1/submissions/submissions/exams/{self.exam.id}/omr/batch/",
            data={
                "batch_id": str(batch_id),
                "item_ordinals": [str(value) for value in ordinals],
                "files": uploads,
            },
            format="multipart",
        )
        force_authenticate(request, user=self.user)
        request.tenant = self.tenant
        return ExamOMRBatchUploadView.as_view()(request, exam_id=self.exam.id)

    def _detail(self, batch_id: UUID | str, *, user=None, tenant=None):
        request = self.factory.get(f"/api/v1/submissions/submissions/omr/batches/{batch_id}/")
        force_authenticate(request, user=user or self.user)
        request.tenant = tenant or self.tenant
        return OmrUploadBatchDetailView.as_view()(request, batch_id=batch_id)

    @staticmethod
    def _image(name: str):
        return SimpleUploadedFile(
            name,
            f"jpeg-body:{name}".encode(),
            content_type="image/jpeg",
        )

    def test_rejects_multipage_pdf_before_creating_submission(self):
        upload = SimpleUploadedFile(
            "scan-bundle.pdf",
            _pdf_bytes(2),
            content_type="application/pdf",
        )

        response = self._post(upload)

        self.assertEqual(response.status_code, 400)
        self.assertIn("2페이지 PDF", response.data["detail"])
        self.assertIn("답안지 1장당 1개 파일", response.data["detail"])

    @patch("apps.domains.submissions.views.exam_omr_batch_upload_view.dispatch_submission")
    @patch("apps.domains.submissions.serializers.submission.upload_fileobj_to_r2")
    def test_accepts_single_page_pdf(self, upload_fileobj_to_r2, dispatch_submission):
        upload = SimpleUploadedFile(
            "one-sheet.pdf",
            _pdf_bytes(1),
            content_type="application/pdf",
        )

        response = self._post(upload)

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["created_count"], 1)
        upload_fileobj_to_r2.assert_called_once()
        dispatch_submission.assert_called_once()

    def test_initializes_a_durable_batch_before_file_admission(self):
        response = self._initialize(22)

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["total_count"], 22)
        self.assertEqual(response.data["counts"]["pending_admission"], 22)
        self.assertEqual(response.data["pending_admission_ordinals"], list(range(1, 23)))

    def test_empty_file_keeps_a_stable_failed_scan_identity(self):
        batch_id = self._initialize(1).data["id"]

        response = self._upload_to_batch(
            batch_id,
            [SimpleUploadedFile("empty.jpg", b"", content_type="image/jpeg")],
            [1],
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["created_count"], 0)
        self.assertEqual(response.data["counts"]["failed"], 1)
        self.assertEqual(response.data["admission_failed_ordinals"], [1])
        self.assertEqual(response.data["items"], [
            {
                "id": f"{batch_id}:1",
                "ordinal": 1,
                "admission_status": "failed",
                "status": "failed",
                "submission_id": None,
                "duplicate_of_submission_id": None,
                "submission_status": None,
                "identifier_status": None,
                "failure_code": "empty_file",
                "failure_message": "빈 파일은 접수할 수 없습니다.",
            }
        ])

    @patch("apps.domains.submissions.views.exam_omr_batch_upload_view.dispatch_submission")
    @patch("apps.domains.submissions.serializers.submission.upload_fileobj_to_r2")
    def test_exact_file_reupload_across_batches_is_accounted_without_new_submission(
        self,
        upload_fileobj_to_r2,
        dispatch_submission,
    ):
        first_batch = self._initialize(1).data["id"]
        first = self._upload_to_batch(
            first_batch,
            [SimpleUploadedFile("scan-a.jpg", b"same-scan", content_type="image/jpeg")],
            [1],
        )
        second_batch = self._initialize(1).data["id"]
        duplicate = self._upload_to_batch(
            second_batch,
            [SimpleUploadedFile("renamed.jpg", b"same-scan", content_type="image/jpeg")],
            [1],
        )

        self.assertEqual(first.data["created_count"], 1)
        self.assertEqual(duplicate.status_code, 201)
        self.assertEqual(duplicate.data["created_count"], 0)
        self.assertEqual(duplicate.data["counts"]["duplicate"], 1)
        self.assertEqual(duplicate.data["duplicate_ordinals"], [1])
        self.assertTrue(duplicate.data["terminal"])
        self.assertEqual(Submission.objects.filter(target_id=self.exam.id).count(), 1)
        duplicate_item = OmrUploadBatchItem.objects.get(batch_id=second_batch, ordinal=1)
        self.assertEqual(
            duplicate_item.duplicate_of_submission_id,
            first.data["submission_ids"][0],
        )
        self.assertEqual(upload_fileobj_to_r2.call_count, 1)
        self.assertEqual(dispatch_submission.call_count, 1)

    @patch("apps.domains.submissions.views.exam_omr_batch_upload_view.dispatch_submission")
    @patch("apps.domains.submissions.serializers.submission.upload_fileobj_to_r2")
    def test_same_filename_with_different_bytes_is_not_a_duplicate(
        self,
        upload_fileobj_to_r2,
        dispatch_submission,
    ):
        first_batch = self._initialize(1).data["id"]
        self._upload_to_batch(
            first_batch,
            [SimpleUploadedFile("scan.jpg", b"scan-one", content_type="image/jpeg")],
            [1],
        )
        second_batch = self._initialize(1).data["id"]
        second = self._upload_to_batch(
            second_batch,
            [SimpleUploadedFile("scan.jpg", b"scan-two", content_type="image/jpeg")],
            [1],
        )

        self.assertEqual(second.data["created_count"], 1)
        self.assertEqual(second.data["counts"]["duplicate"], 0)
        self.assertEqual(Submission.objects.filter(target_id=self.exam.id).count(), 2)
        self.assertEqual(upload_fileobj_to_r2.call_count, 2)
        self.assertEqual(dispatch_submission.call_count, 2)

    def test_preserves_1_22_and_100_item_totals(self):
        for total_count in (1, 22, 100):
            with self.subTest(total_count=total_count):
                response = self._initialize(total_count)
                self.assertEqual(response.status_code, 201)
                batch = OmrUploadBatch.objects.get(id=response.data["id"])
                self.assertEqual(batch.total_count, total_count)
                self.assertEqual(batch.items.count(), total_count)

    @patch("apps.domains.submissions.views.exam_omr_batch_upload_view.dispatch_submission")
    @patch("apps.domains.submissions.serializers.submission.upload_fileobj_to_r2")
    def test_partial_admission_retry_keeps_successes_and_reuses_only_failed_ordinal(
        self,
        upload_fileobj_to_r2,
        dispatch_submission,
    ):
        batch_id = self._initialize(3).data["id"]
        upload_fileobj_to_r2.side_effect = [None, RuntimeError("r2 unavailable"), None]

        first = self._upload_to_batch(
            batch_id,
            [self._image("first.jpg"), self._image("second.jpg"), self._image("third.jpg")],
            [1, 2, 3],
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(first.data["created_count"], 2)
        self.assertEqual(first.data["counts"]["received"], 2)
        self.assertEqual(first.data["counts"]["failed"], 1)
        self.assertEqual(first.data["failed_ordinals"], [2])
        self.assertNotIn("first.jpg", str(first.data))
        item_one_id = OmrUploadBatchItem.objects.get(batch_id=batch_id, ordinal=1).submission_id
        self.assertEqual(Submission.objects.filter(target_id=self.exam.id).count(), 2)

        upload_fileobj_to_r2.side_effect = None
        retry = self._upload_to_batch(batch_id, [self._image("second-retry.jpg")], [2])

        self.assertEqual(retry.status_code, 201)
        self.assertEqual(retry.data["created_count"], 1)
        self.assertEqual(retry.data["counts"]["received"], 3)
        self.assertEqual(Submission.objects.filter(target_id=self.exam.id).count(), 3)
        self.assertEqual(
            OmrUploadBatchItem.objects.get(batch_id=batch_id, ordinal=1).submission_id,
            item_one_id,
        )

        duplicate = self._upload_to_batch(batch_id, [self._image("first-again.jpg")], [1])
        self.assertEqual(duplicate.data["created_count"], 0)
        self.assertEqual(Submission.objects.filter(target_id=self.exam.id).count(), 3)
        self.assertEqual(dispatch_submission.call_count, 3)

    @patch("apps.domains.submissions.views.exam_omr_batch_upload_view.dispatch_submission")
    @patch("apps.domains.submissions.serializers.submission.upload_fileobj_to_r2")
    def test_100_file_request_recovers_one_failed_ordinal_without_duplicate_successes(
        self,
        upload_fileobj_to_r2,
        dispatch_submission,
    ):
        batch_id = self._initialize(100).data["id"]
        call_count = 0

        def admit_or_fail(**_kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 57:
                raise TimeoutError("synthetic item admission timeout")

        upload_fileobj_to_r2.side_effect = admit_or_fail
        first = self._upload_to_batch(
            batch_id,
            [self._image(f"scan-{ordinal}.jpg") for ordinal in range(1, 101)],
            list(range(1, 101)),
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(first.data["total_count"], 100)
        self.assertEqual(first.data["created_count"], 99)
        self.assertEqual(first.data["counts"]["received"], 99)
        self.assertEqual(first.data["failed_ordinals"], [57])
        successful_ids = set(first.data["submission_ids"])
        self.assertEqual(len(successful_ids), 99)

        upload_fileobj_to_r2.side_effect = None
        retry = self._upload_to_batch(batch_id, [self._image("retry.jpg")], [57])

        self.assertEqual(retry.status_code, 201)
        self.assertEqual(retry.data["created_count"], 1)
        self.assertEqual(retry.data["counts"]["received"], 100)
        self.assertEqual(Submission.objects.filter(target_id=self.exam.id).count(), 100)
        self.assertEqual(dispatch_submission.call_count, 100)
        self.assertTrue(
            successful_ids.isdisjoint(set(retry.data["submission_ids"])),
            "retry must create only the failed ordinal",
        )

    @patch("apps.domains.submissions.views.exam_omr_batch_upload_view.dispatch_submission")
    @patch("apps.domains.submissions.serializers.submission.upload_fileobj_to_r2")
    def test_received_ordinal_ignores_invalid_resend_without_losing_link(
        self,
        upload_fileobj_to_r2,
        dispatch_submission,
    ):
        batch_id = self._initialize(1).data["id"]
        first = self._upload_to_batch(batch_id, [self._image("valid.jpg")], [1])
        item = OmrUploadBatchItem.objects.get(batch_id=batch_id, ordinal=1)
        original_submission_id = item.submission_id

        invalid = SimpleUploadedFile("invalid.txt", b"not-an-image", content_type="text/plain")
        duplicate = self._upload_to_batch(batch_id, [invalid], [1])

        item.refresh_from_db()
        self.assertEqual(first.data["created_count"], 1)
        self.assertEqual(duplicate.status_code, 201)
        self.assertEqual(duplicate.data["created_count"], 0)
        self.assertEqual(duplicate.data["counts"]["received"], 1)
        self.assertEqual(item.admission_status, OmrUploadBatchItem.AdmissionStatus.RECEIVED)
        self.assertEqual(item.submission_id, original_submission_id)
        self.assertEqual(upload_fileobj_to_r2.call_count, 1)
        self.assertEqual(dispatch_submission.call_count, 1)

    @patch(
        "apps.domains.submissions.views.exam_omr_batch_upload_view.delete_object_r2_storage",
        create=True,
    )
    @patch("apps.domains.submissions.views.exam_omr_batch_upload_view.dispatch_submission")
    @patch("apps.domains.submissions.serializers.submission.upload_fileobj_to_r2")
    def test_dispatch_rollback_deletes_exact_uploaded_object(
        self,
        upload_fileobj_to_r2,
        dispatch_submission,
        delete_object_r2_storage,
    ):
        batch_id = self._initialize(1).data["id"]
        dispatch_submission.side_effect = RuntimeError("dispatch failed after upload")

        response = self._upload_to_batch(batch_id, [self._image("rollback.jpg")], [1])

        uploaded_key = upload_fileobj_to_r2.call_args.kwargs["key"]
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["failed_ordinals"], [1])
        self.assertFalse(Submission.objects.filter(target_id=self.exam.id).exists())
        delete_object_r2_storage.assert_called_once_with(key=uploaded_key)

    @patch(
        "apps.domains.submissions.serializers.submission.delete_object_r2_storage",
        create=True,
    )
    @patch("apps.domains.submissions.serializers.submission.upload_fileobj_to_r2")
    def test_serializer_file_metadata_failure_deletes_exact_uploaded_object(
        self,
        upload_fileobj_to_r2,
        delete_object_r2_storage,
    ):
        original_save = Submission.save

        def fail_file_metadata_save(instance, *args, **kwargs):
            if "file_key" in set(kwargs.get("update_fields") or []):
                raise RuntimeError("file metadata save failed")
            return original_save(instance, *args, **kwargs)

        with patch.object(Submission, "save", new=fail_file_metadata_save):
            response = self._post(self._image("metadata-failure.jpg"))

        uploaded_key = upload_fileobj_to_r2.call_args.kwargs["key"]
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["failed_ordinals"], [1])
        self.assertFalse(Submission.objects.filter(target_id=self.exam.id).exists())
        delete_object_r2_storage.assert_called_once_with(key=uploaded_key)

    def test_legacy_invalid_sheet_does_not_leave_pending_batch(self):
        request = self.factory.post(
            f"/api/v1/submissions/submissions/exams/{self.exam.id}/omr/batch/",
            data={"files": [self._image("valid.jpg")], "sheet_id": 999999},
            format="multipart",
        )
        force_authenticate(request, user=self.user)
        request.tenant = self.tenant

        response = ExamOMRBatchUploadView.as_view()(request, exam_id=self.exam.id)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(OmrUploadBatch.objects.filter(exam_id=self.exam.id).count(), 0)

    def test_detail_maps_submission_states_without_claiming_completion(self):
        batch_id = self._initialize(7).data["id"]
        statuses = [
            Submission.Status.SUBMITTED,
            Submission.Status.DISPATCHED,
            Submission.Status.ANSWERS_READY,
            Submission.Status.DONE,
            Submission.Status.NEEDS_IDENTIFICATION,
            Submission.Status.FAILED,
            Submission.Status.SUPERSEDED,
        ]
        for ordinal, submission_status in enumerate(statuses, start=1):
            submission = Submission.objects.create(
                tenant=self.tenant,
                user=self.user,
                target_type=Submission.TargetType.EXAM,
                target_id=self.exam.id,
                source=Submission.Source.OMR_SCAN,
                status=submission_status,
            )
            OmrUploadBatchItem.objects.filter(batch_id=batch_id, ordinal=ordinal).update(
                admission_status=OmrUploadBatchItem.AdmissionStatus.RECEIVED,
                submission=submission,
            )

        batch_before = OmrUploadBatch.objects.get(id=batch_id)
        response = self._detail(batch_id)
        batch_after = OmrUploadBatch.objects.get(id=batch_id)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["counts"]["received"], 1)
        self.assertEqual(response.data["counts"]["processing"], 2)
        self.assertEqual(response.data["counts"]["completed"], 1)
        self.assertEqual(response.data["counts"]["needs_identification"], 1)
        self.assertEqual(response.data["counts"]["failed"], 1)
        self.assertEqual(response.data["counts"]["superseded"], 1)
        self.assertEqual(
            [item["status"] for item in response.data["items"]],
            [
                "received",
                "processing",
                "processing",
                "completed",
                "needs_identification",
                "failed",
                "superseded",
            ],
        )
        self.assertEqual(
            [item["id"] for item in response.data["items"]],
            [f"{batch_id}:{ordinal}" for ordinal in range(1, 8)],
        )
        self.assertFalse(response.data["terminal"])
        self.assertIsNone(batch_after.completion_notice_claimed_at)
        self.assertEqual(batch_after.updated_at, batch_before.updated_at)

    @patch("apps.domains.submissions.views.exam_omr_batch_upload_view.dispatch_submission")
    def test_retry_action_dispatches_only_linked_failed_ordinals(self, dispatch_submission):
        batch_id = self._initialize(2).data["id"]
        submissions = []
        for ordinal, submission_status in enumerate(
            [Submission.Status.FAILED, Submission.Status.DONE],
            start=1,
        ):
            submission = Submission.objects.create(
                tenant=self.tenant,
                user=self.user,
                target_type=Submission.TargetType.EXAM,
                target_id=self.exam.id,
                source=Submission.Source.OMR_SCAN,
                status=submission_status,
                file_key=f"opaque-{ordinal}",
            )
            submissions.append(submission)
            OmrUploadBatchItem.objects.filter(batch_id=batch_id, ordinal=ordinal).update(
                admission_status=OmrUploadBatchItem.AdmissionStatus.RECEIVED,
                submission=submission,
            )
        request = self.factory.post(
            f"/api/v1/submissions/submissions/omr/batches/{batch_id}/retry/",
            data={"item_ordinals": [1, 2]},
            format="json",
        )
        force_authenticate(request, user=self.user)
        request.tenant = self.tenant

        response = OmrUploadBatchRetryView.as_view()(request, batch_id=batch_id)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["retried_ordinals"], [1])
        self.assertEqual(response.data["skipped_ordinals"], [2])
        submissions[0].refresh_from_db()
        self.assertEqual(submissions[0].status, Submission.Status.SUBMITTED)
        dispatch_submission.assert_called_once()

    @patch("apps.domains.submissions.views.exam_omr_batch_upload_view.dispatch_submission")
    @patch(
        "apps.domains.submissions.views.exam_omr_batch_upload_view.retry_failed_submission",
        side_effect=InvalidTransitionError("FAILED", "SUBMITTED", "concurrent transition"),
    )
    def test_retry_transition_race_is_skipped_instead_of_returning_500(
        self,
        retry_failed_submission,
        dispatch_submission,
    ):
        batch_id = self._initialize(1).data["id"]
        submission = Submission.objects.create(
            tenant=self.tenant,
            user=self.user,
            target_type=Submission.TargetType.EXAM,
            target_id=self.exam.id,
            source=Submission.Source.OMR_SCAN,
            status=Submission.Status.FAILED,
            file_key="opaque-retry",
        )
        OmrUploadBatchItem.objects.filter(batch_id=batch_id, ordinal=1).update(
            admission_status=OmrUploadBatchItem.AdmissionStatus.RECEIVED,
            submission=submission,
        )
        request = self.factory.post(
            f"/api/v1/submissions/submissions/omr/batches/{batch_id}/retry/",
            data={"item_ordinals": [1]},
            format="json",
        )
        force_authenticate(request, user=self.user)
        request.tenant = self.tenant

        response = OmrUploadBatchRetryView.as_view()(request, batch_id=batch_id)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["retried_ordinals"], [])
        self.assertEqual(response.data["skipped_ordinals"], [1])
        retry_failed_submission.assert_called_once()
        dispatch_submission.assert_not_called()

    def test_terminal_completion_claim_is_explicit_and_exactly_once(self):
        batch_id = self._initialize(1).data["id"]
        submission = Submission.objects.create(
            tenant=self.tenant,
            user=self.user,
            target_type=Submission.TargetType.EXAM,
            target_id=self.exam.id,
            source=Submission.Source.OMR_SCAN,
            status=Submission.Status.DONE,
        )
        OmrUploadBatchItem.objects.filter(batch_id=batch_id, ordinal=1).update(
            admission_status=OmrUploadBatchItem.AdmissionStatus.RECEIVED,
            submission=submission,
        )

        detail = self._detail(batch_id)
        self.assertTrue(detail.data["terminal"])
        self.assertFalse(detail.data["completion_notice_claimed"])

        def claim():
            request = self.factory.post(
                f"/api/v1/submissions/submissions/omr/batches/{batch_id}/claim-completion/",
                data={},
                format="json",
            )
            force_authenticate(request, user=self.user)
            request.tenant = self.tenant
            return OmrUploadBatchCompletionClaimView.as_view()(request, batch_id=batch_id)

        first = claim()
        second = claim()

        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.data["notify"])
        self.assertFalse(second.data["notify"])
        self.assertTrue(second.data["batch"]["completion_notice_claimed"])

    def test_completion_claim_fails_closed_while_processing(self):
        batch_id = self._initialize(1).data["id"]
        request = self.factory.post(
            f"/api/v1/submissions/submissions/omr/batches/{batch_id}/claim-completion/",
            data={},
            format="json",
        )
        force_authenticate(request, user=self.user)
        request.tenant = self.tenant

        response = OmrUploadBatchCompletionClaimView.as_view()(request, batch_id=batch_id)

        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.data["batch"]["terminal"])
        self.assertFalse(OmrUploadBatch.objects.get(id=batch_id).completion_notice_claimed_at)

    def test_list_and_detail_are_creator_and_tenant_scoped(self):
        batch_id = self._initialize(1).data["id"]
        other_user = User.objects.create_user(
            username="other-omr-staff",
            password="pass1234!",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=other_user, role="admin")
        other_tenant = Tenant.objects.create(code="omr-other", name="OMR Other")

        self.assertEqual(self._detail(batch_id, user=other_user).status_code, 404)
        self.assertEqual(self._detail(batch_id, tenant=other_tenant).status_code, 403)

        request = self.factory.get("/api/v1/submissions/submissions/omr/batches/")
        force_authenticate(request, user=self.user)
        request.tenant = self.tenant
        response = OmrUploadBatchListView.as_view()(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.data], [str(batch_id)])


class OmrUploadBatchCompletionClaimConcurrencyPostgresTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        if connection.vendor != "postgresql":
            raise unittest.SkipTest("PostgreSQL is required for row-lock verification.")
        super().setUpClass()

    def test_concurrent_completion_claim_allows_exactly_one_notification(self):
        tenant = Tenant.objects.create(code="omr-claim-race", name="OMR Claim Race")
        user = User.objects.create_user(
            username="omr-claim-race-admin",
            password="pass1234!",
            tenant=tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(tenant=tenant, user=user, role="admin")
        exam = Exam.objects.create(
            tenant=tenant,
            title="OMR Claim Exam",
            exam_type=Exam.ExamType.TEMPLATE,
        )
        batch = OmrUploadBatch.objects.create(
            tenant=tenant,
            created_by=user,
            exam_id=exam.id,
            total_count=1,
        )
        submission = Submission.objects.create(
            tenant=tenant,
            user=user,
            target_type=Submission.TargetType.EXAM,
            target_id=exam.id,
            source=Submission.Source.OMR_SCAN,
            status=Submission.Status.DONE,
        )
        OmrUploadBatchItem.objects.create(
            tenant=tenant,
            exam_id=exam.id,
            batch=batch,
            ordinal=1,
            admission_status=OmrUploadBatchItem.AdmissionStatus.RECEIVED,
            submission=submission,
        )
        barrier = threading.Barrier(2, timeout=10)
        outcomes: list[bool] = []
        errors: list[Exception] = []

        def worker():
            close_old_connections()
            try:
                thread_user = User.objects.get(pk=user.pk)
                thread_tenant = Tenant.objects.get(pk=tenant.pk)
                request = APIRequestFactory().post(
                    f"/api/v1/submissions/submissions/omr/batches/{batch.id}/claim-completion/",
                    data={},
                    format="json",
                )
                force_authenticate(request, user=thread_user)
                request.tenant = thread_tenant
                barrier.wait()
                response = OmrUploadBatchCompletionClaimView.as_view()(
                    request,
                    batch_id=batch.id,
                )
                outcomes.append(bool(response.data["notify"]))
            except Exception as exc:
                errors.append(exc)
            finally:
                close_old_connections()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertCountEqual(outcomes, [True, False])


class OmrUploadBatchMutationConcurrencyPostgresTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        if connection.vendor != "postgresql":
            raise unittest.SkipTest("PostgreSQL is required for row-lock verification.")
        super().setUpClass()

    def setUp(self):
        self.tenant = Tenant.objects.create(code="omr-mutation-race", name="OMR Mutation Race")
        self.user = User.objects.create_user(
            username="omr-mutation-race-admin",
            password="pass1234!",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=self.user, role="admin")
        self.exam = Exam.objects.create(
            tenant=self.tenant,
            title="OMR Mutation Race Exam",
            exam_type=Exam.ExamType.TEMPLATE,
        )
        Sheet.objects.create(exam=self.exam, name="MAIN", total_questions=30)

    def _initialize(self) -> OmrUploadBatch:
        request = APIRequestFactory().post(
            f"/api/v1/submissions/submissions/exams/{self.exam.id}/omr/batches/",
            data={"total_count": 1},
            format="json",
        )
        force_authenticate(request, user=self.user)
        request.tenant = self.tenant
        response = ExamOMRBatchInitializeView.as_view()(request, exam_id=self.exam.id)
        return OmrUploadBatch.objects.get(id=response.data["id"])

    @staticmethod
    def _thread_upload(*, batch_id, exam_id, user, tenant, upload_file):
        request = APIRequestFactory().post(
            f"/api/v1/submissions/submissions/exams/{exam_id}/omr/batch/",
            data={
                "batch_id": str(batch_id),
                "item_ordinals": ["1"],
                "files": [upload_file],
            },
            format="multipart",
        )
        force_authenticate(request, user=user)
        request.tenant = tenant
        return ExamOMRBatchUploadView.as_view()(request, exam_id=exam_id)

    def test_invalid_concurrent_resend_cannot_clear_received_item(self):
        batch = self._initialize()
        upload_entered = threading.Event()
        allow_upload = threading.Event()
        invalid_done = threading.Event()
        responses = {}
        errors: list[Exception] = []

        def blocking_upload(**_kwargs):
            upload_entered.set()
            if not allow_upload.wait(timeout=10):
                raise TimeoutError("test did not release valid upload")

        def valid_worker():
            close_old_connections()
            try:
                user = User.objects.get(pk=self.user.pk)
                tenant = Tenant.objects.get(pk=self.tenant.pk)
                with (
                    patch(
                        "apps.domains.submissions.serializers.submission.upload_fileobj_to_r2",
                        side_effect=blocking_upload,
                    ),
                    patch(
                        "apps.domains.submissions.views.exam_omr_batch_upload_view.dispatch_submission"
                    ),
                ):
                    responses["valid"] = self._thread_upload(
                        batch_id=batch.id,
                        exam_id=self.exam.id,
                        user=user,
                        tenant=tenant,
                        upload_file=SimpleUploadedFile(
                            "valid.jpg",
                            b"jpeg-body",
                            content_type="image/jpeg",
                        ),
                    )
            except Exception as exc:
                errors.append(exc)
            finally:
                close_old_connections()

        def invalid_worker():
            close_old_connections()
            try:
                user = User.objects.get(pk=self.user.pk)
                tenant = Tenant.objects.get(pk=self.tenant.pk)
                responses["invalid"] = self._thread_upload(
                    batch_id=batch.id,
                    exam_id=self.exam.id,
                    user=user,
                    tenant=tenant,
                    upload_file=SimpleUploadedFile(
                        "invalid.txt",
                        b"invalid",
                        content_type="text/plain",
                    ),
                )
            except Exception as exc:
                errors.append(exc)
            finally:
                invalid_done.set()
                close_old_connections()

        valid_thread = threading.Thread(target=valid_worker)
        valid_thread.start()
        self.assertTrue(upload_entered.wait(timeout=10))
        invalid_thread = threading.Thread(target=invalid_worker)
        invalid_thread.start()
        time.sleep(0.5)
        self.assertFalse(invalid_done.is_set())
        allow_upload.set()
        valid_thread.join(timeout=15)
        invalid_thread.join(timeout=15)

        self.assertFalse(valid_thread.is_alive())
        self.assertFalse(invalid_thread.is_alive())
        self.assertEqual(errors, [])
        item = OmrUploadBatchItem.objects.get(batch=batch, ordinal=1)
        self.assertEqual(item.admission_status, OmrUploadBatchItem.AdmissionStatus.RECEIVED)
        self.assertIsNotNone(item.submission_id)
        self.assertEqual(responses["valid"].data["created_count"], 1)
        self.assertEqual(responses["invalid"].data["created_count"], 0)

    def test_retry_locks_submission_before_transition_and_preserves_newer_callback(self):
        batch = self._initialize()
        submission = Submission.objects.create(
            tenant=self.tenant,
            user=self.user,
            target_type=Submission.TargetType.EXAM,
            target_id=self.exam.id,
            source=Submission.Source.OMR_SCAN,
            status=Submission.Status.FAILED,
            file_key="opaque-race",
        )
        OmrUploadBatchItem.objects.filter(batch=batch, ordinal=1).update(
            admission_status=OmrUploadBatchItem.AdmissionStatus.RECEIVED,
            submission=submission,
        )
        retry_entered = threading.Event()
        allow_retry = threading.Event()
        callback_done = threading.Event()
        errors: list[Exception] = []
        responses = {}

        def blocking_retry(locked_submission, *, actor):
            retry_entered.set()
            if not allow_retry.wait(timeout=10):
                raise TimeoutError("test did not release retry")
            retry_failed_submission_lifecycle(locked_submission, actor=actor)

        def retry_worker():
            close_old_connections()
            try:
                user = User.objects.get(pk=self.user.pk)
                tenant = Tenant.objects.get(pk=self.tenant.pk)
                request = APIRequestFactory().post(
                    f"/api/v1/submissions/submissions/omr/batches/{batch.id}/retry/",
                    data={"item_ordinals": [1]},
                    format="json",
                )
                force_authenticate(request, user=user)
                request.tenant = tenant
                with (
                    patch(
                        "apps.domains.submissions.views.exam_omr_batch_upload_view.retry_failed_submission",
                        side_effect=blocking_retry,
                    ),
                    patch(
                        "apps.domains.submissions.views.exam_omr_batch_upload_view.dispatch_submission"
                    ),
                ):
                    responses["retry"] = OmrUploadBatchRetryView.as_view()(
                        request,
                        batch_id=batch.id,
                    )
            except Exception as exc:
                errors.append(exc)
            finally:
                close_old_connections()

        def callback_worker():
            close_old_connections()
            try:
                with transaction.atomic():
                    current = Submission.objects.select_for_update().get(pk=submission.pk)
                    current.status = Submission.Status.DONE
                    current.save(update_fields=["status", "updated_at"])
            except Exception as exc:
                errors.append(exc)
            finally:
                callback_done.set()
                close_old_connections()

        retry_thread = threading.Thread(target=retry_worker)
        retry_thread.start()
        self.assertTrue(retry_entered.wait(timeout=10))
        callback_thread = threading.Thread(target=callback_worker)
        callback_thread.start()
        callback_finished_before_retry = callback_done.wait(timeout=1)
        allow_retry.set()
        retry_thread.join(timeout=15)
        callback_thread.join(timeout=15)

        self.assertFalse(retry_thread.is_alive())
        self.assertFalse(callback_thread.is_alive())
        self.assertFalse(callback_finished_before_retry)
        self.assertEqual(errors, [])
        submission.refresh_from_db()
        self.assertEqual(submission.status, Submission.Status.DONE)
        self.assertEqual(responses["retry"].data["retried_ordinals"], [1])
