"""수납 도메인 핵심 라이프사이클 단위테스트.

검증 범위:
- 부분납 → 추가납 → 완납 시 paid_amount/status 정합성
- 환불(cancel_payment) 시 paid_amount 차감 + status 되돌림
- idempotency_key 중복 호출 시 동일 payment 반환
- 시간 윈도우 중복 차단
- 초과 납부 / 취소 청구서 / 활성 수납 invoice 취소 등 가드
- mark_overdue_invoices 정확성
- 테넌트 격리 (cross-tenant payment 시도 차단)
"""
import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory, APITestCase, force_authenticate

from apps.core.models import Tenant, TenantMembership
from apps.domains.fees import services
from apps.domains.fees.models import (
    FeePayment,
    FeeTemplate,
    StudentFee,
    StudentInvoice,
    InvoiceItem,
)
from apps.domains.fees.services import (
    cancel_invoice,
    cancel_payment,
    generate_monthly_invoices,
    get_dashboard_stats,
    mark_overdue_invoices,
    record_payment,
)
from apps.domains.fees.views import StudentFeeInvoiceDetailView, StudentFeeInvoiceListView, StudentFeePaymentListView

Enrollment = apps.get_model("enrollment", "Enrollment")
Lecture = apps.get_model("lectures", "Lecture")
Parent = apps.get_model("parents", "Parent")
Student = apps.get_model("students", "Student")

User = get_user_model()


class FeesTestMixin:
    """fees 도메인용 fixture helper."""

    def make_tenant(self, code="t_fees"):
        return Tenant.objects.create(code=code, name=f"Academy {code}")

    def make_student(self, tenant, suffix=1):
        user = User.objects.create_user(
            username=f"student_fee_{tenant.code}_{suffix}",
            password="test1234",
        )
        return Student.objects.create(
            tenant=tenant, user=user,
            ps_number=f"PS{suffix:04d}",
            omr_code=f"OMR{suffix:04d}"[:8],
            name=f"학생{suffix}",
            parent_phone=f"010-0000-{suffix:04d}"[:13],
        )

    def make_fee_template(self, tenant, amount=100_000):
        return FeeTemplate.objects.create(
            tenant=tenant,
            name=f"수강료-{tenant.code}",
            fee_type=FeeTemplate.FeeType.TUITION,
            amount=amount,
        )

    def make_invoice(self, tenant, student, total=100_000, year=2026, month=4):
        invoice = StudentInvoice.objects.create(
            tenant=tenant,
            student=student,
            invoice_number=f"FEE-{year}-{month:02d}-{student.id:04d}",
            billing_year=year,
            billing_month=month,
            total_amount=total,
            due_date=timezone.localdate() + timedelta(days=10),
        )
        InvoiceItem.objects.create(
            tenant=tenant,
            invoice=invoice,
            description="수강료",
            amount=total,
        )
        return invoice


class PaymentLifecycleTest(FeesTestMixin, TestCase):
    """부분납 → 완납 → 환불 흐름 정합성."""

    def setUp(self):
        self.tenant = self.make_tenant()
        self.student = self.make_student(self.tenant)
        self.invoice = self.make_invoice(self.tenant, self.student, total=100_000)

    def test_partial_then_complete_updates_status(self):
        # 첫 부분납 30,000원
        record_payment(
            self.tenant, self.invoice.id, 30_000, "CASH",
            idempotency_key="key-1",
        )
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.paid_amount, 30_000)
        self.assertEqual(self.invoice.status, "PARTIAL")
        self.assertIsNone(self.invoice.paid_at)

        # 추가납 50,000원
        record_payment(
            self.tenant, self.invoice.id, 50_000, "BANK_TRANSFER",
            idempotency_key="key-2",
        )
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.paid_amount, 80_000)
        self.assertEqual(self.invoice.status, "PARTIAL")

        # 완납 20,000원
        record_payment(
            self.tenant, self.invoice.id, 20_000, "CASH",
            idempotency_key="key-3",
        )
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.paid_amount, 100_000)
        self.assertEqual(self.invoice.status, "PAID")
        self.assertIsNotNone(self.invoice.paid_at)

    @patch("apps.support.fees.service_dependencies.send_event_notification")
    def test_payment_occurrence_binds_invoice_and_payment_across_years(
        self,
        send_notification,
    ):
        with self.captureOnCommitCallbacks(execute=True):
            first_payment = record_payment(
                self.tenant,
                self.invoice.id,
                100_000,
                "CASH",
                idempotency_key="annual-payment-2026",
            )
        with self.captureOnCommitCallbacks(execute=True):
            retry = record_payment(
                self.tenant,
                self.invoice.id,
                100_000,
                "CASH",
                idempotency_key="annual-payment-2026",
            )

        next_invoice = self.make_invoice(
            self.tenant,
            self.student,
            total=100_000,
            year=2027,
            month=self.invoice.billing_month,
        )
        with self.captureOnCommitCallbacks(execute=True):
            second_payment = record_payment(
                self.tenant,
                next_invoice.id,
                100_000,
                "CASH",
                idempotency_key="annual-payment-2027",
            )

        self.assertEqual(retry.id, first_payment.id)
        self.assertEqual(send_notification.call_count, 2)
        occurrence_keys = [
            call.kwargs["context"]["_domain_object_id"]
            for call in send_notification.call_args_list
        ]
        self.assertEqual(
            occurrence_keys,
            [
                f"payment:{self.invoice.id}:{first_payment.id}",
                f"payment:{next_invoice.id}:{second_payment.id}",
            ],
        )

    def test_overpayment_blocked(self):
        record_payment(
            self.tenant, self.invoice.id, 60_000, "CASH",
            idempotency_key="part-1",
        )
        # 잔액 40,000원인데 50,000원 납부 시도
        with self.assertRaises(ValueError) as ctx:
            record_payment(
                self.tenant, self.invoice.id, 50_000, "CASH",
                idempotency_key="part-2",
            )
        self.assertIn("미납 잔액", str(ctx.exception))

    def test_payment_rejects_non_positive_non_integer_and_oversized_key(self):
        invalid_cases = [
            {"amount": 0, "idempotency_key": "zero"},
            {"amount": -1, "idempotency_key": "negative"},
            {"amount": True, "idempotency_key": "boolean"},
            {"amount": "1000", "idempotency_key": "string"},
            {"amount": 1_000, "idempotency_key": "x" * 101},
        ]

        for values in invalid_cases:
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    record_payment(
                        self.tenant,
                        self.invoice.id,
                        values["amount"],
                        "CASH",
                        idempotency_key=values["idempotency_key"],
                    )

        self.assertFalse(FeePayment.objects.filter(invoice=self.invoice).exists())

    def test_cancel_payment_restores_status(self):
        # 완납 후 환불(취소) → PARTIAL or PENDING으로 되돌림
        p1 = record_payment(
            self.tenant, self.invoice.id, 60_000, "CASH",
            idempotency_key="r1",
        )
        record_payment(
            self.tenant, self.invoice.id, 40_000, "CASH",
            idempotency_key="r2",
        )
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, "PAID")

        # p1 취소 → paid_amount 60_000 차감
        cancel_payment(self.tenant, p1.id)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.paid_amount, 40_000)
        self.assertEqual(self.invoice.status, "PARTIAL")
        self.assertIsNone(self.invoice.paid_at)

        # p1 다시 취소 시도 → ValueError
        with self.assertRaises(ValueError):
            cancel_payment(self.tenant, p1.id)

    def test_past_due_invoice_stays_overdue_during_payment_recalculation(self):
        StudentInvoice.objects.filter(pk=self.invoice.pk).update(
            due_date=timezone.localdate() - timedelta(days=1),
        )

        payment = record_payment(
            self.tenant,
            self.invoice.id,
            100_000,
            "CASH",
            idempotency_key="past-due-paid",
        )
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, "PAID")

        cancel_payment(self.tenant, payment.id)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.paid_amount, 0)
        self.assertEqual(self.invoice.status, "OVERDUE")

    def test_partial_payment_on_past_due_invoice_is_overdue_immediately(self):
        StudentInvoice.objects.filter(pk=self.invoice.pk).update(
            due_date=timezone.localdate() - timedelta(days=1),
        )

        record_payment(
            self.tenant,
            self.invoice.id,
            30_000,
            "CASH",
            idempotency_key="past-due-partial",
        )

        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.paid_amount, 30_000)
        self.assertEqual(self.invoice.status, "OVERDUE")


class StudentFeeParentSelectionTest(FeesTestMixin, TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.tenant = self.make_tenant(code="t_parent_fee")
        self.parent_user = User.objects.create_user(
            username="parent_fee_user",
            password="test1234",
            tenant=self.tenant,
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=self.parent_user, role="parent")
        self.parent = Parent.objects.create(
            tenant=self.tenant,
            user=self.parent_user,
            name="Parent",
            phone="01099990000",
        )
        self.student_a = self.make_student(self.tenant, suffix=101)
        self.student_b = self.make_student(self.tenant, suffix=102)
        self.student_a.parent = self.parent
        self.student_b.parent = self.parent
        self.student_a.save(update_fields=["parent"])
        self.student_b.save(update_fields=["parent"])
        self.invoice_a = self.make_invoice(self.tenant, self.student_a, total=100_000, year=2026, month=6)
        self.invoice_b = self.make_invoice(self.tenant, self.student_b, total=200_000, year=2026, month=6)

    def _request(self, path: str, student: Student):
        request = self.factory.get(path, HTTP_X_STUDENT_ID=str(student.id))
        force_authenticate(request, user=self.parent_user)
        request.tenant = self.tenant
        return request

    def test_parent_invoice_list_requires_explicit_child_header(self):
        request = self.factory.get("/student/fees/invoices/")
        force_authenticate(request, user=self.parent_user)
        request.tenant = self.tenant

        response = StudentFeeInvoiceListView.as_view()(request)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(str(response.data["detail"]), "자녀를 선택한 뒤 다시 시도해 주세요.")

    def test_parent_invoice_list_uses_selected_child_only(self):
        response = StudentFeeInvoiceListView.as_view()(
            self._request("/student/fees/invoices/", self.student_a)
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.data], [self.invoice_a.id])

    def test_parent_invoice_detail_rejects_other_child_when_selected_child_differs(self):
        response = StudentFeeInvoiceDetailView.as_view()(
            self._request(f"/student/fees/invoices/{self.invoice_b.id}/", self.student_a),
            pk=self.invoice_b.id,
        )

        self.assertEqual(response.status_code, 404)

    def test_parent_payment_list_uses_selected_child_only(self):
        payment_a = record_payment(self.tenant, self.invoice_a.id, 100_000, "CASH", idempotency_key="child-a")
        record_payment(self.tenant, self.invoice_b.id, 200_000, "CASH", idempotency_key="child-b")

        response = StudentFeePaymentListView.as_view()(
            self._request("/student/fees/payments/", self.student_a)
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.data], [payment_a.id])


class IdempotencyTest(FeesTestMixin, TestCase):
    """idempotency_key + 시간 윈도우 중복 방지."""

    def setUp(self):
        self.tenant = self.make_tenant(code="t_idem")
        self.student = self.make_student(self.tenant)
        self.invoice = self.make_invoice(self.tenant, self.student, total=100_000)

    def test_idempotency_key_returns_same_payment(self):
        p1 = record_payment(
            self.tenant, self.invoice.id, 50_000, "CASH",
            idempotency_key="dup-key",
        )
        # 동일 키로 재호출 → 새로 생성하지 않고 기존 반환
        p2 = record_payment(
            self.tenant, self.invoice.id, 50_000, "CASH",
            idempotency_key="dup-key",
        )
        self.assertEqual(p1.id, p2.id)
        self.assertEqual(
            FeePayment.objects.filter(invoice=self.invoice).count(), 1
        )
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.paid_amount, 50_000)

    def test_window_dedup_blocks_recent_duplicate(self):
        # 키 없이 동일 invoice/금액/수단 빠른 중복 → ValueError
        record_payment(
            self.tenant, self.invoice.id, 30_000, "CASH",
        )
        with self.assertRaises(ValueError) as ctx:
            record_payment(
                self.tenant, self.invoice.id, 30_000, "CASH",
            )
        self.assertIn("중복", str(ctx.exception))

    def test_idempotency_constraint_prevents_db_duplicate(self):
        # DB 레벨 partial unique constraint도 살아 있는지 검증.
        record_payment(
            self.tenant, self.invoice.id, 50_000, "CASH",
            idempotency_key="hard-key",
        )
        # 직접 INSERT로 동일 key 재시도 → IntegrityError
        with self.assertRaises(IntegrityError):
            FeePayment.objects.create(
                tenant=self.tenant,
                invoice=self.invoice,
                student=self.student,
                amount=10_000,
                payment_method="CASH",
                paid_at=timezone.now(),
                idempotency_key="hard-key",
            )

    def test_cancelled_idempotency_key_returns_domain_error(self):
        payment = record_payment(
            self.tenant,
            self.invoice.id,
            50_000,
            "CASH",
            idempotency_key="cancelled-key",
        )
        cancel_payment(self.tenant, payment.id)

        with self.assertRaises(ValueError) as ctx:
            record_payment(
                self.tenant,
                self.invoice.id,
                50_000,
                "CASH",
                idempotency_key="cancelled-key",
            )

        self.assertIn("이미 사용", str(ctx.exception))

    def test_idempotency_key_rejects_different_payment_parameters(self):
        record_payment(
            self.tenant,
            self.invoice.id,
            50_000,
            "CASH",
            idempotency_key="parameter-key",
        )

        with self.assertRaises(ValueError) as ctx:
            record_payment(
                self.tenant,
                self.invoice.id,
                40_000,
                "BANK_TRANSFER",
                idempotency_key="parameter-key",
            )

        self.assertIn("요청 정보", str(ctx.exception))


class InvoiceGenerationSnapshotTest(FeesTestMixin, TestCase):
    def test_generation_reloads_amount_and_discount_after_candidate_snapshot(self):
        tenant = self.make_tenant(code="t_generation_snapshot")
        student = self.make_student(tenant)
        tuition_template = self.make_fee_template(tenant, amount=100_000)
        tuition_fee = StudentFee.objects.create(
            tenant=tenant,
            student=student,
            fee_template=tuition_template,
        )
        material_template = FeeTemplate.objects.create(
            tenant=tenant,
            name="교재비-snapshot",
            fee_type=FeeTemplate.FeeType.TEXTBOOK,
            amount=50_000,
        )
        material_fee = StudentFee.objects.create(
            tenant=tenant,
            student=student,
            fee_template=material_template,
        )
        original_groupby = services.groupby

        def mutate_after_snapshot(*args, **kwargs):
            FeeTemplate.objects.filter(id=tuition_template.id).update(amount=140_000)
            StudentFee.objects.filter(id=tuition_fee.id).update(discount_amount=20_000)
            StudentFee.objects.filter(id=material_fee.id).update(
                adjusted_amount=80_000,
                discount_amount=5_000,
            )
            return original_groupby(*args, **kwargs)

        with patch(
            "apps.domains.fees.services.groupby",
            side_effect=mutate_after_snapshot,
        ):
            result = generate_monthly_invoices(
                tenant,
                billing_year=2026,
                billing_month=5,
                due_date=timezone.localdate() + timedelta(days=10),
            )

        self.assertEqual(result["created"], 1, result)
        invoice = StudentInvoice.objects.get(
            tenant=tenant,
            student=student,
            billing_year=2026,
            billing_month=5,
        )
        self.assertEqual(invoice.total_amount, 195_000)

    def test_legacy_malformed_month_does_not_block_valid_fee(self):
        tenant = self.make_tenant(code="t_generation_legacy_month")
        student = self.make_student(tenant)
        valid_template = self.make_fee_template(tenant, amount=100_000)
        StudentFee.objects.create(
            tenant=tenant,
            student=student,
            fee_template=valid_template,
        )
        malformed_start_template = FeeTemplate.objects.create(
            tenant=tenant,
            name="레거시 시작월 형식 비목",
            fee_type=FeeTemplate.FeeType.TEXTBOOK,
            amount=30_000,
        )
        StudentFee.objects.create(
            tenant=tenant,
            student=student,
            fee_template=malformed_start_template,
            billing_start_month="2026-00",
        )
        malformed_end_template = FeeTemplate.objects.create(
            tenant=tenant,
            name="레거시 종료월 형식 비목",
            fee_type=FeeTemplate.FeeType.TEXTBOOK,
            amount=40_000,
        )
        StudentFee.objects.create(
            tenant=tenant,
            student=student,
            fee_template=malformed_end_template,
            billing_end_month="2026-99",
        )

        result = generate_monthly_invoices(
            tenant,
            billing_year=2026,
            billing_month=5,
            due_date=timezone.localdate() + timedelta(days=10),
        )

        self.assertEqual(result["created"], 1, result)
        self.assertEqual(result["skipped"], 0, result)
        self.assertEqual(len(result["errors"]), 2, result)
        self.assertTrue(all("유효하지 않은 청구월" in error for error in result["errors"]))
        invoice = StudentInvoice.objects.get(
            tenant=tenant,
            student=student,
            billing_year=2026,
            billing_month=5,
        )
        self.assertEqual(invoice.total_amount, 100_000)
        self.assertEqual(
            list(invoice.items.values_list("fee_template_id", flat=True)),
            [valid_template.id],
        )

    def test_auto_assignment_uses_student_scope_lock(self):
        tenant = self.make_tenant(code="t_auto_assignment_lock")
        student = self.make_student(tenant)
        lecture = Lecture.objects.create(
            tenant=tenant,
            title="자동 비용 배정 강의",
            name="자동 비용 배정 강의",
            subject="MATH",
        )
        template = FeeTemplate.objects.create(
            tenant=tenant,
            lecture=lecture,
            name="자동 배정 수강료",
            fee_type=FeeTemplate.FeeType.TUITION,
            amount=100_000,
            auto_assign=True,
        )
        enrollment = Enrollment.objects.create(
            tenant=tenant,
            student=student,
            lecture=lecture,
            status="ACTIVE",
        )

        with patch(
            "apps.domains.fees.services.lock_student_fee_assignment_scopes",
            wraps=services.lock_student_fee_assignment_scopes,
        ) as lock_scope:
            created = services.auto_assign_fees_on_enrollment(
                tenant,
                student,
                lecture,
                enrollment,
            )

        self.assertEqual(created, 1)
        lock_scope.assert_called_once_with(tenant=tenant, student_ids=[student.id])
        self.assertTrue(
            StudentFee.objects.filter(
                tenant=tenant,
                student=student,
                fee_template=template,
            ).exists(),
        )


class FeesOpenApiContractTest(SimpleTestCase):
    def setUp(self):
        schema_path = Path(__file__).resolve().parents[4] / "schema" / "openapi.json"
        self.schema = json.loads(schema_path.read_text(encoding="utf-8"))

    def _json_request_ref(self, path, method="post"):
        return self.schema["paths"][path][method]["requestBody"]["content"][
            "application/json"
        ]["schema"]["$ref"]

    def _response_ref(self, path, status_code, method="post"):
        return self.schema["paths"][path][method]["responses"][status_code]["content"][
            "application/json"
        ]["schema"]["$ref"]

    def test_fee_write_operations_publish_actual_request_and_response_contracts(self):
        generate_path = "/api/v1/fees/invoices/generate/"
        payment_path = "/api/v1/fees/payments/"
        cancel_path = "/api/v1/fees/payments/{id}/cancel/"

        self.assertEqual(
            self._json_request_ref(generate_path),
            "#/components/schemas/GenerateInvoicesRequest",
        )
        self.assertEqual(
            self._response_ref(generate_path, "200"),
            "#/components/schemas/GenerateInvoicesResult",
        )
        self.assertEqual(
            self._json_request_ref(payment_path),
            "#/components/schemas/RecordPaymentRequest",
        )
        self.assertEqual(
            self._response_ref(payment_path, "201"),
            "#/components/schemas/FeePayment",
        )
        self.assertNotIn("requestBody", self.schema["paths"][cancel_path]["post"])
        self.assertEqual(
            self._response_ref(cancel_path, "200"),
            "#/components/schemas/FeePayment",
        )

        bulk_assign_path = "/api/v1/fees/student-fees/bulk-assign/"
        invoice_detail_path = "/api/v1/fees/invoices/{id}/"
        self.assertEqual(
            self._json_request_ref(bulk_assign_path),
            "#/components/schemas/StudentFeeBulkAssignRequest",
        )
        self.assertEqual(
            self._response_ref(bulk_assign_path, "200"),
            "#/components/schemas/StudentFeeBulkAssignResult",
        )
        self.assertEqual(
            self._json_request_ref(invoice_detail_path, method="patch"),
            "#/components/schemas/PatchedStudentInvoiceUpdateRequest",
        )
        self.assertEqual(
            self._response_ref(invoice_detail_path, "200", method="patch"),
            "#/components/schemas/StudentInvoiceDetail",
        )


class InvoiceCancelGuardTest(FeesTestMixin, TestCase):
    """청구서 취소 가드: 활성 수납 있으면 취소 불가."""

    def setUp(self):
        self.tenant = self.make_tenant(code="t_cancel")
        self.student = self.make_student(self.tenant)
        self.invoice = self.make_invoice(self.tenant, self.student, total=50_000)

    def test_cancel_invoice_with_active_payment_raises(self):
        record_payment(
            self.tenant, self.invoice.id, 20_000, "CASH",
            idempotency_key="any",
        )
        with self.assertRaises(ValueError) as ctx:
            cancel_invoice(self.tenant, self.invoice.id)
        self.assertIn("수납을 먼저 취소", str(ctx.exception))

    def test_cancel_invoice_without_payment_succeeds(self):
        cancel_invoice(self.tenant, self.invoice.id)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, "CANCELLED")

    def test_payment_blocked_on_cancelled_invoice(self):
        cancel_invoice(self.tenant, self.invoice.id)
        with self.assertRaises(ValueError):
            record_payment(
                self.tenant, self.invoice.id, 10_000, "CASH",
                idempotency_key="cancelled",
            )

    def test_cancelled_invoice_can_be_reissued_for_same_student_and_period(self):
        template = self.make_fee_template(self.tenant, amount=50_000)
        StudentFee.objects.create(
            tenant=self.tenant,
            student=self.student,
            fee_template=template,
        )
        cancel_invoice(self.tenant, self.invoice.id)

        result = generate_monthly_invoices(
            self.tenant,
            billing_year=self.invoice.billing_year,
            billing_month=self.invoice.billing_month,
            due_date=timezone.localdate() + timedelta(days=10),
        )

        self.assertEqual(result, {"created": 1, "skipped": 0, "errors": []})
        invoices = list(
            StudentInvoice.objects.filter(
                tenant=self.tenant,
                student=self.student,
                billing_year=self.invoice.billing_year,
                billing_month=self.invoice.billing_month,
            ).order_by("id")
        )
        self.assertEqual(len(invoices), 2)
        self.assertEqual(invoices[0].status, "CANCELLED")
        self.assertEqual(invoices[1].status, "PENDING")
        self.assertNotEqual(invoices[0].invoice_number, invoices[1].invoice_number)


class FeeDashboardQueryTest(FeesTestMixin, TestCase):
    def test_dashboard_uses_one_invoice_aggregate_and_one_item_aggregate(self):
        tenant = self.make_tenant(code="t_dashboard_queries")
        pending = self.make_invoice(tenant, self.make_student(tenant, suffix=201), total=100_000)
        overdue = self.make_invoice(tenant, self.make_student(tenant, suffix=202), total=70_000)
        paid = self.make_invoice(tenant, self.make_student(tenant, suffix=203), total=30_000)
        StudentInvoice.objects.filter(pk=overdue.pk).update(status="OVERDUE", paid_amount=20_000)
        StudentInvoice.objects.filter(pk=paid.pk).update(status="PAID", paid_amount=30_000)

        with self.assertNumQueries(2):
            stats = get_dashboard_stats(tenant, pending.billing_year, pending.billing_month)

        self.assertEqual(stats["total_billed"], 200_000)
        self.assertEqual(stats["total_paid"], 50_000)
        self.assertEqual(stats["total_outstanding"], 150_000)
        self.assertEqual(stats["overdue_count"], 1)
        self.assertEqual(stats["pending_count"], 1)
        self.assertEqual(stats["paid_count"], 1)
        self.assertEqual(stats["invoice_count"], 3)


class OverdueTest(FeesTestMixin, TestCase):
    """납부 기한 경과 청구서 → OVERDUE 일괄 처리."""

    def test_mark_overdue_only_pending_or_partial(self):
        tenant = self.make_tenant(code="t_overdue")
        student = self.make_student(tenant)
        past = timezone.localdate() - timedelta(days=5)
        future = timezone.localdate() + timedelta(days=5)

        # 미납·기한 경과 → OVERDUE 대상
        inv_overdue = self.make_invoice(tenant, student, year=2026, month=1)
        StudentInvoice.objects.filter(id=inv_overdue.id).update(due_date=past)

        # 부분납·기한 경과 → OVERDUE 대상
        inv_partial = self.make_invoice(tenant, student, year=2026, month=2)
        StudentInvoice.objects.filter(id=inv_partial.id).update(due_date=past)
        record_payment(
            tenant, inv_partial.id, 30_000, "CASH",
            idempotency_key="part-overdue",
        )
        inv_partial.refresh_from_db()
        self.assertEqual(inv_partial.status, "OVERDUE")

        # 기한 미도래 → 제외
        inv_future = self.make_invoice(tenant, student, year=2026, month=3)
        StudentInvoice.objects.filter(id=inv_future.id).update(due_date=future)

        # 완납 → 제외
        inv_paid = self.make_invoice(tenant, student, total=10_000, year=2026, month=5)
        StudentInvoice.objects.filter(id=inv_paid.id).update(due_date=past)
        record_payment(
            tenant, inv_paid.id, 10_000, "CASH",
            idempotency_key="paid-overdue",
        )

        updated = mark_overdue_invoices(tenant)
        self.assertEqual(updated, 1)

        inv_overdue.refresh_from_db()
        inv_partial.refresh_from_db()
        inv_future.refresh_from_db()
        inv_paid.refresh_from_db()
        self.assertEqual(inv_overdue.status, "OVERDUE")
        self.assertEqual(inv_partial.status, "OVERDUE")
        self.assertEqual(inv_future.status, "PENDING")
        self.assertEqual(inv_paid.status, "PAID")


class TenantIsolationTest(FeesTestMixin, TestCase):
    """수납이 절대 cross-tenant로 새지 않도록."""

    def test_record_payment_blocks_other_tenant(self):
        t_a = self.make_tenant(code="t_a")
        t_b = self.make_tenant(code="t_b")
        student_a = self.make_student(t_a, suffix=1)
        invoice_a = self.make_invoice(t_a, student_a, total=50_000)

        # t_b 컨텍스트로 t_a invoice를 결제 시도 → DoesNotExist
        with self.assertRaises(StudentInvoice.DoesNotExist):
            record_payment(
                t_b, invoice_a.id, 10_000, "CASH",
                idempotency_key="cross",
            )

    def test_cancel_payment_blocks_other_tenant(self):
        t_a = self.make_tenant(code="t_a2")
        t_b = self.make_tenant(code="t_b2")
        student_a = self.make_student(t_a, suffix=1)
        invoice_a = self.make_invoice(t_a, student_a, total=50_000)
        payment = record_payment(
            t_a, invoice_a.id, 10_000, "CASH",
            idempotency_key="key",
        )
        with self.assertRaises(FeePayment.DoesNotExist):
            cancel_payment(t_b, payment.id)


class FeesInvoiceApiHardeningTest(FeesTestMixin, APITestCase):
    """HTTP 경계에서 청구서/수납 무결성 필드가 변조되지 않도록 검증."""

    def setUp(self):
        self.tenant = self.make_tenant(code="t_fee_api")
        program = self.tenant.program
        program.feature_flags = {"fee_management": True}
        program.save(update_fields=["feature_flags"])

        self.staff = User.objects.create_user(
            username="fee_api_staff",
            password="test1234!",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.objects.create(
            user=self.staff,
            tenant=self.tenant,
            role="admin",
            is_active=True,
        )
        self.client.force_authenticate(user=self.staff)
        self.headers = {"HTTP_HOST": "localhost", "HTTP_X_TENANT_CODE": self.tenant.code}

        self.student = self.make_student(self.tenant, suffix=1)
        self.other_student = self.make_student(self.tenant, suffix=2)
        self.invoice = self.make_invoice(self.tenant, self.student, total=100_000)

    def test_invoice_patch_cannot_forge_financial_or_identity_fields(self):
        url = f"/api/v1/fees/invoices/{self.invoice.id}/"
        new_due_date = (timezone.localdate() + timedelta(days=20)).isoformat()

        resp = self.client.patch(
            url,
            data={
                "memo": "safe memo",
                "due_date": new_due_date,
                "paid_amount": 100_000,
                "total_amount": 1,
                "status": "PAID",
                "student": self.other_student.id,
            },
            format="json",
            **self.headers,
        )

        self.assertEqual(resp.status_code, 200, resp.content)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.memo, "safe memo")
        self.assertEqual(self.invoice.due_date.isoformat(), new_due_date)
        self.assertEqual(self.invoice.paid_amount, 0)
        self.assertEqual(self.invoice.total_amount, 100_000)
        self.assertEqual(self.invoice.status, "PENDING")
        self.assertEqual(self.invoice.student_id, self.student.id)

    def test_invoice_patch_recalculates_status_from_due_date(self):
        url = f"/api/v1/fees/invoices/{self.invoice.id}/"
        past_due_date = (timezone.localdate() - timedelta(days=1)).isoformat()
        future_due_date = (timezone.localdate() + timedelta(days=1)).isoformat()

        overdue = self.client.patch(
            url,
            data={"due_date": past_due_date},
            format="json",
            **self.headers,
        )
        self.assertEqual(overdue.status_code, 200, overdue.content)
        self.assertEqual(overdue.data["status"], "OVERDUE")

        pending = self.client.patch(
            url,
            data={"due_date": future_due_date},
            format="json",
            **self.headers,
        )
        self.assertEqual(pending.status_code, 200, pending.content)
        self.assertEqual(pending.data["status"], "PENDING")

    def test_direct_invoice_create_is_not_available(self):
        before = StudentInvoice.objects.filter(tenant=self.tenant).count()

        response = self.client.post(
            "/api/v1/fees/invoices/",
            data={
                "invoice_number": "FORGED-INVOICE",
                "student": self.student.id,
                "billing_year": 2026,
                "billing_month": 8,
                "total_amount": 1,
                "paid_amount": 1,
                "status": "PAID",
                "due_date": timezone.localdate().isoformat(),
            },
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 405, response.content)
        self.assertEqual(StudentInvoice.objects.filter(tenant=self.tenant).count(), before)

    def test_teacher_role_cannot_access_fee_management_api(self):
        teacher = User.objects.create_user(
            username="fee_api_teacher",
            password="test1234!",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.objects.create(
            user=teacher,
            tenant=self.tenant,
            role="teacher",
            is_active=True,
        )
        self.client.force_authenticate(user=teacher)

        resp = self.client.get("/api/v1/fees/invoices/", **self.headers)

        self.assertEqual(resp.status_code, 403)

    def test_fee_management_is_available_regardless_of_legacy_flag(self):
        program = self.tenant.program
        program.feature_flags = {"fee_management": False}
        program.save(update_fields=["feature_flags"])

        resp = self.client.get("/api/v1/fees/invoices/", **self.headers)

        self.assertEqual(resp.status_code, 200)

    def test_student_fee_create_rejects_same_tenant_inconsistent_enrollment(self):
        lecture = Lecture.objects.create(
            tenant=self.tenant,
            title="Fee Lecture",
            name="Fee Lecture",
            subject="MATH",
        )
        other_lecture = Lecture.objects.create(
            tenant=self.tenant,
            title="Other Fee Lecture",
            name="Other Fee Lecture",
            subject="MATH",
        )
        template = FeeTemplate.objects.create(
            tenant=self.tenant,
            lecture=lecture,
            name="수강료",
            fee_type=FeeTemplate.FeeType.TUITION,
            billing_cycle=FeeTemplate.BillingCycle.MONTHLY,
            amount=100_000,
        )
        other_enrollment = Enrollment.objects.create(
            tenant=self.tenant,
            student=self.other_student,
            lecture=lecture,
            status="ACTIVE",
        )
        mismatched_lecture_enrollment = Enrollment.objects.create(
            tenant=self.tenant,
            student=self.student,
            lecture=other_lecture,
            status="ACTIVE",
        )

        student_mismatch = self.client.post(
            "/api/v1/fees/student-fees/",
            data={
                "student": self.student.id,
                "fee_template": template.id,
                "enrollment": other_enrollment.id,
                "is_active": True,
            },
            format="json",
            **self.headers,
        )
        lecture_mismatch = self.client.post(
            "/api/v1/fees/student-fees/",
            data={
                "student": self.student.id,
                "fee_template": template.id,
                "enrollment": mismatched_lecture_enrollment.id,
                "is_active": True,
            },
            format="json",
            **self.headers,
        )

        self.assertEqual(student_mismatch.status_code, 400)
        self.assertEqual(lecture_mismatch.status_code, 400)

    def test_student_fee_create_uses_student_scope_lock(self):
        template = self.make_fee_template(self.tenant)

        with patch(
            "apps.domains.fees.services.lock_student_fee_assignment_scopes",
            wraps=services.lock_student_fee_assignment_scopes,
        ) as lock_scope:
            response = self.client.post(
                "/api/v1/fees/student-fees/",
                data={
                    "student": self.student.id,
                    "fee_template": template.id,
                    "is_active": True,
                },
                format="json",
                **self.headers,
            )

        self.assertEqual(response.status_code, 201, response.content)
        lock_scope.assert_called_once_with(
            tenant=self.tenant,
            student_ids=[self.student.id],
        )

    def test_payment_create_passes_idempotency_key_and_does_not_double_increment(self):
        payload = {
            "invoice_id": self.invoice.id,
            "amount": 40_000,
            "payment_method": "CASH",
            "idempotency_key": "api-payment-key-1",
        }

        first = self.client.post("/api/v1/fees/payments/", data=payload, format="json", **self.headers)
        second = self.client.post("/api/v1/fees/payments/", data=payload, format="json", **self.headers)

        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(second.status_code, 201, second.content)
        self.assertEqual(FeePayment.objects.filter(invoice=self.invoice).count(), 1)
        payment = FeePayment.objects.get(invoice=self.invoice)
        self.assertEqual(payment.idempotency_key, "api-payment-key-1")
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.paid_amount, 40_000)
        self.assertEqual(self.invoice.status, "PARTIAL")

    def test_payment_receipt_note_honors_database_length(self):
        response = self.client.post(
            "/api/v1/fees/payments/",
            data={
                "invoice_id": self.invoice.id,
                "amount": 40_000,
                "payment_method": "CASH",
                "receipt_note": "x" * 301,
                "idempotency_key": "receipt-note-too-long",
            },
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("receipt_note", response.data)

    def test_student_fee_rejects_invalid_billing_periods(self):
        invalid_periods = [
            {"billing_start_month": "2026-13"},
            {"billing_start_month": "not-a-month"},
            {
                "billing_start_month": "2026-09",
                "billing_end_month": "2026-08",
            },
        ]

        for index, values in enumerate(invalid_periods, start=1):
            with self.subTest(values=values):
                template = FeeTemplate.objects.create(
                    tenant=self.tenant,
                    name=f"기간 검증 비목 {index}",
                    fee_type=FeeTemplate.FeeType.TUITION,
                    amount=100_000,
                )
                response = self.client.post(
                    "/api/v1/fees/student-fees/",
                    data={
                        "student": self.student.id,
                        "fee_template": template.id,
                        "is_active": True,
                        **values,
                    },
                    format="json",
                    **self.headers,
                )
                self.assertEqual(response.status_code, 400, response.content)

    def test_bulk_assign_reactivates_existing_student_fee(self):
        template = self.make_fee_template(self.tenant)
        student_fee = StudentFee.objects.create(
            tenant=self.tenant,
            student=self.student,
            fee_template=template,
            is_active=False,
            billing_end_month="2026-07",
        )

        with patch(
            "apps.domains.fees.services.lock_student_fee_assignment_scopes",
            wraps=services.lock_student_fee_assignment_scopes,
        ) as lock_scope:
            response = self.client.post(
                "/api/v1/fees/student-fees/bulk-assign/",
                data={
                    "student_ids": [self.student.id],
                    "fee_template_id": template.id,
                },
                format="json",
                **self.headers,
            )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["created"], 1)
        self.assertEqual(response.data["skipped"], 0)
        student_fee.refresh_from_db()
        self.assertTrue(student_fee.is_active)
        self.assertEqual(student_fee.billing_end_month, "")
        lock_scope.assert_called_once_with(
            tenant=self.tenant,
            student_ids=[self.student.id],
        )

    def test_invoice_delete_with_active_payment_returns_400_not_500(self):
        record_payment(
            self.tenant,
            self.invoice.id,
            40_000,
            "CASH",
            idempotency_key="api-delete-guard",
        )

        resp = self.client.delete(f"/api/v1/fees/invoices/{self.invoice.id}/", **self.headers)

        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("수납을 먼저 취소", resp.data["detail"])
        self.invoice.refresh_from_db()
        self.assertNotEqual(self.invoice.status, "CANCELLED")
