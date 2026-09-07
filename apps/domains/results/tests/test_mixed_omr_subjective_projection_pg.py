from __future__ import annotations

import json
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate

from apps.core.models import Tenant, TenantMembership
from apps.domains.results.models import (
    ExamResult,
    Result,
    ResultFact,
    ResultItem,
    ScoreEditDraft,
)
from apps.domains.results.services.grading_service import grade_submission
from apps.domains.results.services.student_result_service import (
    get_my_exam_result_data,
)
from apps.domains.results.utils.ranking import compute_exam_rankings
from apps.domains.results.views.admin_exam_subjective_score_view import (
    AdminExamSubjectiveScoreView,
)
from apps.domains.results.views.admin_exam_summary_view import AdminExamSummaryView
from apps.domains.results.views.admin_session_exams_summary_view import (
    AdminSessionExamsSummaryView,
)
from apps.domains.results.views.admin_student_grades_view import (
    AdminStudentGradesView,
)
from apps.domains.results.services.session_score_summary_service import (
    SessionScoreSummaryService,
)
from apps.support.results.enterprise_analytics import (
    build_teacher_enterprise_analytics,
)
from apps.support.student_app.results_summary import build_student_grades_summary
from apps.domains.results.views.admin_exam_result_detail_view import (
    AdminExamResultDetailView,
)
from apps.domains.results.views.admin_exam_results_view import (
    AdminExamResultsView,
)
from apps.domains.results.views.admin_exam_item_score_view import (
    AdminExamItemScoreView,
)
from apps.domains.results.views.admin_exam_objective_score_view import (
    AdminExamObjectiveScoreView,
)
from apps.domains.results.views.admin_exam_total_score_view import (
    AdminExamTotalScoreView,
)
from apps.domains.results.views.session_scores_view import SessionScoresView
from apps.support.results.tests.omr_subjective_completion_fixtures import (
    AnswerKey,
    ClinicLink,
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
    dispatch_progress_pipeline,
)


User = get_user_model()


class MixedOmrSubjectiveProjectionPostgresTests(TestCase):
    """A mixed OMR score is staff-visible but not published until grading is complete."""

    def setUp(self):
        self.tenant = Tenant.objects.create(
            code="mixed-omr-pending",
            name="Mixed OMR pending",
            is_active=True,
        )
        self.staff = User.objects.create_user(
            username="mixed-omr-staff",
            password="pw1234",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=self.staff,
            role="admin",
        )
        self.factory = APIRequestFactory()
        student_user = User.objects.create_user(
            username="mixed-omr-student",
            password="pw1234",
            tenant=self.tenant,
        )
        self.student = Student.objects.create(
            tenant=self.tenant,
            user=student_user,
            name="Mixed OMR student",
            ps_number="MIXED-OMR-1",
            omr_code="87654321",
        )
        self.lecture = Lecture.objects.create(
            tenant=self.tenant,
            title="Mixed OMR lecture",
            name="Mixed OMR lecture",
            subject="MATH",
        )
        self.session = Session.objects.create(
            lecture=self.lecture,
            order=1,
            title="Mixed OMR session",
        )
        self.enrollment = Enrollment.objects.create(
            tenant=self.tenant,
            student=self.student,
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
            title="Mixed OMR exam",
            exam_type=Exam.ExamType.REGULAR,
            grading_mode=Exam.GradingMode.CHOICE,
            pass_score=90,
            max_score=100,
            student_results_published=True,
        )
        self.exam.sessions.add(self.session)
        ExamEnrollment.objects.create(exam=self.exam, enrollment=self.enrollment)
        self.sheet = Sheet.objects.create(
            exam=self.exam,
            name="MAIN",
            total_questions=2,
            choice_count=1,
            essay_count=1,
        )
        self.choice = ExamQuestion.objects.create(
            sheet=self.sheet,
            number=1,
            score=80,
            question_kind=ExamQuestion.QuestionKind.CHOICE,
        )
        self.essay = ExamQuestion.objects.create(
            sheet=self.sheet,
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

    def _grade_objective_only(self) -> tuple[ExamResult, Result]:
        legacy = grade_submission(self.submission.id)
        canonical = Result.objects.get(
            target_type="exam",
            target_id=self.exam.id,
            enrollment=self.enrollment,
        )
        return legacy, canonical

    def _new_omr_submission(self, *, answer: str = "1") -> Submission:
        submission = Submission.objects.create(
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
            submission=submission,
            exam_question_id=self.choice.id,
            answer=answer,
        )
        return submission

    def _patch_item_score(self, *, question: ExamQuestion, score: float):
        request = self.factory.patch(
            "/results/admin/exams/items/",
            {"score": score},
            format="json",
            HTTP_X_SCORE_EDITOR_CLIENT="mixed-omr-browser",
            HTTP_X_SCORE_SESSION_ID=str(self.session.id),
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.staff)
        return AdminExamItemScoreView.as_view()(
            request,
            exam_id=self.exam.id,
            enrollment_id=self.enrollment.id,
            question_id=question.id,
        )

    def test_objective_only_mixed_omr_stays_draft_and_skips_projection_dispatch(self):
        with patch(
            "apps.domains.results.services.grading_service.dispatch_progress_pipeline"
        ) as dispatch:
            legacy, canonical = self._grade_objective_only()

        self.assertEqual(legacy.status, ExamResult.Status.DRAFT)
        self.assertIsNone(legacy.finalized_at)
        self.assertEqual(float(canonical.objective_score), 80.0)
        self.assertEqual(float(canonical.total_score), 80.0)
        self.assertEqual(float(canonical.max_score), 100.0)
        dispatch.assert_not_called()

    def test_omr_review_save_reports_objective_regrade_without_final_projection(self):
        unrelated_submission = Submission.objects.create(
            tenant=self.tenant,
            user=self.staff,
            enrollment=self.enrollment,
            target_type=Submission.TargetType.EXAM,
            target_id=self.exam.id,
            source=Submission.Source.ONLINE,
            status=Submission.Status.DONE,
        )
        ExamResult.objects.create(
            submission=unrelated_submission,
            exam=self.exam,
        )
        self._grade_objective_only()
        client = APIClient()
        client.force_authenticate(user=self.staff)
        response = client.post(
            f"/api/v1/submissions/submissions/{self.submission.id}/manual-edit/",
            {
                "answers": [
                    {
                        "exam_question_id": self.choice.id,
                        "answer": "1",
                    }
                ],
                "note": "mixed_omr_realuse_review",
            },
            format="json",
            HTTP_HOST="api.hakwonplus.com",
            HTTP_X_TENANT_CODE=self.tenant.code,
        )

        payload = response.json()
        self.assertEqual(response.status_code, 200, payload)
        self.assertTrue(payload["graded"])
        self.assertFalse(payload["projection_ready"])
        self.assertEqual(payload["grading_status"], "subjective_pending")
        self.assertEqual(float(payload["score"]), 80.0)

    def test_omr_result_sync_failure_rolls_back_and_skips_projection_dispatch(self):
        with patch(
            "apps.domains.results.services.grading_service.sync_result_from_exam_submission",
            side_effect=RuntimeError("result sync failed"),
        ), patch(
            "apps.domains.results.services.grading_service.dispatch_progress_pipeline"
        ) as dispatch:
            with self.assertRaises(RuntimeError):
                grade_submission(self.submission.id)

        self.assertFalse(
            ExamResult.objects.filter(submission=self.submission).exists()
        )
        dispatch.assert_not_called()

    def test_objective_only_mixed_omr_is_hidden_from_student_and_rank(self):
        _legacy, canonical = self._grade_objective_only()
        request = SimpleNamespace(tenant=self.tenant, user=self.student.user)

        with patch(
            "apps.domains.results.services.student_result_service.get_request_student",
            return_value=self.student,
        ):
            payload = get_my_exam_result_data(request, self.exam.id, tenant=self.tenant)
        rankings = compute_exam_rankings(
            exam_id=self.exam.id,
            tenant=self.tenant,
            lecture_ids={self.lecture.id},
        )

        self.assertFalse(payload["student_results_published"])
        self.assertEqual(payload["grading_status"], "subjective_pending")
        self.assertNotIn("total_score", payload)
        self.assertNotIn(self.enrollment.id, rankings)
        self.assertEqual(float(canonical.objective_score), 80.0)

    def test_historical_final_partial_omr_is_still_hidden(self):
        legacy, _canonical = self._grade_objective_only()
        legacy.status = ExamResult.Status.FINAL
        legacy.save(update_fields=["status", "updated_at"])
        request = SimpleNamespace(tenant=self.tenant, user=self.student.user)

        with patch(
            "apps.domains.results.services.student_result_service.get_request_student",
            return_value=self.student,
        ):
            payload = get_my_exam_result_data(request, self.exam.id, tenant=self.tenant)
        rankings = compute_exam_rankings(
            exam_id=self.exam.id,
            tenant=self.tenant,
            lecture_ids={self.lecture.id},
        )

        self.assertFalse(payload["student_results_published"])
        self.assertEqual(payload["grading_status"], "subjective_pending")
        self.assertNotIn("total_score", payload)
        self.assertNotIn(self.enrollment.id, rankings)

    def test_pending_partial_is_visible_but_scoreless_in_student_parent_and_staff_history(self):
        self._grade_objective_only()

        student_payload = build_student_grades_summary(
            tenant=self.tenant,
            student=self.student,
        )
        student_exam = student_payload["exams"][0]
        self.assertEqual(student_exam["grading_status"], "subjective_pending")
        self.assertTrue(student_exam["is_provisional"])
        self.assertIsNone(student_exam["total_score"])
        self.assertIsNone(student_exam["is_pass"])
        self.assertIsNone(student_exam["rank"])
        self.assertEqual(student_exam["total_questions"], 0)
        self.assertEqual(student_exam["wrong_question_numbers"], [])
        self.assertEqual(student_payload["exam_summary"]["scored_count"], 0)
        self.assertEqual(student_payload["exam_trend"], [])

        # Parent grades use the same authorized student read model. Keep this
        # assertion explicit so future parent-only shaping cannot leak the score.
        parent_payload = build_student_grades_summary(
            tenant=self.tenant,
            student=self.student,
        )
        self.assertIsNone(parent_payload["exams"][0]["total_score"])
        self.assertEqual(
            parent_payload["exams"][0]["grading_status"],
            "subjective_pending",
        )

        staff_request = self.factory.get(
            "/results/admin/student-grades/",
            {"student_id": self.student.id},
        )
        staff_request.tenant = self.tenant
        force_authenticate(staff_request, user=self.staff)
        staff_response = AdminStudentGradesView.as_view()(staff_request)
        self.assertEqual(staff_response.status_code, 200, staff_response.data)
        staff_exam = staff_response.data["exams"][0]
        self.assertIsNone(staff_exam["total_score"])
        self.assertIsNone(staff_exam["rank"])
        self.assertEqual(staff_exam["grading_status"], "subjective_pending")
        self.assertEqual(staff_response.data["exam_summary"]["scored_count"], 0)

    def test_pending_partial_is_excluded_from_all_staff_score_aggregates(self):
        self._grade_objective_only()

        exam_request = self.factory.get(
            f"/results/admin/exams/{self.exam.id}/summary/"
        )
        exam_request.tenant = self.tenant
        force_authenticate(exam_request, user=self.staff)
        exam_response = AdminExamSummaryView.as_view()(
            exam_request,
            exam_id=self.exam.id,
        )
        self.assertEqual(exam_response.status_code, 200, exam_response.data)
        self.assertEqual(float(exam_response.data["avg_score"]), 0.0)
        self.assertEqual(exam_response.data["pass_count"], 0)
        self.assertEqual(exam_response.data["fail_count"], 0)

        session_request = self.factory.get(
            f"/results/admin/sessions/{self.session.id}/exams/summary/"
        )
        session_request.tenant = self.tenant
        force_authenticate(session_request, user=self.staff)
        session_response = AdminSessionExamsSummaryView.as_view()(
            session_request,
            session_id=self.session.id,
        )
        self.assertEqual(session_response.status_code, 200, session_response.data)
        session_exam = session_response.data["exams"][0]
        self.assertEqual(float(session_exam["avg_score"]), 0.0)
        self.assertEqual(session_exam["participant_count"], 0)
        self.assertEqual(session_exam["pass_count"], 0)
        self.assertEqual(session_exam["fail_count"], 0)

        score_summary = SessionScoreSummaryService.build(session_id=self.session.id)
        self.assertEqual(float(score_summary["avg_score"]), 0.0)
        self.assertEqual(float(score_summary["min_score"]), 0.0)
        self.assertEqual(float(score_summary["max_score"]), 0.0)

        analytics = build_teacher_enterprise_analytics(
            tenant=self.tenant,
            days=365,
        )
        self.assertEqual(analytics["summary"]["scored_count"], 0)
        self.assertIsNone(analytics["summary"]["avg_score_pct"])
        self.assertEqual(analytics["top_exams"], [])
        self.assertEqual(analytics["weak_questions"], [])

    def test_reconcile_command_dry_run_then_retracts_historical_projections(self):
        legacy, _canonical = self._grade_objective_only()
        legacy.status = ExamResult.Status.FINAL
        legacy.save(update_fields=["status", "updated_at"])
        progress = SessionProgress.objects.create(
            enrollment=self.enrollment,
            session=self.session,
            exam_attempted=True,
            exam_aggregate_score=80,
            exam_passed=True,
        )
        link = ClinicLink.objects.create(
            tenant=self.tenant,
            enrollment=self.enrollment,
            session=self.session,
            reason=ClinicLink.Reason.AUTO_FAILED,
            is_auto=True,
            source_type="exam",
            source_id=self.exam.id,
        )

        session_request = self.factory.get(
            f"/results/admin/sessions/{self.session.id}/exams/summary/"
        )
        session_request.tenant = self.tenant
        force_authenticate(session_request, user=self.staff)
        session_response = AdminSessionExamsSummaryView.as_view()(
            session_request,
            session_id=self.session.id,
        )
        self.assertEqual(session_response.status_code, 200, session_response.data)
        self.assertEqual(float(session_response.data["pass_rate"]), 0.0)
        self.assertEqual(float(session_response.data["clinic_rate"]), 0.0)

        exam_request = self.factory.get(
            f"/results/admin/exams/{self.exam.id}/summary/"
        )
        exam_request.tenant = self.tenant
        force_authenticate(exam_request, user=self.staff)
        exam_response = AdminExamSummaryView.as_view()(
            exam_request,
            exam_id=self.exam.id,
        )
        self.assertEqual(exam_response.data["clinic_count"], 0)

        dry_output = StringIO()
        call_command(
            "reconcile_mixed_omr_projections",
            tenant=self.tenant.id,
            stdout=dry_output,
            as_json=True,
        )
        dry_payload = json.loads(dry_output.getvalue())
        self.assertTrue(dry_payload["dry_run"])
        self.assertEqual(dry_payload["pending_result_count"], 1)
        self.assertEqual(dry_payload["stale_progress_count"], 1)
        self.assertEqual(dry_payload["stale_clinic_count"], 1)
        progress.refresh_from_db()
        link.refresh_from_db()
        self.assertEqual(float(progress.exam_aggregate_score), 80.0)
        self.assertTrue(progress.exam_passed)
        self.assertIsNone(link.resolved_at)

        apply_output = StringIO()
        call_command(
            "reconcile_mixed_omr_projections",
            tenant=self.tenant.id,
            apply=True,
            stdout=apply_output,
            as_json=True,
        )
        apply_payload = json.loads(apply_output.getvalue())
        self.assertFalse(apply_payload["dry_run"])
        self.assertEqual(apply_payload["reconciled_count"], 1)
        progress.refresh_from_db()
        link.refresh_from_db()
        self.assertIsNone(progress.exam_aggregate_score)
        self.assertFalse(progress.exam_passed)
        self.assertEqual(
            progress.exam_meta["exams"][0]["grading_status"],
            "subjective_pending",
        )
        self.assertIsNotNone(link.resolved_at)
        self.assertEqual(
            link.resolution_type,
            ClinicLink.ResolutionType.GRADING_RETRACTED,
        )

    def test_objective_only_mixed_omr_does_not_create_clinic_projection(self):
        self._grade_objective_only()

        dispatch_progress_pipeline(submission_id=self.submission.id)

        self.assertFalse(
            ClinicLink.objects.filter(
                tenant=self.tenant,
                enrollment=self.enrollment,
                source_type="exam",
                source_id=self.exam.id,
            ).exists()
        )

    def test_session_score_entry_shows_actionable_pending_without_clinic_or_correction(self):
        self._grade_objective_only()
        ClinicLink.objects.create(
            tenant=self.tenant,
            enrollment=self.enrollment,
            session=self.session,
            reason=ClinicLink.Reason.AUTO_FAILED,
            is_auto=True,
            source_type="exam",
            source_id=self.exam.id,
        )
        request = self.factory.get(
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
        self.assertEqual(
            exam_entry["block"]["grading_status"],
            "subjective_pending",
        )
        self.assertEqual(float(exam_entry["block"]["objective_score"]), 80.0)
        self.assertEqual(float(exam_entry["block"]["score"]), 80.0)
        self.assertIsNone(exam_entry["block"]["subjective_score"])
        self.assertIsNone(exam_entry["block"]["passed"])
        self.assertFalse(exam_entry["block"]["clinic_required"])
        self.assertIsNone(exam_entry["block"]["correction_status"])
        self.assertIsNone(exam_entry["clinic_link_id"])
        self.assertFalse(row["clinic_required"])

    def test_admin_list_and_detail_keep_pending_score_out_of_rank_and_clinic(self):
        self._grade_objective_only()
        ClinicLink.objects.create(
            tenant=self.tenant,
            enrollment=self.enrollment,
            session=self.session,
            reason=ClinicLink.Reason.AUTO_FAILED,
            is_auto=True,
            source_type="exam",
            source_id=self.exam.id,
        )

        list_request = self.factory.get(
            f"/results/admin/exams/{self.exam.id}/results/"
        )
        list_request.tenant = self.tenant
        force_authenticate(list_request, user=self.staff)
        list_response = AdminExamResultsView.as_view()(
            list_request,
            exam_id=self.exam.id,
        )

        self.assertEqual(list_response.status_code, 200, list_response.data)
        list_row = list_response.data["results"][0]
        self.assertEqual(list_row["grading_status"], "subjective_pending")
        self.assertTrue(list_row["is_provisional"])
        self.assertEqual(list_row["result_status"], "PARTIAL")
        self.assertEqual(float(list_row["exam_score"]), 80.0)
        self.assertEqual(float(list_row["final_score"]), 80.0)
        self.assertIsNone(list_row["passed"])
        self.assertIsNone(list_row["rank"])
        self.assertIsNone(list_row["ranking_score"])
        self.assertFalse(list_row["clinic_required"])
        self.assertIsNone(list_row["correction_status"])
        self.assertFalse(list_row["name_highlight_clinic_target"])

        detail_request = self.factory.get(
            f"/results/admin/exams/{self.exam.id}/enrollments/{self.enrollment.id}/"
        )
        detail_request.tenant = self.tenant
        force_authenticate(detail_request, user=self.staff)
        detail_response = AdminExamResultDetailView.as_view()(
            detail_request,
            exam_id=self.exam.id,
            enrollment_id=self.enrollment.id,
        )

        self.assertEqual(detail_response.status_code, 200, detail_response.data)
        self.assertEqual(
            detail_response.data["grading_status"],
            "subjective_pending",
        )
        self.assertTrue(detail_response.data["is_provisional"])
        self.assertEqual(float(detail_response.data["total_score"]), 80.0)
        self.assertIsNone(detail_response.data["passed"])
        self.assertFalse(detail_response.data["clinic_required"])

    def test_subjective_completion_finalizes_once_and_enables_projections(self):
        legacy, canonical = self._grade_objective_only()
        attempt = canonical.attempt
        ResultFact.objects.create(
            target_type="exam",
            target_id=self.exam.id,
            enrollment=self.enrollment,
            submission_id=self.submission.id,
            attempt_id=attempt.id,
            question_id=0,
            answer="",
            is_correct=True,
            score=20,
            max_score=20,
            source="manual_subjective",
            meta={"manual_subjective": True, "subjective_score": 20},
        )
        canonical.total_score = 100
        canonical.save(update_fields=["total_score", "updated_at"])

        from apps.domains.results.services.omr_subjective_completion import (
            finalize_omr_result_if_ready,
        )

        first = finalize_omr_result_if_ready(result_id=canonical.id)
        legacy.refresh_from_db()
        first_finalized_at = legacy.finalized_at
        second = finalize_omr_result_if_ready(result_id=canonical.id)
        legacy.refresh_from_db()

        self.assertTrue(first.projection_ready)
        self.assertTrue(first.transitioned)
        self.assertTrue(second.projection_ready)
        self.assertFalse(second.transitioned)
        self.assertEqual(legacy.status, ExamResult.Status.FINAL)
        self.assertEqual(legacy.finalized_at, first_finalized_at)
        self.assertEqual(float(legacy.objective_score), 80.0)
        self.assertEqual(float(legacy.subjective_score), 20.0)
        self.assertEqual(float(legacy.total_score), 100.0)

        request = SimpleNamespace(tenant=self.tenant, user=self.student.user)
        with patch(
            "apps.domains.results.services.student_result_service.get_request_student",
            return_value=self.student,
        ):
            payload = get_my_exam_result_data(request, self.exam.id, tenant=self.tenant)
        rankings = compute_exam_rankings(
            exam_id=self.exam.id,
            tenant=self.tenant,
            lecture_ids={self.lecture.id},
        )
        self.assertTrue(payload["student_results_published"])
        self.assertEqual(float(payload["total_score"]), 100.0)
        self.assertEqual(rankings[self.enrollment.id]["ranking_score"], 100.0)

    def test_subjective_score_endpoint_finalizes_pending_omr_and_dispatches_once(self):
        legacy, canonical = self._grade_objective_only()
        ScoreEditDraft.objects.create(
            tenant=self.tenant,
            session=self.session,
            editor_user=self.staff,
            client_id="mixed-omr-browser",
            payload={"client_id": "mixed-omr-browser", "changes": []},
        )
        request = self.factory.patch(
            "/results/admin/exams/subjective/",
            {"score": 20},
            format="json",
            HTTP_X_SCORE_EDITOR_CLIENT="mixed-omr-browser",
            HTTP_X_SCORE_SESSION_ID=str(self.session.id),
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.staff)

        with patch(
            "apps.domains.results.views.admin_exam_subjective_score_view.dispatch_progress_pipeline"
        ) as dispatch, self.captureOnCommitCallbacks(execute=True):
            response = AdminExamSubjectiveScoreView.as_view()(
                request,
                exam_id=self.exam.id,
                enrollment_id=self.enrollment.id,
            )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["ok"])
        self.assertTrue(response.data["saved"])
        self.assertTrue(response.data["projection_ready"])
        self.assertIsNone(response.data["grading_status"])
        legacy.refresh_from_db()
        canonical.refresh_from_db()
        self.assertEqual(legacy.status, ExamResult.Status.FINAL)
        self.assertEqual(float(legacy.total_score), 100.0)
        self.assertEqual(float(canonical.total_score), 100.0)
        dispatch.assert_called_once_with(submission_id=self.submission.id)

    def test_objective_quick_edit_saves_but_does_not_claim_final_success(self):
        legacy, _canonical = self._grade_objective_only()
        ScoreEditDraft.objects.create(
            tenant=self.tenant,
            session=self.session,
            editor_user=self.staff,
            client_id="mixed-omr-browser",
            payload={"client_id": "mixed-omr-browser", "changes": []},
        )
        request = self.factory.patch(
            "/results/admin/exams/objective/",
            {"score": 75},
            format="json",
            HTTP_X_SCORE_EDITOR_CLIENT="mixed-omr-browser",
            HTTP_X_SCORE_SESSION_ID=str(self.session.id),
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.staff)

        with patch(
            "apps.domains.results.views.admin_exam_objective_score_view.dispatch_progress_pipeline"
        ) as dispatch, self.captureOnCommitCallbacks(execute=True):
            response = AdminExamObjectiveScoreView.as_view()(
                request,
                exam_id=self.exam.id,
                enrollment_id=self.enrollment.id,
            )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data["ok"])
        self.assertTrue(response.data["saved"])
        self.assertFalse(response.data["projection_ready"])
        self.assertEqual(response.data["grading_status"], "subjective_pending")
        legacy.refresh_from_db()
        self.assertEqual(legacy.status, ExamResult.Status.DRAFT)
        dispatch.assert_not_called()

    def test_total_quick_edit_is_explicit_complete_score_and_finalizes(self):
        legacy, _canonical = self._grade_objective_only()
        ScoreEditDraft.objects.create(
            tenant=self.tenant,
            session=self.session,
            editor_user=self.staff,
            client_id="mixed-omr-browser",
            payload={"client_id": "mixed-omr-browser", "changes": []},
        )
        request = self.factory.patch(
            "/results/admin/exams/score/",
            {"score": 95, "max_score": 100},
            format="json",
            HTTP_X_SCORE_EDITOR_CLIENT="mixed-omr-browser",
            HTTP_X_SCORE_SESSION_ID=str(self.session.id),
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.staff)

        with patch(
            "apps.domains.results.views.admin_exam_total_score_view.dispatch_progress_pipeline"
        ) as dispatch:
            response = AdminExamTotalScoreView.as_view()(
                request,
                exam_id=self.exam.id,
                enrollment_id=self.enrollment.id,
            )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["ok"])
        self.assertTrue(response.data["saved"])
        self.assertTrue(response.data["projection_ready"])
        self.assertIsNone(response.data["grading_status"])
        legacy.refresh_from_db()
        self.assertEqual(legacy.status, ExamResult.Status.FINAL)
        self.assertEqual(float(legacy.total_score), 95.0)
        self.assertEqual(float(legacy.subjective_score), 15.0)
        dispatch.assert_called_once_with(submission_id=self.submission.id)

    def test_completed_subjective_score_survives_objective_regrade(self):
        legacy, canonical = self._grade_objective_only()
        ResultFact.objects.create(
            target_type="exam",
            target_id=self.exam.id,
            enrollment=self.enrollment,
            submission_id=self.submission.id,
            attempt_id=canonical.attempt_id,
            question_id=0,
            answer="",
            is_correct=True,
            score=20,
            max_score=20,
            source="manual_subjective",
            meta={"manual_subjective": True, "subjective_score": 20},
        )
        canonical.total_score = 100
        canonical.save(update_fields=["total_score", "updated_at"])
        from apps.domains.results.services.omr_subjective_completion import (
            finalize_omr_result_if_ready,
        )

        finalize_omr_result_if_ready(result_id=canonical.id)
        SubmissionAnswer.objects.filter(
            submission=self.submission,
            exam_question_id=self.choice.id,
        ).update(answer="2")
        self.submission.status = Submission.Status.ANSWERS_READY
        self.submission.save(update_fields=["status", "updated_at"])

        with patch(
            "apps.domains.results.services.grading_service.dispatch_progress_pipeline"
        ) as dispatch:
            regraded = grade_submission(self.submission.id, force_regrade=True)

        canonical.refresh_from_db()
        self.assertEqual(regraded.status, ExamResult.Status.FINAL)
        self.assertEqual(float(regraded.objective_score), 0.0)
        self.assertEqual(float(regraded.subjective_score), 20.0)
        self.assertEqual(float(regraded.total_score), 20.0)
        self.assertEqual(float(canonical.objective_score), 0.0)
        self.assertEqual(float(canonical.total_score), 20.0)
        dispatch.assert_called_once_with(submission_id=self.submission.id)

    def test_new_omr_attempt_does_not_reuse_prior_attempt_manual_essay_item(self):
        self.exam.allow_retake = True
        self.exam.max_attempts = 2
        self.exam.save(update_fields=["allow_retake", "max_attempts", "updated_at"])
        _legacy, canonical = self._grade_objective_only()
        ResultItem.objects.create(
            result=canonical,
            question=self.essay,
            answer="",
            is_correct=True,
            include_in_wrong_note=False,
            score=20,
            max_score=20,
            source="manual",
        )
        canonical.total_score = 100
        canonical.save(update_fields=["total_score", "updated_at"])
        prior_attempt_id = int(canonical.attempt_id)
        second_submission = self._new_omr_submission()

        second_legacy = grade_submission(second_submission.id)

        canonical.refresh_from_db()
        self.assertNotEqual(int(canonical.attempt_id), prior_attempt_id)
        self.assertEqual(second_legacy.status, ExamResult.Status.DRAFT)
        self.assertEqual(float(canonical.total_score), 80.0)
        self.assertFalse(
            ResultItem.objects.filter(
                result=canonical,
                question=self.essay,
                source="manual",
            ).exists()
        )

    def test_each_required_essay_item_must_be_scored_before_finalization(self):
        self.choice.score = 60
        self.choice.save(update_fields=["score", "updated_at"])
        second_essay = ExamQuestion.objects.create(
            sheet=self.sheet,
            number=3,
            score=20,
            question_kind=ExamQuestion.QuestionKind.ESSAY,
        )
        self.sheet.total_questions = 3
        self.sheet.essay_count = 2
        self.sheet.save(update_fields=["total_questions", "essay_count", "updated_at"])
        answer_key = AnswerKey.objects.get(exam=self.exam)
        answer_key.answers = {
            str(self.choice.id): "1",
            str(self.essay.id): "해설참조",
            str(second_essay.id): "해설참조",
        }
        answer_key.save(update_fields=["answers", "updated_at"])
        legacy, _canonical = self._grade_objective_only()
        ScoreEditDraft.objects.create(
            tenant=self.tenant,
            session=self.session,
            editor_user=self.staff,
            client_id="mixed-omr-browser",
            payload={"client_id": "mixed-omr-browser", "changes": []},
        )

        with patch(
            "apps.domains.results.views.admin_exam_item_score_view.dispatch_progress_pipeline"
        ) as dispatch:
            first = self._patch_item_score(question=self.essay, score=20)
            legacy.refresh_from_db()
            self.assertEqual(first.status_code, 200, first.data)
            self.assertFalse(first.data["ok"])
            self.assertTrue(first.data["saved"])
            self.assertFalse(first.data["projection_ready"])
            self.assertEqual(first.data["grading_status"], "subjective_pending")
            self.assertEqual(legacy.status, ExamResult.Status.DRAFT)
            dispatch.assert_not_called()

            second = self._patch_item_score(question=second_essay, score=20)

        legacy.refresh_from_db()
        self.assertEqual(second.status_code, 200, second.data)
        self.assertTrue(second.data["ok"])
        self.assertTrue(second.data["saved"])
        self.assertTrue(second.data["projection_ready"])
        self.assertIsNone(second.data["grading_status"])
        self.assertEqual(legacy.status, ExamResult.Status.FINAL)
        self.assertEqual(float(legacy.total_score), 100.0)
        dispatch.assert_called_once_with(submission_id=self.submission.id)

    def test_mismatched_omr_tenant_scope_fails_closed(self):
        legacy, canonical = self._grade_objective_only()
        other_tenant = Tenant.objects.create(
            code="mixed-omr-other",
            name="Mixed OMR other tenant",
            is_active=True,
        )
        Submission.objects.filter(id=self.submission.id).update(tenant=other_tenant)
        ResultFact.objects.create(
            target_type="exam",
            target_id=self.exam.id,
            enrollment=self.enrollment,
            submission_id=self.submission.id,
            attempt_id=canonical.attempt_id,
            question_id=0,
            answer="",
            is_correct=True,
            score=20,
            max_score=20,
            source="manual_subjective",
        )

        from apps.domains.results.services.omr_subjective_completion import (
            finalize_omr_result_if_ready,
        )

        decision = finalize_omr_result_if_ready(result_id=canonical.id)

        legacy.refresh_from_db()
        self.assertFalse(decision.projection_ready)
        self.assertFalse(decision.transitioned)
        self.assertEqual(decision.pending_reason, "invalid_omr_scope")
        self.assertEqual(legacy.status, ExamResult.Status.DRAFT)

    def test_shared_exam_finalizes_each_lecture_enrollment_independently(self):
        second_lecture = Lecture.objects.create(
            tenant=self.tenant,
            title="Mixed OMR second lecture",
            name="Mixed OMR second lecture",
            subject="MATH",
        )
        second_session = Session.objects.create(
            lecture=second_lecture,
            order=1,
            title="Mixed OMR second session",
        )
        second_user = User.objects.create_user(
            username="mixed-omr-second-student",
            password="pw1234",
            tenant=self.tenant,
        )
        second_student = Student.objects.create(
            tenant=self.tenant,
            user=second_user,
            name="Mixed OMR second student",
            ps_number="MIXED-OMR-2",
            omr_code="87654322",
        )
        second_enrollment = Enrollment.objects.create(
            tenant=self.tenant,
            student=second_student,
            lecture=second_lecture,
            status="ACTIVE",
        )
        SessionEnrollment.objects.create(
            tenant=self.tenant,
            session=second_session,
            enrollment=second_enrollment,
        )
        self.exam.sessions.add(second_session)
        ExamEnrollment.objects.create(
            exam=self.exam,
            enrollment=second_enrollment,
        )
        second_submission = Submission.objects.create(
            tenant=self.tenant,
            user=self.staff,
            enrollment=second_enrollment,
            target_type=Submission.TargetType.EXAM,
            target_id=self.exam.id,
            source=Submission.Source.OMR_SCAN,
            status=Submission.Status.ANSWERS_READY,
            meta={"manual_review": {"required": False}},
        )
        SubmissionAnswer.objects.create(
            tenant=self.tenant,
            submission=second_submission,
            exam_question_id=self.choice.id,
            answer="1",
        )
        _first_legacy, first_result = self._grade_objective_only()
        second_legacy = grade_submission(second_submission.id)
        second_result = Result.objects.get(
            target_type="exam",
            target_id=self.exam.id,
            enrollment=second_enrollment,
        )
        ResultFact.objects.create(
            target_type="exam",
            target_id=self.exam.id,
            enrollment=self.enrollment,
            submission_id=self.submission.id,
            attempt_id=first_result.attempt_id,
            question_id=0,
            answer="",
            is_correct=True,
            score=20,
            max_score=20,
            source="manual_subjective",
        )
        first_result.total_score = 100
        first_result.save(update_fields=["total_score", "updated_at"])

        from apps.domains.results.services.omr_subjective_completion import (
            finalize_omr_result_if_ready,
        )

        finalize_omr_result_if_ready(result_id=first_result.id)
        rankings = compute_exam_rankings(
            exam_id=self.exam.id,
            tenant=self.tenant,
            lecture_ids={self.lecture.id, second_lecture.id},
        )

        second_legacy.refresh_from_db()
        self.assertEqual(second_legacy.status, ExamResult.Status.DRAFT)
        self.assertIn(self.enrollment.id, rankings)
        self.assertNotIn(second_enrollment.id, rankings)
        self.assertEqual(float(second_result.objective_score), 80.0)

    def test_pure_objective_omr_keeps_immediate_finalization(self):
        exam = Exam.objects.create(
            tenant=self.tenant,
            title="Pure objective OMR",
            exam_type=Exam.ExamType.REGULAR,
            pass_score=50,
            max_score=100,
            student_results_published=True,
        )
        exam.sessions.add(self.session)
        ExamEnrollment.objects.create(exam=exam, enrollment=self.enrollment)
        sheet = Sheet.objects.create(
            exam=exam,
            name="OBJECTIVE",
            total_questions=1,
            choice_count=1,
            essay_count=0,
        )
        question = ExamQuestion.objects.create(
            sheet=sheet,
            number=1,
            score=100,
            question_kind=ExamQuestion.QuestionKind.CHOICE,
        )
        AnswerKey.objects.create(exam=exam, answers={str(question.id): "1"})
        submission = Submission.objects.create(
            tenant=self.tenant,
            user=self.staff,
            enrollment=self.enrollment,
            target_type=Submission.TargetType.EXAM,
            target_id=exam.id,
            source=Submission.Source.OMR_SCAN,
            status=Submission.Status.ANSWERS_READY,
            meta={"manual_review": {"required": False}},
        )
        SubmissionAnswer.objects.create(
            tenant=self.tenant,
            submission=submission,
            exam_question_id=question.id,
            answer="1",
        )

        with patch(
            "apps.domains.results.services.grading_service.dispatch_progress_pipeline"
        ) as dispatch:
            legacy = grade_submission(submission.id)

        self.assertEqual(legacy.status, ExamResult.Status.FINAL)
        self.assertIsNotNone(legacy.finalized_at)
        self.assertEqual(float(legacy.total_score), 100.0)
        dispatch.assert_called_once_with(submission_id=submission.id)
