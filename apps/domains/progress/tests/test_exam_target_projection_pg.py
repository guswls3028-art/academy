from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.apps import apps
from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.domains.progress.services.session_calculator import SessionProgressCalculator
from apps.domains.progress.tests import test_shared_exam_progress_pipeline_pg as fixtures


class ExamTargetProjectionPostgresTests(TransactionTestCase):
    _lecture_session = fixtures.SharedExamProgressPipelinePostgresTests._lecture_session
    _enrollment = fixtures.SharedExamProgressPipelinePostgresTests._enrollment

    def setUp(self):
        fixtures.SharedExamProgressPipelinePostgresTests.setUp(self)
        for enrollment, session in self.points():
            SessionProgressCalculator.calculate(
                enrollment_id=enrollment.id,
                session=session,
                attendance_type="offline",
                homework_submitted=True,
            )

    def points(self):
        return [
            *((enrollment, self.session_a) for enrollment in self.enrollments_a),
            *((enrollment, self.session_b) for enrollment in self.enrollments_b),
        ]

    def protected(self):
        return {
            label: list(apps.get_model(label).objects.order_by("pk").values())
            for label in (
                "results.Result", "results.ResultFact", "results.ExamAttempt",
                "progress.ClinicLink", "progress.AssessmentCorrection",
                "progress.RiskLog", "messaging.NotificationLog",
                "messaging.ScheduledNotification", "core.PlatformPushOutbox",
            )
        }

    def projection_rows(self):
        return list(self.SessionProgress.objects.order_by("pk").values())

    def put_targets(self, exam, ids, *, session=None, client=None):
        return (client or self.client).put(
            f"/api/v1/exams/{exam.id}/enrollments/?session_id={(session or self.session_a).id}",
            {"enrollment_ids": ids}, format="json", **self.headers,
        )

    def create_exam(self, *, client=None):
        return (client or self.client).post(
            "/api/v1/exams/",
            {"title": "Synthetic new exam", "exam_type": "regular",
             "session_id": self.session_a.id, "pass_score": 60, "max_score": 100},
            format="json", **self.headers,
        )

    def missing_exam(self, enrollments):
        exam = self.Exam.objects.create(
            tenant=self.tenant, title="Synthetic missing-result exam",
            exam_type="regular", pass_score=60, max_score=100,
        )
        exam.sessions.add(self.session_a, self.session_b)
        self.ExamEnrollment.objects.bulk_create([
            self.ExamEnrollment(exam=exam, enrollment=enrollment)
            for enrollment in enrollments
        ])
        return exam

    def recalculate(self):
        for enrollment, session in self.points():
            SessionProgressCalculator.calculate(
                enrollment_id=enrollment.id, session=session,
                attendance_type="offline", homework_submitted=True,
            )

    def assert_canonical(self):
        for row in self.SessionProgress.objects.select_related("session__lecture"):
            policy = self.ProgressPolicy.objects.get(lecture_id=row.session.lecture_id)
            expected = SessionProgressCalculator._aggregate_exam_results(
                enrollment_id=row.enrollment_id, session=row.session, policy=policy,
            )
            self.assertEqual(
                (row.exam_attempted, row.exam_aggregate_score, row.exam_passed, row.exam_meta),
                expected, f"stale projection {row.id}",
            )
            self.assertEqual(row.completed, row.video_completed and row.exam_passed and row.homework_passed)

    def test_regular_creation_refreshes_missing_results_without_source_or_manual_writes(self):
        subject = self.enrollments_a[0]
        link = self.ClinicLink.objects.create(
            tenant=self.tenant, enrollment=subject, session=self.session_a,
            reason="MANUAL_REQUEST", source_type="exam", source_id=self.exam.id,
            resolved_at=timezone.now(), resolution_type="MANUAL_OVERRIDE",
            resolution_evidence={"manual": True}, memo="Keep teacher decision",
        )
        apps.get_model("progress.AssessmentCorrection").objects.create(
            tenant=self.tenant, enrollment=subject, session=self.session_a,
            source_type="exam", source_id=self.exam.id, completed=True,
            completed_at=timezone.now(), note="Keep manual review",
        )
        protected = self.protected()
        before = {row["id"]: row for row in self.projection_rows()}
        response = self.create_exam()
        self.assertEqual(response.status_code, 201, response.data)
        self.assert_canonical()
        self.assertEqual(self.protected(), protected)
        link.refresh_from_db()
        self.assertEqual(link.resolution_type, "MANUAL_OVERRIDE")
        for row in self.projection_rows():
            for field in ("attendance_type", "video_progress_rate", "video_completed",
                          "homework_submitted", "homework_passed", "meta", "completed_at"):
                self.assertEqual(row[field], before[row["id"]][field])
        self.assertEqual(len(self.projection_rows()), len(before))
        for enrollment in self.enrollments_a:
            lecture = self.LectureProgress.objects.get(enrollment=enrollment)
            self.assertEqual((lecture.completed_sessions, lecture.failed_sessions), (0, 1))

    def test_target_addition_refreshes_pass_to_fail_and_duplicate_is_noop(self):
        missing = self.missing_exam([self.enrollments_a[1], *self.enrollments_b])
        self.recalculate()
        protected = self.protected()
        ids = [row.id for row in self.enrollments_a]
        response = self.put_targets(missing, ids)
        self.assertEqual(response.status_code, 200, response.data)
        self.assert_canonical()
        first = self.projection_rows()
        self.assertEqual(self.put_targets(missing, ids).status_code, 200)
        self.assertEqual(self.projection_rows(), first)
        self.assertEqual(self.protected(), protected)

    def test_target_removal_refreshes_fail_to_pass(self):
        missing = self.missing_exam([*self.enrollments_a, *self.enrollments_b])
        self.recalculate()
        protected = self.protected()
        response = self.put_targets(missing, [self.enrollments_a[1].id])
        self.assertEqual(response.status_code, 200, response.data)
        self.assert_canonical()
        self.assertEqual(self.protected(), protected)

    def test_target_removal_audit_closes_only_removed_students_clinic_links(self):
        missing = self.missing_exam([*self.enrollments_a, *self.enrollments_b])
        removed = self.enrollments_a[0]
        retained = self.enrollments_a[1]
        removed_link = self.ClinicLink.objects.create(
            tenant=self.tenant,
            enrollment=removed,
            session=self.session_a,
            reason=self.ClinicLink.Reason.AUTO_FAILED,
            is_auto=True,
            source_type="exam",
            source_id=missing.id,
        )
        retained_link = self.ClinicLink.objects.create(
            tenant=self.tenant,
            enrollment=retained,
            session=self.session_a,
            reason=self.ClinicLink.Reason.AUTO_FAILED,
            is_auto=True,
            source_type="exam",
            source_id=missing.id,
        )
        delivery_before = {
            label: list(apps.get_model(label).objects.order_by("pk").values())
            for label in (
                "messaging.NotificationLog",
                "messaging.ScheduledNotification",
                "core.PlatformPushOutbox",
            )
        }

        response = self.put_targets(missing, [retained.id])

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["removed_effective_target_count"], 1)
        self.assertEqual(response.data["removed_effective_target_session_count"], 1)
        self.assertEqual(response.data["removed_clinic_link_count"], 1)
        removed_link.refresh_from_db()
        retained_link.refresh_from_db()
        self.assertEqual(removed_link.resolution_type, self.ClinicLink.ResolutionType.SOURCE_REMOVED)
        self.assertIsNotNone(removed_link.resolved_at)
        self.assertEqual(
            removed_link.resolution_evidence,
            {
                "reason": "exam_enrollment_removed",
                "source_type": "exam",
                "source_id": missing.id,
                "session_id": self.session_a.id,
                "enrollment_ids": [removed.id],
                "user_id": self.admin.id,
            },
        )
        self.assertEqual(
            removed_link.resolution_history[-1]["action"],
            "resolve_source_removed",
        )
        self.assertIsNone(retained_link.resolved_at)
        self.assertEqual(
            {
                label: list(apps.get_model(label).objects.order_by("pk").values())
                for label in delivery_before
            },
            delivery_before,
        )

    def test_first_explicit_target_closes_legacy_links_in_every_affected_session(self):
        second = self.Session.objects.create(
            lecture=self.lecture_a,
            order=2,
            title="Second legacy target session",
        )
        for enrollment in self.enrollments_a:
            self.SessionEnrollment.objects.create(
                tenant=self.tenant,
                enrollment=enrollment,
                session=second,
            )
        legacy_exam = self.missing_exam([])
        legacy_exam.sessions.add(second)
        removed = self.enrollments_a[0]
        links = [
            self.ClinicLink.objects.create(
                tenant=self.tenant,
                enrollment=enrollment,
                session=session,
                reason=self.ClinicLink.Reason.AUTO_FAILED,
                is_auto=True,
                source_type="exam",
                source_id=legacy_exam.id,
            )
            for enrollment, session in (
                (removed, self.session_a),
                (removed, second),
                (self.enrollments_b[0], self.session_b),
            )
        ]

        response = self.put_targets(legacy_exam, [self.enrollments_a[1].id])

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["removed_effective_target_count"], 4)
        self.assertEqual(response.data["removed_effective_target_session_count"], 3)
        self.assertEqual(response.data["removed_clinic_link_count"], 3)
        for link in links:
            link.refresh_from_db()
            self.assertEqual(
                link.resolution_type,
                self.ClinicLink.ResolutionType.SOURCE_REMOVED,
            )
            self.assertIn(
                link.enrollment_id,
                link.resolution_evidence["enrollment_ids"],
            )

    def test_target_replacement_rolls_back_when_removed_link_resolution_fails(self):
        missing = self.missing_exam([*self.enrollments_a, *self.enrollments_b])
        target_ids_before = set(
            self.ExamEnrollment.objects.filter(exam=missing).values_list(
                "enrollment_id", flat=True
            )
        )
        progress_before = self.projection_rows()

        with patch(
            "apps.domains.exams.views.exam_enrollment_view.resolve_removed_exam_clinic_links",
            side_effect=RuntimeError("synthetic clinic resolution failure"),
        ) as resolver:
            response = self.put_targets(missing, [self.enrollments_a[1].id])

        resolver.assert_called_once()
        self.assertEqual(response.status_code, 500, response.content)

        self.assertEqual(
            set(
                self.ExamEnrollment.objects.filter(exam=missing).values_list(
                    "enrollment_id", flat=True
                )
            ),
            target_ids_before,
        )
        self.assertEqual(self.projection_rows(), progress_before)

    def test_shared_same_lecture_sessions_are_all_refreshed(self):
        second = self.Session.objects.create(lecture=self.lecture_a, order=2, title="Second")
        self.exam.sessions.add(second)
        missing = self.missing_exam([self.enrollments_a[1], *self.enrollments_b])
        missing.sessions.add(second)
        self.recalculate()
        for enrollment in self.enrollments_a:
            self.SessionEnrollment.objects.create(tenant=self.tenant, enrollment=enrollment, session=second)
            SessionProgressCalculator.calculate(enrollment_id=enrollment.id, session=second, attendance_type="offline")
        before_b = list(self.SessionProgress.objects.filter(session=self.session_b).values())
        response = self.put_targets(missing, [row.id for row in self.enrollments_a])
        self.assertEqual(response.status_code, 200, response.data)
        self.assert_canonical()
        self.assertEqual(list(self.SessionProgress.objects.filter(session=self.session_b).values()), before_b)

    def test_legacy_to_explicit_refreshes_other_linked_lecture(self):
        missing = self.missing_exam([])
        self.recalculate()
        response = self.put_targets(missing, [self.enrollments_a[0].id])
        self.assertEqual(response.status_code, 200, response.data)
        self.assert_canonical()
        self.assertTrue(self.SessionProgress.objects.get(enrollment=self.enrollments_b[0]).exam_passed)

    def test_last_explicit_removal_refreshes_legacy_targets_in_all_linked_lectures(self):
        missing = self.missing_exam([self.enrollments_a[0]])
        self.recalculate()
        response = self.put_targets(missing, [])
        self.assertEqual(response.status_code, 200, response.data)
        self.assert_canonical()
        self.assertFalse(self.SessionProgress.objects.get(enrollment=self.enrollments_b[0]).exam_passed)

    def test_unlinked_foreign_and_inactive_projections_are_untouched(self):
        missing = self.missing_exam([*self.enrollments_a, *self.enrollments_b])
        self.recalculate()
        self.enrollments_b[0].status = "INACTIVE"
        self.enrollments_b[0].save(update_fields=["status"])
        foreign = self.SessionProgress.objects.create(enrollment=self.other_enrollment, session=self.other_session)
        invalid = self.SessionProgress.objects.create(enrollment=self.other_enrollment, session=self.session_a)
        missing.sessions.add(self.other_session)
        before = list(self.SessionProgress.objects.filter(pk__in=[foreign.pk, invalid.pk]).values())
        inactive = self.SessionProgress.objects.get(enrollment=self.enrollments_b[0])
        inactive_before = self.SessionProgress.objects.filter(pk=inactive.pk).values().get()
        self.assertEqual(self.put_targets(missing, []).status_code, 200)
        self.assertEqual(list(self.SessionProgress.objects.filter(pk__in=[foreign.pk, invalid.pk]).values()), before)
        self.assertEqual(self.SessionProgress.objects.filter(pk=inactive.pk).values().get(), inactive_before)

    def test_projection_failure_rolls_back_target_replacement_and_partial_projection(self):
        missing = self.missing_exam([*self.enrollments_a, *self.enrollments_b])
        self.recalculate()
        roster = list(self.ExamEnrollment.objects.order_by("pk").values())
        before = self.projection_rows()
        canonical = SessionProgressCalculator._aggregate_exam_results
        calls = 0

        def fail_second(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("synthetic projection failure")
            return canonical(**kwargs)

        with patch.object(SessionProgressCalculator, "_aggregate_exam_results", side_effect=fail_second):
            response = self.put_targets(missing, [])
        self.assertEqual(response.status_code, 500, response.json())
        self.assertEqual(calls, 2)
        self.assertEqual(self.projection_rows(), before)
        self.assertEqual(list(self.ExamEnrollment.objects.order_by("pk").values()), roster)

    def test_projection_failure_rolls_back_regular_creation(self):
        before = self.projection_rows()
        exams = self.Exam.objects.count()
        roster = self.ExamEnrollment.objects.count()
        with patch.object(SessionProgressCalculator, "_aggregate_exam_results", side_effect=RuntimeError("projection failed")):
            response = self.create_exam()
        self.assertEqual(response.status_code, 500, response.json())
        self.assertEqual(self.Exam.objects.count(), exams)
        self.assertEqual(self.ExamEnrollment.objects.count(), roster)
        self.assertEqual(self.projection_rows(), before)

    def test_after_commit_callback_failure_cannot_leave_projection_stale(self):
        missing = self.missing_exam([self.enrollments_a[1], *self.enrollments_b])
        self.recalculate()

        def fail_after_commit():
            raise RuntimeError("unrelated after-commit callback failed")

        with self.assertRaisesRegex(RuntimeError, "after-commit callback failed"):
            with transaction.atomic():
                transaction.on_commit(fail_after_commit)
                response = self.put_targets(missing, [row.id for row in self.enrollments_a])
                self.assertEqual(response.status_code, 200, response.data)
        self.assert_canonical()

    def test_absent_projection_create_read_then_first_score_uses_normal_generation(self):
        # A new session roster has no progress until a real assessment/input event.
        self.SessionProgress.objects.filter(session=self.session_a).delete()
        self.Result.objects.filter(enrollment__lecture=self.lecture_a).delete()
        self.exam.sessions.remove(self.session_a)
        protected = self.protected()
        with self.assertLogs("apps.domains.progress.services.exam_target_projection", level="INFO") as logs:
            created = self.create_exam()
        self.assertEqual(created.status_code, 201, created.data)
        self.assertTrue(any("existing=0 absent=2" in line for line in logs.output))
        exam_id = created.data["id"]
        summary_url = f"/api/v1/results/admin/sessions/{self.session_a.id}/exams/summary/"
        summary = self.client.get(summary_url, **self.headers)
        self.assertEqual(summary.status_code, 200, summary.data)
        self.assertEqual((summary.data["participant_count"], summary.data["pass_rate"]), (0, 0))
        self.assertEqual([row["exam_id"] for row in summary.data["exams"]], [exam_id])
        roster = self.client.get(f"/api/v1/exams/{exam_id}/enrollments/?session_id={self.session_a.id}", **self.headers)
        self.assertEqual(roster.status_code, 200, roster.data)
        self.assertEqual({row["enrollment_id"] for row in roster.data["items"] if row["is_selected"]}, {row.id for row in self.enrollments_a})
        self.assertFalse(self.SessionProgress.objects.filter(session=self.session_a).exists())
        self.assertEqual(self.protected(), protected)

        apps.get_model("results.ScoreEditDraft").objects.create(
            tenant=self.tenant, session=self.session_a, editor_user=self.admin,
            payload={"client_id": "projection-first-score", "changes": []},
        )
        scored = self.client.patch(
            f"/api/v1/results/admin/exams/{exam_id}/enrollments/{self.enrollments_a[0].id}/score/",
            {"score": 80, "max_score": 100}, format="json", **self.headers,
            HTTP_X_SCORE_EDITOR_CLIENT="projection-first-score",
            HTTP_X_SCORE_SESSION_ID=str(self.session_a.id),
        )
        self.assertEqual(scored.status_code, 200, scored.data)
        self.assertTrue(scored.data["progress"]["dispatched"], scored.data)
        self.assertEqual(self.SessionProgress.objects.filter(session=self.session_a).count(), 1)
        after = self.client.get(summary_url, **self.headers)
        self.assertEqual((after.data["participant_count"], after.data["pass_rate"]), (1, 1))
        self.assert_canonical()

    @skipUnless(connection.vendor == "postgresql", "PostgreSQL row-lock concurrency contract")
    def test_cross_concurrent_create_and_replace_have_no_deadlock_or_lost_projection(self):
        for _ in range(3):
            missing = self.missing_exam([*self.enrollments_a, *self.enrollments_b])
            self.recalculate()
            barrier = Barrier(2)

            def call(create):
                close_old_connections()
                try:
                    client = APIClient()
                    client.force_authenticate(self.admin)
                    barrier.wait(timeout=10)
                    response = self.create_exam(client=client) if create else self.put_targets(missing, [self.enrollments_a[1].id], client=client)
                    return response.status_code
                finally:
                    close_old_connections()

            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(call, create) for create in (True, False)]
                self.assertEqual([future.result(timeout=30) for future in futures], [201, 200])
            self.assert_canonical()

    def test_canonical_field_semantics_completion_history_and_risk_thresholds(self):
        from apps.domains.progress.services.lecture_calculator import LectureProgressCalculator
        from apps.domains.progress.services.risk_evaluator import RiskEvaluator

        missing = self.missing_exam([*self.enrollments_a, *self.enrollments_b])
        self.recalculate()
        subject = self.enrollments_a[0]
        for order in (2, 3):
            session = self.Session.objects.create(lecture=self.lecture_a, order=order, title=f"Risk {order}")
            missing.sessions.add(session)
            self.exam.sessions.add(session)
            SessionProgressCalculator.calculate(enrollment_id=subject.id, session=session, attendance_type="offline", homework_submitted=True)
        self.SessionProgress.objects.filter(enrollment=subject).update(completed_at=None)
        protected = self.protected()
        # Removing all missing-result requirements completes three sessions at once.
        self.assertEqual(self.put_targets(missing, [self.enrollments_a[1].id]).status_code, 200)
        first_completed = {}
        for row in self.SessionProgress.objects.filter(enrollment=subject):
            first_completed[row.id] = row.completed_at
            self.assertIsNotNone(row.completed_at)
            expected = SessionProgressCalculator.calculate(
                enrollment_id=subject.id, session=row.session,
                attendance_type=row.attendance_type, video_progress_rate=row.video_progress_rate,
                homework_submitted=row.homework_submitted,
            )
            for field in ("exam_attempted", "exam_aggregate_score", "exam_passed", "exam_meta", "completed", "completed_at"):
                self.assertEqual(getattr(row, field), getattr(expected, field), field)
        # Adding them back must preserve the first completion timestamps.
        self.assertEqual(self.put_targets(missing, [row.id for row in self.enrollments_a]).status_code, 200)
        for row in self.SessionProgress.objects.filter(enrollment=subject):
            self.assertFalse(row.completed)
            self.assertEqual(row.completed_at, first_completed[row.id])
        stored = self.LectureProgress.objects.get(enrollment=subject)
        expected = LectureProgressCalculator.calculate(enrollment_id=subject.id, lecture=self.lecture_a)
        for field in ("total_sessions", "completed_sessions", "failed_sessions", "consecutive_failed_sessions", "last_session_id"):
            self.assertEqual(getattr(stored, field), getattr(expected, field), field)
        self.assertEqual(stored.risk_level, "DANGER")
        with patch.object(RiskEvaluator, "_log_once"):
            for count in (0, 1, 2, 3, 4):
                expected.consecutive_failed_sessions = count
                RiskEvaluator.evaluate(expected)
                self.assertEqual(expected.risk_level, RiskEvaluator.level_for_consecutive_failures(count))
        self.assertEqual(self.protected(), protected)

    @skipUnless(connection.vendor == "postgresql", "PostgreSQL row-lock concurrency contract")
    def test_concurrent_replacements_finish_with_exact_roster_and_canonical_projection(self):
        missing = self.missing_exam([*self.enrollments_a, *self.enrollments_b])
        self.recalculate()
        barrier = Barrier(2)

        def replace(enrollment):
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(self.admin)
                barrier.wait(timeout=10)
                return self.put_targets(missing, [enrollment.id], client=client).status_code
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(replace, enrollment) for enrollment in self.enrollments_a]
            self.assertEqual([future.result(timeout=30) for future in futures], [200, 200])
        selected = set(self.ExamEnrollment.objects.filter(exam=missing, enrollment__lecture=self.lecture_a).values_list("enrollment_id", flat=True))
        self.assertIn(selected, [{self.enrollments_a[0].id}, {self.enrollments_a[1].id}])
        self.assert_canonical()
