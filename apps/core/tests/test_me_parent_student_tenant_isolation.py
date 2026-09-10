from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.core.models import Tenant, TenantMembership
from apps.core.views.auth import MeView
from apps.domains.parents.models import Parent
from apps.domains.students.models import Student


User = get_user_model()


class MeParentStudentTenantIsolationTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.tenant = Tenant.objects.create(
            code="me-parent-scope",
            name="Me Parent Scope",
            is_active=True,
        )
        self.other_tenant = Tenant.objects.create(
            code="me-parent-scope-other",
            name="Me Parent Scope Other",
            is_active=True,
        )
        self.parent_user = User.objects.create_user(
            username="me-parent-scope-user",
            password="pw1234",
            tenant=self.tenant,
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=self.parent_user,
            role="parent",
        )
        self.parent = Parent.objects.create(
            tenant=self.tenant,
            user=self.parent_user,
            name="Local Parent",
            phone="01011112222",
        )

    def _student(self, *, tenant, username: str, name: str, suffix: str) -> Student:
        user = User.objects.create_user(
            username=username,
            password="pw1234",
            tenant=tenant,
        )
        return Student.objects.create(
            tenant=tenant,
            user=user,
            parent=self.parent,
            ps_number=f"ME-{suffix}",
            omr_code=f"{suffix:0>8}"[-8:],
            name=name,
            phone="01033334444",
            parent_phone=self.parent.phone,
        )

    def test_me_excludes_corrupt_cross_tenant_parent_student_relation(self):
        local_student = self._student(
            tenant=self.tenant,
            username="me-parent-local-child",
            name="Local Child",
            suffix="101",
        )
        second_local_student = self._student(
            tenant=self.tenant,
            username="me-parent-local-child-two",
            name="Local Child Two",
            suffix="303",
        )
        foreign_student = self._student(
            tenant=self.other_tenant,
            username="me-parent-foreign-child",
            name="Foreign Child",
            suffix="202",
        )

        request = self.factory.get("/api/v1/core/me/")
        request.tenant = self.tenant
        force_authenticate(request, user=self.parent_user)

        response = MeView.as_view()(request)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn("linkedStudentId", response.data)
        self.assertNotIn("linkedStudentName", response.data)
        self.assertEqual(
            response.data["linkedStudents"],
            [
                {"id": local_student.id, "name": local_student.name},
                {"id": second_local_student.id, "name": second_local_student.name},
            ],
        )
        self.assertNotIn(
            foreign_student.id,
            [row["id"] for row in response.data["linkedStudents"]],
        )
