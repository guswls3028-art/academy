from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient, APIRequestFactory
from rest_framework_simplejwt.tokens import AccessToken

from apps.core.models import PendingPasswordReset, Tenant, TenantMembership
from apps.core.models.user import user_internal_username
from apps.core.services.password import generate_temp_password
from apps.domains.parents.test_support import create_parent_account_fixture
from apps.domains.students.models import Student
from apps.domains.students.views.credential_views import SendExistingCredentialsView
from apps.domains.students.views.password_views import (
    _pw_reset_cache_key,
    StudentPasswordFindRequestView,
    StudentPasswordFindVerifyView,
    StudentPasswordResetSendView,
)


class StudentPasswordResetSafetyTests(TestCase):
    def setUp(self):
        cache.clear()
        self.factory = APIRequestFactory()
        self.tenant = Tenant.objects.create(name="비번안전학원", code="pw-safe")
        self.owner_settings = override_settings(OWNER_TENANT_ID=self.tenant.id)
        self.owner_settings.enable()
        self.addCleanup(self.owner_settings.disable)
        User = get_user_model()
        self.user = User.objects.create_user(
            username=user_internal_username(self.tenant, "S001"),
            password="oldpw123",
            tenant=self.tenant,
            must_change_password=False,
            token_version=0,
        )
        self.student = Student.objects.create(
            tenant=self.tenant,
            user=self.user,
            ps_number="S001",
            omr_code="11112222",
            name="홍길동",
            phone="01011112222",
            parent_phone="01033334444",
        )

    def _post(self, view_class, path: str, data: dict):
        request = self.factory.post(path, data, format="json")
        request.tenant = self.tenant
        return view_class.as_view()(request)

    def _staff_auth_headers(self, *, role: str = "teacher") -> dict[str, str]:
        User = get_user_model()
        staff = User.objects.create_user(
            username=user_internal_username(self.tenant, f"{role}01"),
            password="staffpw123",
            tenant=self.tenant,
            token_version=0,
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=staff, role=role)
        return self._headers_for_staff(staff)

    def _headers_for_staff(self, staff) -> dict[str, str]:
        token = AccessToken.for_user(staff)
        token["tenant_id"] = self.tenant.id
        token["token_version"] = 0
        return {
            "HTTP_HOST": "api.hakwonplus.com",
            "HTTP_X_TENANT_CODE": self.tenant.code,
            "HTTP_AUTHORIZATION": f"Bearer {str(token)}",
        }

    @override_settings(
        ALLOWED_HOSTS=["api.hakwonplus.com", "testserver"],
        TENANT_HEADER_CODE_ALLOWED_HOSTS=("api.hakwonplus.com",),
    )
    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    def test_teacher_reset_student_password_notifies_even_with_legacy_skip_flag(self, send_mock):
        response = APIClient().post(
            "/api/v1/students/password_reset_send/",
            {
                "target": "student",
                "student_name": self.student.name,
                "student_ps_number": self.student.ps_number,
                "temp_password": "4444",
                "skip_notify": True,
            },
            format="json",
            **self._staff_auth_headers(role="teacher"),
        )

        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("4444"))
        self.assertTrue(self.user.must_change_password)
        self.assertEqual(self.user.token_version, 1)
        self.assertFalse(PendingPasswordReset.objects.filter(user=self.user).exists())
        self.assertEqual(send_mock.call_args.kwargs["trigger"], "password_reset_student")

    @override_settings(
        ALLOWED_HOSTS=["api.hakwonplus.com", "testserver"],
        TENANT_HEADER_CODE_ALLOWED_HOSTS=("api.hakwonplus.com",),
    )
    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    def test_teacher_reset_requires_explicit_password_without_mutation(self, send_mock):
        response = APIClient().post(
            "/api/v1/students/password_reset_send/",
            {
                "target": "student",
                "student_name": self.student.name,
                "student_ps_number": self.student.ps_number,
            },
            format="json",
            **self._staff_auth_headers(role="teacher"),
        )

        self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("oldpw123"))
        self.assertFalse(self.user.must_change_password)
        self.assertEqual(self.user.token_version, 0)
        send_mock.assert_not_called()

    @override_settings(
        ALLOWED_HOSTS=["api.hakwonplus.com", "testserver"],
        TENANT_HEADER_CODE_ALLOWED_HOSTS=("api.hakwonplus.com",),
    )
    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=False)
    def test_staff_reset_notice_failure_rolls_back_token_and_password_state(self, _send_mock):
        response = APIClient().post(
            "/api/v1/students/password_reset_send/",
            {
                "target": "student",
                "student_name": self.student.name,
                "student_ps_number": self.student.ps_number,
                "temp_password": "4444",
            },
            format="json",
            **self._staff_auth_headers(role="teacher"),
        )

        self.assertEqual(response.status_code, 503)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("oldpw123"))
        self.assertFalse(self.user.must_change_password)
        self.assertEqual(self.user.token_version, 0)
        self.assertFalse(PendingPasswordReset.objects.filter(user=self.user).exists())

    @override_settings(
        ALLOWED_HOSTS=["api.hakwonplus.com", "testserver"],
        TENANT_HEADER_CODE_ALLOWED_HOSTS=("api.hakwonplus.com",),
    )
    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    def test_staff_password_reset_uses_staff_throttle_not_public_sms_ip_bucket(self, send_mock):
        User = get_user_model()
        staff = User.objects.create_user(
            username=user_internal_username(self.tenant, "teacher-throttle"),
            password="staffpw123",
            tenant=self.tenant,
            token_version=0,
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=staff, role="teacher")
        headers = self._headers_for_staff(staff)

        for idx in range(6):
            response = APIClient().post(
                "/api/v1/students/password_reset_send/",
                {
                    "target": "student",
                    "student_name": self.student.name,
                    "student_ps_number": self.student.ps_number,
                    "temp_password": f"reset{idx}pw",
                    "skip_notify": True,
                },
                format="json",
                **headers,
            )
            self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(send_mock.call_count, 6)

    @override_settings(
        ALLOWED_HOSTS=["api.hakwonplus.com", "testserver"],
        TENANT_HEADER_CODE_ALLOWED_HOSTS=("api.hakwonplus.com",),
    )
    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    def test_teacher_can_reset_student_password_by_student_phone_with_notice(self, send_mock):
        response = APIClient().post(
            "/api/v1/students/password_reset_send/",
            {
                "target": "student",
                "student_name": self.student.name,
                "student_phone": self.student.phone,
                "temp_password": "5555",
                "skip_notify": True,
            },
            format="json",
            **self._staff_auth_headers(role="teacher"),
        )

        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("5555"))
        self.assertTrue(self.user.must_change_password)
        self.assertEqual(self.user.token_version, 1)
        self.assertEqual(send_mock.call_args.kwargs["to"], self.student.phone)

    @override_settings(
        ALLOWED_HOSTS=["api.hakwonplus.com", "testserver"],
        TENANT_HEADER_CODE_ALLOWED_HOSTS=("api.hakwonplus.com",),
    )
    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    def test_staff_reset_prefers_ps_number_over_phone_when_both_are_sent(self, send_mock):
        User = get_user_model()
        other_user = User.objects.create_user(
            username=user_internal_username(self.tenant, "S002"),
            password="otherpw123",
            tenant=self.tenant,
            must_change_password=False,
            token_version=0,
        )
        other_student = Student.objects.create(
            tenant=self.tenant,
            user=other_user,
            ps_number="S002",
            omr_code="99998888",
            name=self.student.name,
            phone="01099998888",
            parent_phone="01077776666",
        )

        response = APIClient().post(
            "/api/v1/students/password_reset_send/",
            {
                "target": "student",
                "student_name": self.student.name,
                "student_ps_number": self.student.ps_number,
                "student_phone": other_student.phone,
                "temp_password": "6666",
                "skip_notify": True,
            },
            format="json",
            **self._staff_auth_headers(role="teacher"),
        )

        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        other_user.refresh_from_db()
        self.assertTrue(self.user.check_password("6666"))
        self.assertFalse(other_user.check_password("6666"))
        self.assertTrue(other_user.check_password("otherpw123"))
        self.assertEqual(send_mock.call_args.kwargs["to"], self.student.phone)

    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    @override_settings(
        ALLOWED_HOSTS=["api.hakwonplus.com", "testserver"],
        TENANT_HEADER_CODE_ALLOWED_HOSTS=("api.hakwonplus.com",),
    )
    def test_staff_password_reset_uses_account_recovery_log_metadata(self, send_mock):
        response = APIClient().post(
            "/api/v1/students/password_reset_send/",
            {
                "target": "student",
                "student_name": self.student.name,
                "student_ps_number": self.student.ps_number,
                "temp_password": "7777",
            },
            format="json",
            **self._staff_auth_headers(role="teacher"),
        )

        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("7777"))
        kwargs = send_mock.call_args.kwargs
        self.assertEqual(kwargs["source_tenant_id"], self.tenant.id)
        self.assertEqual(kwargs["log_target_type"], "account")
        self.assertEqual(kwargs["log_target_id"], f"student:{self.student.id}")
        self.assertEqual(kwargs["log_target_name"], self.student.name)

    @override_settings(
        ALLOWED_HOSTS=["api.hakwonplus.com", "testserver"],
        TENANT_HEADER_CODE_ALLOWED_HOSTS=("api.hakwonplus.com",),
    )
    def test_stale_staff_jwt_cannot_use_privileged_reset_options(self):
        User = get_user_model()
        staff = User.objects.create_user(
            username=user_internal_username(self.tenant, "staff01"),
            password="staffpw123",
            tenant=self.tenant,
            token_version=0,
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=staff, role="teacher")
        token = AccessToken.for_user(staff)
        token["tenant_id"] = self.tenant.id
        token["token_version"] = 0
        staff.token_version = 1
        staff.save(update_fields=["token_version"])

        response = APIClient().post(
            "/api/v1/students/password_reset_send/",
            {
                "target": "student",
                "student_name": self.student.name,
                "student_phone": self.student.phone,
                "temp_password": "44445555",
                "skip_notify": True,
            },
            format="json",
            HTTP_HOST="api.hakwonplus.com",
            HTTP_X_TENANT_CODE=self.tenant.code,
            HTTP_AUTHORIZATION=f"Bearer {str(token)}",
        )

        self.assertIn(response.status_code, (401, 403))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("oldpw123"))
        self.assertFalse(self.user.must_change_password)
        self.assertFalse(PendingPasswordReset.objects.filter(user=self.user).exists())

    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=False)
    def test_public_password_reset_send_failure_keeps_current_password_state(self, _send):
        response = self._post(
            StudentPasswordResetSendView,
            "/api/v1/students/password_reset_send/",
            {
                "target": "student",
                "student_name": self.student.name,
                "student_phone": self.student.phone,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("message", response.data)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("oldpw123"))
        self.assertFalse(self.user.must_change_password)
        self.assertEqual(self.user.token_version, 0)
        self.assertFalse(PendingPasswordReset.objects.filter(user=self.user).exists())

    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    def test_public_password_reset_sends_to_verified_parent_phone(self, send_mock):
        response = self._post(
            StudentPasswordResetSendView,
            "/api/v1/students/password_reset_send/",
            {
                "target": "student",
                "student_name": self.student.name,
                "parent_phone": self.student.parent_phone,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(send_mock.call_args.kwargs["to"], self.student.parent_phone)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("oldpw123"))
        self.assertFalse(self.user.must_change_password)
        self.assertTrue(PendingPasswordReset.objects.filter(user=self.user).exists())

    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    def test_public_password_reset_unknown_account_is_generic_noop(self, send_mock):
        response = self._post(
            StudentPasswordResetSendView,
            "/api/v1/students/password_reset_send/",
            {
                "target": "student",
                "student_name": "없는학생",
                "student_phone": "01099998888",
            },
        )

        self.assertEqual(response.status_code, 200)
        send_mock.assert_not_called()
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("oldpw123"))
        self.assertFalse(self.user.must_change_password)
        self.assertFalse(PendingPasswordReset.objects.filter(user=self.user).exists())

    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=False)
    def test_existing_credentials_send_failure_keeps_current_password_state(self, _send):
        response = self._post(
            SendExistingCredentialsView,
            "/api/v1/students/send_existing_credentials/",
            {
                "name": self.student.name,
                "phone": self.student.phone,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("message", response.data)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("oldpw123"))
        self.assertFalse(self.user.must_change_password)
        self.assertEqual(self.user.token_version, 0)
        self.assertFalse(PendingPasswordReset.objects.filter(user=self.user).exists())

    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    def test_existing_credentials_sends_to_verified_parent_phone(self, send_mock):
        response = self._post(
            SendExistingCredentialsView,
            "/api/v1/students/send_existing_credentials/",
            {
                "name": self.student.name,
                "phone": self.student.parent_phone,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(send_mock.call_args.kwargs["to"], self.student.parent_phone)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("oldpw123"))
        self.assertTrue(PendingPasswordReset.objects.filter(user=self.user).exists())

    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    def test_legacy_password_find_request_is_gone_without_side_effects(self, send_mock):
        key = _pw_reset_cache_key(self.tenant.id, self.student.phone)
        cache.set(f"{key}:fail", 4, timeout=600)

        response = self._post(
            StudentPasswordFindRequestView,
            "/api/v1/students/password_find/request/",
            {
                "name": self.student.name,
                "phone": self.student.phone,
            },
        )

        self.assertEqual(response.status_code, 410)
        self.assertEqual(cache.get(f"{key}:fail"), 4)
        self.assertIsNone(cache.get(key))
        send_mock.assert_not_called()

    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=False)
    def test_legacy_password_find_verify_is_gone_without_side_effects(self, send_mock):
        key = _pw_reset_cache_key(self.tenant.id, self.student.phone)
        cache.set(key, {"user_id": self.user.id, "code": "123456"}, timeout=600)

        response = self._post(
            StudentPasswordFindVerifyView,
            "/api/v1/students/password_find/verify/",
            {
                "phone": self.student.phone,
                "code": "123456",
                "new_password": "7777",
            },
        )

        self.assertEqual(response.status_code, 410)
        self.assertIsNotNone(cache.get(key))
        self.assertIsNone(cache.get(f"{key}:fail"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("oldpw123"))
        send_mock.assert_not_called()

    @override_settings(
        ALLOWED_HOSTS=["api.hakwonplus.com", "testserver"],
        TENANT_HEADER_CODE_ALLOWED_HOSTS=("api.hakwonplus.com",),
    )
    def test_student_account_notification_log_endpoint_uses_account_metadata(self):
        from academy.adapters.db.django.repositories_messaging import create_notification_log

        create_notification_log(
            tenant_id=self.tenant.id,
            source_tenant_id=self.tenant.id,
            target_type="account",
            target_id=f"student:{self.student.id}",
            target_name=self.student.name,
            notification_type="password_reset_student",
            amount_deducted=Decimal("0"),
            message_mode="alimtalk",
            recipient_summary="홍길동 0101****",
            success=True,
        )
        create_notification_log(
            tenant_id=self.tenant.id,
            source_tenant_id=self.tenant.id,
            target_type="account",
            target_id="student:99999",
            target_name="다른학생",
            notification_type="password_reset_student",
            amount_deducted=Decimal("0"),
            message_mode="alimtalk",
            recipient_summary="다른학생 0109****",
            success=True,
        )

        response = APIClient().get(
            f"/api/v1/students/{self.student.id}/account-notifications/",
            **self._staff_auth_headers(role="teacher"),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["results"]), 1)
        item = response.data["results"][0]
        self.assertEqual(item["notification_type"], "password_reset_student")
        self.assertEqual(item["target_id"], f"student:{self.student.id}")

    def test_sensitive_account_notification_message_body_is_redacted(self):
        from django.apps import apps

        from academy.adapters.db.django.repositories_messaging import create_notification_log

        create_notification_log(
            tenant_id=self.tenant.id,
            source_tenant_id=self.tenant.id,
            target_type="account",
            target_id=f"student:{self.student.id}",
            target_name=self.student.name,
            notification_type="password_reset_student",
            amount_deducted=Decimal("0"),
            message_mode="alimtalk",
            recipient_summary="홍길동 0101****",
            message_body="아이디: S001\n임시비밀번호: 123456",
            success=True,
        )

        NotificationLog = apps.get_model("messaging", "NotificationLog")
        log = NotificationLog.objects.get(target_id=f"student:{self.student.id}")
        self.assertEqual(log.message_body, "[보안] 계정/인증 알림 본문은 저장하지 않습니다.")

    @override_settings(
        ALLOWED_HOSTS=["api.hakwonplus.com", "testserver"],
        TENANT_HEADER_CODE_ALLOWED_HOSTS=("api.hakwonplus.com",),
    )
    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    def test_staff_username_guidance_keeps_student_password_state(self, send_mock):
        response = APIClient().post(
            f"/api/v1/students/{self.student.id}/account-notifications/",
            {"target": "student"},
            format="json",
            **self._staff_auth_headers(role="teacher"),
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("비밀번호는 변경되지 않았습니다", response.data["message"])
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("oldpw123"))
        self.assertFalse(self.user.must_change_password)
        self.assertEqual(self.user.token_version, 0)
        self.assertFalse(PendingPasswordReset.objects.filter(user=self.user).exists())
        self.assertEqual(send_mock.call_args.kwargs["trigger"], "registration_approved_student")
        self.assertEqual(send_mock.call_args.kwargs["to"], self.student.phone)

    @override_settings(
        ALLOWED_HOSTS=["api.hakwonplus.com", "testserver"],
        TENANT_HEADER_CODE_ALLOWED_HOSTS=("api.hakwonplus.com",),
    )
    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    def test_staff_parent_username_guidance_uses_parent_account(self, send_mock):
        create_parent_account_fixture(
            tenant=self.tenant,
            parent_phone=self.student.parent_phone,
            student_name=self.student.name,
            initial_password="existing-parent-password",
        )
        response = APIClient().post(
            f"/api/v1/students/{self.student.id}/account-notifications/",
            {"target": "parent"},
            format="json",
            **self._staff_auth_headers(role="teacher"),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(send_mock.call_args.kwargs["trigger"], "registration_approved_parent")
        self.assertEqual(send_mock.call_args.kwargs["to"], self.student.parent_phone)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("oldpw123"))

    @override_settings(
        ALLOWED_HOSTS=["api.hakwonplus.com", "testserver"],
        TENANT_HEADER_CODE_ALLOWED_HOSTS=("api.hakwonplus.com",),
    )
    def test_staff_username_guidance_is_tenant_scoped(self):
        other_tenant = Tenant.objects.create(name="다른학원", code="pw-other")
        User = get_user_model()
        other_user = User.objects.create_user(
            username=user_internal_username(other_tenant, "S900"),
            password="otherpw123",
            tenant=other_tenant,
        )
        other_student = Student.objects.create(
            tenant=other_tenant,
            user=other_user,
            ps_number="S900",
            omr_code="99990000",
            name="타학원학생",
            phone="01099990000",
        )

        response = APIClient().post(
            f"/api/v1/students/{other_student.id}/account-notifications/",
            {"target": "student"},
            format="json",
            **self._staff_auth_headers(role="teacher"),
        )

        self.assertEqual(response.status_code, 404)

    @override_settings(
        ALLOWED_HOSTS=["api.hakwonplus.com", "testserver"],
        TENANT_HEADER_CODE_ALLOWED_HOSTS=("api.hakwonplus.com",),
    )
    def test_staff_username_guidance_rejects_invalid_payload(self):
        response = APIClient().post(
            f"/api/v1/students/{self.student.id}/account-notifications/",
            ["student"],
            format="json",
            **self._staff_auth_headers(role="teacher"),
        )

        self.assertEqual(response.status_code, 400)

    def test_auto_temp_password_is_six_digits(self):
        temp_password = generate_temp_password()

        self.assertRegex(temp_password, r"^\d{6}$")
