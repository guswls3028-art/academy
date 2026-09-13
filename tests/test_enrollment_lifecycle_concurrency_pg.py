from __future__ import annotations

import threading
import time
import unittest
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from academy.adapters.db.django import repositories_enrollment as enroll_repo
from apps.core.models import Tenant, TenantMembership
from apps.domains.attendance.models import Attendance
from apps.domains.attendance.services import create_attendance_roster
from apps.domains.enrollment.models import Enrollment, SessionEnrollment
from apps.domains.enrollment.services.lifecycle import (
    bulk_create_enrollments,
    bulk_create_session_enrollments,
)
from apps.domains.lectures.models import Lecture, Session
from apps.domains.students.models import Student
from apps.support.enrollment.lifecycle_dependencies import (
    ensure_session_roster_membership,
)


User = get_user_model()


class EnrollmentLifecycleConcurrencyPostgresTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        if connection.vendor != "postgresql":
            raise unittest.SkipTest(
                "PostgreSQL row-level locking is required for lifecycle concurrency."
            )
        super().setUpClass()

    def setUp(self):
        suffix = uuid.uuid4().hex[:8]
        self.tenant = Tenant.objects.create(
            name=f"Enrollment concurrency {suffix}",
            code=f"enrollment-concurrency-{suffix}",
            is_active=True,
        )
        self.students = [self._student(suffix, index) for index in range(2)]

    def _student(self, suffix: str, index: int) -> Student:
        user = User.objects.create_user(
            tenant=self.tenant,
            username=f"enrollment-concurrency-{suffix}-{index}",
            password="test1234",
        )
        return Student.objects.create(
            tenant=self.tenant,
            user=user,
            name=f"Concurrent student {index}",
            ps_number=f"EC-{suffix}-{index}",
            omr_code=f"{index + 1:08d}",
            parent_phone=f"0108111000{index}",
        )

    def _lecture(self, title: str) -> Lecture:
        return Lecture.objects.create(
            tenant=self.tenant,
            title=title,
            name=title,
            subject="MATH",
        )

    @staticmethod
    def _start_and_join(threads: list[threading.Thread]) -> None:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

    def test_reverse_order_bulk_enrollment_locks_students_without_deadlock(self):
        lectures = [self._lecture("Bulk A"), self._lecture("Bulk B")]
        student_ids = [student.id for student in self.students]
        barrier = threading.Barrier(2, timeout=10)
        errors: list[BaseException] = []

        def worker(lecture_id: int, requested_student_ids: list[int]) -> None:
            close_old_connections()
            try:
                tenant = Tenant.objects.get(pk=self.tenant.pk)
                barrier.wait()
                bulk_create_enrollments(
                    tenant=tenant,
                    lecture_id=lecture_id,
                    student_ids=requested_student_ids,
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                close_old_connections()

        threads = [
            threading.Thread(
                target=worker,
                args=(lectures[0].id, student_ids),
                name="bulk-enrollment-forward",
            ),
            threading.Thread(
                target=worker,
                args=(lectures[1].id, list(reversed(student_ids))),
                name="bulk-enrollment-reverse",
            ),
        ]
        with patch(
            "apps.domains.enrollment.services.lifecycle.schedule_pending_account_notice"
        ):
            self._start_and_join(threads)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(
            Enrollment.objects.filter(
                tenant=self.tenant,
                lecture__in=lectures,
            ).count(),
            4,
        )

    def test_reverse_order_session_batch_locks_students_then_enrollments(self):
        lecture = self._lecture("Session batch")
        session = Session.objects.create(
            lecture=lecture,
            order=1,
            title="Session batch",
        )
        enrollments = [
            Enrollment.objects.create(
                tenant=self.tenant,
                lecture=lecture,
                student=student,
                status="ACTIVE",
            )
            for student in self.students
        ]
        enrollment_ids = [enrollment.id for enrollment in enrollments]
        barrier = threading.Barrier(2, timeout=10)
        errors: list[BaseException] = []

        def worker(requested_enrollment_ids: list[int]) -> None:
            close_old_connections()
            try:
                tenant = Tenant.objects.get(pk=self.tenant.pk)
                barrier.wait()
                bulk_create_session_enrollments(
                    tenant=tenant,
                    session_id=session.id,
                    enrollment_ids=requested_enrollment_ids,
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                close_old_connections()

        threads = [
            threading.Thread(
                target=worker,
                args=(enrollment_ids,),
                name="session-enrollment-forward",
            ),
            threading.Thread(
                target=worker,
                args=(list(reversed(enrollment_ids)),),
                name="session-enrollment-reverse",
            ),
        ]
        self._start_and_join(threads)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(
            SessionEnrollment.objects.filter(
                tenant=self.tenant,
                session=session,
            ).count(),
            2,
        )
        self.assertEqual(
            Attendance.objects.filter(
                tenant=self.tenant,
                session=session,
            ).count(),
            2,
        )

    def test_concurrent_public_roster_services_restore_one_existing_attendance(self):
        lecture = self._lecture("Session re-registration")
        session = Session.objects.create(lecture=lecture, order=1, title="Session re-registration")
        enrollment = Enrollment.objects.create(
            tenant=self.tenant, lecture=lecture, student=self.students[0], status="ACTIVE",
        )
        attendance = Attendance.objects.create(
            tenant=self.tenant, enrollment=enrollment, session=session,
            status="SECESSION", memo="Keep this attendance history",
        )
        barrier = threading.Barrier(2, timeout=10)
        errors: list[BaseException] = []

        def worker(use_enrollment_service: bool) -> None:
            close_old_connections()
            try:
                tenant = Tenant.objects.get(pk=self.tenant.pk)
                barrier.wait()
                if use_enrollment_service:
                    bulk_create_session_enrollments(
                        tenant=tenant, session_id=session.id, enrollment_ids=[enrollment.id],
                    )
                else:
                    create_attendance_roster(
                        tenant=tenant, session_id=session.id, student_ids=[enrollment.student_id],
                    )
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                close_old_connections()

        threads = [
            threading.Thread(target=worker, args=(False,), name="attendance-reregister"),
            threading.Thread(target=worker, args=(True,), name="session-enrollment-reregister"),
        ]
        self._start_and_join(threads)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        attendance.refresh_from_db()
        self.assertEqual(attendance.status, "UNSET")
        self.assertEqual(attendance.memo, "Keep this attendance history")
        self.assertEqual(Attendance.objects.get(
            tenant=self.tenant, enrollment=enrollment, session=session,
        ).id, attendance.id)
        self.assertEqual(SessionEnrollment.objects.filter(
            tenant=self.tenant, enrollment=enrollment, session=session,
        ).count(), 1)

    def test_session_withdrawal_waiting_for_reregistration_finishes_without_deadlock(self):
        self._assert_attendance_mutation_waiting_for_reregistration(
            action="withdraw", expected_response=200, expected_status="SECESSION", expected_membership=False,
        )

    def test_detail_patch_waiting_for_reregistration_preserves_online_edit(self):
        self._assert_attendance_mutation_waiting_for_reregistration(
            action="patch", expected_response=200, expected_status="ONLINE", expected_membership=True,
        )

    def test_detail_put_waiting_for_reregistration_preserves_online_edit(self):
        self._assert_attendance_mutation_waiting_for_reregistration(
            action="put", expected_response=200, expected_status="ONLINE", expected_membership=True,
        )

    def test_detail_delete_waiting_for_reregistration_removes_roster(self):
        self._assert_attendance_mutation_waiting_for_reregistration(
            action="delete", expected_response=204, expected_status=None, expected_membership=False,
        )

    def test_bulk_present_waiting_for_roster_repeat_marks_online_attendance(self):
        self._assert_attendance_mutation_waiting_for_reregistration(
            action="bulk_set_present", initial_status="ONLINE", expected_response=200,
            expected_status="PRESENT", expected_membership=True,
        )

    def test_stale_bulk_undo_waiting_for_reregistration_keeps_terminal_guard(self):
        self._assert_attendance_mutation_waiting_for_reregistration(
            action="bulk_undo_present", initial_status="ONLINE", expected_response=409,
            expected_status="UNSET", expected_membership=True,
        )

    def _assert_attendance_mutation_waiting_for_reregistration(
        self, *, action, expected_response, expected_status, expected_membership, initial_status="SECESSION",
    ):
        lecture = self._lecture("Attendance mutation versus re-registration")
        session = Session.objects.create(lecture=lecture, order=1, title="Session")
        enrollment = Enrollment.objects.create(
            tenant=self.tenant, lecture=lecture, student=self.students[0], status="ACTIVE",
        )
        attendance = Attendance.objects.create(
            tenant=self.tenant, enrollment=enrollment, session=session,
            status=initial_status, memo="Keep withdrawal history",
        )
        if initial_status != "SECESSION":
            SessionEnrollment.objects.create(tenant=self.tenant, enrollment=enrollment, session=session)
        staff = User.objects.create_user(
            tenant=self.tenant, username=f"withdrawal-{self.tenant.id}", is_staff=True,
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=staff, role="admin")
        headers = {"HTTP_HOST": "localhost", "HTTP_X_TENANT_CODE": self.tenant.code}
        detail_url = f"/api/v1/lectures/attendance/{attendance.id}/"
        withdrawal_payload = {"status": "SECESSION", "confirm_secession": True, "secession_scope": "session"}
        request_method = "patch"
        request_url = detail_url
        request_payload = {"status": "ONLINE"}
        if action == "withdraw":
            request_payload = withdrawal_payload
        elif action == "put":
            request_method = "put"
            request_payload.update(session=session.id, enrollment_id=enrollment.id)
        elif action == "delete":
            request_method = "delete"
            request_payload = {}
        elif action in {"bulk_set_present", "bulk_undo_present"}:
            request_method = "post"
            request_url = f"/api/v1/lectures/attendance/{action}/"
            request_payload = {"session": session.id}
            if action == "bulk_undo_present":
                setup_client = APIClient()
                setup_client.force_authenticate(staff)
                present_response = setup_client.post(
                    "/api/v1/lectures/attendance/bulk_set_present/",
                    {"session": session.id}, format="json", **headers,
                )
                self.assertEqual(present_response.status_code, 200, present_response.data)
                self.assertEqual(present_response.data["updated"], 1)
                request_payload = {"undo_token": present_response.data["undo_token"]}
                withdrawal_response = setup_client.patch(
                    detail_url, withdrawal_payload, format="json", **headers,
                )
                self.assertEqual(withdrawal_response.status_code, 200, withdrawal_response.data)
                attendance.refresh_from_db()
                self.assertEqual(attendance.status, "SECESSION")
        roster_locked = threading.Event()
        resume_roster = threading.Event()
        mutation_connected = threading.Event()
        backend_pids: dict[str, int] = {}
        errors: list[BaseException] = []
        responses: list[int] = []
        response_bodies: list[dict] = []
        original_attendance = enroll_repo.attendance_get_or_create_tenant

        def paused_attendance(*args, **kwargs):
            # The canonical helper has locked Student and Enrollment at this point.
            roster_locked.set()
            if not resume_roster.wait(timeout=10):
                raise AssertionError("attendance mutation did not reach the database lock boundary")
            return original_attendance(*args, **kwargs)

        def reregister() -> None:
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET statement_timeout = '10s'")
                    cursor.execute("SELECT pg_backend_pid()")
                    backend_pids["roster"] = cursor.fetchone()[0]
                create_attendance_roster(
                    tenant=self.tenant, session_id=session.id, student_ids=[enrollment.student_id],
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                close_old_connections()

        def track_mutation_connection(execute, sql, params, many, context):
            if "mutation" not in backend_pids:
                # Capture the connection opened inside the actual HTTP request.
                with context["connection"].connection.cursor() as cursor:
                    cursor.execute("SET statement_timeout = '10s'")
                    cursor.execute("SELECT pg_backend_pid()")
                    backend_pids["mutation"] = cursor.fetchone()[0]
                mutation_connected.set()
            try:
                return execute(sql, params, many, context)
            except DatabaseError as exc:
                sqlstate = getattr(exc.__cause__, "sqlstate", None) or getattr(exc.__cause__, "pgcode", None)
                print(f"{action} database error: {type(exc).__name__}; SQLSTATE={sqlstate}; {exc}")
                raise

        def mutate_attendance() -> None:
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(staff)
                with connection.execute_wrapper(track_mutation_connection):
                    response = getattr(client, request_method)(
                        request_url, request_payload, format="json", **headers,
                    )
                responses.append(response.status_code)
                response_bodies.append(getattr(response, "data", {}))
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                close_old_connections()

        threads = [
            threading.Thread(target=reregister, name="reregister-before-mutation"),
            threading.Thread(target=mutate_attendance, name=f"{action}-during-reregistration"),
        ]
        with patch.object(enroll_repo, "attendance_get_or_create_tenant", side_effect=paused_attendance):
            threads[0].start()
            try:
                self.assertTrue(roster_locked.wait(timeout=10), errors)
                threads[1].start()
                self.assertTrue(mutation_connected.wait(timeout=10), errors)
                waiting_on_roster = False
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT %s = ANY(pg_blocking_pids(%s))",
                            [backend_pids["roster"], backend_pids["mutation"]],
                        )
                        waiting_on_roster = cursor.fetchone()[0]
                    if waiting_on_roster:
                        break
                    # Poll a database-observed boundary; elapsed time does not release the roster.
                    resume_roster.wait(timeout=0.01)
                self.assertTrue(waiting_on_roster, errors)
                attendance_locked_by_mutation = False
                try:
                    with transaction.atomic():
                        Attendance.objects.select_for_update(nowait=True).get(pk=attendance.id)
                except DatabaseError as exc:
                    if getattr(exc.__cause__, "sqlstate", None) != "55P03" and getattr(
                        exc.__cause__, "pgcode", None,
                    ) != "55P03":
                        raise
                    attendance_locked_by_mutation = True
                print(
                    f"{action} blocked by roster transaction; "
                    f"attendance_nowait_blocked={attendance_locked_by_mutation}"
                )
            finally:
                resume_roster.set()
                for thread in threads:
                    if thread.ident is not None:
                        thread.join(timeout=15)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(responses, [expected_response], response_bodies)
        enrollment.refresh_from_db()
        self.assertEqual(enrollment.status, "ACTIVE")
        if expected_status is None:
            self.assertFalse(Attendance.objects.filter(pk=attendance.id).exists())
        else:
            attendance.refresh_from_db()
            self.assertEqual(attendance.status, expected_status)
            self.assertEqual(attendance.memo, "Keep withdrawal history")
        self.assertEqual(SessionEnrollment.objects.filter(
            tenant=self.tenant, enrollment=enrollment, session=session,
        ).exists(), expected_membership)
        if action == "bulk_set_present":
            self.assertEqual(response_bodies[0]["updated"], 1)
        elif action == "bulk_undo_present":
            self.assertIn("이미 변경", response_bodies[0]["detail"])

    def test_detail_student_deleted_after_snapshot_preserves_not_found(self):
        self._assert_attendance_snapshot_change(change="student_deleted", expected_response=404)

    def test_detail_status_filter_changed_after_snapshot_preserves_not_found(self):
        self._assert_attendance_snapshot_change(change="status_filter", expected_response=404)

    def test_detail_enrollment_lecture_changed_after_snapshot_rejects_relationship_drift(self):
        self._assert_attendance_snapshot_change(change="enrollment_lecture", expected_response=409)

    def test_bulk_present_enrollment_inactivated_after_snapshot_changes_no_attendance(self):
        self._assert_attendance_snapshot_change(change="enrollment_inactive", expected_response=409)

    def _assert_attendance_snapshot_change(self, *, change, expected_response):
        lecture = self._lecture("Attendance snapshot")
        other_lecture = self._lecture("Different lecture")
        session = Session.objects.create(lecture=lecture, order=1, title="Snapshot session")
        targets = self.students if change == "enrollment_inactive" else self.students[:1]
        enrollments = []
        attendances = []
        for student in targets:
            enrollment = Enrollment.objects.create(
                tenant=self.tenant, lecture=lecture, student=student, status="ACTIVE",
            )
            enrollments.append(enrollment)
            SessionEnrollment.objects.create(tenant=self.tenant, enrollment=enrollment, session=session)
            attendances.append(Attendance.objects.create(
                tenant=self.tenant, enrollment=enrollment, session=session,
                status="ONLINE", memo="Keep snapshot history",
            ))
        staff = User.objects.create_user(
            tenant=self.tenant, username=f"snapshot-{self.tenant.id}", is_staff=True,
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=staff, role="admin")
        headers = {"HTTP_HOST": "localhost", "HTTP_X_TENANT_CODE": self.tenant.code}
        snapshot_ready = threading.Event()
        resume_parent_locks = threading.Event()
        request_pids: list[int] = []
        errors: list[BaseException] = []
        responses = []
        original_parent_locks = enroll_repo.lock_attendance_parent_rows

        def paused_parent_locks(*args, **kwargs):
            # This boundary is after the queryset snapshot and before any parent row lock.
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                request_pids.append(cursor.fetchone()[0])
            snapshot_ready.set()
            if not resume_parent_locks.wait(timeout=10):
                raise AssertionError("the independent snapshot change did not commit")
            return original_parent_locks(*args, **kwargs)

        def mutate_attendance() -> None:
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(staff)
                if change == "enrollment_inactive":
                    response = client.post(
                        "/api/v1/lectures/attendance/bulk_set_present/",
                        {"session": session.id}, format="json", **headers,
                    )
                else:
                    url = f"/api/v1/lectures/attendance/{attendances[0].id}/"
                    payload = {"status": "PRESENT"}
                    if change == "status_filter":
                        url += "?status=ONLINE"
                    elif change == "enrollment_lecture":
                        payload = {
                            "status": "SECESSION", "confirm_secession": True, "secession_scope": "session",
                        }
                    response = client.patch(url, payload, format="json", **headers)
                responses.append(response)
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                close_old_connections()

        worker = threading.Thread(target=mutate_attendance, name=f"snapshot-{change}")
        with patch.object(enroll_repo, "lock_attendance_parent_rows", side_effect=paused_parent_locks):
            worker.start()
            try:
                self.assertTrue(snapshot_ready.wait(timeout=10), errors)
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    self.assertNotEqual(cursor.fetchone()[0], request_pids[0])
                if change == "enrollment_inactive":
                    change_client = APIClient()
                    change_client.force_authenticate(staff)
                    changed = change_client.patch(
                        f"/api/v1/enrollments/{enrollments[0].id}/",
                        {"status": "INACTIVE"}, format="json", **headers,
                    )
                    self.assertEqual(changed.status_code, 200, changed.data)
                else:
                    with transaction.atomic():
                        if change == "student_deleted":
                            Student.objects.filter(pk=targets[0].id).update(deleted_at=timezone.now())
                        elif change == "status_filter":
                            Attendance.objects.filter(pk=attendances[0].id).update(status="LATE")
                        else:
                            # Public enrollment edits forbid lecture moves; exercise DB relationship drift explicitly.
                            Enrollment.objects.filter(pk=enrollments[0].id).update(lecture=other_lecture)
                print(f"{change}: independent connection committed after snapshot, before parent locks")
            finally:
                resume_parent_locks.set()
                worker.join(timeout=15)

        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0].status_code, expected_response, getattr(responses[0], "data", {}))
        for attendance in attendances:
            attendance.refresh_from_db()
            expected_status = "LATE" if change == "status_filter" else "ONLINE"
            self.assertEqual(attendance.status, expected_status)
            self.assertEqual(attendance.memo, "Keep snapshot history")
            self.assertTrue(SessionEnrollment.objects.filter(
                tenant=self.tenant, enrollment_id=attendance.enrollment_id, session=session,
            ).exists())
        enrollments[0].refresh_from_db()
        if change == "student_deleted":
            targets[0].refresh_from_db()
            self.assertIsNotNone(targets[0].deleted_at)
        elif change == "enrollment_lecture":
            self.assertEqual(enrollments[0].lecture_id, other_lecture.id)
        elif change == "enrollment_inactive":
            self.assertEqual(enrollments[0].status, "INACTIVE")
            enrollments[1].refresh_from_db()
            self.assertEqual(enrollments[1].status, "ACTIVE")

    def test_stale_active_enrollment_is_reloaded_after_concurrent_deactivation(self):
        lecture = self._lecture("Stale enrollment")
        session = Session.objects.create(
            lecture=lecture,
            order=1,
            title="Stale enrollment",
        )
        enrollment = Enrollment.objects.create(
            tenant=self.tenant,
            lecture=lecture,
            student=self.students[0],
            status="ACTIVE",
        )
        stale_enrollment = Enrollment.objects.select_related("student").get(
            pk=enrollment.pk
        )
        barrier = threading.Barrier(2, timeout=10)
        deactivated = threading.Event()
        outcomes: list[str] = []
        errors: list[BaseException] = []

        def deactivate() -> None:
            close_old_connections()
            try:
                barrier.wait()
                Enrollment.objects.filter(pk=enrollment.pk).update(status="INACTIVE")
                outcomes.append("deactivated")
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                deactivated.set()
                close_old_connections()

        def add_to_roster() -> None:
            close_old_connections()
            try:
                tenant = Tenant.objects.get(pk=self.tenant.pk)
                current_session = Session.objects.select_related("lecture").get(
                    pk=session.pk
                )
                barrier.wait()
                if not deactivated.wait(timeout=10):
                    raise AssertionError("deactivation did not finish")
                with self.assertRaises(ValidationError):
                    ensure_session_roster_membership(
                        tenant=tenant,
                        session=current_session,
                        enrollment=stale_enrollment,
                    )
                outcomes.append("roster-blocked")
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                close_old_connections()

        threads = [
            threading.Thread(target=deactivate, name="deactivate-enrollment"),
            threading.Thread(target=add_to_roster, name="add-stale-enrollment"),
        ]
        self._start_and_join(threads)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertCountEqual(outcomes, ["deactivated", "roster-blocked"])
        self.assertFalse(
            SessionEnrollment.objects.filter(
                tenant=self.tenant,
                session=session,
                enrollment=enrollment,
            ).exists()
        )
        self.assertFalse(
            Attendance.objects.filter(
                tenant=self.tenant,
                session=session,
                enrollment=enrollment,
            ).exists()
        )
