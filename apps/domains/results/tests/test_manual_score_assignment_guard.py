from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.core.models import Tenant, TenantMembership
from apps.domains.enrollment.models import Enrollment, SessionEnrollment
from apps.domains.exams.models import AnswerKey, Exam, ExamEnrollment, ExamQuestion, Sheet
from apps.domains.lectures.models import Lecture, Session
from apps.domains.results.models import (
    ExamAttempt,
    Result,
    ResultFact,
    ResultItem,
    ScoreEditDraft,
)
from apps.domains.results.views.admin_exam_item_score_view import AdminExamItemScoreView
from apps.domains.results.views.admin_exam_objective_score_view import AdminExamObjectiveScoreView
from apps.domains.results.views.admin_exam_result_detail_view import AdminExamResultDetailView
from apps.domains.results.views.admin_exam_subjective_score_view import AdminExamSubjectiveScoreView
from apps.domains.results.views.admin_exam_total_score_view import AdminExamTotalScoreView
from apps.domains.results.views.session_scores_view import SessionScoresView
from apps.domains.results.utils.initial_exam_score import load_initial_exam_scores
from apps.domains.students.models import Student
from apps.support.results.session_scores_dependencies import Attendance


User = get_user_model()


class ManualExamScoreAssignmentGuardTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.tenant = Tenant.objects.create(name="Manual Guard", code="manual-guard", is_active=True)
        self.admin = User.objects.create_user(
            username="manual-guard-admin",
            password="pw1234",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=self.admin, role="admin")

        self.lecture = Lecture.objects.create(
            tenant=self.tenant,
            title="Lecture",
            name="Lecture",
            subject="MATH",
        )
        self.session = Session.objects.create(lecture=self.lecture, order=1, title="S1")
        self.exam = Exam.objects.create(
            tenant=self.tenant,
            title="Exam",
            exam_type=Exam.ExamType.REGULAR,
            max_score=100,
            pass_score=60,
        )
        self.exam.sessions.add(self.session)

        self.assigned_enrollment = self._create_enrollment("assigned")
        self.unassigned_enrollment = self._create_enrollment("unassigned")
        self.session_roster_enrollment = self._create_enrollment("session-roster")
        self.attendance_roster_enrollment = self._create_enrollment("attendance-roster")
        ExamEnrollment.objects.create(exam=self.exam, enrollment=self.assigned_enrollment)
        SessionEnrollment.objects.create(
            tenant=self.tenant,
            session=self.session,
            enrollment=self.session_roster_enrollment,
        )

        self.sheet = Sheet.objects.create(exam=self.exam, name="MAIN", total_questions=1)
        self.question = ExamQuestion.objects.create(sheet=self.sheet, number=1, score=5)

    def _create_enrollment(self, suffix: str) -> Enrollment:
        user = User.objects.create_user(
            username=f"manual-guard-{suffix}",
            password="pw1234",
            tenant=self.tenant,
        )
        student = Student.objects.create(
            tenant=self.tenant,
            user=user,
            name=f"Student {suffix}",
            ps_number=f"MG-{suffix}",
            omr_code=f"MG{suffix.upper()}"[:8],
        )
        return Enrollment.objects.create(
            tenant=self.tenant,
            lecture=self.lecture,
            student=student,
            status="ACTIVE",
        )

    def _patch(self, view_cls, data=None, enrollment=None, **kwargs):
        ScoreEditDraft.objects.update_or_create(
            session=self.session,
            tenant=self.tenant,
            editor_user=self.admin,
            defaults={"payload": {"client_id": "test-score-tab", "changes": []}},
        )
        request = self.factory.patch(
            "/results/admin/exams/manual/",
            data or {"score": 10},
            format="json",
            HTTP_X_SCORE_EDITOR_CLIENT="test-score-tab",
            HTTP_X_SCORE_SESSION_ID=str(self.session.id),
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)
        return view_cls.as_view()(
            request,
            exam_id=self.exam.id,
            enrollment_id=(enrollment or self.unassigned_enrollment).id,
            **kwargs,
        )

    def _patch_for_exam(self, view_cls, exam, data=None, enrollment=None, **kwargs):
        ScoreEditDraft.objects.update_or_create(
            session=self.session,
            tenant=self.tenant,
            editor_user=self.admin,
            defaults={"payload": {"client_id": "test-score-tab", "changes": []}},
        )
        request = self.factory.patch(
            "/results/admin/exams/manual/",
            data or {"score": 10},
            format="json",
            HTTP_X_SCORE_EDITOR_CLIENT="test-score-tab",
            HTTP_X_SCORE_SESSION_ID=str(self.session.id),
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)
        return view_cls.as_view()(
            request,
            exam_id=exam.id,
            enrollment_id=(enrollment or self.assigned_enrollment).id,
            **kwargs,
        )

    def _get_session_scores(self):
        request = self.factory.get("/results/admin/sessions/scores/")
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)
        return SessionScoresView.as_view()(request, session_id=self.session.id)

    def _create_structured_exam(self, title: str, choice_scores: list[float], essay_scores: list[float]):
        exam = Exam.objects.create(
            tenant=self.tenant,
            title=title,
            exam_type=Exam.ExamType.REGULAR,
            max_score=sum(choice_scores) + sum(essay_scores),
            pass_score=60,
        )
        exam.sessions.add(self.session)
        ExamEnrollment.objects.create(exam=exam, enrollment=self.assigned_enrollment)
        SessionEnrollment.objects.get_or_create(
            tenant=self.tenant,
            session=self.session,
            enrollment=self.assigned_enrollment,
        )
        sheet = Sheet.objects.create(
            exam=exam,
            name="MAIN",
            total_questions=len(choice_scores) + len(essay_scores),
            choice_count=len(choice_scores),
            essay_count=len(essay_scores),
        )
        questions = []
        for number, score in enumerate([*choice_scores, *essay_scores], start=1):
            questions.append(ExamQuestion.objects.create(sheet=sheet, number=number, score=score))
        return exam, questions

    def _create_zero_score_mixed_exam(self, title: str):
        exam = Exam.objects.create(
            tenant=self.tenant,
            title=title,
            exam_type=Exam.ExamType.REGULAR,
            max_score=100,
            pass_score=0,
        )
        exam.sessions.add(self.session)
        ExamEnrollment.objects.create(exam=exam, enrollment=self.assigned_enrollment)
        SessionEnrollment.objects.get_or_create(
            tenant=self.tenant,
            session=self.session,
            enrollment=self.assigned_enrollment,
        )
        sheet = Sheet.objects.create(
            exam=exam,
            name="MAIN",
            total_questions=2,
            choice_count=1,
            essay_count=1,
        )
        questions = [
            ExamQuestion.objects.create(sheet=sheet, number=1, score=0),
            ExamQuestion.objects.create(sheet=sheet, number=2, score=0),
        ]
        return exam, questions

    def _create_result(self, exam, objective_score: float):
        attempt = ExamAttempt.objects.create(
            exam=exam,
            enrollment=self.assigned_enrollment,
            submission_id=0,
            attempt_index=1,
            is_representative=True,
            status="done",
            meta={"total_score": float(objective_score), "max_score": float(exam.max_score or 0)},
        )
        return Result.objects.create(
            target_type="exam",
            target_id=exam.id,
            enrollment=self.assigned_enrollment,
            attempt=attempt,
            total_score=float(objective_score),
            max_score=float(exam.max_score or 0),
            objective_score=float(objective_score),
        )

    def _assert_no_manual_score_side_effects(self):
        self.assertFalse(
            ExamAttempt.objects.filter(
                exam=self.exam,
                enrollment=self.unassigned_enrollment,
            ).exists()
        )
        self.assertFalse(
            Result.objects.filter(
                target_type="exam",
                target_id=self.exam.id,
                enrollment=self.unassigned_enrollment,
            ).exists()
        )
        self.assertFalse(
            ResultFact.objects.filter(
                target_type="exam",
                target_id=self.exam.id,
                enrollment_id=self.unassigned_enrollment.id,
            ).exists()
        )
        self.assertFalse(
            ResultItem.objects.filter(
                result__target_type="exam",
                result__target_id=self.exam.id,
                result__enrollment=self.unassigned_enrollment,
            ).exists()
        )

    def test_total_score_rejects_unassigned_enrollment(self):
        response = self._patch(AdminExamTotalScoreView, {"score": 10, "max_score": 100})

        self.assertEqual(response.status_code, 400, response.data)
        self._assert_no_manual_score_side_effects()

    def test_total_score_accepts_linked_session_roster_and_materializes_exam_enrollment(self):
        ExamEnrollment.objects.filter(exam=self.exam).delete()
        response = self._patch(
            AdminExamTotalScoreView,
            {"score": 10, "max_score": 100},
            enrollment=self.session_roster_enrollment,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(
            ExamEnrollment.objects.filter(
                exam=self.exam,
                enrollment=self.session_roster_enrollment,
            ).exists()
        )
        self.assertTrue(
            Result.objects.filter(
                target_type="exam",
                target_id=self.exam.id,
                enrollment=self.session_roster_enrollment,
                total_score=10,
            ).exists()
        )

    def test_total_score_accepts_attendance_roster_and_materializes_exam_enrollment(self):
        ExamEnrollment.objects.filter(exam=self.exam).delete()
        Attendance.objects.create(
            tenant=self.tenant,
            session=self.session,
            enrollment=self.attendance_roster_enrollment,
            status="PRESENT",
        )
        response = self._patch(
            AdminExamTotalScoreView,
            {"score": 10, "max_score": 100},
            enrollment=self.attendance_roster_enrollment,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(
            ExamEnrollment.objects.filter(
                exam=self.exam,
                enrollment=self.attendance_roster_enrollment,
            ).exists()
        )

    def test_attendance_from_another_lecture_is_not_a_score_roster_assignment(self):
        other_lecture = Lecture.objects.create(
            tenant=self.tenant,
            title="Other Lecture",
            name="Other Lecture",
            subject="MATH",
        )
        cross_lecture_enrollment = self._create_enrollment("cross-lecture")
        cross_lecture_enrollment.lecture = other_lecture
        cross_lecture_enrollment.save(update_fields=["lecture"])
        Attendance.objects.create(
            tenant=self.tenant,
            session=self.session,
            enrollment=cross_lecture_enrollment,
            status="PRESENT",
        )

        score_response = self._get_session_scores()
        detail_request = self.factory.get("/results/admin/exams/detail/")
        detail_request.tenant = self.tenant
        force_authenticate(detail_request, user=self.admin)
        detail_response = AdminExamResultDetailView.as_view()(
            detail_request,
            exam_id=self.exam.id,
            enrollment_id=cross_lecture_enrollment.id,
        )
        write_response = self._patch(
            AdminExamTotalScoreView,
            {"score": 10, "max_score": 100},
            enrollment=cross_lecture_enrollment,
        )

        self.assertEqual(score_response.status_code, 200, score_response.data)
        self.assertNotIn(
            cross_lecture_enrollment.id,
            [row["enrollment_id"] for row in score_response.data["rows"]],
        )
        self.assertEqual(detail_response.status_code, 400, detail_response.data)
        self.assertEqual(write_response.status_code, 400, write_response.data)
        self.assertFalse(
            ExamEnrollment.objects.filter(
                exam=self.exam,
                enrollment=cross_lecture_enrollment,
            ).exists()
        )

    def test_objective_score_rejects_unassigned_enrollment(self):
        response = self._patch(AdminExamObjectiveScoreView, {"score": 10})

        self.assertEqual(response.status_code, 400, response.data)
        self._assert_no_manual_score_side_effects()

    def test_subjective_score_rejects_unassigned_enrollment(self):
        response = self._patch(AdminExamSubjectiveScoreView, {"score": 10})

        self.assertEqual(response.status_code, 400, response.data)
        self._assert_no_manual_score_side_effects()

    def test_item_score_rejects_unassigned_enrollment(self):
        response = self._patch(
            AdminExamItemScoreView,
            {"score": 3, "answer": "2"},
            question_id=self.question.id,
        )

        self.assertEqual(response.status_code, 400, response.data)
        self._assert_no_manual_score_side_effects()

    def test_manual_score_writes_reject_non_finite_numbers_without_side_effects(self):
        cases = (
            (AdminExamTotalScoreView, {"score": "NaN", "max_score": 100}, {}),
            (AdminExamTotalScoreView, {"score": 10, "max_score": "Infinity"}, {}),
            (AdminExamTotalScoreView, {"score": True, "max_score": 100}, {}),
            (AdminExamObjectiveScoreView, {"score": "Infinity"}, {}),
            (AdminExamSubjectiveScoreView, {"score": "-Infinity"}, {}),
            (
                AdminExamItemScoreView,
                {"score": "NaN", "answer": "2"},
                {"question_id": self.question.id},
            ),
        )

        for view_cls, payload, kwargs in cases:
            with self.subTest(view=view_cls.__name__, score=payload["score"]):
                response = self._patch(
                    view_cls,
                    payload,
                    enrollment=self.assigned_enrollment,
                    **kwargs,
                )
                self.assertEqual(response.status_code, 400, response.data)

        self.assertFalse(
            Result.objects.filter(
                target_type="exam",
                target_id=self.exam.id,
                enrollment=self.assigned_enrollment,
            ).exists()
        )
        self.assertFalse(
            ResultFact.objects.filter(
                target_type="exam",
                target_id=self.exam.id,
                enrollment_id=self.assigned_enrollment.id,
            ).exists()
        )

    @patch("apps.domains.results.views.admin_exam_item_score_view.dispatch_progress_pipeline")
    def test_item_score_recomputes_required_multi_choice_answer_on_server(self, mock_dispatch):
        AnswerKey.objects.create(
            exam=self.exam,
            answers={str(self.question.id): "2,3"},
        )

        partial_response = self._patch(
            AdminExamItemScoreView,
            {"score": 5, "answer": "2"},
            enrollment=self.assigned_enrollment,
            question_id=self.question.id,
        )

        self.assertEqual(partial_response.status_code, 200, partial_response.data)
        item = ResultItem.objects.get(
            result__target_type="exam",
            result__target_id=self.exam.id,
            result__enrollment=self.assigned_enrollment,
            question_id=self.question.id,
        )
        self.assertEqual(float(item.score), 0.0)
        self.assertFalse(item.is_correct)

        full_response = self._patch(
            AdminExamItemScoreView,
            {"score": 0, "answer": "2,3"},
            enrollment=self.assigned_enrollment,
            question_id=self.question.id,
        )

        self.assertEqual(full_response.status_code, 200, full_response.data)
        item.refresh_from_db()
        self.assertEqual(float(item.score), 5.0)
        self.assertTrue(item.is_correct)
        mock_dispatch.assert_called()

    @patch("apps.domains.results.views.admin_exam_subjective_score_view.dispatch_progress_pipeline")
    def test_subjective_score_adds_to_objective_score_with_essay_cap(self, mock_dispatch):
        exam, _questions = self._create_structured_exam("Mixed", [40, 40], [20])
        result = self._create_result(exam, objective_score=70)

        response = self._patch_for_exam(
            AdminExamSubjectiveScoreView,
            exam,
            {"score": 18},
        )

        self.assertEqual(response.status_code, 200, response.data)
        result.refresh_from_db()
        self.assertEqual(float(result.objective_score), 70.0)
        self.assertEqual(float(result.total_score), 88.0)
        self.assertEqual(float(result.max_score), 100.0)
        self.assertEqual(response.data["subjective_max_score"], 20.0)
        fact = ResultFact.objects.filter(
            target_type="exam",
            target_id=exam.id,
            enrollment_id=self.assigned_enrollment.id,
            source="manual_subjective",
        ).latest("id")
        self.assertEqual(float(fact.score), 18.0)
        self.assertEqual(float(fact.max_score), 20.0)

    @patch("apps.domains.results.views.admin_exam_subjective_score_view.dispatch_progress_pipeline")
    def test_subjective_score_updates_omr_first_attempt_projection_before_retake(self, mock_dispatch):
        exam, _questions = self._create_structured_exam("OMR then essay", [80], [20])
        result = self._create_result(exam, objective_score=80)
        first_attempt = result.attempt
        first_attempt.meta = {
            "total_score": 80.0,
            "max_score": 100.0,
            "initial_snapshot": {
                "total_score": 80.0,
                "max_score": 100.0,
                "submitted_at": "2026-09-05T10:55:38+09:00",
                "source": "submission_sync",
                "submission_id": 1234,
            },
        }
        first_attempt.save(update_fields=["meta", "updated_at"])

        response = self._patch_for_exam(
            AdminExamSubjectiveScoreView,
            exam,
            {"score": 15},
        )

        self.assertEqual(response.status_code, 200, response.data)
        first_attempt.refresh_from_db()
        snapshot = first_attempt.meta["initial_snapshot"]
        self.assertEqual(float(snapshot["total_score"]), 95.0)
        self.assertEqual(float(snapshot["max_score"]), 100.0)
        self.assertEqual(snapshot["source"], "submission_sync")
        self.assertEqual(snapshot["submission_id"], 1234)

        first_attempt.is_representative = False
        first_attempt.save(update_fields=["is_representative", "updated_at"])
        retake = ExamAttempt.objects.create(
            exam=exam,
            enrollment=self.assigned_enrollment,
            submission_id=0,
            attempt_index=2,
            is_retake=True,
            is_representative=True,
            status="done",
            meta={"total_score": 90.0, "max_score": 100.0},
        )
        result.attempt = retake
        result.total_score = 90.0
        result.save(update_fields=["attempt", "total_score", "updated_at"])

        projected = load_initial_exam_scores(
            exam_ids=[exam.id],
            enrollment_ids=[self.assigned_enrollment.id],
        )[(exam.id, self.assigned_enrollment.id)]
        self.assertEqual(projected.total_score, 95.0)
        self.assertEqual(projected.max_score, 100.0)

    @patch("apps.domains.results.views.admin_exam_subjective_score_view.dispatch_progress_pipeline")
    def test_subjective_score_accepts_direct_input_for_positive_essay_score_without_answer_key_entry(self, mock_dispatch):
        exam, questions = self._create_structured_exam("Mixed direct essay", [70], [30])
        AnswerKey.objects.create(exam=exam, answers={str(questions[0].id): "1"})
        result = self._create_result(exam, objective_score=42)

        response = self._patch_for_exam(
            AdminExamSubjectiveScoreView,
            exam,
            {"score": 27},
        )

        self.assertEqual(response.status_code, 200, response.data)
        result.refresh_from_db()
        self.assertEqual(float(result.objective_score), 42.0)
        self.assertEqual(float(result.total_score), 69.0)
        self.assertEqual(float(result.max_score), 100.0)
        self.assertEqual(response.data["subjective_max_score"], 30.0)
        fact = ResultFact.objects.filter(
            target_type="exam",
            target_id=exam.id,
            enrollment_id=self.assigned_enrollment.id,
            source="manual_subjective",
        ).latest("id")
        self.assertEqual(float(fact.score), 27.0)
        self.assertEqual(float(fact.max_score), 30.0)

    @patch("apps.domains.results.views.admin_exam_subjective_score_view.dispatch_progress_pipeline")
    def test_subjective_score_rejects_decorative_essay_space_without_positive_score(self, mock_dispatch):
        exam, _questions = self._create_zero_score_mixed_exam("Decorative essay direct")
        result = self._create_result(exam, objective_score=80)

        response = self._patch_for_exam(
            AdminExamSubjectiveScoreView,
            exam,
            {"score": 5},
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data["code"], "INVALID")
        self.assertIn("채점 대상 서술형", response.data["detail"])
        result.refresh_from_db()
        self.assertEqual(float(result.total_score), 80.0)
        self.assertFalse(
            ResultFact.objects.filter(
                target_type="exam",
                target_id=exam.id,
                source="manual_subjective",
            ).exists()
        )
        mock_dispatch.assert_not_called()

    @patch("apps.domains.results.views.admin_exam_objective_score_view.dispatch_progress_pipeline")
    def test_objective_score_rejects_essay_only_exam(self, mock_dispatch):
        exam, _questions = self._create_structured_exam("Essay only", [], [100])
        result = self._create_result(exam, objective_score=0)

        response = self._patch_for_exam(
            AdminExamObjectiveScoreView,
            exam,
            {"score": 5},
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data["code"], "INVALID")
        self.assertIn("채점 대상 선택형", response.data["detail"])
        result.refresh_from_db()
        self.assertEqual(float(result.total_score), 0.0)
        self.assertFalse(
            ResultFact.objects.filter(
                target_type="exam",
                target_id=exam.id,
                source="manual_objective",
            ).exists()
        )
        mock_dispatch.assert_not_called()

    @patch("apps.domains.results.views.admin_exam_subjective_score_view.dispatch_progress_pipeline")
    def test_subjective_score_rejects_score_above_essay_max(self, mock_dispatch):
        exam, _questions = self._create_structured_exam("Mixed cap", [40, 40], [20])
        result = self._create_result(exam, objective_score=70)

        response = self._patch_for_exam(
            AdminExamSubjectiveScoreView,
            exam,
            {"score": 21},
        )

        self.assertEqual(response.status_code, 400, response.data)
        result.refresh_from_db()
        self.assertEqual(float(result.total_score), 70.0)
        mock_dispatch.assert_not_called()

    @patch("apps.domains.results.views.admin_exam_subjective_score_view.dispatch_progress_pipeline")
    def test_subjective_score_rejects_objective_only_sheet(self, mock_dispatch):
        exam, _questions = self._create_structured_exam("Objective only", [50, 50], [])
        result = self._create_result(exam, objective_score=80)

        response = self._patch_for_exam(
            AdminExamSubjectiveScoreView,
            exam,
            {"score": 5},
        )

        self.assertEqual(response.status_code, 400, response.data)
        result.refresh_from_db()
        self.assertEqual(float(result.total_score), 80.0)
        mock_dispatch.assert_not_called()

    @patch("apps.domains.results.views.admin_exam_objective_score_view.dispatch_progress_pipeline")
    def test_objective_score_rejects_score_above_objective_max(self, mock_dispatch):
        exam, _questions = self._create_structured_exam("Objective cap", [40, 40], [20])
        result = self._create_result(exam, objective_score=70)

        response = self._patch_for_exam(
            AdminExamObjectiveScoreView,
            exam,
            {"score": 90},
        )

        self.assertEqual(response.status_code, 400, response.data)
        result.refresh_from_db()
        self.assertEqual(float(result.objective_score), 70.0)
        self.assertEqual(float(result.total_score), 70.0)
        mock_dispatch.assert_not_called()

    @patch("apps.domains.results.views.admin_exam_item_score_view.dispatch_progress_pipeline")
    def test_item_score_preserves_manual_subjective_total_when_objective_item_changes(self, mock_dispatch):
        exam, questions = self._create_structured_exam("Item plus subjective", [40, 40], [20])
        result = self._create_result(exam, objective_score=80)
        result.total_score = 90
        result.save(update_fields=["total_score", "updated_at"])
        ResultFact.objects.create(
            target_type="exam",
            target_id=exam.id,
            enrollment=self.assigned_enrollment,
            submission_id=0,
            attempt=result.attempt,
            question_id=0,
            answer="",
            is_correct=True,
            score=10,
            max_score=20,
            source="manual_subjective",
            meta={"manual_subjective": True},
        )
        ResultItem.objects.create(
            result=result,
            question=questions[0],
            answer="1",
            is_correct=True,
            score=40,
            max_score=40,
            source="omr",
        )
        ResultItem.objects.create(
            result=result,
            question=questions[1],
            answer="2",
            is_correct=True,
            score=40,
            max_score=40,
            source="omr",
        )

        response = self._patch_for_exam(
            AdminExamItemScoreView,
            exam,
            {"score": 30, "answer": "1"},
            question_id=questions[0].id,
        )

        self.assertEqual(response.status_code, 200, response.data)
        result.refresh_from_db()
        self.assertEqual(float(result.objective_score), 70.0)
        self.assertEqual(float(result.total_score), 80.0)
        self.assertEqual(float(result.max_score), 100.0)
        mock_dispatch.assert_called()

    @patch("apps.domains.results.views.admin_exam_item_score_view.dispatch_progress_pipeline")
    def test_item_score_drops_stale_subjective_difference_without_manual_evidence(self, mock_dispatch):
        exam, questions = self._create_structured_exam("Item stale subjective", [40, 40], [20])
        result = self._create_result(exam, objective_score=80)
        result.total_score = 90
        result.save(update_fields=["total_score", "updated_at"])
        ResultItem.objects.create(
            result=result,
            question=questions[0],
            answer="1",
            is_correct=True,
            score=40,
            max_score=40,
            source="omr",
        )
        ResultItem.objects.create(
            result=result,
            question=questions[1],
            answer="2",
            is_correct=True,
            score=40,
            max_score=40,
            source="omr",
        )

        response = self._patch_for_exam(
            AdminExamItemScoreView,
            exam,
            {"score": 30, "answer": "1"},
            question_id=questions[0].id,
        )

        self.assertEqual(response.status_code, 200, response.data)
        result.refresh_from_db()
        self.assertEqual(float(result.objective_score), 70.0)
        self.assertEqual(float(result.total_score), 70.0)
        self.assertEqual(float(result.max_score), 100.0)
        mock_dispatch.assert_called()

    @patch("apps.domains.results.views.admin_exam_item_score_view.dispatch_progress_pipeline")
    def test_item_score_rejects_positive_score_for_decorative_essay_space(self, mock_dispatch):
        exam, questions = self._create_zero_score_mixed_exam("Zero score mixed manual")
        essay = questions[1]

        response = self._patch_for_exam(
            AdminExamItemScoreView,
            exam,
            {"score": 40, "answer": "manual"},
            question_id=essay.id,
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data["code"], "INVALID")
        self.assertIn("score must be between 0 and 0.0", response.data["detail"])
        self.assertFalse(
            ResultItem.objects.filter(
                result__target_type="exam",
                result__target_id=exam.id,
                question=essay,
            ).exists()
        )

        session_response = self._get_session_scores()
        self.assertEqual(session_response.status_code, 200, session_response.data)
        exam_meta = next(e for e in session_response.data["meta"]["exams"] if e["exam_id"] == exam.id)
        self.assertEqual(exam_meta["objective_max_score"], 100.0)
        self.assertEqual(exam_meta["subjective_max_score"], 0.0)
        self.assertIn("decorative_essay", exam_meta["score_shape_source"])
        self.assertEqual(
            [(q["number"], q["kind"], q["max_score"]) for q in exam_meta["questions"]],
            [(1, "choice", 100.0), (2, "essay", 0.0)],
        )
        mock_dispatch.assert_not_called()

    def test_score_shape_treats_zero_score_mixed_sheet_essay_as_decorative_without_essay_evidence(self):
        exam, _questions = self._create_zero_score_mixed_exam("Zero score mixed metadata")

        session_response = self._get_session_scores()

        self.assertEqual(session_response.status_code, 200, session_response.data)
        exam_meta = next(e for e in session_response.data["meta"]["exams"] if e["exam_id"] == exam.id)
        self.assertEqual(exam_meta["objective_max_score"], 100.0)
        self.assertEqual(exam_meta["subjective_max_score"], 0.0)
        self.assertIn("decorative_essay", exam_meta["score_shape_source"])
        self.assertEqual(
            [(q["number"], q["kind"], q["max_score"]) for q in exam_meta["questions"]],
            [(1, "choice", 100.0), (2, "essay", 0.0)],
        )

        request = self.factory.get("/results/admin/exams/detail/")
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)
        detail_response = AdminExamResultDetailView.as_view()(
            request,
            exam_id=exam.id,
            enrollment_id=self.assigned_enrollment.id,
        )

        self.assertEqual(detail_response.status_code, 200, detail_response.data)
        self.assertEqual(
            [(q["number"], q["kind"], q["max_score"]) for q in detail_response.data["questions"]],
            [(1, "choice", 100.0), (2, "essay", 0.0)],
        )

    def test_detail_for_assigned_student_is_read_only_when_result_is_missing(self):
        request = self.factory.get("/results/admin/exams/detail/")
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)

        response = AdminExamResultDetailView.as_view()(
            request,
            exam_id=self.exam.id,
            enrollment_id=self.assigned_enrollment.id,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNone(response.data["total_score"])
        self.assertIsNone(response.data["attempt_id"])
        self.assertFalse(
            Result.objects.filter(
                target_type="exam",
                target_id=self.exam.id,
                enrollment=self.assigned_enrollment,
            ).exists()
        )
        self.assertFalse(
            ExamAttempt.objects.filter(
                exam=self.exam,
                enrollment=self.assigned_enrollment,
            ).exists()
        )

    def test_detail_for_session_roster_is_read_only_without_materializing_assignment(self):
        ExamEnrollment.objects.filter(exam=self.exam).delete()
        request = self.factory.get("/results/admin/exams/detail/")
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)

        response = AdminExamResultDetailView.as_view()(
            request,
            exam_id=self.exam.id,
            enrollment_id=self.session_roster_enrollment.id,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNone(response.data["total_score"])
        self.assertFalse(
            ExamEnrollment.objects.filter(
                exam=self.exam,
                enrollment=self.session_roster_enrollment,
            ).exists()
        )
        self.assertFalse(
            Result.objects.filter(
                target_type="exam",
                target_id=self.exam.id,
                enrollment=self.session_roster_enrollment,
            ).exists()
        )
        self.assertFalse(
            ExamAttempt.objects.filter(
                exam=self.exam,
                enrollment=self.session_roster_enrollment,
            ).exists()
        )

    def test_detail_for_attendance_roster_is_read_only_without_materializing_assignment(self):
        ExamEnrollment.objects.filter(exam=self.exam).delete()
        Attendance.objects.create(
            tenant=self.tenant,
            session=self.session,
            enrollment=self.attendance_roster_enrollment,
            status="PRESENT",
        )
        Result.objects.create(
            target_type="exam",
            target_id=self.exam.id,
            enrollment=self.attendance_roster_enrollment,
            total_score=0,
            max_score=100,
        )
        request = self.factory.get("/results/admin/exams/detail/")
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)

        response = AdminExamResultDetailView.as_view()(
            request,
            exam_id=self.exam.id,
            enrollment_id=self.attendance_roster_enrollment.id,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(float(response.data["total_score"]), 0.0)
        self.assertFalse(
            ExamEnrollment.objects.filter(
                exam=self.exam,
                enrollment=self.attendance_roster_enrollment,
            ).exists()
        )

    def test_detail_rejects_unassigned_student_without_materializing_result(self):
        request = self.factory.get("/results/admin/exams/detail/")
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)

        response = AdminExamResultDetailView.as_view()(
            request,
            exam_id=self.exam.id,
            enrollment_id=self.unassigned_enrollment.id,
        )

        self.assertEqual(response.status_code, 400, response.data)
        self._assert_no_manual_score_side_effects()

    def test_detail_exposes_positive_essay_score_without_answer_placeholder(self):
        exam, questions = self._create_structured_exam("Essay no placeholder", [70], [30])
        self._create_result(exam, objective_score=70)

        request = self.factory.get("/results/admin/exams/detail/")
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)
        response = AdminExamResultDetailView.as_view()(
            request,
            exam_id=exam.id,
            enrollment_id=self.assigned_enrollment.id,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["score_shape"]["objective_max_score"], 70.0)
        self.assertEqual(response.data["score_shape"]["subjective_max_score"], 30.0)
        self.assertEqual(response.data["score_shape"]["total_max_score"], 100.0)
        self.assertEqual(
            [(q["number"], q["kind"], q["max_score"]) for q in response.data["questions"]],
            [(1, "choice", 70.0), (2, "essay", 30.0)],
        )
        self.assertNotIn(str(questions[1].id), response.data["correct_answers"])

    def test_detail_exposes_user_entered_essay_answer_when_present(self):
        exam, questions = self._create_structured_exam("Essay answer present", [70], [30])
        AnswerKey.objects.create(
            exam=exam,
            answers={
                str(questions[0].id): "1",
                str(questions[1].id): "서술 정답",
            },
        )
        self._create_result(exam, objective_score=70)

        request = self.factory.get("/results/admin/exams/detail/")
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)
        response = AdminExamResultDetailView.as_view()(
            request,
            exam_id=exam.id,
            enrollment_id=self.assigned_enrollment.id,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["score_shape"]["subjective_max_score"], 30.0)
        self.assertEqual(response.data["correct_answers"][str(questions[1].id)], "서술 정답")

    def test_session_scores_meta_exposes_score_shape_and_component_scores(self):
        exam, _questions = self._create_structured_exam("Scores meta", [40, 40], [20])
        result = self._create_result(exam, objective_score=70)
        result.total_score = 88
        result.save(update_fields=["total_score", "updated_at"])

        response = self._get_session_scores()

        self.assertEqual(response.status_code, 200, response.data)
        exam_meta = next(e for e in response.data["meta"]["exams"] if e["exam_id"] == exam.id)
        self.assertEqual(exam_meta["choice_count"], 2)
        self.assertEqual(exam_meta["essay_count"], 1)
        self.assertEqual(exam_meta["objective_max_score"], 80.0)
        self.assertEqual(exam_meta["subjective_max_score"], 20.0)
        row = next(r for r in response.data["rows"] if r["enrollment_id"] == self.assigned_enrollment.id)
        entry = next(e for e in row["exams"] if e["exam_id"] == exam.id)
        self.assertEqual(entry["block"]["objective_score"], 70.0)
        self.assertEqual(entry["block"]["subjective_score"], 18.0)
        self.assertEqual(entry["block"]["score"], 88.0)
