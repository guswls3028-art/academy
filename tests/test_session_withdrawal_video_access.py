from datetime import date, time
from unittest.mock import patch

from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase

from apps.core.models import Tenant, TenantMembership, User
from apps.domains.attendance.models import Attendance
from apps.domains.attendance.services import ensure_session_roster_membership
from apps.domains.enrollment.models import Enrollment, SessionEnrollment
from apps.domains.lectures.models import Lecture, Section, Session
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

    def reregister(self, *, enrollment_api=False):
        self.client.force_authenticate(self.staff)
        url = (
            "/api/v1/enrollments/session-enrollments/bulk_create/"
            if enrollment_api else "/api/v1/lectures/attendance/bulk_create/"
        )
        payload = {"session": self.attendances[2].session_id}
        payload["enrollments" if enrollment_api else "students"] = [
            self.enrollment.id if enrollment_api else self.student.id
        ]
        return self.client.post(url, payload, format="json", **self.headers)

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

    def test_explicit_session_reregistration_restores_editable_attendance_and_preserves_history(self):
        # Exercise the registration endpoint used by the attendance roster UI.
        Attendance.objects.filter(tenant=self.tenant, enrollment=self.enrollment).delete()
        SessionEnrollment.objects.filter(tenant=self.tenant, enrollment=self.enrollment).delete()
        self.attendances = []
        for video in self.videos:
            self.client.force_authenticate(self.staff)
            registered = self.client.post(
                "/api/v1/lectures/attendance/bulk_create/",
                {"session": video.session_id, "students": [self.student.id]},
                format="json", **self.headers,
            )
            self.assertEqual(registered.status_code, 201, registered.data)
            attendance = Attendance.objects.get(pk=registered.data[0]["id"])
            updated = self.client.patch(
                f"/api/v1/lectures/attendance/{attendance.id}/",
                {"status": "ONLINE", "memo": f"Keep session {video.session.order} history"},
                format="json", **self.headers,
            )
            self.assertEqual(updated.status_code, 200, updated.data)
            attendance.refresh_from_db()
            self.attendances.append(attendance)

        original_rows = [(row.id, row.status, row.memo) for row in self.attendances]
        target = self.attendances[2]
        progress = VideoProgress.objects.create(
            video=self.videos[2], enrollment=self.enrollment, progress=0.25, last_position=150,
        )
        for index in range(8):
            self.assertEqual(self.access(index).status_code, 200)

        self.assertEqual(self.withdraw("session").status_code, 200)
        self.assertEqual(self.access(2).status_code, 403)
        self.client.force_authenticate(self.staff)
        ordinary_update = self.client.patch(
            f"/api/v1/lectures/attendance/{target.id}/", {"status": "PRESENT"},
            format="json", **self.headers,
        )
        self.assertEqual(ordinary_update.status_code, 409, ordinary_update.data)
        self.assertFalse(SessionEnrollment.objects.filter(
            tenant=self.tenant, enrollment=self.enrollment, session_id=target.session_id,
        ).exists())

        registered_again = self.client.post(
            "/api/v1/lectures/attendance/bulk_create/",
            {"session": target.session_id, "students": [self.student.id]},
            format="json", **self.headers,
        )
        self.assertEqual(registered_again.status_code, 201, registered_again.data)
        self.assertEqual(registered_again.data[0]["id"], target.id)
        self.assertEqual(SessionEnrollment.objects.filter(
            tenant=self.tenant, enrollment=self.enrollment, session_id=target.session_id,
        ).count(), 1)
        reloaded = self.client.get(f"/api/v1/lectures/attendance/{target.id}/", **self.headers)
        self.assertEqual(reloaded.status_code, 200, reloaded.data)
        with self.subTest(step="explicit reregistration clears the terminal attendance state"):
            self.assertEqual(reloaded.data["status"], "UNSET")

        # A successful re-registration must restore normal editing, not only video access.
        for attendance_status in ("PRESENT", "ONLINE"):
            updated = self.client.patch(
                f"/api/v1/lectures/attendance/{target.id}/", {"status": attendance_status},
                format="json", **self.headers,
            )
            with self.subTest(step="normal attendance editing", status=attendance_status):
                self.assertEqual(updated.status_code, 200, updated.data)
        reloaded = self.client.get(f"/api/v1/lectures/attendance/{target.id}/", **self.headers)
        with self.subTest(step="edited attendance persists after reload"):
            self.assertEqual(reloaded.data["status"], "ONLINE")
        self.assertEqual(reloaded.data["memo"], original_rows[2][2])
        self.enrollment.refresh_from_db()
        progress.refresh_from_db()
        self.assertEqual(self.enrollment.status, "ACTIVE")
        self.assertEqual(progress.progress, 0.25)
        self.assertEqual(progress.last_position, 150)
        for index, attendance in enumerate(self.attendances):
            self.assertEqual(self.access(index).status_code, 200)
            if index != 2:
                attendance.refresh_from_db()
                self.assertEqual((attendance.id, attendance.status, attendance.memo), original_rows[index])

    def test_explicit_session_enrollment_repairs_retained_membership_and_repeats_preserve_edits(self):
        target = self.attendances[2]
        section = Section.objects.create(
            tenant=self.tenant, lecture=self.enrollment.lecture, label="A",
            day_of_week=0, start_time=time(17, 0),
        )
        target.memo = "Keep the original attendance record"
        target.planned_arrival_date = date(2026, 9, 21)
        target.planned_arrival_time = time(17, 30)
        target.attended_section = section
        target.save(update_fields=["memo", "planned_arrival_date", "planned_arrival_time", "attended_section"])
        recorded_at = target.recorded_at
        self.assertEqual(self.withdraw("session").status_code, 200)
        # Recover the historical partial re-registration state as well.
        membership = SessionEnrollment.objects.create(
            tenant=self.tenant, enrollment=self.enrollment, session=target.session,
        )
        registered = self.reregister(enrollment_api=True)
        self.assertEqual(registered.status_code, 201, registered.data)
        self.assertEqual(registered.data[0]["id"], membership.id)
        target.refresh_from_db()
        self.assertEqual(target.status, "UNSET")
        self.assertEqual(target.memo, "Keep the original attendance record")
        self.assertEqual(target.recorded_at, recorded_at)
        self.assertEqual(target.planned_arrival_date, date(2026, 9, 21))
        self.assertEqual(target.planned_arrival_time, time(17, 30))
        self.assertEqual(target.attended_section_id, section.id)
        updated = self.client.patch(
            f"/api/v1/lectures/attendance/{target.id}/", {"status": "PRESENT"},
            format="json", **self.headers,
        )
        self.assertEqual(updated.status_code, 200, updated.data)
        for enrollment_api in (True, False):
            repeated = self.reregister(enrollment_api=enrollment_api)
            self.assertEqual(repeated.status_code, 201, repeated.data)
            target.refresh_from_db()
            self.assertEqual(target.status, "PRESENT")
            self.assertEqual(target.memo, "Keep the original attendance record")
            self.assertEqual(target.recorded_at, recorded_at)
            self.assertEqual(target.planned_arrival_date, date(2026, 9, 21))
            self.assertEqual(target.planned_arrival_time, time(17, 30))
            self.assertEqual(target.attended_section_id, section.id)
        self.assertEqual(Attendance.objects.filter(
            tenant=self.tenant, enrollment=self.enrollment, session=target.session,
        ).count(), 1)
        self.assertEqual(SessionEnrollment.objects.get(
            tenant=self.tenant, enrollment=self.enrollment, session=target.session,
        ).id, membership.id)

    def test_reads_and_automatic_roster_calls_do_not_reregister_a_withdrawn_session(self):
        self.assertEqual(self.withdraw("session").status_code, 200)
        target = self.attendances[2]
        for url in ("/api/v1/lectures/attendance/", "/api/v1/enrollments/session-enrollments/"):
            response = self.client.get(url, {"session": target.session_id}, **self.headers)
            self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.access(2).status_code, 403)
        with patch("apps.domains.attendance.services.roster.auto_assign_roster_fees") as assign_fees:
            with self.assertRaisesMessage(ValidationError, "명시적으로 재등록"):
                ensure_session_roster_membership(
                    tenant=self.tenant, enrollment=self.enrollment, session=target.session,
                )
        assign_fees.assert_not_called()
        target.refresh_from_db()
        self.assertEqual(target.status, "SECESSION")
        self.assertFalse(SessionEnrollment.objects.filter(
            tenant=self.tenant, enrollment=self.enrollment, session=target.session,
        ).exists())
        self.assertEqual(self.access(2).status_code, 403)

    def test_public_reregistration_cannot_reactivate_whole_lecture_withdrawal(self):
        self.assertEqual(self.withdraw("lecture").status_code, 200)
        for enrollment_api in (False, True):
            with self.subTest(enrollment_api=enrollment_api):
                registered = self.reregister(enrollment_api=enrollment_api)
                self.assertEqual(registered.status_code, 400, registered.data)
        self.enrollment.refresh_from_db()
        self.assertEqual(self.enrollment.status, "INACTIVE")
        self.assertFalse(Attendance.objects.filter(
            tenant=self.tenant, enrollment=self.enrollment,
        ).exclude(status="SECESSION").exists())
        for index in range(8):
            self.assertEqual(self.access(index).status_code, 403)
