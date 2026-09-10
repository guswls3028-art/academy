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

    def test_explicit_time_range_session_cannot_end_after_next_day_midnight(self):
        self.client.force_authenticate(user=self.data["admin_user"])

        response = self.client.post(
            "/api/v1/clinic/sessions/",
            {
                "date": self.session.date,
                "start_time": "18:00",
                "duration_minutes": 420,
                "location": "overnight-invalid",
                "max_participants": 10,
                "booking_mode": "time_range",
                "booking_interval_minutes": 60,
                "booking_max_stay_minutes": 600,
            },
            format="json",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 400, response.data)
        update = self.client.patch(
            f"/api/v1/clinic/sessions/{self.session.id}/",
            {"duration_minutes": 420},
            format="json",
            **self._headers(),
        )
        self.assertEqual(update.status_code, 400, update.data)
        self.session.refresh_from_db()
        self.assertEqual(self.session.duration_minutes, 360)

    def test_omitted_mode_uses_time_range_tenant_default_for_create_and_bulk_validation(self):
        self.tenant.clinic_booking_mode = "time_range"
        self.tenant.clinic_booking_interval_minutes = 60
        self.tenant.clinic_booking_max_stay_minutes = 600
        self.tenant.save(update_fields=[
            "clinic_booking_mode",
            "clinic_booking_interval_minutes",
            "clinic_booking_max_stay_minutes",
        ])
        self.client.force_authenticate(user=self.data["admin_user"])
        common = {
            "start_time": "18:00",
            "duration_minutes": 420,
            "location": "tenant-default-invalid",
            "max_participants": 10,
        }

        create = self.client.post(
            "/api/v1/clinic/sessions/",
            {**common, "date": self.session.date},
            format="json",
            **self._headers(),
        )
        bulk = self.client.post(
            "/api/v1/clinic/sessions/bulk-create/",
            {**common, "dates": [self.session.date]},
            format="json",
            **self._headers(),
        )
        bulk_with_multi_slot = self.client.post(
            "/api/v1/clinic/sessions/bulk-create/",
            {
                **common,
                "dates": [self.session.date],
                "duration_minutes": 360,
                "allow_multi_slot_booking": True,
            },
            format="json",
            **self._headers(),
        )

        self.assertEqual(create.status_code, 400, create.data)
        self.assertEqual(bulk.status_code, 400, bulk.data)
        self.assertEqual(bulk_with_multi_slot.status_code, 400, bulk_with_multi_slot.data)

        valid_create = self.client.post(
            "/api/v1/clinic/sessions/",
            {
                **common,
                "date": self.session.date,
                "duration_minutes": 360,
                "location": "tenant-default-valid-create",
            },
            format="json",
            **self._headers(),
        )
        valid_bulk = self.client.post(
            "/api/v1/clinic/sessions/bulk-create/",
            {
                **common,
                "dates": [self.session.date + datetime.timedelta(days=1)],
                "duration_minutes": 360,
                "location": "tenant-default-valid-bulk",
            },
            format="json",
            **self._headers(),
        )
        self.assertEqual(valid_create.status_code, 201, valid_create.data)
        self.assertEqual(valid_create.data["booking_mode"], "time_range")
        self.assertEqual(valid_create.data["end_time"], datetime.time.min)
        self.assertEqual(valid_bulk.status_code, 201, valid_bulk.data)
        bulk_session = self.session.__class__.objects.get(
            id=valid_bulk.data["created"][0]["id"]
        )
        self.assertEqual(bulk_session.booking_mode, "time_range")
        bulk_end = datetime.datetime.combine(
            bulk_session.date,
            bulk_session.start_time,
        ) + datetime.timedelta(minutes=bulk_session.duration_minutes)
        self.assertEqual(bulk_end.time(), datetime.time.min)

    def test_active_time_range_booking_blocks_window_change_that_would_orphan_it(self):
        booked = self._book(self.students[0], start="22:00", end="00:00")
        self.assertEqual(booked.status_code, 201, booked.data)
        self.client.force_authenticate(user=self.data["admin_user"])

        response = self.client.patch(
            f"/api/v1/clinic/sessions/{self.session.id}/",
            {"duration_minutes": 240},
            format="json",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.session.refresh_from_db()
        self.assertEqual(self.session.duration_minutes, 360)


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
