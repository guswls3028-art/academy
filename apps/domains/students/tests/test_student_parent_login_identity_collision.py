from django.test import TestCase

from academy.adapters.db.django import repositories_core as core_repo
from apps.core.models import Tenant
from apps.core.models.user import user_display_username
from apps.domains.students.services.creation import create_student_account
from apps.domains.students.services.identity import StudentIdentityError
from apps.domains.students.services.profile import (
    StudentProfileUpdateError,
    update_student_profile,
)


class StudentParentLoginIdentityCollisionTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            code="student-parent-login-collision",
            name="학생 학부모 로그인 충돌",
            is_active=True,
        )

    def _create_identifier_student(self, *, suffix: str, parent_phone: str):
        return create_student_account(
            tenant=self.tenant,
            password="initial-password",
            student_data={
                "name": f"학생{suffix}",
                "phone": None,
                "parent_phone": parent_phone,
                "ps_number": f"STUDENT-{suffix}",
                "omr_code": parent_phone[-8:],
                "uses_identifier": True,
                "school_type": "HIGH",
                "grade": 1,
            },
        ).student

    def test_pre_normalized_shared_phone_cannot_become_student_login_id(self):
        parent_phone = "01071112222"

        student = create_student_account(
            tenant=self.tenant,
            password="initial-password",
            student_data={
                "name": "사전 정규화 학생",
                "phone": None,
                "parent_phone": parent_phone,
                "ps_number": parent_phone,
                "omr_code": parent_phone[-8:],
                "uses_identifier": True,
                "school_type": "HIGH",
                "grade": 1,
            },
        ).student

        self.assertNotEqual(student.ps_number, parent_phone)
        candidates = core_repo.user_list_by_tenant_login_identifier(
            self.tenant,
            student.ps_number,
        )
        self.assertEqual([candidate.id for candidate in candidates], [student.user_id])

    def test_profile_login_id_cannot_collide_with_parent_phone_account(self):
        parent_phone = "01073334444"
        student = self._create_identifier_student(
            suffix="PROFILE",
            parent_phone=parent_phone,
        )

        with self.assertRaises(StudentProfileUpdateError):
            update_student_profile(
                student=student,
                tenant=self.tenant,
                data={"ps_number": parent_phone},
                identity_field="ps_number",
            )

        student.refresh_from_db()
        student.user.refresh_from_db()
        self.assertEqual(student.ps_number, "STUDENT-PROFILE")
        self.assertEqual(user_display_username(student.user), "STUDENT-PROFILE")

    def test_canonical_duplicate_create_returns_stable_identity_error(self):
        first = self._create_identifier_student(
            suffix="DUPLICATE",
            parent_phone="01074445555",
        )

        with self.assertRaises(StudentIdentityError) as ctx:
            create_student_account(
                tenant=self.tenant,
                password="initial-password",
                student_data={
                    "name": "중복 학생",
                    "phone": "01075556666",
                    "parent_phone": "",
                    "ps_number": first.ps_number,
                    "omr_code": "75556666",
                },
            )

        self.assertIn("ps_number", ctx.exception.detail)
