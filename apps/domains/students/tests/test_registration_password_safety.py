from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth.hashers import check_password, identify_hasher, make_password
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.core.models import Tenant, TenantMembership
from apps.domains.students.models import Student, StudentRegistrationRequest
from apps.domains.students.serializers import StudentCreateSerializer
from apps.domains.students.services.account_notice import _decrypt
from apps.domains.students.views.registration_views import (
    RegistrationRequestViewSet,
    _approve_registration_request,
)
from apps.domains.students.views import StudentViewSet
from apps.support.students.lifecycle_dependencies import ensure_parent_account_for_student

User = get_user_model()


class RegistrationPasswordSafetyTests(TestCase):
    def setUp(self):
        cache.clear()
        self.factory = APIRequestFactory()
        self.tenant = Tenant.objects.create(name="가입보안학원", code="regsafe", is_active=True)
        self.admin = User.objects.create_user(
            username="regsafe-admin",
            password="test1234",
            tenant=self.tenant,
            is_staff=True,
            name="가입 관리자",
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=self.admin, role="owner")

    def _registration_payload(self) -> dict:
        return {
            "name": "가입학생",
            "username": "REGSAFE01",
            "initial_password": "rawpw1234",
            "password_confirmation": "rawpw1234",
            "parent_phone": "01055556666",
            "phone": "01077778888",
            "school_type": "HIGH",
            "high_school": "테스트고",
            "origin_middle_school": "테스트중",
            "grade": 1,
            "gender": "M",
            "address": "서울",
        }

    def test_student_create_serializer_hides_pending_notice_secrets(self):
        fields = StudentCreateSerializer().fields

        self.assertNotIn(
            "pending_account_notice_student_password_ciphertext",
            fields,
        )
        self.assertNotIn(
            "pending_account_notice_parent_password_ciphertext",
            fields,
        )
        self.assertNotIn("pending_account_notice_since", fields)

    def test_registration_create_does_not_persist_plain_password(self):
        request = self.factory.post(
            "/api/v1/students/registration-requests/",
            self._registration_payload(),
            format="json",
        )
        request.tenant = self.tenant

        response = RegistrationRequestViewSet.as_view({"post": "create"})(request)

        self.assertEqual(response.status_code, 201)
        reg = StudentRegistrationRequest.objects.get()
        self.assertEqual(reg.initial_password_plain, "")
        self.assertNotEqual(reg.initial_password, "rawpw1234")
        identify_hasher(reg.initial_password)
        self.assertTrue(check_password("rawpw1234", reg.initial_password))
        self.assertNotIn("password_confirmation", {field.name for field in reg._meta.fields})

    def test_registration_password_confirmation_mismatch_creates_no_request(self):
        original_user_count = User.objects.filter(tenant=self.tenant).count()
        original_student_count = Student.objects.filter(tenant=self.tenant).count()
        request = self.factory.post(
            "/api/v1/students/registration-requests/",
            {
                **self._registration_payload(),
                "password_confirmation": "typo-password",
            },
            format="json",
        )
        request.tenant = self.tenant

        response = RegistrationRequestViewSet.as_view({"post": "create"})(request)

        self.assertEqual(response.status_code, 400)
        self.assertIn("password_confirmation", response.data)
        self.assertFalse(StudentRegistrationRequest.objects.filter(tenant=self.tenant).exists())
        self.assertEqual(User.objects.filter(tenant=self.tenant).count(), original_user_count)
        self.assertEqual(
            Student.objects.filter(tenant=self.tenant).count(),
            original_student_count,
        )

    def test_approval_stages_non_secret_password_phrase_until_enrollment(self):
        reg = StudentRegistrationRequest.objects.create(
            tenant=self.tenant,
            status=StudentRegistrationRequest.PENDING,
            initial_password=make_password("rawpw1234"),
            initial_password_plain="legacy-rawpw",
            name="가입학생",
            username="REGSAFE02",
            parent_phone="01055556666",
            phone="01077778888",
            school_type="HIGH",
            high_school="테스트고",
            origin_middle_school="테스트중",
            grade=1,
            gender="M",
            address="서울",
        )
        request = self.factory.post("/api/v1/students/registration-requests/approve/")
        request.tenant = self.tenant

        error = _approve_registration_request(request, reg)

        self.assertIsNone(error)
        self.assertEqual(
            _decrypt(
                reg.student.pending_account_notice_student_password_ciphertext
            ),
            "가입 신청 시 입력한 비밀번호",
        )
        self.assertEqual(
            _decrypt(
                reg.student.pending_account_notice_parent_password_ciphertext
            ),
            "가입 신청 시 입력한 비밀번호",
        )
        reg.refresh_from_db()
        self.assertEqual(reg.initial_password_plain, "")

    def test_approval_stages_parent_password_unchanged_for_existing_parent(self):
        ensure_parent_account_for_student(
            tenant=self.tenant,
            parent_phone="01055556666",
            student_name="기존학생",
            initial_password="existing-parent-password",
        )
        reg = StudentRegistrationRequest.objects.create(
            tenant=self.tenant,
            status=StudentRegistrationRequest.PENDING,
            initial_password=make_password("rawpw1234"),
            initial_password_plain="",
            name="가입학생",
            username="REGSAFE03",
            parent_phone="01055556666",
            phone="01077778889",
            school_type="HIGH",
            high_school="테스트고",
            origin_middle_school="테스트중",
            grade=1,
            gender="M",
            address="서울",
        )
        request = self.factory.post("/api/v1/students/registration-requests/approve/")
        request.tenant = self.tenant

        error = _approve_registration_request(request, reg)

        self.assertIsNone(error)
        self.assertEqual(
            _decrypt(
                reg.student.pending_account_notice_parent_password_ciphertext
            ),
            "변경되지 않음",
        )

    def test_approve_action_returns_created_student_without_refreshing_input_instance(self):
        reg = StudentRegistrationRequest.objects.create(
            tenant=self.tenant,
            status=StudentRegistrationRequest.PENDING,
            initial_password=make_password("rawpw1234"),
            initial_password_plain="legacy-rawpw",
            name="액션승인학생",
            username="REGSAFE04",
            parent_phone="01055556666",
            phone="01077778890",
            school_type="HIGH",
            high_school="테스트고",
            origin_middle_school="테스트중",
            grade=1,
            gender="M",
            address="서울",
        )
        request = self.factory.post(f"/api/v1/students/registration-requests/{reg.id}/approve/")
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant

        response = RegistrationRequestViewSet.as_view({"post": "approve"})(request, pk=reg.id)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["name"], "액션승인학생")
        reg.refresh_from_db()
        self.assertEqual(reg.status, StudentRegistrationRequest.APPROVED)
        self.assertIsNotNone(reg.student_id)
        self.assertIsNotNone(reg.student.pending_account_notice_since)

    def test_auto_approve_create_returns_student_and_approves_request(self):
        self.tenant.student_registration_auto_approve = True
        self.tenant.save(update_fields=["student_registration_auto_approve"])
        request = self.factory.post(
            "/api/v1/students/registration-requests/",
            {
                **self._registration_payload(),
                "username": "REGSAFE05",
                "phone": "01077778895",
            },
            format="json",
        )
        request.tenant = self.tenant

        response = RegistrationRequestViewSet.as_view({"post": "create"})(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["name"], "가입학생")
        reg = StudentRegistrationRequest.objects.get(username="REGSAFE05")
        self.assertEqual(reg.status, StudentRegistrationRequest.APPROVED)
        self.assertIsNotNone(reg.student_id)
        self.assertIsNotNone(reg.student.pending_account_notice_since)

    @patch(
        "apps.domains.messaging.services.send_registration_approved_messages",
    )
    def test_approval_does_not_dispatch_before_enrollment(self, send_mock):
        reg = StudentRegistrationRequest.objects.create(
            tenant=self.tenant,
            status=StudentRegistrationRequest.PENDING,
            initial_password=make_password("rawpw1234"),
            initial_password_plain="legacy-rawpw",
            name="알림실패승인학생",
            username="REGSAFE06",
            parent_phone="01055556666",
            phone="01077778896",
            school_type="HIGH",
            high_school="테스트고",
            origin_middle_school="테스트중",
            grade=1,
            gender="M",
            address="서울",
        )
        request = self.factory.post(f"/api/v1/students/registration-requests/{reg.id}/approve/")
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant

        response = RegistrationRequestViewSet.as_view({"post": "approve"})(request, pk=reg.id)

        self.assertEqual(response.status_code, 200)
        reg.refresh_from_db()
        self.assertEqual(reg.status, StudentRegistrationRequest.APPROVED)
        self.assertIsNotNone(reg.student_id)
        send_mock.assert_not_called()

    @patch("apps.domains.messaging.services.send_welcome_messages")
    def test_student_create_stages_parent_initial_password_until_enrollment(self, send_mock):
        request = self.factory.post(
            "/api/v1/students/",
            {
                "name": "직접등록학생",
                "ps_number": "REGSAFE-CREATE-1",
                "initial_password": "stud1234",
                "parent_phone": "01055556666",
                "phone": "01077778891",
                "school_type": "HIGH",
                "grade": 1,
            },
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant

        response = StudentViewSet.as_view({"post": "create"})(request)

        self.assertEqual(response.status_code, 201)
        send_mock.assert_not_called()
        student = Student.objects.get(pk=response.data["id"])
        self.assertEqual(
            _decrypt(student.pending_account_notice_parent_password_ciphertext),
            "stud1234",
        )
        self.assertTrue(student.user.must_change_password)
        self.assertTrue(student.parent.user.check_password("stud1234"))
        self.assertTrue(student.parent.user.must_change_password)

    @patch("apps.domains.messaging.services.send_welcome_messages")
    def test_student_create_without_student_phone_creates_separate_student_and_parent_accounts(self, send_mock):
        request = self.factory.post(
            "/api/v1/students/",
            {
                "name": "번호없는학생",
                "initial_password": "stud1234",
                "parent_phone": "01055556669",
                "phone": "",
                "school_type": "HIGH",
                "grade": 1,
            },
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant

        response = StudentViewSet.as_view({"post": "create"})(request)

        self.assertEqual(response.status_code, 201)
        student = Student.objects.get(tenant=self.tenant, name="번호없는학생")
        self.assertIsNotNone(student.user_id)
        self.assertEqual(student.user.phone, "")
        self.assertTrue(student.uses_identifier)
        self.assertIsNotNone(student.parent_id)
        self.assertIsNotNone(student.parent.user_id)
        send_mock.assert_not_called()
        self.assertEqual(
            _decrypt(student.pending_account_notice_student_password_ciphertext),
            "stud1234",
        )
        self.assertEqual(
            _decrypt(student.pending_account_notice_parent_password_ciphertext),
            "stud1234",
        )

    @patch("apps.domains.messaging.services.send_welcome_messages")
    def test_student_create_stages_parent_password_unchanged_when_account_exists(self, send_mock):
        ensure_parent_account_for_student(
            tenant=self.tenant,
            parent_phone="01055556666",
            student_name="기존학생",
            initial_password="existing-parent-password",
        )
        request = self.factory.post(
            "/api/v1/students/",
            {
                "name": "추가자녀",
                "ps_number": "REGSAFE-CREATE-2",
                "initial_password": "stud1234",
                "parent_phone": "01055556666",
                "phone": "01077778892",
                "school_type": "HIGH",
                "grade": 1,
            },
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant

        response = StudentViewSet.as_view({"post": "create"})(request)

        self.assertEqual(response.status_code, 201)
        send_mock.assert_not_called()
        student = Student.objects.get(pk=response.data["id"])
        self.assertEqual(
            _decrypt(student.pending_account_notice_parent_password_ciphertext),
            "변경되지 않음",
        )

    @patch("apps.domains.messaging.services.send_welcome_messages")
    def test_student_excel_stages_parent_initial_password_until_enrollment(self, send_mock):
        from apps.domains.students.services.bulk_from_excel import (
            bulk_create_students_from_excel_rows,
        )

        result = bulk_create_students_from_excel_rows(
            tenant_id=self.tenant.id,
            students_data=[
                {
                    "name": "엑셀등록학생",
                    "parent_phone": "01055556666",
                    "phone": "01077778893",
                    "school_type": "HIGH",
                    "grade": 1,
                }
            ],
            initial_password="stud1234",
        )

        self.assertEqual(result["created"], 1)
        send_mock.assert_not_called()
        student = Student.objects.get(tenant=self.tenant, name="엑셀등록학생")
        self.assertEqual(
            _decrypt(student.pending_account_notice_parent_password_ciphertext),
            "stud1234",
        )

    @patch("apps.domains.messaging.services.send_welcome_messages")
    def test_student_excel_stages_notice_until_enrollment(self, send_mock):
        from apps.domains.students.services.bulk_from_excel import (
            bulk_create_students_from_excel_rows,
        )

        result = bulk_create_students_from_excel_rows(
            tenant_id=self.tenant.id,
            students_data=[
                {
                    "name": "엑셀무알림학생",
                    "parent_phone": "01055556666",
                    "phone": "01077778894",
                    "school_type": "HIGH",
                    "grade": 1,
                }
            ],
            initial_password="stud1234",
        )

        self.assertEqual(result["created"], 1)
        send_mock.assert_not_called()
        student = Student.objects.get(tenant=self.tenant, name="엑셀무알림학생")
        self.assertIsNotNone(student.pending_account_notice_since)

    @patch("apps.domains.messaging.services.send_welcome_messages")
    def test_student_import_reports_active_duplicate_without_new_account(self, send_mock):
        from apps.domains.students.models import Student
        from apps.domains.students.services import create_student_account, import_students_from_rows

        existing = create_student_account(
            tenant=self.tenant,
            password="stud1234",
            student_data={
                "name": "엑셀중복학생",
                "ps_number": "REGSAFE-IMP-1",
                "phone": "01077778897",
                "parent_phone": "01055556667",
                "omr_code": "77778897",
                "uses_identifier": False,
                "school_type": "HIGH",
                "grade": 1,
            },
        ).student

        result = import_students_from_rows(
            tenant_id=self.tenant.id,
            students_data=[
                {
                    "name": "엑셀중복학생",
                    "parent_phone": "01055556667",
                    "phone": "01077778898",
                    "school_type": "HIGH",
                    "grade": 1,
                }
            ],
            initial_password="stud1234",
        )

        self.assertEqual(result["created"], 0)
        self.assertEqual(result["duplicates"][0]["student_id"], existing.id)
        self.assertEqual(
            Student.objects.filter(
                tenant=self.tenant,
                name="엑셀중복학생",
                parent_phone="01055556667",
                deleted_at__isnull=True,
            ).count(),
            1,
        )
        send_mock.assert_not_called()

    @patch("apps.domains.messaging.services.send_welcome_messages")
    def test_student_import_restores_deleted_student_without_welcome(self, send_mock):
        from apps.domains.students.services import (
            create_student_account,
            import_students_from_rows,
            soft_delete_student,
        )

        student = create_student_account(
            tenant=self.tenant,
            password="stud1234",
            student_data={
                "name": "엑셀복원학생",
                "ps_number": "REGSAFE-IMP-2",
                "phone": "01077778899",
                "parent_phone": "01055556668",
                "omr_code": "77778899",
                "uses_identifier": False,
                "school_type": "HIGH",
                "grade": 1,
            },
        ).student
        soft_delete_student(student, tenant=self.tenant)

        result = import_students_from_rows(
            tenant_id=self.tenant.id,
            students_data=[
                {
                    "name": "엑셀복원학생",
                    "parent_phone": "01055556668",
                    "phone": "01077778899",
                    "school_type": "HIGH",
                    "grade": 2,
                    "memo": "복원 메모",
                }
            ],
            initial_password="stud1234",
        )

        self.assertEqual(result["created"], 0)
        self.assertEqual(result["restored"][0]["student_id"], student.id)
        student.refresh_from_db()
        self.assertIsNone(student.deleted_at)
        self.assertEqual(student.grade, 2)
        self.assertEqual(student.memo, "복원 메모")
        send_mock.assert_not_called()

    @patch("apps.domains.messaging.services.send_welcome_messages")
    def test_json_bulk_create_reports_active_duplicate_without_new_account(self, send_mock):
        from apps.domains.students.models import Student
        from apps.domains.students.services import create_student_account

        existing = create_student_account(
            tenant=self.tenant,
            password="stud1234",
            student_data={
                "name": "JSON중복학생",
                "ps_number": "REGSAFE-BULK-1",
                "phone": "01077778900",
                "parent_phone": "01055556670",
                "omr_code": "77778900",
                "uses_identifier": False,
                "school_type": "HIGH",
                "grade": 1,
            },
        ).student
        request = self.factory.post(
            "/api/v1/students/bulk_create/",
            {
                "initial_password": "stud1234",
                "students": [
                    {
                        "name": "JSON중복학생",
                        "parent_phone": "01055556670",
                        "phone": "01077778901",
                        "school_type": "HIGH",
                        "grade": 1,
                    }
                ],
            },
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant

        response = StudentViewSet.as_view({"post": "bulk_create"})(request)

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["created"], 0)
        self.assertEqual(response.data["duplicates"][0]["student_id"], existing.id)
        self.assertEqual(
            Student.objects.filter(
                tenant=self.tenant,
                name="JSON중복학생",
                parent_phone="01055556670",
                deleted_at__isnull=True,
            ).count(),
            1,
        )
        send_mock.assert_not_called()

    @patch("apps.domains.messaging.services.send_welcome_messages")
    def test_bulk_resolve_delete_recreate_stages_parent_notice(self, send_mock):
        from apps.domains.students.models import Student
        from apps.domains.students.services import create_student_account, soft_delete_student

        old_student = create_student_account(
            tenant=self.tenant,
            password="oldpass123",
            student_data={
                "name": "삭제재생성대상",
                "ps_number": "REGSAFE-DEL-1",
                "phone": "01077778902",
                "parent_phone": "01055556671",
                "omr_code": "77778902",
                "uses_identifier": False,
                "school_type": "HIGH",
                "grade": 1,
            },
        ).student
        soft_delete_student(old_student, tenant=self.tenant)
        request = self.factory.post(
            "/api/v1/students/bulk_resolve_conflicts/",
            {
                "initial_password": "newpass123",
                "resolutions": [
                    {
                        "row": 1,
                        "student_id": old_student.id,
                        "action": "delete",
                        "student_data": {
                            "name": "삭제재생성대상",
                            "parent_phone": "01055556671",
                            "phone": "01077778903",
                            "school_type": "HIGH",
                            "grade": 1,
                        },
                    }
                ],
            },
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant

        response = StudentViewSet.as_view({"post": "bulk_resolve_conflicts"})(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["created"], 1)
        self.assertEqual(response.data["failed"], [])
        self.assertFalse(Student.objects.filter(id=old_student.id).exists())
        self.assertTrue(
            Student.objects.filter(
                tenant=self.tenant,
                name="삭제재생성대상",
                phone="01077778903",
                deleted_at__isnull=True,
            ).exists()
        )
        send_mock.assert_not_called()
        replacement = Student.objects.get(
            tenant=self.tenant,
            name="삭제재생성대상",
            phone="01077778903",
        )
        self.assertEqual(
            _decrypt(replacement.pending_account_notice_parent_password_ciphertext),
            "변경되지 않음",
        )

    def test_bulk_resolve_delete_recreate_failure_keeps_deleted_student(self):
        from apps.domains.students.models import Student
        from apps.domains.students.services import create_student_account, soft_delete_student

        old_student = create_student_account(
            tenant=self.tenant,
            password="oldpass123",
            student_data={
                "name": "롤백대상",
                "ps_number": "REGSAFE-ROLL-1",
                "phone": "01077778904",
                "parent_phone": "01055556672",
                "omr_code": "77778904",
                "uses_identifier": False,
                "school_type": "HIGH",
                "grade": 1,
            },
        ).student
        soft_delete_student(old_student, tenant=self.tenant)
        request = self.factory.post(
            "/api/v1/students/bulk_resolve_conflicts/",
            {
                "initial_password": "newpass123",
                "resolutions": [
                    {
                        "row": 1,
                        "student_id": old_student.id,
                        "action": "delete",
                        "student_data": {
                            "name": "롤백대상",
                            "parent_phone": "not-a-phone",
                            "phone": "01077778905",
                            "school_type": "HIGH",
                            "grade": 1,
                        },
                    }
                ],
            },
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant

        response = StudentViewSet.as_view({"post": "bulk_resolve_conflicts"})(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["created"], 0)
        self.assertEqual(len(response.data["failed"]), 1)
        old_student.refresh_from_db()
        self.assertIsNotNone(old_student.deleted_at)
        self.assertTrue(Student.objects.filter(id=old_student.id).exists())
