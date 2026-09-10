from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from apps.domains.students.services.account_notifications import (
    _parent_target_id,
    send_parent_password_changed_notice,
)


class AccountNotificationTargetIdTests(SimpleTestCase):
    def test_parent_target_uses_student_identity_without_phone(self):
        student = SimpleNamespace(id=17, ps_number="S017")

        target_id = _parent_target_id(student)

        self.assertEqual(target_id, "parent:17")
        self.assertNotIn("010", target_id)

    @patch(
        "apps.domains.students.services.account_notifications._send_owner_account_notice",
        return_value=True,
    )
    def test_parent_notice_without_student_context_uses_parent_identity(self, send_notice):
        parent = SimpleNamespace(
            id=23,
            tenant_id=5,
            phone="01012345678",
            name="학부모",
            user=SimpleNamespace(username="parent-user"),
            students=MagicMock(),
        )
        sent = send_parent_password_changed_notice(
            parent=parent,
            password="temporary-secret",
        )

        self.assertTrue(sent)
        target_id = send_notice.call_args.kwargs["log_target_id"]
        self.assertEqual(target_id, "parent-account:23")
        self.assertNotIn(parent.phone, target_id)
        parent.students.filter.assert_not_called()
        self.assertEqual(send_notice.call_args.kwargs["replacements"]["학생이름"], "")
        self.assertEqual(send_notice.call_args.kwargs["replacements"]["학생아이디"], "")

    @patch(
        "apps.domains.students.services.account_notifications._send_owner_account_notice",
        return_value=True,
    )
    def test_explicit_student_context_is_used_without_parent_child_lookup(self, send_notice):
        parent = SimpleNamespace(
            id=23,
            tenant_id=5,
            phone="01012345678",
            name="학부모",
            user=SimpleNamespace(username="parent-user"),
            students=MagicMock(),
        )
        student = SimpleNamespace(id=17, ps_number="S017", name="선택 학생")

        sent = send_parent_password_changed_notice(
            parent=parent,
            password="temporary-secret",
            student=student,
        )

        self.assertTrue(sent)
        parent.students.filter.assert_not_called()
        self.assertEqual(send_notice.call_args.kwargs["log_target_id"], "parent:17")
        self.assertEqual(send_notice.call_args.kwargs["replacements"]["학생이름"], student.name)
        self.assertEqual(send_notice.call_args.kwargs["replacements"]["학생아이디"], student.ps_number)
