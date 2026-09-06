from __future__ import annotations

from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone
from unittest.mock import patch

from apps.core.models import Tenant, TenantMembership
from apps.domains.messaging.models import MessagingObserver, ScheduledNotification
from apps.domains.messaging.scheduled import (
    create_notification_outboxes,
    dispatch_notification_now,
    process_due_notifications,
)


User = get_user_model()


class MessagingObserverOutboxTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(code="observer", name="Observer")
        self.observer = User.objects.create_user(
            username="observer-admin",
            name="Observer Admin",
            phone="010-2222-3333",
            password="not-used",
        )
        self.membership = TenantMembership.objects.create(
            tenant=self.tenant,
            user=self.observer,
            role="admin",
            is_active=True,
        )
        MessagingObserver.objects.create(tenant=self.tenant, user=self.observer)

    def _create(
        self,
        *,
        to="01011112222",
        trigger="clinic_reminder",
        event_type=None,
    ):
        return create_notification_outboxes(
            tenant_id=self.tenant.id,
            notifications=[
                {
                    "trigger": trigger,
                    "send_at": timezone.now(),
                    "payload": {
                        "tenant_id": self.tenant.id,
                        "to": to,
                        "text": "sensitive original body",
                        "message_mode": "alimtalk",
                        "event_type": event_type or trigger,
                        "target_type": "student",
                        "target_id": "student:42",
                        "origin_type": "clinic_session",
                        "origin_id": "42",
                    },
                }
            ],
        )

    def test_creates_audited_copy_but_returns_only_original_outbox(self):
        originals = self._create()

        self.assertEqual(len(originals), 1)
        self.assertEqual(ScheduledNotification.objects.count(), 2)
        original = originals[0]
        observer_copy = ScheduledNotification.objects.exclude(pk=original.pk).get()
        self.assertEqual(observer_copy.payload["to"], "01022223333")
        self.assertEqual(observer_copy.payload["text"], original.payload["text"])
        self.assertEqual(observer_copy.payload["occurrence_key"], original.payload["occurrence_key"])
        self.assertEqual(observer_copy.payload["target_type"], "messaging_observer")
        self.assertEqual(observer_copy.payload["target_id"], f"user:{self.observer.id}")
        self.assertEqual(observer_copy.origin_type, "messaging_observer")
        self.assertEqual(observer_copy.origin_id, f"outbox:{original.id}")
        self.assertNotEqual(
            observer_copy.business_idempotency_key,
            original.business_idempotency_key,
        )

    def test_does_not_duplicate_when_observer_is_original_recipient(self):
        originals = self._create(to="01022223333")

        self.assertEqual(len(originals), 1)
        self.assertEqual(ScheduledNotification.objects.count(), 1)

    def test_account_credential_triggers_never_create_observer_copies(self):
        blocked_triggers = (
            "registration_approved_student",
            "registration_approved_parent",
            "password_find_otp",
            "password_reset_student",
            "password_reset_parent",
        )

        for trigger in blocked_triggers:
            with self.subTest(trigger=trigger):
                ScheduledNotification.objects.all().delete()

                originals = self._create(trigger=trigger)

                self.assertEqual(len(originals), 1)
                self.assertEqual(ScheduledNotification.objects.count(), 1)

    def test_sensitive_payload_event_type_suppresses_copy_when_trigger_mismatches(self):
        originals = self._create(
            trigger="clinic_reminder",
            event_type="password_reset_student",
        )

        self.assertEqual(len(originals), 1)
        self.assertEqual(ScheduledNotification.objects.count(), 1)

    @patch("apps.domains.messaging.services.enqueue_alimtalk")
    def test_retry_rechecks_and_blocks_observer_copy_that_became_sensitive(self, enqueue):
        original = self._create()[0]
        observer_copy = ScheduledNotification.objects.exclude(pk=original.pk).get()
        observer_copy.trigger = "password_reset_student"
        observer_copy.payload = {
            **observer_copy.payload,
            "event_type": "password_reset_student",
        }
        observer_copy.save(update_fields=["trigger", "payload"])

        process_due_notifications(
            batch_size=1,
            notification_ids=[observer_copy.id],
        )

        observer_copy.refresh_from_db()
        self.assertEqual(observer_copy.status, ScheduledNotification.Status.FAILED)
        self.assertEqual(
            observer_copy.error_message,
            "sensitive_observer_copy_blocked",
        )
        enqueue.assert_not_called()

    def test_inactive_membership_suppresses_observer_copy(self):
        self.membership.is_active = False
        self.membership.save(update_fields=["is_active"])

        self._create()

        self.assertEqual(ScheduledNotification.objects.count(), 1)

    def test_student_role_suppresses_observer_copy(self):
        self.membership.role = "student"
        self.membership.save(update_fields=["role"])

        self._create()

        self.assertEqual(ScheduledNotification.objects.count(), 1)

    def test_observers_with_the_same_phone_receive_one_copy(self):
        duplicate = User.objects.create_user(
            username="observer-duplicate",
            name="Observer Duplicate",
            phone="01022223333",
            password="not-used",
        )
        TenantMembership.objects.create(
            tenant=self.tenant,
            user=duplicate,
            role="owner",
            is_active=True,
        )
        MessagingObserver.objects.create(tenant=self.tenant, user=duplicate)

        self._create()

        self.assertEqual(ScheduledNotification.objects.count(), 2)

    @patch("apps.domains.messaging.scheduled.process_due_notifications")
    def test_immediate_dispatch_processes_original_and_observer_together(self, process_due):
        with self.captureOnCommitCallbacks(execute=True):
            original = dispatch_notification_now(
                tenant_id=self.tenant.id,
                trigger="clinic_reminder",
                payload={
                    "tenant_id": self.tenant.id,
                    "to": "01011112222",
                    "text": "sensitive original body",
                    "message_mode": "alimtalk",
                    "event_type": "clinic_reminder",
                    "target_type": "student",
                    "target_id": "student:42",
                    "origin_type": "clinic_session",
                    "origin_id": "42",
                },
            )

        copy = ScheduledNotification.objects.exclude(pk=original.pk).get()
        process_due.assert_called_once_with(
            batch_size=2,
            notification_ids=[original.id, copy.id],
        )


class SetMessagingObserversCommandTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(code="observer-command", name="Observer Command")
        self.user = User.objects.create_user(
            username="observer-owner",
            name="Observer Owner",
            phone="01044445555",
            password="not-used",
        )
        TenantMembership.objects.create(
            tenant=self.tenant,
            user=self.user,
            role="owner",
            is_active=True,
        )

    def test_dry_run_does_not_change_rows(self):
        output = StringIO()
        call_command(
            "set_messaging_observers",
            "--tenant-id",
            str(self.tenant.id),
            "--user-id",
            str(self.user.id),
            stdout=output,
        )

        self.assertEqual(MessagingObserver.objects.count(), 0)
        self.assertIn("dry-run", output.getvalue())

    def test_empty_replacement_requires_explicit_clear(self):
        with self.assertRaisesRegex(CommandError, "--clear"):
            call_command(
                "set_messaging_observers",
                "--tenant-id",
                str(self.tenant.id),
            )

    def test_apply_requires_sensitive_content_acknowledgement(self):
        with self.assertRaisesRegex(CommandError, "ack-sensitive-content"):
            call_command(
                "set_messaging_observers",
                "--tenant-id",
                str(self.tenant.id),
                "--user-id",
                str(self.user.id),
                "--apply",
            )

    def test_apply_rejects_student_membership(self):
        membership = TenantMembership.objects.get(tenant=self.tenant, user=self.user)
        membership.role = "student"
        membership.save(update_fields=["role"])

        with self.assertRaisesRegex(CommandError, "owner/admin/staff"):
            call_command(
                "set_messaging_observers",
                "--tenant-id",
                str(self.tenant.id),
                "--user-id",
                str(self.user.id),
                "--apply",
                "--ack-sensitive-content",
            )

    def test_apply_and_clear_replace_the_exact_set(self):
        call_command(
            "set_messaging_observers",
            "--tenant-id",
            str(self.tenant.id),
            "--user-id",
            str(self.user.id),
            "--apply",
            "--ack-sensitive-content",
            stdout=StringIO(),
        )
        self.assertEqual(
            list(MessagingObserver.objects.values_list("user_id", flat=True)),
            [self.user.id],
        )

        call_command(
            "set_messaging_observers",
            "--tenant-id",
            str(self.tenant.id),
            "--clear",
            "--apply",
            stdout=StringIO(),
        )
        self.assertEqual(MessagingObserver.objects.count(), 0)
