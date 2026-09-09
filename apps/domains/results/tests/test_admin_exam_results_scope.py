from io import BytesIO

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from openpyxl import load_workbook
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.core.models import Tenant, TenantMembership
from apps.domains.results.models import ExamAttempt, Result
from apps.domains.results.aggregations.lecture_results import (
    build_lecture_results_snapshot,
)
from apps.domains.results.aggregations.session_results import (
    build_session_results_snapshot,
)
from apps.domains.results.services.exam_analysis_export import (
    build_exam_analysis_export,
)
from apps.domains.results.services.assessment_correction_status import (
    exam_correction_fingerprint,
)
from apps.domains.results.views.admin_exam_results_view import (
    AdminExamResultsView,
    _result_display_status,
)


User = get_user_model()


class ResultDisplayStatusTests(SimpleTestCase):
    def test_status_precedence_and_score_zero(self):
        cases = [
            ({"meta_status": "NOT_SUBMITTED", "submission_status": "done", "visible_total_score": 100, "is_provisional": False}, "NOT_SUBMITTED"),
            ({"meta_status": None, "submission_status": "failed", "visible_total_score": None, "is_provisional": False}, "FAILED"),
            ({"meta_status": None, "submission_status": "grading", "visible_total_score": 10, "is_provisional": False}, "PROCESSING"),
            ({"meta_status": None, "submission_status": "done", "visible_total_score": None, "is_provisional": False}, "DONE"),
            ({"meta_status": None, "submission_status": None, "visible_total_score": 0, "is_provisional": False}, "DONE"),
            ({"meta_status": None, "submission_status": None, "visible_total_score": 10, "is_provisional": True}, "PARTIAL"),
            ({"meta_status": None, "submission_status": None, "visible_total_score": None, "is_provisional": False}, "NOT_SUBMITTED"),
        ]
        for kwargs, expected in cases:
            with self.subTest(expected=expected, kwargs=kwargs):
                self.assertEqual(_result_display_status(**kwargs), expected)


class AdminExamResultsScopeTest(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.tenant = Tenant.objects.create(
            name="Admin Exam Results Scope",
            code="admin-exam-results-scope",
            is_active=True,
        )
        self.Lecture = django_apps.get_model("lectures", "Lecture")
        self.Session = django_apps.get_model("lectures", "Session")
        self.Student = django_apps.get_model("students", "Student")
        self.Enrollment = django_apps.get_model("enrollment", "Enrollment")
        self.Exam = django_apps.get_model("exams", "Exam")
        self.admin_user = User.objects.create_user(
            username="admin_exam_results_scope_admin",
            password="test1234",
            tenant=self.tenant,
            is_staff=True,
            is_superuser=True,
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=self.admin_user, role="admin")
        self.lecture = self.Lecture.objects.create(
            tenant=self.tenant,
            title="Scope Lecture",
            name="Scope Lecture",
            subject="MATH",
        )
        self.lec_session = self.Session.objects.create(lecture=self.lecture, order=1, title="1회차")
        self.enrollment = self._make_enrollment(self.tenant, self.lecture, "SCOPE001", "범위 학생")

    def _make_enrollment(self, tenant, lecture, ps_number: str, name: str):
        user = User.objects.create_user(
            username=f"{tenant.code}_{ps_number}",
            password="test1234",
            tenant=tenant,
        )
        student = self.Student.objects.create(
            tenant=tenant,
            user=user,
            ps_number=ps_number,
            omr_code=ps_number[-8:],
            name=name,
            parent_phone="01000000000",
        )
        return self.Enrollment.objects.create(
            tenant=tenant,
            student=student,
            lecture=lecture,
            status="ACTIVE",
        )

    def _make_exam(self, title="scope exam"):
        exam = self.Exam.objects.create(
            tenant=self.tenant,
            title=title,
            pass_score=60,
            max_score=100,
        )
        exam.sessions.add(self.lec_session)
        return exam

    def _get(self, exam_id: int, *, lecture_id: int | None = None):
        suffix = f"?lecture_id={lecture_id}" if lecture_id is not None else ""
        request = self.factory.get(
            f"/results/admin/exams/{exam_id}/results/{suffix}"
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin_user)
        return AdminExamResultsView.as_view()(request, exam_id=exam_id)

    def test_null_enrollment_result_is_ignored_without_500(self):
        exam = self._make_exam()
        Result.objects.create(
            target_type="exam",
            target_id=exam.id,
            enrollment=None,
            total_score=10,
            max_score=100,
        )
        Result.objects.create(
            target_type="exam",
            target_id=exam.id,
            enrollment=self.enrollment,
            total_score=80,
            max_score=100,
        )

        response = self._get(exam.id)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 1)
        row = response.data["results"][0]
        self.assertEqual(row["enrollment_id"], self.enrollment.id)
        self.assertEqual(row["ranking_score"], 80.0)
        self.assertEqual(row["result_status"], "DONE")

    def test_cross_tenant_enrollment_result_is_ignored(self):
        exam = self._make_exam()
        other_tenant = Tenant.objects.create(
            name="Admin Exam Results Scope Other",
            code="admin-exam-results-scope-other",
            is_active=True,
        )
        other_lecture = self.Lecture.objects.create(
            tenant=other_tenant,
            title="Other Lecture",
            name="Other Lecture",
            subject="MATH",
        )
        other_enrollment = self._make_enrollment(other_tenant, other_lecture, "OTHER001", "타 테넌트 학생")
        Result.objects.create(
            target_type="exam",
            target_id=exam.id,
            enrollment=other_enrollment,
            total_score=100,
            max_score=100,
        )
        Result.objects.create(
            target_type="exam",
            target_id=exam.id,
            enrollment=self.enrollment,
            total_score=80,
            max_score=100,
        )

        response = self._get(exam.id)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["enrollment_id"], self.enrollment.id)

    def test_results_expose_current_correction_for_one_exact_lecture_session(self):
        exam = self._make_exam()
        result = Result.objects.create(
            target_type="exam",
            target_id=exam.id,
            enrollment=self.enrollment,
            total_score=70,
            max_score=100,
        )

        pending = self._get(exam.id)

        self.assertEqual(pending.status_code, 200, pending.data)
        self.assertEqual(
            pending.data["results"][0]["correction_session_id"],
            self.lec_session.id,
        )
        self.assertEqual(pending.data["results"][0]["correction_status"], "PENDING")

        assessment_correction = django_apps.get_model("progress", "AssessmentCorrection")
        assessment_correction.objects.create(
            tenant=self.tenant,
            enrollment=self.enrollment,
            session=self.lec_session,
            source_type=assessment_correction.SourceType.EXAM,
            source_id=exam.id,
            completed=True,
            source_fingerprint=exam_correction_fingerprint(
                result=result,
                items=result.items.all(),
                current_max_score=float(exam.max_score),
            ),
            updated_by=self.admin_user,
        )

        completed = self._get(exam.id)

        self.assertEqual(completed.data["results"][0]["correction_status"], "COMPLETED")

        result.total_score = 60
        result.save(update_fields=["total_score", "updated_at"])
        stale = self._get(exam.id)
        self.assertEqual(stale.data["results"][0]["correction_status"], "PENDING")

        second_session = self.Session.objects.create(
            lecture=self.lecture,
            order=2,
            title="2회차",
        )
        exam.sessions.add(second_session)
        ambiguous = self._get(exam.id)
        self.assertIsNone(ambiguous.data["results"][0]["correction_session_id"])
        self.assertIsNone(ambiguous.data["results"][0]["correction_status"])

    def test_results_expose_ranking_score_and_backend_status_in_rank_order(self):
        exam = self._make_exam()
        peer = self._make_enrollment(
            self.tenant,
            self.lecture,
            "SCOPE002",
            "두번째 학생",
        )
        first_attempt = ExamAttempt.objects.create(
            exam=exam,
            enrollment=self.enrollment,
            submission_id=0,
            attempt_index=1,
            is_representative=False,
            status="done",
            meta={"initial_snapshot": {"total_score": 20}},
        )
        representative_attempt = ExamAttempt.objects.create(
            exam=exam,
            enrollment=self.enrollment,
            submission_id=0,
            attempt_index=2,
            is_retake=True,
            is_representative=True,
            status="done",
            meta={"total_score": 19},
        )
        peer_attempt = ExamAttempt.objects.create(
            exam=exam,
            enrollment=peer,
            submission_id=0,
            attempt_index=1,
            is_representative=True,
            status="done",
            meta={"initial_snapshot": {"total_score": 19}},
        )
        Result.objects.create(
            target_type="exam",
            target_id=exam.id,
            enrollment=self.enrollment,
            attempt=representative_attempt,
            total_score=19,
            max_score=100,
        )
        Result.objects.create(
            target_type="exam",
            target_id=exam.id,
            enrollment=peer,
            attempt=peer_attempt,
            total_score=19,
            max_score=100,
        )

        response = self._get(exam.id)

        self.assertEqual(response.status_code, 200, response.data)
        rows = response.data["results"]
        self.assertEqual(
            [row["enrollment_id"] for row in rows],
            [self.enrollment.id, peer.id],
        )
        self.assertEqual(
            [(row["rank"], row["ranking_score"], row["final_score"]) for row in rows],
            [(1, 20.0, 20.0), (2, 19.0, 19.0)],
        )
        self.assertEqual([row["result_status"] for row in rows], ["DONE", "DONE"])
        self.assertEqual(first_attempt.attempt_index, 1)

    def test_confirmed_same_first_attempt_total_updates_score_and_competition_rank(self):
        exam = self._make_exam()
        enrollments = [
            self.enrollment,
            self._make_enrollment(
                self.tenant,
                self.lecture,
                "SCOPE002",
                "두번째 학생",
            ),
            self._make_enrollment(
                self.tenant,
                self.lecture,
                "SCOPE003",
                "세번째 학생",
            ),
        ]
        for enrollment, confirmed_score in zip(
            enrollments,
            [100, 95, 95],
            strict=True,
        ):
            attempt = ExamAttempt.objects.create(
                exam=exam,
                enrollment=enrollment,
                submission_id=0,
                attempt_index=1,
                is_representative=True,
                status="done",
                meta={"initial_snapshot": {"total_score": 95, "max_score": 100}},
            )
            Result.objects.create(
                target_type="exam",
                target_id=exam.id,
                enrollment=enrollment,
                attempt=attempt,
                total_score=confirmed_score,
                max_score=100,
            )

        response = self._get(exam.id)

        self.assertEqual(response.status_code, 200, response.data)
        rows = response.data["results"]
        self.assertEqual(
            [
                (row["rank"], row["ranking_score"], row["final_score"])
                for row in rows
            ],
            [(1, 100.0, 100.0), (2, 95.0, 95.0), (2, 95.0, 95.0)],
        )

    def test_ties_use_competition_rank_and_skip_occupied_positions(self):
        exam = self._make_exam()
        enrollments = [
            self.enrollment,
            self._make_enrollment(self.tenant, self.lecture, "SCOPE002", "두번째 학생"),
            self._make_enrollment(self.tenant, self.lecture, "SCOPE003", "세번째 학생"),
            self._make_enrollment(self.tenant, self.lecture, "SCOPE004", "네번째 학생"),
        ]
        for enrollment, score in zip(enrollments, [19, 14, 14, 12], strict=True):
            Result.objects.create(
                target_type="exam",
                target_id=exam.id,
                enrollment=enrollment,
                total_score=score,
                max_score=100,
            )

        response = self._get(exam.id)

        self.assertEqual(response.status_code, 200, response.data)
        rows = response.data["results"]
        self.assertEqual(
            [(row["rank"], row["ranking_score"]) for row in rows],
            [(1, 19.0), (2, 14.0), (2, 14.0), (4, 12.0)],
        )
        self.assertEqual([row["cohort_size"] for row in rows], [4, 4, 4, 4])
        self.assertEqual([row["percentile"] for row in rows], [25.0, 50.0, 50.0, 100.0])

    def test_shared_exam_filters_results_and_applies_each_lecture_cutoff(self):
        exam = self._make_exam()
        lecture_b = self.Lecture.objects.create(
            tenant=self.tenant,
            title="B학교 강의",
            name="B학교 강의",
            subject="MATH",
        )
        session_b = self.Session.objects.create(
            lecture=lecture_b,
            order=1,
            title="1회차",
        )
        enrollment_b = self._make_enrollment(
            self.tenant,
            lecture_b,
            "SCOPEB01",
            "B학교 학생",
        )
        exam.sessions.add(session_b)
        ExamLecturePolicy = django_apps.get_model("exams", "ExamLecturePolicy")
        ExamLecturePolicy.objects.create(
            exam=exam,
            lecture=lecture_b,
            pass_score=70,
        )
        Result.objects.create(
            target_type="exam",
            target_id=exam.id,
            enrollment=self.enrollment,
            total_score=65,
            max_score=100,
        )
        Result.objects.create(
            target_type="exam",
            target_id=exam.id,
            enrollment=enrollment_b,
            total_score=65,
            max_score=100,
        )
        ExamEnrollment = django_apps.get_model("exams", "ExamEnrollment")
        ExamEnrollment.objects.bulk_create([
            ExamEnrollment(exam=exam, enrollment=self.enrollment),
            ExamEnrollment(exam=exam, enrollment=enrollment_b),
        ])

        overall = self._get(exam.id)
        filtered = self._get(exam.id, lecture_id=lecture_b.id)

        self.assertEqual(overall.status_code, 200, overall.data)
        self.assertEqual(overall.data["count"], 2)
        rows_by_lecture = {
            row["lecture_id"]: row for row in overall.data["results"]
        }
        self.assertEqual(rows_by_lecture[self.lecture.id]["pass_score"], 60.0)
        self.assertTrue(rows_by_lecture[self.lecture.id]["passed"])
        self.assertEqual(rows_by_lecture[lecture_b.id]["pass_score"], 70.0)
        self.assertFalse(rows_by_lecture[lecture_b.id]["passed"])
        self.assertEqual(
            {row["cohort_size"] for row in overall.data["results"]},
            {2},
        )

        self.assertEqual(filtered.status_code, 200, filtered.data)
        self.assertEqual(filtered.data["count"], 1)
        self.assertEqual(filtered.data["results"][0]["lecture_id"], lecture_b.id)
        self.assertEqual(filtered.data["results"][0]["cohort_size"], 1)
        self.assertEqual(filtered.data["selected_lecture_id"], lecture_b.id)

        session_a_snapshot = build_session_results_snapshot(
            session_id=self.lec_session.id,
        )
        session_b_snapshot = build_session_results_snapshot(session_id=session_b.id)
        self.assertEqual(
            {
                key: session_a_snapshot["exams"][0][key]
                for key in (
                    "pass_score",
                    "participant_count",
                    "pass_count",
                    "fail_count",
                )
            },
            {"pass_score": 60.0, "participant_count": 1, "pass_count": 1, "fail_count": 0},
        )
        self.assertEqual(
            {
                key: session_b_snapshot["exams"][0][key]
                for key in (
                    "pass_score",
                    "participant_count",
                    "pass_count",
                    "fail_count",
                )
            },
            {"pass_score": 70.0, "participant_count": 1, "pass_count": 0, "fail_count": 1},
        )
        lecture_b_snapshot = build_lecture_results_snapshot(
            lecture_id=lecture_b.id,
            include_exam_level_stats=True,
        )
        self.assertEqual(
            lecture_b_snapshot["sessions"][0]["exams"][0]["participant_count"],
            1,
        )
        self.assertEqual(
            lecture_b_snapshot["sessions"][0]["exams"][0]["pass_score"],
            70.0,
        )

        workbook = load_workbook(BytesIO(build_exam_analysis_export(
            exam=exam,
            tenant=self.tenant,
        )))
        ranked_sheet = workbook["학생별 등수"]
        export_by_lecture = {
            ranked_sheet.cell(row, 4).value: {
                "pass_score": ranked_sheet.cell(row, 9).value,
                "verdict": ranked_sheet.cell(row, 10).value,
            }
            for row in range(6, 8)
        }
        self.assertEqual(
            export_by_lecture[self.lecture.title],
            {"pass_score": 60, "verdict": "합격"},
        )
        self.assertEqual(
            export_by_lecture[lecture_b.title],
            {"pass_score": 70, "verdict": "보충 대상"},
        )
