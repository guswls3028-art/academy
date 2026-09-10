from __future__ import annotations

from importlib import import_module
from unittest.mock import MagicMock

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.core.models import Tenant
from apps.domains.enrollment.models import Enrollment
from apps.domains.lectures.models import Lecture
from apps.domains.students.models import Student
from apps.domains.students.services.lifecycle import restore_student, soft_delete_student


MIGRATION = import_module(
    "apps.domains.enrollment.migrations.0002_student_deletion_status_snapshot"
)
User = get_user_model()


class StudentDeletionSnapshotMigrationSQLTests(SimpleTestCase):
    def _schema_editor(self):
        enrollment_model = MagicMock()
        enrollment_model._meta.db_table = "enrollment_enrollment"
        student_model = MagicMock()
        student_model._meta.db_table = "students_student"
        historical_apps = MagicMock()
        historical_apps.get_model.side_effect = (
            lambda app_label, model_name: enrollment_model
            if (app_label, model_name) == ("enrollment", "Enrollment")
            else student_model
        )
        schema_editor = MagicMock()
        schema_editor.connection.vendor = "postgresql"
        schema_editor.quote_name.side_effect = lambda value: f'"{value}"'
        return historical_apps, schema_editor

    def test_forward_sql_is_dual_write_safe_and_reverse_is_exact(self):
        historical_apps, schema_editor = self._schema_editor()

        MIGRATION.install_mixed_runtime_snapshot_trigger(
            historical_apps,
            schema_editor,
        )

        forward_sql = schema_editor.execute.call_args.args[0]
        self.assertIn("OLD.status IN ('ACTIVE', 'PENDING')", forward_sql)
        self.assertIn("NEW.status_before_student_deletion IS NULL", forward_sql)
        self.assertIn("student.deleted_at IS NOT NULL", forward_sql)
        self.assertIn(f"CREATE TRIGGER {MIGRATION.TRIGGER_NAME}", forward_sql)
        self.assertIn(f"EXECUTE FUNCTION {MIGRATION.FUNCTION_NAME}()", forward_sql)

        schema_editor.execute.reset_mock()
        MIGRATION.remove_mixed_runtime_snapshot_trigger(
            historical_apps,
            schema_editor,
        )

        reverse_sql = schema_editor.execute.call_args.args[0]
        self.assertIn(
            f"DROP TRIGGER IF EXISTS {MIGRATION.TRIGGER_NAME} "
            'ON "enrollment_enrollment";',
            reverse_sql,
        )
        self.assertIn(
            f"DROP FUNCTION IF EXISTS {MIGRATION.FUNCTION_NAME}();",
            reverse_sql,
        )


class StudentDeletionSnapshotBackfillTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Snapshot backfill",
            code="snapshot-backfill",
            is_active=True,
        )
        self.lecture = Lecture.objects.create(
            tenant=self.tenant,
            title="Snapshot lecture",
            name="Snapshot lecture",
            subject="MATH",
        )

    def _student(self, suffix: str, *, deleted: bool) -> Student:
        user = User.objects.create_user(
            tenant=self.tenant,
            username=f"snapshot-backfill-{suffix}",
            password="test1234",
        )
        return Student.objects.create(
            tenant=self.tenant,
            user=user,
            name=f"Snapshot {suffix}",
            ps_number=f"SNAP-{suffix}",
            omr_code=f"SNAP{suffix:0>4}",
            parent_phone=f"0109000{int(suffix):04d}",
            deleted_at=timezone.now() if deleted else None,
        )

    def test_legacy_deleted_rows_fail_closed_without_touching_active_students(self):
        deleted_student = self._student("1", deleted=True)
        active_student = self._student("2", deleted=False)
        deleted_enrollment = Enrollment.objects.create(
            tenant=self.tenant,
            lecture=self.lecture,
            student=deleted_student,
            status="ACTIVE",
        )
        active_enrollment = Enrollment.objects.create(
            tenant=self.tenant,
            lecture=self.lecture,
            student=active_student,
            status="PENDING",
        )

        MIGRATION.preserve_legacy_deleted_students_fail_closed(django_apps, None)

        deleted_enrollment.refresh_from_db()
        active_enrollment.refresh_from_db()
        self.assertEqual(deleted_enrollment.status, "INACTIVE")
        self.assertEqual(
            deleted_enrollment.status_before_student_deletion,
            "INACTIVE",
        )
        self.assertEqual(active_enrollment.status, "PENDING")
        self.assertIsNone(active_enrollment.status_before_student_deletion)


class StudentDeletionSnapshotPostgresTests(TestCase):
    @classmethod
    def setUpClass(cls):
        if connection.vendor != "postgresql":
            from unittest import SkipTest

            raise SkipTest("PostgreSQL trigger contract")
        super().setUpClass()

    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Snapshot PostgreSQL",
            code="snapshot-postgresql",
            is_active=True,
        )
        self.user = User.objects.create_user(
            tenant=self.tenant,
            username="snapshot-postgresql-student",
            password="test1234",
        )
        self.student = Student.objects.create(
            tenant=self.tenant,
            user=self.user,
            name="Snapshot Student",
            ps_number="SNAP-PG",
            omr_code="SNAPPG01",
            parent_phone="01095550000",
        )
        self.lecture = Lecture.objects.create(
            tenant=self.tenant,
            title="Snapshot PostgreSQL lecture",
            name="Snapshot PostgreSQL lecture",
            subject="MATH",
        )

    def test_trigger_and_function_are_installed_in_current_schema(self):
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT pg_get_triggerdef(t.oid)
                FROM pg_trigger t
                JOIN pg_class c ON c.oid = t.tgrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE t.tgname = %s
                  AND n.nspname = current_schema()
                  AND NOT t.tgisinternal
                """,
                [MIGRATION.TRIGGER_NAME],
            )
            trigger_definitions = [row[0] for row in cursor.fetchall()]
            cursor.execute(
                """
                SELECT pg_get_functiondef(p.oid)
                FROM pg_proc p
                JOIN pg_namespace n ON n.oid = p.pronamespace
                WHERE p.proname = %s
                  AND n.nspname = current_schema()
                """,
                [MIGRATION.FUNCTION_NAME],
            )
            function_definitions = [row[0] for row in cursor.fetchall()]

        self.assertEqual(len(trigger_definitions), 1)
        self.assertIn("BEFORE UPDATE OF status", trigger_definitions[0])
        self.assertEqual(len(function_definitions), 1)
        self.assertIn(
            "new.status_before_student_deletion is null",
            function_definitions[0].lower(),
        )

    def test_old_runtime_update_is_restored_by_new_runtime(self):
        active_enrollment = Enrollment.objects.create(
            tenant=self.tenant,
            lecture=self.lecture,
            student=self.student,
            status="ACTIVE",
        )
        pending_lecture = Lecture.objects.create(
            tenant=self.tenant,
            title="Pending PostgreSQL lecture",
            name="Pending PostgreSQL lecture",
            subject="MATH",
        )
        pending_enrollment = Enrollment.objects.create(
            tenant=self.tenant,
            lecture=pending_lecture,
            student=self.student,
            status="PENDING",
        )
        self.student.deleted_at = timezone.now()
        self.student.ps_number = f"_del_{self.student.id}_{self.student.ps_number}"
        self.student.save(update_fields=["deleted_at", "ps_number"])

        Enrollment.objects.filter(student=self.student).update(status="INACTIVE")

        active_enrollment.refresh_from_db()
        pending_enrollment.refresh_from_db()
        self.assertEqual(
            active_enrollment.status_before_student_deletion,
            "ACTIVE",
        )
        self.assertEqual(
            pending_enrollment.status_before_student_deletion,
            "PENDING",
        )

        restore_student(
            self.student,
            tenant=self.tenant,
            parent_initial_password="test-password",
        )

        active_enrollment.refresh_from_db()
        pending_enrollment.refresh_from_db()
        self.assertEqual(active_enrollment.status, "ACTIVE")
        self.assertEqual(pending_enrollment.status, "PENDING")
        self.assertIsNone(active_enrollment.status_before_student_deletion)
        self.assertIsNone(pending_enrollment.status_before_student_deletion)

    def test_new_runtime_dual_write_is_not_overwritten_by_trigger(self):
        enrollment = Enrollment.objects.create(
            tenant=self.tenant,
            lecture=self.lecture,
            student=self.student,
            status="PENDING",
        )

        soft_delete_student(self.student, tenant=self.tenant)

        enrollment.refresh_from_db()
        self.assertEqual(enrollment.status, "INACTIVE")
        self.assertEqual(enrollment.status_before_student_deletion, "PENDING")
