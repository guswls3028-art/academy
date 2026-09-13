from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.core.models import Tenant, TenantMembership
from apps.domains.attendance.models import Attendance
from apps.domains.attendance.views import AttendanceViewSet
from apps.domains.enrollment.models import Enrollment, SessionEnrollment
from apps.domains.exams.models import Exam, ExamEnrollment
from apps.domains.fees.models import FeeTemplate, StudentFee
from apps.domains.homework.models import HomeworkAssignment
from apps.domains.homework_results.models import Homework
from apps.domains.lectures.models import Lecture, Session
from apps.domains.students.models import Student


User = get_user_model()


class AttendanceDestroyRosterCleanupTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.tenant = Tenant.objects.create(name="Tenant", code="attdel", is_active=True)
        self.admin = User.objects.create_user(
            username="attdel_admin",
            password="test1234",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=self.admin, role="admin")

        self.lecture = Lecture.objects.create(
            tenant=self.tenant,
            title="Lecture",
            name="Lecture",
            subject="SCIENCE",
        )
        self.session = Session.objects.create(lecture=self.lecture, order=1, title="1주차")
        self.other_session = Session.objects.create(lecture=self.lecture, order=2, title="2주차")

        student_user = User.objects.create_user(
            username="attdel_student",
            password="test1234",
            tenant=self.tenant,
        )
        self.student = Student.objects.create(
            tenant=self.tenant,
            user=student_user,
            ps_number="ATTDEL001",
            omr_code="12345678",
            name="이준우",
            parent_phone="01000000000",
        )
        self.enrollment = Enrollment.objects.create(
            tenant=self.tenant,
            student=self.student,
            lecture=self.lecture,
            status="ACTIVE",
        )
        SessionEnrollment.objects.create(
            tenant=self.tenant,
            session=self.session,
            enrollment=self.enrollment,
        )
        SessionEnrollment.objects.create(
            tenant=self.tenant,
            session=self.other_session,
            enrollment=self.enrollment,
        )
        self.attendance = Attendance.objects.create(
            tenant=self.tenant,
            session=self.session,
            enrollment=self.enrollment,
            status="PRESENT",
        )
        Attendance.objects.create(
            tenant=self.tenant,
            session=self.other_session,
            enrollment=self.enrollment,
            status="PRESENT",
        )

        self.exam = Exam.objects.create(
            tenant=self.tenant,
            title="Current Exam",
            pass_score=60,
            max_score=100,
        )
        self.exam.sessions.add(self.session)
        self.other_exam = Exam.objects.create(
            tenant=self.tenant,
            title="Other Exam",
            pass_score=60,
            max_score=100,
        )
        self.other_exam.sessions.add(self.other_session)
        ExamEnrollment.objects.create(exam=self.exam, enrollment=self.enrollment)
        ExamEnrollment.objects.create(exam=self.other_exam, enrollment=self.enrollment)

        self.homework = Homework.objects.create(
            tenant=self.tenant,
            session=self.session,
            title="Current Homework",
        )
        self.other_homework = Homework.objects.create(
            tenant=self.tenant,
            session=self.other_session,
            title="Other Homework",
        )
        HomeworkAssignment.objects.create(
            tenant=self.tenant,
            homework=self.homework,
            session=self.session,
            enrollment=self.enrollment,
        )
        HomeworkAssignment.objects.create(
            tenant=self.tenant,
            homework=self.other_homework,
            session=self.other_session,
            enrollment=self.enrollment,
        )

    def test_destroy_removes_current_session_roster_and_score_targets_only(self):
        request = self.factory.delete(f"/api/v1/lectures/attendance/{self.attendance.id}/")
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)
        view = AttendanceViewSet.as_view({"delete": "destroy"})

        response = view(request, pk=self.attendance.id)

        self.assertEqual(response.status_code, 204)
        self.assertFalse(Attendance.objects.filter(id=self.attendance.id).exists())
        self.assertFalse(
            SessionEnrollment.objects.filter(
                tenant=self.tenant,
                session=self.session,
                enrollment=self.enrollment,
            ).exists()
        )
        self.assertFalse(
            ExamEnrollment.objects.filter(
                exam=self.exam,
                enrollment=self.enrollment,
            ).exists()
        )
        self.assertFalse(
            HomeworkAssignment.objects.filter(
                tenant=self.tenant,
                homework=self.homework,
                enrollment=self.enrollment,
            ).exists()
        )

        self.assertTrue(
            SessionEnrollment.objects.filter(
                tenant=self.tenant,
                session=self.other_session,
                enrollment=self.enrollment,
            ).exists()
        )
        self.assertTrue(
            ExamEnrollment.objects.filter(
                exam=self.other_exam,
                enrollment=self.enrollment,
            ).exists()
        )
        self.assertTrue(
            HomeworkAssignment.objects.filter(
                tenant=self.tenant,
                homework=self.other_homework,
                enrollment=self.enrollment,
            ).exists()
        )
        self.enrollment.refresh_from_db()
        self.assertEqual(self.enrollment.status, "ACTIVE")

    def test_destroy_keeps_exam_target_when_exam_is_still_assigned_by_other_session(self):
        shared_exam = Exam.objects.create(
            tenant=self.tenant,
            title="Shared Exam",
            pass_score=60,
            max_score=100,
        )
        shared_exam.sessions.add(self.session, self.other_session)
        shared_target = ExamEnrollment.objects.create(exam=shared_exam, enrollment=self.enrollment)
        request = self.factory.delete(f"/api/v1/lectures/attendance/{self.attendance.id}/")
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)
        view = AttendanceViewSet.as_view({"delete": "destroy"})

        response = view(request, pk=self.attendance.id)

        self.assertEqual(response.status_code, 204)
        self.assertTrue(
            ExamEnrollment.objects.filter(id=shared_target.id).exists()
        )

    def test_secession_rejects_string_false_confirmation(self):
        request = self.factory.patch(
            f"/api/v1/lectures/attendance/{self.attendance.id}/",
            {"status": "SECESSION", "confirm_secession": "false"},
            format="json",
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)
        view = AttendanceViewSet.as_view({"patch": "partial_update"})

        response = view(request, pk=self.attendance.id)

        self.assertEqual(response.status_code, 400, response.data)
        self.enrollment.refresh_from_db()
        self.attendance.refresh_from_db()
        self.assertEqual(self.enrollment.status, "ACTIVE")
        self.assertEqual(self.attendance.status, "PRESENT")

    def test_session_secession_preserves_other_session_and_enrollment(self):
        self.attendance.memo = "Keep teacher record"
        self.attendance.save(update_fields=["memo"])
        for _ in range(2):
            request = self.factory.patch(
                f"/api/v1/lectures/attendance/{self.attendance.id}/",
                {"status": "SECESSION", "confirm_secession": True, "secession_scope": "session"},
                format="json",
            )
            request.tenant = self.tenant
            force_authenticate(request, user=self.admin)
            response = AttendanceViewSet.as_view({"patch": "partial_update"})(request, pk=self.attendance.id)
            self.assertEqual(response.status_code, 200, response.data)
        self.enrollment.refresh_from_db()
        self.attendance.refresh_from_db()
        self.assertEqual(self.enrollment.status, "ACTIVE")
        self.assertEqual(self.attendance.status, "SECESSION")
        self.assertEqual(self.attendance.memo, "Keep teacher record")
        self.assertFalse(SessionEnrollment.objects.filter(session=self.session, enrollment=self.enrollment).exists())
        self.assertTrue(SessionEnrollment.objects.filter(session=self.other_session, enrollment=self.enrollment).exists())
        self.assertEqual(Attendance.objects.get(session=self.other_session, enrollment=self.enrollment).status, "PRESENT")
        self.assertFalse(ExamEnrollment.objects.filter(exam=self.exam, enrollment=self.enrollment).exists())
        self.assertTrue(ExamEnrollment.objects.filter(exam=self.other_exam, enrollment=self.enrollment).exists())
        self.assertFalse(HomeworkAssignment.objects.filter(session=self.session, enrollment=self.enrollment).exists())
        self.assertTrue(HomeworkAssignment.objects.filter(session=self.other_session, enrollment=self.enrollment).exists())

    def test_secession_rejects_unknown_scope_without_mutation(self):
        request = self.factory.patch(
            f"/api/v1/lectures/attendance/{self.attendance.id}/",
            {"status": "SECESSION", "confirm_secession": True, "secession_scope": "all"},
            format="json",
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)
        response = AttendanceViewSet.as_view({"patch": "partial_update"})(request, pk=self.attendance.id)
        self.assertEqual(response.status_code, 400)
        self.enrollment.refresh_from_db()
        self.attendance.refresh_from_db()
        self.assertEqual(self.enrollment.status, "ACTIVE")
        self.assertEqual(self.attendance.status, "PRESENT")

    def test_secession_deactivates_enrollment_and_removes_all_targets(self):
        request = self.factory.patch(
            f"/api/v1/lectures/attendance/{self.attendance.id}/",
            {"status": "SECESSION", "confirm_secession": True},
            format="json",
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)
        view = AttendanceViewSet.as_view({"patch": "partial_update"})

        response = view(request, pk=self.attendance.id)

        self.assertEqual(response.status_code, 200, response.data)
        self.enrollment.refresh_from_db()
        self.assertEqual(self.enrollment.status, "INACTIVE")
        self.assertFalse(
            Attendance.objects
            .filter(tenant=self.tenant, enrollment=self.enrollment)
            .exclude(status="SECESSION")
            .exists()
        )
        self.assertFalse(
            ExamEnrollment.objects.filter(
                enrollment=self.enrollment,
                enrollment__tenant=self.tenant,
            ).exists()
        )
        self.assertFalse(
            HomeworkAssignment.objects.filter(
                tenant=self.tenant,
                enrollment=self.enrollment,
            ).exists()
        )

    def test_secession_deactivates_auto_assigned_student_fee(self):
        fee_template = FeeTemplate.objects.create(
            tenant=self.tenant,
            name="월 수강료",
            fee_type=FeeTemplate.FeeType.TUITION,
            billing_cycle=FeeTemplate.BillingCycle.MONTHLY,
            amount=100_000,
            lecture=self.lecture,
            auto_assign=True,
        )
        student_fee = StudentFee.objects.create(
            tenant=self.tenant,
            student=self.student,
            fee_template=fee_template,
            enrollment=self.enrollment,
            is_active=True,
        )

        request = self.factory.patch(
            f"/api/v1/lectures/attendance/{self.attendance.id}/",
            {"status": "SECESSION", "confirm_secession": True},
            format="json",
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)
        view = AttendanceViewSet.as_view({"patch": "partial_update"})

        response = view(request, pk=self.attendance.id)

        self.assertEqual(response.status_code, 200, response.data)
        self.enrollment.refresh_from_db()
        student_fee.refresh_from_db()
        self.assertEqual(self.enrollment.status, "INACTIVE")
        self.assertFalse(student_fee.is_active)
        self.assertEqual(student_fee.billing_end_month, timezone.localdate().strftime("%Y-%m"))

    def test_secession_preserves_manual_student_fee(self):
        fee_template = FeeTemplate.objects.create(
            tenant=self.tenant,
            name="교재비",
            fee_type=FeeTemplate.FeeType.MATERIAL,
            billing_cycle=FeeTemplate.BillingCycle.ONE_TIME,
            amount=20_000,
            lecture=self.lecture,
            auto_assign=False,
        )
        student_fee = StudentFee.objects.create(
            tenant=self.tenant,
            student=self.student,
            fee_template=fee_template,
            enrollment=self.enrollment,
            is_active=True,
        )

        request = self.factory.patch(
            f"/api/v1/lectures/attendance/{self.attendance.id}/",
            {"status": "SECESSION", "confirm_secession": True},
            format="json",
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)
        view = AttendanceViewSet.as_view({"patch": "partial_update"})

        response = view(request, pk=self.attendance.id)

        self.assertEqual(response.status_code, 200, response.data)
        student_fee.refresh_from_db()
        self.assertTrue(student_fee.is_active)
        self.assertEqual(student_fee.billing_end_month, "")

    def test_bulk_create_reactivates_auto_assigned_fee_for_active_enrollment(self):
        fee_template = FeeTemplate.objects.create(
            tenant=self.tenant,
            name="월 수강료",
            fee_type=FeeTemplate.FeeType.TUITION,
            billing_cycle=FeeTemplate.BillingCycle.MONTHLY,
            amount=100_000,
            lecture=self.lecture,
            auto_assign=True,
        )
        student_fee = StudentFee.objects.create(
            tenant=self.tenant,
            student=self.student,
            fee_template=fee_template,
            enrollment=self.enrollment,
            is_active=False,
            billing_end_month="2026-05",
        )

        request = self.factory.post(
            "/api/v1/lectures/attendance/bulk_create/",
            {"session": self.session.id, "students": [self.student.id]},
            format="json",
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)
        view = AttendanceViewSet.as_view({"post": "bulk_create"})

        response = view(request)

        self.assertEqual(response.status_code, 201)
        self.enrollment.refresh_from_db()
        student_fee.refresh_from_db()
        self.assertEqual(self.enrollment.status, "ACTIVE")
        self.assertTrue(student_fee.is_active)
        self.assertEqual(student_fee.billing_end_month, "")
