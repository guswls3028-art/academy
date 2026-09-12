"""Actual PostgreSQL + DRF regression; original RED source retained in the task artifact."""
import threading
import time
import uuid
from queue import Queue
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.test import TransactionTestCase
from django.utils import timezone
from django.urls import resolve
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.core.models import Tenant, TenantMembership
from apps.domains.attendance.models import Attendance
from apps.domains.enrollment.models import Enrollment, SessionEnrollment
from apps.domains.lectures.models import Lecture, Session
from apps.domains.students.models import Student
from apps.domains.video.drm import create_playback_token
from apps.domains.video.models import Video, VideoPlaybackEvent, VideoPlaybackEventBatch, VideoPlaybackSession
from apps.domains.video.services import playback_event_batch, playback_session
from apps.domains.video.views import playback_views


@skipUnless(connection.vendor == "postgresql", "Actual PostgreSQL ordering proof")
class TestVideoFinalEventAtomicity(TransactionTestCase):
    def setUp(self):
        self.redis = patch(
            "apps.domains.video.services.playback_session.is_redis_available",
            return_value=False,
        )
        self.redis.start()
        self.addCleanup(self.redis.stop)
        self.tenant = Tenant.objects.create(code="qa-video-final", name="Synthetic video")
        self.user = get_user_model().objects.create_user(
            username="qa-video-final-student", tenant=self.tenant,
        )
        self.student = Student.objects.create(
            tenant=self.tenant, user=self.user, name="Synthetic student",
            ps_number="QVFINAL", omr_code="QVFINAL1", school_type="HIGH",
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=self.user, role="student")
        self.lecture = Lecture.objects.create(
            tenant=self.tenant, title="Synthetic lecture", name="Synthetic", subject="MATH",
        )
        self.lesson = Session.objects.create(lecture=self.lecture, title="Synthetic", order=1)
        self.enrollment = Enrollment.objects.create(
            tenant=self.tenant, student=self.student, lecture=self.lecture, status="ACTIVE",
        )
        SessionEnrollment.objects.create(
            tenant=self.tenant, session=self.lesson, enrollment=self.enrollment,
        )
        Attendance.objects.create(
            tenant=self.tenant, session=self.lesson, enrollment=self.enrollment, status="ONLINE",
        )
        self.video = Video.objects.create(
            tenant=self.tenant, session=self.lesson, title="Synthetic video",
            status=Video.Status.READY, duration=300,
        )
        policy = playback_views.VideoPlaybackMixin()._effective_policy(
            video=self.video, enrollment=self.enrollment,
        )
        self.assertEqual(policy["access_mode"], "PROCTORED_CLASS")
        self.playback = VideoPlaybackSession.objects.create(
            video=self.video, enrollment=self.enrollment, session_id="qa-exact-session",
            device_id="qa-exact-device", expires_at=timezone.now() + timedelta(minutes=10),
            event_protocol_version=2,
        )
        self.claims = {
            "tenant_id": self.tenant.id, "user_id": self.user.id, "student_id": self.student.id,
            "video_id": self.video.id, "enrollment_id": self.enrollment.id,
            "session_id": self.playback.session_id, "monitoring_enabled": True,
            "access_mode": "PROCTORED_CLASS", "pv": self.video.policy_version,
            "event_protocol_version": 2,
        }
        self.token = create_playback_token(payload=self.claims, ttl_seconds=600)
        self.batch = {"batch_id": str(uuid.uuid4()), "events": [{"type": "FULLSCREEN_ENTER", "payload": {}}]}

    def request(self, kind, *, tenant=None, token=None, data=None, legacy=False, user=None, child=None, batches=None):
        prefix = "" if legacy or kind in ("heartbeat", "renew", "refresh") else "v2/"
        path = f"/api/v1/media/playback/{prefix}{kind}/"
        body = {"token": token or self.token}
        if kind == "events":
            body.update({"events": self.batch["events"]} if legacy else {"batch": self.batch})
        elif kind == "end" and not legacy:
            body["batches"] = batches if batches is not None else []
        request = APIRequestFactory().post(
            path, body if data is None else data,
            format="json",
        )
        request.tenant = tenant or self.tenant
        if child is not None:
            request.META["HTTP_X_STUDENT_ID"] = str(child)
        force_authenticate(request, user=user or self.user)
        route = resolve(path)
        response = route.func(request, *route.args, **route.kwargs)
        return response.status_code, dict(response.data)

    def thread_request(self, kind, *, pid_queue=None, **kwargs):
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET statement_timeout = '10s'")
                cursor.execute("SELECT pg_backend_pid()")
                pid = cursor.fetchone()[0]
            if pid_queue is not None:
                pid_queue.put(pid)
            if kind == "lecture-close":
                return pid, self.close_lecture()
            if kind == "expiry":
                changed = playback_views.video_repo.playback_session_update_expired(timezone.now() + timedelta(hours=1))
                return pid, (200, {"changed": changed})
            return pid, self.request(kind, **kwargs)
        finally:
            connection.close()

    def snapshot(self):
        self.playback.refresh_from_db()
        return {
            "status": self.playback.status, "counter": self.playback.total_count,
            "audit_rows": VideoPlaybackEvent.objects.filter(session_id=self.playback.session_id).count(),
        }

    def test_ordered_event_then_end_persists_exact_audit(self):
        code, data = self.request("events")
        self.assertEqual(code, 201, data)
        self.assertEqual(data["inserted_count"], 1)
        self.assertEqual(VideoPlaybackEvent.objects.get().policy_snapshot["access_mode"], "PROCTORED_CLASS")
        for _ in range(2):
            code, data = self.request("end", batches=[self.batch])
            self.assertEqual(code, 200, data)
            self.assertEqual(data["inserted_count"], 0)
            self.assertEqual(data["acknowledgements"], [{"batch_id": self.batch["batch_id"], "event_count": 1, "duplicate": True}])
            self.assertEqual(self.snapshot(), {"status": "ENDED", "counter": 1, "audit_rows": 1})
            self.assertEqual(self.playback.event_batches.count(), 1)

    def test_new_unsubmitted_event_after_end_remains_rejected(self):
        self.assertEqual(self.request("end")[0], 200)
        self.assertEqual(self.request("events"), (409, {"detail": "session_inactive"}))
        self.assertEqual(self.snapshot(), {"status": "ENDED", "counter": 0, "audit_rows": 0})

    def test_foreign_tenant_cannot_submit_or_end(self):
        foreign = Tenant.objects.create(code="qa-video-foreign", name="Synthetic other")
        for kind in ("events", "end"):
            self.assertEqual(self.request(kind, tenant=foreign), (403, {"detail": "token_tenant_mismatch"}))
        self.assertEqual(self.snapshot(), {"status": "ACTIVE", "counter": 0, "audit_rows": 0})

    def test_submitted_request_overtaken_before_session_admission_keeps_audit(self):
        """Final payload persists the unresolved batch; delayed ordinary request is a duplicate."""
        entered, release = threading.Event(), threading.Event()
        original = playback_event_batch._lock_write_scope

        def pause_before_admission(*args, **kwargs):
            if not entered.is_set():
                entered.set()
                if not release.wait(15):
                    raise AssertionError("event admission barrier did not release")
            return original(*args, **kwargs)

        with patch.object(playback_event_batch, "_lock_write_scope", side_effect=pause_before_admission):
            with ThreadPoolExecutor(max_workers=2) as pool:
                event_future = pool.submit(self.thread_request, "events")
                self.assertTrue(entered.wait(10), "actual event endpoint never reached session admission")
                try:
                    end_pid, end_result = pool.submit(self.thread_request, "end", batches=[self.batch]).result(timeout=10)
                    at_end = self.snapshot()
                finally:
                    release.set()
                event_pid, event_result = event_future.result(timeout=10)
        self.assertNotEqual(event_pid, end_pid)
        self.assertEqual(end_result[0], 200, end_result)
        self.assertEqual(end_result[1]["inserted_count"], 1)
        self.assertEqual(at_end, {"status": "ENDED", "counter": 1, "audit_rows": 1})
        self.assertEqual(event_result[0], 201, event_result)
        self.assertEqual(event_result[1]["inserted_count"], 0)
        self.assertTrue(event_result[1]["acknowledgements"][0]["duplicate"])
        self.assertEqual(self.playback.event_batches.count(), 1)

    def test_admitted_batch_and_end_do_not_publish_partial_final_audit(self):
        """Corrected GREEN choreography: end blocks until audit/counter transaction commits."""
        entered, release = threading.Event(), threading.Event()
        original = playback_views.video_repo.playback_event_bulk_create

        def pause_before_audit_rows(*args, **kwargs):
            entered.set()
            if not release.wait(15):
                raise AssertionError("audit insertion barrier did not release")
            return original(*args, **kwargs)

        with patch.object(playback_views.video_repo, "playback_event_bulk_create", side_effect=pause_before_audit_rows):
            with ThreadPoolExecutor(max_workers=2) as pool:
                event_future = pool.submit(self.thread_request, "events")
                self.assertTrue(entered.wait(10), "actual event endpoint never reached audit insertion")
                pids = Queue()
                end_future = pool.submit(self.thread_request, "end", pid_queue=pids)
                end_pid = pids.get(timeout=5)
                try:
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        with connection.cursor() as cursor:
                            cursor.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s", [end_pid])
                            row = cursor.fetchone()
                        if row and row[0] == "Lock":
                            break
                        time.sleep(0.02)
                    else:
                        self.fail("actual end request did not wait on the PostgreSQL session lock")
                    self.assertFalse(end_future.done())
                    at_end = self.snapshot()
                finally:
                    release.set()
                event_pid, event_result = event_future.result(timeout=10)
                _, end_result = end_future.result(timeout=10)
        self.assertNotEqual(event_pid, end_pid)
        self.assertEqual(end_result[0], 200, end_result)
        self.assertEqual(event_result[0], 201, event_result)
        self.assertEqual(self.snapshot(), {"status": "ENDED", "counter": 1, "audit_rows": 1})
        self.assertEqual(at_end, {"status": "ACTIVE", "counter": 0, "audit_rows": 0})

    def test_concurrent_duplicate_final_packets_insert_once(self):
        gate = threading.Barrier(2)

        def finish():
            gate.wait(5)
            return self.thread_request("end", batches=[self.batch])

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(finish), pool.submit(finish)
            results = [first.result(timeout=10), second.result(timeout=10)]
        self.assertNotEqual(results[0][0], results[1][0])
        self.assertEqual([result[1][0] for result in results], [200, 200], results)
        self.assertEqual(sum(result[1][1]["inserted_count"] for result in results), 1)
        self.assertEqual(self.snapshot(), {"status": "ENDED", "counter": 1, "audit_rows": 1})
        self.assertEqual(self.playback.event_batches.count(), 1)

    def test_failed_insert_rolls_back_receipt_audit_counters_end_then_retry(self):
        with patch.object(playback_views.video_repo, "playback_event_bulk_create", side_effect=IntegrityError("synthetic insert failure")):
            with self.assertLogs(playback_views.logger, level="ERROR"):
                code, data = self.request("end", batches=[self.batch])
        self.assertEqual(code, 503)
        self.assertEqual(data["detail"], "playback_events_unavailable")
        self.assertTrue(data["request_id"])
        self.assertEqual(self.snapshot(), {"status": "ACTIVE", "counter": 0, "audit_rows": 0})
        self.assertEqual(VideoPlaybackEventBatch.objects.count(), 0)
        self.assertEqual(self.request("end", batches=[self.batch])[0], 200)
        self.assertEqual(self.snapshot(), {"status": "ENDED", "counter": 1, "audit_rows": 1})

    def test_policy_failure_does_not_save_empty_policy_or_end(self):
        with patch.object(playback_event_batch, "build_effective_playback_policy", side_effect=RuntimeError("synthetic policy failure")):
            with self.assertLogs(playback_views.logger, level="ERROR"):
                self.assertEqual(self.request("end", batches=[self.batch])[0], 503)
        self.assertEqual(self.snapshot(), {"status": "ACTIVE", "counter": 0, "audit_rows": 0})
        self.assertEqual(VideoPlaybackEventBatch.objects.count(), 0)

    def test_changed_payload_for_known_id_conflicts_without_end(self):
        self.assertEqual(self.request("events")[0], 201)
        changed = {**self.batch, "events": [{"type": "FULLSCREEN_EXIT"}]}
        self.assertEqual(self.request("end", batches=[changed]), (409, {"detail": "batch_payload_conflict"}))
        self.assertEqual(self.snapshot(), {"status": "ACTIVE", "counter": 1, "audit_rows": 1})

    def test_multibatch_final_acknowledges_known_and_inserts_only_missing(self):
        self.assertEqual(self.request("events")[0], 201)
        batches = [self.batch, {"batch_id": str(uuid.uuid4()), "events": [{"type": "FULLSCREEN_EXIT"}]}]
        code, data = self.request("end", batches=batches)
        self.assertEqual(code, 200, data)
        self.assertEqual(data["inserted_count"], 1)
        self.assertEqual([ack["duplicate"] for ack in data["acknowledgements"]], [True, False])
        self.assertEqual(self.snapshot(), {"status": "ENDED", "counter": 2, "audit_rows": 2})
        self.assertEqual(self.playback.event_batches.count(), 2)

    def test_threshold_batch_records_evidence_and_never_downgrades_revoked(self):
        batch = {**self.batch, "events": [{"type": "SEEK_ATTEMPT"}]}
        for _ in range(2):
            code, data = self.request("end", batches=[batch])
            self.assertEqual(code, 200, data)
            self.assertEqual(data["session_status"], "REVOKED")
            self.assertEqual(self.snapshot(), {"status": "REVOKED", "counter": 1, "audit_rows": 1})
            self.assertEqual(self.playback.violated_count, 1)
        self.assertEqual(self.request("events", data={"token": self.token, "batch": {**self.batch, "batch_id": str(uuid.uuid4())}})[0], 409)

    def test_expiry_and_stale_redis_cannot_resurrect_session(self):
        from django.core.management import call_command

        self.playback.expires_at = timezone.now() - timedelta(seconds=1)
        self.playback.save(update_fields=["expires_at"])
        with patch.object(playback_session, "is_redis_available", side_effect=AssertionError("v2 Redis access")):
            self.assertEqual(self.request("events")[0], 409)
            self.assertEqual(self.request("heartbeat")[0], 409)
            self.assertFalse(playback_session.is_session_active(student_id=self.student.id, session_id=self.playback.session_id))
        call_command("expire_playback_sessions", verbosity=0)
        self.assertEqual(self.snapshot(), {"status": "EXPIRED", "counter": 0, "audit_rows": 0})

    def test_renewal_preserves_protocol_and_v2_never_imports_redis_stats(self):
        from apps.domains.video.drm import verify_playback_token

        with patch.object(playback_session, "is_redis_available", side_effect=AssertionError("v2 Redis access")), patch(
            "academy.adapters.cache.redis_playback_session_buffer.buffer_heartbeat_session_ttl", side_effect=AssertionError("v2 Redis TTL"),
        ):
            self.assertEqual(self.request("heartbeat")[0], 200)
            code, data = self.request("renew")
            self.assertEqual(code, 200, data)
            self.assertEqual(data["event_protocol_version"], 2)
            valid, payload, error = verify_playback_token(data["playback_token"])
            self.assertTrue(valid, error)
            self.assertEqual(payload["event_protocol_version"], 2)
            self.assertEqual(payload["session_id"], self.playback.session_id)
            self.assertEqual(self.request("end", token=data["playback_token"], batches=[self.batch])[0], 200)
            self.assertEqual(playback_session.get_session_violation_stats(session_id=self.playback.session_id), {"total": 1, "violated": 0})
            self.assertFalse(playback_session.is_session_active(student_id=self.student.id, session_id=self.playback.session_id))
            playback_session.revoke_session(student_id=self.student.id, session_id=self.playback.session_id)
            self.assertEqual(self.request("end", batches=[self.batch])[0], 200)
        self.assertEqual(self.snapshot(), {"status": "ENDED", "counter": 1, "audit_rows": 1})

    def test_known_v2_wrong_student_never_falls_back_to_redis(self):
        from rest_framework.exceptions import PermissionDenied

        with patch.object(playback_session, "is_redis_available", side_effect=AssertionError("wrong-owner Redis access")):
            for method, extra in (
                (playback_session.end_session, {}), (playback_session.revoke_session, {}),
                (playback_session.is_session_active, {}), (playback_session.heartbeat_session, {"ttl_seconds": 600}),
                (playback_session.record_session_event, {"violated": False}),
            ):
                with self.subTest(method=method.__name__), self.assertRaises(PermissionDenied):
                    method(student_id=self.student.id + 10000, session_id=self.playback.session_id, **extra)
        self.assertEqual(self.snapshot(), {"status": "ACTIVE", "counter": 0, "audit_rows": 0})

    def test_legacy_urls_refuse_v2_even_when_claim_removed(self):
        claims = {key: value for key, value in self.claims.items() if key != "event_protocol_version"}
        stripped = create_playback_token(payload=claims, ttl_seconds=600)
        for token in (self.token, stripped):
            for kind in ("events", "end"):
                self.assertEqual(self.request(kind, legacy=True, token=token), (409, {"detail": "event_protocol_mismatch"}))
        for method, extra in ((playback_session.end_session, {}), (playback_session.record_session_event, {"violated": False})):
            with self.assertRaises(ValueError):
                method(student_id=self.student.id, session_id=self.playback.session_id, **extra)

    def test_legacy_missing_claim_and_existing_redis_flush_semantics_unchanged(self):
        self.playback.event_protocol_version = 1
        self.playback.save(update_fields=["event_protocol_version"])
        token = create_playback_token(payload={key: value for key, value in self.claims.items() if key != "event_protocol_version"}, ttl_seconds=600)
        self.assertEqual(self.request("events", legacy=True, token=token), (201, {"stored": 1}))
        self.assertEqual(self.request("end", legacy=True, token=token), (200, {"ok": True}))
        with patch.object(playback_session, "is_redis_available", return_value=True), patch.object(
            playback_session, "get_session_violation_stats_redis", return_value={"total": 7, "violated": 2},
        ) as stats, patch.object(playback_session, "flush_session_stats") as flush, patch.object(playback_session, "flush_session_buffer"):
            self.assertEqual(self.request("end", legacy=True, token=token), (200, {"ok": True}))
            stats.assert_called_once_with(self.playback.session_id)
            flush.assert_called_once_with(self.playback.session_id)
        self.playback.refresh_from_db()
        self.assertEqual((self.playback.status, self.playback.total_count, self.playback.violated_count), ("ENDED", 7, 2))
        self.assertEqual(VideoPlaybackEventBatch.objects.count(), 0)

    def test_old_process_insert_uses_persistent_db_default_and_invalid_versions_fail(self):
        with connection.cursor() as cursor:
            cursor.execute("""INSERT INTO video_videoplaybacksession
                (created_at, updated_at, video_id, enrollment_id, session_id, device_id, status, started_at,
                 total_count, violated_count, is_revoked)
                VALUES (NOW(), NOW(), %s, %s, 'qa-old-insert', 'qa-old-device', 'ACTIVE', NOW(), 0, 0, FALSE)
                RETURNING event_protocol_version""", [self.video.id, self.enrollment.id])
            self.assertEqual(cursor.fetchone()[0], 1)
            cursor.execute("SELECT column_default FROM information_schema.columns WHERE table_name='video_videoplaybacksession' AND column_name='event_protocol_version'")
            self.assertIn("1", cursor.fetchone()[0])
        for version in (0, 3):
            with self.assertRaises(IntegrityError), transaction.atomic():
                VideoPlaybackSession.objects.filter(pk=self.playback.pk).update(event_protocol_version=version)

    def test_claim_user_student_video_enrollment_mismatches_are_denied(self):
        outsider = get_user_model().objects.create_user(username="qa-video-outsider", tenant=self.tenant)
        for kind in ("events", "end"):
            self.assertEqual(self.request(kind, user=outsider)[0], 403)
            for field in ("student_id", "video_id", "enrollment_id"):
                token = create_playback_token(payload={**self.claims, field: self.claims[field] + 10000}, ttl_seconds=600)
                self.assertEqual(self.request(kind, token=token)[0], 403)
        self.assertEqual(self.snapshot(), {"status": "ACTIVE", "counter": 0, "audit_rows": 0})

    def test_parent_exact_child_positive_and_sibling_denial(self):
        from apps.domains.parents.models import Parent

        user = get_user_model().objects.create_user(username="qa-video-parent", tenant=self.tenant)
        TenantMembership.ensure_active(tenant=self.tenant, user=user, role="parent")
        parent = Parent.objects.create(tenant=self.tenant, user=user, name="Synthetic parent", phone="01000000001")
        self.student.parent = parent
        self.student.save(update_fields=["parent"])
        sibling_user = get_user_model().objects.create_user(username="qa-video-sibling", tenant=self.tenant)
        sibling = Student.objects.create(tenant=self.tenant, user=sibling_user, parent=parent, name="Synthetic sibling", ps_number="SIB", omr_code="SIB00001", school_type="HIGH")
        token = create_playback_token(payload={**self.claims, "user_id": user.id}, ttl_seconds=600)
        self.assertEqual(self.request("events", user=user, token=token)[0], 403)
        self.assertEqual(self.request("events", user=user, token=token, child=sibling.id)[0], 403)
        self.assertEqual(self.request("events", user=user, token=token, child=self.student.id)[0], 201)
        self.assertEqual(self.request("end", user=user, token=token, child=self.student.id, batches=[self.batch])[0], 200)
        self.assertEqual(self.snapshot(), {"status": "ENDED", "counter": 1, "audit_rows": 1})

    def test_request_bounds_missing_final_and_unknown_fields_never_partially_save(self):
        batches = [
            [self.batch, self.batch],
            [{**self.batch, "events": self.batch["events"] * 51}],
            [{**self.batch, "events": [{"type": "PLAYER_ERROR", "payload": {"message": "x" * 8192}}]}],
            [{"batch_id": str(uuid.uuid4()), "events": self.batch["events"] * 50} for _ in range(5)],
            [{"batch_id": str(uuid.uuid4()), "events": self.batch["events"]} for _ in range(9)],
        ]
        for value in batches:
            self.assertEqual(self.request("end", batches=value)[0], 400)
        self.assertEqual(self.request("end", data={"token": self.token})[0], 400)
        self.assertEqual(self.request("end", data={"token": self.token, "batches": [], "unknown": True})[0], 400)
        self.assertEqual(self.request("end", data={"token": self.token, "batches": [], "unknown": "x" * (48 * 1024)})[0], 413)
        self.assertEqual(self.snapshot(), {"status": "ACTIVE", "counter": 0, "audit_rows": 0})
        self.assertEqual(VideoPlaybackEventBatch.objects.count(), 0)

    def test_maximum_final_packet_saves_all_200_events_without_truncation(self):
        batches = [{"batch_id": str(uuid.uuid4()), "events": self.batch["events"] * 50} for _ in range(4)]
        code, data = self.request("end", batches=batches)
        self.assertEqual(code, 200, data)
        self.assertEqual(data["inserted_count"], 200)
        self.assertEqual(sum(ack["event_count"] for ack in data["acknowledgements"]), 200)
        self.assertEqual(self.snapshot(), {"status": "ENDED", "counter": 200, "audit_rows": 200})
        self.assertEqual(self.playback.event_batches.count(), 4)

    def test_supported_deletion_between_reference_and_lock_returns_scope_denial(self):
        original = playback_event_batch._lock_write_scope

        def delete_then_lock(*args, **kwargs):
            VideoPlaybackSession.objects.filter(pk=self.playback.pk).delete()
            return original(*args, **kwargs)

        with patch.object(playback_event_batch, "_lock_write_scope", side_effect=delete_then_lock):
            self.assertEqual(self.request("end", batches=[self.batch]), (403, {"detail": "session_scope_mismatch"}))
        self.assertEqual(VideoPlaybackEventBatch.objects.count(), 0)

    def test_real_grant_opt_in_default_and_free_review_contract(self):
        from apps.support.student_app.video_media import issue_playback_access_grant

        args = {"video": self.video, "enrollment": self.enrollment, "user": self.user, "device_id": "qa-new-grant"}
        with patch.object(playback_session, "init_session_redis") as init:
            grant = issue_playback_access_grant(**args, event_protocol_version=2)
            self.assertIsNone(grant.error)
            self.assertEqual(grant.event_protocol_version, 2)
            self.assertEqual(VideoPlaybackSession.objects.get(session_id=grant.session_id).event_protocol_version, 2)
            init.assert_not_called()
            grant = issue_playback_access_grant(**args)
            self.assertEqual(grant.event_protocol_version, 1)
            init.assert_called_once()
        Attendance.objects.filter(enrollment=self.enrollment).update(status="PRESENT")
        with patch.object(playback_session, "init_session_redis") as init:
            grant = issue_playback_access_grant(**args, event_protocol_version=2)
        self.assertEqual(grant.access_mode, "FREE_REVIEW")
        self.assertIsNone(grant.session_id)
        self.assertEqual(grant.event_protocol_version, 1)
        init.assert_not_called()

    def test_student_bootstrap_get_is_pure_post_echoes_v2(self):
        from apps.domains.student_app.media.views import StudentVideoPlaybackView

        self.video.hls_path = "qa-fixtures/video/master.m3u8"
        self.video.save(update_fields=["hls_path"])
        path = f"/api/v1/student/video/videos/{self.video.id}/playback/?enrollment={self.enrollment.id}"
        count = VideoPlaybackSession.objects.count()
        for method in ("get", "post"):
            request = (APIRequestFactory().get(path + "&access_check=1") if method == "get" else APIRequestFactory().post(path, {"event_protocol_version": 2}, format="json"))
            request.tenant = self.tenant
            force_authenticate(request, user=self.user)
            response = StudentVideoPlaybackView.as_view()(request, video_id=self.video.id)
            self.assertEqual(response.status_code, 200, response.data)
            if method == "get":
                self.assertEqual(VideoPlaybackSession.objects.count(), count)
            else:
                self.assertEqual(response.data["event_protocol_version"], 2)
                self.assertEqual(VideoPlaybackSession.objects.get(session_id=response.data["playback_session_id"]).event_protocol_version, 2)

    def test_public_lecture_close_preserves_audit_and_known_receipt(self):
        self.assertEqual(self.request("events")[0], 201)
        code, data = self.close_lecture()
        self.assertEqual(code, 200, data)
        self.assertEqual(self.snapshot(), {"status": "REVOKED", "counter": 1, "audit_rows": 1})
        self.assertEqual(self.request("end", batches=[self.batch])[0], 200)
        self.assertEqual(self.snapshot(), {"status": "REVOKED", "counter": 1, "audit_rows": 1})

    def close_lecture(self):
        from apps.domains.lectures.views import LectureViewSet

        teacher = get_user_model().objects.create_user(username="qa-video-teacher", tenant=self.tenant, is_staff=True)
        TenantMembership.ensure_active(tenant=self.tenant, user=teacher, role="teacher")
        request = APIRequestFactory().patch(f"/api/v1/lectures/{self.lecture.id}/", {"is_active": False}, format="json")
        request.tenant = self.tenant
        force_authenticate(request, user=teacher)
        response = LectureViewSet.as_view({"patch": "partial_update"})(request, pk=self.lecture.id)
        return response.status_code, dict(response.data)

    def assert_lifecycle_waits_for_admitted_batch(self, kind, expected_status):
        entered, release = threading.Event(), threading.Event()
        original = playback_views.video_repo.playback_event_bulk_create

        def pause_insert(*args, **kwargs):
            entered.set()
            if not release.wait(15):
                raise AssertionError("lifecycle audit barrier did not release")
            return original(*args, **kwargs)

        with patch.object(playback_views.video_repo, "playback_event_bulk_create", side_effect=pause_insert):
            with ThreadPoolExecutor(max_workers=2) as pool:
                event_future = pool.submit(self.thread_request, "events")
                self.assertTrue(entered.wait(10))
                pids = Queue()
                lifecycle_future = pool.submit(self.thread_request, kind, pid_queue=pids)
                pid = pids.get(timeout=5)
                try:
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        with connection.cursor() as cursor:
                            cursor.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s", [pid])
                            row = cursor.fetchone()
                        if row and row[0] == "Lock":
                            break
                        time.sleep(0.02)
                    else:
                        self.fail("the competing lifecycle did not wait on a real PostgreSQL lock")
                    self.assertFalse(lifecycle_future.done())
                    self.assertEqual(self.snapshot(), {"status": "ACTIVE", "counter": 0, "audit_rows": 0})
                finally:
                    release.set()
                event_pid, event_response = event_future.result(timeout=10)
                _, lifecycle_response = lifecycle_future.result(timeout=10)
        self.assertNotEqual(event_pid, pid)
        self.assertEqual(event_response[0], 201)
        self.assertEqual(lifecycle_response[0], 200)
        self.assertEqual(self.snapshot(), {"status": expected_status, "counter": 1, "audit_rows": 1})

    def test_public_lecture_close_waits_for_admitted_batch(self):
        self.assert_lifecycle_waits_for_admitted_batch("lecture-close", "REVOKED")

    def test_public_renew_waits_for_admitted_batch(self):
        self.assert_lifecycle_waits_for_admitted_batch("renew", "ACTIVE")

    def test_expiry_worker_update_waits_for_admitted_batch(self):
        self.assert_lifecycle_waits_for_admitted_batch("expiry", "EXPIRED")

    def test_inactive_heartbeat_cannot_extend_signed_entitlement_ceiling(self):
        from django.core.management import call_command
        from apps.domains.video.models import InactiveVideoEntitlement
        from apps.support.student_app.video_media import issue_playback_access_grant

        self.playback.status = "ENDED"
        self.playback.save(update_fields=["status"])
        self.tenant.video_max_sessions = 1
        self.tenant.save(update_fields=["video_max_sessions"])
        self.enrollment.status = "INACTIVE"
        self.enrollment.save(update_fields=["status"])
        entitlement = InactiveVideoEntitlement.objects.create(
            tenant=self.tenant, student=self.student, enrollment=self.enrollment, video=self.video,
            access_mode="PROCTORED_CLASS", source="STAFF_AUTHORIZATION", source_reference="qa-exact",
            reason="Synthetic explicitly authorized video", granted_by_reference="qa-teacher",
            expires_at=timezone.now() + timedelta(seconds=45),
        )
        grant = issue_playback_access_grant(
            video=self.video, enrollment=self.enrollment, user=self.user, device_id="qa-inactive",
            event_protocol_version=2,
        )
        self.assertIsNone(grant.error)
        self.assertEqual(grant.event_protocol_version, 2)
        with patch.object(playback_session, "is_redis_available", side_effect=AssertionError("v2 Redis access")):
            self.assertEqual(self.request("heartbeat", token=grant.token)[0], 200)
        session = VideoPlaybackSession.objects.get(session_id=grant.session_id)
        self.assertLessEqual(session.expires_at.timestamp(), grant.expires_at)
        self.assertLessEqual(session.expires_at, entitlement.expires_at)
        code, data = self.request("renew", token=grant.token)
        self.assertEqual(code, 200)
        self.assertLessEqual(data["playback_expires_at"], int(entitlement.expires_at.timestamp()))
        after_expiry = entitlement.expires_at + timedelta(seconds=1)
        with patch("django.utils.timezone.now", return_value=after_expiry):
            self.assertEqual(self.request("heartbeat", token=grant.token)[0], 403)
            call_command("expire_playback_sessions", verbosity=0)
            session.refresh_from_db()
            self.assertEqual(session.status, "EXPIRED")
            # The same student becomes normally enrolled again: the expired lease
            # must not consume the sole slot for the next legitimate grant.
            self.enrollment.status = "ACTIVE"
            self.enrollment.save(update_fields=["status"])
            next_grant = issue_playback_access_grant(
                video=self.video, enrollment=self.enrollment, user=self.user,
                device_id="qa-normal-after-expiry", event_protocol_version=2,
            )
            self.assertIsNone(next_grant.error)
            self.assertIsNotNone(next_grant.session_id)
            self.assertNotEqual(next_grant.session_id, grant.session_id)

    def test_v2_followups_require_exact_parent_child_and_token_graph(self):
        for kind in ("heartbeat", "refresh", "renew"):
            for field in ("student_id", "video_id", "enrollment_id"):
                token = create_playback_token(payload={**self.claims, field: self.claims[field] + 10000}, ttl_seconds=600)
                self.assertEqual(self.request(kind, token=token)[0], 403)

    def test_delayed_old_heartbeat_does_not_shorten_renewed_lease(self):
        old_expiry = int((timezone.now() + timedelta(seconds=45)).timestamp())
        old_token = create_playback_token(payload=self.claims, expires_at=old_expiry)
        self.playback.expires_at = timezone.datetime.fromtimestamp(old_expiry, tz=timezone.get_default_timezone())
        self.playback.save(update_fields=["expires_at"])
        code, renewed = self.request("renew", token=old_token)
        self.assertEqual(code, 200)
        self.assertGreater(renewed["playback_expires_at"], old_expiry)
        self.playback.refresh_from_db()
        renewed_lease = self.playback.expires_at
        self.assertEqual(self.request("heartbeat", token=old_token)[0], 200)
        self.playback.refresh_from_db()
        self.assertEqual(self.playback.expires_at, renewed_lease, "old heartbeat reversed an already committed renewal")

    def test_renew_waits_for_inflight_heartbeat_without_lease_clobber(self):
        entered, release = threading.Event(), threading.Event()
        original = VideoPlaybackSession.save

        def pause_heartbeat(instance, *args, **kwargs):
            fields = kwargs.get("update_fields") or []
            if "last_seen" in fields and "updated_at" not in fields:
                entered.set()
                if not release.wait(15):
                    raise AssertionError("heartbeat barrier did not release")
            return original(instance, *args, **kwargs)

        with patch.object(VideoPlaybackSession, "save", new=pause_heartbeat):
            with ThreadPoolExecutor(max_workers=2) as pool:
                heartbeat = pool.submit(self.thread_request, "heartbeat")
                self.assertTrue(entered.wait(10))
                pids = Queue()
                renew = pool.submit(self.thread_request, "renew", pid_queue=pids)
                pid = pids.get(timeout=5)
                try:
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        with connection.cursor() as cursor:
                            cursor.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s", [pid])
                            row = cursor.fetchone()
                        if row and row[0] == "Lock":
                            break
                        time.sleep(0.02)
                    else:
                        self.fail("renew did not wait on the actual heartbeat session lock")
                    self.assertFalse(renew.done())
                finally:
                    release.set()
                heartbeat_pid, heartbeat_result = heartbeat.result(timeout=10)
                _, renew_result = renew.result(timeout=10)
        self.assertNotEqual(heartbeat_pid, pid)
        self.assertEqual(heartbeat_result[0], 200)
        self.assertEqual(renew_result[0], 200)
        self.playback.refresh_from_db()
        self.assertEqual(int(self.playback.expires_at.timestamp()), renew_result[1]["playback_expires_at"])
