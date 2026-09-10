import datetime
import json
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from apps.domains.clinic.models import SessionParticipant
from apps.domains.clinic.tests import ClinicTestMixin


class ConvertLimglishClinicTimeRangesCommandTest(TestCase, ClinicTestMixin):
    def setUp(self):
        self.from_date = datetime.date.today()
        self.limglish = self.setup_full_tenant("limglish", student_count=2)
        self.foreign = self.setup_full_tenant("godmin", student_count=1)
        self.session = self.limglish["clinic_session"]
        self.session.date = self.from_date
        self.session.start_time = datetime.time(18, 0)
        self.session.duration_minutes = 360
        self.session.booking_max_stay_minutes = 240
        self.session.allow_multi_slot_booking = True
        self.session.allow_time_preference = True
        self.session.save(update_fields=[
            "date",
            "start_time",
            "duration_minutes",
            "booking_max_stay_minutes",
            "allow_multi_slot_booking",
            "allow_time_preference",
            "updated_at",
        ])
        self.participants = [
            self.make_participant(
                self.limglish["tenant"],
                self.session,
                student,
                status=status,
            )
            for student, status in zip(
                self.limglish["students"],
                (SessionParticipant.Status.BOOKED, SessionParticipant.Status.CANCELLED),
            )
        ]
        foreign_session = self.foreign["clinic_session"]
        foreign_session.date = self.from_date
        foreign_session.save(update_fields=["date", "updated_at"])

    def _dry_run(self):
        output = StringIO()
        call_command(
            "convert_limglish_clinic_time_ranges",
            "--from-date",
            self.from_date.isoformat(),
            stdout=output,
        )
        return json.loads(output.getvalue())

    def test_dry_run_is_pii_free_and_does_not_mutate_any_tenant(self):
        report = self._dry_run()

        self.assertEqual(report["mode"], "dry-run")
        self.assertEqual(report["tenant_code"], "limglish")
        self.assertEqual(report["target_session_ids"], [self.session.id])
        self.assertEqual(report["target_participant_count"], 2)
        self.assertNotIn("student", json.dumps(report).lower())
        self.assertTrue(report["required_confirmation_token"])
        self.session.refresh_from_db()
        self.limglish["tenant"].refresh_from_db()
        self.assertEqual(self.session.booking_mode, "fixed_slot")
        self.assertEqual(self.limglish["tenant"].clinic_booking_mode, "fixed_slot")

    def test_execute_requires_exact_fresh_token_and_converts_only_limglish(self):
        report = self._dry_run()
        with self.assertRaises(CommandError):
            call_command(
                "convert_limglish_clinic_time_ranges",
                "--from-date",
                self.from_date.isoformat(),
                "--execute",
                "--confirm",
                "wrong",
                stdout=StringIO(),
            )

        output = StringIO()
        call_command(
            "convert_limglish_clinic_time_ranges",
            "--from-date",
            self.from_date.isoformat(),
            "--execute",
            "--confirm",
            report["required_confirmation_token"],
            stdout=output,
        )
        executed = json.loads(output.getvalue())
        self.assertEqual(executed["mode"], "execute")
        self.assertEqual(executed["converted_session_count"], 1)
        self.assertEqual(executed["backfilled_participant_count"], 2)

        self.limglish["tenant"].refresh_from_db()
        self.session.refresh_from_db()
        self.foreign["tenant"].refresh_from_db()
        self.assertEqual(self.limglish["tenant"].clinic_booking_mode, "time_range")
        self.assertEqual(self.limglish["tenant"].clinic_booking_interval_minutes, 60)
        self.assertEqual(self.limglish["tenant"].clinic_booking_max_stay_minutes, 600)
        self.assertFalse(self.limglish["tenant"].clinic_allow_multi_slot_booking_default)
        self.assertEqual(self.session.booking_mode, "time_range")
        self.assertEqual(self.session.booking_interval_minutes, 60)
        self.assertEqual(self.session.booking_max_stay_minutes, 600)
        self.assertFalse(self.session.allow_multi_slot_booking)
        self.assertFalse(self.session.allow_time_preference)
        self.assertEqual(self.foreign["tenant"].clinic_booking_mode, "fixed_slot")
        for participant in self.participants:
            participant.refresh_from_db()
            self.assertEqual(participant.booking_start_time, datetime.time(18, 0))
            self.assertEqual(participant.booking_end_time, datetime.time(0, 0))

        second = self._dry_run()
        self.assertEqual(second["target_session_ids"], [])
        self.assertEqual(second["target_participant_count"], 0)
        self.assertFalse(second["tenant_default_change_required"])

    def test_unsupported_session_past_midnight_fails_closed_without_partial_writes(self):
        self.session.duration_minutes = 420
        self.session.save(update_fields=["duration_minutes", "updated_at"])

        with self.assertRaisesMessage(CommandError, "자정 이후"):
            self._dry_run()

        self.limglish["tenant"].refresh_from_db()
        self.session.refresh_from_db()
        self.assertEqual(self.limglish["tenant"].clinic_booking_mode, "fixed_slot")
        self.assertEqual(self.session.booking_mode, "fixed_slot")

    def test_existing_time_range_is_normalized_without_overwriting_actual_ranges(self):
        self.session.booking_mode = "time_range"
        self.session.booking_interval_minutes = 30
        self.session.booking_max_stay_minutes = 240
        self.session.allow_multi_slot_booking = True
        self.session.allow_time_preference = True
        self.session.save(update_fields=[
            "booking_mode",
            "booking_interval_minutes",
            "booking_max_stay_minutes",
            "allow_multi_slot_booking",
            "allow_time_preference",
            "updated_at",
        ])
        expected_ranges = (
            (datetime.time(18, 0), datetime.time(20, 0)),
            (datetime.time(20, 0), datetime.time(0, 0)),
        )
        for participant, (start, end) in zip(self.participants, expected_ranges):
            participant.booking_start_time = start
            participant.booking_end_time = end
            participant.save(update_fields=[
                "booking_start_time",
                "booking_end_time",
                "updated_at",
            ])

        report = self._dry_run()
        self.assertEqual(report["target_session_ids"], [self.session.id])
        self.assertEqual(report["target_participant_count"], 0)
        output = StringIO()
        call_command(
            "convert_limglish_clinic_time_ranges",
            "--from-date",
            self.from_date.isoformat(),
            "--execute",
            "--confirm",
            report["required_confirmation_token"],
            stdout=output,
        )

        self.session.refresh_from_db()
        self.assertEqual(self.session.booking_interval_minutes, 60)
        self.assertEqual(self.session.booking_max_stay_minutes, 600)
        self.assertFalse(self.session.allow_multi_slot_booking)
        self.assertFalse(self.session.allow_time_preference)
        for participant, expected_range in zip(self.participants, expected_ranges):
            participant.refresh_from_db()
            self.assertEqual(
                (participant.booking_start_time, participant.booking_end_time),
                expected_range,
            )

    def test_existing_time_range_with_half_hour_participant_fails_before_normalizing(self):
        self.session.booking_mode = "time_range"
        self.session.booking_interval_minutes = 30
        self.session.save(update_fields=[
            "booking_mode",
            "booking_interval_minutes",
            "updated_at",
        ])
        participant = self.participants[0]
        participant.booking_start_time = datetime.time(18, 30)
        participant.booking_end_time = datetime.time(20, 0)
        participant.save(update_fields=[
            "booking_start_time",
            "booking_end_time",
            "updated_at",
        ])
        other = self.participants[1]
        other.booking_start_time = datetime.time(20, 0)
        other.booking_end_time = datetime.time(22, 0)
        other.save(update_fields=[
            "booking_start_time",
            "booking_end_time",
            "updated_at",
        ])

        with self.assertRaisesMessage(CommandError, "60-minute interval"):
            self._dry_run()

        self.session.refresh_from_db()
        participant.refresh_from_db()
        self.assertEqual(self.session.booking_interval_minutes, 30)
        self.assertEqual(participant.booking_start_time, datetime.time(18, 30))

    def test_fixed_participant_longer_than_new_max_stay_fails_without_writes(self):
        self.session.start_time = datetime.time(8, 0)
        self.session.duration_minutes = 720
        self.session.save(update_fields=["start_time", "duration_minutes", "updated_at"])

        with self.assertRaisesMessage(CommandError, "maximum stay"):
            self._dry_run()

        self.session.refresh_from_db()
        self.assertEqual(self.session.booking_mode, "fixed_slot")
        for participant in self.participants:
            participant.refresh_from_db()
            self.assertIsNone(participant.booking_start_time)
            self.assertIsNone(participant.booking_end_time)

    def test_exact_tenant_code_must_resolve_once(self):
        self.limglish["tenant"].delete()

        with self.assertRaisesMessage(CommandError, "exactly one"):
            self._dry_run()
