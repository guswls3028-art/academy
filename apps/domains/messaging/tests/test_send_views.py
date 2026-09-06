from datetime import timedelta
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.core.models import Tenant, TenantMembership
from apps.core.models.user import user_internal_username
from apps.domains.messaging.models import MessageTemplate, NotificationLog, ScheduledNotification
from apps.domains.messaging.views.send_views import SendMessageView
from apps.domains.messaging.views.template_views import MessageTemplateListCreateView


User = get_user_model()
Student = apps.get_model("students", "Student")


class SendMessageViewTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.tenant = Tenant.objects.create(
            code="msg-send",
            name="Msg Send",
            is_active=True,
            messaging_sender="01012345678",
        )
        self.admin = User.objects.create_user(
            username="msg-send-owner",
            password="test1234",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=self.admin, role="owner")

        student_user = User.objects.create_user(
            username=user_internal_username(self.tenant, "S001"),
            password="test1234",
            tenant=self.tenant,
            phone="01011112222",
            name="테스트학생",
        )
        self.student = Student.objects.create(
            tenant=self.tenant,
            user=student_user,
            ps_number="S001",
            name="테스트학생",
            phone="01011112222",
            parent_phone="01033334444",
            omr_code="11112222",
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=student_user, role="student")

        MessageTemplate.objects.create(
            tenant=self.tenant,
            category="default",
            name="자유양식",
            subject="",
            body="#{공지내용}\n#{사이트링크}",
            solapi_template_id="FREEFORM-SID",
            solapi_status="APPROVED",
            is_system=True,
        )

    def _send(self, request):
        # Immediate dispatch is intentionally registered with transaction.on_commit.
        # TestCase wraps each test in a transaction, so execute callbacks explicitly.
        with self.captureOnCommitCallbacks(execute=True):
            return SendMessageView.as_view()(request)

    def _create_student(
        self,
        suffix: str,
        *,
        phone: str,
        parent_phone: str,
    ):
        student_user = User.objects.create_user(
            username=user_internal_username(self.tenant, suffix),
            password="test1234",
            tenant=self.tenant,
            phone=phone,
            name=f"학생{suffix}",
        )
        student = Student.objects.create(
            tenant=self.tenant,
            user=student_user,
            ps_number=suffix,
            name=f"학생{suffix}",
            phone=phone,
            parent_phone=parent_phone,
            omr_code=f"99{suffix}",
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=student_user,
            role="student",
        )
        return student

    def test_student_direct_alimtalk_uses_selected_attendance_envelope(self):
        request = self.factory.post(
            "/api/v1/messaging/send/",
            data={
                "send_to": "student",
                "student_ids": [self.student.id],
                "raw_body": "직접 작성한 안내입니다.",
                "block_category": "attendance",
            },
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.user = self.admin
        request.tenant = self.tenant

        with (
            patch("apps.domains.messaging.services.get_tenant_site_url", return_value="https://example.test"),
            patch("apps.domains.messaging.services.enqueue_alimtalk", return_value=True) as enqueue_alimtalk,
        ):
            response = self._send(request)

        self.assertEqual(response.status_code, 200)
        # The response is returned before the outer transaction commits. The
        # durable outbox is accepted as scheduled, then the callback enqueues it.
        self.assertEqual(response.data["enqueued"], 0)
        self.assertEqual(response.data["scheduled"], 1)
        enqueue_alimtalk.assert_called_once()
        self.assertEqual(
            ScheduledNotification.objects.get(tenant=self.tenant).status,
            ScheduledNotification.Status.SENT,
        )
        kwargs = enqueue_alimtalk.call_args.kwargs
        self.assertEqual(kwargs["to"], "01011112222")
        self.assertEqual(kwargs["target_type"], "student")
        self.assertEqual(kwargs["target_id"], self.student.id)
        self.assertEqual(kwargs["template_id"], "KA01TP260406121126868FGddLmrDFUC")
        replacements = {item["key"]: item["value"] for item in kwargs["alimtalk_replacements"]}
        self.assertEqual(replacements["선생님메모"], "직접 작성한 안내입니다.")
        self.assertNotIn("공지내용", replacements)
        self.assertNotIn("내용", replacements)
        self.assertNotIn("선생님메모1", replacements)

    def test_manual_send_blocks_when_source_business_tenant_quota_is_full(self):
        provider_owner = Tenant.objects.create(code="msg-send-provider", name="Provider", is_active=True)
        NotificationLog.objects.create(
            tenant=provider_owner,
            source_tenant=self.tenant,
            success=False,
            status="processing",
            message_mode="alimtalk",
        )
        request = self.factory.post(
            "/api/v1/messaging/send/",
            data={
                "send_to": "student",
                "student_ids": [self.student.id],
                "raw_body": "한도 확인 안내입니다.",
                "block_category": "attendance",
            },
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.user = self.admin
        request.tenant = self.tenant

        with (
            patch("apps.domains.messaging.views.send_views.HOURLY_SEND_LIMIT", 1),
            patch("apps.domains.messaging.services.enqueue_alimtalk", return_value=True) as enqueue_alimtalk,
        ):
            response = self._send(request)

        self.assertEqual(response.status_code, 429)
        enqueue_alimtalk.assert_not_called()

    def test_default_direct_alimtalk_requires_selected_envelope(self):
        request = self.factory.post(
            "/api/v1/messaging/send/",
            data={
                "send_to": "student",
                "student_ids": [self.student.id],
                "raw_body": "봉투 없이 직접 작성한 안내입니다.",
                "block_category": "default",
            },
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.user = self.admin
        request.tenant = self.tenant

        with patch("apps.domains.messaging.services.enqueue_alimtalk", return_value=True) as enqueue_alimtalk:
            response = self._send(request)

        self.assertEqual(response.status_code, 400)
        self.assertIn("카카오 승인 봉투", response.data["detail"])
        enqueue_alimtalk.assert_not_called()

    def test_exam_category_manual_send_uses_attendance_unified_envelope(self):
        template = MessageTemplate.objects.create(
            tenant=self.tenant,
            category="exam",
            name="시험 안내",
            subject="",
            body="시험 안내입니다. #{강의명} #{차시명} #{시험명}",
            solapi_template_id="",
            solapi_status="",
        )
        request = self.factory.post(
            "/api/v1/messaging/send/",
            data={
                "send_to": "student",
                "student_ids": [self.student.id],
                "template_id": template.id,
                "raw_body": "시험 안내입니다. #{강의명} #{차시명} #{시험명}",
                "block_category": "exam",
                "alimtalk_extra_vars": {
                    "강의명": "수학A반",
                    "차시명": "3회차",
                    "시험명": "중간고사",
                },
            },
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.user = self.admin
        request.tenant = self.tenant

        with (
            patch("apps.domains.messaging.services.get_tenant_site_url", return_value="https://example.test"),
            patch("apps.domains.messaging.services.enqueue_alimtalk", return_value=True) as enqueue_alimtalk,
        ):
            response = self._send(request)

        self.assertEqual(response.status_code, 200, response.data)
        kwargs = enqueue_alimtalk.call_args.kwargs
        self.assertEqual(kwargs["template_id"], "KA01TP260406121126868FGddLmrDFUC")
        replacements = {item["key"]: item["value"] for item in kwargs["alimtalk_replacements"]}
        self.assertEqual(replacements["강의명"], "수학A반")
        self.assertEqual(replacements["차시명"], "3회차")
        self.assertIn("중간고사", replacements["선생님메모"])

    def test_parent_direct_alimtalk_uses_parent_phone_and_target_type(self):
        request = self.factory.post(
            "/api/v1/messaging/send/",
            data={
                "send_to": "parent",
                "student_ids": [self.student.id],
                "raw_body": "학부모 안내입니다.",
                "block_category": "attendance",
            },
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.user = self.admin
        request.tenant = self.tenant

        with (
            patch("apps.domains.messaging.services.get_tenant_site_url", return_value="https://example.test"),
            patch("apps.domains.messaging.services.enqueue_alimtalk", return_value=True) as enqueue_alimtalk,
        ):
            response = self._send(request)

        self.assertEqual(response.status_code, 200)
        enqueue_alimtalk.assert_called_once()
        kwargs = enqueue_alimtalk.call_args.kwargs
        self.assertEqual(kwargs["to"], "01033334444")
        self.assertEqual(kwargs["target_type"], "parent")
        self.assertEqual(kwargs["target_id"], self.student.id)

    def test_grade_message_to_student_dispatches_to_student_phone(self):
        request = self.factory.post(
            "/api/v1/messaging/send/",
            data={
                "send_to": "student",
                "student_ids": [self.student.id],
                "raw_body": "성적표 안내입니다.",
                "block_category": "grades",
                "alimtalk_extra_vars": {
                    "강의명": "중2 수학",
                    "차시명": "1차시",
                },
                "alimtalk_extra_vars_per_student": {
                    str(self.student.id): {
                        "_body_subst": "단원평가 85/100",
                    },
                },
            },
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.user = self.admin
        request.tenant = self.tenant

        with (
            patch(
                "apps.domains.messaging.services.get_tenant_site_url",
                return_value="https://example.test",
            ),
            patch(
                "apps.domains.messaging.services.enqueue_alimtalk",
                return_value=True,
            ) as enqueue_alimtalk,
        ):
            response = self._send(request)

        self.assertEqual(response.status_code, 200, response.data)
        enqueue_alimtalk.assert_called_once()
        self.assertEqual(enqueue_alimtalk.call_args.kwargs["to"], "01011112222")
        self.assertEqual(enqueue_alimtalk.call_args.kwargs["target_type"], "student")
        self.assertEqual(enqueue_alimtalk.call_args.kwargs["target_id"], self.student.id)

    def test_grade_message_rejects_incomplete_per_student_bodies(self):
        second_student = self._create_student(
            "S002",
            phone="01022223333",
            parent_phone="01044445555",
        )
        first_body = "시험 80/100\n과제 20/100\n메인자료 미제출"
        request = self.factory.post(
            "/api/v1/messaging/send/",
            data={
                "send_to": "parent",
                "student_ids": [self.student.id, second_student.id],
                "raw_body": first_body,
                "block_category": "grades",
                "alimtalk_extra_vars": {
                    "강의명": "중2 과학",
                    "차시명": "2차시",
                    "시험성적": "80/100",
                },
                "alimtalk_extra_vars_per_student": {
                    str(self.student.id): {"_body_subst": first_body},
                },
            },
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.user = self.admin
        request.tenant = self.tenant

        with patch(
            "apps.domains.messaging.services.enqueue_alimtalk",
            return_value=True,
        ) as enqueue_alimtalk:
            response = self._send(request)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "grade_personalization_incomplete")
        enqueue_alimtalk.assert_not_called()
        self.assertFalse(ScheduledNotification.objects.exists())

    def test_grade_message_dispatches_exact_distinct_per_student_bodies(self):
        second_student = self._create_student(
            "S003",
            phone="01055556666",
            parent_phone="01077778888",
        )
        first_body = "시험 70/100\n교재 100/100\n메인자료 100/100"
        second_body = "시험 80/100\n교재 20/100\n메인자료 미제출"
        request = self.factory.post(
            "/api/v1/messaging/send/",
            data={
                "send_to": "parent",
                "student_ids": [self.student.id, second_student.id],
                "raw_body": "전역 첫 학생 본문은 사용하면 안 됩니다.",
                "block_category": "grades",
                "alimtalk_extra_vars": {
                    "강의명": "중2 과학",
                    "차시명": "2차시",
                    "시험성적": "80/100",
                },
                "alimtalk_extra_vars_per_student": {
                    str(self.student.id): {"_body_subst": first_body},
                    str(second_student.id): {"_body_subst": second_body},
                },
            },
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.user = self.admin
        request.tenant = self.tenant

        with (
            patch(
                "apps.domains.messaging.services.get_tenant_site_url",
                return_value="https://example.test",
            ),
            patch(
                "apps.domains.messaging.services.enqueue_alimtalk",
                return_value=True,
            ) as enqueue_alimtalk,
        ):
            response = self._send(request)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(enqueue_alimtalk.call_count, 2)
        sent_memos = []
        for call in enqueue_alimtalk.call_args_list:
            replacements = {
                item["key"]: item["value"]
                for item in call.kwargs["alimtalk_replacements"]
            }
            sent_memos.append(replacements["선생님메모"])
        self.assertCountEqual(sent_memos, [first_body, second_body])
        self.assertNotIn("전역 첫 학생 본문", "\n".join(sent_memos))

    def test_score_entry_category_overrides_reused_clinic_copy_envelope(self):
        clinic_copy = MessageTemplate.objects.create(
            tenant=self.tenant,
            category="clinic",
            name="클리닉에서 저장한 문구",
            subject="",
            body="이번 수업 결과를 안내드립니다.",
        )
        request = self.factory.post(
            "/api/v1/messaging/send/",
            data={
                "send_to": "parent",
                "student_ids": [self.student.id],
                "template_id": clinic_copy.id,
                "raw_body": "이번 수업 결과를 안내드립니다.",
                "block_category": "grades",
                "alimtalk_extra_vars": {
                    "강의명": "중2 수학",
                    "차시명": "1차시",
                },
                "alimtalk_extra_vars_per_student": {
                    str(self.student.id): {
                        "_body_subst": "이번 수업 결과를 안내드립니다.",
                    },
                },
            },
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.user = self.admin
        request.tenant = self.tenant

        with (
            patch("apps.domains.messaging.services.get_tenant_site_url", return_value="https://example.test"),
            patch("apps.domains.messaging.services.enqueue_alimtalk", return_value=True) as enqueue_alimtalk,
        ):
            response = self._send(request)

        self.assertEqual(response.status_code, 200, response.data)
        kwargs = enqueue_alimtalk.call_args.kwargs
        self.assertEqual(kwargs["template_id"], "KA01TP260406105458211774JKJ3OU55")
        replacements = {item["key"]: item["value"] for item in kwargs["alimtalk_replacements"]}
        self.assertEqual(replacements["강의명"], "중2 수학")
        self.assertEqual(replacements["차시명"], "1차시")
        self.assertNotIn("클리닉장소", replacements)
        self.assertNotIn("클리닉날짜", replacements)
        self.assertNotIn("클리닉시간", replacements)

    def test_manual_send_can_be_scheduled_without_immediate_enqueue(self):
        send_at = timezone.now() + timedelta(hours=1)
        request = self.factory.post(
            "/api/v1/messaging/send/",
            data={
                "send_to": "parent",
                "student_ids": [self.student.id],
                "raw_body": "예약 안내입니다.",
                "block_category": "attendance",
                "scheduled_send_at": send_at.isoformat(),
            },
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.user = self.admin
        request.tenant = self.tenant

        with (
            patch("apps.domains.messaging.services.get_tenant_site_url", return_value="https://example.test"),
            patch("apps.domains.messaging.services.enqueue_alimtalk", return_value=True) as enqueue_alimtalk,
        ):
            response = self._send(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["enqueued"], 0)
        self.assertEqual(response.data["scheduled"], 1)
        enqueue_alimtalk.assert_not_called()
        scheduled = ScheduledNotification.objects.get(tenant=self.tenant)
        self.assertEqual(scheduled.trigger, "manual_send")
        self.assertEqual(scheduled.status, ScheduledNotification.Status.PENDING)
        self.assertEqual(scheduled.payload["to"], "01033334444")
        self.assertEqual(scheduled.payload["target_type"], "parent")

    def test_future_scheduled_send_is_accepted_when_current_hour_is_full(self):
        provider_owner = Tenant.objects.create(
            code="msg-send-scheduled-provider",
            name="Scheduled Provider",
            is_active=True,
        )
        NotificationLog.objects.create(
            tenant=provider_owner,
            source_tenant=self.tenant,
            success=False,
            status="processing",
            message_mode="alimtalk",
        )
        request = self.factory.post(
            "/api/v1/messaging/send/",
            data={
                "send_to": "parent",
                "student_ids": [self.student.id],
                "raw_body": "한도 이후 예약 안내입니다.",
                "block_category": "attendance",
                "scheduled_send_at": (timezone.now() + timedelta(hours=2)).isoformat(),
            },
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.user = self.admin
        request.tenant = self.tenant

        with (
            patch("apps.domains.messaging.views.send_views.HOURLY_SEND_LIMIT", 1),
            patch("apps.domains.messaging.services.enqueue_alimtalk") as enqueue_alimtalk,
        ):
            response = self._send(request)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["scheduled"], 1)
        enqueue_alimtalk.assert_not_called()
        scheduled = ScheduledNotification.objects.get(tenant=self.tenant)
        self.assertEqual(scheduled.status, ScheduledNotification.Status.PENDING)

    def test_immediate_send_transient_enqueue_failure_is_durably_retried(self):
        request = self.factory.post(
            "/api/v1/messaging/send/",
            data={
                "send_to": "parent",
                "student_ids": [self.student.id],
                "raw_body": "즉시 발송 재시도 안내입니다.",
                "block_category": "attendance",
            },
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.user = self.admin
        request.tenant = self.tenant

        with (
            patch(
                "apps.domains.messaging.services.get_tenant_site_url",
                return_value="https://example.test",
            ),
            patch(
                "apps.domains.messaging.services.enqueue_alimtalk",
                return_value=False,
            ) as enqueue_alimtalk,
        ):
            response = self._send(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["enqueued"], 0)
        self.assertEqual(response.data["scheduled"], 1)
        self.assertEqual(response.data["enqueue_failed"], 0)
        enqueue_alimtalk.assert_called_once()
        dispatch = ScheduledNotification.objects.get(tenant=self.tenant)
        self.assertEqual(dispatch.status, ScheduledNotification.Status.PENDING)
        self.assertEqual(dispatch.attempt_count, 1)
        self.assertIsNotNone(dispatch.next_attempt_at)
        self.assertTrue(dispatch.payload["occurrence_key"].startswith("dispatch:"))

    def test_manual_send_rejects_past_scheduled_time(self):
        request = self.factory.post(
            "/api/v1/messaging/send/",
            data={
                "send_to": "student",
                "student_ids": [self.student.id],
                "raw_body": "과거 예약 안내입니다.",
                "block_category": "default",
                "scheduled_send_at": (timezone.now() - timedelta(minutes=1)).isoformat(),
            },
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.user = self.admin
        request.tenant = self.tenant

        with patch("apps.domains.messaging.services.enqueue_alimtalk", return_value=True) as enqueue_alimtalk:
            response = self._send(request)

        self.assertEqual(response.status_code, 400)
        self.assertIn("scheduled_send_at", response.data)
        enqueue_alimtalk.assert_not_called()
        self.assertFalse(ScheduledNotification.objects.exists())

    def test_payment_send_fail_closes_when_provider_sid_is_missing(self):
        template = MessageTemplate.objects.create(
            tenant=self.tenant,
            category=MessageTemplate.Category.PAYMENT,
            name="결제 완료 안내",
            subject="",
            body="결제 완료 안내입니다.",
            solapi_template_id="STALE-PAYMENT-SID",
            solapi_status="APPROVED",
        )
        request = self.factory.post(
            "/api/v1/messaging/send/",
            data={
                "send_to": "parent",
                "student_ids": [self.student.id],
                "template_id": template.id,
                "raw_body": "결제 완료 안내입니다.",
                "block_category": "payment",
            },
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.user = self.admin
        request.tenant = self.tenant

        with patch("apps.domains.messaging.services.enqueue_alimtalk") as enqueue_alimtalk:
            response = self._send(request)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "unified_template_unavailable")
        self.assertEqual(response.data["template_type"], "notice_payment")
        enqueue_alimtalk.assert_not_called()

    def test_student_direct_alimtalk_omits_deleted_and_cross_tenant_students(self):
        deleted_user = User.objects.create_user(
            username=user_internal_username(self.tenant, "S002"),
            password="test1234",
            tenant=self.tenant,
            phone="01022223333",
            name="삭제학생",
        )
        deleted_student = Student.objects.create(
            tenant=self.tenant,
            user=deleted_user,
            ps_number="S002",
            name="삭제학생",
            phone="01022223333",
            parent_phone="01044445555",
            omr_code="22223333",
            deleted_at=timezone.now(),
        )
        other_tenant = Tenant.objects.create(code="msg-send-other", name="Other", is_active=True)
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
            parent_phone="01077776666",
            omr_code="99998888",
        )
        request = self.factory.post(
            "/api/v1/messaging/send/",
            data={
                "send_to": "parent",
                "student_ids": [self.student.id, deleted_student.id, other_student.id, self.student.id],
                "raw_body": "선택 학생 안내입니다.",
                "block_category": "attendance",
            },
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.user = self.admin
        request.tenant = self.tenant

        with (
            patch("apps.domains.messaging.services.get_tenant_site_url", return_value="https://example.test"),
            patch("apps.domains.messaging.services.enqueue_alimtalk", return_value=True) as enqueue_alimtalk,
        ):
            response = self._send(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["enqueued"], 0)
        self.assertEqual(response.data["scheduled"], 1)
        enqueue_alimtalk.assert_called_once()
        kwargs = enqueue_alimtalk.call_args.kwargs
        self.assertEqual(kwargs["target_id"], self.student.id)
        self.assertEqual(kwargs["to"], "01033334444")

    def test_direct_raw_body_requires_entry_category(self):
        request = self.factory.post(
            "/api/v1/messaging/send/",
            data={
                "send_to": "student",
                "student_ids": [self.student.id],
                "raw_body": "진입점 없이 직접 보내는 안내입니다.",
            },
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.user = self.admin
        request.tenant = self.tenant

        with patch("apps.domains.messaging.services.enqueue_alimtalk", return_value=True) as enqueue_alimtalk:
            response = self._send(request)

        self.assertEqual(response.status_code, 400)
        self.assertIn("block_category", response.data)
        enqueue_alimtalk.assert_not_called()

    def test_staff_target_manual_send_is_disabled(self):
        request = self.factory.post(
            "/api/v1/messaging/send/",
            data={
                "send_to": "staff",
                "staff_ids": [1],
                "raw_body": "직원 대상 안내입니다.",
                "block_category": "default",
            },
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.user = self.admin
        request.tenant = self.tenant

        with patch("apps.domains.messaging.services.enqueue_alimtalk", return_value=True) as enqueue_alimtalk:
            response = self._send(request)

        self.assertEqual(response.status_code, 400)
        self.assertIn("send_to", response.data)
        enqueue_alimtalk.assert_not_called()

    def test_staff_membership_cannot_send_manual_messages(self):
        staff_user = User.objects.create_user(
            username="msg-send-staff",
            password="test1234",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=staff_user, role="staff")
        request = self.factory.post(
            "/api/v1/messaging/send/",
            data={
                "send_to": "student",
                "student_ids": [self.student.id],
                "raw_body": "직원이 직접 발송하는 안내입니다.",
                "block_category": "default",
            },
            format="json",
        )
        force_authenticate(request, user=staff_user)
        request.user = staff_user
        request.tenant = self.tenant

        with patch("apps.domains.messaging.services.enqueue_alimtalk", return_value=True) as enqueue_alimtalk:
            response = self._send(request)

        self.assertEqual(response.status_code, 403)
        enqueue_alimtalk.assert_not_called()

    def test_student_template_category_is_saved_as_default(self):
        request = self.factory.post(
            "/api/v1/messaging/templates/",
            data={
                "category": "student",
                "name": "학생 선택 안내",
                "subject": "",
                "body": "#{학생이름} 안내입니다.",
            },
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.user = self.admin
        request.tenant = self.tenant

        response = MessageTemplateListCreateView.as_view()(request)

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["category"], "default")
        saved = MessageTemplate.objects.get(id=response.data["id"])
        self.assertEqual(saved.category, MessageTemplate.Category.DEFAULT)
