from unittest.mock import patch

from rest_framework.test import APITestCase

from apps.core.models import Tenant, TenantMembership, User
from apps.domains.attendance.models import Attendance
from apps.domains.enrollment.models import Enrollment, SessionEnrollment
from apps.domains.lectures.models import Lecture, Session
from apps.domains.students.models import Student
from apps.domains.video.models import Video, VideoProgress


class SessionWithdrawalVideoAccessTests(APITestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(code="qa-withdrawal", name="QA", is_active=True)
        self.staff = User.objects.create_user(username="qa-withdrawal-admin", tenant=self.tenant, is_staff=True)
        TenantMembership.ensure_active(tenant=self.tenant, user=self.staff, role="admin")
        self.user = User.objects.create_user(username="qa-withdrawal-student", tenant=self.tenant)
        TenantMembership.ensure_active(tenant=self.tenant, user=self.user, role="student")
        self.student = Student.objects.create(
            tenant=self.tenant, user=self.user, name="QA", ps_number="QAW1",
            omr_code="87654321", parent_phone="01000000000",
        )
        lecture = Lecture.objects.create(tenant=self.tenant, name="QA", title="QA", subject="MATH")
        self.enrollment = Enrollment.objects.create(tenant=self.tenant, student=self.student, lecture=lecture)
        self.videos = []
        self.attendances = []
        for order in range(1, 9):
            session = Session.objects.create(lecture=lecture, title=f"Session {order}", order=order)
            SessionEnrollment.objects.create(tenant=self.tenant, session=session, enrollment=self.enrollment)
            self.attendances.append(Attendance.objects.create(
                tenant=self.tenant, session=session, enrollment=self.enrollment, status="ONLINE",
            ))
            self.videos.append(Video.objects.create(
                tenant=self.tenant, session=session, title=f"Video {order}", duration=600, status=Video.Status.READY,
            ))
        self.headers = {"HTTP_HOST": "localhost", "HTTP_X_TENANT_CODE": self.tenant.code}

    def access(self, index):
        self.client.force_authenticate(self.user)
        return self.client.get(
            f"/api/v1/student/video/videos/{self.videos[index].id}/playback/?access_check=true", **self.headers,
        )

    def withdraw(self, scope):
        self.client.force_authenticate(self.staff)
        return self.client.patch(
            f"/api/v1/lectures/attendance/{self.attendances[2].id}/",
            {"status": "SECESSION", "confirm_secession": True, "secession_scope": scope},
            format="json", **self.headers,
        )

    def test_session_withdrawal_revokes_monitored_token_preserves_other_seven_and_history(self):
        self.client.force_authenticate(self.user)
        with patch("apps.domains.video.services.playback_session.init_session_redis"):
            start = self.client.post(
                f"/api/v1/student/video/videos/{self.videos[2].id}/playback/",
                {"device_id": "qa-withdrawal-device"}, format="json", **self.headers,
            )
        self.assertEqual(start.status_code, 200)
        self.assertEqual(start.data["policy"]["access_mode"], "PROCTORED_CLASS")
        progress = VideoProgress.objects.create(
            video=self.videos[2], enrollment=self.enrollment, progress=0.25, last_position=150,
        )
        response = self.withdraw("session")
        self.assertEqual(response.status_code, 200)
        self.enrollment.refresh_from_db()
        progress.refresh_from_db()
        self.assertEqual(self.enrollment.status, "ACTIVE")
        self.assertEqual(progress.last_position, 150)
        for index in range(8):
            self.assertEqual(self.access(index).status_code, 403 if index == 2 else 200)
        self.client.force_authenticate(self.user)
        for action in ("refresh", "renew", "heartbeat"):
            result = self.client.post(
                f"/api/v1/media/playback/{action}/", {"token": start.data["playback_token"]},
                format="json", **self.headers,
            )
            self.assertEqual(result.status_code, 403, action)
        # Choosing whole-lecture withdrawal after session withdrawal still works.
        self.assertEqual(self.withdraw("lecture").status_code, 200)
        for index in range(8):
            self.assertEqual(self.access(index).status_code, 403)

    def test_explicit_lecture_withdrawal_revokes_all_eight(self):
        self.assertEqual(self.withdraw("lecture").status_code, 200)
        self.enrollment.refresh_from_db()
        self.assertEqual(self.enrollment.status, "INACTIVE")
        for index in range(8):
            self.assertEqual(self.access(index).status_code, 403)
