from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.core.models import Tenant, TenantMembership
from apps.core.models.user import user_internal_username
from apps.domains.parents.models import Parent
from apps.domains.parents.services import (
    ensure_parent_account_for_student,
)


class ParentAccountCreationTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Parent Account", code="parent-account")

    def test_phone_is_normalized_before_identity_creation(self):
        result = ensure_parent_account_for_student(
            tenant=self.tenant,
            parent_phone="010-1234-5678",
            student_name="학생",
            initial_password="chosen-5678",
        )

        self.assertEqual(result.parent.phone, "01012345678")
        self.assertEqual(result.password_for_notice, "chosen-5678")
        self.assertEqual(result.parent.user.username, f"p_{self.tenant.id}_01012345678")

    def test_new_parent_requires_explicit_password(self):
        with self.assertRaisesMessage(ValueError, "초기 비밀번호를 입력"):
            ensure_parent_account_for_student(
                tenant=self.tenant,
                parent_phone="01012345678",
                student_name="학생",
            )

        self.assertFalse(Parent.objects.filter(tenant=self.tenant).exists())

    def test_explicit_student_registration_password_is_shared_with_new_parent(self):
        result = ensure_parent_account_for_student(
            tenant=self.tenant,
            parent_phone="01012345678",
            student_name="학생",
            initial_password="stud1234",
        )

        self.assertEqual(result.password_for_notice, "stud1234")
        self.assertTrue(result.parent.user.check_password("stud1234"))
        self.assertTrue(result.parent.user.must_change_password)

    def test_explicit_password_does_not_bypass_parent_phone_validation(self):
        with self.assertRaisesMessage(ValueError, "010 11자리"):
            ensure_parent_account_for_student(
                tenant=self.tenant,
                parent_phone="1234",
                student_name="학생",
                initial_password="stud1234",
            )

    def test_parent_phone_cannot_reuse_another_active_login_id(self):
        phone = "01012345678"
        student_user = get_user_model().objects.create_user(
            username=user_internal_username(self.tenant, phone),
            tenant=self.tenant,
            password="student-password",
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=student_user,
            role="student",
        )

        with self.assertRaisesMessage(ValueError, "다른 계정의 로그인 아이디"):
            ensure_parent_account_for_student(
                tenant=self.tenant,
                parent_phone=phone,
                student_name="학생",
                initial_password="parent-password",
            )

        self.assertFalse(Parent.objects.filter(tenant=self.tenant, phone=phone).exists())

    def test_repeated_ensure_is_idempotent(self):
        first = ensure_parent_account_for_student(
            tenant=self.tenant,
            parent_phone="01012345678",
            student_name="첫째",
            initial_password="first-password",
        )
        second = ensure_parent_account_for_student(
            tenant=self.tenant,
            parent_phone="01012345678",
            student_name="둘째",
        )

        self.assertEqual(first.parent.id, second.parent.id)
        self.assertTrue(first.credentials_initialized)
        self.assertFalse(second.credentials_initialized)
        self.assertEqual(Parent.objects.filter(tenant=self.tenant).count(), 1)
        self.assertEqual(
            TenantMembership.objects.filter(
                tenant=self.tenant,
                user=first.parent.user,
                role="parent",
                is_active=True,
            ).count(),
            1,
        )

    def test_existing_orphan_user_is_linked_without_password_overwrite(self):
        phone = "01012345678"
        user = get_user_model().objects.create_user(
            username=f"p_{self.tenant.id}_{phone}",
            tenant=self.tenant,
            password="preserved-password",
        )

        result = ensure_parent_account_for_student(
            tenant=self.tenant,
            parent_phone=phone,
            student_name="학생",
        )

        user.refresh_from_db()
        self.assertEqual(result.parent.user_id, user.id)
        self.assertFalse(result.credentials_initialized)
        self.assertTrue(user.check_password("preserved-password"))

    def test_unusable_existing_credentials_require_and_use_explicit_password(self):
        phone = "01012345678"
        user = get_user_model().objects.create_user(
            username=f"p_{self.tenant.id}_{phone}",
            tenant=self.tenant,
        )
        parent = Parent.objects.create(
            tenant=self.tenant,
            user=user,
            name="학생 학부모",
            phone=phone,
        )

        with self.assertRaisesMessage(ValueError, "초기 비밀번호를 입력"):
            ensure_parent_account_for_student(
                tenant=self.tenant,
                parent_phone=phone,
                student_name="학생",
            )

        result = ensure_parent_account_for_student(
            tenant=self.tenant,
            parent_phone=phone,
            student_name="학생",
            initial_password="repaired-password",
        )

        user.refresh_from_db()
        self.assertEqual(result.parent.id, parent.id)
        self.assertTrue(result.credentials_initialized)
        self.assertEqual(result.password_for_notice, "repaired-password")
        self.assertTrue(user.check_password("repaired-password"))
