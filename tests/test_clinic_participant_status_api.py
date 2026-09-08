from unittest.mock import patch

from django.utils import timezone
from rest_framework.test import APITestCase

from apps.core.models import TenantMembership
from apps.domains.clinic.tests import ClinicAPITestMixin


class ParticipantPendingStatusTransitionAPITest(APITestCase, ClinicAPITestMixin):
    def setUp(self):
        self.data = self.setup_api_tenant("pending_status_api", student_count=2)
        self.tenant = self.data["tenant"]
        self.admin = self.data["admin_user"]
        self.student = self.data["students"][0]
        self.student.user.tenant = self.tenant
        self.student.user.save(update_fields=["tenant"])

    def test_staff_can_approve_pending_booking(self):
        self.client.force_authenticate(user=self.admin)
        participant = self.make_participant(
            self.tenant,
            self.data["clinic_session"],
            self.student,
            status="pending",
        )

        resp = self.client.patch(
            f"/api/v1/clinic/participants/{participant.id}/set_status/",
            {"status": "booked"},
            format="json",
            **self._headers(self.tenant),
        )

        self.assertEqual(resp.status_code, 200, resp.data)
        participant.refresh_from_db()
        self.assertEqual(participant.status, "booked")
        self.assertEqual(participant.status_changed_by_id, self.admin.id)

    def test_staff_can_correct_no_show_to_attended_and_preserve_study_completion(self):
        self.client.force_authenticate(user=self.admin)
        participant = self.make_participant(
            self.tenant,
            self.data["clinic_session"],
            self.student,
            status="no_show",
        )
        participant.completed_at = timezone.now()
        participant.completed_by = self.admin
        participant.save(update_fields=["completed_at", "completed_by"])

        resp = self.client.patch(
            f"/api/v1/clinic/participants/{participant.id}/set_status/",
            {"status": "attended"},
            format="json",
            **self._headers(self.tenant),
        )

        self.assertEqual(resp.status_code, 200, resp.data)
        participant.refresh_from_db()
        self.assertEqual(participant.status, "attended")
        self.assertIsNotNone(participant.completed_at)
        self.assertEqual(participant.completed_by_id, self.admin.id)

    @patch(
        "apps.domains.clinic.views.participant_views.send_clinic_reminder_for_participant",
        return_value={"status": "ok", "sent": 1, "skipped": 0},
    )
    def test_staff_can_remind_one_booked_participant(self, mock_remind):
        self.client.force_authenticate(user=self.admin)
        participant = self.make_participant(
            self.tenant,
            self.data["clinic_session"],
            self.student,
            status="booked",
        )

        resp = self.client.post(
            f"/api/v1/clinic/participants/{participant.id}/remind/",
            **self._headers(self.tenant),
        )

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["sent"], 1)
        mock_remind.assert_called_once_with(
            tenant_id=self.tenant.id,
            participant_id=participant.id,
            actor_id=self.admin.id,
        )

    def test_staff_cannot_remind_no_show_participant(self):
        self.client.force_authenticate(user=self.admin)
        participant = self.make_participant(
            self.tenant,
            self.data["clinic_session"],
            self.student,
            status="no_show",
        )

        resp = self.client.post(
            f"/api/v1/clinic/participants/{participant.id}/remind/",
            **self._headers(self.tenant),
        )

        self.assertEqual(resp.status_code, 409, resp.data)

    @patch(
        "apps.domains.clinic.views.participant_views.send_clinic_reminder_for_participant",
        return_value={"status": "delivery_failed", "sent": 0, "skipped": 1},
    )
    def test_staff_reminder_reports_delivery_configuration_failure(self, _mock_remind):
        self.client.force_authenticate(user=self.admin)
        participant = self.make_participant(
            self.tenant,
            self.data["clinic_session"],
            self.student,
            status="booked",
        )

        resp = self.client.post(
            f"/api/v1/clinic/participants/{participant.id}/remind/",
            **self._headers(self.tenant),
        )

        self.assertEqual(resp.status_code, 503, resp.data)
        self.assertIn("알림 설정과 학생 전화번호", resp.data["detail"])

    def test_staff_cannot_mark_pending_attended_without_approval(self):
        self.client.force_authenticate(user=self.admin)
        participant = self.make_participant(
            self.tenant,
            self.data["clinic_session"],
            self.student,
            status="pending",
        )

        resp = self.client.patch(
            f"/api/v1/clinic/participants/{participant.id}/set_status/",
            {"status": "attended"},
            format="json",
            **self._headers(self.tenant),
        )

        self.assertEqual(resp.status_code, 400, resp.data)
        participant.refresh_from_db()
        self.assertEqual(participant.status, "pending")

    def test_non_staff_member_cannot_use_staff_status_transition(self):
        member = self.make_user("non_staff_pending_status_api")
        TenantMembership.objects.create(
            user=member,
            tenant=self.tenant,
            role="student",
            is_active=True,
        )
        self.client.force_authenticate(user=member)
        participant = self.make_participant(
            self.tenant,
            self.data["clinic_session"],
            self.student,
            status="pending",
        )

        resp = self.client.patch(
            f"/api/v1/clinic/participants/{participant.id}/set_status/",
            {"status": "booked"},
            format="json",
            **self._headers(self.tenant),
        )

        self.assertEqual(resp.status_code, 403, resp.data)
        participant.refresh_from_db()
        self.assertEqual(participant.status, "pending")

    def test_student_cannot_send_participant_reminder(self):
        self.client.force_authenticate(user=self.student.user)
        participant = self.make_participant(
            self.tenant,
            self.data["clinic_session"],
            self.student,
            status="booked",
        )

        resp = self.client.post(
            f"/api/v1/clinic/participants/{participant.id}/remind/",
            **self._headers(self.tenant),
        )

        self.assertEqual(resp.status_code, 403, resp.data)

    def test_student_can_still_cancel_own_pending_booking(self):
        self.client.force_authenticate(user=self.student.user)
        participant = self.make_participant(
            self.tenant,
            self.data["clinic_session"],
            self.student,
            status="pending",
        )

        with patch(
            "apps.domains.clinic.services.lifecycle.send_clinic_event_notification",
            return_value=True,
        ) as notify:
            resp = self.client.patch(
                f"/api/v1/clinic/participants/{participant.id}/set_status/",
                {"status": "cancelled", "send_to": "parent"},
                format="json",
                **self._headers(self.tenant),
            )

        self.assertEqual(resp.status_code, 200, resp.data)
        participant.refresh_from_db()
        self.assertEqual(participant.status, "cancelled")
        self.assertEqual(participant.status_changed_by_id, self.student.user_id)
        self.assertEqual(notify.call_count, 2)
        self.assertEqual(
            [captured.kwargs["send_to"] for captured in notify.call_args_list],
            ["parent", "student"],
        )
        for captured in notify.call_args_list:
            self.assertEqual(captured.kwargs["tenant"], self.tenant)
            self.assertEqual(captured.kwargs["trigger"], "clinic_cancelled")
            self.assertEqual(captured.kwargs["student"], self.student)
