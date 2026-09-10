"""Cross-domain Excel worker secret and atomic-completion integration tests."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import openpyxl
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from academy.adapters.db.django.repositories_ai import DjangoAIJobRepository
from academy.application.services.excel_parsing_service import ExcelParsingService
from apps.core.models import Tenant
from apps.domains.ai.models import AIJobModel
from apps.domains.ai.services.excel_job_secrets import (
    decrypt_excel_job_secret,
    encrypt_excel_job_secret,
    public_excel_result,
    secure_excel_result,
)
from apps.domains.students.models import Student


class _WorkbookStorage:
    def __init__(self, rows: list[list[str]]) -> None:
        self.rows = rows

    def download_to_path(self, bucket: str, key: str, local_path: str) -> None:
        workbook = openpyxl.Workbook()
        worksheet = workbook.active
        for row in self.rows:
            worksheet.append(row)
        workbook.save(local_path)


class ExcelJobSecretTests(SimpleTestCase):
    def test_secret_round_trip_never_contains_plaintext(self):
        encrypted = encrypt_excel_job_secret("fixed-secret-1234")

        self.assertNotIn("fixed-secret-1234", encrypted)
        self.assertEqual(
            decrypt_excel_job_secret(encrypted),
            "fixed-secret-1234",
        )

    def test_large_credentials_are_encrypted_and_expire(self):
        now = timezone.now()
        credentials = [
            {
                "name": f"학생-{index:04d}",
                "login_id": f"student-{index:04d}",
                "password": f"{index % 10000:04d}",
            }
            for index in range(500)
        ]
        secured = secure_excel_result(
            {"created": len(credentials), "credentials": credentials},
            now=now,
        )

        self.assertNotIn(credentials[0]["password"], str(secured))
        self.assertEqual(
            public_excel_result(
                secured,
                include_credentials=True,
                now=now + timedelta(minutes=59),
            )["credentials"],
            credentials,
        )
        self.assertNotIn(
            "credentials",
            public_excel_result(
                secured,
                include_credentials=True,
                now=now + timedelta(hours=1, seconds=1),
            ),
        )

    def test_legacy_direct_credentials_never_bypass_envelope_expiry(self):
        self.assertEqual(
            public_excel_result(
                {
                    "created": 1,
                    "credentials": [{
                        "name": "legacy",
                        "login_id": "legacy",
                        "password": "1234",
                    }],
                },
                include_credentials=True,
            ),
            {"created": 1},
        )


class ExcelJobAtomicCompletionTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="원자적 엑셀 작업 학원",
            code="excel_atomic_completion",
            is_active=True,
        )
        self.job = AIJobModel.objects.create(
            job_id="excel-atomic-job",
            job_type="excel_parsing",
            status="RUNNING",
            tenant_id=str(self.tenant.id),
            payload={},
        )
        self.rows = [
            ["이름", "학부모전화번호", "학생전화번호"],
            ["원자적학생", "01070001111", "01090001234"],
        ]

    @patch("apps.domains.messaging.services.send_welcome_messages")
    @patch(
        "apps.domains.students.services.import_passwords.secrets.randbelow",
        return_value=42,
    )
    def test_account_and_encrypted_credentials_commit_together(
        self,
        _random_mock,
        _welcome_mock,
    ):
        result = ExcelParsingService(_WorkbookStorage(self.rows)).run(
            self.job.job_id,
            {
                "file_key": "excel/test.xlsx",
                "bucket": "academy-excel",
                "tenant_id": self.tenant.id,
                "password_mode": "random",
            },
        )

        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "DONE")
        self.assertTrue(
            Student.objects.filter(
                tenant=self.tenant,
                name="원자적학생",
            ).exists()
        )
        student = Student.objects.get(tenant=self.tenant, name="원자적학생")
        self.assertEqual(student.pending_account_notice_origin_type, "excel_import")
        self.assertEqual(
            student.pending_account_notice_origin_id,
            self.job.job_id,
        )
        self.assertEqual(result["credentials"][0]["password"], "000042")
        stored = DjangoAIJobRepository().get_result_payload_for_job(
            self.job,
            include_excel_credentials=True,
        )
        self.assertEqual(stored["credentials"], result["credentials"])

    @patch(
        "academy.adapters.db.django.repositories_ai.DjangoAIJobRepository.mark_done",
        return_value=False,
    )
    @patch("apps.domains.messaging.services.send_welcome_messages")
    def test_completion_failure_rolls_back_created_accounts(
        self,
        _welcome_mock,
        _mark_done_mock,
    ):
        with self.assertRaisesRegex(
            RuntimeError,
            "excel_job_atomic_completion_failed",
        ):
            ExcelParsingService(_WorkbookStorage(self.rows)).run(
                self.job.job_id,
                {
                    "file_key": "excel/test.xlsx",
                    "bucket": "academy-excel",
                    "tenant_id": self.tenant.id,
                    "password_mode": "fixed",
                    "initial_password_secret": encrypt_excel_job_secret("selected-password"),
                },
            )

        self.assertFalse(
            Student.objects.filter(
                tenant=self.tenant,
                name="원자적학생",
            ).exists()
        )

    @patch("apps.domains.messaging.services.send_welcome_messages")
    def test_invalid_excel_row_does_not_block_valid_students(self, _welcome_mock):
        rows = [
            ["이름", "학부모전화번호", "학생전화번호"],
            ["정상학생하나", "01070001111", "01090001234"],
            ["오류학생", "번호오류", "01090005678"],
            ["정상학생둘", "01070003333", "01090009876"],
        ]

        result = ExcelParsingService(_WorkbookStorage(rows)).run(
            self.job.job_id,
            {
                "file_key": "excel/partial.xlsx",
                "bucket": "academy-excel",
                "tenant_id": self.tenant.id,
                "password_mode": "fixed",
                "initial_password_secret": encrypt_excel_job_secret("selected-password"),
            },
        )

        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "DONE")
        self.assertEqual(result["created"], 2)
        self.assertEqual(result["total"], 3)
        self.assertEqual(
            result["failed"],
            [{
                "row": 3,
                "name": "오류학생",
                "error": "학부모 전화번호가 없거나 형식이 잘못되었습니다(010 포함 11자리).",
                "reason_code": "invalid_row",
                "conflict_student_id": None,
            }],
        )
        self.assertSetEqual(
            set(Student.objects.filter(tenant=self.tenant).values_list("name", flat=True)),
            {"정상학생하나", "정상학생둘"},
        )
