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
from apps.domains.results.models import ExamAttempt, Result, ResultFact, ScoreEditDraft
from apps.domains.results.views.admin_representative_attempt_view import (
    AdminRepresentativeAttemptView,
)
from apps.domains.results.views.admin_exam_total_score_view import (
    AdminExamTotalScoreView,
)
from apps.support.results.admin_exam_dependencies import (
    lock_regular_active_exam_for_tenant,
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

    def test_representative_switch_and_total_write_share_exam_result_attempt_lock_order(self):
        first_attempt = ExamAttempt.objects.create(
            exam=self.exam,
            enrollment=self.enrollment,
            submission_id=11,
            attempt_index=1,
            is_retake=False,
            is_representative=False,
            status="done",
            meta={"total_score": 70.0, "max_score": 100.0},
        )
        second_attempt = ExamAttempt.objects.create(
            exam=self.exam,
            enrollment=self.enrollment,
            submission_id=12,
            attempt_index=2,
            is_retake=True,
            is_representative=True,
            status="done",
            meta={"total_score": 80.0, "max_score": 100.0},
        )
        Result.objects.create(
            target_type="exam",
            target_id=self.exam.id,
            enrollment=self.enrollment,
            attempt=second_attempt,
            total_score=80,
            max_score=100,
        )
        ResultFact.objects.create(
            target_type="exam",
            target_id=self.exam.id,
            enrollment=self.enrollment,
            submission_id=first_attempt.submission_id,
            attempt=first_attempt,
            question_id=0,
            answer="",
            is_correct=True,
            score=70,
            max_score=100,
            source="manual_total",
            meta={"manual_total": True},
        )

        representative_locked_exam = threading.Event()
        total_started = threading.Event()
        responses: list[tuple[str, int]] = []
        errors: list[str] = []

        def pausing_representative_exam_lock(*, exam_id, tenant):
            exam = lock_regular_active_exam_for_tenant(
                exam_id=exam_id,
                tenant=tenant,
            )
            representative_locked_exam.set()
            if not total_started.wait(timeout=5):
                raise AssertionError("total score writer did not start")
            time.sleep(0.2)
            return exam

        def representative_writer() -> None:
            close_old_connections()
            try:
                tenant = Tenant.objects.get(id=self.tenant.id)
                admin = User.objects.get(id=self.admin.id)
                request = APIRequestFactory().post(
                    "/results/admin/exams/representative-attempt/",
                    {
                        "enrollment_id": self.enrollment.id,
                        "attempt_id": first_attempt.id,
                    },
                    format="json",
                )
                request.tenant = tenant
                force_authenticate(request, user=admin)
                response = AdminRepresentativeAttemptView.as_view()(
                    request,
                    exam_id=self.exam.id,
                )
                responses.append(("representative", response.status_code))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"representative: {exc!r}")
            finally:
                close_old_connections()

        def total_writer() -> None:
            close_old_connections()
            try:
                if not representative_locked_exam.wait(timeout=5):
                    raise AssertionError("representative writer did not lock exam")
                tenant = Tenant.objects.get(id=self.tenant.id)
                admin = User.objects.get(id=self.admin.id)
                request = APIRequestFactory().patch(
                    "/results/admin/exams/manual/",
                    {"score": 75, "max_score": 100},
                    format="json",
                    HTTP_X_SCORE_EDITOR_CLIENT="exam-max-lock",
                    HTTP_X_SCORE_SESSION_ID=str(self.session.id),
                )
                request.tenant = tenant
                force_authenticate(request, user=admin)
                total_started.set()
                response = AdminExamTotalScoreView.as_view()(
                    request,
                    exam_id=self.exam.id,
                    enrollment_id=self.enrollment.id,
                )
                responses.append(("total", response.status_code))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"total: {exc!r}")
            finally:
                close_old_connections()

        with (
            patch(
                "apps.domains.results.views.admin_representative_attempt_view."
                "lock_regular_active_exam_for_tenant",
                side_effect=pausing_representative_exam_lock,
            ),
            patch(
                "apps.domains.results.views.admin_representative_attempt_view."
                "get_latest_session_submission_id",
                return_value=99,
            ),
            patch(
                "apps.domains.results.views.admin_representative_attempt_view."
                "dispatch_progress_pipeline"
            ),
            patch(
                "apps.domains.results.views.admin_exam_total_score_view."
                "dispatch_progress_pipeline"
            ),
        ):
            representative = threading.Thread(target=representative_writer)
            total = threading.Thread(target=total_writer)
            representative.start()
            total.start()
            representative.join(timeout=10)
            total.join(timeout=10)

        self.assertFalse(representative.is_alive(), "representative writer deadlocked")
        self.assertFalse(total.is_alive(), "total score writer deadlocked")
        self.assertEqual(errors, [])
        self.assertEqual(
            sorted(responses),
            [("representative", 200), ("total", 200)],
        )
        result = Result.objects.get(
            target_type="exam",
            target_id=self.exam.id,
            enrollment=self.enrollment,
        )
        self.assertEqual(result.attempt_id, first_attempt.id)
        self.assertEqual(result.total_score, 75.0)
