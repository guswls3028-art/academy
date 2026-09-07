from __future__ import annotations

from io import StringIO
import re
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command, CommandError
from django.test import TestCase
from django.utils import timezone

from apps.core.models import Tenant
from apps.core.management.commands.cleanup_e2e_residue import matches_residue
from apps.domains.exams.models import Exam
from apps.domains.enrollment.models import Enrollment
from apps.domains.fees.models import FeeTemplate, StudentFee
from apps.domains.inventory.models import InventoryFile
from apps.domains.lectures.models import Lecture, Session
from apps.domains.matchup.models import MatchupDocument
from apps.domains.messaging.models import (
    MessageTemplate,
    NotificationLog,
    ScheduledNotification,
)
from apps.domains.results.models import Result, ScoreEditDraft
from apps.domains.staffs.models import Staff
from apps.domains.students.models import Student
from apps.domains.submissions.models import (
    OmrUploadBatch,
    OmrUploadBatchItem,
    Submission,
)


User = get_user_model()


class CleanupE2EResidueTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            code="cleanup-e2e",
            name="Cleanup E2E",
            is_active=True,
        )

    def execute_cleanup(self):
        dry_run = StringIO()
        call_command(
            "cleanup_e2e_residue",
            "--tenant-id",
            str(self.tenant.id),
            "--dry-run",
            stdout=dry_run,
        )
        token_match = re.search(
            r"확인 토큰 \(exact targets\): ([0-9a-f]{64})",
            dry_run.getvalue(),
        )
        self.assertIsNotNone(token_match)
        call_command(
            "cleanup_e2e_residue",
            "--tenant-id",
            str(self.tenant.id),
            "--execute",
            "--confirm-token",
            token_match.group(1),
            stdout=StringIO(),
        )

    def test_fee_templates_delete_only_when_unreferenced(self):
        unreferenced = FeeTemplate.objects.create(
            tenant=self.tenant,
            name="[E2E-123456] Smoke Fee",
            fee_type=FeeTemplate.FeeType.TUITION,
            amount=1000,
        )
        referenced = FeeTemplate.objects.create(
            tenant=self.tenant,
            name="[E2E-234567] Linked Fee",
            fee_type=FeeTemplate.FeeType.TUITION,
            amount=2000,
            is_active=True,
            auto_assign=True,
        )
        user = User.objects.create_user(
            tenant=self.tenant,
            username="cleanup-e2e-student",
            password="test1234",
        )
        student = Student.objects.create(
            tenant=self.tenant,
            user=user,
            ps_number="CLEAN001",
            omr_code="CLN001",
            name="Cleanup Student",
            parent_phone="010-0000-0001",
        )
        StudentFee.objects.create(
            tenant=self.tenant,
            student=student,
            fee_template=referenced,
        )

        self.execute_cleanup()

        self.assertFalse(FeeTemplate.objects.filter(id=unreferenced.id).exists())
        referenced.refresh_from_db()
        self.assertFalse(referenced.is_active)
        self.assertFalse(referenced.auto_assign)
        self.assertTrue(StudentFee.objects.filter(fee_template=referenced).exists())

    def test_execute_requires_exact_dry_run_token(self):
        MessageTemplate.objects.create(
            tenant=self.tenant,
            category="default",
            name="[E2E-123456] Token guard",
            body="guard",
            is_system=False,
        )

        with self.assertRaisesMessage(CommandError, "확인 토큰"):
            call_command(
                "cleanup_e2e_residue",
                "--tenant-id",
                str(self.tenant.id),
                "--execute",
                stdout=StringIO(),
            )

    def test_soft_deleted_student_uses_permanent_lifecycle_cleanup(self):
        user = User.objects.create_user(
            tenant=self.tenant,
            username="e2e-residue-user",
            password="test1234",
            is_active=False,
        )
        student = Student.objects.create(
            tenant=self.tenant,
            user=user,
            ps_number="_del_999_e2e123456",
            omr_code="12345678",
            name="[E2E-123456] Student",
            parent_phone="01000000000",
            deleted_at=timezone.now(),
        )

        self.execute_cleanup()

        self.assertFalse(Student.objects.filter(id=student.id).exists())
        self.assertFalse(User.objects.filter(id=user.id).exists())

    @patch(
        "apps.infrastructure.storage.r2.head_object_r2_storage",
        return_value=(False, 0),
    )
    @patch("apps.infrastructure.storage.r2.delete_object_r2_storage")
    def test_matchup_cleanup_removes_inventory_and_r2_before_db(
        self,
        delete_object,
        _head_object,
    ):
        key = f"tenants/{self.tenant.id}/matchup/e2e-source.pdf"
        inventory = InventoryFile.objects.create(
            tenant=self.tenant,
            scope="admin",
            display_name="[E2E-123456] Matchup source",
            r2_key=key,
            original_name="source.pdf",
            size_bytes=123,
            content_type="application/pdf",
        )
        document = MatchupDocument.objects.create(
            tenant=self.tenant,
            inventory_file=inventory,
            title="[E2E-123456] Matchup source",
            r2_key=key,
            original_name="source.pdf",
            size_bytes=123,
            content_type="application/pdf",
            status="done",
        )

        self.execute_cleanup()

        self.assertFalse(MatchupDocument.objects.filter(id=document.id).exists())
        self.assertFalse(InventoryFile.objects.filter(id=inventory.id).exists())
        delete_object.assert_called_once_with(key=key)

    def test_exam_cleanup_removes_generic_result_and_submission_rows(self):
        user = User.objects.create_user(
            tenant=self.tenant,
            username="cleanup-e2e-exam-user",
            password="test1234",
        )
        exam = Exam.objects.create(
            tenant=self.tenant,
            title="[E2E-123456] Clinic remediation exam",
            exam_type=Exam.ExamType.REGULAR,
            max_score=100,
            pass_score=80,
        )
        result = Result.objects.create(
            target_type="exam",
            target_id=exam.id,
            total_score=20,
            max_score=100,
        )
        submission = Submission.objects.create(
            tenant=self.tenant,
            user=user,
            target_type=Submission.TargetType.EXAM,
            target_id=exam.id,
            source=Submission.Source.ONLINE,
            status=Submission.Status.DONE,
        )

        self.execute_cleanup()

        self.assertFalse(Exam.objects.filter(id=exam.id).exists())
        self.assertFalse(Result.objects.filter(id=result.id).exists())
        self.assertFalse(Submission.objects.filter(id=submission.id).exists())

    @patch(
        "apps.infrastructure.storage.r2.head_object_r2_storage",
        return_value=(False, 0),
    )
    @patch("apps.infrastructure.storage.r2.delete_object_r2_storage")
    def test_exam_cleanup_removes_omr_batch_graph_and_scan_objects(
        self,
        delete_object,
        _head_object,
    ):
        student_user = User.objects.create_user(
            tenant=self.tenant,
            username="cleanup-e2e-omr-user",
            password="test1234",
            is_active=False,
        )
        uploader = User.objects.create_user(
            tenant=self.tenant,
            username="cleanup-e2e-omr-uploader",
            password="test1234",
        )
        student = Student.objects.create(
            tenant=self.tenant,
            user=student_user,
            ps_number="_del_777_e2eomr123456",
            omr_code="OMR12345",
            name="[E2E-123456] OMR Student",
            deleted_at=timezone.now(),
        )
        lecture = Lecture.objects.create(
            tenant=self.tenant,
            title="[E2E-123456] OMR lecture",
            name="E2E",
            subject="MATH",
        )
        enrollment = Enrollment.objects.create(
            tenant=self.tenant,
            student=student,
            lecture=lecture,
            status="INACTIVE",
        )
        exam = Exam.objects.create(
            tenant=self.tenant,
            title="[E2E-123456] OMR exam",
            exam_type=Exam.ExamType.REGULAR,
            max_score=100,
            pass_score=80,
        )
        submission = Submission.objects.create(
            tenant=self.tenant,
            user=uploader,
            enrollment=enrollment,
            target_type=Submission.TargetType.EXAM,
            target_id=exam.id,
            source=Submission.Source.OMR_SCAN,
            status=Submission.Status.DONE,
            file_key=None,
            meta={
                "ai_result": {
                    "result": {
                        "aligned_image_key": ""
                    }
                }
            },
        )
        original_key = (
            f"tenants/{self.tenant.id}/ai/submissions/{submission.id}/original.pdf"
        )
        aligned_key = (
            f"tenants/{self.tenant.id}/ai/submissions/{submission.id}/aligned/job.jpg"
        )
        legacy_aligned_key = (
            f"tenants/{self.tenant.id}/ai/submissions/{submission.id}/aligned/legacy.jpg"
        )
        submission.file_key = original_key
        submission.meta["ai_result"]["result"]["aligned_image_key"] = aligned_key
        submission.meta["ai_result"]["result"][
            "aligned_scan_image_key"
        ] = legacy_aligned_key
        submission.save(update_fields=["file_key", "meta"])
        batch = OmrUploadBatch.objects.create(
            tenant=self.tenant,
            created_by=uploader,
            exam_id=exam.id,
            lecture_id=lecture.id,
            total_count=1,
        )
        item = OmrUploadBatchItem.objects.create(
            tenant=self.tenant,
            exam_id=exam.id,
            batch=batch,
            ordinal=1,
            submission=submission,
            content_sha256="a" * 64,
            admission_status=OmrUploadBatchItem.AdmissionStatus.RECEIVED,
        )

        self.execute_cleanup()

        self.assertFalse(Student.objects.filter(id=student.id).exists())
        self.assertFalse(Exam.objects.filter(id=exam.id).exists())
        self.assertFalse(Submission.objects.filter(id=submission.id).exists())
        self.assertFalse(OmrUploadBatch.objects.filter(id=batch.id).exists())
        self.assertFalse(OmrUploadBatchItem.objects.filter(id=item.id).exists())
        self.assertEqual(
            {call.kwargs["key"] for call in delete_object.call_args_list},
            {original_key, aligned_key, legacy_aligned_key},
        )
        self.assertEqual(NotificationLog.objects.count(), 0)
        self.assertEqual(ScheduledNotification.objects.count(), 0)

    def test_exam_cleanup_refuses_omr_batch_item_linked_outside_target_exam(self):
        uploader = User.objects.create_user(
            tenant=self.tenant,
            username="cleanup-e2e-cross-exam-uploader",
            password="test1234",
        )
        target_exam = Exam.objects.create(
            tenant=self.tenant,
            title="[E2E-123456] Target OMR exam",
            exam_type=Exam.ExamType.REGULAR,
            max_score=100,
            pass_score=80,
        )
        normal_exam = Exam.objects.create(
            tenant=self.tenant,
            title="사용자 정규 시험",
            exam_type=Exam.ExamType.REGULAR,
            max_score=100,
            pass_score=80,
        )
        normal_submission = Submission.objects.create(
            tenant=self.tenant,
            user=uploader,
            target_type=Submission.TargetType.EXAM,
            target_id=normal_exam.id,
            source=Submission.Source.OMR_SCAN,
            status=Submission.Status.DONE,
        )
        batch = OmrUploadBatch.objects.create(
            tenant=self.tenant,
            created_by=uploader,
            exam_id=target_exam.id,
            total_count=1,
        )
        item = OmrUploadBatchItem.objects.create(
            tenant=self.tenant,
            exam_id=target_exam.id,
            batch=batch,
            ordinal=1,
            submission=normal_submission,
            content_sha256="b" * 64,
            admission_status=OmrUploadBatchItem.AdmissionStatus.RECEIVED,
        )

        with self.assertRaisesMessage(CommandError, "exact target 밖"):
            self.execute_cleanup()

        self.assertTrue(Exam.objects.filter(id=target_exam.id).exists())
        self.assertTrue(Exam.objects.filter(id=normal_exam.id).exists())
        self.assertTrue(Submission.objects.filter(id=normal_submission.id).exists())
        self.assertTrue(OmrUploadBatch.objects.filter(id=batch.id).exists())
        self.assertTrue(OmrUploadBatchItem.objects.filter(id=item.id).exists())

    def test_legacy_omr_timestamp_graph_is_strict_residue(self):
        user = User.objects.create_user(
            tenant=self.tenant,
            username="cleanup-e2e-legacy-omr",
            password="test1234",
        )
        marker = "[E2E-OMR-REAL-20260524143859]"
        lecture = Lecture.objects.create(
            tenant=self.tenant,
            title=f"{marker} OMR 실측 25장 채점 검증",
            name="E2E",
            subject="MATH",
        )
        session = Session.objects.create(
            lecture=lecture,
            title=f"{marker} 실측 OMR",
            order=1,
        )
        exam = Exam.objects.create(
            tenant=self.tenant,
            title=f"{marker} OMR 실측 배치 시험",
            exam_type=Exam.ExamType.REGULAR,
            max_score=100,
            pass_score=80,
        )
        exam.sessions.add(session)
        result = Result.objects.create(
            target_type="exam",
            target_id=exam.id,
            total_score=100,
            max_score=100,
        )
        submission = Submission.objects.create(
            tenant=self.tenant,
            user=user,
            target_type=Submission.TargetType.EXAM,
            target_id=exam.id,
            source=Submission.Source.OMR_SCAN,
            status=Submission.Status.DONE,
        )

        self.assertTrue(matches_residue(lecture.title))
        self.assertFalse(matches_residue("[E2E-OMR] 사용자 강의"))
        self.assertFalse(matches_residue("[E2E-OMR-20260524] 사용자 강의"))

        self.execute_cleanup()

        self.assertFalse(Lecture.objects.filter(id=lecture.id).exists())
        self.assertFalse(Session.objects.filter(id=session.id).exists())
        self.assertFalse(Exam.objects.filter(id=exam.id).exists())
        self.assertFalse(Result.objects.filter(id=result.id).exists())
        self.assertFalse(Submission.objects.filter(id=submission.id).exists())

    def test_labeled_unix_timestamp_and_exact_legacy_titles_are_strict(self):
        self.assertTrue(matches_residue("[E2E-cut-fix-1778777196] 자료"))
        self.assertTrue(matches_residue("[E2E-selflike-test]"))
        self.assertTrue(matches_residue("[E2E-test] board"))
        self.assertTrue(matches_residue("[E2E-persist-test]"))
        self.assertTrue(
            matches_residue("[E2E-SCORE-AUDIT-20260517-02] 감사 관리자")
        )
        self.assertFalse(matches_residue("[E2E-cut-fix-verify] 자료"))
        self.assertFalse(matches_residue("[E2E-OMR-20260524] 사용자 강의"))
        self.assertFalse(matches_residue("[E2E-user-note] 사용자 메모"))

    def test_lecture_cleanup_removes_marker_session_and_score_draft(self):
        editor = User.objects.create_user(
            tenant=self.tenant,
            username="cleanup-e2e-editor",
            password="test1234",
        )
        lecture = Lecture.objects.create(
            tenant=self.tenant,
            title="[E2E-123456] OMR lecture",
            name="E2E",
            subject="MATH",
        )
        session = Session.objects.create(
            lecture=lecture,
            title="[E2E-123456] OMR session",
            order=1,
        )
        draft = ScoreEditDraft.objects.create(
            tenant=self.tenant,
            session=session,
            editor_user=editor,
            payload={"client_id": "e2e", "changes": []},
        )

        self.execute_cleanup()

        self.assertFalse(Lecture.objects.filter(id=lecture.id).exists())
        self.assertFalse(Session.objects.filter(id=session.id).exists())
        self.assertFalse(ScoreEditDraft.objects.filter(id=draft.id).exists())

    def test_lecture_cleanup_refuses_non_marker_child_session(self):
        lecture = Lecture.objects.create(
            tenant=self.tenant,
            title="[E2E-123456] Mixed lecture",
            name="E2E",
            subject="MATH",
        )
        session = Session.objects.create(
            lecture=lecture,
            title="사용자 정규 차시",
            order=1,
        )

        with self.assertRaisesMessage(CommandError, "strict marker"):
            self.execute_cleanup()

        self.assertTrue(Lecture.objects.filter(id=lecture.id).exists())
        self.assertTrue(Session.objects.filter(id=session.id).exists())

    def test_lecture_cleanup_refuses_non_marker_child_exam(self):
        marker = "[E2E-OMR-20260524103023]"
        lecture = Lecture.objects.create(
            tenant=self.tenant,
            title=f"{marker} OMR 실사용 검증",
            name="E2E",
            subject="MATH",
        )
        session = Session.objects.create(
            lecture=lecture,
            title=f"{marker} 1차시",
            order=1,
        )
        exam = Exam.objects.create(
            tenant=self.tenant,
            title="사용자 정규 시험",
            exam_type=Exam.ExamType.REGULAR,
            max_score=100,
            pass_score=80,
        )
        exam.sessions.add(session)

        with self.assertRaisesMessage(CommandError, "strict marker 대상이 아닌 시험"):
            self.execute_cleanup()

        self.assertTrue(Lecture.objects.filter(id=lecture.id).exists())
        self.assertTrue(Session.objects.filter(id=session.id).exists())
        self.assertTrue(Exam.objects.filter(id=exam.id).exists())

    def test_only_explicit_e2e_template_residue_is_removed(self):
        residue = MessageTemplate.objects.create(
            tenant=self.tenant,
            category="default",
            name="복사 - 복사 - 복사 - [E2E-123456] 출석 안내",
            body="E2E residue",
            is_system=False,
        )
        normal_copy = MessageTemplate.objects.create(
            tenant=self.tenant,
            category="default",
            name="복사 - 학부모 안내",
            body="사용자 문구",
            is_system=False,
        )

        self.execute_cleanup()

        self.assertFalse(MessageTemplate.objects.filter(id=residue.id).exists())
        self.assertTrue(MessageTemplate.objects.filter(id=normal_copy.id).exists())

    def test_recursive_copy_prefix_without_e2e_marker_is_preserved(self):
        legitimate = MessageTemplate.objects.create(
            tenant=self.tenant,
            category="default",
            name="복사 - 복사 - 복사 - 정식 학부모 안내",
            body="사용자가 반복 복제한 정식 문구",
            is_system=False,
        )

        self.execute_cleanup()

        self.assertTrue(MessageTemplate.objects.filter(id=legitimate.id).exists())

    def test_high_confidence_recursive_copy_chain_keeps_root_only(self):
        root = MessageTemplate.objects.create(
            tenant=self.tenant,
            category="default",
            name="정식 학부모 안내",
            body="같은 본문",
            is_system=False,
        )
        copies = [
            MessageTemplate.objects.create(
                tenant=self.tenant,
                category="default",
                name=f"{'복사 - ' * depth}정식 학부모 안내",
                body="같은 본문",
                is_system=False,
            )
            for depth in range(1, 11)
        ]

        self.execute_cleanup()

        self.assertTrue(MessageTemplate.objects.filter(id=root.id).exists())
        self.assertFalse(
            MessageTemplate.objects.filter(id__in=[copy.id for copy in copies]).exists()
        )

    def test_strict_userless_staff_without_history_is_removed(self):
        staff = Staff.objects.create(
            tenant=self.tenant,
            name="[E2E-SCORE-AUDIT-20260517-02] 감사 관리자",
            phone="",
            is_manager=True,
        )

        self.execute_cleanup()

        self.assertFalse(Staff.objects.filter(id=staff.id).exists())

    def test_recursive_copy_prefix_never_deletes_non_template_business_data(self):
        user = User.objects.create_user(
            tenant=self.tenant,
            username="legitimate-copy-name",
            password="test1234",
        )
        student = Student.objects.create(
            tenant=self.tenant,
            user=user,
            ps_number="COPY001",
            omr_code="CPY001",
            name="복사 - 복사 - 복사 - 정식 등록 학생",
            parent_phone="010-0000-0002",
        )

        self.execute_cleanup()

        self.assertTrue(Student.objects.filter(id=student.id).exists())
