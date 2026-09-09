import datetime

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import resolve
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.core.models import Tenant, TenantMembership
from apps.domains.attendance.models import Attendance
from apps.domains.enrollment.models import Enrollment, SessionEnrollment
from apps.domains.exams.models import AnswerKey, ExamQuestion, Sheet
from apps.domains.exams.models import Exam, ExamEnrollment, ExamLecturePolicy
from apps.domains.homework.models import HomeworkAssignment
from apps.domains.homework_results.models import Homework, HomeworkScore
from apps.domains.lectures.models import Lecture, Session
from apps.domains.progress.models import AssessmentCorrection, ClinicLink, SessionProgress
from apps.domains.progress.services.clinic_remediation_service import ClinicRemediationService
from apps.domains.results.services.clinic_target_service import ClinicTargetService
from apps.domains.results.services.session_score_summary_service import (
    SessionScoreSummaryService,
)
from apps.domains.results.guards.exam_enrollment_guard import (
    validate_exam_enrollment_assigned,
)
from apps.domains.results.utils.clinic_highlight import compute_clinic_highlight_map
from apps.domains.results.models import ExamAttempt, Result, ResultItem
from apps.domains.results.services.wrong_note_service import WrongNoteQuery, list_wrong_notes_for_enrollment
from apps.domains.results.views.session_scores_view import (
    SessionScoreCorrectionView,
    SessionScoresView,
)
from apps.domains.results.views.admin_session_exams_summary_view import AdminSessionExamsSummaryView
from apps.domains.students.models import Student
from apps.domains.submissions.models import OMRStudentMatch, Submission
from apps.domains.submissions.views.submission_view import SubmissionViewSet
from apps.support.submissions.dependencies import validate_exam_enrollment_candidate


User = get_user_model()


class SessionScoresRosterScopeTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.tenant = Tenant.objects.create(name="Tenant", code="scorecope", is_active=True)
        self.admin = User.objects.create_user(
            username="score_scope_admin",
            password="test1234",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=self.admin, role="admin")

        self.lecture = Lecture.objects.create(
            tenant=self.tenant,
            title="Lecture",
            name="Lecture",
            subject="SCIENCE",
        )
        self.session = Session.objects.create(lecture=self.lecture, order=1, title="1주차")
        self.exam = Exam.objects.create(
            tenant=self.tenant,
            title="주간 테스트",
            pass_score=60,
            max_score=100,
        )
        self.exam.sessions.add(self.session)
        self.homework = Homework.objects.create(
            tenant=self.tenant,
            session=self.session,
            title="주간 과제",
        )

        self.active_enrollment = self._create_enrollment("ACTIVE001", "현재 학생")
        self.stale_enrollment = self._create_enrollment("STALE001", "출결 제외 학생")

        SessionEnrollment.objects.create(
            tenant=self.tenant,
            session=self.session,
            enrollment=self.active_enrollment,
        )
        SessionEnrollment.objects.create(
            tenant=self.tenant,
            session=self.session,
            enrollment=self.stale_enrollment,
        )
        Attendance.objects.create(
            tenant=self.tenant,
            session=self.session,
            enrollment=self.active_enrollment,
            status="PRESENT",
        )

        for enrollment in (self.active_enrollment, self.stale_enrollment):
            ExamEnrollment.objects.create(exam=self.exam, enrollment=enrollment)
            HomeworkAssignment.objects.create(
                tenant=self.tenant,
                homework=self.homework,
                session=self.session,
                enrollment=enrollment,
            )

    def _create_enrollment(self, ps_number: str, name: str) -> Enrollment:
        user = User.objects.create_user(
            username=f"score_scope_{ps_number}",
            password="test1234",
            tenant=self.tenant,
        )
        student = Student.objects.create(
            tenant=self.tenant,
            user=user,
            ps_number=ps_number,
            omr_code=ps_number[-8:],
            name=name,
            parent_phone="01000000000",
        )
        return Enrollment.objects.create(
            tenant=self.tenant,
            student=student,
            lecture=self.lecture,
            status="ACTIVE",
        )

    def test_session_scores_excludes_assignment_without_attendance_row(self):
        request = self.factory.get(f"/api/v1/results/admin/sessions/{self.session.id}/scores/")
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)

        response = SessionScoresView.as_view()(request, session_id=self.session.id)

        self.assertEqual(response.status_code, 200, response.data)
        rows = response.data["rows"]
        self.assertEqual(response.data["meta"]["exams"][0]["exam_id"], self.exam.id)
        self.assertEqual(response.data["meta"]["exams"][0]["grading_mode"], "choice")
        self.assertEqual(
            response.data["meta"]["exams"][0]["manual_grading_method"],
            "score",
        )
        self.assertEqual(response.data["meta"]["homeworks"][0]["homework_id"], self.homework.id)
        self.assertEqual(response.data["meta"]["homeworks"][0]["grading_mode"], "SCORE")
        self.assertEqual([row["enrollment_id"] for row in rows], [self.active_enrollment.id])
        self.assertEqual(rows[0]["student_name"], "현재 학생")
        self.assertEqual(len(rows[0]["exams"]), 1)
        self.assertEqual(len(rows[0]["homeworks"]), 1)

    def test_session_scores_meta_and_row_use_same_lecture_pass_score(self):
        ExamLecturePolicy.objects.create(
            exam=self.exam,
            lecture=self.lecture,
            pass_score=70,
        )
        Result.objects.create(
            target_type="exam",
            target_id=self.exam.id,
            enrollment=self.active_enrollment,
            total_score=65,
            max_score=100,
            objective_score=65,
        )
        request = self.factory.get(f"/api/v1/results/admin/sessions/{self.session.id}/scores/")
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)

        response = SessionScoresView.as_view()(request, session_id=self.session.id)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["meta"]["exams"][0]["pass_score"], 70.0)
        exam_row = response.data["rows"][0]["exams"][0]
        self.assertEqual(exam_row["pass_score"], 70.0)
        self.assertFalse(exam_row["block"]["passed"])
        summary_request = self.factory.get(
            f"/api/v1/results/admin/sessions/{self.session.id}/exams/summary/"
        )
        summary_request.tenant = self.tenant
        force_authenticate(summary_request, user=self.admin)
        summary = AdminSessionExamsSummaryView.as_view()(
            summary_request,
            session_id=self.session.id,
        )
        self.assertEqual(summary.data["exams"][0]["pass_score"], 70.0)
        self.assertEqual(summary.data["exams"][0]["fail_count"], 1)

    def test_session_exam_summary_excludes_absent_from_pass_rate_denominator(self):
        Attendance.objects.create(
            tenant=self.tenant,
            session=self.session,
            enrollment=self.stale_enrollment,
            status="PRESENT",
        )
        Result.objects.create(
            target_type="exam",
            target_id=self.exam.id,
            enrollment=self.active_enrollment,
            total_score=80,
            max_score=100,
            objective_score=80,
        )
        absent_attempt = ExamAttempt.objects.create(
            exam=self.exam,
            enrollment=self.stale_enrollment,
            attempt_index=1,
            status="done",
            meta={"status": "NOT_SUBMITTED"},
        )
        Result.objects.create(
            target_type="exam",
            target_id=self.exam.id,
            enrollment=self.stale_enrollment,
            attempt=absent_attempt,
            total_score=0,
            max_score=100,
            objective_score=0,
        )
        request = self.factory.get(
            f"/api/v1/results/admin/sessions/{self.session.id}/exams/summary/"
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)

        response = AdminSessionExamsSummaryView.as_view()(request, session_id=self.session.id)

        self.assertEqual(response.status_code, 200, response.data)
        exam_row = response.data["exams"][0]
        self.assertEqual(exam_row["participant_count"], 1)
        self.assertEqual(exam_row["pass_rate"], 1.0)

    def test_roster_replacement_hides_removed_target_clinic_link(self):
        Attendance.objects.create(
            tenant=self.tenant,
            session=self.session,
            enrollment=self.stale_enrollment,
            status="PRESENT",
        )
        ClinicLink.objects.create(
            tenant=self.tenant,
            enrollment=self.stale_enrollment,
            session=self.session,
            reason=ClinicLink.Reason.AUTO_FAILED,
            is_auto=True,
            source_type="exam",
            source_id=self.exam.id,
        )
        sheet = Sheet.objects.create(exam=self.exam, name="TARGET", total_questions=1)
        question = ExamQuestion.objects.create(sheet=sheet, number=1, score=10)
        stale_result = Result.objects.create(
            target_type="exam",
            target_id=self.exam.id,
            enrollment=self.stale_enrollment,
            total_score=0,
            max_score=10,
            objective_score=0,
        )
        ResultItem.objects.create(
            result=stale_result,
            question=question,
            answer="2",
            is_correct=False,
            score=0,
            max_score=10,
            source="manual",
        )
        request = self.factory.put(
            f"/api/v1/exams/{self.exam.id}/enrollments/?session_id={self.session.id}",
            {"enrollment_ids": [self.active_enrollment.id]},
            format="json",
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)

        response = resolve(request.path_info).func(request, exam_id=self.exam.id)

        self.assertEqual(response.status_code, 200, response.data)
        target_ids = {
            row["enrollment_id"]
            for row in ClinicTargetService.list_admin_targets(tenant=self.tenant)
        }
        self.assertNotIn(self.stale_enrollment.id, target_ids)
        wrong_count, _ = list_wrong_notes_for_enrollment(
            enrollment_id=self.stale_enrollment.id,
            q=WrongNoteQuery(exam_id=self.exam.id),
        )
        self.assertEqual(wrong_count, 0)
        summary_request = self.factory.get(
            f"/api/v1/results/admin/sessions/{self.session.id}/exams/summary/"
        )
        summary_request.tenant = self.tenant
        force_authenticate(summary_request, user=self.admin)
        summary = AdminSessionExamsSummaryView.as_view()(
            summary_request,
            session_id=self.session.id,
        )
        self.assertEqual(summary.data["exams"][0]["participant_count"], 0)

    def test_session_scores_rows_use_stable_student_name_order(self):
        self.active_enrollment.student.name = "Zulu student"
        self.active_enrollment.student.save(update_fields=["name", "updated_at"])
        self.stale_enrollment.student.name = "Alpha student"
        self.stale_enrollment.student.save(update_fields=["name", "updated_at"])
        Attendance.objects.create(
            tenant=self.tenant,
            session=self.session,
            enrollment=self.stale_enrollment,
            status="PRESENT",
        )
        request = self.factory.get(
            f"/api/v1/results/admin/sessions/{self.session.id}/scores/"
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)

        response = SessionScoresView.as_view()(request, session_id=self.session.id)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            [row["student_name"] for row in response.data["rows"]],
            ["Alpha student", "Zulu student"],
        )

    def test_session_scores_uses_configured_homework_max_score(self):
        self.homework.meta = {"default_max_score": 43}
        self.homework.save(update_fields=["meta", "updated_at"])
        HomeworkScore.objects.create(
            enrollment=self.active_enrollment,
            session=self.session,
            homework=self.homework,
            score=41,
            max_score=100,
            passed=False,
        )

        request = self.factory.get(f"/api/v1/results/admin/sessions/{self.session.id}/scores/")
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)

        response = SessionScoresView.as_view()(request, session_id=self.session.id)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["meta"]["homeworks"][0]["max_score"], 43.0)
        self.assertEqual(
            response.data["rows"][0]["homeworks"][0]["block"]["max_score"],
            43.0,
        )

    def test_session_scores_uses_current_exam_max_and_preserves_attempt_history(self):
        attempt = ExamAttempt.objects.create(
            exam=self.exam,
            enrollment=self.active_enrollment,
            attempt_index=1,
            is_retake=False,
            is_representative=True,
            status="done",
            meta={
                "initial_snapshot": {
                    "total_score": 97.0,
                    "max_score": 97.0,
                    "source": "admin_manual_total",
                },
                "total_score": 97.0,
                "max_score": 97.0,
            },
        )
        result = Result.objects.create(
            target_type="exam",
            target_id=self.exam.id,
            enrollment=self.active_enrollment,
            attempt=attempt,
            total_score=97,
            max_score=97,
            objective_score=97,
        )
        self.exam.max_score = 105
        self.exam.save(update_fields=["max_score", "updated_at"])

        request = self.factory.get(f"/api/v1/results/admin/sessions/{self.session.id}/scores/")
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)
        response = SessionScoresView.as_view()(request, session_id=self.session.id)

        self.assertEqual(response.status_code, 200, response.data)
        exam_meta = response.data["meta"]["exams"][0]
        exam_row = response.data["rows"][0]["exams"][0]
        self.assertEqual(exam_meta["max_score"], 105.0)
        self.assertEqual(exam_row["block"]["max_score"], 105.0)
        self.assertEqual(exam_row["attempts"][0]["max_score"], 97.0)
        self.assertEqual(exam_row["block"]["correction_status"], "PENDING")

        correction_request = self.factory.patch(
            f"/api/v1/results/admin/sessions/{self.session.id}/score-correction/",
            {
                "enrollment_id": self.active_enrollment.id,
                "source_type": "exam",
                "source_id": self.exam.id,
                "completed": True,
                "note": "현재 만점 기준으로 오답 확인 완료",
            },
            format="json",
        )
        correction_request.tenant = self.tenant
        force_authenticate(correction_request, user=self.admin)
        completion = SessionScoreCorrectionView.as_view()(
            correction_request,
            session_id=self.session.id,
        )

        self.assertEqual(completion.status_code, 200, completion.data)
        self.assertEqual(completion.data["correction_status"], "COMPLETED")

        reload_request = self.factory.get(
            f"/api/v1/results/admin/sessions/{self.session.id}/scores/"
        )
        reload_request.tenant = self.tenant
        force_authenticate(reload_request, user=self.admin)
        reloaded = SessionScoresView.as_view()(
            reload_request,
            session_id=self.session.id,
        )
        reloaded_block = reloaded.data["rows"][0]["exams"][0]["block"]
        self.assertEqual(reloaded_block["max_score"], 105.0)
        self.assertEqual(reloaded_block["correction_status"], "COMPLETED")
        result.refresh_from_db()
        self.assertEqual(result.max_score, 97.0)
        attempt.refresh_from_db()
        self.assertEqual(attempt.meta["initial_snapshot"]["max_score"], 97.0)

    def test_session_scores_exposes_homework_cell_version(self):
        score = HomeworkScore.objects.create(
            enrollment=self.active_enrollment,
            session=self.session,
            homework=self.homework,
            score=41,
            max_score=100,
            passed=False,
        )
        request = self.factory.get(f"/api/v1/results/admin/sessions/{self.session.id}/scores/")
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)

        response = SessionScoresView.as_view()(request, session_id=self.session.id)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            response.data["rows"][0]["homeworks"][0]["block"]["updated_at"],
            timezone.localtime(score.updated_at).isoformat(),
        )

    def test_session_scores_treats_session_student_as_omr_exam_target(self):
        ExamEnrollment.objects.filter(exam=self.exam).delete()

        request = self.factory.get(f"/api/v1/results/admin/sessions/{self.session.id}/scores/")
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)

        response = SessionScoresView.as_view()(request, session_id=self.session.id)

        self.assertEqual(response.status_code, 200, response.data)
        rows = response.data["rows"]
        self.assertEqual([row["enrollment_id"] for row in rows], [self.active_enrollment.id])
        self.assertEqual(len(rows[0]["exams"]), 1)
        self.assertEqual(rows[0]["exams"][0]["exam_id"], self.exam.id)
        self.assertIsNone(rows[0]["exams"][0]["block"]["score"])

    def test_explicit_exam_targets_exclude_non_target_exam_from_scores_and_clinic(self):
        Attendance.objects.create(
            tenant=self.tenant,
            session=self.session,
            enrollment=self.stale_enrollment,
            status="PRESENT",
        )
        other_exam = Exam.objects.create(
            tenant=self.tenant,
            title="다른 학생만 보는 순환",
            pass_score=70,
            max_score=100,
        )
        other_exam.sessions.add(self.session)
        ExamEnrollment.objects.create(
            exam=other_exam,
            enrollment=self.stale_enrollment,
        )
        Result.objects.create(
            target_type="exam",
            target_id=self.exam.id,
            enrollment=self.active_enrollment,
            total_score=85,
            max_score=100,
            objective_score=85,
        )
        Result.objects.create(
            target_type="exam",
            target_id=other_exam.id,
            enrollment=self.active_enrollment,
            total_score=0,
            max_score=100,
            objective_score=0,
        )
        SessionProgress.objects.create(
            enrollment=self.active_enrollment,
            session=self.session,
            exam_passed=True,
            homework_passed=True,
            video_completed=True,
            completed=True,
        )

        request = self.factory.get(
            f"/api/v1/results/admin/sessions/{self.session.id}/scores/"
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)
        response = SessionScoresView.as_view()(request, session_id=self.session.id)

        self.assertEqual(response.status_code, 200, response.data)
        active_row = next(
            row
            for row in response.data["rows"]
            if row["enrollment_id"] == self.active_enrollment.id
        )
        self.assertEqual(
            [row["exam_id"] for row in active_row["exams"]],
            [self.exam.id],
        )

        self.assertFalse(
            ClinicLink.objects.filter(
                enrollment=self.active_enrollment,
                source_type="exam",
                source_id=other_exam.id,
            ).exists()
        )
        summary = SessionScoreSummaryService.build(session_id=self.session.id)
        self.assertEqual(summary["avg_score"], 85.0)
        with self.assertRaises(ValidationError):
            validate_exam_enrollment_assigned(other_exam, self.active_enrollment.id)
        candidate = validate_exam_enrollment_candidate(
            tenant=self.tenant,
            exam_id=other_exam.id,
            enrollment_id=self.active_enrollment.id,
        )
        self.assertFalse(candidate.ok)
        self.assertFalse(candidate.should_create)

    def test_session_scores_uses_one_latest_result_lookup_for_many_exams(self):
        for idx in range(3):
            exam = Exam.objects.create(
                tenant=self.tenant,
                title=f"추가 시험 {idx}",
                pass_score=60,
                max_score=100,
            )
            exam.sessions.add(self.session)

        request = self.factory.get(
            f"/api/v1/results/admin/sessions/{self.session.id}/scores/"
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)

        with CaptureQueriesContext(connection) as captured:
            response = SessionScoresView.as_view()(request, session_id=self.session.id)

        latest_result_queries = [
            query["sql"]
            for query in captured.captured_queries
            if 'FROM "results_result"' in query["sql"]
            and "MAX(" in query["sql"].upper()
        ]
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data["meta"]["exams"]), 4)
        self.assertEqual(len(latest_result_queries), 1, latest_result_queries)

    def test_session_scores_excludes_cross_tenant_exam_m2m_contamination(self):
        other_tenant = Tenant.objects.create(name="Other Tenant", code="scorecope-other", is_active=True)
        foreign_exam = Exam.objects.create(
            tenant=other_tenant,
            title="타 테넌트 시험",
            pass_score=40,
            max_score=100,
        )
        foreign_exam.sessions.add(self.session)

        request = self.factory.get(f"/api/v1/results/admin/sessions/{self.session.id}/scores/")
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)

        response = SessionScoresView.as_view()(request, session_id=self.session.id)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            [row["exam_id"] for row in response.data["meta"]["exams"]],
            [self.exam.id],
        )
        self.assertEqual(
            [row["exam_id"] for row in response.data["rows"][0]["exams"]],
            [self.exam.id],
        )

    def test_omr_incomplete_identifier_manual_match_after_exam_close_registers_target_and_score(self):
        ExamEnrollment.objects.filter(exam=self.exam).delete()
        self.exam.close_at = timezone.now() - datetime.timedelta(minutes=1)
        self.exam.save(update_fields=["close_at", "updated_at"])
        sheet = Sheet.objects.create(exam=self.exam, name="MAIN", total_questions=2)
        q1 = ExamQuestion.objects.create(sheet=sheet, number=1, score=5)
        q2 = ExamQuestion.objects.create(sheet=sheet, number=2, score=5)
        AnswerKey.objects.create(exam=self.exam, answers={str(q1.id): "2", str(q2.id): "4"})

        submission = Submission.objects.create(
            tenant=self.tenant,
            user=self.admin,
            enrollment_id=None,
            target_type=Submission.TargetType.EXAM,
            target_id=self.exam.id,
            source=Submission.Source.OMR_SCAN,
            status=Submission.Status.NEEDS_IDENTIFICATION,
            meta={
                "identifier_status": "incomplete",
                "manual_review": {
                    "required": True,
                    "reasons": ["IDENTIFIER_INCOMPLETE"],
                },
                "omr": {"identifier": "01012??5678"},
            },
        )

        request = self.factory.post(
            f"/api/v1/submissions/submissions/{submission.id}/manual-edit/",
            {
                "identifier": {"enrollment_id": self.active_enrollment.id},
                "answers": [
                    {"exam_question_id": q1.id, "answer": "2"},
                    {"exam_question_id": q2.id, "answer": "4"},
                ],
                "note": "test_manual_match",
            },
            format="json",
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)

        response = SubmissionViewSet.as_view({"post": "manual_edit"})(
            request,
            pk=submission.id,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["graded"], response.data)
        self.assertEqual(response.data["enrollment_id"], self.active_enrollment.id)
        self.assertTrue(
            ExamEnrollment.objects.filter(
                exam=self.exam,
                enrollment=self.active_enrollment,
            ).exists()
        )
        result = Result.objects.get(
            target_type="exam",
            target_id=self.exam.id,
            enrollment=self.active_enrollment,
        )
        self.assertEqual(float(result.total_score), 10.0)

        submission.refresh_from_db()
        self.assertEqual(submission.status, Submission.Status.DONE)
        self.assertEqual(submission.enrollment_id, self.active_enrollment.id)
        self.assertFalse(submission.meta["manual_review"]["required"])
        self.assertEqual(submission.meta["identifier_status"], "matched")
        current_match = OMRStudentMatch.objects.get(
            submission=submission,
            is_current=True,
        )
        self.assertEqual(current_match.enrollment_id, self.active_enrollment.id)
        self.assertEqual(current_match.status, OMRStudentMatch.Status.CONFIRMED)
        self.assertEqual(current_match.method, OMRStudentMatch.Method.MANUAL)

        score_request = self.factory.get(f"/api/v1/results/admin/sessions/{self.session.id}/scores/")
        score_request.tenant = self.tenant
        force_authenticate(score_request, user=self.admin)
        score_response = SessionScoresView.as_view()(score_request, session_id=self.session.id)

        self.assertEqual(score_response.status_code, 200, score_response.data)
        rows = score_response.data["rows"]
        active_row = next(row for row in rows if row["enrollment_id"] == self.active_enrollment.id)
        self.assertEqual(len(active_row["exams"]), 1)
        self.assertEqual(active_row["exams"][0]["block"]["score"], 10.0)

        detail_request = self.factory.get(
            f"/api/v1/submissions/submissions/{submission.id}/manual-edit/"
        )
        detail_request.tenant = self.tenant
        force_authenticate(detail_request, user=self.admin)
        detail_response = SubmissionViewSet.as_view({"get": "manual_edit"})(
            detail_request,
            pk=submission.id,
        )
        self.assertEqual(detail_response.status_code, 200, detail_response.data)
        self.assertEqual(detail_response.data["submission_status"], Submission.Status.DONE)
        self.assertEqual(detail_response.data["enrollment_id"], self.active_enrollment.id)
        self.assertEqual(detail_response.data["meta"]["identifier_status"], "matched")

    def test_session_scores_marks_omr_review_required_without_fail_score(self):
        Submission.objects.create(
            tenant=self.tenant,
            user=self.admin,
            enrollment_id=self.active_enrollment.id,
            target_type=Submission.TargetType.EXAM,
            target_id=self.exam.id,
            source=Submission.Source.OMR_SCAN,
            status=Submission.Status.DONE,
            meta={
                "manual_review": {
                    "required": True,
                    "reasons": ["ANSWER_STATUS_NOT_OK"],
                }
            },
        )

        request = self.factory.get(f"/api/v1/results/admin/sessions/{self.session.id}/scores/")
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)

        response = SessionScoresView.as_view()(request, session_id=self.session.id)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["meta"]["exams"][0]["exam_id"], self.exam.id)
        row = next(row for row in response.data["rows"] if row["enrollment_id"] == self.active_enrollment.id)
        block = row["exams"][0]["block"]
        self.assertIsNone(block["score"])
        self.assertEqual(block["meta"]["status"], "OMR_REVIEW_REQUIRED")
        self.assertTrue(block["meta"]["manual_review_required"])
        self.assertEqual(block["meta"]["manual_review_reasons"], ["ANSWER_STATUS_NOT_OK"])

    def test_session_scores_ignores_superseded_omr_review_when_current_scan_is_clean(self):
        Submission.objects.create(
            tenant=self.tenant,
            user=self.admin,
            enrollment_id=self.active_enrollment.id,
            target_type=Submission.TargetType.EXAM,
            target_id=self.exam.id,
            source=Submission.Source.OMR_SCAN,
            status=Submission.Status.SUPERSEDED,
            meta={
                "manual_review": {
                    "required": True,
                    "reasons": ["ANSWER_STATUS_NOT_OK"],
                }
            },
        )
        Submission.objects.create(
            tenant=self.tenant,
            user=self.admin,
            enrollment_id=self.active_enrollment.id,
            target_type=Submission.TargetType.EXAM,
            target_id=self.exam.id,
            source=Submission.Source.OMR_SCAN,
            status=Submission.Status.DONE,
            meta={"manual_review": {"required": False}},
        )

        request = self.factory.get(f"/api/v1/results/admin/sessions/{self.session.id}/scores/")
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)

        response = SessionScoresView.as_view()(request, session_id=self.session.id)

        self.assertEqual(response.status_code, 200, response.data)
        row = next(
            item
            for item in response.data["rows"]
            if item["enrollment_id"] == self.active_enrollment.id
        )
        block = row["exams"][0]["block"]
        self.assertNotEqual((block.get("meta") or {}).get("status"), "OMR_REVIEW_REQUIRED")

    def test_exam_correction_completion_is_manual_persistent_and_score_versioned(self):
        result = Result.objects.create(
            target_type="exam",
            target_id=self.exam.id,
            enrollment=self.active_enrollment,
            total_score=50,
            max_score=100,
        )

        score_request = self.factory.get(
            f"/api/v1/results/admin/sessions/{self.session.id}/scores/"
        )
        score_request.tenant = self.tenant
        force_authenticate(score_request, user=self.admin)
        initial = SessionScoresView.as_view()(score_request, session_id=self.session.id)
        initial_row = initial.data["rows"][0]

        self.assertEqual(
            initial_row["exams"][0]["block"]["correction_status"],
            "PENDING",
        )
        self.assertEqual(initial_row["correction_pending_count"], 1)
        self.assertTrue(initial_row["name_highlight_followup_required"])

        correction_request = self.factory.patch(
            f"/api/v1/results/admin/sessions/{self.session.id}/score-correction/",
            {
                "enrollment_id": self.active_enrollment.id,
                "source_type": "exam",
                "source_id": self.exam.id,
                "completed": True,
                "note": "서술형 3번 풀이를 다시 확인함",
            },
            format="json",
        )
        correction_request.tenant = self.tenant
        force_authenticate(correction_request, user=self.admin)
        completion = SessionScoreCorrectionView.as_view()(
            correction_request,
            session_id=self.session.id,
        )

        self.assertEqual(completion.status_code, 200, completion.data)
        self.assertEqual(completion.data["correction_status"], "COMPLETED")
        self.assertTrue(completion.data["teacher_resolved"])
        self.assertEqual(
            completion.data["correction_note"],
            "서술형 3번 풀이를 다시 확인함",
        )

        refreshed_request = self.factory.get(
            f"/api/v1/results/admin/sessions/{self.session.id}/scores/"
        )
        refreshed_request.tenant = self.tenant
        force_authenticate(refreshed_request, user=self.admin)
        refreshed = SessionScoresView.as_view()(
            refreshed_request,
            session_id=self.session.id,
        )
        refreshed_row = refreshed.data["rows"][0]
        self.assertEqual(
            refreshed_row["exams"][0]["block"]["correction_status"],
            "COMPLETED",
        )
        self.assertEqual(
            refreshed_row["exams"][0]["block"]["correction_note"],
            "서술형 3번 풀이를 다시 확인함",
        )
        self.assertEqual(refreshed_row["correction_pending_count"], 0)
        self.assertFalse(refreshed_row["name_highlight_followup_required"])
        refreshed_block = refreshed_row["exams"][0]["block"]
        self.assertEqual(refreshed_block["score"], 50.0)
        self.assertFalse(refreshed_block["passed"])
        self.assertTrue(refreshed_block["final_pass"])
        self.assertEqual(refreshed_block["achievement"], "REMEDIATED")
        self.assertFalse(refreshed_row["clinic_required"])
        correction = AssessmentCorrection.objects.get(
            tenant=self.tenant,
            enrollment=self.active_enrollment,
            session=self.session,
            source_type=AssessmentCorrection.SourceType.EXAM,
            source_id=self.exam.id,
        )
        self.assertEqual(len(correction.source_fingerprint), 64)
        teacher_link = ClinicLink.objects.get(
            tenant=self.tenant,
            enrollment=self.active_enrollment,
            session=self.session,
            source_type="exam",
            source_id=self.exam.id,
        )
        self.assertIsNotNone(teacher_link.resolved_at)
        self.assertEqual(
            teacher_link.resolution_type,
            ClinicLink.ResolutionType.MANUAL_OVERRIDE,
        )
        self.assertEqual(
            teacher_link.resolution_evidence["assessment_correction_id"],
            correction.id,
        )

        result.save(update_fields=["updated_at"])
        timestamp_only_request = self.factory.get(
            f"/api/v1/results/admin/sessions/{self.session.id}/scores/"
        )
        timestamp_only_request.tenant = self.tenant
        force_authenticate(timestamp_only_request, user=self.admin)
        timestamp_only = SessionScoresView.as_view()(
            timestamp_only_request,
            session_id=self.session.id,
        )
        self.assertEqual(
            timestamp_only.data["rows"][0]["exams"][0]["block"]["correction_status"],
            "COMPLETED",
        )

        result.total_score = 55
        result.save(update_fields=["total_score", "updated_at"])
        stale_request = self.factory.get(
            f"/api/v1/results/admin/sessions/{self.session.id}/scores/"
        )
        stale_request.tenant = self.tenant
        force_authenticate(stale_request, user=self.admin)
        stale = SessionScoresView.as_view()(stale_request, session_id=self.session.id)

        self.assertEqual(
            stale.data["rows"][0]["exams"][0]["block"]["correction_status"],
            "PENDING",
        )
        self.assertEqual(
            stale.data["rows"][0]["exams"][0]["block"]["correction_note"],
            "서술형 3번 풀이를 다시 확인함",
        )
        self.assertTrue(
            stale.data["rows"][0]["name_highlight_followup_required"]
        )
        stale_block = stale.data["rows"][0]["exams"][0]["block"]
        self.assertFalse(stale_block["teacher_resolved"])
        self.assertFalse(stale_block["final_pass"])
        self.assertEqual(stale_block["achievement"], "FAIL")

    def test_legacy_exam_completion_without_fingerprint_remains_complete(self):
        result = Result.objects.create(
            target_type="exam",
            target_id=self.exam.id,
            enrollment=self.active_enrollment,
            total_score=50,
            max_score=100,
        )
        AssessmentCorrection.objects.create(
            tenant=self.tenant,
            enrollment=self.active_enrollment,
            session=self.session,
            source_type=AssessmentCorrection.SourceType.EXAM,
            source_id=self.exam.id,
            completed=True,
            source_updated_at_snapshot=result.updated_at,
        )
        result.save(update_fields=["updated_at"])

        request = self.factory.get(
            f"/api/v1/results/admin/sessions/{self.session.id}/scores/"
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)
        response = SessionScoresView.as_view()(request, session_id=self.session.id)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            response.data["rows"][0]["exams"][0]["block"]["correction_status"],
            "COMPLETED",
        )

    def test_exam_correction_locks_result_without_nullable_attempt_join(self):
        Result.objects.create(
            target_type="exam",
            target_id=self.exam.id,
            enrollment=self.active_enrollment,
            total_score=50,
            max_score=100,
        )
        correction_request = self.factory.patch(
            f"/api/v1/results/admin/sessions/{self.session.id}/score-correction/",
            {
                "enrollment_id": self.active_enrollment.id,
                "source_type": "exam",
                "source_id": self.exam.id,
                "completed": True,
                "note": "잠금 쿼리 확인",
            },
            format="json",
        )
        correction_request.tenant = self.tenant
        force_authenticate(correction_request, user=self.admin)

        with CaptureQueriesContext(connection) as captured:
            response = SessionScoreCorrectionView.as_view()(
                correction_request,
                session_id=self.session.id,
            )

        self.assertEqual(response.status_code, 200, response.data)
        result_queries = [
            query["sql"]
            for query in captured.captured_queries
            if "results_result" in query["sql"] and "LIMIT 1" in query["sql"]
        ]
        self.assertTrue(result_queries)
        self.assertFalse(
            any("LEFT OUTER JOIN" in query.upper() for query in result_queries),
            result_queries,
        )

    def test_homework_correction_completion_can_be_reopened(self):
        HomeworkScore.objects.create(
            enrollment=self.active_enrollment,
            session=self.session,
            homework=self.homework,
            score=70,
            max_score=100,
        )

        for completed, expected_status in (
            (True, "COMPLETED"),
            (False, "PENDING"),
        ):
            request = self.factory.patch(
                f"/api/v1/results/admin/sessions/{self.session.id}/score-correction/",
                {
                    "enrollment_id": self.active_enrollment.id,
                    "source_type": "homework",
                    "source_id": self.homework.id,
                    "completed": completed,
                    "note": "협의 후 완료" if completed else "추가 확인 필요",
                },
                format="json",
            )
            request.tenant = self.tenant
            force_authenticate(request, user=self.admin)
            response = SessionScoreCorrectionView.as_view()(
                request,
                session_id=self.session.id,
            )

            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(response.data["correction_status"], expected_status)

    def test_homework_inspection_without_score_persists_status_and_note(self):
        score_request = self.factory.get(
            f"/api/v1/results/admin/sessions/{self.session.id}/scores/"
        )
        score_request.tenant = self.tenant
        force_authenticate(score_request, user=self.admin)
        initial = SessionScoresView.as_view()(
            score_request,
            session_id=self.session.id,
        )
        initial_block = initial.data["rows"][0]["homeworks"][0]["block"]
        self.assertIsNone(initial_block["correction_status"])
        self.assertEqual(initial_block["correction_note"], "")

        incomplete_request = self.factory.patch(
            f"/api/v1/results/admin/sessions/{self.session.id}/score-correction/",
            {
                "enrollment_id": self.active_enrollment.id,
                "source_type": "homework",
                "source_id": self.homework.id,
                "completed": False,
                "note": "연습문제 12~15번 미완료",
            },
            format="json",
        )
        incomplete_request.tenant = self.tenant
        force_authenticate(incomplete_request, user=self.admin)
        incomplete = SessionScoreCorrectionView.as_view()(
            incomplete_request,
            session_id=self.session.id,
        )

        self.assertEqual(incomplete.status_code, 200, incomplete.data)
        self.assertEqual(incomplete.data["correction_status"], "PENDING")
        self.assertEqual(
            incomplete.data["correction_note"],
            "연습문제 12~15번 미완료",
        )
        correction = AssessmentCorrection.objects.get(
            tenant=self.tenant,
            enrollment=self.active_enrollment,
            session=self.session,
            source_type=AssessmentCorrection.SourceType.HOMEWORK,
            source_id=self.homework.id,
        )
        self.assertFalse(correction.completed)
        self.assertEqual(correction.updated_by, self.admin)
        self.assertEqual(correction.note, "연습문제 12~15번 미완료")
        self.assertFalse(
            HomeworkScore.objects.filter(
                enrollment=self.active_enrollment,
                session=self.session,
                homework=self.homework,
            ).exists()
        )

        refreshed_request = self.factory.get(
            f"/api/v1/results/admin/sessions/{self.session.id}/scores/"
        )
        refreshed_request.tenant = self.tenant
        force_authenticate(refreshed_request, user=self.admin)
        refreshed = SessionScoresView.as_view()(
            refreshed_request,
            session_id=self.session.id,
        )
        refreshed_row = refreshed.data["rows"][0]
        self.assertEqual(
            refreshed_row["homeworks"][0]["block"]["correction_status"],
            "PENDING",
        )
        self.assertEqual(
            refreshed_row["homeworks"][0]["block"]["correction_note"],
            "연습문제 12~15번 미완료",
        )
        self.assertEqual(refreshed_row["correction_pending_count"], 1)
        self.assertTrue(refreshed_row["name_highlight_followup_required"])

        complete_request = self.factory.patch(
            f"/api/v1/results/admin/sessions/{self.session.id}/score-correction/",
            {
                "enrollment_id": self.active_enrollment.id,
                "source_type": "homework",
                "source_id": self.homework.id,
                "completed": True,
            },
            format="json",
        )
        complete_request.tenant = self.tenant
        force_authenticate(complete_request, user=self.admin)
        complete = SessionScoreCorrectionView.as_view()(
            complete_request,
            session_id=self.session.id,
        )
        self.assertEqual(complete.status_code, 200, complete.data)
        self.assertEqual(complete.data["correction_status"], "COMPLETED")
        self.assertTrue(complete.data["teacher_resolved"])
        self.assertEqual(
            complete.data["correction_note"],
            "연습문제 12~15번 미완료",
        )
        self.assertFalse(
            HomeworkScore.objects.filter(
                enrollment=self.active_enrollment,
                session=self.session,
                homework=self.homework,
            ).exists()
        )
        teacher_link = ClinicLink.objects.get(
            tenant=self.tenant,
            enrollment=self.active_enrollment,
            session=self.session,
            source_type="homework",
            source_id=self.homework.id,
        )
        self.assertIsNotNone(teacher_link.resolved_at)
        self.assertEqual(
            teacher_link.resolution_type,
            ClinicLink.ResolutionType.MANUAL_OVERRIDE,
        )

    def test_zero_score_teacher_pass_preserves_raw_score_and_unset_reopens_clinic(self):
        result = Result.objects.create(
            target_type="exam",
            target_id=self.exam.id,
            enrollment=self.active_enrollment,
            total_score=0,
            max_score=100,
        )
        complete_request = self.factory.patch(
            f"/api/v1/results/admin/sessions/{self.session.id}/score-correction/",
            {
                "enrollment_id": self.active_enrollment.id,
                "source_type": "exam",
                "source_id": self.exam.id,
                "completed": True,
                "note": "현장 풀이 재확인 완료",
                "expected_updated_at": None,
            },
            format="json",
        )
        complete_request.tenant = self.tenant
        force_authenticate(complete_request, user=self.admin)
        complete = SessionScoreCorrectionView.as_view()(
            complete_request,
            session_id=self.session.id,
        )
        self.assertEqual(complete.status_code, 200, complete.data)

        correction = AssessmentCorrection.objects.get(
            tenant=self.tenant,
            enrollment=self.active_enrollment,
            session=self.session,
            source_type=AssessmentCorrection.SourceType.EXAM,
            source_id=self.exam.id,
        )
        conflict_request = self.factory.patch(
            f"/api/v1/results/admin/sessions/{self.session.id}/score-correction/",
            {
                "enrollment_id": self.active_enrollment.id,
                "source_type": "exam",
                "source_id": self.exam.id,
                "completed": False,
                "expected_updated_at": "2000-01-01T00:00:00Z",
            },
            format="json",
        )
        conflict_request.tenant = self.tenant
        force_authenticate(conflict_request, user=self.admin)
        conflict = SessionScoreCorrectionView.as_view()(
            conflict_request,
            session_id=self.session.id,
        )
        self.assertEqual(conflict.status_code, 409, conflict.data)

        reopen_request = self.factory.patch(
            f"/api/v1/results/admin/sessions/{self.session.id}/score-correction/",
            {
                "enrollment_id": self.active_enrollment.id,
                "source_type": "exam",
                "source_id": self.exam.id,
                "completed": False,
                "note": "추가 보완 필요",
                "expected_updated_at": correction.updated_at.isoformat(),
            },
            format="json",
        )
        reopen_request.tenant = self.tenant
        force_authenticate(reopen_request, user=self.admin)
        reopened = SessionScoreCorrectionView.as_view()(
            reopen_request,
            session_id=self.session.id,
        )
        self.assertEqual(reopened.status_code, 200, reopened.data)
        self.assertFalse(reopened.data["teacher_resolved"])

        result.refresh_from_db()
        self.assertEqual(result.total_score, 0)
        link = ClinicLink.objects.get(
            tenant=self.tenant,
            enrollment=self.active_enrollment,
            session=self.session,
            source_type="exam",
            source_id=self.exam.id,
        )
        self.assertIsNone(link.resolved_at)
        self.assertIsNone(link.resolution_type)
        self.assertEqual(
            link.resolution_history[-1]["action"],
            "unresolve_teacher_assessment",
        )

        scores_request = self.factory.get(
            f"/api/v1/results/admin/sessions/{self.session.id}/scores/"
        )
        scores_request.tenant = self.tenant
        force_authenticate(scores_request, user=self.admin)
        scores = SessionScoresView.as_view()(
            scores_request,
            session_id=self.session.id,
        )
        block = scores.data["rows"][0]["exams"][0]["block"]
        self.assertEqual(block["score"], 0.0)
        self.assertFalse(block["final_pass"])
        self.assertEqual(block["achievement"], "FAIL")
        self.assertTrue(block["clinic_required"])

    def test_teacher_toggle_does_not_replace_or_reopen_existing_waiver(self):
        result = Result.objects.create(
            target_type="exam",
            target_id=self.exam.id,
            enrollment=self.active_enrollment,
            total_score=25,
            max_score=100,
        )
        waived = ClinicLink.objects.create(
            tenant=self.tenant,
            enrollment=self.active_enrollment,
            session=self.session,
            source_type="exam",
            source_id=self.exam.id,
            reason=ClinicLink.Reason.AUTO_FAILED,
            is_auto=True,
            approved=True,
            cycle_no=1,
            resolved_at=timezone.now(),
            resolution_type=ClinicLink.ResolutionType.WAIVED,
            resolution_evidence={"memo": "결석 사유 확인"},
            memo="결석 사유 확인",
        )

        complete_request = self.factory.patch(
            f"/api/v1/results/admin/sessions/{self.session.id}/score-correction/",
            {
                "enrollment_id": self.active_enrollment.id,
                "source_type": "exam",
                "source_id": self.exam.id,
                "completed": True,
                "note": "현장 풀이 확인",
                "expected_updated_at": None,
            },
            format="json",
        )
        complete_request.tenant = self.tenant
        force_authenticate(complete_request, user=self.admin)
        complete = SessionScoreCorrectionView.as_view()(
            complete_request,
            session_id=self.session.id,
        )
        self.assertEqual(complete.status_code, 200, complete.data)
        self.assertTrue(complete.data["teacher_resolved"])

        correction = AssessmentCorrection.objects.get(
            tenant=self.tenant,
            enrollment=self.active_enrollment,
            session=self.session,
            source_type=AssessmentCorrection.SourceType.EXAM,
            source_id=self.exam.id,
        )
        reopen_request = self.factory.patch(
            f"/api/v1/results/admin/sessions/{self.session.id}/score-correction/",
            {
                "enrollment_id": self.active_enrollment.id,
                "source_type": "exam",
                "source_id": self.exam.id,
                "completed": False,
                "note": "추가 보완 필요",
                "expected_updated_at": correction.updated_at.isoformat(),
            },
            format="json",
        )
        reopen_request.tenant = self.tenant
        force_authenticate(reopen_request, user=self.admin)
        reopened = SessionScoreCorrectionView.as_view()(
            reopen_request,
            session_id=self.session.id,
        )

        self.assertEqual(reopened.status_code, 200, reopened.data)
        waived.refresh_from_db()
        result.refresh_from_db()
        self.assertEqual(result.total_score, 25)
        self.assertIsNotNone(waived.resolved_at)
        self.assertEqual(waived.resolution_type, ClinicLink.ResolutionType.WAIVED)
        self.assertEqual(
            ClinicLink.objects.filter(
                tenant=self.tenant,
                enrollment=self.active_enrollment,
                session=self.session,
                source_type="exam",
                source_id=self.exam.id,
            ).count(),
            1,
        )

    def test_student_cannot_set_teacher_assessment_resolution(self):
        Result.objects.create(
            target_type="exam",
            target_id=self.exam.id,
            enrollment=self.active_enrollment,
            total_score=25,
            max_score=100,
        )
        request = self.factory.patch(
            f"/api/v1/results/admin/sessions/{self.session.id}/score-correction/",
            {
                "enrollment_id": self.active_enrollment.id,
                "source_type": "exam",
                "source_id": self.exam.id,
                "completed": True,
                "note": "권한 없는 변경",
            },
            format="json",
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.active_enrollment.student.user)
        response = SessionScoreCorrectionView.as_view()(
            request,
            session_id=self.session.id,
        )
        self.assertEqual(response.status_code, 403, response.data)
        self.assertFalse(AssessmentCorrection.objects.exists())

    def test_homework_manual_completion_is_independent_from_later_score_entry(self):
        request = self.factory.patch(
            f"/api/v1/results/admin/sessions/{self.session.id}/score-correction/",
            {
                "enrollment_id": self.active_enrollment.id,
                "source_type": "homework",
                "source_id": self.homework.id,
                "completed": True,
                "note": "종이 과제 검사 완료",
            },
            format="json",
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)
        response = SessionScoreCorrectionView.as_view()(
            request,
            session_id=self.session.id,
        )
        self.assertEqual(response.status_code, 200, response.data)

        HomeworkScore.objects.create(
            enrollment=self.active_enrollment,
            session=self.session,
            homework=self.homework,
            score=40,
            max_score=100,
        )
        score_request = self.factory.get(
            f"/api/v1/results/admin/sessions/{self.session.id}/scores/"
        )
        score_request.tenant = self.tenant
        force_authenticate(score_request, user=self.admin)
        refreshed = SessionScoresView.as_view()(
            score_request,
            session_id=self.session.id,
        )
        block = refreshed.data["rows"][0]["homeworks"][0]["block"]
        self.assertEqual(block["score"], 40.0)
        self.assertEqual(block["correction_status"], "COMPLETED")
        self.assertEqual(block["correction_note"], "종이 과제 검사 완료")

    def test_correction_note_is_limited_to_500_characters(self):
        request = self.factory.patch(
            f"/api/v1/results/admin/sessions/{self.session.id}/score-correction/",
            {
                "enrollment_id": self.active_enrollment.id,
                "source_type": "homework",
                "source_id": self.homework.id,
                "completed": False,
                "note": "가" * 501,
            },
            format="json",
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)

        response = SessionScoreCorrectionView.as_view()(
            request,
            session_id=self.session.id,
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("note", response.data)
        self.assertFalse(AssessmentCorrection.objects.exists())

    def test_correction_rejects_student_outside_attendance_roster(self):
        Result.objects.create(
            target_type="exam",
            target_id=self.exam.id,
            enrollment=self.stale_enrollment,
            total_score=50,
            max_score=100,
        )
        request = self.factory.patch(
            f"/api/v1/results/admin/sessions/{self.session.id}/score-correction/",
            {
                "enrollment_id": self.stale_enrollment.id,
                "source_type": "exam",
                "source_id": self.exam.id,
                "completed": True,
            },
            format="json",
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)

        response = SessionScoreCorrectionView.as_view()(
            request,
            session_id=self.session.id,
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("enrollment_id", response.data)

    def test_perfect_score_is_automatically_not_required(self):
        Result.objects.create(
            target_type="exam",
            target_id=self.exam.id,
            enrollment=self.active_enrollment,
            total_score=100,
            max_score=100,
        )
        score_request = self.factory.get(
            f"/api/v1/results/admin/sessions/{self.session.id}/scores/"
        )
        score_request.tenant = self.tenant
        force_authenticate(score_request, user=self.admin)
        score_response = SessionScoresView.as_view()(
            score_request,
            session_id=self.session.id,
        )
        self.assertEqual(
            score_response.data["rows"][0]["exams"][0]["block"]["correction_status"],
            "NOT_REQUIRED",
        )
        self.assertFalse(
            score_response.data["rows"][0]["name_highlight_followup_required"]
        )

        correction_request = self.factory.patch(
            f"/api/v1/results/admin/sessions/{self.session.id}/score-correction/",
            {
                "enrollment_id": self.active_enrollment.id,
                "source_type": "exam",
                "source_id": self.exam.id,
                "completed": False,
            },
            format="json",
        )
        correction_request.tenant = self.tenant
        force_authenticate(correction_request, user=self.admin)
        correction_response = SessionScoreCorrectionView.as_view()(
            correction_request,
            session_id=self.session.id,
        )
        self.assertEqual(correction_response.status_code, 400)

    def test_completed_progress_overrides_unresolved_clinic_link(self):
        link = ClinicLink.objects.create(
            tenant=self.tenant,
            enrollment=self.active_enrollment,
            session=self.session,
            reason=ClinicLink.Reason.AUTO_FAILED,
            is_auto=True,
            approved=True,
            source_type="exam",
            source_id=self.exam.id,
        )
        SessionProgress.objects.create(
            enrollment=self.active_enrollment,
            session=self.session,
            exam_passed=True,
            homework_passed=True,
            video_completed=True,
            completed=True,
        )

        request = self.factory.get(f"/api/v1/results/admin/sessions/{self.session.id}/scores/")
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)

        response = SessionScoresView.as_view()(request, session_id=self.session.id)

        self.assertEqual(response.status_code, 200, response.data)
        row = next(row for row in response.data["rows"] if row["enrollment_id"] == self.active_enrollment.id)
        self.assertTrue(row["progress_completed"])
        self.assertEqual(row["progress_status"], "completed")
        self.assertFalse(row["clinic_required"])
        self.assertFalse(row["exams"][0]["block"]["clinic_required"])
        self.assertIsNone(row["exams"][0]["clinic_link_id"])

        targets = ClinicTargetService.list_admin_targets(tenant=self.tenant)
        self.assertFalse(
            any(target.get("clinic_link_id") == link.id for target in targets),
            "완료 상태 학생은 현재 클리닉 대상자 API에서 제외되어야 한다.",
        )
        highlights = compute_clinic_highlight_map(
            tenant=self.tenant,
            enrollment_ids={self.active_enrollment.id},
            session=self.session,
        )
        self.assertFalse(highlights[self.active_enrollment.id])

    def test_session_scores_highlight_matches_passcard_booking_state(self):
        clinic_session_model = django_apps.get_model("clinic", "Session")
        session_participant_model = django_apps.get_model("clinic", "SessionParticipant")
        link = ClinicLink.objects.create(
            tenant=self.tenant,
            enrollment=self.active_enrollment,
            session=self.session,
            reason=ClinicLink.Reason.AUTO_FAILED,
            is_auto=True,
            approved=True,
            source_type="exam",
            source_id=self.exam.id,
        )

        def read_row():
            request = self.factory.get(
                f"/api/v1/results/admin/sessions/{self.session.id}/scores/"
            )
            request.tenant = self.tenant
            force_authenticate(request, user=self.admin)
            response = SessionScoresView.as_view()(
                request,
                session_id=self.session.id,
            )
            self.assertEqual(response.status_code, 200, response.data)
            return next(
                row
                for row in response.data["rows"]
                if row["enrollment_id"] == self.active_enrollment.id
            )

        target_row = read_row()
        self.assertTrue(target_row["clinic_required"])
        self.assertTrue(target_row["name_highlight_clinic_target"])

        clinic_session = clinic_session_model.objects.create(
            tenant=self.tenant,
            date=timezone.localdate() + datetime.timedelta(days=7),
            start_time=datetime.time(17, 0),
            location="성적표 예약 검증실",
            max_participants=10,
        )
        participant = session_participant_model.objects.create(
            tenant=self.tenant,
            session=clinic_session,
            student=self.active_enrollment.student,
            enrollment=self.active_enrollment,
            status=session_participant_model.Status.PENDING,
        )
        self.assertTrue(read_row()["name_highlight_clinic_target"])

        participant.status = session_participant_model.Status.BOOKED
        participant.save(update_fields=["status", "updated_at"])
        self.assertFalse(read_row()["name_highlight_clinic_target"])

        clinic_session.date = timezone.localdate() - datetime.timedelta(days=1)
        clinic_session.save(update_fields=["date", "updated_at"])
        participant.status = session_participant_model.Status.ATTENDED
        participant.save(update_fields=["status", "updated_at"])
        self.assertFalse(read_row()["name_highlight_clinic_target"])

        participant.completed_at = timezone.now()
        participant.save(update_fields=["completed_at", "updated_at"])
        completed_row = read_row()
        self.assertTrue(completed_row["clinic_required"])
        self.assertTrue(completed_row["name_highlight_clinic_target"])

        link.resolved_at = timezone.now()
        link.save(update_fields=["resolved_at", "updated_at"])
        passed_row = read_row()
        self.assertFalse(passed_row["clinic_required"])
        self.assertFalse(passed_row["name_highlight_clinic_target"])

    def test_session_scores_ignores_exam_clinic_link_when_source_not_in_session(self):
        other_exam = Exam.objects.create(
            tenant=self.tenant,
            title="다른 차시 시험",
            pass_score=60,
            max_score=100,
        )
        link = ClinicLink.objects.create(
            tenant=self.tenant,
            enrollment=self.active_enrollment,
            session=self.session,
            reason=ClinicLink.Reason.AUTO_FAILED,
            is_auto=True,
            source_type="exam",
            source_id=other_exam.id,
            meta={"kind": "EXAM_FAILED", "exam_id": other_exam.id},
        )

        request = self.factory.get(f"/api/v1/results/admin/sessions/{self.session.id}/scores/")
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)
        response = SessionScoresView.as_view()(request, session_id=self.session.id)

        self.assertEqual(response.status_code, 200, response.data)
        row = next(row for row in response.data["rows"] if row["enrollment_id"] == self.active_enrollment.id)
        self.assertFalse(row["clinic_required"])
        self.assertFalse(row["name_highlight_clinic_target"])

        targets = ClinicTargetService.list_admin_targets(tenant=self.tenant)
        self.assertFalse(any(target.get("clinic_link_id") == link.id for target in targets))

    def test_session_scores_ignores_homework_clinic_link_when_assignment_removed(self):
        HomeworkAssignment.objects.filter(
            homework=self.homework,
            enrollment=self.active_enrollment,
        ).delete()
        link = ClinicLink.objects.create(
            tenant=self.tenant,
            enrollment=self.active_enrollment,
            session=self.session,
            reason=ClinicLink.Reason.AUTO_FAILED,
            is_auto=True,
            source_type="homework",
            source_id=self.homework.id,
            meta={"kind": "HOMEWORK_FAILED", "homework_id": self.homework.id},
        )

        request = self.factory.get(f"/api/v1/results/admin/sessions/{self.session.id}/scores/")
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)
        response = SessionScoresView.as_view()(request, session_id=self.session.id)

        self.assertEqual(response.status_code, 200, response.data)
        row = next(row for row in response.data["rows"] if row["enrollment_id"] == self.active_enrollment.id)
        self.assertFalse(row["clinic_required"])
        self.assertFalse(row["name_highlight_clinic_target"])
        self.assertEqual(row["homeworks"], [])

        targets = ClinicTargetService.list_admin_targets(tenant=self.tenant)
        self.assertFalse(any(target.get("clinic_link_id") == link.id for target in targets))

    def test_homework_clinic_target_uses_homework_specific_cutline(self):
        self.homework.meta = {"default_max_score": 20}
        self.homework.cutline_mode = Homework.CutlineMode.COUNT
        self.homework.cutline_value = 15
        self.homework.round_unit_percent = 5
        self.homework.save(
            update_fields=[
                "meta",
                "cutline_mode",
                "cutline_value",
                "round_unit_percent",
                "updated_at",
            ]
        )
        HomeworkScore.objects.create(
            enrollment=self.active_enrollment,
            session=self.session,
            homework=self.homework,
            attempt_index=1,
            score=10,
            max_score=20,
            passed=False,
            clinic_required=True,
        )
        link = ClinicLink.objects.create(
            tenant=self.tenant,
            enrollment=self.active_enrollment,
            session=self.session,
            reason=ClinicLink.Reason.AUTO_FAILED,
            is_auto=True,
            approved=True,
            source_type="homework",
            source_id=self.homework.id,
            meta={"kind": "HOMEWORK_FAILED", "homework_id": self.homework.id},
        )

        count_target = next(
            target
            for target in ClinicTargetService.list_admin_targets(tenant=self.tenant)
            if target.get("clinic_link_id") == link.id
        )

        self.assertIsNone(count_target["exam_score"])
        self.assertIsNone(count_target["cutline_score"])
        self.assertEqual(count_target["homework_score"], 10.0)
        self.assertEqual(count_target["homework_cutline"], 15.0)
        self.assertEqual(count_target["homework_cutline_mode"], "COUNT")
        self.assertEqual(count_target["homework_cutline_value"], 15.0)
        self.assertEqual(count_target["max_score"], 20.0)

        self.homework.cutline_mode = Homework.CutlineMode.PERCENT
        self.homework.cutline_value = 70
        self.homework.save(
            update_fields=["cutline_mode", "cutline_value", "updated_at"]
        )
        percent_target = next(
            target
            for target in ClinicTargetService.list_admin_targets(tenant=self.tenant)
            if target.get("clinic_link_id") == link.id
        )

        self.assertEqual(percent_target["homework_cutline"], 14.0)
        self.assertEqual(percent_target["homework_cutline_mode"], "PERCENT")
        self.assertEqual(percent_target["homework_cutline_value"], 70.0)
        self.assertEqual(percent_target["homework_round_unit_percent"], 5)

    def test_session_scores_include_retake_history_and_final_pass(self):
        self.exam.pass_score = 70
        self.exam.max_score = 100
        self.exam.save(update_fields=["pass_score", "max_score"])

        attempt1 = ExamAttempt.objects.create(
            exam=self.exam,
            enrollment=self.active_enrollment,
            attempt_index=1,
            is_retake=False,
            is_representative=True,
            status="done",
            meta={
                "initial_snapshot": {
                    "total_score": 50.0,
                    "max_score": 100.0,
                    "source": "test",
                },
                "total_score": 50.0,
            },
        )
        Result.objects.create(
            target_type="exam",
            target_id=self.exam.id,
            enrollment=self.active_enrollment,
            attempt=attempt1,
            total_score=50,
            max_score=100,
        )
        link = ClinicLink.objects.create(
            tenant=self.tenant,
            enrollment=self.active_enrollment,
            session=self.session,
            reason=ClinicLink.Reason.AUTO_FAILED,
            is_auto=True,
            approved=True,
            source_type="exam",
            source_id=self.exam.id,
        )
        ClinicRemediationService.submit_exam_retake(
            clinic_link_id=link.id,
            score=70,
            max_score=100,
            pass_score=60,
            graded_by_user_id=self.admin.id,
        )

        request = self.factory.get(f"/api/v1/results/admin/sessions/{self.session.id}/scores/")
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)

        response = SessionScoresView.as_view()(request, session_id=self.session.id)

        self.assertEqual(response.status_code, 200, response.data)
        row = response.data["rows"][0]
        exam_entry = row["exams"][0]
        self.assertEqual(exam_entry["block"]["passed"], False)
        self.assertEqual(exam_entry["block"]["final_pass"], True)
        self.assertEqual(exam_entry["block"]["achievement"], "REMEDIATED")
        self.assertEqual(len(exam_entry["attempts"]), 2)
        self.assertEqual(exam_entry["attempts"][0]["pass_score"], 70.0)
        self.assertEqual(exam_entry["attempts"][1]["pass_score"], 60.0)
        self.assertEqual(exam_entry["attempts"][1]["passed"], True)
