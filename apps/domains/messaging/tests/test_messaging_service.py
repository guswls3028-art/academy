# apps/support/messaging/tests/test_messaging_service.py
"""
알림톡/메시징 서비스 단위 테스트.
DB 의존 없이 mock 기반으로 핵심 로직을 검증.

검증 범위:
1. send_event_notification — 트리거별 발송 경로
2. enqueue_alimtalk — SQS enqueue 정책 / tenant isolation
3. is_reservation_cancelled — tenant isolation
4. 템플릿 변수 치환 — 누락/오입력 방어
5. 성적표 메시지 포맷 — 시험/과제/합불/미응시
6. worker dequeue — tenant_id 필수 / silent failure 방지
7. 가입 관련 회귀 — send_welcome_messages
8. 반 등록 완료 / 퇴원 알림
"""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch


# ──────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────

def _make_tenant(tid=1, name="학원플러스", kakao_pfid="KA01PF_TEST", provider="solapi",
                 is_active=True, balance=100):
    t = SimpleNamespace(
        id=tid, pk=tid, name=name, kakao_pfid=kakao_pfid,
        messaging_provider=provider, messaging_is_active=is_active,
        credit_balance=balance, messaging_sender="01031217466",
        messaging_base_price=10,
    )
    domain_mock = MagicMock()
    domain_mock.filter.return_value.first.return_value = SimpleNamespace(host="hakwonplus.com")
    t.domains = domain_mock
    return t


def _make_student(name="홍길동", phone="01012345678", parent_phone="01087654321"):
    return SimpleNamespace(name=name, phone=phone, parent_phone=parent_phone,
                           ps_number="PS001", tenant_id=1)


def _make_config(trigger, enabled=True, message_mode="alimtalk",
                 template_name="Test", solapi_template_id="KA01TP_TEST",
                 solapi_status="APPROVED", body="#{학원명} #{학생이름2} 안내",
                 delay_mode="immediate", delay_value=None, template_tenant_id=1):
    tmpl = SimpleNamespace(
        name=template_name, body=body,
        tenant_id=template_tenant_id,
        solapi_template_id=solapi_template_id,
        solapi_status=solapi_status,
        subject="테스트 제목",
    )
    return SimpleNamespace(
        trigger=trigger, enabled=enabled, message_mode=message_mode,
        template=tmpl, delay_mode=delay_mode, delay_value=delay_value,
    )


# services.py에서 내부 import하는 모듈 경로
_SEL = "apps.domains.messaging.selectors"
_POL = "apps.domains.messaging.policy"
_SVC = "apps.domains.messaging.services"
_QSV = "apps.domains.messaging.services.queue_service"
_REG = "apps.domains.messaging.services.registration_service"
_SQS = "apps.domains.messaging.sqs_queue"


class _DispatchThroughQueueMixin:
    """Keep pure notification-format tests independent of the outbox database.

    Durable outbox behavior is covered by ``test_scheduled_notifications``.  These
    tests intentionally exercise the payload handed to the queue boundary, so the
    adapter below makes the mocked dispatch boundary forward that payload to the
    already-mocked ``enqueue_alimtalk`` function.
    """

    def setUp(self):
        super().setUp()
        patcher = patch(
            "apps.domains.messaging.scheduled.dispatch_notification_now",
            side_effect=self._dispatch_through_queue,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _dispatch_through_queue(*, tenant_id, trigger, payload):
        from apps.domains.messaging.models import ScheduledNotification
        from apps.domains.messaging.services.queue_service import enqueue_alimtalk

        del tenant_id, trigger
        enqueued = enqueue_alimtalk(**payload)
        return SimpleNamespace(
            status=(
                ScheduledNotification.Status.SENT
                if enqueued
                else ScheduledNotification.Status.FAILED
            )
        )


# ──────────────────────────────────────────
# 1. send_event_notification 테스트
# ──────────────────────────────────────────

class TestSendEventNotification(_DispatchThroughQueueMixin, TestCase):
    """send_event_notification 핵심 경로 테스트."""

    @patch(f"{_QSV}.enqueue_alimtalk")
    @patch(f"{_SEL}.get_auto_send_config")
    @patch(f"{_POL}.is_messaging_disabled", return_value=False)
    @patch(f"{_POL}.get_owner_tenant_id", return_value=1)
    def test_sends_to_parent_with_correct_replacements(
        self, mock_owner, mock_disabled, mock_config, mock_enqueue
    ):
        """학부모 전화번호로 올바른 치환 변수와 함께 enqueue."""
        tenant = _make_tenant()
        student = _make_student()
        config = _make_config("check_in_complete",
                              body="#{학원명}입니다. #{학생이름2}학생이 입실했습니다. #{강의명} #{사이트링크}")
        mock_config.return_value = config
        mock_enqueue.return_value = True

        from apps.domains.messaging.services import send_event_notification
        result = send_event_notification(
            tenant=tenant, trigger="check_in_complete", student=student,
            send_to="parent", context={"강의명": "수학A반"},
        )

        self.assertTrue(result)
        mock_enqueue.assert_called_once()
        kw = mock_enqueue.call_args.kwargs
        self.assertEqual(kw["tenant_id"], 1)
        self.assertEqual(kw["to"], "01087654321")
        self.assertEqual(kw["message_mode"], "alimtalk")
        self.assertEqual(kw["target_type"], "parent")
        # 통합 승인 봉투 우선 라우팅 정책: check_in_complete는 attendance 통합 템플릿 사용
        self.assertEqual(kw["template_id"], "KA01TP260406121126868FGddLmrDFUC")
        # 알림톡 대체/미리보기 text
        self.assertIn("학원플러스", kw["text"])
        self.assertIn("길동", kw["text"])
        self.assertIn("수학A반", kw["text"])
        # alimtalk replacements
        reps = {r["key"]: r["value"] for r in kw["alimtalk_replacements"]}
        # 통합 attendance 템플릿 변수 스키마: 학원이름/학생이름/강의명...
        self.assertEqual(reps["학원이름"], "학원플러스")
        self.assertEqual(reps["학생이름"], "홍길동")
        self.assertEqual(reps["강의명"], "수학A반")
        self.assertEqual(kw["provider_template_type"], "attendance")
        self.assertEqual(kw["provider_template_version"], "CXHFcwKdJU")
        self.assertEqual(
            kw["provider_template_structure_fingerprint"],
            "7f443b87eb8d8c95",
        )
        self.assertEqual(len(kw["content_snapshot_sha256"]), 64)

    @patch(f"{_QSV}.enqueue_alimtalk")
    @patch(f"{_SEL}.get_auto_send_config")
    @patch(f"{_POL}.is_messaging_disabled", return_value=False)
    @patch(f"{_POL}.get_owner_tenant_id", return_value=1)
    def test_sends_to_student_phone(self, mock_owner, mock_disabled, mock_config, mock_enqueue):
        """send_to='student'이면 학생 전화번호 사용."""
        tenant = _make_tenant()
        student = _make_student(phone="01099998888")
        config = _make_config("exam_score_published")
        mock_config.return_value = config
        mock_enqueue.return_value = True

        from apps.domains.messaging.services import send_event_notification
        result = send_event_notification(
            tenant=tenant, trigger="exam_score_published", student=student,
            send_to="student",
        )

        self.assertTrue(result)
        self.assertEqual(mock_enqueue.call_args.kwargs["to"], "01099998888")
        self.assertEqual(mock_enqueue.call_args.kwargs["target_type"], "student")

    @patch(f"{_QSV}.enqueue_alimtalk")
    @patch(f"{_SEL}.get_auto_send_config")
    @patch(f"{_POL}.is_messaging_disabled", return_value=False)
    @patch(f"{_POL}.get_owner_tenant_id", return_value=1)
    def test_skips_when_disabled(self, mock_owner, mock_disabled, mock_config, mock_enqueue):
        """config 비활성이면 발송 스킵."""
        tenant = _make_tenant()
        student = _make_student()
        config = _make_config("check_in_complete", enabled=False)
        mock_config.return_value = config

        from apps.domains.messaging.services import send_event_notification
        result = send_event_notification(tenant=tenant, trigger="check_in_complete", student=student)

        self.assertFalse(result)
        mock_enqueue.assert_not_called()

    @patch(f"{_QSV}.enqueue_alimtalk")
    @patch(f"{_SEL}.get_auto_send_config", return_value=None)
    @patch(f"{_POL}.is_messaging_disabled", return_value=False)
    @patch(f"{_POL}.get_owner_tenant_id", return_value=1)
    def test_skips_when_no_config(self, mock_owner, mock_disabled, mock_config, mock_enqueue):
        """config가 없으면 발송 스킵."""
        tenant = _make_tenant()
        student = _make_student()

        from apps.domains.messaging.services import send_event_notification
        result = send_event_notification(tenant=tenant, trigger="nonexistent", student=student)

        self.assertFalse(result)
        mock_enqueue.assert_not_called()

    @patch(f"{_QSV}.enqueue_alimtalk")
    @patch(f"{_SEL}.get_auto_send_config")
    @patch(f"{_POL}.is_messaging_disabled", return_value=False)
    @patch(f"{_POL}.get_owner_tenant_id", return_value=1)
    def test_uses_unified_template_when_template_not_approved(self, mock_owner, mock_disabled, mock_config, mock_enqueue):
        """통합 승인 봉투 우선 정책: 개별 템플릿이 미승인이어도 통합 템플릿으로 발송."""
        tenant = _make_tenant()
        student = _make_student()
        config = _make_config("check_in_complete", solapi_status="PENDING")
        mock_config.return_value = config
        mock_enqueue.return_value = True

        from apps.domains.messaging.services import send_event_notification
        result = send_event_notification(tenant=tenant, trigger="check_in_complete", student=student)

        self.assertTrue(result)
        mock_enqueue.assert_called_once()
        self.assertEqual(
            mock_enqueue.call_args.kwargs["template_id"],
            "KA01TP260406121126868FGddLmrDFUC",
        )

    @patch(f"{_QSV}.enqueue_alimtalk")
    @patch(f"{_SEL}.get_auto_send_config")
    @patch(f"{_POL}.is_messaging_disabled", return_value=False)
    @patch(f"{_POL}.get_owner_tenant_id", return_value=1)
    def test_clinic_checkout_uses_shared_clinic_envelope(
        self, mock_owner, mock_disabled, mock_config, mock_enqueue
    ):
        tenant = _make_tenant()
        student = _make_student()
        mock_config.return_value = _make_config(
            "clinic_check_out",
            body="클리닉에서 하원하였습니다.",
        )
        mock_enqueue.return_value = True

        from apps.domains.messaging.services import send_event_notification

        result = send_event_notification(
            tenant=tenant,
            trigger="clinic_check_out",
            student=student,
            send_to="parent",
            context={
                "장소": "2층 클리닉실",
                "날짜": "2026-09-03",
                "시간": "21:40",
                "_domain_object_id": "participant_42_checkout",
            },
        )

        self.assertTrue(result)
        payload = mock_enqueue.call_args.kwargs
        self.assertEqual(payload["template_id"], "KA01TP2604061058318608Hy40ZnTFZT")
        self.assertEqual(payload["domain_object_id"], "participant_42_checkout")
        self.assertEqual(
            payload["occurrence_key"],
            "clinic_check_out:participant_42_checkout",
        )
        replacements = {item["key"]: item["value"] for item in payload["alimtalk_replacements"]}
        self.assertEqual(replacements["클리닉장소"], "2층 클리닉실")
        self.assertEqual(replacements["클리닉날짜"], "2026-09-03")
        self.assertEqual(replacements["클리닉시간"], "21:40")
        self.assertIn("하원", replacements["선생님메모"])
        self.assertEqual(payload["provider_template_type"], "clinic_info")
        self.assertEqual(payload["provider_template_version"], "W6jQe04a0p")
        self.assertEqual(
            payload["provider_template_structure_fingerprint"],
            "9e8c96df5beebac2",
        )

    @patch(f"{_QSV}.enqueue_alimtalk")
    @patch(f"{_SEL}.get_auto_send_config")
    @patch(f"{_POL}.is_messaging_disabled", return_value=False)
    @patch(f"{_POL}.get_owner_tenant_id", return_value=1)
    def test_auto_send_blocks_legacy_sms_mode(
        self, mock_owner, mock_disabled, mock_config, mock_enqueue
    ):
        """명시적인 비알림톡 설정은 조용히 알림톡으로 바꾸지 않는다."""
        tenant = _make_tenant()
        student = _make_student()
        config = _make_config("check_in_complete", message_mode="sms")
        mock_config.return_value = config
        mock_enqueue.return_value = True

        from apps.domains.messaging.services import send_event_notification
        result = send_event_notification(tenant=tenant, trigger="check_in_complete", student=student)

        self.assertFalse(result)
        mock_enqueue.assert_not_called()

    @patch(f"{_QSV}.enqueue_alimtalk")
    @patch(f"{_SEL}.get_auto_send_config")
    @patch(f"{_POL}.is_messaging_disabled", return_value=False)
    @patch(f"{_POL}.get_owner_tenant_id", return_value=1)
    def test_skips_when_no_phone(self, mock_owner, mock_disabled, mock_config, mock_enqueue):
        """전화번호 없으면 발송 스킵."""
        tenant = _make_tenant()
        student = _make_student(parent_phone="")
        config = _make_config("check_in_complete")
        mock_config.return_value = config

        from apps.domains.messaging.services import send_event_notification
        result = send_event_notification(
            tenant=tenant, trigger="check_in_complete", student=student, send_to="parent",
        )

        self.assertFalse(result)
        mock_enqueue.assert_not_called()

    @patch(f"{_POL}.is_messaging_disabled", return_value=True)
    def test_skips_test_tenant(self, mock_disabled):
        """테스트 테넌트(9999)는 발송 스킵."""
        tenant = _make_tenant(tid=9999)
        student = _make_student()

        from apps.domains.messaging.services import send_event_notification
        result = send_event_notification(tenant=tenant, trigger="check_in_complete", student=student)

        self.assertFalse(result)

    @patch(f"{_QSV}.enqueue_alimtalk")
    @patch(f"{_SEL}.get_auto_send_config")
    @patch(f"{_POL}.is_messaging_disabled", return_value=False)
    @patch(f"{_POL}.get_owner_tenant_id", return_value=1)
    def test_skips_when_tenant_has_no_config(
        self, mock_owner, mock_disabled, mock_config, mock_enqueue
    ):
        """테넌트 config 없으면 owner config로 fallback하지 않고 스킵."""
        tenant = _make_tenant(tid=2, name="박철과학")
        student = _make_student()
        mock_config.return_value = None

        from apps.domains.messaging.services import send_event_notification
        result = send_event_notification(tenant=tenant, trigger="check_in_complete", student=student)

        self.assertFalse(result)
        mock_enqueue.assert_not_called()
        mock_config.assert_called_once_with(2, "check_in_complete")

    @patch(f"{_QSV}.enqueue_alimtalk")
    @patch(f"{_SEL}.get_auto_send_config")
    @patch(f"{_POL}.is_messaging_disabled", return_value=False)
    @patch(f"{_POL}.get_owner_tenant_id", return_value=1)
    def test_owner_fallback_blocked_for_opt_in_trigger_without_tenant_row(
        self, mock_owner, mock_disabled, mock_config, mock_enqueue
    ):
        """명시 opt-in 트리거는 테넌트 row가 없으면 오너 설정으로 우회 발송하지 않는다."""
        tenant = _make_tenant(tid=2, name="박철과학")
        student = _make_student()
        mock_config.return_value = None

        from apps.domains.messaging.services import send_event_notification
        result = send_event_notification(tenant=tenant, trigger="qna_answered", student=student)

        self.assertFalse(result)
        mock_enqueue.assert_not_called()
        mock_config.assert_called_once_with(2, "qna_answered")

    @patch("apps.domains.messaging.scheduled.schedule_notification")
    @patch(f"{_QSV}.enqueue_alimtalk")
    @patch(f"{_SEL}.get_auto_send_config")
    @patch(f"{_POL}.is_messaging_disabled", return_value=False)
    @patch(f"{_POL}.get_owner_tenant_id", return_value=1)
    def test_delay_mode_schedules_instead_of_immediate_enqueue(
        self, mock_owner, mock_disabled, mock_config, mock_enqueue, mock_schedule
    ):
        """자동발송 지연 설정은 즉시 enqueue하지 않고 예약 대기열에 기록한다."""
        tenant = _make_tenant()
        student = _make_student()
        config = _make_config("check_in_complete", delay_mode="delay_minutes", delay_value=15)
        mock_config.return_value = config

        from apps.domains.messaging.services import send_event_notification
        result = send_event_notification(tenant=tenant, trigger="check_in_complete", student=student)

        self.assertTrue(result)
        mock_enqueue.assert_not_called()
        mock_schedule.assert_called_once()
        kwargs = mock_schedule.call_args.kwargs
        self.assertEqual(kwargs["tenant_id"], tenant.id)
        self.assertEqual(kwargs["trigger"], "check_in_complete")
        self.assertEqual(kwargs["delay_mode"], "delay_minutes")
        self.assertEqual(kwargs["delay_value"], 15)
        self.assertEqual(kwargs["payload"]["message_mode"], "alimtalk")
        self.assertEqual(kwargs["payload"]["target_type"], "parent")

    @patch("apps.domains.messaging.scheduled.schedule_notification")
    @patch(f"{_QSV}.enqueue_alimtalk")
    @patch(f"{_SEL}.get_auto_send_config")
    @patch(f"{_POL}.is_messaging_disabled", return_value=False)
    @patch(f"{_POL}.get_owner_tenant_id", return_value=1)
    def test_invalid_scheduled_hour_is_not_sent_immediately(
        self, mock_owner, mock_disabled, mock_config, mock_enqueue, mock_schedule
    ):
        """잘못된 지정 시각은 즉시 발송으로 새지 않도록 차단한다."""
        tenant = _make_tenant()
        student = _make_student()
        config = _make_config("check_in_complete", delay_mode="scheduled_hour", delay_value=24)
        mock_config.return_value = config

        from apps.domains.messaging.services import send_event_notification
        result = send_event_notification(tenant=tenant, trigger="check_in_complete", student=student)

        self.assertFalse(result)
        mock_enqueue.assert_not_called()
        mock_schedule.assert_not_called()

    @patch("apps.domains.messaging.scheduled.schedule_notification")
    @patch(f"{_QSV}.enqueue_alimtalk")
    @patch(f"{_SEL}.get_auto_send_config")
    @patch(f"{_POL}.is_messaging_disabled", return_value=False)
    @patch(f"{_POL}.get_owner_tenant_id", return_value=1)
    def test_invalid_delay_mode_is_not_sent_immediately(
        self, mock_owner, mock_disabled, mock_config, mock_enqueue, mock_schedule
    ):
        """DB에 잘못된 delay_mode가 들어와도 즉시 발송으로 열리지 않는다."""
        tenant = _make_tenant()
        student = _make_student()
        config = _make_config("check_in_complete", delay_mode="legacy_mode", delay_value=10)
        mock_config.return_value = config

        from apps.domains.messaging.services import send_event_notification
        result = send_event_notification(tenant=tenant, trigger="check_in_complete", student=student)

        self.assertFalse(result)
        mock_enqueue.assert_not_called()
        mock_schedule.assert_not_called()

    @patch("apps.domains.messaging.scheduled.schedule_notification")
    @patch(f"{_QSV}.enqueue_alimtalk")
    @patch(f"{_SEL}.get_auto_send_config")
    @patch(f"{_POL}.is_messaging_disabled", return_value=False)
    @patch(f"{_POL}.get_owner_tenant_id", return_value=1)
    def test_negative_delay_minutes_is_not_sent_immediately(
        self, mock_owner, mock_disabled, mock_config, mock_enqueue, mock_schedule
    ):
        """음수 지연값은 예약/즉시 발송 모두 차단한다."""
        tenant = _make_tenant()
        student = _make_student()
        config = _make_config("check_in_complete", delay_mode="delay_minutes", delay_value=-1)
        mock_config.return_value = config

        from apps.domains.messaging.services import send_event_notification
        result = send_event_notification(tenant=tenant, trigger="check_in_complete", student=student)

        self.assertFalse(result)
        mock_enqueue.assert_not_called()
        mock_schedule.assert_not_called()

    def test_schedule_notification_rejects_unknown_delay_mode(self):
        """하위 예약 함수도 알 수 없는 mode를 즉시 발송으로 fail-open 하지 않는다."""
        from apps.domains.messaging.scheduled import schedule_notification

        with self.assertRaises(ValueError):
            schedule_notification(
                tenant_id=1,
                trigger="check_in_complete",
                delay_mode="legacy_mode",
                delay_value=10,
                payload={"tenant_id": 1, "to": "01012345678", "text": "test"},
            )


# ──────────────────────────────────────────
# 2. tenant isolation 테스트
# ──────────────────────────────────────────

class TestTenantIsolation(_DispatchThroughQueueMixin, TestCase):
    """멀티테넌트 격리 검증."""

    def test_is_reservation_cancelled_requires_tenant_id(self):
        """tenant_id 없이 호출하면 항상 False (cross-tenant lookup 차단)."""
        from apps.domains.messaging.services import is_reservation_cancelled
        result = is_reservation_cancelled(reservation_id=123, tenant_id=None)
        self.assertFalse(result)

    @patch(f"{_QSV}.enqueue_alimtalk")
    @patch(f"{_SEL}.get_auto_send_config")
    @patch(f"{_POL}.is_messaging_disabled", return_value=False)
    @patch(f"{_POL}.get_owner_tenant_id", return_value=1)
    def test_event_notification_uses_correct_tenant_id(
        self, mock_owner, mock_disabled, mock_config, mock_enqueue
    ):
        """send_event_notification이 owner tenant로 enqueue하고 source tenant를 메타데이터로 남김."""
        tenant_a = _make_tenant(tid=2, name="테넌트A")
        student = _make_student()
        config = _make_config("check_in_complete", template_tenant_id=2)
        owner_config = _make_config("check_in_complete", template_tenant_id=1)
        mock_config.side_effect = [config, owner_config]
        mock_enqueue.return_value = True

        from apps.domains.messaging.services import send_event_notification
        send_event_notification(tenant=tenant_a, trigger="check_in_complete", student=student)

        self.assertEqual(mock_enqueue.call_args.kwargs["tenant_id"], 1)
        self.assertEqual(mock_enqueue.call_args.kwargs["source_tenant_id"], 2)

    @patch(f"{_QSV}.enqueue_alimtalk")
    @patch(f"{_SEL}.get_auto_send_config")
    @patch(f"{_POL}.is_messaging_disabled", return_value=False)
    @patch(f"{_POL}.get_owner_tenant_id", return_value=1)
    def test_no_cross_tenant_channel_fallback(
        self, mock_owner, mock_disabled, mock_config, mock_enqueue
    ):
        """테넌트 A 이벤트도 공용 owner 채널로만 enqueue."""
        tenant_a = _make_tenant(tid=3, name="테넌트A")
        student = _make_student()
        config = _make_config("withdrawal_complete", template_tenant_id=3)
        owner_config = _make_config("withdrawal_complete", template_tenant_id=1)
        mock_config.side_effect = [config, owner_config]
        mock_enqueue.return_value = True

        from apps.domains.messaging.services import send_event_notification
        send_event_notification(tenant=tenant_a, trigger="withdrawal_complete", student=student)

        kw = mock_enqueue.call_args.kwargs
        self.assertEqual(kw["tenant_id"], 1)
        self.assertEqual(kw["source_tenant_id"], 3)
        reps = {r["key"]: r["value"] for r in kw["alimtalk_replacements"]}
        self.assertEqual(reps["학원명"], "테넌트A")


# ──────────────────────────────────────────
# 3. enqueue_alimtalk 정책 테스트
# ──────────────────────────────────────────

class TestEnqueueAlimtalkPolicy(TestCase):
    """enqueue_alimtalk 메시지 모드 및 정책 테스트."""

    @patch(f"{_POL}.is_messaging_disabled", return_value=False)
    def test_sms_mode_is_disabled_service_wide(self, mock_disabled):
        """SMS 모드는 전체 서비스에서 차단."""
        from apps.domains.messaging.services import enqueue_alimtalk
        from apps.domains.messaging.policy import MessagingPolicyError

        with self.assertRaises(MessagingPolicyError) as ctx:
            enqueue_alimtalk(
                tenant_id=1,
                to="01012345678",
                text="테스트",
                message_mode="sms",
            )
        self.assertEqual(ctx.exception.reason, "sms_disabled")

    @patch(f"{_POL}.is_messaging_disabled", return_value=False)
    def test_sms_blocked_for_non_owner(self, mock_disabled):
        """SMS는 owner 여부와 관계없이 MessagingPolicyError 발생."""
        from apps.domains.messaging.services import enqueue_alimtalk
        from apps.domains.messaging.policy import MessagingPolicyError

        with self.assertRaises(MessagingPolicyError) as ctx:
            enqueue_alimtalk(tenant_id=5, to="01012345678", text="테스트", message_mode="sms")
        self.assertEqual(ctx.exception.reason, "sms_disabled")

    @patch(f"{_SQS}.MessagingSQSQueue")
    @patch(f"{_POL}.is_messaging_disabled", return_value=False)
    @patch(f"{_QSV}._require_active_business_tenant")
    def test_alimtalk_mode_skips_sms_check(
        self,
        mock_active_tenant,
        mock_disabled,
        mock_queue_cls,
    ):
        """알림톡 전용 모드에서는 SMS 정책 검사 불필요."""
        mock_queue = MagicMock()
        mock_queue.enqueue.return_value = True
        mock_queue_cls.return_value = mock_queue

        from apps.domains.messaging.services import enqueue_alimtalk
        result = enqueue_alimtalk(
            tenant_id=5, to="01012345678", text="테스트", message_mode="alimtalk",
            sender="01099999999",
        )

        self.assertTrue(result)
        kwargs = mock_queue.enqueue.call_args.kwargs
        self.assertEqual(kwargs["tenant_id"], 1)
        self.assertEqual(kwargs["source_tenant_id"], 5)
        self.assertIsNone(kwargs["sender"])

    @patch("apps.core.models.Tenant.objects.filter")
    @patch(f"{_SQS}.MessagingSQSQueue")
    @patch(f"{_POL}.check_recipient_allowed", return_value=True)
    @patch(f"{_POL}.is_messaging_disabled", return_value=False)
    def test_inactive_business_tenant_is_terminally_blocked(
        self,
        mock_disabled,
        mock_recipient,
        mock_queue_cls,
        mock_tenant_filter,
    ):
        from apps.domains.messaging.policy import MessagingPolicyError
        from apps.domains.messaging.services import enqueue_alimtalk

        mock_tenant_filter.return_value.exists.return_value = False

        with self.assertRaisesRegex(MessagingPolicyError, "비활성화") as raised:
            enqueue_alimtalk(
                tenant_id=77,
                to="01012345678",
                text="test",
                message_mode="alimtalk",
            )

        self.assertEqual(raised.exception.reason, "business_tenant_inactive")
        mock_queue_cls.assert_not_called()

    @patch(f"{_QSV}._require_active_business_tenant", side_effect=RuntimeError("db down"))
    @patch(f"{_POL}.check_recipient_allowed", return_value=True)
    @patch(f"{_POL}.is_messaging_disabled", return_value=False)
    def test_business_tenant_lookup_error_is_raised_for_retry(
        self,
        mock_disabled,
        mock_recipient,
        mock_active_tenant,
    ):
        from apps.domains.messaging.services import enqueue_alimtalk

        with self.assertRaisesRegex(RuntimeError, "db down"):
            enqueue_alimtalk(
                tenant_id=5,
                to="01012345678",
                text="test",
                message_mode="alimtalk",
            )

    @patch(f"{_POL}.is_messaging_disabled", return_value=True)
    def test_test_tenant_skipped(self, mock_disabled):
        """테스트 테넌트(9999)는 enqueue 스킵."""
        from apps.domains.messaging.services import enqueue_alimtalk
        result = enqueue_alimtalk(tenant_id=9999, to="01012345678", text="테스트")
        self.assertFalse(result)


# ──────────────────────────────────────────
# 4. 템플릿 변수 치환 테스트
# ──────────────────────────────────────────

class TestTemplateVariableSubstitution(_DispatchThroughQueueMixin, TestCase):
    """알림톡 템플릿 변수 치환 검증."""

    @patch(f"{_QSV}.enqueue_alimtalk")
    @patch(f"{_SEL}.get_auto_send_config")
    @patch(f"{_POL}.is_messaging_disabled", return_value=False)
    @patch(f"{_POL}.get_owner_tenant_id", return_value=1)
    def test_none_notice_uses_registered_replacements(self, mock_owner, mock_disabled, mock_config, mock_enqueue):
        """NONE 고정 본문은 등록 변수 replacements로만 값을 전달."""
        tenant = _make_tenant(name="수학왕학원")
        student = _make_student(name="김철수")
        config = _make_config(
            "withdrawal_complete",
            body="#{학원명}입니다. #{학생이름2}님 퇴원. #{학생이름} #{사이트링크}",
        )
        mock_config.return_value = config
        mock_enqueue.return_value = True

        from apps.domains.messaging.services import send_event_notification
        send_event_notification(tenant=tenant, trigger="withdrawal_complete", student=student)

        reps = {
            item["key"]: item["value"]
            for item in mock_enqueue.call_args.kwargs["alimtalk_replacements"]
        }
        self.assertEqual(reps, {
            "학원명": "수학왕학원",
            "학생이름2": "철수",
        })

    @patch(f"{_QSV}.enqueue_alimtalk")
    @patch(f"{_SEL}.get_auto_send_config")
    @patch(f"{_POL}.is_messaging_disabled", return_value=False)
    @patch(f"{_POL}.get_owner_tenant_id", return_value=1)
    def test_context_vars_substituted(self, mock_owner, mock_disabled, mock_config, mock_enqueue):
        """context에 전달한 추가 변수도 치환."""
        tenant = _make_tenant()
        student = _make_student()
        config = _make_config(
            "check_in_complete",
            body="#{학원명} #{학생이름2} #{강의명} #{차시명} #{날짜} #{시간}",
        )
        mock_config.return_value = config
        mock_enqueue.return_value = True

        from apps.domains.messaging.services import send_event_notification
        send_event_notification(
            tenant=tenant, trigger="check_in_complete", student=student,
            context={"강의명": "영어반", "차시명": "3차시", "날짜": "2026-03-25", "시간": "14:00"},
        )

        text = mock_enqueue.call_args.kwargs["text"]
        self.assertIn("영어반", text)
        self.assertIn("3차시", text)
        self.assertIn("2026-03-25", text)
        self.assertIn("14:00", text)
        self.assertNotIn("#{", text)

    @patch(f"{_QSV}.enqueue_alimtalk")
    @patch(f"{_SEL}.get_auto_send_config")
    @patch(f"{_POL}.is_messaging_disabled", return_value=False)
    @patch(f"{_POL}.get_owner_tenant_id", return_value=1)
    def test_exam_score_published_vars(self, mock_owner, mock_disabled, mock_config, mock_enqueue):
        """성적 공개 알림의 시험명/강의명/시험성적 치환."""
        tenant = _make_tenant()
        student = _make_student()
        config = _make_config(
            "exam_score_published",
            body="#{학원명} #{학생이름2} #{시험명} #{강의명} 성적: #{시험성적} #{사이트링크}",
        )
        mock_config.return_value = config
        mock_enqueue.return_value = True

        from apps.domains.messaging.services import send_event_notification
        send_event_notification(
            tenant=tenant, trigger="exam_score_published", student=student,
            context={"시험명": "중간고사", "강의명": "수학A", "시험성적": "85/100"},
        )

        text = mock_enqueue.call_args.kwargs["text"]
        self.assertIn("중간고사", text)
        self.assertIn("수학A", text)
        self.assertIn("85/100", text)
        self.assertNotIn("#{", text)

    @patch(f"{_QSV}.enqueue_alimtalk")
    @patch(f"{_SEL}.get_auto_send_config")
    @patch(f"{_POL}.is_messaging_disabled", return_value=False)
    @patch(f"{_POL}.get_owner_tenant_id", return_value=1)
    def test_single_char_name_safe(self, mock_owner, mock_disabled, mock_config, mock_enqueue):
        """1글자 이름도 NONE 고정 본문 replacements에서 안전하게 처리."""
        tenant = _make_tenant()
        student = _make_student(name="이")
        config = _make_config("withdrawal_complete", body="#{학생이름2}님 안녕")
        mock_config.return_value = config
        mock_enqueue.return_value = True

        from apps.domains.messaging.services import send_event_notification
        send_event_notification(tenant=tenant, trigger="withdrawal_complete", student=student)

        reps = {
            item["key"]: item["value"]
            for item in mock_enqueue.call_args.kwargs["alimtalk_replacements"]
        }
        self.assertEqual(reps["학생이름2"], "이")


# ──────────────────────────────────────────
# 5. 성적표 메시지 포맷 테스트
# ──────────────────────────────────────────

class TestScoreReportFormat(TestCase):
    """성적표 메시지 구조 및 알림톡 템플릿 매칭 검증."""

    def test_score_report_structure_2_exams_4_homeworks(self):
        """시험 2개 + 과제 4개: [시험], [과제], [요약] 구조 검증."""
        detail_lines = [
            "[시험]",
            "- 단원평가 1: 92/100 (92%) 합격",
            "- 중간고사: 미응시",
            "",
            "[과제]",
            "- 영어쓰기: 80/100 (80%) 합격",
            "- 수학풀이: 50/100 (50%) 불합격",
            "- 독후감: 미제출",
            "- 실험보고서: 100/100 (100%) 합격",
            "",
            "[요약]",
            "- 시험: 1/2 합격 (평균 92점)",
            "- 과제: 2/4 합격 (평균 77점)",
            "- 최종: 보충 필요",
            "- 보충 대상: 중간고사, 수학풀이, 독후감",
        ]
        detail = "\n".join(detail_lines)

        # 구조 검증
        self.assertIn("[시험]", detail)
        self.assertIn("[과제]", detail)
        self.assertIn("[요약]", detail)
        self.assertIn("합격", detail)
        self.assertIn("불합격", detail)
        self.assertIn("미응시", detail)
        self.assertIn("미제출", detail)
        self.assertIn("보충 필요", detail)
        self.assertIn("보충 대상:", detail)
        self.assertIn("시험: 1/2 합격", detail)
        self.assertIn("과제: 2/4 합격", detail)

    def test_template_237_structure(self):
        """Template ID=237 (성적표 안내)에 #{시험성적} 변수가 정확히 삽입되는지."""
        template_body = (
            "안녕하세요, #{학원명}입니다.\n"
            "#{학생이름2}학생님, 성적표 안내드립니다.\n"
            "\n"
            "#{강의명} · #{차시명}\n"
            "━━━━━━━━━━━━━━━━\n"
            "\n"
            "#{시험성적}\n"
            "\n"
            "━━━━━━━━━━━━━━━━\n"
            "상세 결과는 앱에서 확인하실 수 있습니다.\n"
            "#{사이트링크}"
        )
        replacements = {
            "학원명": "학원플러스",
            "학생이름2": "길동",
            "강의명": "수학A반",
            "차시명": "5차시",
            "시험성적": "[시험]\n- 단원평가: 92/100 (92%) 합격\n\n[요약]\n- 시험: 1/1 합격 (평균 92점)\n- 최종: 합격",
            "사이트링크": "https://hakwonplus.com",
        }
        text = template_body
        for k, v in replacements.items():
            text = text.replace(f"#{{{k}}}", v)

        self.assertNotIn("#{", text)
        self.assertIn("학원플러스", text)
        self.assertIn("길동", text)
        self.assertIn("수학A반 · 5차시", text)
        self.assertIn("단원평가: 92/100 (92%) 합격", text)
        self.assertIn("최종: 합격", text)

    def test_all_passed_shows_합격(self):
        """모든 시험/과제 합격이면 '최종: 합격'."""
        summary = "[요약]\n- 시험: 2/2 합격 (평균 95점)\n- 최종: 합격"
        self.assertIn("최종: 합격", summary)
        self.assertNotIn("보충", summary)


# ──────────────────────────────────────────
# 6. Worker tenant_id 필수 검증
# ──────────────────────────────────────────

class TestWorkerTenantIdEnforcement(TestCase):
    """워커의 tenant_id 필수 정책 검증."""

    def test_message_without_tenant_id(self):
        """tenant_id 없는 메시지는 거부 대상."""
        msg = {"to": "01012345678", "text": "테스트"}
        self.assertIsNone(msg.get("tenant_id"))

    def test_message_with_tenant_id(self):
        """tenant_id 있는 메시지는 정상."""
        msg = {"tenant_id": 1, "to": "01012345678", "text": "테스트"}
        self.assertIsNotNone(msg.get("tenant_id"))


# ──────────────────────────────────────────
# 7. 가입 관련 회귀
# ──────────────────────────────────────────

class TestRegistrationMessages(TestCase):
    """가입 승인 알림톡 회귀 테스트."""

    @patch(f"{_REG}._dispatch_registration_durably")
    @patch(f"{_POL}.get_owner_tenant_id", return_value=1)
    @patch(f"{_SEL}.get_auto_send_config")
    def test_send_registration_approved_student(self, mock_config, mock_owner, mock_enqueue):
        """학생 가입 승인 메시지가 올바른 변수로 발송."""
        tmpl = SimpleNamespace(
            body="#{학생이름}님 승인. ID: #{학생아이디} PW: #{학생비밀번호} #{사이트링크} #{비밀번호안내}",
            tenant_id=1,
            solapi_template_id="KA01TP_REG", solapi_status="APPROVED",
            subject="가입 승인", name="가입승인학생",
        )
        config = SimpleNamespace(enabled=True, template=tmpl)
        mock_config.return_value = config
        mock_enqueue.return_value = True

        from apps.domains.messaging.services import send_registration_approved_messages
        result = send_registration_approved_messages(
            tenant_id=2, site_url="https://hakwonplus.com",
            student_name="홍길동", student_phone="01012345678",
            student_id="PS001", student_password="test1234", student_pk=10,
            parent_phone="01087654321", parent_password="parent123",
        )

        self.assertEqual(result["status"], "enqueued")
        self.assertGreaterEqual(result["enqueued"], 1)
        text = mock_enqueue.call_args_list[0].kwargs["text"]
        self.assertIn("홍길동", text)
        self.assertIn("PS001", text)
        student_kwargs = mock_enqueue.call_args_list[0].kwargs
        parent_kwargs = mock_enqueue.call_args_list[1].kwargs
        self.assertEqual(student_kwargs["event_type"], "registration_approved_student")
        self.assertEqual(student_kwargs["target_type"], "account")
        self.assertEqual(student_kwargs["target_id"], "student:10")
        self.assertEqual(student_kwargs["source_tenant_id"], 2)
        self.assertEqual(parent_kwargs["event_type"], "registration_approved_parent")
        self.assertEqual(parent_kwargs["target_type"], "account")
        self.assertEqual(parent_kwargs["target_id"], "parent:10")
        self.assertEqual(parent_kwargs["source_tenant_id"], 2)

    @patch(f"{_REG}._dispatch_registration_durably")
    @patch(f"{_POL}.get_owner_tenant_id", return_value=1)
    @patch(f"{_SEL}.get_auto_send_config")
    def test_send_registration_approved_dedupes_same_student_parent_phone(self, mock_config, mock_owner, mock_enqueue):
        """학생/학부모 수신번호가 같으면 종합 parent 템플릿 1건만 발송."""
        parent_tmpl = SimpleNamespace(
            body="#{학생이름} #{학생아이디} #{학생비밀번호} #{학부모아이디} #{학부모비밀번호}",
            tenant_id=1,
            solapi_template_id="KA01TP_PARENT", solapi_status="APPROVED",
            subject="가입 승인", name="가입승인학부모",
        )
        mock_config.side_effect = [
            SimpleNamespace(enabled=True, template=parent_tmpl),
        ]
        mock_enqueue.return_value = True

        from apps.domains.messaging.services import send_registration_approved_messages
        result = send_registration_approved_messages(
            tenant_id=2, site_url="https://hakwonplus.com",
            student_name="홍길동", student_phone="01012345678",
            student_id="PS001", student_password="test1234", student_pk=10,
            parent_phone="01012345678", parent_password="parent123",
        )

        self.assertEqual(result["status"], "enqueued")
        self.assertEqual(result["enqueued"], 1)
        self.assertEqual(mock_enqueue.call_count, 1)
        kwargs = mock_enqueue.call_args.kwargs
        self.assertEqual(kwargs["event_type"], "registration_approved_parent")
        self.assertEqual(kwargs["to"], "01012345678")
        self.assertEqual(kwargs["target_id"], "parent:10")
        self.assertIn("PS001", kwargs["text"])
        self.assertIn("parent123", kwargs["text"])

    @patch(f"{_REG}._dispatch_registration_durably")
    @patch(f"{_POL}.get_owner_tenant_id", return_value=1)
    @patch(f"{_SEL}.get_auto_send_config")
    def test_registration_approved_rejects_cross_tenant_owner_template(
        self,
        mock_config,
        mock_owner,
        mock_enqueue,
    ):
        foreign_template = SimpleNamespace(
            tenant_id=99,
            body="다른 학원 가입 문구",
            solapi_template_id="FOREIGN-APPROVED",
            solapi_status="APPROVED",
        )
        mock_config.return_value = SimpleNamespace(enabled=True, template=foreign_template)

        from apps.domains.messaging.services import send_registration_approved_messages

        result = send_registration_approved_messages(
            tenant_id=2,
            site_url="https://example.test",
            student_name="격리검증",
            student_phone="01012345678",
            student_id="PS999",
            student_password="test1234",
            parent_phone="01087654321",
            parent_password="parent123",
        )

        self.assertEqual(result["enqueued"], 0)
        mock_enqueue.assert_not_called()

    @patch(f"{_REG}._dispatch_registration_durably")
    @patch(f"{_POL}.get_owner_tenant_id", return_value=1)
    @patch(f"{_SEL}.get_auto_send_config")
    def test_send_welcome_passes_account_event_metadata(self, mock_config, mock_owner, mock_enqueue):
        """학생 직접 등록 welcome도 계정성 알림 trigger로 로그 분류된다."""
        student_config = _make_config(
            "registration_approved_student",
            body="#{학생이름} #{학생아이디} #{학생비밀번호} #{사이트링크}",
        )
        parent_config = _make_config(
            "registration_approved_parent",
            body="#{학생이름} #{학부모아이디} #{학부모비밀번호} #{사이트링크}",
        )
        mock_config.side_effect = [student_config, parent_config]
        mock_enqueue.return_value = True
        student = SimpleNamespace(
            name="홍길동",
            phone="01012345678",
            parent_phone="01087654321",
            ps_number="PS001",
            tenant_id=1,
            id=10,
        )

        from apps.domains.messaging.services import send_welcome_messages
        result = send_welcome_messages(
            created_students=[student],
            student_password="test1234",
            parent_password_by_phone={"01087654321": "4321"},
            site_url="https://hakwonplus.com",
        )

        self.assertEqual(result["status"], "enqueued")
        self.assertEqual(result["enqueued"], 2)
        student_kwargs = mock_enqueue.call_args_list[0].kwargs
        parent_kwargs = mock_enqueue.call_args_list[1].kwargs
        self.assertEqual(student_kwargs["event_type"], "registration_approved_student")
        self.assertEqual(student_kwargs["target_type"], "account")
        self.assertEqual(student_kwargs["target_id"], "student:10")
        self.assertEqual(student_kwargs["source_tenant_id"], 1)
        self.assertEqual(parent_kwargs["event_type"], "registration_approved_parent")
        self.assertEqual(parent_kwargs["target_type"], "account")
        self.assertEqual(parent_kwargs["target_id"], "parent:10")
        self.assertEqual(parent_kwargs["source_tenant_id"], 1)

    @patch(f"{_REG}._dispatch_registration_durably")
    @patch(f"{_POL}.get_owner_tenant_id", return_value=1)
    @patch(f"{_SEL}.get_auto_send_config")
    def test_send_welcome_system_auto_ignores_stale_disabled_flags(
        self,
        mock_config,
        mock_owner,
        mock_enqueue,
    ):
        """SYSTEM_AUTO 계정 안내는 legacy enabled=False 때문에 누락되지 않는다."""
        student_config = _make_config(
            "registration_approved_student",
            enabled=False,
            body="#{학생이름} #{학생아이디} #{학생비밀번호} #{사이트링크}",
        )
        parent_config = _make_config(
            "registration_approved_parent",
            enabled=False,
            body="#{학생이름} #{학생아이디} #{학생비밀번호} #{학부모아이디} #{학부모비밀번호} #{사이트링크}",
        )
        mock_config.side_effect = [student_config, parent_config]
        mock_enqueue.return_value = True
        student = SimpleNamespace(
            name="홍길동",
            phone="01012345678",
            parent_phone="01087654321",
            ps_number="PS001",
            tenant_id=1,
            id=10,
        )

        from apps.domains.messaging.services import send_welcome_messages

        result = send_welcome_messages(
            created_students=[student],
            student_password="test1234",
            parent_password_by_phone={"01087654321": "4321"},
            site_url="https://hakwonplus.com",
        )

        self.assertEqual(result, {"status": "enqueued", "enqueued": 2})
        self.assertEqual(
            [call.kwargs["event_type"] for call in mock_enqueue.call_args_list],
            ["registration_approved_student", "registration_approved_parent"],
        )

    @patch(f"{_REG}._dispatch_registration_durably")
    @patch(f"{_POL}.get_owner_tenant_id", return_value=1)
    @patch(f"{_SEL}.get_auto_send_config")
    def test_send_welcome_dedupes_same_student_parent_phone(self, mock_config, mock_owner, mock_enqueue):
        """직접 등록 welcome도 같은 번호에는 parent 템플릿 1건만 보낸다."""
        student_config = _make_config(
            "registration_approved_student",
            body="#{학생이름} #{학생아이디} #{학생비밀번호}",
        )
        parent_config = _make_config(
            "registration_approved_parent",
            body="#{학생이름} #{학생아이디} #{학생비밀번호} #{학부모아이디} #{학부모비밀번호}",
        )
        mock_config.side_effect = [student_config, parent_config]
        mock_enqueue.return_value = True
        student = SimpleNamespace(
            name="홍길동",
            phone="01012345678",
            parent_phone="01012345678",
            ps_number="PS001",
            tenant_id=1,
            id=10,
        )

        from apps.domains.messaging.services import send_welcome_messages
        result = send_welcome_messages(
            created_students=[student],
            student_password="test1234",
            parent_password_by_phone={"01012345678": "4321"},
            site_url="https://hakwonplus.com",
        )

        self.assertEqual(result["status"], "enqueued")
        self.assertEqual(result["enqueued"], 1)
        self.assertEqual(mock_enqueue.call_count, 1)
        kwargs = mock_enqueue.call_args.kwargs
        self.assertEqual(kwargs["event_type"], "registration_approved_parent")
        self.assertEqual(kwargs["to"], "01012345678")
        self.assertEqual(kwargs["target_id"], "parent:10")

    @patch(f"{_REG}._dispatch_registration_durably")
    @patch(f"{_POL}.get_owner_tenant_id", return_value=1)
    @patch(f"{_SEL}.get_auto_send_config")
    def test_send_welcome_no_students(self, mock_config, mock_owner, mock_enqueue):
        """학생 없으면 skip."""
        from apps.domains.messaging.services import send_welcome_messages
        result = send_welcome_messages(created_students=[], student_password="test")
        self.assertEqual(result["status"], "skip")


# ──────────────────────────────────────────
# 8. 반 등록 완료 / 퇴원 알림
# ──────────────────────────────────────────

class TestEnrollmentAndWithdrawalNotifications(_DispatchThroughQueueMixin, TestCase):
    """반 등록 완료, 퇴원 알림 테스트."""

    @patch(f"{_QSV}.enqueue_alimtalk")
    @patch(f"{_SEL}.get_auto_send_config")
    @patch(f"{_POL}.get_owner_tenant_id")
    def test_disabled_and_unknown_triggers_fail_closed_before_config_lookup(
        self,
        mock_owner,
        mock_config,
        mock_enqueue,
    ):
        """정책상 비활성/미등록 트리거는 설정이나 큐를 조회하지 않는다."""
        tenant = _make_tenant()
        student = _make_student()
        mock_config.return_value = _make_config("class_enrollment_complete")
        mock_enqueue.return_value = True

        from apps.domains.messaging.services import send_event_notification

        for trigger in (
            "class_enrollment_complete",
            "enrollment_expiring_soon",
            "student_signup",
            "unregistered_trigger",
        ):
            with self.subTest(trigger=trigger):
                result = send_event_notification(
                    tenant=tenant,
                    trigger=trigger,
                    student=student,
                )
                self.assertFalse(result)

        mock_config.assert_not_called()
        mock_owner.assert_not_called()
        mock_enqueue.assert_not_called()

    @patch(f"{_QSV}.enqueue_alimtalk")
    @patch(f"{_SEL}.get_auto_send_config")
    @patch(f"{_POL}.is_messaging_disabled", return_value=False)
    @patch(f"{_POL}.get_owner_tenant_id", return_value=1)
    def test_withdrawal_complete(self, mock_owner, mock_disabled, mock_config, mock_enqueue):
        """퇴원 알림이 올바른 트리거/변수로 발송."""
        tenant = _make_tenant()
        student = _make_student()
        config = _make_config(
            "withdrawal_complete",
            body="#{학원명}입니다. #{학생이름2}님 퇴원 완료.",
        )
        mock_config.return_value = config
        mock_enqueue.return_value = True

        from apps.domains.messaging.services import send_event_notification
        result = send_event_notification(
            tenant=tenant, trigger="withdrawal_complete", student=student, send_to="parent",
        )

        self.assertTrue(result)
        reps = {
            item["key"]: item["value"]
            for item in mock_enqueue.call_args.kwargs["alimtalk_replacements"]
        }
        self.assertEqual(reps["학원명"], "학원플러스")
        self.assertEqual(reps["학생이름2"], "길동")


# ──────────────────────────────────────────
# 안전장치 테스트 — idempotency, recipient whitelist
# ──────────────────────────────────────────

class TestIdempotencyMetadata(_DispatchThroughQueueMixin, TestCase):
    """send_event_notification이 멱등성 메타데이터를 enqueue_alimtalk에 전달."""

    @patch(f"{_QSV}.enqueue_alimtalk")
    @patch(f"{_SEL}.get_auto_send_config")
    @patch(f"{_POL}.is_messaging_disabled", return_value=False)
    @patch(f"{_POL}.get_owner_tenant_id", return_value=1)
    def test_passes_event_metadata(self, mock_owner, mock_disabled, mock_config, mock_enqueue):
        """trigger, student_id, occurrence_key가 enqueue_alimtalk에 전달됨."""
        tenant = _make_tenant()
        student = SimpleNamespace(
            id=42, pk=42, name="홍길동", phone="01012345678",
            parent_phone="01087654321", ps_number="PS001", tenant_id=1,
        )
        config = _make_config("check_in_complete")
        mock_config.return_value = config
        mock_enqueue.return_value = True

        from apps.domains.messaging.services import send_event_notification
        send_event_notification(tenant=tenant, trigger="check_in_complete", student=student)

        kwargs = mock_enqueue.call_args.kwargs
        self.assertEqual(kwargs["event_type"], "check_in_complete")
        self.assertEqual(kwargs["target_type"], "parent")
        self.assertEqual(kwargs["target_id"], 42)
        self.assertTrue(kwargs["occurrence_key"])  # 날짜 기반 키

    @patch(f"{_QSV}.enqueue_alimtalk")
    @patch(f"{_SEL}.get_auto_send_config")
    @patch(f"{_POL}.is_messaging_disabled", return_value=False)
    @patch(f"{_POL}.get_owner_tenant_id", return_value=1)
    def test_passes_source_metadata(self, mock_owner, mock_disabled, mock_config, mock_enqueue):
        tenant = _make_tenant()
        student = SimpleNamespace(
            id=42, pk=42, name="홍길동", phone="01012345678",
            parent_phone="01087654321", ps_number="PS001", tenant_id=1,
        )
        mock_config.return_value = _make_config("check_in_complete")
        mock_enqueue.return_value = True

        from apps.domains.messaging.services import send_event_notification
        send_event_notification(
            tenant=tenant,
            trigger="check_in_complete",
            student=student,
            context={
                "_source_domain": "attendance",
                "_source_use_case": "attendance.check_in_complete",
                "_domain_object_id": "123",
                "_actor_id": "7",
            },
        )

        kwargs = mock_enqueue.call_args.kwargs
        self.assertEqual(kwargs["source_domain"], "attendance")
        self.assertEqual(kwargs["source_use_case"], "attendance.check_in_complete")
        self.assertEqual(kwargs["domain_object_id"], "123")
        self.assertEqual(kwargs["actor_id"], "7")


class TestOwnerAlimtalkMetadata(TestCase):
    """계정/비밀번호 알림톡도 운영 로그 추적 메타데이터를 전달."""

    @patch("apps.domains.messaging.scheduled.dispatch_notification_now")
    @patch(f"{_SEL}.get_auto_send_config")
    @patch(f"{_POL}.is_messaging_disabled", return_value=False)
    @patch(f"{_POL}.get_owner_tenant_id", return_value=1)
    def test_password_reset_passes_event_metadata(
        self, mock_owner, mock_disabled, mock_config, mock_dispatch
    ):
        """비밀번호 재설정 알림톡은 trigger와 계정 식별 정보를 큐에 남김."""
        config = _make_config(
            "password_reset_student",
            solapi_template_id="KA01TP_PASSWORD",
            body="#{학생이름} #{학생아이디} #{학생비밀번호} #{사이트링크}",
        )
        mock_config.return_value = config
        mock_dispatch.return_value = SimpleNamespace(status="pending")

        from apps.domains.messaging.policy import send_alimtalk_via_owner
        result = send_alimtalk_via_owner(
            trigger="password_reset_student",
            to="01031217466",
            replacements={
                "학생이름": "전청조",
                "학생아이디": "jeon-test",
                "학생비밀번호": "0000",
                "사이트링크": "https://hakwonplus.com",
            },
        )

        self.assertTrue(result)
        kwargs = mock_dispatch.call_args.kwargs
        self.assertEqual(kwargs["trigger"], "password_reset_student")
        payload = kwargs["payload"]
        self.assertEqual(payload["event_type"], "password_reset_student")
        self.assertEqual(payload["target_type"], "account")
        self.assertEqual(payload["target_id"], "jeon-test")
        self.assertEqual(payload["target_name"], "전청조")
        self.assertEqual(payload["message_mode"], "alimtalk")
        self.assertEqual(payload["template_id"], "KA01TP_PASSWORD")
        self.assertEqual(payload["origin_type"], "system_account")

    @patch("apps.domains.messaging.scheduled.dispatch_notification_now")
    @patch(f"{_SEL}.get_auto_send_config")
    @patch(f"{_POL}.is_messaging_disabled", return_value=False)
    @patch(f"{_POL}.get_owner_tenant_id", return_value=1)
    def test_missing_exact_template_does_not_fallback(
        self, mock_owner, mock_disabled, mock_config, mock_dispatch
    ):
        """exact trigger 템플릿이 미승인이면 다른 trigger로 fallback하지 않음."""
        pending_config = _make_config(
            "password_find_otp",
            solapi_template_id="KA01TP_PENDING",
            solapi_status="PENDING",
            body="#{인증번호}",
        )
        mock_config.return_value = pending_config

        from apps.domains.messaging.policy import send_alimtalk_via_owner
        result = send_alimtalk_via_owner(
            trigger="password_find_otp",
            to="01031217466",
            replacements={
                "학생이름": "전청조",
                "학생비밀번호": "123456",
            },
        )

        self.assertFalse(result)
        mock_config.assert_called_once_with(1, "password_find_otp")
        mock_dispatch.assert_not_called()


class TestRecipientWhitelist(TestCase):
    """recipient guard: 테스트 모드에서 whitelist 번호만 발송."""

    @patch.dict("os.environ", {"MESSAGING_TEST_WHITELIST": "01031217466,01034137466"})
    def test_allowed_number(self):
        from apps.domains.messaging.policy import check_recipient_allowed
        self.assertTrue(check_recipient_allowed("01031217466"))
        self.assertTrue(check_recipient_allowed("01034137466"))

    @patch.dict("os.environ", {"MESSAGING_TEST_WHITELIST": "01031217466,01034137466"})
    def test_blocked_number(self):
        from apps.domains.messaging.policy import check_recipient_allowed
        self.assertFalse(check_recipient_allowed("01099999999"))

    @patch.dict("os.environ", {"MESSAGING_TEST_WHITELIST": ""})
    def test_empty_whitelist_allows_all(self):
        """운영 모드: whitelist 비어있으면 모든 번호 허용."""
        from apps.domains.messaging.policy import check_recipient_allowed
        self.assertTrue(check_recipient_allowed("01099999999"))

    @patch.dict("os.environ", {}, clear=False)
    def test_no_env_allows_all(self):
        """MESSAGING_TEST_WHITELIST 미설정 시 모든 번호 허용."""
        import os
        os.environ.pop("MESSAGING_TEST_WHITELIST", None)
        from apps.domains.messaging.policy import check_recipient_allowed
        self.assertTrue(check_recipient_allowed("01099999999"))

    @patch.dict(
        "os.environ",
        {
            "MESSAGING_RECIPIENT_DENYLIST": "010-9999-8888",
            "MESSAGING_TEST_WHITELIST": "01099998888,01031217466",
        },
    )
    def test_operational_denylist_overrides_whitelist(self):
        from apps.domains.messaging.policy import check_recipient_allowed

        self.assertFalse(check_recipient_allowed("01099998888"))
        self.assertFalse(check_recipient_allowed("010-9999-8888"))
        self.assertTrue(check_recipient_allowed("01031217466"))


class TestDryRunMode(TestCase):
    """dry-run 모드: 로그만 남기고 실발송 안 함."""

    @patch.dict("os.environ", {"MESSAGING_DRY_RUN_TRIGGERS": "*"})
    def test_wildcard_blocks_general_triggers(self):
        from apps.domains.messaging.policy import is_event_dry_run
        self.assertTrue(is_event_dry_run("check_in_complete"))
        self.assertTrue(is_event_dry_run("exam_score_published"))
        self.assertTrue(is_event_dry_run("withdrawal_complete"))

    @patch.dict("os.environ", {"MESSAGING_DRY_RUN_TRIGGERS": "*"})
    def test_wildcard_allows_registration_triggers(self):
        """가입/비밀번호 관련은 dry-run에서도 실발송."""
        from apps.domains.messaging.policy import is_event_dry_run
        self.assertFalse(is_event_dry_run("registration_approved_student"))
        self.assertFalse(is_event_dry_run("registration_approved_parent"))
        self.assertFalse(is_event_dry_run("password_find_otp"))
        self.assertFalse(is_event_dry_run("password_reset_student"))

    @patch.dict("os.environ", {"MESSAGING_DRY_RUN_TRIGGERS": "check_in_complete,absent_occurred"})
    def test_specific_triggers_only(self):
        from apps.domains.messaging.policy import is_event_dry_run
        self.assertTrue(is_event_dry_run("check_in_complete"))
        self.assertTrue(is_event_dry_run("absent_occurred"))
        self.assertFalse(is_event_dry_run("exam_score_published"))

    @patch.dict("os.environ", {"MESSAGING_DRY_RUN_TRIGGERS": ""})
    def test_empty_env_no_dry_run(self):
        from apps.domains.messaging.policy import is_event_dry_run
        self.assertFalse(is_event_dry_run("check_in_complete"))

    @patch(f"{_QSV}.enqueue_alimtalk")
    @patch(f"{_SEL}.get_auto_send_config")
    @patch(f"{_POL}.is_messaging_disabled", return_value=False)
    @patch(f"{_POL}.get_owner_tenant_id", return_value=1)
    @patch.dict("os.environ", {"MESSAGING_DRY_RUN_TRIGGERS": "*"})
    def test_send_event_notification_dry_run(
        self, mock_owner, mock_disabled, mock_config, mock_enqueue
    ):
        """dry-run 모드에서 send_event_notification은 enqueue 호출하지 않음."""
        tenant = _make_tenant()
        student = _make_student()
        config = _make_config("check_in_complete")
        mock_config.return_value = config

        from apps.domains.messaging.services import send_event_notification
        result = send_event_notification(tenant=tenant, trigger="check_in_complete", student=student)

        self.assertFalse(result)
        mock_enqueue.assert_not_called()


class TestAttendanceNotificationHook(TestCase):
    """일반 출결 저장이 자동 알림 훅과 분리되어 있는지 확인."""

    def test_partial_update_has_no_automatic_notification_hook(self):
        """PRESENT/ABSENT 저장은 수동 preview→confirm 흐름을 우회하지 않는다."""
        from apps.domains.attendance import views as att_views
        import inspect
        source = inspect.getsource(att_views.AttendanceViewSet.partial_update)
        self.assertNotIn("_send_attendance_notification", source)
        self.assertNotIn("send_event_notification", source)
        self.assertNotIn("check_in_complete", source)
        self.assertNotIn("absent_occurred", source)
