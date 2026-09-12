import datetime
import json
import threading
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.domains.clinic.management.commands import convert_limglish_clinic_time_ranges
from apps.domains.clinic.models import Session, SessionParticipant
from apps.domains.clinic.serializers import ClinicSessionSerializer
from apps.domains.clinic.tests import ClinicAPITestMixin, ClinicTestMixin


@override_settings(CLINIC_MIDNIGHT_TIME_RANGE_WRITES_ENABLED=True)
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
        with override_settings(CLINIC_MIDNIGHT_TIME_RANGE_WRITES_ENABLED=False):
            no_op_output = StringIO()
            call_command(
                "convert_limglish_clinic_time_ranges",
                "--from-date",
                self.from_date.isoformat(),
                "--execute",
                "--confirm",
                second["required_confirmation_token"],
                stdout=no_op_output,
            )
        no_op = json.loads(no_op_output.getvalue())
        self.assertEqual(no_op["converted_session_count"], 0)
        self.assertEqual(no_op["backfilled_participant_count"], 0)

    @override_settings(CLINIC_MIDNIGHT_TIME_RANGE_WRITES_ENABLED=False)
    def test_execute_requires_reader_first_midnight_write_activation(self):
        report = self._dry_run()

        with self.assertRaisesMessage(CommandError, "reader-first release"):
            call_command(
                "convert_limglish_clinic_time_ranges",
                "--from-date",
                self.from_date.isoformat(),
                "--execute",
                "--confirm",
                report["required_confirmation_token"],
                stdout=StringIO(),
            )

        self.limglish["tenant"].refresh_from_db()
        self.session.refresh_from_db()
        self.assertEqual(self.limglish["tenant"].clinic_booking_mode, "fixed_slot")
        self.assertEqual(self.session.booking_mode, "fixed_slot")

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

    def test_execute_rolls_back_when_locked_postcondition_still_has_targets(self):
        report = self._dry_run()

        with patch.object(Session.objects, "bulk_update", return_value=0):
            with self.assertRaisesMessage(CommandError, "postcondition"):
                call_command(
                    "convert_limglish_clinic_time_ranges",
                    "--from-date",
                    self.from_date.isoformat(),
                    "--execute",
                    "--confirm",
                    report["required_confirmation_token"],
                    stdout=StringIO(),
                )

        self.limglish["tenant"].refresh_from_db()
        self.session.refresh_from_db()
        self.assertEqual(self.limglish["tenant"].clinic_booking_mode, "fixed_slot")
        self.assertEqual(self.session.booking_mode, "fixed_slot")


class ClinicSessionPolicyWriteRaceAPITest(APITestCase, ClinicAPITestMixin):
    def setUp(self):
        self.data = self.setup_api_tenant("limglish", student_count=1)
        self.tenant = self.data["tenant"]
        self.session = self.data["clinic_session"]
        self.session.date = datetime.date.today() + datetime.timedelta(days=1)
        self.session.save(update_fields=["date", "updated_at"])
        self.client.force_authenticate(user=self.data["admin_user"])

    def _flip_tenant_policy_after_first_validation(self):
        original_is_valid = ClinicSessionSerializer.is_valid
        flipped = False

        def is_valid_then_flip(serializer, *args, **kwargs):
            nonlocal flipped
            result = original_is_valid(serializer, *args, **kwargs)
            if not flipped:
                flipped = True
                self.tenant.__class__.objects.filter(pk=self.tenant.pk).update(
                    clinic_booking_mode="time_range",
                    clinic_booking_interval_minutes=60,
                    clinic_booking_max_stay_minutes=600,
                    clinic_allow_multi_slot_booking_default=False,
                )
            return result

        return patch.object(ClinicSessionSerializer, "is_valid", is_valid_then_flip)

    def test_create_rejects_stale_tenant_policy_instead_of_writing_old_default(self):
        with self._flip_tenant_policy_after_first_validation():
            response = self.client.post(
                "/api/v1/clinic/sessions/",
                {
                    "date": self.session.date,
                    "start_time": "15:00",
                    "duration_minutes": 60,
                    "location": "stale-create",
                    "max_participants": 10,
                },
                format="json",
                **self._headers(self.tenant),
            )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(Session.objects.filter(
            tenant=self.tenant,
            location="stale-create",
        ).exists())

    def test_patch_rejects_stale_session_instead_of_reverting_conversion(self):
        original_is_valid = ClinicSessionSerializer.is_valid
        flipped = False

        def is_valid_then_convert(serializer, *args, **kwargs):
            nonlocal flipped
            result = original_is_valid(serializer, *args, **kwargs)
            if not flipped:
                flipped = True
                Session.objects.filter(pk=self.session.pk).update(
                    booking_mode="time_range",
                    booking_interval_minutes=60,
                    booking_max_stay_minutes=600,
                    allow_multi_slot_booking=False,
                    allow_time_preference=False,
                    updated_at=timezone.now(),
                )
            return result

        with patch.object(ClinicSessionSerializer, "is_valid", is_valid_then_convert):
            response = self.client.patch(
                f"/api/v1/clinic/sessions/{self.session.id}/",
                {"title": "stale patch"},
                format="json",
                **self._headers(self.tenant),
            )

        self.assertEqual(response.status_code, 400, response.data)
        self.session.refresh_from_db()
        self.assertEqual(self.session.booking_mode, "time_range")
        self.assertNotEqual(self.session.title, "stale patch")

    def test_locked_patch_response_keeps_participant_projections(self):
        self.make_participant(
            self.tenant,
            self.session,
            self.data["students"][0],
            status=SessionParticipant.Status.BOOKED,
        )

        response = self.client.patch(
            f"/api/v1/clinic/sessions/{self.session.id}/",
            {"title": "projection-safe patch"},
            format="json",
            **self._headers(self.tenant),
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["participant_count"], 1)
        self.assertEqual(response.data["booked_count"], 1)


@override_settings(CLINIC_MIDNIGHT_TIME_RANGE_WRITES_ENABLED=True)
class ClinicConversionPostgresConcurrencyTest(TransactionTestCase, ClinicAPITestMixin):
    reset_sequences = True

    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("conversion writer locking requires PostgreSQL")
        self.from_date = datetime.date.today() + datetime.timedelta(days=1)
        self.data = self.setup_api_tenant("limglish", student_count=1)
        self.tenant = self.data["tenant"]
        self.session = self.data["clinic_session"]
        self.session.date = self.from_date
        self.session.start_time = datetime.time(18, 0)
        self.session.duration_minutes = 360
        self.session.save(update_fields=[
            "date",
            "start_time",
            "duration_minutes",
            "updated_at",
        ])

    def _dry_run_token(self):
        output = StringIO()
        call_command(
            "convert_limglish_clinic_time_ranges",
            "--from-date",
            self.from_date.isoformat(),
            stdout=output,
        )
        return json.loads(output.getvalue())["required_confirmation_token"]

    def _race_conversion_with_writer(self, writer):
        conversion_locked = threading.Event()
        writer_validated = threading.Event()
        token = self._dry_run_token()
        responses = []
        errors = []
        original_build_plan = convert_limglish_clinic_time_ranges._build_plan
        original_is_valid = ClinicSessionSerializer.is_valid

        def gated_build_plan(*args, **kwargs):
            plan = original_build_plan(*args, **kwargs)
            if kwargs.get("lock") and not conversion_locked.is_set():
                conversion_locked.set()
                if not writer_validated.wait(10):
                    raise AssertionError("writer did not validate before conversion release")
            return plan

        def observed_is_valid(serializer, *args, **kwargs):
            result = original_is_valid(serializer, *args, **kwargs)
            writer_validated.set()
            return result

        def convert():
            close_old_connections()
            try:
                with patch.object(
                    convert_limglish_clinic_time_ranges,
                    "_build_plan",
                    side_effect=gated_build_plan,
                ):
                    call_command(
                        "convert_limglish_clinic_time_ranges",
                        "--from-date",
                        self.from_date.isoformat(),
                        "--execute",
                        "--confirm",
                        token,
                        stdout=StringIO(),
                    )
            except Exception as exc:  # pragma: no cover - asserted by caller
                errors.append(exc)
            finally:
                close_old_connections()

        def write():
            close_old_connections()
            try:
                if not conversion_locked.wait(10):
                    raise AssertionError("conversion did not acquire locks")
                client = APIClient()
                client.force_authenticate(user=self.data["admin_user"])
                with patch.object(ClinicSessionSerializer, "is_valid", observed_is_valid):
                    responses.append(writer(client))
            except Exception as exc:  # pragma: no cover - asserted by caller
                errors.append(exc)
            finally:
                close_old_connections()

        threads = [
            threading.Thread(target=convert, name="clinic-conversion"),
            threading.Thread(target=write, name="clinic-session-writer"),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertFalse(any(thread.is_alive() for thread in threads), "race threads hung")
        self.assertEqual(errors, [])
        self.assertEqual(len(responses), 1)
        return responses[0]

    def test_concurrent_create_cannot_escape_conversion_plan(self):
        response = self._race_conversion_with_writer(lambda client: client.post(
            "/api/v1/clinic/sessions/",
            {
                "date": self.from_date,
                "start_time": "15:00",
                "duration_minutes": 60,
                "location": "concurrent-stale-create",
                "max_participants": 10,
            },
            format="json",
            **self._headers(self.tenant),
        ))

        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(Session.objects.filter(
            tenant=self.tenant,
            location="concurrent-stale-create",
        ).exists())
        self.session.refresh_from_db()
        self.assertEqual(self.session.booking_mode, "time_range")

    def test_concurrent_patch_cannot_revert_converted_session(self):
        response = self._race_conversion_with_writer(lambda client: client.patch(
            f"/api/v1/clinic/sessions/{self.session.id}/",
            {"title": "concurrent stale patch"},
            format="json",
            **self._headers(self.tenant),
        ))

        self.assertEqual(response.status_code, 400, response.data)
        self.session.refresh_from_db()
        self.assertEqual(self.session.booking_mode, "time_range")
        self.assertNotEqual(self.session.title, "concurrent stale patch")
