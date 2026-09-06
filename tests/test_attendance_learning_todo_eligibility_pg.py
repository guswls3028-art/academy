"""Cross-domain attendance learning-todo integration regressions."""

from __future__ import annotations

import threading
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate

from apps.core.models import PlatformPushOutbox, Tenant, TenantMembership
from apps.domains.attendance.models import Attendance
from apps.domains.attendance.views import AttendanceViewSet
from apps.domains.enrollment.models import Enrollment, SessionEnrollment
from apps.domains.exams.models import Exam, ExamEnrollment
from apps.domains.homework.models import HomeworkAssignment
from apps.domains.homework.views.homework_assignment_view import (
    HomeworkAssignmentManageView,
)
from apps.domains.homework_results.models import Homework, HomeworkScore
from apps.domains.lectures.models import Lecture, Session
from apps.domains.progress.models import ClinicLink, SessionProgress
from apps.domains.progress.services.clinic_trigger_service import ClinicTriggerService
from apps.domains.results.models import ExamAttempt, Result
from apps.domains.results.services.clinic_target_service import ClinicTargetService
from apps.domains.results.views.session_scores_view import SessionScoresView
from apps.domains.student_app.exams.views import StudentExamListView
from apps.domains.students.models import Student
from apps.domains.submissions.models import Submission
from apps.support.homework_results.score_dependencies import sync_homework_clinic_link
from apps.support.attendance.learning_todo_eligibility import (
    learning_todo_eligible_pairs,
)


User = get_user_model()


class LearningTodoEligibilityPostgresTests(TransactionTestCase):
    """Attendance learning-todo matrix at its API and persisted boundaries."""

    def setUp(self):
        self.factory = APIRequestFactory()
        self.client = APIClient()
        self.tenant = Tenant.objects.create(
            name="Learning todo tenant",
            code="learning-todo",
            is_active=True,
        )
        self.other_tenant = Tenant.objects.create(
            name="Other learning todo tenant",
            code="other-learning-todo",
            is_active=True,
        )
        self.admin = User.objects.create_user(
            username="learning_todo_admin",
            password="test1234",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=self.admin,
            role="admin",
        )
        self.client.force_authenticate(self.admin)
        self.headers = {
            "HTTP_HOST": "localhost",
            "HTTP_X_TENANT_CODE": self.tenant.code,
        }

        self.lecture = Lecture.objects.create(
            tenant=self.tenant,
            title="Learning todo lecture",
            name="Learning todo lecture",
            subject="MATH",
        )
        self.session = Session.objects.create(
            lecture=self.lecture,
            order=1,
            title="1주차",
        )
        self.enrollments: dict[str, Enrollment] = {}
        self.attendances: dict[str, Attendance] = {}
        for index, status in enumerate(("PRESENT", "ONLINE", "ABSENT"), start=1):
            enrollment = self._enrollment(
                tenant=self.tenant,
                lecture=self.lecture,
                session=self.session,
                suffix=f"{status}{index}",
            )
            self.enrollments[status] = enrollment
            self.attendances[status] = Attendance.objects.create(
                tenant=self.tenant,
                session=self.session,
                enrollment=enrollment,
                status=status,
            )

    def _enrollment(self, *, tenant, lecture, session, suffix: str) -> Enrollment:
        user = User.objects.create_user(
            username=f"learning_todo_{tenant.id}_{suffix}",
            password="test1234",
            tenant=tenant,
        )
        TenantMembership.ensure_active(tenant=tenant, user=user, role="student")
        student = Student.objects.create(
            tenant=tenant,
            user=user,
            ps_number=f"LT{tenant.id}{suffix}",
            omr_code=f"{tenant.id}{suffix}"[-8:],
            name=f"Student {suffix}",
            parent_phone="01000000000",
        )
        enrollment = Enrollment.objects.create(
            tenant=tenant,
            student=student,
            lecture=lecture,
            status="ACTIVE",
        )
        SessionEnrollment.objects.create(
            tenant=tenant,
            session=session,
            enrollment=enrollment,
        )
        return enrollment

    def _patch_attendance(self, status: str, *, current_status: str = "ABSENT"):
        attendance = self.attendances[current_status]
        request = self.factory.patch(
            f"/api/v1/lectures/attendance/{attendance.id}/",
            {"status": status},
            format="json",
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)
        return AttendanceViewSet.as_view({"patch": "partial_update"})(
            request,
            pk=attendance.id,
        )

    def _session_scores(self):
        request = self.factory.get(
            f"/api/v1/results/admin/sessions/{self.session.id}/scores/"
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)
        return SessionScoresView.as_view()(request, session_id=self.session.id)

    def _student_exam_list(self, status: str):
        enrollment = self.enrollments[status]
        request = self.factory.get("/api/v1/student/exams/")
        request.tenant = self.tenant
        force_authenticate(request, user=enrollment.student.user)
        return StudentExamListView.as_view()(request)

    def _unrelated_write_counts(self) -> dict[str, int]:
        from apps.domains.messaging.models import NotificationLog, ScheduledNotification

        return {
            "notification_logs": NotificationLog.objects.count(),
            "scheduled_notifications": ScheduledNotification.objects.count(),
            "platform_push_outbox": PlatformPushOutbox.objects.count(),
        }

    def test_exam_and_homework_assignment_apis_include_online_and_exclude_absent(self):
        other_lecture = Lecture.objects.create(
            tenant=self.other_tenant,
            title="Other tenant lecture",
            name="Other tenant lecture",
            subject="MATH",
        )
        other_session = Session.objects.create(
            lecture=other_lecture,
            order=1,
            title="Other tenant session",
        )
        other_enrollment = self._enrollment(
            tenant=self.other_tenant,
            lecture=other_lecture,
            session=other_session,
            suffix="OTHER",
        )
        Attendance.objects.create(
            tenant=self.other_tenant,
            session=other_session,
            enrollment=other_enrollment,
            status="ONLINE",
        )

        created = self.client.post(
            "/api/v1/exams/",
            {
                "title": "Attendance matrix exam",
                "exam_type": Exam.ExamType.REGULAR,
                "session_id": self.session.id,
                "pass_score": 60,
                "max_score": 100,
            },
            format="json",
            **self.headers,
        )
        self.assertEqual(created.status_code, 201, created.data)
        exam = Exam.objects.get(id=created.data["id"])
        expected_ids = {
            self.enrollments["PRESENT"].id,
            self.enrollments["ONLINE"].id,
        }
        self.assertEqual(
            set(
                ExamEnrollment.objects.filter(exam=exam).values_list(
                    "enrollment_id",
                    flat=True,
                )
            ),
            expected_ids,
        )
        self.assertEqual(
            learning_todo_eligible_pairs(
                tenant=self.tenant,
                enrollment_session_pairs={
                    (self.enrollments["ONLINE"].id, self.session.id),
                    (other_enrollment.id, other_session.id),
                },
            ),
            {(self.enrollments["ONLINE"].id, self.session.id)},
        )

        homework = Homework.objects.create(
            tenant=self.tenant,
            session=self.session,
            title="Attendance matrix homework",
        )
        list_request = self.factory.get(
            f"/api/v1/homework/assignments/?homework_id={homework.id}"
        )
        list_request.tenant = self.tenant
        force_authenticate(list_request, user=self.admin)
        listed = HomeworkAssignmentManageView.as_view()(list_request)
        self.assertEqual(listed.status_code, 200, listed.data)
        self.assertEqual(
            {item["enrollment_id"] for item in listed.data["items"]},
            expected_ids,
        )

        rejected_request = self.factory.put(
            f"/api/v1/homework/assignments/?homework_id={homework.id}",
            {
                "enrollment_ids": [
                    *sorted(expected_ids),
                    self.enrollments["ABSENT"].id,
                ]
            },
            format="json",
        )
        rejected_request.tenant = self.tenant
        force_authenticate(rejected_request, user=self.admin)
        rejected = HomeworkAssignmentManageView.as_view()(rejected_request)
        self.assertEqual(rejected.status_code, 400, rejected.data)

        update_request = self.factory.put(
            f"/api/v1/homework/assignments/?homework_id={homework.id}",
            {"enrollment_ids": sorted(expected_ids)},
            format="json",
        )
        update_request.tenant = self.tenant
        force_authenticate(update_request, user=self.admin)
        updated = HomeworkAssignmentManageView.as_view()(update_request)
        self.assertEqual(updated.status_code, 200, updated.data)
        self.assertEqual(updated.data["selected_count"], 2)
        self.assertEqual(
            set(
                HomeworkAssignment.objects.filter(homework=homework).values_list(
                    "enrollment_id",
                    flat=True,
                )
            ),
            expected_ids,
        )

    def test_absent_to_online_restores_existing_todos_once_and_projection_reloads(self):
        absent = self.enrollments["ABSENT"]
        exam = Exam.objects.create(
            tenant=self.tenant,
            title="Restore exam",
            exam_type=Exam.ExamType.REGULAR,
            pass_score=60,
            max_score=100,
        )
        exam.sessions.add(self.session)
        homework = Homework.objects.create(
            tenant=self.tenant,
            session=self.session,
            title="Restore homework",
        )
        writes_before = self._unrelated_write_counts()

        first = self._patch_attendance("ONLINE")
        second = self._patch_attendance("ONLINE")

        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(
            ExamEnrollment.objects.filter(exam=exam, enrollment=absent).count(),
            1,
        )
        self.assertEqual(
            HomeworkAssignment.objects.filter(
                tenant=self.tenant,
                homework=homework,
                session=self.session,
                enrollment=absent,
            ).count(),
            1,
        )

        excluded = self._patch_attendance("ABSENT")
        self.assertEqual(excluded.status_code, 200, excluded.data)
        response = self._session_scores()
        self.assertEqual(response.status_code, 200, response.data)
        row = next(
            item for item in response.data["rows"]
            if item["enrollment_id"] == absent.id
        )
        self.assertEqual(row["attendance_status"], "ABSENT")
        self.assertFalse(row["assessment_todo_eligible"])
        self.assertEqual(row["exams"], [])
        self.assertEqual(row["homeworks"], [])

        restored = self._patch_attendance("ONLINE")
        self.assertEqual(restored.status_code, 200, restored.data)
        response = self._session_scores()
        row = next(
            item for item in response.data["rows"]
            if item["enrollment_id"] == absent.id
        )
        self.assertEqual(row["attendance_status"], "ONLINE")
        self.assertTrue(row["assessment_todo_eligible"])
        self.assertEqual([item["exam_id"] for item in row["exams"]], [exam.id])
        self.assertEqual(
            [item["homework_id"] for item in row["homeworks"]],
            [homework.id],
        )
        self.assertEqual(self._unrelated_write_counts(), writes_before)

    def test_bulk_present_materializes_todos_and_undo_restores_absent_projection(self):
        absent = self.enrollments["ABSENT"]
        exam = Exam.objects.create(
            tenant=self.tenant,
            title="Bulk present exam",
            exam_type=Exam.ExamType.REGULAR,
            pass_score=60,
            max_score=100,
        )
        exam.sessions.add(self.session)
        homework = Homework.objects.create(
            tenant=self.tenant,
            session=self.session,
            title="Bulk present homework",
        )
        writes_before = self._unrelated_write_counts()

        request = self.factory.post(
            "/api/v1/lectures/attendance/bulk_set_present/",
            {"session": self.session.id},
            format="json",
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)
        changed = AttendanceViewSet.as_view({"post": "bulk_set_present"})(request)

        self.assertEqual(changed.status_code, 200, changed.data)
        self.assertEqual(
            ExamEnrollment.objects.filter(exam=exam, enrollment=absent).count(),
            1,
        )
        self.assertEqual(
            HomeworkAssignment.objects.filter(
                tenant=self.tenant,
                homework=homework,
                session=self.session,
                enrollment=absent,
            ).count(),
            1,
        )

        undo_request = self.factory.post(
            "/api/v1/lectures/attendance/bulk_undo_present/",
            {"undo_token": changed.data["undo_token"]},
            format="json",
        )
        undo_request.tenant = self.tenant
        force_authenticate(undo_request, user=self.admin)
        undone = AttendanceViewSet.as_view({"post": "bulk_undo_present"})(
            undo_request
        )

        self.assertEqual(undone.status_code, 200, undone.data)
        self.assertEqual(
            Attendance.objects.get(id=self.attendances["ABSENT"].id).status,
            "ABSENT",
        )
        scores = self._session_scores()
        row = next(
            item
            for item in scores.data["rows"]
            if int(item["enrollment_id"]) == absent.id
        )
        self.assertFalse(row["assessment_todo_eligible"])
        self.assertEqual(row["exams"], [])
        self.assertEqual(row["homeworks"], [])
        self.assertEqual(self._unrelated_write_counts(), writes_before)

    def test_clinic_trigger_materializes_online_missing_exam_but_not_actual_absent(self):
        exam = Exam.objects.create(
            tenant=self.tenant,
            title="Missing exam",
            exam_type=Exam.ExamType.REGULAR,
            pass_score=60,
            max_score=100,
        )
        exam.sessions.add(self.session)
        for status in ("ONLINE", "ABSENT"):
            enrollment = self.enrollments[status]
            ExamEnrollment.objects.create(exam=exam, enrollment=enrollment)
            progress = SessionProgress.objects.create(
                enrollment=enrollment,
                session=self.session,
                completed=False,
                exam_meta={
                    "exams": [
                        {
                            "exam_id": exam.id,
                            "no_result": True,
                            "score": None,
                            "passed": False,
                            "pass_score": 60,
                        }
                    ]
                },
            )
            ClinicTriggerService.auto_create_per_exam(progress)

        self.assertTrue(
            ClinicLink.objects.filter(
                tenant=self.tenant,
                enrollment=self.enrollments["ONLINE"],
                session=self.session,
                source_type="exam",
                source_id=exam.id,
                resolved_at__isnull=True,
            ).exists()
        )
        self.assertFalse(
            ClinicLink.objects.filter(
                tenant=self.tenant,
                enrollment=self.enrollments["ABSENT"],
                session=self.session,
                source_type="exam",
                source_id=exam.id,
                resolved_at__isnull=True,
            ).exists()
        )

    def test_clinic_projection_keeps_online_missing_and_failed_work_excludes_absent(self):
        exam = Exam.objects.create(
            tenant=self.tenant,
            title="Explicit missing exam",
            exam_type=Exam.ExamType.REGULAR,
            pass_score=60,
            max_score=100,
        )
        exam.sessions.add(self.session)
        homework = Homework.objects.create(
            tenant=self.tenant,
            session=self.session,
            title="Failed homework",
        )
        for status in ("ONLINE", "ABSENT"):
            enrollment = self.enrollments[status]
            ExamEnrollment.objects.create(exam=exam, enrollment=enrollment)
            attempt = ExamAttempt.objects.create(
                exam=exam,
                enrollment=enrollment,
                attempt_index=1,
                status="done",
                meta={"status": "NOT_SUBMITTED"},
            )
            Result.objects.create(
                target_type="exam",
                target_id=exam.id,
                enrollment=enrollment,
                attempt=attempt,
                total_score=0,
                max_score=100,
            )
            HomeworkAssignment.objects.create(
                tenant=self.tenant,
                homework=homework,
                session=self.session,
                enrollment=enrollment,
            )
            sync_homework_clinic_link(
                enrollment_id=enrollment.id,
                session=self.session,
                homework_id=homework.id,
                passed=False,
                score=20,
                max_score=100,
            )

        targets = ClinicTargetService.list_admin_targets(tenant=self.tenant)
        explicit_exam_enrollment_ids = {
            int(row["enrollment_id"])
            for row in targets
            if row.get("source_type") == "exam"
            and int(row.get("source_id") or 0) == exam.id
        }
        failed_homework_enrollment_ids = {
            int(row["enrollment_id"])
            for row in targets
            if row.get("source_type") == "homework"
            and int(row.get("source_id") or 0) == homework.id
        }
        self.assertEqual(
            explicit_exam_enrollment_ids,
            {self.enrollments["ONLINE"].id},
        )
        self.assertEqual(
            failed_homework_enrollment_ids,
            {self.enrollments["ONLINE"].id},
        )

    def test_absent_transition_preserves_authored_assessment_history(self):
        enrollment = self.enrollments["ONLINE"]
        exam = Exam.objects.create(
            tenant=self.tenant,
            title="Historical exam",
            exam_type=Exam.ExamType.REGULAR,
            pass_score=60,
            max_score=100,
        )
        exam.sessions.add(self.session)
        ExamEnrollment.objects.create(exam=exam, enrollment=enrollment)
        submission = Submission.objects.create(
            tenant=self.tenant,
            user=enrollment.student.user,
            enrollment=enrollment,
            target_type=Submission.TargetType.EXAM,
            target_id=exam.id,
            source=Submission.Source.ONLINE,
            status=Submission.Status.DONE,
        )
        attempt = ExamAttempt.objects.create(
            exam=exam,
            enrollment=enrollment,
            submission_id=submission.id,
            attempt_index=1,
            status="done",
        )
        result = Result.objects.create(
            target_type="exam",
            target_id=exam.id,
            enrollment=enrollment,
            attempt=attempt,
            total_score=75,
            max_score=100,
        )
        homework = Homework.objects.create(
            tenant=self.tenant,
            session=self.session,
            title="Historical homework",
        )
        assignment = HomeworkAssignment.objects.create(
            tenant=self.tenant,
            homework=homework,
            session=self.session,
            enrollment=enrollment,
        )
        homework_score = HomeworkScore.objects.create(
            homework=homework,
            session=self.session,
            enrollment=enrollment,
            attempt_index=1,
            score=70,
            max_score=100,
        )
        link = ClinicLink.objects.create(
            tenant=self.tenant,
            enrollment=enrollment,
            session=self.session,
            source_type="homework",
            source_id=homework.id,
            reason=ClinicLink.Reason.AUTO_FAILED,
            is_auto=True,
            cycle_no=1,
        )
        writes_before = self._unrelated_write_counts()

        changed = self._patch_attendance("ABSENT", current_status="ONLINE")

        self.assertEqual(changed.status_code, 200, changed.data)
        self.assertTrue(ExamEnrollment.objects.filter(exam=exam, enrollment=enrollment).exists())
        self.assertTrue(Submission.objects.filter(id=submission.id).exists())
        self.assertTrue(ExamAttempt.objects.filter(id=attempt.id).exists())
        self.assertTrue(Result.objects.filter(id=result.id, total_score=75).exists())
        self.assertTrue(HomeworkAssignment.objects.filter(id=assignment.id).exists())
        self.assertTrue(HomeworkScore.objects.filter(id=homework_score.id, score=70).exists())
        self.assertTrue(ClinicLink.objects.filter(id=link.id, resolved_at__isnull=True).exists())
        scores = self._session_scores()
        row = next(
            item
            for item in scores.data["rows"]
            if int(item["enrollment_id"]) == enrollment.id
        )
        self.assertFalse(row["assessment_todo_eligible"])
        self.assertFalse(row["clinic_required"])
        self.assertEqual([item["exam_id"] for item in row["exams"]], [exam.id])
        self.assertEqual(
            [item["homework_id"] for item in row["homeworks"]],
            [homework.id],
        )
        link.resolved_at = timezone.now()
        link.save(update_fields=["resolved_at"])
        historical_targets = ClinicTargetService.list_admin_targets(
            tenant=self.tenant,
            include_resolved=True,
        )
        self.assertIn(
            link.id,
            {
                int(item["clinic_link_id"])
                for item in historical_targets
                if item.get("clinic_link_id") is not None
            },
        )
        self.assertEqual(self._unrelated_write_counts(), writes_before)

    def test_student_exam_todo_excludes_absent_but_preserves_history_without_retest(self):
        exam = Exam.objects.create(
            tenant=self.tenant,
            title="Student todo exam",
            exam_type=Exam.ExamType.REGULAR,
            pass_score=60,
            max_score=100,
        )
        exam.sessions.add(self.session)
        for status in ("ONLINE", "ABSENT"):
            ExamEnrollment.objects.create(
                exam=exam,
                enrollment=self.enrollments[status],
            )

        online = self._student_exam_list("ONLINE")
        absent = self._student_exam_list("ABSENT")
        self.assertEqual([item["id"] for item in online.data["items"]], [exam.id])
        self.assertEqual(absent.data["items"], [])

        absent_enrollment = self.enrollments["ABSENT"]
        attempt = ExamAttempt.objects.create(
            exam=exam,
            enrollment=absent_enrollment,
            attempt_index=1,
            status="done",
        )
        Result.objects.create(
            target_type="exam",
            target_id=exam.id,
            enrollment=absent_enrollment,
            attempt=attempt,
            total_score=80,
            max_score=100,
        )
        historical = self._student_exam_list("ABSENT")
        self.assertEqual(
            [item["id"] for item in historical.data["items"]],
            [exam.id],
        )

        from apps.support.student_app.exam_dependencies import (
            get_enrollment_for_student_exam,
        )

        enrollment, tenant = get_enrollment_for_student_exam(
            absent_enrollment.student,
            exam.id,
            tenant=self.tenant,
        )
        self.assertIsNone(enrollment)
        self.assertIsNone(tenant)

    def test_shared_exam_remains_actionable_when_another_linked_session_is_online(self):
        enrollment = self.enrollments["ABSENT"]
        online_session = Session.objects.create(
            lecture=self.lecture,
            order=2,
            title="2주차",
        )
        SessionEnrollment.objects.create(
            tenant=self.tenant,
            session=online_session,
            enrollment=enrollment,
        )
        Attendance.objects.create(
            tenant=self.tenant,
            session=online_session,
            enrollment=enrollment,
            status="ONLINE",
        )
        exam = Exam.objects.create(
            tenant=self.tenant,
            title="Shared session exam",
            exam_type=Exam.ExamType.REGULAR,
            pass_score=60,
            max_score=100,
        )
        exam.sessions.add(self.session, online_session)
        ExamEnrollment.objects.create(exam=exam, enrollment=enrollment)

        student_list = self._student_exam_list("ABSENT")
        self.assertEqual([item["id"] for item in student_list.data["items"]], [exam.id])
        first_session_scores = self._session_scores()
        first_session_row = next(
            item
            for item in first_session_scores.data["rows"]
            if int(item["enrollment_id"]) == enrollment.id
        )
        self.assertFalse(first_session_row["assessment_todo_eligible"])
        self.assertEqual(first_session_row["exams"], [])

        from apps.support.student_app.exam_dependencies import (
            get_enrollment_for_student_exam,
        )

        resolved_enrollment, tenant = get_enrollment_for_student_exam(
            enrollment.student,
            exam.id,
            tenant=self.tenant,
        )
        self.assertEqual(resolved_enrollment, enrollment)
        self.assertEqual(tenant, self.tenant)

    @skipUnless(connection.vendor == "postgresql", "PostgreSQL row-lock contract")
    def test_concurrent_status_patches_leave_projection_at_last_committed_state(self):
        enrollment = self.enrollments["ABSENT"]
        attendance_id = self.attendances["ABSENT"].id
        exam = Exam.objects.create(
            tenant=self.tenant,
            title="Concurrent exam",
            exam_type=Exam.ExamType.REGULAR,
            pass_score=60,
            max_score=100,
        )
        exam.sessions.add(self.session)
        homework = Homework.objects.create(
            tenant=self.tenant,
            session=self.session,
            title="Concurrent homework",
        )
        ExamEnrollment.objects.create(exam=exam, enrollment=enrollment)
        HomeworkAssignment.objects.create(
            tenant=self.tenant,
            homework=homework,
            session=self.session,
            enrollment=enrollment,
        )

        barrier = threading.Barrier(2)
        outcomes: list[object] = []
        outcomes_lock = threading.Lock()

        def patch_status(next_status: str) -> None:
            close_old_connections()
            try:
                request = APIRequestFactory().patch(
                    f"/api/v1/lectures/attendance/{attendance_id}/",
                    {"status": next_status},
                    format="json",
                )
                request.tenant = Tenant.objects.get(id=self.tenant.id)
                force_authenticate(request, user=User.objects.get(id=self.admin.id))
                barrier.wait(timeout=10)
                response = AttendanceViewSet.as_view({"patch": "partial_update"})(
                    request,
                    pk=attendance_id,
                )
                outcome: object = int(response.status_code)
            except BaseException as exc:  # pragma: no cover - assertion below reports it
                outcome = exc
            finally:
                close_old_connections()
            with outcomes_lock:
                outcomes.append(outcome)

        threads = [
            threading.Thread(target=patch_status, args=(status,), daemon=True)
            for status in ("ONLINE", "ABSENT")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertEqual(outcomes, [200, 200])
        attendance = Attendance.objects.get(id=attendance_id)
        scores = self._session_scores()
        row = next(
            item
            for item in scores.data["rows"]
            if int(item["enrollment_id"]) == enrollment.id
        )
        expected_eligible = attendance.status == "ONLINE"
        self.assertEqual(row["assessment_todo_eligible"], expected_eligible)
        self.assertEqual(bool(row["exams"]), expected_eligible)
        self.assertEqual(bool(row["homeworks"]), expected_eligible)
        current_targets = ClinicTargetService.list_admin_targets(tenant=self.tenant)
        self.assertEqual(
            any(
                int(item["enrollment_id"]) == enrollment.id
                and int(item["session_id"]) == self.session.id
                for item in current_targets
            ),
            expected_eligible,
        )
        self.assertEqual(
            ExamEnrollment.objects.filter(exam=exam, enrollment=enrollment).count(),
            1,
        )
        self.assertEqual(
            HomeworkAssignment.objects.filter(
                tenant=self.tenant,
                homework=homework,
                session=self.session,
                enrollment=enrollment,
            ).count(),
            1,
        )
