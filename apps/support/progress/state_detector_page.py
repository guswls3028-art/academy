"""Bounded page-local source reads; never shared across snapshots or tenants."""

from collections import defaultdict
import time

from apps.domains.exams.models import ExamEnrollment, ExamLecturePolicy
from apps.domains.progress.models import ClinicLink, ProgressPolicy
from apps.domains.results.models import ExamAttempt, Result
from apps.domains.results.utils.session_exam import get_all_exams_for_session, get_exam_ids_for_session
from apps.domains.submissions.models import Submission
from apps.support.progress.session_calculator_dependencies import target_exam_ids_from_rows

BATCH_SOURCE_LIMIT = 10000


class StateDetectorPage:
    def __init__(self, rows, *, source_limit, failure, deadline):
        self.source_limit = source_limit
        self.failure = failure
        self.deadline = deadline
        rows = [row for row in rows if row.enrollment.status == "ACTIVE" and not row.enrollment.student.deleted_at]
        sessions = {row.session_id: row.session for row in rows}
        enrollment_ids = {row.enrollment_id for row in rows}
        lecture_ids = {session.lecture_id for session in sessions.values()}
        self.policies = {row.lecture_id: row for row in self._read(ProgressPolicy.objects.filter(lecture_id__in=lecture_ids))}
        self.exams = {}
        self.live_ids = {}
        exam_count = 0
        for session_id, session in sessions.items():
            self._check_budget()
            self.exams[session_id] = self.check_group(list(get_all_exams_for_session(session).order_by("id")[:source_limit + 1]))
            exam_count += len(self.exams[session_id])
            if exam_count > BATCH_SOURCE_LIMIT:
                raise failure("page_source_limit_exceeded")
            self.live_ids[session_id] = get_exam_ids_for_session(session)
        exam_ids = {exam.pk for group in self.exams.values() for exam in group}
        self.targets = self._group(
            self._read(ExamEnrollment.objects.filter(exam_id__in=exam_ids).select_related("enrollment").order_by("id")),
            lambda row: row.exam_id,
        )
        self.links = self._group(
            self._read(ClinicLink.objects.filter(enrollment_id__in=enrollment_ids, session_id__in=sessions).order_by("-cycle_no", "-id")),
            lambda row: (row.enrollment_id, row.session_id),
        )
        self.attempts = self._group(
            self._read(ExamAttempt.objects.filter(enrollment_id__in=enrollment_ids, exam_id__in=exam_ids).order_by("exam_id", "-attempt_index")),
            lambda row: row.enrollment_id,
        )
        self.submissions = self._group(
            self._read(Submission.objects.filter(enrollment_id__in=enrollment_ids, target_type="exam", target_id__in=exam_ids).order_by("id")),
            lambda row: row.enrollment_id,
        )
        self.results = self._group(
            self._read(Result.objects.filter(enrollment_id__in=enrollment_ids, target_type="exam", target_id__in=exam_ids).select_related("attempt").order_by("id")),
            lambda row: row.enrollment_id,
        )
        self.overrides = self._group(
            self._read(ExamLecturePolicy.objects.filter(lecture_id__in=lecture_ids, exam_id__in=exam_ids).order_by("id")),
            lambda row: row.lecture_id,
        )

    def _read(self, queryset):
        self._check_budget()
        rows = list(queryset[:BATCH_SOURCE_LIMIT + 1])
        self._check_budget()
        if len(rows) > BATCH_SOURCE_LIMIT:
            raise self.failure("page_source_limit_exceeded")
        return rows

    def _check_budget(self):
        if time.monotonic() > self.deadline:
            raise self.failure("scan_timeout")

    def check_group(self, rows):
        if len(rows) > self.source_limit:
            raise self.failure("source_limit_exceeded")
        return rows

    @staticmethod
    def _group(rows, key):
        groups = defaultdict(list)
        for row in rows:
            groups[key(row)].append(row)
        return groups

    def target_rows(self, session_id):
        return self.check_group([
            target for exam in self.exams[session_id] for target in self.targets[exam.id]
        ])

    def target_ids(self, *, session_id, enrollment_id):
        return target_exam_ids_from_rows(
            exam_ids=self.live_ids[session_id],
            explicit_rows=[(row.exam_id, row.enrollment_id) for row in self.target_rows(session_id)],
            enrollment_id=enrollment_id,
        )
