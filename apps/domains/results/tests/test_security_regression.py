# PATH: apps/domains/results/tests/test_security_regression.py
"""
보안 회귀 — 2026-04-25 정밀검사:
  C-4  WrongNote/WrongNotePDF/StudentExamAttempts 의 user_id↔student_id PK 충돌 차단

이전 코드는 hasattr(Enrollment, "user_id") 폴백으로 student_id=user.id를 비교해
Student.pk와 User.pk 공간 충돌로 타 학생 데이터에 우연히 접근 가능했다.
이 테스트는 다음 시나리오를 강제로 만든 뒤 차단 확인:
  - 학생 user.id == 다른 학생 student.id 인 상황을 fixture로 구성
  - 우연 매칭으로 타인 enrollment에 접근 시도 → 403
  - 본인 enrollment 접근은 정상 → 권한 통과
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.core.management import call_command
from django.db.models import Max
from django.test import TestCase
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.core.models.tenant import Tenant
from apps.core.models.tenant_membership import TenantMembership
from apps.core.models.user import user_internal_username
from apps.domains.ai.callbacks import dispatch_ai_result_to_domain
from apps.domains.students.models import Student
from apps.domains.lectures.models import Lecture
from apps.domains.exams.models import Exam, ExamEnrollment
from apps.domains.exams.views.exam_questions_by_exam_view import (
    ExamQuestionsByExamView,
)
from apps.domains.exams.views.question_view import QuestionViewSet
from apps.domains.enrollment.models import Enrollment
from apps.domains.results.models import ExamAttempt
from apps.domains.results.models.wrong_note_pdf import WrongNotePDF
from apps.domains.results.views.wrong_note_pdf_status_view import WrongNotePDFStatusView
from apps.domains.results.views.wrong_note_view import WrongNoteView
from apps.domains.results.views.wrong_note_pdf_view import WrongNotePDFCreateView
from apps.domains.results.views.student_exam_attempts_view import MyExamAttemptsView
from apps.domains.results.services.wrong_note_pdf_worker import (
    handle_wrong_note_pdf_generation_job,
)
from apps.domains.results.services.wrong_note_pdf_service import (
    delete_wrong_note_pdf_object,
)
from apps.shared.contracts.ai_job import AIJob
from apps.support.results.wrong_note_pdf_dependencies import (
    get_wrong_note_pdf_ai_job_model,
)

User = get_user_model()
AIJobModel = get_wrong_note_pdf_ai_job_model()


def _make_tenant():
    return Tenant.objects.create(name="ResultsSecAcademy", code="ressec", is_active=True)


def _make_student(
    tenant,
    ps_number,
    name="학생",
    forced_user_id=None,
    forced_student_id=None,
):
    """일반 학생 생성. 명시 PK는 User↔Student 충돌 회귀에서만 사용한다."""
    internal = user_internal_username(tenant, ps_number)
    user_kwargs = dict(
        username=internal, password="test1234",
        tenant=tenant, name=name,
    )
    if forced_user_id is not None:
        user = User(**user_kwargs)
        user.id = forced_user_id
        user.set_password("test1234")
        user.save(force_insert=True)
    else:
        user = User.objects.create_user(**user_kwargs)

    student_kwargs = dict(
        tenant=tenant, user=user,
        ps_number=ps_number, name=name,
        omr_code=ps_number[:8].rjust(8, "0"),
        parent_phone=f"010-3333-{ps_number[-4:]:>04}",
    )
    if forced_student_id is not None:
        student = Student(id=forced_student_id, **student_kwargs)
        student.save(force_insert=True)
    else:
        student = Student.objects.create(**student_kwargs)
    TenantMembership.ensure_active(tenant=tenant, user=user, role="student")
    return user, student


class _Mixin:

    def _setup(self):
        self.factory = APIRequestFactory()
        self.tenant = _make_tenant()

        self.lecture = Lecture.objects.create(
            tenant=self.tenant, title="L", name="L", subject="MATH",
        )

        # 전체 테스트 순서와 무관한 빈 PK를 고른 뒤 Student A에 부여한다.
        # 고정 PK(예: 900)는 다른 테스트가 sequence로 이미 소비할 수 있으므로 금지한다.
        collision_id = max(
            User.objects.aggregate(max_id=Max("id"))["max_id"] or 0,
            Student.objects.aggregate(max_id=Max("id"))["max_id"] or 0,
        ) + 10_000
        self.user_a, self.student_a = _make_student(
            self.tenant,
            "A001",
            "학생A",
            forced_student_id=collision_id,
        )
        self.enroll_a = Enrollment.objects.create(
            tenant=self.tenant, student=self.student_a,
            lecture=self.lecture, status="ACTIVE",
        )

        # 학생 B — User.id == student_a.id 가 되도록 강제 (PK 공간 충돌)
        # 옛 버그(student_id=user.id)에서는 user_b 가 enroll_a 에 우연 접근 가능.
        self.user_b, self.student_b = _make_student(
            self.tenant, "B001", "학생B",
            forced_user_id=self.student_a.id,
        )
        self.enroll_b = Enrollment.objects.create(
            tenant=self.tenant, student=self.student_b,
            lecture=self.lecture, status="ACTIVE",
        )
        self.staff_user = User.objects.create_user(
            username="results-security-teacher",
            password="test1234",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=self.staff_user,
            role="teacher",
        )

    def _get(self, view, user, **query):
        from urllib.parse import urlencode
        qs = ("?" + urlencode(query)) if query else ""
        req = self.factory.get(f"/api/v1/results/wrong-notes/{qs}")
        force_authenticate(req, user=user)
        req.tenant = self.tenant
        return view(req)


# ═══════════════════════════════════════════════════
# C-4 WrongNoteView — PK 공간 충돌 차단
# ═══════════════════════════════════════════════════

class TestC4WrongNotePkCollisionGuard(_Mixin, TestCase):

    def setUp(self):
        self._setup()

    def test_user_b_cannot_access_student_a_enrollment_via_pk_collision(self):
        """user_b.id == student_a.id 상황에서 user_b가 enroll_a 접근 시도 → 403."""
        # 사전조건: user_b.id == student_a.id
        self.assertEqual(self.user_b.id, self.student_a.id,
                         "fixture 무결성: PK 충돌이 강제되어야 함")
        # student_a.user_id != student_b.user_id (다른 사람)
        self.assertNotEqual(self.user_a.id, self.user_b.id)

        view = WrongNoteView.as_view()
        resp = self._get(view, user=self.user_b, enrollment_id=self.enroll_a.id)
        self.assertEqual(resp.status_code, 403,
                         "CRITICAL: PK 공간 충돌(student.id == user.id)로 "
                         "타 학생 enrollment 접근 가능!")

    def test_student_cannot_access_staff_wrong_note_builder(self):
        """오답노트 제작 화면은 교직원 전용이다."""
        view = WrongNoteView.as_view()
        resp = self._get(view, user=self.user_a, enrollment_id=self.enroll_a.id)
        self.assertEqual(resp.status_code, 403)

    def test_student_cannot_list_admin_exam_questions(self):
        request = self.factory.get("/api/v1/exams/questions/")
        force_authenticate(request, user=self.user_a)
        request.tenant = self.tenant

        response = QuestionViewSet.as_view({"get": "list"})(request)

        self.assertEqual(response.status_code, 403)

    def test_student_cannot_read_admin_exam_question_shape(self):
        exam = Exam.objects.create(
            tenant=self.tenant,
            title="비공개 시험",
            exam_type=Exam.ExamType.REGULAR,
        )
        request = self.factory.get(f"/api/v1/exams/{exam.id}/questions/")
        force_authenticate(request, user=self.user_a)
        request.tenant = self.tenant

        response = ExamQuestionsByExamView.as_view()(request, exam_id=exam.id)

        self.assertEqual(response.status_code, 403)

    def test_student_cannot_access_wrong_note_for_inactive_own_enrollment(self):
        """학생 본인 enrollment라도 비활성 수강이면 오답노트 조회 불가."""
        self.enroll_a.status = "INACTIVE"
        self.enroll_a.save(update_fields=["status", "updated_at"])

        view = WrongNoteView.as_view()
        resp = self._get(view, user=self.user_a, enrollment_id=self.enroll_a.id)

        self.assertEqual(resp.status_code, 403)

    def test_wrong_note_list_rejects_reversed_session_range(self):
        view = WrongNoteView.as_view()
        resp = self._get(
            view,
            user=self.staff_user,
            enrollment_id=self.enroll_a.id,
            from_session_order=4,
            to_session_order=2,
        )

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("detail", resp.data)

    def test_pdf_create_user_b_cannot_use_student_a_enrollment(self):
        """WrongNotePDFCreate도 동일한 가드. PK 충돌로 타인 enrollment PDF 생성 불가."""
        view = WrongNotePDFCreateView.as_view()
        req = self.factory.post(
            "/api/v1/results/wrong-notes/pdf/",
            data={"enrollment_id": self.enroll_a.id}, format="json",
        )
        force_authenticate(req, user=self.user_b)
        req.tenant = self.tenant
        resp = view(req)
        self.assertEqual(resp.status_code, 403)

    @patch(
        "apps.domains.results.views.wrong_note_pdf_view.publish_wrong_note_pdf_ai_job",
        return_value=True,
    )
    def test_pdf_create_enqueues_tools_job_without_generating_in_request(self, publish):
        view = WrongNotePDFCreateView.as_view()
        req = self.factory.post(
            "/api/v1/results/wrong-notes/pdf/",
            data={
                "enrollment_id": self.enroll_a.id,
                "from_session_order": 1,
                "to_session_order": 4,
            },
            format="json",
        )
        force_authenticate(req, user=self.staff_user)
        req.tenant = self.tenant

        resp = view(req)

        self.assertEqual(resp.status_code, 202, resp.data)
        job = WrongNotePDF.objects.get(id=resp.data["job_id"])
        self.assertEqual(job.status, WrongNotePDF.Status.PENDING)
        self.assertEqual(job.lecture_id, self.lecture.id)
        self.assertEqual(job.from_session_order, 1)
        self.assertEqual(job.to_session_order, 4)
        ai_job = AIJobModel.objects.get(source_domain="results_wrong_note_pdf")
        self.assertEqual(ai_job.job_type, "wrong_note_pdf_generation")
        self.assertEqual(ai_job.source_id, str(job.id))
        self.assertRegex(job.source_fingerprint, r"^[0-9a-f]{64}$")
        self.assertEqual(
            ai_job.payload,
            {
                "wrong_note_pdf_job_id": job.id,
                "source_fingerprint": job.source_fingerprint,
            },
        )
        publish.assert_called_once_with(ai_job)

    @patch(
        "apps.domains.results.views.wrong_note_pdf_view.publish_wrong_note_pdf_ai_job",
        return_value=True,
    )
    @patch(
        "apps.domains.results.views.wrong_note_pdf_status_view.generate_presigned_get_url_storage",
        return_value="https://storage.test/wrong-note.hwpx",
    )
    def test_hwpx_create_and_status_preserve_format(self, presign, _publish):
        request = self.factory.post(
            "/api/v1/results/wrong-notes/documents/",
            data={
                "enrollment_id": self.enroll_a.id,
                "output_format": "hwpx",
            },
            format="json",
        )
        force_authenticate(request, user=self.staff_user)
        request.tenant = self.tenant

        created = WrongNotePDFCreateView.as_view()(request)

        self.assertEqual(created.status_code, 202, created.data)
        self.assertEqual(created.data["output_format"], "hwpx")
        job = WrongNotePDF.objects.get(id=created.data["job_id"])
        self.assertEqual(job.output_format, WrongNotePDF.OutputFormat.HWPX)
        job.status = WrongNotePDF.Status.DONE
        job.file_path = f"tenants/{self.tenant.id}/results/wrong-notes/{job.id}.hwpx"
        job.save(update_fields=["status", "file_path", "updated_at"])

        status_request = self.factory.get(
            f"/api/v1/results/wrong-notes/documents/{job.id}/"
        )
        force_authenticate(status_request, user=self.staff_user)
        status_request.tenant = self.tenant
        status_response = WrongNotePDFStatusView.as_view()(status_request, job_id=job.id)

        self.assertEqual(status_response.status_code, 200, status_response.data)
        self.assertEqual(status_response.data["output_format"], "hwpx")
        self.assertEqual(status_response.data["filename"], f"wrong-note-{job.id}.hwpx")
        presign.assert_called_once_with(
            key=job.file_path,
            expires_in=3600,
            filename=f"wrong-note-{job.id}.hwpx",
            content_type="application/vnd.hancom.hwpx",
        )

    def test_pdf_create_rejects_reversed_session_range(self):
        view = WrongNotePDFCreateView.as_view()
        req = self.factory.post(
            "/api/v1/results/wrong-notes/pdf/",
            data={
                "enrollment_id": self.enroll_a.id,
                "from_session_order": 4,
                "to_session_order": 2,
            },
            format="json",
        )
        force_authenticate(req, user=self.staff_user)
        req.tenant = self.tenant

        resp = view(req)

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("시작 회차", resp.data["detail"])
        self.assertFalse(WrongNotePDF.objects.exists())

    @patch(
        "apps.domains.results.views.wrong_note_pdf_view.publish_wrong_note_pdf_ai_job",
        return_value=False,
    )
    def test_pdf_create_marks_both_jobs_failed_when_queue_rejects(self, _publish):
        view = WrongNotePDFCreateView.as_view()
        req = self.factory.post(
            "/api/v1/results/wrong-notes/pdf/",
            data={"enrollment_id": self.enroll_a.id},
            format="json",
        )
        force_authenticate(req, user=self.staff_user)
        req.tenant = self.tenant

        resp = view(req)

        self.assertEqual(resp.status_code, 503, resp.data)
        self.assertEqual(
            WrongNotePDF.objects.get().status,
            WrongNotePDF.Status.FAILED,
        )
        self.assertEqual(AIJobModel.objects.get().status, "FAILED")

    def _wrong_note_ai_contract(self, pdf_job: WrongNotePDF) -> AIJob:
        ai_job = AIJobModel.objects.create(
            job_id=f"wrong-note-pdf-{pdf_job.id}",
            job_type="wrong_note_pdf_generation",
            status="PENDING",
            tenant_id=str(self.tenant.id),
            source_domain="results_wrong_note_pdf",
            source_id=str(pdf_job.id),
            payload={
                "wrong_note_pdf_job_id": pdf_job.id,
                "source_fingerprint": pdf_job.source_fingerprint,
            },
            tier="basic",
        )
        return AIJob(
            id=ai_job.job_id,
            type="wrong_note_pdf_generation",
            tenant_id=ai_job.tenant_id,
            source_domain=ai_job.source_domain,
            source_id=ai_job.source_id,
            payload=ai_job.payload,
        )

    @patch(
        "apps.domains.results.services.wrong_note_pdf_worker.generate_and_store_wrong_note_pdf"
    )
    def test_tools_worker_result_is_finalized_by_idempotent_callback(self, generate):
        pdf_job = WrongNotePDF.objects.create(
            enrollment=self.enroll_a,
            lecture=self.lecture,
            status=WrongNotePDF.Status.PENDING,
        )
        expected_key = (
            f"tenants/{self.tenant.id}/results/wrong-notes/{pdf_job.id}.pdf"
        )
        generate.return_value = expected_key
        contract = self._wrong_note_ai_contract(pdf_job)

        result = handle_wrong_note_pdf_generation_job(contract)
        pdf_job.refresh_from_db()
        self.assertEqual(pdf_job.status, WrongNotePDF.Status.RUNNING)
        self.assertEqual(result.status, "DONE")

        handled = dispatch_ai_result_to_domain(
            job_id=contract.id,
            status=result.status,
            result_payload=result.result,
            error=result.error,
            source_domain=contract.source_domain,
            source_id=contract.source_id,
        )

        self.assertTrue(handled)
        pdf_job.refresh_from_db()
        self.assertEqual(pdf_job.status, WrongNotePDF.Status.DONE)
        self.assertEqual(pdf_job.file_path, expected_key)

    @patch(
        "apps.domains.results.views.wrong_note_pdf_status_view.generate_presigned_get_url_storage",
        return_value="https://storage.test/current-wrong-note.pdf",
    )
    @patch(
        "apps.domains.results.services.wrong_note_pdf_worker.generate_and_store_wrong_note_pdf"
    )
    @patch(
        "apps.domains.results.views.wrong_note_pdf_view.publish_wrong_note_pdf_ai_job",
        return_value=True,
    )
    def test_stale_retry_rejects_old_terminal_callbacks_and_exposes_only_new_file(
        self,
        _publish,
        generate,
        presign,
    ):
        old_job = WrongNotePDF.objects.create(
            enrollment=self.enroll_a,
            lecture=self.lecture,
            status=WrongNotePDF.Status.RUNNING,
        )
        old_contract = self._wrong_note_ai_contract(old_job)
        WrongNotePDF.objects.filter(id=old_job.id).update(
            updated_at=timezone.now() - timedelta(minutes=6),
        )

        request = self.factory.post(
            "/api/v1/results/wrong-notes/pdf/",
            data={"enrollment_id": self.enroll_a.id},
            format="json",
        )
        force_authenticate(request, user=self.staff_user)
        request.tenant = self.tenant

        created = WrongNotePDFCreateView.as_view()(request)

        self.assertEqual(created.status_code, 202, created.data)
        new_job = WrongNotePDF.objects.get(id=created.data["job_id"])
        old_job.refresh_from_db()
        self.assertEqual(old_job.status, WrongNotePDF.Status.FAILED)
        self.assertEqual(new_job.status, WrongNotePDF.Status.PENDING)
        stale_state = (
            old_job.status,
            old_job.file_path,
            old_job.error_message,
            old_job.updated_at,
        )

        old_key = (
            f"tenants/{self.tenant.id}/results/wrong-notes/{old_job.id}.pdf"
        )
        for callback_status, payload, callback_error in (
            (
                "DONE",
                {
                    "outcome": WrongNotePDF.Status.DONE,
                    "wrong_note_pdf_job_id": old_job.id,
                    "file_path": old_key,
                },
                None,
            ),
            (
                "DONE",
                {
                    "outcome": WrongNotePDF.Status.FAILED,
                    "wrong_note_pdf_job_id": old_job.id,
                    "error_message": "late worker failure",
                    "file_path": "",
                },
                None,
            ),
            ("FAILED", {}, "late transport failure"),
        ):
            handled = dispatch_ai_result_to_domain(
                job_id=old_contract.id,
                status=callback_status,
                result_payload=payload,
                error=callback_error,
                source_domain=old_contract.source_domain,
                source_id=old_contract.source_id,
            )
            self.assertTrue(handled)
            old_job.refresh_from_db()
            self.assertEqual(
                (
                    old_job.status,
                    old_job.file_path,
                    old_job.error_message,
                    old_job.updated_at,
                ),
                stale_state,
            )

        new_ai_job = AIJobModel.objects.get(
            source_domain="results_wrong_note_pdf",
            source_id=str(new_job.id),
        )
        new_contract = AIJob(
            id=new_ai_job.job_id,
            type=new_ai_job.job_type,
            tenant_id=new_ai_job.tenant_id,
            source_domain=new_ai_job.source_domain,
            source_id=new_ai_job.source_id,
            payload=new_ai_job.payload,
        )
        new_key = (
            f"tenants/{self.tenant.id}/results/wrong-notes/{new_job.id}.pdf"
        )
        generate.return_value = new_key

        result = handle_wrong_note_pdf_generation_job(new_contract)
        handled = dispatch_ai_result_to_domain(
            job_id=new_contract.id,
            status=result.status,
            result_payload=result.result,
            error=result.error,
            source_domain=new_contract.source_domain,
            source_id=new_contract.source_id,
        )

        self.assertTrue(handled)
        new_job.refresh_from_db()
        self.assertEqual(new_job.status, WrongNotePDF.Status.DONE)
        self.assertEqual(new_job.file_path, new_key)
        self.assertEqual(
            WrongNotePDF.objects.filter(
                enrollment__tenant=self.tenant,
                status__in=[
                    WrongNotePDF.Status.PENDING,
                    WrongNotePDF.Status.RUNNING,
                ],
            ).count(),
            0,
        )
        self.assertEqual(
            WrongNotePDF.objects.filter(
                enrollment__tenant=self.tenant,
                status=WrongNotePDF.Status.DONE,
            ).values_list("id", flat=True).get(),
            new_job.id,
        )

        old_status_request = self.factory.get(
            f"/api/v1/results/wrong-notes/pdf/{old_job.id}/"
        )
        force_authenticate(old_status_request, user=self.staff_user)
        old_status_request.tenant = self.tenant
        old_status = WrongNotePDFStatusView.as_view()(
            old_status_request,
            job_id=old_job.id,
        )
        new_status_request = self.factory.get(
            f"/api/v1/results/wrong-notes/pdf/{new_job.id}/"
        )
        force_authenticate(new_status_request, user=self.staff_user)
        new_status_request.tenant = self.tenant
        new_status = WrongNotePDFStatusView.as_view()(
            new_status_request,
            job_id=new_job.id,
        )

        self.assertEqual(old_status.status_code, 200)
        self.assertEqual(old_status.data["status"], WrongNotePDF.Status.FAILED)
        self.assertEqual(old_status.data["file_url"], "")
        self.assertEqual(new_status.status_code, 200)
        self.assertEqual(new_status.data["status"], WrongNotePDF.Status.DONE)
        self.assertEqual(
            new_status.data["file_url"],
            "https://storage.test/current-wrong-note.pdf",
        )
        presign.assert_called_once_with(
            key=new_key,
            expires_in=3600,
            filename=f"wrong-note-{new_job.id}.pdf",
            content_type="application/pdf",
        )

    @patch(
        "apps.domains.results.services.wrong_note_pdf_worker.generate_and_store_wrong_note_pdf"
    )
    def test_tools_worker_scopes_pdf_lookup_by_contract_tenant(self, generate):
        pdf_job = WrongNotePDF.objects.create(
            enrollment=self.enroll_a,
            lecture=self.lecture,
            status=WrongNotePDF.Status.PENDING,
        )
        contract = AIJob(
            id="wrong-tenant-contract",
            type="wrong_note_pdf_generation",
            tenant_id=str(self.tenant.id + 999),
            source_domain="results_wrong_note_pdf",
            source_id=str(pdf_job.id),
            payload={"wrong_note_pdf_job_id": pdf_job.id},
        )

        result = handle_wrong_note_pdf_generation_job(contract)

        self.assertEqual(result.status, "FAILED")
        pdf_job.refresh_from_db()
        self.assertEqual(pdf_job.status, WrongNotePDF.Status.PENDING)
        generate.assert_not_called()

    @patch(
        "apps.domains.results.services.wrong_note_pdf_worker.generate_and_store_wrong_note_pdf"
    )
    def test_tools_worker_rejects_mismatched_source_fingerprint(self, generate):
        pdf_job = WrongNotePDF.objects.create(
            enrollment=self.enroll_a,
            lecture=self.lecture,
            status=WrongNotePDF.Status.PENDING,
            source_fingerprint="a" * 64,
        )
        contract = self._wrong_note_ai_contract(pdf_job)
        contract.payload["source_fingerprint"] = "b" * 64

        result = handle_wrong_note_pdf_generation_job(contract)

        self.assertEqual(result.status, "DONE")
        self.assertEqual(result.result["outcome"], WrongNotePDF.Status.FAILED)
        self.assertIn("일치하지 않습니다", result.result["error_message"])
        pdf_job.refresh_from_db()
        self.assertEqual(pdf_job.status, WrongNotePDF.Status.PENDING)
        generate.assert_not_called()

    @patch("apps.domains.results.services.wrong_note_pdf_service.time.sleep")
    @patch(
        "apps.domains.results.services.wrong_note_pdf_service.delete_object_r2_storage",
        side_effect=[TimeoutError("transient"), None],
    )
    def test_pdf_object_cleanup_retries_transient_failure(self, delete, sleep):
        self.assertTrue(delete_wrong_note_pdf_object("tracked.pdf"))
        self.assertEqual(delete.call_count, 2)
        sleep.assert_called_once_with(0.25)

    @patch("apps.domains.results.services.wrong_note_pdf_service.time.sleep")
    @patch(
        "apps.domains.results.services.wrong_note_pdf_service.delete_object_r2_storage",
        side_effect=TimeoutError("persistent"),
    )
    def test_pdf_object_cleanup_tracks_key_after_bounded_retries(self, delete, sleep):
        self.assertFalse(delete_wrong_note_pdf_object("tracked.pdf"))
        self.assertEqual(delete.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    @patch(
        "apps.domains.results.management.commands.cleanup_failed_wrong_note_pdfs."
        "delete_wrong_note_pdf_object",
        return_value=True,
    )
    def test_failed_pdf_reconciler_deletes_and_clears_tracked_key(self, delete):
        job = WrongNotePDF.objects.create(
            enrollment=self.enroll_a,
            lecture=self.lecture,
            status=WrongNotePDF.Status.FAILED,
            file_path="tenants/1/results/wrong-notes/tracked.pdf",
        )
        WrongNotePDF.objects.filter(id=job.id).update(
            updated_at=timezone.now() - timedelta(minutes=10),
        )

        call_command(
            "cleanup_failed_wrong_note_pdfs",
            limit=10,
            older_than_minutes=5,
            silent=True,
        )

        job.refresh_from_db()
        self.assertEqual(job.file_path, "")
        delete.assert_called_once_with(
            "tenants/1/results/wrong-notes/tracked.pdf",
        )

    @patch(
        "apps.domains.results.management.commands.cleanup_failed_wrong_note_pdfs."
        "delete_wrong_note_pdf_object",
        return_value=False,
    )
    def test_failed_pdf_reconciler_retains_key_when_delete_is_unconfirmed(self, _delete):
        job = WrongNotePDF.objects.create(
            enrollment=self.enroll_a,
            lecture=self.lecture,
            status=WrongNotePDF.Status.FAILED,
            file_path="tenants/1/results/wrong-notes/unconfirmed.pdf",
        )
        WrongNotePDF.objects.filter(id=job.id).update(
            updated_at=timezone.now() - timedelta(minutes=10),
        )

        call_command(
            "cleanup_failed_wrong_note_pdfs",
            limit=10,
            older_than_minutes=5,
            silent=True,
        )

        job.refresh_from_db()
        self.assertEqual(
            job.file_path,
            "tenants/1/results/wrong-notes/unconfirmed.pdf",
        )

    @patch(
        "apps.domains.results.services.wrong_note_pdf_worker.delete_wrong_note_pdf_object",
        return_value=False,
    )
    @patch(
        "apps.domains.results.services.wrong_note_pdf_worker.generate_and_store_wrong_note_pdf",
        side_effect=TimeoutError("SDK response lost"),
    )
    def test_upload_response_loss_tracks_unconfirmed_object_key(
        self,
        _generate,
        _delete,
    ):
        pdf_job = WrongNotePDF.objects.create(
            enrollment=self.enroll_a,
            lecture=self.lecture,
            status=WrongNotePDF.Status.PENDING,
        )
        contract = self._wrong_note_ai_contract(pdf_job)

        result = handle_wrong_note_pdf_generation_job(contract)
        handled = dispatch_ai_result_to_domain(
            job_id=contract.id,
            status=result.status,
            result_payload=result.result,
            error=result.error,
            source_domain=contract.source_domain,
            source_id=contract.source_id,
        )

        self.assertTrue(handled)
        pdf_job.refresh_from_db()
        self.assertEqual(pdf_job.status, WrongNotePDF.Status.FAILED)
        self.assertEqual(
            pdf_job.file_path,
            f"tenants/{self.tenant.id}/results/wrong-notes/{pdf_job.id}.pdf",
        )

    @patch(
        "apps.domains.results.views.wrong_note_pdf_status_view.generate_presigned_get_url_storage",
        return_value="https://storage.test/wrong-note.pdf",
    )
    def test_pdf_status_returns_downloadable_pdf_url(self, presign):
        job = WrongNotePDF.objects.create(
            enrollment_id=self.enroll_a.id,
            lecture_id=self.lecture.id,
            status=WrongNotePDF.Status.DONE,
            file_path="tenants/1/results/wrong-notes/7.pdf",
        )
        view = WrongNotePDFStatusView.as_view()
        req = self.factory.get(f"/api/v1/results/wrong-notes/pdf/{job.id}/")
        force_authenticate(req, user=self.staff_user)
        req.tenant = self.tenant

        resp = view(req, job_id=job.id)

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["file_url"], "https://storage.test/wrong-note.pdf")
        presign.assert_called_once_with(
            key=job.file_path,
            expires_in=3600,
            filename=f"wrong-note-{job.id}.pdf",
            content_type="application/pdf",
        )

    def test_student_cannot_create_wrong_note_pdf_for_inactive_own_enrollment(self):
        """학생 본인 enrollment라도 비활성 수강이면 오답노트 PDF 생성 불가."""
        self.enroll_a.status = "INACTIVE"
        self.enroll_a.save(update_fields=["status", "updated_at"])

        view = WrongNotePDFCreateView.as_view()
        req = self.factory.post(
            "/api/v1/results/wrong-notes/pdf/",
            data={"enrollment_id": self.enroll_a.id},
            format="json",
        )
        force_authenticate(req, user=self.user_a)
        req.tenant = self.tenant

        resp = view(req)

        self.assertEqual(resp.status_code, 403)
        self.assertFalse(WrongNotePDF.objects.exists())

    def test_pdf_create_rejects_cross_tenant_lecture_id(self):
        """enrollment은 본인 것이어도 lecture_id가 다른 테넌트면 Job 생성 금지."""
        other_tenant = Tenant.objects.create(
            name="OtherResultsSecAcademy",
            code="ressec-other",
            is_active=True,
        )
        other_lecture = Lecture.objects.create(
            tenant=other_tenant,
            title="Other L",
            name="Other L",
            subject="MATH",
        )
        view = WrongNotePDFCreateView.as_view()
        req = self.factory.post(
            "/api/v1/results/wrong-notes/pdf/",
            data={
                "enrollment_id": self.enroll_a.id,
                "lecture_id": other_lecture.id,
            },
            format="json",
        )
        force_authenticate(req, user=self.staff_user)
        req.tenant = self.tenant

        resp = view(req)

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(WrongNotePDF.objects.exists())

    def test_pdf_create_rejects_cross_tenant_exam_id(self):
        """exam_id도 같은 tenant와 같은 lecture/session에 연결된 시험만 허용."""
        other_tenant = Tenant.objects.create(
            name="OtherResultsSecAcademy2",
            code="ressec-other-2",
            is_active=True,
        )
        other_exam = Exam.objects.create(
            tenant=other_tenant,
            title="외부시험",
            exam_type=Exam.ExamType.REGULAR,
        )
        view = WrongNotePDFCreateView.as_view()
        req = self.factory.post(
            "/api/v1/results/wrong-notes/pdf/",
            data={
                "enrollment_id": self.enroll_a.id,
                "lecture_id": self.lecture.id,
                "exam_id": other_exam.id,
            },
            format="json",
        )
        force_authenticate(req, user=self.staff_user)
        req.tenant = self.tenant

        resp = view(req)

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(WrongNotePDF.objects.exists())

    @patch(
        "apps.domains.results.views.wrong_note_pdf_view.publish_wrong_note_pdf_ai_job"
    )
    def test_pdf_create_rejects_stale_preview_fingerprint(self, publish):
        view = WrongNotePDFCreateView.as_view()
        req = self.factory.post(
            "/api/v1/results/wrong-notes/documents/",
            data={
                "enrollment_id": self.enroll_a.id,
                "source_fingerprint": "f" * 64,
            },
            format="json",
        )
        force_authenticate(req, user=self.staff_user)
        req.tenant = self.tenant

        resp = view(req)

        self.assertEqual(resp.status_code, 409, resp.data)
        self.assertIn("최신 오답", resp.data["detail"])
        self.assertRegex(resp.data["source_fingerprint"], r"^[0-9a-f]{64}$")
        self.assertFalse(WrongNotePDF.objects.exists())
        publish.assert_not_called()

    @patch(
        "apps.domains.results.views.wrong_note_pdf_view.publish_wrong_note_pdf_ai_job"
    )
    def test_pdf_create_allows_only_one_running_job_per_tenant(self, publish):
        WrongNotePDF.objects.create(
            enrollment_id=self.enroll_b.id,
            lecture_id=self.lecture.id,
            status=WrongNotePDF.Status.RUNNING,
        )
        view = WrongNotePDFCreateView.as_view()
        req = self.factory.post(
            "/api/v1/results/wrong-notes/pdf/",
            data={"enrollment_id": self.enroll_a.id},
            format="json",
        )
        force_authenticate(req, user=self.staff_user)
        req.tenant = self.tenant

        resp = view(req)

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(WrongNotePDF.objects.count(), 1)
        publish.assert_not_called()

    def test_pdf_create_staff_without_tenant_membership_rejected(self):
        """전역 is_staff라도 request.tenant 멤버십 없으면 PDF job 생성 불가."""
        other_tenant = Tenant.objects.create(
            name="OtherResultsSecAcademy3",
            code="ressec-other-3",
            is_active=True,
        )
        other_lecture = Lecture.objects.create(
            tenant=other_tenant,
            title="Other L3",
            name="Other L3",
            subject="MATH",
        )
        _other_user, other_student = _make_student(other_tenant, "O001", "외부학생")
        other_enroll = Enrollment.objects.create(
            tenant=other_tenant,
            student=other_student,
            lecture=other_lecture,
            status="ACTIVE",
        )
        staff_without_membership = User.objects.create_user(
            username="staff_without_other_membership",
            password="test1234",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=staff_without_membership,
            role="teacher",
        )

        view = WrongNotePDFCreateView.as_view()
        req = self.factory.post(
            "/api/v1/results/wrong-notes/pdf/",
            data={"enrollment_id": other_enroll.id},
            format="json",
        )
        force_authenticate(req, user=staff_without_membership)
        req.tenant = other_tenant

        resp = view(req)

        self.assertEqual(resp.status_code, 403)
        self.assertFalse(WrongNotePDF.objects.exists())

    def test_wrong_note_view_staff_without_tenant_membership_rejected(self):
        """전역 is_staff라도 request.tenant 멤버십 없으면 오답노트 조회 불가."""
        other_tenant = Tenant.objects.create(
            name="OtherResultsSecAcademy4",
            code="ressec-other-4",
            is_active=True,
        )
        other_lecture = Lecture.objects.create(
            tenant=other_tenant,
            title="Other L4",
            name="Other L4",
            subject="MATH",
        )
        _other_user, other_student = _make_student(other_tenant, "O004", "외부학생4")
        other_enroll = Enrollment.objects.create(
            tenant=other_tenant,
            student=other_student,
            lecture=other_lecture,
            status="ACTIVE",
        )
        staff_without_membership = User.objects.create_user(
            username="staff_without_wrong_note_membership",
            password="test1234",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=staff_without_membership,
            role="teacher",
        )

        view = WrongNoteView.as_view()
        req = self.factory.get(
            f"/api/v1/results/wrong-notes/?enrollment_id={other_enroll.id}"
        )
        force_authenticate(req, user=staff_without_membership)
        req.tenant = other_tenant

        resp = view(req)

        self.assertEqual(resp.status_code, 403)

    def test_wrong_note_pdf_status_staff_without_tenant_membership_rejected(self):
        """전역 is_staff라도 request.tenant 멤버십 없으면 PDF 상태 조회 불가."""
        other_tenant = Tenant.objects.create(
            name="OtherResultsSecAcademy5",
            code="ressec-other-5",
            is_active=True,
        )
        other_lecture = Lecture.objects.create(
            tenant=other_tenant,
            title="Other L5",
            name="Other L5",
            subject="MATH",
        )
        _other_user, other_student = _make_student(other_tenant, "O005", "외부학생5")
        other_enroll = Enrollment.objects.create(
            tenant=other_tenant,
            student=other_student,
            lecture=other_lecture,
            status="ACTIVE",
        )
        job = WrongNotePDF.objects.create(
            enrollment_id=other_enroll.id,
            status=WrongNotePDF.Status.PENDING,
        )
        staff_without_membership = User.objects.create_user(
            username="staff_without_pdf_status_membership",
            password="test1234",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=staff_without_membership,
            role="teacher",
        )

        view = WrongNotePDFStatusView.as_view()
        req = self.factory.get(f"/api/v1/results/wrong-notes/pdf/{job.id}/")
        force_authenticate(req, user=staff_without_membership)
        req.tenant = other_tenant

        resp = view(req, job_id=job.id)

        self.assertEqual(resp.status_code, 403)

    def test_student_cannot_poll_wrong_note_pdf_for_inactive_own_enrollment(self):
        """학생 본인 PDF job이라도 수강이 비활성화되면 상태 조회 불가."""
        self.enroll_a.status = "INACTIVE"
        self.enroll_a.save(update_fields=["status", "updated_at"])
        job = WrongNotePDF.objects.create(
            enrollment_id=self.enroll_a.id,
            status=WrongNotePDF.Status.PENDING,
        )

        view = WrongNotePDFStatusView.as_view()
        req = self.factory.get(f"/api/v1/results/wrong-notes/pdf/{job.id}/")
        force_authenticate(req, user=self.user_a)
        req.tenant = self.tenant

        resp = view(req, job_id=job.id)

        self.assertEqual(resp.status_code, 403)

    def test_attempts_user_b_pk_collision_blocked(self):
        """MyExamAttemptsView에서도 PK 충돌 사용자가 타인 attempts 접근 불가.

        과거 코드는 student_id=user.id 비교라 user_b.id == student_a.id 인 user_b가
        enrollment_a에 우연 매칭. 수정 후엔 student_profile.id 기준이라 user_b는
        student_b 의 enrollment만 보인다 → enroll_a 의 attempts는 노출되지 않는다.
        IsStudent 권한이라 200 반환하되, 매칭이 안 되면 빈 리스트 반환이 정상.
        """
        view = MyExamAttemptsView.as_view()
        req = self.factory.get("/api/v1/results/me/exams/9999/attempts/")
        force_authenticate(req, user=self.user_b)
        req.tenant = self.tenant
        resp = view(req, exam_id=9999)
        # 본인 enrollment만 봐야 하므로 enroll_a 데이터 노출 0건
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, [])

    def test_attempts_for_inactive_own_enrollment_hidden(self):
        """본인 attempt라도 시험 대상 수강이 비활성화되면 학생 히스토리에서 숨김."""
        exam = Exam.objects.create(
            tenant=self.tenant,
            title="비활성 attempt 시험",
            exam_type=Exam.ExamType.REGULAR,
            is_active=True,
        )
        ExamEnrollment.objects.create(exam=exam, enrollment=self.enroll_a)
        ExamAttempt.objects.create(
            exam=exam,
            enrollment=self.enroll_a,
            attempt_index=1,
            is_retake=False,
            is_representative=True,
            status="done",
        )
        self.enroll_a.status = "INACTIVE"
        self.enroll_a.save(update_fields=["status", "updated_at"])

        view = MyExamAttemptsView.as_view()
        req = self.factory.get(f"/api/v1/results/me/exams/{exam.id}/attempts/")
        force_authenticate(req, user=self.user_a)
        req.tenant = self.tenant

        resp = view(req, exam_id=exam.id)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, [])
