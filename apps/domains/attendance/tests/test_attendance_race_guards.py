from __future__ import annotations

import inspect

from django.test import SimpleTestCase

from academy.adapters.db.django import repositories_enrollment as enroll_repo
from apps.domains.attendance.views import AttendanceViewSet


class AttendanceRaceGuardTests(SimpleTestCase):
    def test_status_mutations_use_row_locks(self):
        object_source = inspect.getsource(AttendanceViewSet.get_object)
        lock_source = inspect.getsource(AttendanceViewSet._lock_attendance_rows)
        parent_source = inspect.getsource(enroll_repo.lock_attendance_parent_rows)
        enrollment_source = inspect.getsource(enroll_repo.lock_enrollments_by_ids_with_lecture)
        bulk_source = inspect.getsource(AttendanceViewSet.bulk_set_present)
        undo_source = inspect.getsource(AttendanceViewSet.bulk_undo_present)

        self.assertIn("self._lock_attendance_rows", object_source)
        self.assertIn("self.filter_queryset(self.get_queryset())", object_source)
        self.assertIn("self.check_object_permissions", object_source)
        for method in (AttendanceViewSet.update, AttendanceViewSet.partial_update, AttendanceViewSet.destroy):
            source = inspect.getsource(method)
            self.assertIn("@transaction.atomic", source)
            self.assertIn("self.get_object()", source)
        for source in (bulk_source, undo_source):
            self.assertIn("@transaction.atomic", source)
            self.assertIn("self._lock_attendance_rows", source)

        self.assertLess(lock_source.index("lock_attendance_parent_rows"), lock_source.index("select_for_update"))
        self.assertIn('select_for_update(of=("self",))', lock_source)
        self.assertIn("queryset.filter(id__in=snapshot)", lock_source)
        self.assertLess(parent_source.index("Student.objects.select_for_update"), parent_source.index("lock_enrollments"))
        self.assertIn('select_for_update(of=("self",))', enrollment_source)
        self.assertIn('status="SECESSION"', bulk_source)
        self.assertIn('enrollment__status="INACTIVE"', bulk_source)
        self.assertLess(bulk_source.index("self._lock_attendance_rows"), bulk_source.index('.update(status="PRESENT")'))
        self.assertLess(undo_source.index("self._lock_attendance_rows"), undo_source.index('row.status != "PRESENT"'))
        self.assertLess(undo_source.index('row.status != "PRESENT"'), undo_source.index("Attendance.objects.bulk_update"))
        for method in (AttendanceViewSet.update, AttendanceViewSet.partial_update):
            self.assertIn("_secession_status_conflict", inspect.getsource(method))
