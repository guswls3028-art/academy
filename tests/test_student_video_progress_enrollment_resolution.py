from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from datetime import timedelta

from django.db import IntegrityError, connection, transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.core.models import Tenant, TenantMembership, User
from apps.domains.attendance.models import Attendance
from apps.domains.enrollment.models import Enrollment, SessionEnrollment
from apps.domains.lectures.models import Lecture, Session
from apps.domains.parents.models import Parent
from apps.domains.student_app.media.views import (
    StudentPublicSessionView,
    StudentVideoCommentListView,
    StudentVideoLikeView,
    StudentVideoMeView,
    StudentVideoPlaybackView,
    StudentVideoProgressView,
    StudentVideoForwardSkipView,
    StudentSessionVideoListView,
    StudentVideoStatsView,
)
from apps.domains.students.models import Student
from apps.domains.video.models import (
    AccessMode,
    Video,
    VideoAccess,
    InactiveVideoEntitlement,
    VideoComment,
    VideoLike,
    VideoPlaybackSession,
    VideoProgress,
)
from apps.domains.video.drm import verify_playback_token
from apps.domains.video.services.inactive_entitlements import (
    get_active_inactive_video_entitlement,
)
from apps.domains.video.services.access_resolver import get_effective_access_mode
from apps.domains.video.views.playback_views import (
    PlaybackHeartbeatView,
    PlaybackRenewView,
    PlaybackStartView,
    _is_policy_token_valid,
)
from apps.domains.video.views.permission_views import InactiveVideoEntitlementViewSet
from apps.support.student_app.video_media import (
    build_thumbnail_url,
    issue_playback_access_grant,
    pick_video_urls,
)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class StudentVideoProgressEnrollmentResolutionTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.tenant = Tenant.objects.create(
            code="student-video-progress",
            name="Student Video Progress",
            is_active=True,
        )
        self.user = User.objects.create_user(
            username="student-video-progress-user",
            password="testpass123",
            tenant=self.tenant,
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=self.user, role="student")
        self.parent_user = User.objects.create_user(
            username="student-video-progress-parent",
            password="testpass123",
            tenant=self.tenant,
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=self.parent_user, role="parent")
        self.parent = Parent.objects.create(
            tenant=self.tenant,
            user=self.parent_user,
            name="Video Parent",
            phone="01099998888",
        )
        self.student = Student.objects.create(
            tenant=self.tenant,
            user=self.user,
            parent=self.parent,
            name="Video Student",
            ps_number="SVP-001",
            omr_code="12345678",
            parent_phone="01012345678",
            school_type="HIGH",
        )
        self.old_lecture = Lecture.objects.create(
            tenant=self.tenant,
            title="Old Lecture",
            name="Old Lecture",
            subject="MATH",
        )
        self.target_lecture = Lecture.objects.create(
            tenant=self.tenant,
            title="Target Lecture",
            name="Target Lecture",
            subject="MATH",
        )
        self.old_enrollment = Enrollment.objects.create(
            tenant=self.tenant,
            student=self.student,
            lecture=self.old_lecture,
            status="ACTIVE",
        )
        self.target_enrollment = Enrollment.objects.create(
            tenant=self.tenant,
            student=self.student,
            lecture=self.target_lecture,
            status="ACTIVE",
        )
        self.target_session = Session.objects.create(
            lecture=self.target_lecture,
            title="Target Session",
            order=1,
        )
        SessionEnrollment.objects.get_or_create(
            tenant=self.tenant, session=self.target_session, enrollment=self.target_enrollment,
        )
        self.video = Video.objects.create(
            tenant=self.tenant,
            session=self.target_session,
            title="Target Video",
            status=Video.Status.READY,
            duration=100,
        )

    def _post_progress(self, payload, *, user=None, selected_student_id=None, video=None):
        target_video = video or self.video
        request = self.factory.post(
            f"/api/v1/student/video/videos/{target_video.id}/progress/",
            payload,
            format="json",
        )
        if selected_student_id is not None:
            request.META["HTTP_X_STUDENT_ID"] = str(selected_student_id)
        request.tenant = self.tenant
        force_authenticate(request, user=user or self.user)
        return StudentVideoProgressView.as_view()(request, video_id=target_video.id)

    def _post_forward_skip(self, *, user=None, enrollment_id=None, selected_student_id=None):
        payload = {"enrollment_id": enrollment_id} if enrollment_id is not None else {}
        request = self.factory.post(
            f"/api/v1/student/video/videos/{self.video.id}/forward-skip/",
            payload,
            format="json",
        )
        if selected_student_id is not None:
            request.META["HTTP_X_STUDENT_ID"] = str(selected_student_id)
        request.tenant = self.tenant
        force_authenticate(request, user=user or self.user)
        return StudentVideoForwardSkipView.as_view()(request, video_id=self.video.id)

    def _get_playback(
        self,
        *,
        user=None,
        enrollment_id=None,
        selected_student_id=None,
        video=None,
        access_check=False,
    ):
        target_video = video or self.video
        path = f"/api/v1/student/video/videos/{target_video.id}/playback/"
        query = []
        if enrollment_id is not None:
            query.append(f"enrollment={enrollment_id}")
        if access_check:
            query.append("access_check=true")
        if query:
            path += f"?{'&'.join(query)}"
        request = self.factory.get(path) if access_check else self.factory.post(path)
        if selected_student_id is not None:
            request.META["HTTP_X_STUDENT_ID"] = str(selected_student_id)
        request.tenant = self.tenant
        force_authenticate(request, user=user or self.user)
        return StudentVideoPlaybackView.as_view()(request, video_id=target_video.id)

    def _get_me_stats(self, *, user=None, selected_student_id=None):
        request = self.factory.get("/api/v1/student/video/me/stats/")
        if selected_student_id is not None:
            request.META["HTTP_X_STUDENT_ID"] = str(selected_student_id)
        request.tenant = self.tenant
        force_authenticate(request, user=user or self.user)
        return StudentVideoStatsView.as_view()(request)

    def _get_me(self, *, user=None, selected_student_id=None):
        request = self.factory.get("/api/v1/student/video/me/")
        if selected_student_id is not None:
            request.META["HTTP_X_STUDENT_ID"] = str(selected_student_id)
        request.tenant = self.tenant
        force_authenticate(request, user=user or self.user)
        return StudentVideoMeView.as_view()(request)

    def _get_session_videos(self, *, user=None, enrollment_id=None, selected_student_id=None):
        path = f"/api/v1/student/video/sessions/{self.target_session.id}/videos/"
        if enrollment_id is not None:
            path += f"?enrollment={enrollment_id}"
        request = self.factory.get(path)
        if selected_student_id is not None:
            request.META["HTTP_X_STUDENT_ID"] = str(selected_student_id)
        request.tenant = self.tenant
        force_authenticate(request, user=user or self.user)
        return StudentSessionVideoListView.as_view()(request, session_id=self.target_session.id)

    def _get_public_session(self, *, user=None, selected_student_id=None):
        request = self.factory.get("/api/v1/student/video/public-session/")
        if selected_student_id is not None:
            request.META["HTTP_X_STUDENT_ID"] = str(selected_student_id)
        request.tenant = self.tenant
        force_authenticate(request, user=user or self.user)
        return StudentPublicSessionView.as_view()(request)

    def _post_like(self, *, user=None, selected_student_id=None):
        request = self.factory.post(
            f"/api/v1/student/video/videos/{self.video.id}/like/",
            {},
            format="json",
        )
        if selected_student_id is not None:
            request.META["HTTP_X_STUDENT_ID"] = str(selected_student_id)
        request.tenant = self.tenant
        force_authenticate(request, user=user or self.user)
        return StudentVideoLikeView.as_view()(request, video_id=self.video.id)

    def _post_comment(self, *, user=None, selected_student_id=None):
        request = self.factory.post(
            f"/api/v1/student/video/videos/{self.video.id}/comments/",
            {"content": "must not be written"},
            format="json",
        )
        if selected_student_id is not None:
            request.META["HTTP_X_STUDENT_ID"] = str(selected_student_id)
        request.tenant = self.tenant
        force_authenticate(request, user=user or self.user)
        return StudentVideoCommentListView.as_view()(request, video_id=self.video.id)

    def _create_parent_child(self, suffix: str):
        child_user = User.objects.create_user(
            username=f"student-video-progress-child-{suffix}",
            password="testpass123",
            tenant=self.tenant,
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=child_user, role="student")
        return Student.objects.create(
            tenant=self.tenant,
            user=child_user,
            parent=self.parent,
            name=f"Video Child {suffix}",
            ps_number=f"SVP-{suffix}",
            omr_code=f"8765{suffix.zfill(4)}",
            parent_phone="01012345678",
            school_type="HIGH",
        )

    def _create_unowned_student(self):
        child_user = User.objects.create_user(
            username="student-video-progress-unowned",
            password="testpass123",
            tenant=self.tenant,
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=child_user, role="student")
        return Student.objects.create(
            tenant=self.tenant,
            user=child_user,
            name="Unowned Video Student",
            ps_number="SVP-UNOWNED",
            omr_code="SVUNOWND",
            parent_phone="01012345678",
            school_type="HIGH",
        )

    def _create_cross_tenant_student(self):
        foreign_tenant = Tenant.objects.create(
            name="Student Video Foreign",
            code="student_video_progress_foreign",
            is_active=True,
        )
        child_user = User.objects.create_user(
            username="student-video-progress-foreign",
            password="testpass123",
            tenant=foreign_tenant,
        )
        TenantMembership.ensure_active(
            tenant=foreign_tenant,
            user=child_user,
            role="student",
        )
        return Student.objects.create(
            tenant=foreign_tenant,
            user=child_user,
            name="Foreign Video Student",
            ps_number="SVP-FOREIGN",
            omr_code="SVFOREI1",
            parent_phone="01012345678",
            school_type="HIGH",
        )

    def test_invalid_parent_child_headers_fail_closed_across_media_reads(self):
        unowned_student = self._create_unowned_student()

        for raw_student_id in ("not-a-student-id", unowned_student.id, ""):
            with self.subTest(raw_student_id=raw_student_id):
                responses = [
                    self._get_public_session(
                        user=self.parent_user,
                        selected_student_id=raw_student_id,
                    ),
                    self._get_me(
                        user=self.parent_user,
                        selected_student_id=raw_student_id,
                    ),
                    self._get_me_stats(
                        user=self.parent_user,
                        selected_student_id=raw_student_id,
                    ),
                    self._get_session_videos(
                        user=self.parent_user,
                        selected_student_id=raw_student_id,
                    ),
                    self._get_playback(
                        user=self.parent_user,
                        enrollment_id=self.target_enrollment.id,
                        selected_student_id=raw_student_id,
                    ),
                ]
                for response in responses:
                    self.assertGreaterEqual(response.status_code, 400, response.data)
                    self.assertLess(response.status_code, 500, response.data)

                self.video.refresh_from_db()
                self.assertEqual(self.video.view_count, 0)
                self.assertFalse(
                    Lecture.objects.filter(tenant=self.tenant, is_system=True).exists()
                )
                self.assertFalse(VideoPlaybackSession.objects.exists())

    def test_invalid_parent_child_headers_reject_media_writes_without_mutation(self):
        unowned_student = self._create_unowned_student()
        foreign_student = self._create_cross_tenant_student()

        for raw_student_id in (
            "not-a-student-id",
            unowned_student.id,
            foreign_student.id,
            "",
        ):
            with self.subTest(raw_student_id=raw_student_id):
                responses = [
                    self._post_progress(
                        {
                            "enrollment_id": self.target_enrollment.id,
                            "progress": 50,
                        },
                        user=self.parent_user,
                        selected_student_id=raw_student_id,
                    ),
                    self._post_like(
                        user=self.parent_user,
                        selected_student_id=raw_student_id,
                    ),
                    self._post_comment(
                        user=self.parent_user,
                        selected_student_id=raw_student_id,
                    ),
                ]
                for response in responses:
                    self.assertGreaterEqual(response.status_code, 400, response.data)
                    self.assertLess(response.status_code, 500, response.data)

                self.assertFalse(VideoProgress.objects.exists())
                self.assertFalse(VideoLike.objects.exists())
                self.assertFalse(VideoComment.objects.exists())
                self.video.refresh_from_db()
                self.assertEqual(self.video.like_count, 0)
                self.assertEqual(self.video.comment_count, 0)
                self.assertEqual(self.video.view_count, 0)

    def test_malformed_explicit_enrollment_rejects_without_fallback_or_mutation(self):
        invalid_values = (
            "not-an-enrollment",
            "",
            self.target_enrollment.id + 0.5,
        )

        for invalid_value in invalid_values:
            with self.subTest(invalid_value=invalid_value):
                session_response = self._get_session_videos(enrollment_id=invalid_value)
                playback_response = self._get_playback(enrollment_id=invalid_value)
                progress_response = self._post_progress(
                    {"enrollment_id": invalid_value, "progress": 50}
                )

                self.assertEqual(session_response.status_code, 400, session_response.data)
                self.assertEqual(playback_response.status_code, 400, playback_response.data)
                self.assertEqual(progress_response.status_code, 400, progress_response.data)
                self.assertFalse(VideoProgress.objects.exists())
                self.assertFalse(VideoPlaybackSession.objects.exists())
                self.video.refresh_from_db()
                self.assertEqual(self.video.view_count, 0)

    def test_ended_lecture_blocks_playback_and_returns_readonly_history(self):
        VideoProgress.objects.create(
            video=self.video,
            enrollment=self.target_enrollment,
            progress=1,
            completed=True,
            last_position=100,
        )
        initial_playback = self._get_playback(
            enrollment_id=self.target_enrollment.id,
        )
        self.assertEqual(initial_playback.status_code, 200, initial_playback.data)
        self.target_lecture.is_active = False
        self.target_lecture.save(update_fields=["is_active", "updated_at"])

        playback = self._get_playback(enrollment_id=self.target_enrollment.id)
        self.assertEqual(playback.status_code, 403)

        overview = self._get_me()
        self.assertEqual(overview.status_code, 200)
        self.assertNotIn(self.target_lecture.id, [row["id"] for row in overview.data["lectures"]])
        archived = overview.data["archived_lectures"]
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0]["id"], self.target_lecture.id)
        self.assertEqual(archived[0]["video_count"], 1)
        self.assertEqual(archived[0]["completed_count"], 1)
        self.assertEqual(archived[0]["play_count"], 1)

    def test_access_check_rejects_malformed_boolean_without_playback_side_effects(self):
        request = self.factory.get(
            f"/api/v1/student/video/videos/{self.video.id}/playback/"
            f"?enrollment={self.target_enrollment.id}&access_check=flase"
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.user)

        response = StudentVideoPlaybackView.as_view()(request, video_id=self.video.id)

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("access_check", response.data)
        self.assertFalse(VideoPlaybackSession.objects.exists())
        self.video.refresh_from_db()
        self.assertEqual(self.video.view_count, 0)

    def test_playback_get_requires_explicit_post_without_side_effects(self):
        request = self.factory.get(
            f"/api/v1/student/video/videos/{self.video.id}/playback/"
            f"?enrollment={self.target_enrollment.id}"
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.user)

        response = StudentVideoPlaybackView.as_view()(request, video_id=self.video.id)

        self.assertEqual(response.status_code, 405, response.data)
        self.assertFalse(VideoPlaybackSession.objects.exists())
        self.video.refresh_from_db()
        self.assertEqual(self.video.view_count, 0)

    def test_access_check_reports_authoritative_policy_drift_without_side_effects(self):
        initial = self._get_playback(
            enrollment_id=self.target_enrollment.id,
            access_check=True,
        )
        self.assertEqual(initial.status_code, 200, initial.data)
        self.assertEqual(initial.data["access_mode"], AccessMode.FREE_REVIEW.value)
        self.assertFalse(initial.data["monitoring_enabled"])

        Attendance.objects.create(
            tenant=self.tenant,
            session=self.target_session,
            enrollment=self.target_enrollment,
            status="ONLINE",
        )
        Video.objects.filter(pk=self.video.pk).update(
            policy_version=self.video.policy_version + 1,
        )

        drifted = self._get_playback(
            enrollment_id=self.target_enrollment.id,
            access_check=True,
        )

        self.assertEqual(drifted.status_code, 200, drifted.data)
        self.assertEqual(
            drifted.data["access_mode"],
            AccessMode.PROCTORED_CLASS.value,
        )
        self.assertTrue(drifted.data["monitoring_enabled"])
        self.assertEqual(
            drifted.data["policy_version"],
            self.video.policy_version + 1,
        )
        self.assertFalse(VideoPlaybackSession.objects.exists())
        self.video.refresh_from_db()
        self.assertEqual(self.video.view_count, 0)

    def test_inactive_enrollment_without_exact_entitlement_denies_playback(self):
        SessionEnrollment.objects.get_or_create(
            tenant=self.tenant,
            session=self.target_session,
            enrollment=self.target_enrollment,
        )
        self.target_enrollment.status = "INACTIVE"
        self.target_enrollment.save(update_fields=["status"])

        response = self._get_playback(enrollment_id=self.target_enrollment.id)

        self.assertEqual(response.status_code, 403)
        self.assertFalse(VideoPlaybackSession.objects.exists())
        self.video.refresh_from_db()
        self.assertEqual(self.video.view_count, 0)

    def test_access_check_matches_offline_explicit_proctored_override(self):
        VideoAccess.objects.create(
            video=self.video,
            enrollment=self.target_enrollment,
            rule="once",
            access_mode=AccessMode.PROCTORED_CLASS,
            is_override=True,
        )

        access_check = self._get_playback(
            enrollment_id=self.target_enrollment.id,
            access_check=True,
        )

        self.assertEqual(access_check.status_code, 200, access_check.data)
        self.assertEqual(
            access_check.data,
            {
                "ok": True,
                "access_mode": AccessMode.PROCTORED_CLASS.value,
                "monitoring_enabled": True,
                "policy_version": self.video.policy_version,
            },
        )
        self.assertFalse(VideoPlaybackSession.objects.exists())
        self.video.refresh_from_db()
        self.assertEqual(self.video.view_count, 0)

        playback = self._get_playback(enrollment_id=self.target_enrollment.id)
        self.assertEqual(playback.status_code, 200, playback.data)
        self.assertEqual(playback.data["policy"]["access_mode"], access_check.data["access_mode"])
        self.assertEqual(
            playback.data["policy"]["monitoring_enabled"],
            access_check.data["monitoring_enabled"],
        )
        self.assertEqual(playback.data["policy_version"], access_check.data["policy_version"])
        self.assertIsNotNone(playback.data["playback_session_id"])

    def test_access_check_matches_online_explicit_free_review_override(self):
        Attendance.objects.create(
            tenant=self.tenant,
            session=self.target_session,
            enrollment=self.target_enrollment,
            status="ONLINE",
        )
        VideoAccess.objects.create(
            video=self.video,
            enrollment=self.target_enrollment,
            rule="free",
            access_mode=AccessMode.FREE_REVIEW,
            is_override=True,
        )

        access_check = self._get_playback(
            enrollment_id=self.target_enrollment.id,
            access_check=True,
        )

        self.assertEqual(access_check.status_code, 200, access_check.data)
        self.assertEqual(
            access_check.data,
            {
                "ok": True,
                "access_mode": AccessMode.FREE_REVIEW.value,
                "monitoring_enabled": False,
                "policy_version": self.video.policy_version,
            },
        )
        self.assertFalse(VideoPlaybackSession.objects.exists())
        self.video.refresh_from_db()
        self.assertEqual(self.video.view_count, 0)

        playback = self._get_playback(enrollment_id=self.target_enrollment.id)
        self.assertEqual(playback.status_code, 200, playback.data)
        self.assertEqual(playback.data["policy"]["access_mode"], access_check.data["access_mode"])
        self.assertEqual(
            playback.data["policy"]["monitoring_enabled"],
            access_check.data["monitoring_enabled"],
        )
        self.assertEqual(playback.data["policy_version"], access_check.data["policy_version"])
        self.assertIsNone(playback.data["playback_session_id"])

    def test_public_video_access_check_never_creates_or_reactivates_enrollment(self):
        system_lecture = Lecture.get_or_create_system_lecture(self.tenant)
        system_session = Session.objects.create(
            lecture=system_lecture,
            title="Public Session",
            order=1,
        )
        public_video = Video.objects.create(
            tenant=self.tenant,
            session=system_session,
            title="Public Video",
            status=Video.Status.READY,
            visibility=Video.Visibility.PUBLIC,
            duration=60,
        )
        system_enrollment = Enrollment.objects.create(
            tenant=self.tenant,
            student=self.student,
            lecture=system_lecture,
            status="INACTIVE",
        )
        before = list(
            Enrollment.objects.filter(
                tenant=self.tenant,
                student=self.student,
                lecture=system_lecture,
            ).values_list("id", "status")
        )

        response = self._get_playback(video=public_video, access_check=True)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            list(
                Enrollment.objects.filter(
                    tenant=self.tenant,
                    student=self.student,
                    lecture=system_lecture,
                ).values_list("id", "status")
            ),
            before,
        )
        system_enrollment.refresh_from_db()
        self.assertEqual(system_enrollment.status, "INACTIVE")
        self.assertFalse(VideoPlaybackSession.objects.exists())
        public_video.refresh_from_db()
        self.assertEqual(public_video.view_count, 0)

    def test_inactive_entitlement_exposes_and_plays_only_the_exact_video(self):
        SessionEnrollment.objects.get_or_create(
            tenant=self.tenant,
            session=self.target_session,
            enrollment=self.target_enrollment,
        )
        sibling_video = Video.objects.create(
            tenant=self.tenant,
            session=self.target_session,
            title="Unpaid Sibling Video",
            status=Video.Status.READY,
            duration=100,
            order=2,
        )
        self.target_enrollment.status = "INACTIVE"
        self.target_enrollment.save(update_fields=["status"])
        InactiveVideoEntitlement.objects.create(
            tenant=self.tenant,
            student=self.student,
            enrollment=self.target_enrollment,
            video=self.video,
            access_mode=AccessMode.PROCTORED_CLASS,
            source="STAFF_AUTHORIZATION",
            source_reference="test:paid-session-1",
            reason="Paid first-session video access",
            granted_by_reference="test:staff-1",
            expires_at=timezone.now() + timedelta(days=7),
        )

        overview = self._get_me()
        session_videos = self._get_session_videos(
            enrollment_id=self.target_enrollment.id,
        )
        access_check = self._get_playback(
            enrollment_id=self.target_enrollment.id,
            access_check=True,
        )
        self.assertEqual(VideoPlaybackSession.objects.count(), 0)
        self.video.refresh_from_db()
        self.assertEqual(self.video.view_count, 0)
        playback = self._get_playback(enrollment_id=self.target_enrollment.id)
        progress_write = self._post_progress(
            {
                "enrollment_id": self.target_enrollment.id,
                "progress": 0.25,
                "last_position": 25,
            }
        )
        skip_write = self._post_forward_skip(
            enrollment_id=self.target_enrollment.id,
        )
        sibling_playback = self._get_playback(
            enrollment_id=self.target_enrollment.id,
            video=sibling_video,
        )

        self.assertEqual(overview.status_code, 200, overview.data)
        paid_lecture = next(
            row for row in overview.data["lectures"]
            if row["id"] == self.target_lecture.id
        )
        self.assertEqual(paid_lecture["enrollment_id"], self.target_enrollment.id)
        self.assertEqual(paid_lecture["video_count"], 1)
        self.assertEqual(
            [row["id"] for row in paid_lecture["sessions"]],
            [self.target_session.id],
        )
        self.assertEqual(session_videos.status_code, 200, session_videos.data)
        self.assertEqual(
            [row["id"] for row in session_videos.data["items"]],
            [self.video.id],
        )
        self.assertEqual(playback.status_code, 200, playback.data)
        self.assertEqual(access_check.status_code, 200, access_check.data)
        self.assertEqual(
            playback.data["policy"]["access_mode"],
            AccessMode.PROCTORED_CLASS,
        )
        self.assertTrue(playback.data["policy"]["monitoring_enabled"])
        self.assertEqual(
            access_check.data["access_mode"],
            playback.data["policy"]["access_mode"],
        )
        self.assertEqual(
            access_check.data["monitoring_enabled"],
            playback.data["policy"]["monitoring_enabled"],
        )
        self.assertEqual(
            access_check.data["policy_version"],
            playback.data["policy_version"],
        )
        self.assertEqual(sibling_playback.status_code, 403)
        self.assertEqual(progress_write.status_code, 200, progress_write.data)
        self.assertEqual(skip_write.status_code, 200, skip_write.data)
        progress = VideoProgress.objects.get(
            video=self.video,
            enrollment=self.target_enrollment,
        )
        self.assertEqual(progress.forward_skip_seconds_used, 10)

    def test_inactive_entitlement_does_not_open_likes_or_comments(self):
        SessionEnrollment.objects.get_or_create(
            tenant=self.tenant,
            session=self.target_session,
            enrollment=self.target_enrollment,
        )
        self.target_enrollment.status = "INACTIVE"
        self.target_enrollment.save(update_fields=["status"])
        InactiveVideoEntitlement.objects.create(
            tenant=self.tenant,
            student=self.student,
            enrollment=self.target_enrollment,
            video=self.video,
            access_mode=AccessMode.FREE_REVIEW,
            source=InactiveVideoEntitlement.Source.STAFF_AUTHORIZATION,
            source_reference="test:paid-session-1",
            reason="Exact first-session video access",
            granted_by_reference="test:staff-1",
            expires_at=timezone.now() + timedelta(days=7),
        )

        like = self._post_like()
        comment = self._post_comment()

        self.assertEqual(like.status_code, 403, like.data)
        self.assertEqual(comment.status_code, 403, comment.data)
        self.assertFalse(VideoLike.objects.exists())
        self.assertFalse(VideoComment.objects.exists())

    def test_soft_deleted_entitled_video_is_hidden_and_staff_state_is_ineligible(self):
        staff = User.objects.create_user(
            username="student-video-entitlement-deleted-video-staff",
            password="testpass123",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=staff,
            role="admin",
        )
        SessionEnrollment.objects.get_or_create(
            tenant=self.tenant,
            session=self.target_session,
            enrollment=self.target_enrollment,
        )
        self.target_enrollment.status = "INACTIVE"
        self.target_enrollment.save(update_fields=["status"])
        entitlement = InactiveVideoEntitlement.objects.create(
            tenant=self.tenant,
            student=self.student,
            enrollment=self.target_enrollment,
            video=self.video,
            access_mode=AccessMode.FREE_REVIEW,
            source=InactiveVideoEntitlement.Source.STAFF_AUTHORIZATION,
            source_reference="test:deleted-video",
            reason="Deleted media must fail closed",
            granted_by=staff,
            granted_by_reference=f"user:{staff.id}",
            expires_at=timezone.now() + timedelta(hours=1),
        )

        self.video.delete()
        resolved = get_active_inactive_video_entitlement(
            video=self.video,
            enrollment=self.target_enrollment,
        )
        overview = self._get_me()
        session_videos = self._get_session_videos(
            enrollment_id=self.target_enrollment.id,
        )
        list_request = self.factory.get(
            f"/api/v1/media/inactive-video-entitlements/?video_id={self.video.id}"
        )
        list_request.tenant = self.tenant
        force_authenticate(list_request, user=staff)
        staff_list = InactiveVideoEntitlementViewSet.as_view({"get": "list"})(
            list_request
        )

        self.assertIsNone(resolved)
        self.assertEqual(overview.status_code, 200, overview.data)
        self.assertNotIn(
            self.target_lecture.id,
            [lecture["id"] for lecture in overview.data["lectures"]],
        )
        self.assertEqual(session_videos.status_code, 403, session_videos.data)
        self.assertNotIn("thumbnail", str(session_videos.data).lower())
        self.assertEqual(staff_list.status_code, 200, staff_list.data)
        rows = (
            staff_list.data["results"]
            if isinstance(staff_list.data, dict) and "results" in staff_list.data
            else staff_list.data
        )
        deleted_row = next(row for row in rows if row["id"] == entitlement.id)
        self.assertEqual(deleted_row["state"], "INELIGIBLE")

    def test_unsupported_stored_source_never_resolves_plays_or_lists_active(self):
        if connection.vendor != "sqlite":
            self.skipTest("SQLite corruption fixture; PostgreSQL enforces the DB CHECK")
        staff = User.objects.create_user(
            username="student-video-entitlement-invalid-source-staff",
            password="testpass123",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=staff,
            role="admin",
        )
        SessionEnrollment.objects.get_or_create(
            tenant=self.tenant,
            session=self.target_session,
            enrollment=self.target_enrollment,
        )
        self.target_enrollment.status = "INACTIVE"
        self.target_enrollment.save(update_fields=["status"])
        with self.assertRaises(IntegrityError), transaction.atomic():
            InactiveVideoEntitlement.objects.create(
                tenant=self.tenant,
                student=self.student,
                enrollment=self.target_enrollment,
                video=self.video,
                access_mode=AccessMode.FREE_REVIEW,
                source="PAYMENT_AUTO",
                source_reference="test:db-check-rejects-source",
                reason="Unsupported source must violate the DB CHECK",
                granted_by=staff,
                granted_by_reference=f"user:{staff.id}",
                expires_at=timezone.now() + timedelta(hours=1),
            )
        entitlement = InactiveVideoEntitlement.objects.create(
            tenant=self.tenant,
            student=self.student,
            enrollment=self.target_enrollment,
            video=self.video,
            access_mode=AccessMode.FREE_REVIEW,
            source=InactiveVideoEntitlement.Source.STAFF_AUTHORIZATION,
            source_reference="test:unsupported-stored-source",
            reason="Corruption fixture must remain fail closed",
            granted_by=staff,
            granted_by_reference=f"user:{staff.id}",
            expires_at=timezone.now() + timedelta(hours=1),
        )
        table = InactiveVideoEntitlement._meta.db_table
        try:
            with connection.cursor() as cursor:
                cursor.execute("PRAGMA ignore_check_constraints = ON")
                cursor.execute(
                    f'UPDATE "{table}" SET "source" = %s WHERE "id" = %s',
                    ["PAYMENT_AUTO", entitlement.id],
                )
        finally:
            with connection.cursor() as cursor:
                cursor.execute("PRAGMA ignore_check_constraints = OFF")

        entitlement.refresh_from_db()
        resolved = get_active_inactive_video_entitlement(
            video=self.video,
            enrollment=self.target_enrollment,
        )
        playback = self._get_playback(enrollment_id=self.target_enrollment.id)
        overview = self._get_me()
        list_request = self.factory.get(
            f"/api/v1/media/inactive-video-entitlements/?video_id={self.video.id}"
        )
        list_request.tenant = self.tenant
        force_authenticate(list_request, user=staff)
        staff_list = InactiveVideoEntitlementViewSet.as_view({"get": "list"})(
            list_request
        )

        self.assertEqual(entitlement.source, "PAYMENT_AUTO")
        self.assertIsNone(resolved)
        self.assertEqual(playback.status_code, 403, playback.data)
        self.assertNotIn(
            self.target_lecture.id,
            [lecture["id"] for lecture in overview.data["lectures"]],
        )
        rows = (
            staff_list.data["results"]
            if isinstance(staff_list.data, dict) and "results" in staff_list.data
            else staff_list.data
        )
        invalid_row = next(row for row in rows if row["id"] == entitlement.id)
        self.assertEqual(invalid_row["state"], "INELIGIBLE")

    @override_settings(
        CDN_HLS_BASE_URL="https://cdn.example.test",
        CDN_HLS_SIGNING_SECRET="inactive-entitlement-test-secret",
        VIDEO_PLAYBACK_TTL_SECONDS=3600,
    )
    def test_inactive_media_urls_and_token_clamp_config_drift_and_revoke(self):
        staff = User.objects.create_user(
            username="student-video-entitlement-bounded-media-staff",
            password="testpass123",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=staff,
            role="admin",
        )
        SessionEnrollment.objects.get_or_create(
            tenant=self.tenant,
            session=self.target_session,
            enrollment=self.target_enrollment,
        )
        self.target_enrollment.status = "INACTIVE"
        self.target_enrollment.save(update_fields=["status"])
        expires_at = timezone.now() + timedelta(hours=2)
        entitlement = InactiveVideoEntitlement.objects.create(
            tenant=self.tenant,
            student=self.student,
            enrollment=self.target_enrollment,
            video=self.video,
            access_mode=AccessMode.FREE_REVIEW,
            source=InactiveVideoEntitlement.Source.STAFF_AUTHORIZATION,
            source_reference="test:bounded-media",
            reason="Bound exact hosted media access",
            granted_by=staff,
            granted_by_reference=f"user:{staff.id}",
            expires_at=expires_at,
        )
        self.video.refresh_from_db()
        policy_version = self.video.policy_version

        playback = self._get_playback(enrollment_id=self.target_enrollment.id)

        self.assertEqual(playback.status_code, 200, playback.data)
        valid, token_payload, token_error = verify_playback_token(
            playback.data["playback_token"]
        )
        self.assertTrue(valid, token_error)
        hls_expiry = int(parse_qs(urlparse(playback.data["play_url"]).query)["exp"][0])
        thumbnail_expiry = int(
            parse_qs(urlparse(playback.data["video"]["thumbnail_url"]).query)["exp"][0]
        )
        entitlement_expiry = int(expires_at.timestamp())
        now_timestamp = int(timezone.now().timestamp())
        for bounded_expiry in (
            hls_expiry,
            thumbnail_expiry,
            int(token_payload["exp"]),
            int(playback.data["playback_expires_at"]),
        ):
            self.assertGreater(bounded_expiry, now_timestamp)
            self.assertLessEqual(bounded_expiry, entitlement_expiry)
            self.assertLessEqual(bounded_expiry, now_timestamp + 600)

        renew_request = self.factory.post(
            "/api/v1/media/playback/renew/",
            {"token": playback.data["playback_token"]},
            format="json",
        )
        renew_request.tenant = self.tenant
        force_authenticate(renew_request, user=self.user)
        renewal = PlaybackRenewView.as_view()(renew_request)
        self.assertEqual(renewal.status_code, 200, renewal.data)
        self.assertIsNone(renewal.data["playback_session_id"])
        self.assertTrue(renewal.data["play_url"])
        renewed_ok, renewed_payload, renewed_error = verify_playback_token(
            renewal.data["playback_token"]
        )
        self.assertTrue(renewed_ok, renewed_error)
        renewed_url_expiry = int(
            parse_qs(urlparse(renewal.data["play_url"]).query)["exp"][0]
        )
        self.assertEqual(renewed_url_expiry, int(renewed_payload["exp"]))
        self.assertLessEqual(renewed_url_expiry, entitlement_expiry)
        self.assertLessEqual(renewed_url_expiry, int(timezone.now().timestamp()) + 600)

        revoke_request = self.factory.post(
            f"/api/v1/media/inactive-video-entitlements/{entitlement.id}/revoke/",
            {"reason": "Stop new exact media grants"},
            format="json",
        )
        revoke_request.tenant = self.tenant
        force_authenticate(revoke_request, user=staff)
        revoke = InactiveVideoEntitlementViewSet.as_view({"post": "revoke"})(
            revoke_request,
            pk=entitlement.id,
        )
        denied = self._get_playback(enrollment_id=self.target_enrollment.id)

        self.assertEqual(revoke.status_code, 200, revoke.data)
        self.assertEqual(denied.status_code, 403, denied.data)
        stale_renew_request = self.factory.post(
            "/api/v1/media/playback/renew/",
            {"token": playback.data["playback_token"]},
            format="json",
        )
        stale_renew_request.tenant = self.tenant
        force_authenticate(stale_renew_request, user=self.user)
        stale_renewal = PlaybackRenewView.as_view()(stale_renew_request)
        self.assertEqual(stale_renewal.status_code, 403, stale_renewal.data)
        self.video.refresh_from_db()
        self.assertEqual(self.video.policy_version, policy_version)
        self.assertFalse(_is_policy_token_valid(token_payload))
        # CDN signatures contain no entitlement callback. The already-issued
        # URLs remain usable only until their bounded exp values above.
        self.assertGreater(hls_expiry, now_timestamp)
        self.assertGreater(thumbnail_expiry, now_timestamp)

    @override_settings(
        CDN_HLS_BASE_URL="https://cdn.example.test",
        CDN_HLS_SIGNING_SECRET="inactive-entitlement-absolute-expiry-secret",
        VIDEO_PLAYBACK_TTL_SECONDS=3600,
    )
    def test_inactive_grant_uses_one_absolute_expiry_across_clock_steps(self):
        staff = User.objects.create_user(
            username="student-video-entitlement-absolute-expiry-staff",
            password="testpass123",
            tenant=self.tenant,
            is_staff=True,
        )
        SessionEnrollment.objects.get_or_create(
            tenant=self.tenant,
            session=self.target_session,
            enrollment=self.target_enrollment,
        )
        self.target_enrollment.status = "INACTIVE"
        self.target_enrollment.save(update_fields=["status"])
        InactiveVideoEntitlement.objects.create(
            tenant=self.tenant,
            student=self.student,
            enrollment=self.target_enrollment,
            video=self.video,
            access_mode=AccessMode.PROCTORED_CLASS,
            source=InactiveVideoEntitlement.Source.STAFF_AUTHORIZATION,
            source_reference="test:absolute-expiry",
            reason="Use one absolute expiry across a stepped clock",
            granted_by=staff,
            granted_by_reference=f"user:{staff.id}",
            expires_at=timezone.now() + timedelta(hours=2),
        )
        request = self.factory.post(
            f"/api/v1/student/video/videos/{self.video.id}/playback/"
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.user)
        base = timezone.now().replace(microsecond=100_000)
        later = base + timedelta(seconds=1, microseconds=200_000)

        with (
            patch(
                "apps.support.student_app.video_media._playback_access_now",
                return_value=base,
            ),
            patch(
                "apps.domains.video.services.playback_session._session_now",
                return_value=later,
            ),
            patch(
                "apps.domains.video.services.playback_session.init_session_redis",
                return_value=False,
            ),
            patch(
                "apps.domains.video.drm._token_now",
                return_value=int(later.timestamp()),
            ),
        ):
            grant = issue_playback_access_grant(
                video=self.video,
                enrollment=self.target_enrollment,
                user=self.user,
                device_id="absolute-expiry-device",
            )

        self.assertIsNone(grant.error)
        self.assertIsNotNone(grant.token)
        expected_expiry = int(base.timestamp()) + 600
        valid, token_payload, token_error = verify_playback_token(grant.token)
        self.assertTrue(valid, token_error)
        session = VideoPlaybackSession.objects.get(session_id=grant.session_id)
        hls_url, _ = pick_video_urls(
            self.video,
            request=request,
            expires_at=grant.expires_at,
        )
        thumbnail_url = build_thumbnail_url(
            self.video,
            expires_at=grant.expires_at,
        )

        self.assertEqual(grant.expires_at, expected_expiry)
        self.assertEqual(int(token_payload["exp"]), expected_expiry)
        self.assertEqual(int(session.expires_at.timestamp()), expected_expiry)
        self.assertEqual(
            int(parse_qs(urlparse(hls_url).query)["exp"][0]),
            expected_expiry,
        )
        self.assertEqual(
            int(parse_qs(urlparse(thumbnail_url).query)["exp"][0]),
            expected_expiry,
        )

    @override_settings(VIDEO_PLAYBACK_TTL_SECONDS=3600)
    def test_inactive_grant_fails_if_entitlement_expires_during_issue(self):
        staff = User.objects.create_user(
            username="student-video-entitlement-imminent-expiry-staff",
            password="testpass123",
            tenant=self.tenant,
            is_staff=True,
        )
        SessionEnrollment.objects.get_or_create(
            tenant=self.tenant,
            session=self.target_session,
            enrollment=self.target_enrollment,
        )
        self.target_enrollment.status = "INACTIVE"
        self.target_enrollment.save(update_fields=["status"])
        base = timezone.now().replace(microsecond=100_000)
        InactiveVideoEntitlement.objects.create(
            tenant=self.tenant,
            student=self.student,
            enrollment=self.target_enrollment,
            video=self.video,
            access_mode=AccessMode.PROCTORED_CLASS,
            source=InactiveVideoEntitlement.Source.STAFF_AUTHORIZATION,
            source_reference="test:imminent-expiry",
            reason="Fail closed when entitlement expires during issue",
            granted_by=staff,
            granted_by_reference=f"user:{staff.id}",
            expires_at=base + timedelta(seconds=60),
        )
        after_entitlement_expiry = base + timedelta(
            seconds=60,
            microseconds=200_000,
        )
        with (
            patch(
                "apps.support.student_app.video_media._playback_access_now",
                return_value=base,
            ),
            patch(
                "apps.domains.video.services.playback_session._session_now",
                return_value=after_entitlement_expiry,
            ),
        ):
            expired_grant = issue_playback_access_grant(
                video=self.video,
                enrollment=self.target_enrollment,
                user=self.user,
                device_id="absolute-expiry-device",
            )

        self.assertEqual(expired_grant.error, "access_expired")
        self.assertIsNone(expired_grant.token)
        self.assertFalse(VideoPlaybackSession.objects.exists())

    @override_settings(
        CDN_HLS_BASE_URL="https://cdn.example.test",
        CDN_HLS_SIGNING_SECRET="log-redaction-test-secret",
        VIDEO_PLAYBACK_TTL_SECONDS=600,
    )
    def test_signed_media_query_and_token_are_never_logged(self):
        SessionEnrollment.objects.get_or_create(
            tenant=self.tenant,
            session=self.target_session,
            enrollment=self.target_enrollment,
        )
        self.target_enrollment.status = "INACTIVE"
        self.target_enrollment.save(update_fields=["status"])
        InactiveVideoEntitlement.objects.create(
            tenant=self.tenant,
            student=self.student,
            enrollment=self.target_enrollment,
            video=self.video,
            access_mode=AccessMode.FREE_REVIEW,
            source=InactiveVideoEntitlement.Source.STAFF_AUTHORIZATION,
            source_reference="test:log-redaction",
            reason="Bearer media must not enter logs",
            granted_by_reference="test:staff-1",
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        sentinel = "PAID_MEDIA_SIGNATURE_SENTINEL"
        signed_url = (
            "https://cdn.example.test/hls/master.m3u8"
            f"?exp=9999999999&sig={sentinel}&kid=v1&uid={self.user.id}"
        )

        with self.assertLogs(
            "apps.support.student_app.video_media",
            level="INFO",
        ) as media_logs:
            with self.assertLogs(
                "apps.domains.student_app.media.views",
                level="INFO",
            ) as view_logs:
                with patch(
                    "apps.domains.video.cdn.cloudflare_signing."
                    "CloudflareSignedURL.build_url",
                    return_value=signed_url,
                ):
                    response = self._get_playback(
                        enrollment_id=self.target_enrollment.id
                    )

        self.assertEqual(response.status_code, 200, response.data)
        captured = "\n".join(media_logs.output + view_logs.output)
        self.assertNotIn(sentinel, captured)
        self.assertNotIn("sig=", captured)
        self.assertNotIn(response.data["playback_token"], captured)

    def test_revoked_expired_and_inactive_account_entitlements_fail_closed(self):
        SessionEnrollment.objects.get_or_create(
            tenant=self.tenant,
            session=self.target_session,
            enrollment=self.target_enrollment,
        )
        self.target_enrollment.status = "INACTIVE"
        self.target_enrollment.save(update_fields=["status"])
        entitlement = InactiveVideoEntitlement.objects.create(
            tenant=self.tenant,
            student=self.student,
            enrollment=self.target_enrollment,
            video=self.video,
            access_mode=AccessMode.FREE_REVIEW,
            source="STAFF_AUTHORIZATION",
            source_reference="test:paid-session-1",
            reason="Paid first-session video access",
            granted_by_reference="test:staff-1",
            granted_at=timezone.now() - timedelta(days=1),
            expires_at=timezone.now() - timedelta(seconds=1),
        )

        expired = self._get_playback(enrollment_id=self.target_enrollment.id)
        entitlement.expires_at = timezone.now() + timedelta(days=7)
        entitlement.revoked_at = timezone.now()
        entitlement.revoked_by_reference = "test:staff-1"
        entitlement.revoke_reason = "Access withdrawn"
        entitlement.save(
            update_fields=[
                "expires_at",
                "revoked_at",
                "revoked_by_reference",
                "revoke_reason",
                "updated_at",
            ]
        )
        revoked = self._get_playback(enrollment_id=self.target_enrollment.id)
        entitlement.revoked_at = None
        entitlement.revoked_by_reference = ""
        entitlement.revoke_reason = ""
        entitlement.save(
            update_fields=[
                "revoked_at",
                "revoked_by_reference",
                "revoke_reason",
                "updated_at",
            ]
        )
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        inactive_account = self._get_playback(enrollment_id=self.target_enrollment.id)
        self.user.is_active = True
        self.user.save(update_fields=["is_active"])
        TenantMembership.objects.filter(
            tenant=self.tenant,
            user=self.user,
            role="student",
        ).update(is_active=False)
        inactive_membership = self._get_playback(
            enrollment_id=self.target_enrollment.id
        )

        self.assertEqual(expired.status_code, 403)
        self.assertEqual(revoked.status_code, 403)
        self.assertEqual(inactive_account.status_code, 403)
        self.assertEqual(inactive_membership.status_code, 403)

    def test_legacy_video_access_override_never_grants_inactive_enrollment(self):
        SessionEnrollment.objects.get_or_create(
            tenant=self.tenant,
            session=self.target_session,
            enrollment=self.target_enrollment,
        )
        self.target_enrollment.status = "INACTIVE"
        self.target_enrollment.save(update_fields=["status"])
        VideoAccess.objects.create(
            video=self.video,
            enrollment=self.target_enrollment,
            rule="free",
            access_mode=AccessMode.FREE_REVIEW,
            is_override=True,
        )

        response = self._get_playback(enrollment_id=self.target_enrollment.id)

        self.assertEqual(response.status_code, 403)

    def test_blocked_video_access_wins_over_inactive_entitlement(self):
        SessionEnrollment.objects.get_or_create(
            tenant=self.tenant,
            session=self.target_session,
            enrollment=self.target_enrollment,
        )
        self.target_enrollment.status = "INACTIVE"
        self.target_enrollment.save(update_fields=["status"])
        InactiveVideoEntitlement.objects.create(
            tenant=self.tenant,
            student=self.student,
            enrollment=self.target_enrollment,
            video=self.video,
            access_mode=AccessMode.PROCTORED_CLASS,
            source=InactiveVideoEntitlement.Source.STAFF_AUTHORIZATION,
            source_reference="test:paid-session-1",
            reason="Exact first-session video access",
            granted_by_reference="test:staff-1",
            expires_at=timezone.now() + timedelta(days=7),
        )
        VideoAccess.objects.create(
            video=self.video,
            enrollment=self.target_enrollment,
            rule="blocked",
            access_mode=AccessMode.BLOCKED,
            is_override=True,
        )

        overview = self._get_me()
        playback = self._get_playback(enrollment_id=self.target_enrollment.id)

        self.assertEqual(overview.status_code, 200, overview.data)
        self.assertNotIn(
            self.target_lecture.id,
            [row["id"] for row in overview.data["lectures"]],
        )
        self.assertEqual(playback.status_code, 403)

    def test_active_enrollment_grant_is_staged_until_exact_enrollment_is_inactive(self):
        staff = User.objects.create_user(
            username="student-video-entitlement-staging-staff",
            password="testpass123",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=staff,
            role="admin",
        )
        SessionEnrollment.objects.get_or_create(
            tenant=self.tenant,
            session=self.target_session,
            enrollment=self.target_enrollment,
        )
        VideoAccess.objects.create(
            video=self.video,
            enrollment=self.target_enrollment,
            rule="free",
            access_mode=AccessMode.FREE_REVIEW,
            is_override=True,
        )
        request = self.factory.post(
            "/api/v1/media/inactive-video-entitlements/",
            {
                "student_id": self.student.id,
                "enrollment_id": self.target_enrollment.id,
                "video_id": self.video.id,
                "access_mode": AccessMode.PROCTORED_CLASS,
                "source": InactiveVideoEntitlement.Source.STAFF_AUTHORIZATION,
                "source_reference": "test:staged-explicit-authorization",
                "reason": "Stage exact access before secession restoration",
            },
            format="json",
        )
        request.tenant = self.tenant
        force_authenticate(request, user=staff)

        grant = InactiveVideoEntitlementViewSet.as_view({"post": "create"})(request)
        active_check = self._get_playback(
            enrollment_id=self.target_enrollment.id,
            access_check=True,
        )

        self.assertEqual(grant.status_code, 201, grant.data)
        self.assertEqual(grant.data["entitlement"]["state"], "STAGED")
        self.assertEqual(active_check.status_code, 200, active_check.data)
        self.assertEqual(active_check.data["access_mode"], AccessMode.FREE_REVIEW)

        self.target_enrollment.status = "INACTIVE"
        self.target_enrollment.save(update_fields=["status"])
        inactive_check = self._get_playback(
            enrollment_id=self.target_enrollment.id,
            access_check=True,
        )

        self.assertEqual(inactive_check.status_code, 200, inactive_check.data)
        self.assertEqual(
            inactive_check.data["access_mode"],
            AccessMode.PROCTORED_CLASS,
        )

    def test_staged_grant_and_revoke_do_not_invalidate_active_playback_token(self):
        staff = User.objects.create_user(
            username="student-video-entitlement-active-token-staff",
            password="testpass123",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=staff,
            role="admin",
        )
        SessionEnrollment.objects.get_or_create(
            tenant=self.tenant,
            session=self.target_session,
            enrollment=self.target_enrollment,
        )
        playback = self._get_playback(enrollment_id=self.target_enrollment.id)
        self.assertEqual(playback.status_code, 200, playback.data)
        valid, token_payload, token_error = verify_playback_token(
            playback.data["playback_token"]
        )
        self.assertTrue(valid, token_error)
        self.video.refresh_from_db()
        policy_version = self.video.policy_version

        grant_request = self.factory.post(
            "/api/v1/media/inactive-video-entitlements/",
            {
                "student_id": self.student.id,
                "enrollment_id": self.target_enrollment.id,
                "video_id": self.video.id,
                "access_mode": AccessMode.FREE_REVIEW,
                "source": InactiveVideoEntitlement.Source.STAFF_AUTHORIZATION,
                "source_reference": "test:staged-active-token",
                "reason": "Stage without changing active playback policy",
            },
            format="json",
        )
        grant_request.tenant = self.tenant
        force_authenticate(grant_request, user=staff)
        grant = InactiveVideoEntitlementViewSet.as_view({"post": "create"})(
            grant_request
        )
        self.assertEqual(grant.status_code, 201, grant.data)
        self.video.refresh_from_db()
        self.assertEqual(self.video.policy_version, policy_version)
        self.assertTrue(_is_policy_token_valid(token_payload))

        entitlement_id = grant.data["entitlement"]["id"]
        revoke_request = self.factory.post(
            f"/api/v1/media/inactive-video-entitlements/{entitlement_id}/revoke/",
            {"reason": "Remove staged access"},
            format="json",
        )
        revoke_request.tenant = self.tenant
        force_authenticate(revoke_request, user=staff)
        revoke = InactiveVideoEntitlementViewSet.as_view({"post": "revoke"})(
            revoke_request,
            pk=entitlement_id,
        )
        self.assertEqual(revoke.status_code, 200, revoke.data)
        self.video.refresh_from_db()
        self.assertEqual(self.video.policy_version, policy_version)
        self.assertTrue(_is_policy_token_valid(token_payload))

    def test_staff_grant_and_revoke_are_exact_idempotent_and_auditable(self):
        staff = User.objects.create_user(
            username="student-video-entitlement-staff",
            password="testpass123",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=staff,
            role="admin",
        )
        SessionEnrollment.objects.get_or_create(
            tenant=self.tenant,
            session=self.target_session,
            enrollment=self.target_enrollment,
        )
        self.target_enrollment.status = "INACTIVE"
        self.target_enrollment.save(update_fields=["status"])
        payload = {
            "student_id": self.student.id,
            "enrollment_id": self.target_enrollment.id,
            "video_id": self.video.id,
            "access_mode": AccessMode.PROCTORED_CLASS,
            "source": InactiveVideoEntitlement.Source.STAFF_AUTHORIZATION,
            "source_reference": "test:explicit-operator-authorization",
            "reason": "Exact first-session video access",
            "expires_at": (timezone.now() + timedelta(days=7)).isoformat(),
        }

        def grant():
            request = self.factory.post(
                "/api/v1/media/inactive-video-entitlements/",
                payload,
                format="json",
            )
            request.tenant = self.tenant
            force_authenticate(request, user=staff)
            return InactiveVideoEntitlementViewSet.as_view({"post": "create"})(request)

        first = grant()
        duplicate = grant()

        self.assertEqual(first.status_code, 201, first.data)
        self.assertTrue(first.data["created"])
        self.assertEqual(duplicate.status_code, 200, duplicate.data)
        self.assertFalse(duplicate.data["created"])
        self.assertFalse(duplicate.data["changed"])
        self.assertEqual(InactiveVideoEntitlement.objects.count(), 1)
        original = InactiveVideoEntitlement.objects.get()
        payload["reason"] = "Updated exact first-session authorization"
        updated = grant()
        self.assertEqual(updated.status_code, 201, updated.data)
        original.refresh_from_db()
        self.assertIsNotNone(original.revoked_at)
        self.assertEqual(original.revoke_reason, "Superseded by updated grant")
        self.assertEqual(InactiveVideoEntitlement.objects.count(), 2)
        entitlement = InactiveVideoEntitlement.objects.get(revoked_at__isnull=True)
        self.assertEqual(entitlement.granted_by_id, staff.id)
        self.assertEqual(entitlement.granted_by_reference, f"user:{staff.id}")
        self.assertEqual(entitlement.reason, payload["reason"])
        playback = self._get_playback(enrollment_id=self.target_enrollment.id)
        self.assertEqual(playback.status_code, 200, playback.data)
        token_ok, token_payload, _token_error = verify_playback_token(
            playback.data["playback_token"]
        )
        self.assertTrue(token_ok)
        self.assertTrue(_is_policy_token_valid(token_payload))
        legacy_request = self.factory.post(
            "/api/v1/media/playback/start/",
            {
                "video_id": self.video.id,
                "enrollment_id": self.target_enrollment.id,
                "device_id": "test-device",
            },
            format="json",
        )
        legacy_request.tenant = self.tenant
        force_authenticate(legacy_request, user=self.user)
        legacy_playback = PlaybackStartView.as_view()(legacy_request)
        self.assertEqual(legacy_playback.status_code, 201, legacy_playback.data)
        self.assertEqual(
            legacy_playback.data["access_mode"],
            AccessMode.PROCTORED_CLASS,
        )

        revoke_payload = {"reason": "Operator revoked exact access"}

        def revoke():
            request = self.factory.post(
                f"/api/v1/media/inactive-video-entitlements/{entitlement.id}/revoke/",
                revoke_payload,
                format="json",
            )
            request.tenant = self.tenant
            force_authenticate(request, user=staff)
            return InactiveVideoEntitlementViewSet.as_view({"post": "revoke"})(
                request,
                pk=entitlement.id,
            )

        first_revoke = revoke()
        duplicate_revoke = revoke()

        self.assertEqual(first_revoke.status_code, 200, first_revoke.data)
        self.assertTrue(first_revoke.data["changed"])
        self.assertEqual(duplicate_revoke.status_code, 200, duplicate_revoke.data)
        self.assertFalse(duplicate_revoke.data["changed"])
        entitlement.refresh_from_db()
        self.assertEqual(entitlement.revoked_by_id, staff.id)
        self.assertEqual(entitlement.revoke_reason, revoke_payload["reason"])
        self.assertFalse(_is_policy_token_valid(token_payload))
        denied = self._get_playback(enrollment_id=self.target_enrollment.id)
        self.assertEqual(denied.status_code, 403)

        regrant = grant()
        self.assertEqual(regrant.status_code, 201, regrant.data)
        self.assertEqual(InactiveVideoEntitlement.objects.count(), 3)
        self.assertEqual(
            InactiveVideoEntitlement.objects.filter(revoked_at__isnull=True).count(),
            1,
        )

    def test_staff_grant_rejects_cross_tenant_and_wrong_session_scope(self):
        staff = User.objects.create_user(
            username="student-video-entitlement-scope-staff",
            password="testpass123",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=staff,
            role="admin",
        )
        other_lecture = Lecture.objects.create(
            tenant=self.tenant,
            title="Other Lecture",
            name="Other Lecture",
            subject="MATH",
        )
        other_session = Session.objects.create(
            lecture=other_lecture,
            title="Other Session",
            order=1,
        )
        wrong_video = Video.objects.create(
            tenant=self.tenant,
            session=other_session,
            title="Wrong Lecture Video",
            status=Video.Status.READY,
        )
        self.target_enrollment.status = "INACTIVE"
        self.target_enrollment.save(update_fields=["status"])

        def post(tenant, video_id):
            request = self.factory.post(
                "/api/v1/media/inactive-video-entitlements/",
                {
                    "student_id": self.student.id,
                    "enrollment_id": self.target_enrollment.id,
                    "video_id": video_id,
                    "access_mode": AccessMode.PROCTORED_CLASS,
                    "source": InactiveVideoEntitlement.Source.STAFF_AUTHORIZATION,
                    "source_reference": "test:explicit-operator-authorization",
                    "reason": "Exact first-session video access",
                },
                format="json",
            )
            request.tenant = tenant
            force_authenticate(request, user=staff)
            return InactiveVideoEntitlementViewSet.as_view({"post": "create"})(request)

        SessionEnrollment.objects.filter(
            tenant=self.tenant, session=self.target_session, enrollment=self.target_enrollment,
        ).delete()
        no_session_scope = post(self.tenant, self.video.id)
        wrong_lecture = post(self.tenant, wrong_video.id)
        other_tenant = Tenant.objects.create(
            code="student-video-entitlement-other",
            name="Student Video Entitlement Other",
            is_active=True,
        )
        cross_tenant = post(other_tenant, self.video.id)

        self.assertEqual(no_session_scope.status_code, 400, no_session_scope.data)
        self.assertEqual(no_session_scope.data["code"], "session_scope_missing")
        self.assertEqual(wrong_lecture.status_code, 400, wrong_lecture.data)
        self.assertEqual(wrong_lecture.data["code"], "video_scope_mismatch")
        self.assertIn(cross_tenant.status_code, (403, 404), cross_tenant.data)
        self.assertFalse(InactiveVideoEntitlement.objects.exists())

    def test_staff_entitlement_api_errors_use_stable_code_detail_shape(self):
        staff = User.objects.create_user(
            username="student-video-entitlement-error-staff",
            password="testpass123",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=staff,
            role="admin",
        )

        invalid_create_request = self.factory.post(
            "/api/v1/media/inactive-video-entitlements/",
            {},
            format="json",
        )
        invalid_create_request.tenant = self.tenant
        force_authenticate(invalid_create_request, user=staff)
        invalid_create = InactiveVideoEntitlementViewSet.as_view(
            {"post": "create"}
        )(invalid_create_request)

        invalid_list_request = self.factory.get(
            "/api/v1/media/inactive-video-entitlements/?student_id=bad"
        )
        invalid_list_request.tenant = self.tenant
        force_authenticate(invalid_list_request, user=staff)
        invalid_list = InactiveVideoEntitlementViewSet.as_view(
            {"get": "list"}
        )(invalid_list_request)

        forbidden_request = self.factory.get(
            "/api/v1/media/inactive-video-entitlements/"
        )
        forbidden_request.tenant = self.tenant
        force_authenticate(forbidden_request, user=self.user)
        forbidden = InactiveVideoEntitlementViewSet.as_view(
            {"get": "list"}
        )(forbidden_request)

        missing_revoke_request = self.factory.post(
            "/api/v1/media/inactive-video-entitlements/missing/revoke/",
            {"reason": "Explicit revoke"},
            format="json",
        )
        missing_revoke_request.tenant = self.tenant
        force_authenticate(missing_revoke_request, user=staff)
        missing_revoke = InactiveVideoEntitlementViewSet.as_view(
            {"post": "revoke"}
        )(missing_revoke_request, pk="missing")

        missing_retrieve_request = self.factory.get(
            "/api/v1/media/inactive-video-entitlements/missing/"
        )
        missing_retrieve_request.tenant = self.tenant
        force_authenticate(missing_retrieve_request, user=staff)
        missing_retrieve = InactiveVideoEntitlementViewSet.as_view(
            {"get": "retrieve"}
        )(missing_retrieve_request, pk="missing")

        for response, expected_status in (
            (invalid_create, 400),
            (invalid_list, 400),
            (forbidden, 403),
            (missing_revoke, 404),
            (missing_retrieve, 404),
        ):
            with self.subTest(status=expected_status):
                self.assertEqual(response.status_code, expected_status, response.data)
                self.assertEqual(set(response.data), {"code", "detail"})

    def test_staff_entitlement_rejects_youtube_source_fail_closed(self):
        staff = User.objects.create_user(
            username="student-video-entitlement-youtube-staff",
            password="testpass123",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=staff,
            role="admin",
        )
        SessionEnrollment.objects.get_or_create(
            tenant=self.tenant,
            session=self.target_session,
            enrollment=self.target_enrollment,
        )
        self.target_enrollment.status = "INACTIVE"
        self.target_enrollment.save(update_fields=["status"])
        youtube_video = Video.objects.create(
            tenant=self.tenant,
            session=self.target_session,
            title="Unrevocable YouTube Video",
            order=2,
            status=Video.Status.READY,
            source_type=Video.SourceType.YOUTUBE,
            youtube_video_id="youtube-test-id",
        )
        request = self.factory.post(
            "/api/v1/media/inactive-video-entitlements/",
            {
                "student_id": self.student.id,
                "enrollment_id": self.target_enrollment.id,
                "video_id": youtube_video.id,
                "access_mode": AccessMode.FREE_REVIEW,
                "source": InactiveVideoEntitlement.Source.STAFF_AUTHORIZATION,
                "source_reference": "test:youtube-must-fail",
                "reason": "Must not grant an unrevocable media source",
            },
            format="json",
        )
        request.tenant = self.tenant
        force_authenticate(request, user=staff)

        response = InactiveVideoEntitlementViewSet.as_view({"post": "create"})(
            request
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data["code"], "video_source_unsupported")
        self.assertFalse(InactiveVideoEntitlement.objects.exists())
        InactiveVideoEntitlement.objects.create(
            tenant=self.tenant,
            student=self.student,
            enrollment=self.target_enrollment,
            video=youtube_video,
            access_mode=AccessMode.FREE_REVIEW,
            source=InactiveVideoEntitlement.Source.STAFF_AUTHORIZATION,
            source_reference="test:legacy-youtube-row",
            reason="Legacy row must still fail at runtime",
            granted_by_reference=f"user:{staff.id}",
            expires_at=timezone.now() + timedelta(hours=1),
        )

        runtime = self._get_playback(
            enrollment_id=self.target_enrollment.id,
            video=youtube_video,
        )

        self.assertEqual(runtime.status_code, 403, runtime.data)

    def test_progress_without_explicit_enrollment_uses_video_lecture_enrollment(self):
        response = self._post_progress({
            "progress": 50,
            "last_position": 37,
            "completed": False,
        })

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["enrollment_id"], self.target_enrollment.id)

        progress = VideoProgress.objects.get(video=self.video)
        self.assertEqual(progress.enrollment_id, self.target_enrollment.id)
        self.assertEqual(progress.last_position, 37)
        self.assertAlmostEqual(progress.progress, 0.5)
        self.assertFalse(
            VideoProgress.objects.filter(
                video=self.video,
                enrollment=self.old_enrollment,
            ).exists()
        )

    def test_progress_response_uses_domain_completion_threshold(self):
        response = self._post_progress({
            "progress": 90,
            "last_position": 90,
            "completed": False,
        })

        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["completed"])

    def test_public_video_progress_persists_with_hidden_enrollment(self):
        public_lecture = Lecture.get_or_create_system_lecture(self.tenant)
        public_video = Video.objects.create(
            tenant=self.tenant,
            session=None,
            title="Public Progress Video",
            status=Video.Status.READY,
            visibility=Video.Visibility.PUBLIC,
            duration=100,
        )

        response = self._post_progress(
            {"progress": 90, "last_position": 90, "completed": False},
            video=public_video,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotEqual(response.data["enrollment_id"], 0)
        self.assertTrue(response.data["completed"])
        public_video.refresh_from_db()
        self.assertEqual(public_video.session.lecture_id, public_lecture.id)
        self.assertTrue(
            VideoProgress.objects.filter(
                video=public_video,
                enrollment_id=response.data["enrollment_id"],
            ).exists()
        )

        me_response = self._get_me()
        self.assertEqual(me_response.status_code, 200, me_response.data)
        self.assertEqual(me_response.data["public"]["lecture_id"], public_lecture.id)
        self.assertNotIn(
            public_lecture.id,
            [lecture["id"] for lecture in me_response.data["lectures"]],
        )

        stats_response = self._get_me_stats()
        self.assertEqual(stats_response.status_code, 200, stats_response.data)
        self.assertEqual(stats_response.data["total_videos"], 2)
        self.assertEqual(stats_response.data["completed_videos"], 1)

    @override_settings(CDN_HLS_BASE_URL="https://cdn.example.test", CDN_HLS_SIGNING_SECRET="")
    def test_public_video_playback_uses_free_review_policy(self):
        public_video = Video.objects.create(
            tenant=self.tenant,
            session=None,
            title="Public Playback Video",
            status=Video.Status.READY,
            visibility=Video.Visibility.PUBLIC,
            duration=100,
        )

        response = self._get_playback(video=public_video)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNone(response.data["video"]["access_mode"])
        self.assertEqual(response.data["policy"]["access_mode"], AccessMode.FREE_REVIEW.value)
        self.assertFalse(response.data["policy"]["monitoring_enabled"])
        self.assertEqual(response.data["policy"]["seek"]["mode"], "free")

    def test_inactive_system_public_lecture_keeps_free_playback_and_heartbeat(self):
        self.target_lecture.is_system = True
        self.target_lecture.is_active = False
        self.target_lecture.save(update_fields=["is_system", "is_active"])
        self.video.visibility = Video.Visibility.PUBLIC
        self.video.save(update_fields=["visibility"])
        SessionEnrollment.objects.get_or_create(
            tenant=self.tenant,
            session=self.target_session,
            enrollment=self.target_enrollment,
        )

        access_check = self._get_playback(
            enrollment_id=self.target_enrollment.id,
            access_check=True,
        )
        bootstrap = self._get_playback(enrollment_id=self.target_enrollment.id)
        start_request = self.factory.post(
            "/api/v1/media/playback/start/",
            {
                "video_id": self.video.id,
                "enrollment_id": self.target_enrollment.id,
                "device_id": "system-public-device",
            },
            format="json",
        )
        start_request.tenant = self.tenant
        force_authenticate(start_request, user=self.user)
        start = PlaybackStartView.as_view()(start_request)
        heartbeat_request = self.factory.post(
            "/api/v1/media/playback/heartbeat/",
            {"token": start.data.get("token")},
            format="json",
        )
        heartbeat_request.tenant = self.tenant
        force_authenticate(heartbeat_request, user=self.user)
        heartbeat = PlaybackHeartbeatView.as_view()(heartbeat_request)

        self.assertEqual(access_check.status_code, 200, access_check.data)
        self.assertEqual(access_check.data["access_mode"], AccessMode.FREE_REVIEW)
        self.assertEqual(bootstrap.status_code, 200, bootstrap.data)
        self.assertEqual(bootstrap.data["policy"]["access_mode"], AccessMode.FREE_REVIEW)
        self.assertEqual(start.status_code, 201, start.data)
        self.assertEqual(start.data["access_mode"], AccessMode.FREE_REVIEW)
        self.assertEqual(heartbeat.status_code, 200, heartbeat.data)

    def test_student_stats_uses_domain_completion_threshold(self):
        VideoProgress.objects.create(
            video=self.video,
            enrollment=self.target_enrollment,
            progress=0.9,
            completed=False,
        )

        response = self._get_me_stats()

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["total_videos"], 1)
        self.assertEqual(response.data["completed_videos"], 1)
        self.assertEqual(response.data["completion_rate"], 100)
        self.assertEqual(response.data["lectures"][0]["completed_count"], 1)

    def test_student_stats_counts_ready_videos_without_progress(self):
        Video.objects.create(
            tenant=self.tenant,
            session=self.target_session,
            title="Unwatched Target Video",
            status=Video.Status.READY,
            duration=200,
            order=2,
        )
        VideoProgress.objects.create(
            video=self.video,
            enrollment=self.target_enrollment,
            progress=0.9,
            completed=False,
        )

        response = self._get_me_stats()

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["total_videos"], 2)
        self.assertEqual(response.data["completed_videos"], 1)
        self.assertEqual(response.data["completion_rate"], 50)
        self.assertEqual(response.data["total_watch_duration"], 90)
        self.assertEqual(response.data["total_content_duration"], 300)
        self.assertEqual(response.data["lectures"][0]["video_count"], 2)
        self.assertEqual(response.data["lectures"][0]["completed_count"], 1)
        self.assertEqual(response.data["lectures"][0]["progress_pct"], 50)

    def test_video_me_hides_inactive_enrollment_lecture(self):
        self.target_enrollment.status = "INACTIVE"
        self.target_enrollment.save(update_fields=["status", "updated_at"])

        response = self._get_me()

        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn(
            self.target_lecture.id,
            [lecture["id"] for lecture in response.data["lectures"]],
        )

    def test_student_stats_ignore_inactive_enrollment_videos_and_progress(self):
        VideoProgress.objects.create(
            video=self.video,
            enrollment=self.target_enrollment,
            progress=0.9,
            completed=False,
        )
        self.target_enrollment.status = "INACTIVE"
        self.target_enrollment.save(update_fields=["status", "updated_at"])

        response = self._get_me_stats()

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["total_videos"], 0)
        self.assertEqual(response.data["completed_videos"], 0)
        self.assertEqual(response.data["lectures"], [])

    def test_session_video_list_uses_prefetched_completion_and_access_modes(self):
        second_video = Video.objects.create(
            tenant=self.tenant,
            session=self.target_session,
            title="Second Target Video",
            status=Video.Status.READY,
            duration=100,
            order=2,
        )
        Attendance.objects.create(
            tenant=self.tenant,
            session=self.target_session,
            enrollment=self.target_enrollment,
            status="ONLINE",
        )
        VideoProgress.objects.create(
            video=self.video,
            enrollment=self.target_enrollment,
            progress=0.9,
            completed=False,
        )

        response = self._get_session_videos(enrollment_id=self.target_enrollment.id)

        self.assertEqual(response.status_code, 200, response.data)
        rows = {row["id"]: row for row in response.data["items"]}
        self.assertTrue(rows[self.video.id]["completed"])
        self.assertEqual(rows[self.video.id]["access_mode"], AccessMode.FREE_REVIEW.value)
        self.assertFalse(rows[second_video.id]["completed"])
        self.assertEqual(rows[second_video.id]["access_mode"], AccessMode.PROCTORED_CLASS.value)

    def test_progress_body_enrollment_id_is_validated_against_video_lecture(self):
        response = self._post_progress({
            "enrollment_id": self.old_enrollment.id,
            "progress": 50,
        })

        self.assertEqual(response.status_code, 400)
        self.assertFalse(VideoProgress.objects.filter(video=self.video).exists())

    def test_blocked_access_mode_rejects_progress_even_when_legacy_rule_is_free(self):
        VideoAccess.objects.create(
            video=self.video,
            enrollment=self.target_enrollment,
            rule="free",
            access_mode=AccessMode.BLOCKED,
        )

        response = self._post_progress({"progress": 50})

        self.assertEqual(response.status_code, 403)
        self.assertFalse(VideoProgress.objects.filter(video=self.video).exists())

    def test_explicit_access_override_resolves_to_access_mode_enum(self):
        VideoAccess.objects.create(
            video=self.video,
            enrollment=self.target_enrollment,
            rule="once",
            access_mode=AccessMode.PROCTORED_CLASS,
            is_override=True,
        )

        resolved = get_effective_access_mode(
            video=self.video,
            enrollment=self.target_enrollment,
        )

        self.assertIsInstance(resolved, AccessMode)
        self.assertEqual(resolved, AccessMode.PROCTORED_CLASS)

    def test_blocked_access_mode_rejects_playback_even_when_legacy_rule_is_free(self):
        VideoAccess.objects.create(
            video=self.video,
            enrollment=self.target_enrollment,
            rule="free",
            access_mode=AccessMode.BLOCKED,
        )

        response = self._get_playback(enrollment_id=self.target_enrollment.id)
        access_check = self._get_playback(
            enrollment_id=self.target_enrollment.id,
            access_check=True,
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(access_check.status_code, 403)

    def test_active_override_access_check_matches_playback(self):
        VideoAccess.objects.create(
            video=self.video,
            enrollment=self.target_enrollment,
            rule="once",
            access_mode=AccessMode.PROCTORED_CLASS,
            is_override=True,
        )

        access_check = self._get_playback(
            enrollment_id=self.target_enrollment.id,
            access_check=True,
        )
        playback = self._get_playback(enrollment_id=self.target_enrollment.id)

        self.assertEqual(access_check.status_code, 200, access_check.data)
        self.assertEqual(playback.status_code, 200, playback.data)
        self.assertEqual(
            access_check.data["access_mode"],
            playback.data["policy"]["access_mode"],
        )
        self.assertEqual(
            access_check.data["monitoring_enabled"],
            playback.data["policy"]["monitoring_enabled"],
        )
        self.assertEqual(
            access_check.data["policy_version"],
            playback.data["policy_version"],
        )

    @override_settings(
        CDN_HLS_BASE_URL="https://cdn.hakwonplus.com",
        CDN_HLS_SIGNING_SECRET="test-production-video-signing-secret",
        CDN_HLS_SIGNING_KEY_ID="v1",
    )
    def test_playback_uses_canonical_signed_cdn_url(self):
        response = self._get_playback(enrollment_id=self.target_enrollment.id)

        self.assertEqual(response.status_code, 200, response.data)
        parsed = urlparse(response.data["play_url"])
        query = parse_qs(parsed.query)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "cdn.hakwonplus.com")
        self.assertEqual(query["kid"], ["v1"])
        self.assertEqual(query["uid"], [str(self.user.id)])
        self.assertEqual(len(query["exp"]), 1)
        self.assertGreater(int(query["exp"][0]), 0)
        self.assertEqual(len(query["sig"]), 1)
        self.assertGreaterEqual(len(query["sig"][0]), 32)

    @override_settings(
        CDN_HLS_BASE_URL="https://cdn.hakwonplus.com",
        CDN_HLS_SIGNING_SECRET="test-production-video-signing-secret",
        VIDEO_PLAYBACK_TTL_SECONDS=600,
    )
    def test_active_signed_url_is_bounded_by_duration_plus_access_grace(self):
        request = self.factory.get("/api/v1/student/video/")
        force_authenticate(request, user=self.user)
        now = int(timezone.now().timestamp())

        hls_url, _ = pick_video_urls(self.video, request=request)
        expires_at = int(parse_qs(urlparse(hls_url).query)["exp"][0])

        self.assertGreaterEqual(expires_at, now + self.video.duration + 598)
        self.assertLessEqual(expires_at, now + self.video.duration + 602)

        self.video.duration = None
        self.video.save(update_fields=["duration"])
        legacy_url, _ = pick_video_urls(self.video, request=request)
        legacy_expires_at = int(parse_qs(urlparse(legacy_url).query)["exp"][0])
        self.assertLessEqual(
            legacy_expires_at,
            int(timezone.now().timestamp()) + 86400,
        )

    @override_settings(CDN_HLS_BASE_URL="https://cdn.example.test", CDN_HLS_SIGNING_SECRET="")
    def test_free_review_default_allows_free_seeking(self):
        self.video.allow_skip = False
        self.video.max_speed = 1.0
        self.video.show_watermark = True
        self.video.save(update_fields=["allow_skip", "max_speed", "show_watermark"])

        response = self._get_playback(enrollment_id=self.target_enrollment.id)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["policy"]["access_mode"], AccessMode.FREE_REVIEW.value)
        self.assertFalse(response.data["policy"]["monitoring_enabled"])
        self.assertTrue(response.data["policy"]["allow_seek"])
        self.assertEqual(response.data["policy"]["seek"]["mode"], "free")
        self.assertIsNone(response.data["policy"]["seek"]["forward_limit"])
        self.assertEqual(response.data["policy"]["playback_rate"]["max"], 1.0)
        self.assertTrue(response.data["policy"]["watermark"]["enabled"])

    @override_settings(CDN_HLS_BASE_URL="https://cdn.example.test", CDN_HLS_SIGNING_SECRET="")
    def test_free_review_playback_allows_explicit_relaxed_video_controls(self):
        self.video.allow_skip = True
        self.video.max_speed = 2.0
        self.video.show_watermark = False
        self.video.save(update_fields=["allow_skip", "max_speed", "show_watermark"])

        response = self._get_playback(enrollment_id=self.target_enrollment.id)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["policy"]["allow_seek"])
        self.assertEqual(response.data["policy"]["seek"]["mode"], "free")
        self.assertEqual(response.data["policy"]["playback_rate"]["max"], 2.0)
        self.assertFalse(response.data["policy"]["watermark"]["enabled"])

    @override_settings(
        CDN_HLS_BASE_URL="https://cdn.example.test",
        CDN_HLS_SIGNING_SECRET="test-production-video-signing-secret",
        VIDEO_PLAYBACK_TTL_SECONDS=600,
    )
    def test_proctored_playback_issues_session_with_aware_expiry(self):
        Attendance.objects.create(
            tenant=self.tenant,
            session=self.target_session,
            enrollment=self.target_enrollment,
            status="ONLINE",
        )

        now = int(timezone.now().timestamp())
        response = self._get_playback(enrollment_id=self.target_enrollment.id)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["video"]["access_mode"], AccessMode.PROCTORED_CLASS.value)
        self.assertEqual(response.data["policy"]["access_mode"], AccessMode.PROCTORED_CLASS.value)
        self.assertTrue(response.data["policy"]["allow_seek"])
        self.assertEqual(response.data["policy"]["seek"]["mode"], "budgeted_forward")
        self.assertEqual(response.data["policy"]["seek"]["step_seconds"], 10)
        self.assertEqual(response.data["policy"]["seek"]["limit_seconds"], 20)
        self.assertEqual(response.data["policy"]["seek"]["remaining_seconds"], 20)
        self.assertIsNotNone(response.data["playback_session_id"])
        self.assertIsNotNone(response.data["playback_token"])
        token_ok, token_payload, token_error = verify_playback_token(
            response.data["playback_token"]
        )
        self.assertTrue(token_ok, token_error)
        self.assertEqual(
            int(token_payload["exp"]),
            int(response.data["playback_expires_at"]),
        )
        self.assertGreaterEqual(int(token_payload["exp"]), now + 598)
        self.assertLessEqual(int(token_payload["exp"]), now + 602)
        media_expiry = int(
            parse_qs(urlparse(response.data["play_url"]).query)["exp"][0]
        )
        self.assertGreaterEqual(media_expiry, now + self.video.duration + 598)
        self.assertLessEqual(media_expiry, now + self.video.duration + 602)
        session = VideoPlaybackSession.objects.get(session_id=response.data["playback_session_id"])
        self.assertEqual(session.video_id, self.video.id)
        self.assertEqual(session.enrollment_id, self.target_enrollment.id)
        self.assertIsNotNone(session.expires_at.tzinfo)
        self.assertEqual(int(session.expires_at.timestamp()), int(token_payload["exp"]))

    @override_settings(CDN_HLS_BASE_URL="https://cdn.example.test", CDN_HLS_SIGNING_SECRET="")
    def test_proctored_playback_honors_teacher_free_seek_setting(self):
        self.video.allow_skip = True
        self.video.save(update_fields=["allow_skip"])
        Attendance.objects.create(
            tenant=self.tenant,
            session=self.target_session,
            enrollment=self.target_enrollment,
            status="ONLINE",
        )

        response = self._get_playback(enrollment_id=self.target_enrollment.id)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["policy"]["access_mode"], AccessMode.PROCTORED_CLASS.value)
        self.assertTrue(response.data["policy"]["monitoring_enabled"])
        self.assertTrue(response.data["policy"]["allow_seek"])
        self.assertEqual(response.data["policy"]["seek"]["mode"], "free")

    @override_settings(CDN_HLS_BASE_URL="https://cdn.example.test", CDN_HLS_SIGNING_SECRET="")
    def test_completed_proctored_progress_restores_free_seek(self):
        Attendance.objects.create(
            tenant=self.tenant,
            session=self.target_session,
            enrollment=self.target_enrollment,
            status="ONLINE",
        )
        VideoProgress.objects.create(
            video=self.video,
            enrollment=self.target_enrollment,
            progress=0.9,
            last_position=90,
            completed=True,
        )

        response = self._get_playback(enrollment_id=self.target_enrollment.id)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["policy"]["access_mode"], AccessMode.FREE_REVIEW.value)
        self.assertFalse(response.data["policy"]["monitoring_enabled"])
        self.assertTrue(response.data["policy"]["allow_seek"])
        self.assertEqual(response.data["policy"]["seek"]["mode"], "free")

    @override_settings(CDN_HLS_BASE_URL="https://cdn.example.test", CDN_HLS_SIGNING_SECRET="")
    def test_proctored_completion_record_restores_free_seek_without_progress(self):
        Attendance.objects.create(
            tenant=self.tenant,
            session=self.target_session,
            enrollment=self.target_enrollment,
            status="ONLINE",
        )
        VideoAccess.objects.create(
            video=self.video,
            enrollment=self.target_enrollment,
            rule="free",
            access_mode=AccessMode.FREE_REVIEW,
            proctored_completed_at=timezone.now(),
        )

        response = self._get_playback(enrollment_id=self.target_enrollment.id)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["policy"]["access_mode"], AccessMode.FREE_REVIEW.value)
        self.assertFalse(response.data["policy"]["monitoring_enabled"])
        self.assertTrue(response.data["policy"]["allow_seek"])
        self.assertEqual(response.data["policy"]["seek"]["mode"], "free")

    @override_settings(CDN_HLS_BASE_URL="https://cdn.example.test", CDN_HLS_SIGNING_SECRET="")
    def test_completed_watch_does_not_override_explicit_seek_block(self):
        Attendance.objects.create(
            tenant=self.tenant,
            session=self.target_session,
            enrollment=self.target_enrollment,
            status="ONLINE",
        )
        VideoProgress.objects.create(
            video=self.video,
            enrollment=self.target_enrollment,
            progress=0.9,
            last_position=90,
            completed=True,
        )
        VideoAccess.objects.create(
            video=self.video,
            enrollment=self.target_enrollment,
            rule="free",
            access_mode=AccessMode.FREE_REVIEW,
            block_seek=True,
            proctored_completed_at=timezone.now(),
        )

        response = self._get_playback(enrollment_id=self.target_enrollment.id)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["policy"]["access_mode"], AccessMode.FREE_REVIEW.value)
        self.assertFalse(response.data["policy"]["allow_seek"])
        self.assertEqual(response.data["policy"]["seek"]["mode"], "blocked")

    @override_settings(CDN_HLS_BASE_URL="https://cdn.example.test", CDN_HLS_SIGNING_SECRET="")
    def test_proctored_forward_skip_is_server_counted_and_survives_playback_reload(self):
        Attendance.objects.create(
            tenant=self.tenant,
            session=self.target_session,
            enrollment=self.target_enrollment,
            status="ONLINE",
        )

        first = self._post_forward_skip(enrollment_id=self.target_enrollment.id)
        second = self._post_forward_skip(enrollment_id=self.target_enrollment.id)
        exhausted = self._post_forward_skip(enrollment_id=self.target_enrollment.id)

        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(first.data["granted_seconds"], 10)
        self.assertEqual(first.data["remaining_seconds"], 10)
        self.assertEqual(second.data["granted_seconds"], 10)
        self.assertEqual(second.data["remaining_seconds"], 0)
        self.assertEqual(exhausted.data["granted_seconds"], 0)
        self.assertEqual(exhausted.data["unavailable_reason"], "limit_reached")

        progress = VideoProgress.objects.get(
            video=self.video,
            enrollment=self.target_enrollment,
        )
        self.assertEqual(progress.forward_skip_seconds_used, 20)

        playback = self._get_playback(enrollment_id=self.target_enrollment.id)
        self.assertEqual(playback.status_code, 200, playback.data)
        self.assertEqual(playback.data["policy"]["seek"]["used_seconds"], 20)
        self.assertEqual(playback.data["policy"]["seek"]["remaining_seconds"], 0)

    def test_free_review_forward_skip_budget_is_not_applicable(self):
        response = self._post_forward_skip(enrollment_id=self.target_enrollment.id)

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["code"], "skip_budget_not_applicable")
        self.assertFalse(
            VideoProgress.objects.filter(video=self.video, enrollment=self.target_enrollment).exists()
        )

    def test_forward_skip_rejects_video_with_explicit_free_seeking(self):
        self.video.allow_skip = True
        self.video.save(update_fields=["allow_skip"])

        response = self._post_forward_skip(enrollment_id=self.target_enrollment.id)

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["code"], "skip_budget_not_applicable")
        self.assertFalse(VideoProgress.objects.filter(video=self.video).exists())

    def test_parent_cannot_consume_student_forward_skip_budget(self):
        Attendance.objects.create(
            tenant=self.tenant,
            session=self.target_session,
            enrollment=self.target_enrollment,
            status="ONLINE",
        )

        response = self._post_forward_skip(
            user=self.parent_user,
            enrollment_id=self.target_enrollment.id,
            selected_student_id=self.student.id,
        )

        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(response.data["code"], "student_only")
        self.assertFalse(VideoProgress.objects.filter(video=self.video).exists())

    def test_parent_progress_echo_requires_selected_child_video_enrollment(self):
        unlinked_parent_user = User.objects.create_user(
            username="student-video-unlinked-parent",
            password="testpass123",
            tenant=self.tenant,
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=unlinked_parent_user, role="parent")
        Parent.objects.create(
            tenant=self.tenant,
            user=unlinked_parent_user,
            name="Unlinked Parent",
            phone="01055556666",
        )

        response = self._post_progress({"progress": 50}, user=unlinked_parent_user)

        self.assertEqual(response.status_code, 403)
        self.assertFalse(VideoProgress.objects.filter(video=self.video).exists())

    def test_parent_progress_persists_for_selected_child_and_survives_reload(self):
        response = self._post_progress(
            {"progress": 90, "last_position": 90, "completed": True},
            user=self.parent_user,
            selected_student_id=self.student.id,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["enrollment_id"], self.target_enrollment.id)
        self.assertEqual(response.data["progress_percent"], 90)
        self.assertTrue(response.data["completed"])
        progress = VideoProgress.objects.get(
            video=self.video,
            enrollment=self.target_enrollment,
        )
        self.assertEqual(progress.progress, 0.9)
        self.assertTrue(progress.completed)
        self.assertEqual(progress.last_position, 90)

        reload_response = self._get_session_videos(
            user=self.parent_user,
            enrollment_id=self.target_enrollment.id,
            selected_student_id=self.student.id,
        )
        self.assertEqual(reload_response.status_code, 200, reload_response.data)
        reloaded = next(
            row
            for row in reload_response.data["items"]
            if row["id"] == self.video.id
        )
        self.assertEqual(reloaded["progress"], 90)
        self.assertTrue(reloaded["completed"])

    def test_parent_progress_echo_uses_domain_completion_threshold(self):
        response = self._post_progress(
            {"progress": 90, "last_position": 90, "completed": False},
            user=self.parent_user,
            selected_student_id=self.student.id,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["enrollment_id"], self.target_enrollment.id)
        self.assertTrue(response.data["completed"])
        progress = VideoProgress.objects.get(
            video=self.video,
            enrollment=self.target_enrollment,
        )
        self.assertEqual(progress.progress, 0.9)
        self.assertFalse(progress.completed)

    def test_parent_progress_requires_explicit_selected_child_when_multiple_children_exist(self):
        self._create_parent_child("002")

        response = self._post_progress(
            {"progress": 90, "last_position": 90, "completed": True},
            user=self.parent_user,
        )

        self.assertEqual(response.status_code, 403, response.data)
        self.assertFalse(VideoProgress.objects.filter(video=self.video).exists())

    def test_parent_progress_rejects_body_enrollment_without_selected_child_header(self):
        response = self._post_progress(
            {"enrollment_id": self.target_enrollment.id, "progress": 90, "completed": True},
            user=self.parent_user,
        )

        self.assertEqual(response.status_code, 403, response.data)
        self.assertFalse(VideoProgress.objects.filter(video=self.video).exists())

    def test_parent_progress_echo_rejects_explicit_enrollment_for_different_selected_child(self):
        other_child = self._create_parent_child("003")

        response = self._post_progress(
            {"enrollment_id": self.target_enrollment.id, "progress": 90, "completed": True},
            user=self.parent_user,
            selected_student_id=other_child.id,
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(VideoProgress.objects.filter(video=self.video).exists())
