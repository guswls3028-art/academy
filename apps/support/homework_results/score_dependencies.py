"""Cross-domain helpers for homework score updates."""

from __future__ import annotations

from typing import Any


def validate_enrollment_belongs_to_tenant(enrollment_id: int, tenant: Any) -> None:
    from apps.domains.results.guards.enrollment_tenant_guard import (
        validate_enrollment_belongs_to_tenant as validate,
    )

    validate(enrollment_id, tenant)


def calc_homework_passed_and_clinic(
    *,
    session: Any,
    homework: Any | None = None,
    score: float | None,
    max_score: float | None,
):
    from apps.domains.homework.utils.homework_policy import (
        calc_homework_passed_and_clinic as calculate,
    )

    return calculate(
        session=session,
        homework=homework,
        score=score,
        max_score=max_score,
    )


def latest_homework_submission(*, enrollment_id: int, homework_id: int):
    from apps.domains.submissions.models import Submission

    return (
        Submission.objects
        .filter(
            enrollment_id=enrollment_id,
            target_type="homework",
            target_id=homework_id,
        )
        .order_by("-id")
        .first()
    )


def dispatch_progress_pipeline(*, submission_id: int) -> None:
    from apps.domains.progress.dispatcher import dispatch_progress_pipeline as dispatch

    dispatch(submission_id=submission_id)


def resolve_homework_clinic_pass(
    *,
    enrollment_id: int,
    session_id: int,
    homework_id: int,
    score: float | None,
    max_score: float | None,
) -> None:
    from apps.domains.progress.services.clinic_resolution_service import (
        ClinicResolutionService,
    )

    ClinicResolutionService.resolve_by_homework_pass(
        enrollment_id=int(enrollment_id),
        session_id=int(session_id),
        homework_id=int(homework_id),
        score=score,
        max_score=max_score,
    )


def homework_assignment_exists(
    *,
    tenant: Any,
    homework: Any,
    session: Any,
    enrollment_id: int,
) -> bool:
    from apps.domains.homework.models import HomeworkAssignment

    return HomeworkAssignment.objects.filter(
        tenant=tenant,
        homework=homework,
        session=session,
        enrollment_id=enrollment_id,
    ).exists()


def sync_homework_clinic_link(
    *,
    enrollment_id: int,
    session: Any,
    homework_id: int,
    passed: bool,
    score: float | None,
    max_score: float | None,
) -> None:
    from django.db import IntegrityError as DjangoIntegrityError
    from django.db.models import Max

    from apps.domains.enrollment.models import Enrollment
    from apps.domains.progress.models import ClinicLink
    from apps.domains.progress.services.clinic_resolution_service import (
        ClinicResolutionService,
    )

    if passed:
        ClinicResolutionService.resolve_by_homework_pass(
            enrollment_id=enrollment_id,
            session_id=session.id,
            homework_id=homework_id,
            score=score,
            max_score=max_score,
        )
        return

    from apps.support.attendance.learning_todo_eligibility import (
        enrollment_session_is_learning_todo_eligible,
    )

    tenant_id = (
        Enrollment.objects
        .filter(id=enrollment_id)
        .values_list("tenant_id", flat=True)
        .first()
    )
    if tenant_id is None or not enrollment_session_is_learning_todo_eligible(
        tenant_id=int(tenant_id),
        enrollment_id=int(enrollment_id),
        session_id=int(session.id),
    ):
        return

    latest_link = (
        ClinicLink.objects.filter(
            enrollment_id=enrollment_id,
            session=session,
            source_type="homework",
            source_id=homework_id,
        )
        .order_by("-cycle_no", "-id")
        .first()
    )
    if (
        latest_link
        and latest_link.resolved_at
        and latest_link.resolution_type == ClinicLink.ResolutionType.MANUAL_OVERRIDE
        and (latest_link.resolution_evidence or {}).get("assessment_correction_id")
    ):
        # 현장 협의 완료는 원점수와 독립적이다. 이후 점수 저장이 완료 판정을
        # 덮거나 새 Clinic 대상을 만들면 안 된다. 교사가 미완료로 되돌릴 때
        # resolution service가 이 링크를 다시 연다.
        return

    existing_unresolved = ClinicLink.objects.filter(
        enrollment_id=enrollment_id,
        session=session,
        source_type="homework",
        source_id=homework_id,
        resolved_at__isnull=True,
    ).exists()
    if existing_unresolved:
        return

    max_cycle = (
        ClinicLink.objects
        .filter(
            enrollment_id=enrollment_id,
            session=session,
            source_type="homework",
            source_id=homework_id,
        )
        .aggregate(Max("cycle_no"))["cycle_no__max"]
        or 0
    )
    try:
        ClinicLink.objects.create(
            enrollment_id=enrollment_id,
            session=session,
            source_type="homework",
            source_id=homework_id,
            reason=ClinicLink.Reason.AUTO_FAILED,
            is_auto=True,
            approved=False,
            cycle_no=max(max_cycle + 1, 1),
            tenant_id=tenant_id,
            meta={
                "kind": "HOMEWORK_FAILED",
                "homework_id": homework_id,
                "score": score,
                "max_score": max_score,
            },
        )
    except DjangoIntegrityError:
        pass
