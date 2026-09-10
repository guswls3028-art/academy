"""Cross-domain student/enrollment Excel password-mode integration tests."""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from apps.core.models import Tenant
from apps.domains.enrollment.services import lecture_enroll_from_excel_rows
from apps.domains.lectures.models import Lecture
from apps.domains.students.models import Student
from apps.domains.students.services.account_notice import _decrypt
from apps.domains.students.services import (
    StudentImportPasswordError,
    import_students_from_rows,
)


class StudentExcelImportPasswordModeTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="엑셀 비밀번호 학원",
            code="excel_password_modes",
            is_active=True,
        )

    def test_phone_last4_mode_is_rejected_without_creating_accounts(self):
        with self.assertRaisesRegex(StudentImportPasswordError, "fixed 또는 random"):
            import_students_from_rows(
                tenant_id=self.tenant.id,
                students_data=[
                    {
                        "name": "폐기정책학생",
                        "parent_phone": "01070001111",
                        "phone": "01090001234",
                        "school_type": "HIGH",
                        "grade": 1,
                        "uses_identifier": False,
                    }
                ],
                initial_password="",
                password_mode="phone_last4",
            )

        self.assertFalse(Student.objects.filter(tenant=self.tenant).exists())

    @patch("apps.domains.messaging.services.send_welcome_messages")
    @patch("apps.domains.students.services.import_passwords.secrets.randbelow", return_value=42)
    def test_random_mode_returns_download_credentials_and_sets_password(
        self,
        _random_mock,
        send_mock,
    ):
        result = import_students_from_rows(
            tenant_id=self.tenant.id,
            students_data=[
                {
                    "name": "랜덤학생",
                    "parent_phone": "01070004444",
                    "phone": "01090009999",
                    "school_type": "HIGH",
                    "grade": 1,
                    "uses_identifier": False,
                }
            ],
            initial_password="",
            password_mode="random",
        )

        student = Student.objects.get(tenant=self.tenant, name="랜덤학생")
        self.assertTrue(student.user.check_password("000042"))
        self.assertTrue(student.user.must_change_password)
        self.assertEqual(
            result["credentials"],
            [{
                "name": "랜덤학생",
                "login_id": student.ps_number,
                "password": "000042",
            }],
        )
        send_mock.assert_not_called()
        self.assertEqual(
            _decrypt(student.pending_account_notice_student_password_ciphertext),
            "000042",
        )

    @patch(
        "apps.support.students.account_notice_dependencies.send_welcome_messages",
        return_value={"status": "enqueued", "enqueued": 2},
    )
    def test_existing_student_lecture_excel_import_sends_first_enrollment_notice(
        self,
        send_welcome,
    ):
        lecture = Lecture.objects.create(
            tenant=self.tenant,
            title="비밀번호 방식 강의",
            name="비밀번호 방식 강의",
            subject="MATH",
        )
        import_students_from_rows(
            tenant_id=self.tenant.id,
            students_data=[
                {
                    "name": "강의등록학생",
                    "parent_phone": "01070005555",
                    "phone": "01090008765",
                    "school_type": "HIGH",
                    "grade": 1,
                    "uses_identifier": False,
                }
            ],
            initial_password="teacher-selected-password",
            password_mode="fixed",
        )

        with self.captureOnCommitCallbacks(execute=True):
            result = lecture_enroll_from_excel_rows(
                tenant_id=self.tenant.id,
                lecture_id=lecture.id,
                students_data=[
                    {
                        "name": "강의등록학생",
                        "parent_phone": "01070005555",
                        "phone": "01090008765",
                        "school_type": "HIGH",
                        "grade": 1,
                        "uses_identifier": False,
                    }
                ],
            )

        student = Student.objects.get(tenant=self.tenant, name="강의등록학생")
        self.assertTrue(student.user.check_password("teacher-selected-password"))
        self.assertEqual(result["created_students_count"], 0)
        self.assertEqual(result["enrolled_count"], 1)
        send_welcome.assert_called_once()
        self.assertEqual(
            send_welcome.call_args.kwargs["student_password"],
            "teacher-selected-password",
        )
