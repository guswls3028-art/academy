import json
from io import BytesIO, StringIO

from django.contrib.auth import get_user_model
from django.apps import apps
from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIRequestFactory, force_authenticate
from openpyxl import load_workbook

from apps.core.models import Tenant, TenantMembership
from apps.domains.results.utils.session_exam import get_exams_for_session
from apps.domains.results.services.session_score_summary_service import (
    SessionScoreSummaryService,
)
from apps.domains.results.services.exam_analysis_export import (
    build_exam_analysis_export,
)
from apps.domains.results.services.clinic_target_service import ClinicTargetService
from apps.domains.results.aggregations.lecture_results import (
    build_lecture_results_snapshot,
)
from apps.domains.results.aggregations.session_results import (
    build_session_results_snapshot,
)
from apps.domains.results.views.admin_session_exams_summary_view import (
    AdminSessionExamsSummaryView,
)
from apps.domains.results.views.admin_exam_summary_view import AdminExamSummaryView
from apps.domains.results.views.session_scores_view import SessionScoresView
from apps.domains.results.views.question_stats_views import (
    _finalized_representative_scope,
)
from apps.domains.results.services.question_stats_service import QuestionStatsService


User = get_user_model()


class AssessmentLifecycleSsotTests(TestCase):
    def setUp(self):
        Attendance = apps.get_model("attendance", "Attendance")
        Enrollment = apps.get_model("enrollment", "Enrollment")
        SessionEnrollment = apps.get_model("enrollment", "SessionEnrollment")
        self.Exam = apps.get_model("exams", "Exam")
        Lecture = apps.get_model("lectures", "Lecture")
        Session = apps.get_model("lectures", "Session")
        self.ClinicLink = apps.get_model("progress", "ClinicLink")
        Student = apps.get_model("students", "Student")

        self.factory = APIRequestFactory()
        self.tenant = Tenant.objects.create(
            name="Assessment Lifecycle",
            code="assessment-life",
            is_active=True,
        )
        self.admin = User.objects.create_user(
            username="assessment-life-admin",
            password="test1234",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=self.admin,
            role="admin",
        )
        self.lecture = Lecture.objects.create(
            tenant=self.tenant,
            title="Lifecycle Lecture",
            name="Lifecycle Lecture",
            subject="MATH",
        )
        self.session = Session.objects.create(
            lecture=self.lecture,
            order=1,
            title="1회차",
        )
        student_user = User.objects.create_user(
            username="assessment-life-student",
            password="test1234",
            tenant=self.tenant,
        )
        self.student = Student.objects.create(
            tenant=self.tenant,
            user=student_user,
            name="학생",
            ps_number="AL-001",
            omr_code="AL000001",
            parent_phone="01000000000",
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
        Attendance.objects.create(
            tenant=self.tenant,
            session=self.session,
            enrollment=self.enrollment,
            status="PRESENT",
        )

    def _request(self, path: str):
        request = self.factory.get(path)
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)
        return request

    def test_live_session_exam_ssot_excludes_inactive_and_templates(self):
        active_exam = self.Exam.objects.create(
            tenant=self.tenant,
            title="운영 시험",
            exam_type="regular",
            is_active=True,
        )
        inactive_exam = self.Exam.objects.create(
            tenant=self.tenant,
            title="삭제된 시험",
            exam_type="regular",
            is_active=False,
        )
        template_exam = self.Exam.objects.create(
            tenant=self.tenant,
            title="양식",
            subject="MATH",
            exam_type="template",
            is_active=True,
        )
        active_exam.sessions.add(self.session)
        inactive_exam.sessions.add(self.session)
        template_exam.sessions.add(self.session)

        self.assertEqual(
            list(get_exams_for_session(self.session).values_list("id", flat=True)),
            [active_exam.id],
        )

        response = SessionScoresView.as_view()(
            self._request(f"/api/v1/results/admin/sessions/{self.session.id}/scores/"),
            session_id=self.session.id,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            [row["exam_id"] for row in response.data["meta"]["exams"]],
            [active_exam.id],
        )
        self.assertEqual(
            [row["exam_id"] for row in response.data["rows"][0]["exams"]],
            [active_exam.id],
        )

    def test_summary_clinic_rate_ignores_unresolved_link_whose_source_is_not_live(self):
        SessionProgress = apps.get_model("progress", "SessionProgress")
        inactive_exam = self.Exam.objects.create(
            tenant=self.tenant,
            title="삭제된 시험",
            exam_type="regular",
            is_active=False,
        )
        inactive_exam.sessions.add(self.session)
        SessionProgress.objects.create(
            session=self.session,
            enrollment=self.enrollment,
            completed=False,
        )
        self.ClinicLink.objects.create(
            tenant=self.tenant,
            enrollment=self.enrollment,
            session=self.session,
            reason=self.ClinicLink.Reason.AUTO_FAILED,
            is_auto=True,
            source_type="exam",
            source_id=inactive_exam.id,
        )

        response = AdminSessionExamsSummaryView.as_view()(
            self._request(
                f"/api/v1/results/admin/sessions/{self.session.id}/exams/summary/"
            ),
            session_id=self.session.id,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["participant_count"], 1)
        self.assertEqual(response.data["clinic_rate"], 0.0)

        active_exam = self.Exam.objects.create(
            tenant=self.tenant,
            title="운영 시험",
            exam_type="regular",
            is_active=True,
        )
        active_exam.sessions.add(self.session)
        self.ClinicLink.objects.create(
            tenant=self.tenant,
            enrollment=self.enrollment,
            session=self.session,
            reason=self.ClinicLink.Reason.AUTO_FAILED,
            is_auto=True,
            source_type="exam",
            source_id=active_exam.id,
        )

        response = AdminSessionExamsSummaryView.as_view()(
            self._request(
                f"/api/v1/results/admin/sessions/{self.session.id}/exams/summary/"
            ),
            session_id=self.session.id,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["clinic_rate"], 1.0)

    def test_session_score_attempt_stats_group_by_exam_and_enrollment(self):
        SessionProgress = apps.get_model("progress", "SessionProgress")
        ExamAttempt = apps.get_model("results", "ExamAttempt")
        Result = apps.get_model("results", "Result")

        SessionProgress.objects.create(
            session=self.session,
            enrollment=self.enrollment,
            completed=False,
        )
        exams = [
            self.Exam.objects.create(
                tenant=self.tenant,
                title=f"운영 시험 {idx}",
                exam_type="regular",
                is_active=True,
                max_score=100,
                pass_score=60,
            )
            for idx in (1, 2)
        ]
        for idx, exam in enumerate(exams, start=1):
            exam.sessions.add(self.session)
            attempt = ExamAttempt.objects.create(
                exam=exam,
                enrollment=self.enrollment,
                attempt_index=1,
                is_representative=True,
                status="done",
            )
            Result.objects.create(
                target_type="exam",
                target_id=exam.id,
                enrollment=self.enrollment,
                attempt=attempt,
                total_score=70 + idx,
                max_score=100,
            )

        summary = SessionScoreSummaryService.build(session_id=self.session.id)

        self.assertEqual(summary["attempt_stats"]["avg_attempts"], 1.0)
        self.assertEqual(summary["attempt_stats"]["retake_ratio"], 0.0)

    def test_retake_result_never_replaces_initial_score_in_session_consumers(self):
        SessionProgress = apps.get_model("progress", "SessionProgress")
        ExamAttempt = apps.get_model("results", "ExamAttempt")
        ExamQuestion = apps.get_model("exams", "ExamQuestion")
        Result = apps.get_model("results", "Result")
        ResultFact = apps.get_model("results", "ResultFact")
        ResultItem = apps.get_model("results", "ResultItem")
        Sheet = apps.get_model("exams", "Sheet")

        SessionProgress.objects.create(
            session=self.session,
            enrollment=self.enrollment,
            completed=False,
            exam_passed=False,
        )
        exam = self.Exam.objects.create(
            tenant=self.tenant,
            title="원시험과 재시험 분리",
            exam_type="regular",
            is_active=True,
            max_score=100,
            pass_score=60,
        )
        exam.sessions.add(self.session)
        sheet = Sheet.objects.create(exam=exam, name="MAIN", total_questions=1)
        question = ExamQuestion.objects.create(sheet=sheet, number=1, score=100)
        first_attempt = ExamAttempt.objects.create(
            exam=exam,
            enrollment=self.enrollment,
            attempt_index=1,
            is_representative=False,
            status="done",
            meta={
                "initial_snapshot": {
                    "total_score": 25.0,
                    "max_score": 100.0,
                    "source": "test",
                },
                "total_score": 25.0,
                "max_score": 100.0,
            },
        )
        retake = ExamAttempt.objects.create(
            exam=exam,
            enrollment=self.enrollment,
            attempt_index=2,
            is_retake=True,
            is_representative=True,
            status="done",
            meta={"total_score": 100.0, "max_score": 100.0, "pass_score": 60.0},
        )
        retake_result = Result.objects.create(
            target_type="exam",
            target_id=exam.id,
            enrollment=self.enrollment,
            attempt=retake,
            total_score=100,
            max_score=100,
        )
        ResultItem.objects.create(
            result=retake_result,
            question=question,
            answer="재시험 답안",
            is_correct=True,
            score=100,
            max_score=100,
            source="online",
        )
        ResultFact.objects.create(
            target_type="exam",
            target_id=exam.id,
            enrollment=self.enrollment,
            submission_id=0,
            attempt=first_attempt,
            question_id=question.id,
            answer="1차 오답",
            is_correct=False,
            score=0,
            max_score=100,
            source="online",
        )
        self.ClinicLink.objects.create(
            tenant=self.tenant,
            enrollment=self.enrollment,
            session=self.session,
            reason="AUTO_FAILED",
            source_type="exam",
            source_id=exam.id,
            is_auto=True,
            meta={"kind": "EXAM_FAILED", "exam_id": exam.id},
        )
        ResultFact.objects.create(
            target_type="exam",
            target_id=exam.id,
            enrollment=self.enrollment,
            submission_id=0,
            attempt=retake,
            question_id=question.id,
            answer="재시험 정답",
            is_correct=True,
            score=100,
            max_score=100,
            source="online",
        )

        summary = SessionScoreSummaryService.build(session_id=self.session.id)
        self.assertEqual(summary["avg_score"], 25.0)
        self.assertEqual(summary["min_score"], 25.0)
        self.assertEqual(summary["max_score"], 25.0)

        response = AdminSessionExamsSummaryView.as_view()(
            self._request(
                f"/api/v1/results/admin/sessions/{self.session.id}/exams/summary/"
            ),
            session_id=self.session.id,
        )
        self.assertEqual(response.status_code, 200, response.data)
        exam_summary = response.data["exams"][0]
        self.assertEqual(exam_summary["avg_score"], 25.0)
        self.assertEqual(exam_summary["highest_score"], 25.0)
        self.assertEqual(exam_summary["pass_count"], 0)
        self.assertEqual(exam_summary["fail_count"], 1)

        legacy_summary = AdminExamSummaryView.as_view()(
            self._request(f"/api/v1/results/admin/exams/{exam.id}/summary/"),
            exam_id=exam.id,
        )
        self.assertEqual(legacy_summary.status_code, 200, legacy_summary.data)
        self.assertEqual(legacy_summary.data["avg_score"], 25.0)
        self.assertEqual(legacy_summary.data["pass_count"], 0)
        self.assertEqual(legacy_summary.data["fail_count"], 1)

        scores_response = SessionScoresView.as_view()(
            self._request(
                f"/api/v1/results/admin/sessions/{self.session.id}/scores/"
            ),
            session_id=self.session.id,
        )
        self.assertEqual(scores_response.status_code, 200, scores_response.data)
        exam_entry = scores_response.data["rows"][0]["exams"][0]
        self.assertEqual(exam_entry["block"]["score"], 25.0)
        self.assertIsNone(exam_entry["block"]["objective_score"])
        self.assertIsNone(exam_entry["block"]["subjective_score"])
        self.assertFalse(exam_entry["block"]["passed"])
        self.assertEqual(
            [attempt["score"] for attempt in exam_entry["attempts"]],
            [25.0, 100.0],
        )
        self.assertEqual(exam_entry["items"], [])

        attempt_ids, legacy_enrollment_ids = _finalized_representative_scope(
            exam_id=exam.id,
            tenant=self.tenant,
        )
        self.assertEqual(attempt_ids, [first_attempt.id])
        self.assertEqual(legacy_enrollment_ids, [])
        question_stats = QuestionStatsService.per_question_stats(
            exam_id=exam.id,
            attempt_ids=attempt_ids,
            legacy_enrollment_ids=legacy_enrollment_ids,
        )
        self.assertEqual(question_stats[0]["attempts"], 1)
        self.assertEqual(question_stats[0]["correct"], 0)

        clinic_targets = ClinicTargetService.list_admin_targets(tenant=self.tenant)
        clinic_target = next(
            row for row in clinic_targets if int(row.get("exam_id") or 0) == exam.id
        )
        self.assertEqual(clinic_target["exam_score"], 25.0)
        self.assertEqual(
            [attempt["score"] for attempt in clinic_target["attempt_history"]],
            [25.0, 100.0],
        )

        workbook = load_workbook(BytesIO(build_exam_analysis_export(
            exam=exam,
            tenant=self.tenant,
        )))
        self.assertEqual(workbook["학생별 등수"]["E6"].value, 25)
        self.assertEqual(workbook["학생별 등수"]["L6"].value, "1")
        self.assertEqual(workbook["학생별 답안"]["H6"].value, "1차 오답")

        session_snapshot = build_session_results_snapshot(session_id=self.session.id)
        self.assertEqual(session_snapshot["exams"][0]["avg_score"], 25.0)
        self.assertEqual(session_snapshot["exams"][0]["pass_count"], 0)
        lecture_snapshot = build_lecture_results_snapshot(
            lecture_id=self.lecture.id,
            include_exam_level_stats=True,
        )
        self.assertEqual(lecture_snapshot["sessions"][0]["exams"][0]["avg_score"], 25.0)
        self.assertEqual(lecture_snapshot["sessions"][0]["exams"][0]["pass_count"], 0)

    def test_session_score_summary_uses_one_latest_result_lookup_for_many_exams(self):
        SessionProgress = apps.get_model("progress", "SessionProgress")
        Result = apps.get_model("results", "Result")

        SessionProgress.objects.create(
            session=self.session,
            enrollment=self.enrollment,
            completed=False,
        )
        for idx in range(3):
            exam = self.Exam.objects.create(
                tenant=self.tenant,
                title=f"쿼리 시험 {idx}",
                exam_type="regular",
                is_active=True,
                max_score=100,
                pass_score=60,
            )
            exam.sessions.add(self.session)
            Result.objects.create(
                target_type="exam",
                target_id=exam.id,
                enrollment=self.enrollment,
                total_score=70 + idx,
                max_score=100,
            )

        with CaptureQueriesContext(connection) as captured:
            summary = SessionScoreSummaryService.build(session_id=self.session.id)

        latest_result_queries = [
            query["sql"]
            for query in captured.captured_queries
            if 'FROM "results_result"' in query["sql"]
            and "MAX(" in query["sql"].upper()
        ]
        self.assertEqual(summary["avg_score"], 71.0)
        self.assertEqual(len(latest_result_queries), 1, latest_result_queries)

        with CaptureQueriesContext(connection) as captured:
            response = AdminSessionExamsSummaryView.as_view()(
                self._request(
                    f"/api/v1/results/admin/sessions/{self.session.id}/exams/summary/"
                ),
                session_id=self.session.id,
            )
        latest_result_queries = [
            query["sql"]
            for query in captured.captured_queries
            if 'FROM "results_result"' in query["sql"]
            and "MAX(" in query["sql"].upper()
        ]
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(latest_result_queries), 1, latest_result_queries)

    def test_session_score_summary_excludes_non_participant_and_foreign_results(self):
        Enrollment = apps.get_model("enrollment", "Enrollment")
        ExamAttempt = apps.get_model("results", "ExamAttempt")
        Lecture = apps.get_model("lectures", "Lecture")
        Result = apps.get_model("results", "Result")
        SessionProgress = apps.get_model("progress", "SessionProgress")
        Student = apps.get_model("students", "Student")

        SessionProgress.objects.create(
            session=self.session,
            enrollment=self.enrollment,
            completed=False,
        )
        exam = self.Exam.objects.create(
            tenant=self.tenant,
            title="테넌트 경계 시험",
            exam_type="regular",
            is_active=True,
            max_score=100,
            pass_score=60,
        )
        exam.sessions.add(self.session)
        local_attempt = ExamAttempt.objects.create(
            exam=exam,
            enrollment=self.enrollment,
            attempt_index=1,
            is_representative=True,
            status="done",
        )
        Result.objects.create(
            target_type="exam",
            target_id=exam.id,
            enrollment=self.enrollment,
            attempt=local_attempt,
            total_score=80,
            max_score=100,
        )

        outside_user = User.objects.create_user(
            username="assessment-life-outside-student",
            password="test1234",
            tenant=self.tenant,
        )
        outside_student = Student.objects.create(
            tenant=self.tenant,
            user=outside_user,
            name="차시 외 학생",
            ps_number="AL-O-001",
            omr_code="ALO00001",
            parent_phone="01000000000",
        )
        outside_enrollment = Enrollment.objects.create(
            tenant=self.tenant,
            student=outside_student,
            lecture=self.lecture,
            status="ACTIVE",
        )
        ExamAttempt.objects.create(
            exam=exam,
            enrollment=outside_enrollment,
            attempt_index=1,
            is_representative=False,
            status="done",
        )
        outside_attempt = ExamAttempt.objects.create(
            exam=exam,
            enrollment=outside_enrollment,
            attempt_index=2,
            is_representative=True,
            status="done",
        )
        Result.objects.create(
            target_type="exam",
            target_id=exam.id,
            enrollment=outside_enrollment,
            attempt=outside_attempt,
            total_score=0,
            max_score=100,
        )
        self.ClinicLink.objects.create(
            tenant=self.tenant,
            enrollment=outside_enrollment,
            session=self.session,
            reason=self.ClinicLink.Reason.AUTO_FAILED,
            is_auto=True,
            source_type="exam",
            source_id=exam.id,
        )

        foreign_tenant = Tenant.objects.create(
            name="Foreign Tenant",
            code="assessment-life-foreign",
            is_active=True,
        )
        foreign_user = User.objects.create_user(
            username="assessment-life-foreign-student",
            password="test1234",
            tenant=foreign_tenant,
        )
        foreign_student = Student.objects.create(
            tenant=foreign_tenant,
            user=foreign_user,
            name="외부 학생",
            ps_number="AL-F-001",
            omr_code="ALF00001",
            parent_phone="01000000000",
        )
        foreign_lecture = Lecture.objects.create(
            tenant=foreign_tenant,
            title="Foreign Lecture",
            name="Foreign Lecture",
            subject="MATH",
        )
        foreign_enrollment = Enrollment.objects.create(
            tenant=foreign_tenant,
            student=foreign_student,
            lecture=foreign_lecture,
            status="ACTIVE",
        )
        SessionProgress.objects.create(
            session=self.session,
            enrollment=foreign_enrollment,
            completed=True,
        )
        foreign_attempt = ExamAttempt.objects.create(
            exam=exam,
            enrollment=foreign_enrollment,
            attempt_index=1,
            is_representative=True,
            status="done",
        )
        ExamAttempt.objects.create(
            exam=exam,
            enrollment=foreign_enrollment,
            attempt_index=2,
            is_representative=False,
            status="done",
        )
        Result.objects.create(
            target_type="exam",
            target_id=exam.id,
            enrollment=foreign_enrollment,
            attempt=foreign_attempt,
            total_score=0,
            max_score=100,
        )

        summary = SessionScoreSummaryService.build(session_id=self.session.id)

        self.assertEqual(summary["avg_score"], 80.0)
        self.assertEqual(summary["min_score"], 80.0)
        self.assertEqual(summary["max_score"], 80.0)
        self.assertEqual(summary["participant_count"], 1)
        self.assertEqual(summary["pass_rate"], 0.0)
        self.assertEqual(summary["clinic_rate"], 0.0)
        self.assertEqual(summary["attempt_stats"]["avg_attempts"], 1.0)
        self.assertEqual(summary["attempt_stats"]["retake_ratio"], 0.0)

        response = AdminSessionExamsSummaryView.as_view()(
            self._request(
                f"/api/v1/results/admin/sessions/{self.session.id}/exams/summary/"
            ),
            session_id=self.session.id,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["participant_count"], 1)
        self.assertEqual(response.data["pass_rate"], 0.0)
        self.assertEqual(response.data["clinic_rate"], 0.0)
        self.assertEqual(response.data["exams"][0]["participant_count"], 1)
        self.assertEqual(response.data["exams"][0]["avg_score"], 80.0)
        self.assertEqual(response.data["exams"][0]["pass_count"], 1)
        self.assertEqual(response.data["exams"][0]["fail_count"], 0)

    def test_session_exams_summary_excludes_not_submitted_from_fail_count(self):
        SessionProgress = apps.get_model("progress", "SessionProgress")
        ExamAttempt = apps.get_model("results", "ExamAttempt")
        Result = apps.get_model("results", "Result")

        SessionProgress.objects.create(
            session=self.session,
            enrollment=self.enrollment,
            completed=False,
        )
        exam = self.Exam.objects.create(
            tenant=self.tenant,
            title="운영 시험",
            exam_type="regular",
            is_active=True,
            max_score=100,
            pass_score=60,
        )
        exam.sessions.add(self.session)
        attempt = ExamAttempt.objects.create(
            exam=exam,
            enrollment=self.enrollment,
            attempt_index=1,
            is_representative=True,
            status="done",
            meta={"status": "NOT_SUBMITTED"},
        )
        Result.objects.create(
            target_type="exam",
            target_id=exam.id,
            enrollment=self.enrollment,
            attempt=attempt,
            total_score=40,
            max_score=100,
        )

        summary = SessionScoreSummaryService.build(session_id=self.session.id)

        self.assertEqual(summary["avg_score"], 0.0)
        self.assertEqual(summary["min_score"], 0.0)
        self.assertEqual(summary["max_score"], 0.0)

        response = AdminSessionExamsSummaryView.as_view()(
            self._request(
                f"/api/v1/results/admin/sessions/{self.session.id}/exams/summary/"
            ),
            session_id=self.session.id,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["exams"][0]["participant_count"], 0)
        self.assertEqual(response.data["exams"][0]["pass_count"], 0)
        self.assertEqual(response.data["exams"][0]["fail_count"], 0)

    def test_session_exams_summary_separates_exam_max_from_highest_score(self):
        ExamAttempt = apps.get_model("results", "ExamAttempt")
        Result = apps.get_model("results", "Result")

        exam = self.Exam.objects.create(
            tenant=self.tenant,
            title="서술형 합산 시험",
            exam_type="regular",
            is_active=True,
            max_score=100,
            pass_score=60,
        )
        exam.sessions.add(self.session)
        attempt = ExamAttempt.objects.create(
            exam=exam,
            enrollment=self.enrollment,
            attempt_index=1,
            is_representative=True,
            status="done",
        )
        Result.objects.create(
            target_type="exam",
            target_id=exam.id,
            enrollment=self.enrollment,
            attempt=attempt,
            total_score=95,
            max_score=100,
        )

        response = AdminSessionExamsSummaryView.as_view()(
            self._request(
                f"/api/v1/results/admin/sessions/{self.session.id}/exams/summary/"
            ),
            session_id=self.session.id,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["exams"][0]["max_score"], 100.0)
        self.assertEqual(response.data["exams"][0]["highest_score"], 95.0)

    def test_session_exams_summary_does_not_treat_unset_pass_score_as_pass(self):
        Result = apps.get_model("results", "Result")

        exam = self.Exam.objects.create(
            tenant=self.tenant,
            title="기준 미설정 시험",
            exam_type="regular",
            is_active=True,
            max_score=100,
            pass_score=0,
        )
        exam.sessions.add(self.session)
        Result.objects.create(
            target_type="exam",
            target_id=exam.id,
            enrollment=self.enrollment,
            total_score=80,
            max_score=100,
        )

        response = AdminSessionExamsSummaryView.as_view()(
            self._request(
                f"/api/v1/results/admin/sessions/{self.session.id}/exams/summary/"
            ),
            session_id=self.session.id,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["exams"][0]["participant_count"], 1)
        self.assertEqual(response.data["exams"][0]["pass_count"], 0)
        self.assertEqual(response.data["exams"][0]["fail_count"], 0)
        self.assertEqual(response.data["exams"][0]["pass_rate"], 0.0)

    def test_detect_assessment_state_drift_command_reports_non_live_sources(self):
        inactive_exam = self.Exam.objects.create(
            tenant=self.tenant,
            title="삭제된 시험",
            exam_type="regular",
            is_active=False,
        )
        inactive_exam.sessions.add(self.session)
        self.ClinicLink.objects.create(
            tenant=self.tenant,
            enrollment=self.enrollment,
            session=self.session,
            reason=self.ClinicLink.Reason.AUTO_FAILED,
            is_auto=True,
            source_type="exam",
            source_id=inactive_exam.id,
        )

        out = StringIO()
        call_command(
            "detect_assessment_state_drift",
            "--tenant",
            str(self.tenant.id),
            "--json",
            stdout=out,
        )
        report = json.loads(out.getvalue())

        self.assertEqual(report["inactive_regular_linked_exam_count"], 1)
        self.assertEqual(report["unresolved_non_live_source_clinic_link_count"], 1)

    def test_assessment_drift_commands_detect_and_repair_unassigned_exam_link(self):
        Enrollment = apps.get_model("enrollment", "Enrollment")
        ExamEnrollment = apps.get_model("exams", "ExamEnrollment")
        Student = apps.get_model("students", "Student")
        other_user = User.objects.create_user(
            username="assessment-life-targeted-student",
            password="test1234",
            tenant=self.tenant,
        )
        other_student = Student.objects.create(
            tenant=self.tenant,
            user=other_user,
            name="배정 학생",
            ps_number="AL-002",
            omr_code="AL000002",
            parent_phone="01000000000",
        )
        other_enrollment = Enrollment.objects.create(
            tenant=self.tenant,
            student=other_student,
            lecture=self.lecture,
            status="ACTIVE",
        )
        exam = self.Exam.objects.create(
            tenant=self.tenant,
            title="일부 학생만 보는 시험",
            exam_type="regular",
            is_active=True,
        )
        exam.sessions.add(self.session)
        ExamEnrollment.objects.create(exam=exam, enrollment=other_enrollment)
        link = self.ClinicLink.objects.create(
            tenant=self.tenant,
            enrollment=self.enrollment,
            session=self.session,
            reason=self.ClinicLink.Reason.AUTO_FAILED,
            is_auto=True,
            source_type="exam",
            source_id=exam.id,
        )

        out = StringIO()
        call_command(
            "detect_assessment_state_drift",
            "--tenant",
            str(self.tenant.id),
            "--json",
            stdout=out,
        )
        report = json.loads(out.getvalue())
        self.assertEqual(report["unresolved_non_live_source_clinic_link_count"], 1)
        self.assertEqual(
            report["unresolved_non_live_source_clinic_link_reason_counts"],
            {"exam_enrollment_unassigned": 1},
        )
        self.assertEqual(
            report["samples"]["unresolved_non_live_source_clinic_links"][0]["id"],
            link.id,
        )
        self.assertEqual(
            report["samples"]["unresolved_non_live_source_clinic_links"][0]["state_reason"],
            "exam_enrollment_unassigned",
        )

        out = StringIO()
        call_command(
            "repair_assessment_state_drift",
            "--tenant",
            str(self.tenant.id),
            "--apply",
            "--json",
            stdout=out,
        )
        repair = json.loads(out.getvalue())
        link.refresh_from_db()
        self.assertEqual(repair["resolved_non_live_source_clinic_link_count"], 1)
        self.assertEqual(
            repair["samples"]["non_live_source_clinic_links"][0]["state_reason"],
            "exam_enrollment_unassigned",
        )
        self.assertEqual(link.resolution_type, self.ClinicLink.ResolutionType.SOURCE_REMOVED)
        self.assertIsNotNone(link.resolved_at)

    def test_repair_assessment_state_drift_detaches_inactive_exam_links(self):
        inactive_exam = self.Exam.objects.create(
            tenant=self.tenant,
            title="삭제된 시험",
            exam_type="regular",
            is_active=False,
        )
        inactive_exam.sessions.add(self.session)

        out = StringIO()
        call_command(
            "repair_assessment_state_drift",
            "--tenant",
            str(self.tenant.id),
            "--json",
            stdout=out,
        )
        dry_run_report = json.loads(out.getvalue())

        self.assertEqual(dry_run_report["mode"], "dry-run")
        self.assertEqual(dry_run_report["detachable_exam_session_pair_count"], 1)
        self.assertTrue(inactive_exam.sessions.filter(id=self.session.id).exists())

        out = StringIO()
        call_command(
            "repair_assessment_state_drift",
            "--tenant",
            str(self.tenant.id),
            "--apply",
            "--json",
            stdout=out,
        )
        apply_report = json.loads(out.getvalue())
        inactive_exam.refresh_from_db()

        self.assertEqual(apply_report["mode"], "apply")
        self.assertEqual(apply_report["detachable_exam_session_pair_count"], 1)
        self.assertFalse(inactive_exam.sessions.filter(id=self.session.id).exists())
