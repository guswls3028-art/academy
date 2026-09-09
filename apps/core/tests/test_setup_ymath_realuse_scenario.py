import json
import os
import threading
import time
from datetime import timedelta
from io import StringIO
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections, connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from apps.api.common.auth_jwt import TenantAwareTokenObtainPairSerializer
from apps.core.management.commands.setup_ymath_realuse_scenario import (
    Command,
    ensure_development_messaging_baseline,
)
from apps.core.models import OpsAuditLog, Program, Tenant, TenantMembership
from apps.core.models.user import user_display_username
from apps.domains.parents.models import Parent
from apps.domains.messaging.models import AutoSendConfig, MessageTemplate
from apps.domains.staffs.models import Staff
from apps.domains.video.models import (
    AccessMode,
    Video,
    VideoAccess,
    VideoPlaybackEvent,
    VideoPlaybackSession,
    VideoProgress,
)


class SetupYmathRealuseScenarioTests(TestCase):
    def _call_command(self, **kwargs):
        out = StringIO()
        student_count = kwargs.pop("student_count", 2)
        session_count = kwargs.pop("session_count", 3)
        with patch.dict(
            os.environ,
            {"YMATH_REALUSE_SCENARIO_PASSWORD": "scenario-test-password"},
        ):
            call_command(
                "setup_ymath_realuse_scenario",
                stdout=out,
                student_count=student_count,
                session_count=session_count,
                **kwargs,
            )
        return out.getvalue()

    def test_creates_idempotent_ymath_shaped_scenario(self):
        first = self._call_command()
        second = self._call_command()

        self.assertIn("YMATH_REALUSE_SCENARIO_READY", first)
        self.assertIn("YMATH_REALUSE_SCENARIO_READY", second)
        tenant = Tenant.objects.get(code="qa-ymath-realuse-20260805")
        program = Program.objects.get(tenant=tenant)
        self.assertEqual(program.brand_key, "ymath")
        self.assertFalse(program.feature_flags["section_mode"])
        self.assertEqual(program.feature_flags["clinic_mode"], "remediation")
        self.assertEqual(program.feature_flags["score_output_mode"], "anonymous_billboard")
        self.assertEqual(program.feature_flags["score_summary_column_default"], "exam_wrong")
        self.assertEqual(program.subscription_status, Program.SubscriptionStatus.ACTIVE)
        self.assertEqual(program.subscription_started_at, timezone.localdate())
        self.assertEqual(
            program.subscription_expires_at,
            timezone.localdate() + timedelta(days=365),
        )
        self.assertFalse(program.cancel_at_period_end)
        self.assertTrue(program.is_subscription_active)
        self.assertIn(
            f'"subscription_expires_at": "{program.subscription_expires_at.isoformat()}"',
            second,
        )
        self.assertEqual(tenant.students.count(), 2)
        self.assertEqual(tenant.lectures.count(), 2)
        self.assertEqual(sum(lecture.sessions.count() for lecture in tenant.lectures.all()), 6)
        self.assertEqual(tenant.enrollments.count(), 4)
        self.assertEqual(
            TenantMembership.objects.filter(tenant=tenant, role="admin", is_active=True).count(),
            1,
        )
        self.assertEqual(
            TenantMembership.objects.filter(tenant=tenant, role="student", is_active=True).count(),
            2,
        )
        User = get_user_model()
        teacher = User.objects.get(username=f"t{tenant.id}_ymath-qa-teacher")
        self.assertTrue(teacher.check_password("scenario-test-password"))
        student = User.objects.get(username=f"t{tenant.id}_ymath-qa-student-01")
        self.assertTrue(student.check_password("scenario-test-password"))
        self.assertFalse(Video.objects.filter(tenant=tenant).exists())

    def test_development_messaging_baseline_is_idempotent_and_preserves_approved_config(self):
        owner = Tenant.objects.create(
            code="academy-development-owner",
            name="Academy Development Owner",
            is_active=True,
        )
        approved = MessageTemplate.objects.create(
            tenant=owner,
            category=MessageTemplate.Category.SIGNUP,
            name="Existing approved student registration",
            body="#{학생이름}",
            solapi_template_id="existing-approved-registration",
            solapi_status="APPROVED",
            is_system=True,
        )
        existing_config = AutoSendConfig.objects.create(
            tenant=owner,
            trigger="registration_approved_student",
            template=approved,
            enabled=False,
            message_mode="sms",
        )

        with (
            override_settings(
                OWNER_TENANT_ID=owner.id,
                SOLAPI_KAKAO_PF_ID="development-mock-pfid",
            ),
            patch(
                "apps.core.management.commands.setup_ymath_realuse_scenario._is_persistent_development_runtime",
                return_value=True,
            ),
            patch.dict(os.environ, {"SOLAPI_MOCK": "true"}),
        ):
            ensure_development_messaging_baseline()
            ensure_development_messaging_baseline()

        existing_config.refresh_from_db()
        self.assertEqual(existing_config.template_id, approved.id)
        self.assertTrue(existing_config.enabled)
        self.assertEqual(existing_config.message_mode, "alimtalk")
        configs = AutoSendConfig.objects.filter(
            tenant=owner,
            trigger__in={
                "registration_approved_student",
                "registration_approved_parent",
                "password_reset_student",
                "password_reset_parent",
            },
        ).select_related("template")
        self.assertEqual(configs.count(), 4)
        for config in configs:
            self.assertTrue(config.enabled)
            self.assertEqual(config.message_mode, "alimtalk")
            self.assertEqual(config.template.solapi_status, "APPROVED")
            self.assertTrue(config.template.solapi_template_id)
        self.assertEqual(
            MessageTemplate.objects.filter(
                tenant=owner,
                solapi_template_id__startswith="development-mock-",
            ).count(),
            3,
        )

    def test_development_messaging_baseline_bootstraps_missing_exact_owner(self):
        self.assertFalse(Tenant.objects.exists())
        with (
            override_settings(
                OWNER_TENANT_ID=1,
                SOLAPI_KAKAO_PF_ID="development-mock-pfid",
            ),
            patch(
                "apps.core.management.commands.setup_ymath_realuse_scenario._is_persistent_development_runtime",
                return_value=True,
            ),
            patch.dict(os.environ, {"SOLAPI_MOCK": "true"}),
        ):
            ensure_development_messaging_baseline()

        owner = Tenant.objects.get(pk=1)
        self.assertEqual(owner.code, "academy-development-owner")
        self.assertEqual(owner.name, "Academy Development Owner")
        self.assertTrue(owner.is_active)
        self.assertEqual(AutoSendConfig.objects.filter(tenant=owner).count(), 4)

    def test_development_messaging_baseline_rejects_owner_identity_drift(self):
        owner = Tenant.objects.create(pk=1, code="unexpected-owner", name="Unexpected Owner")
        with (
            override_settings(
                OWNER_TENANT_ID=1,
                SOLAPI_KAKAO_PF_ID="development-mock-pfid",
            ),
            patch(
                "apps.core.management.commands.setup_ymath_realuse_scenario._is_persistent_development_runtime",
                return_value=True,
            ),
            patch.dict(os.environ, {"SOLAPI_MOCK": "true"}),
            self.assertRaisesMessage(CommandError, "identity is not exact"),
        ):
            ensure_development_messaging_baseline()

        owner.refresh_from_db()
        self.assertEqual(owner.code, "unexpected-owner")
        self.assertFalse(AutoSendConfig.objects.filter(tenant=owner).exists())

    def test_development_messaging_baseline_rejects_missing_owner_in_nonempty_database(self):
        other = Tenant.objects.create(pk=2, code="existing-development-tenant", name="Existing")
        with (
            override_settings(
                OWNER_TENANT_ID=1,
                SOLAPI_KAKAO_PF_ID="development-mock-pfid",
            ),
            patch(
                "apps.core.management.commands.setup_ymath_realuse_scenario._is_persistent_development_runtime",
                return_value=True,
            ),
            patch.dict(os.environ, {"SOLAPI_MOCK": "true"}),
            self.assertRaisesMessage(CommandError, "non-empty development database"),
        ):
            ensure_development_messaging_baseline()

        self.assertFalse(Tenant.objects.filter(pk=1).exists())
        self.assertTrue(Tenant.objects.filter(pk=other.pk, code=other.code).exists())
        self.assertFalse(AutoSendConfig.objects.exists())

    def test_explicit_long_video_fixture_creates_two_proctored_accesses_only(self):
        payload = json.loads(self._call_command(synthetic_long_video=True).splitlines()[-1])

        tenant = Tenant.objects.get(code="qa-ymath-realuse-20260805")
        video = Video.objects.get(tenant=tenant)
        accesses = VideoAccess.objects.filter(video=video).order_by("enrollment_id")

        self.assertEqual(video.status, Video.Status.READY)
        self.assertEqual(video.duration, 900)
        self.assertEqual(video.hls_path, "qa-fixtures/video-long/master.m3u8")
        self.assertEqual(video.session, tenant.lectures.order_by("id").first().sessions.order_by("order").first())
        self.assertEqual(accesses.count(), 2)
        self.assertEqual(set(accesses.values_list("access_mode", flat=True)), {AccessMode.PROCTORED_CLASS})
        self.assertEqual(set(accesses.values_list("rule", flat=True)), {"once"})
        self.assertTrue(all(accesses.values_list("is_override", flat=True)))
        self.assertEqual(
            payload["synthetic_long_video"],
            {
                "access_mode": AccessMode.PROCTORED_CLASS,
                "duration_seconds": 900,
                "hls_path": "qa-fixtures/video-long/master.m3u8",
                "video_accesses": 2,
                "video_id": video.id,
            },
        )
        self.assertEqual(
            payload["video_state"],
            {
                "active_playback_sessions": 0,
                "playback_events": 0,
                "playback_sessions": 0,
                "player_errors": 0,
                "proctored_video_accesses": 2,
                "video_accesses": 2,
                "video_progresses": 0,
                "videos": 1,
                "violated_events": 0,
            },
        )

    def test_long_video_fixture_requires_exactly_two_students_before_mutation(self):
        with self.assertRaisesMessage(
            CommandError,
            "--synthetic-long-video requires --student-count=2",
        ):
            self._call_command(synthetic_long_video=True, student_count=1)

        self.assertFalse(Tenant.objects.filter(code="qa-ymath-realuse-20260805").exists())

    def test_destroy_reports_zero_video_residue_after_playback_rows(self):
        self._call_command(synthetic_long_video=True)
        tenant = Tenant.objects.get(code="qa-ymath-realuse-20260805")
        video = Video.objects.get(tenant=tenant)
        access = VideoAccess.objects.filter(video=video).select_related("enrollment").first()
        progress = VideoProgress.objects.create(
            video=video,
            enrollment=access.enrollment,
            progress=0.25,
            last_position=225,
        )
        playback = VideoPlaybackSession.objects.create(
            video=video,
            enrollment=access.enrollment,
            session_id="qa-long-video-session",
            device_id="qa-long-video-device",
            status=VideoPlaybackSession.Status.ACTIVE,
        )
        VideoPlaybackEvent.objects.create(
            video=video,
            enrollment=access.enrollment,
            session_id=playback.session_id,
            user_id=access.enrollment.student.user_id,
            event_type=VideoPlaybackEvent.EventType.PLAYER_ERROR,
            violated=True,
        )
        self.assertIsNotNone(progress.pk)

        out = StringIO()
        call_command(
            "setup_ymath_realuse_scenario",
            tenant_code=tenant.code,
            destroy=True,
            stdout=out,
        )
        payload = json.loads(out.getvalue().splitlines()[-1])

        self.assertEqual(payload["status"], "YMATH_REALUSE_SCENARIO_DESTROYED")
        self.assertEqual(
            payload["video_residue"],
            {
                "active_playback_sessions": 0,
                "playback_events": 0,
                "playback_sessions": 0,
                "player_errors": 0,
                "proctored_video_accesses": 0,
                "video_accesses": 0,
                "video_progresses": 0,
                "videos": 0,
                "violated_events": 0,
            },
        )

    def test_rejects_non_scenario_tenant_code(self):
        with self.assertRaisesMessage(CommandError, "tenant-code must match"):
            self._call_command(tenant_code="ymath")

    def test_rejects_invalid_scenario_suffix_before_mutation(self):
        for tenant_code in (
            "qa-ymath-realuse-",
            "qa-ymath-realuse-invalid_suffix",
            "qa-ymath-realuse-invalid.suffix",
        ):
            with self.subTest(tenant_code=tenant_code):
                with self.assertRaisesMessage(CommandError, "tenant-code must match"):
                    self._call_command(tenant_code=tenant_code)
                self.assertFalse(Tenant.objects.filter(code=tenant_code).exists())

    def test_login_uat_creates_secret_free_ten_by_ten_by_ten_manifest(self):
        out = StringIO()
        secret = "scenario-test-password"
        with patch.dict(
            os.environ,
            {"YMATH_REALUSE_SCENARIO_PASSWORD": secret},
        ):
            call_command(
                "setup_ymath_realuse_scenario",
                stdout=out,
                tenant_code="qa-ymath-realuse-login-uat",
                session_count=1,
                login_uat=True,
                reset=True,
            )

        payload = json.loads(out.getvalue().splitlines()[-1])
        manifest = payload["login_manifest"]
        accounts = manifest["accounts"]
        tenant = Tenant.objects.get(code="qa-ymath-realuse-login-uat")

        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["tenant_code"], tenant.code)
        self.assertEqual(manifest["account_count"], 30)
        self.assertEqual(len(accounts), 30)
        self.assertEqual(len({account["username"] for account in accounts}), 30)
        self.assertEqual(
            {role: sum(account["role"] == role for account in accounts) for role in ("student", "parent", "staff")},
            {"student": 10, "parent": 10, "staff": 10},
        )
        self.assertEqual(tenant.students.count(), 10)
        self.assertEqual(Parent.objects.filter(tenant=tenant).count(), 10)
        self.assertEqual(Staff.objects.filter(tenant=tenant).count(), 10)
        self.assertEqual(
            TenantMembership.objects.filter(tenant=tenant, role="student", is_active=True).count(),
            10,
        )
        self.assertEqual(
            TenantMembership.objects.filter(tenant=tenant, role="parent", is_active=True).count(),
            10,
        )
        self.assertEqual(
            TenantMembership.objects.filter(
                tenant=tenant,
                role="staff",
                is_active=True,
            ).count(),
            10,
        )
        self.assertEqual(
            TenantMembership.objects.filter(tenant=tenant, role="admin", is_active=True).count(),
            1,
        )
        self.assertNotIn(secret, out.getvalue())
        self.assertTrue(all(set(account) == {"role", "username", "landing_path"} for account in accounts))

        expected_users = {}
        for student in tenant.students.select_related("user"):
            expected_users[("student", user_display_username(student.user))] = student.user
        for manifest_parent in Parent.objects.filter(tenant=tenant).select_related("user"):
            expected_users[("parent", str(manifest_parent.phone))] = manifest_parent.user
        for manifest_staff in Staff.objects.filter(tenant=tenant).select_related("user"):
            expected_users[("staff", user_display_username(manifest_staff.user))] = manifest_staff.user

        class RequestStub:
            META = {}
            data = {}

            @staticmethod
            def get_host():
                return "api.hakwonplus.com"

        for account in accounts:
            serializer = TenantAwareTokenObtainPairSerializer(
                data={
                    "tenant_code": tenant.code,
                    "username": account["username"],
                    "password": secret,
                },
                context={"request": RequestStub()},
            )
            self.assertTrue(serializer.is_valid(), serializer.errors)
            access = AccessToken(serializer.validated_data["access"])
            expected_user = expected_users[(account["role"], account["username"])]
            self.assertEqual(str(access[api_settings.USER_ID_CLAIM]), str(expected_user.id))

        parent = Parent.objects.get(tenant=tenant, phone="01099000001")
        self.assertEqual(parent.students.count(), 1)
        self.assertTrue(parent.user.check_password(secret))
        self.assertFalse(parent.user.must_change_password)
        self.assertEqual(parent.user.token_version, 1)

        first_student = tenant.students.get(name="검증학생 01")
        self.assertTrue(first_student.user.check_password(secret))
        self.assertFalse(first_student.user.must_change_password)
        self.assertEqual(first_student.user.token_version, 1)

        first_staff = Staff.objects.get(tenant=tenant, name="로그인 검증 직원 01")
        self.assertTrue(first_staff.user.check_password(secret))
        self.assertFalse(first_staff.user.must_change_password)
        self.assertEqual(first_staff.user.token_version, 1)
        self.assertEqual(
            TenantMembership.objects.get(tenant=tenant, user=first_staff.user).role,
            "staff",
        )

        tenant_id = tenant.id
        user_ids = list(tenant.users.values_list("id", flat=True))
        cleanup_out = StringIO()
        with patch.dict(os.environ, {}, clear=True):
            call_command(
                "setup_ymath_realuse_scenario",
                stdout=cleanup_out,
                tenant_code=tenant.code,
                destroy=True,
            )
        cleanup_payload = json.loads(cleanup_out.getvalue().splitlines()[-1])
        self.assertEqual(cleanup_payload["remaining"], {"tenants": 0, "users": 0})
        self.assertEqual(cleanup_payload["deleted"]["parents"], 10)
        self.assertEqual(cleanup_payload["deleted"]["staffs"], 10)
        self.assertFalse(Tenant.objects.filter(id=tenant_id).exists())
        self.assertFalse(get_user_model().objects.filter(id__in=user_ids).exists())

    def test_login_uat_requires_reset_when_tenant_already_exists(self):
        Tenant.objects.create(code="qa-ymath-realuse-login-existing", name="existing")

        with self.assertRaisesMessage(CommandError, "--reset"):
            self._call_command(
                tenant_code="qa-ymath-realuse-login-existing",
                login_uat=True,
            )

        self.assertEqual(Tenant.objects.filter(code="qa-ymath-realuse-login-existing").count(), 1)

    def test_case_variant_tenant_blocks_setup_and_destroy_without_mutation(self):
        upper_code = "QA-YMATH-REALUSE-CASE-VARIANT"
        lower_code = upper_code.lower()
        existing = Tenant.objects.create(code=upper_code, name="preserve case variant")

        with self.assertRaisesMessage(CommandError, "case-variant"):
            self._call_command(
                tenant_code=lower_code,
                login_uat=True,
                reset=True,
            )
        self.assertTrue(Tenant.objects.filter(id=existing.id, code=upper_code).exists())
        self.assertFalse(Tenant.objects.filter(code=lower_code).exists())

        with self.assertRaisesMessage(CommandError, "case-variant"):
            with patch.dict(os.environ, {}, clear=True):
                call_command(
                    "setup_ymath_realuse_scenario",
                    tenant_code=lower_code,
                    destroy=True,
                )
        self.assertTrue(Tenant.objects.filter(id=existing.id, code=upper_code).exists())
        self.assertFalse(Tenant.objects.filter(code=lower_code).exists())

    def test_rejects_empty_or_reserved_teacher_username_before_mutation(self):
        for username in ("", "   ", "ymath-qa-student-01", "YMATH-QA-STAFF-10"):
            tenant_code = "qa-ymath-realuse-invalid-" + str(len(username))
            with self.subTest(username=username):
                with self.assertRaises(CommandError):
                    self._call_command(
                        tenant_code=tenant_code,
                        teacher_username=username,
                        login_uat=True,
                        reset=True,
                    )
                self.assertFalse(Tenant.objects.filter(code=tenant_code).exists())

    def test_rejects_teacher_collision_with_dynamic_parent_login_before_mutation(self):
        tenant_code = "qa-ymath-realuse-parent-teacher-collision"
        with self.assertRaisesMessage(CommandError, "generated parent login identifier"):
            self._call_command(
                tenant_code=tenant_code,
                teacher_username="01099000001",
                login_uat=True,
                reset=True,
            )
        self.assertFalse(Tenant.objects.filter(code=tenant_code).exists())

    def test_reused_user_password_uses_password_service_token_version(self):
        tenant = Tenant.objects.create(code="qa-ymath-realuse-reused-user", name="reuse")
        user = Command._ensure_user(
            tenant=tenant,
            login_username="ymath-qa-student-01",
            password="first-password",
            name="학생",
            is_staff=False,
        )
        first_version = user.token_version

        reused = Command._ensure_user(
            tenant=tenant,
            login_username="ymath-qa-student-01",
            password="second-password",
            name="학생",
            is_staff=False,
        )

        self.assertTrue(reused.check_password("second-password"))
        self.assertEqual(reused.token_version, first_version + 1)
        self.assertFalse(reused.must_change_password)

    def test_login_uat_reset_rolls_back_old_tenant_on_mid_build_error(self):
        tenant_code = "qa-ymath-realuse-login-rollback"
        self._call_command(tenant_code=tenant_code)
        original = Command._ensure_user

        def fail_during_staff_creation(**kwargs):
            if kwargs["login_username"] == "ymath-qa-staff-05":
                raise RuntimeError("forced mid-build failure")
            return original(**kwargs)

        with patch.object(Command, "_ensure_user", side_effect=fail_during_staff_creation):
            with self.assertRaisesMessage(RuntimeError, "forced mid-build failure"):
                self._call_command(
                    tenant_code=tenant_code,
                    login_uat=True,
                    reset=True,
                )

        tenant = Tenant.objects.get(code=tenant_code)
        self.assertEqual(tenant.students.count(), 2)
        self.assertEqual(Parent.objects.filter(tenant=tenant).count(), 0)
        self.assertEqual(Staff.objects.filter(tenant=tenant).count(), 0)

    def test_destroy_removes_exact_scenario_tenant_and_users_without_password(self):
        self._call_command()
        tenant = Tenant.objects.get(code="qa-ymath-realuse-20260805")
        tenant_id = tenant.id
        user_ids = list(tenant.users.values_list("id", flat=True))
        out = StringIO()

        with patch.dict(os.environ, {}, clear=True):
            call_command(
                "setup_ymath_realuse_scenario",
                stdout=out,
                tenant_code="qa-ymath-realuse-20260805",
                destroy=True,
            )

        self.assertIn("YMATH_REALUSE_SCENARIO_DESTROYED", out.getvalue())
        self.assertIn('"remaining": {"tenants": 0, "users": 0}', out.getvalue())
        self.assertFalse(Tenant.objects.filter(id=tenant_id).exists())
        self.assertFalse(get_user_model().objects.filter(id__in=user_ids).exists())

    def test_destroy_removes_only_owned_tokens_and_activity_audits_preserving_seal(self):
        tenant_code = "qa-ymath-realuse-owned-evidence"
        self._call_command(tenant_code=tenant_code)
        tenant = Tenant.objects.get(code=tenant_code)
        owned_users = list(tenant.users.order_by("id"))
        owned_token_ids = [RefreshToken.for_user(user)["jti"] for user in owned_users]

        other_tenant = Tenant.objects.create(code="other-evidence-tenant", name="Other")
        other_user = get_user_model().objects.create_user(
            username="other-evidence-user",
            password="other-password",
            tenant=other_tenant,
        )
        other_token_id = RefreshToken.for_user(other_user)["jti"]

        seal = OpsAuditLog.objects.create(
            actor_username="frontend-release-runner",
            action="development.qa.setup",
            target_tenant=tenant,
            payload={"tenant_code": tenant_code, "tenant_id": tenant.id, "owner_sha256": "a" * 64},
        )
        owned_activity_ids = [
            OpsAuditLog.objects.create(
                actor_user=user,
                actor_username=user.username,
                action=action,
                target_tenant=tenant,
                target_user=user,
                payload={"screen_id": "student.dashboard.home"},
            ).id
            for user, action in zip(
                owned_users[:2],
                ("student_activity.login", "student_activity.screen_view"),
                strict=True,
            )
        ]
        foreign_actor_audit = OpsAuditLog.objects.create(
            actor_user=other_user,
            actor_username=other_user.username,
            action="student_activity.screen_view",
            target_tenant=tenant,
            target_user=owned_users[0],
        )
        outside_window_audit = OpsAuditLog.objects.create(
            actor_user=owned_users[0],
            actor_username=owned_users[0].username,
            action="student_activity.screen_view",
            target_tenant=tenant,
            target_user=owned_users[0],
        )
        OpsAuditLog.objects.filter(id=outside_window_audit.id).update(
            created_at=seal.created_at - timedelta(seconds=1)
        )

        out = StringIO()
        with patch.dict(os.environ, {}, clear=True):
            call_command(
                "setup_ymath_realuse_scenario",
                stdout=out,
                tenant_code=tenant_code,
                destroy=True,
            )
        payload = json.loads(out.getvalue().splitlines()[-1])

        self.assertFalse(OutstandingToken.objects.filter(jti__in=owned_token_ids).exists())
        self.assertTrue(OutstandingToken.objects.filter(jti=other_token_id).exists())
        self.assertFalse(OpsAuditLog.objects.filter(id__in=owned_activity_ids).exists())
        self.assertTrue(OpsAuditLog.objects.filter(id=foreign_actor_audit.id).exists())
        self.assertTrue(OpsAuditLog.objects.filter(id=outside_window_audit.id).exists())
        self.assertTrue(OpsAuditLog.objects.filter(id=seal.id, action="development.qa.setup").exists())
        self.assertTrue(
            OpsAuditLog.objects.filter(
                action="development.qa.scenario",
                target_tenant__isnull=True,
                payload__tenant_code=tenant_code,
            ).exists()
        )
        self.assertEqual(
            payload["residue"],
            {"activity_audits": 0, "outstanding_tokens": 0},
        )

    def test_destroy_fails_closed_when_activity_has_no_setup_ownership_seal(self):
        tenant_code = "qa-ymath-realuse-unsealed-evidence"
        self._call_command(tenant_code=tenant_code)
        tenant = Tenant.objects.get(code=tenant_code)
        user = tenant.users.order_by("id").first()
        token_id = RefreshToken.for_user(user)["jti"]
        OpsAuditLog.objects.filter(
            action="development.qa.scenario",
            target_tenant=tenant,
        ).delete()
        activity = OpsAuditLog.objects.create(
            actor_user=user,
            actor_username=user.username,
            action="student_activity.screen_view",
            target_tenant=tenant,
            target_user=user,
        )

        with patch.dict(os.environ, {}, clear=True), self.assertRaisesMessage(
            CommandError,
            "exact QA ownership provenance seal",
        ):
            call_command(
                "setup_ymath_realuse_scenario",
                tenant_code=tenant_code,
                destroy=True,
            )

        self.assertTrue(Tenant.objects.filter(id=tenant.id).exists())
        self.assertTrue(OutstandingToken.objects.filter(jti=token_id).exists())
        self.assertTrue(OpsAuditLog.objects.filter(id=activity.id).exists())

    def test_non_database_residue_uses_only_exact_prefix_process_and_listener_boundaries(self):
        tenant_id = 712
        tenant_code = "qa-ymath-realuse-residue"
        exact_prefixes = (
            f"tenants/{tenant_id}/",
            f"excel/{tenant_id}/",
            f"tenant-logos/{tenant_id}/",
            f"landing-public/reviews/{tenant_id}/",
            f"matchup-showcase-snapshots/tenant_{tenant_id}/",
        )
        requests = []
        client = Mock()

        def list_objects_v2(**kwargs):
            requests.append(kwargs)
            if kwargs["Prefix"] == exact_prefixes[0] and "ContinuationToken" not in kwargs:
                return {"Contents": [{"Key": "one"}], "IsTruncated": True, "NextContinuationToken": "next"}
            if kwargs.get("ContinuationToken") == "next":
                return {"Contents": [{"Key": "two"}], "IsTruncated": False}
            return {"Contents": [], "IsTruncated": False}

        client.list_objects_v2.side_effect = list_objects_v2
        owned_pid = str(os.getpid() + 10_000)
        foreign_pid = str(os.getpid() + 10_001)

        class FakePath:
            def __init__(self, value):
                self.value = str(value)

            @property
            def name(self):
                return self.value.rsplit("/", 1)[-1]

            def __truediv__(self, child):
                return FakePath(f"{self.value}/{child}")

            def iterdir(self):
                self._assert_value("/proc")
                return [FakePath(f"/proc/{owned_pid}"), FakePath(f"/proc/{foreign_pid}")]

            def read_bytes(self):
                if self.value == f"/proc/{owned_pid}/environ":
                    return f"QA_TENANT={tenant_code}\0OTHER=value".encode()
                if self.value == f"/proc/{foreign_pid}/environ":
                    return b"QA_TENANT=foreign\0"
                raise AssertionError(f"Unexpected bytes path: {self.value}")

            def read_text(self):
                if self.value == "/proc/net/tcp":
                    return "header\n0: 0100007F:4650 00000000:0000 0A"
                if self.value == "/proc/net/tcp6":
                    return "header"
                raise AssertionError(f"Unexpected text path: {self.value}")

            def _assert_value(self, expected):
                if self.value != expected:
                    raise AssertionError(f"Expected {expected}, got {self.value}")

        with patch("boto3.client", return_value=client), patch(
            "apps.core.management.commands.setup_ymath_realuse_scenario.Path",
            FakePath,
        ):
            residue = Command._non_database_residue(tenant_id=tenant_id, tenant_code=tenant_code)
            no_tenant_residue = Command._non_database_residue(tenant_id=None, tenant_code=tenant_code)

        self.assertEqual(residue, {"listeners": 1, "processes": 1, "r2_objects": 2})
        self.assertEqual(no_tenant_residue, {"listeners": 1, "processes": 1, "r2_objects": 0})
        self.assertEqual([request["Prefix"] for request in requests], [exact_prefixes[0], *exact_prefixes])
        self.assertEqual(requests[1]["ContinuationToken"], "next")
        self.assertTrue(all(request["Bucket"] == "test-storage" for request in requests))

    def test_destroy_is_idempotent_when_scenario_is_absent(self):
        out = StringIO()

        with patch.dict(os.environ, {}, clear=True):
            call_command(
                "setup_ymath_realuse_scenario",
                stdout=out,
                tenant_code="qa-ymath-realuse-20260805",
                destroy=True,
            )

        self.assertIn("YMATH_REALUSE_SCENARIO_ABSENT", out.getvalue())
        self.assertIn('"remaining": {"tenants": 0, "users": 0}', out.getvalue())

    def test_destroy_fails_closed_if_same_code_is_recreated_before_readback(self):
        tenant_code = "qa-ymath-realuse-destroy-race"
        self._call_command(tenant_code=tenant_code)
        original = Command._remaining_for_code

        def recreate_and_readback(code):
            Tenant.objects.create(code=code, name="raced replacement")
            return original(code)

        with patch.object(Command, "_remaining_for_code", side_effect=recreate_and_readback):
            with self.assertRaisesMessage(CommandError, "same-code"):
                with patch.dict(os.environ, {}, clear=True):
                    call_command(
                        "setup_ymath_realuse_scenario",
                        tenant_code=tenant_code,
                        destroy=True,
                    )

        self.assertEqual(Tenant.objects.filter(code=tenant_code).count(), 1)

    @override_settings(
        DATABASES={"default": {"NAME": "academy_api", "ENGINE": "django.db.backends.postgresql"}},
        R2_AI_BUCKET="academy-ai",
        R2_STORAGE_BUCKET="academy-storage",
        R2_EXCEL_BUCKET="academy-excel",
        R2_ADMIN_BUCKET="academy-admin",
    )
    def test_rejects_production_shaped_runtime(self):
        with self.assertRaisesMessage(CommandError, "isolated development"):
            self._call_command(login_uat=True)


class SetupYmathRealuseScenarioPostgresLockTests(TransactionTestCase):
    reset_sequences = True

    def test_development_messaging_baseline_bootstraps_exact_owner_and_sequence(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL development-owner sequence regression")

        self.assertFalse(Tenant.objects.exists())
        with (
            override_settings(
                OWNER_TENANT_ID=1,
                SOLAPI_KAKAO_PF_ID="development-mock-pfid",
            ),
            patch(
                "apps.core.management.commands.setup_ymath_realuse_scenario._is_persistent_development_runtime",
                return_value=True,
            ),
            patch.dict(os.environ, {"SOLAPI_MOCK": "true"}),
            transaction.atomic(),
        ):
            ensure_development_messaging_baseline()
            ensure_development_messaging_baseline()

        owner = Tenant.objects.get(pk=1)
        self.assertEqual(owner.code, "academy-development-owner")
        self.assertEqual(owner.name, "Academy Development Owner")
        self.assertTrue(owner.is_active)
        self.assertEqual(AutoSendConfig.objects.filter(tenant=owner).count(), 4)

        later = Tenant.objects.create(code="after-development-owner", name="Later tenant")
        self.assertGreater(later.pk, owner.pk)

    @staticmethod
    def _set_application_name(name):
        with connection.cursor() as cursor:
            cursor.execute("SET application_name = %s", [name])

    def _wait_for_advisory_lock(self, application_name, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT wait_event_type, wait_event
                    FROM pg_stat_activity
                    WHERE application_name = %s
                    """,
                    [application_name],
                )
                rows = cursor.fetchall()
            if any(event_type == "Lock" and event == "advisory" for event_type, event in rows):
                return
            time.sleep(0.05)
        self.fail(f"{application_name} did not enter a PostgreSQL advisory lock wait")

    @staticmethod
    def _call_full_command(*, tenant_code, stdout, destroy=False):
        kwargs = {
            "tenant_code": tenant_code,
            "stdout": stdout,
        }
        if destroy:
            kwargs["destroy"] = True
        else:
            kwargs.update({"login_uat": True, "session_count": 1})
        call_command("setup_ymath_realuse_scenario", **kwargs)

    def test_full_login_uat_commands_serialize_absent_setup_and_require_reset(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL advisory-lock regression")

        tenant_code = "qa-ymath-realuse-concurrent-full-setup"
        first_locked = threading.Event()
        second_started = threading.Event()
        allow_first_commit = threading.Event()
        errors: list[BaseException] = []
        outcomes = {}
        original_lock = Command._lock_tenant_code

        def coordinated_lock(code):
            original_lock(code)
            if threading.current_thread().name == "ymath-first-setup":
                first_locked.set()
                if not allow_first_commit.wait(30):
                    raise TimeoutError("first setup command was not released")

        def worker(name, application_name):
            close_old_connections()
            try:
                self._set_application_name(application_name)
                if name == "second":
                    second_started.set()
                out = StringIO()
                try:
                    self._call_full_command(tenant_code=tenant_code, stdout=out)
                    outcomes[name] = ("success", out.getvalue())
                except CommandError as error:
                    outcomes[name] = ("command_error", str(error))
                except BaseException as error:  # pragma: no cover - surfaced below
                    errors.append(error)
            finally:
                close_old_connections()

        first = threading.Thread(
            target=worker,
            args=("first", "ymath-uat-full-setup-first"),
            name="ymath-first-setup",
        )
        second = threading.Thread(
            target=worker,
            args=("second", "ymath-uat-full-setup-second"),
            name="ymath-second-setup",
        )
        with patch.dict(os.environ, {"YMATH_REALUSE_SCENARIO_PASSWORD": "pg-command-password"}):
            with patch.object(Command, "_lock_tenant_code", new=staticmethod(coordinated_lock)):
                first.start()
                self.assertTrue(first_locked.wait(10))
                second.start()
                self.assertTrue(second_started.wait(10))
                try:
                    self._wait_for_advisory_lock("ymath-uat-full-setup-second")
                finally:
                    allow_first_commit.set()
                first.join(120)
                second.join(120)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(outcomes["first"][0], "success")
        self.assertIn("YMATH_REALUSE_SCENARIO_READY", outcomes["first"][1])
        self.assertEqual(outcomes["second"][0], "command_error")
        self.assertIn("--reset", outcomes["second"][1])

        tenant = Tenant.objects.get(code=tenant_code)
        self.assertEqual(tenant.students.count(), 10)
        self.assertEqual(Parent.objects.filter(tenant=tenant).count(), 10)
        self.assertEqual(Staff.objects.filter(tenant=tenant).count(), 10)
        self.assertEqual(
            {
                role: TenantMembership.objects.filter(
                    tenant=tenant,
                    role=role,
                    is_active=True,
                ).count()
                for role in ("student", "parent", "staff", "admin")
            },
            {"student": 10, "parent": 10, "staff": 10, "admin": 1},
        )

    def test_full_setup_then_destroy_waits_on_advisory_lock_and_reads_exact_zero(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL advisory-lock regression")

        tenant_code = "qa-ymath-realuse-concurrent-full-destroy"
        setup_locked = threading.Event()
        destroy_started = threading.Event()
        allow_setup_commit = threading.Event()
        errors: list[BaseException] = []
        outputs = {}
        original_lock = Command._lock_tenant_code

        def coordinated_lock(code):
            original_lock(code)
            if threading.current_thread().name == "ymath-inflight-setup":
                setup_locked.set()
                if not allow_setup_commit.wait(30):
                    raise TimeoutError("in-flight setup command was not released")

        def setup_worker():
            close_old_connections()
            try:
                self._set_application_name("ymath-uat-inflight-setup")
                out = StringIO()
                self._call_full_command(tenant_code=tenant_code, stdout=out)
                outputs["setup"] = out.getvalue()
            except BaseException as error:  # pragma: no cover - surfaced below
                errors.append(error)
            finally:
                close_old_connections()

        def destroy_worker():
            close_old_connections()
            try:
                self._set_application_name("ymath-uat-inflight-destroy")
                destroy_started.set()
                out = StringIO()
                self._call_full_command(tenant_code=tenant_code, stdout=out, destroy=True)
                outputs["destroy"] = out.getvalue()
            except BaseException as error:  # pragma: no cover - surfaced below
                errors.append(error)
            finally:
                close_old_connections()

        setup = threading.Thread(target=setup_worker, name="ymath-inflight-setup")
        destroy = threading.Thread(target=destroy_worker, name="ymath-inflight-destroy")
        with patch.dict(os.environ, {"YMATH_REALUSE_SCENARIO_PASSWORD": "pg-command-password"}):
            with patch.object(Command, "_lock_tenant_code", new=staticmethod(coordinated_lock)):
                setup.start()
                self.assertTrue(setup_locked.wait(10))
                destroy.start()
                self.assertTrue(destroy_started.wait(10))
                try:
                    self._wait_for_advisory_lock("ymath-uat-inflight-destroy")
                finally:
                    allow_setup_commit.set()
                setup.join(120)
                destroy.join(120)

        self.assertFalse(setup.is_alive())
        self.assertFalse(destroy.is_alive())
        self.assertEqual(errors, [])
        self.assertIn("YMATH_REALUSE_SCENARIO_READY", outputs["setup"])
        cleanup = json.loads(outputs["destroy"].splitlines()[-1])
        self.assertEqual(cleanup["status"], "YMATH_REALUSE_SCENARIO_DESTROYED")
        self.assertEqual(cleanup["remaining"], {"tenants": 0, "users": 0})
        self.assertFalse(Tenant.objects.filter(code__iexact=tenant_code).exists())
        self.assertFalse(get_user_model().objects.filter(tenant__code__iexact=tenant_code).exists())
