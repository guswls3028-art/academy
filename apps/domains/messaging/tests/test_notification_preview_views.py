from datetime import timedelta
from uuid import uuid4

from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.core.models import Tenant, TenantMembership
from apps.core.models.user import user_internal_username
from apps.domains.messaging.models import (
    AutoSendConfig,
    MessageTemplate,
    NotificationPreviewToken,
    ScheduledNotification,
)
from unittest.mock import patch

from apps.domains.messaging.notification_dispatch import build_student_list_preview, execute_notification_batch
from apps.domains.messaging.scheduled import MessagingHourlyQuotaExceeded
from apps.domains.messaging.views_notification import (
    AttendanceNotificationConfirmView,
    AttendanceNotificationPreviewView,
    ManualNotificationConfirmView,
    ManualNotificationPreviewView,
)
from apps.worker.messaging_worker.sqs_main import _allowed_common_template_ids
User = get_user_model()
Student = apps.get_model("students", "Student")


class NotificationPreviewViewValidationTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.tenant = Tenant.objects.create(code="msg-preview", name="Msg Preview", is_active=True)
        self.admin = User.objects.create_user(
            username="msg-preview-owner",
            password="test1234",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=self.admin, role="owner")

    def _post(self, view, path: str, data: dict, *, user=None):
        request = self.factory.post(path, data=data, format="json")
        actor = user or self.admin
        force_authenticate(request, user=actor)
        request.user = actor
        request.tenant = self.tenant
        return view.as_view()(request)

    def test_generic_staff_cannot_preview_or_confirm_external_messages(self):
        staff = User.objects.create_user(
            username="msg-preview-staff",
            password="test1234",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=staff, role="staff")

        cases = (
            (
                AttendanceNotificationPreviewView,
                "/api/v1/messaging/attendance-notification/preview/",
                {"session_id": 1, "notification_type": "check_in"},
            ),
            (
                AttendanceNotificationConfirmView,
                "/api/v1/messaging/attendance-notification/confirm/",
                {"preview_token": str(uuid4())},
            ),
            (
                ManualNotificationPreviewView,
                "/api/v1/messaging/manual-notification/preview/",
                {"trigger": "exam_score_published", "student_ids": [1]},
            ),
            (
                ManualNotificationConfirmView,
                "/api/v1/messaging/manual-notification/confirm/",
                {"preview_token": str(uuid4())},
            ),
        )
        for view, path, data in cases:
            with self.subTest(path=path):
                response = self._post(view, path, data, user=staff)
                self.assertEqual(response.status_code, 403)

    def test_teacher_can_open_manual_message_preview(self):
        teacher = User.objects.create_user(
            username="msg-preview-teacher",
            password="test1234",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=teacher, role="teacher")
        with patch(
            "apps.domains.messaging.views_notification.build_student_list_preview",
            return_value={"recipients": [], "total_count": 0, "excluded_count": 0},
        ):
            response = self._post(
                ManualNotificationPreviewView,
                "/api/v1/messaging/manual-notification/preview/",
                {"trigger": "exam_score_published", "student_ids": [1]},
                user=teacher,
            )

        self.assertEqual(response.status_code, 200)

    def test_attendance_preview_rejects_invalid_session_id(self):
        response = self._post(
            AttendanceNotificationPreviewView,
            "/api/v1/messaging/attendance-notification/preview/",
            {"session_id": "abc", "notification_type": "check_in", "send_to": "parent"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["detail"], "session_id는 양의 정수여야 합니다.")

    def test_attendance_preview_rejects_invalid_send_to(self):
        response = self._post(
            AttendanceNotificationPreviewView,
            "/api/v1/messaging/attendance-notification/preview/",
            {"session_id": 1, "notification_type": "check_in", "send_to": "staff"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["detail"], "send_to는 'parent' 또는 'student'만 가능합니다.")

    def test_manual_preview_rejects_invalid_send_to(self):
        response = self._post(
            ManualNotificationPreviewView,
            "/api/v1/messaging/manual-notification/preview/",
            {"trigger": "exam_score_published", "student_ids": [1], "send_to": "staff"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["detail"], "send_to는 'parent' 또는 'student'만 가능합니다.")

    def test_manual_score_preview_allows_student_recipient(self):
        with patch(
            "apps.domains.messaging.views_notification.build_student_list_preview",
            return_value={"recipients": [], "total_count": 0, "excluded_count": 0},
        ) as build_preview:
            response = self._post(
                ManualNotificationPreviewView,
                "/api/v1/messaging/manual-notification/preview/",
                {
                    "trigger": "exam_score_published",
                    "student_ids": [1],
                    "send_to": "student",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(build_preview.call_args.kwargs["send_to"], "student")

    def test_manual_preview_rejects_invalid_student_ids(self):
        response = self._post(
            ManualNotificationPreviewView,
            "/api/v1/messaging/manual-notification/preview/",
            {"trigger": "exam_score_published", "student_ids": [1, "bad"], "send_to": "parent"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["detail"], "student_ids는 양의 정수 목록이어야 합니다.")

    def test_manual_preview_rejects_non_object_context(self):
        response = self._post(
            ManualNotificationPreviewView,
            "/api/v1/messaging/manual-notification/preview/",
            {
                "trigger": "exam_score_published",
                "student_ids": [1],
                "send_to": "parent",
                "context": ["bad"],
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["detail"], "context는 객체여야 합니다.")

    def test_manual_preview_rejects_non_object_context_per_student(self):
        response = self._post(
            ManualNotificationPreviewView,
            "/api/v1/messaging/manual-notification/preview/",
            {
                "trigger": "exam_score_published",
                "student_ids": [1],
                "send_to": "parent",
                "context_per_student": ["bad"],
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["detail"], "context_per_student는 객체여야 합니다.")

    def test_manual_preview_rejects_oversized_context_before_preview_build(self):
        with patch(
            "apps.domains.messaging.views_notification.build_student_list_preview"
        ) as build_preview:
            response = self._post(
                ManualNotificationPreviewView,
                "/api/v1/messaging/manual-notification/preview/",
                {
                    "trigger": "exam_score_published",
                    "student_ids": [1],
                    "send_to": "parent",
                    "context": {"시험명": "x" * 2_000_000},
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("200KB", response.data["detail"])
        build_preview.assert_not_called()

    def test_manual_preview_rejects_oversized_per_student_value_before_preview_build(self):
        with patch(
            "apps.domains.messaging.views_notification.build_student_list_preview"
        ) as build_preview:
            response = self._post(
                ManualNotificationPreviewView,
                "/api/v1/messaging/manual-notification/preview/",
                {
                    "trigger": "exam_score_published",
                    "student_ids": [1],
                    "send_to": "parent",
                    "context_per_student": {"1": {"시험명": "x" * 1_001}},
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("최대 1000자", response.data["detail"])
        build_preview.assert_not_called()

    def test_manual_preview_zero_sendable_recipients_hides_internal_fields(self):
        student_user = User.objects.create_user(
            username=user_internal_username(self.tenant, "S001"),
            password="test1234",
            tenant=self.tenant,
            phone="",
            name="전화없음",
        )
        student = Student.objects.create(
            tenant=self.tenant,
            user=student_user,
            ps_number="S001",
            name="전화없음",
            phone="",
            parent_phone="",
            omr_code="11112222",
        )
        template = MessageTemplate.objects.create(
            tenant=self.tenant,
            category="grades",
            name="성적 안내",
            subject="",
            body="성적 안내 #{학생이름}",
            solapi_template_id="APPROVED-SID",
            solapi_status="APPROVED",
        )
        AutoSendConfig.objects.create(
            tenant=self.tenant,
            trigger="exam_score_published",
            template=template,
            enabled=False,
            message_mode="alimtalk",
        )

        response = self._post(
            ManualNotificationPreviewView,
            "/api/v1/messaging/manual-notification/preview/",
            {"trigger": "exam_score_published", "student_ids": [student.id], "send_to": "parent"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data["preview_token"])
        self.assertEqual(response.data["total_count"], 0)
        self.assertEqual(response.data["excluded_count"], 1)
        recipient = response.data["recipients"][0]
        self.assertNotIn("phone_raw", recipient)
        self.assertNotIn("alimtalk_replacements", recipient)

    def test_manual_preview_omits_deleted_and_cross_tenant_students(self):
        active_user = User.objects.create_user(
            username=user_internal_username(self.tenant, "S010"),
            password="test1234",
            tenant=self.tenant,
            phone="01010101010",
            name="활성학생",
        )
        active_student = Student.objects.create(
            tenant=self.tenant,
            user=active_user,
            ps_number="S010",
            name="활성학생",
            phone="01010101010",
            parent_phone="01055556666",
            omr_code="10101010",
        )
        deleted_user = User.objects.create_user(
            username=user_internal_username(self.tenant, "S011"),
            password="test1234",
            tenant=self.tenant,
            phone="01020202020",
            name="삭제학생",
        )
        deleted_student = Student.objects.create(
            tenant=self.tenant,
            user=deleted_user,
            ps_number="S011",
            name="삭제학생",
            phone="01020202020",
            parent_phone="01066667777",
            omr_code="20202020",
            deleted_at=timezone.now(),
        )
        other_tenant = Tenant.objects.create(code="msg-preview-other", name="Other", is_active=True)
        other_user = User.objects.create_user(
            username=user_internal_username(other_tenant, "S999"),
            password="test1234",
            tenant=other_tenant,
            phone="01099998888",
            name="타원생",
        )
        other_student = Student.objects.create(
            tenant=other_tenant,
            user=other_user,
            ps_number="S999",
            name="타원생",
            phone="01099998888",
            parent_phone="01077778888",
            omr_code="99998888",
        )
        template = MessageTemplate.objects.create(
            tenant=self.tenant,
            category="grades",
            name="성적 안내",
            subject="",
            body="성적 안내 #{학생이름}",
            solapi_template_id="APPROVED-SID",
            solapi_status="APPROVED",
        )
        AutoSendConfig.objects.create(
            tenant=self.tenant,
            trigger="exam_score_published",
            template=template,
            enabled=False,
            message_mode="alimtalk",
        )

        response = self._post(
            ManualNotificationPreviewView,
            "/api/v1/messaging/manual-notification/preview/",
            {
                "trigger": "exam_score_published",
                "student_ids": [
                    active_student.id,
                    deleted_student.id,
                    other_student.id,
                    active_student.id,
                ],
                "send_to": "parent",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["total_count"], 1)
        self.assertEqual(response.data["excluded_count"], 0)
        self.assertEqual(len(response.data["recipients"]), 1)
        recipient = response.data["recipients"][0]
        self.assertEqual(recipient["student_id"], active_student.id)
        self.assertEqual(recipient["phone"], "010****6666")
        self.assertNotIn("phone_raw", recipient)
        self.assertNotIn("alimtalk_replacements", recipient)
        self.assertIn("Msg Preview입니다.", recipient["full_message_body"])
        self.assertIn("활성학생학생님.", recipient["full_message_body"])
        self.assertIn("성적 안내 활성학생", recipient["full_message_body"])

        token = NotificationPreviewToken.objects.get(token=response.data["preview_token"])
        payload_recipient = token.payload["recipients"][0]
        self.assertEqual(payload_recipient["student_id"], active_student.id)
        self.assertEqual(payload_recipient["phone_raw"], "01055556666")
        self.assertNotIn("full_message_body", payload_recipient)

    def test_manual_student_list_preview_uses_owner_exact_template_for_non_owner_tenant(self):
        owner = Tenant.objects.create(code="msg-owner", name="Owner", is_active=True)
        tenant = Tenant.objects.create(code="msg-child", name="Child", is_active=True)
        student_user = User.objects.create_user(
            username=user_internal_username(tenant, "S020"),
            password="test1234",
            tenant=tenant,
            phone="01010102020",
            name="비오너학생",
        )
        student = Student.objects.create(
            tenant=tenant,
            user=student_user,
            ps_number="S020",
            name="비오너학생",
            phone="01010102020",
            parent_phone="01088889999",
            omr_code="20202020",
        )
        tenant_template = MessageTemplate.objects.create(
            tenant=tenant,
            category="grades",
            name="테넌트 성적 안내",
            subject="",
            body="테넌트 문구 #{학생이름}",
            solapi_template_id="TENANT-PENDING",
            solapi_status="PENDING",
        )
        owner_template = MessageTemplate.objects.create(
            tenant=owner,
            category="grades",
            name="오너 성적 안내",
            subject="",
            body="오너 검수 문구 #{학생이름}",
            solapi_template_id="OWNER-APPROVED",
            solapi_status="APPROVED",
        )
        AutoSendConfig.objects.create(
            tenant=tenant,
            trigger="owner_exact_manual_notice",
            template=tenant_template,
            enabled=True,
            message_mode="alimtalk",
        )
        AutoSendConfig.objects.create(
            tenant=owner,
            trigger="owner_exact_manual_notice",
            template=owner_template,
            enabled=True,
            message_mode="alimtalk",
        )

        with override_settings(OWNER_TENANT_ID=owner.id):
            preview = build_student_list_preview(
                tenant,
                trigger="owner_exact_manual_notice",
                student_ids=[student.id],
                send_to="parent",
            )

        self.assertNotIn("error", preview)
        self.assertEqual(preview["solapi_template_id"], "OWNER-APPROVED")
        self.assertEqual(preview["message_template_body"], "오너 검수 문구 #{학생이름}")
        self.assertEqual(preview["recipients"][0]["message_body"], "오너 검수 문구 비오너학생")

        with override_settings(OWNER_TENANT_ID=owner.id):
            batch = execute_notification_batch(
                tenant,
                preview,
                batch_id="owner-exact-contract",
                staff_id=None,
                process=False,
            )
            scheduled = ScheduledNotification.objects.get(tenant=tenant)
            self.assertEqual(scheduled.trigger, "owner_exact_manual_notice")
            self.assertEqual(scheduled.payload["event_type"], "owner_exact_manual_notice")
            self.assertIn("OWNER-APPROVED", _allowed_common_template_ids("owner_exact_manual_notice"))
        self.assertEqual(batch["pending_count"], 1)

    def test_manual_preview_rejects_cross_tenant_content_template_drift(self):
        other = Tenant.objects.create(code="msg-preview-drift", name="Drift", is_active=True)
        foreign_template = MessageTemplate.objects.create(
            tenant=other,
            category="grades",
            name="Foreign",
            body="다른 학원 문구",
            solapi_template_id="FOREIGN-APPROVED",
            solapi_status="APPROVED",
        )
        AutoSendConfig.objects.create(
            tenant=self.tenant,
            trigger="exam_score_published",
            template=foreign_template,
            enabled=False,
            message_mode="alimtalk",
        )

        preview = build_student_list_preview(
            self.tenant,
            trigger="exam_score_published",
            student_ids=[1],
        )

        self.assertEqual(preview["error"], "발송 템플릿의 테넌트가 일치하지 않습니다.")


class NotificationBatchDispatchPolicyTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(code="msg-batch", name="Msg Batch", is_active=True)

    @patch("apps.domains.messaging.policy.check_recipient_allowed", return_value=True)
    @patch("apps.domains.messaging.services.enqueue_alimtalk", return_value=True)
    def test_non_alimtalk_preview_payload_is_blocked(self, mock_enqueue, _mock_allowed):
        result = execute_notification_batch(
            tenant=self.tenant,
            payload={
                "recipients": [
                    {
                        "student_id": 11,
                        "student_name": "정합성",
                        "phone_raw": "01012345678",
                        "message_body": "알림 본문",
                        "alimtalk_replacements": [{"key": "선생님메모", "value": "알림 본문"}],
                    }
                ],
                "solapi_template_id": "KA01TP_TEST",
                "message_mode": "sms",
                "notification_type": "check_in",
            },
            batch_id="batch-legacy-sms",
            staff_id=7,
        )

        self.assertEqual(result["sent_count"], 0)
        self.assertEqual(result["pending_count"], 0)
        self.assertEqual(result["accepted_count"], 0)
        self.assertEqual(result["failed_count"], 1)
        mock_enqueue.assert_not_called()

    @patch("apps.domains.messaging.policy.check_recipient_allowed", return_value=True)
    @patch("apps.domains.messaging.services.enqueue_alimtalk", return_value=True)
    def test_preview_batch_marks_student_target_when_sending_to_student(self, mock_enqueue, _mock_allowed):
        result = execute_notification_batch(
            tenant=self.tenant,
            payload={
                "recipients": [
                    {
                        "student_id": 12,
                        "student_name": "학생수신",
                        "phone_raw": "01012345678",
                        "message_body": "학생 안내",
                        "alimtalk_replacements": [{"key": "선생님메모", "value": "학생 안내"}],
                    }
                ],
                "solapi_template_id": "KA01TP_TEST",
                "message_mode": "alimtalk",
                "notification_type": "clinic_reminder",
                "send_to": "student",
            },
            batch_id="batch-student-target",
            staff_id=7,
        )

        self.assertEqual(result["sent_count"], 1)
        self.assertEqual(result["pending_count"], 0)
        self.assertEqual(result["accepted_count"], 1)
        self.assertEqual(mock_enqueue.call_args.kwargs["target_type"], "student")


class NotificationPreviewConfirmDurabilityTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.tenant = Tenant.objects.create(
            code="msg-confirm-durable",
            name="Msg Confirm Durable",
            is_active=True,
        )
        self.admin = User.objects.create_user(
            username="msg-confirm-durable-owner",
            password="test1234",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=self.admin,
            role="owner",
        )

    def _token(self) -> NotificationPreviewToken:
        return NotificationPreviewToken.objects.create(
            token=uuid4(),
            tenant=self.tenant,
            notification_type="clinic_reminder",
            session_type="manual",
            session_id=0,
            send_to="parent",
            payload={
                "recipients": [
                    {
                        "student_id": 101,
                        "student_name": "내구성",
                        "phone_raw": "01012345678",
                        "message_body": "확정 발송",
                        "alimtalk_replacements": [],
                    }
                ],
                "solapi_template_id": "KA01TP_DURABLE",
                "message_mode": "alimtalk",
                "notification_type": "clinic_reminder",
                "send_to": "parent",
            },
            expires_at=timezone.now() + timedelta(minutes=5),
        )

    def _confirm(self, token: NotificationPreviewToken):
        request = self.factory.post(
            "/api/v1/messaging/manual-notification/confirm/",
            {"preview_token": str(token.token)},
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant
        return ManualNotificationConfirmView.as_view()(request)

    @patch("apps.domains.messaging.policy.check_recipient_allowed", return_value=True)
    @patch("apps.domains.messaging.services.enqueue_alimtalk", return_value=False)
    def test_queue_failure_keeps_durable_pending_outbox_and_consumes_token_once(
        self,
        mock_enqueue,
        _mock_allowed,
    ):
        token = self._token()

        response = self._confirm(token)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["sent_count"], 0)
        self.assertEqual(response.data["pending_count"], 1)
        self.assertEqual(response.data["accepted_count"], 1)
        token.refresh_from_db()
        self.assertIsNotNone(token.used_at)
        self.assertEqual(token.payload["recipients"], [])
        self.assertEqual(token.payload["redacted"], True)
        self.assertNotIn("01012345678", str(token.payload))
        self.assertNotIn("확정 발송", str(token.payload))
        outbox = ScheduledNotification.objects.get(tenant=self.tenant)
        self.assertEqual(outbox.status, ScheduledNotification.Status.PENDING)
        self.assertEqual(outbox.attempt_count, 1)
        mock_enqueue.assert_called_once()

        duplicate = self._confirm(token)
        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(ScheduledNotification.objects.count(), 1)

    @patch(
        "apps.domains.messaging.scheduled.create_notification_outboxes",
        side_effect=MessagingHourlyQuotaExceeded("hourly_notification_limit"),
    )
    def test_reservation_failure_rolls_back_token_consumption(self, _mock_create):
        token = self._token()

        response = self._confirm(token)

        self.assertEqual(response.status_code, 429)
        token.refresh_from_db()
        self.assertIsNone(token.used_at)
        self.assertFalse(ScheduledNotification.objects.exists())
