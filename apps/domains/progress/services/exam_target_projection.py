"""Atomically refresh existing projections when effective exam targets change."""

from contextlib import contextmanager
import logging

from django.db import connection
from django.db.models import F
from django.utils import timezone

from apps.domains.progress.models import ProgressPolicy, SessionProgress
from apps.domains.progress.services.lecture_calculator import LectureProgressCalculator
from apps.domains.progress.services.risk_evaluator import RiskEvaluator
from apps.domains.progress.services.session_calculator import SessionProgressCalculator
from apps.support.progress.exam_target_projection_dependencies import (
    lock_target_projection_enrollments,
    target_pairs_for_exam,
)

logger = logging.getLogger(__name__)

EXAM_PROJECTION_FIELDS = (
    "exam_attempted", "exam_aggregate_score", "exam_passed", "exam_meta",
    "completed", "completed_at",
)


@contextmanager
def refresh_exam_target_projections(*, exam):
    """Source writer must own its exam/session lock and enclosing transaction.

    There is deliberately no on-commit callback: a failed projection rolls back
    the source edit too. No grading, clinic, correction, risk-log or delivery
    service runs here. Existing attendance/homework/manual metadata stay intact.
    The yielded set is populated after the source edit with effective
    ``(enrollment_id, session_id)`` targets that were removed. Callers may use
    it after the context exits to audit-close source-specific state.
    """
    if not connection.in_atomic_block:
        raise RuntimeError("exam target projection refresh requires a source transaction")
    before = target_pairs_for_exam(exam)
    removed: set[tuple[int, int]] = set()
    yield removed
    after = target_pairs_for_exam(exam)
    removed.update(before - after)
    changed = before.symmetric_difference(after)
    if not changed:
        return
    locked = lock_target_projection_enrollments(
        tenant_id=exam.tenant_id,
        enrollment_ids={enrollment_id for enrollment_id, _ in changed},
    )
    rows = SessionProgress.objects.filter(
        enrollment_id__in=locked,
        session_id__in={session_id for _, session_id in changed},
        enrollment__lecture_id=F("session__lecture_id"),
        enrollment__tenant_id=exam.tenant_id,
        session__lecture__tenant_id=exam.tenant_id,
    ).select_related("session__lecture").select_for_update(of=("self",)).order_by("enrollment_id", "session_id")
    lectures = {}
    found = 0
    for row in rows:
        if (row.enrollment_id, row.session_id) not in changed:
            continue
        found += 1
        previous = tuple(getattr(row, field) for field in EXAM_PROJECTION_FIELDS)
        policy = ProgressPolicy.objects.get(lecture_id=row.session.lecture_id)
        SessionProgressCalculator.set_exam_fields(obj=row, session=row.session, policy=policy)
        SessionProgressCalculator.set_completion_fields(row)
        if tuple(getattr(row, field) for field in EXAM_PROJECTION_FIELDS) == previous:
            continue
        row.calculated_at = timezone.now()
        row.save(update_fields=[*EXAM_PROJECTION_FIELDS, "calculated_at", "updated_at"])
        lectures[row.enrollment_id] = row.session.lecture

    for enrollment_id, lecture in lectures.items():
        summary = LectureProgressCalculator.calculate(enrollment_id=enrollment_id, lecture=lecture)
        summary.risk_level = RiskEvaluator.level_for_consecutive_failures(summary.consecutive_failed_sessions)
        summary.save(update_fields=["risk_level", "updated_at"])
    logger.info(
        "exam target projection refresh tenant=%s exam=%s affected=%s existing=%s absent=%s",
        exam.tenant_id, exam.id, len(changed), found, len(changed) - found,
    )
