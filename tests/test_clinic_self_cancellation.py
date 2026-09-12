import datetime
import json
import logging
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.db import close_old_connections, connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.core.models import Tenant, TenantMembership
from apps.domains.clinic.models import SessionParticipant, SessionParticipantPlanItem
from apps.domains.clinic.services import change_participant_status
from apps.domains.clinic.services.lifecycle import Conflict
from apps.domains.clinic.tests import ClinicAPITestMixin, ClinicTestMixin
from apps.domains.exams.models import Exam
from apps.domains.messaging.alimtalk_content_builders import SOLAPI_CLINIC_CHANGE
from apps.domains.messaging.models import (
    AlimtalkChannelBinding,
    AlimtalkTemplateBinding,
    AutoSendConfig,
    MessageTemplate,
    NotificationLog,
    ScheduledNotification,
)
from apps.domains.messaging.security import verify_tenant_binding_signature
from apps.domains.parents.models import Parent
from apps.domains.progress.models import SessionProgress


class ClinicSelfCancellationAPITest(APITestCase, ClinicAPITestMixin):
    def setUp(self):
        self.data = self.setup_api_tenant("clinic_self_cancel", student_count=1)
        self.tenant = self.data["tenant"]
        self.student = self.data["students"][0]
        self.enrollment = self.data["enrollments"][0]
        today = timezone.localdate()
        self.week_start = today + datetime.timedelta(days=7 - today.weekday())

    def _session(self, day_offset: int, hour: int):
        return self.make_clinic_session(
            self.tenant,
            date=self.week_start + datetime.timedelta(days=day_offset),
            start_time=datetime.time(hour, 0),
            location=f"{day_offset}-{hour}호",
        )

    def _current_required_link(self):
        exam = Exam.objects.create(
            tenant=self.tenant,
            title="현재 필수 클리닉 시험",
            exam_type=Exam.ExamType.REGULAR,
            is_active=True,
        )
        exam.sessions.add(self.data["lec_session"])
        return self.make_clinic_link(
            self.enrollment,
            self.data["lec_session"],
            source_type="exam",
            source_id=exam.id,
        )

    def _cancel(self, participant):
        self.client.force_authenticate(user=self.student.user)
        return self.client.patch(
            f"/api/v1/clinic/participants/{participant.id}/set_status/",
            {"status": "cancelled", "send_to": "parent"},
            format="json",
            **self._headers(self.tenant),
        )

    def _configure_cancel_notifications(self):
        template = MessageTemplate.objects.create(
            tenant=self.tenant,
            category=MessageTemplate.Category.CLINIC,
            name="클리닉 취소",
            body="클리닉 예약이 취소되었습니다.",
        )
        AutoSendConfig.objects.create(
            tenant=self.tenant,
            trigger=AutoSendConfig.Trigger.CLINIC_CANCELLED,
            template=template,
            enabled=True,
            message_mode="alimtalk",
            delay_mode="immediate",
        )
        self.student.phone = "01011112222"
        self.student.parent_phone = "01033334444"
        self.student.save(update_fields=["phone", "parent_phone"])

    def test_required_student_cannot_cancel_only_active_booking_without_side_effects(self):
        link = self._current_required_link()
        participant = self.make_participant(
            self.tenant,
            self._session(1, 14),
            self.student,
            enrollment=self.enrollment,
            status=SessionParticipant.Status.BOOKED,
        )
        plan = SessionParticipantPlanItem.objects.create(
            participant=participant,
            clinic_link=link,
            selected_by=self.data["admin_user"],
        )
        outbox_before = ScheduledNotification.objects.count()

        with patch(
            "apps.domains.clinic.services.lifecycle.cancel_pending_clinic_participant_reminders"
        ) as cancel_reminders, patch(
            "apps.domains.clinic.views.participant_views._send_clinic_notification"
        ) as send_notification:
            response = self._cancel(participant)

        self.assertEqual(response.status_code, 409, response.data)
        self.assertIn("같은 주", response.data["detail"])
        participant.refresh_from_db()
        plan.refresh_from_db()
        self.assertEqual(participant.status, SessionParticipant.Status.BOOKED)
        self.assertIsNone(plan.removed_at)
        self.assertEqual(ScheduledNotification.objects.count(), outbox_before)
        cancel_reminders.assert_not_called()
        send_notification.assert_not_called()

    def test_required_student_can_cancel_one_of_two_same_week_bookings_and_forces_both_targets(self):
        self._current_required_link()
        first = self.make_participant(
            self.tenant,
            self._session(1, 14),
            self.student,
            enrollment=self.enrollment,
            status=SessionParticipant.Status.BOOKED,
        )
        second = self.make_participant(
            self.tenant,
            self._session(3, 18),
            self.student,
            enrollment=self.enrollment,
            status=SessionParticipant.Status.PENDING,
        )
        notification = {
            "requested": 2,
            "failed": 0,
            "send_to": "both",
            "targets": [
                {"target": "parent", "requested": True},
                {"target": "student", "requested": True},
            ],
        }
        decision_time = timezone.make_aware(
            datetime.datetime.combine(self.week_start, datetime.time(12, 0))
        )

        with patch(
            "apps.domains.clinic.services.lifecycle.send_clinic_event_notification",
            return_value=True,
        ) as send_notification, patch(
            "apps.domains.clinic.services.lifecycle.timezone.now",
            return_value=decision_time,
        ):
            response = self._cancel(first)

        self.assertEqual(response.status_code, 200, response.data)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, SessionParticipant.Status.CANCELLED)
        self.assertEqual(second.status, SessionParticipant.Status.PENDING)
        self.assertEqual(response.data["notification"], notification)
        self.assertEqual(
            [call.kwargs["send_to"] for call in send_notification.call_args_list],
            ["parent", "student"],
        )

    def test_required_student_other_week_booking_does_not_satisfy_minimum(self):
        self._current_required_link()
        current = self.make_participant(
            self.tenant,
            self._session(2, 15),
            self.student,
            status=SessionParticipant.Status.BOOKED,
        )
        self.make_participant(
            self.tenant,
            self.make_clinic_session(
                self.tenant,
                date=self.week_start + datetime.timedelta(days=8),
                start_time=datetime.time(15, 0),
                location="다음주",
            ),
            self.student,
            status=SessionParticipant.Status.BOOKED,
        )

        response = self._cancel(current)

        self.assertEqual(response.status_code, 409, response.data)
        current.refresh_from_db()
        self.assertEqual(current.status, SessionParticipant.Status.BOOKED)

    def test_required_student_ended_same_week_booking_does_not_satisfy_minimum(self):
        self._current_required_link()
        current = self.make_participant(
            self.tenant,
            self._session(3, 15),
            self.student,
            status=SessionParticipant.Status.BOOKED,
        )
        ended = self.make_participant(
            self.tenant,
            self._session(1, 9),
            self.student,
            status=SessionParticipant.Status.BOOKED,
        )
        decision_time = timezone.make_aware(datetime.datetime.combine(
            self.week_start + datetime.timedelta(days=2),
            datetime.time(12, 0),
        ))

        with patch("apps.domains.clinic.services.lifecycle.timezone.now", return_value=decision_time):
            response = self._cancel(current)

        self.assertEqual(response.status_code, 409, response.data)
        current.refresh_from_db()
        ended.refresh_from_db()
        self.assertEqual(current.status, SessionParticipant.Status.BOOKED)
        self.assertEqual(ended.status, SessionParticipant.Status.BOOKED)

    def test_required_student_checked_out_same_week_booking_does_not_satisfy_minimum(self):
        self._current_required_link()
        current = self.make_participant(
            self.tenant,
            self._session(3, 15),
            self.student,
            status=SessionParticipant.Status.BOOKED,
        )
        inactive = self.make_participant(
            self.tenant,
            self._session(4, 17),
            self.student,
            status=SessionParticipant.Status.BOOKED,
        )
        inactive.checked_out_at = timezone.now()
        inactive.save(update_fields=["checked_out_at"])

        response = self._cancel(current)

        self.assertEqual(response.status_code, 409, response.data)
        current.refresh_from_db()
        self.assertEqual(current.status, SessionParticipant.Status.BOOKED)

    def test_non_required_student_can_cancel_final_confirmed_booking(self):
        participant = self.make_participant(
            self.tenant,
            self._session(4, 16),
            self.student,
            status=SessionParticipant.Status.BOOKED,
        )
        with patch(
            "apps.domains.clinic.services.lifecycle.send_clinic_event_notification",
            return_value=True,
        ):
            response = self._cancel(participant)

        self.assertEqual(response.status_code, 200, response.data)
        participant.refresh_from_db()
        self.assertEqual(participant.status, SessionParticipant.Status.CANCELLED)

    def test_parent_can_cancel_selected_child_booking_and_cannot_reduce_recipients(self):
        participant = self.make_participant(
            self.tenant,
            self._session(4, 19),
            self.student,
            status=SessionParticipant.Status.BOOKED,
        )
        parent_user = self.make_user("clinic_self_cancel_parent")
        parent_user.tenant = self.tenant
        parent_user.save(update_fields=["tenant"])
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=parent_user,
            role="parent",
        )
        parent = Parent.objects.create(
            tenant=self.tenant,
            user=parent_user,
            name="학부모",
            phone="01012345678",
        )
        self.student.parent = parent
        self.student.save(update_fields=["parent"])
        self.client.force_authenticate(user=parent_user)

        with patch(
            "apps.domains.clinic.services.lifecycle.send_clinic_event_notification",
            return_value=True,
        ) as send_notification:
            response = self.client.patch(
                f"/api/v1/clinic/participants/{participant.id}/set_status/",
                {"status": "cancelled", "send_to": "parent"},
                format="json",
                HTTP_X_STUDENT_ID=str(self.student.id),
                **self._headers(self.tenant),
            )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            [call.kwargs["send_to"] for call in send_notification.call_args_list],
            ["parent", "student"],
        )

    @override_settings(OWNER_TENANT_ID=0)
    def test_self_cancel_persists_both_target_outboxes_and_retry_is_idempotent(self):
        self._configure_cancel_notifications()
        participant = self.make_participant(
            self.tenant,
            self._session(4, 19),
            self.student,
            status=SessionParticipant.Status.BOOKED,
        )

        with override_settings(OWNER_TENANT_ID=self.tenant.id):
            response = self._cancel(participant)

        self.assertEqual(response.status_code, 200, response.data)
        participant.refresh_from_db()
        self.assertEqual(participant.status, SessionParticipant.Status.CANCELLED)
        outboxes = list(ScheduledNotification.objects.order_by("id"))
        self.assertEqual(len(outboxes), 2)
        self.assertEqual({row.trigger for row in outboxes}, {"clinic_cancelled"})
        self.assertCountEqual(
            [row.payload["target_type"] for row in outboxes],
            ["parent", "student"],
        )
        self.assertTrue(all(row.payload["message_mode"] == "alimtalk" for row in outboxes))
        self.assertEqual(response.data["notification"]["requested"], 2)
        self.assertEqual(response.data["notification"]["failed"], 0)

        with override_settings(OWNER_TENANT_ID=self.tenant.id):
            retry = self._cancel(participant)

        self.assertEqual(retry.status_code, 200, retry.data)
        self.assertEqual(ScheduledNotification.objects.count(), 2)
        self.assertEqual(retry.data["status"], SessionParticipant.Status.CANCELLED)

    def test_self_cancel_rolls_back_when_both_durable_targets_cannot_be_persisted(self):
        from apps.domains.messaging import scheduled

        self._configure_cancel_notifications()
        participant = self.make_participant(
            self.tenant,
            self._session(4, 19),
            self.student,
            status=SessionParticipant.Status.BOOKED,
        )
        original_create = scheduled._create_scheduled_notification_unlocked
        attempts = 0

        def fail_second_outbox(**kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                raise RuntimeError("synthetic second outbox failure")
            return original_create(**kwargs)

        with override_settings(OWNER_TENANT_ID=self.tenant.id), patch(
            "apps.domains.messaging.scheduled._create_scheduled_notification_unlocked",
            side_effect=fail_second_outbox,
        ):
            response = self._cancel(participant)

        self.assertEqual(response.status_code, 503, response.data)
        participant.refresh_from_db()
        self.assertEqual(participant.status, SessionParticipant.Status.BOOKED)
        self.assertEqual(ScheduledNotification.objects.count(), 0)

    def test_self_cancel_queue_failure_keeps_durable_target_pending_for_automatic_retry(self):
        self._configure_cancel_notifications()
        participant = self.make_participant(
            self.tenant,
            self._session(4, 19),
            self.student,
            status=SessionParticipant.Status.BOOKED,
        )

        with override_settings(OWNER_TENANT_ID=self.tenant.id), patch(
            "apps.domains.messaging.services.enqueue_alimtalk",
            side_effect=(True, False),
        ) as enqueue:
            with self.captureOnCommitCallbacks(execute=True):
                response = self._cancel(participant)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["notification"]["requested"], 2)
        self.assertEqual(enqueue.call_count, 2)
        outboxes = list(ScheduledNotification.objects.order_by("id"))
        self.assertEqual(
            [row.status for row in outboxes],
            [ScheduledNotification.Status.SENT, ScheduledNotification.Status.PENDING],
        )
        self.assertIsNotNone(outboxes[1].next_attempt_at)
        self.assertEqual(outboxes[1].attempt_count, 1)

    def test_self_cancel_notification_reports_each_alimtalk_target_without_fallback(self):
        from apps.domains.clinic.views.participant_views import _send_clinic_notification

        with patch(
            "apps.domains.clinic.views.participant_views.send_clinic_event_notification",
            side_effect=(True, False),
        ) as send_event:
            result = _send_clinic_notification(
                self.tenant,
                self.student,
                "clinic_cancelled",
                {"_domain_object_id": "clinic_participant:1:clinic_cancelled:1"},
                send_to="both",
                include_target_results=True,
            )

        self.assertEqual(
            [call.kwargs["send_to"] for call in send_event.call_args_list],
            ["parent", "student"],
        )
        self.assertTrue(all(
            call.kwargs["trigger"] == "clinic_cancelled"
            for call in send_event.call_args_list
        ))
        self.assertEqual(result["requested"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["targets"], [
            {"target": "parent", "requested": True},
            {"target": "student", "requested": False},
        ])

    def test_limglish_cancel_flows_through_verified_route_to_mock_provider(self):
        from apps.worker.messaging_worker import sqs_main
        from apps.worker.messaging_worker.sqs_main import (
            _allowed_common_template_ids,
            _resolve_tenant_delivery_context,
            _worker_tenant_binding_error,
        )

        self.tenant.code = "limglish"
        self.tenant.name = "Limglish"
        self.tenant.save(update_fields=["code", "name"])
        self.student.phone = "01011112222"
        self.student.parent_phone = "01033334444"
        self.student.save(update_fields=["phone", "parent_phone"])
        owner = Tenant.objects.create(code="alimtalk-owner", name="Alimtalk Owner")
        self._configure_cancel_notifications()
        participant = self.make_participant(
            self.tenant,
            self._session(4, 19),
            self.student,
            status=SessionParticipant.Status.BOOKED,
        )

        now = timezone.now()
        channel = AlimtalkChannelBinding.objects.create(
            tenant=self.tenant,
            channel_id="VERIFIED-LIMGLISH-CHANNEL",
            status=AlimtalkChannelBinding.Status.ACTIVE,
            verified_at=now,
            last_synced_at=now,
        )
        mapping = AlimtalkTemplateBinding.objects.create(
            channel=channel,
            source_template_id=SOLAPI_CLINIC_CHANGE,
            channel_template_id="APPROVED-LIMGLISH-CLINIC-CHANGE",
            status="APPROVED",
            source_fingerprint="same-clinic-change-fingerprint",
            channel_fingerprint="same-clinic-change-fingerprint",
            last_synced_at=now,
        )
        fake_queue = MagicMock()
        fake_queue.send_message.return_value = True
        existing_django_logger = logging.getLogger(
            "apps.domains.student_app.media.views"
        )
        self.assertFalse(existing_django_logger.disabled)
        settings_override = override_settings(
            OWNER_TENANT_ID=owner.id,
            SOLAPI_KAKAO_PF_ID="COMMON-OWNER-CHANNEL",
            MESSAGING_TENANT_BINDING_KEY="limglish-clinic-contract-key",
        )
        def provider_mock(*args, before_provider_call=None, **kwargs):
            del args, kwargs
            self.assertIsNotNone(before_provider_call)
            self.assertTrue(before_provider_call())
            return {"status": "ok", "group_id": "provider-mock-only"}

        with settings_override, patch(
            "apps.domains.messaging.sqs_queue.get_queue_client",
            return_value=fake_queue,
        ), patch(
            "apps.domains.messaging.policy.check_recipient_allowed",
            return_value=True,
        ), patch(
            "academy.adapters.compute.ec2_control.ensure_messaging_worker_asg_min_capacity",
        ), patch(
            "apps.worker.messaging_worker.sqs_main.send_one_alimtalk",
            side_effect=provider_mock,
        ) as provider_send:
            with self.captureOnCommitCallbacks(execute=True):
                response = self._cancel(participant)

            route = _resolve_tenant_delivery_context(
                owner.id,
                self.tenant.id,
                SOLAPI_CLINIC_CHANGE,
            )

            messages = [
                call.kwargs["message"]
                for call in fake_queue.send_message.call_args_list
            ]
            raw_messages = [
                {
                    "Body": json.dumps(message),
                    "ReceiptHandle": f"receipt-{index}",
                    "MessageId": f"message-{index}",
                }
                for index, message in enumerate(messages, start=1)
            ]

            def receive_message(**kwargs):
                del kwargs
                if raw_messages:
                    return raw_messages.pop(0)
                sqs_main._shutdown = True
                return None

            fake_queue.receive_message.side_effect = receive_message
            worker_config = SimpleNamespace(
                MESSAGING_SQS_QUEUE_NAME="clinic-cancel-worker-mock",
                SQS_WAIT_TIME_SECONDS=0,
                TEST_TENANT_ID=9999,
                OWNER_TENANT_ID=owner.id,
                SOLAPI_SENDER="0212345678",
                SOLAPI_KAKAO_PF_ID="COMMON-OWNER-CHANNEL",
            )
            sqs_main._shutdown = False
            try:
                with patch.object(sqs_main, "load_config", return_value=worker_config), patch.object(
                    sqs_main,
                    "get_queue_client",
                    return_value=fake_queue,
                ), patch.object(
                    sqs_main,
                    "acquire_job_lock",
                    return_value=True,
                ), patch.object(
                    sqs_main,
                    "release_job_lock",
                ), patch.object(
                    sqs_main,
                    "_record_progress",
                ), patch.object(sqs_main.signal, "signal"):
                    self.assertEqual(sqs_main.main(), 0)
                    self.assertFalse(existing_django_logger.disabled)
            finally:
                sqs_main._shutdown = False

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["notification"], {
            "requested": 2,
            "failed": 0,
            "send_to": "both",
            "targets": [
                {"target": "parent", "requested": True},
                {"target": "student", "requested": True},
            ],
        })
        participant.refresh_from_db()
        self.assertEqual(participant.status, SessionParticipant.Status.CANCELLED)
        outboxes = list(ScheduledNotification.objects.order_by("id"))
        self.assertEqual(len(outboxes), 2)
        self.assertTrue(all(row.status == ScheduledNotification.Status.SENT for row in outboxes))
        self.assertEqual(mapping.status, "APPROVED")
        self.assertEqual(_allowed_common_template_ids("clinic_cancelled"), {SOLAPI_CLINIC_CHANGE})
        self.assertEqual(route["billing_tenant_id"], self.tenant.id)
        self.assertEqual(route["channel_source"], "tenant_verified")
        self.assertEqual(route["channel"]["pf_id"], "VERIFIED-LIMGLISH-CHANNEL")
        self.assertEqual(route["template_id"], "APPROVED-LIMGLISH-CLINIC-CHANGE")

        self.assertEqual(len(messages), 2)
        self.assertCountEqual(
            [message["target_type"] for message in messages],
            ["parent", "student"],
        )
        with override_settings(
            MESSAGING_TENANT_BINDING_KEY="limglish-clinic-contract-key",
        ):
            for message in messages:
                self.assertEqual(message["tenant_id"], owner.id)
                self.assertEqual(message["source_tenant_id"], self.tenant.id)
                self.assertEqual(message["event_type"], "clinic_cancelled")
                self.assertEqual(message["message_mode"], "alimtalk")
                self.assertEqual(message["template_id"], SOLAPI_CLINIC_CHANGE)
                self.assertIsNone(message["sender"])
                self.assertEqual(_worker_tenant_binding_error(message), "")
                self.assertTrue(verify_tenant_binding_signature(
                    signature=message["tenant_binding_signature"],
                    tenant_id=message["tenant_id"],
                    source_tenant_id=message["source_tenant_id"],
                    business_idempotency_key=message["business_idempotency_key"],
                ))
        self.assertEqual(provider_send.call_count, 2)
        self.assertCountEqual(
            [call.kwargs["pf_id"] for call in provider_send.call_args_list],
            ["VERIFIED-LIMGLISH-CHANNEL", "VERIFIED-LIMGLISH-CHANNEL"],
        )
        self.assertCountEqual(
            [call.kwargs["template_id"] for call in provider_send.call_args_list],
            [
                "APPROVED-LIMGLISH-CLINIC-CHANGE",
                "APPROVED-LIMGLISH-CLINIC-CHANGE",
            ],
        )
        delivery_logs = NotificationLog.objects.filter(
            source_tenant_id=self.tenant.id,
            notification_type="clinic_cancelled",
            success=True,
        )
        self.assertEqual(delivery_logs.count(), 2)
        self.assertCountEqual(
            delivery_logs.values_list("target_type", flat=True),
            ["parent", "student"],
        )
        participant.refresh_from_db()
        self.assertEqual(participant.status, SessionParticipant.Status.CANCELLED)

    def test_resolved_stale_and_completed_links_do_not_block_final_booking(self):
        cases = []

        resolved = self._current_required_link()
        resolved.resolved_at = timezone.now()
        resolved.save(update_fields=["resolved_at"])
        cases.append("resolved")

        stale_exam = Exam.objects.create(
            tenant=self.tenant,
            title="차시에서 제거된 시험",
            exam_type=Exam.ExamType.REGULAR,
            is_active=True,
        )
        self.make_clinic_link(
            self.enrollment,
            self.data["lec_session"],
            source_type="exam",
            source_id=stale_exam.id,
            cycle_no=2,
        )
        cases.append("stale")

        completed_exam = Exam.objects.create(
            tenant=self.tenant,
            title="완료된 차시 시험",
            exam_type=Exam.ExamType.REGULAR,
            is_active=True,
        )
        completed_exam.sessions.add(self.data["lec_session"])
        self.make_clinic_link(
            self.enrollment,
            self.data["lec_session"],
            source_type="exam",
            source_id=completed_exam.id,
            cycle_no=3,
        )
        SessionProgress.objects.create(
            enrollment=self.enrollment,
            session=self.data["lec_session"],
            completed=True,
            completed_at=timezone.now(),
        )
        cases.append("completed")

        participant = self.make_participant(
            self.tenant,
            self._session(5, 17),
            self.student,
            status=SessionParticipant.Status.BOOKED,
        )
        with patch(
            "apps.domains.clinic.services.lifecycle.send_clinic_event_notification",
            return_value=True,
        ):
            response = self._cancel(participant)

        self.assertEqual(cases, ["resolved", "stale", "completed"])
        self.assertEqual(response.status_code, 200, response.data)

    def test_staff_keeps_administrative_cancellation_for_required_final_booking(self):
        self._current_required_link()
        participant = self.make_participant(
            self.tenant,
            self._session(6, 13),
            self.student,
            status=SessionParticipant.Status.BOOKED,
        )
        self.client.force_authenticate(user=self.data["admin_user"])
        with patch(
            "apps.domains.clinic.views.participant_views._send_clinic_notification",
            return_value={"requested": 1, "failed": 0, "send_to": "parent", "targets": []},
        ):
            response = self.client.patch(
                f"/api/v1/clinic/participants/{participant.id}/set_status/",
                {"status": "cancelled"},
                format="json",
                **self._headers(self.tenant),
            )

        self.assertEqual(response.status_code, 200, response.data)
        participant.refresh_from_db()
        self.assertEqual(participant.status, SessionParticipant.Status.CANCELLED)

    def test_booking_projection_exposes_server_owned_self_cancel_decision(self):
        self._current_required_link()
        only = self.make_participant(
            self.tenant,
            self._session(2, 14),
            self.student,
            status=SessionParticipant.Status.BOOKED,
        )
        self.client.force_authenticate(user=self.student.user)

        response = self.client.get(
            "/api/v1/clinic/participants/?page_size=200",
            **self._headers(self.tenant),
        )

        self.assertEqual(response.status_code, 200, response.data)
        rows = response.data.get("results", response.data)
        row = next(item for item in rows if item["id"] == only.id)
        self.assertFalse(row["can_self_cancel"])
        self.assertIn("같은 주", row["self_cancel_reason"])


class ClinicSelfCancellationConcurrencyTest(TransactionTestCase, ClinicTestMixin):
    reset_sequences = True

    def setUp(self):
        self.data = self.setup_full_tenant("clinic_cancel_race", student_count=1)
        self.tenant = self.data["tenant"]
        self.student = self.data["students"][0]
        self.actor = self.student.user
        self.actor.tenant = self.tenant
        self.actor.save(update_fields=["tenant"])
        exam = Exam.objects.create(
            tenant=self.tenant,
            title="동시 취소 시험",
            exam_type=Exam.ExamType.REGULAR,
            is_active=True,
        )
        exam.sessions.add(self.data["lec_session"])
        self.make_clinic_link(
            self.data["enrollments"][0],
            self.data["lec_session"],
            source_type="exam",
            source_id=exam.id,
        )
        self.week_start = timezone.localdate() - datetime.timedelta(
            days=timezone.localdate().weekday()
        )
        self.participants = [
            self.make_participant(
                self.tenant,
                self.make_clinic_session(
                    self.tenant,
                    date=self.week_start + datetime.timedelta(days=offset),
                    start_time=datetime.time(hour, 0),
                    location=f"race-{offset}",
                ),
                self.student,
                status=SessionParticipant.Status.BOOKED,
            )
            for offset, hour in ((1, 14), (3, 16))
        ]

    def test_two_simultaneous_cancellations_leave_exactly_one_active_booking(self):
        if connection.vendor != "postgresql":
            self.skipTest("select_for_update concurrency contract requires PostgreSQL")
        barrier = threading.Barrier(2)
        outcomes = []
        outcome_lock = threading.Lock()
        decision_time = timezone.make_aware(
            datetime.datetime.combine(self.week_start, datetime.time(12, 0))
        )

        def cancel(participant_id):
            close_old_connections()
            try:
                tenant = type(self.tenant).objects.get(pk=self.tenant.pk)
                student = type(self.student).objects.get(pk=self.student.pk)
                actor = type(self.actor).objects.get(pk=self.actor.pk)
                barrier.wait(timeout=5)
                change_participant_status(
                    tenant=tenant,
                    participant_id=participant_id,
                    next_status=SessionParticipant.Status.CANCELLED,
                    actor=actor,
                    request_student=student,
                )
                result = "cancelled"
            except Conflict:
                result = "blocked"
            finally:
                close_old_connections()
            with outcome_lock:
                outcomes.append(result)

        with patch(
            "apps.domains.clinic.services.lifecycle.send_clinic_event_notification",
            return_value=True,
        ), patch(
            "apps.domains.clinic.services.lifecycle.timezone.now",
            return_value=decision_time,
        ):
            threads = [
                threading.Thread(target=cancel, args=(participant.id,))
                for participant in self.participants
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertCountEqual(outcomes, ["cancelled", "blocked"])
        self.assertEqual(
            SessionParticipant.objects.filter(
                tenant=self.tenant,
                student=self.student,
                status__in=(
                    SessionParticipant.Status.PENDING,
                    SessionParticipant.Status.BOOKED,
                ),
            ).count(),
            1,
        )
