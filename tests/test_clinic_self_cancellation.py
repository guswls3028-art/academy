import datetime
import threading
from unittest.mock import patch

from django.db import close_old_connections
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.domains.clinic.models import SessionParticipant, SessionParticipantPlanItem
from apps.domains.clinic.services import change_participant_status
from apps.domains.clinic.services.lifecycle import Conflict
from apps.domains.clinic.tests import ClinicAPITestMixin, ClinicTestMixin
from apps.domains.exams.models import Exam
from apps.domains.messaging.models import ScheduledNotification
from apps.domains.parents.models import Parent
from apps.domains.progress.models import SessionProgress
from apps.core.models import TenantMembership


class ClinicSelfCancellationAPITest(APITestCase, ClinicAPITestMixin):
    def setUp(self):
        self.data = self.setup_api_tenant("clinic_self_cancel", student_count=1)
        self.tenant = self.data["tenant"]
        self.student = self.data["students"][0]
        self.enrollment = self.data["enrollments"][0]
        self.week_start = timezone.localdate() - datetime.timedelta(
            days=timezone.localdate().weekday()
        )

    def _session(self, day_offset: int, hour: int):
        return self.make_clinic_session(
            self.tenant,
            date=self.week_start + datetime.timedelta(days=day_offset),
            start_time=datetime.time(hour, 0),
            location=f"{day_offset}-{hour}호",
        )

    def _current_required_link(self):
        exam = Exam.objects.create(
            tenant=self.tenant,
            title="현재 필수 클리닉 시험",
            exam_type=Exam.ExamType.REGULAR,
            is_active=True,
        )
        exam.sessions.add(self.data["lec_session"])
        return self.make_clinic_link(
            self.enrollment,
            self.data["lec_session"],
            source_type="exam",
            source_id=exam.id,
        )

    def _cancel(self, participant):
        self.client.force_authenticate(user=self.student.user)
        return self.client.patch(
            f"/api/v1/clinic/participants/{participant.id}/set_status/",
            {"status": "cancelled", "send_to": "parent"},
            format="json",
            **self._headers(self.tenant),
        )

    def test_required_student_cannot_cancel_only_active_booking_without_side_effects(self):
        link = self._current_required_link()
        participant = self.make_participant(
            self.tenant,
            self._session(1, 14),
            self.student,
            enrollment=self.enrollment,
            status=SessionParticipant.Status.BOOKED,
        )
        plan = SessionParticipantPlanItem.objects.create(
            participant=participant,
            clinic_link=link,
            selected_by=self.data["admin_user"],
        )
        outbox_before = ScheduledNotification.objects.count()

        with patch(
            "apps.domains.clinic.services.lifecycle.cancel_pending_clinic_participant_reminders"
        ) as cancel_reminders, patch(
            "apps.domains.clinic.views.participant_views._send_clinic_notification"
        ) as send_notification:
            response = self._cancel(participant)

        self.assertEqual(response.status_code, 409, response.data)
        self.assertIn("같은 주", response.data["detail"])
        participant.refresh_from_db()
        plan.refresh_from_db()
        self.assertEqual(participant.status, SessionParticipant.Status.BOOKED)
        self.assertIsNone(plan.removed_at)
        self.assertEqual(ScheduledNotification.objects.count(), outbox_before)
        cancel_reminders.assert_not_called()
        send_notification.assert_not_called()

    def test_required_student_can_cancel_one_of_two_same_week_bookings_and_forces_both_targets(self):
        self._current_required_link()
        first = self.make_participant(
            self.tenant,
            self._session(1, 14),
            self.student,
            enrollment=self.enrollment,
            status=SessionParticipant.Status.BOOKED,
        )
        second = self.make_participant(
            self.tenant,
            self._session(3, 18),
            self.student,
            enrollment=self.enrollment,
            status=SessionParticipant.Status.PENDING,
        )
        notification = {
            "requested": 2,
            "failed": 0,
            "send_to": "both",
            "targets": [
                {"target": "parent", "requested": True},
                {"target": "student", "requested": True},
            ],
        }

        with patch(
            "apps.domains.clinic.views.participant_views._send_clinic_notification",
            return_value=notification,
        ) as send_notification:
            response = self._cancel(first)

        self.assertEqual(response.status_code, 200, response.data)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, SessionParticipant.Status.CANCELLED)
        self.assertEqual(second.status, SessionParticipant.Status.PENDING)
        self.assertEqual(response.data["notification"], notification)
        self.assertEqual(send_notification.call_args.args[2], "clinic_cancelled")
        self.assertEqual(send_notification.call_args.kwargs["send_to"], "both")

    def test_required_student_other_week_booking_does_not_satisfy_minimum(self):
        self._current_required_link()
        current = self.make_participant(
            self.tenant,
            self._session(2, 15),
            self.student,
            status=SessionParticipant.Status.BOOKED,
        )
        self.make_participant(
            self.tenant,
            self.make_clinic_session(
                self.tenant,
                date=self.week_start + datetime.timedelta(days=8),
                start_time=datetime.time(15, 0),
                location="다음주",
            ),
            self.student,
            status=SessionParticipant.Status.BOOKED,
        )

        response = self._cancel(current)

        self.assertEqual(response.status_code, 409, response.data)
        current.refresh_from_db()
        self.assertEqual(current.status, SessionParticipant.Status.BOOKED)

    def test_non_required_student_can_cancel_final_confirmed_booking(self):
        participant = self.make_participant(
            self.tenant,
            self._session(4, 16),
            self.student,
            status=SessionParticipant.Status.BOOKED,
        )
        with patch(
            "apps.domains.clinic.views.participant_views._send_clinic_notification",
            return_value={"requested": 2, "failed": 0, "send_to": "both", "targets": []},
        ):
            response = self._cancel(participant)

        self.assertEqual(response.status_code, 200, response.data)
        participant.refresh_from_db()
        self.assertEqual(participant.status, SessionParticipant.Status.CANCELLED)

    def test_parent_can_cancel_selected_child_booking_and_cannot_reduce_recipients(self):
        participant = self.make_participant(
            self.tenant,
            self._session(4, 19),
            self.student,
            status=SessionParticipant.Status.BOOKED,
        )
        parent_user = self.make_user("clinic_self_cancel_parent")
        parent_user.tenant = self.tenant
        parent_user.save(update_fields=["tenant"])
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=parent_user,
            role="parent",
        )
        parent = Parent.objects.create(
            tenant=self.tenant,
            user=parent_user,
            name="학부모",
            phone="01012345678",
        )
        self.student.parent = parent
        self.student.save(update_fields=["parent"])
        self.client.force_authenticate(user=parent_user)

        with patch(
            "apps.domains.clinic.views.participant_views._send_clinic_notification",
            return_value={"requested": 2, "failed": 0, "send_to": "both", "targets": []},
        ) as send_notification:
            response = self.client.patch(
                f"/api/v1/clinic/participants/{participant.id}/set_status/",
                {"status": "cancelled", "send_to": "parent"},
                format="json",
                HTTP_X_STUDENT_ID=str(self.student.id),
                **self._headers(self.tenant),
            )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(send_notification.call_args.kwargs["send_to"], "both")

    def test_self_cancel_notification_reports_each_alimtalk_target_without_fallback(self):
        from apps.domains.clinic.views.participant_views import _send_clinic_notification

        with patch(
            "apps.domains.clinic.views.participant_views.send_clinic_event_notification",
            side_effect=(True, False),
        ) as send_event:
            result = _send_clinic_notification(
                self.tenant,
                self.student,
                "clinic_cancelled",
                {"_domain_object_id": "clinic_participant:1:clinic_cancelled:1"},
                send_to="both",
            )

        self.assertEqual(
            [call.kwargs["send_to"] for call in send_event.call_args_list],
            ["parent", "student"],
        )
        self.assertTrue(all(
            call.kwargs["trigger"] == "clinic_cancelled"
            for call in send_event.call_args_list
        ))
        self.assertEqual(result["requested"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["targets"], [
            {"target": "parent", "requested": True},
            {"target": "student", "requested": False},
        ])

    def test_resolved_stale_and_completed_links_do_not_block_final_booking(self):
        cases = []

        resolved = self._current_required_link()
        resolved.resolved_at = timezone.now()
        resolved.save(update_fields=["resolved_at"])
        cases.append("resolved")

        stale_exam = Exam.objects.create(
            tenant=self.tenant,
            title="차시에서 제거된 시험",
            exam_type=Exam.ExamType.REGULAR,
            is_active=True,
        )
        self.make_clinic_link(
            self.enrollment,
            self.data["lec_session"],
            source_type="exam",
            source_id=stale_exam.id,
            cycle_no=2,
        )
        cases.append("stale")

        completed_exam = Exam.objects.create(
            tenant=self.tenant,
            title="완료된 차시 시험",
            exam_type=Exam.ExamType.REGULAR,
            is_active=True,
        )
        completed_exam.sessions.add(self.data["lec_session"])
        self.make_clinic_link(
            self.enrollment,
            self.data["lec_session"],
            source_type="exam",
            source_id=completed_exam.id,
            cycle_no=3,
        )
        SessionProgress.objects.create(
            enrollment=self.enrollment,
            session=self.data["lec_session"],
            completed=True,
            completed_at=timezone.now(),
        )
        cases.append("completed")

        participant = self.make_participant(
            self.tenant,
            self._session(5, 17),
            self.student,
            status=SessionParticipant.Status.BOOKED,
        )
        with patch(
            "apps.domains.clinic.views.participant_views._send_clinic_notification",
            return_value={"requested": 2, "failed": 0, "send_to": "both", "targets": []},
        ):
            response = self._cancel(participant)

        self.assertEqual(cases, ["resolved", "stale", "completed"])
        self.assertEqual(response.status_code, 200, response.data)

    def test_staff_keeps_administrative_cancellation_for_required_final_booking(self):
        self._current_required_link()
        participant = self.make_participant(
            self.tenant,
            self._session(6, 13),
            self.student,
            status=SessionParticipant.Status.BOOKED,
        )
        self.client.force_authenticate(user=self.data["admin_user"])
        with patch(
            "apps.domains.clinic.views.participant_views._send_clinic_notification",
            return_value={"requested": 1, "failed": 0, "send_to": "parent", "targets": []},
        ):
            response = self.client.patch(
                f"/api/v1/clinic/participants/{participant.id}/set_status/",
                {"status": "cancelled"},
                format="json",
                **self._headers(self.tenant),
            )

        self.assertEqual(response.status_code, 200, response.data)
        participant.refresh_from_db()
        self.assertEqual(participant.status, SessionParticipant.Status.CANCELLED)

    def test_booking_projection_exposes_server_owned_self_cancel_decision(self):
        self._current_required_link()
        only = self.make_participant(
            self.tenant,
            self._session(2, 14),
            self.student,
            status=SessionParticipant.Status.BOOKED,
        )
        self.client.force_authenticate(user=self.student.user)

        response = self.client.get(
            "/api/v1/clinic/participants/?page_size=200",
            **self._headers(self.tenant),
        )

        self.assertEqual(response.status_code, 200, response.data)
        rows = response.data.get("results", response.data)
        row = next(item for item in rows if item["id"] == only.id)
        self.assertFalse(row["can_self_cancel"])
        self.assertIn("같은 주", row["self_cancel_reason"])


class ClinicSelfCancellationConcurrencyTest(TransactionTestCase, ClinicTestMixin):
    reset_sequences = True

    def setUp(self):
        self.data = self.setup_full_tenant("clinic_cancel_race", student_count=1)
        self.tenant = self.data["tenant"]
        self.student = self.data["students"][0]
        self.actor = self.student.user
        self.actor.tenant = self.tenant
        self.actor.save(update_fields=["tenant"])
        exam = Exam.objects.create(
            tenant=self.tenant,
            title="동시 취소 시험",
            exam_type=Exam.ExamType.REGULAR,
            is_active=True,
        )
        exam.sessions.add(self.data["lec_session"])
        self.make_clinic_link(
            self.data["enrollments"][0],
            self.data["lec_session"],
            source_type="exam",
            source_id=exam.id,
        )
        week_start = timezone.localdate() - datetime.timedelta(
            days=timezone.localdate().weekday()
        )
        self.participants = [
            self.make_participant(
                self.tenant,
                self.make_clinic_session(
                    self.tenant,
                    date=week_start + datetime.timedelta(days=offset),
                    start_time=datetime.time(hour, 0),
                    location=f"race-{offset}",
                ),
                self.student,
                status=SessionParticipant.Status.BOOKED,
            )
            for offset, hour in ((1, 14), (3, 16))
        ]

    def test_two_simultaneous_cancellations_leave_exactly_one_active_booking(self):
        barrier = threading.Barrier(2)
        outcomes = []
        outcome_lock = threading.Lock()

        def cancel(participant_id):
            close_old_connections()
            try:
                tenant = type(self.tenant).objects.get(pk=self.tenant.pk)
                student = type(self.student).objects.get(pk=self.student.pk)
                actor = type(self.actor).objects.get(pk=self.actor.pk)
                barrier.wait(timeout=5)
                change_participant_status(
                    tenant=tenant,
                    participant_id=participant_id,
                    next_status=SessionParticipant.Status.CANCELLED,
                    actor=actor,
                    request_student=student,
                )
                result = "cancelled"
            except Conflict:
                result = "blocked"
            finally:
                close_old_connections()
            with outcome_lock:
                outcomes.append(result)

        threads = [
            threading.Thread(target=cancel, args=(participant.id,))
            for participant in self.participants
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertCountEqual(outcomes, ["cancelled", "blocked"])
        self.assertEqual(
            SessionParticipant.objects.filter(
                tenant=self.tenant,
                student=self.student,
                status__in=(
                    SessionParticipant.Status.PENDING,
                    SessionParticipant.Status.BOOKED,
                ),
            ).count(),
            1,
        )
