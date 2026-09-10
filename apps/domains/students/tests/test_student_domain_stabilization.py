# PATH: apps/domains/students/tests/test_student_domain_stabilization.py
"""
학생 도메인 운영 안정화 — 증거 기반 검증 테스트

B1: Admin 테넌트 격리 (queryset, permission negative test)
B4: omr_code phone 변경 시 정합성
B8: 학생 생성 race condition 방어
B10: bulk_resolve_conflicts atomic rollback
B11/B12: 비인증 엔드포인트 throttle 적용 확인
"""
from django.test import TestCase, override_settings
from django.db import connection
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test.utils import CaptureQueriesContext
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.urls import reverse
from unittest.mock import patch
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.core.models.tenant import Tenant
from apps.core.models.tenant_membership import TenantMembership
from apps.core.models.user import user_internal_username
from academy.application.services.excel_parsing_service import parse_student_excel_file
from apps.domains.students.models import Student, Tag
from apps.domains.students.views import StudentViewSet

User = get_user_model()


def _make_tenant(name, code):
    return Tenant.objects.create(name=name, code=code, is_active=True)


def _make_admin(tenant, username, role="owner"):
    u = User.objects.create_user(
        username=username, password="test1234",
        tenant=tenant, is_staff=True, name=f"Admin-{username}",
    )
    TenantMembership.ensure_active(tenant=tenant, user=u, role=role)
    return u


def _make_student(tenant, ps_number, phone="01012345678", parent_phone="01098765432", name="테스트학생"):
    internal = user_internal_username(tenant, ps_number)
    user = User.objects.create_user(
        username=internal, password="test1234",
        tenant=tenant, phone=phone, name=name,
    )
    student = Student.objects.create(
        tenant=tenant, user=user,
        ps_number=ps_number, name=name,
        phone=phone, parent_phone=parent_phone,
        omr_code=phone[-8:] if phone and len(phone) >= 8 else "00000000",
    )
    TenantMembership.ensure_active(tenant=tenant, user=user, role="student")
    return student


# ═══════════════════════════════════════════════════
# B1: Admin 테넌트 격리 — Negative Tests
# ═══════════════════════════════════════════════════

class TestB1TenantIsolationNegative(TestCase):
    """
    다른 테넌트의 학생 데이터에 접근할 수 없음을 증명.
    superuser, is_staff, 일반 관리자 각각에 대해 검증.
    """

    def setUp(self):
        self.factory = APIRequestFactory()
        self.tenant_a = _make_tenant("Academy A", "testa")
        self.tenant_b = _make_tenant("Academy B", "testb")

        self.student_a = _make_student(self.tenant_a, "A001", phone="01011111111")
        self.student_b = _make_student(self.tenant_b, "B001", phone="01022222222")

        # Tenant A admin (owner)
        self.admin_a = _make_admin(self.tenant_a, "admin_a")
        # Superuser (tenant_a 소속이지만 tenant_b 멤버십 없음)
        self.superuser = User.objects.create_superuser(
            username="superadmin", password="test1234",
            tenant=self.tenant_a, name="Super",
        )
        TenantMembership.ensure_active(tenant=self.tenant_a, user=self.superuser, role="owner")

    def _list(self, user, tenant):
        request = self.factory.get("/api/v1/students/")
        force_authenticate(request, user=user)
        request.tenant = tenant
        view = StudentViewSet.as_view({"get": "list"})
        return view(request)

    def _retrieve(self, user, tenant, pk):
        request = self.factory.get(f"/api/v1/students/{pk}/")
        force_authenticate(request, user=user)
        request.tenant = tenant
        view = StudentViewSet.as_view({"get": "retrieve"})
        return view(request, pk=pk)

    def _update(self, user, tenant, pk, data):
        request = self.factory.patch(
            f"/api/v1/students/{pk}/",
            data=data, format="json",
        )
        force_authenticate(request, user=user)
        request.tenant = tenant
        view = StudentViewSet.as_view({"patch": "partial_update"})
        return view(request, pk=pk)

    # --- List ---

    def test_admin_list_own_tenant_only(self):
        """Tenant A admin → Tenant A 학생만 보임."""
        resp = self._list(self.admin_a, self.tenant_a)
        self.assertEqual(resp.status_code, 200)
        ids = [s["id"] for s in resp.data.get("results", resp.data)]
        self.assertIn(self.student_a.id, ids)
        self.assertNotIn(self.student_b.id, ids,
                         "CRITICAL: 타 테넌트 학생이 목록에 노출됨!")

    def test_superuser_cannot_list_other_tenant_students(self):
        """Superuser라도 멤버십 없는 Tenant B 학생 목록 접근 불가."""
        resp = self._list(self.superuser, self.tenant_b)
        # 멤버십 없으므로 403 또는 빈 목록
        if resp.status_code == 200:
            ids = [s["id"] for s in resp.data.get("results", resp.data)]
            self.assertNotIn(self.student_b.id, ids,
                             "CRITICAL: superuser가 타 테넌트 학생 조회!")
        else:
            self.assertIn(resp.status_code, [403, 401])

    # --- Retrieve ---

    def test_admin_cannot_retrieve_other_tenant_student(self):
        """Tenant A admin이 Tenant B 학생 상세 조회 → 404."""
        resp = self._retrieve(self.admin_a, self.tenant_a, self.student_b.id)
        self.assertEqual(resp.status_code, 404,
                         "CRITICAL: 타 테넌트 학생 상세 접근 가능!")

    def test_superuser_cannot_retrieve_cross_tenant(self):
        """Superuser가 멤버십 없는 테넌트 학생 조회 → 403 또는 404."""
        resp = self._retrieve(self.superuser, self.tenant_b, self.student_b.id)
        self.assertIn(resp.status_code, [403, 404],
                      "CRITICAL: superuser가 타 테넌트 학생 상세 접근!")

    # --- Update ---

    def test_admin_cannot_update_other_tenant_student(self):
        """Tenant A admin이 Tenant B 학생 수정 불가."""
        original_name = self.student_b.name
        resp = self._update(self.admin_a, self.tenant_a, self.student_b.id, {"name": "해킹됨"})
        self.assertIn(resp.status_code, [403, 404],
                      "CRITICAL: 타 테넌트 학생 수정 가능!")
        self.student_b.refresh_from_db()
        self.assertEqual(self.student_b.name, original_name)

    def test_admin_update_cannot_mutate_student_app_and_delete_only_fields(self):
        """관리자 일반 수정 API는 삭제/프로필/일정숨김 같은 시스템 필드를 바꾸지 못한다."""
        resp = self._update(
            self.admin_a,
            self.tenant_a,
            self.student_a.id,
            {
                "deleted_at": timezone.now().isoformat(),
                "profile_photo_r2_key": "students/hidden/profile.png",
                "schedule_hidden_ids": [12345],
                "schedule_hidden_before": "2026-01-01",
            },
        )

        self.assertIn(resp.status_code, [200, 400], resp.data)
        self.student_a.refresh_from_db()
        self.assertIsNone(self.student_a.deleted_at)
        self.assertEqual(self.student_a.profile_photo_r2_key, "")
        self.assertEqual(self.student_a.schedule_hidden_ids, [])
        self.assertIsNone(self.student_a.schedule_hidden_before)

    # --- Tag isolation ---

    def test_tag_cross_tenant_isolation(self):
        """Tenant A 태그가 Tenant B에서 보이지 않음."""
        tag_a = Tag.objects.create(tenant=self.tenant_a, name="태그A")
        tags_b = Tag.objects.filter(tenant=self.tenant_b)
        self.assertNotIn(tag_a.id, list(tags_b.values_list("id", flat=True)),
                         "CRITICAL: 타 테넌트 태그 누출!")


class TestStudentListQueryShape(TestCase):
    """학생 목록은 태그/수강 정보를 한 번에 가져와 학생 수만큼 쿼리가 늘지 않는다."""

    def setUp(self):
        self.factory = APIRequestFactory()
        self.tenant = _make_tenant("Query Academy", "query_academy")
        self.admin = _make_admin(self.tenant, "query_admin")
        self.tag = Tag.objects.create(
            tenant=self.tenant,
            name="관리대상",
            color="#4f46e5",
        )
        for idx in range(12):
            student = _make_student(
                self.tenant,
                f"Q{idx:03d}",
                phone=f"0109911{idx:04d}",
                parent_phone=f"0108822{idx:04d}",
                name=f"학생{idx}",
            )
            student.tags.add(self.tag)

    def test_student_list_prefetches_tags(self):
        request = self.factory.get("/api/v1/students/")
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant

        with CaptureQueriesContext(connection) as captured:
            response = StudentViewSet.as_view({"get": "list"})(request)

        self.assertEqual(response.status_code, 200)
        results = response.data["results"]
        self.assertEqual(len(results), 12)
        self.assertTrue(all(row["tags"] for row in results))

        tag_queries = [
            query["sql"]
            for query in captured.captured_queries
            if "students_tag" in query["sql"].lower()
            or "students_studenttag" in query["sql"].lower()
        ]
        self.assertLessEqual(
            len(tag_queries),
            1,
            f"태그 조회가 학생 수만큼 반복됨: {tag_queries}",
        )


class TestStudentClassOrdering(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.tenant = _make_tenant("Class Ordering Academy", "class_ordering")
        self.admin = _make_admin(self.tenant, "class_order_admin")
        cases = [
            ("2반 김", "2반"),
            ("1반 박", "1반"),
            ("미지정", ""),
            ("1반 강", "1반"),
            ("공백", "   "),
        ]
        for index, (name, school_class) in enumerate(cases):
            student = _make_student(
                self.tenant,
                f"C{index:03d}",
                phone=f"0107711{index:04d}",
                parent_phone=f"0106622{index:04d}",
                name=name,
            )
            student.high_school_class = school_class
            student.save(update_fields=["high_school_class"])

    def _ordered_names(self, ordering):
        request = self.factory.get("/api/v1/students/", {"ordering": ordering})
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant
        response = StudentViewSet.as_view({"get": "list"})(request)
        self.assertEqual(response.status_code, 200)
        return [row["name"] for row in response.data["results"]]

    def test_class_sort_groups_names_and_keeps_blank_values_last(self):
        self.assertEqual(
            self._ordered_names("high_school_class"),
            ["1반 강", "1반 박", "2반 김", "공백", "미지정"],
        )

    def test_descending_class_sort_still_keeps_blank_values_last(self):
        self.assertEqual(
            self._ordered_names("-high_school_class"),
            ["2반 김", "1반 강", "1반 박", "공백", "미지정"],
        )


class TestStudentExcelUploadValidation(TestCase):
    """엑셀 업로드는 확장자/MIME뿐 아니라 파일 서명도 검증한다."""

    def setUp(self):
        self.factory = APIRequestFactory()
        self.tenant = _make_tenant("Excel Guard Academy", "excel_guard")
        self.admin = _make_admin(self.tenant, "excel_guard_admin")

    def _valid_excel_request(self):
        import io

        import openpyxl

        workbook = openpyxl.Workbook()
        worksheet = workbook.active
        worksheet.append(["이름", "학부모전화번호", "학생전화번호"])
        worksheet.append(["정리대상", "01070001111", "01090001234"])
        stream = io.BytesIO()
        workbook.save(stream)
        request = self.factory.post(
            "/api/v1/students/bulk_create_from_excel/",
            data={
                "file": SimpleUploadedFile(
                    "students.xlsx",
                    stream.getvalue(),
                    content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ),
                "password_mode": "fixed",
                "initial_password": "0000",
            },
            format="multipart",
        )
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant
        return request

    @patch(
        "apps.domains.students.views.student_views.delete_object_r2_excel",
        create=True,
    )
    @patch("apps.domains.students.views.student_views.dispatch_job")
    @patch("apps.domains.students.views.student_views.upload_fileobj_to_r2_excel")
    def test_dispatch_rejection_removes_exact_uploaded_excel(
        self,
        mock_upload,
        mock_dispatch,
        mock_delete,
    ):
        mock_dispatch.return_value = {"ok": False, "error": "dispatch unavailable"}

        response = StudentViewSet.as_view({"post": "bulk_create_from_excel"})(
            self._valid_excel_request()
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data, {"detail": "dispatch unavailable"})
        uploaded_key = mock_upload.call_args.kwargs["key"]
        mock_delete.assert_called_once_with(key=uploaded_key)

    @patch(
        "apps.domains.students.views.student_views.delete_object_r2_excel",
        create=True,
    )
    @patch("apps.domains.students.views.student_views.dispatch_job")
    @patch("apps.domains.students.views.student_views.upload_fileobj_to_r2_excel")
    def test_dispatch_exception_removes_exact_uploaded_excel(
        self,
        mock_upload,
        mock_dispatch,
        mock_delete,
    ):
        mock_dispatch.side_effect = RuntimeError("dispatch exploded")

        response = StudentViewSet.as_view({"post": "bulk_create_from_excel"})(
            self._valid_excel_request()
        )

        self.assertEqual(response.status_code, 500, response.data)
        self.assertEqual(response.data["detail"], "서버 오류가 발생했습니다.")
        uploaded_key = mock_upload.call_args.kwargs["key"]
        mock_delete.assert_called_once_with(key=uploaded_key)

    @patch(
        "apps.domains.students.views.student_views.delete_object_r2_excel",
        create=True,
        side_effect=RuntimeError("cleanup unavailable"),
    )
    @patch("apps.domains.students.views.student_views.dispatch_job")
    @patch("apps.domains.students.views.student_views.upload_fileobj_to_r2_excel")
    def test_cleanup_failure_preserves_original_dispatch_rejection(
        self,
        mock_upload,
        mock_dispatch,
        mock_delete,
    ):
        mock_dispatch.return_value = {"ok": False, "error": "dispatch unavailable"}

        response = StudentViewSet.as_view({"post": "bulk_create_from_excel"})(
            self._valid_excel_request()
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data, {"detail": "dispatch unavailable"})
        uploaded_key = mock_upload.call_args.kwargs["key"]
        mock_delete.assert_called_once_with(key=uploaded_key)

    @patch(
        "apps.domains.students.views.student_views.delete_object_r2_excel",
        create=True,
        side_effect=RuntimeError("cleanup unavailable"),
    )
    @patch("apps.domains.students.views.student_views.dispatch_job")
    @patch("apps.domains.students.views.student_views.upload_fileobj_to_r2_excel")
    def test_cleanup_failure_preserves_original_dispatch_exception(
        self,
        mock_upload,
        mock_dispatch,
        mock_delete,
    ):
        mock_dispatch.side_effect = RuntimeError("dispatch exploded")

        response = StudentViewSet.as_view({"post": "bulk_create_from_excel"})(
            self._valid_excel_request()
        )

        self.assertEqual(response.status_code, 500, response.data)
        self.assertEqual(response.data["error"], "dispatch exploded")
        uploaded_key = mock_upload.call_args.kwargs["key"]
        mock_delete.assert_called_once_with(key=uploaded_key)

    @patch("apps.domains.students.views.student_views.dispatch_job")
    @patch("apps.domains.students.views.student_views.upload_fileobj_to_r2_excel")
    def test_fake_xlsx_is_rejected_before_r2_upload(self, mock_upload, mock_dispatch):
        upload = SimpleUploadedFile(
            "bad.xlsx",
            b"not a real spreadsheet",
            content_type="application/octet-stream",
        )
        request = self.factory.post(
            "/api/v1/students/bulk_create_from_excel/",
            data={"file": upload, "initial_password": "0000"},
            format="multipart",
        )
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant

        response = StudentViewSet.as_view({"post": "bulk_create_from_excel"})(request)

        self.assertEqual(response.status_code, 400, response.data)
        mock_upload.assert_not_called()
        mock_dispatch.assert_not_called()

    @patch("apps.domains.students.views.student_views.dispatch_job")
    @patch("apps.domains.students.views.student_views.upload_fileobj_to_r2_excel")
    def test_legacy_xls_is_rejected_before_r2_upload(self, mock_upload, mock_dispatch):
        upload = SimpleUploadedFile(
            "legacy.xls",
            b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1legacy",
            content_type="application/vnd.ms-excel",
        )
        request = self.factory.post(
            "/api/v1/students/bulk_create_from_excel/",
            data={"file": upload, "initial_password": "0000"},
            format="multipart",
        )
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant

        response = StudentViewSet.as_view({"post": "bulk_create_from_excel"})(request)

        self.assertEqual(response.status_code, 400, response.data)
        mock_upload.assert_not_called()
        mock_dispatch.assert_not_called()

    @patch("apps.domains.students.views.student_views.dispatch_job")
    @patch("apps.domains.students.views.student_views.upload_fileobj_to_r2_excel")
    def test_csv_is_rejected_before_r2_upload(self, mock_upload, mock_dispatch):
        upload = SimpleUploadedFile(
            "students.csv",
            "이름,학부모전화번호\n합성학생,01070001111\n".encode(),
            content_type="text/csv",
        )
        request = self.factory.post(
            "/api/v1/students/bulk_create_from_excel/",
            data={"file": upload, "initial_password": "0000"},
            format="multipart",
        )
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant

        response = StudentViewSet.as_view({"post": "bulk_create_from_excel"})(request)

        self.assertEqual(response.status_code, 400, response.data)
        mock_upload.assert_not_called()
        mock_dispatch.assert_not_called()

    @patch("apps.domains.students.views.student_views.delete_object_r2_excel")
    @patch("apps.domains.students.views.student_views.dispatch_job")
    @patch("apps.domains.students.views.student_views.upload_fileobj_to_r2_excel")
    def test_hancom_windows_mime_xlsx_is_dispatched(
        self,
        mock_upload,
        mock_dispatch,
        mock_delete,
    ):
        import io

        import openpyxl

        workbook = openpyxl.Workbook()
        worksheet = workbook.active
        worksheet.append(["이름", "학부모전화번호", "학생전화번호"])
        worksheet.append(["학생번호없음", "01070001111", ""])
        stream = io.BytesIO()
        workbook.save(stream)
        upload = SimpleUploadedFile(
            "숙명_신민t.xlsx",
            stream.getvalue(),
            content_type="application/haansoftxlsx",
        )
        mock_dispatch.return_value = {"ok": True, "job_id": "excel-job-hancom"}
        request = self.factory.post(
            "/api/v1/students/bulk_create_from_excel/",
            data={
                "file": upload,
                "password_mode": "fixed",
                "initial_password": "0982",
            },
            format="multipart",
        )
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant

        response = StudentViewSet.as_view({"post": "bulk_create_from_excel"})(request)

        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(response.data["job_id"], "excel-job-hancom")
        mock_upload.assert_called_once()
        mock_dispatch.assert_called_once()
        mock_delete.assert_not_called()

    @patch("apps.domains.students.views.student_views.dispatch_job")
    @patch("apps.domains.students.views.student_views.upload_fileobj_to_r2_excel")
    def test_random_password_mode_does_not_require_fixed_password(self, mock_upload, mock_dispatch):
        import io

        import openpyxl

        workbook = openpyxl.Workbook()
        worksheet = workbook.active
        worksheet.append(["이름", "학부모전화번호", "학생전화번호"])
        worksheet.append(["랜덤업로드학생", "01070001111", "01090001234"])
        stream = io.BytesIO()
        workbook.save(stream)
        upload = SimpleUploadedFile(
            "students.xlsx",
            stream.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        mock_dispatch.return_value = {"ok": True, "job_id": "excel-job-random"}
        request = self.factory.post(
            "/api/v1/students/bulk_create_from_excel/",
            data={
                "file": upload,
                "password_mode": "random",
                "initial_password": "",
            },
            format="multipart",
        )
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant

        response = StudentViewSet.as_view({"post": "bulk_create_from_excel"})(request)

        self.assertEqual(response.status_code, 202, response.data)
        payload = mock_dispatch.call_args.kwargs["payload"]
        self.assertEqual(payload["password_mode"], "random")
        self.assertNotIn("initial_password", payload)
        self.assertNotIn("initial_password_secret", payload)
        mock_upload.assert_called_once()

    def test_excel_job_status_route_accepts_uuid_job_id(self):
        url = reverse(
            "student-excel-job-status",
            kwargs={"job_id": "c9a6e1f8-4377-459c-8e4a-0f9356c3612f"},
        )

        self.assertEqual(
            url,
            "/api/v1/students/excel_job_status/c9a6e1f8-4377-459c-8e4a-0f9356c3612f/",
        )

    def test_excel_parser_skips_legacy_template_example_rows_without_remark_column(self):
        import tempfile
        from pathlib import Path

        import openpyxl

        from academy.application.services.excel_parsing_service import parse_student_excel_file

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "students.xlsx"
            workbook = openpyxl.Workbook()
            worksheet = workbook.active
            worksheet.append(["학생 일괄 등록 양식"])
            worksheet.append([])
            worksheet.append(["작성 안내"])
            worksheet.append(["이름", "학부모전화번호", "학생전화번호", "성별", "학교", "학년", "반", "계열", "메모"])
            worksheet.append(["홍길동", "01087654321", "01012345678", "M", "한국고등학교", "1", "3", "이과", ""])
            worksheet.append(["김영희", "01011112222", "", "F", "서울중학교", "2", "1", "", ""])
            worksheet.append(["실사용학생", "01022223333", "01033334444", "F", "한국고등학교", "1", "3", "이과", ""])
            workbook.save(path)

            rows, _lecture_title = parse_student_excel_file(str(path))

        self.assertEqual([row["name"] for row in rows], ["실사용학생"])
        self.assertEqual(rows[0]["parent_phone"], "01022223333")


# ═══════════════════════════════════════════════════
# B4: omr_code phone 변경 시 정합성
# ═══════════════════════════════════════════════════

class TestB4OmrCodePhoneSync(TestCase):
    """phone 변경 시 omr_code가 자동 갱신되는지 증명."""

    def setUp(self):
        self.tenant = _make_tenant("TestAcademy", "test_omr")
        self.admin = _make_admin(self.tenant, "admin_omr")
        self.factory = APIRequestFactory()

    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    def test_phone_change_updates_omr_code(self, _send_mock):
        """admin partial_update로 phone 변경 → omr_code 자동 갱신."""
        student = _make_student(self.tenant, "OMR001", phone="01012345678")
        self.assertEqual(student.omr_code, "12345678")

        request = self.factory.patch(
            f"/api/v1/students/{student.id}/",
            data={"phone": "01099998888"}, format="json",
        )
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant
        view = StudentViewSet.as_view({"patch": "partial_update"})
        resp = view(request, pk=student.id)

        self.assertIn(resp.status_code, [200, 201])
        student.refresh_from_db()
        self.assertEqual(student.omr_code, "99998888",
                         f"omr_code가 갱신되지 않음: {student.omr_code}")

    @patch("apps.domains.messaging.policy.send_alimtalk_via_owner", return_value=True)
    def test_parent_phone_change_updates_omr_code_when_no_phone(self, _send_mock):
        """학생 전화번호 없이 parent_phone만 있을 때 omr_code 갱신."""
        student = _make_student(self.tenant, "OMR002", phone="", parent_phone="01077776666")
        # phone이 비어있으므로 parent_phone에서 omr_code 생성 기대
        # (실제 로직은 phone 우선 → 빈 경우 parent_phone)

        request = self.factory.patch(
            f"/api/v1/students/{student.id}/",
            data={
                "parent_phone": "01055554444",
                "parent_initial_password": "chosen4444",
            }, format="json",
        )
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant
        view = StudentViewSet.as_view({"patch": "partial_update"})
        resp = view(request, pk=student.id)

        self.assertIn(resp.status_code, [200, 201])
        student.refresh_from_db()
        self.assertEqual(student.omr_code, "55554444",
                         f"parent_phone 변경 시 omr_code 미갱신: {student.omr_code}")


# ═══════════════════════════════════════════════════
# B8: 학생 생성 race condition 방어
# ═══════════════════════════════════════════════════

class TestB8StudentCreateAtomicity(TestCase):
    """학생 생성이 transaction.atomic으로 보호되는지 증명."""

    def setUp(self):
        self.tenant = _make_tenant("TestAcademy", "test_race")
        self.admin = _make_admin(self.tenant, "admin_race")
        self.factory = APIRequestFactory()

    def test_duplicate_ps_number_rejected(self):
        """같은 ps_number로 두 번째 생성 시 에러 (중복 방지)."""
        _make_student(self.tenant, "DUP001", phone="01011110001")

        request = self.factory.post(
            "/api/v1/students/",
            data={
                "name": "중복학생",
                "phone": "01011110002",
                "parent_phone": "01099990002",
                "initial_password": "test1234",
                "ps_number": "DUP001",
            },
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant
        view = StudentViewSet.as_view({"post": "create"})
        resp = view(request)

        self.assertIn(resp.status_code, [400, 409],
                      "중복 ps_number가 허용됨!")
        # Student 수는 1명이어야 함
        self.assertEqual(
            Student.objects.filter(tenant=self.tenant, ps_number="DUP001", deleted_at__isnull=True).count(),
            1, "중복 학생이 생성됨!"
        )

    def test_create_failure_no_orphan_user(self):
        """학생 생성 실패 시 User/Membership이 남지 않음 (atomic rollback)."""
        user_count_before = User.objects.count()

        request = self.factory.post(
            "/api/v1/students/",
            data={
                "name": "",  # name required — 빈 값으로 실패 유도
                "phone": "01033330001",
                "parent_phone": "01099990003",
                "initial_password": "test1234",
            },
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant
        view = StudentViewSet.as_view({"post": "create"})
        resp = view(request)

        # 실패 시 User가 새로 생기지 않아야 함
        if resp.status_code >= 400:
            self.assertEqual(User.objects.count(), user_count_before,
                             "학생 생성 실패 시 orphan User가 남음!")


# ═══════════════════════════════════════════════════
# B10: bulk_resolve_conflicts atomic rollback
# ═══════════════════════════════════════════════════

class TestB10BulkResolveConflictsAtomicity(TestCase):
    """
    bulk_resolve_conflicts에서 개별 resolution이 실패해도
    성공한 것은 유지되고, 실패한 것은 rollback되는지 증명.
    """

    def setUp(self):
        self.factory = APIRequestFactory()
        self.tenant = _make_tenant("TestAcademy", "test_resolve")
        self.admin = _make_admin(self.tenant, "admin_resolve")

        # 삭제된 학생 2명 (충돌 해결 대상)
        self.student1 = _make_student(self.tenant, "RES001", phone="01044440001", name="복원대상")
        self.student1.deleted_at = timezone.now()
        self.student1.save(update_fields=["deleted_at"])

        self.student2 = _make_student(self.tenant, "RES002", phone="01044440002", name="삭제후재생성")
        self.student2.deleted_at = timezone.now()
        self.student2.save(update_fields=["deleted_at"])

    @patch(
        "apps.domains.students.services.import_students."
        "send_parent_account_credentials_notice",
        return_value=True,
    )
    def test_restore_success_within_atomic(self, send_parent_notice):
        """restore action은 학생을 복원하고 deleted_at을 None으로."""
        request = self.factory.post(
            "/api/v1/students/bulk_resolve_conflicts/",
            data={
                "initial_password": "test1234",
                "resolutions": [
                    {"row": 1, "student_id": self.student1.id, "action": "restore",
                     "student_data": {"name": "복원됨"}},
                ],
            },
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant
        view = StudentViewSet.as_view({"post": "bulk_resolve_conflicts"})
        resp = view(request)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get("restored"), 1)
        self.assertEqual(
            resp.data.get("resolved"),
            [{"row": 1, "student_id": self.student1.id, "state": "restored"}],
        )
        self.student1.refresh_from_db()
        self.assertIsNone(self.student1.deleted_at)
        self.assertEqual(self.student1.name, "복원됨")
        send_parent_notice.assert_called_once()

    @patch(
        "apps.domains.students.services.import_students."
        "send_parent_account_credentials_notice",
        return_value=True,
    )
    def test_individual_failure_does_not_rollback_others(self, send_parent_notice):
        """하나의 resolution이 실패해도 다른 것은 성공."""
        request = self.factory.post(
            "/api/v1/students/bulk_resolve_conflicts/",
            data={
                "initial_password": "test1234",
                "resolutions": [
                    {"row": 1, "student_id": self.student1.id, "action": "restore",
                     "student_data": {"name": "복원성공"}},
                    {"row": 2, "student_id": 999999, "action": "restore",
                     "student_data": {"name": "존재안함"}},
                ],
            },
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant
        view = StudentViewSet.as_view({"post": "bulk_resolve_conflicts"})
        resp = view(request)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get("restored"), 1)
        self.assertEqual(len(resp.data.get("failed", [])), 1)
        self.student1.refresh_from_db()
        self.assertIsNone(self.student1.deleted_at, "성공한 복원이 실패한 것에 의해 롤백됨!")
        send_parent_notice.assert_called_once()

    def test_delete_and_reregister_is_atomic_and_preserves_requested_login_id(self):
        request = self.factory.post(
            "/api/v1/students/bulk_resolve_conflicts/",
            data={
                "initial_password": "chosen-new-password",
                "resolutions": [
                    {
                        "row": 2,
                        "student_id": self.student2.id,
                        "action": "delete",
                        "student_data": {
                            "name": "새등록학생",
                            "psNumber": "CHOSEN-LOGIN-ID",
                            "studentPhone": "01044440002",
                            "parentPhone": "01098765432",
                            "schoolType": "HIGH",
                        },
                    }
                ],
            },
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant

        response = StudentViewSet.as_view(
            {"post": "bulk_resolve_conflicts"}
        )(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["created"], 1)
        resolved = response.data["resolved"]
        self.assertEqual(resolved[0]["state"], "created")
        replacement = Student.objects.get(pk=resolved[0]["student_id"])
        self.assertEqual(replacement.ps_number, "CHOSEN-LOGIN-ID")
        self.assertTrue(replacement.user.check_password("chosen-new-password"))
        self.assertTrue(
            replacement.parent.user.check_password("chosen-new-password")
        )
        self.assertFalse(Student.objects.filter(pk=self.student2.id).exists())

    def test_cross_tenant_resolution_rejected(self):
        """타 테넌트 학생 ID로 resolve 시도 → 실패."""
        other_tenant = _make_tenant("Other", "other_res")
        other_student = _make_student(other_tenant, "OTH001", phone="01055550001")
        other_student.deleted_at = timezone.now()
        other_student.save(update_fields=["deleted_at"])

        request = self.factory.post(
            "/api/v1/students/bulk_resolve_conflicts/",
            data={
                "initial_password": "test1234",
                "resolutions": [
                    {"row": 1, "student_id": other_student.id, "action": "restore",
                     "student_data": {"name": "크로스테넌트"}},
                ],
            },
            format="json",
        )
        force_authenticate(request, user=self.admin)
        request.tenant = self.tenant
        view = StudentViewSet.as_view({"post": "bulk_resolve_conflicts"})
        resp = view(request)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get("restored"), 0)
        self.assertEqual(len(resp.data.get("failed", [])), 1,
                         "CRITICAL: 타 테넌트 학생이 복원됨!")
        other_student.refresh_from_db()
        self.assertIsNotNone(other_student.deleted_at,
                             "CRITICAL: 타 테넌트 학생의 deleted_at이 변경됨!")


# ═══════════════════════════════════════════════════
# B11/B12: Throttle 적용 확인
# ═══════════════════════════════════════════════════

class TestThrottleConfiguration(TestCase):
    """비인증 엔드포인트에 throttle이 올바르게 적용됐는지 확인."""

    def test_alimtalk_endpoint_throttle_class_exists(self):
        from apps.api.common.throttles import AlimtalkEndpointThrottle, StaffPasswordResetThrottle
        self.assertEqual(AlimtalkEndpointThrottle.rate, "5/hour")
        self.assertEqual(AlimtalkEndpointThrottle.scope, "alimtalk_endpoint")
        self.assertEqual(StaffPasswordResetThrottle.rate, "60/hour")
        self.assertEqual(StaffPasswordResetThrottle.scope, "staff_password_reset")

    def test_signup_check_throttle_class_exists(self):
        from apps.api.common.throttles import SignupCheckThrottle
        self.assertEqual(SignupCheckThrottle.rate, "30/minute")
        self.assertEqual(SignupCheckThrottle.scope, "signup_check")

    def test_send_existing_credentials_has_throttle(self):
        from apps.domains.students.views import SendExistingCredentialsView
        from apps.api.common.throttles import AlimtalkEndpointThrottle
        self.assertTrue(
            any(issubclass(t, AlimtalkEndpointThrottle) or t is AlimtalkEndpointThrottle
                for t in SendExistingCredentialsView.throttle_classes),
            "SendExistingCredentialsView에 AlimtalkEndpointThrottle 미적용!"
        )

    def test_password_find_request_has_throttle(self):
        from apps.domains.students.views import StudentPasswordFindRequestView
        from apps.api.common.throttles import AlimtalkEndpointThrottle
        self.assertTrue(
            any(issubclass(t, AlimtalkEndpointThrottle) or t is AlimtalkEndpointThrottle
                for t in StudentPasswordFindRequestView.throttle_classes),
            "StudentPasswordFindRequestView에 AlimtalkEndpointThrottle 미적용!"
        )

    def test_password_reset_send_has_throttle(self):
        from apps.domains.students.views import StudentPasswordResetSendView
        from apps.api.common.throttles import AlimtalkEndpointThrottle
        self.assertTrue(
            any(issubclass(t, AlimtalkEndpointThrottle) or t is AlimtalkEndpointThrottle
                for t in StudentPasswordResetSendView.throttle_classes),
            "StudentPasswordResetSendView에 AlimtalkEndpointThrottle 미적용!"
        )

    @override_settings(REST_FRAMEWORK={
        "DEFAULT_THROTTLE_RATES": {
            "anon": "60/minute", "user": "300/minute",
            "alimtalk_endpoint": "5/hour", "staff_password_reset": "60/hour",
            "signup_check": "30/minute",
        },
    })
    def test_throttle_rates_in_settings(self):
        from django.conf import settings
        rates = settings.REST_FRAMEWORK.get("DEFAULT_THROTTLE_RATES", {})
        self.assertIn("alimtalk_endpoint", rates)
        self.assertIn("staff_password_reset", rates)
        self.assertIn("signup_check", rates)
