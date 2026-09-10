from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.core.models import Tenant
from apps.core.models.tenant_membership import TenantMembership
from apps.core.models.user import user_internal_username
from apps.domains.students.models import Student
from apps.domains.students.services import import_students_from_rows
from apps.domains.students.views import StudentViewSet


User = get_user_model()


def _tenant(*, name: str, code: str) -> Tenant:
    return Tenant.objects.create(name=name, code=code, is_active=True)


def _staff(*, tenant: Tenant, username: str):
    user = User.objects.create_user(
        username=username,
        password="test-password",
        tenant=tenant,
        is_staff=True,
        name=f"Staff-{username}",
    )
    TenantMembership.ensure_active(tenant=tenant, user=user, role="owner")
    return user


def _student(
    *,
    tenant: Tenant,
    ps_number: str,
    name: str,
    phone: str,
    parent_phone: str,
) -> Student:
    user = User.objects.create_user(
        username=user_internal_username(tenant, ps_number),
        password="test-password",
        tenant=tenant,
        phone=phone,
        name=name,
    )
    TenantMembership.ensure_active(tenant=tenant, user=user, role="student")
    return Student.objects.create(
        tenant=tenant,
        user=user,
        ps_number=ps_number,
        name=name,
        phone=phone,
        parent_phone=parent_phone,
        omr_code=phone.replace("-", "")[-8:],
    )


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class StudentImportResultContractTests(TestCase):
    def setUp(self):
        self.tenant = _tenant(name="Import Result Academy", code="import-result")

    def test_created_rows_preserve_excel_row_name_and_created_student_id(self):
        result = import_students_from_rows(
            tenant_id=self.tenant.id,
            students_data=[{
                "_excel_row": 7,
                "name": "합성학생A",
                "parent_phone": "01070000001",
                "phone": "01080000001",
                "school_type": "HIGH",
                "grade": 1,
            }],
            initial_password="test-password",
        )

        created = Student.objects.get(tenant=self.tenant, name="합성학생A")
        self.assertEqual(result["created"], 1)
        self.assertEqual(result["created_rows"], [{
            "row": 7,
            "name": "합성학생A",
            "student_id": created.id,
        }])

    @patch(
        "apps.domains.students.services.import_students.resolve_student_import_row",
        side_effect=RuntimeError("internal-db-detail-must-not-leak"),
    )
    def test_unexpected_row_exception_returns_safe_reason(self, _resolve_mock):
        result = import_students_from_rows(
            tenant_id=self.tenant.id,
            students_data=[{
                "_excel_row": 11,
                "name": "합성학생B",
                "parent_phone": "01070000002",
                "phone": "01080000002",
            }],
            initial_password="test-password",
        )

        self.assertEqual(result["failed"], [{
            "row": 11,
            "name": "합성학생B",
            "error": "처리 중 오류가 발생했습니다. 입력값을 확인한 뒤 다시 시도해 주세요.",
            "reason_code": "processing_error",
            "conflict_student_id": None,
        }])
        self.assertNotIn("internal-db-detail", str(result))


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class StudentPhoneSearchNormalizationTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.tenant = _tenant(name="Phone Search Academy", code="phone-search")
        self.other_tenant = _tenant(name="Other Academy", code="phone-search-other")
        self.admin = _staff(tenant=self.tenant, username="phone-search-admin")
        self.student_digits = _student(
            tenant=self.tenant,
            ps_number="PHONE-001",
            name="합성학생C",
            phone="01088424864",
            parent_phone="01071112222",
        )
        self.student_hyphens = _student(
            tenant=self.tenant,
            ps_number="PHONE-002",
            name="합성학생D",
            phone="010-9555-6666",
            parent_phone="010-7222-3333",
        )
        _student(
            tenant=self.other_tenant,
            ps_number="PHONE-003",
            name="타학원합성학생",
            phone="01088424864",
            parent_phone="01073334444",
        )

    def _list(self, params: dict[str, str]):
        request = self.factory.get("/api/v1/students/", params)
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant
        return StudentViewSet.as_view({"get": "list"})(request)

    @staticmethod
    def _ids(response) -> list[int]:
        return [row["id"] for row in response.data.get("results", response.data)]

    def test_general_search_matches_full_phone_with_or_without_hyphens(self):
        formatted = self._list({"search": "010-8842-4864"})
        digits = self._list({"search": "01095556666"})

        self.assertEqual(formatted.status_code, 200)
        self.assertEqual(self._ids(formatted), [self.student_digits.id])
        self.assertEqual(self._ids(digits), [self.student_hyphens.id])

    def test_phone_search_is_exact_and_tenant_scoped(self):
        response = self._list({"search": "010-8842-4864"})
        near_match = self._list({"search": "010-8842-4865"})

        self.assertEqual(self._ids(response), [self.student_digits.id])
        self.assertEqual(self._ids(near_match), [])

    def test_phone_filters_share_normalized_exact_match_contract(self):
        student_phone = self._list({"student_phone": "010-9555-6666"})
        parent_phone = self._list({"parent_phone": "01072223333"})

        self.assertEqual(self._ids(student_phone), [self.student_hyphens.id])
        self.assertEqual(self._ids(parent_phone), [self.student_hyphens.id])

    def test_non_phone_text_search_keeps_existing_name_behavior(self):
        response = self._list({"search": "합성학생C"})

        self.assertEqual(self._ids(response), [self.student_digits.id])
