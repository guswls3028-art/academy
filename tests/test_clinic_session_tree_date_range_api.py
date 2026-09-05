import datetime

from rest_framework.test import APITestCase

from apps.domains.clinic.tests import ClinicAPITestMixin


class ClinicSessionTreeDateRangeAPITest(APITestCase, ClinicAPITestMixin):
    def setUp(self):
        self.data = self.setup_api_tenant("clinic_tree_week", student_count=2)
        self.tenant = self.data["tenant"]
        self.admin = self.data["admin_user"]
        self.client.force_authenticate(user=self.admin)

    def _headers(self):
        return super()._headers(self.tenant)

    def test_tree_range_crosses_month_boundary_and_preserves_copy_settings(self):
        monday = self.data["clinic_session"]
        monday.title = "월말 클리닉"
        monday.date = datetime.date(2026, 8, 31)
        monday.start_time = datetime.time(16, 30)
        monday.duration_minutes = 120
        monday.location = "월말 교실"
        monday.max_participants = 14
        monday.target_grade = 2
        monday.target_school_type = "HIGH"
        monday.allow_time_preference = True
        monday.allow_multi_slot_booking = True
        monday.booking_mode = "fixed_slot"
        monday.booking_interval_minutes = 30
        monday.booking_max_stay_minutes = 120
        monday.save()
        monday.target_lectures.add(self.data["lecture"])
        self.make_participant(
            self.tenant,
            monday,
            self.data["students"][0],
            status="cancelled",
        )

        sunday = self.make_clinic_session(
            self.tenant,
            date=datetime.date(2026, 9, 6),
            start_time=datetime.time(18, 0),
            location="월초 교실",
        )
        self.make_participant(
            self.tenant,
            sunday,
            self.data["students"][1],
            status="booked",
        )
        outside_before = self.make_clinic_session(
            self.tenant,
            date=datetime.date(2026, 8, 30),
            start_time=datetime.time(15, 0),
            location="범위 전",
        )
        outside_after = self.make_clinic_session(
            self.tenant,
            date=datetime.date(2026, 9, 7),
            start_time=datetime.time(15, 0),
            location="범위 후",
        )
        deleted = self.make_clinic_session(
            self.tenant,
            date=datetime.date(2026, 9, 2),
            start_time=datetime.time(20, 0),
            location="삭제 일정",
        )
        deleted_id = deleted.id
        deleted.delete()

        foreign = self.setup_api_tenant("clinic_tree_week_foreign", student_count=1)
        foreign_session = foreign["clinic_session"]
        foreign_session.date = datetime.date(2026, 9, 2)
        foreign_session.save(update_fields=["date", "updated_at"])

        response = self.client.get(
            "/api/v1/clinic/sessions/tree/",
            {"date_from": "2026-08-31", "date_to": "2026-09-06"},
            **self._headers(),
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([row["id"] for row in response.data], [monday.id, sunday.id])
        self.assertNotIn(outside_before.id, {row["id"] for row in response.data})
        self.assertNotIn(outside_after.id, {row["id"] for row in response.data})
        self.assertNotIn(deleted_id, {row["id"] for row in response.data})
        self.assertNotIn(foreign_session.id, {row["id"] for row in response.data})

        monday_row = response.data[0]
        self.assertEqual(monday_row["participant_count"], 1)
        self.assertEqual(monday_row["booked_count"], 0)
        self.assertEqual(monday_row["target_school_type"], "HIGH")
        self.assertEqual(monday_row["target_lecture_ids"], [self.data["lecture"].id])
        self.assertTrue(monday_row["allow_time_preference"])
        self.assertTrue(monday_row["allow_multi_slot_booking"])
        self.assertEqual(monday_row["booking_mode"], "fixed_slot")
        self.assertEqual(monday_row["booking_interval_minutes"], 30)
        self.assertEqual(monday_row["booking_max_stay_minutes"], 120)

    def test_tree_range_rejects_partial_reversed_mixed_and_unbounded_ranges(self):
        cases = [
            {"date_from": "2026-08-31"},
            {"date_from": "2026-02-30", "date_to": "2026-03-01"},
            {"date_from": "2026-09-06", "date_to": "2026-08-31"},
            {
                "date_from": "2026-08-31",
                "date_to": "2026-09-06",
                "year": 2026,
                "month": 9,
            },
            {"date_from": "2026-08-01", "date_to": "2026-09-01"},
        ]

        for params in cases:
            with self.subTest(params=params):
                response = self.client.get(
                    "/api/v1/clinic/sessions/tree/",
                    params,
                    **self._headers(),
                )
                self.assertEqual(response.status_code, 400, getattr(response, "data", None))
