"""PostgreSQL lock-order coverage for mixed OMR grading and manual essay input."""

from __future__ import annotations

import threading
import unittest
import uuid
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection
from django.test import TransactionTestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.core.models import Tenant, TenantMembership
from apps.domains.exams.views.exam_recalculate_view import ExamRecalculateView
from apps.domains.results.guards.score_edit_lease_state import (
    score_edit_payload_is_invalidated,
)
from apps.domains.results.models import ExamResult, Result, ResultFact, ScoreEditDraft
from apps.domains.results.services.exam_grading_service import ExamGradingService
from apps.domains.results.services.grading_service import grade_submission
from apps.domains.results.views.admin_exam_subjective_score_view import (
    AdminExamSubjectiveScoreView,
)
from apps.domains.results.views.admin_exam_attempts_view import AdminExamAttemptsView
from apps.domains.results.views.score_draft_view import ScoreDraftView
from apps.domains.results.views.session_scores_view import SessionScoresView
from apps.domains.submissions.views.submission_view import SubmissionViewSet
from apps.support.results.tests.omr_subjective_completion_fixtures import (
    AnswerKey,
    Enrollment,
    Exam,
    ExamEnrollment,
    ExamQuestion,
    Lecture,
    ProgressPolicy,
    Session,
    SessionEnrollment,
    SessionProgress,
    Sheet,
    Student,
    Submission,
    SubmissionAnswer,
)


pytestmark = pytest.mark.django_db(transaction=True)
User = get_user_model()


class MixedOmrGradingConcurrencyPostgresTests(TransactionTestCase):
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
            code=f"mixed_omr_lock_{suffix}",
            name=f"Mixed OMR lock {suffix}",
            is_active=True,
        )
        self.staff = User.objects.create_user(
            username=f"mixed-omr-lock-{suffix}",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=self.staff,
            role="admin",
        )
        student_user = User.objects.create_user(
            username=f"mixed-omr-lock-student-{suffix}",
            tenant=self.tenant,
        )
        student = Student.objects.create(
            tenant=self.tenant,
            user=student_user,
            name="Mixed OMR lock student",
            ps_number=f"LOCK-{suffix}",
            omr_code="87654321",
        )
        self.lecture = Lecture.objects.create(
            tenant=self.tenant,
            title="Mixed OMR lock lecture",
            name="Mixed OMR lock lecture",
            subject="MATH",
        )
        self.session = Session.objects.create(
            lecture=self.lecture,
            order=1,
            title="Mixed OMR lock session",
        )
        self.enrollment = Enrollment.objects.create(
            tenant=self.tenant,
            student=student,
            lecture=self.lecture,
            status="ACTIVE",
        )
        SessionEnrollment.objects.create(
            tenant=self.tenant,
            session=self.session,
            enrollment=self.enrollment,
        )
        self.exam = Exam.objects.create(
            tenant=self.tenant,
            title="Mixed OMR lock exam",
            exam_type=Exam.ExamType.REGULAR,
            grading_mode=Exam.GradingMode.CHOICE,
            pass_score=90,
            max_score=100,
            student_results_published=True,
        )
        self.exam.sessions.add(self.session)
        ExamEnrollment.objects.create(exam=self.exam, enrollment=self.enrollment)
        sheet = Sheet.objects.create(
            exam=self.exam,
            name="MAIN",
            total_questions=2,
            choice_count=1,
            essay_count=1,
        )
        self.choice = ExamQuestion.objects.create(
            sheet=sheet,
            number=1,
            score=80,
            question_kind=ExamQuestion.QuestionKind.CHOICE,
        )
        self.essay = ExamQuestion.objects.create(
            sheet=sheet,
            number=2,
            score=20,
            question_kind=ExamQuestion.QuestionKind.ESSAY,
        )
        AnswerKey.objects.create(
            exam=self.exam,
            answers={str(self.choice.id): "1", str(self.essay.id): "해설참조"},
        )
        ProgressPolicy.objects.create(
            lecture=self.lecture,
            video_required_rate=0,
            exam_start_session_order=1,
            exam_end_session_order=9999,
            exam_pass_score=90,
            exam_aggregate_strategy=ProgressPolicy.ExamAggregateStrategy.MAX,
            exam_pass_source=ProgressPolicy.ExamPassSource.EXAM,
            homework_start_session_order=9999,
            homework_end_session_order=9999,
            homework_pass_type=ProgressPolicy.HomeworkPassType.TEACHER_APPROVAL,
        )
        self.submission = Submission.objects.create(
            tenant=self.tenant,
            user=self.staff,
            enrollment=self.enrollment,
            target_type=Submission.TargetType.EXAM,
            target_id=self.exam.id,
            source=Submission.Source.OMR_SCAN,
            status=Submission.Status.ANSWERS_READY,
            meta={"manual_review": {"required": False}},
        )
        SubmissionAnswer.objects.create(
            tenant=self.tenant,
            submission=self.submission,
            exam_question_id=self.choice.id,
            answer="1",
        )
        grade_submission(self.submission.id)
        self.draft = ScoreEditDraft.objects.create(
            tenant=self.tenant,
            session=self.session,
            editor_user=self.staff,
            client_id="mixed-omr-concurrent-browser",
            payload={"client_id": "mixed-omr-concurrent-browser", "changes": []},
        )
        self.submission.status = Submission.Status.ANSWERS_READY
        self.submission.save(update_fields=["status", "updated_at"])

    def _renew_score_edit_lease(self) -> None:
        request = APIRequestFactory().put(
            f"/results/admin/sessions/{self.session.id}/score-draft/",
            {"changes": [], "acknowledge_stale": True},
            format="json",
            HTTP_X_SCORE_EDITOR_CLIENT="mixed-omr-concurrent-browser",
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.staff)
        response = ScoreDraftView.as_view()(request, session_id=self.session.id)
        self.assertEqual(response.status_code, 200, response.data)

    def _retry_subjective_patch(self):
        request = APIRequestFactory().patch(
            "/results/admin/exams/subjective/",
            {"score": 20},
            format="json",
            HTTP_X_SCORE_EDITOR_CLIENT="mixed-omr-concurrent-browser",
            HTTP_X_SCORE_SESSION_ID=str(self.session.id),
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.staff)
        return AdminExamSubjectiveScoreView.as_view()(
            request,
            exam_id=self.exam.id,
            enrollment_id=self.enrollment.id,
        )

    def _assert_subjective_score_persisted_and_visible(
        self,
        *,
        submission,
        retry_response,
    ) -> None:
        self.assertEqual(float(retry_response.data["subjective_score"]), 20.0)
        self.assertEqual(float(retry_response.data["total_score"]), 100.0)
        self.assertTrue(retry_response.data["projection_ready"])
        self.assertIsNone(retry_response.data["grading_status"])

        canonical = Result.objects.get(
            target_type="exam",
            target_id=self.exam.id,
            enrollment=self.enrollment,
        )
        legacy = ExamResult.objects.get(submission=submission)
        self.assertEqual(float(canonical.objective_score), 80.0)
        self.assertEqual(float(canonical.total_score), 100.0)
        self.assertEqual(legacy.status, ExamResult.Status.FINAL)
        self.assertEqual(float(legacy.subjective_score), 20.0)
        self.assertEqual(float(legacy.total_score), 100.0)

        attempts_request = APIRequestFactory().get(
            f"/results/admin/exams/{self.exam.id}/enrollments/"
            f"{self.enrollment.id}/attempts/"
        )
        attempts_request.tenant = self.tenant
        force_authenticate(attempts_request, user=self.staff)
        attempts_response = AdminExamAttemptsView.as_view()(
            attempts_request,
            exam_id=self.exam.id,
            enrollment_id=self.enrollment.id,
        )
        self.assertEqual(attempts_response.status_code, 200, attempts_response.data)
        representative = next(
            item for item in attempts_response.data if item["is_representative"]
        )
        self.assertEqual(representative["status"], "done")
        self.assertEqual(float(representative["meta"]["subjective_score"]), 20.0)
        self.assertEqual(float(representative["meta"]["total_score"]), 100.0)

        request = APIRequestFactory().get(
            f"/results/admin/sessions/{self.session.id}/scores/"
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.staff)
        response = SessionScoresView.as_view()(request, session_id=self.session.id)
        self.assertEqual(response.status_code, 200, response.data)
        row = next(
            item
            for item in response.data["rows"]
            if item["enrollment_id"] == self.enrollment.id
        )
        exam_entry = next(
            item for item in row["exams"] if item["exam_id"] == self.exam.id
        )
        block = exam_entry["block"]
        self.assertIsNone(block["grading_status"])

        progress = SessionProgress.objects.get(
            enrollment=self.enrollment,
            session=self.session,
        )
        self.assertTrue(progress.exam_passed)
        self.assertEqual(float(progress.exam_aggregate_score), 100.0)

    def test_subjective_patch_and_force_regrade_share_one_lock_order(self):
        session_locked = threading.Event()
        worker_started = threading.Event()
        worker_exam_result_locked = threading.Event()
        statuses: list[int] = []
        errors: list[str] = []

        from apps.domains.results.views import admin_exam_subjective_score_view as subjective_view

        original_require = subjective_view.require_score_edit_lease_from_headers
        original_auto_grade = ExamGradingService.auto_grade_objective

        def wait_after_manual_session_lock(*args, **kwargs):
            session = original_require(*args, **kwargs)
            session_locked.set()
            if not worker_started.wait(timeout=5):
                raise AssertionError("worker did not start")
            worker_exam_result_locked.wait(timeout=0.5)
            return session

        def observe_worker_exam_result_lock(service, **kwargs):
            result = original_auto_grade(service, **kwargs)
            worker_exam_result_locked.set()
            return result

        def manual_writer() -> None:
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                request = APIRequestFactory().patch(
                    "/results/admin/exams/subjective/",
                    {"score": 20},
                    format="json",
                    HTTP_X_SCORE_EDITOR_CLIENT="mixed-omr-concurrent-browser",
                    HTTP_X_SCORE_SESSION_ID=str(self.session.id),
                )
                request.tenant = Tenant.objects.get(id=self.tenant.id)
                force_authenticate(request, user=User.objects.get(id=self.staff.id))
                with patch.object(
                    subjective_view,
                    "require_score_edit_lease_from_headers",
                    side_effect=wait_after_manual_session_lock,
                ):
                    response = AdminExamSubjectiveScoreView.as_view()(
                        request,
                        exam_id=self.exam.id,
                        enrollment_id=self.enrollment.id,
                    )
                statuses.append(response.status_code)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"manual: {exc!r}")
            finally:
                close_old_connections()

        def worker_writer() -> None:
            close_old_connections()
            try:
                if not session_locked.wait(timeout=5):
                    raise AssertionError("manual writer did not lock the session scope")
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                worker_started.set()
                with patch.object(
                    ExamGradingService,
                    "auto_grade_objective",
                    new=observe_worker_exam_result_lock,
                ):
                    grade_submission(self.submission.id, force_regrade=True)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"worker: {exc!r}")
            finally:
                close_old_connections()

        manual = threading.Thread(target=manual_writer)
        worker = threading.Thread(target=worker_writer)
        manual.start()
        worker.start()
        manual.join(timeout=15)
        worker.join(timeout=15)

        self.assertFalse(manual.is_alive(), "manual writer did not finish")
        self.assertFalse(worker.is_alive(), "worker writer did not finish")
        self.assertEqual(errors, [])
        self.assertEqual(statuses, [200])

        self.draft.refresh_from_db()
        self.assertTrue(score_edit_payload_is_invalidated(self.draft.payload))
        canonical = Result.objects.get(
            target_type="exam",
            target_id=self.exam.id,
            enrollment=self.enrollment,
        )
        legacy = ExamResult.objects.get(submission=self.submission)
        self.assertEqual(float(canonical.objective_score), 80.0)
        self.assertEqual(float(canonical.total_score), 100.0)
        self.assertEqual(legacy.status, ExamResult.Status.FINAL)
        self.assertEqual(float(legacy.subjective_score), 20.0)
        self.assertEqual(float(legacy.total_score), 100.0)
        self.assertEqual(
            ResultFact.objects.filter(
                target_type="exam",
                target_id=self.exam.id,
                enrollment=self.enrollment,
                source="manual_subjective",
            ).count(),
            1,
        )

    def test_first_grade_and_subjective_patch_share_exam_then_session_lock_order(self):
        self.exam.allow_retake = True
        self.exam.max_attempts = 2
        self.exam.save(update_fields=["allow_retake", "max_attempts", "updated_at"])
        fresh_submission = Submission.objects.create(
            tenant=self.tenant,
            user=self.staff,
            enrollment=self.enrollment,
            target_type=Submission.TargetType.EXAM,
            target_id=self.exam.id,
            source=Submission.Source.OMR_SCAN,
            status=Submission.Status.ANSWERS_READY,
            meta={"manual_review": {"required": False}},
        )
        SubmissionAnswer.objects.create(
            tenant=self.tenant,
            submission=fresh_submission,
            exam_question_id=self.choice.id,
            answer="1",
        )

        worker_scope_locked = threading.Event()
        teacher_exam_locked = threading.Event()
        statuses: list[int] = []
        errors: list[str] = []

        from apps.domains.results.services import grading_service
        from apps.domains.results.views import admin_exam_subjective_score_view as subjective_view

        original_worker_scope = grading_service.lock_score_edit_scope_before_submission_grading
        original_teacher_exam = subjective_view.lock_regular_active_exam_for_tenant

        def observe_worker_scope(*args, **kwargs):
            scope_ids = original_worker_scope(*args, **kwargs)
            worker_scope_locked.set()
            # With the corrected order, the worker also owns the Exam lock and
            # the teacher cannot reach this signal until the worker commits.
            teacher_exam_locked.wait(timeout=0.5)
            return scope_ids

        def observe_teacher_exam(*args, **kwargs):
            exam = original_teacher_exam(*args, **kwargs)
            teacher_exam_locked.set()
            return exam

        def worker_writer() -> None:
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                with patch.object(
                    grading_service,
                    "lock_score_edit_scope_before_submission_grading",
                    side_effect=observe_worker_scope,
                ):
                    grade_submission(fresh_submission.id)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"worker: {exc!r}")
            finally:
                close_old_connections()

        def manual_writer() -> None:
            close_old_connections()
            try:
                if not worker_scope_locked.wait(timeout=5):
                    raise AssertionError("worker did not lock the score-edit scope")
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                request = APIRequestFactory().patch(
                    "/results/admin/exams/subjective/",
                    {"score": 20},
                    format="json",
                    HTTP_X_SCORE_EDITOR_CLIENT="mixed-omr-concurrent-browser",
                    HTTP_X_SCORE_SESSION_ID=str(self.session.id),
                )
                request.tenant = Tenant.objects.get(id=self.tenant.id)
                force_authenticate(request, user=User.objects.get(id=self.staff.id))
                with patch.object(
                    subjective_view,
                    "lock_regular_active_exam_for_tenant",
                    side_effect=observe_teacher_exam,
                ):
                    response = AdminExamSubjectiveScoreView.as_view()(
                        request,
                        exam_id=self.exam.id,
                        enrollment_id=self.enrollment.id,
                    )
                statuses.append(response.status_code)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"manual: {exc!r}")
            finally:
                close_old_connections()

        worker = threading.Thread(target=worker_writer)
        manual = threading.Thread(target=manual_writer)
        worker.start()
        manual.start()
        worker.join(timeout=15)
        manual.join(timeout=15)

        self.assertFalse(worker.is_alive(), "worker writer did not finish")
        self.assertFalse(manual.is_alive(), "manual writer did not finish")
        self.assertEqual(errors, [])
        self.assertEqual(statuses, [409])
        self.assertTrue(ExamResult.objects.filter(submission=fresh_submission).exists())
        self.draft.refresh_from_db()
        self.assertTrue(score_edit_payload_is_invalidated(self.draft.payload))
        self._renew_score_edit_lease()
        retry = self._retry_subjective_patch()
        self.assertEqual(retry.status_code, 200, retry.data)
        self._assert_subjective_score_persisted_and_visible(
            submission=fresh_submission,
            retry_response=retry,
        )

    def test_public_recalculate_and_subjective_patch_share_exam_then_session_lock_order(self):
        recalculate_rows_locked = threading.Event()
        teacher_session_locked = threading.Event()
        recalculate_responses: list[dict] = []
        subjective_statuses: list[int] = []
        errors: list[str] = []

        from apps.domains.results.views import admin_exam_subjective_score_view as subjective_view
        from apps.support.submissions import dependencies as submission_dependencies

        original_grade = submission_dependencies.grade_submission_objective
        original_require = subjective_view.require_score_edit_lease_from_headers

        def observe_recalculate_rows(*args, **kwargs):
            recalculate_rows_locked.set()
            # Once recalculate owns Exam -> Session, the teacher must wait at
            # Exam and cannot reach its session signal until this call commits.
            teacher_session_locked.wait(timeout=0.5)
            return original_grade(*args, **kwargs)

        def observe_teacher_session(*args, **kwargs):
            session = original_require(*args, **kwargs)
            teacher_session_locked.set()
            return session

        def recalculate_writer() -> None:
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                request = APIRequestFactory().post(
                    f"/api/v1/exams/{self.exam.id}/recalculate/"
                )
                request.tenant = Tenant.objects.get(id=self.tenant.id)
                force_authenticate(request, user=User.objects.get(id=self.staff.id))
                with patch.object(
                    submission_dependencies,
                    "grade_submission_objective",
                    side_effect=observe_recalculate_rows,
                ):
                    response = ExamRecalculateView.as_view()(request, exam_id=self.exam.id)
                recalculate_responses.append(dict(response.data))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"recalculate: {exc!r}")
            finally:
                close_old_connections()

        def manual_writer() -> None:
            close_old_connections()
            try:
                if not recalculate_rows_locked.wait(timeout=5):
                    raise AssertionError("recalculate did not lock result rows")
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                request = APIRequestFactory().patch(
                    "/results/admin/exams/subjective/",
                    {"score": 20},
                    format="json",
                    HTTP_X_SCORE_EDITOR_CLIENT="mixed-omr-concurrent-browser",
                    HTTP_X_SCORE_SESSION_ID=str(self.session.id),
                )
                request.tenant = Tenant.objects.get(id=self.tenant.id)
                force_authenticate(request, user=User.objects.get(id=self.staff.id))
                with patch.object(
                    subjective_view,
                    "require_score_edit_lease_from_headers",
                    side_effect=observe_teacher_session,
                ):
                    response = AdminExamSubjectiveScoreView.as_view()(
                        request,
                        exam_id=self.exam.id,
                        enrollment_id=self.enrollment.id,
                    )
                subjective_statuses.append(response.status_code)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"manual: {exc!r}")
            finally:
                close_old_connections()

        recalculate = threading.Thread(target=recalculate_writer)
        manual = threading.Thread(target=manual_writer)
        recalculate.start()
        manual.start()
        recalculate.join(timeout=15)
        manual.join(timeout=15)

        self.assertFalse(recalculate.is_alive(), "recalculate writer did not finish")
        self.assertFalse(manual.is_alive(), "manual writer did not finish")
        self.assertEqual(errors, [])
        self.assertEqual(subjective_statuses, [409])
        self.assertEqual(len(recalculate_responses), 1)
        self.assertEqual(recalculate_responses[0]["failed"], [])
        self.draft.refresh_from_db()
        self.assertTrue(score_edit_payload_is_invalidated(self.draft.payload))
        self._renew_score_edit_lease()
        retry = self._retry_subjective_patch()
        self.assertEqual(retry.status_code, 200, retry.data)
        self._assert_subjective_score_persisted_and_visible(
            submission=self.submission,
            retry_response=retry,
        )

    def _assert_submission_writer_serializes_with_public_recalculate(self, writer_call) -> None:
        writer_grade_started = threading.Event()
        recalculate_scope_locked = threading.Event()
        writer_responses: list[tuple[int, dict]] = []
        recalculate_responses: list[dict] = []
        errors: list[str] = []

        from academy.application.use_cases.omr import grading_readiness
        from apps.support.results import grading_dependencies

        original_grade = grading_readiness.grade_omr_submission_if_ready
        original_scope_lock = grading_dependencies.lock_exam_and_score_edit_scope_for_grading

        def observe_writer_grade(*args, **kwargs):
            writer_grade_started.set()
            recalculate_scope_locked.wait(timeout=0.5)
            return original_grade(*args, **kwargs)

        def observe_recalculate_scope(*args, **kwargs):
            result = original_scope_lock(*args, **kwargs)
            if threading.current_thread().name == "public-recalculate-writer-race":
                recalculate_scope_locked.set()
            return result

        def submission_writer() -> None:
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                with patch.object(
                    grading_readiness,
                    "grade_omr_submission_if_ready",
                    side_effect=observe_writer_grade,
                ):
                    response = writer_call()
                writer_responses.append((response.status_code, dict(response.data)))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"submission-writer: {exc!r}")
            finally:
                close_old_connections()

        def recalculate_writer() -> None:
            close_old_connections()
            try:
                if not writer_grade_started.wait(timeout=5):
                    raise AssertionError("submission writer did not reach grading")
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                request = APIRequestFactory().post(
                    f"/api/v1/exams/{self.exam.id}/recalculate/"
                )
                request.tenant = Tenant.objects.get(id=self.tenant.id)
                force_authenticate(request, user=User.objects.get(id=self.staff.id))
                with patch.object(
                    grading_dependencies,
                    "lock_exam_and_score_edit_scope_for_grading",
                    side_effect=observe_recalculate_scope,
                ):
                    response = ExamRecalculateView.as_view()(request, exam_id=self.exam.id)
                recalculate_responses.append(dict(response.data))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"recalculate: {exc!r}")
            finally:
                close_old_connections()

        writer = threading.Thread(target=submission_writer, name="submission-writer-race")
        recalculate = threading.Thread(
            target=recalculate_writer,
            name="public-recalculate-writer-race",
        )
        writer.start()
        recalculate.start()
        writer.join(timeout=15)
        recalculate.join(timeout=15)

        self.assertFalse(writer.is_alive(), "submission writer did not finish")
        self.assertFalse(recalculate.is_alive(), "recalculate writer did not finish")
        self.assertEqual(errors, [])
        self.assertEqual(len(writer_responses), 1)
        self.assertEqual(writer_responses[0][0], 200, writer_responses[0][1])
        self.assertEqual(len(recalculate_responses), 1)
        self.assertEqual(recalculate_responses[0]["failed"], [])

    def test_manual_edit_and_public_recalculate_share_exam_then_session_lock_order(self):
        def manual_edit_call():
            request = APIRequestFactory().post(
                f"/submissions/submissions/{self.submission.id}/manual-edit/",
                {
                    "identifier": {"enrollment_id": self.enrollment.id},
                    "answers": [
                        {"exam_question_id": self.choice.id, "answer": "1"},
                    ],
                    "note": "concurrency regression",
                },
                format="json",
            )
            request.tenant = Tenant.objects.get(id=self.tenant.id)
            force_authenticate(request, user=User.objects.get(id=self.staff.id))
            return SubmissionViewSet.as_view({"post": "manual_edit"})(
                request,
                pk=self.submission.id,
            )

        self._assert_submission_writer_serializes_with_public_recalculate(manual_edit_call)

    def test_duplicate_accept_and_public_recalculate_share_exam_then_session_lock_order(self):
        kept = Submission.objects.create(
            tenant=self.tenant,
            user=self.staff,
            enrollment=self.enrollment,
            target_type=Submission.TargetType.EXAM,
            target_id=self.exam.id,
            source=Submission.Source.OMR_SCAN,
            status=Submission.Status.ANSWERS_READY,
            meta={
                "identifier_status": "matched_ambiguous",
                "manual_review": {"required": True},
            },
        )
        SubmissionAnswer.objects.create(
            tenant=self.tenant,
            submission=kept,
            exam_question_id=self.choice.id,
            answer="1",
        )

        def accept_duplicate_call():
            request = APIRequestFactory().post(
                f"/submissions/submissions/{kept.id}/accept-from-duplicates/"
            )
            request.tenant = Tenant.objects.get(id=self.tenant.id)
            force_authenticate(request, user=User.objects.get(id=self.staff.id))
            return SubmissionViewSet.as_view({"post": "accept_from_duplicates"})(
                request,
                pk=kept.id,
            )

        self._assert_submission_writer_serializes_with_public_recalculate(
            accept_duplicate_call
        )
