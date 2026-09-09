import uuid
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

# PATH: apps/domains/students/tests/test_bulk_permanent_delete_tenant_isolation.py
"""
크로스테넌트 보호 증명 테스트 — bulk_permanent_delete

시나리오: User X가 Tenant A(학생), Tenant B(teacher Membership + Submission).
  Tenant A에서 영구삭제 시 Tenant B 데이터 보존 증명.
"""
from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.core.management import call_command
from django.db import transaction
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken

from apps.core.models import PendingPasswordReset
from apps.core.models.tenant import Tenant
from apps.core.models.tenant_membership import TenantMembership
from apps.core.services.password import create_pending_password_reset
from apps.domains.enrollment.models import Enrollment
from apps.domains.fees.models import FeePayment, FeeTemplate, InvoiceItem, StudentFee, StudentInvoice
from apps.domains.lectures.models import Lecture
from apps.domains.lectures.models import Section, SectionAssignment
from apps.domains.parents.models import Parent
from apps.domains.clinic.models import SessionParticipant
from apps.domains.submissions.models import (
    OMRDetectedAnswer,
    OMRRecognitionRun,
    OMRStudentMatch,
    OmrUploadBatch,
    OmrUploadBatchItem,
    Submission,
    SubmissionMedia,
)
from apps.domains.students.models import Student, StudentSupportSession
from apps.domains.students.services import (
    StudentLifecycleError,
    permanently_delete_students,
    soft_delete_student,
)
from apps.domains.students.services.lifecycle import (
    PERMANENT_DELETE_ENROLLMENT_TENANT_PATHS,
    PERMANENT_DELETE_STUDENT_RELATIONS,
    PERMANENT_DELETE_SUBMISSION_RELATIONS,
)
from apps.domains.students.views import StudentViewSet
from apps.domains.video.models import (
    AccessMode,
    DirectVideoEntitlement,
    InactiveVideoEntitlement,
    Video,
    VideoComment,
)
from apps.support.students.lifecycle_dependencies import (
    delete_submission_storage_for_permanent_delete,
    process_pending_submission_storage_cleanup,
    submission_storage_reference_fields,
)

User = get_user_model()
StudentReportedScore = django_apps.get_model("results", "StudentReportedScore")
Exam = django_apps.get_model("exams", "Exam")
ExamResult = django_apps.get_model("results", "ExamResult")
SubmissionAnswer = django_apps.get_model("submissions", "SubmissionAnswer")
InventoryFile = django_apps.get_model("inventory", "InventoryFile")
WrongNotePDF = django_apps.get_model("results", "WrongNotePDF")
SubmissionStorageCleanupIntent = django_apps.get_model(
    "submissions",
    "SubmissionStorageCleanupIntent",
)
STORAGE_OBJECT_REFERENCE_FIELDS = submission_storage_reference_fields()


class TestBulkPermanentDeleteTenantIsolation(TestCase):

    def setUp(self):
        self.factory = APIRequestFactory()
        self.tenant_a = Tenant.objects.create(name="Academy A", code="testa", is_active=True)
        self.tenant_b = Tenant.objects.create(name="Academy B", code="testb", is_active=True)

        # User X: Tenant A의 학생
        self.user_x = User.objects.create_user(
            username="t_a_student001", password="test1234",
            tenant=self.tenant_a, phone="01012340000", name="학생X",
        )
        self.student_a = Student.objects.create(
            tenant=self.tenant_a, user=self.user_x,
            ps_number="A001", name="학생X",
            phone="01012340000", parent_phone="01099990000",
            omr_code="99990000",
        )
        TenantMembership.ensure_active(tenant=self.tenant_a, user=self.user_x, role="student")
        # User X: Tenant B teacher 멤버십
        TenantMembership.ensure_active(tenant=self.tenant_b, user=self.user_x, role="teacher")

        # Tenant A 데이터
        self.lecture_a = Lecture.objects.create(tenant=self.tenant_a, name="강의A")
        self.enrollment_a = Enrollment.objects.create(
            tenant=self.tenant_a, student=self.student_a,
            lecture=self.lecture_a, status="ACTIVE",
        )
        self.sub_a = Submission.objects.create(
            tenant=self.tenant_a, user=self.user_x,
            enrollment_id=self.enrollment_a.id,
            target_type="exam", target_id=1,
            source="omr_manual", status="done",
        )

        # Tenant B 데이터 (같은 user의 submission)
        self.sub_b = Submission.objects.create(
            tenant=self.tenant_b, user=self.user_x,
            enrollment_id=None,
            target_type="exam", target_id=2,
            source="omr_manual", status="done",
        )

        # 소프트삭제 (영구삭제 전제조건)
        self.student_a.deleted_at = timezone.now()
        self.student_a.save(update_fields=["deleted_at"])

        # Tenant A admin (Staff 권한)
        self.admin_a = User.objects.create_user(
            username="t_a_admin", password="test1234",
            tenant=self.tenant_a, is_staff=True, name="AdminA",
        )
        TenantMembership.ensure_active(tenant=self.tenant_a, user=self.admin_a, role="owner")

    def _call(self, tenant, admin_user, student_ids):
        request = self.factory.post(
            "/api/v1/students/bulk_permanent_delete/",
            data={"ids": student_ids}, format="json",
        )
        force_authenticate(request, user=admin_user)
        request.tenant = tenant
        view = StudentViewSet.as_view({"post": "bulk_permanent_delete"})
        return view(request)

    def test_delete_preserves_other_tenant_data(self):
        """핵심 증명: Tenant A 영구삭제 → Tenant B 데이터 100% 보존."""
        PendingPasswordReset.objects.create(
            tenant=self.tenant_b,
            user=self.user_x,
            password_hash=make_password("87654321"),
            expires_at=timezone.now() + timedelta(minutes=30),
        )

        # PRE
        self.assertEqual(Submission.objects.filter(tenant=self.tenant_a, user=self.user_x).count(), 1)
        self.assertEqual(Submission.objects.filter(tenant=self.tenant_b, user=self.user_x).count(), 1)
        self.assertTrue(TenantMembership.objects.filter(tenant=self.tenant_b, user=self.user_x).exists())

        # ACT
        resp = self._call(self.tenant_a, self.admin_a, [self.student_a.id])
        self.assertEqual(resp.status_code, 200, f"응답: {resp.data}")
        self.assertEqual(resp.data.get("deleted"), 1)

        # Tenant A 삭제 확인
        self.assertFalse(Student.objects.filter(id=self.student_a.id).exists())
        self.assertFalse(Enrollment.objects.filter(id=self.enrollment_a.id).exists())
        self.assertEqual(Submission.objects.filter(tenant=self.tenant_a, user=self.user_x).count(), 0)
        self.assertFalse(TenantMembership.objects.filter(tenant=self.tenant_a, user=self.user_x).exists())

        # ★ Tenant B 보존 증명
        self.assertEqual(
            Submission.objects.filter(tenant=self.tenant_b, user=self.user_x).count(), 1,
            "❌ CRITICAL: Tenant B submission 삭제됨 = 크로스테넌트 데이터 유실!"
        )
        self.assertTrue(
            TenantMembership.objects.filter(tenant=self.tenant_b, user=self.user_x).exists(),
            "❌ CRITICAL: Tenant B membership 삭제됨 = 다른 학원 접속 불가!"
        )
        self.assertTrue(
            User.objects.filter(id=self.user_x.id).exists(),
            "❌ CRITICAL: 다른 테넌트 멤버십이 있는 User가 삭제됨!"
        )
        self.assertTrue(
            PendingPasswordReset.objects.filter(tenant=self.tenant_b, user=self.user_x).exists(),
            "❌ CRITICAL: 다른 테넌트 pending password reset이 삭제됨!"
        )

    @patch("apps.infrastructure.storage.r2.delete_object_r2_ai")
    def test_permanent_delete_removes_detached_homework_media_and_preserves_shared_object(
        self,
        delete_object_r2_ai,
    ):
        """수강 연결이 먼저 삭제된 제출도 media FK/R2 잔여 없이 정리한다."""
        target_key = (
            f"tenants/{self.tenant_a.id}/ai/submissions/{self.sub_a.id}/homework.jpg"
        )
        shared_key = (
            f"tenants/{self.tenant_a.id}/ai/submissions/{self.sub_a.id}/shared.jpg"
        )
        target_media = SubmissionMedia.objects.create(
            tenant=self.tenant_a,
            submission=self.sub_a,
            client_upload_id=uuid.uuid4(),
            upload_batch_id=uuid.uuid4(),
            fingerprint="a" * 64,
            object_key=target_key,
            original_filename="풀이.jpg",
            media_kind=SubmissionMedia.Kind.IMAGE,
            mime_type="image/jpeg",
            size=1024,
            position=0,
            status=SubmissionMedia.Status.UPLOADED,
        )
        shared_target_media = SubmissionMedia.objects.create(
            tenant=self.tenant_a,
            submission=self.sub_a,
            client_upload_id=uuid.uuid4(),
            upload_batch_id=uuid.uuid4(),
            fingerprint="b" * 64,
            object_key=shared_key,
            original_filename="공유.jpg",
            media_kind=SubmissionMedia.Kind.IMAGE,
            mime_type="image/jpeg",
            size=2048,
            position=1,
            status=SubmissionMedia.Status.UPLOADED,
        )
        foreign_media = SubmissionMedia.objects.create(
            tenant=self.tenant_b,
            submission=self.sub_b,
            client_upload_id=uuid.uuid4(),
            upload_batch_id=uuid.uuid4(),
            fingerprint="c" * 64,
            object_key=shared_key,
            original_filename="보존.jpg",
            media_kind=SubmissionMedia.Kind.IMAGE,
            mime_type="image/jpeg",
            size=2048,
            position=0,
            status=SubmissionMedia.Status.UPLOADED,
        )
        Enrollment.objects.filter(id=self.enrollment_a.id).delete()
        self.sub_a.refresh_from_db()
        self.assertIsNone(self.sub_a.enrollment_id)

        with self.captureOnCommitCallbacks(execute=True):
            response = self._call(self.tenant_a, self.admin_a, [self.student_a.id])

        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(SubmissionMedia.objects.filter(id=target_media.id).exists())
        self.assertFalse(SubmissionMedia.objects.filter(id=shared_target_media.id).exists())
        self.assertTrue(SubmissionMedia.objects.filter(id=foreign_media.id).exists())
        delete_object_r2_ai.assert_called_once_with(key=target_key)

    @patch("apps.infrastructure.storage.r2.delete_object_r2_ai")
    def test_submission_cleanup_does_not_confuse_same_key_in_storage_bucket(
        self,
        delete_object_r2_ai,
    ):
        """같은 문자열 key라도 Storage owner는 AI bucket 삭제를 막지 않는다."""
        key = f"tenants/{self.tenant_a.id}/ai/submissions/{self.sub_a.id}/shared.pdf"
        media = SubmissionMedia.objects.create(
            tenant=self.tenant_a,
            submission=self.sub_a,
            client_upload_id=uuid.uuid4(),
            upload_batch_id=uuid.uuid4(),
            fingerprint="9" * 64,
            object_key=key,
            original_filename="shared.pdf",
            media_kind=SubmissionMedia.Kind.IMAGE,
            mime_type="application/pdf",
            size=512,
            position=0,
            status=SubmissionMedia.Status.UPLOADED,
        )
        other_owner = InventoryFile.objects.create(
            tenant=self.tenant_b,
            scope="admin",
            display_name="shared.pdf",
            r2_key=key,
            original_name="shared.pdf",
            size_bytes=512,
            content_type="application/pdf",
        )

        with self.captureOnCommitCallbacks(execute=True):
            response = self._call(self.tenant_a, self.admin_a, [self.student_a.id])

        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(SubmissionMedia.objects.filter(id=media.id).exists())
        self.assertTrue(InventoryFile.objects.filter(id=other_owner.id).exists())
        intent = SubmissionStorageCleanupIntent.objects.get()
        self.assertEqual(intent.bucket, SubmissionStorageCleanupIntent.Bucket.AI)
        delete_object_r2_ai.assert_called_once_with(key=key)

    @patch("apps.infrastructure.storage.r2.delete_object_r2_ai")
    def test_submission_storage_cleanup_waits_for_database_commit(
        self,
        delete_object_r2_ai,
    ):
        """후속 DB 실패로 rollback되면 R2 object는 절대 먼저 지우지 않는다."""
        media = SubmissionMedia.objects.create(
            tenant=self.tenant_a,
            submission=self.sub_a,
            client_upload_id=uuid.uuid4(),
            upload_batch_id=uuid.uuid4(),
            fingerprint="d" * 64,
            object_key=(
                f"tenants/{self.tenant_a.id}/ai/submissions/"
                f"{self.sub_a.id}/rollback.jpg"
            ),
            original_filename="rollback.jpg",
            media_kind=SubmissionMedia.Kind.IMAGE,
            mime_type="image/jpeg",
            size=512,
            position=0,
            status=SubmissionMedia.Status.UPLOADED,
        )

        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                delete_submission_storage_for_permanent_delete(
                    tenant_id=self.tenant_a.id,
                    submission_ids=[self.sub_a.id],
                )
                raise RuntimeError("later database failure")

        self.assertTrue(SubmissionMedia.objects.filter(id=media.id).exists())
        delete_object_r2_ai.assert_not_called()

    @patch("apps.infrastructure.storage.r2.delete_object_r2_ai")
    def test_permanent_delete_fails_closed_while_media_upload_is_in_flight(
        self,
        delete_object_r2_ai,
    ):
        """UPLOADING row가 PUT과 DB finalize 사이에 있으면 학생 삭제를 기다린다."""
        media = SubmissionMedia.objects.create(
            tenant=self.tenant_a,
            submission=self.sub_a,
            client_upload_id=uuid.uuid4(),
            upload_batch_id=uuid.uuid4(),
            fingerprint="6" * 64,
            object_key=(
                f"tenants/{self.tenant_a.id}/ai/submissions/"
                f"{self.sub_a.id}/in-flight.jpg"
            ),
            original_filename="in-flight.jpg",
            media_kind=SubmissionMedia.Kind.IMAGE,
            mime_type="image/jpeg",
            size=512,
            position=0,
            status=SubmissionMedia.Status.UPLOADING,
        )

        response = self._call(self.tenant_a, self.admin_a, [self.student_a.id])

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["code"], "storage_cleanup_scope_mismatch")
        self.assertTrue(Student.objects.filter(id=self.student_a.id).exists())
        self.assertTrue(SubmissionMedia.objects.filter(id=media.id).exists())
        self.assertFalse(SubmissionStorageCleanupIntent.objects.exists())
        delete_object_r2_ai.assert_not_called()

    @patch("apps.infrastructure.storage.r2.delete_object_r2_ai")
    def test_permanent_delete_recovers_stale_media_upload_after_lease(
        self,
        delete_object_r2_ai,
    ):
        """중단된 오래된 PUT은 durable cleanup으로 회수해 삭제를 끝낸다."""
        frozen_now = timezone.now()
        key = (
            f"tenants/{self.tenant_a.id}/ai/submissions/"
            f"{self.sub_a.id}/stale-upload.jpg"
        )
        media = SubmissionMedia.objects.create(
            tenant=self.tenant_a,
            submission=self.sub_a,
            client_upload_id=uuid.uuid4(),
            upload_batch_id=uuid.uuid4(),
            fingerprint="8" * 64,
            object_key=key,
            original_filename="stale-upload.jpg",
            media_kind=SubmissionMedia.Kind.IMAGE,
            mime_type="image/jpeg",
            size=512,
            position=0,
            status=SubmissionMedia.Status.UPLOADING,
            upload_started_at=frozen_now - timedelta(hours=2),
        )

        with patch(
            "apps.domains.submissions.services.lifecycle.timezone.now",
            return_value=frozen_now,
        ):
            with self.captureOnCommitCallbacks(execute=True):
                response = self._call(self.tenant_a, self.admin_a, [self.student_a.id])

        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(SubmissionMedia.objects.filter(id=media.id).exists())
        intent = SubmissionStorageCleanupIntent.objects.get(
            bucket=SubmissionStorageCleanupIntent.Bucket.AI,
            object_key=key,
        )
        self.assertEqual(intent.status, SubmissionStorageCleanupIntent.Status.CLEANED)
        delete_object_r2_ai.assert_called_once_with(key=key)

    @patch("apps.infrastructure.storage.r2.delete_object_r2_ai")
    def test_legacy_submission_namespace_is_cleaned_after_commit(
        self,
        delete_object_r2_ai,
    ):
        """숫자 submission 경로 이전의 tenant-scoped legacy key도 안전하게 정리한다."""
        key = f"tenants/{self.tenant_a.id}/ai/submissions/legacy/page.jpg"
        SubmissionMedia.objects.create(
            tenant=self.tenant_a,
            submission=self.sub_a,
            client_upload_id=uuid.uuid4(),
            upload_batch_id=uuid.uuid4(),
            fingerprint="7" * 64,
            object_key=key,
            original_filename="legacy.jpg",
            media_kind=SubmissionMedia.Kind.IMAGE,
            mime_type="image/jpeg",
            size=512,
            position=0,
            status=SubmissionMedia.Status.UPLOADED,
        )

        with self.captureOnCommitCallbacks(execute=True):
            response = self._call(self.tenant_a, self.admin_a, [self.student_a.id])

        self.assertEqual(response.status_code, 200, response.data)
        delete_object_r2_ai.assert_called_once_with(key=key)

    @patch("apps.infrastructure.storage.r2.delete_object_r2_ai")
    def test_historical_global_submission_key_is_cleaned_by_exact_owner(
        self,
        delete_object_r2_ai,
    ):
        """구 serializer의 submissions/{id}/ key도 exact DB owner일 때만 정리한다."""
        key = f"submissions/{self.sub_a.id}/legacy-page.jpg"
        SubmissionMedia.objects.create(
            tenant=self.tenant_a,
            submission=self.sub_a,
            client_upload_id=uuid.uuid4(),
            upload_batch_id=uuid.uuid4(),
            fingerprint="9" * 64,
            object_key=key,
            original_filename="legacy-page.jpg",
            media_kind=SubmissionMedia.Kind.IMAGE,
            mime_type="image/jpeg",
            size=512,
            position=0,
            status=SubmissionMedia.Status.UPLOADED,
        )

        with self.captureOnCommitCallbacks(execute=True):
            response = self._call(self.tenant_a, self.admin_a, [self.student_a.id])

        self.assertEqual(response.status_code, 200, response.data)
        delete_object_r2_ai.assert_called_once_with(key=key)

    @patch("apps.infrastructure.storage.r2.delete_object_r2_storage")
    def test_wrong_note_cleanup_rolls_back_then_retries_durably(
        self,
        delete_object_r2_storage,
    ):
        """오답노트도 DB commit 전 삭제하지 않고 provider 실패를 재시도한다."""
        key = f"tenants/{self.tenant_a.id}/results/wrong-notes/rollback.pdf"
        wrong_note = WrongNotePDF.objects.create(
            enrollment=self.enrollment_a,
            lecture=self.lecture_a,
            status=WrongNotePDF.Status.DONE,
            file_path=key,
        )

        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                delete_submission_storage_for_permanent_delete(
                    tenant_id=self.tenant_a.id,
                    submission_ids=[],
                    wrong_note_pdf_ids=[wrong_note.id],
                )
                raise RuntimeError("later database failure")

        self.assertTrue(WrongNotePDF.objects.filter(id=wrong_note.id).exists())
        self.assertFalse(SubmissionStorageCleanupIntent.objects.exists())
        delete_object_r2_storage.assert_not_called()

        with self.captureOnCommitCallbacks(execute=False):
            response = self._call(self.tenant_a, self.admin_a, [self.student_a.id])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["storage_cleanup"], {"pending": 1, "failed": 0})
        self.assertFalse(WrongNotePDF.objects.filter(id=wrong_note.id).exists())

        intent = SubmissionStorageCleanupIntent.objects.get(object_key=key)
        delete_object_r2_storage.side_effect = RuntimeError("private provider detail")
        failed = process_pending_submission_storage_cleanup(
            intent_ids=[intent.id]
        )
        self.assertEqual((failed.cleaned, failed.failed, failed.deferred), (0, 1, 0))
        intent.refresh_from_db()
        self.assertEqual(intent.last_error, "storage_delete_failed")

        delete_object_r2_storage.side_effect = None
        recovered = process_pending_submission_storage_cleanup(
            intent_ids=[intent.id]
        )
        self.assertEqual((recovered.cleaned, recovered.failed, recovered.deferred), (1, 0, 0))
        intent.refresh_from_db()
        self.assertEqual(intent.status, SubmissionStorageCleanupIntent.Status.CLEANED)

    @patch("apps.infrastructure.storage.r2.delete_object_r2_storage")
    def test_cleanup_processor_skips_active_claim_and_recovers_stale_claim(
        self,
        delete_object_r2_storage,
    ):
        """최근 claim은 중복 실행하지 않고 15분 지난 PROCESSING만 회수한다."""
        recent_key = f"tenants/{self.tenant_a.id}/ai/submissions/recent.jpg"
        stale_key = f"tenants/{self.tenant_a.id}/ai/submissions/stale.jpg"
        recent = SubmissionStorageCleanupIntent.objects.create(
            tenant=self.tenant_a,
            bucket=SubmissionStorageCleanupIntent.Bucket.STORAGE,
            object_key=recent_key,
            status=SubmissionStorageCleanupIntent.Status.PROCESSING,
            attempt_count=1,
            last_attempt_at=timezone.now(),
        )
        stale = SubmissionStorageCleanupIntent.objects.create(
            tenant=self.tenant_a,
            bucket=SubmissionStorageCleanupIntent.Bucket.STORAGE,
            object_key=stale_key,
            status=SubmissionStorageCleanupIntent.Status.PROCESSING,
            attempt_count=1,
            last_attempt_at=timezone.now() - timedelta(minutes=16),
        )

        result = process_pending_submission_storage_cleanup(
            intent_ids=[recent.id, stale.id]
        )

        self.assertEqual((result.cleaned, result.failed, result.deferred), (1, 0, 0))
        recent.refresh_from_db()
        stale.refresh_from_db()
        self.assertEqual(recent.status, SubmissionStorageCleanupIntent.Status.PROCESSING)
        self.assertEqual(recent.attempt_count, 1)
        self.assertEqual(stale.status, SubmissionStorageCleanupIntent.Status.CLEANED)
        self.assertEqual(stale.attempt_count, 2)
        delete_object_r2_storage.assert_called_once_with(key=stale_key)

    @patch("apps.infrastructure.storage.r2.delete_object_r2_storage")
    @patch("apps.infrastructure.storage.r2.delete_object_r2_ai")
    def test_cleanup_intent_bucket_is_part_of_identity(
        self,
        delete_object_r2_ai,
        delete_object_r2_storage,
    ):
        """동일 key 문자열이어도 AI/Storage 버킷은 각각 정확히 정리한다."""
        key = f"tenants/{self.tenant_a.id}/same-text-key.bin"
        intents = [
            SubmissionStorageCleanupIntent.objects.create(
                tenant=self.tenant_a,
                bucket=bucket,
                object_key=key,
            )
            for bucket in (
                SubmissionStorageCleanupIntent.Bucket.AI,
                SubmissionStorageCleanupIntent.Bucket.STORAGE,
            )
        ]

        result = process_pending_submission_storage_cleanup(
            intent_ids=[intent.id for intent in intents]
        )

        self.assertEqual((result.cleaned, result.failed, result.deferred), (2, 0, 0))
        delete_object_r2_ai.assert_called_once_with(key=key)
        delete_object_r2_storage.assert_called_once_with(key=key)

    @patch("apps.infrastructure.storage.r2.delete_object_r2_ai")
    def test_cleanup_lost_claim_cannot_overwrite_reclaimed_attempt(
        self,
        delete_object_r2_ai,
    ):
        """느린 worker의 완료가 새 claim 상태를 덮어쓰지 않는다."""
        intent = SubmissionStorageCleanupIntent.objects.create(
            tenant=self.tenant_a,
            bucket=SubmissionStorageCleanupIntent.Bucket.AI,
            object_key=f"tenants/{self.tenant_a.id}/ai/submissions/claim-race.jpg",
        )
        replacement_token = uuid.uuid4()

        def replace_claim(*, key):
            SubmissionStorageCleanupIntent.objects.filter(id=intent.id).update(
                status=SubmissionStorageCleanupIntent.Status.PROCESSING,
                claim_token=replacement_token,
                last_attempt_at=timezone.now(),
            )

        delete_object_r2_ai.side_effect = replace_claim
        lost = process_pending_submission_storage_cleanup(
            intent_ids=[intent.id]
        )

        self.assertEqual((lost.cleaned, lost.failed, lost.deferred), (0, 0, 0))
        intent.refresh_from_db()
        self.assertEqual(intent.status, SubmissionStorageCleanupIntent.Status.PROCESSING)
        self.assertEqual(intent.claim_token, replacement_token)

        SubmissionStorageCleanupIntent.objects.filter(id=intent.id).update(
            last_attempt_at=timezone.now() - timedelta(minutes=16)
        )
        delete_object_r2_ai.side_effect = None
        recovered = process_pending_submission_storage_cleanup(
            intent_ids=[intent.id]
        )
        self.assertEqual((recovered.cleaned, recovered.failed, recovered.deferred), (1, 0, 0))

    @patch("apps.infrastructure.storage.r2.delete_object_r2_ai")
    def test_submission_storage_cleanup_retries_only_failed_key(
        self,
        delete_object_r2_ai,
    ):
        """다중 key 중 일부가 실패해도 완료 key를 되풀이하지 않고 재시도한다."""
        keys = [
            f"tenants/{self.tenant_a.id}/ai/submissions/{self.sub_a.id}/a.jpg",
            f"tenants/{self.tenant_a.id}/ai/submissions/{self.sub_a.id}/b.jpg",
        ]
        for position, key in enumerate(keys):
            SubmissionMedia.objects.create(
                tenant=self.tenant_a,
                submission=self.sub_a,
                client_upload_id=uuid.uuid4(),
                upload_batch_id=uuid.uuid4(),
                fingerprint=str(position + 1) * 64,
                object_key=key,
                original_filename=f"{position}.jpg",
                media_kind=SubmissionMedia.Kind.IMAGE,
                mime_type="image/jpeg",
                size=512,
                position=position,
                status=SubmissionMedia.Status.UPLOADED,
            )

        with self.captureOnCommitCallbacks(execute=False):
            response = self._call(self.tenant_a, self.admin_a, [self.student_a.id])

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["storage_cleanup"], {"pending": 2, "failed": 0})
        intents = list(SubmissionStorageCleanupIntent.objects.order_by("object_key"))
        self.assertEqual([intent.object_key for intent in intents], keys)

        delete_object_r2_ai.side_effect = [None, RuntimeError("private provider detail")]
        first = process_pending_submission_storage_cleanup(
            intent_ids=[intent.id for intent in intents],
        )

        self.assertEqual((first.cleaned, first.failed, first.deferred), (1, 1, 0))
        intents[0].refresh_from_db()
        intents[1].refresh_from_db()
        self.assertEqual(intents[0].status, SubmissionStorageCleanupIntent.Status.CLEANED)
        self.assertEqual(intents[1].status, SubmissionStorageCleanupIntent.Status.FAILED)
        self.assertEqual(intents[1].last_error, "storage_delete_failed")
        self.assertNotIn("private provider detail", intents[1].last_error)

        delete_object_r2_ai.reset_mock()
        delete_object_r2_ai.side_effect = None
        second = process_pending_submission_storage_cleanup(
            intent_ids=[intent.id for intent in intents],
        )

        self.assertEqual((second.cleaned, second.failed, second.deferred), (1, 0, 0))
        delete_object_r2_ai.assert_called_once_with(key=keys[1])
        self.assertFalse(
            SubmissionStorageCleanupIntent.objects.exclude(
                status=SubmissionStorageCleanupIntent.Status.CLEANED
            ).exists()
        )

    @patch("apps.infrastructure.storage.r2.delete_object_r2_storage")
    def test_daily_student_purge_retries_failed_submission_storage_cleanup(
        self,
        delete_object_r2_storage,
    ):
        """매일 학생 purge 스케줄이 이전 FAILED intent를 실제로 재시도한다."""
        key = f"tenants/{self.tenant_a.id}/results/wrong-notes/retry.pdf"
        intent = SubmissionStorageCleanupIntent.objects.create(
            tenant=self.tenant_a,
            bucket=SubmissionStorageCleanupIntent.Bucket.STORAGE,
            object_key=key,
            status=SubmissionStorageCleanupIntent.Status.FAILED,
            attempt_count=1,
            last_error="storage_delete_failed",
        )

        call_command("purge_deleted_students", "--days", "30", stdout=StringIO())

        intent.refresh_from_db()
        self.assertEqual(intent.status, SubmissionStorageCleanupIntent.Status.CLEANED)
        self.assertEqual(intent.attempt_count, 2)
        delete_object_r2_storage.assert_called_once_with(key=key)

    @patch("apps.infrastructure.storage.r2.delete_object_r2_ai")
    def test_multi_role_submission_for_other_student_is_preserved(
        self,
        delete_object_r2_ai,
    ):
        """같은 actor user만으로 다른 학생 제출을 대상 학생 소유로 추측하지 않는다."""
        Parent.objects.create(
            tenant=self.tenant_a,
            user=self.user_x,
            name="공유 학부모",
            phone="01056565656",
        )
        other_user = User.objects.create_user(
            username="same-tenant-other-student",
            password="test1234",
            tenant=self.tenant_a,
            phone="01056560001",
            name="다른 학생",
        )
        other_student = Student.objects.create(
            tenant=self.tenant_a,
            user=other_user,
            ps_number="A002",
            name="다른 학생",
            phone="01056560001",
            parent_phone="01056560002",
            omr_code="56560001",
        )
        other_enrollment = Enrollment.objects.create(
            tenant=self.tenant_a,
            student=other_student,
            lecture=self.lecture_a,
            status="ACTIVE",
        )
        unrelated = Submission.objects.create(
            tenant=self.tenant_a,
            user=self.user_x,
            enrollment=other_enrollment,
            target_type=Submission.TargetType.EXAM,
            target_id=77,
            source=Submission.Source.OMR_SCAN,
            status=Submission.Status.DONE,
        )
        unrelated_media = SubmissionMedia.objects.create(
            tenant=self.tenant_a,
            submission=unrelated,
            client_upload_id=uuid.uuid4(),
            upload_batch_id=uuid.uuid4(),
            fingerprint="e" * 64,
            object_key=(
                f"tenants/{self.tenant_a.id}/ai/submissions/"
                f"{unrelated.id}/other-student.jpg"
            ),
            original_filename="other-student.jpg",
            media_kind=SubmissionMedia.Kind.IMAGE,
            mime_type="image/jpeg",
            size=512,
            position=0,
            status=SubmissionMedia.Status.UPLOADED,
        )

        with self.captureOnCommitCallbacks(execute=True):
            response = self._call(self.tenant_a, self.admin_a, [self.student_a.id])

        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(Student.objects.filter(id=self.student_a.id).exists())
        self.assertFalse(Submission.objects.filter(id=self.sub_a.id).exists())
        self.assertTrue(Submission.objects.filter(id=unrelated.id).exists())
        self.assertTrue(SubmissionMedia.objects.filter(id=unrelated_media.id).exists())
        self.assertTrue(Student.objects.filter(id=other_student.id).exists())
        self.assertTrue(Parent.objects.filter(tenant=self.tenant_a, user=self.user_x).exists())
        self.assertTrue(User.objects.filter(id=self.user_x.id).exists())
        delete_object_r2_ai.assert_not_called()

    def test_permanent_delete_removes_reported_scores_and_preserves_other_tenant(self):
        """선택 학생의 자발 성적만 정리하고 다른 테넌트 학생 성적은 보존한다."""
        target_score = StudentReportedScore.objects.create(
            tenant=self.tenant_a,
            student=self.student_a,
            source=StudentReportedScore.Source.SCHOOL_EXAM,
            academic_year=2026,
            semester=1,
            exam_round=StudentReportedScore.ExamRound.FIRST,
            subject="영어",
            score=95,
            max_score=100,
            status=StudentReportedScore.Status.REJECTED,
        )
        foreign_user = User.objects.create_user(
            username="reported-score-foreign",
            password="test1234",
            tenant=self.tenant_b,
            name="보존학생",
        )
        foreign_student = Student.objects.create(
            tenant=self.tenant_b,
            user=foreign_user,
            ps_number="B-REPORTED",
            name="보존학생",
            phone="01077778888",
            parent_phone="01099998888",
            omr_code="77778888",
        )
        foreign_score = StudentReportedScore.objects.create(
            tenant=self.tenant_b,
            student=foreign_student,
            source=StudentReportedScore.Source.SCHOOL_EXAM,
            academic_year=2026,
            semester=1,
            exam_round=StudentReportedScore.ExamRound.FIRST,
            subject="영어",
            score=88,
            max_score=100,
            status=StudentReportedScore.Status.REJECTED,
        )

        response = self._call(self.tenant_a, self.admin_a, [self.student_a.id])

        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(StudentReportedScore.objects.filter(id=target_score.id).exists())
        self.assertTrue(StudentReportedScore.objects.filter(id=foreign_score.id).exists())

    def test_permanent_delete_covers_support_and_video_student_relations(self):
        """지원 세션과 영상 예외 권한이 남아도 선택 학생을 완전 정리한다."""
        support = StudentSupportSession.objects.create(
            tenant=self.tenant_a,
            student=self.student_a,
            operator=self.admin_a,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        video = Video.objects.create(
            tenant=self.tenant_a,
            title="영상 권한 정리",
            status=Video.Status.READY,
        )
        inactive = InactiveVideoEntitlement.objects.create(
            tenant=self.tenant_a,
            student=self.student_a,
            enrollment=self.enrollment_a,
            video=video,
            access_mode=AccessMode.FREE_REVIEW,
            source=InactiveVideoEntitlement.Source.STAFF_AUTHORIZATION,
            source_reference="cleanup-test-inactive",
            reason="영구 삭제 테스트",
            granted_by=self.admin_a,
            granted_by_reference="cleanup-test-admin",
        )
        direct = DirectVideoEntitlement.objects.create(
            tenant=self.tenant_a,
            student=self.student_a,
            video=video,
            source=DirectVideoEntitlement.Source.STAFF_AUTHORIZATION,
            source_reference="cleanup-test-direct",
            reason="영구 삭제 테스트",
            granted_by=self.admin_a,
            granted_by_reference="cleanup-test-admin",
        )

        response = self._call(self.tenant_a, self.admin_a, [self.student_a.id])

        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(StudentSupportSession.objects.filter(id=support.id).exists())
        self.assertFalse(InactiveVideoEntitlement.objects.filter(id=inactive.id).exists())
        self.assertFalse(DirectVideoEntitlement.objects.filter(id=direct.id).exists())

    def test_permanent_delete_nulls_omr_batch_submission_references(self):
        """OMR 배치 이력은 보존하되 삭제 제출 참조는 비운다."""
        batch = OmrUploadBatch.objects.create(
            tenant=self.tenant_a,
            created_by=self.admin_a,
            exam_id=1,
            total_count=2,
        )
        submission_item = OmrUploadBatchItem.objects.create(
            tenant=self.tenant_a,
            exam_id=1,
            batch=batch,
            ordinal=0,
            submission=self.sub_a,
            admission_status=OmrUploadBatchItem.AdmissionStatus.RECEIVED,
        )
        duplicate_item = OmrUploadBatchItem.objects.create(
            tenant=self.tenant_a,
            exam_id=1,
            batch=batch,
            ordinal=1,
            duplicate_of_submission=self.sub_a,
            admission_status=OmrUploadBatchItem.AdmissionStatus.DUPLICATE,
        )

        response = self._call(self.tenant_a, self.admin_a, [self.student_a.id])

        self.assertEqual(response.status_code, 200, response.data)
        submission_item.refresh_from_db()
        duplicate_item.refresh_from_db()
        self.assertIsNone(submission_item.submission_id)
        self.assertIsNone(duplicate_item.duplicate_of_submission_id)

    def test_permanent_delete_accepts_legacy_null_tenant_omr_batch_item(self):
        """legacy null item은 same-tenant batch이면 안전하게 참조만 비운다."""
        batch = OmrUploadBatch.objects.create(
            tenant=self.tenant_a,
            created_by=self.admin_a,
            exam_id=1,
            total_count=1,
        )
        item = OmrUploadBatchItem.objects.create(
            tenant=None,
            exam_id=1,
            batch=batch,
            ordinal=0,
            submission=self.sub_a,
        )

        response = self._call(self.tenant_a, self.admin_a, [self.student_a.id])

        self.assertEqual(response.status_code, 200, response.data)
        item.refresh_from_db()
        self.assertIsNone(item.submission_id)

    def test_enrollment_child_owner_preflight_blocks_foreign_video(self):
        """tenant 필드 없는 enrollment child도 소유 video tenant를 선행 검증한다."""
        foreign_video = Video.objects.create(
            tenant=self.tenant_b,
            title="foreign enrollment child",
            status=Video.Status.READY,
        )
        video_access_model = django_apps.get_model("video", "VideoAccess")
        access = video_access_model.objects.create(
            video=foreign_video,
            enrollment=self.enrollment_a,
        )

        response = self._call(self.tenant_a, self.admin_a, [self.student_a.id])

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["code"], "cross_tenant_reference")
        self.assertTrue(Student.objects.filter(id=self.student_a.id).exists())
        self.assertTrue(video_access_model.objects.filter(id=access.id).exists())

    @patch("apps.infrastructure.storage.r2.delete_object_r2_ai")
    def test_submission_reverse_relations_preflight_tenant_before_any_mutation(
        self,
        delete_object_r2_ai,
    ):
        """모든 submission reverse FK의 tenant·batch parent 오염을 선행 차단한다."""

        def submission_answer():
            SubmissionAnswer.objects.create(
                tenant=self.tenant_b,
                submission=self.sub_a,
                exam_question_id=91001,
            )

        def recognition_run():
            OMRRecognitionRun.objects.create(
                tenant=self.tenant_b,
                submission=self.sub_a,
                job_id="foreign-run",
                status="done",
            )

        def detected_answer():
            run = OMRRecognitionRun.objects.create(
                tenant=self.tenant_a,
                submission=self.sub_a,
                job_id="valid-parent-run",
                status="done",
            )
            OMRDetectedAnswer.objects.create(
                tenant=self.tenant_b,
                submission=self.sub_a,
                recognition_run=run,
                question_number=1,
            )

        def student_match():
            OMRStudentMatch.objects.create(
                tenant=self.tenant_b,
                submission=self.sub_a,
                status=OMRStudentMatch.Status.NEEDS_REVIEW,
            )

        def submission_media():
            SubmissionMedia.objects.create(
                tenant=self.tenant_b,
                submission=self.sub_a,
                client_upload_id=uuid.uuid4(),
                upload_batch_id=uuid.uuid4(),
                fingerprint="f" * 64,
                object_key=(
                    f"tenants/{self.tenant_a.id}/ai/submissions/"
                    f"{self.sub_a.id}/foreign.jpg"
                ),
                original_filename="foreign.jpg",
                media_kind=SubmissionMedia.Kind.IMAGE,
                mime_type="image/jpeg",
                size=512,
                position=0,
                status=SubmissionMedia.Status.UPLOADED,
            )

        def exam_result():
            foreign_exam = Exam.objects.create(
                tenant=self.tenant_b,
                title="foreign exam",
            )
            ExamResult.objects.create(
                submission=self.sub_a,
                exam=foreign_exam,
            )

        def batch_item_tenant():
            batch = OmrUploadBatch.objects.create(
                tenant=self.tenant_b,
                created_by=self.admin_a,
                exam_id=1,
                total_count=1,
            )
            OmrUploadBatchItem.objects.create(
                tenant=self.tenant_b,
                exam_id=1,
                batch=batch,
                ordinal=0,
                submission=self.sub_a,
            )

        def batch_parent_tenant():
            batch = OmrUploadBatch.objects.create(
                tenant=self.tenant_b,
                created_by=self.admin_a,
                exam_id=1,
                total_count=1,
            )
            OmrUploadBatchItem.objects.create(
                tenant=self.tenant_a,
                exam_id=1,
                batch=batch,
                ordinal=0,
                duplicate_of_submission=self.sub_a,
            )

        cases = {
            "submission_answer": submission_answer,
            "recognition_run": recognition_run,
            "detected_answer": detected_answer,
            "student_match": student_match,
            "submission_media": submission_media,
            "exam_result": exam_result,
            "batch_item_tenant": batch_item_tenant,
            "batch_parent_tenant": batch_parent_tenant,
        }
        for name, create_corrupt_relation in cases.items():
            with self.subTest(name=name):
                savepoint = transaction.savepoint()
                try:
                    create_corrupt_relation()
                    response = self._call(
                        self.tenant_a,
                        self.admin_a,
                        [self.student_a.id],
                    )
                    self.assertEqual(response.status_code, 409, response.data)
                    self.assertEqual(response.data["code"], "cross_tenant_reference")
                    self.assertTrue(Student.objects.filter(id=self.student_a.id).exists())
                    self.assertTrue(Submission.objects.filter(id=self.sub_a.id).exists())
                    self.assertFalse(SubmissionStorageCleanupIntent.objects.exists())
                    delete_object_r2_ai.assert_not_called()
                finally:
                    transaction.savepoint_rollback(savepoint)
                    delete_object_r2_ai.reset_mock()

    def test_permanent_delete_relation_contract_matches_model_graph(self):
        """신규 Student/Submission FK가 수동 삭제 그래프에서 누락되지 않게 한다."""
        student_relations = {
            (relation.related_model._meta.db_table, relation.field.column)
            for relation in Student._meta.related_objects
        }
        submission_relations = {
            (relation.related_model._meta.db_table, relation.field.column)
            for relation in Submission._meta.related_objects
        }

        self.assertEqual(student_relations, PERMANENT_DELETE_STUDENT_RELATIONS)
        self.assertEqual(submission_relations, PERMANENT_DELETE_SUBMISSION_RELATIONS)
        enrollment_relation_labels = {
            relation.related_model._meta.label
            for relation in Enrollment._meta.related_objects
        }
        self.assertEqual(
            enrollment_relation_labels,
            set(PERMANENT_DELETE_ENROLLMENT_TENANT_PATHS),
        )

    def test_storage_owner_registry_matches_all_scalar_key_fields(self):
        """새 object-store key 필드는 공유 참조 registry 누락 시 즉시 실패한다."""
        storage_field_names = {field_name for _, _, _, field_name in STORAGE_OBJECT_REFERENCE_FIELDS}
        discovered = {
            (model._meta.app_label, model.__name__, field.name)
            for model in django_apps.get_models()
            for field in model._meta.fields
            if field.name in storage_field_names
            and model is not SubmissionStorageCleanupIntent
        }
        registered = {
            (app_label, model_name, field_name)
            for _, app_label, model_name, field_name in STORAGE_OBJECT_REFERENCE_FIELDS
        }

        self.assertEqual(discovered, registered)
        self.assertIn(
            ("ai", "exams", "ExamAsset", "file_key"),
            STORAGE_OBJECT_REFERENCE_FIELDS,
        )
        self.assertIn(
            ("storage", "exams", "ExamAsset", "file_key"),
            STORAGE_OBJECT_REFERENCE_FIELDS,
        )

    def test_deleted_tenant_pending_reset_removed_even_when_user_retained(self):
        """삭제되는 테넌트의 pending reset은 User가 다른 테넌트에 남아도 정리."""
        create_pending_password_reset(self.user_x, "12345678")

        resp = self._call(self.tenant_a, self.admin_a, [self.student_a.id])

        self.assertEqual(resp.status_code, 200, f"응답: {resp.data}")
        self.assertTrue(User.objects.filter(id=self.user_x.id).exists())
        self.assertFalse(
            PendingPasswordReset.objects.filter(tenant=self.tenant_a, user=self.user_x).exists()
        )

    def test_soft_then_permanent_delete_keeps_cross_tenant_account_active(self):
        """실제 soft delete 경유 후에도 다른 테넌트 계정이 잠기지 않는다."""
        user = User.objects.create_user(
            username="shared_soft_delete",
            password="test1234",
            tenant=self.tenant_a,
            phone="01012121212",
            name="공유계정",
        )
        TenantMembership.ensure_active(tenant=self.tenant_a, user=user, role="student")
        TenantMembership.ensure_active(tenant=self.tenant_b, user=user, role="teacher")
        student = Student.objects.create(
            tenant=self.tenant_a,
            user=user,
            ps_number="A-SHARED",
            name="공유계정학생",
            phone="01012121212",
            parent_phone="01034343434",
            omr_code="12121212",
        )

        soft_delete_student(student, tenant=self.tenant_a)
        user.refresh_from_db()

        self.assertTrue(user.is_active)
        self.assertFalse(TenantMembership.objects.get(tenant=self.tenant_a, user=user).is_active)
        self.assertTrue(TenantMembership.objects.get(tenant=self.tenant_b, user=user).is_active)

        permanently_delete_students(tenant=self.tenant_a, student_ids=[student.id])
        user.refresh_from_db()

        self.assertTrue(user.is_active)
        self.assertFalse(Student.objects.filter(id=student.id).exists())
        self.assertFalse(TenantMembership.objects.filter(tenant=self.tenant_a, user=user).exists())
        self.assertTrue(TenantMembership.objects.filter(tenant=self.tenant_b, user=user).exists())

    def test_user_deleted_when_orphaned(self):
        """멤버십이 전부 삭제되면 User도 삭제."""
        create_pending_password_reset(self.user_x, "12345678")
        refresh = RefreshToken.for_user(self.user_x)
        refresh.blacklist()
        self.assertTrue(OutstandingToken.objects.filter(user=self.user_x).exists())
        self.assertTrue(BlacklistedToken.objects.filter(token__user=self.user_x).exists())
        TenantMembership.objects.filter(tenant=self.tenant_b, user=self.user_x).delete()
        Submission.objects.filter(tenant=self.tenant_b, user=self.user_x).delete()

        resp = self._call(self.tenant_a, self.admin_a, [self.student_a.id])
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertFalse(User.objects.filter(id=self.user_x.id).exists(),
                         "orphan User는 삭제되어야 함")
        self.assertFalse(PendingPasswordReset.objects.filter(user=self.user_x).exists())
        self.assertFalse(OutstandingToken.objects.filter(user_id=self.user_x.id).exists())
        self.assertFalse(BlacklistedToken.objects.filter(token__user_id=self.user_x.id).exists())

    def test_same_tenant_parent_profile_keeps_membership_and_user(self):
        """같은 테넌트에 남은 계정 프로필이 있으면 멤버십과 User를 보존."""
        TenantMembership.objects.filter(tenant=self.tenant_b, user=self.user_x).delete()
        Submission.objects.filter(tenant=self.tenant_b, user=self.user_x).delete()
        Parent.objects.create(
            tenant=self.tenant_a,
            user=self.user_x,
            name="학생X 보호자",
            phone="01088889999",
        )
        resp = self._call(self.tenant_a, self.admin_a, [self.student_a.id])

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Student.objects.filter(id=self.student_a.id).exists())
        self.assertTrue(User.objects.filter(id=self.user_x.id).exists())
        self.assertTrue(Parent.objects.filter(tenant=self.tenant_a, user=self.user_x).exists())
        self.assertTrue(TenantMembership.objects.filter(tenant=self.tenant_a, user=self.user_x).exists())

    def test_same_tenant_teacher_membership_without_staff_profile_is_preserved(self):
        """학생 프로필 삭제가 같은 테넌트의 staff/teacher 권한을 제거하지 않는다."""
        teacher_user = User.objects.create_user(
            username="student_teacher_shared",
            password="test1234",
            tenant=self.tenant_a,
            is_staff=True,
            name="선생공유",
        )
        TenantMembership.ensure_active(tenant=self.tenant_a, user=teacher_user, role="teacher")
        student = Student.objects.create(
            tenant=self.tenant_a,
            user=teacher_user,
            ps_number="_del_903_TEACHER_SHARED",
            name="선생공유학생",
            phone="",
            parent_phone="01055556666",
            omr_code="55556666",
            deleted_at=timezone.now(),
        )

        permanently_delete_students(tenant=self.tenant_a, student_ids=[student.id])

        self.assertFalse(Student.objects.filter(id=student.id).exists())
        self.assertTrue(User.objects.filter(id=teacher_user.id).exists())
        self.assertTrue(
            TenantMembership.objects.filter(
                tenant=self.tenant_a,
                user=teacher_user,
                role="teacher",
                is_active=True,
            ).exists()
        )

    def test_video_comment_replies_deleted_with_student_comment(self):
        """학생 댓글에 달린 답글이 있어도 영구삭제 그래프가 댓글 트리를 정리."""
        other_user = User.objects.create_user(
            username="video_reply_student",
            password="test1234",
            tenant=self.tenant_a,
            name="댓글학생",
        )
        other_student = Student.objects.create(
            tenant=self.tenant_a,
            user=other_user,
            ps_number="A-VIDEO-REPLY",
            name="댓글학생",
            phone="01023232323",
            parent_phone="01045454545",
            omr_code="23232323",
        )
        video = Video.objects.create(
            tenant=self.tenant_a,
            session=None,
            title="댓글 테스트",
            status=Video.Status.READY,
        )
        parent_comment = VideoComment.objects.create(
            tenant=self.tenant_a,
            video=video,
            author_student=self.student_a,
            content="삭제될 댓글",
        )
        VideoComment.objects.create(
            tenant=self.tenant_a,
            video=video,
            author_student=other_student,
            parent=parent_comment,
            content="답글",
        )

        permanently_delete_students(tenant=self.tenant_a, student_ids=[self.student_a.id])

        self.assertFalse(VideoComment.objects.filter(id=parent_comment.id).exists())
        self.assertFalse(VideoComment.objects.filter(parent_id=parent_comment.id).exists())

    def test_cross_tenant_student_reference_blocks_permanent_delete(self):
        """테넌트가 틀어진 child row는 조용히 삭제하지 않고 중단."""
        SessionParticipant.objects.create(
            tenant=self.tenant_b,
            student=self.student_a,
            status=SessionParticipant.Status.BOOKED,
        )

        with self.assertRaises(StudentLifecycleError) as ctx:
            permanently_delete_students(tenant=self.tenant_a, student_ids=[self.student_a.id])

        self.assertEqual(ctx.exception.code, "cross_tenant_reference")
        self.assertTrue(Student.objects.filter(id=self.student_a.id).exists())

    def test_cross_tenant_student_id_rejected(self):
        """다른 테넌트 학생 ID → deleted=0."""
        other_user = User.objects.create_user(
            username="t_b_stu", password="test1234", tenant=self.tenant_b, name="학생B",
        )
        other_stu = Student.objects.create(
            tenant=self.tenant_b, user=other_user,
            ps_number="B001", name="학생B",
            phone="01055550000", parent_phone="01066660000", omr_code="55550000",
        )
        other_stu.deleted_at = timezone.now()
        other_stu.save(update_fields=["deleted_at"])

        resp = self._call(self.tenant_a, self.admin_a, [other_stu.id])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get("deleted"), 0)
        self.assertTrue(Student.objects.filter(id=other_stu.id).exists())

    def test_service_deletes_fee_and_section_assignment_dependencies(self):
        """영구삭제 SSOT가 현재 Student/Enrollment FK 의존 row를 함께 정리."""
        section = Section.objects.create(
            tenant=self.tenant_a,
            lecture=self.lecture_a,
            label="A",
            section_type="CLASS",
            day_of_week=0,
            start_time="10:00",
        )
        SectionAssignment.objects.create(
            tenant=self.tenant_a,
            enrollment=self.enrollment_a,
            class_section=section,
        )
        template = FeeTemplate.objects.create(
            tenant=self.tenant_a,
            name="월수강료",
            fee_type=FeeTemplate.FeeType.TUITION,
            amount=100000,
        )
        StudentFee.objects.create(
            tenant=self.tenant_a,
            student=self.student_a,
            fee_template=template,
            enrollment=self.enrollment_a,
        )
        invoice = StudentInvoice.objects.create(
            tenant=self.tenant_a,
            student=self.student_a,
            invoice_number=f"FEE-2026-05-{self.student_a.id}",
            billing_year=2026,
            billing_month=5,
            total_amount=100000,
            due_date=timezone.localdate() + timedelta(days=7),
        )
        InvoiceItem.objects.create(
            tenant=self.tenant_a,
            invoice=invoice,
            description="월수강료",
            amount=100000,
        )
        FeePayment.objects.create(
            tenant=self.tenant_a,
            invoice=invoice,
            student=self.student_a,
            amount=100000,
            payment_method="CASH",
            paid_at=timezone.now(),
        )

        result = permanently_delete_students(
            tenant=self.tenant_a,
            student_ids=[self.student_a.id],
        )

        self.assertEqual(result.deleted_count, 1)
        self.assertFalse(SectionAssignment.objects.filter(enrollment=self.enrollment_a).exists())
        self.assertFalse(StudentFee.objects.filter(student_id=self.student_a.id).exists())
        self.assertFalse(StudentInvoice.objects.filter(student_id=self.student_a.id).exists())
        self.assertFalse(InvoiceItem.objects.filter(invoice=invoice).exists())
        self.assertFalse(FeePayment.objects.filter(student_id=self.student_a.id).exists())
        self.assertFalse(Student.objects.filter(id=self.student_a.id).exists())

    def test_permanent_delete_waits_for_running_wrong_note_pdf(self):
        from django.apps import apps

        wrong_note_pdf = apps.get_model("results", "WrongNotePDF")
        wrong_note_pdf.objects.create(
            enrollment_id=self.enrollment_a.id,
            lecture_id=self.lecture_a.id,
            status="RUNNING",
        )

        with self.assertRaisesRegex(StudentLifecycleError, "PDF 생성이 끝난 뒤"):
            permanently_delete_students(
                tenant=self.tenant_a,
                student_ids=[self.student_a.id],
            )

        self.assertTrue(Student.objects.filter(id=self.student_a.id).exists())
        self.assertTrue(
            Enrollment.objects.filter(id=self.enrollment_a.id).exists()
        )

    def test_service_deletes_omr_fact_dependencies_before_submission(self):
        """OMR fact row가 붙은 submission도 학생 영구삭제에서 FK 오류 없이 정리된다."""
        run = OMRRecognitionRun.objects.create(
            tenant=self.tenant_a,
            submission=self.sub_a,
            job_id="omr-delete-test",
            status="done",
        )
        OMRDetectedAnswer.objects.create(
            tenant=self.tenant_a,
            submission=self.sub_a,
            recognition_run=run,
            question_number=1,
            exam_question_id=101,
            answer="1",
            status="detected",
        )
        OMRStudentMatch.objects.create(
            tenant=self.tenant_a,
            submission=self.sub_a,
            enrollment_id=self.enrollment_a.id,
            status=OMRStudentMatch.Status.CONFIRMED,
            method=OMRStudentMatch.Method.MANUAL,
        )

        result = permanently_delete_students(
            tenant=self.tenant_a,
            student_ids=[self.student_a.id],
        )

        self.assertEqual(result.deleted_count, 1)
        self.assertFalse(OMRDetectedAnswer.objects.filter(submission=self.sub_a).exists())
        self.assertFalse(OMRStudentMatch.objects.filter(submission=self.sub_a).exists())
        self.assertFalse(OMRRecognitionRun.objects.filter(submission=self.sub_a).exists())
        self.assertFalse(Submission.objects.filter(id=self.sub_a.id).exists())
        self.assertFalse(Student.objects.filter(id=self.student_a.id).exists())

    def test_duplicate_cleanup_command_preserves_other_tenant_user_data(self):
        """중복 삭제 정리 명령도 영구삭제 SSOT를 사용해 cross-tenant User를 보존."""
        keep_user = User.objects.create_user(
            username="dup_keep", password="test1234", tenant=self.tenant_a, name="Keep",
        )
        remove_user = User.objects.create_user(
            username="dup_remove", password="test1234", tenant=self.tenant_a, name="Remove",
        )
        TenantMembership.ensure_active(tenant=self.tenant_a, user=keep_user, role="student")
        TenantMembership.ensure_active(tenant=self.tenant_a, user=remove_user, role="student")
        TenantMembership.ensure_active(tenant=self.tenant_b, user=remove_user, role="teacher")
        keep = Student.objects.create(
            tenant=self.tenant_a,
            user=keep_user,
            ps_number="_del_900_DUPKEEP",
            name="중복학생",
            phone="",
            parent_phone="01077778888",
            omr_code="77778888",
            deleted_at=timezone.now() - timedelta(days=2),
        )
        remove = Student.objects.create(
            tenant=self.tenant_a,
            user=remove_user,
            ps_number="_del_901_DUPREMOVE",
            name="중복학생",
            phone="",
            parent_phone="01077778888",
            omr_code="77778888",
            deleted_at=timezone.now() - timedelta(days=1),
        )
        Submission.objects.create(
            tenant=self.tenant_b,
            user=remove_user,
            enrollment_id=None,
            target_type="exam",
            target_id=77,
            source="omr_manual",
            status="done",
        )

        call_command("check_deleted_student_duplicates", "--fix", stdout=StringIO())

        self.assertTrue(Student.objects.filter(id=keep.id).exists())
        self.assertFalse(Student.objects.filter(id=remove.id).exists())
        self.assertTrue(User.objects.filter(id=remove_user.id).exists())
        self.assertTrue(TenantMembership.objects.filter(tenant=self.tenant_b, user=remove_user).exists())
        self.assertTrue(Submission.objects.filter(tenant=self.tenant_b, user=remove_user).exists())

    def test_purge_deleted_students_removes_fee_dependencies(self):
        """30일 purge 명령도 lifecycle 영구삭제 그래프를 사용한다."""
        old_user = User.objects.create_user(
            username="old_deleted", password="test1234", tenant=self.tenant_a, name="Old",
        )
        TenantMembership.ensure_active(tenant=self.tenant_a, user=old_user, role="student")
        old_student = Student.objects.create(
            tenant=self.tenant_a,
            user=old_user,
            ps_number="_del_902_OLD",
            name="오래된삭제",
            phone="",
            parent_phone="01066667777",
            omr_code="66667777",
            deleted_at=timezone.now() - timedelta(days=40),
        )
        template = FeeTemplate.objects.create(
            tenant=self.tenant_a,
            name="오래된삭제수강료",
            fee_type=FeeTemplate.FeeType.TUITION,
            amount=50000,
        )
        StudentFee.objects.create(
            tenant=self.tenant_a,
            student=old_student,
            fee_template=template,
        )

        call_command("purge_deleted_students", "--days", "30", stdout=StringIO())

        self.assertFalse(Student.objects.filter(id=old_student.id).exists())
        self.assertFalse(StudentFee.objects.filter(student_id=old_student.id).exists())
