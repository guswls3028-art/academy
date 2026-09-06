from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from django.utils import timezone

from academy.adapters.db.django.repositories_ai import DjangoAIJobRepository
from academy.application.use_cases.ai.process_ai_job_from_sqs import PreparedJob
from academy.application.use_cases.ai.pipelines.tier_enforcer import enforce_tier_limits
from academy.application.use_cases.tools.worker_dispatcher import handle_tools_job
from academy.framework.workers.ai_sqs_worker import _run_inference
from apps.domains.ai.models import AIJobModel, AIResultModel
from apps.domains.ai.queueing.publisher import publish_ai_job_sqs
from apps.domains.ai.services.excel_job_secrets import (
    EXCEL_CREDENTIALS_ENVELOPE_FIELD,
)
from apps.shared.contracts.ai_result import AIResult


class ToolWorkerRoutingTests(TestCase):
    def _job(self, job_type: str) -> AIJobModel:
        return AIJobModel.objects.create(
            job_id=str(uuid.uuid4()),
            job_type=job_type,
            status="PENDING",
            tenant_id="1",
            payload={"tenant_id": "1"},
            tier="basic",
            source_domain="tools",
        )

    @override_settings(TOOLS_SQS_QUEUE_NAME="test-tools-queue")
    @patch(
        "academy.adapters.compute.ec2_control.ensure_tools_worker_asg_min_capacity",
        return_value=True,
    )
    @patch("apps.support.ai.services.sqs_queue.get_queue_client")
    def test_deterministic_document_jobs_publish_to_tools_queue(
        self,
        get_queue_client,
        wake_tools_worker,
    ):
        for job_type in (
            "ppt_generation",
            "excel_parsing",
            "attendance_excel_export",
            "staff_excel_export",
            "problem_studio_transfer",
            "problem_review_export",
            "wrong_note_pdf_generation",
        ):
            with self.subTest(job_type=job_type):
                client = Mock()
                client.send_message.return_value = True
                get_queue_client.return_value = client

                assert publish_ai_job_sqs(self._job(job_type))

                client.send_message.assert_called_once()
                assert client.send_message.call_args.kwargs["queue_name"] == "test-tools-queue"
        assert wake_tools_worker.call_count == 7

    @override_settings(TOOLS_SQS_QUEUE_NAME="test-tools-queue")
    @patch("apps.support.ai.services.sqs_queue.get_queue_client")
    def test_failed_tool_enqueue_does_not_claim_success_or_wake_worker(
        self,
        get_queue_client,
    ):
        client = Mock()
        client.send_message.return_value = False
        get_queue_client.return_value = client

        with patch(
            "academy.adapters.compute.ec2_control.ensure_tools_worker_asg_min_capacity"
        ) as wake_tools_worker:
            assert not publish_ai_job_sqs(self._job("wrong_note_pdf_generation"))

        get_queue_client.assert_called_once_with(request_timeout_seconds=3)
        wake_tools_worker.assert_not_called()

    @patch("apps.support.ai.services.sqs_queue.get_queue_client")
    def test_non_tool_job_stays_on_ai_queue(self, get_queue_client):
        client = Mock()
        client.send_message.return_value = True
        get_queue_client.return_value = client

        assert publish_ai_job_sqs(self._job("ocr"))

        get_queue_client.assert_called_once_with(request_timeout_seconds=3)
        client.send_message.assert_called_once()
        assert client.send_message.call_args.kwargs["queue_name"] == "test-ai-queue"

    @patch("apps.support.ai.services.sqs_queue.get_queue_client")
    def test_problem_studio_transcription_stays_on_ai_queue(self, get_queue_client):
        client = Mock()
        client.send_message.return_value = True
        get_queue_client.return_value = client

        assert publish_ai_job_sqs(self._job("problem_studio_transcription"))

        client.send_message.assert_called_once()
        assert client.send_message.call_args.kwargs["queue_name"] == "test-ai-queue"

    @patch("apps.support.ai.services.sqs_queue.get_queue_client")
    def test_problem_review_analysis_stays_on_ai_queue(self, get_queue_client):
        client = Mock()
        client.send_message.return_value = True
        get_queue_client.return_value = client

        assert publish_ai_job_sqs(self._job("problem_review_analysis"))

        client.send_message.assert_called_once()
        assert client.send_message.call_args.kwargs["queue_name"] == "test-ai-queue"

    def test_ai_job_execution_timestamps_are_preserved_after_completion(self):
        job = self._job("ppt_generation")
        repo = DjangoAIJobRepository()
        started = timezone.now()

        assert repo.mark_running(
            job.job_id,
            "tools-sqs-worker",
            started + timedelta(minutes=30),
            started,
        )
        assert repo.mark_done(job.job_id, timezone.now(), {"slide_count": 1})

        job.refresh_from_db()
        assert job.started_at is not None
        assert job.completed_at is not None
        assert job.started_at <= job.completed_at

    def test_completed_at_is_backfilled_for_idempotent_done_mark(self):
        job = self._job("ppt_generation")
        job.status = "DONE"
        job.completed_at = None
        job.save(update_fields=["status", "completed_at", "updated_at"])

        assert DjangoAIJobRepository().mark_done(job.job_id, timezone.now(), {"slide_count": 1})

        job.refresh_from_db()
        assert job.completed_at is not None

    @patch("apps.domains.ai.redis_status_cache.cache_job_status")
    def test_excel_random_credentials_are_encrypted_and_plaintext_payload_is_scrubbed(
        self,
        cache_status,
    ):
        job = self._job("excel_parsing")
        job.payload = {
            **job.payload,
            "initial_password": "plain-fixed-password",
        }
        job.save(update_fields=["payload", "updated_at"])
        result = {
            "created": 1,
            "credentials": [{
                "name": "보호학생",
                "login_id": "student-1",
                "password": "0042",
            }],
        }

        repo = DjangoAIJobRepository()
        started = timezone.now()
        assert repo.mark_running(
            job.job_id,
            "tools-sqs-worker",
            started + timedelta(minutes=30),
            started,
        )
        with self.captureOnCommitCallbacks(execute=True):
            assert repo.mark_done(job.job_id, timezone.now(), result)

        stored = AIResultModel.objects.get(job=job).payload
        assert stored["created"] == 1
        assert EXCEL_CREDENTIALS_ENVELOPE_FIELD in stored
        assert "0042" not in str(stored)
        assert repo.get_result_payload_for_job(job) == {"created": 1}
        assert repo.get_result_payload_for_job(
            job,
            include_excel_credentials=True,
        ) == result
        job.refresh_from_db()
        assert "initial_password" not in job.payload
        assert cache_status.call_args.kwargs["result"] == {"created": 1}
        assert cache_status.call_args.kwargs["ttl"] is None

    def test_tools_inference_handler_bypasses_ai_dispatcher_imports(self):
        prepared = PreparedJob(
            job_id=str(uuid.uuid4()),
            job_type="excel_parsing",
            tier="basic",
            payload={"tenant_id": "1", "file_key": "excel/1/test.xlsx"},
            receipt_handle="receipt",
            tenant_id="1",
            source_domain="tools",
        )

        real_import = __import__

        def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "academy.application.use_cases.ai.pipelines.dispatcher":
                raise AssertionError("tools worker must not import AI dispatcher")
            return real_import(name, globals, locals, fromlist, level)

        with (
            patch("builtins.__import__", guarded_import),
            patch(
                "academy.application.use_cases.ai.pipelines.excel_handler.handle_excel_parsing_job",
                return_value=AIResult.done(prepared.job_id, {"ok": True}),
            ),
        ):
            result = _run_inference(
                prepared,
                inference_handler=handle_tools_job,
            )

        assert result.status == "DONE"
        assert result.result == {"ok": True}

    def test_tools_dispatcher_rejects_unknown_job_type(self):
        prepared = PreparedJob(
            job_id=str(uuid.uuid4()),
            job_type="unknown_tool",
            tier="premium",
            payload={"tenant_id": "1"},
            receipt_handle="receipt",
            tenant_id="1",
            source_domain="tools",
        )

        result = _run_inference(prepared, inference_handler=handle_tools_job)

        assert result.status == "FAILED"
        assert "Unsupported tools job type" in (result.error or "")

    def test_basic_tier_allows_deterministic_tools_jobs(self):
        for job_type in (
            "attendance_excel_export",
            "staff_excel_export",
            "problem_studio_transfer",
            "problem_review_export",
            "wrong_note_pdf_generation",
        ):
            with self.subTest(job_type=job_type):
                allowed, error = enforce_tier_limits(tier="basic", job_type=job_type)

                assert allowed
                assert error is None

        allowed, error = enforce_tier_limits(tier="basic", job_type="problem_studio_transcription")
        assert allowed
        assert error is None

        allowed, error = enforce_tier_limits(tier="basic", job_type="problem_review_analysis")
        assert allowed
        assert error is None
