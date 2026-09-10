from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TransactionTestCase, override_settings

from apps.core.services.state_detector import run_state_detector
from apps.core.tests import test_state_detector as fixtures
from apps.domains.progress.services.session_calculator import SessionProgressCalculator
from apps.support.progress import state_detector_dependencies as detector


class StateDetectorPaginationTests(TransactionTestCase):
    age_sources = fixtures.StateDetectorTests.age_sources

    def setUp(self):
        fixtures.StateDetectorTests.setUp(self)
        self.progresses = [self.progress]
        for order in (2, 3):
            session = self.Session.objects.create(lecture=self.lecture, order=order, title="Synthetic page")
            self.exam.sessions.add(session)
            self.progresses.append(self.SessionProgress.objects.create(
                enrollment=self.enrollment, session=session,
                exam_passed=order == 3, calculated_at=self.old,
            ))
        self.age_sources()

    def scan(self):
        return detector.inspect_session_exam_state(tenant_id=self.tenant.id, limit=1, now=self.now)

    def test_keyset_pages_cover_every_row_and_keep_last_page_finding(self):
        seen = []
        inspect = detector._inspect_row

        def record(progress, **kwargs):
            seen.append(progress.pk)
            return inspect(progress, **kwargs)

        with patch.object(detector, "_inspect_row", side_effect=record):
            report = self.scan()
        self.assertEqual(report["inspection_status"], "complete", report)
        self.assertEqual(report["finding_count"], 1)
        self.assertEqual(seen, [row.pk for row in self.progresses])
        self.assertEqual((report["source_count"], report["scanned"], report["page_count"]), (3, 3, 3))
        self.assertEqual(len(report["_covered_subjects"]), 3)

    def test_middle_page_failure_preserves_findings_without_completeness(self):
        inspect = detector._inspect_row
        self.SessionProgress.objects.filter(pk=self.progress.pk).update(exam_passed=True)

        def fail_middle(progress, **kwargs):
            if progress.pk == self.progresses[1].pk:
                raise detector.InspectionFailure("synthetic_page_failure")
            return inspect(progress, **kwargs)

        with patch.object(detector, "_inspect_row", side_effect=fail_middle):
            report = self.scan()
        self.assertEqual(report["errors"], ["synthetic_page_failure"])
        self.assertEqual(report["inspection_status"], "failed")
        self.assertEqual(report["finding_count"], 1)
        self.assertEqual(report["scanned"], 1)

    def test_postgresql_pages_share_snapshot_across_concurrent_update(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL snapshot contract")
        other = connection.copy()
        inspect = detector._inspect_row

        def change_after_first(progress, **kwargs):
            result = inspect(progress, **kwargs)
            if progress.pk == self.progress.pk:
                with other.cursor() as cursor:
                    cursor.execute('UPDATE progress_sessionprogress SET exam_passed=false WHERE id=%s', [self.progresses[2].pk])
            return result

        try:
            with patch.object(detector, "_inspect_row", side_effect=change_after_first):
                report = self.scan()
        finally:
            other.close()
        self.assertEqual(report["inspection_status"], "complete", report)
        self.assertEqual(report["finding_count"], 1)
        self.assertEqual(self.scan()["finding_count"], 0)

    def test_snapshot_neither_skips_deleted_rows_nor_includes_new_rows_between_pages(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL snapshot contract")
        session = self.Session.objects.create(lecture=self.lecture, order=4, title="Concurrent insert")
        self.exam.sessions.add(session)
        self.age_sources()
        other = connection.copy()
        inspect = detector._inspect_row
        seen = []

        def mutate_between_pages(progress, **kwargs):
            seen.append(progress.pk)
            result = inspect(progress, **kwargs)
            if progress.pk == self.progress.pk:
                with other.cursor() as cursor:
                    cursor.execute(
                        'INSERT INTO progress_sessionprogress (created_at, updated_at, enrollment_id, session_id, attendance_type, video_progress_rate, video_completed, exam_attempted, exam_passed, homework_submitted, homework_passed, completed, calculated_at) '
                        'SELECT created_at, updated_at, enrollment_id, %s, attendance_type, video_progress_rate, video_completed, exam_attempted, exam_passed, homework_submitted, homework_passed, completed, calculated_at FROM progress_sessionprogress WHERE id=%s',
                        [session.id, self.progress.pk],
                    )
                    cursor.execute('DELETE FROM progress_sessionprogress WHERE id=%s', [self.progresses[2].pk])
            return result

        try:
            with patch.object(detector, "_inspect_row", side_effect=mutate_between_pages):
                report = self.scan()
        finally:
            other.close()
        self.assertEqual(report["inspection_status"], "complete", report)
        self.assertEqual(seen, [row.pk for row in self.progresses])
        self.assertEqual((report["source_count"], report["scanned"], report["finding_count"]), (3, 3, 1))
        self.assertEqual(self.scan()["finding_count"], 0)

    def test_mid_scan_deadline_preserves_prefix_and_never_reports_healthy(self):
        inspect = detector._inspect_row
        monotonic = detector.time.monotonic
        expired = False

        def inspect_then_expire(progress, **kwargs):
            nonlocal expired
            result = inspect(progress, **kwargs)
            expired = True
            return result

        with (
            patch.object(detector, "_inspect_row", side_effect=inspect_then_expire),
            patch.object(detector.time, "monotonic", side_effect=lambda: monotonic() + (31 if expired else 0)),
        ):
            report = self.scan()
        self.assertEqual(report["errors"], ["scan_timeout"])
        self.assertEqual((report["source_count"], report["scanned"]), (3, 1))
        self.assertEqual(report["state"], "unknown")

    def test_batched_sources_match_full_canonical_tuple_for_all_policy_modes(self):
        second = self.Exam.objects.create(
            tenant=self.tenant, title="Second canonical input", exam_type="regular", pass_score=50, max_score=100,
        )
        second.sessions.add(self.session)
        self.ExamEnrollment.objects.create(exam=second, enrollment=self.enrollment)
        result = self.Result.objects.create(
            target_type="exam", target_id=second.id, enrollment=self.enrollment,
            total_score=80, max_score=100,
        )
        self.ExamLecturePolicy.objects.create(exam=second, lecture=self.lecture, pass_score=90)
        self.age_sources()
        aggregate = SessionProgressCalculator._aggregate_exam_results
        compared = 0

        def parity(**kwargs):
            nonlocal compared
            self.assertIsNotNone(kwargs.get("read_sources"))
            batched = aggregate(**kwargs)
            direct = aggregate(**{**kwargs, "read_sources": None})
            self.assertEqual(batched, direct)
            compared += 1
            return batched

        with patch.object(SessionProgressCalculator, "_aggregate_exam_results", side_effect=parity):
            for strategy in ("MAX", "AVG", "LATEST"):
                for source in ("EXAM", "POLICY"):
                    self.ProgressPolicy.objects.filter(pk=self.policy.pk).update(exam_aggregate_strategy=strategy, exam_pass_source=source)
                    self.assertEqual(self.scan()["inspection_status"], "complete")
            self.ExamAttempt.objects.filter(pk=self.attempt.pk).update(meta={"status": "NOT_SUBMITTED"})
            self.Exam.objects.filter(pk=self.exam.pk).update(pass_score=0)
            self.assertEqual(self.scan()["inspection_status"], "complete")
            result.delete()
            self.assertEqual(self.scan()["inspection_status"], "complete")
            self.result.delete()
            self.assertEqual(self.scan()["inspection_status"], "complete")
        self.assertGreaterEqual(compared, 9)

    def test_page_source_memory_bound_fails_without_sampling_success(self):
        with patch("apps.support.progress.state_detector_page.BATCH_SOURCE_LIMIT", 0):
            report = self.scan()
        self.assertEqual(report["inspection_status"], "failed")
        self.assertIn("page_source_limit_exceeded", report["errors"])
        self.assertEqual(report["scanned"], 0)

    @override_settings(DEV_ALERTS_WEBHOOK_URL="https://receiver.invalid")
    def test_partial_page_failure_cannot_send_false_recovery(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL operational receipt/advisory-lock contract")
        with patch("apps.core.services.state_detector._deliver", return_value="delivered") as deliver:
            first = run_state_detector(tenant_id=self.tenant.id, limit=1)
            self.assertEqual(first["delivery_status"], "delivered", first)
            self.SessionProgress.objects.update(exam_passed=False)
            with patch.object(detector, "SCAN_TIMEOUT_SECONDS", -1):
                failed = run_state_detector(tenant_id=self.tenant.id, limit=1)
            self.assertEqual(failed["inspection_status"], "failed")
            self.assertEqual(deliver.call_count, 1)

    def test_2256_real_projections_complete_with_bounded_pages_and_queries(self):
        users = get_user_model().objects.bulk_create([
            get_user_model()(username=f"qa-page-student-{index}") for index in range(43)
        ])
        students = self.Student.objects.bulk_create([
            self.Student(tenant=self.tenant, user=user, name="Synthetic scale student", ps_number=f"QAP{index}", omr_code=f"QAP{index}")
            for index, user in enumerate(users)
        ])
        enrollments = [self.enrollment, *self.Enrollment.objects.bulk_create([
            self.Enrollment(tenant=self.tenant, lecture=self.lecture, student=student, status="ACTIVE")
            for student in students
        ])]
        sessions = list(self.Session.objects.order_by("order")) + self.Session.objects.bulk_create([
            self.Session(lecture=self.lecture, order=order, regular_order=order, title="Synthetic scale session") for order in range(4, 81)
        ])
        self.exam.sessions.add(*sessions)
        self.ExamEnrollment.objects.bulk_create([
            self.ExamEnrollment(exam=self.exam, enrollment=enrollment) for enrollment in enrollments[1:]
        ])
        attempts = self.ExamAttempt.objects.bulk_create([
            self.ExamAttempt(exam=self.exam, enrollment=enrollment, attempt_index=1, status="done") for enrollment in enrollments[1:]
        ])
        self.Result.objects.bulk_create([
            self.Result(target_type="exam", target_id=self.exam.id, enrollment=enrollment, attempt=attempt, total_score=20, max_score=100)
            for enrollment, attempt in zip(enrollments[1:], attempts)
        ])
        existing = {(row.enrollment_id, row.session_id) for row in self.progresses}
        self.SessionProgress.objects.bulk_create([
            self.SessionProgress(enrollment=enrollment, session=session, exam_passed=False, calculated_at=self.old)
            for index, session in enumerate(sessions)
            for enrollment in (enrollments if index == 79 else enrollments[:28])
            if (enrollment.id, session.id) not in existing
        ])
        self.age_sources()
        counts = {"selects": 0}

        def count_queries(execute, sql, params, many, context):
            if sql.lstrip().upper().startswith("SELECT "):
                counts["selects"] += 1
            return execute(sql, params, many, context)

        with connection.execute_wrapper(count_queries):
            report = detector.inspect_session_exam_state(tenant_id=self.tenant.id, limit=200, now=self.now)
        public = {key: value for key, value in report.items() if not key.startswith("_")}
        print("PAGINATION_BENCHMARK", {**public, **counts})
        self.assertEqual(report["inspection_status"], "complete", public)
        self.assertEqual((report["source_count"], report["scanned"], report["page_count"]), (2256, 2256, 12))
        self.assertEqual(report["finding_count"], 1)
        self.assertLessEqual(report["elapsed_ms"], 30000)
        self.assertLessEqual(counts["selects"], 600)
