from __future__ import annotations

from contextlib import nullcontext
from datetime import timedelta
from io import StringIO
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
from unittest import skipUnless
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from apps.core.models import OpsAuditLog, Tenant
from apps.core.services.state_detector import _exclusive_monitor, _lock_key, run_state_detector
from apps.support.progress.state_detector_dependencies import (
    InspectionFailure,
    _read_snapshot,
    inspect_session_exam_state,
)


class StateDetectorTests(TransactionTestCase):
    def setUp(self):
        self.now = timezone.now()
        self.old = self.now - timedelta(minutes=20)
        self.tenant = Tenant.objects.create(code="qa-state-detector", name="Synthetic")
        self.other = Tenant.objects.create(code="qa-state-other", name="Other")
        for app, names in {
            "lectures": ["Lecture", "Session"],
            "students": ["Student"],
            "enrollment": ["Enrollment"],
            "exams": ["Exam", "ExamEnrollment", "ExamLecturePolicy"],
            "results": ["Result", "ExamAttempt"],
            "progress": ["ProgressPolicy", "SessionProgress", "ClinicLink"],
        }.items():
            for name in names:
                setattr(self, name, apps.get_model(app, name))
        self.lecture = self.Lecture.objects.create(tenant=self.tenant, title="Synthetic", name="Synthetic")
        self.session = self.Session.objects.create(lecture=self.lecture, order=1, title="Synthetic")
        self.student = self.Student.objects.create(
            tenant=self.tenant,
            user=get_user_model().objects.create_user(username="qa-state-student"),
            name="Do not emit this name",
            ps_number="QA1",
            omr_code="QA1",
        )
        self.enrollment = self.Enrollment.objects.create(
            tenant=self.tenant, lecture=self.lecture, student=self.student, status="ACTIVE"
        )
        self.policy = self.ProgressPolicy.objects.create(lecture=self.lecture, exam_start_session_order=1)
        self.exam = self.Exam.objects.create(
            tenant=self.tenant, title="Synthetic", exam_type="regular", pass_score=60, max_score=100
        )
        self.exam.sessions.add(self.session)
        self.target = self.ExamEnrollment.objects.create(exam=self.exam, enrollment=self.enrollment)
        self.attempt = self.ExamAttempt.objects.create(
            exam=self.exam, enrollment=self.enrollment, attempt_index=1, status="done"
        )
        self.result = self.Result.objects.create(
            target_type="exam",
            target_id=self.exam.id,
            enrollment=self.enrollment,
            attempt=self.attempt,
            total_score=20,
            max_score=100,
        )
        self.progress = self.SessionProgress.objects.create(
            enrollment=self.enrollment, session=self.session, exam_passed=False, completed=False, calculated_at=self.old
        )
        self.age_sources()

    def age_sources(self):
        for model in (
            self.Lecture,
            self.Session,
            self.Student,
            self.Enrollment,
            self.ProgressPolicy,
            self.Exam,
            self.Result,
            self.ExamAttempt,
            self.SessionProgress,
            self.ClinicLink,
            self.ExamLecturePolicy,
        ):
            model.objects.all().update(updated_at=self.old)
        self.ExamEnrollment.objects.all().update(created_at=self.old)

    def scan(self, **kwargs):
        return inspect_session_exam_state(tenant_id=self.tenant.id, now=self.now, **kwargs)

    def run_monitor(self):
        # PostgreSQL exercises the real advisory lock. SQLite only exercises the
        # portable observation/receipt logic; production execution requires PG.
        lock = (
            patch("apps.core.services.state_detector._exclusive_monitor", return_value=nullcontext())
            if connection.vendor != "postgresql"
            else nullcontext()
        )
        with lock, patch("apps.core.services.state_detector.timezone.now", return_value=self.now):
            return run_state_detector(tenant_id=self.tenant.id)

    def make_contradiction(self):
        self.SessionProgress.objects.filter(pk=self.progress.pk).update(exam_passed=True)

    def test_real_source_mismatch_is_found_without_modifying_business_rows(self):
        self.make_contradiction()
        before = list(self.SessionProgress.objects.values())
        report = self.scan()
        self.assertEqual(report["inspection_status"], "complete")
        self.assertEqual(report["state"], "contradiction")
        self.assertEqual(report["finding_count"], 1)
        self.assertEqual(list(self.SessionProgress.objects.values()), before)
        self.assertEqual(OpsAuditLog.objects.count(), 0)
        self.assertNotIn(self.student.name, json.dumps(report))

    def test_current_failed_score_and_historical_completion_timestamp_are_normal(self):
        self.SessionProgress.objects.filter(pk=self.progress.pk).update(
            completed_at=self.old, exam_meta={"exams": [{"passed": True}]}
        )
        self.assertEqual(self.scan()["state"], "healthy")

    def test_manual_waiver_carry_over_and_closed_source_are_not_inferred_score_failures(self):
        self.make_contradiction()
        for resolution in (
            "MANUAL_OVERRIDE",
            "WAIVED",
            "CARRIED_OVER",
            "SOURCE_REMOVED",
            "NOT_SUBMITTED",
            "BOOKING_LEGACY",
        ):
            with self.subTest(resolution=resolution):
                link = self.ClinicLink.objects.create(
                    tenant=self.tenant,
                    enrollment=self.enrollment,
                    session=self.session,
                    source_type="exam",
                    source_id=self.exam.id,
                    reason="AUTO_FAILED",
                    resolved_at=self.old,
                    resolution_type=resolution,
                )
                self.age_sources()
                report = self.scan()
                self.assertEqual(report["state"], "healthy")
                self.assertEqual(report["excluded"], 1)
                link.delete()

    def test_non_target_and_supplement_session_are_normal(self):
        self.make_contradiction()
        other_student = self.Student.objects.create(
            tenant=self.tenant,
            user=get_user_model().objects.create_user(username="qa-state-other-student"),
            name="Other target",
            ps_number="QA2",
            omr_code="QA2",
        )
        self.ExamEnrollment.objects.filter(pk=self.target.pk).update(
            enrollment_id=self.Enrollment.objects.create(
                tenant=self.tenant, lecture=self.lecture, student=other_student, status="ACTIVE"
            ).pk
        )
        self.assertEqual(self.scan()["state"], "healthy")
        self.Session.objects.filter(pk=self.session.pk).update(session_type="SUPPLEMENT", regular_order=None)
        self.assertEqual(self.scan()["state"], "healthy")

    def test_latest_representative_not_historical_failure_drives_projection(self):
        self.make_contradiction()
        self.ExamAttempt.objects.filter(pk=self.attempt.pk).update(is_representative=False)
        latest = self.ExamAttempt.objects.create(
            exam=self.exam, enrollment=self.enrollment, attempt_index=2, is_retake=True, status="done"
        )
        self.Result.objects.filter(pk=self.result.pk).update(attempt=latest, total_score=80)
        self.age_sources()
        self.assertEqual(self.scan()["state"], "healthy")

    def test_representative_not_submitted_does_not_pass_at_zero_threshold(self):
        self.make_contradiction()
        self.Exam.objects.filter(pk=self.exam.pk).update(pass_score=0)
        self.ExamAttempt.objects.filter(pk=self.attempt.pk).update(meta={"status": "NOT_SUBMITTED"})
        self.assertEqual(self.scan()["finding_count"], 1)

    def test_lecture_specific_threshold_is_used(self):
        self.make_contradiction()
        self.ExamLecturePolicy.objects.create(exam=self.exam, lecture=self.lecture, pass_score=10)
        self.age_sources()
        self.assertEqual(self.scan()["state"], "healthy")

    def test_recent_changes_and_active_grading_defer_without_declaring_recovery(self):
        self.make_contradiction()
        self.Result.objects.filter(pk=self.result.pk).update(updated_at=self.now)
        self.assertEqual(self.scan()["inspection_status"], "deferred")
        self.age_sources()
        self.ExamAttempt.objects.filter(pk=self.attempt.pk).update(status="grading")
        self.assertEqual(self.scan()["inspection_status"], "deferred")

    def test_missing_policy_invalid_graph_and_limit_are_inspection_failures(self):
        self.policy.delete()
        self.assertIn("missing_policy", self.scan()["errors"])
        self.policy = self.ProgressPolicy.objects.create(lecture=self.lecture, exam_start_session_order=1)
        self.age_sources()
        self.Lecture.objects.filter(pk=self.lecture.pk).update(tenant=self.other)
        self.assertIn("invalid_tenant_graph", self.scan()["errors"])
        self.assertEqual(self.scan(limit=0)["inspection_status"], "failed")

    def test_tenant_is_required_and_other_tenant_rows_do_not_leak(self):
        self.make_contradiction()
        report = inspect_session_exam_state(tenant_id=self.other.id, now=self.now)
        self.assertEqual(report["checked"], 0)
        self.assertEqual(report["finding_count"], 0)
        for tenant_id in (None, 0, 9999999):
            self.assertEqual(
                inspect_session_exam_state(tenant_id=tenant_id, now=self.now)["inspection_status"], "failed"
            )

    def test_dry_run_without_receiver_has_no_receipts_or_external_calls(self):
        self.make_contradiction()
        with (
            override_settings(DEV_ALERTS_WEBHOOK_URL=""),
            patch("apps.core.services.state_detector._deliver") as deliver,
        ):
            out = StringIO()
            call_command("check_state_integrity", tenant=self.tenant.id, dry_run=True, stdout=out)
        self.assertEqual(json.loads(out.getvalue())["state"], "contradiction")
        self.assertEqual(OpsAuditLog.objects.count(), 0)
        deliver.assert_not_called()

    @override_settings(DEV_ALERTS_WEBHOOK_URL="https://receiver.invalid")
    def test_repeated_detection_recovery_and_reappearance_have_durable_transitions(self):
        self.make_contradiction()
        with patch("apps.core.services.state_detector._deliver", return_value="delivered") as deliver:
            first = self.run_monitor()
            duplicate = self.run_monitor()
            self.assertEqual(first["delivery_status"], "delivered")
            self.assertEqual(duplicate["delivery_status"], "suppressed")
            self.assertEqual(deliver.call_count, 1)
            self.SessionProgress.objects.filter(pk=self.progress.pk).update(exam_passed=False)
            recovered = self.run_monitor()
            self.assertEqual(recovered["event"], "recovered")
            self.assertEqual(self.run_monitor()["delivery_status"], "suppressed")
            self.make_contradiction()
            self.assertEqual(self.run_monitor()["event"], "opened")
            self.assertEqual(deliver.call_count, 3)

    @override_settings(DEV_ALERTS_WEBHOOK_URL="https://receiver.invalid")
    def test_delivery_failure_is_separate_and_retries_same_event(self):
        self.make_contradiction()
        with patch("apps.core.services.state_detector._deliver", side_effect=["failed", "delivered"]) as deliver:
            failed = self.run_monitor()
            retry = self.run_monitor()
        self.assertEqual(failed["state"], "contradiction")
        self.assertEqual(failed["delivery_status"], "failed")
        self.assertEqual(retry["delivery_status"], "delivered")
        self.assertEqual(failed["event_id"], retry["event_id"])
        self.assertEqual(deliver.call_count, 2)
        self.assertTrue(OpsAuditLog.objects.filter(payload__delivery_status="failed").exists())

    @override_settings(DEV_ALERTS_WEBHOOK_URL="https://receiver.invalid")
    def test_incomplete_scan_or_disappearing_subject_never_sends_false_recovery(self):
        self.make_contradiction()
        with patch("apps.core.services.state_detector._deliver", return_value="delivered") as deliver:
            self.run_monitor()
            self.policy.delete()
            self.assertEqual(self.run_monitor()["inspection_status"], "failed")
            self.progress.delete()
            report = self.run_monitor()
            self.assertIn("previous_subject_missing", report["errors"])
            self.assertEqual(deliver.call_count, 1)

    @override_settings(DEV_ALERTS_WEBHOOK_URL="")
    def test_missing_receiver_fails_but_preserves_observed_state(self):
        self.make_contradiction()
        result = self.run_monitor()
        self.assertEqual(result["state"], "contradiction")
        self.assertEqual(result["delivery_status"], "failed")
        self.assertIn("receiver_missing", result["errors"])

    def test_command_requires_explicit_tenant(self):
        with self.assertRaises(CommandError):
            call_command("check_state_integrity", dry_run=True, stdout=StringIO())

    def test_old_resolved_cycle_does_not_hide_reopened_source(self):
        self.make_contradiction()
        self.ClinicLink.objects.create(
            tenant=self.tenant,
            enrollment=self.enrollment,
            session=self.session,
            source_type="exam",
            source_id=self.exam.id,
            reason="AUTO_FAILED",
            cycle_no=1,
            resolved_at=self.old,
            resolution_type="WAIVED",
        )
        self.ClinicLink.objects.create(
            tenant=self.tenant,
            enrollment=self.enrollment,
            session=self.session,
            source_type="exam",
            source_id=self.exam.id,
            reason="AUTO_FAILED",
            cycle_no=2,
        )
        self.age_sources()
        self.assertEqual(self.scan()["finding_count"], 1)

    def test_homework_exception_with_same_numeric_id_does_not_hide_exam_mismatch(self):
        self.make_contradiction()
        self.ClinicLink.objects.create(
            tenant=self.tenant,
            enrollment=self.enrollment,
            session=self.session,
            source_type="homework",
            source_id=self.exam.id,
            reason="AUTO_FAILED",
            resolved_at=self.old,
            resolution_type="WAIVED",
        )
        self.age_sources()
        self.assertEqual(self.scan()["finding_count"], 1)

    def test_page_size_preserves_full_coverage_and_source_limit_still_fails_closed(self):
        session = self.Session.objects.create(lecture=self.lecture, order=2, title="Synthetic")
        self.SessionProgress.objects.create(enrollment=self.enrollment, session=session, calculated_at=self.old)
        report = self.scan(limit=1)
        self.assertEqual(report["inspection_status"], "complete", report)
        self.assertEqual((report["source_count"], report["scanned"], report["page_count"]), (2, 2, 2))
        with patch("apps.support.progress.state_detector_dependencies.SOURCE_LIMIT", 0):
            self.assertIn("source_limit_exceeded", self.scan()["errors"])

    def test_expired_scan_budget_never_reports_healthy(self):
        with patch("apps.support.progress.state_detector_dependencies.SCAN_TIMEOUT_SECONDS", -1):
            self.assertIn("scan_timeout", self.scan()["errors"])

    def test_missing_calculation_and_unknown_states_are_inspection_failures(self):
        self.SessionProgress.objects.filter(pk=self.progress.pk).update(calculated_at=None)
        self.assertIn("missing_calculation", self.scan()["errors"])
        self.SessionProgress.objects.filter(pk=self.progress.pk).update(calculated_at=self.old)
        self.ExamAttempt.objects.filter(pk=self.attempt.pk).update(status="unrecognized")
        self.assertIn("unknown_attempt_state", self.scan()["errors"])

    def test_booking_cancellation_checkout_and_self_study_do_not_determine_exam_pass(self):
        participant_model = apps.get_model("clinic", "SessionParticipant")
        participant = participant_model.objects.create(
            tenant=self.tenant, student=self.student, enrollment=self.enrollment, status="cancelled"
        )
        for status, checkout, completed in (
            ("cancelled", None, None),
            ("booked", self.old, None),
            ("attended", None, self.old),
        ):
            with self.subTest(status=status):
                participant_model.objects.filter(pk=participant.pk).update(
                    status=status, checked_out_at=checkout, completed_at=completed
                )
                self.assertEqual(self.scan()["state"], "healthy")
                self.make_contradiction()
                self.assertEqual(self.scan()["finding_count"], 1)
                self.SessionProgress.objects.filter(pk=self.progress.pk).update(exam_passed=False)

    def test_legacy_result_without_attempt_and_missing_result_use_canonical_policy(self):
        self.Result.objects.filter(pk=self.result.pk).update(attempt=None)
        self.assertEqual(self.scan()["state"], "healthy")
        self.result.delete()
        self.assertEqual(self.scan()["state"], "healthy")
        self.make_contradiction()
        self.assertEqual(self.scan()["finding_count"], 1)

    def test_recent_submission_and_unknown_tenant_relation_are_not_healthy(self):
        submission_model = apps.get_model("submissions", "Submission")
        submission = submission_model.objects.create(
            tenant=self.tenant,
            user=self.student.user,
            enrollment=self.enrollment,
            target_type="exam",
            target_id=self.exam.id,
            source="omr_scan",
            status="grading",
        )
        self.assertEqual(self.scan()["inspection_status"], "deferred")
        submission_model.objects.filter(pk=submission.pk).update(status="done", updated_at=self.now)
        self.assertEqual(self.scan()["inspection_status"], "deferred")
        submission_model.objects.filter(pk=submission.pk).update(tenant=self.other, updated_at=self.old)
        self.assertIn("invalid_tenant_graph", self.scan()["errors"])

    def test_cross_tenant_bound_exam_is_inspection_failure_without_sibling_data(self):
        self.Exam.objects.filter(pk=self.exam.pk).update(tenant=self.other)
        report = self.scan()
        self.assertIn("invalid_tenant_graph", report["errors"])
        self.assertEqual(report["finding_count"], 0)

    def test_evaluation_exception_is_sanitized_and_never_healthy(self):
        with patch(
            "apps.support.progress.state_detector_dependencies.SessionProgressCalculator._aggregate_exam_results",
            side_effect=RuntimeError("private input"),
        ):
            report = self.scan()
        self.assertEqual(report["inspection_status"], "failed")
        self.assertEqual(report["state"], "unknown")
        self.assertNotIn("private", json.dumps(report))

    def test_accidental_business_write_is_refused_and_rolled_back(self):
        def writer(**kwargs):
            self.SessionProgress.objects.filter(pk=self.progress.pk).update(exam_passed=True)

        with patch(
            "apps.support.progress.state_detector_dependencies.SessionProgressCalculator._aggregate_exam_results",
            side_effect=writer,
        ):
            self.assertIn("business_write_refused", self.scan()["errors"])
        self.progress.refresh_from_db()
        self.assertFalse(self.progress.exam_passed)

    @override_settings(DEV_ALERTS_WEBHOOK_URL="https://receiver.invalid")
    def test_unknown_delivery_is_not_blindly_retried_or_called_recovered(self):
        self.make_contradiction()
        with patch("apps.core.services.state_detector._deliver", return_value="unknown") as deliver:
            self.assertEqual(self.run_monitor()["delivery_status"], "unknown")
            self.SessionProgress.objects.filter(pk=self.progress.pk).update(exam_passed=False)
            self.assertIn("delivery_reconciliation_required", self.run_monitor()["errors"])
            deliver.assert_called_once()

    @override_settings(DEV_ALERTS_WEBHOOK_URL="https://receiver.invalid")
    def test_failure_to_record_acknowledgment_preserves_pending_and_blocks_resend(self):
        from apps.core.services.state_detector import _receipt

        self.make_contradiction()
        calls = []

        def fail_second_receipt(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("Do not emit private failure text")
            return _receipt(*args, **kwargs)

        with (
            patch("apps.core.services.state_detector._receipt", side_effect=fail_second_receipt),
            patch("apps.core.services.state_detector._deliver", return_value="delivered") as deliver,
        ):
            report = self.run_monitor()
            self.assertEqual(report["delivery_status"], "unknown")
            self.assertNotIn("private", json.dumps(report))
            self.assertIn("delivery_reconciliation_required", self.run_monitor()["errors"])
            deliver.assert_called_once()

    def test_real_loopback_503_then_200_keeps_failure_and_suppresses_duplicate(self):
        received = []

        class Receiver(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(503 if len(received) == 1 else 200)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.make_contradiction()
        try:
            with override_settings(DEV_ALERTS_WEBHOOK_URL=f"http://127.0.0.1:{server.server_port}"):
                failed = self.run_monitor()
                delivered = self.run_monitor()
                self.assertEqual(failed["delivery_status"], "failed")
                self.assertEqual(delivered["delivery_status"], "delivered")
                self.assertEqual(failed["event_id"], delivered["event_id"])
                self.assertEqual(self.run_monitor()["delivery_status"], "suppressed")
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)
        self.assertEqual(len(received), 2)
        self.assertNotIn(self.student.name, json.dumps(received))
        self.assertTrue(OpsAuditLog.objects.filter(payload__delivery_status="failed").exists())

    @skipUnless(connection.vendor == "postgresql", "PostgreSQL transaction contract")
    def test_postgresql_snapshot_is_read_only_and_rejects_nested_transactions(self):
        with _read_snapshot(), connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('transaction_read_only'), current_setting('transaction_isolation')")
            self.assertEqual(cursor.fetchone(), ("on", "repeatable read"))
        with transaction.atomic():
            with self.assertRaisesMessage(InspectionFailure, "nested_snapshot_refused"), _read_snapshot():
                pass

    @skipUnless(connection.vendor == "postgresql", "PostgreSQL cross-connection dedup contract")
    def test_postgresql_lock_refuses_competing_connection_and_releases_on_exception(self):
        other_connection = connection.copy()
        try:
            with _exclusive_monitor(self.tenant.id), other_connection.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_lock(%s)", [_lock_key(self.tenant.id)])
                self.assertFalse(cursor.fetchone()[0])
            with self.assertRaises(ValueError), _exclusive_monitor(self.tenant.id):
                raise ValueError("test")
            with other_connection.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_lock(%s)", [_lock_key(self.tenant.id)])
                self.assertTrue(cursor.fetchone()[0])
                with patch("apps.core.services.state_detector._deliver") as deliver:
                    self.assertIn("monitor_busy", run_state_detector(tenant_id=self.tenant.id)["errors"])
                    deliver.assert_not_called()
                    self.assertEqual(OpsAuditLog.objects.count(), 0)
                cursor.execute("SELECT pg_advisory_unlock(%s)", [_lock_key(self.tenant.id)])
        finally:
            other_connection.close()
