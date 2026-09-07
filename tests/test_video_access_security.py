import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate
from rest_framework_simplejwt.tokens import RefreshToken

from apps.core.models import Tenant, TenantMembership
from apps.domains.attendance.models import Attendance
from apps.domains.enrollment.models import Enrollment, SessionEnrollment
from apps.domains.lectures.models import Lecture, Session
from apps.domains.lectures.serializers import LectureSerializer
from apps.domains.lectures.views import LectureViewSet
from apps.domains.students.models import Student
from apps.domains.video.drm import verify_playback_token
from apps.domains.video.models import (
    AccessMode,
    Video,
    VideoAccess,
    VideoFolder,
    VideoPlaybackSession,
)
from academy.application.use_cases.student_video_access_context import (
    StudentVideoAccessContext,
)
from apps.support.student_app.video_media import issue_playback_access_grant
from apps.domains.video.views.playback_views import (
    PlaybackEndView,
    PlaybackEventBatchView,
    PlaybackHeartbeatView,
    PlaybackRefreshView,
    PlaybackRenewView,
    PlaybackStartView,
)


User = get_user_model()


class PlaybackStartStudentEnrollmentAccessTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.tenant = Tenant.objects.create(
            code="playback-start-access",
            name="Playback Start Access",
            is_active=True,
        )
        self.lecture = Lecture.objects.create(
            tenant=self.tenant,
            title="Shared Lecture",
            name="Shared Lecture",
            subject="MATH",
        )
        self.session = Session.objects.create(
            lecture=self.lecture,
            title="Shared Session",
            order=1,
        )
        self.video = Video.objects.create(
            tenant=self.tenant,
            session=self.session,
            title="Shared Video",
            status=Video.Status.READY,
            duration=100,
        )
        self.student_a, self.enrollment_a = self._create_student_enrollment("a", "A")
        self.student_b, self.enrollment_b = self._create_student_enrollment("b", "B")

    def _create_student_enrollment(self, suffix: str, name: str):
        user = User.objects.create_user(
            username=f"t{self.tenant.id}_playback-start-{suffix}",
            password="testpass123",
            tenant=self.tenant,
        )
        student = Student.objects.create(
            tenant=self.tenant,
            user=user,
            name=f"Student {name}",
            ps_number=f"PB-{suffix}",
            omr_code=f"PB{suffix.upper()}0000"[:8],
            parent_phone="01012345678",
            school_type="HIGH",
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=user, role="student")
        enrollment = Enrollment.objects.create(
            tenant=self.tenant,
            student=student,
            lecture=self.lecture,
            status="ACTIVE",
        )
        SessionEnrollment.objects.create(
            tenant=self.tenant,
            session=self.session,
            enrollment=enrollment,
        )
        return student, enrollment

    def _post(self, *, student: Student, enrollment: Enrollment):
        request = self.factory.post(
            "/api/v1/video/playback/start/",
            {
                "video_id": self.video.id,
                "enrollment_id": enrollment.id,
                "device_id": "same-tenant-idor-regression",
            },
            format="json",
        )
        request.tenant = self.tenant
        force_authenticate(request, user=student.user)
        return PlaybackStartView.as_view()(request)

    def _followup(self, view, token):
        request = self.factory.post(
            "/api/v1/video/playback/followup/",
            {"token": token},
            format="json",
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.student_a.user)
        return view(request)

    def test_same_tenant_students_cannot_start_with_each_others_enrollment(self):
        response_a_using_b = self._post(
            student=self.student_a,
            enrollment=self.enrollment_b,
        )
        response_b_using_a = self._post(
            student=self.student_b,
            enrollment=self.enrollment_a,
        )

        self.assertEqual(response_a_using_b.status_code, 403, response_a_using_b.data)
        self.assertEqual(response_b_using_a.status_code, 403, response_b_using_a.data)

    def test_students_start_only_with_their_own_enrollment(self):
        response_a = self._post(student=self.student_a, enrollment=self.enrollment_a)
        response_b = self._post(student=self.student_b, enrollment=self.enrollment_b)

        self.assertEqual(response_a.status_code, 201, response_a.data)
        self.assertEqual(response_b.status_code, 201, response_b.data)
        valid_a, payload_a, error_a = verify_playback_token(response_a.data["token"])
        valid_b, payload_b, error_b = verify_playback_token(response_b.data["token"])
        self.assertTrue(valid_a, error_a)
        self.assertTrue(valid_b, error_b)
        self.assertEqual(payload_a["student_id"], self.student_a.id)
        self.assertEqual(payload_a["enrollment_id"], self.enrollment_a.id)
        self.assertEqual(payload_a["tenant_id"], self.tenant.id)
        self.assertEqual(payload_b["student_id"], self.student_b.id)
        self.assertEqual(payload_b["enrollment_id"], self.enrollment_b.id)
        self.assertEqual(payload_b["tenant_id"], self.tenant.id)

    def test_legacy_blocked_rule_wins_over_free_override(self):
        VideoAccess.objects.create(
            video=self.video,
            enrollment=self.enrollment_a,
            rule="blocked",
            access_mode=AccessMode.FREE_REVIEW,
            is_override=True,
        )

        response = self._post(student=self.student_a, enrollment=self.enrollment_a)

        self.assertEqual(response.status_code, 403, response.data)

    def test_start_rejects_malformed_and_missing_video_ids_without_server_error(self):
        base = {
            "enrollment_id": self.enrollment_a.id,
            "device_id": "invalid-video-id",
        }
        for video_id, expected in (("not-an-id", 400), (-1, 400), (999999999, 404)):
            request = self.factory.post(
                "/api/v1/video/playback/start/",
                {**base, "video_id": video_id},
                format="json",
            )
            request.tenant = self.tenant
            force_authenticate(request, user=self.student_a.user)
            with self.subTest(video_id=video_id):
                response = PlaybackStartView.as_view()(request)
                self.assertEqual(response.status_code, expected, response.data)

    def test_ended_lecture_invalidates_existing_playback_token(self):
        started = self._post(student=self.student_a, enrollment=self.enrollment_a)
        self.assertEqual(started.status_code, 201, started.data)
        active_refresh = self._followup(
            PlaybackRefreshView.as_view(),
            started.data["token"],
        )
        self.assertEqual(active_refresh.status_code, 200, active_refresh.data)

        self.lecture.is_active = False
        self.lecture.save(update_fields=["is_active", "updated_at"])

        for view in (
            PlaybackRefreshView.as_view(),
            PlaybackRenewView.as_view(),
            PlaybackHeartbeatView.as_view(),
        ):
            with self.subTest(view=view):
                response = self._followup(view, started.data["token"])
                self.assertEqual(response.status_code, 403, response.data)
                self.assertEqual(response.data["detail"], "policy_changed")

    def test_renew_rotates_token_without_replacing_the_monitored_session(self):
        Attendance.objects.create(
            tenant=self.tenant,
            session=self.session,
            enrollment=self.enrollment_a,
            status="ONLINE",
        )
        started = self._post(student=self.student_a, enrollment=self.enrollment_a)
        self.assertEqual(started.status_code, 201, started.data)
        original_session_id = started.data["session_id"]
        original_token = started.data["token"]
        self.assertIsNotNone(original_session_id)
        self.assertEqual(VideoPlaybackSession.objects.count(), 1)
        playback_session = VideoPlaybackSession.objects.get(session_id=original_session_id)
        playback_session.expires_at = timezone.now() + timedelta(seconds=5)
        playback_session.save(update_fields=["expires_at", "updated_at"])
        forced_expiry = playback_session.expires_at

        refreshed = self._followup(PlaybackRenewView.as_view(), original_token)

        self.assertEqual(refreshed.status_code, 200, refreshed.data)
        self.assertEqual(refreshed.data["playback_session_id"], original_session_id)
        self.assertEqual(refreshed.data["policy_version"], self.video.policy_version)
        self.assertEqual(refreshed.data["access_mode"], AccessMode.PROCTORED_CLASS)
        self.assertTrue(refreshed.data["monitoring_enabled"])
        self.assertGreater(refreshed.data["playback_expires_at"], int(timezone.now().timestamp()))
        self.assertTrue(refreshed.data["playback_token"])
        self.assertEqual(VideoPlaybackSession.objects.count(), 1)
        playback_session.refresh_from_db()
        self.assertEqual(playback_session.status, VideoPlaybackSession.Status.ACTIVE)
        self.assertGreater(playback_session.expires_at, forced_expiry)
        self.assertEqual(
            int(playback_session.expires_at.timestamp()),
            refreshed.data["playback_expires_at"],
        )

        valid, payload, error = verify_playback_token(refreshed.data["playback_token"])
        self.assertTrue(valid, error)
        self.assertEqual(payload["session_id"], original_session_id)
        self.assertEqual(payload["enrollment_id"], self.enrollment_a.id)

    def test_lecture_deactivation_revokes_active_proctored_sessions(self):
        Attendance.objects.create(
            tenant=self.tenant,
            session=self.session,
            enrollment=self.enrollment_a,
            status="ONLINE",
        )
        started = self._post(student=self.student_a, enrollment=self.enrollment_a)
        self.assertEqual(started.status_code, 201, started.data)
        playback_session = VideoPlaybackSession.objects.get(
            session_id=started.data["session_id"],
        )

        serializer = LectureSerializer(
            instance=self.lecture,
            data={"is_active": False},
            partial=True,
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        view = LectureViewSet()
        view.request = SimpleNamespace(tenant=self.tenant)
        view.perform_update(serializer)

        playback_session.refresh_from_db()
        self.assertEqual(playback_session.status, VideoPlaybackSession.Status.REVOKED)
        self.assertTrue(playback_session.is_revoked)
        self.assertIsNotNone(playback_session.ended_at)

    def test_dispose_preserves_revoked_status_and_flushes_final_redis_stats(self):
        Attendance.objects.create(
            tenant=self.tenant,
            session=self.session,
            enrollment=self.enrollment_a,
            status="ONLINE",
        )
        started = self._post(student=self.student_a, enrollment=self.enrollment_a)
        self.assertEqual(started.status_code, 201, started.data)
        serializer = LectureSerializer(
            instance=self.lecture,
            data={"is_active": False},
            partial=True,
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        view = LectureViewSet()
        view.request = SimpleNamespace(tenant=self.tenant)
        view.perform_update(serializer)

        with (
            patch(
                "apps.domains.video.services.playback_session.is_redis_available",
                return_value=True,
            ),
            patch(
                "apps.domains.video.services.playback_session."
                "get_session_violation_stats_redis",
                return_value={"violated": 3, "total": 11},
            ),
            patch(
                "apps.domains.video.services.playback_session.flush_session_stats"
            ) as flush_stats,
            patch(
                "apps.domains.video.services.playback_session.flush_session_buffer"
            ) as flush_buffer,
        ):
            ended = self._followup(PlaybackEndView.as_view(), started.data["token"])

        self.assertEqual(ended.status_code, 200, ended.data)
        playback_session = VideoPlaybackSession.objects.get(
            session_id=started.data["session_id"],
        )
        self.assertEqual(playback_session.status, VideoPlaybackSession.Status.REVOKED)
        self.assertTrue(playback_session.is_revoked)
        self.assertEqual(playback_session.violated_count, 3)
        self.assertEqual(playback_session.total_count, 11)
        flush_stats.assert_called_once_with(started.data["session_id"])
        flush_buffer.assert_called_once_with(started.data["session_id"])


class EndedLecturePlaybackConcurrencyPostgresTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL lecture/playback row-lock contract")
        self.tenant = Tenant.objects.create(
            code="ended-playback-pg",
            name="Ended Playback PG",
            is_active=True,
        )
        self.lecture = Lecture.objects.create(
            tenant=self.tenant,
            title="Concurrent Lecture",
            name="Concurrent Lecture",
            subject="MATH",
        )
        self.session = Session.objects.create(
            lecture=self.lecture,
            title="Concurrent Session",
            order=1,
        )
        self.user = User.objects.create_user(
            username="ended-playback-pg-student",
            password="testpass123",
            tenant=self.tenant,
        )
        self.student = Student.objects.create(
            tenant=self.tenant,
            user=self.user,
            name="Concurrent Student",
            ps_number="EP-PG-1",
            omr_code="EPPG0001",
            parent_phone="01012345678",
            school_type="HIGH",
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=self.user,
            role="student",
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
        self.video = Video.objects.create(
            tenant=self.tenant,
            session=self.session,
            title="Concurrent Video",
            status=Video.Status.READY,
            duration=600,
            hls_path=f"tenants/{self.tenant.id}/video/race/master.m3u8",
        )
        Attendance.objects.create(
            tenant=self.tenant,
            session=self.session,
            enrollment=self.enrollment,
            status="ONLINE",
        )
        VideoAccess.objects.create(
            video=self.video,
            enrollment=self.enrollment,
            rule="once",
            access_mode=AccessMode.PROCTORED_CLASS,
            is_override=True,
        )

    def _issue_grant(self):
        close_old_connections()
        try:
            return issue_playback_access_grant(
                video=Video.objects.select_related("session__lecture").get(
                    pk=self.video.pk
                ),
                enrollment=Enrollment.objects.get(pk=self.enrollment.pk),
                user=User.objects.get(pk=self.user.pk),
                device_id="pg-race-device",
            )
        finally:
            close_old_connections()

    def _deactivate_lecture(self):
        close_old_connections()
        try:
            lecture = Lecture.objects.get(pk=self.lecture.pk)
            serializer = LectureSerializer(
                instance=lecture,
                data={"is_active": False},
                partial=True,
            )
            if not serializer.is_valid():
                raise AssertionError(serializer.errors)
            view = LectureViewSet()
            view.request = SimpleNamespace(tenant=self.tenant)
            self.deactivation_started.set()
            view.perform_update(serializer)
        finally:
            close_old_connections()

    def test_deactivation_waits_for_inflight_grant_then_revokes_created_session(self):
        grant_holds_lecture = threading.Event()
        release_grant = threading.Event()
        self.deactivation_started = threading.Event()

        def hold_policy_check(_lecture):
            grant_holds_lecture.set()
            if not release_grant.wait(timeout=10):
                raise TimeoutError("deactivation race test did not release grant")
            return True

        issue_payload = {
            "session_id": "ended-playback-pg-session",
            "expires_at": int(timezone.now().timestamp()) + 600,
        }
        with (
            patch(
                "academy.application.use_cases.student_video_access_context."
                "lecture_allows_student_learning",
                side_effect=hold_policy_check,
            ),
            patch(
                "apps.domains.video.services.playback_session.issue_session",
                return_value=(True, issue_payload, None),
            ),
            patch("apps.domains.video.services.playback_session.init_session_redis"),
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                grant_future = pool.submit(self._issue_grant)
                self.assertTrue(grant_holds_lecture.wait(timeout=10))
                deactivate_future = pool.submit(self._deactivate_lecture)
                self.assertTrue(self.deactivation_started.wait(timeout=10))
                release_grant.set()
                grant = grant_future.result(timeout=15)
                deactivate_future.result(timeout=15)

        self.assertTrue(grant.token)
        self.lecture.refresh_from_db()
        self.assertFalse(self.lecture.is_active)
        playback_session = VideoPlaybackSession.objects.get(
            session_id=issue_payload["session_id"],
        )
        self.assertEqual(playback_session.status, VideoPlaybackSession.Status.REVOKED)
        self.assertTrue(playback_session.is_revoked)
        self.assertIsNotNone(playback_session.ended_at)

    @override_settings(
        CDN_HLS_BASE_URL="https://cdn.example.test",
        CDN_HLS_SIGNING_SECRET="endpoint-race-secret",
    )
    def test_real_media_start_endpoint_cannot_issue_after_close_commits(self):
        start_validated = threading.Event()
        release_start = threading.Event()
        stale_context = StudentVideoAccessContext(
            enrollment=self.enrollment,
            access_mode=AccessMode.PROCTORED_CLASS,
            is_public_video=False,
        )

        def mark_access_validated(**_kwargs):
            start_validated.set()
            if not release_start.wait(timeout=10):
                raise TimeoutError("endpoint race test did not release playback start")
            return True, ""

        access = RefreshToken.for_user(self.user).access_token
        access["tenant_id"] = self.tenant.id
        access["token_version"] = self.user.token_version
        authorization = f"Bearer {access}"
        issue_payload = {
            "session_id": "ended-playback-real-endpoint-race",
            "expires_at": int(timezone.now().timestamp()) + 600,
        }

        def post_start_endpoint():
            close_old_connections()
            try:
                client = APIClient()
                return client.post(
                    "/api/v1/media/playback/start/",
                    {
                        "video_id": self.video.id,
                        "enrollment_id": self.enrollment.id,
                        "device_id": "real-endpoint-race-device",
                    },
                    format="json",
                    HTTP_AUTHORIZATION=authorization,
                    HTTP_HOST="localhost",
                    HTTP_X_TENANT_CODE=self.tenant.code,
                )
            finally:
                close_old_connections()

        with (
            patch(
                "apps.domains.video.views.playback_views."
                "resolve_student_video_access_context",
                return_value=stale_context,
            ),
            patch(
                "apps.domains.video.views.playback_views."
                "PlaybackStartView._check_access",
                side_effect=mark_access_validated,
            ),
            patch(
                "apps.domains.video.services.playback_session.issue_session",
                return_value=(True, issue_payload, None),
            ),
            patch("apps.domains.video.services.playback_session.init_session_redis"),
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                self.deactivation_started = threading.Event()
                start_future = pool.submit(post_start_endpoint)
                self.assertTrue(start_validated.wait(timeout=10))
                try:
                    close_future = pool.submit(self._deactivate_lecture)
                    close_future.result(timeout=15)
                finally:
                    release_start.set()
                response = start_future.result(timeout=15)

        self.assertEqual(response.status_code, 403, response.data)
        self.assertNotIn("token", response.data)
        self.assertNotIn("play_url", response.data)
        self.assertFalse(
            VideoPlaybackSession.objects.filter(
                session_id=issue_payload["session_id"],
            ).exists()
        )


class PlaybackFollowupTokenBindingTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.owner = User.objects.create_user(username="playback-token-owner")
        self.other = User.objects.create_user(username="playback-token-other")
        self.tenant = Tenant.objects.create(
            code="playback-followup-token",
            name="Playback Followup Token",
            is_active=True,
        )

    @patch("apps.domains.video.views.playback_views.verify_playback_token")
    def test_all_followup_endpoints_reject_token_owned_by_another_user(self, verify):
        verify.return_value = (True, {"user_id": self.owner.id}, None)
        cases = (
            ("refresh", PlaybackRefreshView.as_view(), {"token": "signed"}),
            ("heartbeat", PlaybackHeartbeatView.as_view(), {"token": "signed"}),
            ("end", PlaybackEndView.as_view(), {"token": "signed"}),
            ("events", PlaybackEventBatchView.as_view(), {"token": "signed", "events": []}),
        )

        for label, view, data in cases:
            request = self.factory.post("/api/v1/video/playback/followup/", data, format="json")
            force_authenticate(request, user=self.other)
            with self.subTest(view=label):
                response = view(request)
                self.assertEqual(response.status_code, 403, response.data)
                self.assertEqual(response.data["detail"], "token_user_mismatch")

    @patch("apps.domains.video.views.playback_views.verify_playback_token")
    def test_followups_reject_explicit_cross_tenant_token(self, verify):
        verify.return_value = (
            True,
            {"user_id": self.owner.id, "tenant_id": self.tenant.id + 1},
            None,
        )
        request = self.factory.post(
            "/api/v1/video/playback/end/",
            {"token": "signed"},
            format="json",
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.owner)

        response = PlaybackEndView.as_view()(request)

        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(response.data["detail"], "token_tenant_mismatch")

    @patch("apps.domains.video.views.playback_views.verify_playback_token")
    @patch("apps.domains.video.views.playback_views.video_repo.video_get_by_id_with_relations")
    def test_legacy_token_is_bound_to_authoritative_video_tenant(self, get_video, verify):
        verify.return_value = (
            True,
            {"user_id": self.owner.id, "video_id": 17, "monitoring_enabled": False},
            None,
        )
        get_video.return_value = type("LegacyVideo", (), {"tenant_id": self.tenant.id})()
        request = self.factory.post(
            "/api/v1/video/playback/end/",
            {"token": "signed"},
            format="json",
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.owner)

        response = PlaybackEndView.as_view()(request)

        self.assertEqual(response.status_code, 200, response.data)


@override_settings(ALLOWED_HOSTS=["video-auth-guard", "testserver"])
class VideoManagementAuthenticationGuardTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            code="video-auth-guard",
            name="Video Auth Guard",
            is_active=True,
        )
        self.user = User.objects.create_user(
            username="video-auth-guard-admin",
            password="testpass123",
            tenant=self.tenant,
            is_staff=True,
        )
        self.membership = TenantMembership.ensure_active(
            tenant=self.tenant,
            user=self.user,
            role="admin",
        )
        self.path = "/api/v1/media/videos/folders/"

    def _access_token(self):
        token = RefreshToken.for_user(self.user).access_token
        token["token_version"] = self.user.token_version
        token["tenant_id"] = self.tenant.id
        return str(token)

    def _post_with_token(self, token):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return client.post(
            self.path,
            {"name": "must-not-create"},
            format="json",
            HTTP_HOST=self.tenant.code,
        )

    def test_password_revoked_jwt_cannot_mutate_video_management(self):
        token = self._access_token()
        self.user.token_version += 1
        self.user.save(update_fields=["token_version"])

        response = self._post_with_token(token)

        self.assertEqual(response.status_code, 401, response.data)
        self.assertFalse(VideoFolder.objects.filter(tenant=self.tenant).exists())

    def test_membership_revoked_jwt_cannot_mutate_video_management(self):
        token = self._access_token()
        self.membership.is_active = False
        self.membership.save(update_fields=["is_active"])

        response = self._post_with_token(token)

        self.assertEqual(response.status_code, 401, response.data)
        self.assertFalse(VideoFolder.objects.filter(tenant=self.tenant).exists())

    def test_session_post_without_csrf_token_is_rejected(self):
        client = APIClient(enforce_csrf_checks=True)
        client.force_login(self.user)

        response = client.post(
            self.path,
            {"name": "must-not-create"},
            format="json",
            HTTP_HOST=self.tenant.code,
        )

        self.assertEqual(response.status_code, 403, response.data)
        self.assertFalse(VideoFolder.objects.filter(tenant=self.tenant).exists())

    def test_membership_revoked_session_with_valid_csrf_is_rejected(self):
        self.membership.is_active = False
        self.membership.save(update_fields=["is_active"])
        client = APIClient(enforce_csrf_checks=True)
        client.force_login(self.user)
        csrf_secret = "a" * 32
        client.cookies["csrftoken"] = csrf_secret

        response = client.post(
            self.path,
            {"name": "must-not-create"},
            format="json",
            HTTP_HOST=self.tenant.code,
            HTTP_X_CSRFTOKEN=csrf_secret,
        )

        self.assertEqual(response.status_code, 401, response.data)
        self.assertIn("권한", str(response.data["detail"]))
        self.assertFalse(VideoFolder.objects.filter(tenant=self.tenant).exists())
