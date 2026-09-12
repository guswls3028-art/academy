from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.apps import apps
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from apps.core.models import Tenant
from apps.domains.video.models import Video, VideoFolder


Lecture = apps.get_model("lectures", "Lecture")
Session = apps.get_model("lectures", "Session")


class RepairVideoOrderDuplicatesCommandTests(TransactionTestCase):
    """Exercise the Phase A repair command against the pre-constraint schema."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.addClassCleanup(cls._restore_current_schema)
        MigrationExecutor(connection).migrate([("video", "0018_video_source_type_youtube")])

    @classmethod
    def _restore_current_schema(cls):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes("video"))

    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Repair Video Tenant",
            code="repair-video",
            is_active=True,
        )
        lecture = Lecture.get_or_create_system_lecture(self.tenant)
        self.duplicate_session = Session.objects.create(
            lecture=lecture,
            title="중복 차시",
            order=1,
            regular_order=1,
        )
        self.unique_session = Session.objects.create(
            lecture=lecture,
            title="간격 유지 차시",
            order=2,
            regular_order=2,
        )
        self.a = Video.objects.create(
            tenant=self.tenant,
            session=self.duplicate_session,
            title="A",
            order=1,
        )
        self.b = Video.objects.create(
            tenant=self.tenant,
            session=self.duplicate_session,
            title="B",
            order=1,
        )
        self.c = Video.objects.create(
            tenant=self.tenant,
            session=self.duplicate_session,
            title="C",
            order=2,
        )
        self.unique = Video.objects.create(
            tenant=self.tenant,
            session=self.unique_session,
            title="Unique gap",
            order=9,
        )

    def test_dry_run_execute_and_rollback_are_checksummed_and_reversible(self):
        with TemporaryDirectory() as temp_dir:
            plan_path = Path(temp_dir) / "video-order-plan.json"
            backup_path = Path(temp_dir) / "video-order-backup.json"
            stdout = StringIO()
            call_command(
                "repair_video_order_duplicates",
                tenant=self.tenant.code,
                backup_file=str(plan_path),
                stdout=stdout,
            )
            dry_plan = json.loads(plan_path.read_text(encoding="utf-8"))

            self.assertEqual(dry_plan["affected_session_ids"], [self.duplicate_session.id])
            self.assertEqual(dry_plan["changed_rows"], 2)
            self.assertNotIn("title", dry_plan["rows"][0])
            self.assertIn(f"checksum={dry_plan['rows_checksum']}", stdout.getvalue())

            call_command(
                "repair_video_order_duplicates",
                tenant=self.tenant.code,
                execute=True,
                confirm=self.tenant.code,
                expected_checksum=dry_plan["rows_checksum"],
                backup_file=str(backup_path),
            )

            self.a.refresh_from_db()
            self.b.refresh_from_db()
            self.c.refresh_from_db()
            self.unique.refresh_from_db()
            self.assertEqual((self.a.order, self.b.order, self.c.order), (1, 2, 3))
            self.assertEqual(self.unique.order, 9)
            applied = json.loads(backup_path.read_text(encoding="utf-8"))
            self.assertEqual(applied["state"], "applied")

            call_command(
                "repair_video_order_duplicates",
                tenant=self.tenant.code,
                rollback=str(backup_path),
                confirm=self.tenant.code,
            )

            self.a.refresh_from_db()
            self.b.refresh_from_db()
            self.c.refresh_from_db()
            self.assertEqual((self.a.order, self.b.order, self.c.order), (1, 1, 2))

    def test_execute_rejects_changed_plan_checksum_without_mutation(self):
        with TemporaryDirectory() as temp_dir:
            backup_path = Path(temp_dir) / "video-order-backup.json"

            with self.assertRaisesRegex(CommandError, "plan_checksum_mismatch"):
                call_command(
                    "repair_video_order_duplicates",
                    tenant=self.tenant.code,
                    execute=True,
                    confirm=self.tenant.code,
                    expected_checksum="not-the-reviewed-plan",
                    backup_file=str(backup_path),
                )

            self.b.refresh_from_db()
            self.c.refresh_from_db()
            self.assertEqual((self.b.order, self.c.order), (1, 2))

    def test_execute_refuses_to_overwrite_existing_backup(self):
        with TemporaryDirectory() as temp_dir:
            backup_path = Path(temp_dir) / "existing.json"
            backup_path.write_text("evidence", encoding="utf-8")

            with self.assertRaisesRegex(CommandError, "backup_file_already_exists"):
                call_command(
                    "repair_video_order_duplicates",
                    tenant=self.tenant.code,
                    execute=True,
                    confirm=self.tenant.code,
                    expected_checksum="irrelevant",
                    backup_file=str(backup_path),
                )

            self.assertEqual(backup_path.read_text(encoding="utf-8"), "evidence")

    def test_rollback_rejects_dry_run_backup(self):
        with TemporaryDirectory() as temp_dir:
            plan_path = Path(temp_dir) / "dry-run.json"
            call_command(
                "repair_video_order_duplicates",
                tenant=self.tenant.code,
                backup_file=str(plan_path),
            )

            with self.assertRaisesRegex(CommandError, "rollback_requires_applied_backup"):
                call_command(
                    "repair_video_order_duplicates",
                    tenant=self.tenant.code,
                    rollback=str(plan_path),
                    confirm=self.tenant.code,
                )

    def test_folder_precedence_repairs_videos_that_also_have_session(self):
        folder = VideoFolder.objects.create(
            tenant=self.tenant,
            session=self.duplicate_session,
            name="공개 폴더",
        )
        first = Video.objects.create(
            tenant=self.tenant,
            session=self.duplicate_session,
            folder=folder,
            title="Folder A",
            order=5,
        )
        second = Video.objects.create(
            tenant=self.tenant,
            session=self.duplicate_session,
            folder=folder,
            title="Folder B",
            order=5,
        )

        stdout = StringIO()
        call_command(
            "repair_video_order_duplicates",
            tenant=self.tenant.code,
            folder_ids=[folder.id],
            stdout=stdout,
        )

        self.assertIn("folders=1", stdout.getvalue())
        self.assertIn(f"folder={folder.id}", stdout.getvalue())
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual((first.order, second.order), (5, 5))

    def test_planned_execute_backup_can_recover_if_applied_marker_write_crashes(self):
        with TemporaryDirectory() as temp_dir:
            plan_path = Path(temp_dir) / "review-plan.json"
            backup_path = Path(temp_dir) / "execute-backup.json"
            call_command(
                "repair_video_order_duplicates",
                tenant=self.tenant.code,
                backup_file=str(plan_path),
            )
            plan = json.loads(plan_path.read_text(encoding="utf-8"))

            with patch(
                "apps.domains.video.management.commands."
                "repair_video_order_duplicates.os.replace",
                side_effect=OSError("simulated_atomic_replace_failure"),
            ):
                with self.assertRaisesRegex(CommandError, "backup_state_update_failed"):
                    call_command(
                        "repair_video_order_duplicates",
                        tenant=self.tenant.code,
                        execute=True,
                        confirm=self.tenant.code,
                        expected_checksum=plan["rows_checksum"],
                        backup_file=str(backup_path),
                    )

            self.b.refresh_from_db()
            self.assertEqual(self.b.order, 2)
            crash_backup = json.loads(backup_path.read_text(encoding="utf-8"))
            self.assertEqual(crash_backup["state"], "planned")
            self.assertTrue(crash_backup["mutation_backup"])

            call_command(
                "repair_video_order_duplicates",
                tenant=self.tenant.code,
                rollback=str(backup_path),
                confirm=self.tenant.code,
            )

            self.b.refresh_from_db()
            self.assertEqual(self.b.order, 1)
