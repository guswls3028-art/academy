import datetime

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.domains.clinic.contracts import is_clinic_booking_reminder_active
from apps.domains.clinic.models import SessionParticipant
from apps.domains.clinic.services.lifecycle import booking_availability_for_session
from apps.domains.clinic.tests import ClinicAPITestMixin


class ClinicTimeRangeMidnightAPITest(APITestCase, ClinicAPITestMixin):
    def setUp(self):
        self.data = self.setup_api_tenant("clinic_midnight_range", student_count=2)
        self.tenant = self.data["tenant"]
        self.students = self.data["students"]
        self.session = self.data["clinic_session"]
        self.session.date = datetime.date.today() + datetime.timedelta(days=1)
        self.session.start_time = datetime.time(18, 0)
        self.session.duration_minutes = 360
        self.session.max_participants = 1
        self.session.booking_mode = "time_range"
        self.session.booking_interval_minutes = 60
        self.session.booking_max_stay_minutes = 600
        self.session.save(update_fields=[
            "date",
            "start_time",
            "duration_minutes",
            "max_participants",
            "booking_mode",
            "booking_interval_minutes",
            "booking_max_stay_minutes",
            "updated_at",
        ])

    def _headers(self):
        return super()._headers(self.tenant)

    def _book(self, student, *, start, end):
        self.client.force_authenticate(user=student.user)
        return self.client.post(
            "/api/v1/clinic/participants/bulk-create/",
            {
                "session_ids": [self.session.id],
                "booking_start_time": start,
                "booking_end_time": end,
            },
            format="json",
            **self._headers(),
        )

    def test_exact_midnight_end_books_and_counts_capacity_by_interval(self):
        response = self._book(self.students[0], start="22:00", end="00:00")

        self.assertEqual(response.status_code, 201, response.data)
        participant = SessionParticipant.objects.get(
            tenant=self.tenant,
            session=self.session,
            student=self.students[0],
        )
        self.assertEqual(participant.booking_start_time, datetime.time(22, 0))
        self.assertEqual(participant.booking_end_time, datetime.time(0, 0))

        availability = booking_availability_for_session(
            tenant=self.tenant,
            session=self.session,
        )
        slots = {slot["start_time"]: slot for slot in availability["slots"]}
        self.assertEqual(availability["window"]["end_time"], "00:00")
        self.assertEqual(slots["21:00"]["remaining_capacity"], 1)
        self.assertEqual(slots["22:00"]["remaining_capacity"], 0)
        self.assertEqual(slots["23:00"]["remaining_capacity"], 0)

        rejected = self._book(self.students[1], start="23:00", end="00:00")
        self.assertEqual(rejected.status_code, 409, rejected.data)

    def test_midnight_booking_reminder_contract_uses_next_day_for_end(self):
        response = self._book(self.students[0], start="22:00", end="00:00")
        self.assertEqual(response.status_code, 201, response.data)
        participant = SessionParticipant.objects.get(id=response.data["participants"][0]["id"])
        participant.status = SessionParticipant.Status.BOOKED
        participant.save(update_fields=["status", "updated_at"])
        now = timezone.make_aware(
            datetime.datetime.combine(self.session.date, datetime.time(12, 0))
        )
        origin_id = (
            f"clinic_booking:{participant.id}:{self.session.id}:"
            f"{self.session.date:%Y%m%d}:2200"
        )

        self.assertTrue(
            is_clinic_booking_reminder_active(
                tenant_id=self.tenant.id,
                origin_id=origin_id,
                now=now,
            )
        )


class ClinicTimeRangeMidnightConstraintTest(TestCase, ClinicAPITestMixin):
    def test_database_allows_paired_range_that_ends_exactly_at_midnight(self):
        data = self.setup_full_tenant("clinic_midnight_constraint")
        session = data["clinic_session"]
        session.booking_mode = "time_range"
        session.start_time = datetime.time(18, 0)
        session.duration_minutes = 360
        session.save(update_fields=["booking_mode", "start_time", "duration_minutes", "updated_at"])

        participant = SessionParticipant.objects.create(
            tenant=data["tenant"],
            session=session,
            student=data["students"][0],
            status=SessionParticipant.Status.BOOKED,
            booking_start_time=datetime.time(22, 0),
            booking_end_time=datetime.time(0, 0),
        )

        self.assertEqual(participant.booking_end_time, datetime.time(0, 0))
