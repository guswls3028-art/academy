from __future__ import annotations

from unittest import mock

from django.test import TestCase

from academy.framework.workers import ai_sqs_worker
from apps.core.models import Tenant
from apps.domains.ai.models import AIJobModel, AIResultModel
from apps.shared.contracts.ai_result import AIResult


class _OneMessageQueue:
    def __init__(self, message: dict):
        self._message = message
        self.deleted = False

    def receive(self, *, tier: str, wait_time_seconds: int):
        message = self._message
        self._message = None
        if message is None:
            ai_sqs_worker._shutdown = True
        return message

    def delete(self, receipt_handle: str, tier: str) -> bool:
        self.deleted = True
        return True

    def extend_visibility(self, receipt_handle: str, tier: str, timeout: int) -> bool:
        return True


class AISQSWorkerCallbackTests(TestCase):
    def setUp(self):
        self.close_old_connections_patcher = mock.patch.object(
            ai_sqs_worker,
            "close_old_connections",
        )
        self.close_old_connections = self.close_old_connections_patcher.start()
        self.addCleanup(self.close_old_connections_patcher.stop)
        self.release_connections_patcher = mock.patch.object(
            ai_sqs_worker,
            "_release_db_connections",
        )
        self.release_connections = self.release_connections_patcher.start()
        self.addCleanup(self.release_connections_patcher.stop)
        self.tenant = Tenant.objects.create(name="AI Callback", code="ai-cb", is_active=True)

    def tearDown(self):
        ai_sqs_worker._shutdown = False
        ai_sqs_worker._current_receipt_handle = None

    def _message_for(self, job: AIJobModel) -> dict:
        return {
            "receipt_handle": f"rh-{job.job_id}",
            "job_id": job.job_id,
            "job_type": job.job_type,
            "tier": job.tier,
            "tenant_id": str(self.tenant.id),
            "source_domain": "matchup",
            "source_id": "123",
            "payload": {},
        }

    def test_callback_failure_keeps_terminal_result_and_message_for_retry(self):
        job = AIJobModel.objects.create(
            job_id="callback-failure",
            job_type="ocr",
            status="PENDING",
            tenant_id=str(self.tenant.id),
            tier="basic",
        )
        queue = _OneMessageQueue(self._message_for(job))

        def inference_handler(contract_job):
            ai_sqs_worker._shutdown = True
            return AIResult.done(contract_job.id, {"ok": True})

        with mock.patch(
            "apps.domains.ai.callbacks.dispatch_ai_result_to_domain",
            side_effect=RuntimeError("domain write failed"),
        ):
            exit_code = ai_sqs_worker.run_ai_sqs_worker(
                queue=queue,
                inference_handler=inference_handler,
            )

        job.refresh_from_db()
        self.assertEqual(exit_code, 0)
        self.assertFalse(queue.deleted)
        self.assertEqual(job.status, "DONE")
        self.assertTrue(AIResultModel.objects.filter(job=job, payload={"ok": True}).exists())

    def test_terminal_redelivery_retries_callback_and_deletes_message(self):
        job = AIJobModel.objects.create(
            job_id="callback-redelivery",
            job_type="ocr",
            status="DONE",
            tenant_id=str(self.tenant.id),
            tier="basic",
            source_domain="matchup",
            source_id="123",
        )
        AIResultModel.objects.create(job=job, payload={"ok": True})
        queue = _OneMessageQueue(self._message_for(job))

        inference_handler = mock.Mock(side_effect=AssertionError("terminal job must not rerun inference"))
        with mock.patch("apps.domains.ai.callbacks.dispatch_ai_result_to_domain") as dispatch:
            exit_code = ai_sqs_worker.run_ai_sqs_worker(
                queue=queue,
                inference_handler=inference_handler,
            )

        self.assertEqual(exit_code, 0)
        self.assertTrue(queue.deleted)
        inference_handler.assert_not_called()
        dispatch.assert_called_once_with(
            job_id="callback-redelivery",
            status="DONE",
            result_payload={"ok": True},
            error=None,
            source_domain="matchup",
            source_id="123",
            tier="basic",
        )

    def test_callback_success_completes_and_deletes_message(self):
        job = AIJobModel.objects.create(
            job_id="callback-success",
            job_type="ocr",
            status="PENDING",
            tenant_id=str(self.tenant.id),
            tier="basic",
        )
        queue = _OneMessageQueue(self._message_for(job))

        def inference_handler(contract_job):
            ai_sqs_worker._shutdown = True
            return AIResult.done(contract_job.id, {"ok": True})

        with mock.patch("apps.domains.ai.callbacks.dispatch_ai_result_to_domain"):
            exit_code = ai_sqs_worker.run_ai_sqs_worker(
                queue=queue,
                inference_handler=inference_handler,
            )

        job.refresh_from_db()
        self.assertEqual(exit_code, 0)
        self.assertTrue(queue.deleted)
        self.assertEqual(job.status, "DONE")
        self.assertTrue(AIResultModel.objects.filter(job=job, payload={"ok": True}).exists())

    def test_basic_failure_callback_uses_persisted_status_and_error(self):
        job = AIJobModel.objects.create(
            job_id="callback-basic-failure",
            job_type="ocr",
            status="PENDING",
            tenant_id=str(self.tenant.id),
            tier="basic",
        )
        queue = _OneMessageQueue(self._message_for(job))

        def inference_handler(contract_job):
            ai_sqs_worker._shutdown = True
            return AIResult.failed(contract_job.id, "provider-timeout")

        with mock.patch(
            "apps.domains.ai.callbacks.dispatch_ai_result_to_domain"
        ) as dispatch:
            exit_code = ai_sqs_worker.run_ai_sqs_worker(
                queue=queue,
                inference_handler=inference_handler,
            )

        job.refresh_from_db()
        self.assertEqual(exit_code, 0)
        self.assertTrue(queue.deleted)
        self.assertEqual(job.status, "DONE")
        self.assertEqual(job.error_message, "provider-timeout")
        dispatch.assert_called_once_with(
            job_id=job.job_id,
            status=job.status,
            result_payload={},
            error=job.error_message,
            source_domain="matchup",
            source_id="123",
            tier="basic",
        )
