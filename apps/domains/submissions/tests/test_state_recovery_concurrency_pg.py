from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import close_old_connections, connection
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.core.models import Tenant, TenantMembership
from apps.domains.assets.omr.services.meta_generator import build_omr_meta
from apps.domains.enrollment.models import Enrollment, SessionEnrollment
from apps.domains.exams.models import (
    AnswerKey,
    Exam,
    ExamEnrollment,
    ExamQuestion,
    Sheet,
)
from apps.domains.lectures.models import Lecture, Session
from apps.domains.results.models import ExamResult, Result, ResultFact
from apps.domains.students.services.creation import create_student_account
from apps.domains.submissions.models import (
    OMRDetectedAnswer,
    OMRRecognitionRun,
    OMRStudentMatch,
    Submission,
    SubmissionAnswer,
)
from apps.domains.submissions.omr_pipeline.services.state_recovery import (
    detect_stuck_submissions,
    recover_stuck_submissions,
)
from apps.domains.submissions.views.exam_omr_batch_upload_view import (
    ExamOMRBatchUploadView,
)
from apps.domains.submissions.views.submission_view import SubmissionViewSet


User = get_user_model()


class _InProcessSQSClient:
    """SQS-shaped FIFO transport; the production queue codec stays real."""

    def __init__(self) -> None:
        self.message: dict | None = None
        self.deleted = False

    def send_message(self, *, queue_name: str, message: dict) -> bool:
        if self.message is not None and not self.deleted:
            raise AssertionError("the focused flow must publish exactly one message")
        self.deleted = False
        self.message = {
            "Body": json.dumps(message),
            "ReceiptHandle": "omr-positive-flow-receipt",
            "MessageId": "omr-positive-flow-message",
            "QueueName": queue_name,
        }
        return True

    def receive_message(self, *, queue_name: str, wait_time_seconds: int) -> dict | None:
        del wait_time_seconds
        if self.deleted or self.message is None:
            return None
        if self.message["QueueName"] != queue_name:
            return None
        return dict(self.message)

    def delete_message(self, *, queue_name: str, receipt_handle: str) -> bool:
        if self.message is None:
            return False
        if self.message["QueueName"] != queue_name or self.message["ReceiptHandle"] != receipt_handle:
            return False
        self.deleted = True
        return True

    def change_message_visibility(
        self,
        *,
        queue_name: str,
        receipt_handle: str,
        visibility_timeout: int,
    ) -> bool:
        del visibility_timeout
        return bool(
            self.message and self.message["QueueName"] == queue_name and self.message["ReceiptHandle"] == receipt_handle
        )

    def get_queue_counts(self, *, queue_name: str) -> dict[str, int]:
        visible = int(self.message is not None and not self.deleted and self.message["QueueName"] == queue_name)
        return {"visible": visible, "not_visible": 0, "delayed": 0}


class StateRecoveryConcurrencyPostgresTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        if connection.vendor != "postgresql":
            raise unittest.SkipTest(
                "PostgreSQL row locking is required for state recovery concurrency."
            )
        super().setUpClass()

    def setUp(self):
        suffix = uuid.uuid4().hex[:8]
        self.tenant = Tenant.objects.create(
            name=f"State Recovery Concurrency {suffix}",
            code=f"state-recovery-concurrency-{suffix}",
            is_active=True,
        )
        self.user = User.objects.create_user(
            username=f"state-recovery-concurrency-{suffix}",
            password="test1234",
            tenant=self.tenant,
            is_staff=True,
        )

    def _make_stale_submission(self, *, status: str) -> Submission:
        submission = Submission.objects.create(
            tenant=self.tenant,
            user=self.user,
            target_type=Submission.TargetType.EXAM,
            target_id=1,
            source=Submission.Source.OMR_SCAN,
            status=status,
            file_key="omr/state-recovery-concurrency.jpg",
        )
        Submission.objects.filter(pk=submission.pk).update(
            updated_at=timezone.now() - timedelta(minutes=45)
        )
        submission.refresh_from_db()
        return submission

    def test_worker_status_heartbeat_after_detection_is_not_overwritten(self):
        submission = self._make_stale_submission(
            status=Submission.Status.DISPATCHED
        )
        detected = threading.Barrier(2, timeout=10)
        worker_finished = threading.Event()
        errors: list[BaseException] = []

        def detect_then_release_worker(**kwargs):
            alerts = detect_stuck_submissions(**kwargs)
            detected.wait()
            if not worker_finished.wait(timeout=10):
                raise AssertionError("worker heartbeat did not finish")
            return alerts

        def worker_heartbeat() -> None:
            close_old_connections()
            try:
                detected.wait()
                Submission.objects.filter(pk=submission.pk).update(
                    status=Submission.Status.EXTRACTING,
                    updated_at=timezone.now(),
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                worker_finished.set()
                close_old_connections()

        worker = threading.Thread(
            target=worker_heartbeat,
            name="omr-worker-heartbeat",
        )
        worker.start()
        with patch(
            "apps.domains.submissions.omr_pipeline.services.state_recovery."
            "detect_stuck_submissions",
            side_effect=detect_then_release_worker,
        ):
            report = recover_stuck_submissions(actor="test")
        worker.join(timeout=15)

        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(report.recovered, [])
        self.assertEqual(report.skipped, [submission.pk])
        submission.refresh_from_db()
        self.assertEqual(submission.status, Submission.Status.EXTRACTING)
        self.assertEqual(submission.error_message, "")
        self.assertNotIn("state_recovery", submission.meta or {})

    def test_queue_worker_review_save_redelivery_survives_stale_detection(self):
        """One real PostgreSQL flow crosses upload, worker, review, and reload."""
        import cv2

        from academy.adapters.db.django.uow import DjangoUnitOfWork
        from academy.adapters.queue.sqs.ai_queue import SQSAIQueueAdapter
        from academy.application.use_cases.ai.process_ai_job_from_sqs import (
            complete_ai_job,
            prepare_ai_job,
        )
        from academy.framework.workers.ai_sqs_worker import (
            _dispatch_domain_callback,
            _dispatch_terminal_callback_from_message,
            _run_inference,
        )
        from apps.support.ai.services.sqs_queue import AISQSQueue
        from tests.omr.test_omr_realuse import render_marked_pdf

        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=self.user,
            role="teacher",
        )
        student_result = create_student_account(
            tenant=self.tenant,
            student_data={
                "ps_number": f"OMRPG{self.tenant.id:06d}",
                "name": "OMR PostgreSQL Student",
                "phone": "01012345678",
                "parent_phone": "01087654321",
                "omr_code": "12345678",
                "school_type": "HIGH",
            },
            password="test1234",
        )
        lecture = Lecture.objects.create(
            tenant=self.tenant,
            title="OMR PostgreSQL Lecture",
            name="OMR PostgreSQL Lecture",
            subject="MATH",
        )
        session = Session.objects.create(
            lecture=lecture,
            order=1,
            title="OMR PostgreSQL Session",
        )
        enrollment = Enrollment.objects.create(
            tenant=self.tenant,
            student=student_result.student,
            lecture=lecture,
            status="ACTIVE",
        )
        SessionEnrollment.objects.create(
            tenant=self.tenant,
            session=session,
            enrollment=enrollment,
        )
        exam = Exam.objects.create(
            tenant=self.tenant,
            title="OMR PostgreSQL Exam",
            subject="MATH",
            exam_type=Exam.ExamType.REGULAR,
            pass_score=0,
            max_score=100,
            max_attempts=1,
        )
        exam.sessions.add(session)
        ExamEnrollment.objects.create(exam=exam, enrollment=enrollment)
        sheet = Sheet.objects.create(exam=exam, name="MAIN", total_questions=10)
        questions = [ExamQuestion.objects.create(sheet=sheet, number=number, score=10) for number in range(1, 11)]
        marks = {str(number): str(((number - 1) % 5) + 1) for number in range(1, 11)}
        AnswerKey.objects.create(
            exam=exam,
            answers={str(question.id): marks[str(question.number)] for question in questions},
        )

        omr_meta = build_omr_meta(question_count=10, n_choices=5)
        rendered = render_marked_pdf(
            omr_meta,
            marks,
            {},
            dpi=200,
            jpeg_quality=80,
        )
        encoded_ok, encoded = cv2.imencode(".jpg", rendered)
        self.assertTrue(encoded_ok)

        temp_dir = Path(tempfile.mkdtemp(prefix="ai-job-omr-positive-flow-"))
        self.addCleanup(shutil.rmtree, temp_dir, True)
        temp_image = temp_dir / "input.jpg"
        temp_image.write_bytes(encoded.tobytes())

        queue_client = _InProcessSQSClient()
        factory = APIRequestFactory()
        upload_request = factory.post(
            f"/api/v1/submissions/submissions/exams/{exam.id}/omr/batch/",
            data={
                "files": [
                    SimpleUploadedFile(
                        "positive-flow.jpg",
                        encoded.tobytes(),
                        content_type="image/jpeg",
                    )
                ],
                "sheet_id": str(sheet.id),
                "session_id": str(session.id),
            },
            format="multipart",
        )
        force_authenticate(upload_request, user=self.user)
        upload_request.tenant = self.tenant

        with (
            patch(
                "apps.support.ai.services.sqs_queue.get_queue_client",
                return_value=queue_client,
            ),
            patch("apps.domains.submissions.serializers.submission.upload_fileobj_to_r2"),
            patch(
                "apps.support.omr.payload_builder.generate_presigned_get_url",
                return_value="https://isolated.invalid/positive-flow.jpg",
            ),
            patch("apps.domains.submissions.services.dispatcher.start_ai_worker_instance"),
            patch("academy.adapters.compute.ec2_control.ensure_ai_worker_asg_min_capacity"),
            patch(
                "academy.application.use_cases.ai.pipelines.dispatcher.download_to_tmp",
                return_value=str(temp_image),
            ),
            patch(
                "academy.application.use_cases.ai.pipelines.dispatcher._upload_omr_aligned_preview",
                return_value={},
            ),
            patch(
                "academy.adapters.ai.omr.identifier.detect_identifier_v1",
                return_value={
                    "status": "blank",
                    "identifier": None,
                    "raw_identifier": "????????",
                },
            ),
        ):
            upload_response = ExamOMRBatchUploadView.as_view()(
                upload_request,
                exam_id=exam.id,
            )
            self.assertEqual(upload_response.status_code, 201, upload_response.data)
            self.assertEqual(upload_response.data["created_count"], 1)
            submission = Submission.objects.get(pk=upload_response.data["submission_ids"][0])
            self.assertEqual(submission.status, Submission.Status.DISPATCHED)

            queue = SQSAIQueueAdapter()
            queue._impl = AISQSQueue()
            message = queue.receive(tier="basic", wait_time_seconds=0)
            self.assertIsNotNone(message)
            assert message is not None
            self.assertEqual(message["source_id"], str(submission.id))
            self.assertEqual(message["tenant_id"], str(self.tenant.id))
            self.assertEqual(message["job_type"], "omr_grading")

            Submission.objects.filter(pk=submission.pk).update(updated_at=timezone.now() - timedelta(minutes=45))
            detected = threading.Barrier(2, timeout=60)
            worker_finished = threading.Event()
            worker_result: dict = {}
            errors: list[BaseException] = []

            def detect_then_release_worker(**kwargs):
                alerts = detect_stuck_submissions(**kwargs)
                detected.wait()
                if not worker_finished.wait(timeout=60):
                    raise AssertionError("worker callback did not finish")
                return alerts

            def worker() -> None:
                close_old_connections()
                try:
                    detected.wait()
                    prepared = prepare_ai_job(
                        DjangoUnitOfWork(),
                        job_id=message["job_id"],
                        receipt_handle=message["receipt_handle"],
                        tier=message["tier"],
                        payload=message["payload"],
                        job_type=message["job_type"],
                        tenant_id=message["tenant_id"],
                        source_domain=message["source_domain"],
                        source_id=message["source_id"],
                        worker_id="omr-positive-flow-worker",
                    )
                    if prepared is None:
                        raise AssertionError("fresh queue message was not claimed")
                    result = _run_inference(prepared)
                    worker_result["status"] = result.status
                    worker_result["payload"] = result.result
                    if result.status != "DONE":
                        raise AssertionError(result.error or "OMR inference failed")
                    if not complete_ai_job(
                        DjangoUnitOfWork(),
                        prepared.job_id,
                        result.result,
                    ):
                        raise AssertionError("worker completion was not persisted")
                    if not _dispatch_domain_callback(
                        prepared,
                        status="DONE",
                        result_payload=result.result,
                        error=None,
                    ):
                        raise AssertionError("submission callback was not applied")
                except BaseException as exc:  # pragma: no cover - asserted below
                    errors.append(exc)
                finally:
                    worker_finished.set()
                    close_old_connections()

            worker_thread = threading.Thread(
                target=worker,
                name="omr-positive-flow-worker",
            )
            worker_thread.start()
            with patch(
                "apps.domains.submissions.omr_pipeline.services.state_recovery.detect_stuck_submissions",
                side_effect=detect_then_release_worker,
            ):
                recovery_report = recover_stuck_submissions(
                    actor="test.positive_flow",
                    timeouts={Submission.Status.DISPATCHED: 30},
                )
            worker_thread.join(timeout=60)

            self.assertFalse(worker_thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(worker_result["status"], "DONE")
            self.assertEqual(recovery_report.recovered, [])
            self.assertEqual(recovery_report.skipped, [submission.id])
            submission.refresh_from_db()
            self.assertEqual(
                submission.status,
                Submission.Status.NEEDS_IDENTIFICATION,
            )
            self.assertEqual(submission.answers.count(), 10)
            self.assertNotIn("state_recovery", submission.meta or {})

            review_request = factory.get(f"/api/v1/submissions/submissions/{submission.id}/manual-edit/")
            force_authenticate(review_request, user=self.user)
            review_request.tenant = self.tenant
            review_response = SubmissionViewSet.as_view({"get": "manual_edit"})(
                review_request,
                pk=submission.id,
            )
            self.assertEqual(review_response.status_code, 200, review_response.data)
            self.assertEqual(len(review_response.data["answers"]), 10)
            self.assertTrue(review_response.data["meta"]["manual_review"]["required"])

            save_request = factory.post(
                f"/api/v1/submissions/submissions/{submission.id}/manual-edit/",
                data={
                    "identifier": {"enrollment_id": enrollment.id},
                    "answers": [],
                    "note": "postgres_queue_worker_positive_flow",
                },
                format="json",
            )
            force_authenticate(save_request, user=self.user)
            save_request.tenant = self.tenant
            save_response = SubmissionViewSet.as_view({"post": "manual_edit"})(
                save_request,
                pk=submission.id,
            )
            self.assertEqual(save_response.status_code, 200, save_response.data)
            self.assertTrue(save_response.data["graded"])
            self.assertEqual(float(save_response.data["score"]), 100.0)

            redelivered = queue.receive(tier="basic", wait_time_seconds=0)
            self.assertIsNotNone(redelivered)
            assert redelivered is not None
            duplicate_claim = prepare_ai_job(
                DjangoUnitOfWork(),
                job_id=redelivered["job_id"],
                receipt_handle=redelivered["receipt_handle"],
                tier=redelivered["tier"],
                payload=redelivered["payload"],
                job_type=redelivered["job_type"],
                tenant_id=redelivered["tenant_id"],
                source_domain=redelivered["source_domain"],
                source_id=redelivered["source_id"],
                worker_id="omr-positive-flow-redelivery",
            )
            self.assertIsNone(duplicate_claim)
            self.assertTrue(
                _dispatch_terminal_callback_from_message(
                    redelivered["job_id"],
                    redelivered,
                    redelivered["tier"],
                )
            )
            self.assertTrue(queue.delete(redelivered["receipt_handle"], redelivered["tier"]))
            self.assertIsNone(queue.receive(tier="basic", wait_time_seconds=0))

            submission.refresh_from_db()
            self.assertEqual(submission.status, Submission.Status.DONE)
            self.assertEqual(submission.enrollment_id, enrollment.id)
            self.assertEqual(OMRRecognitionRun.objects.filter(submission=submission).count(), 1)
            self.assertEqual(OMRDetectedAnswer.objects.filter(submission=submission).count(), 10)
            self.assertEqual(SubmissionAnswer.objects.filter(submission=submission).count(), 10)
            self.assertEqual(
                OMRStudentMatch.objects.filter(
                    submission=submission,
                    is_current=True,
                    enrollment=enrollment,
                ).count(),
                1,
            )
            self.assertEqual(
                Result.objects.filter(
                    target_type="exam",
                    target_id=exam.id,
                    enrollment=enrollment,
                ).count(),
                1,
            )
            self.assertEqual(
                ResultFact.objects.filter(
                    target_type="exam",
                    target_id=exam.id,
                    enrollment=enrollment,
                    submission_id=submission.id,
                ).count(),
                10,
            )
            self.assertEqual(ExamResult.objects.filter(submission=submission).count(), 1)

            reconnect_request = factory.get(f"/api/v1/submissions/submissions/{submission.id}/manual-edit/")
            force_authenticate(reconnect_request, user=self.user)
            reconnect_request.tenant = self.tenant
            reconnect_response = SubmissionViewSet.as_view({"get": "manual_edit"})(
                reconnect_request,
                pk=submission.id,
            )
            self.assertEqual(reconnect_response.status_code, 200, reconnect_response.data)
            self.assertEqual(reconnect_response.data["submission_status"], Submission.Status.DONE)
            self.assertEqual(reconnect_response.data["enrollment_id"], enrollment.id)
            self.assertEqual(len(reconnect_response.data["answers"]), 10)

            foreign_tenant = Tenant.objects.create(
                name="OMR Foreign Tenant",
                code=f"omr-foreign-{uuid.uuid4().hex[:8]}",
                is_active=True,
            )
            foreign_user = User.objects.create_user(
                username=f"omr-foreign-{uuid.uuid4().hex[:8]}",
                password="test1234",
                tenant=foreign_tenant,
                is_staff=True,
            )
            TenantMembership.ensure_active(
                tenant=foreign_tenant,
                user=foreign_user,
                role="teacher",
            )
            foreign_request = factory.get(f"/api/v1/submissions/submissions/{submission.id}/manual-edit/")
            force_authenticate(foreign_request, user=foreign_user)
            foreign_request.tenant = foreign_tenant
            foreign_response = SubmissionViewSet.as_view({"get": "manual_edit"})(
                foreign_request,
                pk=submission.id,
            )
            self.assertEqual(foreign_response.status_code, 404)

        self.assertFalse(temp_dir.exists())
