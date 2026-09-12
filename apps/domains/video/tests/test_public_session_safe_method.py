"""Public-session preparation and reads through the installed HTTP middleware."""

from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import OperationalError, connections
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from rest_framework.test import APIClient

from apps.core.models import Tenant, TenantDomain, TenantMembership
from apps.domains.video.models import Video
from apps.support.video.view_dependencies import get_or_create_public_video_session

Lecture = apps.get_model("lectures", "Lecture")
Session = apps.get_model("lectures", "Session")
Student = apps.get_model("students", "Student")


class PublicSessionSafeMethodTests(TestCase):
    def setUp(self):
        cache.clear()
        self.assertIn(
            "apps.core.middleware.safe_method_write.SafeMethodDatabaseWriteMiddleware",
            settings.MIDDLEWARE,
        )
        self.tenant = Tenant.objects.create(
            name="Public session verification", code="public-session-verification",
        )
        self.host = "public-session-verification.example.com"
        TenantDomain.objects.update_or_create(
            tenant=self.tenant,
            defaults={"host": self.host, "is_primary": True, "is_active": True},
        )
        self.user = get_user_model().objects.create_user(
            username="public-session-admin", password="test-password",
            tenant=self.tenant,
        )
        self.membership = TenantMembership.ensure_active(
            tenant=self.tenant, user=self.user, role="admin",
        )
        self.client = APIClient(raise_request_exception=False)
        self.client.force_authenticate(user=self.user)

    def _get(self):
        return self.client.get(
            "/api/v1/media/videos/public-session/", HTTP_HOST=self.host,
        )

    def _prepare(self):
        return self.client.post(
            "/api/v1/media/videos/public-session/", {}, format="json", HTTP_HOST=self.host,
        )

    def test_first_preparation_returns_a_usable_public_container(self):
        self.assertFalse(Lecture.objects.filter(tenant=self.tenant).exists())
        self.assertIsNone(self._get().json())
        self.assertFalse(Lecture.objects.filter(tenant=self.tenant).exists())

        response = self._prepare()

        self.assertEqual(response.status_code, 200, (
            response.content,
            response.exc_info[1] if response.exc_info else None,
            {"tenant_lecture_count": Lecture.objects.filter(tenant=self.tenant).count()},
        ))
        lecture = Lecture.objects.get(pk=response.json()["lecture_id"])
        session = Session.objects.get(pk=response.json()["session_id"])
        self.assertEqual(lecture.tenant_id, self.tenant.pk)
        self.assertTrue(lecture.is_system)
        self.assertEqual(session.lecture_id, lecture.pk)
        self.assertEqual(session.order, 1)
        self.assertEqual(self._get().json(), response.json())
        self.assertEqual(self._prepare().json(), response.json())
        self.assertEqual(Lecture.objects.filter(tenant=self.tenant).count(), 1)
        self.assertEqual(Session.objects.filter(lecture=lecture).count(), 1)

        created = self.client.post("/api/v1/media/videos/youtube/", {
            "session": session.pk, "title": "First public video",
            "url": "https://youtu.be/VnqgmOJaMGc",
        }, format="json", HTTP_HOST=self.host)
        self.assertEqual(created.status_code, 201, created.content)
        video = Video.objects.get(pk=created.json()["video"]["id"])
        self.assertEqual(video.visibility, Video.Visibility.PUBLIC)
        self.assertEqual(video.session_id, session.pk)
        listed = self.client.get(
            "/api/v1/media/videos/", {"session": session.pk}, HTTP_HOST=self.host,
        )
        self.assertEqual(listed.status_code, 200, listed.content)
        self.assertEqual([row["id"] for row in listed.json()["results"]], [video.pk])
        student_user = get_user_model().objects.create_user(
            username="public-session-student", password="test-password", tenant=self.tenant,
        )
        Student.objects.create(
            tenant=self.tenant, user=student_user, name="Public video student",
            ps_number="PUBLIC-1", omr_code="12345678", parent_phone="01012345678",
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=student_user, role="student")
        self.client.force_authenticate(user=student_user)
        student_read = self.client.get(
            "/api/v1/student/video/public-session/", HTTP_HOST=self.host,
        )
        self.assertEqual(student_read.status_code, 200, student_read.content)
        self.assertEqual(student_read.json(), response.json())

    def test_existing_container_get_remains_compatible_for_admin_and_teacher(self):
        lecture = Lecture.get_or_create_system_lecture(self.tenant)
        session = Session.objects.create(lecture=lecture, order=1, title="전체공개영상")
        before = (Lecture.objects.count(), Session.objects.count())

        for role in ("admin", "teacher"):
            with self.subTest(role=role):
                self.membership.role = role
                self.membership.save(update_fields=["role"])
                response = self._get()
                self.assertEqual(response.status_code, 200, response.content)
                self.assertEqual(response.json(), {
                    "lecture_id": lecture.pk, "session_id": session.pk,
                })
                self.assertEqual((Lecture.objects.count(), Session.objects.count()), before)

    def test_legacy_container_preparation_preserves_existing_ids(self):
        lecture = Lecture.objects.create(
            tenant=self.tenant, title="전체공개영상", name="전체공개영상", subject="공개",
        )
        session = Session.objects.create(lecture=lecture, order=1, title="전체공개영상")

        response = self._get()

        lecture.refresh_from_db()
        self.assertEqual(response.status_code, 200, (
            response.content,
            response.exc_info[1] if response.exc_info else None,
            {"legacy_is_system": lecture.is_system},
        ))
        self.assertEqual(response.json(), {
            "lecture_id": lecture.pk, "session_id": session.pk,
        })
        lecture.refresh_from_db()
        self.assertFalse(lecture.is_system)
        prepared = self._prepare()
        self.assertEqual(prepared.status_code, 200, prepared.content)
        self.assertEqual(prepared.json(), response.json())
        lecture.refresh_from_db()
        self.assertTrue(lecture.is_system)

    def test_failed_preparation_rolls_back_and_can_be_retried(self):
        original_save = Session.save

        def save_then_fail(instance, *args, **kwargs):
            original_save(instance, *args, **kwargs)
            raise OperationalError("private database diagnostic")

        with patch.object(Session, "save", save_then_fail):
            response = self._prepare()
        self.assertEqual(response.status_code, 503, response.content)
        self.assertEqual(response.json()["code"], "public_video_session_failed")
        self.assertNotIn("private database diagnostic", str(response.content))
        self.assertFalse(Lecture.objects.filter(tenant=self.tenant).exists())
        self.assertFalse(Session.objects.exists())
        self.assertIsNone(self._get().json())
        retried = self._prepare()
        self.assertEqual(retried.status_code, 200, retried.content)
        self.assertEqual(self._get().json(), retried.json())

    def test_old_client_upload_posts_normalize_only_the_selected_legacy_container(self):
        lecture = Lecture.objects.create(
            tenant=self.tenant, title="전체공개영상", name="전체공개영상", subject="공개",
        )
        session = Session.objects.create(lecture=lecture, order=1, title="전체공개영상")
        existing_video = Video.objects.create(
            tenant=self.tenant, session=session, title="Existing unchanged video",
            visibility=Video.Visibility.ENROLLED,
        )
        for path, payload in (
            ("upload/init", {"title": "Legacy upload", "filename": "lesson.mp4"}),
            ("youtube", {"title": "Legacy link", "url": "https://youtu.be/VnqgmOJaMGc"}),
        ):
            with self.subTest(path=path):
                lecture.is_system = False
                lecture.save(update_fields=["is_system"])
                ids = self._get().json()
                lecture.refresh_from_db()
                self.assertFalse(lecture.is_system)
                with patch(
                    "apps.domains.video.views.video_views.create_presigned_put_url",
                    return_value="https://uploads.example/fixture-only",
                ):
                    response = self.client.post(
                        f"/api/v1/media/videos/{path}/",
                        {"session": ids["session_id"], **payload},
                        format="json", HTTP_HOST=self.host,
                    )
                self.assertEqual(response.status_code, 201, response.content)
                created = Video.objects.get(pk=response.json()["video"]["id"])
                self.assertEqual(created.visibility, Video.Visibility.PUBLIC)
                self.assertEqual(created.session_id, session.pk)
                self.assertEqual(self._get().json(), ids)
                lecture.refresh_from_db()
                self.assertTrue(lecture.is_system)
                existing_video.refresh_from_db()
                self.assertEqual(existing_video.visibility, Video.Visibility.ENROLLED)
        self.assertEqual(Lecture.objects.filter(tenant=self.tenant).count(), 1)
        self.assertEqual(Session.objects.filter(lecture=lecture).count(), 1)

    def test_read_failure_is_not_an_unprepared_success(self):
        with patch(
            "apps.domains.video.views.video_views.get_public_video_session",
            side_effect=OperationalError("private database diagnostic"),
        ):
            response = self._get()
        self.assertEqual(response.status_code, 503, response.content)
        self.assertEqual(response.json()["code"], "public_video_session_failed")
        self.assertNotIn("private database diagnostic", str(response.content))

    def test_partial_container_read_does_not_create_a_session(self):
        lecture = Lecture.get_or_create_system_lecture(self.tenant)
        self.assertIsNone(self._get().json())
        self.assertFalse(Session.objects.filter(lecture=lecture).exists())
        prepared = self._prepare()
        self.assertEqual(prepared.status_code, 200, prepared.content)
        self.assertEqual(prepared.json()["lecture_id"], lecture.pk)
        self.assertEqual(self._get().json(), prepared.json())

    def test_student_parent_and_other_tenant_cannot_prepare_or_read_staff_container(self):
        for role in ("student", "parent"):
            self.membership.role = role
            self.membership.save(update_fields=["role"])
            for method in (self._get, self._prepare):
                with self.subTest(role=role, method=method.__name__):
                    response = method()
                    self.assertEqual(response.status_code, 403, response.content)
        self.membership.role = "admin"
        self.membership.save(update_fields=["role"])
        other = Tenant.objects.create(name="Other public tenant", code="public-session-other")
        other_host = TenantDomain.objects.get(tenant=other).host
        for method in (self.client.get, self.client.post):
            response = method("/api/v1/media/videos/public-session/", HTTP_HOST=other_host)
            self.assertEqual(response.status_code, 403, response.content)
        self.assertFalse(Lecture.objects.filter(tenant__in=[self.tenant, other]).exists())


class PublicSessionConcurrencyTests(TransactionTestCase):
    @skipUnlessDBFeature("has_select_for_update")
    def test_simultaneous_preparation_returns_one_container(self):
        tenant = Tenant.objects.create(name="Concurrent public", code="public-concurrent")
        barrier = Barrier(2)

        def prepare():
            try:
                current = Tenant.objects.get(pk=tenant.pk)
                barrier.wait(timeout=10)
                lecture, session = get_or_create_public_video_session(tenant=current)
                return lecture.pk, session.pk
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _index: prepare(), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(Lecture.objects.filter(tenant=tenant).count(), 1)
        self.assertEqual(Session.objects.filter(lecture__tenant=tenant).count(), 1)
