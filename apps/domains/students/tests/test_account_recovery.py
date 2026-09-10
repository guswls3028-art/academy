from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient, APIRequestFactory

from apps.api.common.auth_jwt import TenantAwareTokenObtainPairView
from apps.core.models import PendingPasswordReset, Tenant, TenantMembership
from apps.core.models.user import user_internal_username
from apps.core.views.account_recovery import AccountRecoveryDispatchView
from apps.domains.parents.models import Parent
from apps.domains.parents.services import ensure_parent_account_for_student
from apps.domains.students.models import Student


class AccountRecoveryDispatchTests(TestCase):
    def setUp(self):
        cache.clear()
        self.factory = APIRequestFactory()
        self.tenant = Tenant.objects.create(name="복구테스트학원", code="recover")
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
        TenantMembership.objects.create(
            tenant=self.tenant,
            user=self.user,
            role="student",
            is_active=True,
        )

    def _post(self, data: dict):
        request = self.factory.post(
            "/api/v1/auth/account-recovery/dispatch/",
            data,
            format="json",
        )
        request.tenant = self.tenant
        return AccountRecoveryDispatchView.as_view()(request)

    @override_settings(
        ALLOWED_HOSTS=["api.hakwonplus.com", "testserver"],
        TENANT_HEADER_CODE_ALLOWED_HOSTS=("api.hakwonplus.com",),
    )
    def _token_post(self, username: str, password: str):
        request = self.factory.post(
            "/api/v1/token/",
            {"username": username, "password": password, "tenant_code": self.tenant.code},
            format="json",
            HTTP_HOST="api.hakwonplus.com",
            HTTP_X_TENANT_CODE=self.tenant.code,
        )
        return TenantAwareTokenObtainPairView.as_view()(request)

    @override_settings(
        ALLOWED_HOSTS=["api.hakwonplus.com", "testserver"],
        TENANT_HEADER_CODE_ALLOWED_HOSTS=("api.hakwonplus.com",),
    )
    def _api_post(self, data: dict, *, tenant_code: str | None = None, host: str = "api.hakwonplus.com"):
        headers = {"HTTP_HOST": host}
        if tenant_code:
            headers["HTTP_X_TENANT_CODE"] = tenant_code
        return APIClient().post(
            "/api/v1/auth/account-recovery/dispatch/",
            data,
            format="json",
            **headers,
        )

    @override_settings(
        ALLOWED_HOSTS=["api.hakwonplus.com", "testserver"],
        TENANT_HEADER_CODE_ALLOWED_HOSTS=("api.hakwonplus.com",),
    )
    def _api_token_post(self, username: str, password: str):
        return APIClient().post(
            "/api/v1/token/",
            {"username": username, "password": password, "tenant_code": self.tenant.code},
            format="json",
            HTTP_HOST="api.hakwonplus.com",
            HTTP_X_TENANT_CODE=self.tenant.code,
        )

    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    def test_invalid_payload_returns_400_without_side_effects(self, send_mock):
        invalid_payloads = [
            {
                "mode": "legacy",
                "target": "student",
                "student_name": self.student.name,
                "phone": self.student.phone,
            },
            {
                "mode": "password",
                "target": "teacher",
                "student_name": self.student.name,
                "phone": self.student.phone,
            },
            {
                "mode": "password",
                "target": "student",
                "student_name": "",
                "phone": self.student.phone,
            },
            {
                "mode": "password",
                "target": "student",
                "student_name": self.student.name,
                "phone": "0101234",
            },
        ]

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                response = self._post(payload)
                self.assertEqual(response.status_code, 400)
                self.assertFalse(PendingPasswordReset.objects.filter(user=self.user).exists())
                self.user.refresh_from_db()
                self.assertTrue(self.user.check_password("oldpw123"))
                self.assertFalse(self.user.must_change_password)

        send_mock.assert_not_called()

    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    def test_username_recovery_sends_id_without_resetting_password(self, send_mock):
        response = self._post(
            {
                "mode": "username",
                "target": "student",
                "student_name": self.student.name,
                "phone": self.student.phone,
            }
        )

        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("oldpw123"))
        self.assertFalse(self.user.must_change_password)
        self.assertEqual(send_mock.call_args.kwargs["trigger"], "registration_approved_student")
        self.assertEqual(send_mock.call_args.kwargs["to"], self.student.phone)
        self.assertEqual(send_mock.call_args.kwargs["replacements"]["학생아이디"], "S001")

    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    def test_password_recovery_creates_pending_password_and_sends_to_verified_phone(self, send_mock):
        response = self._post(
            {
                "mode": "password",
                "target": "student",
                "student_name": self.student.name,
                "phone": self.student.parent_phone,
            }
        )

        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("oldpw123"))
        self.assertFalse(self.user.must_change_password)
        self.assertTrue(PendingPasswordReset.objects.filter(user=self.user).exists())
        self.assertEqual(send_mock.call_args.kwargs["trigger"], "password_reset_student")
        self.assertEqual(send_mock.call_args.kwargs["to"], self.student.parent_phone)
        self.assertRegex(
            send_mock.call_args.kwargs["replacements"]["임시비밀번호"],
            r"^\d{6}$",
        )

    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    def test_api_dispatch_requires_resolved_tenant(self, send_mock):
        response = self._api_post(
            {
                "mode": "username",
                "target": "student",
                "student_name": self.student.name,
                "phone": self.student.phone,
            }
        )

        self.assertEqual(response.status_code, 403)
        send_mock.assert_not_called()

    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    def test_api_dispatch_uses_tenant_header_and_response_header(self, send_mock):
        response = self._api_post(
            {
                "mode": "username",
                "target": "student",
                "student_name": self.student.name,
                "phone": self.student.phone,
            },
            tenant_code=self.tenant.code,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["X-Tenant-Code"], self.tenant.code)
        self.assertEqual(send_mock.call_args.kwargs["replacements"]["학생아이디"], "S001")

    @patch("apps.domains.students.services.account_recovery.generate_temp_password", return_value="11112222")
    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    def test_api_dispatch_pending_password_lifecycle_uses_real_urls(self, send_mock, _generate):
        response = self._api_post(
            {
                "mode": "password",
                "target": "student",
                "student_name": self.student.name,
                "phone": self.student.phone,
            },
            tenant_code=self.tenant.code,
        )

        self.assertEqual(response.status_code, 200)
        send_mock.assert_called_once()
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("oldpw123"))
        self.assertEqual(self._api_token_post("S001", "oldpw123").status_code, 200)
        self.assertTrue(PendingPasswordReset.objects.filter(user=self.user).exists())

        token_response = self._api_token_post("S001", "11112222")

        self.assertEqual(token_response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("11112222"))
        self.assertTrue(self.user.must_change_password)
        self.assertFalse(PendingPasswordReset.objects.filter(user=self.user).exists())
        self.assertEqual(self._api_token_post("S001", "oldpw123").status_code, 400)

    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    def test_api_dispatch_isolates_same_name_phone_by_tenant_header(self, send_mock):
        User = get_user_model()
        other_tenant = Tenant.objects.create(name="다른복구학원", code="recover2")
        other_user = User.objects.create_user(
            username=user_internal_username(other_tenant, "S999"),
            password="oldpw123",
            tenant=other_tenant,
        )
        Student.objects.create(
            tenant=other_tenant,
            user=other_user,
            ps_number="S999",
            omr_code="33334444",
            name=self.student.name,
            phone=self.student.phone,
            parent_phone=self.student.parent_phone,
        )
        payload = {
            "mode": "username",
            "target": "student",
            "student_name": self.student.name,
            "phone": self.student.phone,
        }

        first_response = self._api_post(payload, tenant_code=self.tenant.code)
        second_response = self._api_post(payload, tenant_code=other_tenant.code)

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        first_call, second_call = send_mock.call_args_list
        self.assertEqual(first_call.kwargs["replacements"]["학생아이디"], "S001")
        self.assertEqual(second_call.kwargs["replacements"]["학생아이디"], "S999")

    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    def test_pending_temp_password_login_activates_reset_and_forces_change(self, send_mock):
        response = self._post(
            {
                "mode": "password",
                "target": "student",
                "student_name": self.student.name,
                "phone": self.student.phone,
            }
        )
        temp_password = send_mock.call_args.kwargs["replacements"]["임시비밀번호"]

        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.user.check_password("oldpw123"))
        old_password_response = self._token_post("S001", "oldpw123")
        self.assertEqual(old_password_response.status_code, 200)
        self.assertTrue(PendingPasswordReset.objects.filter(user=self.user).exists())

        token_response = self._token_post("S001", temp_password)

        self.assertEqual(token_response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(temp_password))
        self.assertFalse(self.user.check_password("oldpw123"))
        self.assertTrue(self.user.must_change_password)
        self.assertFalse(PendingPasswordReset.objects.filter(user=self.user).exists())
        self.assertEqual(self._token_post("S001", "oldpw123").status_code, 400)

    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    def test_parent_pending_temp_password_login_activates_parent_reset(self, send_mock):
        ensure_parent_account_for_student(
            tenant=self.tenant,
            parent_phone=self.student.parent_phone,
            student_name=self.student.name,
            initial_password="existing-parent-password",
        )
        response = self._post(
            {
                "mode": "password",
                "target": "parent",
                "student_name": self.student.name,
                "phone": self.student.parent_phone,
            }
        )
        temp_password = send_mock.call_args.kwargs["replacements"]["임시비밀번호"]
        parent = Parent.objects.get(tenant=self.tenant, phone=self.student.parent_phone)
        self.assertTrue(PendingPasswordReset.objects.filter(user=parent.user).exists())

        token_response = self._token_post(self.student.parent_phone, temp_password)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(token_response.status_code, 200)
        parent.user.refresh_from_db()
        self.assertTrue(parent.user.check_password(temp_password))
        self.assertTrue(parent.user.must_change_password)
        self.assertFalse(PendingPasswordReset.objects.filter(user=parent.user).exists())

    @patch("apps.domains.students.services.account_recovery.generate_temp_password", side_effect=["11112222", "33334444"])
    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    def test_repeated_password_recovery_replaces_previous_pending_password(self, send_mock, _generate):
        payload = {
            "mode": "password",
            "target": "student",
            "student_name": self.student.name,
            "phone": self.student.phone,
        }

        first_response = self._post(payload)
        first_temp = send_mock.call_args.kwargs["replacements"]["임시비밀번호"]
        second_response = self._post(payload)
        second_temp = send_mock.call_args.kwargs["replacements"]["임시비밀번호"]

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        self.assertNotEqual(first_temp, second_temp)
        self.assertEqual(PendingPasswordReset.objects.filter(user=self.user).count(), 1)
        self.assertEqual(self._token_post("S001", first_temp).status_code, 400)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("oldpw123"))

        token_response = self._token_post("S001", second_temp)

        self.assertEqual(token_response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(second_temp))
        self.assertTrue(self.user.must_change_password)

    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=False)
    def test_password_recovery_delivery_failure_clears_pending_without_lockout(self, send_mock):
        response = self._post(
            {
                "mode": "password",
                "target": "student",
                "student_name": self.student.name,
                "phone": self.student.phone,
            }
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data["message"],
            "입력한 정보가 등록되어 있다면 해당 번호로 임시 비밀번호 알림톡이 발송됩니다.",
        )
        send_mock.assert_called_once()
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("oldpw123"))
        self.assertFalse(self.user.must_change_password)
        self.assertFalse(PendingPasswordReset.objects.filter(user=self.user).exists())

    @patch("apps.domains.students.services.account_recovery.generate_temp_password", side_effect=["11112222", "33334444"])
    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", side_effect=[True, False])
    def test_delivery_failure_restores_previous_pending_temp_password(self, send_mock, _generate):
        payload = {
            "mode": "password",
            "target": "student",
            "student_name": self.student.name,
            "phone": self.student.phone,
        }

        first_response = self._post(payload)
        second_response = self._post(payload)

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(send_mock.call_count, 2)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("oldpw123"))
        self.assertFalse(self.user.must_change_password)
        self.assertEqual(PendingPasswordReset.objects.filter(user=self.user).count(), 1)
        self.assertEqual(self._token_post("S001", "33334444").status_code, 400)
        self.assertEqual(self._token_post("S001", "11112222").status_code, 200)

    @patch("apps.domains.students.services.account_recovery.generate_temp_password", side_effect=["11112222", "33334444"])
    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", side_effect=[True, False])
    def test_api_dispatch_delivery_failure_restores_previous_pending_temp_password(self, send_mock, _generate):
        payload = {
            "mode": "password",
            "target": "student",
            "student_name": self.student.name,
            "phone": self.student.phone,
        }

        first_response = self._api_post(payload, tenant_code=self.tenant.code)
        second_response = self._api_post(payload, tenant_code=self.tenant.code)

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(send_mock.call_count, 2)
        self.assertEqual(PendingPasswordReset.objects.filter(user=self.user).count(), 1)
        self.assertEqual(self._api_token_post("S001", "33334444").status_code, 400)
        self.assertEqual(self._api_token_post("S001", "11112222").status_code, 200)

    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    def test_disabled_tenant_skips_recovery_delivery_without_pending_secret(self, send_mock):
        with override_settings(TEST_TENANT_ID=self.tenant.id):
            response = self._post(
                {
                    "mode": "password",
                    "target": "student",
                    "student_name": self.student.name,
                    "phone": self.student.phone,
                }
            )

        self.assertEqual(response.status_code, 200)
        send_mock.assert_not_called()
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("oldpw123"))
        self.assertFalse(PendingPasswordReset.objects.filter(user=self.user).exists())

    @patch("apps.domains.students.services.account_recovery.generate_temp_password", return_value="11112222")
    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    def test_inactive_user_login_does_not_consume_pending_password(self, _send, _generate):
        response = self._post(
            {
                "mode": "password",
                "target": "student",
                "student_name": self.student.name,
                "phone": self.student.phone,
            }
        )
        self.assertEqual(response.status_code, 200)

        self.user.is_active = False
        self.user.save(update_fields=["is_active"])

        token_response = self._token_post("S001", "11112222")

        self.assertEqual(token_response.status_code, 400)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("oldpw123"))
        self.assertFalse(self.user.must_change_password)
        self.assertTrue(PendingPasswordReset.objects.filter(user=self.user).exists())

    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    def test_unknown_account_returns_generic_success_without_sending(self, send_mock):
        response = self._post(
            {
                "mode": "username",
                "target": "student",
                "student_name": "없는학생",
                "phone": "01099998888",
            }
        )

        self.assertEqual(response.status_code, 200)
        send_mock.assert_not_called()
        self.assertFalse(PendingPasswordReset.objects.filter(user=self.user).exists())

    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    def test_ambiguous_student_recovery_returns_generic_without_pending_side_effect(self, send_mock):
        User = get_user_model()
        other_user = User.objects.create_user(
            username=user_internal_username(self.tenant, "S002"),
            password="oldpw123",
            tenant=self.tenant,
        )
        Student.objects.create(
            tenant=self.tenant,
            user=other_user,
            ps_number="S002",
            omr_code="22223333",
            name=self.student.name,
            phone="01055556666",
            parent_phone=self.student.parent_phone,
        )

        response = self._post(
            {
                "mode": "password",
                "target": "student",
                "student_name": self.student.name,
                "phone": self.student.parent_phone,
            }
        )

        self.assertEqual(response.status_code, 200)
        send_mock.assert_not_called()
        self.assertFalse(PendingPasswordReset.objects.filter(user=self.user).exists())

    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    def test_parent_username_recovery_uses_parent_account(self, send_mock):
        ensure_parent_account_for_student(
            tenant=self.tenant,
            parent_phone=self.student.parent_phone,
            student_name=self.student.name,
            initial_password="parent-chosen-password",
        )

        response = self._post(
            {
                "mode": "username",
                "target": "parent",
                "student_name": self.student.name,
                "phone": self.student.parent_phone,
            }
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(send_mock.call_args.kwargs["trigger"], "registration_approved_parent")
        self.assertEqual(send_mock.call_args.kwargs["to"], self.student.parent_phone)
        self.assertEqual(send_mock.call_args.kwargs["replacements"]["학부모아이디"], self.student.parent_phone)

    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    def test_ambiguous_parent_recovery_returns_generic_without_parent_side_effect(self, send_mock):
        User = get_user_model()
        other_user = User.objects.create_user(
            username=user_internal_username(self.tenant, "S002"),
            password="oldpw123",
            tenant=self.tenant,
        )
        Student.objects.create(
            tenant=self.tenant,
            user=other_user,
            ps_number="S002",
            omr_code="22223333",
            name=self.student.name,
            phone="01055556666",
            parent_phone=self.student.parent_phone,
        )

        response = self._post(
            {
                "mode": "username",
                "target": "parent",
                "student_name": self.student.name,
                "phone": self.student.parent_phone,
            }
        )

        self.assertEqual(response.status_code, 200)
        send_mock.assert_not_called()
        self.assertFalse(
            Parent.objects.filter(tenant=self.tenant, phone=self.student.parent_phone).exists()
        )
