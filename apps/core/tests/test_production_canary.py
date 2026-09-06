from __future__ import annotations

import json
from datetime import timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core.models import Tenant, WorkerHeartbeatModel
from apps.core.models.program import Program
from apps.domains.lectures.models import Lecture, Session
from apps.domains.messaging.models import AutoSendConfig, MessageTemplate
from apps.domains.staffs.models import Staff
from apps.domains.students.models import Student
from apps.domains.video.models import Video, VideoTranscodeJob


User = get_user_model()


class ProductionCanaryTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            code="prod-canary",
            name="Production Canary",
            is_active=True,
        )
        self.owner_settings = override_settings(OWNER_TENANT_ID=self.tenant.id)
        self.owner_settings.enable()
        self.addCleanup(self.owner_settings.disable)
        self.program = Program.objects.get(tenant=self.tenant)
        self.program.subscription_expires_at = timezone.localdate() + timedelta(days=30)
        self.program.next_billing_at = timezone.localdate() + timedelta(days=30)
        self.program.feature_flags = {
            "student_app_enabled": True,
            "admin_enabled": True,
            "attendance_hourly_rate": 15000,
        }
        self.program.save(
            update_fields=["subscription_expires_at", "next_billing_at", "feature_flags", "updated_at"]
        )

    def _call(self, *extra):
        out = StringIO()
        call_command(
            "production_canary",
            "--tenant-id",
            str(self.tenant.id),
            "--indent",
            "2",
            *extra,
            stdout=out,
        )
        return json.loads(out.getvalue())

    def _call_expect_fail(self, *extra):
        out = StringIO()
        with self.assertRaises(CommandError):
            call_command(
                "production_canary",
                "--tenant-id",
                str(self.tenant.id),
                "--indent",
                "2",
                *extra,
                stdout=out,
            )
        return json.loads(out.getvalue())

    def test_clean_tenant_passes(self):
        payload = self._call()

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["summary"]["errors"], 0)

    def test_idle_messaging_worker_option_allows_idle_heartbeat_with_fail_on_warning(self):
        WorkerHeartbeatModel.objects.create(
            name="messaging",
            instance="i-idle-worker",
            last_seen_at=timezone.now() - timedelta(minutes=10),
        )

        failed_payload = self._call_expect_fail("--fail-on-warning")
        failed_check = next(
            item for item in failed_payload["checks"] if item["name"] == "messaging_worker_heartbeat"
        )
        self.assertFalse(failed_check["ok"])

        payload = self._call("--fail-on-warning", "--allow-idle-messaging-worker")

        check = next(item for item in payload["checks"] if item["name"] == "messaging_worker_heartbeat")
        self.assertTrue(check["ok"])
        self.assertEqual(check["data"]["status"], "idle")
        self.assertTrue(check["data"]["idle_scale_to_zero_allowed"])

    def test_enabled_autosend_without_registered_event_envelope_fails(self):
        template = MessageTemplate.objects.create(
            tenant=self.tenant,
            name="Pending Matchup",
            body="Body",
            solapi_template_id="pending-sid",
            solapi_status="PENDING",
        )
        AutoSendConfig.objects.create(
            tenant=self.tenant,
            trigger="matchup_report_submitted",
            template=template,
            enabled=True,
            message_mode="alimtalk",
        )

        payload = self._call_expect_fail()

        check = next(item for item in payload["checks"] if item["name"] == "messaging_autosend_ready")
        self.assertFalse(check["ok"])
        self.assertEqual(check["severity"], "error")
        self.assertEqual(check["data"]["enabled_without_template"], 1)
        self.assertEqual(check["data"]["enabled_unapproved_template"], 0)
        self.assertEqual(check["data"]["enabled_manual_only"], 0)
        self.assertEqual(check["data"]["samples"][0]["trigger"], "matchup_report_submitted")

    def test_e2e_residue_fails(self):
        user = User.objects.create_user(
            tenant=self.tenant,
            username="prod-canary-residue",
            password="test1234",
        )
        Student.objects.create(
            tenant=self.tenant,
            user=user,
            name="[E2E-123456] Residue Student",
            ps_number="E2E-123456",
            omr_code="E2123456",
            parent_phone="01012345678",
        )

        payload = self._call_expect_fail()

        check = next(item for item in payload["checks"] if item["name"] == "production_e2e_residue_absent")
        self.assertFalse(check["ok"])
        self.assertEqual(check["data"]["total"], 1)
        self.assertEqual(check["data"]["groups"]["students"]["count"], 1)

    def test_e2e_lecture_and_session_residue_fail(self):
        lecture = Lecture.objects.create(
            tenant=self.tenant,
            title="[E2E-123456] OMR lecture",
            name="E2E",
            subject="MATH",
        )
        Session.objects.create(
            lecture=lecture,
            title="[E2E-123456] OMR session",
            order=1,
        )

        payload = self._call_expect_fail()

        check = next(
            item
            for item in payload["checks"]
            if item["name"] == "production_e2e_residue_absent"
        )
        self.assertFalse(check["ok"])
        self.assertEqual(check["data"]["total"], 2)
        self.assertEqual(check["data"]["groups"]["lectures"]["count"], 1)
        self.assertEqual(check["data"]["groups"]["sessions"]["count"], 1)

    def test_recursive_message_template_copy_chain_fails(self):
        MessageTemplate.objects.create(
            tenant=self.tenant,
            category="default",
            name="학부모 안내",
            body="같은 본문",
        )
        for depth in range(1, 11):
            MessageTemplate.objects.create(
                tenant=self.tenant,
                category="default",
                name=f"{'복사 - ' * depth}학부모 안내",
                body="같은 본문",
            )

        payload = self._call_expect_fail()

        check = next(
            item
            for item in payload["checks"]
            if item["name"] == "production_e2e_residue_absent"
        )
        self.assertFalse(check["ok"])
        self.assertEqual(
            check["data"]["groups"]["message_template_copy_chains"]["count"],
            10,
        )

    def test_strict_staff_residue_fails(self):
        Staff.objects.create(
            tenant=self.tenant,
            name="[E2E-SCORE-AUDIT-20260517-02] 감사 관리자",
            phone="",
            is_manager=True,
        )

        payload = self._call_expect_fail()

        check = next(
            item
            for item in payload["checks"]
            if item["name"] == "production_e2e_residue_absent"
        )
        self.assertFalse(check["ok"])
        self.assertEqual(check["data"]["groups"]["staffs"]["count"], 1)

    def test_ready_video_without_hls_fails(self):
        Video.objects.create(
            tenant=self.tenant,
            title="Broken Ready Video",
            status=Video.Status.READY,
        )

        payload = self._call_expect_fail()

        check = next(item for item in payload["checks"] if item["name"] == "video_ready_has_hls")
        self.assertFalse(check["ok"])
        self.assertEqual(check["data"]["ready_missing_hls"], 1)
        self.assertEqual(check["data"]["samples"][0]["title"], "Broken Ready Video")

    def test_ready_youtube_video_without_hls_passes(self):
        Video.objects.create(
            tenant=self.tenant,
            title="YouTube Ready Video",
            status=Video.Status.READY,
            source_type=Video.SourceType.YOUTUBE,
            youtube_video_id="dQw4w9WgXcQ",
        )

        payload = self._call()

        hls_check = next(item for item in payload["checks"] if item["name"] == "video_ready_has_hls")
        thumbnail_check = next(item for item in payload["checks"] if item["name"] == "video_ready_has_thumbnail")
        self.assertTrue(hls_check["ok"])
        self.assertEqual(hls_check["data"]["ready_missing_hls"], 0)
        self.assertTrue(thumbnail_check["ok"])
        self.assertEqual(thumbnail_check["data"]["ready_missing_thumbnail"], 0)

    def test_stale_running_video_job_fails(self):
        video = Video.objects.create(
            tenant=self.tenant,
            title="Stale Processing",
            status=Video.Status.PROCESSING,
        )
        job = VideoTranscodeJob.objects.create(
            tenant=self.tenant,
            video=video,
            state=VideoTranscodeJob.State.RUNNING,
            last_heartbeat_at=timezone.now() - timedelta(hours=3),
        )
        video.current_job = job
        video.save(update_fields=["current_job"])

        payload = self._call_expect_fail("--video-stale-minutes", "30")

        check = next(item for item in payload["checks"] if item["name"] == "video_active_jobs_not_stale")
        self.assertFalse(check["ok"])
        self.assertEqual(check["data"]["stale_active_jobs"], 1)

    def test_stale_queued_video_job_fails(self):
        video = Video.objects.create(
            tenant=self.tenant,
            title="Stale Queued",
            status=Video.Status.UPLOADED,
        )
        job = VideoTranscodeJob.objects.create(
            tenant=self.tenant,
            video=video,
            state=VideoTranscodeJob.State.QUEUED,
        )
        video.current_job = job
        video.save(update_fields=["current_job"])
        old = timezone.now() - timedelta(hours=3)
        Video.objects.filter(pk=video.pk).update(updated_at=old)
        VideoTranscodeJob.objects.filter(pk=job.pk).update(updated_at=old)

        payload = self._call_expect_fail("--video-stale-minutes", "30")

        check = next(item for item in payload["checks"] if item["name"] == "video_active_jobs_not_stale")
        self.assertFalse(check["ok"])
        self.assertEqual(check["data"]["stale_active_jobs"], 1)

    def test_video_current_job_must_match_same_tenant_and_video(self):
        other_tenant = Tenant.objects.create(
            code="prod-canary-other",
            name="Production Canary Other",
            is_active=True,
        )
        video = Video.objects.create(
            tenant=self.tenant,
            title="Wrong Current Job",
            status=Video.Status.PROCESSING,
        )
        other_video = Video.objects.create(
            tenant=other_tenant,
            title="Other Tenant Job Owner",
            status=Video.Status.PROCESSING,
        )
        wrong_job = VideoTranscodeJob.objects.create(
            tenant=other_tenant,
            video=other_video,
            state=VideoTranscodeJob.State.RUNNING,
            last_heartbeat_at=timezone.now(),
        )
        video.current_job = wrong_job
        video.save(update_fields=["current_job"])

        payload = self._call_expect_fail()

        check = next(item for item in payload["checks"] if item["name"] == "video_current_job_matches_video")
        self.assertFalse(check["ok"])
        self.assertEqual(check["data"]["mismatched_current_job"], 1)
        self.assertEqual(check["data"]["samples"][0]["current_job__tenant_id"], other_tenant.id)

    @override_settings(TOSS_AUTO_BILLING_ENABLED=True, TOSS_PAYMENTS_SECRET_KEY="")
    def test_auto_billing_enabled_without_secret_fails(self):
        payload = self._call_expect_fail()

        check = next(item for item in payload["checks"] if item["name"] == "billing_auto_enabled_has_secret")
        self.assertFalse(check["ok"])
        self.assertEqual(check["severity"], "error")

    @override_settings(
        TOSS_AUTO_BILLING_ENABLED=True,
        TOSS_PAYMENTS_CLIENT_KEY="test_ck_not_live",
        TOSS_PAYMENTS_SECRET_KEY="test_sk_not_live",
    )
    def test_auto_billing_enabled_with_test_keys_fails_live_pair_check(self):
        payload = self._call_expect_fail()

        check = next(
            item for item in payload["checks"]
            if item["name"] == "billing_auto_enabled_has_live_key_pair"
        )
        self.assertFalse(check["ok"])
        self.assertEqual(check["severity"], "error")

    @override_settings(
        TOSS_AUTO_BILLING_ENABLED=True,
        TOSS_PAYMENTS_CLIENT_KEY="live_ck_billing",
        TOSS_PAYMENTS_SECRET_KEY="live_sk_billing",
        BILLING_KEY_ENCRYPTION_WRITE_ENABLED=False,
        BILLING_KEY_ENCRYPTION_PRIMARY_KEY="",
    )
    def test_auto_billing_enabled_without_encrypted_storage_fails(self):
        payload = self._call_expect_fail()

        check = next(
            item for item in payload["checks"]
            if item["name"] == "billing_auto_enabled_has_encrypted_key_storage"
        )
        self.assertFalse(check["ok"])
        self.assertEqual(check["severity"], "error")

    def test_fee_management_is_available_without_allowlist(self):
        self.program.feature_flags = {"fee_management": False}
        self.program.save(update_fields=["feature_flags", "updated_at"])

        payload = self._call()

        check = next(
            item for item in payload["checks"]
            if item["name"] == "fee_management_availability"
        )
        self.assertTrue(check["ok"])
        self.assertTrue(check["data"]["available_to_all_tenants"])
