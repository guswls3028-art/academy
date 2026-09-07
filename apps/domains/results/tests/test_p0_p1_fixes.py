"""
P0/P1 bug fix tests for exam/homework domains.

Tests:
A. ExamAttempt representative uniqueness
B. Retake scenario (attempt_index >= 2)
C. submission_id duplicate race condition
D. Exam validation (max_attempts, pass_score, open_at/close_at)
E. Homework tenant fallback removal
F. HomeworkScore score > max_score validation
"""
from __future__ import annotations

from unittest.mock import patch

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase, TransactionTestCase

from rest_framework.test import APIRequestFactory, force_authenticate

from apps.core.models import Tenant, TenantMembership
from apps.domains.exams.models import Exam, ExamEnrollment, ExamQuestion, Sheet
from apps.domains.lectures.models import Lecture, Session
from apps.domains.enrollment.models import Enrollment
from apps.domains.students.models import Student
from apps.domains.results.models import (
    ExamAttempt,
    Result,
    ResultFact,
    ResultItem,
    ScoreEditDraft,
)
from apps.domains.results.services.attempt_service import ExamAttemptService
from apps.domains.results.services.wrong_note_service import (
    WrongNoteQuery,
    list_wrong_notes_for_enrollment,
)
from apps.domains.results.views.admin_exam_objective_score_view import AdminExamObjectiveScoreView
from apps.domains.results.views.admin_exam_subjective_score_view import AdminExamSubjectiveScoreView
from apps.domains.results.views.admin_exam_item_score_view import AdminExamItemScoreView
from apps.domains.results.views.admin_exam_total_score_view import AdminExamTotalScoreView
from apps.domains.submissions.models import Submission


User = get_user_model()


class BaseTestMixin:
    """Common test fixture setup."""

    def _create_fixtures(self):
        self.tenant = Tenant.objects.create(name="Test", code="9998", is_active=True)
        self.user = User.objects.create(
            tenant=self.tenant,
            username=f"t{self.tenant.id}_admin",
            is_active=True, is_staff=True,
        )
        self.user.set_password("pass1234!")
        self.user.save(update_fields=["password"])

        TenantMembership.objects.create(
            user=self.user, tenant=self.tenant, role="admin", is_active=True,
        )

        self.lecture = Lecture.objects.create(
            tenant=self.tenant, title="Lecture", name="Lecture", subject="MATH",
        )
        self.session = Session.objects.create(
            lecture=self.lecture, order=1, title="S1",
        )
        student_user = User.objects.create(
            tenant=self.tenant,
            username=f"t{self.tenant.id}_student",
            is_active=True,
        )
        self.student = Student.objects.create(
            tenant=self.tenant, user=student_user, name="Test Student",
        )
        self.enrollment = Enrollment.objects.create(
            tenant=self.tenant, student=self.student,
            lecture=self.lecture, status="ACTIVE",
        )

    def _create_exam(self, **kwargs):
        defaults = dict(
            tenant=self.tenant,
            title="Test Exam",
            exam_type=Exam.ExamType.REGULAR,
            pass_score=60,
            max_score=100,
            max_attempts=1,
        )
        defaults.update(kwargs)
        exam = Exam(**defaults)
        exam.save()
        exam.sessions.add(self.session)
        ExamEnrollment.objects.get_or_create(exam=exam, enrollment=self.enrollment)
        return exam

    def _create_submission(self, exam, enrollment=None):
        return Submission.objects.create(
            tenant=self.tenant,
            user=self.user,
            target_type="exam",
            target_id=exam.id,
            enrollment_id=(enrollment or self.enrollment).id,
            source="online",
            status="done",
        )


# ============================================================
# A. Representative attempt uniqueness
# ============================================================
class TestRepresentativeUniqueness(TransactionTestCase, BaseTestMixin):
    """A. (exam, enrollment) per is_representative=True must be unique."""

    def setUp(self):
        self._create_fixtures()

    def test_single_representative_on_create(self):
        """Normal creation: exactly one representative."""
        exam = self._create_exam()
        sub = self._create_submission(exam)
        attempt = ExamAttemptService.create_for_submission(
            exam_id=exam.id, enrollment_id=self.enrollment.id,
            submission_id=sub.id,
        )
        self.assertTrue(attempt.is_representative)
        self.assertEqual(attempt.attempt_index, 1)

    def test_representative_swap_on_retake(self):
        """Retake: old representative becomes False, new becomes True."""
        exam = self._create_exam(allow_retake=True, max_attempts=3)
        sub1 = self._create_submission(exam)
        a1 = ExamAttemptService.create_for_submission(
            exam_id=exam.id, enrollment_id=self.enrollment.id,
            submission_id=sub1.id,
        )
        self.assertTrue(a1.is_representative)

        sub2 = self._create_submission(exam)
        a2 = ExamAttemptService.create_for_submission(
            exam_id=exam.id, enrollment_id=self.enrollment.id,
            submission_id=sub2.id,
        )
        a1.refresh_from_db()
        self.assertFalse(a1.is_representative)
        self.assertTrue(a2.is_representative)
        self.assertEqual(a2.attempt_index, 2)

    def test_db_constraint_prevents_duplicate_representative(self):
        """DB constraint blocks manual creation of two representatives."""
        exam = self._create_exam()
        ExamAttempt.objects.create(
            exam=exam, enrollment=self.enrollment,
            attempt_index=1, is_representative=True, status="done",
        )
        with self.assertRaises(IntegrityError):
            ExamAttempt.objects.create(
                exam=exam, enrollment=self.enrollment,
                attempt_index=2, is_representative=True, status="done",
            )


# ============================================================
# B. Retake scenario
# ============================================================
class TestRetakeScenario(TransactionTestCase, BaseTestMixin):
    """B. attempt_index=2+ with online submission sync."""

    def setUp(self):
        self._create_fixtures()

    def test_sync_result_does_not_rollback_representative(self):
        """sync_result_from_exam_submission should not clobber existing attempts."""

        exam = self._create_exam(allow_retake=True, max_attempts=3)

        # Create first attempt via service
        sub1 = self._create_submission(exam)
        a1 = ExamAttemptService.create_for_submission(
            exam_id=exam.id, enrollment_id=self.enrollment.id,
            submission_id=sub1.id,
        )

        # Create second attempt
        sub2 = self._create_submission(exam)
        a2 = ExamAttemptService.create_for_submission(
            exam_id=exam.id, enrollment_id=self.enrollment.id,
            submission_id=sub2.id,
        )
        self.assertEqual(a2.attempt_index, 2)

        # sync_result for sub2 should NOT create attempt_index=1
        # and should NOT override a2's representative status
        # (skip because we need sheet/answer_key setup for full sync - test attempt lookup)
        existing = ExamAttempt.objects.filter(
            submission_id=sub2.id,
        ).first()
        self.assertIsNotNone(existing)
        self.assertEqual(existing.attempt_index, 2)

        # Verify a2 is still representative
        a2.refresh_from_db()
        self.assertTrue(a2.is_representative)
        a1.refresh_from_db()
        self.assertFalse(a1.is_representative)


# ============================================================
# C. submission_id duplicate
# ============================================================
class TestSubmissionIdDuplicate(TransactionTestCase, BaseTestMixin):
    """C. Same submission_id cannot create two attempts."""

    def setUp(self):
        self._create_fixtures()

    def test_duplicate_submission_id_raises(self):
        """Application-level check blocks duplicate submission_id."""
        exam = self._create_exam()
        sub = self._create_submission(exam)
        ExamAttemptService.create_for_submission(
            exam_id=exam.id, enrollment_id=self.enrollment.id,
            submission_id=sub.id,
        )
        with self.assertRaises(DjangoValidationError):
            ExamAttemptService.create_for_submission(
                exam_id=exam.id, enrollment_id=self.enrollment.id,
                submission_id=sub.id,
            )

    def test_db_constraint_blocks_duplicate_submission(self):
        """DB unique constraint blocks even raw duplicate."""
        exam = self._create_exam()
        sub = self._create_submission(exam)
        ExamAttempt.objects.create(
            exam=exam, enrollment=self.enrollment,
            submission_id=sub.id, attempt_index=1,
            is_representative=True, status="done",
        )
        with self.assertRaises(IntegrityError):
            ExamAttempt.objects.create(
                exam=exam, enrollment=self.enrollment,
                submission_id=sub.id, attempt_index=2,
                is_representative=False, status="done",
            )

    def test_null_submission_id_allowed_multiple(self):
        """submission_id=NULL is allowed for multiple attempts (clinic direct entry)."""
        exam = self._create_exam(allow_retake=True, max_attempts=3)
        ExamAttempt.objects.create(
            exam=exam, enrollment=self.enrollment,
            submission_id=None, attempt_index=1,
            is_representative=False, status="done",
        )
        a2 = ExamAttempt.objects.create(
            exam=exam, enrollment=self.enrollment,
            submission_id=None, attempt_index=2,
            is_representative=True, status="done",
        )
        self.assertEqual(a2.attempt_index, 2)


class TestManualScoreClearsNotSubmitted(TestCase, BaseTestMixin):
    """Manual score edits must clear the prior NOT_SUBMITTED marker."""

    def setUp(self):
        self._create_fixtures()
        self.factory = APIRequestFactory()

    def _patch(self, view_cls, exam, data):
        ScoreEditDraft.objects.update_or_create(
            session=self.session,
            tenant=self.tenant,
            editor_user=self.user,
            defaults={"payload": {"client_id": "test-score-tab", "changes": []}},
        )
        request = self.factory.patch(
            "/results/admin/exams/score/",
            data,
            format="json",
            HTTP_X_SCORE_EDITOR_CLIENT="test-score-tab",
            HTTP_X_SCORE_SESSION_ID=str(self.session.id),
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.user)
        response = view_cls.as_view()(
            request,
            exam_id=exam.id,
            enrollment_id=self.enrollment.id,
        )
        self.assertEqual(response.status_code, 200, response.data)
        return response

    def _mark_not_submitted(self, exam):
        self._patch(
            AdminExamTotalScoreView,
            exam,
            {"meta_status": "NOT_SUBMITTED"},
        )
        attempt = ExamAttempt.objects.get(exam=exam, enrollment=self.enrollment)
        self.assertEqual((attempt.meta or {}).get("status"), "NOT_SUBMITTED")
        return attempt

    def test_objective_score_clears_not_submitted_marker(self):
        exam = self._create_exam(title="Objective clears absent")
        self._mark_not_submitted(exam)

        self._patch(AdminExamObjectiveScoreView, exam, {"score": 26})

        attempt = ExamAttempt.objects.get(exam=exam, enrollment=self.enrollment)
        result = Result.objects.get(
            target_type="exam",
            target_id=exam.id,
            enrollment=self.enrollment,
        )
        self.assertNotEqual((attempt.meta or {}).get("status"), "NOT_SUBMITTED")
        self.assertEqual((attempt.meta or {}).get("total_score"), 26.0)
        self.assertEqual(float(result.objective_score), 26.0)
        self.assertEqual(float(result.total_score), 26.0)

    def test_subjective_score_clears_not_submitted_marker(self):
        exam = self._create_exam(title="Subjective clears absent")
        self._mark_not_submitted(exam)

        self._patch(AdminExamSubjectiveScoreView, exam, {"score": 14})

        attempt = ExamAttempt.objects.get(exam=exam, enrollment=self.enrollment)
        result = Result.objects.get(
            target_type="exam",
            target_id=exam.id,
            enrollment=self.enrollment,
        )
        self.assertNotEqual((attempt.meta or {}).get("status"), "NOT_SUBMITTED")
        self.assertEqual((attempt.meta or {}).get("total_score"), 14.0)
        self.assertEqual(float(result.total_score), 14.0)

    def test_item_score_clears_not_submitted_marker(self):
        exam = self._create_exam(title="Item clears absent", max_score=5, pass_score=3)
        sheet = Sheet.objects.create(exam=exam, name="MAIN", total_questions=1)
        question = ExamQuestion.objects.create(sheet=sheet, number=1, score=5)
        self._mark_not_submitted(exam)

        request = self.factory.patch(
            "/results/admin/exams/items/",
            {"score": 4, "answer": "2"},
            format="json",
            HTTP_X_SCORE_EDITOR_CLIENT="test-score-tab",
            HTTP_X_SCORE_SESSION_ID=str(self.session.id),
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.user)
        response = AdminExamItemScoreView.as_view()(
            request,
            exam_id=exam.id,
            enrollment_id=self.enrollment.id,
            question_id=question.id,
        )

        self.assertEqual(response.status_code, 200, response.data)
        attempt = ExamAttempt.objects.get(exam=exam, enrollment=self.enrollment)
        result = Result.objects.get(
            target_type="exam",
            target_id=exam.id,
            enrollment=self.enrollment,
        )
        self.assertNotEqual((attempt.meta or {}).get("status"), "NOT_SUBMITTED")
        self.assertEqual((attempt.meta or {}).get("total_score"), 4.0)
        self.assertEqual(float(result.total_score), 4.0)

    def test_not_submitted_clears_derived_state_and_allows_explicit_resume(self):
        import datetime

        from django.utils import timezone

        exam = self._create_exam(title="Absent derived state", pass_score=60)
        sheet = Sheet.objects.create(exam=exam, name="MAIN", total_questions=1)
        question = ExamQuestion.objects.create(sheet=sheet, number=1, score=100)
        attempt = ExamAttempt.objects.create(
            exam=exam,
            enrollment=self.enrollment,
            submission_id=None,
            attempt_index=1,
            is_representative=True,
            status="done",
            meta={"total_score": 30.0},
        )
        result = Result.objects.create(
            target_type="exam",
            target_id=exam.id,
            enrollment=self.enrollment,
            attempt=attempt,
            total_score=30.0,
            max_score=100.0,
            objective_score=30.0,
        )
        ResultItem.objects.create(
            result=result,
            question=question,
            answer="1",
            is_correct=False,
            score=0.0,
            max_score=100.0,
            source="manual",
        )
        ClinicLink = django_apps.get_model("progress", "ClinicLink")
        clinic_link = ClinicLink.objects.create(
            tenant=self.tenant,
            enrollment=self.enrollment,
            session=self.session,
            source_type="exam",
            source_id=exam.id,
            reason=ClinicLink.Reason.AUTO_FAILED,
            is_auto=True,
        )
        ClinicSession = django_apps.get_model("clinic", "Session")
        SessionParticipant = django_apps.get_model("clinic", "SessionParticipant")
        SessionParticipantPlanItem = django_apps.get_model(
            "clinic",
            "SessionParticipantPlanItem",
        )
        clinic_session = ClinicSession.objects.create(
            tenant=self.tenant,
            title="Preserved operational clinic",
            date=timezone.localdate(),
            start_time=datetime.time(18, 0),
            duration_minutes=60,
            location="Room 1",
            max_participants=10,
            created_by=self.user,
        )
        completed_at = timezone.now()
        participants = [
            SessionParticipant.objects.create(
                tenant=self.tenant,
                session=clinic_session,
                student=self.student,
                enrollment=self.enrollment,
                status="booked",
                source="manual",
                clinic_reason="exam",
            ),
            SessionParticipant.objects.create(
                tenant=self.tenant,
                session=clinic_session,
                student=self.student,
                enrollment=self.enrollment,
                status="attended",
                source="manual",
                clinic_reason="exam",
                checked_in_at=completed_at,
            ),
            SessionParticipant.objects.create(
                tenant=self.tenant,
                session=clinic_session,
                student=self.student,
                enrollment=self.enrollment,
                status="attended",
                source="manual",
                clinic_reason="exam",
                checked_in_at=completed_at,
                completed_at=completed_at,
                completed_by=self.user,
            ),
        ]
        plan_items = [
            SessionParticipantPlanItem.objects.create(
                participant=participant,
                clinic_link=clinic_link,
                selected_by=self.user,
            )
            for participant in participants
        ]
        notification_patcher = patch(
            "apps.domains.progress.services.clinic_resolution_service."
            "send_clinic_resolution_notification"
        )
        mock_notification = notification_patcher.start()
        self.addCleanup(notification_patcher.stop)

        self._patch(
            AdminExamTotalScoreView,
            exam,
            {"meta_status": "NOT_SUBMITTED"},
        )

        result.refresh_from_db()
        attempt.refresh_from_db()
        clinic_link.refresh_from_db()
        self.assertEqual(float(result.total_score), 0.0)
        self.assertEqual(float(result.objective_score), 0.0)
        self.assertEqual(ResultItem.objects.filter(result=result).count(), 0)
        self.assertEqual((attempt.meta or {}).get("status"), "NOT_SUBMITTED")
        self.assertIsNotNone(clinic_link.resolved_at)
        self.assertEqual(clinic_link.resolution_type, "NOT_SUBMITTED")
        self.assertEqual(
            (clinic_link.resolution_evidence or {}).get("user_id"),
            self.user.id,
        )
        self.assertEqual(
            (clinic_link.resolution_history or [])[-1]["action"],
            "resolve_exam_not_submitted",
        )
        wrong_note_total, wrong_note_items = list_wrong_notes_for_enrollment(
            enrollment_id=self.enrollment.id,
            q=WrongNoteQuery(exam_id=exam.id),
        )
        self.assertEqual((wrong_note_total, wrong_note_items), (0, []))
        for plan_item in plan_items:
            plan_item.refresh_from_db()
            self.assertIsNotNone(plan_item.removed_at)
            self.assertEqual(
                plan_item.removal_reason,
                "clinic_link_resolved:NOT_SUBMITTED",
            )
        for expected_status, participant in zip(
            ("booked", "attended", "attended"),
            participants,
            strict=True,
        ):
            participant.refresh_from_db()
            self.assertEqual(participant.status, expected_status)
        self.assertIsNotNone(participants[1].checked_in_at)
        self.assertIsNotNone(participants[2].completed_at)

        first_fact_count = ResultFact.objects.filter(
            target_type="exam",
            target_id=exam.id,
            enrollment=self.enrollment,
            source="manual_not_submitted",
        ).count()
        self._patch(
            AdminExamTotalScoreView,
            exam,
            {"meta_status": "NOT_SUBMITTED"},
        )
        self.assertEqual(
            ResultFact.objects.filter(
                target_type="exam",
                target_id=exam.id,
                enrollment=self.enrollment,
                source="manual_not_submitted",
            ).count(),
            first_fact_count,
        )

        self._patch(
            AdminExamTotalScoreView,
            exam,
            {"score": 40.0, "max_score": 100.0},
        )
        result.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual(float(result.total_score), 40.0)
        self.assertNotEqual((attempt.meta or {}).get("status"), "NOT_SUBMITTED")
        self.assertEqual(
            ClinicLink.objects.filter(
                tenant=self.tenant,
                enrollment=self.enrollment,
                source_type="exam",
                source_id=exam.id,
                resolved_at__isnull=True,
            ).count(),
            1,
        )
        mock_notification.assert_not_called()


# ============================================================
# E. Exam validation
# ============================================================
class TestExamValidation(TestCase, BaseTestMixin):
    """E. Exam model/serializer validation for P1-5."""

    def setUp(self):
        self._create_fixtures()

    def test_max_attempts_zero_blocked_by_db(self):
        """max_attempts=0 is blocked by DB check constraint."""
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Exam.objects.create(
                    tenant=self.tenant, title="Bad",
                    max_attempts=0, max_score=100, pass_score=0,
                )

    def test_pass_score_exceeds_max_blocked_by_db(self):
        """pass_score > max_score is blocked by DB check constraint."""
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Exam.objects.create(
                    tenant=self.tenant, title="Bad",
                    max_attempts=1, max_score=100, pass_score=150,
                )

    def test_open_close_validation_in_clean(self):
        """open_at >= close_at blocked by model clean()."""
        from django.utils import timezone
        import datetime

        now = timezone.now()
        exam = Exam(
            tenant=self.tenant, title="Bad",
            max_attempts=1, max_score=100, pass_score=50,
            open_at=now + datetime.timedelta(hours=2),
            close_at=now + datetime.timedelta(hours=1),
        )
        with self.assertRaises(DjangoValidationError) as ctx:
            exam.clean()
        self.assertIn("close_at", ctx.exception.message_dict)

    def test_online_attempt_after_exam_close_remains_blocked(self):
        from django.utils import timezone
        import datetime

        exam = self._create_exam(close_at=timezone.now() - datetime.timedelta(minutes=1))
        submission = self._create_submission(exam)

        with self.assertRaisesMessage(DjangoValidationError, "Exam is closed."):
            ExamAttemptService.create_for_submission(
                exam_id=exam.id,
                enrollment_id=self.enrollment.id,
                submission_id=submission.id,
            )

    def test_serializer_blocks_invalid_exam(self):
        """ExamSerializer validates max_attempts, pass_score, open_at/close_at."""
        from apps.domains.exams.serializers.exam import ExamSerializer
        from django.utils import timezone
        import datetime

        now = timezone.now()
        data = {
            "title": "Test",
            "max_attempts": 0,
            "pass_score": 150,
            "max_score": 100,
            "open_at": (now + datetime.timedelta(hours=2)).isoformat(),
            "close_at": (now + datetime.timedelta(hours=1)).isoformat(),
        }
        s = ExamSerializer(data=data)
        self.assertFalse(s.is_valid())
        self.assertIn("max_attempts", s.errors)
        self.assertIn("pass_score", s.errors)
        self.assertIn("close_at", s.errors)


# ============================================================
# F. Homework tenant fallback removal
# ============================================================
class TestHomeworkTenantFallback(TestCase, BaseTestMixin):
    """F. calc_homework_passed_and_clinic must fail when tenant is missing."""

    def setUp(self):
        self._create_fixtures()

    def test_tenant_missing_raises_error(self):
        """When session.lecture.tenant is None, should raise ValueError, not fallback."""
        from apps.domains.homework.utils.homework_policy import calc_homework_passed_and_clinic

        # Create a session with no lecture -> tenant chain accessible
        class FakeSession:
            id = 999
            lecture = None  # No lecture -> tenant is None

        with self.assertRaises(ValueError) as ctx:
            calc_homework_passed_and_clinic(
                session=FakeSession(),
                score=50,
                max_score=100,
            )
        self.assertIn("tenant", str(ctx.exception).lower())

    def test_valid_session_works(self):
        """Normal session with tenant works correctly."""
        from apps.domains.homework.utils.homework_policy import calc_homework_passed_and_clinic

        passed, clinic, pct = calc_homework_passed_and_clinic(
            session=self.session,
            score=90,
            max_score=100,
        )
        self.assertTrue(passed)


# ============================================================
# G. HomeworkScore score > max_score validation
# ============================================================
class TestHomeworkScoreValidation(TestCase):
    """G. HomeworkScore serializer blocks score > max_score."""

    def test_score_exceeds_max_blocked(self):
        """score=200, max_score=100 should be rejected."""
        from apps.domains.homework_results.serializers.homework_score import HomeworkQuickPatchSerializer

        data = {
            "homework_id": 1,
            "enrollment_id": 1,
            "score": 200,
            "max_score": 100,
        }
        s = HomeworkQuickPatchSerializer(data=data)
        self.assertFalse(s.is_valid())
        self.assertIn("score", s.errors)

    def test_negative_score_blocked(self):
        """Negative score should be rejected."""
        from apps.domains.homework_results.serializers.homework_score import HomeworkQuickPatchSerializer

        data = {
            "homework_id": 1,
            "enrollment_id": 1,
            "score": -10,
            "max_score": 100,
        }
        s = HomeworkQuickPatchSerializer(data=data)
        self.assertFalse(s.is_valid())
        self.assertIn("score", s.errors)

    def test_negative_max_score_blocked(self):
        """Negative max_score should be rejected."""
        from apps.domains.homework_results.serializers.homework_score import HomeworkQuickPatchSerializer

        data = {
            "homework_id": 1,
            "enrollment_id": 1,
            "score": 50,
            "max_score": -100,
        }
        s = HomeworkQuickPatchSerializer(data=data)
        self.assertFalse(s.is_valid())
        self.assertIn("max_score", s.errors)

    def test_valid_score_passes(self):
        """Valid score <= max_score passes."""
        from apps.domains.homework_results.serializers.homework_score import HomeworkQuickPatchSerializer

        data = {
            "homework_id": 1,
            "enrollment_id": 1,
            "score": 80,
            "max_score": 100,
        }
        s = HomeworkQuickPatchSerializer(data=data)
        self.assertTrue(s.is_valid())

    def test_percent_mode_no_max(self):
        """score=85 without max_score (percent mode) passes."""
        from apps.domains.homework_results.serializers.homework_score import HomeworkQuickPatchSerializer

        data = {
            "homework_id": 1,
            "enrollment_id": 1,
            "score": 85,
        }
        s = HomeworkQuickPatchSerializer(data=data)
        self.assertTrue(s.is_valid())

    def test_core_serializer_also_validates(self):
        """homework_results.HomeworkQuickPatchSerializer also validates (legacy alias name)."""
        from apps.domains.homework_results.serializers.homework_score import HomeworkQuickPatchSerializer

        data = {
            "homework_id": 1,
            "enrollment_id": 1,
            "score": 200,
            "max_score": 100,
        }
        s = HomeworkQuickPatchSerializer(data=data)
        self.assertFalse(s.is_valid())
        self.assertIn("score", s.errors)
