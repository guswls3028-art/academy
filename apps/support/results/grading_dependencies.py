"""Cross-domain dependencies for result grading and scope guards."""

from __future__ import annotations

from typing import Any

from django.db.models import F
from django.shortcuts import get_object_or_404


def get_enrollment_for_tenant(*, enrollment_id: int, tenant: Any) -> Any | None:
    from apps.domains.enrollment.models import Enrollment

    return Enrollment.objects.filter(id=int(enrollment_id), tenant=tenant).first()


def exam_enrollment_exists(*, exam_id: int, enrollment_id: int) -> bool:
    from apps.domains.exams.models import ExamEnrollment

    return ExamEnrollment.objects.filter(
        exam_id=int(exam_id),
        enrollment_id=int(enrollment_id),
    ).exists()


def exam_has_explicit_targets(*, exam_id: int) -> bool:
    from apps.domains.exams.models import ExamEnrollment

    return ExamEnrollment.objects.filter(exam_id=int(exam_id)).exists()


def linked_session_enrollment_exists(*, exam: Any, enrollment_id: int) -> bool:
    from apps.support.attendance.learning_todo_eligibility import (
        exam_is_learning_todo_eligible,
    )

    return exam_is_learning_todo_eligible(
        tenant_id=int(exam.tenant_id),
        enrollment_id=int(enrollment_id),
        exam_id=int(exam.id),
    )


def materialize_exam_enrollment_from_linked_session(*, exam: Any, enrollment_id: int) -> bool:
    from apps.domains.exams.models import ExamEnrollment

    if exam_has_explicit_targets(exam_id=exam.id):
        return False
    if not linked_session_enrollment_exists(exam=exam, enrollment_id=enrollment_id):
        return False

    ExamEnrollment.objects.get_or_create(
        exam_id=exam.id,
        enrollment_id=int(enrollment_id),
    )
    return True


def get_active_submission_enrollment(*, submission: Any) -> Any | None:
    from apps.domains.enrollment.models import Enrollment

    enrollment_id = getattr(submission, "enrollment_id", None)
    if not enrollment_id:
        return None
    return (
        Enrollment.objects
        .filter(
            id=int(enrollment_id),
            tenant_id=int(submission.tenant_id),
            status="ACTIVE",
            student__deleted_at__isnull=True,
        )
        .select_related("student", "lecture")
        .first()
    )


def submission_enrollment_assigned_to_exam(*, exam_id: int, enrollment_id: int, tenant_id: int) -> bool:
    from apps.domains.exams.models import ExamEnrollment

    return ExamEnrollment.objects.filter(
        exam_id=int(exam_id),
        enrollment_id=int(enrollment_id),
        enrollment__tenant_id=int(tenant_id),
        exam__tenant_id=int(tenant_id),
        exam__sessions__lecture_id=F("enrollment__lecture_id"),
        exam__sessions__lecture__tenant_id=int(tenant_id),
    ).exists()


def get_submission_for_grading(*, submission_id: int) -> Any | None:
    from apps.domains.submissions.models import Submission

    return Submission.objects.filter(id=int(submission_id)).only(
        "id",
        "source",
        "meta",
    ).first()


def is_omr_manual_review_required(submission: Any) -> bool:
    if not submission:
        return False

    from apps.domains.submissions.models import Submission

    return bool(
        submission.source == Submission.Source.OMR_SCAN
        and isinstance(submission.meta, dict)
        and isinstance(submission.meta.get("manual_review"), dict)
        and submission.meta["manual_review"].get("required") is True
    )


def dispatch_progress_pipeline(**kwargs: Any) -> Any:
    from apps.domains.progress.dispatcher import dispatch_progress_pipeline as _dispatch

    return _dispatch(**kwargs)


def get_submission_for_result_sync(*, submission_id: int) -> Any:
    from apps.domains.submissions.models import Submission

    return get_object_or_404(
        Submission.objects.select_related("user"),
        id=int(submission_id),
    )


def has_confirmed_current_omr_identity(
    *,
    tenant_id: int,
    exam_id: int,
    enrollment_id: int,
    submission_id: int,
) -> bool:
    from apps.domains.submissions.models import OMRStudentMatch

    return OMRStudentMatch.objects.filter(
        tenant_id=int(tenant_id),
        submission_id=int(submission_id),
        submission__tenant_id=int(tenant_id),
        submission__target_type="exam",
        submission__target_id=int(exam_id),
        submission__source="omr_scan",
        submission__enrollment_id=int(enrollment_id),
        enrollment_id=int(enrollment_id),
        status=OMRStudentMatch.Status.CONFIRMED,
        is_current=True,
    ).exists()


def get_exam_for_result_sync(*, exam_id: int) -> Any:
    from apps.domains.exams.models import Exam

    return get_object_or_404(Exam, id=int(exam_id))
