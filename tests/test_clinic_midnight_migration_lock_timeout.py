import datetime
import threading
import time
from unittest import skipUnless

import psycopg2
from django.db import OperationalError, close_old_connections, connection, connections
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder
from django.test import TransactionTestCase

from apps.domains.clinic.tests import ClinicAPITestMixin


@skipUnless(connection.vendor == "postgresql", "PostgreSQL migration contract")
class ClinicMidnightMigrationLockTimeoutTests(TransactionTestCase, ClinicAPITestMixin):
    migrate_from = ("clinic", "0020_session_booking_interval_minutes_and_more")
    migrate_to = ("clinic", "0021_allow_booking_range_to_end_at_midnight")
    constraint_name = "clinic_participant_booking_range_order"
    table_name = "clinic_sessionparticipant"

    def _migrate(self, target):
        MigrationExecutor(connection).migrate([target])

    def _constraint_definition(self):
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT pg_get_constraintdef(oid)
                FROM pg_constraint
                WHERE conrelid = %s::regclass
                  AND conname = %s
                """,
                [self.table_name, self.constraint_name],
            )
            row = cursor.fetchone()
        self.assertIsNotNone(row)
        return row[0]

    def _migration_is_applied(self):
        return self.migrate_to in MigrationRecorder(connection).applied_migrations()

    def _timeout_settings(self, database_connection):
        with database_connection.cursor() as cursor:
            cursor.execute("SHOW lock_timeout")
            lock_timeout = cursor.fetchone()[0]
            cursor.execute("SHOW statement_timeout")
            statement_timeout = cursor.fetchone()[0]
        return lock_timeout, statement_timeout

    def test_lock_timeout_rolls_back_constraint_change_and_retry_preserves_rows(self):
        self._migrate(self.migrate_from)
        data = self.setup_api_tenant("clinic_migration_lock", student_count=1)
        participant = self.make_participant(
            data["tenant"],
            data["clinic_session"],
            data["students"][0],
            status="booked",
        )
        participant.booking_start_time = datetime.time(10, 0)
        participant.booking_end_time = datetime.time(11, 0)
        participant.save(update_fields=["booking_start_time", "booking_end_time", "updated_at"])
        original_constraint = self._constraint_definition()

        blocker = psycopg2.connect(**connection.get_connection_params())
        blocker.autocommit = False
        migration_finished = threading.Event()
        migration_errors = []
        failure_timeout_settings = []
        timeout_observation_errors = []
        migration_thread = None
        completed_while_locked = False

        try:
            with blocker.cursor() as cursor:
                cursor.execute(f"LOCK TABLE {self.table_name} IN ACCESS SHARE MODE")

            def migrate_while_locked():
                close_old_connections()
                started_at = time.monotonic()
                thread_connection = connections["default"]
                before_timeouts = None
                try:
                    before_timeouts = self._timeout_settings(thread_connection)
                    MigrationExecutor(thread_connection).migrate([self.migrate_to])
                except Exception as exc:  # pragma: no cover - asserted by the parent thread
                    migration_errors.append((exc, time.monotonic() - started_at))
                    try:
                        failure_timeout_settings.append(
                            (before_timeouts, self._timeout_settings(thread_connection))
                        )
                    except Exception as observation_exc:  # pragma: no cover
                        timeout_observation_errors.append(observation_exc)
                finally:
                    close_old_connections()
                    migration_finished.set()

            migration_thread = threading.Thread(
                target=migrate_while_locked,
                name="clinic-midnight-migration",
            )
            migration_thread.start()
            completed_while_locked = migration_finished.wait(timeout=8)
        finally:
            blocker.rollback()
            blocker.close()
            if migration_thread is not None:
                migration_thread.join(timeout=10)

        try:
            self.assertTrue(completed_while_locked, "migration exceeded its database lock budget")
            self.assertFalse(migration_thread.is_alive(), "migration thread did not terminate")
            self.assertEqual(len(migration_errors), 1)
            error, elapsed = migration_errors[0]
            self.assertIsInstance(error, OperationalError)
            self.assertEqual(getattr(error.__cause__, "pgcode", None), "55P03")
            self.assertIn("lock timeout", str(error.__cause__).lower())
            self.assertLess(elapsed, 8)
            self.assertEqual(timeout_observation_errors, [])
            self.assertEqual(len(failure_timeout_settings), 1)
            failure_before, failure_after = failure_timeout_settings[0]
            self.assertIsNotNone(failure_before)
            self.assertEqual(failure_after, failure_before)
            self.assertFalse(self._migration_is_applied())
            self.assertEqual(self._constraint_definition(), original_constraint)

            participant.refresh_from_db()
            self.assertEqual(participant.booking_start_time, datetime.time(10, 0))
            self.assertEqual(participant.booking_end_time, datetime.time(11, 0))

            success_before = self._timeout_settings(connection)
            self._migrate(self.migrate_to)
            success_after = self._timeout_settings(connection)
            self.assertTrue(self._migration_is_applied())
            self.assertEqual(success_after, success_before)
            participant.refresh_from_db()
            self.assertEqual(participant.booking_start_time, datetime.time(10, 0))
            self.assertEqual(participant.booking_end_time, datetime.time(11, 0))

            participant.booking_start_time = datetime.time(21, 0)
            participant.booking_end_time = datetime.time(0, 0)
            participant.save(
                update_fields=["booking_start_time", "booking_end_time", "updated_at"]
            )
            participant.refresh_from_db()
            self.assertEqual(participant.booking_end_time, datetime.time(0, 0))
        finally:
            self._migrate(self.migrate_to)
