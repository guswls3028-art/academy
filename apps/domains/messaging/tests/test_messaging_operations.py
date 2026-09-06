from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.core.models import Tenant, TenantMembership, WorkerHeartbeatModel
from apps.core.models.user import user_internal_username
from apps.domains.messaging.models import AutoSendConfig, MessageTemplate, NotificationLog, ScheduledNotification
from apps.domains.messaging.views.operations_views import MessagingOperationsStatusView, SendMessagePreflightView


User = get_user_model()
Student = apps.get_model("students", "Student")


class MessagingOperationsBase(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.tenant = Tenant.objects.create(
            code="msg-ops",
            name="Msg Ops",
            is_active=True,
            messaging_sender="01012345678",
        )
        self.owner = User.objects.create_user(
            username="msg-ops-owner",
            password="test1234",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=self.owner, role="owner")

    def _request(self, method: str, path: str, data=None):
        request = getattr(self.factory, method)(path, data=data or {}, format="json")
        force_authenticate(request, user=self.owner)
        request.user = self.owner
        request.tenant = self.tenant
        return request

    def _student(self, suffix: str, phone: str = "01011112222", parent_phone: str = "01033334444"):
        user = User.objects.create_user(
            username=user_internal_username(self.tenant, suffix),
            password="test1234",
            tenant=self.tenant,
            phone=phone,
            name=f"학생{suffix}",
        )
        student = Student.objects.create(
            tenant=self.tenant,
            user=user,
            ps_number=suffix,
            name=f"학생{suffix}",
            phone=phone,
            parent_phone=parent_phone,
            omr_code=f"99{suffix}",
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=user, role="student")
        return student


class SendMessagePreflightViewTests(MessagingOperationsBase):
    def test_preflight_allows_complete_grade_messages_to_students(self):
        student = self._student("011")

        response = SendMessagePreflightView.as_view()(
            self._request(
                "post",
                "/api/v1/messaging/send/preflight/",
                {
                    "send_to": "student",
                    "student_ids": [student.id],
                    "raw_body": "성적표 안내입니다.",
                    "block_category": "grades",
                    "alimtalk_extra_vars": {
                        "강의명": "중2 수학",
                        "차시명": "1차시",
                    },
                    "alimtalk_extra_vars_per_student": {
                        str(student.id): {
                            "_body_subst": "단원평가 85/100",
                        },
                    },
                },
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["ok"], response.data)
        self.assertEqual(response.data["send_to"], "student")
        self.assertEqual(response.data["recipient"]["valid_phone"], 1)

    def test_preflight_blocks_incomplete_grade_personalization(self):
        first_student = self._student("012", parent_phone="01033334444")
        second_student = self._student("013", parent_phone="01055556666")

        response = SendMessagePreflightView.as_view()(
            self._request(
                "post",
                "/api/v1/messaging/send/preflight/",
                {
                    "send_to": "parent",
                    "student_ids": [first_student.id, second_student.id],
                    "raw_body": "전역 본문",
                    "block_category": "grades",
                    "alimtalk_extra_vars": {
                        "강의명": "중2 과학",
                        "차시명": "2차시",
                    },
                    "alimtalk_extra_vars_per_student": {
                        str(first_student.id): {
                            "_body_subst": "시험 70/100\n교재 100/100\n메인자료 100/100",
                        },
                    },
                },
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["ok"])
        self.assertTrue(any(
            item["code"] == "grade_personalization_incomplete"
            for item in response.data["blockers"]
        ))

    def test_preflight_reports_recipient_template_and_phone_health(self):
        ok_student = self._student("001", parent_phone="01033334444")
        no_phone_student = self._student("002", parent_phone="")
        deleted_student = self._student("003", parent_phone="01055556666")
        deleted_student.deleted_at = timezone.now()
        deleted_student.save(update_fields=["deleted_at"])
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

        response = SendMessagePreflightView.as_view()(
            self._request(
                "post",
                "/api/v1/messaging/send/preflight/",
                {
                    "send_to": "parent",
                    "student_ids": [ok_student.id, no_phone_student.id, deleted_student.id, ok_student.id],
                    "raw_body": "성적표 안내입니다.",
                    "block_category": "attendance",
                },
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["ok"])
        self.assertEqual(response.data["recipient"]["selected"], 3)
        self.assertEqual(response.data["recipient"]["resolved"], 2)
        self.assertEqual(response.data["recipient"]["valid_phone"], 1)
        self.assertEqual(response.data["recipient"]["skipped_no_phone"], 1)
        self.assertEqual(response.data["recipient"]["invalid_or_deleted"], 1)
        self.assertEqual(response.data["template"]["source"], "unified")
        self.assertEqual(response.data["template"]["solapi_status"], "APPROVED")
        self.assertEqual(response.data["template"]["template_type"], "attendance")
        preview_rows = response.data["preview_recipients"]
        self.assertEqual(
            [row["student_id"] for row in preview_rows],
            [ok_student.id, no_phone_student.id],
        )
        self.assertFalse(preview_rows[0]["excluded"])
        self.assertTrue(preview_rows[1]["excluded"])
        self.assertEqual(preview_rows[1]["exclude_reason"], "전화번호 없음")
        self.assertIn("Msg Ops입니다.", preview_rows[0]["full_message_body"])
        self.assertIn("학생001학생님.", preview_rows[0]["full_message_body"])
        self.assertIn("성적표 안내입니다.", preview_rows[0]["full_message_body"])
        self.assertNotIn("학원플러스", preview_rows[0]["full_message_body"])
        self.assertTrue(any(item["code"] == "missing_phone" for item in response.data["warnings"]))

    def test_preflight_blocks_without_approved_template(self):
        student = self._student("004")

        response = SendMessagePreflightView.as_view()(
            self._request(
                "post",
                "/api/v1/messaging/send/preflight/",
                {
                    "send_to": "parent",
                    "student_ids": [student.id],
                    "raw_body": "검수 전 본문입니다.",
                    "block_category": "default",
                },
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["ok"])
        self.assertTrue(any(item["code"] == "template_not_ready" for item in response.data["blockers"]))

    def test_preflight_keeps_score_envelope_when_reusing_clinic_copy(self):
        student = self._student("010")
        clinic_copy = MessageTemplate.objects.create(
            tenant=self.tenant,
            category="clinic",
            name="클리닉에서 저장한 문구",
            subject="",
            body="이번 수업 결과를 안내드립니다.",
        )

        response = SendMessagePreflightView.as_view()(
            self._request(
                "post",
                "/api/v1/messaging/send/preflight/",
                {
                    "send_to": "parent",
                    "student_ids": [student.id],
                    "template_id": clinic_copy.id,
                    "raw_body": "이번 수업 결과를 안내드립니다.",
                    "block_category": "grades",
                    "alimtalk_extra_vars": {
                        "강의명": "중2 수학",
                        "차시명": "1차시",
                    },
                    "alimtalk_extra_vars_per_student": {
                        str(student.id): {
                            "_body_subst": "이번 수업 결과를 안내드립니다.",
                        },
                    },
                },
            )
        )

        self.assertTrue(response.data["ok"], response.data)
        self.assertEqual(response.data["template"]["template_type"], "score")
        preview = response.data["preview_recipients"][0]["full_message_body"]
        self.assertIn("성적표 안내", preview)
        self.assertIn("중2 수학", preview)
        self.assertIn("1차시", preview)
        self.assertNotIn("장소\n-", preview)

    def test_preflight_fail_closes_payment_when_provider_sid_is_missing(self):
        student = self._student("006")
        template = MessageTemplate.objects.create(
            tenant=self.tenant,
            category=MessageTemplate.Category.PAYMENT,
            name="결제 완료 안내",
            subject="",
            body="결제 완료 안내입니다.",
            solapi_template_id="STALE-PAYMENT-SID",
            solapi_status="APPROVED",
        )

        response = SendMessagePreflightView.as_view()(
            self._request(
                "post",
                "/api/v1/messaging/send/preflight/",
                {
                    "send_to": "parent",
                    "student_ids": [student.id],
                    "template_id": template.id,
                    "raw_body": "결제 완료 안내입니다.",
                    "block_category": "payment",
                },
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["ok"])
        self.assertEqual(response.data["template"]["source"], "unified_missing")
        self.assertEqual(response.data["template"]["solapi_template_id"], "")

    def test_preflight_quota_uses_source_business_tenant_only(self):
        student = self._student("005")
        provider_owner = Tenant.objects.create(code="msg-ops-provider", name="Provider", is_active=True)
        other_customer = Tenant.objects.create(code="msg-ops-other", name="Other", is_active=True)
        NotificationLog.objects.create(
            tenant=provider_owner,
            source_tenant=self.tenant,
            success=False,
            status="processing",
            message_mode="alimtalk",
        )
        NotificationLog.objects.create(
            tenant=provider_owner,
            source_tenant=other_customer,
            success=True,
            status="sent",
            message_mode="alimtalk",
        )
        NotificationLog.objects.create(
            tenant=provider_owner,
            success=True,
            status="sent",
            message_mode="alimtalk",
        )

        with patch("apps.domains.messaging.services.preflight.HOURLY_SEND_LIMIT", 2):
            response = SendMessagePreflightView.as_view()(
                self._request(
                    "post",
                    "/api/v1/messaging/send/preflight/",
                    {
                        "send_to": "parent",
                        "student_ids": [student.id],
                        "raw_body": "쿼터 확인 안내입니다.",
                        "block_category": "attendance",
                    },
                )
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["ok"])
        self.assertEqual(response.data["limits"]["sent_last_hour"], 1)
        self.assertEqual(response.data["limits"]["remaining_this_hour"], 1)

    def test_future_reservation_is_not_blocked_by_current_hour_quota(self):
        student = self._student("007")
        with patch("apps.domains.messaging.services.preflight.HOURLY_SEND_LIMIT", 0):
            response = SendMessagePreflightView.as_view()(
                self._request(
                    "post",
                    "/api/v1/messaging/send/preflight/",
                    {
                        "send_to": "parent",
                        "student_ids": [student.id],
                        "raw_body": "예약 안내입니다.",
                        "block_category": "attendance",
                        "scheduled_send_at": (
                            timezone.now() + timedelta(hours=2)
                        ).isoformat(),
                    },
                )
            )

        self.assertTrue(response.data["ok"])
        self.assertFalse(any(
            item["code"] == "hourly_limit" for item in response.data["blockers"]
        ))

    def test_immediate_preflight_blocks_when_provider_daily_limit_is_full(self):
        student = self._student("009")
        with (
            patch(
                "apps.domains.messaging.services.preflight.get_provider_daily_dispatch_limit",
                return_value=1,
            ),
            patch(
                "apps.domains.messaging.services.preflight.get_provider_daily_notification_usage",
                return_value=1,
            ),
        ):
            response = SendMessagePreflightView.as_view()(
                self._request(
                    "post",
                    "/api/v1/messaging/send/preflight/",
                    {
                        "send_to": "parent",
                        "student_ids": [student.id],
                        "raw_body": "일일 한도 확인",
                        "block_category": "attendance",
                    },
                )
            )

        self.assertFalse(response.data["ok"])
        self.assertEqual(response.data["limits"]["provider_daily_remaining"], 0)
        self.assertTrue(any(
            item["code"] == "provider_daily_limit"
            for item in response.data["blockers"]
        ))

    def test_operationally_disabled_tenant_is_a_preflight_blocker(self):
        student = self._student("008")
        with (
            patch(
                "apps.domains.messaging.services.preflight.is_messaging_disabled",
                return_value=True,
            ),
            patch(
                "apps.domains.messaging.services.preflight.get_messaging_disabled_reason",
                return_value="운영 중지",
            ),
        ):
            response = SendMessagePreflightView.as_view()(
                self._request(
                    "post",
                    "/api/v1/messaging/send/preflight/",
                    {
                        "send_to": "parent",
                        "student_ids": [student.id],
                        "raw_body": "중지 상태 안내",
                        "block_category": "attendance",
                    },
                )
            )

        self.assertFalse(response.data["ok"])
        self.assertTrue(any(
            item["code"] == "messaging_disabled"
            for item in response.data["blockers"]
        ))


class MessagingOperationsStatusViewTests(MessagingOperationsBase):
    def test_operations_status_summarizes_worker_queue_logs_and_auto_send_risks(self):
        now = timezone.now()
        WorkerHeartbeatModel.objects.create(
            name="messaging",
            instance="i-stale",
            last_seen_at=now - timedelta(minutes=10),
            version="sha-old",
        )
        ScheduledNotification.objects.create(
            tenant=self.tenant,
            trigger="manual_send",
            send_at=now - timedelta(minutes=5),
            status=ScheduledNotification.Status.PENDING,
            payload={"to": "01011112222", "text": "예약"},
        )
        ScheduledNotification.objects.create(
            tenant=self.tenant,
            trigger="manual_send",
            send_at=now - timedelta(minutes=1),
            status=ScheduledNotification.Status.FAILED,
            payload={"to": "01011112222", "text": "예약"},
        )
        NotificationLog.objects.create(
            tenant=self.tenant,
            success=True,
            status="sent",
            recipient_summary="학생",
            message_mode="alimtalk",
        )
        NotificationLog.objects.create(
            tenant=self.tenant,
            success=False,
            status="failed",
            recipient_summary="학생",
            failure_reason="provider error",
            message_mode="alimtalk",
        )
        AutoSendConfig.objects.create(
            tenant=self.tenant,
            trigger="clinic_reminder",
            enabled=True,
            message_mode="alimtalk",
        )

        response = MessagingOperationsStatusView.as_view()(
            self._request("get", "/api/v1/messaging/operations/status/")
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["worker"]["status"], "stale")
        self.assertEqual(response.data["scheduled"]["pending"], 1)
        self.assertEqual(response.data["scheduled"]["overdue"], 1)
        self.assertEqual(response.data["scheduled"]["failed_24h"], 1)
        self.assertEqual(response.data["log_24h"]["sent"], 1)
        self.assertEqual(response.data["log_24h"]["failed"], 1)
        self.assertEqual(
            response.data["rate_limit_provider_daily"]["timezone"],
            "Asia/Seoul",
        )
        self.assertEqual(response.data["auto_send"]["enabled_without_template"], 1)
        self.assertTrue(any(item["code"] == "scheduled_overdue" for item in response.data["risks"]))

    def test_operations_status_marks_stale_heartbeat_idle_when_no_backlog(self):
        WorkerHeartbeatModel.objects.create(
            name="messaging",
            instance="i-old",
            last_seen_at=timezone.now() - timedelta(minutes=10),
            version="sha-old",
        )

        response = MessagingOperationsStatusView.as_view()(
            self._request("get", "/api/v1/messaging/operations/status/")
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["worker"]["status"], "idle")
        self.assertEqual(response.data["worker"]["idle_reason"], "scale_to_zero_no_backlog")
        self.assertFalse(any(item["code"] == "worker_attention" for item in response.data["risks"]))

    def test_operations_status_uses_business_tenant_for_logs_and_hourly_quota(self):
        provider_owner = Tenant.objects.create(code="msg-status-provider", name="Provider", is_active=True)
        other_customer = Tenant.objects.create(code="msg-status-other", name="Other", is_active=True)
        NotificationLog.objects.create(
            tenant=provider_owner,
            source_tenant=self.tenant,
            success=True,
            status="sent",
            message_mode="alimtalk",
        )
        NotificationLog.objects.create(
            tenant=provider_owner,
            source_tenant=other_customer,
            success=False,
            status="failed",
            message_mode="alimtalk",
        )
        NotificationLog.objects.create(
            tenant=provider_owner,
            success=False,
            status="failed",
            message_mode="alimtalk",
        )

        response = MessagingOperationsStatusView.as_view()(
            self._request("get", "/api/v1/messaging/operations/status/")
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["log_24h"], {
            "sent": 1,
            "failed": 0,
            "processing": 0,
            "sending": 0,
            "retryable_failed": 0,
            "ambiguous": 0,
            "action_required": 0,
            "total": 1,
        })
        self.assertEqual(response.data["rate_limit_hourly"], {
            "limit": 500,
            "used": 1,
            "remaining": 499,
        })

    def test_operations_status_counts_sending_and_ambiguous_as_action_required(self):
        NotificationLog.objects.create(
            tenant=self.tenant,
            success=False,
            status="sending",
            message_mode="alimtalk",
        )
        NotificationLog.objects.create(
            tenant=self.tenant,
            success=False,
            status="ambiguous",
            message_mode="alimtalk",
        )

        response = MessagingOperationsStatusView.as_view()(
            self._request("get", "/api/v1/messaging/operations/status/")
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["log_24h"]["sending"], 1)
        self.assertEqual(response.data["log_24h"]["ambiguous"], 1)
        self.assertEqual(response.data["log_24h"]["action_required"], 2)
        risk = next(
            item
            for item in response.data["risks"]
            if item["code"] == "provider_outcome_ambiguous"
        )
        self.assertIn("sending 1, ambiguous 1", risk["detail"])

    def test_operations_status_keeps_old_unresolved_provider_outcome_actionable(self):
        log = NotificationLog.objects.create(
            tenant=self.tenant,
            success=False,
            status="ambiguous",
            message_mode="alimtalk",
        )
        NotificationLog.objects.filter(pk=log.pk).update(
            sent_at=timezone.now() - timedelta(days=3)
        )

        response = MessagingOperationsStatusView.as_view()(
            self._request("get", "/api/v1/messaging/operations/status/")
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["log_24h"]["action_required"], 0)
        self.assertEqual(response.data["unresolved"]["action_required"], 1)
        self.assertEqual(response.data["unresolved"]["ambiguous"], 1)
        self.assertEqual(
            response.data["unresolved"]["age_buckets"],
            {"under_1h": 0, "from_1h_to_24h": 0, "over_24h": 1},
        )
        self.assertTrue(
            any(
                item["code"] == "provider_outcome_ambiguous"
                for item in response.data["risks"]
            )
        )

    def test_operations_status_flags_mapped_trigger_when_provider_sid_is_missing(self):
        AutoSendConfig.objects.create(
            tenant=self.tenant,
            trigger="payment_complete",
            enabled=True,
            message_mode="alimtalk",
        )

        response = MessagingOperationsStatusView.as_view()(
            self._request("get", "/api/v1/messaging/operations/status/")
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["auto_send"]["enabled_without_template"], 1)
        self.assertIn(
            {
                "trigger": "payment_complete",
                "code": "provider_template_missing",
                "detail": (
                    "매핑된 카카오 승인 봉투 SID가 공급사에 없습니다. "
                    "이 트리거는 fail-closed 상태입니다."
                ),
            },
            response.data["auto_send"]["issues"],
        )
        self.assertTrue(any(item["code"] == "auto_send_template_attention" for item in response.data["risks"]))
