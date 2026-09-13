from unittest.mock import patch

from rest_framework.test import APITestCase

from apps.core.models import Tenant, TenantMembership, User
from apps.domains.enrollment.test_support import (
    create_enrollment_fixture,
    create_session_enrollment_fixture,
)
from apps.domains.lectures.test_support import create_lecture_fixture, create_session_fixture
from apps.domains.students.test_support import create_student_fixture
from apps.domains.video.models import Video


class SessionVideoEntitlementTests(APITestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(code="qa-session-video", name="QA", is_active=True)
        self.user = User.objects.create_user(username="qa-video-student", tenant=self.tenant)
        TenantMembership.ensure_active(tenant=self.tenant, user=self.user, role="student")
        self.student = create_student_fixture(
            tenant=self.tenant, user=self.user, name="QA Student", ps_number="QAV001",
            omr_code="12345678", parent_phone="01000000000",
        )
        self.lecture = create_lecture_fixture(
            tenant=self.tenant, title="QA Lecture", name="QA Lecture", subject="MATH",
        )
        self.enrollment = create_enrollment_fixture(
            tenant=self.tenant, student=self.student, lecture=self.lecture, status="ACTIVE",
        )
        self.sessions = []
        self.videos = []
        for order in range(1, 9):
            session = create_session_fixture(lecture=self.lecture, title=f"Session {order}", order=order)
            self.sessions.append(session)
            self.videos.append(Video.objects.create(
                tenant=self.tenant, session=session, title=f"Video {order}",
                status=Video.Status.READY, duration=600,
            ))
        self.membership = create_session_enrollment_fixture(
            tenant=self.tenant, session=self.sessions[2], enrollment=self.enrollment,
        )
        self.client.force_authenticate(self.user)
        self.headers = {"HTTP_HOST": "localhost", "HTTP_X_TENANT_CODE": self.tenant.code}

    def access(self, index):
        return self.client.get(
            f"/api/v1/student/video/videos/{self.videos[index].id}/playback/?access_check=true",
            **self.headers,
        )

    def test_only_registered_session_can_list_and_play(self):
        for index, session in enumerate(self.sessions):
            with self.subTest(session=index + 1):
                expected = 200 if index == 2 else 403
                self.assertEqual(self.access(index).status_code, expected)
                response = self.client.get(
                    f"/api/v1/student/video/sessions/{session.id}/videos/", **self.headers,
                )
                self.assertEqual(response.status_code, expected, response.data)

    def test_home_and_stats_only_include_registered_session(self):
        response = self.client.get("/api/v1/student/video/me/", **self.headers)
        self.assertEqual(response.status_code, 200, response.data)
        lecture = next(row for row in response.data["lectures"] if row["id"] == self.lecture.id)
        self.assertEqual([row["id"] for row in lecture["sessions"]], [self.sessions[2].id])
        self.assertEqual(lecture["video_count"], 1)
        stats = self.client.get("/api/v1/student/video/me/stats/", **self.headers)
        self.assertEqual(stats.data["total_videos"], 1)

    def test_session_removal_revokes_only_that_session(self):
        for session in self.sessions:
            if session != self.sessions[2]:
                create_session_enrollment_fixture(
                    tenant=self.tenant, session=session, enrollment=self.enrollment,
                )
        self.assertEqual(self.access(2).status_code, 200)
        self.membership.delete()
        self.enrollment.refresh_from_db()
        self.assertEqual(self.enrollment.status, "ACTIVE")
        for index in range(8):
            self.assertEqual(self.access(index).status_code, 403 if index == 2 else 200)

    def test_existing_token_and_new_playback_fail_after_session_removal(self):
        with patch("apps.domains.video.services.playback_session.init_session_redis"):
            response = self.client.post(
                f"/api/v1/student/video/videos/{self.videos[2].id}/playback/",
                {"device_id": "qa-device"}, format="json", **self.headers,
            )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["playback_token"])
        token = response.data["playback_token"]
        self.membership.delete()
        repeated = self.client.post(
            f"/api/v1/student/video/videos/{self.videos[2].id}/playback/",
            {"device_id": "qa-device"}, format="json", **self.headers,
        )
        self.assertEqual(repeated.status_code, 403, repeated.data)
        for action in ("refresh", "renew", "heartbeat"):
            response = self.client.post(
                f"/api/v1/media/playback/{action}/", {"token": token},
                format="json", **self.headers,
            )
            self.assertEqual(response.status_code, 403, (action, response.data))

    def test_inactive_lecture_enrollment_revokes_all_registered_sessions(self):
        self.enrollment.status = "INACTIVE"
        self.enrollment.save(update_fields=["status"])
        self.assertEqual(self.access(2).status_code, 403)

    def test_public_video_remains_available_without_session_registration(self):
        self.videos[0].visibility = Video.Visibility.PUBLIC
        self.videos[0].save(update_fields=["visibility"])
        self.assertEqual(self.access(0).status_code, 200)

    def test_foreign_tenant_membership_cannot_grant_session_access(self):
        other = Tenant.objects.create(code="qa-other-video", name="Other", is_active=True)
        self.membership.tenant = other
        self.membership.save(update_fields=["tenant"])
        self.assertEqual(self.access(2).status_code, 403)
