from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.core.models import Tenant, TenantMembership
from apps.core.models.user import user_internal_username
from apps.domains.enrollment.test_support import create_enrollment_fixture
from apps.domains.lectures.test_support import create_lecture_fixture
from apps.domains.students.models import Student, StudentRegistrationRequest
from apps.domains.students.services.creation import create_student_account
from apps.domains.students.services.lifecycle import soft_delete_student
from apps.domains.students.services.registration_approval import (
    RegistrationApprovalError,
    approve_registration_request,
    resolve_deleted_registration_request,
)
from apps.domains.students.views.registration_views import RegistrationRequestViewSet
from apps.support.students.lifecycle_dependencies import ensure_parent_account_for_student


User = get_user_model()


class RegistrationApprovalIdentityTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.tenant = Tenant.objects.create(
            name="가입 식별 학원",
            code="registration-identity",
            is_active=True,
        )

    def _student(
        self,
        *,
        name: str = "기존학생",
        ps_number: str = "EXISTING-001",
        phone: str = "01070001111",
        parent_phone: str = "01070002222",
    ) -> Student:
        return create_student_account(
            tenant=self.tenant,
            password="teacher-password",
            student_data={
                "name": name,
                "ps_number": ps_number,
                "phone": phone,
                "parent_phone": parent_phone,
                "omr_code": (phone or parent_phone)[-8:],
                "uses_identifier": False,
                "school_type": "HIGH",
                "grade": 1,
            },
        ).student

    def _registration(
        self,
        *,
        name: str = "기존학생",
        username: str = "EXISTING-001",
        phone: str | None = "01070001111",
        parent_phone: str = "01070002222",
        tenant: Tenant | None = None,
    ) -> StudentRegistrationRequest:
        return StudentRegistrationRequest.objects.create(
            tenant=tenant or self.tenant,
            status=StudentRegistrationRequest.PENDING,
            initial_password=make_password("signup-password"),
            initial_password_plain="",
            name=name,
            username=username,
            parent_phone=parent_phone,
            phone=phone,
            school_type="HIGH",
            high_school="테스트고",
            origin_middle_school="테스트중",
            grade=1,
            gender="M",
            address="서울",
        )

    def _signup_payload(self) -> dict[str, object]:
        return {
            "name": "가입학생",
            "username": "SIGNUP-001",
            "initial_password": "signup-password",
            "password_confirmation": "signup-password",
            "parent_phone": "01071112222",
            "phone": "01073334444",
            "school_type": "HIGH",
            "high_school": "테스트고",
            "origin_middle_school": "테스트중",
            "grade": 1,
            "gender": "M",
            "address": "서울",
        }

    def _parent_count(self, *, tenant: Tenant | None = None) -> int:
        parent_model = Student._meta.get_field("parent").remote_field.model
        queryset = parent_model.objects.all()
        if tenant is not None:
            queryset = queryset.filter(tenant=tenant)
        return queryset.count()

    def test_approval_reuses_exact_existing_student_without_changing_identity_or_credentials(self):
        student = self._student()
        registration = self._registration(username="REQUESTED-NEW-ID")
        student.refresh_from_db()
        student.user.refresh_from_db()
        original = {
            "student_count": Student.objects.filter(tenant=self.tenant).count(),
            "parent_count": self._parent_count(tenant=self.tenant),
            "user_count": User.objects.filter(tenant=self.tenant).count(),
            "membership_count": TenantMembership.objects.filter(tenant=self.tenant).count(),
            "ps_number": student.ps_number,
            "password": student.user.password,
            "token_version": student.user.token_version,
            "student_notice": student.pending_account_notice_student_password_ciphertext,
            "parent_notice": student.pending_account_notice_parent_password_ciphertext,
        }

        result = approve_registration_request(
            tenant=self.tenant,
            registration_id=registration.id,
        )

        registration.refresh_from_db()
        student.refresh_from_db()
        student.user.refresh_from_db()
        self.assertEqual(result.student.id, student.id)
        self.assertEqual(registration.student_id, student.id)
        self.assertEqual(registration.status, StudentRegistrationRequest.APPROVED)
        self.assertEqual(Student.objects.filter(tenant=self.tenant).count(), original["student_count"])
        self.assertEqual(self._parent_count(tenant=self.tenant), original["parent_count"])
        self.assertEqual(User.objects.filter(tenant=self.tenant).count(), original["user_count"])
        self.assertEqual(
            TenantMembership.objects.filter(tenant=self.tenant).count(),
            original["membership_count"],
        )
        self.assertEqual(student.ps_number, original["ps_number"])
        self.assertEqual(student.user.password, original["password"])
        self.assertEqual(student.user.token_version, original["token_version"])
        self.assertEqual(
            student.pending_account_notice_student_password_ciphertext,
            original["student_notice"],
        )
        self.assertEqual(
            student.pending_account_notice_parent_password_ciphertext,
            original["parent_notice"],
        )
        self.assertEqual(result.notice.student_id, original["ps_number"])
        self.assertEqual(result.notice.student_password, "변경되지 않음")
        self.assertEqual(result.notice.parent_password, "변경되지 않음")

    def test_approval_fails_closed_when_phone_matches_multiple_active_students(self):
        self._student(ps_number="AMBIGUOUS-001")
        self._student(ps_number="AMBIGUOUS-002")
        registration = self._registration(username="REQUESTED-AMBIGUOUS")
        original_counts = (Student.objects.count(), User.objects.count(), self._parent_count())

        with self.assertRaisesMessage(RegistrationApprovalError, "여러 명"):
            approve_registration_request(
                tenant=self.tenant,
                registration_id=registration.id,
            )

        registration.refresh_from_db()
        self.assertEqual(registration.status, StudentRegistrationRequest.PENDING)
        self.assertIsNone(registration.student_id)
        self.assertEqual(
            (Student.objects.count(), User.objects.count(), self._parent_count()),
            original_counts,
        )

    def test_approval_reuses_parent_phone_identity_when_student_phone_is_empty(self):
        parent_phone = "01070002222"
        student = self._student(phone="", parent_phone=parent_phone)
        registration = self._registration(
            username="REQUESTED-PARENT-PHONE",
            phone=parent_phone,
            parent_phone=parent_phone,
        )
        original_counts = (Student.objects.count(), User.objects.count(), self._parent_count())

        result = approve_registration_request(
            tenant=self.tenant,
            registration_id=registration.id,
        )

        registration.refresh_from_db()
        self.assertEqual(result.student.id, student.id)
        self.assertEqual(registration.student_id, student.id)
        self.assertEqual(
            (Student.objects.count(), User.objects.count(), self._parent_count()),
            original_counts,
        )

    def test_approval_fails_closed_for_matching_deleted_student(self):
        student = self._student()
        student.deleted_at = timezone.now()
        student.save(update_fields=["deleted_at", "updated_at"])
        registration = self._registration()
        original_counts = (Student.objects.count(), User.objects.count(), self._parent_count())

        with self.assertRaisesMessage(RegistrationApprovalError, "삭제"):
            approve_registration_request(
                tenant=self.tenant,
                registration_id=registration.id,
            )

        registration.refresh_from_db()
        self.assertEqual(registration.status, StudentRegistrationRequest.PENDING)
        self.assertIsNone(registration.student_id)
        self.assertEqual(
            (Student.objects.count(), User.objects.count(), self._parent_count()),
            original_counts,
        )

    def test_deleted_conflict_exposes_staff_resolution_candidates(self):
        student = self._student()
        soft_delete_student(student, tenant=self.tenant)
        registration = self._registration()
        staff = User.objects.create_user(
            username="registration-recovery-staff",
            password="staff-password",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=staff, role="teacher")
        request = self.factory.post(
            f"/api/v1/students/registration_requests/{registration.id}/approve/"
        )
        force_authenticate(request, user=staff)
        request.tenant = self.tenant

        response = RegistrationRequestViewSet.as_view({"post": "approve"})(
            request,
            pk=registration.id,
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "deleted_student_conflict")
        self.assertEqual(
            response.data["candidates"],
            [
                {
                    "student_id": student.id,
                    "created_at": student.created_at,
                    "deleted_at": Student.objects.get(pk=student.id).deleted_at,
                    "enrollment_count": 0,
                }
            ],
        )

    def test_explicit_deleted_resolution_restores_selected_history_and_adopts_signup_login(self):
        selected = self._student(ps_number="OLD-SELECTED", phone="01070001111")
        lecture = create_lecture_fixture(
            tenant=self.tenant,
            title="복구 이력 강의",
            name="복구 이력 강의",
            subject="테스트",
        )
        enrollment = create_enrollment_fixture(
            tenant=self.tenant,
            student=selected,
            lecture=lecture,
            status="ACTIVE",
        )
        pending_lecture = create_lecture_fixture(
            tenant=self.tenant,
            title="복구 대기 강의",
            name="복구 대기 강의",
            subject="테스트",
        )
        pending_enrollment = create_enrollment_fixture(
            tenant=self.tenant,
            student=selected,
            lecture=pending_lecture,
            status="PENDING",
        )
        inactive_lecture = create_lecture_fixture(
            tenant=self.tenant,
            title="복구 비활성 강의",
            name="복구 비활성 강의",
            subject="테스트",
        )
        inactive_enrollment = create_enrollment_fixture(
            tenant=self.tenant,
            student=selected,
            lecture=inactive_lecture,
            status="INACTIVE",
        )
        ended_lecture = create_lecture_fixture(
            tenant=self.tenant,
            title="복구 중 종료 강의",
            name="복구 중 종료 강의",
            subject="테스트",
            end_date=timezone.localdate() + timedelta(days=1),
        )
        ended_enrollment = create_enrollment_fixture(
            tenant=self.tenant,
            student=selected,
            lecture=ended_lecture,
            status="ACTIVE",
        )
        enrollment_expectations = (
            (enrollment, "ACTIVE", "ACTIVE"),
            (pending_enrollment, "PENDING", "PENDING"),
            (inactive_enrollment, "INACTIVE", "INACTIVE"),
            (ended_enrollment, "ACTIVE", "INACTIVE"),
        )
        enrollment_links = {
            row.id: (row.student_id, row.lecture_id)
            for row, _original, _restored in enrollment_expectations
        }
        previous_registration = self._registration(username="OLD-SELECTED")
        previous_registration.status = StudentRegistrationRequest.APPROVED
        previous_registration.student = selected
        previous_registration.save(update_fields=["status", "student", "updated_at"])
        parent_model = Student._meta.get_field("parent").remote_field.model
        parent = parent_model.objects.select_related("user").get(pk=selected.parent_id)
        parent_snapshot = (
            parent.id,
            parent.user_id,
            parent.user.password,
            parent.user.token_version,
        )
        original_token_version = selected.user.token_version
        soft_delete_student(selected, tenant=self.tenant)
        for row, original_status, _restored_status in enrollment_expectations:
            row.refresh_from_db()
            self.assertEqual(row.status, "INACTIVE")
            self.assertEqual(row.status_before_student_deletion, original_status)
        ended_lecture.end_date = timezone.localdate() - timedelta(days=1)
        ended_lecture.save(update_fields=["end_date"])

        duplicate = self._student(ps_number="OLD-DUPLICATE", phone="01070003333")
        soft_delete_student(duplicate, tenant=self.tenant)
        registration = self._registration(username="RECOVERED-LOGIN")
        graph_counts = {
            "students": Student.objects.filter(tenant=self.tenant).count(),
            "users": User.objects.filter(tenant_memberships__tenant=self.tenant).distinct().count(),
            "memberships": TenantMembership.objects.filter(tenant=self.tenant).count(),
            "enrollments": apps.get_model("enrollment", "Enrollment").objects.filter(
                tenant=self.tenant
            ).count(),
            "notifications": apps.get_model("messaging", "NotificationLog").objects.count(),
            "scheduled": apps.get_model(
                "messaging", "ScheduledNotification"
            ).objects.count(),
        }

        result = resolve_deleted_registration_request(
            tenant=self.tenant,
            registration_id=registration.id,
            student_id=selected.id,
        )

        selected.refresh_from_db()
        selected.user.refresh_from_db()
        duplicate.refresh_from_db()
        registration.refresh_from_db()
        previous_registration.refresh_from_db()
        self.assertEqual(result.student.id, selected.id)
        self.assertIsNone(selected.deleted_at)
        self.assertIsNotNone(duplicate.deleted_at)
        self.assertEqual(selected.ps_number, "RECOVERED-LOGIN")
        self.assertEqual(
            selected.user.username,
            user_internal_username(self.tenant, "RECOVERED-LOGIN"),
        )
        self.assertTrue(selected.user.check_password("signup-password"))
        self.assertGreater(selected.user.token_version, original_token_version)
        self.assertFalse(selected.user.must_change_password)
        self.assertTrue(
            TenantMembership.objects.get(
                tenant=self.tenant,
                user=selected.user,
            ).is_active
        )
        self.assertEqual(registration.status, StudentRegistrationRequest.APPROVED)
        self.assertEqual(registration.student_id, selected.id)
        self.assertEqual(previous_registration.student_id, selected.id)
        parent = parent_model.objects.select_related("user").get(pk=parent.id)
        self.assertEqual(
            (parent.id, parent.user_id, parent.user.password, parent.user.token_version),
            parent_snapshot,
        )
        self.assertEqual(
            set(
                StudentRegistrationRequest.objects.filter(student=selected).values_list(
                    "id", flat=True
                )
            ),
            {registration.id, previous_registration.id},
        )
        for row, _original_status, restored_status in enrollment_expectations:
            row.refresh_from_db()
            self.assertEqual((row.student_id, row.lecture_id), enrollment_links[row.id])
            self.assertEqual(row.status, restored_status)
            self.assertIsNone(row.status_before_student_deletion)
        restored_enrollment_state = {
            row.id: (row.status, row.status_before_student_deletion)
            for row, _original_status, _restored_status in enrollment_expectations
        }

        password_hash = selected.user.password
        token_version = selected.user.token_version
        with self.assertRaises(RegistrationApprovalError) as retry:
            resolve_deleted_registration_request(
                tenant=self.tenant,
                registration_id=registration.id,
                student_id=selected.id,
            )
        selected.user.refresh_from_db()
        self.assertEqual(retry.exception.status_code, 409)
        self.assertEqual(selected.user.password, password_hash)
        self.assertEqual(selected.user.token_version, token_version)
        self.assertEqual(
            StudentRegistrationRequest.objects.filter(student=selected).count(),
            2,
        )
        for row, _original_status, _restored_status in enrollment_expectations:
            row.refresh_from_db()
            self.assertEqual(
                (row.status, row.status_before_student_deletion),
                restored_enrollment_state[row.id],
            )
        self.assertEqual(
            {
                "students": Student.objects.filter(tenant=self.tenant).count(),
                "users": User.objects.filter(tenant_memberships__tenant=self.tenant)
                .distinct()
                .count(),
                "memberships": TenantMembership.objects.filter(tenant=self.tenant).count(),
                "enrollments": apps.get_model("enrollment", "Enrollment").objects.filter(
                    tenant=self.tenant
                ).count(),
                "notifications": apps.get_model(
                    "messaging", "NotificationLog"
                ).objects.count(),
                "scheduled": apps.get_model(
                    "messaging", "ScheduledNotification"
                ).objects.count(),
            },
            graph_counts,
        )

    def test_registration_history_relation_is_plural_and_non_unique(self):
        field = StudentRegistrationRequest._meta.get_field("student")

        self.assertTrue(field.many_to_one)
        self.assertFalse(field.unique)
        self.assertEqual(field.remote_field.related_name, "registration_requests")

    def test_deleted_resolution_rejects_foreign_or_nonmatching_candidate(self):
        selected = self._student()
        soft_delete_student(selected, tenant=self.tenant)
        other_tenant = Tenant.objects.create(
            name="다른 복구 학원",
            code="other-recovery",
            is_active=True,
        )
        self.tenant, original_tenant = other_tenant, self.tenant
        foreign = self._student()
        soft_delete_student(foreign, tenant=other_tenant)
        self.tenant = original_tenant
        registration = self._registration()

        with self.assertRaisesMessage(RegistrationApprovalError, "일치하지 않습니다"):
            resolve_deleted_registration_request(
                tenant=self.tenant,
                registration_id=registration.id,
                student_id=foreign.id,
            )

        registration.refresh_from_db()
        selected.refresh_from_db()
        foreign.refresh_from_db()
        self.assertEqual(registration.status, StudentRegistrationRequest.PENDING)
        self.assertIsNotNone(selected.deleted_at)
        self.assertIsNotNone(foreign.deleted_at)

    def test_deleted_resolution_rejects_candidate_that_is_now_active_without_mutation(self):
        selected = self._student(ps_number="ACTIVE-CANDIDATE")
        registration = self._registration(username="RECOVERY-STALE")
        original_password = selected.user.password
        original_token_version = selected.user.token_version

        with self.assertRaises(RegistrationApprovalError) as stale:
            resolve_deleted_registration_request(
                tenant=self.tenant,
                registration_id=registration.id,
                student_id=selected.id,
            )

        self.assertEqual(stale.exception.status_code, 409)
        registration.refresh_from_db()
        selected.refresh_from_db()
        selected.user.refresh_from_db()
        self.assertEqual(registration.status, StudentRegistrationRequest.PENDING)
        self.assertIsNone(registration.student_id)
        self.assertIsNone(selected.deleted_at)
        self.assertEqual(selected.user.password, original_password)
        self.assertEqual(selected.user.token_version, original_token_version)

    def test_ambiguous_deleted_matches_require_explicit_selection_without_mutation(self):
        first = self._student(ps_number="DELETED-FIRST")
        second = self._student(ps_number="DELETED-SECOND")
        soft_delete_student(first, tenant=self.tenant)
        soft_delete_student(second, tenant=self.tenant)
        registration = self._registration(username="RECOVERY-AMBIGUOUS")

        with self.assertRaises(RegistrationApprovalError) as ctx:
            approve_registration_request(
                tenant=self.tenant,
                registration_id=registration.id,
            )

        self.assertEqual(ctx.exception.code, "deleted_student_conflict")
        self.assertEqual(
            {item["student_id"] for item in ctx.exception.context["candidates"]},
            {first.id, second.id},
        )
        registration.refresh_from_db()
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(registration.status, StudentRegistrationRequest.PENDING)
        self.assertIsNone(registration.student_id)
        self.assertIsNotNone(first.deleted_at)
        self.assertIsNotNone(second.deleted_at)

    def test_deleted_resolution_rejects_cross_tenant_membership_without_mutation(self):
        selected = self._student()
        other_tenant = Tenant.objects.create(
            name="다른 멤버십 학원",
            code="other-recovery-membership",
            is_active=True,
        )
        TenantMembership.ensure_active(
            tenant=other_tenant,
            user=selected.user,
            role="student",
        )
        soft_delete_student(selected, tenant=self.tenant)
        registration = self._registration(username="RECOVERY-CROSS-MEMBERSHIP")

        with self.assertRaisesMessage(RegistrationApprovalError, "다른 학원"):
            resolve_deleted_registration_request(
                tenant=self.tenant,
                registration_id=registration.id,
                student_id=selected.id,
            )

        registration.refresh_from_db()
        selected.refresh_from_db()
        self.assertEqual(registration.status, StudentRegistrationRequest.PENDING)
        self.assertIsNone(registration.student_id)
        self.assertIsNotNone(selected.deleted_at)

    def test_deleted_resolution_rejects_foreign_user_tenant_pointer_without_mutation(self):
        selected = self._student(ps_number="DRIFTED-USER-TENANT")
        lecture = create_lecture_fixture(
            tenant=self.tenant,
            title="계정 테넌트 오염 강의",
            name="계정 테넌트 오염 강의",
            subject="테스트",
        )
        enrollment = create_enrollment_fixture(
            tenant=self.tenant,
            student=selected,
            lecture=lecture,
            status="ACTIVE",
        )
        previous_registration = self._registration(username="DRIFTED-USER-HISTORY")
        previous_registration.status = StudentRegistrationRequest.APPROVED
        previous_registration.student = selected
        previous_registration.save(update_fields=["status", "student", "updated_at"])
        soft_delete_student(selected, tenant=self.tenant)

        foreign_tenant = Tenant.objects.create(
            name="오염된 계정 포인터 학원",
            code="drifted-user-pointer",
            is_active=True,
        )
        User.objects.filter(pk=selected.user_id).update(tenant=foreign_tenant)
        registration = self._registration(username="RECOVERY-DRIFTED-USER")

        selected.refresh_from_db()
        selected.user.refresh_from_db()
        enrollment.refresh_from_db()
        membership = TenantMembership.objects.get(tenant=self.tenant, user=selected.user)
        original = {
            "student": (
                selected.deleted_at,
                selected.ps_number,
                selected.name,
                selected.phone,
                selected.parent_phone,
                selected.parent_id,
            ),
            "user": (
                selected.user.tenant_id,
                selected.user.username,
                selected.user.phone,
                selected.user.password,
                selected.user.token_version,
                selected.user.is_active,
                selected.user.must_change_password,
            ),
            "membership": (membership.role, membership.is_active),
            "enrollment": (
                enrollment.student_id,
                enrollment.lecture_id,
                enrollment.status,
                enrollment.status_before_student_deletion,
            ),
            "history": list(
                StudentRegistrationRequest.objects.order_by("id").values_list(
                    "id", "status", "student_id"
                )
            ),
            "notifications": apps.get_model("messaging", "NotificationLog").objects.count(),
            "scheduled": apps.get_model(
                "messaging", "ScheduledNotification"
            ).objects.count(),
        }

        with self.assertRaisesMessage(RegistrationApprovalError, "테넌트") as ctx:
            resolve_deleted_registration_request(
                tenant=self.tenant,
                registration_id=registration.id,
                student_id=selected.id,
            )

        self.assertEqual(ctx.exception.status_code, 409)
        selected.refresh_from_db()
        selected.user.refresh_from_db()
        enrollment.refresh_from_db()
        membership.refresh_from_db()
        registration.refresh_from_db()
        previous_registration.refresh_from_db()
        self.assertEqual(registration.status, StudentRegistrationRequest.PENDING)
        self.assertIsNone(registration.student_id)
        self.assertEqual(
            (
                selected.deleted_at,
                selected.ps_number,
                selected.name,
                selected.phone,
                selected.parent_phone,
                selected.parent_id,
            ),
            original["student"],
        )
        self.assertEqual(
            (
                selected.user.tenant_id,
                selected.user.username,
                selected.user.phone,
                selected.user.password,
                selected.user.token_version,
                selected.user.is_active,
                selected.user.must_change_password,
            ),
            original["user"],
        )
        self.assertEqual((membership.role, membership.is_active), original["membership"])
        self.assertEqual(
            (
                enrollment.student_id,
                enrollment.lecture_id,
                enrollment.status,
                enrollment.status_before_student_deletion,
            ),
            original["enrollment"],
        )
        self.assertEqual(
            list(
                StudentRegistrationRequest.objects.order_by("id").values_list(
                    "id", "status", "student_id"
                )
            ),
            original["history"],
        )
        self.assertEqual(previous_registration.student_id, selected.id)
        self.assertEqual(
            apps.get_model("messaging", "NotificationLog").objects.count(),
            original["notifications"],
        )
        self.assertEqual(
            apps.get_model("messaging", "ScheduledNotification").objects.count(),
            original["scheduled"],
        )

    def test_approval_fails_closed_for_cross_tenant_user_link_drift(self):
        other_tenant = Tenant.objects.create(name="다른 학원", code="other-registration", is_active=True)
        foreign_user = User.objects.create_user(
            username="foreign-student-user",
            password="foreign-password",
            tenant=other_tenant,
            phone="01070001111",
        )
        Student.objects.create(
            tenant=self.tenant,
            user=foreign_user,
            name="기존학생",
            ps_number="FOREIGN-LINK",
            phone="01070001111",
            parent_phone="01070002222",
            omr_code="70001111",
            school_type="HIGH",
            grade=1,
        )
        registration = self._registration(username="FOREIGN-LINK")
        original_counts = (Student.objects.count(), User.objects.count(), self._parent_count())

        with self.assertRaisesMessage(RegistrationApprovalError, "테넌트"):
            approve_registration_request(
                tenant=self.tenant,
                registration_id=registration.id,
            )

        registration.refresh_from_db()
        self.assertEqual(registration.status, StudentRegistrationRequest.PENDING)
        self.assertIsNone(registration.student_id)
        self.assertEqual(
            (Student.objects.count(), User.objects.count(), self._parent_count()),
            original_counts,
        )

    def test_cross_tenant_phone_match_is_never_reused(self):
        other_tenant = Tenant.objects.create(name="다른 학원", code="other-phone", is_active=True)
        original_tenant = self.tenant
        self.tenant = other_tenant
        other_student = self._student()
        self.tenant = original_tenant
        registration = self._registration(username="LOCAL-STUDENT")

        result = approve_registration_request(
            tenant=self.tenant,
            registration_id=registration.id,
        )

        self.assertNotEqual(result.student.id, other_student.id)
        self.assertEqual(result.student.tenant_id, self.tenant.id)
        self.assertEqual(Student.objects.filter(phone="01070001111").count(), 2)

    def test_existing_student_reuse_rejects_student_username_drift(self):
        student = self._student()
        student.user.username = "drifted-student-login"
        student.user.save(update_fields=["username"])
        registration = self._registration(username="REQUESTED-AFTER-DRIFT")

        with self.assertRaises(RegistrationApprovalError) as ctx:
            approve_registration_request(
                tenant=self.tenant,
                registration_id=registration.id,
            )

        self.assertEqual(ctx.exception.status_code, 409)
        registration.refresh_from_db()
        self.assertEqual(registration.status, StudentRegistrationRequest.PENDING)
        self.assertIsNone(registration.student_id)

    def test_existing_student_reuse_rejects_inactive_student_user(self):
        student = self._student()
        student.user.is_active = False
        student.user.save(update_fields=["is_active"])
        registration = self._registration(username="REQUESTED-INACTIVE")

        with self.assertRaises(RegistrationApprovalError) as ctx:
            approve_registration_request(
                tenant=self.tenant,
                registration_id=registration.id,
            )

        self.assertEqual(ctx.exception.status_code, 409)
        registration.refresh_from_db()
        self.assertEqual(registration.status, StudentRegistrationRequest.PENDING)
        self.assertIsNone(registration.student_id)

    def test_existing_student_reuse_rejects_missing_student_membership(self):
        student = self._student()
        TenantMembership.objects.filter(tenant=self.tenant, user=student.user).delete()
        registration = self._registration(username="REQUESTED-NO-STUDENT-MEMBERSHIP")

        with self.assertRaises(RegistrationApprovalError) as ctx:
            approve_registration_request(
                tenant=self.tenant,
                registration_id=registration.id,
            )

        self.assertEqual(ctx.exception.status_code, 409)
        registration.refresh_from_db()
        self.assertEqual(registration.status, StudentRegistrationRequest.PENDING)
        self.assertIsNone(registration.student_id)

    def test_existing_student_reuse_rejects_missing_parent_user(self):
        student = self._student()
        parent = student.parent
        parent.user = None
        parent.save(update_fields=["user"])
        registration = self._registration(username="REQUESTED-NO-PARENT-USER")

        with self.assertRaises(RegistrationApprovalError) as ctx:
            approve_registration_request(
                tenant=self.tenant,
                registration_id=registration.id,
            )

        self.assertEqual(ctx.exception.status_code, 409)
        registration.refresh_from_db()
        self.assertEqual(registration.status, StudentRegistrationRequest.PENDING)
        self.assertIsNone(registration.student_id)

    def test_existing_student_reuse_rejects_missing_parent_membership(self):
        student = self._student()
        TenantMembership.objects.filter(tenant=self.tenant, user=student.parent.user).delete()
        registration = self._registration(username="REQUESTED-NO-PARENT-MEMBERSHIP")

        with self.assertRaises(RegistrationApprovalError) as ctx:
            approve_registration_request(
                tenant=self.tenant,
                registration_id=registration.id,
            )

        self.assertEqual(ctx.exception.status_code, 409)
        registration.refresh_from_db()
        self.assertEqual(registration.status, StudentRegistrationRequest.PENDING)
        self.assertIsNone(registration.student_id)

    def test_new_student_rejects_linked_parent_user_phone_and_identity_drift(self):
        parent_result = ensure_parent_account_for_student(
            tenant=self.tenant,
            parent_phone="01076662222",
            student_name="신규학생",
            initial_password="parent-password",
        )
        parent_user = parent_result.parent.user
        parent_user.username = "drifted-parent-login"
        parent_user.phone = "01070009999"
        parent_user.save(update_fields=["username", "phone"])
        registration = self._registration(
            name="신규학생",
            username="NEW-STUDENT-AFTER-PARENT-DRIFT",
            phone="01076661111",
            parent_phone="01076662222",
        )

        with self.assertRaises(RegistrationApprovalError) as ctx:
            approve_registration_request(
                tenant=self.tenant,
                registration_id=registration.id,
            )

        self.assertEqual(ctx.exception.status_code, 409)
        registration.refresh_from_db()
        self.assertEqual(registration.status, StudentRegistrationRequest.PENDING)
        self.assertIsNone(registration.student_id)
        self.assertFalse(Student.objects.filter(tenant=self.tenant).exists())

    def test_new_approval_rejects_explicit_parent_phone_student_login_id(self):
        shared_phone = "01074445555"
        registration = self._registration(
            name="공유번호신규",
            username=shared_phone,
            phone=shared_phone,
            parent_phone=shared_phone,
        )

        with self.assertRaises(RegistrationApprovalError) as ctx:
            approve_registration_request(
                tenant=self.tenant,
                registration_id=registration.id,
            )

        self.assertEqual(ctx.exception.status_code, 400)
        registration.refresh_from_db()
        self.assertEqual(registration.status, StudentRegistrationRequest.PENDING)
        self.assertIsNone(registration.student_id)
        self.assertFalse(Student.objects.filter(tenant=self.tenant).exists())

    def test_public_signup_rejects_parent_phone_as_student_login_id(self):
        payload = self._signup_payload()
        payload["username"] = "010-7111-2222"
        request = self.factory.post(
            "/api/v1/students/registration_requests/",
            payload,
            format="json",
        )
        request.tenant = self.tenant

        response = RegistrationRequestViewSet.as_view({"post": "create"})(request)

        self.assertEqual(response.status_code, 400)
        self.assertIn("username", response.data)
        self.assertFalse(
            StudentRegistrationRequest.objects.filter(tenant=self.tenant).exists()
        )

    def test_disabled_tenants_reject_public_signup_and_pending_approval(self):
        for code in ("godmin", "tchul"):
            with self.subTest(code=code):
                tenant = Tenant.objects.create(name=code, code=code, is_active=True)
                duplicate_request = self.factory.post(
                    "/api/v1/students/registration_requests/check_duplicate/",
                    {"phone": "01073334444"},
                    format="json",
                )
                duplicate_request.tenant = tenant
                duplicate_response = RegistrationRequestViewSet.as_view(
                    {"post": "check_duplicate"}
                )(duplicate_request)
                self.assertEqual(duplicate_response.status_code, 403)
                self.assertEqual(
                    duplicate_response.data["code"],
                    "self_registration_disabled",
                )

                request = self.factory.post(
                    "/api/v1/students/registration_requests/",
                    self._signup_payload(),
                    format="json",
                )
                request.tenant = tenant

                response = RegistrationRequestViewSet.as_view({"post": "create"})(request)

                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.data["code"], "self_registration_disabled")
                self.assertFalse(StudentRegistrationRequest.objects.filter(tenant=tenant).exists())

                registration = self._registration(
                    tenant=tenant,
                    username=f"{code}-pending",
                    phone="01079990001",
                    parent_phone="01079990002",
                )
                with self.assertRaisesMessage(RegistrationApprovalError, "회원가입"):
                    approve_registration_request(
                        tenant=tenant,
                        registration_id=registration.id,
                    )
                registration.refresh_from_db()
                self.assertEqual(registration.status, StudentRegistrationRequest.PENDING)
                self.assertIsNone(registration.student_id)

    def test_disabled_tenant_pending_list_is_policy_history_not_actionable_work(self):
        tenant = Tenant.objects.create(name="비활성 가입 학원", code="godmin", is_active=True)
        registration = self._registration(
            tenant=tenant,
            username="historical-pending",
            phone="01079991001",
            parent_phone="01079991002",
        )
        staff = User.objects.create_user(
            username="disabled-registration-list-staff",
            password="staff-password",
            tenant=tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(tenant=tenant, user=staff, role="teacher")
        request = self.factory.get(
            "/api/v1/students/registration_requests/",
            {"status": StudentRegistrationRequest.PENDING},
        )
        force_authenticate(request, user=staff)
        request.tenant = tenant

        response = RegistrationRequestViewSet.as_view({"get": "list"})(request)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["code"], "self_registration_disabled")
        registration.refresh_from_db()
        self.assertEqual(registration.status, StudentRegistrationRequest.PENDING)

    def test_disabled_tenant_staff_actions_preserve_pending_history(self):
        tenant = Tenant.objects.create(name="비활성 가입 학원", code="godmin", is_active=True)
        staff = User.objects.create_user(
            username="disabled-registration-action-staff",
            password="staff-password",
            tenant=tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(tenant=tenant, user=staff, role="teacher")

        cases = (
            ("approve", "approve", False),
            ("bulk_approve", "bulk_approve", True),
            ("reject", "reject", False),
            ("bulk_reject", "bulk_reject", True),
        )
        for index, (label, action_name, is_bulk) in enumerate(cases, start=1):
            with self.subTest(action=label):
                registration = self._registration(
                    tenant=tenant,
                    username=f"historical-pending-{index}",
                    phone=f"01079992{index:03d}",
                    parent_phone=f"01079993{index:03d}",
                )
                url = (
                    f"/api/v1/students/registration_requests/{action_name}/"
                    if is_bulk
                    else f"/api/v1/students/registration_requests/{registration.id}/{action_name}/"
                )
                data = {"ids": [registration.id]} if is_bulk else {}
                request = self.factory.post(url, data, format="json")
                force_authenticate(request, user=staff)
                request.tenant = tenant
                view = RegistrationRequestViewSet.as_view({"post": action_name})

                response = view(request) if is_bulk else view(request, pk=registration.id)

                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.data["code"], "self_registration_disabled")
                registration.refresh_from_db()
                self.assertEqual(registration.status, StudentRegistrationRequest.PENDING)
                self.assertIsNone(registration.student_id)

    def test_disabled_tenant_generic_patch_cannot_move_or_resolve_history(self):
        tenant = Tenant.objects.create(name="비활성 가입 수정 학원", code="godmin", is_active=True)
        other_tenant = Tenant.objects.create(
            name="다른 가입 학원",
            code="registration-other-tenant",
            is_active=True,
        )
        registration = self._registration(
            tenant=tenant,
            username="historical-patch",
            phone="01079994001",
            parent_phone="01079994002",
        )
        staff = User.objects.create_user(
            username="disabled-registration-patch-staff",
            password="staff-password",
            tenant=tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(tenant=tenant, user=staff, role="teacher")
        request = self.factory.patch(
            f"/api/v1/students/registration_requests/{registration.id}/",
            {
                "tenant": other_tenant.id,
                "status": StudentRegistrationRequest.APPROVED,
            },
            format="json",
        )
        force_authenticate(request, user=staff)
        request.tenant = tenant

        response = RegistrationRequestViewSet.as_view({"patch": "partial_update"})(
            request,
            pk=registration.id,
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["code"], "self_registration_disabled")
        registration.refresh_from_db()
        self.assertEqual(registration.tenant_id, tenant.id)
        self.assertEqual(registration.status, StudentRegistrationRequest.PENDING)
        self.assertIsNone(registration.student_id)

    def test_disabled_tenant_generic_delete_preserves_history_row(self):
        tenant = Tenant.objects.create(name="비활성 가입 삭제 학원", code="godmin", is_active=True)
        registration = self._registration(
            tenant=tenant,
            username="historical-delete",
            phone="01079995001",
            parent_phone="01079995002",
        )
        staff = User.objects.create_user(
            username="disabled-registration-delete-staff",
            password="staff-password",
            tenant=tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(tenant=tenant, user=staff, role="teacher")
        request = self.factory.delete(
            f"/api/v1/students/registration_requests/{registration.id}/",
        )
        force_authenticate(request, user=staff)
        request.tenant = tenant

        response = RegistrationRequestViewSet.as_view({"delete": "destroy"})(
            request,
            pk=registration.id,
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["code"], "self_registration_disabled")
        self.assertTrue(StudentRegistrationRequest.objects.filter(pk=registration.id).exists())
        registration.refresh_from_db()
        self.assertEqual(registration.tenant_id, tenant.id)
        self.assertEqual(registration.status, StudentRegistrationRequest.PENDING)
        self.assertIsNone(registration.student_id)

    def test_disabled_tenant_resolve_deleted_preserves_history_and_deleted_student(self):
        tenant = Tenant.objects.create(name="비활성 가입 복구 학원", code="godmin", is_active=True)
        deleted_student = create_student_account(
            tenant=tenant,
            password="teacher-password",
            student_data={
                "name": "과거학생",
                "ps_number": "DISABLED-RESOLVE-DELETED",
                "phone": "01079996001",
                "parent_phone": "01079996002",
                "omr_code": "996001",
                "uses_identifier": False,
                "school_type": "HIGH",
                "grade": 1,
            },
        ).student
        soft_delete_student(deleted_student, tenant=tenant)
        deleted_student.refresh_from_db()
        deleted_at = deleted_student.deleted_at
        registration = self._registration(
            tenant=tenant,
            name=deleted_student.name,
            username="disabled-resolve-request",
            phone="01079996001",
            parent_phone="01079996002",
        )
        staff = User.objects.create_user(
            username="disabled-registration-resolve-staff",
            password="staff-password",
            tenant=tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(tenant=tenant, user=staff, role="teacher")
        request = self.factory.post(
            f"/api/v1/students/registration_requests/{registration.id}/resolve_deleted/",
            {"student_id": deleted_student.id},
            format="json",
        )
        force_authenticate(request, user=staff)
        request.tenant = tenant

        response = RegistrationRequestViewSet.as_view({"post": "resolve_deleted"})(
            request,
            pk=registration.id,
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["code"], "self_registration_disabled")
        registration.refresh_from_db()
        deleted_student.refresh_from_db()
        self.assertEqual(registration.tenant_id, tenant.id)
        self.assertEqual(registration.status, StudentRegistrationRequest.PENDING)
        self.assertIsNone(registration.student_id)
        self.assertEqual(deleted_student.deleted_at, deleted_at)

    def test_enabled_tenant_generic_mutations_are_read_only(self):
        registration = self._registration(username="enabled-read-only")
        other_tenant = Tenant.objects.create(
            name="다른 가입 학원",
            code="registration-put-other-tenant",
            is_active=True,
        )
        other_student = self._student(ps_number="PUT-OTHER-STUDENT")
        staff = User.objects.create_user(
            username="enabled-registration-read-only-staff",
            password="staff-password",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=staff, role="teacher")

        cases = (
            (
                "patch",
                "partial_update",
                {"status": StudentRegistrationRequest.APPROVED},
            ),
            (
                "put",
                "update",
                {
                    "tenant": other_tenant.id,
                    "student": other_student.id,
                    "status": StudentRegistrationRequest.APPROVED,
                },
            ),
            ("delete", "destroy", None),
        )
        for method, action_name, data in cases:
            with self.subTest(method=method):
                request_factory = getattr(self.factory, method)
                request = request_factory(
                    f"/api/v1/students/registration_requests/{registration.id}/",
                    data=data,
                    format="json",
                )
                force_authenticate(request, user=staff)
                request.tenant = self.tenant

                response = RegistrationRequestViewSet.as_view({method: action_name})(
                    request,
                    pk=registration.id,
                )

                self.assertEqual(response.status_code, 405)
                self.assertEqual(response.data["code"], "registration_request_read_only")
                registration.refresh_from_db()
                self.assertEqual(registration.tenant_id, self.tenant.id)
                self.assertEqual(registration.status, StudentRegistrationRequest.PENDING)
                self.assertIsNone(registration.student_id)

    def test_openapi_seals_registration_history_and_documents_disabled_policy(self):
        schema_path = Path(__file__).resolve().parents[4] / "schema" / "openapi.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        paths = schema["paths"]

        detail_operations = paths["/api/v1/students/registration_requests/{id}/"]
        self.assertEqual(set(detail_operations), {"get"})

        guarded_operations = (
            ("/api/v1/students/registration_requests/", "get"),
            ("/api/v1/students/registration_requests/", "post"),
            ("/api/v1/students/registration_requests/bulk_approve/", "post"),
            ("/api/v1/students/registration_requests/bulk_reject/", "post"),
            ("/api/v1/students/registration_requests/check_duplicate/", "post"),
            ("/api/v1/students/registration_requests/{id}/approve/", "post"),
            ("/api/v1/students/registration_requests/{id}/reject/", "post"),
            ("/api/v1/students/registration_requests/{id}/resolve_deleted/", "post"),
        )
        for path, method in guarded_operations:
            with self.subTest(path=path, method=method):
                response_schema = paths[path][method]["responses"]["403"]["content"][
                    "application/json"
                ]["schema"]
                self.assertEqual(
                    response_schema["$ref"],
                    "#/components/schemas/SelfRegistrationDisabledError",
                )

        list_operation = paths["/api/v1/students/registration_requests/"]["get"]
        status_parameters = [
            parameter
            for parameter in list_operation["parameters"]
            if parameter["in"] == "query" and parameter["name"] == "status"
        ]
        self.assertEqual(len(status_parameters), 1)
        self.assertEqual(
            status_parameters[0]["schema"]["enum"],
            ["approved", "pending", "rejected"],
        )
        self.assertIn(
            "status=pending",
            list_operation["responses"]["403"]["description"],
        )

        error_schema = schema["components"]["schemas"]["SelfRegistrationDisabledError"]
        code_ref = error_schema["properties"]["code"]["$ref"]
        code_schema = schema["components"]["schemas"][code_ref.rsplit("/", 1)[-1]]
        self.assertEqual(
            code_schema["enum"],
            ["self_registration_disabled"],
        )

    def test_enabled_tenant_pending_list_and_reject_remain_available(self):
        registration = self._registration(username="enabled-pending")
        staff = User.objects.create_user(
            username="enabled-registration-staff",
            password="staff-password",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=staff, role="teacher")
        list_request = self.factory.get(
            "/api/v1/students/registration_requests/",
            {"status": StudentRegistrationRequest.PENDING},
        )
        force_authenticate(list_request, user=staff)
        list_request.tenant = self.tenant

        list_response = RegistrationRequestViewSet.as_view({"get": "list"})(list_request)

        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(list_response.data["count"], 1)

        reject_request = self.factory.post(
            f"/api/v1/students/registration_requests/{registration.id}/reject/",
            {},
            format="json",
        )
        force_authenticate(reject_request, user=staff)
        reject_request.tenant = self.tenant
        reject_response = RegistrationRequestViewSet.as_view({"post": "reject"})(
            reject_request,
            pk=registration.id,
        )

        self.assertEqual(reject_response.status_code, 200)
        registration.refresh_from_db()
        self.assertEqual(registration.status, StudentRegistrationRequest.REJECTED)
