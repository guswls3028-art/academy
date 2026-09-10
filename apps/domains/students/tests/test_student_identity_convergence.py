from django.contrib.auth.hashers import make_password
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.core.models import Tenant, TenantMembership
from apps.core.models.user import user_internal_username
from apps.domains.students.models import Student, StudentRegistrationRequest
from apps.domains.students.services import approve_registration_request, resolve_student_import_row
from apps.domains.students.services.identity import derive_student_omr_code
from apps.domains.students.views import StudentViewSet

User = get_user_model()


def make_tenant(code="identity-convergence"):
    return Tenant.objects.create(name=f"Tenant {code}", code=code, is_active=True)


def make_admin(tenant):
    user = User.objects.create_user(
        username=f"admin-{tenant.code}",
        password="test1234",
        tenant=tenant,
        is_staff=True,
        name="관리자",
    )
    TenantMembership.ensure_active(tenant=tenant, user=user, role="owner")
    return user


def make_student(
    tenant,
    *,
    ps_number="IDENTITY-001",
    phone="01011112222",
    parent_phone="01033334444",
):
    user = User.objects.create_user(
        username=user_internal_username(tenant, ps_number),
        password="test1234",
        tenant=tenant,
        phone=phone or "",
        name="학생",
    )
    student = Student.objects.create(
        tenant=tenant,
        user=user,
        ps_number=ps_number,
        name="학생",
        phone=phone or None,
        parent_phone=parent_phone,
        omr_code=derive_student_omr_code(phone=phone, parent_phone=parent_phone),
        school_type="HIGH",
        grade=1,
    )
    TenantMembership.ensure_active(tenant=tenant, user=user, role="student")
    return student


class StudentIdentityConvergenceTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.tenant = make_tenant()
        self.admin = make_admin(self.tenant)

    def test_admin_create_without_student_phone_uses_parent_tail8_identity(self):
        request = self.factory.post(
            "/api/v1/students/",
            data={
                "name": "부모번호식별학생",
                "parent_phone": "01088887777",
                "initial_password": "test1234",
                "school_type": "HIGH",
                "grade": 1,
            },
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant

        response = StudentViewSet.as_view({"post": "create"})(request)

        self.assertEqual(response.status_code, 201)
        student = Student.objects.get(tenant=self.tenant, name="부모번호식별학생")
        self.assertIsNone(student.phone)
        self.assertTrue(student.uses_identifier)
        self.assertEqual(student.omr_code, "88887777")
        self.assertTrue(student.ps_number)
        self.assertTrue(student.user.username.startswith(f"t{self.tenant.id}_"))

    def test_admin_create_matching_contacts_keeps_parent_phone_only(self):
        request = self.factory.post(
            "/api/v1/students/",
            data={
                "name": "동일번호무휴대폰학생",
                "phone": "01088887777",
                "parent_phone": "01088887777",
                "initial_password": "test1234",
                "school_type": "HIGH",
                "grade": 1,
            },
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant

        response = StudentViewSet.as_view({"post": "create"})(request)

        self.assertEqual(response.status_code, 201, response.data)
        student = Student.objects.get(tenant=self.tenant, name="동일번호무휴대폰학생")
        student.user.refresh_from_db()
        self.assertIsNone(student.phone)
        self.assertFalse(student.user.phone)
        self.assertTrue(student.uses_identifier)
        self.assertNotEqual(student.ps_number, student.parent_phone)
        self.assertEqual(student.omr_code, "88887777")

    def test_admin_create_rejects_explicit_parent_phone_as_student_login_id(self):
        request = self.factory.post(
            "/api/v1/students/",
            data={
                "name": "학부모아이디충돌학생",
                "parent_phone": "01088887777",
                "ps_number": "010-8888-7777",
                "no_phone": True,
                "initial_password": "test1234",
                "school_type": "HIGH",
                "grade": 1,
            },
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant

        response = StudentViewSet.as_view({"post": "create"})(request)

        self.assertEqual(response.status_code, 400)
        self.assertIn("ps_number", response.data)
        self.assertFalse(
            Student.objects.filter(
                tenant=self.tenant,
                name="학부모아이디충돌학생",
            ).exists()
        )

    def test_admin_create_rejects_student_phone_login_collision_without_generated_fallback(self):
        phone = "01066667777"
        conflicting_user = User.objects.create_user(
            username=user_internal_username(self.tenant, phone),
            tenant=self.tenant,
            password="staff-password",
            name="기존 사용자",
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=conflicting_user,
            role="teacher",
        )
        request = self.factory.post(
            "/api/v1/students/",
            data={
                "name": "충돌학생",
                "phone": phone,
                "parent_phone": "01088887777",
                "initial_password": "test1234",
                "school_type": "HIGH",
                "grade": 1,
            },
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant

        response = StudentViewSet.as_view({"post": "create"})(request)

        self.assertEqual(response.status_code, 400)
        self.assertIn("ps_number", response.data)
        self.assertFalse(
            Student.objects.filter(tenant=self.tenant, name="충돌학생").exists()
        )

    def test_admin_update_clearing_student_phone_recomputes_omr_from_parent_without_fake_phone(self):
        student = make_student(
            self.tenant,
            phone="01012345678",
            parent_phone="01099998888",
        )
        self.assertEqual(student.omr_code, "12345678")

        request = self.factory.patch(
            f"/api/v1/students/{student.id}/",
            data={"phone": None, "uses_identifier": True},
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant

        response = StudentViewSet.as_view({"patch": "partial_update"})(request, pk=student.id)

        self.assertEqual(response.status_code, 200)
        student.refresh_from_db()
        student.user.refresh_from_db()
        self.assertIsNone(student.phone)
        self.assertIsNone(student.user.phone)
        self.assertTrue(student.uses_identifier)
        self.assertEqual(student.omr_code, "99998888")

    def test_registration_approval_without_student_phone_uses_same_parent_tail8_identity(self):
        reg = StudentRegistrationRequest.objects.create(
            tenant=self.tenant,
            status=StudentRegistrationRequest.PENDING,
            initial_password=make_password("rawpw1234"),
            initial_password_plain="",
            name="가입부모번호학생",
            username="",
            parent_phone="01077776666",
            phone=None,
            school_type="HIGH",
            high_school="테스트고",
            origin_middle_school="테스트중",
            grade=1,
            gender="M",
            address="서울",
        )

        result = approve_registration_request(tenant=self.tenant, registration_id=reg.id)

        result.student.refresh_from_db()
        self.assertIsNone(result.student.phone)
        self.assertTrue(result.student.uses_identifier)
        self.assertEqual(result.student.omr_code, "77776666")

    def test_registration_approval_matching_contacts_keeps_parent_phone_only(self):
        reg = StudentRegistrationRequest.objects.create(
            tenant=self.tenant,
            status=StudentRegistrationRequest.PENDING,
            initial_password=make_password("rawpw1234"),
            initial_password_plain="",
            name="가입동일번호학생",
            username="",
            parent_phone="01077776666",
            phone="01077776666",
            school_type="HIGH",
            high_school="테스트고",
            origin_middle_school="테스트중",
            grade=1,
            gender="M",
            address="서울",
        )

        result = approve_registration_request(tenant=self.tenant, registration_id=reg.id)

        result.student.refresh_from_db()
        result.student.user.refresh_from_db()
        self.assertIsNone(result.student.phone)
        self.assertFalse(result.student.user.phone)
        self.assertTrue(result.student.uses_identifier)
        self.assertNotEqual(result.student.ps_number, result.student.parent_phone)
        self.assertEqual(result.notice.student_phone, "")
        self.assertEqual(result.notice.student_id, result.student.ps_number)

    def test_excel_import_without_student_phone_uses_same_parent_tail8_identity(self):
        result = resolve_student_import_row(
            self.tenant,
            {
                "name": "엑셀부모번호학생",
                "parent_phone": "01055554444",
                "phone": "",
                "school_type": "HIGH",
                "grade": 1,
            },
            "test1234",
        )

        self.assertTrue(result.created)
        result.student.refresh_from_db()
        self.assertIsNone(result.student.phone)
        self.assertTrue(result.student.uses_identifier)
        self.assertEqual(result.student.omr_code, "55554444")

    def test_excel_import_matching_contacts_keeps_parent_phone_only(self):
        result = resolve_student_import_row(
            self.tenant,
            {
                "name": "엑셀동일번호학생",
                "parent_phone": "01055554444",
                "phone": "01055554444",
                "school_type": "HIGH",
                "grade": 1,
            },
            "test1234",
        )

        self.assertTrue(result.created)
        result.student.refresh_from_db()
        result.student.user.refresh_from_db()
        self.assertIsNone(result.student.phone)
        self.assertFalse(result.student.user.phone)
        self.assertTrue(result.student.uses_identifier)
        self.assertNotEqual(result.student.ps_number, result.student.parent_phone)
        self.assertEqual(result.student.omr_code, "55554444")

    def test_excel_import_rejects_malformed_student_phone_instead_of_silent_identifier_fallback(self):
        with self.assertRaisesMessage(ValueError, "학생 전화번호는 010XXXXXXXX 11자리여야 합니다."):
            resolve_student_import_row(
                self.tenant,
                {
                    "name": "엑셀오입력학생",
                    "parent_phone": "01055553333",
                    "phone": "010-12",
                    "school_type": "HIGH",
                    "grade": 1,
                },
                "test1234",
            )
