"""PostgreSQL concurrency coverage for score-preserving teacher decisions."""

from __future__ import annotations

import threading
import time
import unittest
import uuid
from unittest.mock import patch

import pytest
from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.core.models import Tenant, TenantMembership
from apps.domains.results.views.session_scores_view import SessionScoreCorrectionView
from apps.domains.submissions.views.homework_submission_media_view import (
    HomeworkSubmissionMediaCollectionView,
    HomeworkSubmissionMediaDetailView,
)
from apps.domains.submissions.views.homework_submissions_list_view import (
    HomeworkSubmissionsListView,
)


pytestmark = pytest.mark.django_db(transaction=True)
User = get_user_model()
Attendance = apps.get_model("attendance", "Attendance")
Enrollment = apps.get_model("enrollment", "Enrollment")
SessionEnrollment = apps.get_model("enrollment", "SessionEnrollment")
HomeworkAssignment = apps.get_model("homework", "HomeworkAssignment")
Homework = apps.get_model("homework_results", "Homework")
Lecture = apps.get_model("lectures", "Lecture")
Session = apps.get_model("lectures", "Session")
AssessmentCorrection = apps.get_model("progress", "AssessmentCorrection")
Submission = apps.get_model("submissions", "Submission")
SubmissionMedia = apps.get_model("submissions", "SubmissionMedia")
Student = apps.get_model("students", "Student")


class AssessmentCorrectionConcurrencyPGTests(TransactionTestCase):
    """An unscored homework decision still has a row lock for first-write CAS."""

    @classmethod
    def setUpClass(cls):
        if connection.vendor != "postgresql":
            raise unittest.SkipTest(
                "PostgreSQL row-level locking is required for this regression test."
            )
        super().setUpClass()

    def setUp(self):
        suffix = uuid.uuid4().hex[:8]
        self.tenant = Tenant.objects.create(
            name=f"Correction Lock {suffix}",
            code=f"correction_lock_{suffix}",
            is_active=True,
        )
        self.admin = User.objects.create_user(
            username=f"correction-lock-{suffix}",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=self.admin,
            role="admin",
        )
        lecture = Lecture.objects.create(
            tenant=self.tenant,
            title="Correction Lock Lecture",
            name="Correction Lock Lecture",
            subject="MATH",
        )
        self.session = Session.objects.create(
            lecture=lecture,
            order=1,
            title="Session 1",
        )
        self.student_user = User.objects.create_user(
            username=f"correction-student-{suffix}",
            tenant=self.tenant,
        )
        student = Student.objects.create(
            tenant=self.tenant,
            user=self.student_user,
            name="Correction Student",
            ps_number=f"P{suffix}",
        )
        self.enrollment = Enrollment.objects.create(
            tenant=self.tenant,
            student=student,
            lecture=lecture,
            status="ACTIVE",
        )
        SessionEnrollment.objects.create(
            tenant=self.tenant,
            session=self.session,
            enrollment=self.enrollment,
        )
        Attendance.objects.create(
            tenant=self.tenant,
            session=self.session,
            enrollment=self.enrollment,
            status="PRESENT",
        )
        self.homework = Homework.objects.create(
            tenant=self.tenant,
            session=self.session,
            title="Unscored Homework",
        )
        self.assignment = HomeworkAssignment.objects.create(
            tenant=self.tenant,
            homework=self.homework,
            session=self.session,
            enrollment=self.enrollment,
        )

    def _student_upload(self, *, client_file_id: str | None = None):
        request = APIRequestFactory().post(
            f"/api/v1/submissions/submissions/homework/{self.homework.id}/media/",
            {
                "enrollment_id": self.enrollment.id,
                "client_file_id": client_file_id or str(uuid.uuid4()),
                "upload_batch_id": str(uuid.uuid4()),
                "position": 0,
                "file": SimpleUploadedFile(
                    "proof.jpg",
                    b"\xff\xd8\xff\xe0proof",
                    content_type="image/jpeg",
                ),
            },
            format="multipart",
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.student_user)
        return HomeworkSubmissionMediaCollectionView.as_view()(
            request,
            homework_id=self.homework.id,
        )

    def _student_delete(self, *, media_id: int):
        request = APIRequestFactory().delete(
            f"/api/v1/submissions/submissions/homework/{self.homework.id}/media/{media_id}/",
            {"enrollment_id": self.enrollment.id},
            format="json",
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.student_user)
        return HomeworkSubmissionMediaDetailView.as_view()(
            request,
            homework_id=self.homework.id,
            media_id=str(media_id),
        )

    def _teacher_complete(self):
        request = APIRequestFactory().patch(
            f"/api/v1/results/admin/sessions/{self.session.id}/score-correction/",
            {
                "enrollment_id": self.enrollment.id,
                "source_type": "homework",
                "source_id": self.homework.id,
                "completed": True,
                "note": "제출 파일 확인 완료",
                "expected_updated_at": None,
            },
            format="json",
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)
        return SessionScoreCorrectionView.as_view()(
            request,
            session_id=self.session.id,
        )

    def _teacher_submission_list(self):
        request = APIRequestFactory().get(
            f"/api/v1/submissions/submissions/homework/{self.homework.id}/"
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)
        return HomeworkSubmissionsListView.as_view()(
            request,
            homework_id=self.homework.id,
        )

    def test_first_unscored_homework_decision_honors_expected_updated_at(self):
        correction_created = threading.Event()
        competing_request_started = threading.Event()
        statuses: list[int] = []
        errors: list[str] = []

        def first_writer() -> None:
            close_old_connections()
            try:
                with transaction.atomic():
                    HomeworkAssignment.objects.select_for_update().get(
                        id=self.assignment.id
                    )
                    AssessmentCorrection.objects.create(
                        tenant_id=self.tenant.id,
                        enrollment_id=self.enrollment.id,
                        session_id=self.session.id,
                        source_type=AssessmentCorrection.SourceType.HOMEWORK,
                        source_id=self.homework.id,
                        completed=True,
                        note="현장 검사 완료",
                        updated_by_id=self.admin.id,
                    )
                    correction_created.set()
                    if not competing_request_started.wait(timeout=5):
                        raise AssertionError("competing request did not start")
                    time.sleep(0.2)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"first: {exc!r}")
            finally:
                close_old_connections()

        def second_writer() -> None:
            close_old_connections()
            try:
                if not correction_created.wait(timeout=5):
                    raise AssertionError("first correction was not created")
                request = APIRequestFactory().patch(
                    f"/api/v1/results/admin/sessions/{self.session.id}/score-correction/",
                    {
                        "enrollment_id": self.enrollment.id,
                        "source_type": "homework",
                        "source_id": self.homework.id,
                        "completed": False,
                        "expected_updated_at": None,
                    },
                    format="json",
                )
                request.tenant = self.tenant
                force_authenticate(request, user=self.admin)
                competing_request_started.set()
                response = SessionScoreCorrectionView.as_view()(
                    request,
                    session_id=self.session.id,
                )
                statuses.append(response.status_code)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"second: {exc!r}")
            finally:
                close_old_connections()

        first = threading.Thread(target=first_writer)
        second = threading.Thread(target=second_writer)
        first.start()
        second.start()
        first.join(timeout=10)
        second.join(timeout=10)

        self.assertFalse(first.is_alive(), "first writer did not finish")
        self.assertFalse(second.is_alive(), "second writer did not finish")
        self.assertEqual(errors, [])
        self.assertEqual(statuses, [409])
        correction = AssessmentCorrection.objects.get(
            tenant=self.tenant,
            enrollment=self.enrollment,
            session=self.session,
            source_type=AssessmentCorrection.SourceType.HOMEWORK,
            source_id=self.homework.id,
        )
        self.assertTrue(correction.completed)

    @patch("apps.domains.submissions.services.homework_media.upload_fileobj_to_r2")
    def test_teacher_completion_first_rejects_waiting_upload_and_delete(
        self,
        upload_fileobj_to_r2,
    ):
        parent = Submission.objects.create(
            tenant=self.tenant,
            user=self.student_user,
            enrollment=self.enrollment,
            target_type=Submission.TargetType.HOMEWORK,
            target_id=self.homework.id,
            source=Submission.Source.HOMEWORK_IMAGE,
            status=Submission.Status.SUBMITTED,
        )
        existing_media = SubmissionMedia.objects.create(
            tenant=self.tenant,
            submission=parent,
            client_upload_id=uuid.uuid4(),
            upload_batch_id=uuid.uuid4(),
            fingerprint="a" * 64,
            object_key="tenants/test/homework/existing.jpg",
            original_filename="existing.jpg",
            media_kind=SubmissionMedia.Kind.IMAGE,
            mime_type="image/jpeg",
            size=10,
            position=0,
            status=SubmissionMedia.Status.UPLOADED,
        )
        teacher_locked = threading.Event()
        upload_started = threading.Event()
        delete_started = threading.Event()
        statuses: list[int] = []
        errors: list[str] = []

        def teacher_writer() -> None:
            close_old_connections()
            try:
                with transaction.atomic():
                    HomeworkAssignment.objects.select_for_update().get(
                        id=self.assignment.id
                    )
                    AssessmentCorrection.objects.create(
                        tenant_id=self.tenant.id,
                        enrollment_id=self.enrollment.id,
                        session_id=self.session.id,
                        source_type=AssessmentCorrection.SourceType.HOMEWORK,
                        source_id=self.homework.id,
                        completed=True,
                        note="교사 선행 완료",
                        updated_by_id=self.admin.id,
                    )
                    teacher_locked.set()
                    if not upload_started.wait(timeout=5):
                        raise AssertionError("upload request did not start")
                    if not delete_started.wait(timeout=5):
                        raise AssertionError("delete request did not start")
                    time.sleep(0.2)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"teacher: {exc!r}")
            finally:
                close_old_connections()

        def upload_writer() -> None:
            close_old_connections()
            try:
                if not teacher_locked.wait(timeout=5):
                    raise AssertionError("teacher did not acquire the target lock")
                upload_started.set()
                statuses.append(self._student_upload().status_code)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"upload: {exc!r}")
            finally:
                close_old_connections()

        def delete_writer() -> None:
            close_old_connections()
            try:
                if not teacher_locked.wait(timeout=5):
                    raise AssertionError("teacher did not acquire the target lock")
                delete_started.set()
                statuses.append(
                    self._student_delete(media_id=existing_media.id).status_code
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"delete: {exc!r}")
            finally:
                close_old_connections()

        threads = [
            threading.Thread(target=teacher_writer),
            threading.Thread(target=upload_writer),
            threading.Thread(target=delete_writer),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(sorted(statuses), [409, 409])
        upload_fileobj_to_r2.assert_not_called()
        self.assertEqual(Submission.objects.count(), 1)
        self.assertEqual(SubmissionMedia.objects.count(), 1)
        existing_media.refresh_from_db()
        self.assertIsNone(existing_media.removed_at)

    @patch("apps.domains.submissions.services.homework_media.upload_fileobj_to_r2")
    def test_student_upload_first_blocks_teacher_until_exact_media_set_commits(
        self,
        upload_fileobj_to_r2,
    ):
        upload_entered = threading.Event()
        release_upload = threading.Event()
        teacher_started = threading.Event()
        teacher_finished = threading.Event()
        upload_statuses: list[int] = []
        teacher_statuses: list[int] = []
        errors: list[str] = []

        def blocking_upload(**_kwargs) -> None:
            upload_entered.set()
            if not release_upload.wait(timeout=5):
                raise AssertionError("upload was not released")

        upload_fileobj_to_r2.side_effect = blocking_upload

        def student_writer() -> None:
            close_old_connections()
            try:
                upload_statuses.append(self._student_upload().status_code)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"student: {exc!r}")
            finally:
                close_old_connections()

        def teacher_writer() -> None:
            close_old_connections()
            try:
                if not upload_entered.wait(timeout=5):
                    raise AssertionError("student upload did not reach object storage")
                teacher_started.set()
                teacher_statuses.append(self._teacher_complete().status_code)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"teacher: {exc!r}")
            finally:
                teacher_finished.set()
                close_old_connections()

        student = threading.Thread(target=student_writer)
        teacher = threading.Thread(target=teacher_writer)
        student.start()
        self.assertTrue(upload_entered.wait(timeout=5))
        teacher.start()
        self.assertTrue(teacher_started.wait(timeout=5))
        try:
            self.assertFalse(
                teacher_finished.wait(timeout=0.3),
                "teacher completion crossed an in-flight student upload",
            )
        finally:
            release_upload.set()
        student.join(timeout=10)
        teacher.join(timeout=10)

        self.assertFalse(student.is_alive())
        self.assertFalse(teacher.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(upload_statuses, [201])
        self.assertEqual(teacher_statuses, [200])
        correction = AssessmentCorrection.objects.get(
            tenant=self.tenant,
            enrollment=self.enrollment,
            session=self.session,
            source_type=AssessmentCorrection.SourceType.HOMEWORK,
            source_id=self.homework.id,
        )
        listed = self._teacher_submission_list()
        self.assertEqual(listed.status_code, 200, listed.data)
        self.assertEqual(len(correction.source_fingerprint or ""), 64)
        self.assertEqual(
            correction.source_fingerprint,
            listed.data[0]["media_set_fingerprint"],
        )

    def test_student_delete_first_blocks_teacher_until_exact_media_set_commits(self):
        parent = Submission.objects.create(
            tenant=self.tenant,
            user=self.student_user,
            enrollment=self.enrollment,
            target_type=Submission.TargetType.HOMEWORK,
            target_id=self.homework.id,
            source=Submission.Source.HOMEWORK_IMAGE,
            status=Submission.Status.SUBMITTED,
        )
        existing_media = SubmissionMedia.objects.create(
            tenant=self.tenant,
            submission=parent,
            client_upload_id=uuid.uuid4(),
            upload_batch_id=uuid.uuid4(),
            fingerprint="b" * 64,
            object_key="tenants/test/homework/delete.jpg",
            original_filename="delete.jpg",
            media_kind=SubmissionMedia.Kind.IMAGE,
            mime_type="image/jpeg",
            size=10,
            position=0,
            status=SubmissionMedia.Status.UPLOADED,
        )
        delete_entered = threading.Event()
        release_delete = threading.Event()
        teacher_started = threading.Event()
        teacher_finished = threading.Event()
        delete_statuses: list[int] = []
        teacher_statuses: list[int] = []
        errors: list[str] = []
        original_save = SubmissionMedia.save

        def blocking_save(instance, *args, **kwargs):
            delete_entered.set()
            if not release_delete.wait(timeout=5):
                raise AssertionError("delete was not released")
            return original_save(instance, *args, **kwargs)

        def student_writer() -> None:
            close_old_connections()
            try:
                delete_statuses.append(
                    self._student_delete(media_id=existing_media.id).status_code
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"student: {exc!r}")
            finally:
                close_old_connections()

        def teacher_writer() -> None:
            close_old_connections()
            try:
                if not delete_entered.wait(timeout=5):
                    raise AssertionError("student delete did not reach persistence")
                teacher_started.set()
                teacher_statuses.append(self._teacher_complete().status_code)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"teacher: {exc!r}")
            finally:
                teacher_finished.set()
                close_old_connections()

        with patch.object(SubmissionMedia, "save", new=blocking_save):
            student = threading.Thread(target=student_writer)
            teacher = threading.Thread(target=teacher_writer)
            student.start()
            self.assertTrue(delete_entered.wait(timeout=5))
            teacher.start()
            self.assertTrue(teacher_started.wait(timeout=5))
            try:
                self.assertFalse(
                    teacher_finished.wait(timeout=0.3),
                    "teacher completion crossed an in-flight student delete",
                )
            finally:
                release_delete.set()
            student.join(timeout=10)
            teacher.join(timeout=10)

        self.assertFalse(student.is_alive())
        self.assertFalse(teacher.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(delete_statuses, [204])
        self.assertEqual(teacher_statuses, [200])
        existing_media.refresh_from_db()
        self.assertIsNotNone(existing_media.removed_at)
        correction = AssessmentCorrection.objects.get(
            tenant=self.tenant,
            enrollment=self.enrollment,
            session=self.session,
            source_type=AssessmentCorrection.SourceType.HOMEWORK,
            source_id=self.homework.id,
        )
        listed = self._teacher_submission_list()
        self.assertEqual(listed.status_code, 200, listed.data)
        self.assertEqual(len(correction.source_fingerprint or ""), 64)
        self.assertEqual(
            correction.source_fingerprint,
            listed.data[0]["media_set_fingerprint"],
        )
