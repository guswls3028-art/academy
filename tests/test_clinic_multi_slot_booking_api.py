import datetime
import importlib
import threading
from unittest.mock import patch

from django.apps import apps as django_apps
from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase
from rest_framework.test import APITestCase

from apps.core.models import Tenant
from apps.domains.clinic.models import Session, SessionParticipant
from apps.domains.clinic.services import create_participant
from apps.domains.clinic.tests import ClinicAPITestMixin
from apps.domains.students.models import Student


class ClinicMultiSlotBookingAPITest(APITestCase, ClinicAPITestMixin):
    def setUp(self):
        self.data = self.setup_api_tenant("clinic_multi_slot", student_count=2)
        self.tenant = self.data["tenant"]
        self.student = self.data["students"][0]
        self.tomorrow = datetime.date.today() + datetime.timedelta(days=1)
        self.first_session = self.make_clinic_session(
            self.tenant,
            date=self.tomorrow,
            start_time=datetime.time(17, 0),
            location="클리닉 1실",
            max_participants=10,
        )
        self.second_session = self.make_clinic_session(
            self.tenant,
            date=self.tomorrow,
            start_time=datetime.time(18, 0),
            location="클리닉 1실",
            max_participants=10,
        )

    def _headers(self):
        return super()._headers(self.tenant)

    @staticmethod
    def _allow_multiple(*sessions):
        Session.objects.filter(id__in=[session.id for session in sessions]).update(
            allow_multi_slot_booking=True
        )
        for session in sessions:
            session.allow_multi_slot_booking = True

    def test_student_books_two_time_slots_atomically(self):
        self._allow_multiple(self.first_session, self.second_session)
        self.client.force_authenticate(user=self.student.user)

        response = self.client.post(
            "/api/v1/clinic/participants/bulk-create/",
            {
                "session_ids": [self.first_session.id, self.second_session.id],
                "student_request_memo": "17시부터 19시까지 참여",
            },
            format="json",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["count"], 2)
        self.assertEqual(
            [participant["session"] for participant in response.data["participants"]],
            [self.first_session.id, self.second_session.id],
        )
        participants = SessionParticipant.objects.filter(
            tenant=self.tenant,
            student=self.student,
            session_id__in=[self.first_session.id, self.second_session.id],
        ).order_by("session__start_time")
        self.assertEqual(participants.count(), 2)
        self.assertEqual(
            {participant.status for participant in participants},
            {SessionParticipant.Status.PENDING},
        )
        self.assertEqual(
            {participant.source for participant in participants},
            {SessionParticipant.Source.STUDENT_REQUEST},
        )
        self.assertEqual(
            {participant.student_request_memo for participant in participants},
            {"17시부터 19시까지 참여"},
        )

    def test_student_bulk_booking_rolls_back_every_slot_when_one_is_full(self):
        self._allow_multiple(self.first_session, self.second_session)
        self.make_participant(
            self.tenant,
            self.second_session,
            self.data["students"][1],
            status=SessionParticipant.Status.BOOKED,
        )
        self.second_session.max_participants = 1
        self.second_session.save(update_fields=["max_participants"])
        self.client.force_authenticate(user=self.student.user)

        response = self.client.post(
            "/api/v1/clinic/participants/bulk-create/",
            {"session_ids": [self.first_session.id, self.second_session.id]},
            format="json",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 409, response.data)
        self.assertFalse(
            SessionParticipant.objects.filter(
                tenant=self.tenant,
                student=self.student,
                session_id__in=[self.first_session.id, self.second_session.id],
            ).exists()
        )

    def test_student_bulk_booking_rejects_sessions_from_different_dates(self):
        another_date_session = self.make_clinic_session(
            self.tenant,
            date=self.tomorrow + datetime.timedelta(days=1),
            start_time=datetime.time(17, 0),
            location="클리닉 1실",
            max_participants=10,
        )
        self._allow_multiple(self.first_session, another_date_session)
        self.client.force_authenticate(user=self.student.user)

        response = self.client.post(
            "/api/v1/clinic/participants/bulk-create/",
            {
                "session_ids": [self.first_session.id, another_date_session.id],
            },
            format="json",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(
            SessionParticipant.objects.filter(
                tenant=self.tenant,
                student=self.student,
            ).exists()
        )

    def test_student_bulk_booking_rejects_non_contiguous_sessions_atomically(self):
        gap_session = self.make_clinic_session(
            self.tenant,
            date=self.tomorrow,
            start_time=datetime.time(19, 0),
            location="클리닉 1실",
            max_participants=10,
        )
        self._allow_multiple(self.first_session, gap_session)
        self.client.force_authenticate(user=self.student.user)

        response = self.client.post(
            "/api/v1/clinic/participants/bulk-create/",
            {"session_ids": [self.first_session.id, gap_session.id]},
            format="json",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(
            SessionParticipant.objects.filter(
                tenant=self.tenant,
                student=self.student,
            ).exists()
        )

    def test_staff_adds_multiple_students_to_multiple_slots_atomically(self):
        self._allow_multiple(self.first_session, self.second_session)
        self.client.force_authenticate(user=self.data["admin_user"])
        student_ids = [student.id for student in self.data["students"]]

        response = self.client.post(
            "/api/v1/clinic/participants/bulk-create/",
            {
                "session_ids": [self.first_session.id, self.second_session.id],
                "student_ids": student_ids,
            },
            format="json",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["count"], 4)
        participants = SessionParticipant.objects.filter(
            tenant=self.tenant,
            session_id__in=[self.first_session.id, self.second_session.id],
            student_id__in=student_ids,
        )
        self.assertEqual(participants.count(), 4)
        self.assertEqual(
            {participant.status for participant in participants},
            {SessionParticipant.Status.BOOKED},
        )
        self.assertEqual(
            {participant.source for participant in participants},
            {SessionParticipant.Source.MANUAL},
        )

    def test_staff_adds_exact_enrollment_targets_atomically(self):
        self._allow_multiple(self.first_session, self.second_session)
        self.client.force_authenticate(user=self.data["admin_user"])
        enrollment_ids = [enrollment.id for enrollment in self.data["enrollments"]]
        Exam = django_apps.get_model("exams", "Exam")
        ExamEnrollment = django_apps.get_model("exams", "ExamEnrollment")
        exam = Exam.objects.create(
            tenant=self.tenant,
            title="정확 수강 대상 시험",
            exam_type="regular",
            is_active=True,
        )
        exam.sessions.add(self.data["lec_session"])
        ExamEnrollment.objects.create(
            exam=exam,
            enrollment=self.data["enrollments"][0],
        )
        self.make_clinic_link(
            self.data["enrollments"][0],
            self.data["lec_session"],
            tenant=self.tenant,
            source_type="exam",
            source_id=exam.id,
        )

        from apps.support.clinic.session_dependencies import (
            clinic_reasons_for_unresolved_auto_links,
        )

        with patch(
            "apps.domains.clinic.services.lifecycle.clinic_reasons_for_unresolved_auto_links",
            wraps=clinic_reasons_for_unresolved_auto_links,
        ) as reason_loader:
            response = self.client.post(
                "/api/v1/clinic/participants/bulk-create/",
                {
                    "session_ids": [self.first_session.id, self.second_session.id],
                    "enrollment_ids": enrollment_ids,
                },
                format="json",
                **self._headers(),
            )

        self.assertEqual(response.status_code, 201, response.data)
        reason_loader.assert_called_once()
        self.assertEqual(
            set(reason_loader.call_args.args[1]),
            set(enrollment_ids),
        )
        self.assertEqual(response.data["count"], 4)
        participants = SessionParticipant.objects.filter(
            tenant=self.tenant,
            session_id__in=[self.first_session.id, self.second_session.id],
        )
        self.assertEqual(participants.count(), 4)
        self.assertEqual(
            set(participants.values_list("enrollment_id", flat=True)),
            set(enrollment_ids),
        )
        self.assertEqual(
            set(
                participants.filter(enrollment_id=enrollment_ids[0]).values_list(
                    "clinic_reason", flat=True
                )
            ),
            {SessionParticipant.Reason.EXAM},
        )

    def test_staff_exact_enrollment_bulk_ignores_unassigned_exam_reason(self):
        self.client.force_authenticate(user=self.data["admin_user"])
        Exam = django_apps.get_model("exams", "Exam")
        ExamEnrollment = django_apps.get_model("exams", "ExamEnrollment")
        exam = Exam.objects.create(
            tenant=self.tenant,
            title="미배정 사유 제외 시험",
            exam_type="regular",
            is_active=True,
        )
        exam.sessions.add(self.data["lec_session"])
        ExamEnrollment.objects.create(
            exam=exam,
            enrollment=self.data["enrollments"][1],
        )
        self.make_clinic_link(
            self.data["enrollments"][0],
            self.data["lec_session"],
            tenant=self.tenant,
            source_type="exam",
            source_id=exam.id,
        )

        response = self.client.post(
            "/api/v1/clinic/participants/bulk-create/",
            {
                "session_ids": [self.first_session.id],
                "enrollment_ids": [self.data["enrollments"][0].id],
            },
            format="json",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 201, response.data)
        participant = SessionParticipant.objects.get(
            tenant=self.tenant,
            session=self.first_session,
            enrollment=self.data["enrollments"][0],
        )
        self.assertIsNone(participant.clinic_reason)

    def test_staff_exact_enrollment_bulk_rolls_back_with_specific_existing_booking_reason(self):
        self.client.force_authenticate(user=self.data["admin_user"])
        existing_enrollment = self.data["enrollments"][0]
        new_enrollment = self.data["enrollments"][1]
        self.make_participant(
            self.tenant,
            self.first_session,
            existing_enrollment.student,
            enrollment=existing_enrollment,
            status=SessionParticipant.Status.BOOKED,
        )

        response = self.client.post(
            "/api/v1/clinic/participants/bulk-create/",
            {
                "session_ids": [self.first_session.id],
                "enrollment_ids": [new_enrollment.id, existing_enrollment.id],
            },
            format="json",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["detail"], "이미 해당 세션에 예약된 학생입니다.")
        self.assertFalse(
            SessionParticipant.objects.filter(
                tenant=self.tenant,
                session=self.first_session,
                enrollment=new_enrollment,
            ).exists()
        )

    def test_staff_bulk_rejects_mixed_student_and_enrollment_ids_without_writes(self):
        self.client.force_authenticate(user=self.data["admin_user"])

        response = self.client.post(
            "/api/v1/clinic/participants/bulk-create/",
            {
                "session_ids": [self.first_session.id],
                "student_ids": [self.student.id],
                "enrollment_ids": [self.data["enrollments"][0].id],
            },
            format="json",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(
            [str(item) for item in response.data["detail"]],
            ["student_ids와 enrollment_ids 중 하나만 선택해 주세요."],
        )
        self.assertFalse(
            SessionParticipant.objects.filter(
                tenant=self.tenant,
                session=self.first_session,
            ).exists()
        )

    def test_staff_exact_enrollment_bulk_rejects_inactive_and_foreign_targets(self):
        self.client.force_authenticate(user=self.data["admin_user"])
        inactive = self.data["enrollments"][0]
        inactive.status = "INACTIVE"
        inactive.save(update_fields=["status"])
        foreign = self.setup_api_tenant("clinic_multi_slot_foreign_enrollment")

        for enrollment_id in (inactive.id, foreign["enrollments"][0].id):
            with self.subTest(enrollment_id=enrollment_id):
                response = self.client.post(
                    "/api/v1/clinic/participants/bulk-create/",
                    {
                        "session_ids": [self.first_session.id],
                        "enrollment_ids": [enrollment_id],
                    },
                    format="json",
                    **self._headers(),
                )
                self.assertEqual(response.status_code, 404, response.data)

        self.assertFalse(
            SessionParticipant.objects.filter(
                tenant=self.tenant,
                session=self.first_session,
            ).exists()
        )

    def test_student_cannot_supply_bulk_enrollment_ids(self):
        self.client.force_authenticate(user=self.student.user)

        response = self.client.post(
            "/api/v1/clinic/participants/bulk-create/",
            {
                "session_ids": [self.first_session.id],
                "enrollment_ids": [self.data["enrollments"][0].id],
            },
            format="json",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 403, response.data)
        self.assertFalse(
            SessionParticipant.objects.filter(
                tenant=self.tenant,
                session=self.first_session,
            ).exists()
        )

    def test_staff_exact_enrollment_bulk_rejects_two_courses_for_same_student(self):
        self.client.force_authenticate(user=self.data["admin_user"])
        other_lecture = self.make_lecture(self.tenant, title="영어")
        other_enrollment = self.make_enrollment(
            self.tenant,
            self.student,
            other_lecture,
        )

        response = self.client.post(
            "/api/v1/clinic/participants/bulk-create/",
            {
                "session_ids": [self.first_session.id],
                "enrollment_ids": [
                    self.data["enrollments"][0].id,
                    other_enrollment.id,
                ],
            },
            format="json",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(
            str(response.data["enrollment_ids"]),
            "같은 학생의 수강 대상은 한 번만 선택해 주세요.",
        )
        self.assertFalse(
            SessionParticipant.objects.filter(
                tenant=self.tenant,
                session=self.first_session,
            ).exists()
        )

    def test_student_cannot_supply_bulk_student_ids(self):
        self.client.force_authenticate(user=self.student.user)

        response = self.client.post(
            "/api/v1/clinic/participants/bulk-create/",
            {
                "session_ids": [self.first_session.id],
                "student_ids": [self.data["students"][1].id],
            },
            format="json",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 403, response.data)
        self.assertFalse(
            SessionParticipant.objects.filter(
                tenant=self.tenant,
                session=self.first_session,
            ).exists()
        )

    def test_default_off_rejects_second_student_bulk_slot_atomically(self):
        self.client.force_authenticate(user=self.student.user)

        response = self.client.post(
            "/api/v1/clinic/participants/bulk-create/",
            {"session_ids": [self.first_session.id, self.second_session.id]},
            format="json",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 409, response.data)
        self.assertFalse(
            SessionParticipant.objects.filter(
                tenant=self.tenant,
                student=self.student,
            ).exists()
        )

    def test_mixed_policy_bulk_rejects_every_slot_atomically(self):
        self._allow_multiple(self.first_session)
        self.client.force_authenticate(user=self.data["admin_user"])

        response = self.client.post(
            "/api/v1/clinic/participants/bulk-create/",
            {
                "session_ids": [self.first_session.id, self.second_session.id],
                "student_ids": [self.student.id],
            },
            format="json",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 409, response.data)
        self.assertFalse(
            SessionParticipant.objects.filter(
                tenant=self.tenant,
                student=self.student,
            ).exists()
        )

    def test_single_student_post_respects_off_policy(self):
        self.client.force_authenticate(user=self.student.user)

        first = self.client.post(
            "/api/v1/clinic/participants/",
            {"session": self.first_session.id},
            format="json",
            **self._headers(),
        )
        second = self.client.post(
            "/api/v1/clinic/participants/",
            {"session": self.second_session.id},
            format="json",
            **self._headers(),
        )

        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(second.status_code, 409, second.data)
        self.assertEqual(
            SessionParticipant.objects.filter(
                tenant=self.tenant,
                student=self.student,
                status__in=["pending", "booked"],
            ).count(),
            1,
        )

    def test_single_staff_post_respects_off_policy(self):
        self.client.force_authenticate(user=self.data["admin_user"])

        first = self.client.post(
            "/api/v1/clinic/participants/",
            {"session": self.first_session.id, "student": self.student.id},
            format="json",
            **self._headers(),
        )
        second = self.client.post(
            "/api/v1/clinic/participants/",
            {"session": self.second_session.id, "student": self.student.id},
            format="json",
            **self._headers(),
        )

        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(second.status_code, 409, second.data)

    def test_single_post_allows_multiple_when_every_session_is_on(self):
        self._allow_multiple(self.first_session, self.second_session)
        self.client.force_authenticate(user=self.student.user)

        responses = [
            self.client.post(
                "/api/v1/clinic/participants/",
                {"session": session.id},
                format="json",
                **self._headers(),
            )
            for session in (self.first_session, self.second_session)
        ]

        self.assertEqual([response.status_code for response in responses], [201, 201])

    def test_inactive_rows_do_not_block_off_session(self):
        inactive_statuses = [
            SessionParticipant.Status.CANCELLED,
            SessionParticipant.Status.REJECTED,
            SessionParticipant.Status.NO_SHOW,
        ]
        for index, inactive_status in enumerate(inactive_statuses):
            inactive_session = self.make_clinic_session(
                self.tenant,
                date=self.tomorrow,
                start_time=datetime.time(14 + index, 0),
                location=f"클리닉 {index + 2}실",
                max_participants=10,
            )
            self.make_participant(
                self.tenant,
                inactive_session,
                self.student,
                status=inactive_status,
            )
        self.client.force_authenticate(user=self.student.user)

        response = self.client.post(
            "/api/v1/clinic/participants/",
            {"session": self.first_session.id},
            format="json",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 201, response.data)

    def test_switching_on_to_off_preserves_rows_and_blocks_only_new_conflicts(self):
        third_session = self.make_clinic_session(
            self.tenant,
            date=self.tomorrow,
            start_time=datetime.time(19, 0),
            location="클리닉 1실",
            max_participants=10,
        )
        self._allow_multiple(self.first_session, self.second_session, third_session)
        self.client.force_authenticate(user=self.student.user)
        created = self.client.post(
            "/api/v1/clinic/participants/bulk-create/",
            {"session_ids": [self.first_session.id, self.second_session.id]},
            format="json",
            **self._headers(),
        )
        self.assertEqual(created.status_code, 201, created.data)

        self.first_session.allow_multi_slot_booking = False
        self.first_session.save(update_fields=["allow_multi_slot_booking"])
        blocked = self.client.post(
            "/api/v1/clinic/participants/",
            {"session": third_session.id},
            format="json",
            **self._headers(),
        )

        self.assertEqual(blocked.status_code, 409, blocked.data)
        self.assertEqual(
            SessionParticipant.objects.filter(
                tenant=self.tenant,
                student=self.student,
                status__in=["pending", "booked"],
            ).count(),
            2,
        )

    def test_off_policy_allows_atomic_booking_change_replacement(self):
        old_booking = self.make_participant(
            self.tenant,
            self.first_session,
            self.student,
            status=SessionParticipant.Status.PENDING,
            source=SessionParticipant.Source.STUDENT_REQUEST,
        )
        self.client.force_authenticate(user=self.student.user)

        with patch(
            "apps.domains.clinic.views.participant_views._send_clinic_notification",
            return_value={"requested": 2, "failed": 0, "send_to": "both"},
        ):
            response = self.client.post(
                f"/api/v1/clinic/participants/{old_booking.id}/change-booking/",
                {"new_session_id": self.second_session.id},
                format="json",
                **self._headers(),
            )

        self.assertEqual(response.status_code, 200, response.data)
        old_booking.refresh_from_db()
        self.assertEqual(old_booking.status, SessionParticipant.Status.CANCELLED)
        self.assertEqual(response.data["session"], self.second_session.id)

    def test_cross_tenant_mixed_bulk_fails_closed_without_writes(self):
        foreign = self.setup_api_tenant("clinic_multi_slot_foreign", student_count=1)
        foreign_session = self.make_clinic_session(
            foreign["tenant"],
            date=self.tomorrow,
            start_time=datetime.time(19, 0),
            location="타학원 클리닉",
            max_participants=10,
        )
        self._allow_multiple(self.first_session, foreign_session)
        self.client.force_authenticate(user=self.student.user)

        response = self.client.post(
            "/api/v1/clinic/participants/bulk-create/",
            {"session_ids": [self.first_session.id, foreign_session.id]},
            format="json",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 404, response.data)
        self.assertFalse(
            SessionParticipant.objects.filter(
                tenant=self.tenant,
                student=self.student,
            ).exists()
        )

    def test_session_create_snapshots_tenant_default_and_explicit_override(self):
        self.tenant.clinic_allow_multi_slot_booking_default = True
        self.tenant.save(update_fields=["clinic_allow_multi_slot_booking_default"])
        self.client.force_authenticate(user=self.data["admin_user"])
        base_payload = {
            "title": "다중 예약 기본값",
            "date": str(self.tomorrow + datetime.timedelta(days=2)),
            "start_time": "20:00",
            "duration_minutes": 60,
            "location": "기본값실",
            "max_participants": 10,
        }

        default_response = self.client.post(
            "/api/v1/clinic/sessions/",
            base_payload,
            format="json",
            **self._headers(),
        )
        override_response = self.client.post(
            "/api/v1/clinic/sessions/",
            {
                **base_payload,
                "start_time": "21:00",
                "allow_multi_slot_booking": False,
            },
            format="json",
            **self._headers(),
        )

        self.assertEqual(default_response.status_code, 201, default_response.data)
        self.assertTrue(default_response.data["allow_multi_slot_booking"])
        self.assertEqual(override_response.status_code, 201, override_response.data)
        self.assertFalse(override_response.data["allow_multi_slot_booking"])

    def test_session_bulk_create_snapshots_tenant_default(self):
        self.tenant.clinic_allow_multi_slot_booking_default = True
        self.tenant.save(update_fields=["clinic_allow_multi_slot_booking_default"])
        self.client.force_authenticate(user=self.data["admin_user"])
        target_date = self.tomorrow + datetime.timedelta(days=3)

        response = self.client.post(
            "/api/v1/clinic/sessions/bulk-create/",
            {
                "dates": [str(target_date)],
                "title": "반복 다중 예약",
                "start_time": "20:00",
                "duration_minutes": 60,
                "location": "반복실",
                "max_participants": 10,
            },
            format="json",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 201, response.data)
        created = Session.objects.get(id=response.data["created"][0]["id"])
        self.assertTrue(created.allow_multi_slot_booking)

    def test_settings_exposes_multi_slot_tenant_default(self):
        self.tenant.clinic_allow_multi_slot_booking_default = True
        self.tenant.save(update_fields=["clinic_allow_multi_slot_booking_default"])
        self.client.force_authenticate(user=self.data["admin_user"])

        response = self.client.get(
            "/api/v1/clinic/settings/",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["multi_slot_booking_default"])


class ClinicMultiSlotBookingPostgresConcurrencyTest(
    TransactionTestCase,
    ClinicAPITestMixin,
):
    reset_sequences = True

    def setUp(self):
        self.data = self.setup_api_tenant("clinic_multi_slot_pg", student_count=1)
        self.tenant = self.data["tenant"]
        self.student = self.data["students"][0]
        target_date = datetime.date.today() + datetime.timedelta(days=1)
        self.sessions = [
            self.make_clinic_session(
                self.tenant,
                date=target_date,
                start_time=datetime.time(hour, 0),
                location="동시성실",
                max_participants=10,
            )
            for hour in (17, 18)
        ]

    def test_off_policy_serializes_concurrent_single_slot_writes(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL row-lock contract")

        barrier = threading.Barrier(2, timeout=10)
        outcomes = []

        def book(session_id):
            close_old_connections()
            try:
                tenant = Tenant.objects.get(pk=self.tenant.pk)
                student = Student.objects.get(pk=self.student.pk)
                session = Session.objects.get(pk=session_id)
                barrier.wait()
                result = create_participant(
                    tenant=tenant,
                    validated_data={"session": session, "student": student},
                    request_student=student,
                )
                outcomes.append(("created", result.participant.id))
            except Exception as exc:  # pragma: no cover - asserted below
                outcomes.append(("error", getattr(exc, "status_code", None)))
            finally:
                close_old_connections()

        threads = [
            threading.Thread(target=book, args=(session.id,))
            for session in self.sessions
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(sorted(outcome[0] for outcome in outcomes), ["created", "error"])
        self.assertIn(("error", 409), outcomes)
        self.assertEqual(
            SessionParticipant.objects.filter(
                tenant=self.tenant,
                student=self.student,
                status__in=["pending", "booked"],
            ).count(),
            1,
        )


class ClinicMultiSlotBookingMigrationDataTest(TestCase, ClinicAPITestMixin):
    def test_exact_initial_tenant_and_existing_session_policies(self):
        tenants = {
            code: self.make_tenant(code=code, name=code)
            for code in ("tchul", "godmin", "limglish")
        }
        sessions = {
            code: self.make_clinic_session(
                tenant,
                date=datetime.date.today() + datetime.timedelta(days=1),
                start_time=datetime.time(17, 0),
                location=f"{code}-clinic",
                max_participants=10,
            )
            for code, tenant in tenants.items()
        }
        Tenant.objects.filter(pk__in=[tenant.pk for tenant in tenants.values()]).update(
            clinic_allow_multi_slot_booking_default=True
        )
        Session.objects.filter(pk__in=[session.pk for session in sessions.values()]).update(
            allow_multi_slot_booking=True
        )

        tenant_migration = importlib.import_module(
            "apps.core.migrations.0059_tenant_clinic_multi_slot_booking_default"
        )
        session_migration = importlib.import_module(
            "apps.domains.clinic.migrations.0017_session_allow_multi_slot_booking"
        )
        tenant_migration.set_initial_tenant_defaults(django_apps, None)
        session_migration.set_initial_session_policies(django_apps, None)

        for tenant in tenants.values():
            tenant.refresh_from_db()
        for session in sessions.values():
            session.refresh_from_db()
        self.assertFalse(tenants["tchul"].clinic_allow_multi_slot_booking_default)
        self.assertFalse(tenants["godmin"].clinic_allow_multi_slot_booking_default)
        self.assertTrue(tenants["limglish"].clinic_allow_multi_slot_booking_default)
        self.assertFalse(sessions["tchul"].allow_multi_slot_booking)
        self.assertFalse(sessions["godmin"].allow_multi_slot_booking)
        self.assertTrue(sessions["limglish"].allow_multi_slot_booking)
