"""PostgreSQL concurrency coverage for exam max-score edits and score writes."""

from __future__ import annotations

import threading
import time
import unittest
import uuid
from unittest.mock import patch

import pytest
from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.core.models import Tenant, TenantMembership
from apps.domains.results.models import ExamAttempt, Result, ScoreEditDraft
from apps.domains.results.views.admin_exam_objective_score_view import (
    AdminExamObjectiveScoreView,
)
from apps.domains.results.views.admin_exam_total_score_view import (
    AdminExamTotalScoreView,
)


pytestmark = pytest.mark.django_db(transaction=True)
User = get_user_model()
Enrollment = apps.get_model("enrollment", "Enrollment")
SessionEnrollment = apps.get_model("enrollment", "SessionEnrollment")
Exam = apps.get_model("exams", "Exam")
ExamEnrollment = apps.get_model("exams", "ExamEnrollment")
Lecture = apps.get_model("lectures", "Lecture")
Session = apps.get_model("lectures", "Session")
Student = apps.get_model("students", "Student")


class ExamMaxScoreConcurrencyPGTests(TransactionTestCase):
    """A score write waits for an in-flight max-score policy update."""

    @classmethod
    def setUpClass(cls):
        if connection.vendor != "postgresql":
            raise unittest.SkipTest(
                "PostgreSQL row-level locking is required for this regression test."
            )
        super().setUpClass()

    def setUp(self):
        suffix = uuid.uuid4().hex[:8]
        self.tenant = Tenant.objects.create(
            name=f"Exam max lock {suffix}",
            code=f"exam_max_lock_{suffix}",
            is_active=True,
        )
        self.admin = User.objects.create_user(
            username=f"exam-max-lock-{suffix}",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=self.admin,
            role="admin",
        )
        lecture = Lecture.objects.create(
            tenant=self.tenant,
            title="Exam max lock lecture",
            name="Exam max lock lecture",
            subject="MATH",
        )
        self.session = Session.objects.create(
            lecture=lecture,
            order=1,
            title="Session 1",
        )
        self.exam = Exam.objects.create(
            tenant=self.tenant,
            title="Exam max lock",
            exam_type=Exam.ExamType.REGULAR,
            max_score=100,
            pass_score=60,
        )
        self.exam.sessions.add(self.session)
        student_user = User.objects.create_user(
            username=f"exam-max-student-{suffix}",
            tenant=self.tenant,
        )
        student = Student.objects.create(
            tenant=self.tenant,
            user=student_user,
            name="Exam max student",
            ps_number=f"EM{suffix}",
        )
        self.enrollment = Enrollment.objects.create(
            tenant=self.tenant,
            lecture=lecture,
            student=student,
            status="ACTIVE",
        )
        SessionEnrollment.objects.create(
            tenant=self.tenant,
            session=self.session,
            enrollment=self.enrollment,
        )
        ExamEnrollment.objects.create(
            exam=self.exam,
            enrollment=self.enrollment,
        )
        ScoreEditDraft.objects.create(
            tenant=self.tenant,
            session=self.session,
            editor_user=self.admin,
            payload={"client_id": "exam-max-lock", "changes": []},
        )

    @patch(
        "apps.domains.results.views.admin_exam_total_score_view.dispatch_progress_pipeline"
    )
    def test_score_write_waits_for_committed_exam_max(self, dispatch_progress_pipeline):
        exam_locked = threading.Event()
        score_started = threading.Event()
        responses: list[tuple[int, float]] = []
        errors: list[str] = []

        def policy_writer() -> None:
            close_old_connections()
            try:
                with transaction.atomic():
                    exam = Exam.objects.select_for_update().get(
                        id=self.exam.id,
                        tenant_id=self.tenant.id,
                    )
                    exam.max_score = 105
                    exam.save(update_fields=["max_score", "updated_at"])
                    exam_locked.set()
                    if not score_started.wait(timeout=5):
                        raise AssertionError("score writer did not start")
                    time.sleep(0.2)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"policy: {exc!r}")
            finally:
                close_old_connections()

        def score_writer() -> None:
            close_old_connections()
            try:
                if not exam_locked.wait(timeout=5):
                    raise AssertionError("policy writer did not lock exam")
                tenant = Tenant.objects.get(id=self.tenant.id)
                admin = User.objects.get(id=self.admin.id)
                request = APIRequestFactory().patch(
                    "/results/admin/exams/manual/",
                    {"score": 97, "max_score": 100},
                    format="json",
                    HTTP_X_SCORE_EDITOR_CLIENT="exam-max-lock",
                    HTTP_X_SCORE_SESSION_ID=str(self.session.id),
                )
                request.tenant = tenant
                force_authenticate(request, user=admin)
                score_started.set()
                response = AdminExamTotalScoreView.as_view()(
                    request,
                    exam_id=self.exam.id,
                    enrollment_id=self.enrollment.id,
                )
                responses.append(
                    (response.status_code, float(response.data["max_score"]))
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"score: {exc!r}")
            finally:
                close_old_connections()

        policy = threading.Thread(target=policy_writer)
        score = threading.Thread(target=score_writer)
        policy.start()
        score.start()
        policy.join(timeout=10)
        score.join(timeout=10)

        self.assertFalse(policy.is_alive(), "policy writer did not finish")
        self.assertFalse(score.is_alive(), "score writer did not finish")
        self.assertEqual(errors, [])
        self.assertEqual(responses, [(200, 105.0)])
        result = Result.objects.get(
            target_type="exam",
            target_id=self.exam.id,
            enrollment=self.enrollment,
        )
        self.assertEqual(result.max_score, 105.0)

    @patch(
        "apps.domains.results.views.admin_exam_objective_score_view."
        "dispatch_progress_pipeline"
    )
    def test_objective_write_waits_for_committed_exam_max(
        self,
        _dispatch_progress_pipeline,
    ):
        attempt = ExamAttempt.objects.create(
            exam=self.exam,
            enrollment=self.enrollment,
            submission_id=0,
            attempt_index=1,
            is_retake=False,
            is_representative=True,
            status="done",
            meta={"total_score": 70.0, "max_score": 100.0},
        )
        Result.objects.create(
            target_type="exam",
            target_id=self.exam.id,
            enrollment=self.enrollment,
            attempt=attempt,
            total_score=70,
            max_score=100,
            objective_score=70,
        )
        exam_locked = threading.Event()
        objective_started = threading.Event()
        responses: list[tuple[int, float]] = []
        errors: list[str] = []

        def policy_writer() -> None:
            close_old_connections()
            try:
                with transaction.atomic():
                    exam = Exam.objects.select_for_update().get(
                        id=self.exam.id,
                        tenant_id=self.tenant.id,
                    )
                    exam.max_score = 85
                    exam.save(update_fields=["max_score", "updated_at"])
                    exam_locked.set()
                    if not objective_started.wait(timeout=5):
                        raise AssertionError("objective writer did not start")
                    time.sleep(0.2)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"policy: {exc!r}")
            finally:
                close_old_connections()

        def objective_writer() -> None:
            close_old_connections()
            try:
                if not exam_locked.wait(timeout=5):
                    raise AssertionError("policy writer did not lock exam")
                tenant = Tenant.objects.get(id=self.tenant.id)
                admin = User.objects.get(id=self.admin.id)
                request = APIRequestFactory().patch(
                    "/results/admin/exams/manual/",
                    {"score": 80},
                    format="json",
                    HTTP_X_SCORE_EDITOR_CLIENT="exam-max-lock",
                    HTTP_X_SCORE_SESSION_ID=str(self.session.id),
                )
                request.tenant = tenant
                force_authenticate(request, user=admin)
                objective_started.set()
                response = AdminExamObjectiveScoreView.as_view()(
                    request,
                    exam_id=self.exam.id,
                    enrollment_id=self.enrollment.id,
                )
                responses.append(
                    (response.status_code, float(response.data["max_score"]))
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"objective: {exc!r}")
            finally:
                close_old_connections()

        policy = threading.Thread(target=policy_writer)
        objective = threading.Thread(target=objective_writer)
        policy.start()
        objective.start()
        policy.join(timeout=10)
        objective.join(timeout=10)

        self.assertFalse(policy.is_alive(), "policy writer did not finish")
        self.assertFalse(objective.is_alive(), "objective writer did not finish")
        self.assertEqual(errors, [])
        self.assertEqual(responses, [(200, 85.0)])
        result = Result.objects.get(
            target_type="exam",
            target_id=self.exam.id,
            enrollment=self.enrollment,
        )
        self.assertEqual(result.total_score, 80.0)
        self.assertEqual(result.max_score, 85.0)
