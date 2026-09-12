from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import OperationalError
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.core.models import Tenant, TenantMembership
from apps.domains.students.models import Student, StudentRegistrationRequest
from apps.domains.students.views.registration_views import RegistrationRequestViewSet


User = get_user_model()


class RegistrationSettingsTests(TestCase):
    def setUp(self):
        cache.clear()
        self.factory = APIRequestFactory()
        self.tenant = Tenant.objects.create(
            name="자동승인 검증학원", code="autoapprove-test", is_active=True,
        )
        self.admin = User.objects.create_user(
            username="autoapprove-admin", password="test1234", tenant=self.tenant,
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=self.admin, role="owner")

    def _settings(self, method="get", data=None, *, tenant=None, user=None):
        request = getattr(self.factory, method)(
            "/api/v1/students/registration-requests/settings/", data, format="json",
        )
        request.tenant = tenant or Tenant.objects.get(pk=self.tenant.pk)
        force_authenticate(request, user=user or self.admin)
        response = RegistrationRequestViewSet.as_view({
            "get": "registration_settings", "patch": "registration_settings",
        })(request)
        return response, request.tenant

    def _signup(self, suffix):
        request = self.factory.post(
            "/api/v1/students/registration-requests/",
            {
                "name": f"검증학생{suffix}", "username": f"AUTOAPPROVE{suffix}",
                "initial_password": "signup-test-password",
                "password_confirmation": "signup-test-password",
                "parent_phone": f"0105555666{suffix}", "phone": f"0107777888{suffix}",
                "school_type": "HIGH", "high_school": "테스트고",
                "origin_middle_school": "테스트중", "grade": 1,
                "gender": "M", "address": "서울",
            },
            format="json",
        )
        request.tenant = Tenant.objects.get(pk=self.tenant.pk)
        return RegistrationRequestViewSet.as_view({"post": "create"})(request)

    def test_failed_save_returns_safe_retryable_error_and_preserves_disabled_setting(self):
        with patch.object(Tenant, "save", side_effect=OperationalError("private database diagnostic")):
            response, tenant = self._settings("patch", {"auto_approve": True})

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["code"], "registration_settings_save_failed")
        self.assertIn("다시 시도", response.data["detail"])
        self.assertNotIn("private database diagnostic", str(response.data))
        self.assertNotIn("auto_approve", response.data)
        self.assertFalse(tenant.student_registration_auto_approve)
        self.assertEqual(self._settings()[0].data, {"auto_approve": False})

    def test_failed_disabling_preserves_enabled_setting(self):
        Tenant.objects.filter(pk=self.tenant.pk).update(student_registration_auto_approve=True)
        with patch.object(Tenant, "save", side_effect=OperationalError("save unavailable")):
            response, tenant = self._settings("patch", {"auto_approve": False})

        self.assertEqual(response.status_code, 503)
        self.assertTrue(tenant.student_registration_auto_approve)
        self.assertEqual(self._settings()[0].data, {"auto_approve": True})

    def test_failure_after_database_write_rolls_back_before_reply(self):
        original_save = Tenant.save

        def save_then_fail(instance, *args, **kwargs):
            original_save(instance, *args, **kwargs)
            raise OperationalError("post-save failure")

        with patch.object(Tenant, "save", autospec=True, side_effect=save_then_fail):
            response, tenant = self._settings("patch", {"auto_approve": True})

        self.assertEqual(self._settings()[0].data, {"auto_approve": False})
        self.assertEqual(response.status_code, 503)
        self.assertFalse(tenant.student_registration_auto_approve)

    def test_failed_save_logs_event_and_tenant_without_exposing_exception_in_response(self):
        with self.assertLogs(
            "apps.domains.students.views.registration_views", level="ERROR",
        ) as captured:
            with patch.object(Tenant, "save", side_effect=OperationalError("save unavailable")):
                response, _ = self._settings("patch", {"auto_approve": True})

        self.assertEqual(response.status_code, 503)
        self.assertTrue(any(
            "registration_settings_save_failed" in record.getMessage()
            and str(self.tenant.pk) in record.getMessage()
            and record.exc_info is not None
            for record in captured.records
        ))

    def test_retry_then_reload_controls_real_registration_approval(self):
        with patch.object(Tenant, "save", side_effect=OperationalError("temporary failure")):
            failed, _ = self._settings("patch", {"auto_approve": True})
        self.assertEqual(failed.status_code, 503)
        self.assertEqual(self._settings()[0].data, {"auto_approve": False})

        pending_response = self._signup(1)
        self.assertEqual(pending_response.status_code, 201, pending_response.data)
        pending = StudentRegistrationRequest.objects.get(tenant=self.tenant, username="AUTOAPPROVE1")
        self.assertEqual(pending.status, StudentRegistrationRequest.PENDING)
        self.assertIsNone(pending.student_id)
        self.assertFalse(Student.objects.filter(tenant=self.tenant).exists())

        retry, _ = self._settings("patch", {"auto_approve": True})
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.data, {"auto_approve": True})
        self.assertEqual(self._settings()[0].data, retry.data)

        approved_response = self._signup(2)
        self.assertEqual(approved_response.status_code, 200, approved_response.data)
        approved = StudentRegistrationRequest.objects.get(tenant=self.tenant, username="AUTOAPPROVE2")
        self.assertEqual(approved.status, StudentRegistrationRequest.APPROVED)
        self.assertEqual(approved_response.data["id"], approved.student_id)
        self.assertEqual(approved.student.tenant_id, self.tenant.pk)
        self.assertTrue(approved.student.user.check_password("signup-test-password"))
        pending.refresh_from_db()
        self.assertEqual(pending.status, StudentRegistrationRequest.PENDING)

        disabled, _ = self._settings("patch", {"auto_approve": False})
        self.assertEqual(disabled.data, {"auto_approve": False})
        self.assertEqual(self._settings()[0].data, disabled.data)
        self.assertEqual(self._signup(3).status_code, 201)
        self.assertEqual(Student.objects.filter(tenant=self.tenant).count(), 1)

    def test_normal_boolean_and_string_values_persist(self):
        for value, expected in ((True, True), (False, False), ("true", True), ("false", False), (1, True), ("0", False)):
            with self.subTest(value=value):
                response, _ = self._settings("patch", {"auto_approve": value})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data, {"auto_approve": expected})
                self.assertEqual(self._settings()[0].data, response.data)

    def test_null_and_omitted_values_remain_noops(self):
        Tenant.objects.filter(pk=self.tenant.pk).update(student_registration_auto_approve=True)
        for data in ({}, {"auto_approve": None}):
            with self.subTest(data=data), patch.object(Tenant, "save") as save:
                response, _ = self._settings("patch", data)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data, {"auto_approve": True})
                save.assert_not_called()
        self.assertEqual(self._settings()[0].data, {"auto_approve": True})

    def test_invalid_values_remain_validation_errors_without_saving(self):
        for value in ("invalid", 2, [], {}):
            with self.subTest(value=value), patch.object(Tenant, "save") as save:
                response, _ = self._settings("patch", {"auto_approve": value})
                self.assertEqual(response.status_code, 400)
                self.assertIn("auto_approve", response.data)
                save.assert_not_called()
        self.assertEqual(self._settings()[0].data, {"auto_approve": False})

    def test_staff_membership_cannot_read_or_write_another_tenant_setting(self):
        other = Tenant.objects.create(name="다른 검증학원", code="autoapprove-other", is_active=True)
        for method in ("get", "patch"):
            with self.subTest(method=method), patch.object(Tenant, "save") as save:
                response, _ = self._settings(method, {"auto_approve": True}, tenant=other)
                self.assertEqual(response.status_code, 403)
                save.assert_not_called()
        other.refresh_from_db()
        self.assertFalse(other.student_registration_auto_approve)
        self.assertEqual(self._settings()[0].data, {"auto_approve": False})

    def test_student_and_parent_memberships_cannot_read_or_write_settings(self):
        for role in ("student", "parent"):
            user = User.objects.create_user(
                username=f"autoapprove-{role}", password="test1234", tenant=self.tenant,
            )
            TenantMembership.ensure_active(tenant=self.tenant, user=user, role=role)
            for method in ("get", "patch"):
                with self.subTest(role=role, method=method), patch.object(Tenant, "save") as save:
                    response, _ = self._settings(method, {"auto_approve": True}, user=user)
                    self.assertEqual(response.status_code, 403)
                    save.assert_not_called()
        self.assertEqual(self._settings()[0].data, {"auto_approve": False})
