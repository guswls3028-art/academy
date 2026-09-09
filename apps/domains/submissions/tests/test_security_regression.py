# PATH: apps/domains/submissions/tests/test_security_regression.py
"""
보안 회귀 — 2026-04-25 정밀검사에서 차단한 학생/학부모 권한 누출 경로.

검증 대상:
  C-1  SubmissionViewSet — 학생이 list/retrieve/retry/manual_edit/discard 불가
  C-1b SubmissionViewSet — 학생 본인 enrollment의 create는 정상 통과
  C-2  PendingSubmissionsView — 학생 차단, 교사·어드민 통과
  C-2  ExamSubmissionsListView — 학생 차단
  C-2  HomeworkSubmissionsListView — 학생 차단
  C-3  ExamOMRSubmitView — 학생 차단 (운영자 대리 업로드 경로)

권한 게이트 단계 (HTTP) 로 검증한다. 200/403/401 만 본다 — 비즈니스 로직은
별도 테스트가 커버.
"""
from __future__ import annotations

from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError
from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.core.models.tenant import Tenant
from apps.core.models.tenant_membership import TenantMembership
from apps.core.models.user import user_internal_username
from apps.domains.students.models import Student
from apps.domains.lectures.models import Lecture, Session as LectureSession
from apps.domains.enrollment.models import Enrollment, SessionEnrollment
from apps.domains.exams.models import Exam, ExamEnrollment, ExamQuestion, Sheet
from apps.domains.submissions.models import (
    Submission,
    SubmissionAnswer,
    SubmissionStorageCleanupIntent,
)
from apps.domains.submissions.services.ai_omr_result_mapper import apply_omr_ai_result
from apps.domains.submissions.views.submission_view import SubmissionViewSet
from apps.domains.submissions.views.pending_submissions_view import (
    PendingSubmissionPreviewView,
    PendingSubmissionsView,
)
from apps.domains.submissions.views.exam_submissions_list_view import ExamSubmissionsListView
from apps.domains.submissions.views.homework_submissions_list_view import HomeworkSubmissionsListView
from apps.domains.submissions.views.exam_omr_submit_view import ExamOMRSubmitView
from apps.domains.results.views.admin_landing_stats_view import AdminResultsLandingStatsView
from apps.domains.video.views.admin_landing_stats_view import AdminVideosLandingStatsView

User = get_user_model()


def _make_tenant(name, code):
    return Tenant.objects.create(name=name, code=code, is_active=True)


def _make_admin(tenant, username, role="owner"):
    u = User.objects.create_user(
        username=username, password="test1234",
        tenant=tenant, is_staff=True, name=f"Admin-{username}",
    )
    TenantMembership.ensure_active(tenant=tenant, user=u, role=role)
    return u


def _make_teacher(tenant, username):
    u = User.objects.create_user(
        username=username, password="test1234",
        tenant=tenant, name=f"Teacher-{username}",
    )
    TenantMembership.ensure_active(tenant=tenant, user=u, role="teacher")
    return u


def _make_student_user_and_profile(tenant, ps_number, name="학생"):
    internal = user_internal_username(tenant, ps_number)
    user = User.objects.create_user(
        username=internal, password="test1234",
        tenant=tenant, phone=f"010-1111-{ps_number[-4:]:>04}",
        name=name,
    )
    student = Student.objects.create(
        tenant=tenant, user=user,
        ps_number=ps_number, name=name,
        phone=f"010-1111-{ps_number[-4:]:>04}",
        parent_phone=f"010-2222-{ps_number[-4:]:>04}",
        omr_code=ps_number[:8].rjust(8, "0"),
    )
    TenantMembership.ensure_active(tenant=tenant, user=user, role="student")
    return user, student


class _SecurityFixtureMixin:
    """공통 fixture — 1 tenant + 학생 1 + 교사 1 + 어드민 1."""

    def _setup_fixtures(self):
        self.factory = APIRequestFactory()
        self.tenant = _make_tenant("SecAcademy", "sec_test")

        self.admin = _make_admin(self.tenant, "sec_admin")
        self.teacher = _make_teacher(self.tenant, "sec_teacher")
        self.student_user, self.student = _make_student_user_and_profile(
            self.tenant, "S001"
        )

        # lecture/session/exam — submission_view.create 검증용
        self.lecture = Lecture.objects.create(
            tenant=self.tenant, title="Math", name="Math", subject="MATH",
        )
        self.session = LectureSession.objects.create(
            lecture=self.lecture, order=1, title="S1",
        )
        self.enrollment = Enrollment.objects.create(
            tenant=self.tenant, student=self.student,
            lecture=self.lecture, status="ACTIVE",
        )
        SessionEnrollment.objects.create(
            tenant=self.tenant,
            session=self.session,
            enrollment=self.enrollment,
        )
        self.exam = Exam.objects.create(
            tenant=self.tenant,
            title="Test Exam",
            exam_type=Exam.ExamType.REGULAR,
            pass_score=60, max_score=100, max_attempts=1,
        )
        self.exam.sessions.add(self.session)

        # 학생이 자기 것이 아닌 다른 학생의 submission — list/retrieve 누출 확인용
        self.peer_user, self.peer_student = _make_student_user_and_profile(
            self.tenant, "S002", name="피어"
        )
        self.peer_enrollment = Enrollment.objects.create(
            tenant=self.tenant, student=self.peer_student,
            lecture=self.lecture, status="ACTIVE",
        )
        SessionEnrollment.objects.create(
            tenant=self.tenant,
            session=self.session,
            enrollment=self.peer_enrollment,
        )
        self.peer_submission = Submission.objects.create(
            tenant=self.tenant, user=self.peer_user,
            enrollment_id=self.peer_enrollment.id,
            target_type=Submission.TargetType.EXAM,
            target_id=self.exam.id,
            source=Submission.Source.OMR_SCAN,
            file_key="tenants/x/peer.png",
        )

    def _call(self, view_factory, method, path, user, *,
              tenant=None, data=None, **view_kwargs):
        m = method.lower()
        if m == "get":
            req = self.factory.get(path)
        elif m == "post":
            req = self.factory.post(path, data=data, format="json")
        else:
            raise ValueError(f"unsupported method {m}")
        force_authenticate(req, user=user)
        req.tenant = tenant or self.tenant
        return view_factory()(req, **view_kwargs)


# ═══════════════════════════════════════════════════
# C-1 SubmissionViewSet — list / retrieve / actions
# ═══════════════════════════════════════════════════

class TestC1SubmissionViewSetGuard(_SecurityFixtureMixin, TestCase):

    def setUp(self):
        self._setup_fixtures()

    # --- list ---

    def test_student_list_blocked(self):
        """학생 → SubmissionViewSet.list → 403 (Staff 전용)."""
        view = SubmissionViewSet.as_view({"get": "list"})
        resp = self._call(lambda: view, "get", "/api/v1/submissions/submissions/",
                          user=self.student_user)
        self.assertEqual(resp.status_code, 403,
                         f"CRITICAL: 학생이 list에 접근 가능 (peer.id={self.peer_submission.id})")

    def test_teacher_list_allowed(self):
        """교사 → SubmissionViewSet.list → 200."""
        view = SubmissionViewSet.as_view({"get": "list"})
        resp = self._call(lambda: view, "get", "/api/v1/submissions/submissions/",
                          user=self.teacher)
        self.assertEqual(resp.status_code, 200)

    def test_admin_list_allowed(self):
        """어드민 → SubmissionViewSet.list → 200."""
        view = SubmissionViewSet.as_view({"get": "list"})
        resp = self._call(lambda: view, "get", "/api/v1/submissions/submissions/",
                          user=self.admin)
        self.assertEqual(resp.status_code, 200)

    # --- retrieve ---

    def test_student_retrieve_peer_blocked(self):
        """학생 → SubmissionViewSet.retrieve(타인) → 403."""
        view = SubmissionViewSet.as_view({"get": "retrieve"})
        resp = self._call(lambda: view, "get",
                          f"/api/v1/submissions/submissions/{self.peer_submission.id}/",
                          user=self.student_user, pk=self.peer_submission.id)
        self.assertEqual(resp.status_code, 403)

    # --- actions ---

    def test_student_retry_blocked(self):
        """학생 → retry → 403 (Staff 전용)."""
        view = SubmissionViewSet.as_view({"post": "retry"})
        resp = self._call(lambda: view, "post",
                          f"/api/v1/submissions/submissions/{self.peer_submission.id}/retry/",
                          user=self.student_user, data={}, pk=self.peer_submission.id)
        self.assertEqual(resp.status_code, 403)

    def test_student_manual_edit_blocked(self):
        """학생 → manual_edit (타인 점수 덮어쓰기 시도) → 403."""
        view = SubmissionViewSet.as_view({"post": "manual_edit"})
        resp = self._call(lambda: view, "post",
                          f"/api/v1/submissions/submissions/{self.peer_submission.id}/manual-edit/",
                          user=self.student_user,
                          data={"identifier": {"enrollment_id": self.enrollment.id},
                                "answers": []},
                          pk=self.peer_submission.id)
        self.assertEqual(resp.status_code, 403,
                         "CRITICAL: 학생이 타인 답안 manual_edit 가능 — "
                         "다른 학생 성적 덮어쓰기 경로!")

    def test_manual_edit_needs_identification_requires_enrollment(self):
        """식별 필요 OMR은 학생 매칭 없이 answers_ready/채점으로 넘어갈 수 없다."""
        sub = Submission.objects.create(
            tenant=self.tenant,
            user=self.admin,
            target_type=Submission.TargetType.EXAM,
            target_id=self.exam.id,
            source=Submission.Source.OMR_SCAN,
            status=Submission.Status.NEEDS_IDENTIFICATION,
            enrollment_id=None,
        )
        view = SubmissionViewSet.as_view({"post": "manual_edit"})
        resp = self._call(lambda: view, "post",
                          f"/api/v1/submissions/submissions/{sub.id}/manual-edit/",
                          user=self.admin,
                          data={"answers": []},
                          pk=sub.id)
        self.assertEqual(resp.status_code, 400, resp.data)

    def test_manual_edit_rejects_foreign_tenant_question_id(self):
        """수동 답안 수정은 해당 시험/테넌트 문항 id만 허용한다."""
        local_sheet = Sheet.objects.create(exam=self.exam, name="MAIN")
        ExamQuestion.objects.create(sheet=local_sheet, number=1, score=5)
        other_tenant = _make_tenant("Other Question Tenant", "other_question")
        other_exam = Exam.objects.create(
            tenant=other_tenant,
            title="Other Exam",
            exam_type=Exam.ExamType.REGULAR,
        )
        other_sheet = Sheet.objects.create(exam=other_exam, name="MAIN")
        foreign_question = ExamQuestion.objects.create(sheet=other_sheet, number=1, score=5)

        sub = Submission.objects.create(
            tenant=self.tenant,
            user=self.admin,
            enrollment_id=self.enrollment.id,
            target_type=Submission.TargetType.EXAM,
            target_id=self.exam.id,
            source=Submission.Source.OMR_SCAN,
            status=Submission.Status.SUBMITTED,
        )

        view = SubmissionViewSet.as_view({"post": "manual_edit"})
        resp = self._call(
            lambda: view,
            "post",
            f"/api/v1/submissions/submissions/{sub.id}/manual-edit/",
            user=self.admin,
            data={
                "identifier": {"enrollment_id": self.enrollment.id},
                "answers": [{"question_id": foreign_question.id, "answer": "1"}],
            },
            pk=sub.id,
        )

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertFalse(SubmissionAnswer.objects.filter(submission=sub).exists())

    def test_student_discard_blocked(self):
        """학생 → discard → 403."""
        view = SubmissionViewSet.as_view({"post": "discard"})
        resp = self._call(lambda: view, "post",
                          f"/api/v1/submissions/submissions/{self.peer_submission.id}/discard/",
                          user=self.student_user, data={"reason": "abuse"},
                          pk=self.peer_submission.id)
        self.assertEqual(resp.status_code, 403)


# ═══════════════════════════════════════════════════
# C-1b SubmissionViewSet.create — 학생 본인 enrollment는 통과
# ═══════════════════════════════════════════════════

class TestC1bStudentCreateAllowed(_SecurityFixtureMixin, TestCase):
    """학생이 본인 enrollment로 POST 하는 경로(=학생 앱 제출)는 닫히지 않아야 한다."""

    def setUp(self):
        self._setup_fixtures()

    def test_student_create_own_enrollment_passes_permission(self):
        """학생 → POST /submissions/submissions/ (본인 enrollment) → 권한 단계 통과.

        permission 단계만 검증 — dispatch_submission(채점 호출)은 격리해
        권한 결과만 본다.
        """
        view = SubmissionViewSet.as_view({"post": "create"})
        with patch("apps.domains.submissions.views.submission_view.dispatch_submission"):
            resp = self._call(lambda: view, "post",
                              "/api/v1/submissions/submissions/",
                              user=self.student_user,
                              data={
                                  "target_type": "exam",
                                  "target_id": self.exam.id,
                                  "source": "online",
                                  "enrollment_id": self.enrollment.id,
                                  "payload": {"answers": []},
                              })
        # permission 통과 = 401/403 이 아님.
        self.assertNotIn(resp.status_code, (401, 403),
                         f"학생 본인 enrollment 제출이 권한에서 막힘: "
                         f"{resp.status_code} {getattr(resp, 'data', '')}")

    @patch(
        "apps.domains.submissions.views.submission_view.schedule_unreferenced_ai_object_cleanup"
    )
    @patch(
        "apps.domains.submissions.views.submission_view.SubmissionCreateSerializer.save",
        autospec=True,
    )
    def test_generic_create_commit_failure_schedules_uploaded_object_cleanup(
        self,
        serializer_save,
        schedule_cleanup,
    ):
        key = f"tenants/{self.tenant.id}/ai/submissions/999/generic.jpg"

        def fail_after_upload(serializer, **_kwargs):
            serializer.uploaded_object_key = key
            serializer.uploaded_tenant_id = self.tenant.id
            raise IntegrityError("commit failed after upload")

        serializer_save.side_effect = fail_after_upload
        view = SubmissionViewSet.as_view({"post": "create"})
        response = self._call(
            lambda: view,
            "post",
            "/api/v1/submissions/submissions/",
            user=self.student_user,
            data={
                "target_type": "exam",
                "target_id": self.exam.id,
                "source": "online",
                "enrollment_id": self.enrollment.id,
                "payload": {"answers": []},
            },
        )

        self.assertEqual(response.status_code, 503, response.data)
        self.assertEqual(response.data["code"], "submission_storage_cleanup_pending")
        schedule_cleanup.assert_called_once_with(tenant_id=self.tenant.id, key=key)

    def test_student_create_peer_enrollment_blocked(self):
        """학생 → POST /submissions/submissions/ (타인 enrollment) → 403 (perform_create 소유권 검증)."""
        view = SubmissionViewSet.as_view({"post": "create"})
        with patch("apps.domains.submissions.views.submission_view.dispatch_submission"):
            resp = self._call(lambda: view, "post",
                              "/api/v1/submissions/submissions/",
                              user=self.student_user,
                              data={
                                  "target_type": "exam",
                                  "target_id": self.exam.id,
                                  "source": "online",
                                  "enrollment_id": self.peer_enrollment.id,
                                  "payload": {"answers": []},
                              })
        self.assertEqual(resp.status_code, 403,
                         "CRITICAL: 학생이 타인 enrollment_id로 제출 가능!")

    def test_student_create_for_ended_lecture_blocked(self):
        """종료된 강의는 활성 enrollment가 남아 있어도 새 제출을 받지 않는다."""
        self.lecture.is_active = False
        self.lecture.save(update_fields=["is_active"])

        view = SubmissionViewSet.as_view({"post": "create"})
        with patch("apps.domains.submissions.views.submission_view.dispatch_submission") as mock_dispatch:
            resp = self._call(
                lambda: view,
                "post",
                "/api/v1/submissions/submissions/",
                user=self.student_user,
                data={
                    "target_type": "exam",
                    "target_id": self.exam.id,
                    "source": "online",
                    "enrollment_id": self.enrollment.id,
                    "payload": {"answers": []},
                },
            )

        self.assertEqual(resp.status_code, 403, resp.data)
        mock_dispatch.assert_not_called()
        self.assertFalse(
            Submission.objects.filter(
                tenant=self.tenant,
                target_type=Submission.TargetType.EXAM,
                target_id=self.exam.id,
                enrollment_id=self.enrollment.id,
            ).exists()
        )

    def test_create_rejects_enrollment_not_assigned_to_exam(self):
        """같은 테넌트라도 다른 강의 enrollment로 시험 제출 생성 불가."""
        other_lecture = Lecture.objects.create(
            tenant=self.tenant,
            title="Other Math",
            name="Other Math",
            subject="MATH",
        )
        other_session = LectureSession.objects.create(
            lecture=other_lecture,
            order=1,
            title="Other S1",
        )
        other_exam = Exam.objects.create(
            tenant=self.tenant,
            title="Other Exam",
            exam_type=Exam.ExamType.REGULAR,
        )
        other_exam.sessions.add(other_session)

        view = SubmissionViewSet.as_view({"post": "create"})
        with patch("apps.domains.submissions.views.submission_view.dispatch_submission") as mock_dispatch:
            resp = self._call(
                lambda: view,
                "post",
                "/api/v1/submissions/submissions/",
                user=self.student_user,
                data={
                    "target_type": "exam",
                    "target_id": other_exam.id,
                    "source": "online",
                    "enrollment_id": self.enrollment.id,
                    "payload": {"answers": []},
                },
            )

        self.assertEqual(resp.status_code, 403, resp.data)
        mock_dispatch.assert_not_called()
        self.assertFalse(
            Submission.objects.filter(
                tenant=self.tenant,
                target_type=Submission.TargetType.EXAM,
                target_id=other_exam.id,
                enrollment_id=self.enrollment.id,
            ).exists()
        )

    def test_create_rejects_foreign_exam_even_if_attached_to_local_session(self):
        """잘못된 M2M drift가 있어도 foreign tenant exam 대상으로 제출 생성 불가."""
        other_tenant = _make_tenant("ForeignExamAcademy", "foreign_exam")
        foreign_exam = Exam.objects.create(
            tenant=other_tenant,
            title="Foreign Exam",
            exam_type=Exam.ExamType.REGULAR,
        )
        foreign_exam.sessions.add(self.session)

        view = SubmissionViewSet.as_view({"post": "create"})
        with patch("apps.domains.submissions.views.submission_view.dispatch_submission") as mock_dispatch:
            resp = self._call(
                lambda: view,
                "post",
                "/api/v1/submissions/submissions/",
                user=self.student_user,
                data={
                    "target_type": "exam",
                    "target_id": foreign_exam.id,
                    "source": "online",
                    "enrollment_id": self.enrollment.id,
                    "payload": {"answers": []},
                },
            )

        self.assertEqual(resp.status_code, 403, resp.data)
        mock_dispatch.assert_not_called()
        self.assertFalse(
            Submission.objects.filter(
                tenant=self.tenant,
                target_type=Submission.TargetType.EXAM,
                target_id=foreign_exam.id,
                enrollment_id=self.enrollment.id,
            ).exists()
        )

    def test_teacher_create_cross_tenant_enrollment_blocked(self):
        """스태프라도 현재 tenant 밖 enrollment_id는 제출에 사용할 수 없다."""
        other_tenant = _make_tenant("OtherSubmitAcademy", "submit_other")
        other_user, other_student = _make_student_user_and_profile(
            other_tenant, "OS001", name="타학원학생",
        )
        other_lecture = Lecture.objects.create(
            tenant=other_tenant, title="Other Math", name="Other Math", subject="MATH",
        )
        other_enrollment = Enrollment.objects.create(
            tenant=other_tenant,
            student=other_student,
            lecture=other_lecture,
            status="ACTIVE",
        )
        view = SubmissionViewSet.as_view({"post": "create"})
        with patch("apps.domains.submissions.views.submission_view.dispatch_submission"):
            resp = self._call(lambda: view, "post",
                              "/api/v1/submissions/submissions/",
                              user=self.teacher,
                              data={
                                  "target_type": "exam",
                                  "target_id": self.exam.id,
                                  "source": "online",
                                  "enrollment_id": other_enrollment.id,
                                  "payload": {"answers": []},
                              })
        self.assertEqual(resp.status_code, 403)

    @patch("apps.domains.submissions.views.submission_view.dispatch_submission")
    @patch("apps.domains.submissions.serializers.submission.upload_fileobj_to_r2")
    def test_student_create_omr_scan_blocked(self, upload_fileobj_to_r2, dispatch_submission):
        """학생은 일반 create 경로로 OMR_SCAN/AI job을 만들 수 없다."""
        view = SubmissionViewSet.as_view({"post": "create"})
        upload = SimpleUploadedFile("student-omr.png", b"fake-image", content_type="image/png")
        req = self.factory.post(
            "/api/v1/submissions/submissions/",
            data={
                "target_type": "exam",
                "target_id": self.exam.id,
                "source": Submission.Source.OMR_SCAN,
                "file": upload,
            },
            format="multipart",
        )
        force_authenticate(req, user=self.student_user)
        req.tenant = self.tenant

        resp = view(req)

        self.assertEqual(resp.status_code, 403, resp.data)
        upload_fileobj_to_r2.assert_not_called()
        dispatch_submission.assert_not_called()

    @patch("apps.domains.submissions.views.submission_view.dispatch_submission")
    @patch("apps.domains.submissions.serializers.submission.upload_fileobj_to_r2")
    def test_admin_omr_upload_same_tenant_non_candidate_enrollment_blocked(
        self,
        upload_fileobj_to_r2,
        dispatch_submission,
    ):
        """운영자 OMR 업로드도 현재 시험/차시 대상자가 아닌 enrollment_id는 거부한다."""
        Sheet.objects.create(exam=self.exam, name="MAIN", total_questions=1)
        other_user, other_student = _make_student_user_and_profile(
            self.tenant, "S999", name="비대상학생",
        )
        other_lecture = Lecture.objects.create(
            tenant=self.tenant,
            title="Non Candidate Lecture",
            name="Non Candidate Lecture",
            subject="MATH",
        )
        non_candidate_enrollment = Enrollment.objects.create(
            tenant=self.tenant,
            student=other_student,
            lecture=other_lecture,
            status="ACTIVE",
        )
        view = SubmissionViewSet.as_view({"post": "admin_omr_upload"})
        upload = SimpleUploadedFile("scan.png", b"\x89PNG\r\n\x1a\nfake-image", content_type="image/png")
        req = self.factory.post(
            "/api/v1/submissions/submissions/admin/omr-upload/",
            data={
                "target_id": self.exam.id,
                "enrollment_id": non_candidate_enrollment.id,
                "file": upload,
            },
            format="multipart",
        )
        force_authenticate(req, user=self.admin)
        req.tenant = self.tenant

        resp = view(req)

        self.assertEqual(resp.status_code, 403, resp.data)
        upload_fileobj_to_r2.assert_not_called()
        dispatch_submission.assert_not_called()

    @patch(
        "apps.domains.submissions.views.submission_view.schedule_unreferenced_ai_object_cleanup"
    )
    @patch(
        "apps.domains.submissions.views.submission_view.SubmissionCreateSerializer.save",
        autospec=True,
    )
    def test_admin_omr_commit_failure_schedules_uploaded_object_cleanup(
        self,
        serializer_save,
        schedule_cleanup,
    ):
        Sheet.objects.create(exam=self.exam, name="MAIN", total_questions=1)
        key = f"tenants/{self.tenant.id}/ai/submissions/998/admin.jpg"

        def fail_after_upload(serializer, **_kwargs):
            serializer.uploaded_object_key = key
            serializer.uploaded_tenant_id = self.tenant.id
            raise IntegrityError("commit failed after upload")

        serializer_save.side_effect = fail_after_upload
        view = SubmissionViewSet.as_view({"post": "admin_omr_upload"})
        upload = SimpleUploadedFile(
            "admin.png",
            b"\x89PNG\r\n\x1a\nadmin",
            content_type="image/png",
        )
        request = self.factory.post(
            "/api/v1/submissions/submissions/admin/omr-upload/",
            data={
                "target_id": self.exam.id,
                "enrollment_id": self.enrollment.id,
                "file": upload,
            },
            format="multipart",
        )
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant

        response = view(request)

        self.assertEqual(response.status_code, 503, response.data)
        self.assertEqual(response.data["code"], "submission_storage_cleanup_pending")
        schedule_cleanup.assert_called_once_with(tenant_id=self.tenant.id, key=key)

    @patch("apps.domains.submissions.views.submission_view.dispatch_submission")
    @patch("apps.domains.submissions.serializers.submission.upload_fileobj_to_r2")
    def test_admin_omr_upload_rejects_malformed_file_before_upload(
        self,
        upload_fileobj_to_r2,
        dispatch_submission,
    ):
        view = SubmissionViewSet.as_view({"post": "admin_omr_upload"})
        upload = SimpleUploadedFile("scan.png", b"not-a-real-image", content_type="image/png")
        req = self.factory.post(
            "/api/v1/submissions/submissions/admin/omr-upload/",
            data={"target_id": self.exam.id, "file": upload},
            format="multipart",
        )
        force_authenticate(req, user=self.admin)
        req.tenant = self.tenant

        resp = view(req)

        self.assertEqual(resp.status_code, 400, resp.data)
        upload_fileobj_to_r2.assert_not_called()
        dispatch_submission.assert_not_called()

    @patch("apps.domains.submissions.views.submission_view.dispatch_submission")
    @patch("apps.domains.submissions.serializers.submission.upload_fileobj_to_r2")
    def test_admin_omr_upload_duplicate_enrollment_returns_409_before_upload(
        self,
        upload_fileobj_to_r2,
        dispatch_submission,
    ):
        """같은 시험+수강생 OMR을 두 번 올리면 두 번째는 명시적 덮어쓰기 전까지 차단한다."""
        Sheet.objects.create(exam=self.exam, name="MAIN", total_questions=1)
        view = SubmissionViewSet.as_view({"post": "admin_omr_upload"})

        first = SimpleUploadedFile("scan1.png", b"\x89PNG\r\n\x1a\nfirst", content_type="image/png")
        req1 = self.factory.post(
            "/api/v1/submissions/submissions/admin/omr-upload/",
            data={
                "target_id": self.exam.id,
                "enrollment_id": self.enrollment.id,
                "file": first,
            },
            format="multipart",
        )
        force_authenticate(req1, user=self.admin)
        req1.tenant = self.tenant
        first_response = view(req1)

        second = SimpleUploadedFile("scan2.png", b"\x89PNG\r\n\x1a\nsecond", content_type="image/png")
        req2 = self.factory.post(
            "/api/v1/submissions/submissions/admin/omr-upload/",
            data={
                "target_id": self.exam.id,
                "enrollment_id": self.enrollment.id,
                "file": second,
            },
            format="multipart",
        )
        force_authenticate(req2, user=self.admin)
        req2.tenant = self.tenant
        second_response = view(req2)

        self.assertEqual(first_response.status_code, 201, first_response.data)
        self.assertEqual(second_response.status_code, 409, second_response.data)
        self.assertEqual(second_response.data["code"], "DUPLICATE_ENROLLMENT")
        self.assertEqual(
            Submission.objects.filter(
                tenant=self.tenant,
                target_type=Submission.TargetType.EXAM,
                target_id=self.exam.id,
                enrollment_id=self.enrollment.id,
            ).count(),
            1,
        )
        self.assertEqual(upload_fileobj_to_r2.call_count, 1)
        self.assertEqual(dispatch_submission.call_count, 1)


# ═══════════════════════════════════════════════════
# C-2 어드민 인박스 3종 — 학생 차단 / 스태프 통과
# ═══════════════════════════════════════════════════

class TestC2AdminInboxesGuard(_SecurityFixtureMixin, TestCase):

    def setUp(self):
        self._setup_fixtures()

    def test_pending_submissions_student_blocked(self):
        view = PendingSubmissionsView.as_view()
        resp = self._call(lambda: view, "get",
                          "/api/v1/submissions/submissions/pending/",
                          user=self.student_user)
        self.assertEqual(resp.status_code, 403)

    def test_pending_submissions_teacher_allowed(self):
        view = PendingSubmissionsView.as_view()
        resp = self._call(lambda: view, "get",
                          "/api/v1/submissions/submissions/pending/",
                          user=self.teacher)
        self.assertEqual(resp.status_code, 200)

    @patch(
        "apps.domains.submissions.views.pending_submissions_view.generate_presigned_get_url",
        return_value="https://signed.example/submission.png",
    )
    def test_pending_submission_preview_uses_owned_ai_object(self, presign):
        self.peer_submission.file_key = f"tenants/{self.tenant.id}/ai/submissions/peer.png"
        self.peer_submission.save(update_fields=["file_key"])
        view = PendingSubmissionPreviewView.as_view()

        resp = self._call(
            lambda: view,
            "get",
            f"/api/v1/submissions/submissions/{self.peer_submission.id}/preview/",
            user=self.teacher,
            submission_id=self.peer_submission.id,
        )

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data, {"url": "https://signed.example/submission.png"})
        presign.assert_called_once_with(
            key=self.peer_submission.file_key,
            expires_in=900,
        )

    @patch(
        "apps.domains.submissions.views.pending_submissions_view.generate_presigned_get_url",
    )
    def test_pending_submission_preview_student_and_cross_tenant_fail_closed(self, presign):
        self.peer_submission.file_key = f"tenants/{self.tenant.id}/ai/submissions/peer.png"
        self.peer_submission.save(update_fields=["file_key"])
        view = PendingSubmissionPreviewView.as_view()

        student_resp = self._call(
            lambda: view,
            "get",
            f"/api/v1/submissions/submissions/{self.peer_submission.id}/preview/",
            user=self.student_user,
            submission_id=self.peer_submission.id,
        )
        other_tenant = _make_tenant("OtherAcademy", "other_sec_test")
        other_admin = _make_admin(other_tenant, "other_sec_admin")
        cross_tenant_resp = self._call(
            lambda: view,
            "get",
            f"/api/v1/submissions/submissions/{self.peer_submission.id}/preview/",
            user=other_admin,
            tenant=other_tenant,
            submission_id=self.peer_submission.id,
        )

        self.assertEqual(student_resp.status_code, 403)
        self.assertEqual(cross_tenant_resp.status_code, 404)
        presign.assert_not_called()

    @patch(
        "apps.domains.submissions.views.pending_submissions_view.generate_presigned_get_url",
    )
    def test_pending_submission_preview_rejects_missing_or_foreign_object_key(self, presign):
        view = PendingSubmissionPreviewView.as_view()
        self.peer_submission.file_key = ""
        self.peer_submission.save(update_fields=["file_key"])
        missing_resp = self._call(
            lambda: view,
            "get",
            f"/api/v1/submissions/submissions/{self.peer_submission.id}/preview/",
            user=self.teacher,
            submission_id=self.peer_submission.id,
        )

        self.peer_submission.file_key = "tenants/999/ai/submissions/foreign.png"
        self.peer_submission.save(update_fields=["file_key"])
        foreign_resp = self._call(
            lambda: view,
            "get",
            f"/api/v1/submissions/submissions/{self.peer_submission.id}/preview/",
            user=self.teacher,
            submission_id=self.peer_submission.id,
        )

        self.assertEqual(missing_resp.status_code, 404)
        self.assertEqual(foreign_resp.status_code, 404)
        presign.assert_not_called()

    @patch(
        "apps.domains.submissions.views.pending_submissions_view.generate_presigned_get_url",
        side_effect=RuntimeError("signer unavailable"),
    )
    def test_pending_submission_preview_signer_failure_is_explicit(self, presign):
        self.peer_submission.file_key = f"tenants/{self.tenant.id}/ai/submissions/peer.png"
        self.peer_submission.save(update_fields=["file_key"])
        view = PendingSubmissionPreviewView.as_view()

        resp = self._call(
            lambda: view,
            "get",
            f"/api/v1/submissions/submissions/{self.peer_submission.id}/preview/",
            user=self.teacher,
            submission_id=self.peer_submission.id,
        )

        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.data["detail"], "제출 파일 미리보기를 준비하지 못했습니다.")
        presign.assert_called_once()

    def test_pending_submission_list_does_not_expose_object_key(self):
        self.peer_submission.file_key = f"tenants/{self.tenant.id}/ai/submissions/peer.png"
        self.peer_submission.file_type = "image/png"
        self.peer_submission.save(update_fields=["file_key", "file_type"])
        view = PendingSubmissionsView.as_view()

        resp = self._call(
            lambda: view,
            "get",
            "/api/v1/submissions/submissions/pending/",
            user=self.teacher,
        )

        self.assertEqual(resp.status_code, 200, resp.data)
        row = next(item for item in resp.data if item["id"] == self.peer_submission.id)
        self.assertNotIn("file_key", row)
        self.assertTrue(row["has_file"])
        self.assertEqual(row["file_name"], "peer.png")

    def test_exam_submissions_list_student_blocked(self):
        view = ExamSubmissionsListView.as_view()
        resp = self._call(lambda: view, "get",
                          f"/api/v1/submissions/submissions/exams/{self.exam.id}/",
                          user=self.student_user, exam_id=self.exam.id)
        self.assertEqual(resp.status_code, 403)

    def test_homework_submissions_list_student_blocked(self):
        view = HomeworkSubmissionsListView.as_view()
        resp = self._call(lambda: view, "get",
                          "/api/v1/submissions/submissions/homework/9999/",
                          user=self.student_user, homework_id=9999)
        self.assertEqual(resp.status_code, 403)


# ═══════════════════════════════════════════════════
# C-3 ExamOMRSubmitView — 학생 차단
# ═══════════════════════════════════════════════════

class TestC3ExamOMRSubmitGuard(_SecurityFixtureMixin, TestCase):

    def setUp(self):
        self._setup_fixtures()

    def test_student_omr_submit_peer_enrollment_blocked(self):
        """학생이 타 학생 enrollment_id로 OMR 제출 시도 → 403 (Staff 전용으로 상향됨)."""
        view = ExamOMRSubmitView.as_view()
        resp = self._call(lambda: view, "post",
                          f"/api/v1/submissions/submissions/exams/{self.exam.id}/omr/",
                          user=self.student_user,
                          data={"enrollment_id": self.peer_enrollment.id,
                                "sheet_id": 1,
                                "file_key": "tenants/x/fake.png"},
                          exam_id=self.exam.id)
        self.assertEqual(resp.status_code, 403,
                         "CRITICAL: 학생이 타 학생 enrollment로 OMR 제출 가능!")

    def test_student_omr_submit_own_enrollment_still_blocked(self):
        """OMR 제출은 운영자 전용 경로 — 자기 enrollment여도 학생은 차단."""
        view = ExamOMRSubmitView.as_view()
        resp = self._call(lambda: view, "post",
                          f"/api/v1/submissions/submissions/exams/{self.exam.id}/omr/",
                          user=self.student_user,
                          data={"enrollment_id": self.enrollment.id,
                                "sheet_id": 1,
                                "file_key": "tenants/x/own.png"},
                          exam_id=self.exam.id)
        self.assertEqual(resp.status_code, 403)

    def test_teacher_omr_submit_passes_permission(self):
        """교사 → 권한 통과 (실제 처리는 비즈니스 단계 — 200/201/400 모두 OK)."""
        view = ExamOMRSubmitView.as_view()
        resp = self._call(lambda: view, "post",
                          f"/api/v1/submissions/submissions/exams/{self.exam.id}/omr/",
                          user=self.teacher,
                          data={"enrollment_id": self.enrollment.id,
                                "sheet_id": 1,
                                "file_key": "tenants/x/teacher_upload.png"},
                          exam_id=self.exam.id)
        self.assertNotIn(resp.status_code, (401, 403))

    @patch("apps.domains.submissions.views.exam_omr_submit_view.dispatch_submission")
    @patch("apps.domains.submissions.serializers.submission.upload_fileobj_to_r2")
    def test_teacher_omr_submit_accepts_multipart_file_with_default_sheet(
        self,
        upload_fileobj_to_r2,
        dispatch_submission,
    ):
        """교사용 OMR 화면은 실제 파일을 보내므로, file_key 없이도 제출이 생성되어야 한다."""
        sheet = Sheet.objects.create(exam=self.exam, name="MAIN", total_questions=1)
        view = ExamOMRSubmitView.as_view()
        upload = SimpleUploadedFile("teacher-omr.png", b"\x89PNG\r\n\x1a\nscan", content_type="image/png")
        req = self.factory.post(
            f"/api/v1/submissions/submissions/exams/{self.exam.id}/omr/",
            data={"enrollment_id": self.enrollment.id, "file": upload},
            format="multipart",
        )
        force_authenticate(req, user=self.teacher)
        req.tenant = self.tenant

        resp = view(req, exam_id=self.exam.id)

        self.assertEqual(resp.status_code, 201, resp.data)
        submission = Submission.objects.get(id=resp.data["submission_id"])
        self.assertEqual(submission.enrollment_id, self.enrollment.id)
        self.assertEqual(submission.payload["sheet_id"], sheet.id)
        self.assertTrue(submission.file_key)
        upload_fileobj_to_r2.assert_called_once()
        dispatch_submission.assert_called_once_with(submission)

    @patch(
        "apps.domains.submissions.views.exam_omr_submit_view.schedule_unreferenced_ai_object_cleanup"
    )
    @patch(
        "apps.domains.submissions.views.exam_omr_submit_view.SubmissionCreateSerializer.save",
        autospec=True,
    )
    def test_single_omr_commit_failure_schedules_uploaded_object_cleanup(
        self,
        serializer_save,
        schedule_cleanup,
    ):
        Sheet.objects.create(exam=self.exam, name="MAIN", total_questions=1)
        key = f"tenants/{self.tenant.id}/ai/submissions/997/single.jpg"

        def fail_after_upload(serializer, **_kwargs):
            serializer.uploaded_object_key = key
            serializer.uploaded_tenant_id = self.tenant.id
            raise IntegrityError("commit failed after upload")

        serializer_save.side_effect = fail_after_upload
        view = ExamOMRSubmitView.as_view()
        upload = SimpleUploadedFile(
            "single.png",
            b"\x89PNG\r\n\x1a\nsingle",
            content_type="image/png",
        )
        request = self.factory.post(
            f"/api/v1/submissions/submissions/exams/{self.exam.id}/omr/",
            data={"enrollment_id": self.enrollment.id, "file": upload},
            format="multipart",
        )
        force_authenticate(request, user=self.teacher)
        request.tenant = self.tenant

        response = view(request, exam_id=self.exam.id)

        self.assertEqual(response.status_code, 503, response.data)
        schedule_cleanup.assert_called_once_with(tenant_id=self.tenant.id, key=key)

    @patch("apps.domains.submissions.views.exam_omr_submit_view.dispatch_submission")
    def test_teacher_omr_submit_same_tenant_non_candidate_enrollment_blocked(self, dispatch_submission):
        Sheet.objects.create(exam=self.exam, name="MAIN", total_questions=1)
        _other_user, other_student = _make_student_user_and_profile(
            self.tenant, "S777", name="미응시학생",
        )
        other_lecture = Lecture.objects.create(
            tenant=self.tenant,
            title="Not Exam Lecture",
            name="Not Exam Lecture",
            subject="MATH",
        )
        other_enrollment = Enrollment.objects.create(
            tenant=self.tenant,
            student=other_student,
            lecture=other_lecture,
            status="ACTIVE",
        )
        view = ExamOMRSubmitView.as_view()

        resp = self._call(
            lambda: view,
            "post",
            f"/api/v1/submissions/submissions/exams/{self.exam.id}/omr/",
            user=self.teacher,
            data={
                "enrollment_id": other_enrollment.id,
                "file_key": "tenants/x/non_candidate.png",
            },
            exam_id=self.exam.id,
        )

        self.assertEqual(resp.status_code, 403, resp.data)
        self.assertFalse(
            Submission.objects.filter(
                tenant=self.tenant,
                target_type=Submission.TargetType.EXAM,
                target_id=self.exam.id,
                enrollment_id=other_enrollment.id,
            ).exists()
        )
        dispatch_submission.assert_not_called()

    @patch("apps.domains.submissions.views.exam_omr_submit_view.dispatch_submission")
    def test_teacher_omr_submit_duplicate_enrollment_returns_409(self, dispatch_submission):
        Sheet.objects.create(exam=self.exam, name="MAIN", total_questions=1)
        Submission.objects.create(
            tenant=self.tenant,
            user=self.teacher,
            enrollment_id=self.enrollment.id,
            target_type=Submission.TargetType.EXAM,
            target_id=self.exam.id,
            source=Submission.Source.OMR_SCAN,
            status=Submission.Status.DONE,
            file_key="tenants/x/existing.png",
        )
        view = ExamOMRSubmitView.as_view()

        resp = self._call(
            lambda: view,
            "post",
            f"/api/v1/submissions/submissions/exams/{self.exam.id}/omr/",
            user=self.teacher,
            data={
                "enrollment_id": self.enrollment.id,
                "file_key": "tenants/x/duplicate.png",
            },
            exam_id=self.exam.id,
        )

        self.assertEqual(resp.status_code, 409, resp.data)
        self.assertEqual(resp.data["code"], "DUPLICATE_ENROLLMENT")
        dispatch_submission.assert_not_called()

    def test_ai_auto_match_duplicate_stays_needs_identification(self):
        sheet = Sheet.objects.create(exam=self.exam, name="MAIN", total_questions=1)
        ExamQuestion.objects.create(sheet=sheet, number=1, score=5)
        ExamEnrollment.objects.create(exam=self.exam, enrollment=self.enrollment)
        first = Submission.objects.create(
            tenant=self.tenant,
            user=self.teacher,
            target_type=Submission.TargetType.EXAM,
            target_id=self.exam.id,
            source=Submission.Source.OMR_SCAN,
            status=Submission.Status.DISPATCHED,
            file_key="tenants/x/first.png",
        )
        second = Submission.objects.create(
            tenant=self.tenant,
            user=self.teacher,
            target_type=Submission.TargetType.EXAM,
            target_id=self.exam.id,
            source=Submission.Source.OMR_SCAN,
            status=Submission.Status.DISPATCHED,
            file_key="tenants/x/second.png",
        )
        payload = {
            "status": "DONE",
            "identifier": {
                "status": "ok",
                "identifier": str(self.student.phone).replace("-", "")[-8:],
            },
            "answers": [
                {
                    "question_id": 1,
                    "detected": ["1"],
                    "status": "ok",
                    "marking": "single",
                    "confidence": 0.99,
                }
            ],
        }

        apply_omr_ai_result({"submission_id": first.id, **payload})
        apply_omr_ai_result({"submission_id": second.id, **payload})

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, Submission.Status.ANSWERS_READY)
        self.assertEqual(first.enrollment_id, self.enrollment.id)
        self.assertEqual(second.status, Submission.Status.NEEDS_IDENTIFICATION)
        self.assertIsNone(second.enrollment_id)
        self.assertEqual(second.error_message, "duplicate_enrollment")
        self.assertIn("DUPLICATE_ENROLLMENT", second.meta["manual_review"]["reasons"])
        self.assertEqual(
            second.meta["duplicate_conflict"]["conflict_submission_id"],
            first.id,
        )

    @patch("apps.domains.submissions.views.exam_omr_submit_view.dispatch_submission")
    def test_teacher_omr_submit_rejects_cross_tenant_file_key(self, dispatch_submission):
        Sheet.objects.create(exam=self.exam, name="MAIN", total_questions=1)
        view = ExamOMRSubmitView.as_view()

        resp = self._call(
            lambda: view,
            "post",
            f"/api/v1/submissions/submissions/exams/{self.exam.id}/omr/",
            user=self.teacher,
            data={
                "enrollment_id": self.enrollment.id,
                "file_key": "tenants/999/ai/submissions/1/scan.png",
            },
            exam_id=self.exam.id,
        )

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertFalse(
            Submission.objects.filter(
                tenant=self.tenant,
                target_type=Submission.TargetType.EXAM,
                target_id=self.exam.id,
                enrollment_id=self.enrollment.id,
            ).exists()
        )
        dispatch_submission.assert_not_called()

    @patch("apps.domains.submissions.views.exam_omr_submit_view.dispatch_submission")
    def test_teacher_omr_submit_rejects_same_tenant_non_ai_namespace(
        self,
        dispatch_submission,
    ):
        Sheet.objects.create(exam=self.exam, name="MAIN", total_questions=1)
        key = f"tenants/{self.tenant.id}/omr/caller-controlled.png"

        resp = self._call(
            lambda: ExamOMRSubmitView.as_view(),
            "post",
            f"/api/v1/submissions/submissions/exams/{self.exam.id}/omr/",
            user=self.teacher,
            data={"enrollment_id": self.enrollment.id, "file_key": key},
            exam_id=self.exam.id,
        )

        self.assertEqual(resp.status_code, 400, resp.data)
        dispatch_submission.assert_not_called()

    @patch("apps.domains.submissions.views.exam_omr_submit_view.dispatch_submission")
    def test_teacher_omr_submit_rejects_key_already_claimed_for_cleanup(
        self,
        dispatch_submission,
    ):
        Sheet.objects.create(exam=self.exam, name="MAIN", total_questions=1)
        key = f"tenants/{self.tenant.id}/ai/submissions/legacy/cleanup.png"
        SubmissionStorageCleanupIntent.objects.create(
            tenant=self.tenant,
            bucket=SubmissionStorageCleanupIntent.Bucket.AI,
            object_key=key,
        )

        resp = self._call(
            lambda: ExamOMRSubmitView.as_view(),
            "post",
            f"/api/v1/submissions/submissions/exams/{self.exam.id}/omr/",
            user=self.teacher,
            data={"enrollment_id": self.enrollment.id, "file_key": key},
            exam_id=self.exam.id,
        )

        self.assertEqual(resp.status_code, 409, resp.data)
        self.assertEqual(resp.data["code"], "submission_storage_cleanup_conflict")
        dispatch_submission.assert_not_called()


# ═══════════════════════════════════════════════════
# C-5 Landing Stats — 학생 차단 / 스태프 통과 / 크로스테넌트 차단
# (2026-04-26 KPI 인박스 재설계 회귀 가드)
# ═══════════════════════════════════════════════════

class TestC5LandingStatsGuard(_SecurityFixtureMixin, TestCase):

    def setUp(self):
        self._setup_fixtures()
        # cross-tenant 검증용 다른 테넌트 + 어드민
        self.other_tenant = _make_tenant("OtherAcademy", "sec_other")
        self.other_admin = _make_admin(self.other_tenant, "other_admin")

    # --- Results landing-stats ---

    def test_results_landing_stats_student_blocked(self):
        """학생 → /results/admin/landing-stats/ → 403."""
        view = AdminResultsLandingStatsView.as_view()
        resp = self._call(lambda: view, "get",
                          "/api/v1/results/admin/landing-stats/",
                          user=self.student_user)
        self.assertEqual(resp.status_code, 403)

    def test_results_landing_stats_teacher_allowed(self):
        view = AdminResultsLandingStatsView.as_view()
        resp = self._call(lambda: view, "get",
                          "/api/v1/results/admin/landing-stats/",
                          user=self.teacher)
        self.assertEqual(resp.status_code, 200)

    def test_results_landing_stats_admin_allowed(self):
        view = AdminResultsLandingStatsView.as_view()
        resp = self._call(lambda: view, "get",
                          "/api/v1/results/admin/landing-stats/",
                          user=self.admin)
        self.assertEqual(resp.status_code, 200)

    def test_results_landing_stats_cross_tenant_isolated(self):
        """다른 테넌트 어드민이 self.tenant 헤더로 호출 → 403 (멤버십 없음)."""
        view = AdminResultsLandingStatsView.as_view()
        resp = self._call(lambda: view, "get",
                          "/api/v1/results/admin/landing-stats/",
                          user=self.other_admin, tenant=self.tenant)
        self.assertEqual(resp.status_code, 403,
                         "CRITICAL: 크로스테넌트 어드민이 다른 학원 KPI 조회 가능!")

    def test_results_landing_stats_isolation_payload(self):
        """다른 테넌트의 submission이 self.tenant 응답에 섞이지 않는다."""
        # 다른 테넌트에 lecture/exam/submission 생성
        other_lecture = Lecture.objects.create(
            tenant=self.other_tenant, title="OtherMath", name="OtherMath", subject="MATH",
        )
        other_session = LectureSession.objects.create(
            lecture=other_lecture, order=1, title="OtherS1",
        )
        other_exam = Exam.objects.create(
            tenant=self.other_tenant, title="OtherExam",
            exam_type=Exam.ExamType.REGULAR, pass_score=60, max_score=100, max_attempts=1,
        )
        other_exam.sessions.add(other_session)

        view = AdminResultsLandingStatsView.as_view()
        resp = self._call(lambda: view, "get",
                          "/api/v1/results/admin/landing-stats/",
                          user=self.admin)
        self.assertEqual(resp.status_code, 200)
        body = resp.data
        # self.tenant에는 active_lecture 1, active_exam 1 (테스트 fixture 기준)
        self.assertEqual(body["active_lectures"], 1, body)
        self.assertEqual(body["active_exams"], 1, body)

    # --- Videos landing-stats ---

    def test_videos_landing_stats_student_blocked(self):
        view = AdminVideosLandingStatsView.as_view()
        resp = self._call(lambda: view, "get",
                          "/api/v1/media/admin/videos/landing-stats/",
                          user=self.student_user)
        self.assertEqual(resp.status_code, 403)

    def test_videos_landing_stats_teacher_allowed(self):
        view = AdminVideosLandingStatsView.as_view()
        resp = self._call(lambda: view, "get",
                          "/api/v1/media/admin/videos/landing-stats/",
                          user=self.teacher)
        self.assertEqual(resp.status_code, 200)

    def test_videos_landing_stats_admin_allowed(self):
        view = AdminVideosLandingStatsView.as_view()
        resp = self._call(lambda: view, "get",
                          "/api/v1/media/admin/videos/landing-stats/",
                          user=self.admin)
        self.assertEqual(resp.status_code, 200)

    def test_videos_landing_stats_cross_tenant_blocked(self):
        view = AdminVideosLandingStatsView.as_view()
        resp = self._call(lambda: view, "get",
                          "/api/v1/media/admin/videos/landing-stats/",
                          user=self.other_admin, tenant=self.tenant)
        self.assertEqual(resp.status_code, 403,
                         "CRITICAL: 크로스테넌트 어드민이 다른 학원 영상 KPI 조회 가능!")


# ═══════════════════════════════════════════════════
# C-6 SubmissionViewSet.create — 학부모 토큰 차단
# (2026-04-27 시험·성적·과제 실사용 리뷰 회귀 가드)
# ═══════════════════════════════════════════════════

class TestC6ParentSubmissionCreateBlocked(_SecurityFixtureMixin, TestCase):
    """학부모 계정(parent_profile only)이 자녀/타가족 enrollment_id로
    submission POST 시도 → 403.

    학생 토큰의 enrollment 소유권 검증(C-1b)이 student_profile을 통해
    이뤄지므로, parent_profile만 가진 부모 계정은 그 검증을 건너뛰고
    임의 enrollment 제출이 가능했던 이력 차단."""

    def setUp(self):
        from apps.domains.parents.models import Parent
        self._setup_fixtures()
        # parent_profile만 가진 부모 계정. tenant/role=parent.
        self.parent_user = User.objects.create_user(
            username=user_internal_username(self.tenant, "P-PARENT"),
            password="test1234",
            tenant=self.tenant, name="학부모",
        )
        Parent.objects.create(
            tenant=self.tenant, user=self.parent_user,
            name="학부모", phone="01099998888",
        )
        TenantMembership.ensure_active(
            tenant=self.tenant, user=self.parent_user, role="parent",
        )

    def test_parent_create_child_enrollment_blocked(self):
        """부모 → POST /submissions/submissions/ (자녀 enrollment) → 403."""
        view = SubmissionViewSet.as_view({"post": "create"})
        with patch("apps.domains.submissions.views.submission_view.dispatch_submission"):
            resp = self._call(lambda: view, "post",
                              "/api/v1/submissions/submissions/",
                              user=self.parent_user,
                              data={
                                  "target_type": "exam",
                                  "target_id": self.exam.id,
                                  "source": "online",
                                  "enrollment_id": self.enrollment.id,
                                  "payload": {"answers": []},
                              })
        self.assertEqual(resp.status_code, 403,
                         "CRITICAL: 학부모가 자녀/타학생 enrollment_id로 임의 제출 가능!")

    def test_parent_create_peer_enrollment_blocked(self):
        """부모 → POST /submissions/submissions/ (타가족 enrollment) → 403."""
        view = SubmissionViewSet.as_view({"post": "create"})
        with patch("apps.domains.submissions.views.submission_view.dispatch_submission"):
            resp = self._call(lambda: view, "post",
                              "/api/v1/submissions/submissions/",
                              user=self.parent_user,
                              data={
                                  "target_type": "exam",
                                  "target_id": self.exam.id,
                                  "source": "online",
                                  "enrollment_id": self.peer_enrollment.id,
                                  "payload": {"answers": []},
                              })
        self.assertEqual(resp.status_code, 403,
                         "CRITICAL: 학부모가 타가족 enrollment_id로 제출 가능!")
