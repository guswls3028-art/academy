from __future__ import annotations

import uuid
from unittest.mock import patch

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models.query import QuerySet
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.core.models import Tenant, TenantMembership
from apps.domains.enrollment.test_support import create_enrollment_fixture
from apps.domains.homework.test_support import create_homework_assignment_fixture
from apps.domains.homework_results.test_support import (
    create_homework_fixture,
    create_homework_score_fixture,
)
from apps.domains.lectures.test_support import (
    create_lecture_fixture,
    create_session_fixture,
)
from apps.domains.parents.models import Parent
from apps.domains.results.views.session_scores_view import SessionScoreCorrectionView
from apps.domains.students.test_support import create_student_fixture
from apps.domains.submissions.models import Submission, SubmissionMedia
from apps.domains.submissions.services import dispatcher
from apps.domains.submissions.views.homework_submission_media_view import (
    HomeworkSubmissionMediaCollectionView,
    HomeworkSubmissionMediaDetailView,
    HomeworkSubmissionMediaPreviewView,
)
from apps.domains.submissions.views.homework_submissions_list_view import (
    HomeworkSubmissionsListView,
)
from apps.domains.submissions.views.pending_submissions_view import (
    PendingSubmissionsView,
)
from apps.support.results.admin_student_grades_dependencies import (
    submitted_homework_keys_for_grades,
)


User = get_user_model()


def _jpeg(name: str = "page.jpg", *, body: bytes = b"page") -> SimpleUploadedFile:
    return SimpleUploadedFile(
        name,
        b"\xff\xd8\xff\xe0" + body,
        content_type="image/jpeg",
    )


def _mp4(name: str = "proof.mp4") -> SimpleUploadedFile:
    return SimpleUploadedFile(
        name,
        b"\x00\x00\x00\x18ftypmp42" + b"video-proof",
        content_type="video/mp4",
    )


class HomeworkSubmissionMediaTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.tenant = Tenant.objects.create(
            code="homework-media",
            name="Homework Media",
            is_active=True,
        )
        self.student_user = User.objects.create_user(
            username="homework-media-student",
            password="pw1234",
            tenant=self.tenant,
            name="학생",
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=self.student_user,
            role="student",
        )
        self.student = create_student_fixture(
            tenant=self.tenant,
            user=self.student_user,
            ps_number="HWM001",
            omr_code="31415926",
            name="학생",
            phone="01011112222",
            parent_phone="01033334444",
        )
        self.teacher = User.objects.create_user(
            username="homework-media-teacher",
            password="pw1234",
            tenant=self.tenant,
            name="선생님",
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=self.teacher,
            role="teacher",
        )
        self.lecture = create_lecture_fixture(
            tenant=self.tenant,
            title="수학",
            name="수학",
            subject="MATH",
        )
        self.session = create_session_fixture(
            lecture=self.lecture,
            order=1,
            title="1회",
        )
        self.enrollment = create_enrollment_fixture(
            tenant=self.tenant,
            student=self.student,
            lecture=self.lecture,
            status="ACTIVE",
        )
        self.homework = create_homework_fixture(
            tenant=self.tenant,
            session=self.session,
            title="풀이 인증",
            meta={"default_max_score": 10},
        )
        create_homework_assignment_fixture(
            tenant=self.tenant,
            homework=self.homework,
            session=self.session,
            enrollment=self.enrollment,
        )

    def _link_parent(self):
        self.parent_user = User.objects.create_user(
            username="homework-media-parent",
            password="pw1234",
            tenant=self.tenant,
            name="학부모",
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=self.parent_user,
            role="parent",
        )
        self.parent = Parent.objects.create(
            tenant=self.tenant,
            user=self.parent_user,
            name="학부모",
            phone="01033334444",
        )
        self.student.parent = self.parent
        self.student.save(update_fields=["parent", "updated_at"])

    def _request(
        self,
        method: str,
        path: str,
        *,
        user=None,
        data=None,
        student_id=None,
        tenant=None,
    ):
        headers = (
            {"HTTP_X_STUDENT_ID": str(student_id)}
            if student_id is not None
            else {}
        )
        if method == "get":
            request = self.factory.get(path, data=data, **headers)
        elif method == "post":
            request = self.factory.post(path, data=data, format="multipart", **headers)
        elif method == "delete":
            request = self.factory.delete(path, data=data, format="json", **headers)
        elif method == "patch":
            request = self.factory.patch(path, data=data, format="json", **headers)
        else:
            raise AssertionError(f"unsupported method: {method}")
        request.tenant = tenant or self.tenant
        force_authenticate(request, user=user or self.student_user)
        return request

    def _post(
        self,
        *,
        file,
        client_file_id=None,
        batch_id=None,
        position=0,
        user=None,
        student_id=None,
    ):
        path = f"/api/v1/submissions/submissions/homework/{self.homework.id}/media/"
        request = self._request(
            "post",
            path,
            data={
                "enrollment_id": self.enrollment.id,
                "client_file_id": client_file_id or str(uuid.uuid4()),
                "upload_batch_id": batch_id or str(uuid.uuid4()),
                "position": position,
                "file": file,
            },
            user=user,
            student_id=student_id,
        )
        return HomeworkSubmissionMediaCollectionView.as_view()(
            request,
            homework_id=self.homework.id,
        )

    @patch("apps.domains.submissions.services.homework_media.upload_fileobj_to_r2")
    def test_parent_selected_child_upload_persists_as_child_and_reloads_in_projection(
        self,
        upload_fileobj_to_r2,
    ):
        self._link_parent()
        created = self._post(
            file=_jpeg("parent-proof.jpg"),
            user=self.parent_user,
            student_id=self.student.id,
        )

        self.assertEqual(created.status_code, 201, created.data)
        submission = Submission.objects.get(media_files__id=int(created.data["id"]))
        self.assertEqual(submission.user_id, self.student_user.id)
        self.assertEqual(submission.enrollment_id, self.enrollment.id)
        self.assertEqual(submission.meta["submitted_by_user_id"], self.parent_user.id)
        self.assertEqual(
            submitted_homework_keys_for_grades(
                tenant=self.tenant,
                enrollment_ids=[self.enrollment.id],
                homework_ids=[self.homework.id],
            ),
            {(self.enrollment.id, self.homework.id)},
        )

        request = self._request(
            "get",
            f"/api/v1/submissions/submissions/homework/{self.homework.id}/media/",
            user=self.parent_user,
            student_id=self.student.id,
            data={"enrollment_id": self.enrollment.id},
        )
        reloaded = HomeworkSubmissionMediaCollectionView.as_view()(
            request,
            homework_id=self.homework.id,
        )

        self.assertEqual(reloaded.status_code, 200, reloaded.data)
        self.assertEqual(
            [item["original_filename"] for item in reloaded.data["files"]],
            ["parent-proof.jpg"],
        )
        upload_fileobj_to_r2.assert_called_once()

    @patch("apps.domains.submissions.services.homework_media.upload_fileobj_to_r2")
    def test_parent_cannot_upload_without_explicit_selected_child(
        self,
        upload_fileobj_to_r2,
    ):
        self._link_parent()
        response = self._post(file=_jpeg("ambiguous.jpg"), user=self.parent_user)

        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(Submission.objects.count(), 0)
        self.assertEqual(SubmissionMedia.objects.count(), 0)
        upload_fileobj_to_r2.assert_not_called()

    @patch("apps.domains.submissions.services.homework_media.upload_fileobj_to_r2")
    def test_parent_cannot_upload_for_unlinked_same_tenant_student(
        self,
        upload_fileobj_to_r2,
    ):
        self._link_parent()
        unlinked_user = User.objects.create_user(
            username="homework-media-unlinked",
            password="pw1234",
            tenant=self.tenant,
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=unlinked_user,
            role="student",
        )
        unlinked_student = create_student_fixture(
            tenant=self.tenant,
            user=unlinked_user,
            ps_number="HWM-UNLINKED",
            omr_code="11235813",
            name="미연결 학생",
            phone="01055557777",
            parent_phone="01088889999",
        )
        unlinked_enrollment = create_enrollment_fixture(
            tenant=self.tenant,
            student=unlinked_student,
            lecture=self.lecture,
            status="ACTIVE",
        )
        create_homework_assignment_fixture(
            tenant=self.tenant,
            homework=self.homework,
            session=self.session,
            enrollment=unlinked_enrollment,
        )
        path = f"/api/v1/submissions/submissions/homework/{self.homework.id}/media/"
        request = self._request(
            "post",
            path,
            user=self.parent_user,
            student_id=unlinked_student.id,
            data={
                "enrollment_id": unlinked_enrollment.id,
                "client_file_id": str(uuid.uuid4()),
                "upload_batch_id": str(uuid.uuid4()),
                "position": 0,
                "file": _jpeg("unlinked.jpg"),
            },
        )

        response = HomeworkSubmissionMediaCollectionView.as_view()(
            request,
            homework_id=self.homework.id,
        )

        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(Submission.objects.count(), 0)
        self.assertEqual(SubmissionMedia.objects.count(), 0)
        upload_fileobj_to_r2.assert_not_called()

    @patch("apps.domains.submissions.services.homework_media.upload_fileobj_to_r2")
    def test_parent_cannot_upload_for_cross_tenant_student(
        self,
        upload_fileobj_to_r2,
    ):
        self._link_parent()
        other_tenant = Tenant.objects.create(
            code="homework-media-other-tenant",
            name="Other Homework Media",
            is_active=True,
        )
        other_user = User.objects.create_user(
            username="homework-media-cross-tenant",
            password="pw1234",
            tenant=other_tenant,
        )
        TenantMembership.ensure_active(
            tenant=other_tenant,
            user=other_user,
            role="student",
        )
        other_student = create_student_fixture(
            tenant=other_tenant,
            user=other_user,
            ps_number="HWM-CROSS-TENANT",
            omr_code="16180339",
            name="다른 학원 학생",
            phone="01022223333",
            parent_phone="01044445555",
        )

        response = self._post(
            file=_jpeg("cross-tenant.jpg"),
            user=self.parent_user,
            student_id=other_student.id,
        )

        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(Submission.objects.count(), 0)
        self.assertEqual(SubmissionMedia.objects.count(), 0)
        upload_fileobj_to_r2.assert_not_called()

    @patch("apps.domains.submissions.services.homework_media.upload_fileobj_to_r2")
    def test_multiple_images_and_video_persist_with_identity_and_order(
        self,
        upload_fileobj_to_r2,
    ):
        batch_id = str(uuid.uuid4())
        first = self._post(file=_jpeg("01.jpg"), batch_id=batch_id, position=0)
        second = self._post(file=_mp4("02.mp4"), batch_id=batch_id, position=1)

        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(second.status_code, 201, second.data)
        self.assertNotEqual(first.data["id"], second.data["id"])
        self.assertEqual(upload_fileobj_to_r2.call_count, 2)
        self.assertEqual(Submission.objects.count(), 1)
        self.assertEqual(SubmissionMedia.objects.count(), 2)

        request = self._request(
            "get",
            f"/api/v1/submissions/submissions/homework/{self.homework.id}/media/",
            data={"enrollment_id": self.enrollment.id},
        )
        response = HomeworkSubmissionMediaCollectionView.as_view()(
            request,
            homework_id=self.homework.id,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([item["position"] for item in response.data["files"]], [0, 1])
        self.assertEqual(
            [item["media_kind"] for item in response.data["files"]],
            ["image", "video"],
        )
        self.assertEqual(response.data["limits"]["max_files"], 20)
        self.assertEqual(response.data["limits"]["max_file_size_bytes"], 100 * 1024 * 1024)
        self.assertEqual(response.data["limits"]["max_total_size_bytes"], 500 * 1024 * 1024)

    @patch("apps.domains.submissions.services.homework_media.upload_fileobj_to_r2")
    def test_same_client_file_retry_is_idempotent(
        self,
        upload_fileobj_to_r2,
    ):
        client_file_id = str(uuid.uuid4())
        first = self._post(file=_jpeg(body=b"same"), client_file_id=client_file_id)
        second = self._post(file=_jpeg(body=b"same"), client_file_id=client_file_id)

        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(first.data["id"], second.data["id"])
        self.assertTrue(second.data["deduplicated"])
        upload_fileobj_to_r2.assert_called_once()

    @patch("apps.domains.submissions.services.homework_media.upload_fileobj_to_r2")
    def test_same_successful_fingerprint_with_new_client_id_is_not_duplicated(
        self,
        upload_fileobj_to_r2,
    ):
        first = self._post(file=_jpeg(body=b"same-fingerprint"), position=0)
        second = self._post(file=_jpeg(body=b"same-fingerprint"), position=1)

        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(first.data["id"], second.data["id"])
        self.assertTrue(second.data["deduplicated"])
        self.assertEqual(SubmissionMedia.objects.count(), 1)
        upload_fileobj_to_r2.assert_called_once()

    @patch("apps.domains.submissions.services.homework_media.upload_fileobj_to_r2")
    def test_failed_file_retry_reuses_object_identity_and_keeps_successful_file(
        self,
        upload_fileobj_to_r2,
    ):
        upload_fileobj_to_r2.side_effect = [None, RuntimeError("storage unavailable"), None]
        successful = self._post(file=_jpeg("success.jpg", body=b"success"), position=0)
        client_file_id = str(uuid.uuid4())
        failed = self._post(
            file=_jpeg("retry.jpg", body=b"retry"),
            client_file_id=client_file_id,
            position=1,
        )

        self.assertEqual(successful.status_code, 201, successful.data)
        self.assertEqual(failed.status_code, 503, failed.data)
        failed_media = SubmissionMedia.objects.get(client_upload_id=client_file_id)
        failed_object_key = failed_media.object_key
        self.assertEqual(failed_media.status, SubmissionMedia.Status.FAILED)
        self.assertEqual(
            SubmissionMedia.objects.get(id=successful.data["id"]).status,
            SubmissionMedia.Status.UPLOADED,
        )

        retried = self._post(
            file=_jpeg("retry.jpg", body=b"retry"),
            client_file_id=client_file_id,
            position=1,
        )

        self.assertEqual(retried.status_code, 201, retried.data)
        failed_media.refresh_from_db()
        self.assertEqual(failed_media.status, SubmissionMedia.Status.UPLOADED)
        self.assertEqual(failed_media.object_key, failed_object_key)
        self.assertEqual(Submission.objects.count(), 1)
        self.assertEqual(SubmissionMedia.objects.count(), 2)

    @patch("apps.domains.submissions.services.homework_media.upload_fileobj_to_r2")
    def test_object_success_with_db_finalize_failure_is_recoverable_by_same_retry(
        self,
        upload_fileobj_to_r2,
    ):
        client_file_id = str(uuid.uuid4())
        original_update = QuerySet.update

        def fail_uploaded_update(queryset, **kwargs):
            if kwargs.get("status") == SubmissionMedia.Status.UPLOADED:
                raise RuntimeError("database finalize unavailable")
            return original_update(queryset, **kwargs)

        with patch.object(QuerySet, "update", new=fail_uploaded_update):
            failed = self._post(
                file=_jpeg("db-retry.jpg", body=b"db-retry"),
                client_file_id=client_file_id,
            )

        self.assertEqual(failed.status_code, 503, failed.data)
        media = SubmissionMedia.objects.get(client_upload_id=client_file_id)
        object_key = media.object_key
        self.assertEqual(media.status, SubmissionMedia.Status.FAILED)
        self.assertEqual(media.error_message, "파일 저장 확인 실패")

        retried = self._post(
            file=_jpeg("db-retry.jpg", body=b"db-retry"),
            client_file_id=client_file_id,
        )

        self.assertEqual(retried.status_code, 201, retried.data)
        media.refresh_from_db()
        self.assertEqual(media.object_key, object_key)
        self.assertEqual(media.status, SubmissionMedia.Status.UPLOADED)
        self.assertEqual(upload_fileobj_to_r2.call_count, 2)

    @patch("apps.domains.submissions.services.homework_media.upload_fileobj_to_r2")
    def test_accepts_extension_when_signature_or_browser_mime_is_unreliable(
        self,
        upload_fileobj_to_r2,
    ):
        mismatched_browser_mime = self._post(
            file=SimpleUploadedFile("camera.jpg", b"phone-camera-bytes", content_type="application/octet-stream")
        )
        self.assertEqual(mismatched_browser_mime.status_code, 201, mismatched_browser_mime.data)
        self.assertEqual(mismatched_browser_mime.data["media_kind"], "image")
        self.assertEqual(mismatched_browser_mime.data["mime_type"], "image/jpeg")

    @patch("apps.domains.submissions.services.homework_media.upload_fileobj_to_r2")
    def test_accepts_camera_file_without_extension_when_browser_mime_is_supported(
        self,
        upload_fileobj_to_r2,
    ):
        response = self._post(
            file=SimpleUploadedFile("camera-capture", b"phone-camera-bytes", content_type="image/heic")
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["media_kind"], "image")
        self.assertEqual(response.data["mime_type"], "image/heic")

    @patch("apps.domains.submissions.services.homework_media.upload_fileobj_to_r2")
    def test_rejects_twenty_first_active_file(
        self,
        upload_fileobj_to_r2,
    ):

        for position in range(20):
            response = self._post(
                file=_jpeg(f"{position}.jpg", body=f"page-{position}".encode()),
                position=position,
            )
            self.assertEqual(response.status_code, 201, response.data)
        overflow = self._post(
            file=_jpeg("overflow.jpg", body=b"overflow"),
            position=19,
        )

        self.assertEqual(overflow.status_code, 400, overflow.data)
        self.assertEqual(overflow.data["code"], "HOMEWORK_MEDIA_LIMIT")

    @patch("apps.domains.submissions.services.homework_media.upload_fileobj_to_r2")
    def test_cross_enrollment_upload_is_denied(
        self,
        upload_fileobj_to_r2,
    ):
        other_user = User.objects.create_user(
            username="homework-media-other",
            password="pw1234",
            tenant=self.tenant,
        )
        other_student = create_student_fixture(
            tenant=self.tenant,
            user=other_user,
            ps_number="HWM002",
            omr_code="27182818",
            name="다른 학생",
            phone="01055556666",
            parent_phone="01077778888",
        )
        other_enrollment = create_enrollment_fixture(
            tenant=self.tenant,
            student=other_student,
            lecture=self.lecture,
            status="ACTIVE",
        )
        path = f"/api/v1/submissions/submissions/homework/{self.homework.id}/media/"
        request = self._request(
            "post",
            path,
            data={
                "enrollment_id": other_enrollment.id,
                "client_file_id": str(uuid.uuid4()),
                "upload_batch_id": str(uuid.uuid4()),
                "position": 0,
                "file": _jpeg(),
            },
        )
        response = HomeworkSubmissionMediaCollectionView.as_view()(
            request,
            homework_id=self.homework.id,
        )

        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(Submission.objects.count(), 0)

    @patch("apps.domains.submissions.services.homework_media.upload_fileobj_to_r2")
    def test_student_remove_is_soft_and_restores_not_submitted_projection(
        self,
        upload_fileobj_to_r2,
    ):
        created = self._post(file=_jpeg())
        submission_id = created.data["id"]

        request = self._request(
            "delete",
            f"/api/v1/submissions/submissions/homework/{self.homework.id}/media/{submission_id}/",
            data={"enrollment_id": self.enrollment.id},
        )
        response = HomeworkSubmissionMediaDetailView.as_view()(
            request,
            homework_id=self.homework.id,
            media_id=str(submission_id),
        )

        self.assertEqual(response.status_code, 204, getattr(response, "data", None))
        media = SubmissionMedia.objects.get(id=submission_id)
        self.assertIsNotNone(media.removed_at)
        self.assertEqual(media.removed_by_id, self.student_user.id)
        self.assertTrue(media.object_key)
        self.assertEqual(
            submitted_homework_keys_for_grades(
                tenant=self.tenant,
                enrollment_ids=[self.enrollment.id],
                homework_ids=[self.homework.id],
            ),
            set(),
        )

    @patch(
        "apps.domains.submissions.views.homework_submission_media_view.generate_presigned_get_url",
        return_value="https://preview.invalid/legacy",
    )
    def test_existing_single_file_submission_is_projected_and_previewable(self, presign):
        submission = Submission.objects.create(
            tenant=self.tenant,
            user=self.student_user,
            enrollment=self.enrollment,
            target_type=Submission.TargetType.HOMEWORK,
            target_id=self.homework.id,
            source=Submission.Source.HOMEWORK_IMAGE,
            file_key="tenants/1/ai/submissions/legacy/page.jpg",
            file_type="image/jpeg",
            file_size=2345,
            status=Submission.Status.SUBMITTED,
            meta={"original_filename": "기존 풀이.jpg"},
        )
        request = self._request(
            "get",
            f"/api/v1/submissions/submissions/homework/{self.homework.id}/media/",
            data={"enrollment_id": self.enrollment.id},
        )
        response = HomeworkSubmissionMediaCollectionView.as_view()(
            request,
            homework_id=self.homework.id,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data["files"]), 1)
        self.assertEqual(response.data["files"][0]["id"], f"legacy-{submission.id}")
        self.assertEqual(response.data["files"][0]["status"], SubmissionMedia.Status.UPLOADED)
        self.assertEqual(response.data["files"][0]["original_filename"], "기존 풀이.jpg")
        self.assertEqual(SubmissionMedia.objects.count(), 0)

        preview_request = self._request(
            "get",
            f"/api/v1/submissions/submissions/homework/{self.homework.id}/media/legacy-{submission.id}/preview/",
            user=self.teacher,
        )
        preview = HomeworkSubmissionMediaPreviewView.as_view()(
            preview_request,
            homework_id=self.homework.id,
            media_id=f"legacy-{submission.id}",
        )
        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertEqual(preview.data["url"], "https://preview.invalid/legacy")
        presign.assert_called_once_with(key=submission.file_key, expires_in=600)

    @patch(
        "apps.domains.submissions.views.homework_submission_media_view.generate_presigned_get_url"
    )
    def test_removed_legacy_file_is_not_presigned_for_teacher(self, presign):
        submission = Submission.objects.create(
            tenant=self.tenant,
            user=self.student_user,
            enrollment=self.enrollment,
            target_type=Submission.TargetType.HOMEWORK,
            target_id=self.homework.id,
            source=Submission.Source.HOMEWORK_IMAGE,
            file_key="tenants/1/ai/submissions/legacy/removed.jpg",
            file_type="image/jpeg",
            file_size=2345,
            status=Submission.Status.SUBMITTED,
            meta={"homework_media_legacy_removed_at": "2026-08-23T00:00:00Z"},
        )
        request = self._request(
            "get",
            f"/api/v1/submissions/submissions/homework/{self.homework.id}/media/legacy-{submission.id}/preview/",
            user=self.teacher,
        )

        response = HomeworkSubmissionMediaPreviewView.as_view()(
            request,
            homework_id=self.homework.id,
            media_id=f"legacy-{submission.id}",
        )

        self.assertEqual(response.status_code, 404, response.data)
        presign.assert_not_called()

    @patch(
        "apps.domains.submissions.views.homework_submission_media_view.generate_presigned_get_url"
    )
    def test_student_preview_requires_current_active_enrollment(self, presign):
        submission = Submission.objects.create(
            tenant=self.tenant,
            user=self.student_user,
            enrollment=self.enrollment,
            target_type=Submission.TargetType.HOMEWORK,
            target_id=self.homework.id,
            source=Submission.Source.HOMEWORK_IMAGE,
            status=Submission.Status.SUBMITTED,
        )
        media = SubmissionMedia.objects.create(
            tenant=self.tenant,
            submission=submission,
            status=SubmissionMedia.Status.UPLOADED,
            object_key="tenants/1/ai/submissions/1/inactive.jpg",
            fingerprint="d" * 64,
            media_kind=SubmissionMedia.Kind.IMAGE,
            mime_type="image/jpeg",
            size=4567,
            original_filename="지난 풀이.jpg",
            client_upload_id=uuid.uuid4(),
            upload_batch_id=uuid.uuid4(),
            position=0,
        )
        self.enrollment.status = "INACTIVE"
        self.enrollment.save(update_fields=["status", "updated_at"])
        request = self._request(
            "get",
            f"/api/v1/submissions/submissions/homework/{self.homework.id}/media/{media.id}/preview/",
        )

        response = HomeworkSubmissionMediaPreviewView.as_view()(
            request,
            homework_id=self.homework.id,
            media_id=str(media.id),
        )

        self.assertEqual(response.status_code, 403, response.data)
        presign.assert_not_called()

    @patch("apps.domains.submissions.services.homework_media.upload_fileobj_to_r2")
    def test_student_remove_is_locked_after_teacher_score(
        self,
        upload_fileobj_to_r2,
    ):
        created = self._post(file=_jpeg())
        create_homework_score_fixture(
            enrollment=self.enrollment,
            session=self.session,
            homework=self.homework,
            score=10,
            max_score=10,
            passed=True,
            attempt_index=1,
        )
        request = self._request(
            "delete",
            f"/api/v1/submissions/submissions/homework/{self.homework.id}/media/{created.data['id']}/",
            data={"enrollment_id": self.enrollment.id},
        )
        response = HomeworkSubmissionMediaDetailView.as_view()(
            request,
            homework_id=self.homework.id,
            media_id=str(created.data["id"]),
        )

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["code"], "HOMEWORK_MEDIA_REVIEWED")

    @patch("apps.domains.submissions.services.homework_media.upload_fileobj_to_r2")
    def test_student_upload_is_locked_after_passing_teacher_score(
        self,
        upload_fileobj_to_r2,
    ):
        Submission.objects.create(
            tenant=self.tenant,
            user=self.student_user,
            enrollment=self.enrollment,
            target_type=Submission.TargetType.HOMEWORK,
            target_id=self.homework.id,
            source=Submission.Source.HOMEWORK_IMAGE,
            status=Submission.Status.SUBMITTED,
        )
        create_homework_score_fixture(
            enrollment=self.enrollment,
            session=self.session,
            homework=self.homework,
            score=10,
            max_score=10,
            passed=True,
            attempt_index=1,
        )
        submission_count = Submission.objects.count()
        media_count = SubmissionMedia.objects.count()

        response = self._post(file=_jpeg("reviewed-score.jpg"))

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["code"], "HOMEWORK_MEDIA_REVIEWED")
        upload_fileobj_to_r2.assert_not_called()
        self.assertEqual(Submission.objects.count(), submission_count)
        self.assertEqual(SubmissionMedia.objects.count(), media_count)

    @patch("apps.domains.submissions.services.homework_media.upload_fileobj_to_r2")
    def test_latest_failed_score_keeps_student_upload_open_after_earlier_pass_and_manual_review(
        self,
        upload_fileobj_to_r2,
    ):
        AssessmentCorrection = django_apps.get_model("progress", "AssessmentCorrection")
        create_homework_score_fixture(
            enrollment=self.enrollment,
            session=self.session,
            homework=self.homework,
            score=10,
            max_score=10,
            passed=True,
            attempt_index=1,
        )
        create_homework_score_fixture(
            enrollment=self.enrollment,
            session=self.session,
            homework=self.homework,
            score=4,
            max_score=10,
            passed=False,
            attempt_index=2,
        )
        AssessmentCorrection.objects.create(
            tenant=self.tenant,
            enrollment=self.enrollment,
            session=self.session,
            source_type=AssessmentCorrection.SourceType.HOMEWORK,
            source_id=self.homework.id,
            completed=True,
            updated_by=self.teacher,
        )

        response = self._post(file=_jpeg("failed-retry-supplement.jpg"))

        self.assertEqual(response.status_code, 201, response.data)
        upload_fileobj_to_r2.assert_called_once()
        self.assertTrue(
            SubmissionMedia.objects.filter(id=int(response.data["id"])).exists()
        )

    @patch("apps.domains.submissions.services.homework_media.upload_fileobj_to_r2")
    def test_not_submitted_score_keeps_student_remove_open(
        self,
        upload_fileobj_to_r2,
    ):
        created = self._post(file=_jpeg("not-submitted.jpg"))
        create_homework_score_fixture(
            enrollment=self.enrollment,
            session=self.session,
            homework=self.homework,
            score=None,
            max_score=10,
            passed=False,
            attempt_index=1,
            meta={"status": "NOT_SUBMITTED"},
        )
        request = self._request(
            "delete",
            f"/api/v1/submissions/submissions/homework/{self.homework.id}/media/{created.data['id']}/",
            data={"enrollment_id": self.enrollment.id},
        )

        response = HomeworkSubmissionMediaDetailView.as_view()(
            request,
            homework_id=self.homework.id,
            media_id=str(created.data["id"]),
        )

        self.assertEqual(response.status_code, 204, response.data)
        self.assertIsNotNone(
            SubmissionMedia.objects.get(id=int(created.data["id"])).removed_at
        )

    @patch("apps.domains.submissions.services.homework_media.upload_fileobj_to_r2")
    def test_student_upload_is_locked_after_completed_correction_with_historic_parent(
        self,
        upload_fileobj_to_r2,
    ):
        AssessmentCorrection = django_apps.get_model("progress", "AssessmentCorrection")
        Submission.objects.create(
            tenant=self.tenant,
            user=self.student_user,
            enrollment=self.enrollment,
            target_type=Submission.TargetType.HOMEWORK,
            target_id=self.homework.id,
            source=Submission.Source.HOMEWORK_IMAGE,
            status=Submission.Status.DONE,
        )
        AssessmentCorrection.objects.create(
            tenant=self.tenant,
            enrollment=self.enrollment,
            session=self.session,
            source_type=AssessmentCorrection.SourceType.HOMEWORK,
            source_id=self.homework.id,
            completed=True,
            updated_by=self.teacher,
        )
        submission_count = Submission.objects.count()
        media_count = SubmissionMedia.objects.count()

        response = self._post(file=_jpeg("reviewed-correction.jpg"))

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["code"], "HOMEWORK_MEDIA_REVIEWED")
        upload_fileobj_to_r2.assert_not_called()
        self.assertEqual(Submission.objects.count(), submission_count)
        self.assertEqual(SubmissionMedia.objects.count(), media_count)

    @patch("apps.domains.submissions.services.homework_media.upload_fileobj_to_r2")
    def test_upload_reload_teacher_view_grade_then_retry_and_delete_are_denied(
        self,
        upload_fileobj_to_r2,
    ):
        client_file_id = str(uuid.uuid4())
        created = self._post(
            file=_jpeg("positive-journey.jpg", body=b"positive-journey"),
            client_file_id=client_file_id,
        )
        self.assertEqual(created.status_code, 201, created.data)

        reload_request = self._request(
            "get",
            f"/api/v1/submissions/submissions/homework/{self.homework.id}/media/",
            data={"enrollment_id": self.enrollment.id},
        )
        reloaded = HomeworkSubmissionMediaCollectionView.as_view()(
            reload_request,
            homework_id=self.homework.id,
        )
        self.assertEqual(reloaded.status_code, 200, reloaded.data)
        self.assertEqual([item["id"] for item in reloaded.data["files"]], [created.data["id"]])

        teacher_request = self._request(
            "get",
            f"/api/v1/submissions/submissions/homework/{self.homework.id}/",
            user=self.teacher,
        )
        teacher_view = HomeworkSubmissionsListView.as_view()(
            teacher_request,
            homework_id=self.homework.id,
        )
        self.assertEqual(teacher_view.status_code, 200, teacher_view.data)
        viewed_fingerprint = teacher_view.data[0]["media_set_fingerprint"]
        self.assertEqual(len(viewed_fingerprint), 64)

        grade_request = self._request(
            "patch",
            f"/api/v1/results/admin/sessions/{self.session.id}/score-correction/",
            user=self.teacher,
            data={
                "enrollment_id": self.enrollment.id,
                "source_type": "homework",
                "source_id": self.homework.id,
                "completed": True,
                "note": "제출 파일 확인 완료",
                "expected_updated_at": None,
            },
        )
        graded = SessionScoreCorrectionView.as_view()(
            grade_request,
            session_id=self.session.id,
        )
        self.assertEqual(graded.status_code, 200, graded.data)
        AssessmentCorrection = django_apps.get_model("progress", "AssessmentCorrection")
        correction = AssessmentCorrection.objects.get(
            tenant=self.tenant,
            enrollment=self.enrollment,
            session=self.session,
            source_type=AssessmentCorrection.SourceType.HOMEWORK,
            source_id=self.homework.id,
        )
        self.assertEqual(correction.source_fingerprint, viewed_fingerprint)

        retry = self._post(
            file=_jpeg("positive-journey.jpg", body=b"positive-journey"),
            client_file_id=client_file_id,
        )
        delete_request = self._request(
            "delete",
            f"/api/v1/submissions/submissions/homework/{self.homework.id}/media/{created.data['id']}/",
            data={"enrollment_id": self.enrollment.id},
        )
        deleted = HomeworkSubmissionMediaDetailView.as_view()(
            delete_request,
            homework_id=self.homework.id,
            media_id=str(created.data["id"]),
        )

        self.assertEqual(retry.status_code, 409, retry.data)
        self.assertEqual(retry.data["code"], "HOMEWORK_MEDIA_REVIEWED")
        self.assertEqual(deleted.status_code, 409, deleted.data)
        self.assertEqual(deleted.data["code"], "HOMEWORK_MEDIA_REVIEWED")
        upload_fileobj_to_r2.assert_called_once()
        media = SubmissionMedia.objects.get(id=int(created.data["id"]))
        self.assertEqual(media.status, SubmissionMedia.Status.UPLOADED)
        self.assertIsNone(media.removed_at)

    @patch("apps.domains.submissions.services.homework_media.upload_fileobj_to_r2")
    def test_student_cannot_remove_another_students_file(
        self,
        upload_fileobj_to_r2,
    ):
        created = self._post(file=_jpeg())
        other_user = User.objects.create_user(
            username="homework-media-delete-other",
            password="pw1234",
            tenant=self.tenant,
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=other_user,
            role="student",
        )
        other_student = create_student_fixture(
            tenant=self.tenant,
            user=other_user,
            ps_number="HWM003",
            omr_code="16180339",
            name="삭제 시도 학생",
            phone="01088889999",
            parent_phone="01099990000",
        )
        other_enrollment = create_enrollment_fixture(
            tenant=self.tenant,
            student=other_student,
            lecture=self.lecture,
            status="ACTIVE",
        )
        create_homework_assignment_fixture(
            tenant=self.tenant,
            homework=self.homework,
            session=self.session,
            enrollment=other_enrollment,
        )
        request = self._request(
            "delete",
            f"/api/v1/submissions/submissions/homework/{self.homework.id}/media/{created.data['id']}/",
            user=other_user,
            data={"enrollment_id": other_enrollment.id},
        )

        response = HomeworkSubmissionMediaDetailView.as_view()(
            request,
            homework_id=self.homework.id,
            media_id=str(created.data["id"]),
        )

        self.assertEqual(response.status_code, 404, response.data)
        self.assertIsNone(SubmissionMedia.objects.get(id=created.data["id"]).removed_at)

    @patch(
        "apps.domains.submissions.views.homework_submission_media_view.generate_presigned_get_url",
        return_value="https://preview.invalid/file",
    )
    def test_teacher_gets_per_file_status_error_and_authorized_preview(self, presign):
        submission = Submission.objects.create(
            tenant=self.tenant,
            user=self.student_user,
            enrollment=self.enrollment,
            target_type=Submission.TargetType.HOMEWORK,
            target_id=self.homework.id,
            source=Submission.Source.HOMEWORK_IMAGE,
            status=Submission.Status.SUBMITTED,
        )
        failed_media = SubmissionMedia.objects.create(
            tenant=self.tenant,
            submission=submission,
            status=SubmissionMedia.Status.FAILED,
            object_key="tenants/1/ai/submissions/1/video.mp4",
            fingerprint="a" * 64,
            media_kind=SubmissionMedia.Kind.VIDEO,
            mime_type="video/mp4",
            size=1234,
            original_filename="풀이 영상.mp4",
            client_upload_id=uuid.uuid4(),
            upload_batch_id=uuid.uuid4(),
            position=2,
            error_message="analysis failed",
        )
        media = SubmissionMedia.objects.create(
            tenant=self.tenant,
            submission=submission,
            status=SubmissionMedia.Status.UPLOADED,
            object_key="tenants/1/ai/submissions/1/image.jpg",
            fingerprint="b" * 64,
            media_kind=SubmissionMedia.Kind.IMAGE,
            mime_type="image/jpeg",
            size=4567,
            original_filename="풀이 사진.jpg",
            client_upload_id=uuid.uuid4(),
            upload_batch_id=uuid.uuid4(),
            position=3,
        )
        list_request = self._request(
            "get",
            f"/api/v1/submissions/submissions/homework/{self.homework.id}/",
            user=self.teacher,
        )
        listed = HomeworkSubmissionsListView.as_view()(
            list_request,
            homework_id=self.homework.id,
        )

        self.assertEqual(listed.status_code, 200, listed.data)
        self.assertEqual(listed.data[0]["files"][0]["original_filename"], "풀이 영상.mp4")
        self.assertEqual(listed.data[0]["files"][0]["media_kind"], "video")
        self.assertEqual(listed.data[0]["files"][0]["position"], 2)
        self.assertEqual(listed.data[0]["files"][0]["error_message"], "analysis failed")
        self.assertNotIn("object_key", listed.data[0]["files"][0])
        self.assertNotIn("file_key", listed.data[0])
        self.assertFalse(listed.data[0]["teacher_reviewed"])
        self.assertIsNone(listed.data[0]["teacher_review_source"])

        preview_request = self._request(
            "get",
            f"/api/v1/submissions/submissions/homework/{self.homework.id}/media/{media.id}/preview/",
            user=self.teacher,
        )
        preview = HomeworkSubmissionMediaPreviewView.as_view()(
            preview_request,
            homework_id=self.homework.id,
            media_id=str(media.id),
        )

        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertEqual(preview.data["url"], "https://preview.invalid/file")
        self.assertEqual(preview.data["media_kind"], "image")
        presign.assert_called_once_with(key=media.object_key, expires_in=600)

        failed_request = self._request(
            "get",
            f"/api/v1/submissions/submissions/homework/{self.homework.id}/media/{failed_media.id}/preview/",
            user=self.teacher,
        )
        failed_preview = HomeworkSubmissionMediaPreviewView.as_view()(
            failed_request,
            homework_id=self.homework.id,
            media_id=str(failed_media.id),
        )
        self.assertEqual(failed_preview.status_code, 409, failed_preview.data)

        AssessmentCorrection = django_apps.get_model("progress", "AssessmentCorrection")
        correction = AssessmentCorrection.objects.create(
            tenant=self.tenant,
            enrollment=self.enrollment,
            session=self.session,
            source_type=AssessmentCorrection.SourceType.HOMEWORK,
            source_id=self.homework.id,
            completed=True,
            completed_at=timezone.now(),
            note="제출 파일 직접 확인",
            updated_by=self.teacher,
        )
        reviewed_request = self._request(
            "get",
            f"/api/v1/submissions/submissions/homework/{self.homework.id}/",
            user=self.teacher,
        )
        reviewed = HomeworkSubmissionsListView.as_view()(
            reviewed_request,
            homework_id=self.homework.id,
        )
        self.assertEqual(reviewed.status_code, 200, reviewed.data)
        self.assertTrue(reviewed.data[0]["teacher_reviewed"])
        self.assertEqual(reviewed.data[0]["teacher_review_source"], "manual")
        self.assertEqual(reviewed.data[0]["teacher_review_note"], "제출 파일 직접 확인")
        self.assertEqual(
            reviewed.data[0]["teacher_review_updated_at"],
            correction.updated_at.isoformat(),
        )

        failed_score = create_homework_score_fixture(
            enrollment=self.enrollment,
            session=self.session,
            homework=self.homework,
            score=4,
            max_score=10,
            passed=False,
            attempt_index=1,
        )
        failed_request = self._request(
            "get",
            f"/api/v1/submissions/submissions/homework/{self.homework.id}/",
            user=self.teacher,
        )
        failed = HomeworkSubmissionsListView.as_view()(
            failed_request,
            homework_id=self.homework.id,
        )
        self.assertEqual(failed.status_code, 200, failed.data)
        self.assertFalse(failed.data[0]["teacher_reviewed"])
        self.assertIsNone(failed.data[0]["teacher_review_source"])

        failed_score.score = 10
        failed_score.passed = True
        failed_score.save(update_fields=["score", "passed", "updated_at"])
        scored_request = self._request(
            "get",
            f"/api/v1/submissions/submissions/homework/{self.homework.id}/",
            user=self.teacher,
        )
        scored = HomeworkSubmissionsListView.as_view()(
            scored_request,
            homework_id=self.homework.id,
        )
        self.assertEqual(scored.status_code, 200, scored.data)
        self.assertEqual(scored.data[0]["teacher_review_source"], "score")

    @patch("apps.domains.submissions.services.homework_media.upload_fileobj_to_r2")
    def test_homework_media_stays_manual_and_is_excluded_from_auto_inbox(
        self,
        upload_fileobj_to_r2,
    ):
        created = self._post(file=_jpeg("manual-review.jpg"))
        self.assertEqual(created.status_code, 201, created.data)
        parent = Submission.objects.get(media_files__id=int(created.data["id"]))

        with (
            patch.object(dispatcher, "dispatch_job") as dispatch_job,
            patch.object(dispatcher, "start_ai_worker_instance") as start_worker,
        ):
            dispatcher.dispatch_submission(parent)

        parent.refresh_from_db()
        self.assertEqual(parent.status, Submission.Status.SUBMITTED)
        dispatch_job.assert_not_called()
        start_worker.assert_not_called()

        request = self._request(
            "get",
            "/api/v1/submissions/submissions/pending/?filter=pending",
            user=self.teacher,
        )
        response = PendingSubmissionsView.as_view()(request)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data, [])

    @patch(
        "apps.domains.submissions.views.homework_submission_media_view.generate_presigned_get_url",
        return_value="https://preview.invalid/file",
    )
    def test_teacher_cannot_preview_another_tenant_file(self, presign):
        submission = Submission.objects.create(
            tenant=self.tenant,
            user=self.student_user,
            enrollment=self.enrollment,
            target_type=Submission.TargetType.HOMEWORK,
            target_id=self.homework.id,
            source=Submission.Source.HOMEWORK_IMAGE,
            status=Submission.Status.SUBMITTED,
        )
        media = SubmissionMedia.objects.create(
            tenant=self.tenant,
            submission=submission,
            status=SubmissionMedia.Status.UPLOADED,
            object_key="tenants/1/ai/submissions/1/image.jpg",
            fingerprint="c" * 64,
            media_kind=SubmissionMedia.Kind.IMAGE,
            mime_type="image/jpeg",
            size=4567,
            original_filename="풀이 사진.jpg",
            client_upload_id=uuid.uuid4(),
            upload_batch_id=uuid.uuid4(),
            position=0,
        )
        other_tenant = Tenant.objects.create(
            code="homework-media-other-tenant",
            name="Other Tenant",
            is_active=True,
        )
        other_teacher = User.objects.create_user(
            username="homework-media-other-teacher",
            password="pw1234",
            tenant=other_tenant,
        )
        TenantMembership.ensure_active(
            tenant=other_tenant,
            user=other_teacher,
            role="teacher",
        )
        request = self.factory.get(
            f"/api/v1/submissions/submissions/homework/{self.homework.id}/media/{media.id}/preview/"
        )
        request.tenant = other_tenant
        force_authenticate(request, user=other_teacher)

        response = HomeworkSubmissionMediaPreviewView.as_view()(
            request,
            homework_id=self.homework.id,
            media_id=str(media.id),
        )

        self.assertEqual(response.status_code, 404, response.data)
        presign.assert_not_called()
