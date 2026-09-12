"""Integration coverage for student-reported scores across inventory and results."""

import json
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import transaction
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.core.models import TenantMembership
from apps.domains.clinic.tests import ClinicTestMixin
from apps.domains.inventory.models import InventoryFile, InventoryFolder
from apps.domains.inventory.services import move_file, move_folder
from apps.domains.inventory.views import (
    FileDeleteView,
    FileUploadView,
    FolderDeleteView,
    InventoryListView,
)
from apps.domains.parents.models import Parent
from apps.domains.results.models import StudentReportedScore
from apps.domains.students.models import Student
from apps.domains.submissions.models import SubmissionStorageCleanupIntent
from apps.domains.submissions.services.lifecycle import process_submission_storage_cleanup_intents
from apps.domains.results.views.admin_student_performance_view import (
    AdminStudentPerformanceView,
)
from apps.domains.results.views.admin_student_reported_score_view import (
    AdminStudentReportedScoreReviewView,
)


User = get_user_model()


class StudentReportedScoreTest(TestCase, ClinicTestMixin):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.data = self.setup_full_tenant("reported-score", student_count=1)
        self.tenant = self.data["tenant"]
        self.student = self.data["students"][0]
        self.student_user = self.student.user
        self.admin = User.objects.create_user(
            username="reported_score_admin",
            password="test1234",
            tenant=self.tenant,
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=self.admin, role="admin")
        self.parent_user = User.objects.create_user(
            username="reported_score_parent",
            password="test1234",
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
            name="Reported Score Parent",
            phone="01099998888",
        )
        self.student.parent = self.parent
        self.student.save(update_fields=["parent"])

    def _student_upload(
        self,
        *,
        source="school_exam",
        extra=None,
        filename="score.jpg",
        content=b"\xff\xd8\xff\xe0score-image",
        r2_available=True,
    ):
        data = {
            "scope": "student",
            "student_ps": self.student.ps_number,
            "score_submission": "true",
            "score_source": source,
            "academic_year": "2026",
            "subject": "수학",
            "score": "88",
            "max_score": "100",
            "exam_date": "2026-04-28",
            "semester": "1",
            "exam_round": "first",
            "description": "학교 성적표 제출",
            "file": SimpleUploadedFile(
                filename,
                content,
                content_type="image/jpeg",
            ),
        }
        if source != "school_exam":
            data.pop("semester")
            data.pop("exam_round")
            data["exam_month"] = "6"
            data["grade_scale"] = "nine"
            data["grade_rank"] = "2"
            data["percentile"] = "91.5"
        if extra:
            data.update(extra)
        request = self.factory.post("/storage/inventory/upload/", data=data, format="multipart")
        request.tenant = self.tenant
        upload_patch = (
            patch("apps.domains.inventory.views.upload_fileobj_to_r2_storage")
            if r2_available
            else patch("apps.domains.inventory.views.upload_fileobj_to_r2_storage", new=None)
        )
        with (
            patch(
                "apps.domains.inventory.views.JWTAuthentication.authenticate",
                return_value=(self.student_user, None),
            ),
            patch(
                "apps.domains.inventory.views.Program.ensure_for_tenant",
                return_value=SimpleNamespace(plan="pro"),
            ),
            upload_patch as upload_r2,
        ):
            response = FileUploadView.as_view()(request)
        return response, upload_r2

    def _parent_upload(self, *, student=None, selected_student_id=None):
        student = student or self.student
        data = {
            "scope": "student",
            "student_ps": student.ps_number,
            "score_submission": "true",
            "score_source": "school_exam",
            "academic_year": "2026",
            "semester": "1",
            "exam_round": "first",
            "subject": "수학",
            "score": "88",
            "max_score": "100",
            "exam_date": "2026-04-28",
            "file": SimpleUploadedFile(
                "parent-score.jpg",
                b"\xff\xd8\xff\xe0parent-score",
                content_type="image/jpeg",
            ),
        }
        request = self.factory.post(
            "/storage/inventory/upload/",
            data=data,
            format="multipart",
            HTTP_X_STUDENT_ID=str(
                selected_student_id if selected_student_id is not None else student.id
            ),
        )
        request.tenant = self.tenant
        with (
            patch(
                "apps.domains.inventory.views.JWTAuthentication.authenticate",
                return_value=(self.parent_user, None),
            ),
            patch(
                "apps.domains.inventory.views.Program.ensure_for_tenant",
                return_value=SimpleNamespace(plan="pro"),
            ),
            patch("apps.domains.inventory.views.upload_fileobj_to_r2_storage") as upload_r2,
        ):
            response = FileUploadView.as_view()(request)
        return response, upload_r2

    def test_parent_selected_child_score_upload_persists_and_reloads_for_parent_and_staff(self):
        response, upload_r2 = self._parent_upload()

        self.assertEqual(response.status_code, 200, response.content)
        payload = json.loads(response.content)
        upload_r2.assert_called_once()
        score = StudentReportedScore.objects.get(id=payload["scoreSubmission"]["id"])
        self.assertEqual(score.student_id, self.student.id)
        self.assertEqual(score.submitted_by_id, self.parent_user.id)
        self.assertEqual(score.evidence_file.student_ps, self.student.ps_number)

        for user in (self.parent_user, self.admin):
            request = self.factory.get(
                "/storage/inventory/",
                {"scope": "student", "student_ps": self.student.ps_number},
                HTTP_X_STUDENT_ID=str(self.student.id),
            )
            request.tenant = self.tenant
            with patch(
                "apps.domains.inventory.views.JWTAuthentication.authenticate",
                return_value=(user, None),
            ):
                listed = InventoryListView.as_view()(request)
            self.assertEqual(listed.status_code, 200, listed.content)
            listed_file = json.loads(listed.content)["files"][0]
            self.assertEqual(listed_file["scoreSubmission"]["id"], score.id)

    def test_parent_score_upload_rejects_unowned_and_cross_tenant_children_without_storage(self):
        unowned_user = User.objects.create_user(
            username="reported_score_unowned",
            password="test1234",
            tenant=self.tenant,
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=unowned_user,
            role="student",
        )
        unowned = Student.objects.create(
            tenant=self.tenant,
            user=unowned_user,
            name="Unowned",
            ps_number="RS-UNOWNED",
            omr_code="77889900",
        )
        other_tenant = type(self.tenant).objects.create(
            code="reported-score-other",
            name="Reported Score Other",
            is_active=True,
        )
        foreign_user = User.objects.create_user(
            username="reported_score_foreign",
            password="test1234",
            tenant=other_tenant,
        )
        foreign = Student.objects.create(
            tenant=other_tenant,
            user=foreign_user,
            name="Foreign",
            ps_number="RS-FOREIGN",
            omr_code="77889901",
        )

        for student in (unowned, foreign):
            response, upload_r2 = self._parent_upload(
                student=student,
                selected_student_id=student.id,
            )
            self.assertEqual(response.status_code, 403, response.content)
            upload_r2.assert_not_called()
        self.assertFalse(InventoryFile.objects.filter(original_name="parent-score.jpg").exists())
        self.assertFalse(StudentReportedScore.objects.exists())

    def _review(self, score_id, action="verify", *, tenant=None, user=None, extra=None):
        data = {"action": action}
        if extra:
            data.update(extra)
        request = self.factory.patch(
            f"/results/admin/reported-scores/{score_id}/review/",
            data=data,
            format="json",
        )
        request.tenant = tenant or self.tenant
        force_authenticate(request, user=user or self.admin)
        return AdminStudentReportedScoreReviewView.as_view()(request, score_id=score_id)

    def _console(self):
        request = self.factory.get("/results/admin/student-performance/", {"days": "all"})
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)
        return AdminStudentPerformanceView.as_view()(request)

    def test_student_upload_creates_pending_score_with_evidence_and_inventory_projection(self):
        response, upload_r2 = self._student_upload()

        self.assertEqual(response.status_code, 200, response.content)
        payload = json.loads(response.content)
        self.assertEqual(payload["scoreSubmission"]["status"], "pending")
        self.assertEqual(payload["scoreSubmission"]["label"], "2026년 1학기 1차 지필평가(중간)")
        upload_r2.assert_called_once()

        row = StudentReportedScore.objects.get()
        self.assertEqual(row.tenant, self.tenant)
        self.assertEqual(row.student, self.student)
        self.assertEqual(row.evidence_file.scope, "student")

        list_request = self.factory.get(
            "/storage/inventory/",
            {"scope": "student", "student_ps": self.student.ps_number},
        )
        list_request.tenant = self.tenant
        with patch(
            "apps.domains.inventory.views.JWTAuthentication.authenticate",
            return_value=(self.student_user, None),
        ):
            listed = InventoryListView.as_view()(list_request)
        self.assertEqual(json.loads(listed.content)["files"][0]["scoreSubmission"]["status"], "pending")

    def test_invalid_student_metadata_is_rejected_before_r2_side_effect(self):
        response, upload_r2 = self._student_upload(extra={"score": "120"})

        self.assertEqual(response.status_code, 400)
        upload_r2.assert_not_called()
        self.assertFalse(InventoryFile.objects.exists())
        self.assertFalse(StudentReportedScore.objects.exists())

    def test_masqueraded_evidence_file_is_rejected_before_r2_side_effect(self):
        response, upload_r2 = self._student_upload(content=b"not-a-real-jpeg")

        self.assertEqual(response.status_code, 400)
        self.assertIn("파일 내용", json.loads(response.content)["detail"])
        upload_r2.assert_not_called()

    def test_kice_mock_accepts_the_schedule_month_printed_on_the_report(self):
        response, upload_r2 = self._student_upload(
            source="kice_mock",
            extra={"exam_month": "8"},
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(json.loads(response.content)["scoreSubmission"]["exam_month"], 8)
        upload_r2.assert_called_once()

    def test_pending_score_enters_review_queue_and_verified_score_enters_statistics(self):
        upload, _upload_r2 = self._student_upload()
        score_id = json.loads(upload.content)["scoreSubmission"]["id"]

        pending_console = self._console()
        self.assertEqual(pending_console.status_code, 200, pending_console.data)
        self.assertEqual(pending_console.data["summary"]["pending_reported_score_count"], 1)
        self.assertEqual(
            pending_console.data["students"][0]["source_summaries"]["school"]["scored_count"],
            0,
        )

        reviewed = self._review(score_id)
        self.assertEqual(reviewed.status_code, 200, reviewed.data)
        self.assertEqual(reviewed.data["status"], "verified")

        verified_console = self._console()
        student = verified_console.data["students"][0]
        self.assertEqual(verified_console.data["summary"]["pending_reported_score_count"], 0)
        self.assertEqual(student["source_summaries"]["school"]["scored_count"], 1)
        self.assertEqual(student["source_summaries"]["school"]["latest_score_pct"], 88.0)
        self.assertEqual(
            student["subject_summaries"]["school"]["수학"]["latest_score_pct"],
            88.0,
        )
        self.assertEqual(verified_console.data["filter_options"]["reported_subjects"], ["수학"])

    def test_five_grade_and_school_achievement_metrics_are_preserved(self):
        upload, _ = self._student_upload(extra={
            "grade_scale": "five",
            "grade_rank": "4",
            "achievement_level": "B",
            "subject_average": "71.25",
            "standard_deviation": "12.4",
            "cohort_size": "198",
        })

        self.assertEqual(upload.status_code, 200, upload.content)
        score = json.loads(upload.content)["scoreSubmission"]
        self.assertEqual(score["grade_scale"], "five")
        self.assertEqual(score["grade_rank"], 4)
        self.assertEqual(score["achievement_level"], "B")
        self.assertEqual(score["subject_average"], 71.25)
        self.assertEqual(score["standard_deviation"], 12.4)
        self.assertEqual(score["cohort_size"], 198)

    def test_five_grade_scale_rejects_rank_above_five(self):
        upload, upload_r2 = self._student_upload(extra={
            "grade_scale": "five",
            "grade_rank": "6",
        })

        self.assertEqual(upload.status_code, 400)
        upload_r2.assert_not_called()

    def test_grade_rank_requires_explicit_grade_scale(self):
        upload, upload_r2 = self._student_upload(extra={"grade_rank": "2"})

        self.assertEqual(upload.status_code, 400)
        self.assertIn("5등급제 또는 9등급제", json.loads(upload.content)["detail"])
        upload_r2.assert_not_called()

    def test_verifying_corrected_submission_replaces_previous_verified_score(self):
        first, _ = self._student_upload(filename="first.jpg")
        first_id = json.loads(first.content)["scoreSubmission"]["id"]
        self.assertEqual(self._review(first_id).status_code, 200)

        second, _ = self._student_upload(extra={"score": "93"}, filename="second.jpg")
        second_id = json.loads(second.content)["scoreSubmission"]["id"]
        self.assertEqual(self._review(second_id).status_code, 200)

        self.assertEqual(StudentReportedScore.objects.get(id=first_id).status, "rejected")
        self.assertEqual(StudentReportedScore.objects.get(id=second_id).status, "verified")
        self.assertEqual(StudentReportedScore.objects.filter(status="verified").count(), 1)

    def test_verified_trends_are_separated_by_subject(self):
        math_upload, _ = self._student_upload(filename="math.jpg")
        self.assertEqual(
            self._review(json.loads(math_upload.content)["scoreSubmission"]["id"]).status_code,
            200,
        )
        english_upload, _ = self._student_upload(
            filename="english.jpg",
            extra={"subject": "영어", "score": "50"},
        )
        self.assertEqual(
            self._review(json.loads(english_upload.content)["scoreSubmission"]["id"]).status_code,
            200,
        )

        response = self._console()
        summaries = response.data["students"][0]["subject_summaries"]["school"]
        self.assertEqual(summaries["수학"]["latest_score_pct"], 88.0)
        self.assertEqual(summaries["영어"]["latest_score_pct"], 50.0)
        self.assertIsNone(summaries["수학"]["change_pct_points"])
        self.assertIsNone(summaries["영어"]["change_pct_points"])

    def test_review_locks_student_as_shared_concurrency_key(self):
        upload, _ = self._student_upload()
        score_id = json.loads(upload.content)["scoreSubmission"]["id"]

        with patch(
            "apps.support.results.student_reported_scores.Student.objects.select_for_update",
            wraps=Student.objects.select_for_update,
        ) as lock_student:
            response = self._review(score_id)

        self.assertEqual(response.status_code, 200)
        lock_student.assert_called_once_with()

    def test_linked_evidence_cannot_be_deleted_and_foreign_tenant_cannot_review(self):
        upload, _ = self._student_upload()
        payload = json.loads(upload.content)
        inv_file_id = int(payload["id"])
        score_id = payload["scoreSubmission"]["id"]

        delete_request = self.factory.delete(
            f"/storage/inventory/files/{inv_file_id}/?scope=student&student_ps={self.student.ps_number}"
        )
        delete_request.tenant = self.tenant
        with patch(
            "apps.domains.inventory.views.JWTAuthentication.authenticate",
            return_value=(self.student_user, None),
        ):
            deleted = FileDeleteView.as_view()(delete_request, file_id=inv_file_id)
        self.assertEqual(deleted.status_code, 409)
        self.assertTrue(InventoryFile.objects.filter(id=inv_file_id).exists())

        other = self.setup_full_tenant("reported-score-other", student_count=1)
        other_admin = User.objects.create_user(
            username="reported_score_other_admin",
            password="test1234",
            tenant=other["tenant"],
        )
        TenantMembership.ensure_active(
            tenant=other["tenant"],
            user=other_admin,
            role="admin",
        )
        foreign_review = self._review(
            score_id,
            tenant=other["tenant"],
            user=other_admin,
        )
        self.assertEqual(foreign_review.status_code, 404)

    def test_recursive_folder_delete_routes_terminal_score_evidence_to_single_file_delete(self):
        upload, _ = self._student_upload()
        upload_payload = json.loads(upload.content)
        inv_file = InventoryFile.objects.get(id=int(upload_payload["id"]))
        score_id = upload_payload["scoreSubmission"]["id"]
        self.assertEqual(self._review(score_id, action="reject").status_code, 200)
        folder = InventoryFolder.objects.create(
            tenant=self.tenant,
            scope="student",
            student_ps=self.student.ps_number,
            name="성적표",
        )
        inv_file.folder = folder
        inv_file.save(update_fields=["folder", "updated_at"])

        request = self.factory.delete(
            f"/storage/inventory/folders/{folder.id}/"
            f"?scope=student&student_ps={self.student.ps_number}&recursive=true"
        )
        request.tenant = self.tenant
        with (
            patch(
                "apps.domains.inventory.views.JWTAuthentication.authenticate",
                return_value=(self.student_user, None),
            ),
            patch(
                "apps.domains.inventory.services.delete_object_r2_storage"
            ) as delete_r2,
        ):
            response = FolderDeleteView.as_view()(request, folder_id=folder.id)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(json.loads(response.content)["code"], "reported_score_evidence_protected")
        delete_r2.assert_not_called()
        self.assertTrue(InventoryFile.objects.filter(id=inv_file.id).exists())
        self.assertTrue(InventoryFolder.objects.filter(id=folder.id).exists())

    def test_score_evidence_cannot_move_without_a_durable_old_key_cleanup_record(self):
        upload, _ = self._student_upload()
        inv_file = InventoryFile.objects.get(id=int(json.loads(upload.content)["id"]))
        source_folder = InventoryFolder.objects.create(
            tenant=self.tenant,
            scope="student",
            student_ps=self.student.ps_number,
            name="원본 성적표",
        )
        target_folder = InventoryFolder.objects.create(
            tenant=self.tenant,
            scope="student",
            student_ps=self.student.ps_number,
            name="이동 대상",
        )
        inv_file.folder = source_folder
        inv_file.save(update_fields=["folder", "updated_at"])

        with (
            patch("apps.domains.inventory.services.copy_object_r2_storage") as copy_r2,
            patch("apps.domains.inventory.services.delete_object_r2_storage") as delete_r2,
        ):
            file_result = move_file(
                tenant=self.tenant,
                scope="student",
                student_ps=self.student.ps_number,
                source_file_id=inv_file.id,
                target_folder_id=target_folder.id,
            )
            folder_result = move_folder(
                tenant=self.tenant,
                scope="student",
                student_ps=self.student.ps_number,
                source_folder_id=source_folder.id,
                target_folder_id=target_folder.id,
            )

        self.assertEqual(file_result["status"], 409)
        self.assertEqual(folder_result["status"], 409)
        self.assertEqual(file_result["code"], "reported_score_evidence_protected")
        self.assertEqual(folder_result["code"], "reported_score_evidence_protected")
        copy_r2.assert_not_called()
        delete_r2.assert_not_called()
        inv_file.refresh_from_db()
        source_folder.refresh_from_db()
        self.assertEqual(inv_file.folder_id, source_folder.id)
        self.assertIsNone(source_folder.parent_id)

    def test_one_evidence_upload_creates_and_reviews_multiple_subjects_atomically(self):
        score_items = json.dumps([
            {"subject": "수학", "score": 88, "max_score": 100},
            {"subject": "영어", "score": 92, "max_score": 100},
        ])
        response, upload_r2 = self._student_upload(extra={"score_items": score_items})

        self.assertEqual(response.status_code, 200, response.content)
        payload = json.loads(response.content)
        self.assertEqual(len(payload["scoreSubmissions"]), 2)
        self.assertEqual(StudentReportedScore.objects.count(), 2)
        self.assertEqual(StudentReportedScore.objects.values("evidence_file_id").distinct().count(), 1)
        upload_r2.assert_called_once()

        score_id = payload["scoreSubmissions"][0]["id"]
        reviewed = self._review(score_id, extra={"review_all_evidence": True})
        self.assertEqual(reviewed.status_code, 200, reviewed.data)
        self.assertEqual(len(reviewed.data["score_submissions"]), 2)
        self.assertEqual(StudentReportedScore.objects.filter(status="verified").count(), 2)

        list_request = self.factory.get(
            "/storage/inventory/",
            {"scope": "student", "student_ps": self.student.ps_number},
        )
        list_request.tenant = self.tenant
        with patch(
            "apps.domains.inventory.views.JWTAuthentication.authenticate",
            return_value=(self.student_user, None),
        ):
            listed = InventoryListView.as_view()(list_request)
        listed_file = json.loads(listed.content)["files"][0]
        self.assertEqual(len(listed_file["scoreSubmissions"]), 2)
        self.assertEqual(listed_file["scoreSubmission"]["subject"], "수학")

    def test_invalid_multi_subject_row_rejects_whole_report_before_storage_write(self):
        score_items = json.dumps([
            {"subject": "수학", "score": 88, "max_score": 100},
            {"subject": "영어", "score": 120, "max_score": 100},
        ])
        response, upload_r2 = self._student_upload(extra={"score_items": score_items})

        self.assertEqual(response.status_code, 400)
        upload_r2.assert_not_called()
        self.assertFalse(InventoryFile.objects.exists())
        self.assertFalse(StudentReportedScore.objects.exists())

    def test_score_upload_fails_closed_when_storage_adapter_is_unavailable(self):
        response, upload_r2 = self._student_upload(r2_available=False)

        self.assertEqual(response.status_code, 503)
        self.assertIsNone(upload_r2)
        self.assertFalse(InventoryFile.objects.exists())
        self.assertFalse(StudentReportedScore.objects.exists())

    def test_multi_subject_items_cannot_override_shared_exam_metadata(self):
        score_items = json.dumps([
            {"subject": "수학", "score": 88, "max_score": 100},
            {
                "subject": "영어",
                "score": 92,
                "max_score": 100,
                "academic_year": 2025,
            },
        ])

        response, upload_r2 = self._student_upload(extra={"score_items": score_items})

        self.assertEqual(response.status_code, 400)
        upload_r2.assert_not_called()
        self.assertFalse(InventoryFile.objects.exists())
        self.assertFalse(StudentReportedScore.objects.exists())

    def test_group_review_only_processes_remaining_pending_subjects(self):
        score_items = json.dumps([
            {"subject": "수학", "score": 88, "max_score": 100},
            {"subject": "영어", "score": 92, "max_score": 100},
        ])
        response, _ = self._student_upload(extra={"score_items": score_items})
        submissions = json.loads(response.content)["scoreSubmissions"]

        first_review = self._review(submissions[0]["id"])
        self.assertEqual(first_review.status_code, 200, first_review.data)

        remaining_review = self._review(
            submissions[1]["id"],
            extra={"review_all_evidence": True},
        )
        self.assertEqual(remaining_review.status_code, 200, remaining_review.data)
        self.assertEqual(len(remaining_review.data["score_submissions"]), 1)
        self.assertEqual(
            StudentReportedScore.objects.filter(status="verified").count(),
            2,
        )

    def test_flexible_school_assessment_requires_name_and_date_and_preserves_label(self):
        missing_date, upload_r2 = self._student_upload(extra={
            "exam_round": "performance",
            "exam_name": "수학 주제탐구 수행평가",
            "exam_date": "",
        })
        self.assertEqual(missing_date.status_code, 400)
        upload_r2.assert_not_called()

        response, _ = self._student_upload(extra={
            "exam_round": "performance",
            "exam_name": "수학 주제탐구 수행평가",
            "exam_date": "2026-05-14",
        })
        self.assertEqual(response.status_code, 200, response.content)
        score = json.loads(response.content)["scoreSubmission"]
        self.assertEqual(score["exam_name"], "수학 주제탐구 수행평가")
        self.assertEqual(score["label"], "2026년 1학기 수학 주제탐구 수행평가")

    def test_same_named_performance_assessments_on_different_dates_both_accumulate(self):
        first, _ = self._student_upload(
            filename="performance-first.jpg",
            extra={
                "exam_round": "performance",
                "exam_name": "수학 주제탐구 수행평가",
                "exam_date": "2026-05-14",
            },
        )
        second, _ = self._student_upload(
            filename="performance-second.jpg",
            extra={
                "exam_round": "performance",
                "exam_name": "수학 주제탐구 수행평가",
                "exam_date": "2026-06-18",
            },
        )

        first_id = json.loads(first.content)["scoreSubmission"]["id"]
        second_id = json.loads(second.content)["scoreSubmission"]["id"]
        self.assertEqual(self._review(first_id).status_code, 200)
        self.assertEqual(self._review(second_id).status_code, 200)
        self.assertEqual(
            StudentReportedScore.objects.filter(
                status=StudentReportedScore.Status.VERIFIED,
                exam_round=StudentReportedScore.ExamRound.PERFORMANCE,
            ).count(),
            2,
        )

    def test_grade_rank_cannot_be_verified_without_staff_scale_confirmation(self):
        upload, _ = self._student_upload(extra={"grade_scale": "five", "grade_rank": "3"})
        score_id = json.loads(upload.content)["scoreSubmission"]["id"]

        blocked = self._review(score_id)
        self.assertEqual(blocked.status_code, 400)
        self.assertIn("등급 체계", blocked.data["detail"])
        self.assertEqual(StudentReportedScore.objects.get(id=score_id).status, "pending")

        verified = self._review(score_id, extra={"grade_scale_confirmed": True})
        self.assertEqual(verified.status_code, 200, verified.data)
        self.assertEqual(verified.data["status"], "verified")

        stale_reject = self._review(score_id, action="reject")
        self.assertEqual(stale_reject.status_code, 409)
        self.assertEqual(StudentReportedScore.objects.get(id=score_id).status, "verified")

    def test_verified_score_can_be_voided_then_evidence_deleted_without_losing_audit_row(self):
        marker = "[E2E-20260719183000-a1b2c3d4]"
        upload, _ = self._student_upload(extra={
            "subject": marker,
            "exam_round": "other",
            "exam_name": marker,
        })
        payload = json.loads(upload.content)
        score_id = payload["scoreSubmission"]["id"]
        file_id = int(payload["id"])
        self.assertEqual(self._review(score_id).status_code, 200)

        missing_reason = self._review(score_id, action="void")
        self.assertEqual(missing_reason.status_code, 400)
        self.assertEqual(StudentReportedScore.objects.get(id=score_id).status, "verified")

        voided = self._review(
            score_id,
            action="void",
            extra={"review_note": "다른 학생 성적표를 잘못 승인함"},
        )
        self.assertEqual(voided.status_code, 200, voided.data)
        self.assertEqual(voided.data["status"], "voided")
        self.assertEqual(self._console().data["students"][0]["source_summaries"]["school"]["scored_count"], 0)

        delete_request = self.factory.delete(
            f"/storage/inventory/files/{file_id}/?scope=student&student_ps={self.student.ps_number}"
        )
        delete_request.tenant = self.tenant
        original_key = InventoryFile.objects.get(id=file_id).r2_key
        with (
            patch(
                "apps.domains.inventory.views.JWTAuthentication.authenticate",
                return_value=(self.student_user, None),
            ),
            patch("apps.infrastructure.storage.r2.delete_object_r2_storage") as delete_r2,
            self.captureOnCommitCallbacks(execute=True),
        ):
            with self.assertRaisesMessage(RuntimeError, "outer rollback"):
                with transaction.atomic():
                    response = FileDeleteView.as_view()(delete_request, file_id=file_id)
                    self.assertEqual(response.status_code, 204)
                    raise RuntimeError("outer rollback")
        delete_r2.assert_not_called()
        self.assertTrue(InventoryFile.objects.filter(id=file_id).exists())
        self.assertEqual(StudentReportedScore.objects.get(id=score_id).evidence_file_id, file_id)
        self.assertFalse(SubmissionStorageCleanupIntent.objects.filter(object_key=original_key).exists())

        with (
            patch(
                "apps.domains.inventory.views.JWTAuthentication.authenticate",
                return_value=(self.student_user, None),
            ),
            patch("apps.infrastructure.storage.r2.delete_object_r2_storage", new=None),
            self.captureOnCommitCallbacks(execute=True),
        ):
            deleted = FileDeleteView.as_view()(delete_request, file_id=file_id)
            self.assertEqual(deleted.status_code, 204)  # Outer TestCase commit is still pending.
            self.assertEqual(SubmissionStorageCleanupIntent.objects.get(object_key=original_key).status, "pending")
        self.assertFalse(InventoryFile.objects.filter(id=file_id).exists())
        self.assertIsNone(StudentReportedScore.objects.get(id=score_id).evidence_file_id)
        intent = SubmissionStorageCleanupIntent.objects.get(object_key=original_key)
        self.assertEqual(intent.status, "failed")

        with patch(
            "apps.infrastructure.storage.r2.delete_object_r2_storage", side_effect=RuntimeError("provider unavailable"),
        ):
            process_submission_storage_cleanup_intents(intent_ids=[intent.id])
        intent.refresh_from_db()
        self.assertEqual(intent.status, "failed")
        self.assertEqual(intent.attempt_count, 2)
        self.assertEqual(intent.last_error, "storage_delete_failed")
        with patch("apps.infrastructure.storage.r2.delete_object_r2_storage") as delete_r2:
            process_submission_storage_cleanup_intents(intent_ids=[intent.id])
        delete_r2.assert_called_once_with(key=original_key)
        intent.refresh_from_db()
        self.assertEqual(intent.status, "cleaned")
        audit_row = StudentReportedScore.objects.get(id=score_id)
        self.assertEqual(audit_row.status, "voided")
        self.assertIsNone(audit_row.evidence_file_id)
        self.assertEqual(audit_row.review_note, "다른 학생 성적표를 잘못 승인함")

        with self.assertRaises(CommandError):
            call_command(
                "cleanup_reported_score_canary",
                tenant_code=self.tenant.code,
                marker=marker,
                confirm="wrong",
            )
        output = StringIO()
        call_command(
            "cleanup_reported_score_canary",
            tenant_code=self.tenant.code,
            marker=marker,
            confirm=marker,
            stdout=output,
        )
        self.assertIn("rows=1", output.getvalue())
        self.assertFalse(StudentReportedScore.objects.filter(id=score_id).exists())

    def test_canary_recovery_terminalizes_pending_rows_and_deletes_evidence(self):
        marker = "[E2E-20260719183100-b1c2d3e4]"
        upload, _ = self._student_upload(extra={
            "subject": marker,
            "exam_round": "other",
            "exam_name": marker,
        })
        payload = json.loads(upload.content)
        score_id = payload["scoreSubmission"]["id"]
        file_id = int(payload["id"])

        with patch(
            "apps.domains.inventory.services.delete_object_r2_storage"
        ) as delete_r2:
            call_command(
                "cleanup_reported_score_canary",
                tenant_code=self.tenant.code,
                marker=marker,
                confirm=marker,
                recover_active=True,
                wait_seconds=0,
            )

        delete_r2.assert_called_once()
        self.assertFalse(InventoryFile.objects.filter(id=file_id).exists())
        self.assertFalse(StudentReportedScore.objects.filter(id=score_id).exists())
