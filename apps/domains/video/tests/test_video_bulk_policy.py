from __future__ import annotations

from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.core.models import Tenant, TenantMembership
from apps.domains.video.models import Video
from apps.domains.video.views.video_views import VideoViewSet


User = get_user_model()
Lecture = apps.get_model("lectures", "Lecture")
Session = apps.get_model("lectures", "Session")


class VideoBulkPolicyTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.tenant = Tenant.objects.create(
            name="Video Policy Tenant",
            code="video-policy-tenant",
            is_active=True,
        )
        self.other_tenant = Tenant.objects.create(
            name="Other Video Policy Tenant",
            code="other-video-policy-tenant",
            is_active=True,
        )
        self.user = User.objects.create_user(
            username="video_policy_admin",
            password="test1234",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=self.user,
            role="admin",
        )
        lecture = Lecture.objects.create(
            tenant=self.tenant,
            title="정규 강의",
            name="정규 강의",
            subject="MATH",
        )
        self.session = Session.objects.create(lecture=lecture, title="1차시", order=1)
        self.other_session = Session.objects.create(lecture=lecture, title="2차시", order=2)
        other_lecture = Lecture.objects.create(
            tenant=self.other_tenant,
            title="다른 학원 강의",
            name="다른 학원 강의",
            subject="MATH",
        )
        self.cross_tenant_session = Session.objects.create(
            lecture=other_lecture,
            title="다른 학원 차시",
            order=1,
        )
        self.first = Video.objects.create(
            tenant=self.tenant,
            session=self.session,
            title="첫 영상",
            order=1,
            allow_skip=False,
            max_speed=1.0,
            policy_version=4,
        )
        self.second = Video.objects.create(
            tenant=self.tenant,
            session=self.session,
            title="둘째 영상",
            order=2,
            allow_skip=True,
            max_speed=1.5,
            policy_version=7,
        )
        self.other_session_video = Video.objects.create(
            tenant=self.tenant,
            session=self.other_session,
            title="다른 차시 영상",
            order=1,
        )
        self.cross_tenant_video = Video.objects.create(
            tenant=self.other_tenant,
            session=self.cross_tenant_session,
            title="다른 학원 영상",
            order=1,
        )

    def _bulk(self, payload: dict):
        request = self.factory.post(
            "/api/v1/media/videos/bulk-policy/",
            payload,
            format="json",
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.user)
        return VideoViewSet.as_view({"post": "bulk_policy"})(request)

    def _patch(self, video: Video, payload: dict):
        request = self.factory.patch(
            f"/api/v1/media/videos/{video.id}/",
            payload,
            format="json",
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.user)
        return VideoViewSet.as_view({"patch": "partial_update"})(request, pk=video.id)

    def test_bulk_policy_updates_only_requested_fields_and_versions_only_changed_rows(self):
        response = self._bulk(
            {
                "session_id": self.session.id,
                "video_ids": [self.first.id, self.second.id],
                "allow_skip": True,
                "max_speed": 1.5,
            }
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data, {"updated": 2, "changed": 1})
        self.first.refresh_from_db()
        self.second.refresh_from_db()
        self.assertEqual(
            (self.first.allow_skip, self.first.max_speed, self.first.policy_version),
            (True, 1.5, 5),
        )
        self.assertEqual(
            (self.second.allow_skip, self.second.max_speed, self.second.policy_version),
            (True, 1.5, 7),
        )

    def test_bulk_policy_can_change_only_one_field(self):
        response = self._bulk(
            {
                "session_id": self.session.id,
                "video_ids": [self.first.id],
                "max_speed": 2,
            }
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.first.refresh_from_db()
        self.assertFalse(self.first.allow_skip)
        self.assertEqual(self.first.max_speed, 2)
        self.assertEqual(self.first.policy_version, 5)

    def test_bulk_policy_rejects_mixed_session_without_any_write(self):
        response = self._bulk(
            {
                "session_id": self.session.id,
                "video_ids": [self.first.id, self.other_session_video.id],
                "allow_skip": True,
            }
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.first.refresh_from_db()
        self.other_session_video.refresh_from_db()
        self.assertEqual((self.first.allow_skip, self.first.policy_version), (False, 4))
        self.assertEqual(
            (self.other_session_video.allow_skip, self.other_session_video.policy_version),
            (False, 1),
        )

    def test_bulk_policy_rejects_cross_tenant_or_deleted_id_without_any_write(self):
        deleted = Video.objects.create(
            tenant=self.tenant,
            session=self.session,
            title="삭제된 영상",
            order=3,
        )
        deleted.delete()

        for invalid_id in (self.cross_tenant_video.id, deleted.id):
            with self.subTest(invalid_id=invalid_id):
                response = self._bulk(
                    {
                        "session_id": self.session.id,
                        "video_ids": [self.first.id, invalid_id],
                        "allow_skip": True,
                    }
                )
                self.assertEqual(response.status_code, 400, response.data)
                self.first.refresh_from_db()
                self.assertEqual((self.first.allow_skip, self.first.policy_version), (False, 4))

    def test_bulk_policy_rejects_duplicates_bounds_and_missing_change(self):
        invalid_payloads = [
            {
                "session_id": self.session.id,
                "video_ids": [self.first.id, self.first.id],
                "allow_skip": True,
            },
            {
                "session_id": self.session.id,
                "video_ids": [self.first.id] * 501,
                "allow_skip": True,
            },
            {"session_id": self.session.id, "video_ids": [self.first.id]},
            {
                "session_id": self.session.id,
                "video_ids": [self.first.id],
                "max_speed": 0,
            },
            {
                "session_id": self.session.id,
                "video_ids": [self.first.id],
                "allow_skip": "true",
            },
        ]

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                response = self._bulk(payload)
                self.assertEqual(response.status_code, 400, response.data)
                self.first.refresh_from_db()
                self.assertEqual(
                    (self.first.allow_skip, self.first.max_speed, self.first.policy_version),
                    (False, 1.0, 4),
                )

    def test_individual_patch_increments_version_for_actual_policy_change_only(self):
        changed = self._patch(
            self.first,
            {"allow_skip": True, "max_speed": 1.5, "show_watermark": False},
        )
        self.assertEqual(changed.status_code, 200, changed.data)
        self.first.refresh_from_db()
        self.assertEqual(
            (
                self.first.allow_skip,
                self.first.max_speed,
                self.first.show_watermark,
                self.first.policy_version,
            ),
            (True, 1.5, False, 5),
        )

        unchanged = self._patch(
            self.first,
            {"allow_skip": True, "max_speed": 1.5, "show_watermark": False},
        )
        self.assertEqual(unchanged.status_code, 200, unchanged.data)
        self.first.refresh_from_db()
        self.assertEqual(self.first.policy_version, 5)

        title_only = self._patch(self.first, {"title": "제목만 변경"})
        self.assertEqual(title_only.status_code, 200, title_only.data)
        self.first.refresh_from_db()
        self.assertEqual(self.first.policy_version, 5)
