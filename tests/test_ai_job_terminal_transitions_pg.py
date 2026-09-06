from __future__ import annotations

import threading
import unittest
import uuid
from queue import Queue
from unittest.mock import patch

from django.db import connection, connections, transaction
from django.test import TransactionTestCase
from django.utils import timezone

from academy.adapters.db.django.uow import DjangoUnitOfWork
from academy.application.use_cases.ai.process_ai_job_from_sqs import (
    complete_ai_job,
    fail_ai_job,
)
from academy.framework.workers.ai_sqs_worker import (
    _dispatch_terminal_callback_from_message,
)
from apps.domains.ai.models import AIJobModel, AIResultModel


class AIJobTerminalTransitionPostgresTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        if connection.vendor != "postgresql":
            raise unittest.SkipTest(
                "PostgreSQL is required for AI job terminal-state concurrency verification."
            )

    def _job(self, *, status: str = "RUNNING", tier: str = "premium") -> AIJobModel:
        now = timezone.now()
        return AIJobModel.objects.create(
            job_id=str(uuid.uuid4()),
            job_type="matchup_analysis",
            status=status,
            tenant_id="1",
            source_domain="matchup",
            source_id="1",
            tier=tier,
            locked_by="ai-sqs-worker" if status == "RUNNING" else None,
            locked_at=now if status == "RUNNING" else None,
            lease_expires_at=(
                now + timezone.timedelta(minutes=30)
                if status == "RUNNING"
                else None
            ),
            completed_at=now if status != "RUNNING" else None,
            error_message="original-error" if status != "RUNNING" else "",
            last_error="original-error" if status != "RUNNING" else "",
        )

    @patch("apps.domains.ai.redis_status_cache.cache_job_status", return_value=True)
    def test_failed_and_rejected_jobs_cannot_become_done(self, _cache_status) -> None:
        for terminal_status in ("FAILED", "REJECTED_BAD_INPUT"):
            with self.subTest(status=terminal_status):
                job = self._job(status=terminal_status)
                completed_at = job.completed_at

                accepted = complete_ai_job(
                    DjangoUnitOfWork(),
                    job.job_id,
                    {"winner": "late-success"},
                )

                self.assertFalse(accepted)
                job.refresh_from_db()
                self.assertEqual(job.status, terminal_status)
                self.assertEqual(job.error_message, "original-error")
                self.assertEqual(job.completed_at, completed_at)
                self.assertFalse(AIResultModel.objects.filter(job=job).exists())

    @patch("apps.domains.ai.redis_status_cache.cache_job_status", return_value=True)
    def test_done_job_cannot_become_failed(self, _cache_status) -> None:
        job = self._job(status="DONE")
        result = AIResultModel.objects.create(job=job, payload={"winner": "success"})
        completed_at = job.completed_at

        accepted = fail_ai_job(
            DjangoUnitOfWork(),
            job.job_id,
            "late-failure",
            tier="premium",
        )

        self.assertFalse(accepted)
        job.refresh_from_db()
        result.refresh_from_db()
        self.assertEqual(job.status, "DONE")
        self.assertEqual(job.error_message, "original-error")
        self.assertEqual(job.completed_at, completed_at)
        self.assertEqual(result.payload, {"winner": "success"})

    @patch("apps.domains.ai.redis_status_cache.cache_job_status", return_value=True)
    def test_concurrent_complete_and_fail_have_exactly_one_winner(
        self,
        _cache_status,
    ) -> None:
        job = self._job()
        barrier = threading.Barrier(2, timeout=10)
        outcomes: Queue[tuple[str, bool]] = Queue()
        errors: Queue[BaseException] = Queue()

        def run(name: str) -> None:
            connections.close_all()
            try:
                barrier.wait()
                if name == "complete":
                    accepted = complete_ai_job(
                        DjangoUnitOfWork(),
                        job.job_id,
                        {"winner": "success"},
                    )
                else:
                    accepted = fail_ai_job(
                        DjangoUnitOfWork(),
                        job.job_id,
                        "failure-winner",
                        tier="premium",
                    )
                outcomes.put((name, accepted))
            except BaseException as exc:  # noqa: BLE001 - surfaced in parent thread
                errors.put(exc)
            finally:
                connections.close_all()

        threads = [
            threading.Thread(target=run, args=("complete",)),
            threading.Thread(target=run, args=("fail",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        if not errors.empty():
            raise errors.get()

        results = dict(outcomes.get() for _ in range(outcomes.qsize()))
        self.assertEqual(set(results), {"complete", "fail"})
        self.assertEqual(sum(results.values()), 1)

        job.refresh_from_db()
        self.assertIn(job.status, {"DONE", "FAILED"})
        result_rows = list(AIResultModel.objects.filter(job=job).values_list("payload", flat=True))
        if results["complete"]:
            self.assertEqual(job.status, "DONE")
            self.assertEqual(result_rows, [{"winner": "success"}])
        else:
            self.assertEqual(job.status, "FAILED")
            self.assertEqual(result_rows, [])

    def test_redis_and_callback_use_only_the_committed_database_terminal_state(self) -> None:
        job = self._job(tier="basic")
        message = {
            "job_id": job.job_id,
            "tenant_id": job.tenant_id,
            "source_domain": job.source_domain,
            "source_id": job.source_id,
            "tier": job.tier,
        }

        with patch(
            "apps.domains.ai.redis_status_cache.cache_job_status",
            return_value=True,
        ) as cache_status:
            with transaction.atomic():
                self.assertTrue(
                    fail_ai_job(
                        DjangoUnitOfWork(),
                        job.job_id,
                        "rolled-back-failure",
                        tier="basic",
                    )
                )
                transaction.set_rollback(True)

            self.assertFalse(cache_status.called)
            job.refresh_from_db()
            self.assertEqual(job.status, "RUNNING")

            self.assertTrue(
                fail_ai_job(
                    DjangoUnitOfWork(),
                    job.job_id,
                    "committed-failure",
                    tier="basic",
                )
            )
            job.refresh_from_db()
            cached_status = cache_status.call_args.kwargs["status"]
            self.assertEqual(cached_status, job.status)

        with patch(
            "academy.framework.workers.ai_sqs_worker._dispatch_domain_callback",
            return_value=True,
        ) as dispatch_callback:
            self.assertTrue(
                _dispatch_terminal_callback_from_message(job.job_id, message, job.tier)
            )

        callback_status = dispatch_callback.call_args.kwargs["status"]
        self.assertEqual(callback_status, job.status)
        self.assertEqual(callback_status, cached_status)
