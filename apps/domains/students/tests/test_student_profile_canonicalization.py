# PATH: apps/domains/students/tests/test_student_profile_canonicalization.py
import base64
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.permissions import IsAuthenticated
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.core.models import Tenant, TenantMembership
from apps.core.models.user import user_internal_username
from apps.core.models.user import user_display_username
from apps.core.permissions import IsStudent
from apps.domains.parents.test_support import (
    create_parent_account_fixture,
    parent_account_fixture_exists,
)
from apps.domains.students.models import Student
from apps.domains.students.selectors import students_for_tenant
from apps.domains.students.views import StudentViewSet
from apps.domains.student_app.profile.views import StudentProfileView

User = get_user_model()


def make_tenant(code="canon"):
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


def make_student(tenant, *, ps_number="S10001", phone=None, parent_phone="01011112222"):
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
        phone=phone,
        parent_phone=parent_phone,
        omr_code=(phone or parent_phone)[-8:],
        school_type="HIGH",
        grade=1,
    )
    TenantMembership.ensure_active(tenant=tenant, user=user, role="student")
    return student


class StudentSelectorTenantGuardTests(TestCase):
    def test_students_for_tenant_requires_tenant(self):
        with self.assertRaises(ValueError):
            students_for_tenant(None)


class StudentProfileCanonicalizationTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.tenant = make_tenant()
        self.admin = make_admin(self.tenant)
        self.student = make_student(self.tenant)

    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    def test_admin_partial_update_relinks_parent_and_recomputes_parent_omr(self, _send_mock):
        request = self.factory.patch(
            f"/api/v1/students/{self.student.id}/",
            data={
                "parent_phone": "01033334444",
                "parent_initial_password": "chosen3333",
            },
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant

        response = StudentViewSet.as_view({"patch": "partial_update"})(request, pk=self.student.id)

        self.assertEqual(response.status_code, 200)
        self.student.refresh_from_db()
        self.assertEqual(self.student.parent_phone, "01033334444")
        self.assertEqual(self.student.omr_code, "33334444")
        self.assertIsNotNone(self.student.parent_id)
        self.assertEqual(self.student.parent.phone, "01033334444")

    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    def test_admin_add_student_phone_sends_student_account_notice(self, send_mock):
        no_phone_student = make_student(self.tenant, ps_number="S-NOPHONE", phone=None)
        request = self.factory.patch(
            f"/api/v1/students/{no_phone_student.id}/",
            data={"phone": "01099998888"},
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant

        response = StudentViewSet.as_view({"patch": "partial_update"})(request, pk=no_phone_student.id)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(send_mock.call_args.kwargs["trigger"], "registration_approved_student")
        self.assertEqual(send_mock.call_args.kwargs["to"], "01099998888")
        self.assertEqual(
            send_mock.call_args.kwargs["replacements"]["학생비밀번호"],
            "변경되지 않음",
        )

    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    def test_admin_adds_first_real_phone_and_moves_identifier_account_to_phone(self, send_mock):
        no_phone_student = make_student(self.tenant, ps_number="S-NOPHONE-ID", phone=None)
        no_phone_student.uses_identifier = True
        no_phone_student.save(update_fields=["uses_identifier"])
        request = self.factory.patch(
            f"/api/v1/students/{no_phone_student.id}/",
            data={"phone": "01099997777"},
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant

        response = StudentViewSet.as_view({"patch": "partial_update"})(
            request,
            pk=no_phone_student.id,
        )

        self.assertEqual(response.status_code, 200, response.data)
        no_phone_student.refresh_from_db()
        no_phone_student.user.refresh_from_db()
        self.assertEqual(no_phone_student.phone, "01099997777")
        self.assertEqual(no_phone_student.user.phone, "01099997777")
        self.assertEqual(no_phone_student.ps_number, "01099997777")
        self.assertEqual(user_display_username(no_phone_student.user), "01099997777")
        self.assertEqual(no_phone_student.omr_code, "99997777")
        self.assertFalse(no_phone_student.uses_identifier)
        self.assertEqual(
            send_mock.call_args.kwargs["replacements"]["학생아이디"],
            "01099997777",
        )

    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=False)
    def test_first_real_phone_account_transition_rolls_back_when_notice_fails(self, _send_mock):
        no_phone_student = make_student(self.tenant, ps_number="S-NOPHONE-ROLLBACK", phone=None)
        no_phone_student.uses_identifier = True
        no_phone_student.save(update_fields=["uses_identifier"])
        original_username = no_phone_student.user.username
        request = self.factory.patch(
            f"/api/v1/students/{no_phone_student.id}/",
            data={"phone": "01099996666"},
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant

        response = StudentViewSet.as_view({"patch": "partial_update"})(
            request,
            pk=no_phone_student.id,
        )

        self.assertEqual(response.status_code, 503)
        no_phone_student.refresh_from_db()
        no_phone_student.user.refresh_from_db()
        self.assertIsNone(no_phone_student.phone)
        self.assertFalse(no_phone_student.user.phone)
        self.assertEqual(no_phone_student.ps_number, "S-NOPHONE-ROLLBACK")
        self.assertEqual(no_phone_student.user.username, original_username)
        self.assertTrue(no_phone_student.uses_identifier)

    def test_admin_matching_contact_is_stored_as_parent_only(self):
        request = self.factory.patch(
            f"/api/v1/students/{self.student.id}/",
            data={"phone": self.student.parent_phone},
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant

        response = StudentViewSet.as_view({"patch": "partial_update"})(
            request,
            pk=self.student.id,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.student.refresh_from_db()
        self.student.user.refresh_from_db()
        self.assertIsNone(self.student.phone)
        self.assertIsNone(self.student.user.phone)
        self.assertTrue(self.student.uses_identifier)
        self.assertEqual(self.student.omr_code, "11112222")

    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    def test_admin_phone_change_preserves_custom_id_when_legacy_flag_is_stale(self, send_mock):
        custom_student = make_student(
            self.tenant,
            ps_number="S-CUSTOM-PHONE",
            phone="01099994444",
        )
        custom_student.uses_identifier = True
        custom_student.save(update_fields=["uses_identifier"])
        original_ps_number = custom_student.ps_number
        original_username = custom_student.user.username
        request = self.factory.patch(
            f"/api/v1/students/{custom_student.id}/",
            data={"phone": "01099995555"},
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant

        response = StudentViewSet.as_view({"patch": "partial_update"})(
            request,
            pk=custom_student.id,
        )

        self.assertEqual(response.status_code, 200, response.data)
        custom_student.refresh_from_db()
        custom_student.user.refresh_from_db()
        self.assertEqual(custom_student.phone, "01099995555")
        self.assertEqual(custom_student.ps_number, original_ps_number)
        self.assertEqual(custom_student.user.username, original_username)
        self.assertFalse(custom_student.uses_identifier)
        self.assertEqual(
            send_mock.call_args.kwargs["replacements"]["학생아이디"],
            original_ps_number,
        )

    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    def test_admin_student_id_change_sends_student_account_notice(self, send_mock):
        request = self.factory.patch(
            f"/api/v1/students/{self.student.id}/",
            data={"ps_number": "S-CHANGED"},
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant

        response = StudentViewSet.as_view({"patch": "partial_update"})(request, pk=self.student.id)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(send_mock.call_args.kwargs["trigger"], "registration_approved_student")
        self.assertEqual(send_mock.call_args.kwargs["replacements"]["학생아이디"], "S-CHANGED")

    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    def test_admin_phone_login_id_change_uses_canonical_digits(self, send_mock):
        request = self.factory.patch(
            f"/api/v1/students/{self.student.id}/",
            data={"ps_number": "010-9999-8888"},
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant

        response = StudentViewSet.as_view({"patch": "partial_update"})(
            request,
            pk=self.student.id,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.student.refresh_from_db()
        self.student.user.refresh_from_db()
        self.assertEqual(self.student.ps_number, "01099998888")
        self.assertEqual(user_display_username(self.student.user), "01099998888")
        self.assertEqual(
            send_mock.call_args.kwargs["replacements"]["학생아이디"],
            "01099998888",
        )

    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    def test_admin_cannot_change_student_id_to_parent_login_id(self, send_mock):
        request = self.factory.patch(
            f"/api/v1/students/{self.student.id}/",
            data={"ps_number": "010-1111-2222"},
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant

        response = StudentViewSet.as_view({"patch": "partial_update"})(
            request,
            pk=self.student.id,
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("ps_number", response.data)
        self.student.refresh_from_db()
        self.assertEqual(self.student.ps_number, "S10001")
        send_mock.assert_not_called()

    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    def test_admin_parent_phone_change_sends_parent_account_notice(self, send_mock):
        request = self.factory.patch(
            f"/api/v1/students/{self.student.id}/",
            data={
                "parent_phone": "01022223333",
                "parent_initial_password": "chosen2222",
            },
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant

        response = StudentViewSet.as_view({"patch": "partial_update"})(request, pk=self.student.id)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(send_mock.call_args.kwargs["trigger"], "registration_approved_parent")
        self.assertEqual(send_mock.call_args.kwargs["to"], "01022223333")
        self.assertEqual(
            send_mock.call_args.kwargs["replacements"]["학부모비밀번호"],
            "chosen2222",
        )

    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    def test_admin_parent_phone_change_requires_password_for_new_account(self, send_mock):
        original_parent_phone = self.student.parent_phone
        request = self.factory.patch(
            f"/api/v1/students/{self.student.id}/",
            data={"parent_phone": "01022223333"},
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant

        response = StudentViewSet.as_view({"patch": "partial_update"})(request, pk=self.student.id)

        self.assertEqual(response.status_code, 400)
        self.assertIn("parent_initial_password", response.data)
        self.student.refresh_from_db()
        self.assertEqual(self.student.parent_phone, original_parent_phone)
        self.assertFalse(
            parent_account_fixture_exists(
                tenant=self.tenant,
                parent_phone="01022223333",
            )
        )
        send_mock.assert_not_called()

    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    def test_admin_parent_phone_change_reuses_existing_password(self, send_mock):
        existing = create_parent_account_fixture(
            tenant=self.tenant,
            parent_phone="01022223333",
            student_name=self.student.name,
            initial_password="existing-parent-password",
        ).parent

        request = self.factory.patch(
            f"/api/v1/students/{self.student.id}/",
            data={"parent_phone": "01022223333"},
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant

        response = StudentViewSet.as_view({"patch": "partial_update"})(request, pk=self.student.id)

        self.assertEqual(response.status_code, 200)
        self.student.refresh_from_db()
        existing.user.refresh_from_db()
        self.assertEqual(self.student.parent_id, existing.id)
        self.assertTrue(existing.user.check_password("existing-parent-password"))
        self.assertEqual(
            send_mock.call_args.kwargs["replacements"]["학부모비밀번호"],
            "변경되지 않음",
        )

    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=False)
    def test_admin_parent_phone_change_rolls_back_when_notice_delivery_fails(self, _send_mock):
        original_parent_phone = self.student.parent_phone
        original_omr = self.student.omr_code
        request = self.factory.patch(
            f"/api/v1/students/{self.student.id}/",
            data={
                "parent_phone": "01022223333",
                "parent_initial_password": "chosen2222",
            },
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant

        response = StudentViewSet.as_view({"patch": "partial_update"})(request, pk=self.student.id)

        self.assertEqual(response.status_code, 503)
        self.student.refresh_from_db()
        self.assertEqual(self.student.parent_phone, original_parent_phone)
        self.assertEqual(self.student.omr_code, original_omr)

    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    def test_admin_repairs_missing_parent_account_for_unchanged_phone(self, send_mock):
        request = self.factory.patch(
            f"/api/v1/students/{self.student.id}/",
            data={
                "parent_phone": self.student.parent_phone,
                "parent_initial_password": "chosen-repair-password",
            },
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant

        response = StudentViewSet.as_view({"patch": "partial_update"})(
            request,
            pk=self.student.id,
        )

        self.assertEqual(response.status_code, 200)
        self.student.refresh_from_db()
        self.assertIsNotNone(self.student.parent_id)
        self.assertEqual(self.student.parent.phone, self.student.parent_phone)
        self.assertTrue(
            self.student.parent.user.check_password("chosen-repair-password")
        )
        self.assertEqual(
            send_mock.call_args.kwargs["replacements"]["학부모비밀번호"],
            "chosen-repair-password",
        )

    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    def test_student_app_password_change_sends_student_notice(self, send_mock):
        request = self.factory.patch(
            "/api/v1/student-app/me/",
            data={"current_password": "test1234", "new_password": "newpass1234"},
            format="json",
        )
        force_authenticate(request, user=self.student.user)
        request.tenant = self.tenant

        response = StudentProfileView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        self.student.user.refresh_from_db()
        self.assertTrue(self.student.user.check_password("newpass1234"))
        self.assertEqual(send_mock.call_args.kwargs["trigger"], "password_reset_student")
        self.assertEqual(send_mock.call_args.kwargs["replacements"]["학생비밀번호"], "newpass1234")

    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=False)
    def test_student_app_password_change_rolls_back_when_notice_delivery_fails(self, _send_mock):
        request = self.factory.patch(
            "/api/v1/student-app/me/",
            data={"current_password": "test1234", "new_password": "newpass1234", "address": "새 주소"},
            format="json",
        )
        force_authenticate(request, user=self.student.user)
        request.tenant = self.tenant

        response = StudentProfileView.as_view()(request)

        self.assertEqual(response.status_code, 503)
        self.student.refresh_from_db()
        self.student.user.refresh_from_db()
        self.assertTrue(self.student.user.check_password("test1234"))
        self.assertFalse(self.student.user.must_change_password)
        self.assertEqual(self.student.user.token_version, 0)
        self.assertNotEqual(self.student.address, "새 주소")

    @patch("apps.infrastructure.storage.r2.delete_object_r2_storage")
    @patch("academy.adapters.storage.r2_objects.upload_fileobj", side_effect=RuntimeError("r2 unavailable"))
    def test_student_profile_photo_storage_failure_is_not_reported_as_success(
        self,
        _upload_mock,
        _delete_mock,
    ):
        photo = SimpleUploadedFile(
            "profile.png",
            base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
            ),
            content_type="image/png",
        )
        request = self.factory.patch(
            "/api/v1/student-app/me/",
            data={"profile_photo": photo},
            format="multipart",
        )
        force_authenticate(request, user=self.student.user)
        request.tenant = self.tenant

        response = StudentProfileView.as_view()(request)

        self.assertEqual(response.status_code, 503)
        self.student.refresh_from_db()
        self.assertFalse(self.student.profile_photo_r2_key)
        self.assertFalse(bool(self.student.profile_photo))

    @patch("apps.infrastructure.storage.r2.delete_object_r2_storage")
    @patch("academy.adapters.storage.r2_objects.upload_fileobj", side_effect=RuntimeError("r2 unavailable"))
    def test_legacy_student_profile_photo_uses_same_r2_failure_boundary(
        self,
        _upload_mock,
        _delete_mock,
    ):
        photo = SimpleUploadedFile(
            "profile.png",
            base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
            ),
            content_type="image/png",
        )
        request = self.factory.patch(
            "/api/v1/students/me/",
            data={"profile_photo": photo},
            format="multipart",
        )
        force_authenticate(request, user=self.student.user)
        request.tenant = self.tenant

        response = StudentViewSet.as_view(
            {"patch": "me"},
            permission_classes=[IsAuthenticated, IsStudent],
        )(request)

        self.assertEqual(response.status_code, 503)
        self.student.refresh_from_db()
        self.assertFalse(self.student.profile_photo_r2_key)
        self.assertFalse(bool(self.student.profile_photo))

    @patch("academy.adapters.storage.r2_presign.create_presigned_get_url", return_value="https://r2.example/new")
    @patch("apps.infrastructure.storage.r2.delete_object_r2_storage")
    @patch("academy.adapters.storage.r2_objects.upload_fileobj")
    def test_student_profile_photo_replacement_cleans_previous_object(
        self,
        upload_mock,
        delete_mock,
        _presign_mock,
    ):
        self.student.profile_photo_r2_key = "tenants/old/profile.png"
        self.student.save(update_fields=["profile_photo_r2_key"])
        photo = SimpleUploadedFile(
            "profile.png",
            base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
            ),
            content_type="image/png",
        )
        request = self.factory.patch(
            "/api/v1/student-app/me/",
            data={"profile_photo": photo},
            format="multipart",
        )
        force_authenticate(request, user=self.student.user)
        request.tenant = self.tenant

        with self.captureOnCommitCallbacks(execute=True):
            response = StudentProfileView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        self.student.refresh_from_db()
        self.assertNotEqual(self.student.profile_photo_r2_key, "tenants/old/profile.png")
        upload_mock.assert_called_once()
        delete_mock.assert_called_once_with(key="tenants/old/profile.png", timeout_seconds=5)

    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    def test_student_app_username_change_sends_student_account_notice(self, send_mock):
        request = self.factory.patch(
            "/api/v1/student-app/me/",
            data={"username": "S-SELF-APP"},
            format="json",
        )
        force_authenticate(request, user=self.student.user)
        request.tenant = self.tenant

        response = StudentProfileView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        self.student.refresh_from_db()
        self.student.user.refresh_from_db()
        self.assertEqual(self.student.ps_number, "S-SELF-APP")
        self.assertEqual(self.student.user.username, user_internal_username(self.tenant, "S-SELF-APP"))
        self.assertEqual(send_mock.call_args.kwargs["trigger"], "registration_approved_student")
        self.assertEqual(send_mock.call_args.kwargs["replacements"]["학생아이디"], "S-SELF-APP")
        self.assertEqual(send_mock.call_args.kwargs["replacements"]["학생비밀번호"], "변경되지 않음")

    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=False)
    def test_student_app_username_change_rolls_back_when_notice_delivery_fails(self, _send_mock):
        original_username = self.student.user.username
        original_ps_number = self.student.ps_number
        request = self.factory.patch(
            "/api/v1/student-app/me/",
            data={"username": "S-SELF-APP"},
            format="json",
        )
        force_authenticate(request, user=self.student.user)
        request.tenant = self.tenant

        response = StudentProfileView.as_view()(request)

        self.assertEqual(response.status_code, 503)
        self.student.refresh_from_db()
        self.student.user.refresh_from_db()
        self.assertEqual(self.student.ps_number, original_ps_number)
        self.assertEqual(self.student.user.username, original_username)

    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    def test_parent_password_change_sends_parent_notice(self, send_mock):
        from apps.core.views.auth import ChangePasswordView

        parent_result = create_parent_account_fixture(
            tenant=self.tenant,
            parent_phone="01044445555",
            student_name=self.student.name,
            initial_password="parent1234",
        )
        parent = parent_result.parent
        parent_user = parent.user
        parent_user.set_password("parent1234")
        parent_user.save(update_fields=["password"])
        self.student.parent = parent
        self.student.parent_phone = parent.phone
        self.student.save(update_fields=["parent", "parent_phone"])

        request = self.factory.post(
            "/api/v1/core/me/profile/change-password/",
            data={"old_password": "parent1234", "new_password": "parent9999"},
            format="json",
        )
        force_authenticate(request, user=parent_user)
        request.tenant = self.tenant

        response = ChangePasswordView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        parent_user.refresh_from_db()
        self.assertTrue(parent_user.check_password("parent9999"))
        self.assertEqual(send_mock.call_args.kwargs["trigger"], "password_reset_parent")
        self.assertEqual(send_mock.call_args.kwargs["to"], "01044445555")
        self.assertEqual(send_mock.call_args.kwargs["replacements"]["학부모비밀번호"], "parent9999")

    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    def test_student_self_endpoints_reject_parent_account_relink(self, send_mock):
        create_parent_account_fixture(
            tenant=self.tenant,
            parent_phone="01077778888",
            student_name=self.student.name,
            initial_password="existing-parent-password",
        )
        original_parent_phone = self.student.parent_phone

        cases = (
            (
                "/api/v1/student-app/me/",
                StudentProfileView.as_view(),
            ),
            (
                "/api/v1/students/me/",
                StudentViewSet.as_view(
                    {"patch": "me"},
                    permission_classes=[IsAuthenticated, IsStudent],
                ),
            ),
        )
        for path, view in cases:
            with self.subTest(path=path):
                request = self.factory.patch(
                    path,
                    data={"parent_phone": "01077778888"},
                    format="json",
                )
                force_authenticate(request, user=self.student.user)
                request.tenant = self.tenant
                response = view(request)
                self.assertEqual(response.status_code, 400)
                self.assertIn("parent_phone", response.data)

        self.student.refresh_from_db()
        self.assertEqual(self.student.parent_phone, original_parent_phone)
        send_mock.assert_not_called()

    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    def test_students_me_username_change_sends_student_account_notice(self, send_mock):
        request = self.factory.patch(
            "/api/v1/students/me/",
            data={"username": "S-SELF-LEGACY"},
            format="json",
        )
        force_authenticate(request, user=self.student.user)
        request.tenant = self.tenant

        response = StudentViewSet.as_view(
            {"patch": "me"},
            permission_classes=[IsAuthenticated, IsStudent],
        )(request)

        self.assertEqual(response.status_code, 200)
        self.student.refresh_from_db()
        self.student.user.refresh_from_db()
        self.assertEqual(self.student.ps_number, "S-SELF-LEGACY")
        self.assertEqual(self.student.user.username, user_internal_username(self.tenant, "S-SELF-LEGACY"))
        self.assertEqual(send_mock.call_args.kwargs["trigger"], "registration_approved_student")
        self.assertEqual(send_mock.call_args.kwargs["replacements"]["학생아이디"], "S-SELF-LEGACY")

    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=False)
    def test_students_me_username_change_rolls_back_when_notice_delivery_fails(self, _send_mock):
        original_username = self.student.user.username
        original_ps_number = self.student.ps_number
        request = self.factory.patch(
            "/api/v1/students/me/",
            data={"username": "S-SELF-LEGACY"},
            format="json",
        )
        force_authenticate(request, user=self.student.user)
        request.tenant = self.tenant

        response = StudentViewSet.as_view(
            {"patch": "me"},
            permission_classes=[IsAuthenticated, IsStudent],
        )(request)

        self.assertEqual(response.status_code, 503)
        self.student.refresh_from_db()
        self.student.user.refresh_from_db()
        self.assertEqual(self.student.ps_number, original_ps_number)
        self.assertEqual(self.student.user.username, original_username)

    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    def test_students_me_password_change_sends_student_notice(self, send_mock):
        request = self.factory.patch(
            "/api/v1/students/me/",
            data={"current_password": "test1234", "new_password": "legacy9999"},
            format="json",
        )
        force_authenticate(request, user=self.student.user)
        request.tenant = self.tenant

        response = StudentViewSet.as_view(
            {"patch": "me"},
            permission_classes=[IsAuthenticated, IsStudent],
        )(request)

        self.assertEqual(response.status_code, 200)
        self.student.user.refresh_from_db()
        self.assertTrue(self.student.user.check_password("legacy9999"))
        self.assertEqual(send_mock.call_args.kwargs["trigger"], "password_reset_student")
        self.assertEqual(send_mock.call_args.kwargs["replacements"]["학생비밀번호"], "legacy9999")
