from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import resolve
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.core.models import OpsAuditLog, Tenant, TenantMembership
from apps.domains.enrollment.models import Enrollment
from apps.domains.exams.models import AnswerKey, Exam, ExamEnrollment, ExamQuestion, Sheet
from apps.domains.lectures.models import Lecture, Session
from apps.domains.parents.models import Parent
from apps.domains.results.models import ExamAttempt, Result, ResultItem
from apps.domains.student_app.exams.views import (
    StudentExamDetailView,
    StudentExamListView,
    StudentExamQuestionsView,
    StudentExamSubmitView,
)
from apps.domains.student_app.results.views import MyExamResultView
from apps.domains.students.models import Student
from apps.domains.submissions.models import Submission


User = get_user_model()


class ParentExamChildSelectionTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.tenant = Tenant.objects.create(code="parent-exams", name="Parent Exams", is_active=True)
        self.parent_user = User.objects.create_user(
            username="parent-exams-parent",
            password="pw1234",
            tenant=self.tenant,
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=self.parent_user, role="parent")
        self.parent = Parent.objects.create(
            tenant=self.tenant,
            user=self.parent_user,
            name="Parent",
            phone="01011112222",
        )
        self.student_a = self._student("a", "A")
        self.student_b = self._student("b", "B")
        self.exam_a, self.enrollment_a, self.question_a = self._exam_for_student(self.student_a, "A Exam")
        self.exam_b, self.enrollment_b, self.question_b = self._exam_for_student(self.student_b, "B Exam")
        self.result_a = Result.objects.create(
            target_type="exam",
            target_id=self.exam_a.id,
            enrollment=self.enrollment_a,
            total_score=10,
            max_score=10,
            objective_score=10,
            submitted_at=timezone.now(),
        )
        ResultItem.objects.create(
            result=self.result_a,
            question=self.question_a,
            answer="1",
            is_correct=True,
            score=10,
            max_score=10,
            source="online",
        )

    def _student(self, suffix: str, name: str) -> Student:
        user = User.objects.create_user(
            username=f"parent-exams-student-{suffix}",
            password="pw1234",
            tenant=self.tenant,
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=user, role="student")
        return Student.objects.create(
            tenant=self.tenant,
            user=user,
            parent=self.parent,
            ps_number=f"PE-{suffix}",
            omr_code=f"PE{suffix.upper()}0000"[:8],
            name=name,
        )

    def _exam_for_student(self, student: Student, title: str):
        lecture = Lecture.objects.create(
            tenant=self.tenant,
            title=f"{title} Lecture",
            name=f"{title} Lecture",
            subject="MATH",
        )
        session = Session.objects.create(lecture=lecture, order=1, title="1회")
        enrollment = Enrollment.objects.create(
            tenant=self.tenant,
            student=student,
            lecture=lecture,
            status="ACTIVE",
        )
        exam = Exam.objects.create(
            tenant=self.tenant,
            title=title,
            exam_type=Exam.ExamType.REGULAR,
            is_active=True,
            pass_score=0,
            max_score=10,
            answer_visibility=Exam.AnswerVisibility.HIDDEN,
        )
        exam.sessions.add(session)
        ExamEnrollment.objects.create(exam=exam, enrollment=enrollment)
        sheet = Sheet.objects.create(exam=exam, name="MAIN", total_questions=1)
        question = ExamQuestion.objects.create(sheet=sheet, number=1, score=10)
        return exam, enrollment, question

    def _request(self, path: str, *, student: Student):
        request = self.factory.get(path, HTTP_X_STUDENT_ID=str(student.id))
        force_authenticate(request, user=self.parent_user)
        request.tenant = self.tenant
        return request

    def _post_request(self, path: str, *, student: Student, data: dict):
        request = self.factory.post(
            path,
            data,
            format="json",
            HTTP_X_STUDENT_ID=str(student.id),
        )
        force_authenticate(request, user=self.parent_user)
        request.tenant = self.tenant
        return request

    def _student_post_request(self, path: str, *, student: Student, data: dict):
        request = self.factory.post(path, data, format="json")
        force_authenticate(request, user=student.user)
        request.tenant = self.tenant
        return request

    def test_parent_exam_list_uses_selected_child(self):
        view = StudentExamListView.as_view()

        response_a = view(self._request("/student/exams/", student=self.student_a))
        response_b = view(self._request("/student/exams/", student=self.student_b))

        self.assertEqual(response_a.status_code, 200)
        self.assertEqual([row["id"] for row in response_a.data["items"]], [self.exam_a.id])
        self.assertEqual(response_b.status_code, 200)
        self.assertEqual([row["id"] for row in response_b.data["items"]], [self.exam_b.id])

    def test_parent_request_without_selected_child_fails_closed(self):
        request = self.factory.get("/student/exams/")
        force_authenticate(request, user=self.parent_user)
        request.tenant = self.tenant

        response = StudentExamListView.as_view()(request)

        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(response.data["detail"], "자녀를 선택한 뒤 다시 시도해 주세요.")

    @patch("apps.domains.student_app.exams.views.dispatch_student_exam_submission")
    def test_parent_stale_deleted_child_selection_cannot_submit(self, mock_dispatch):
        self.student_b.deleted_at = timezone.now()
        self.student_b.save(update_fields=["deleted_at", "updated_at"])

        response = StudentExamSubmitView.as_view()(
            self._post_request(
                f"/student/exams/{self.exam_b.id}/submit/",
                student=self.student_b,
                data={
                    "answers": [
                        {"exam_question_id": self.question_b.id, "answer": "1"}
                    ]
                },
            ),
            pk=self.exam_b.id,
        )

        self.assertEqual(response.status_code, 403, response.data)
        self.assertFalse(
            Submission.objects.filter(
                enrollment=self.enrollment_b,
                target_type=Submission.TargetType.EXAM,
                target_id=self.exam_b.id,
            ).exists()
        )
        mock_dispatch.assert_not_called()

    @patch("apps.domains.student_app.exams.views.dispatch_student_exam_submission")
    def test_submit_rejects_max_attempts_before_superseding_current_done(self, mock_dispatch):
        self.exam_b.allow_retake = True
        self.exam_b.max_attempts = 2
        self.exam_b.save(update_fields=["allow_retake", "max_attempts", "updated_at"])
        first = Submission.objects.create(
            tenant=self.tenant,
            user=self.student_b.user,
            enrollment=self.enrollment_b,
            target_type=Submission.TargetType.EXAM,
            target_id=self.exam_b.id,
            source=Submission.Source.ONLINE,
            status=Submission.Status.SUPERSEDED,
        )
        second = Submission.objects.create(
            tenant=self.tenant,
            user=self.student_b.user,
            enrollment=self.enrollment_b,
            target_type=Submission.TargetType.EXAM,
            target_id=self.exam_b.id,
            source=Submission.Source.ONLINE,
            status=Submission.Status.DONE,
        )
        ExamAttempt.objects.create(
            exam=self.exam_b,
            enrollment=self.enrollment_b,
            submission_id=first.id,
            attempt_index=1,
            status="done",
            is_representative=False,
        )
        ExamAttempt.objects.create(
            exam=self.exam_b,
            enrollment=self.enrollment_b,
            submission_id=second.id,
            attempt_index=2,
            status="done",
            is_representative=True,
            is_retake=True,
        )

        detail = StudentExamDetailView.as_view()(
            self._request(f"/student/exams/{self.exam_b.id}/", student=self.student_b),
            pk=self.exam_b.id,
        )
        self.assertTrue(detail.data["has_result"])
        self.assertEqual(detail.data["attempt_count"], 2)

        response = StudentExamSubmitView.as_view()(
            self._student_post_request(
                f"/student/exams/{self.exam_b.id}/submit/",
                student=self.student_b,
                data={"answers": [{"exam_question_id": self.question_b.id, "answer": "1"}]},
            ),
            pk=self.exam_b.id,
        )

        self.assertEqual(response.status_code, 409, response.data)
        second.refresh_from_db()
        self.assertEqual(second.status, Submission.Status.DONE)
        self.assertEqual(
            Submission.objects.filter(
                enrollment=self.enrollment_b,
                target_type=Submission.TargetType.EXAM,
                target_id=self.exam_b.id,
            ).count(),
            2,
        )
        mock_dispatch.assert_not_called()

    @patch("apps.domains.student_app.exams.views.dispatch_student_exam_submission")
    def test_unlinked_lecture_blocks_student_exam_endpoints(self, mock_dispatch):
        own_session = self.exam_a.sessions.get(lecture=self.enrollment_a.lecture)
        self.exam_a.sessions.add(self.exam_b.sessions.get())
        self.exam_a.sessions.remove(own_session)

        list_response = StudentExamListView.as_view()(
            self._request("/student/exams/", student=self.student_a)
        )
        questions_response = StudentExamQuestionsView.as_view()(
            self._request(
                f"/student/exams/{self.exam_a.id}/questions/",
                student=self.student_a,
            ),
            pk=self.exam_a.id,
        )
        Submission.objects.create(
            tenant=self.tenant,
            user=self.student_a.user,
            enrollment=self.enrollment_a,
            target_type=Submission.TargetType.EXAM,
            target_id=self.exam_a.id,
            source=Submission.Source.ONLINE,
            status=Submission.Status.SUBMITTED,
        )
        submit_response = StudentExamSubmitView.as_view()(
            self._student_post_request(
                f"/student/exams/{self.exam_a.id}/submit/",
                student=self.student_a,
                data={"answers": [{"exam_question_id": self.question_a.id, "answer": "1"}]},
            ),
            pk=self.exam_a.id,
        )

        self.assertNotIn(self.exam_a.id, [row["id"] for row in list_response.data["items"]])
        self.assertEqual(questions_response.status_code, 404)
        self.assertEqual(submit_response.status_code, 404)
        mock_dispatch.assert_not_called()

    def test_ended_lecture_hides_active_exam_but_preserves_published_result(self):
        lecture = self.enrollment_a.lecture
        lecture.is_active = False
        lecture.save(update_fields=["is_active", "updated_at"])

        exam_response = StudentExamListView.as_view()(
            self._request("/student/exams/", student=self.student_a)
        )
        result_response = MyExamResultView.as_view()(
            self._request(
                f"/student/results/me/exams/{self.exam_a.id}/",
                student=self.student_a,
            ),
            exam_id=self.exam_a.id,
        )

        self.assertEqual(exam_response.status_code, 200)
        self.assertNotIn(
            self.exam_a.id,
            [row["id"] for row in exam_response.data["items"]],
        )
        self.assertEqual(result_response.status_code, 200, result_response.data)
        self.assertEqual(result_response.data["total_score"], 10)

    def test_exam_list_can_include_upcoming_dashboard_window(self):
        view = StudentExamListView.as_view()
        future_exam, _, _ = self._exam_for_student(self.student_a, "Upcoming Exam")
        future_exam.open_at = timezone.now() + timedelta(days=3)
        future_exam.save(update_fields=["open_at"])

        default_response = view(self._request("/student/exams/", student=self.student_a))
        upcoming_response = view(
            self._request("/student/exams/?include_upcoming=true", student=self.student_a)
        )

        self.assertEqual(default_response.status_code, 200)
        self.assertNotIn(future_exam.id, [row["id"] for row in default_response.data["items"]])
        self.assertEqual(upcoming_response.status_code, 200)
        self.assertIn(future_exam.id, [row["id"] for row in upcoming_response.data["items"]])

    def test_exam_list_excludes_ended_lecture_from_ongoing_count(self):
        self.enrollment_a.lecture.is_active = False
        self.enrollment_a.lecture.save(update_fields=["is_active", "updated_at"])

        response = StudentExamListView.as_view()(
            self._request("/student/exams/?include_upcoming=true", student=self.student_a)
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn(self.exam_a.id, [row["id"] for row in response.data["items"]])

    def test_parent_exam_result_uses_selected_child(self):
        view = MyExamResultView.as_view()

        response_a = view(
            self._request(f"/student/results/me/exams/{self.exam_a.id}/", student=self.student_a),
            exam_id=self.exam_a.id,
        )
        response_b = view(
            self._request(f"/student/results/me/exams/{self.exam_a.id}/", student=self.student_b),
            exam_id=self.exam_a.id,
        )

        self.assertEqual(response_a.status_code, 200, response_a.data)
        self.assertEqual(response_a.data["total_score"], 10)
        self.assertEqual(response_b.status_code, 404)

    def test_exam_result_get_is_read_only_and_explicit_activity_post_is_scoped(self):
        result_response = MyExamResultView.as_view()(
            self._request(
                f"/student/results/me/exams/{self.exam_a.id}/",
                student=self.student_a,
            ),
            exam_id=self.exam_a.id,
        )

        self.assertEqual(result_response.status_code, 200, result_response.data)
        self.assertFalse(
            OpsAuditLog.objects.filter(action="student_activity.target_open").exists()
        )

        activity_view = resolve(
            "/api/v1/students/me/activity/exam-result-open/"
        ).func
        parent_activity_response = activity_view(
            self._post_request(
                "/students/me/activity/exam-result-open/",
                student=self.student_a,
                data={"exam_id": self.exam_a.id},
            )
        )

        self.assertEqual(parent_activity_response.status_code, 202, parent_activity_response.data)
        self.assertFalse(parent_activity_response.data["accepted"])
        self.assertFalse(
            OpsAuditLog.objects.filter(action="student_activity.target_open").exists()
        )

        activity_response = activity_view(
            self._student_post_request(
                "/students/me/activity/exam-result-open/",
                student=self.student_a,
                data={"exam_id": self.exam_a.id},
            )
        )

        self.assertEqual(activity_response.status_code, 202, activity_response.data)
        self.assertTrue(activity_response.data["accepted"])
        activity = OpsAuditLog.objects.get(action="student_activity.target_open")
        self.assertEqual(activity.target_user_id, self.student_a.user_id)
        self.assertEqual(activity.payload["target_id"], str(self.exam_a.id))

    def test_exam_result_activity_rejects_other_selected_child(self):
        response = resolve(
            "/api/v1/students/me/activity/exam-result-open/"
        ).func(
            self._post_request(
                "/students/me/activity/exam-result-open/",
                student=self.student_b,
                data={"exam_id": self.exam_a.id},
            )
        )

        self.assertEqual(response.status_code, 404, response.data)
        self.assertFalse(
            OpsAuditLog.objects.filter(action="student_activity.target_open").exists()
        )

    def test_unpublished_result_keeps_retake_policy_without_exposing_score(self):
        self.exam_a.student_results_published = False
        self.exam_a.save(update_fields=["student_results_published", "updated_at"])

        response = MyExamResultView.as_view()(
            self._request(
                f"/student/results/me/exams/{self.exam_a.id}/",
                student=self.student_a,
            ),
            exam_id=self.exam_a.id,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["exam_id"], self.exam_a.id)
        self.assertFalse(response.data["student_results_published"])
        self.assertFalse(response.data["can_retake"])
        self.assertNotIn("total_score", response.data)
        self.assertNotIn("items", response.data)
        self.assertNotIn("rank", response.data)

    @patch("apps.domains.submissions.services.dispatcher.dispatch_submission")
    def test_numeric_short_answer_contract_rejects_invalid_and_normalizes_leading_zeroes(
        self,
        mock_dispatch,
    ):
        sheet = self.question_a.sheet
        sheet.choice_count = 0
        sheet.essay_count = 1
        sheet.save(update_fields=["choice_count", "essay_count", "updated_at"])
        AnswerKey.objects.create(
            exam=self.exam_a,
            answers={str(self.question_a.id): "7"},
        )

        questions_response = StudentExamQuestionsView.as_view()(
            self._request(
                f"/student/exams/{self.exam_a.id}/questions/",
                student=self.student_a,
            ),
            pk=self.exam_a.id,
        )
        invalid_response = StudentExamSubmitView.as_view()(
            self._post_request(
                f"/student/exams/{self.exam_a.id}/submit/",
                student=self.student_a,
                data={
                    "answers": [
                        {"exam_question_id": self.question_a.id, "answer": "1000"}
                    ]
                },
            ),
            pk=self.exam_a.id,
        )
        valid_response = StudentExamSubmitView.as_view()(
            self._post_request(
                f"/student/exams/{self.exam_a.id}/submit/",
                student=self.student_a,
                data={
                    "answers": [
                        {"exam_question_id": self.question_a.id, "answer": "007"}
                    ]
                },
            ),
            pk=self.exam_a.id,
        )

        self.assertEqual(questions_response.status_code, 200, questions_response.data)
        self.assertEqual(questions_response.data[0]["answer_format"], "integer_0_999")
        self.assertEqual(invalid_response.status_code, 400, invalid_response.data)
        self.assertIn("0~999", invalid_response.data["detail"])
        self.assertEqual(valid_response.status_code, 201, valid_response.data)
        submission = Submission.objects.get(id=valid_response.data["submission_id"])
        self.assertEqual(submission.payload["answers"][0]["answer"], "7")
        mock_dispatch.assert_called_once()

    @patch("apps.domains.submissions.services.dispatcher.dispatch_submission")
    def test_parent_can_submit_same_exam_for_each_selected_child(self, mock_dispatch):
        lecture = Lecture.objects.create(
            tenant=self.tenant,
            title="Shared Lecture",
            name="Shared Lecture",
            subject="MATH",
        )
        session = Session.objects.create(lecture=lecture, order=1, title="1회")
        enrollment_a = Enrollment.objects.create(
            tenant=self.tenant,
            student=self.student_a,
            lecture=lecture,
            status="ACTIVE",
        )
        enrollment_b = Enrollment.objects.create(
            tenant=self.tenant,
            student=self.student_b,
            lecture=lecture,
            status="ACTIVE",
        )
        exam = Exam.objects.create(
            tenant=self.tenant,
            title="Shared Exam",
            exam_type=Exam.ExamType.REGULAR,
            is_active=True,
            pass_score=0,
            max_score=10,
        )
        exam.sessions.add(session)
        ExamEnrollment.objects.create(exam=exam, enrollment=enrollment_a)
        ExamEnrollment.objects.create(exam=exam, enrollment=enrollment_b)
        sheet = Sheet.objects.create(exam=exam, name="MAIN", total_questions=1)
        question = ExamQuestion.objects.create(sheet=sheet, number=1, score=10)
        view = StudentExamSubmitView.as_view()

        response_a = view(
            self._post_request(
                f"/student/exams/{exam.id}/submit/",
                student=self.student_a,
                data={"answers": [{"exam_question_id": question.id, "answer": "1"}]},
            ),
            pk=exam.id,
        )
        response_b = view(
            self._post_request(
                f"/student/exams/{exam.id}/submit/",
                student=self.student_b,
                data={"answers": [{"exam_question_id": question.id, "answer": "2"}]},
            ),
            pk=exam.id,
        )

        self.assertEqual(response_a.status_code, 201, response_a.data)
        self.assertEqual(response_b.status_code, 201, response_b.data)
        submissions = list(
            Submission.objects.filter(target_type=Submission.TargetType.EXAM, target_id=exam.id)
            .order_by("enrollment_id")
        )
        self.assertEqual(len(submissions), 2)
        self.assertEqual(submissions[0].user_id, self.student_a.user_id)
        self.assertEqual(submissions[1].user_id, self.student_b.user_id)
        self.assertEqual(submissions[0].meta["submitted_by_user_id"], self.parent_user.id)
        self.assertEqual(submissions[1].meta["submitted_by_user_id"], self.parent_user.id)
        self.assertEqual(mock_dispatch.call_count, 2)
