from unittest.mock import patch
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.exceptions import PermissionDenied
from rest_framework.test import APIRequestFactory, force_authenticate

from academy.application.use_cases.student_video_access_context import get_students_for_request
from apps.core.models import Tenant, TenantMembership
from apps.core.models.user import user_internal_username
from apps.domains.clinic.models import SessionParticipant
from apps.domains.clinic.views.participant_views import ParticipantViewSet
from apps.domains.community.api.views.post_views import PostViewSet
from apps.domains.community.api.views.scope_node_views import ScopeNodeViewSet
from apps.domains.fees.models import FeePayment, StudentInvoice
from apps.domains.fees.views import (
    StudentFeeInvoiceDetailView,
    StudentFeeInvoiceListView,
    StudentFeePaymentListView,
)
from apps.domains.parents.models import Parent
from apps.domains.student_app.exams.views import (
    StudentExamDetailView,
    StudentExamListView,
    StudentExamQuestionsView,
    StudentExamSubmitView,
)
from apps.domains.student_app.permissions import get_request_student
from apps.domains.student_app.sessions.views import (
    StudentSessionClearPastView,
    StudentSessionHideView,
    StudentSessionListView,
    StudentSessionUnhideView,
)
from apps.domains.students.models import Student
from apps.domains.submissions.models import Submission
from apps.domains.submissions.views.submission_view import SubmissionViewSet


User = get_user_model()


class ParentChildSelectionCrossDomainTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.tenant = Tenant.objects.create(
            code="parent-child-security",
            name="Parent Child Security",
            is_active=True,
        )
        self.parent_user = User.objects.create_user(
            username=user_internal_username(self.tenant, "parent"),
            password="pw123456",
            tenant=self.tenant,
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=self.parent_user,
            role="parent",
        )
        self.parent = Parent.objects.create(
            tenant=self.tenant,
            user=self.parent_user,
            name="Parent",
            phone="01011112222",
        )
        self.owned_student = self._student("owned", parent=self.parent)
        self.unowned_student = self._student("unowned")

    def _student(self, suffix: str, *, parent=None) -> Student:
        user = User.objects.create_user(
            username=user_internal_username(self.tenant, f"student-{suffix}"),
            password="pw123456",
            tenant=self.tenant,
        )
        student = Student.objects.create(
            tenant=self.tenant,
            user=user,
            parent=parent,
            ps_number=f"PC-{suffix}",
            omr_code=f"{suffix:0<8}"[:8],
            name=f"Student {suffix}",
            parent_phone="01011112222",
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=user, role="student")
        return student

    def _request(self, method: str, path: str, raw_student_id, data=None):
        request = getattr(self.factory, method)(
            path,
            data or {},
            format="json",
            HTTP_X_STUDENT_ID=str(raw_student_id),
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.parent_user)
        return request

    def test_owned_child_is_selected_and_invalid_explicit_ids_fail_closed(self):
        owned_request = SimpleNamespace(
            user=self.parent_user,
            tenant=self.tenant,
            META={"HTTP_X_STUDENT_ID": str(self.owned_student.id)},
        )
        self.assertEqual(get_request_student(owned_request).id, self.owned_student.id)

        for raw_student_id in ("not-a-student-id", self.unowned_student.id, ""):
            with self.subTest(raw_student_id=raw_student_id):
                with self.assertRaises(PermissionDenied):
                    get_request_student(
                        SimpleNamespace(
                            user=self.parent_user,
                            tenant=self.tenant,
                            META={"HTTP_X_STUDENT_ID": str(raw_student_id)},
                        )
                    )

    def test_parent_without_child_header_never_falls_back(self):
        request = SimpleNamespace(
            user=self.parent_user,
            tenant=self.tenant,
            META={},
        )

        with self.assertRaises(PermissionDenied):
            get_request_student(request)
        with self.assertRaises(PermissionDenied):
            get_students_for_request(request)

    @patch("apps.domains.submissions.views.submission_view.dispatch_submission")
    @patch("apps.domains.submissions.services.dispatcher.dispatch_submission")
    def test_invalid_explicit_child_is_rejected_across_reads_and_writes(
        self,
        exam_dispatch,
        submission_dispatch,
    ):
        initial_participants = SessionParticipant.objects.count()
        initial_submissions = Submission.objects.count()
        initial_invoices = StudentInvoice.objects.count()
        initial_payments = FeePayment.objects.count()

        for raw_student_id in ("not-a-student-id", self.unowned_student.id, ""):
            cases = (
                (
                    ScopeNodeViewSet.as_view({"get": "list"}),
                    self._request("get", "/api/v1/community/scope-nodes/", raw_student_id),
                    {},
                ),
                (
                    PostViewSet.as_view({"get": "board"}),
                    self._request("get", "/api/v1/community/posts/board/", raw_student_id),
                    {},
                ),
                (
                    PostViewSet.as_view({"get": "counts"}),
                    self._request(
                        "get",
                        "/api/v1/community/posts/counts/?post_type=board",
                        raw_student_id,
                    ),
                    {},
                ),
                (
                    ParticipantViewSet.as_view({"get": "list"}),
                    self._request("get", "/api/v1/clinic/participants/", raw_student_id),
                    {},
                ),
                (
                    ParticipantViewSet.as_view({"post": "create"}),
                    self._request(
                        "post",
                        "/api/v1/clinic/participants/",
                        raw_student_id,
                        {"student": self.owned_student.id},
                    ),
                    {},
                ),
                (
                    StudentFeeInvoiceListView.as_view(),
                    self._request("get", "/api/v1/student/fees/invoices/", raw_student_id),
                    {},
                ),
                (
                    StudentFeeInvoiceDetailView.as_view(),
                    self._request("get", "/api/v1/student/fees/invoices/999/", raw_student_id),
                    {"pk": 999},
                ),
                (
                    StudentFeePaymentListView.as_view(),
                    self._request("get", "/api/v1/student/fees/payments/", raw_student_id),
                    {},
                ),
                (
                    StudentExamListView.as_view(),
                    self._request("get", "/api/v1/student/exams/", raw_student_id),
                    {},
                ),
                (
                    StudentExamDetailView.as_view(),
                    self._request("get", "/api/v1/student/exams/999/", raw_student_id),
                    {"pk": 999},
                ),
                (
                    StudentExamQuestionsView.as_view(),
                    self._request("get", "/api/v1/student/exams/999/questions/", raw_student_id),
                    {"pk": 999},
                ),
                (
                    StudentExamSubmitView.as_view(),
                    self._request(
                        "post",
                        "/api/v1/student/exams/999/submit/",
                        raw_student_id,
                        {"answers": []},
                    ),
                    {"pk": 999},
                ),
                (
                    StudentSessionListView.as_view(),
                    self._request("get", "/api/v1/student/sessions/me/", raw_student_id),
                    {},
                ),
                (
                    StudentSessionClearPastView.as_view(),
                    self._request("post", "/api/v1/student/sessions/clear-past/", raw_student_id),
                    {},
                ),
                (
                    StudentSessionHideView.as_view(),
                    self._request(
                        "post",
                        "/api/v1/student/sessions/hide/",
                        raw_student_id,
                        {"id": 123},
                    ),
                    {},
                ),
                (
                    StudentSessionUnhideView.as_view(),
                    self._request(
                        "post",
                        "/api/v1/student/sessions/unhide/",
                        raw_student_id,
                        {"id": 123},
                    ),
                    {},
                ),
                (
                    SubmissionViewSet.as_view({"post": "create"}),
                    self._request(
                        "post",
                        "/api/v1/submissions/submissions/",
                        raw_student_id,
                        {
                            "target_type": "exam",
                            "target_id": 999,
                            "source": "online",
                            "enrollment_id": 999,
                            "payload": {"answers": []},
                        },
                    ),
                    {},
                ),
            )

            for view, request, kwargs in cases:
                with self.subTest(raw_student_id=raw_student_id, path=request.path):
                    response = view(request, **kwargs)
                    self.assertEqual(response.status_code, 403, response.data)

        self.owned_student.refresh_from_db()
        self.assertIsNone(self.owned_student.schedule_hidden_before)
        self.assertEqual(self.owned_student.schedule_hidden_ids, [])
        self.assertEqual(SessionParticipant.objects.count(), initial_participants)
        self.assertEqual(Submission.objects.count(), initial_submissions)
        self.assertEqual(StudentInvoice.objects.count(), initial_invoices)
        self.assertEqual(FeePayment.objects.count(), initial_payments)
        exam_dispatch.assert_not_called()
        submission_dispatch.assert_not_called()
