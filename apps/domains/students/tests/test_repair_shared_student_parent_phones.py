from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from apps.core.models import Tenant, TenantMembership
from apps.core.models.user import user_internal_username
from apps.domains.students.models import Student
from apps.support.students.lifecycle_dependencies import ensure_parent_account_for_student


User = get_user_model()


class RepairSharedStudentParentPhonesTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="공유번호 교정 학원",
            code="shared-phone-repair",
            is_active=True,
        )

    def _shared_phone_student(self, *, suffix: str, phone: str) -> Student:
        parent = ensure_parent_account_for_student(
            tenant=self.tenant,
            parent_phone=phone,
            student_name=f"학생{suffix}",
            initial_password=f"parent-password-{suffix}",
        ).parent
        user = User.objects.create_user(
            username=user_internal_username(self.tenant, f"CUSTOM-{suffix}"),
            password=f"student-password-{suffix}",
            tenant=self.tenant,
            phone=phone,
            name=f"학생{suffix}",
        )
        user.token_version = 7
        user.save(update_fields=["token_version"])
        TenantMembership.ensure_active(tenant=self.tenant, user=user, role="student")
        return Student.objects.create(
            tenant=self.tenant,
            user=user,
            parent=parent,
            ps_number=f"CUSTOM-{suffix}",
            name=f"학생{suffix}",
            phone=phone,
            parent_phone=phone,
            omr_code=phone[-8:],
            uses_identifier=False,
            school_type="HIGH",
            grade=1,
        )

    def test_dry_run_lists_only_pii_free_candidate_ids(self):
        first = self._shared_phone_student(suffix="A", phone="01070001111")
        second = self._shared_phone_student(suffix="B", phone="01070002222")
        output = StringIO()

        call_command(
            "repair_shared_student_parent_phones",
            tenant=self.tenant.code,
            stdout=output,
        )

        text = output.getvalue()
        self.assertIn("mode=dry-run", text)
        self.assertIn("candidates=2", text)
        self.assertIn(f"ids={first.id},{second.id}", text)
        self.assertNotIn("01070001111", text)

    def test_execute_clears_only_student_contact_and_preserves_accounts(self):
        first = self._shared_phone_student(suffix="A", phone="01070001111")
        second = self._shared_phone_student(suffix="B", phone="01070002222")
        student_passwords = {first.id: first.user.password, second.id: second.user.password}
        parent_passwords = {
            first.id: first.parent.user.password,
            second.id: second.parent.user.password,
        }
        output = StringIO()

        call_command(
            "repair_shared_student_parent_phones",
            tenant=self.tenant.code,
            student_ids=f"{first.id},{second.id}",
            execute=True,
            confirm=f"{self.tenant.code}:2",
            stdout=output,
        )

        for student in (first, second):
            student.refresh_from_db()
            student.user.refresh_from_db()
            student.parent.user.refresh_from_db()
            self.assertIsNone(student.phone)
            self.assertIsNone(student.user.phone)
            self.assertTrue(student.uses_identifier)
            self.assertEqual(student.omr_code, student.parent_phone[-8:])
            self.assertEqual(student.ps_number, f"CUSTOM-{'A' if student.id == first.id else 'B'}")
            self.assertEqual(student.user.password, student_passwords[student.id])
            self.assertEqual(student.parent.user.password, parent_passwords[student.id])
            self.assertEqual(student.user.token_version, 7)
        self.assertIn("login_ids_changed=0", output.getvalue())
        self.assertIn("notifications_sent=0", output.getvalue())

    def test_execute_requires_the_complete_current_candidate_set(self):
        first = self._shared_phone_student(suffix="A", phone="01070001111")
        self._shared_phone_student(suffix="B", phone="01070002222")

        with self.assertRaisesMessage(CommandError, "candidate_set_mismatch"):
            call_command(
                "repair_shared_student_parent_phones",
                tenant=self.tenant.code,
                student_ids=str(first.id),
                execute=True,
                confirm=f"{self.tenant.code}:1",
            )

    def test_execute_fails_closed_with_pending_account_notice(self):
        student = self._shared_phone_student(suffix="A", phone="01070001111")
        student.pending_account_notice_student_password_ciphertext = "opaque"
        student.save(update_fields=["pending_account_notice_student_password_ciphertext"])

        with self.assertRaisesMessage(CommandError, "pending_account_notice_exists"):
            call_command(
                "repair_shared_student_parent_phones",
                tenant=self.tenant.code,
                student_ids=str(student.id),
                execute=True,
                confirm=f"{self.tenant.code}:1",
            )

        student.refresh_from_db()
        self.assertEqual(student.phone, "01070001111")

    @patch(
        "apps.domains.students.management.commands.repair_shared_student_parent_phones."
        "active_student_account_outbox_exists",
        return_value=True,
    )
    def test_execute_fails_closed_with_active_account_outbox(self, _outbox_mock):
        student = self._shared_phone_student(suffix="A", phone="01070001111")

        with self.assertRaisesMessage(CommandError, "active_account_outbox_exists"):
            call_command(
                "repair_shared_student_parent_phones",
                tenant=self.tenant.code,
                student_ids=str(student.id),
                execute=True,
                confirm=f"{self.tenant.code}:1",
            )

        student.refresh_from_db()
        self.assertEqual(student.phone, "01070001111")
