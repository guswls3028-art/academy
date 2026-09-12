import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier, Event
from unittest import skipUnless
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.db import close_old_connections, connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.core.models import Tenant, TenantMembership, User
from apps.domains.enrollment.test_support import (
    create_enrollment_fixture,
    create_session_enrollment_fixture,
    get_enrollment_fixture,
)
from apps.domains.lectures.test_support import (
    create_lecture_fixture,
    create_session_fixture,
)
from apps.domains.student_app.media.views import StudentVideoPlaybackView
from apps.domains.students.test_support import create_student_fixture
from apps.domains.video.drm import verify_playback_token
from apps.domains.video.models import (
    AccessMode,
    InactiveVideoEntitlement,
    Video,
    VideoPlaybackSession,
    VideoProgress,
)
from apps.domains.video.services.inactive_entitlements import (
    InactiveVideoEntitlementError,
    get_active_inactive_video_entitlement,
    grant_inactive_video_entitlement,
    revoke_inactive_video_entitlement,
    update_inactive_entitled_video_progress,
)
from apps.domains.video.services.skip_budget import consume_video_forward_skip
from apps.support.student_app.video_media import issue_playback_access_grant


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ActivePlaybackCredentialTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            code="active-video-credential",
            name="Active Video Credential",
            is_active=True,
        )
        student_user = User.objects.create_user(
            username="active-video-credential-student",
            password="testpass123",
            tenant=self.tenant,
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=student_user,
            role="student",
        )
        self.student = create_student_fixture(
            tenant=self.tenant,
            user=student_user,
            name="Active Video Student",
            ps_number="AVE-001",
            omr_code="12345678",
            parent_phone="01012345678",
            school_type="MIDDLE",
        )
        lecture = create_lecture_fixture(
            tenant=self.tenant,
            title="Active Video Lecture",
            name="Active Video Lecture",
            subject="SCIENCE",
        )
        self.enrollment = create_enrollment_fixture(
            tenant=self.tenant,
            student=self.student,
            lecture=lecture,
            status="ACTIVE",
        )
        session = create_session_fixture(
            lecture=lecture,
            title="Long Session",
            order=1,
        )
        create_session_enrollment_fixture(
            tenant=self.tenant,
            enrollment=self.enrollment,
            session=session,
        )
        self.video = Video.objects.create(
            tenant=self.tenant,
            session=session,
            title="Long Active Video",
            status=Video.Status.READY,
            duration=8_516,
        )

    @override_settings(VIDEO_PLAYBACK_TTL_SECONDS=600)
    @patch("apps.domains.video.services.playback_session.init_session_redis")
    def test_long_video_keeps_session_credentials_at_heartbeat_ttl(
        self,
        _init_session_redis,
    ):
        issued_at = timezone.now()
        with patch(
            "apps.domains.video.services.access_resolver.get_effective_access_mode",
            return_value=AccessMode.PROCTORED_CLASS,
        ):
            grant = issue_playback_access_grant(
                video=self.video,
                enrollment=self.enrollment,
                user=self.student.user,
                device_id="active-long-video-device",
            )

        self.assertIsNone(grant.error)
        self.assertEqual(grant.access_mode, AccessMode.PROCTORED_CLASS.value)
        credential_ttl = int(grant.expires_at or 0) - int(issued_at.timestamp())
        self.assertGreaterEqual(credential_ttl, 598)
        self.assertLessEqual(credential_ttl, 602)
        token_ok, token_payload, token_error = verify_playback_token(grant.token or "")
        self.assertTrue(token_ok, token_error)
        self.assertEqual(token_payload["exp"], grant.expires_at)
        playback_session = VideoPlaybackSession.objects.get(
            session_id=grant.session_id,
        )
        self.assertLessEqual(
            (playback_session.expires_at - issued_at).total_seconds(),
            602,
        )
        _init_session_redis.assert_called_once_with(
            session_id=grant.session_id,
            ttl_seconds=600,
        )


@skipUnless(connection.vendor == "postgresql", "PostgreSQL row-lock contract")
@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class InactiveVideoEntitlementConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.tenant = Tenant.objects.create(
            code="inactive-video-entitlement-race",
            name="Inactive Video Entitlement Race",
            is_active=True,
        )
        self.staff = User.objects.create_user(
            username="inactive-video-entitlement-staff",
            password="testpass123",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=self.staff,
            role="admin",
        )
        student_user = User.objects.create_user(
            username="inactive-video-entitlement-student",
            password="testpass123",
            tenant=self.tenant,
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=student_user,
            role="student",
        )
        self.student = create_student_fixture(
            tenant=self.tenant,
            user=student_user,
            name="Entitlement Student",
            ps_number="IVE-001",
            omr_code="87654321",
            parent_phone="01012345678",
            school_type="HIGH",
        )
        lecture = create_lecture_fixture(
            tenant=self.tenant,
            title="Entitlement Lecture",
            name="Entitlement Lecture",
            subject="MATH",
        )
        self.enrollment = create_enrollment_fixture(
            tenant=self.tenant,
            student=self.student,
            lecture=lecture,
            status="INACTIVE",
        )
        session = create_session_fixture(
            lecture=lecture,
            title="Session 1",
            order=1,
        )
        create_session_enrollment_fixture(
            tenant=self.tenant,
            enrollment=self.enrollment,
            session=session,
        )
        self.video = Video.objects.create(
            tenant=self.tenant,
            session=session,
            title="Session 1 Video",
            status=Video.Status.READY,
            duration=100,
        )

    def _grant(self, *, barrier=None):
        close_old_connections()
        tenant = Tenant.objects.get(id=self.tenant.id)
        actor = User.objects.get(id=self.staff.id)
        if barrier is not None:
            barrier.wait(timeout=10)
        result = grant_inactive_video_entitlement(
            tenant=tenant,
            student_id=self.student.id,
            enrollment_id=self.enrollment.id,
            video_id=self.video.id,
            access_mode=AccessMode.FREE_REVIEW,
            source=InactiveVideoEntitlement.Source.STAFF_AUTHORIZATION,
            source_reference="test:postgres-explicit-authorization",
            reason="Exact first-session video access",
            actor=actor,
            actor_reference=f"user:{actor.id}",
            expires_at=self.expires_at,
        )
        close_old_connections()
        return result

    def test_grant_locks_video_without_nullable_session_outer_join(self):
        self.assertTrue(Video._meta.get_field("session").null)
        self.expires_at = timezone.now() + timedelta(days=7)

        result = self._grant()

        self.assertTrue(result.created)
        self.assertEqual(result.entitlement.video_id, self.video.id)

    def test_concurrent_grant_and_playback_follow_canonical_lock_order(self):
        self.expires_at = timezone.now() + timedelta(days=7)
        barrier = Barrier(2)

        def grant():
            return self._grant(barrier=barrier).created

        def playback():
            close_old_connections()
            video = Video.objects.get(id=self.video.id)
            enrollment = get_enrollment_fixture(id=self.enrollment.id)
            user = User.objects.get(id=self.student.user_id)
            barrier.wait(timeout=10)
            result = issue_playback_access_grant(
                video=video,
                enrollment=enrollment,
                user=user,
                device_id="postgres-lock-order",
            )
            close_old_connections()
            return result.error, bool(result.token)

        with ThreadPoolExecutor(max_workers=2) as pool:
            grant_future = pool.submit(grant)
            playback_future = pool.submit(playback)
            self.assertTrue(grant_future.result(timeout=20))
            playback_error, playback_token = playback_future.result(timeout=20)

        self.assertIn(playback_error, (None, "access_blocked"))
        self.assertIsInstance(playback_token, bool)
        self.assertEqual(
            InactiveVideoEntitlement.objects.filter(revoked_at__isnull=True).count(),
            1,
        )

    @override_settings(
        CDN_HLS_BASE_URL="https://cdn.example.test",
        CDN_HLS_SIGNING_SECRET="postgres-entitlement-race-secret",
        VIDEO_PLAYBACK_TTL_SECONDS=600,
    )
    def test_playback_rebuilds_urls_from_locked_replacement_expiry(self):
        self.expires_at = timezone.now() + timedelta(minutes=10)
        self._grant()
        url_built = Event()
        replacement_committed = Event()
        actual_issue = issue_playback_access_grant

        def delayed_locked_grant(**kwargs):
            url_built.set()
            if not replacement_committed.wait(timeout=15):
                raise AssertionError("replacement entitlement did not commit")
            return actual_issue(**kwargs)

        def playback():
            close_old_connections()
            tenant = Tenant.objects.get(id=self.tenant.id)
            user = User.objects.get(id=self.student.user_id)
            request = APIRequestFactory().post(
                f"/api/v1/student/video/videos/{self.video.id}/playback/"
                f"?enrollment={self.enrollment.id}"
            )
            request.tenant = tenant
            force_authenticate(request, user=user)
            with patch(
                "apps.domains.student_app.media.views.issue_playback_access_grant",
                side_effect=delayed_locked_grant,
            ):
                response = StudentVideoPlaybackView.as_view()(
                    request,
                    video_id=self.video.id,
                )
            close_old_connections()
            return response.status_code, dict(response.data)

        def replace_with_shorter_entitlement():
            if not url_built.wait(timeout=15):
                raise AssertionError("playback did not build its initial URL")
            try:
                self.expires_at = timezone.now() + timedelta(seconds=60)
                result = self._grant()
                return int(result.entitlement.expires_at.timestamp())
            finally:
                replacement_committed.set()

        with ThreadPoolExecutor(max_workers=2) as pool:
            playback_future = pool.submit(playback)
            replacement_future = pool.submit(replace_with_shorter_entitlement)
            replacement_expiry = replacement_future.result(timeout=20)
            status_code, payload = playback_future.result(timeout=20)

        self.assertEqual(status_code, 200, payload)
        locked_grant_expiry = int(payload["playback_expires_at"])
        self.assertLessEqual(locked_grant_expiry, replacement_expiry)
        hls_expiry = int(parse_qs(urlparse(payload["play_url"]).query)["exp"][0])
        thumbnail_expiry = int(
            parse_qs(urlparse(payload["video"]["thumbnail_url"]).query)["exp"][0]
        )
        self.assertLessEqual(hls_expiry, locked_grant_expiry)
        self.assertLessEqual(thumbnail_expiry, locked_grant_expiry)

    def test_concurrent_identical_grants_create_one_current_entitlement(self):
        barrier = Barrier(2)
        self.expires_at = timezone.now() + timedelta(days=7)

        def grant():
            result = self._grant(barrier=barrier)
            return result.created, result.changed

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _index: grant(), range(2)))

        self.assertEqual(
            InactiveVideoEntitlement.objects.filter(revoked_at__isnull=True).count(),
            1,
        )
        self.assertEqual(sorted(results), [(False, False), (True, True)])

    def _revoke_after_validation(self, *, validated: Barrier, revoked: Barrier):
        close_old_connections()
        tenant = Tenant.objects.get(id=self.tenant.id)
        actor = User.objects.get(id=self.staff.id)
        entitlement = InactiveVideoEntitlement.objects.get(revoked_at__isnull=True)
        validated.wait(timeout=10)
        result = revoke_inactive_video_entitlement(
            tenant=tenant,
            entitlement_id=entitlement.id,
            reason="Concurrent explicit revoke",
            actor=actor,
            actor_reference=f"user:{actor.id}",
        )
        revoked.wait(timeout=10)
        close_old_connections()
        return result.changed

    def test_progress_write_revalidates_after_concurrent_revoke(self):
        self.expires_at = timezone.now() + timedelta(days=7)
        self._grant()
        validated = Barrier(2)
        revoked = Barrier(2)

        def stale_progress_write():
            close_old_connections()
            video = Video.objects.get(id=self.video.id)
            enrollment = get_enrollment_fixture(id=self.enrollment.id)
            self.assertIsNotNone(
                get_active_inactive_video_entitlement(
                    video=video,
                    enrollment=enrollment,
                )
            )
            expected_policy_version = int(video.policy_version or 1)
            validated.wait(timeout=10)
            revoked.wait(timeout=10)
            try:
                update_inactive_entitled_video_progress(
                    tenant_id=self.tenant.id,
                    enrollment_id=enrollment.id,
                    video_id=video.id,
                    expected_policy_version=expected_policy_version,
                    defaults={"progress": 0.5},
                )
            except InactiveVideoEntitlementError as exc:
                close_old_connections()
                return exc.code
            close_old_connections()
            return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            write_future = pool.submit(stale_progress_write)
            revoke_future = pool.submit(
                self._revoke_after_validation,
                validated=validated,
                revoked=revoked,
            )
            self.assertEqual(
                write_future.result(timeout=20),
                "inactive_entitlement_changed",
            )
            self.assertTrue(revoke_future.result(timeout=20))

        self.assertFalse(VideoProgress.objects.exists())

    def test_skip_write_revalidates_after_concurrent_revoke(self):
        self.expires_at = timezone.now() + timedelta(days=7)
        self._grant()
        validated = Barrier(2)
        revoked = Barrier(2)

        def stale_skip_write():
            close_old_connections()
            video = Video.objects.get(id=self.video.id)
            enrollment = get_enrollment_fixture(id=self.enrollment.id)
            self.assertIsNotNone(
                get_active_inactive_video_entitlement(
                    video=video,
                    enrollment=enrollment,
                )
            )
            expected_policy_version = int(video.policy_version or 1)
            validated.wait(timeout=10)
            revoked.wait(timeout=10)
            try:
                consume_video_forward_skip(
                    video=video,
                    enrollment=enrollment,
                    require_inactive_entitlement=True,
                    expected_policy_version=expected_policy_version,
                )
            except InactiveVideoEntitlementError as exc:
                close_old_connections()
                return exc.code
            close_old_connections()
            return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            write_future = pool.submit(stale_skip_write)
            revoke_future = pool.submit(
                self._revoke_after_validation,
                validated=validated,
                revoked=revoked,
            )
            self.assertEqual(
                write_future.result(timeout=20),
                "inactive_entitlement_changed",
            )
            self.assertTrue(revoke_future.result(timeout=20))

        self.assertFalse(VideoProgress.objects.exists())


@skipUnless(connection.vendor == "postgresql", "PostgreSQL migration contract")
class InactiveVideoEntitlementMigrationCycleTests(TransactionTestCase):
    migrate_from = ("video", "0020_videoprogress_forward_skip_seconds_used")
    migrate_to = ("video", "0021_inactivevideoentitlement")
    table_name = "video_inactivevideoentitlement"

    def _migrate(self, target):
        executor = MigrationExecutor(connection)
        executor.migrate([target])

    def _table_names(self):
        with connection.cursor() as cursor:
            return set(connection.introspection.table_names(cursor))

    def _restore_current_schema(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes("video"))

    def test_apply_rollback_apply_preserves_current_unique_constraint(self):
        self.addCleanup(self._restore_current_schema)
        self._migrate(self.migrate_from)
        self.assertNotIn(self.table_name, self._table_names())

        self._migrate(self.migrate_to)
        self.assertIn(self.table_name, self._table_names())
        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(
                cursor,
                self.table_name,
            )
        self.assertIn("uniq_current_inactive_video_entitlement", constraints)
        self.assertIn("inactive_video_entitlement_source_valid", constraints)

        self._migrate(self.migrate_from)
        self.assertNotIn(self.table_name, self._table_names())

        self._migrate(self.migrate_to)
        self.assertIn(self.table_name, self._table_names())


def test_inactive_entitlement_openapi_declares_filters_and_error_contracts():
    schema = json.loads(
        (Path(__file__).resolve().parents[4] / "schema" / "openapi.json").read_text(
            encoding="utf-8"
        )
    )
    collection = schema["paths"]["/api/v1/media/inactive-video-entitlements/"]
    parameter_names = {
        parameter["name"]
        for parameter in collection["get"]["parameters"]
    }
    assert {"student_id", "enrollment_id", "video_id"} <= parameter_names

    operations = (
        collection["get"],
        collection["post"],
        schema["paths"][
            "/api/v1/media/inactive-video-entitlements/{id}/"
        ]["get"],
        schema["paths"][
            "/api/v1/media/inactive-video-entitlements/{id}/revoke/"
        ]["post"],
    )
    for operation in operations:
        for response_code in ("400", "403", "404"):
            response_schema = operation["responses"][response_code]["content"][
                "application/json"
            ]["schema"]
            assert response_schema["$ref"].endswith(
                "/InactiveVideoEntitlementError"
            )
