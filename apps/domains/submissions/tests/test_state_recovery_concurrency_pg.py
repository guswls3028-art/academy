from __future__ import annotations

import threading
import unittest
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection
from django.test import TransactionTestCase
from django.utils import timezone

from apps.core.models import Tenant
from apps.domains.submissions.models import Submission
from apps.domains.submissions.omr_pipeline.services.state_recovery import (
    detect_stuck_submissions,
    recover_stuck_submissions,
)


User = get_user_model()


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
