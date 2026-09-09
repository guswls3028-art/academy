from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.core.models import Tenant, TenantMembership
from apps.domains.exams.models import Exam, Sheet
from apps.domains.enrollment.models import Enrollment
from apps.domains.lectures.models import Lecture
from apps.domains.results.models import ExamAttempt, Result
from apps.domains.students.models import Student
from apps.domains.exams.serializers.exam import ExamSerializer
from apps.domains.exams.serializers.exam_update import ExamUpdateSerializer
from apps.domains.exams.views.exam_view import ExamViewSet


class ExamPolicyUpdateTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.tenant = Tenant.objects.create(
            name="시험 정책 동시 수정",
            code="exam-policy-update",
            is_active=True,
        )
        self.user = get_user_model().objects.create_user(
            username="exam-policy-admin",
            password="pw1234",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=self.user,
            role="admin",
        )
        self.exam = Exam.objects.create(
            tenant=self.tenant,
            title="중간 점검",
            exam_type=Exam.ExamType.REGULAR,
            max_score=100,
            pass_score=80,
        )
        self.lecture = Lecture.objects.create(
            tenant=self.tenant,
            title="시험 정책 강의",
            name="시험 정책 강의",
            subject="MATH",
        )
        student_user = get_user_model().objects.create_user(
            username="exam-policy-student",
            password="pw1234",
            tenant=self.tenant,
        )
        student = Student.objects.create(
            tenant=self.tenant,
            user=student_user,
            name="시험 정책 학생",
            ps_number="EXAM-POLICY-1",
        )
        self.enrollment = Enrollment.objects.create(
            tenant=self.tenant,
            student=student,
            lecture=self.lecture,
            status="ACTIVE",
        )

    def patch(self, data, *, expected_updated_at: str | None = None):
        headers = {}
        if expected_updated_at is not None:
            headers["HTTP_X_EXPECTED_UPDATED_AT"] = expected_updated_at
        request = self.factory.patch(
            f"/api/v1/exams/{self.exam.id}/",
            data,
            format="json",
            **headers,
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.user)
        view = ExamViewSet.as_view({"patch": "partial_update"})
        return view(request, pk=self.exam.id)

    def test_patch_returns_complete_exam_and_accepts_current_version(self):
        expected_updated_at = ExamSerializer(self.exam).data["updated_at"]

        response = self.patch(
            {"pass_score": 75},
            expected_updated_at=expected_updated_at,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["id"], self.exam.id)
        self.assertEqual(response.data["title"], "중간 점검")
        self.assertEqual(response.data["pass_score"], 75)
        self.assertTrue(response.data["updated_at"])

    def test_patch_accepts_zero_pass_score_with_postgresql_compatible_lock_query(self):
        raw_request = self.factory.get(f"/api/v1/exams/{self.exam.id}/")
        request = Request(raw_request)
        request.tenant = self.tenant
        view = ExamViewSet()
        view.request = request

        self.assertFalse(view.get_queryset().query.distinct)

        expected_updated_at = ExamSerializer(self.exam).data["updated_at"]
        response = self.patch(
            {"max_score": 15, "pass_score": 0},
            expected_updated_at=expected_updated_at,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["max_score"], 15)
        self.assertEqual(response.data["pass_score"], 0)

    def test_patch_rejects_invalid_score_and_retake_bounds(self):
        cases = (
            ({"max_score": 0, "pass_score": 0}, "max_score"),
            ({"pass_score": -1}, "pass_score"),
            ({"allow_retake": True, "max_attempts": 1}, "max_attempts"),
        )

        for payload, error_field in cases:
            with self.subTest(payload=payload):
                self.exam.refresh_from_db()
                expected_updated_at = ExamSerializer(self.exam).data["updated_at"]
                response = self.patch(
                    payload,
                    expected_updated_at=expected_updated_at,
                )

                self.assertEqual(response.status_code, 400, response.data)
                self.assertIn(error_field, response.data)

    def test_patch_rejects_max_below_current_score_until_score_is_corrected(self):
        attempt = ExamAttempt.objects.create(
            exam=self.exam,
            enrollment=self.enrollment,
            attempt_index=1,
            is_retake=False,
            is_representative=True,
            status="done",
            meta={
                "initial_snapshot": {
                    "total_score": 90.0,
                    "max_score": 100.0,
                }
            },
        )
        result = Result.objects.create(
            target_type="exam",
            target_id=self.exam.id,
            enrollment=self.enrollment,
            attempt=attempt,
            total_score=90,
            max_score=100,
        )

        rejected = self.patch({"max_score": 85, "pass_score": 80})

        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertIn("max_score", rejected.data)
        self.exam.refresh_from_db()
        self.assertEqual(self.exam.max_score, 100)
        original_attempt_meta = dict(attempt.meta)

        result.total_score = 80
        result.save(update_fields=["total_score", "updated_at"])
        accepted = self.patch({"max_score": 85, "pass_score": 80})

        self.assertEqual(accepted.status_code, 200, accepted.data)
        self.assertEqual(accepted.data["max_score"], 85)
        attempt.refresh_from_db()
        self.assertEqual(attempt.meta, original_attempt_meta)

    def test_patch_rejects_max_below_preserved_first_attempt_score(self):
        first_attempt = ExamAttempt.objects.create(
            exam=self.exam,
            enrollment=self.enrollment,
            attempt_index=1,
            is_retake=False,
            is_representative=False,
            status="done",
            meta={
                "initial_snapshot": {
                    "total_score": 90.0,
                    "max_score": 100.0,
                }
            },
        )
        representative_attempt = ExamAttempt.objects.create(
            exam=self.exam,
            enrollment=self.enrollment,
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
            attempt=representative_attempt,
            total_score=80,
            max_score=100,
        )

        rejected = self.patch({"max_score": 85, "pass_score": 80})

        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertIn("max_score", rejected.data)
        self.exam.refresh_from_db()
        self.assertEqual(self.exam.max_score, 100)
        self.assertTrue(ExamAttempt.objects.filter(id=first_attempt.id).exists())
        self.assertTrue(
            ExamAttempt.objects.filter(id=representative_attempt.id).exists()
        )

    def test_student_result_publication_defaults_on_and_can_be_disabled(self):
        expected_updated_at = ExamSerializer(self.exam).data["updated_at"]

        response = self.patch(
            {"student_results_published": False},
            expected_updated_at=expected_updated_at,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data["student_results_published"])
        self.exam.refresh_from_db()
        self.assertFalse(self.exam.student_results_published)

    def test_patch_rejects_stale_version_without_overwriting_newer_value(self):
        stale_updated_at = ExamSerializer(self.exam).data["updated_at"]
        newer_updated_at = timezone.now() + timedelta(seconds=1)
        Exam.objects.filter(pk=self.exam.id).update(
            pass_score=70,
            updated_at=newer_updated_at,
        )

        response = self.patch(
            {"pass_score": 60},
            expected_updated_at=stale_updated_at,
        )

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["code"], "stale_resource")
        self.exam.refresh_from_db()
        self.assertEqual(self.exam.pass_score, 70)

    def test_mixed_mode_can_repair_zero_boundary_from_existing_sheet(self):
        Sheet.objects.create(
            exam=self.exam,
            total_questions=2,
            choice_count=1,
            essay_count=1,
        )
        serializer = ExamUpdateSerializer(
            self.exam,
            data={
                "grading_mode": Exam.GradingMode.MIXED,
                "choice_question_count": 1,
            },
            partial=True,
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()
        self.exam.refresh_from_db()
        self.assertEqual(self.exam.grading_mode, Exam.GradingMode.MIXED)
        self.assertEqual(self.exam.choice_question_count, 1)
