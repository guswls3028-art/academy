"""Shared cross-domain dependencies for admin exam result endpoints."""

from __future__ import annotations

from typing import Any

from django.shortcuts import get_object_or_404


def regular_active_exam_with_session_exists(*, exam_id: int, tenant: Any) -> bool:
    from apps.domains.exams.models import Exam

    return Exam.objects.filter(
        id=exam_id,
        tenant=tenant,
        exam_type=Exam.ExamType.REGULAR,
        is_active=True,
        sessions__lecture__tenant=tenant,
    ).exists()


def regular_active_exam_ids_for_tenant(*, tenant: Any) -> list[int]:
    from apps.domains.exams.models import Exam

    return list(
        Exam.objects.filter(
            tenant=tenant,
            exam_type=Exam.ExamType.REGULAR,
            is_active=True,
            sessions__lecture__tenant=tenant,
        )
        .values_list("id", flat=True)
        .distinct()
    )


def get_regular_active_exam_for_tenant_or_none(*, exam_id: int, tenant: Any) -> Any | None:
    from apps.domains.exams.models import Exam

    return Exam.objects.filter(
        id=exam_id,
        tenant=tenant,
        exam_type=Exam.ExamType.REGULAR,
        is_active=True,
    ).first()


def get_regular_active_exam_for_tenant(*, exam_id: int, tenant: Any) -> Any:
    from apps.domains.exams.models import Exam

    return get_object_or_404(
        Exam.objects.filter(
            tenant=tenant,
            exam_type=Exam.ExamType.REGULAR,
            is_active=True,
            sessions__lecture__tenant=tenant,
        ).distinct(),
        id=exam_id,
    )


def lock_regular_active_exam_for_tenant(*, exam_id: int, tenant: Any) -> Any:
    """Lock one tenant-owned exam before reading mutable score policy."""
    from django.http import Http404

    from apps.domains.exams.models import Exam

    exam = get_object_or_404(
        Exam.objects.select_for_update().filter(
            tenant=tenant,
            exam_type=Exam.ExamType.REGULAR,
            is_active=True,
        ),
        id=int(exam_id),
    )
    if not exam.sessions.filter(lecture__tenant=tenant).exists():
        raise Http404
    return exam


def get_enrollment_for_tenant(*, enrollment_id: int, tenant: Any) -> Any | None:
    from apps.domains.enrollment.models import Enrollment

    return Enrollment.objects.filter(id=enrollment_id, tenant=tenant).first()


def lock_enrollment_for_exam_state_transition(
    *,
    enrollment_id: int,
    tenant: Any,
) -> Any:
    from apps.domains.enrollment.models import Enrollment

    return Enrollment.objects.select_for_update().get(
        id=int(enrollment_id),
        tenant=tenant,
    )


def enrollment_exists_for_tenant(*, enrollment_id: int, tenant: Any) -> bool:
    from apps.domains.enrollment.models import Enrollment

    return Enrollment.objects.filter(id=int(enrollment_id), tenant=tenant).exists()


def active_enrollment_exists_for_student(
    *,
    enrollment_id: int,
    tenant: Any,
    student_id: int,
) -> bool:
    from apps.domains.enrollment.models import Enrollment

    return Enrollment.objects.filter(
        id=int(enrollment_id),
        tenant=tenant,
        student_id=int(student_id),
        status="ACTIVE",
        student__deleted_at__isnull=True,
    ).exists()


def enrollment_ids_for_tenant(*, tenant_id: int) -> set[int]:
    from apps.domains.enrollment.models import Enrollment

    return set(
        Enrollment.objects.filter(tenant_id=int(tenant_id)).values_list("id", flat=True)
    )


def get_enrollments_for_tenant_by_id(*, enrollment_ids: list[int], tenant: Any) -> dict[int, Any]:
    from apps.domains.enrollment.models import Enrollment

    return {
        int(enrollment.id): enrollment
        for enrollment in Enrollment.objects
        .filter(id__in=enrollment_ids, tenant=tenant)
        .select_related("student", "lecture")
    }


def get_latest_exam_submission_id(*, enrollment_id: int, exam_id: int) -> int | None:
    from apps.domains.submissions.models import Submission

    submission = (
        Submission.objects.filter(
            enrollment_id=enrollment_id,
            target_type=Submission.TargetType.EXAM,
            target_id=exam_id,
        )
        .order_by("-id")
        .first()
    )
    return int(submission.id) if submission else None


def get_latest_session_submission_id(*, enrollment_id: int, session_id: int) -> int | None:
    from apps.domains.submissions.models import Submission

    submission = (
        Submission.objects
        .filter(enrollment_id=enrollment_id, session_id=session_id)
        .order_by("-id")
        .first()
    )
    return int(submission.id) if submission else None


def get_submission_status_by_id_for_tenant(*, submission_ids: list[int], tenant: Any) -> dict[int, Any]:
    from apps.domains.submissions.models import Submission

    if not submission_ids:
        return {}
    return {
        int(submission.id): submission.status
        for submission in Submission.objects.filter(id__in=submission_ids, tenant=tenant)
    }


def dispatch_progress_pipeline(**kwargs: Any) -> Any:
    from apps.domains.progress.dispatcher import dispatch_progress_pipeline as _dispatch

    return _dispatch(**kwargs)


def resolve_exam_not_submitted_clinic_links(**kwargs: Any) -> int:
    from apps.domains.progress.dispatcher import (
        resolve_exam_not_submitted_clinic_links as _resolve,
    )

    return _resolve(**kwargs)
