"""Teacher homework decisions must reach progress through the public API."""

from unittest.mock import patch

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.core.models import Tenant, TenantMembership
from apps.domains.progress.models import (
    AssessmentCorrection,
    ClinicLink,
    LectureProgress,
    ProgressPolicy,
    SessionProgress,
)
from apps.domains.progress.services.progress_pipeline import ProgressPipelineService
from apps.domains.progress import dispatcher as progress_dispatcher


Enrollment = django_apps.get_model("enrollment", "Enrollment")
SessionEnrollment = django_apps.get_model("enrollment", "SessionEnrollment")
HomeworkAssignment = django_apps.get_model("homework", "HomeworkAssignment")
Homework = django_apps.get_model("homework_results", "Homework")
HomeworkScore = django_apps.get_model("homework_results", "HomeworkScore")
Lecture = django_apps.get_model("lectures", "Lecture")
Session = django_apps.get_model("lectures", "Session")
ScoreEditDraft = django_apps.get_model("results", "ScoreEditDraft")
Student = django_apps.get_model("students", "Student")
Submission = django_apps.get_model("submissions", "Submission")


class HomeworkApprovalProgressTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Homework approval", code="hw-approval")
        self.teacher = get_user_model().objects.create_user(
            username="hw-approval-teacher", password="test-only", tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=self.teacher, role="teacher")
        self.lecture = Lecture.objects.create(tenant=self.tenant, title="Lecture", name="Lecture")
        self.session = Session.objects.create(lecture=self.lecture, title="Session", order=1)
        self.enrollment = self._enrollment("target")
        self.homework = self._homework("First", self.enrollment)
        self.score = HomeworkScore.objects.create(
            session=self.session, homework=self.homework, enrollment=self.enrollment,
            score=100, max_score=100, passed=True, teacher_approved=False,
        )
        self.policy = ProgressPolicy.objects.create(
            lecture=self.lecture, homework_start_session_order=1,
            homework_pass_type=ProgressPolicy.HomeworkPassType.TEACHER_APPROVAL,
        )
        SessionProgress.objects.create(
            enrollment=self.enrollment, session=self.session, attendance_type="offline",
            video_progress_rate=93, homework_submitted=False,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.teacher)
        self.headers = {"HTTP_HOST": "localhost", "HTTP_X_TENANT_CODE": self.tenant.code}
        self.notice = patch(
            "apps.domains.progress.services.clinic_resolution_service._send_resolution_notification"
        )
        self.notice.start()
        self.addCleanup(self.notice.stop)
        self._recompute()

    def _enrollment(self, suffix):
        user = get_user_model().objects.create_user(
            username=f"hw-approval-{suffix}", password="test-only", tenant=self.tenant,
        )
        student = Student.objects.create(
            tenant=self.tenant, user=user, name=suffix, ps_number=f"HW-{suffix}",
            omr_code=f"{user.pk:08d}"[-8:],
        )
        enrollment = Enrollment.objects.create(
            tenant=self.tenant, lecture=self.lecture, student=student, status="ACTIVE",
        )
        SessionEnrollment.objects.create(
            tenant=self.tenant, session=self.session, enrollment=enrollment,
        )
        return enrollment

    def _homework(self, title, enrollment=None, *, removed=False):
        homework = Homework.objects.create(
            tenant=self.tenant, session=self.session, title=title,
            meta={"removed_from_session_at": "2026-09-01T00:00:00Z"} if removed else None,
        )
        if enrollment is not None:
            HomeworkAssignment.objects.create(
                tenant=self.tenant, session=self.session, homework=homework, enrollment=enrollment,
            )
        return homework

    def _recompute(self):
        ProgressPipelineService().apply(enrollment_id=self.enrollment.id, session_id=self.session.id)
        return SessionProgress.objects.get(enrollment=self.enrollment, session=self.session)

    def _complete(self, completed, *, homework=None, expected=200):
        homework = homework or self.homework
        correction = AssessmentCorrection.objects.filter(
            tenant=self.tenant, enrollment=self.enrollment, session=self.session,
            source_type="homework", source_id=homework.id,
        ).first()
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.patch(
                f"/api/v1/results/admin/sessions/{self.session.id}/score-correction/",
                {"enrollment_id": self.enrollment.id, "source_type": "homework",
                 "source_id": homework.id, "completed": completed,
                 "note": "종이 과제 검사 완료" if completed else "추가 검사 필요",
                 "expected_updated_at": correction.updated_at.isoformat() if correction else None},
                format="json", **self.headers,
            )
        self.assertEqual(response.status_code, expected, response.data)
        return response

    def _progress(self):
        return SessionProgress.objects.get(enrollment=self.enrollment, session=self.session)

    def _summary(self):
        response = self.client.get(
            f"/api/v1/results/admin/sessions/{self.session.id}/score-summary/", **self.headers,
        )
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def _set_assignments(self, homework, enrollment_ids):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.put(
                f"/api/v1/homework/assignments/?homework_id={homework.id}",
                {"enrollment_ids": enrollment_ids}, format="json", **self.headers,
            )
        self.assertEqual(response.status_code, 200, response.data)
        reloaded = self.client.get(
            f"/api/v1/homework/assignments/?homework_id={homework.id}", **self.headers,
        )
        self.assertEqual(reloaded.status_code, 200, reloaded.data)
        self.assertEqual(
            {row["enrollment_id"] for row in reloaded.data["items"] if row["is_selected"]},
            set(enrollment_ids),
        )

    def _assert_completion_readback(self, expected):
        response = self.client.get(
            f"/api/v1/results/admin/sessions/{self.session.id}/scores/", **self.headers,
        )
        self.assertEqual(response.status_code, 200, response.data)
        row = next(row for row in response.data["rows"] if row["enrollment_id"] == self.enrollment.id)
        self.assertEqual(row["progress_completed"], expected)
        self.assertEqual(self._progress().homework_passed, expected)
        self.assertEqual(self._summary()["pass_rate"], int(expected))
        self.assertEqual(
            LectureProgress.objects.get(enrollment=self.enrollment).completed_sessions,
            int(expected),
        )
        return row

    def test_assignment_put_addition_invalidates_previous_completion_on_reload(self):
        second = self._homework("New assignment")
        self._complete(True)
        self._assert_completion_readback(True)
        score_before = HomeworkScore.objects.values().get(pk=self.score.pk)
        correction_before = AssessmentCorrection.objects.values().get(source_id=self.homework.id)
        self._set_assignments(second, [self.enrollment.id])
        row = self._assert_completion_readback(False)
        self.assertEqual({item["homework_id"] for item in row["homeworks"]}, {self.homework.id, second.id})
        self.assertEqual(HomeworkScore.objects.values().get(pk=self.score.pk), score_before)
        self.assertEqual(AssessmentCorrection.objects.values().get(source_id=self.homework.id), correction_before)
        self.assertFalse(Submission.objects.exists())
        self.assertEqual(self._progress().attendance_type, "offline")
        self.assertEqual(self._progress().video_progress_rate, 93)

    def test_assignment_put_removal_without_clinic_link_refreshes_completion_on_reload(self):
        second = self._homework("Pending assignment", self.enrollment)
        self._complete(True)
        self._assert_completion_readback(False)
        self.assertFalse(ClinicLink.objects.filter(source_type="homework", source_id=second.id).exists())
        self._set_assignments(second, [])
        row = self._assert_completion_readback(True)
        self.assertEqual([item["homework_id"] for item in row["homeworks"]], [self.homework.id])
        self.assertTrue(Homework.objects.filter(pk=second.pk).exists())
        self.assertFalse(Submission.objects.exists())

    def test_delete_homework_without_clinic_link_refreshes_completion_and_preserves_history(self):
        second = self._homework("Removed pending assignment", self.enrollment)
        second_score = HomeworkScore.objects.create(
            session=self.session, homework=second, enrollment=self.enrollment,
            score=30, max_score=100, teacher_approved=False,
        )
        submission = Submission.objects.create(
            tenant=self.tenant, user=self.enrollment.student.user, enrollment=self.enrollment,
            target_type="homework", target_id=second.id, source="homework_image", status="submitted",
        )
        score_before = HomeworkScore.objects.values().get(pk=second_score.pk)
        submission_before = Submission.objects.values().get(pk=submission.pk)
        self._complete(True)
        self._assert_completion_readback(False)
        self.assertFalse(ClinicLink.objects.filter(source_type="homework", source_id=second.id).exists())
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.delete(f"/api/v1/homeworks/{second.id}/", **self.headers)
        self.assertEqual(response.status_code, 204, response.data)
        self._assert_completion_readback(True)
        self.assertEqual(HomeworkScore.objects.values().get(pk=second_score.pk), score_before)
        self.assertEqual(Submission.objects.values().get(pk=submission.pk), submission_before)
        second.refresh_from_db()
        self.assertIn("removed_from_session_at", second.meta)

    def test_unassigned_legacy_score_does_not_block_current_visible_completion(self):
        hidden = self._homework("Historical unassigned")
        HomeworkScore.objects.create(
            session=self.session, homework=hidden, enrollment=self.enrollment,
            score=30, max_score=100, teacher_approved=False,
        )
        row = self._assert_completion_readback(False)
        self.assertNotIn(hidden.id, [item["homework_id"] for item in row["homeworks"]])
        self._complete(True, homework=hidden, expected=400)
        self._complete(True)
        self._assert_completion_readback(True)

    def test_legacy_any_approval_without_current_assignments_remains_supported(self):
        HomeworkAssignment.objects.filter(homework=self.homework).delete()
        other_legacy = self._homework("Other legacy")
        HomeworkScore.objects.create(
            session=self.session, homework=other_legacy, enrollment=self.enrollment,
            score=20, max_score=100, teacher_approved=False,
        )
        self.score.teacher_approved = True
        self.score.save(update_fields=["teacher_approved"])
        self.assertTrue(self._recompute().homework_passed)

    def test_target_edits_dispatch_once_even_with_existing_clinic_links(self):
        second = self._homework("Target edits")
        self._complete(True)
        with patch.object(
            progress_dispatcher, "dispatch_progress_pipeline",
            wraps=progress_dispatcher.dispatch_progress_pipeline,
        ) as dispatch:
            self._set_assignments(second, [self.enrollment.id])
            dispatch.assert_called_once_with(enrollment_id=self.enrollment.id, session_id=self.session.id)
            self._assert_completion_readback(False)
            dispatch.reset_mock()
            ClinicLink.objects.create(
                tenant=self.tenant, enrollment=self.enrollment, session=self.session,
                source_type="homework", source_id=second.id, cycle_no=1,
                reason=ClinicLink.Reason.AUTO_FAILED,
            )
            self._set_assignments(second, [])
            dispatch.assert_called_once_with(enrollment_id=self.enrollment.id, session_id=self.session.id)
            self._assert_completion_readback(True)
            self._set_assignments(second, [self.enrollment.id])
            dispatch.reset_mock()
            ClinicLink.objects.create(
                tenant=self.tenant, enrollment=self.enrollment, session=self.session,
                source_type="homework", source_id=second.id, cycle_no=2,
                reason=ClinicLink.Reason.AUTO_FAILED,
            )
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.delete(f"/api/v1/homeworks/{second.id}/", **self.headers)
            self.assertEqual(response.status_code, 204, response.data)
            dispatch.assert_called_once_with(enrollment_id=self.enrollment.id, session_id=self.session.id)
            self._assert_completion_readback(True)

    def test_target_edit_rollback_emits_no_progress_and_preserves_assignment_state(self):
        for operation in ("add", "remove", "delete"):
            with self.subTest(operation=operation):
                assigned = operation != "add"
                homework = self._homework(f"Rollback {operation}", self.enrollment if assigned else None)
                self._complete(True)
                progress_before = SessionProgress.objects.values().get(pk=self._progress().pk)
                with patch.object(progress_dispatcher, "dispatch_progress_pipeline") as dispatch:
                    with self.captureOnCommitCallbacks(execute=True):
                        with self.assertRaisesMessage(RuntimeError, "rollback target edit"):
                            with transaction.atomic():
                                if operation == "delete":
                                    response = self.client.delete(f"/api/v1/homeworks/{homework.id}/", **self.headers)
                                    self.assertEqual(response.status_code, 204, response.data)
                                else:
                                    response = self.client.put(
                                        f"/api/v1/homework/assignments/?homework_id={homework.id}",
                                        {"enrollment_ids": [self.enrollment.id] if assigned is False else []},
                                        format="json", **self.headers,
                                    )
                                    self.assertEqual(response.status_code, 200, response.data)
                                raise RuntimeError("rollback target edit")
                    dispatch.assert_not_called()
                self.assertEqual(HomeworkAssignment.objects.filter(homework=homework).exists(), assigned)
                homework.refresh_from_db()
                self.assertNotIn("removed_from_session_at", homework.meta or {})
                self.assertEqual(SessionProgress.objects.values().get(pk=self._progress().pk), progress_before)

    def test_assignment_addition_preserves_hidden_inactive_students(self):
        inactive = self._enrollment("inactive")
        inactive.status = "INACTIVE"
        inactive.save(update_fields=["status"])
        second = self._homework("Inactive history", inactive)
        original = HomeworkAssignment.objects.values().get(homework=second, enrollment=inactive)
        self._complete(True)
        with patch.object(
            progress_dispatcher, "dispatch_progress_pipeline",
            wraps=progress_dispatcher.dispatch_progress_pipeline,
        ) as dispatch:
            self._set_assignments(second, [self.enrollment.id])
            dispatch.assert_called_once_with(enrollment_id=self.enrollment.id, session_id=self.session.id)
        self.assertEqual(HomeworkAssignment.objects.values().get(homework=second, enrollment=inactive), original)
        self.assertFalse(SessionProgress.objects.filter(enrollment=inactive).exists())
        self._assert_completion_readback(False)

    def test_completion_updates_progress_readback_and_aggregates_without_submission(self):
        self.assertFalse(self._progress().homework_passed)
        original = HomeworkScore.objects.values().get(pk=self.score.pk)
        self._complete(True)
        progress = self._progress()
        self.assertTrue(progress.homework_passed)
        self.assertTrue(progress.completed)
        self.assertFalse(progress.homework_submitted)
        self.assertEqual(progress.attendance_type, "offline")
        self.assertEqual(progress.video_progress_rate, 93)
        self.assertEqual(HomeworkScore.objects.values().get(pk=self.score.pk), original)
        self.assertFalse(Submission.objects.exists())
        self.assertEqual(LectureProgress.objects.get(enrollment=self.enrollment).completed_sessions, 1)
        self.assertEqual(self._summary()["pass_rate"], 1)
        response = self.client.get(
            f"/api/v1/results/admin/sessions/{self.session.id}/scores/", **self.headers,
        )
        self.assertEqual(response.status_code, 200, response.data)
        row = next(row for row in response.data["rows"] if row["enrollment_id"] == self.enrollment.id)
        self.assertTrue(row["progress_completed"])
        self.assertEqual(row["homeworks"][0]["block"]["correction_status"], "COMPLETED")
        self.assertTrue(self._recompute().homework_passed)

    def test_low_score_completion_remains_independent_of_later_score_entry(self):
        self.score.score = 40
        self.score.passed = False
        self.score.save(update_fields=["score", "passed", "updated_at"])
        self._complete(True)
        self.assertTrue(self._progress().homework_passed)
        ScoreEditDraft.objects.create(
            tenant=self.tenant, session=self.session, editor_user=self.teacher,
            payload={"client_id": "approval-test", "changes": []},
        )
        response = self.client.patch(
            "/api/v1/homework/scores/quick/",
            {"session_id": self.session.id, "enrollment_id": self.enrollment.id,
             "homework_id": self.homework.id, "score": 20},
            format="json", HTTP_X_SCORE_EDITOR_CLIENT="approval-test",
            HTTP_X_SCORE_SESSION_ID=str(self.session.id), **self.headers,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.score.refresh_from_db()
        self.assertEqual(self.score.score, 20)
        self.assertFalse(self.score.teacher_approved)
        self.assertTrue(self._recompute().homework_passed)
        self.assertTrue(AssessmentCorrection.objects.get(source_id=self.homework.id).completed)

    def test_no_score_completion_creates_neither_score_nor_submission(self):
        self.score.delete()
        self._complete(True)
        self.assertTrue(self._progress().homework_passed)
        self.assertFalse(self._progress().homework_submitted)
        self.assertFalse(HomeworkScore.objects.exists())
        self.assertFalse(Submission.objects.exists())

    def test_explicit_cancellation_overrides_legacy_approval_and_updates_aggregates(self):
        self.score.teacher_approved = True
        self.score.save(update_fields=["teacher_approved", "updated_at"])
        self._complete(True)
        completed_at = self._progress().completed_at
        self._complete(False)
        self.assertFalse(self._progress().homework_passed)
        self.assertFalse(self._progress().completed)
        self.assertEqual(self._progress().completed_at, completed_at)
        self.assertEqual(LectureProgress.objects.get(enrollment=self.enrollment).completed_sessions, 0)
        self.assertEqual(self._summary()["pass_rate"], 0)
        self.assertFalse(self._recompute().homework_passed)
        self.score.refresh_from_db()
        self.assertTrue(self.score.teacher_approved)

    def test_all_current_assignments_need_approval(self):
        second = self._homework("Second", self.enrollment)
        self.score.teacher_approved = True
        self.score.save(update_fields=["teacher_approved"])
        self.assertFalse(self._recompute().homework_passed)
        self._complete(True)
        self.assertFalse(self._progress().homework_passed)
        self._complete(True, homework=second)
        self.assertTrue(self._progress().homework_passed)
        self._complete(False, homework=second)
        self.assertFalse(self._progress().homework_passed)

    def test_factual_resolution_preserves_link_but_still_dispatches_each_teacher_decision(self):
        link, _ = ClinicLink.objects.update_or_create(
            tenant=self.tenant, enrollment=self.enrollment, session=self.session,
            source_type="homework", source_id=self.homework.id, cycle_no=1,
            defaults={
                "reason": ClinicLink.Reason.AUTO_FAILED,
                "resolved_at": timezone.now(),
                "resolution_type": ClinicLink.ResolutionType.HOMEWORK_PASS,
                "resolution_evidence": {"score": 100},
            },
        )
        original = ClinicLink.objects.values().get(pk=link.pk)
        with patch(
            "apps.domains.progress.services.clinic_resolution_service._dispatch_progress_for_link"
        ) as dispatch:
            self._complete(True)
            dispatch.assert_called_once()
            self.assertEqual(dispatch.call_args.args[0].pk, link.pk)
            dispatch.reset_mock()
            self._complete(False)
            dispatch.assert_called_once()
            self.assertEqual(dispatch.call_args.args[0].pk, link.pk)
        self.assertEqual(ClinicLink.objects.values().get(pk=link.pk), original)

    def test_factual_resolution_does_not_leave_progress_stale_after_completion_or_cancellation(self):
        ClinicLink.objects.update_or_create(
            tenant=self.tenant, enrollment=self.enrollment, session=self.session,
            source_type="homework", source_id=self.homework.id, cycle_no=1,
            defaults={
                "reason": ClinicLink.Reason.AUTO_FAILED,
                "resolved_at": timezone.now(),
                "resolution_type": ClinicLink.ResolutionType.HOMEWORK_PASS,
                "resolution_evidence": {"score": 100},
            },
        )
        self._complete(True)
        self.assertTrue(self._progress().homework_passed)
        self._complete(False)
        self.assertFalse(self._progress().homework_passed)

    def test_other_students_and_removed_assignments_do_not_block_completion(self):
        sibling = self._enrollment("sibling")
        self._homework("Sibling only", sibling)
        self._homework("Removed", self.enrollment, removed=True)
        self._complete(True)
        self.assertTrue(self._progress().homework_passed)
        self.assertFalse(SessionProgress.objects.filter(enrollment=sibling).exists())

    def test_foreign_correction_and_assignment_cannot_satisfy_or_block_approval(self):
        other = Tenant.objects.create(name="Other", code="hw-approval-other")
        AssessmentCorrection.objects.create(
            tenant=other, enrollment=self.enrollment, session=self.session,
            source_type="homework", source_id=self.homework.id, completed=True,
        )
        self.assertFalse(self._recompute().homework_passed)
        foreign_homework = Homework.objects.create(tenant=other, session=self.session, title="Foreign")
        HomeworkAssignment.objects.create(
            tenant=other, enrollment=self.enrollment, session=self.session, homework=foreign_homework,
        )
        self._complete(True)
        self.assertTrue(self._progress().homework_passed)
        self._complete(True, homework=foreign_homework, expected=400)

    def test_student_cannot_record_teacher_completion(self):
        TenantMembership.ensure_active(tenant=self.tenant, user=self.enrollment.student.user, role="student")
        self.client.force_authenticate(self.enrollment.student.user)
        self._complete(True, expected=403)
        self.assertFalse(AssessmentCorrection.objects.exists())
        self.assertFalse(self._progress().homework_passed)

    def test_legacy_score_approval_without_assignment_remains_supported(self):
        HomeworkAssignment.objects.filter(homework=self.homework).delete()
        self.score.teacher_approved = True
        self.score.passed = False
        self.score.score = 40
        self.score.save(update_fields=["teacher_approved", "passed", "score", "updated_at"])
        self.assertTrue(self._recompute().homework_passed)

    def test_no_current_target_does_not_inherit_a_removed_legacy_approval(self):
        self.homework.meta = {"removed_from_session_at": "2026-09-01T00:00:00Z"}
        self.homework.save(update_fields=["meta"])
        self.score.teacher_approved = True
        self.score.save(update_fields=["teacher_approved"])
        self.assertFalse(self._recompute().homework_passed)

    def test_other_session_or_enrollment_approval_is_not_used(self):
        sibling = self._enrollment("sibling")
        other_session = Session.objects.create(lecture=self.lecture, title="Other", order=2)
        for enrollment, session in ((sibling, self.session), (self.enrollment, other_session)):
            AssessmentCorrection.objects.create(
                tenant=self.tenant, enrollment=enrollment, session=session,
                source_type="homework", source_id=self.homework.id, completed=True,
            )
        self.assertFalse(self._recompute().homework_passed)

    def test_other_policy_branches_and_online_attendance_remain_distinct(self):
        for policy_type, submitted, expected in (("SUBMIT", False, False), ("SUBMIT", True, True), ("SCORE", False, True)):
            with self.subTest(policy_type=policy_type, submitted=submitted):
                self.policy.homework_pass_type = policy_type
                self.policy.save(update_fields=["homework_pass_type"])
                SessionProgress.objects.filter(pk=self._progress().pk).update(homework_submitted=submitted)
                self.assertEqual(self._recompute().homework_passed, expected)
        self.policy.homework_pass_type = "TEACHER_APPROVAL"
        self.policy.save(update_fields=["homework_pass_type"])
        SessionProgress.objects.filter(pk=self._progress().pk).update(
            attendance_type="online", video_progress_rate=0, homework_submitted=False,
        )
        self._complete(True)
        progress = self._progress()
        self.assertTrue(progress.homework_passed)
        self.assertFalse(progress.video_completed)
        self.assertFalse(progress.homework_submitted)
        self.assertFalse(progress.completed)
