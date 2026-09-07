import json
from datetime import timedelta
from pathlib import Path
from unittest import skipUnless

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.core.models import OpsAuditLog, Tenant, TenantMembership, User
from apps.domains.attendance.models import Attendance
from apps.domains.enrollment.models import Enrollment, SessionEnrollment
from apps.domains.enrollment.test_support import create_enrollment_fixture
from apps.domains.lectures.test_support import (
    create_lecture_fixture,
    create_session_fixture,
)
from apps.domains.messaging.models import NotificationLog, ScheduledNotification
from apps.domains.parents.models import Parent
from apps.domains.student_app.media.views import (
    StudentSessionVideoListView,
    StudentVideoMeView,
    StudentVideoPlaybackView,
)
from apps.domains.students.test_support import create_student_fixture
from apps.domains.video.models import (
    AccessMode,
    DirectVideoEntitlement,
    Video,
    VideoAccess,
    VideoPlaybackEvent,
    VideoPlaybackSession,
    VideoProgress,
)
from apps.domains.video.drm import create_playback_token, verify_playback_token
from apps.domains.video.services.direct_entitlements import (
    DirectVideoEntitlementError,
    get_active_direct_video_entitlement,
    grant_direct_video_entitlement,
    revoke_direct_video_entitlement,
)
from apps.domains.video.views.direct_entitlement_views import (
    DirectVideoEntitlementViewSet,
)
from apps.domains.video.views.playback_views import (
    PlaybackEventBatchView,
    PlaybackHeartbeatView,
    PlaybackRenewView,
    PlaybackRefreshView,
)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class DirectVideoEntitlementTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.tenant = Tenant.objects.create(
            code="direct-video-tenant",
            name="Direct Video Tenant",
            is_active=True,
        )
        self.other_tenant = Tenant.objects.create(
            code="direct-video-other",
            name="Direct Video Other",
            is_active=True,
        )
        self.staff = self._user(
            tenant=self.tenant,
            username="direct-video-staff",
            role="admin",
            is_staff=True,
        )
        self.student_user = self._user(
            tenant=self.tenant,
            username="direct-video-student",
            role="student",
        )
        self.student = create_student_fixture(
            tenant=self.tenant,
            user=self.student_user,
            name="Direct Video Student",
            ps_number="DVE-001",
            omr_code="12345678",
            parent_phone="01011112222",
            school_type="HIGH",
        )
        self.lecture = create_lecture_fixture(
            tenant=self.tenant,
            title="Direct Video Lecture",
            name="Direct Video Lecture",
            subject="MATH",
        )
        self.session = create_session_fixture(
            lecture=self.lecture,
            title="Session 1",
            order=1,
        )
        self.video = Video.objects.create(
            tenant=self.tenant,
            session=self.session,
            title="Exact Direct Video",
            status=Video.Status.READY,
            visibility=Video.Visibility.ENROLLED,
            duration=120,
        )
        self.sibling_video = Video.objects.create(
            tenant=self.tenant,
            session=self.session,
            title="Sibling Video",
            status=Video.Status.READY,
            visibility=Video.Visibility.ENROLLED,
            duration=120,
            order=2,
        )

    def _user(self, *, tenant, username, role, is_staff=False):
        user = User.objects.create_user(
            username=username,
            password="testpass123",
            tenant=tenant,
            is_staff=is_staff,
        )
        TenantMembership.ensure_active(tenant=tenant, user=user, role=role)
        return user

    def _grant(self, **overrides):
        values = {
            "tenant": self.tenant,
            "student_id": self.student.id,
            "video_id": self.video.id,
            "reason": "Teacher approved this exact video without enrollment",
            "actor": self.staff,
            "actor_reference": f"user:{self.staff.id}",
            "source_reference": "admin-video-permission",
            "confirmed_regrant": False,
        }
        values.update(overrides)
        return grant_direct_video_entitlement(**values)

    def _request(self, method, path, *, user, data=None, tenant=None):
        request = getattr(self.factory, method)(path, data or {}, format="json")
        request.tenant = tenant or self.tenant
        force_authenticate(request, user=user)
        return request

    def _side_effect_counts(self):
        return {
            "enrollment": Enrollment.objects.count(),
            "session_enrollment": SessionEnrollment.objects.count(),
            "attendance": Attendance.objects.count(),
            "video_access": VideoAccess.objects.count(),
            "video_progress": VideoProgress.objects.count(),
            "playback_session": VideoPlaybackSession.objects.count(),
            "playback_event": VideoPlaybackEvent.objects.count(),
            "activity": OpsAuditLog.objects.count(),
            "notification_log": NotificationLog.objects.count(),
            "scheduled_notification": ScheduledNotification.objects.count(),
        }

    def test_grant_is_exact_idempotent_and_regrant_requires_confirmation(self):
        before = self._side_effect_counts()

        first = self._grant()
        repeated = self._grant()

        self.assertTrue(first.created)
        self.assertTrue(first.changed)
        self.assertFalse(repeated.created)
        self.assertFalse(repeated.changed)
        self.assertEqual(
            DirectVideoEntitlement.objects.filter(revoked_at__isnull=True).count(),
            1,
        )
        self.assertEqual(self._side_effect_counts(), before)

        revoked = revoke_direct_video_entitlement(
            tenant=self.tenant,
            entitlement_id=first.entitlement.id,
            reason="Teacher revoked exact video access",
            actor=self.staff,
            actor_reference=f"user:{self.staff.id}",
        )
        repeated_revoke = revoke_direct_video_entitlement(
            tenant=self.tenant,
            entitlement_id=first.entitlement.id,
            reason="Teacher revoked exact video access",
            actor=self.staff,
            actor_reference=f"user:{self.staff.id}",
        )
        self.assertTrue(revoked.changed)
        self.assertFalse(repeated_revoke.changed)

        with self.assertRaises(DirectVideoEntitlementError) as blocked:
            self._grant()
        self.assertEqual(blocked.exception.code, "regrant_confirmation_required")

        regranted = self._grant(confirmed_regrant=True)
        self.assertTrue(regranted.created)
        self.assertNotEqual(regranted.entitlement.id, first.entitlement.id)
        self.assertEqual(DirectVideoEntitlement.objects.count(), 2)
        self.assertEqual(self._side_effect_counts(), before)

    def test_existing_enrollment_any_status_blocks_grant_and_runtime(self):
        result = self._grant()
        self.assertIsNotNone(
            get_active_direct_video_entitlement(
                tenant=self.tenant,
                student=self.student,
                video=self.video,
            )
        )

        enrollment = create_enrollment_fixture(
            tenant=self.tenant,
            student=self.student,
            lecture=self.lecture,
            status="ACTIVE",
        )
        self.assertIsNone(
            get_active_direct_video_entitlement(
                tenant=self.tenant,
                student=self.student,
                video=self.video,
            )
        )
        enrollment.status = "INACTIVE"
        enrollment.save(update_fields=["status", "updated_at"])
        self.assertIsNone(
            get_active_direct_video_entitlement(
                tenant=self.tenant,
                student=self.student,
                video=self.video,
            )
        )
        with self.assertRaises(DirectVideoEntitlementError) as blocked:
            self._grant()
        self.assertEqual(blocked.exception.code, "enrollment_exists")
        self.assertEqual(result.entitlement.revoked_at, None)

    def test_grant_rejects_cross_tenant_public_youtube_and_inactive_account(self):
        other_user = self._user(
            tenant=self.other_tenant,
            username="direct-video-other-student",
            role="student",
        )
        other_student = create_student_fixture(
            tenant=self.other_tenant,
            user=other_user,
            name="Other Student",
            ps_number="DVE-OTHER",
            omr_code="87654321",
            parent_phone="01033334444",
            school_type="HIGH",
        )
        with self.assertRaises(DirectVideoEntitlementError) as cross_tenant:
            self._grant(student_id=other_student.id)
        self.assertEqual(cross_tenant.exception.code, "student_not_found")

        self.video.visibility = Video.Visibility.PUBLIC
        self.video.save(update_fields=["visibility", "updated_at"])
        with self.assertRaises(DirectVideoEntitlementError) as public:
            self._grant()
        self.assertEqual(public.exception.code, "video_already_public")

        self.video.visibility = Video.Visibility.ENROLLED
        self.video.source_type = Video.SourceType.YOUTUBE
        self.video.youtube_video_id = "abcdefghijk"
        self.video.save(
            update_fields=[
                "visibility",
                "source_type",
                "youtube_video_id",
                "updated_at",
            ]
        )
        with self.assertRaises(DirectVideoEntitlementError) as youtube:
            self._grant()
        self.assertEqual(youtube.exception.code, "video_source_unsupported")

        self.video.source_type = Video.SourceType.UPLOADED
        self.video.save(update_fields=["source_type", "updated_at"])
        self.student_user.is_active = False
        self.student_user.save(update_fields=["is_active"])
        with self.assertRaises(DirectVideoEntitlementError) as inactive:
            self._grant()
        self.assertEqual(inactive.exception.code, "account_inactive")
        self.assertFalse(DirectVideoEntitlement.objects.exists())

    def test_staff_api_is_tenant_scoped_and_rejects_nonstaff(self):
        create_view = DirectVideoEntitlementViewSet.as_view({"post": "create"})
        list_view = DirectVideoEntitlementViewSet.as_view({"get": "list"})
        payload = {
            "student_id": self.student.id,
            "video_id": self.video.id,
            "reason": "Teacher approved this exact video without enrollment",
            "confirmed_regrant": False,
        }
        request = self._request(
            "post",
            "/api/v1/media/direct-video-entitlements/",
            user=self.staff,
            data=payload,
        )
        response = create_view(request)
        self.assertEqual(response.status_code, 201, response.data)

        request = self._request(
            "get",
            f"/api/v1/media/direct-video-entitlements/?video_id={self.video.id}",
            user=self.staff,
        )
        response = list_view(request)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["student_id"], self.student.id)

        request = self._request(
            "post",
            "/api/v1/media/direct-video-entitlements/",
            user=self.student_user,
            data=payload,
        )
        response = create_view(request)
        self.assertEqual(response.status_code, 403)

        request = self._request(
            "get",
            "/api/v1/media/direct-video-entitlements/",
            user=self.staff,
        )
        response = list_view(request)
        self.assertEqual(response.status_code, 400)

        other_staff = self._user(
            tenant=self.other_tenant,
            username="direct-video-other-staff",
            role="admin",
            is_staff=True,
        )
        request = self._request(
            "get",
            f"/api/v1/media/direct-video-entitlements/?video_id={self.video.id}",
            user=other_staff,
            tenant=self.other_tenant,
        )
        response = list_view(request)
        self.assertEqual(response.status_code, 404)

    @override_settings(
        CDN_HLS_BASE_URL="https://cdn.example.test",
        CDN_HLS_SIGNING_SECRET="direct-video-test-secret",
        VIDEO_PLAYBACK_TTL_SECONDS=3600,
    )
    def test_student_sees_and_plays_only_exact_video_without_writes(self):
        entitlement = self._grant().entitlement
        before = self._side_effect_counts()
        before_view_count = self.video.view_count

        me_request = self._request(
            "get",
            "/api/v1/student/video/me/",
            user=self.student_user,
        )
        me_response = StudentVideoMeView.as_view()(me_request)
        self.assertEqual(me_response.status_code, 200, me_response.data)
        direct_lecture = next(
            row for row in me_response.data["lectures"] if row["id"] == self.lecture.id
        )
        self.assertIsNone(direct_lecture["enrollment_id"])
        self.assertEqual(direct_lecture["video_count"], 1)
        self.assertEqual([row["id"] for row in direct_lecture["sessions"]], [self.session.id])

        list_request = self._request(
            "get",
            f"/api/v1/student/video/sessions/{self.session.id}/videos/",
            user=self.student_user,
        )
        list_response = StudentSessionVideoListView.as_view()(
            list_request,
            session_id=self.session.id,
        )
        self.assertEqual(list_response.status_code, 200, list_response.data)
        self.assertEqual([row["id"] for row in list_response.data["items"]], [self.video.id])
        self.assertIsNone(list_response.data["items"][0]["enrollment_id"])
        self.assertEqual(list_response.data["items"][0]["access_mode"], "FREE_REVIEW")

        check_request = self._request(
            "get",
            f"/api/v1/student/video/videos/{self.video.id}/playback/?access_check=true",
            user=self.student_user,
        )
        check_response = StudentVideoPlaybackView.as_view()(
            check_request,
            video_id=self.video.id,
        )
        self.assertEqual(check_response.status_code, 200, check_response.data)
        self.assertEqual(check_response.data["access_mode"], "FREE_REVIEW")
        self.assertFalse(check_response.data["monitoring_enabled"])

        playback_request = self._request(
            "post",
            f"/api/v1/student/video/videos/{self.video.id}/playback/",
            user=self.student_user,
        )
        playback_response = StudentVideoPlaybackView.as_view()(
            playback_request,
            video_id=self.video.id,
        )
        self.assertEqual(playback_response.status_code, 200, playback_response.data)
        self.assertTrue(playback_response.data["playback_token"])
        self.assertIsNone(playback_response.data["playback_session_id"])
        self.assertIsNone(playback_response.data["video"]["enrollment_id"])
        ttl = playback_response.data["playback_expires_at"] - int(timezone.now().timestamp())
        self.assertGreater(ttl, 0)
        self.assertLessEqual(ttl, 600)

        sibling_request = self._request(
            "get",
            f"/api/v1/student/video/videos/{self.sibling_video.id}/playback/?access_check=true",
            user=self.student_user,
        )
        sibling_response = StudentVideoPlaybackView.as_view()(
            sibling_request,
            video_id=self.sibling_video.id,
        )
        self.assertEqual(sibling_response.status_code, 403)

        revoke_direct_video_entitlement(
            tenant=self.tenant,
            entitlement_id=entitlement.id,
            reason="Teacher revoked exact video access",
            actor=self.staff,
            actor_reference=f"user:{self.staff.id}",
        )
        revoked_request = self._request(
            "get",
            f"/api/v1/student/video/videos/{self.video.id}/playback/?access_check=true",
            user=self.student_user,
        )
        revoked_response = StudentVideoPlaybackView.as_view()(
            revoked_request,
            video_id=self.video.id,
        )
        self.assertEqual(revoked_response.status_code, 403)
        self.video.refresh_from_db()
        self.assertEqual(self.video.view_count, before_view_count)
        self.assertEqual(self._side_effect_counts(), before)

    @override_settings(
        CDN_HLS_BASE_URL="https://cdn.example.test",
        CDN_HLS_SIGNING_SECRET="direct-video-test-secret",
        VIDEO_PLAYBACK_TTL_SECONDS=3600,
    )
    def test_direct_token_consumers_revalidate_exact_current_scope_without_writes(self):
        entitlement = self._grant().entitlement
        before = self._side_effect_counts()
        before_view_count = self.video.view_count
        playback_request = self._request(
            "post",
            f"/api/v1/student/video/videos/{self.video.id}/playback/",
            user=self.student_user,
        )
        playback = StudentVideoPlaybackView.as_view()(
            playback_request,
            video_id=self.video.id,
        )
        self.assertEqual(playback.status_code, 200, playback.data)
        token = playback.data["playback_token"]

        refresh = PlaybackRefreshView.as_view()(
            self._request(
                "post",
                "/api/v1/media/playback/refresh/",
                user=self.student_user,
                data={"token": token},
            )
        )
        self.assertEqual(refresh.status_code, 200, refresh.data)
        renewal = PlaybackRenewView.as_view()(
            self._request(
                "post",
                "/api/v1/media/playback/renew/",
                user=self.student_user,
                data={"token": token},
            )
        )
        self.assertEqual(renewal.status_code, 200, renewal.data)
        self.assertIsNone(renewal.data["playback_session_id"])
        self.assertEqual(renewal.data["access_mode"], AccessMode.FREE_REVIEW)
        self.assertFalse(renewal.data["monitoring_enabled"])
        self.assertTrue(renewal.data["play_url"])
        renewed_ok, renewed_payload, renewed_error = verify_playback_token(
            renewal.data["playback_token"]
        )
        self.assertTrue(renewed_ok, renewed_error)
        self.assertEqual(renewed_payload["direct_entitlement_id"], entitlement.id)
        self.assertEqual(renewed_payload["student_id"], self.student.id)
        self.assertLessEqual(
            int(renewed_payload["exp"]),
            int(timezone.now().timestamp()) + 600,
        )
        heartbeat = PlaybackHeartbeatView.as_view()(
            self._request(
                "post",
                "/api/v1/media/playback/heartbeat/",
                user=self.student_user,
                data={"token": token},
            )
        )
        self.assertEqual(heartbeat.status_code, 200, heartbeat.data)
        events = PlaybackEventBatchView.as_view()(
            self._request(
                "post",
                "/api/v1/media/playback/events/",
                user=self.student_user,
                data={
                    "token": token,
                    "events": [{"type": "FULLSCREEN_ENTER", "payload": {}}],
                },
            )
        )
        self.assertEqual(events.status_code, 201, events.data)
        self.assertEqual(events.data["stored"], 0)

        valid, payload, error = verify_playback_token(token)
        self.assertTrue(valid, error)
        for replacement in (
            {"direct_entitlement_id": entitlement.id + 999},
            {"video_id": self.sibling_video.id},
            {"student_id": self.student.id + 999},
            {"tenant_id": self.other_tenant.id},
            {"aud": "student-video"},
            {"access_source": "ENROLLMENT"},
            {"access_mode": "PROCTORED_CLASS"},
            {"pv": int(payload["pv"]) + 1},
        ):
            tampered_payload = {
                key: value
                for key, value in payload.items()
                if key not in {"iat", "exp"}
            }
            tampered_payload.update(replacement)
            tampered = create_playback_token(
                payload=tampered_payload,
                ttl_seconds=300,
            )
            for view, path in (
                (PlaybackRefreshView, "/api/v1/media/playback/refresh/"),
                (PlaybackRenewView, "/api/v1/media/playback/renew/"),
            ):
                denied = view.as_view()(
                    self._request(
                        "post",
                        path,
                        user=self.student_user,
                        data={"token": tampered},
                    )
                )
                self.assertEqual(denied.status_code, 403, (view, replacement))

        enrollment = create_enrollment_fixture(
            tenant=self.tenant,
            student=self.student,
            lecture=self.lecture,
            status="ACTIVE",
        )
        denied_by_enrollment = PlaybackRefreshView.as_view()(
            self._request(
                "post",
                "/api/v1/media/playback/refresh/",
                user=self.student_user,
                data={"token": token},
            )
        )
        self.assertEqual(denied_by_enrollment.status_code, 403)
        denied_renewal_by_enrollment = PlaybackRenewView.as_view()(
            self._request(
                "post",
                "/api/v1/media/playback/renew/",
                user=self.student_user,
                data={"token": token},
            )
        )
        self.assertEqual(denied_renewal_by_enrollment.status_code, 403)
        enrollment.status = "INACTIVE"
        enrollment.save(update_fields=["status", "updated_at"])
        still_denied = PlaybackRefreshView.as_view()(
            self._request(
                "post",
                "/api/v1/media/playback/refresh/",
                user=self.student_user,
                data={"token": token},
            )
        )
        self.assertEqual(still_denied.status_code, 403)
        self.video.refresh_from_db()
        after = self._side_effect_counts()
        self.assertEqual(after["video_progress"], before["video_progress"])
        self.assertEqual(after["playback_session"], before["playback_session"])
        self.assertEqual(after["playback_event"], before["playback_event"])
        self.assertEqual(after["activity"], before["activity"])
        self.assertEqual(after["notification_log"], before["notification_log"])
        self.assertEqual(after["scheduled_notification"], before["scheduled_notification"])
        self.assertEqual(self.video.view_count, before_view_count)

    @override_settings(
        CDN_HLS_BASE_URL="https://cdn.example.test",
        CDN_HLS_SIGNING_SECRET="direct-video-test-secret",
    )
    def test_revoked_direct_token_is_rejected_by_every_runtime_consumer(self):
        entitlement = self._grant().entitlement
        playback = StudentVideoPlaybackView.as_view()(
            self._request(
                "post",
                f"/api/v1/student/video/videos/{self.video.id}/playback/",
                user=self.student_user,
            ),
            video_id=self.video.id,
        )
        self.assertEqual(playback.status_code, 200, playback.data)
        token = playback.data["playback_token"]
        revoke_direct_video_entitlement(
            tenant=self.tenant,
            entitlement_id=entitlement.id,
            reason="Teacher revoked exact video access",
            actor=self.staff,
            actor_reference=f"user:{self.staff.id}",
        )

        calls = (
            (PlaybackRefreshView, {"token": token}),
            (PlaybackHeartbeatView, {"token": token}),
            (
                PlaybackEventBatchView,
                {
                    "token": token,
                    "events": [{"type": "FULLSCREEN_ENTER", "payload": {}}],
                },
            ),
        )
        before = self._side_effect_counts()
        for view_class, data in calls:
            response = view_class.as_view()(
                self._request(
                    "post",
                    "/api/v1/media/playback/runtime/",
                    user=self.student_user,
                    data=data,
                )
            )
            self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(self._side_effect_counts(), before)

    def test_parent_cannot_select_an_unlinked_entitled_child(self):
        self._grant()
        parent_user = self._user(
            tenant=self.tenant,
            username="direct-video-parent",
            role="parent",
        )
        parent = Parent.objects.create(
            tenant=self.tenant,
            user=parent_user,
            name="Other Parent",
            phone="01055556666",
        )
        other_child_user = self._user(
            tenant=self.tenant,
            username="direct-video-other-child",
            role="student",
        )
        create_student_fixture(
            tenant=self.tenant,
            user=other_child_user,
            parent=parent,
            name="Other Child",
            ps_number="DVE-CHILD",
            omr_code="11223344",
            parent_phone="01055556666",
            school_type="HIGH",
        )
        request = self.factory.get(
            f"/api/v1/student/video/videos/{self.video.id}/playback/?access_check=true",
            HTTP_X_STUDENT_ID=str(self.student.id),
        )
        request.tenant = self.tenant
        force_authenticate(request, user=parent_user)
        response = StudentVideoPlaybackView.as_view()(request, video_id=self.video.id)
        self.assertEqual(response.status_code, 403)


@skipUnless(connection.vendor == "postgresql", "PostgreSQL migration contract")
class DirectVideoEntitlementMigrationCycleTests(TransactionTestCase):
    migrate_from = ("video", "0021_inactivevideoentitlement")
    migrate_to = ("video", "0022_directvideoentitlement")
    table_name = "video_directvideoentitlement"

    def _migrate(self, target):
        MigrationExecutor(connection).migrate([target])

    def _table_names(self):
        with connection.cursor() as cursor:
            return set(connection.introspection.table_names(cursor))

    def test_apply_rollback_apply_preserves_current_unique_constraint(self):
        self._migrate(self.migrate_from)
        self.assertNotIn(self.table_name, self._table_names())
        self._migrate(self.migrate_to)
        self.assertIn(self.table_name, self._table_names())
        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(cursor, self.table_name)
        self.assertIn("uniq_current_direct_video_entitlement", constraints)
        self.assertIn("direct_video_entitlement_source_valid", constraints)
        self._migrate(self.migrate_from)
        self.assertNotIn(self.table_name, self._table_names())
        self._migrate(self.migrate_to)
        self.assertIn(self.table_name, self._table_names())


def test_direct_entitlement_openapi_declares_exact_filters_and_error_contracts():
    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "schema" / "openapi.json").read_text(
            encoding="utf-8"
        )
    )
    collection = schema["paths"]["/api/v1/media/direct-video-entitlements/"]
    parameter_names = {parameter["name"] for parameter in collection["get"]["parameters"]}
    assert "video_id" in parameter_names
    for operation in (
        collection["get"],
        collection["post"],
        schema["paths"]["/api/v1/media/direct-video-entitlements/{id}/revoke/"]["post"],
    ):
        for response_code in ("400", "403", "404"):
            response_schema = operation["responses"][response_code]["content"][
                "application/json"
            ]["schema"]
            assert response_schema["$ref"].endswith("/DirectVideoEntitlementError")
