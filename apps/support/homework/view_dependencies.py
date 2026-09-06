"""Cross-domain dependencies for homework views."""

from __future__ import annotations

from typing import Any


def get_homework_for_assignment(
    *,
    homework_id: int,
    tenant: Any,
    for_update: bool = False,
) -> Any:
    from apps.domains.homework_results.models import Homework

    queryset = (
        Homework.objects.select_related(
            "session",
            "session__lecture",
        )
        .exclude(
            meta__removed_from_session_at__isnull=False,
        )
        .filter(
            id=homework_id,
            session__lecture__tenant=tenant,
        )
    )
    if for_update:
        queryset = queryset.select_for_update(of=("self",))
    return queryset.get()


def active_enrollment_ids_for_session(*, tenant: Any, session_id: int) -> set[int]:
    from apps.domains.lectures.models import Session
    from apps.support.attendance.learning_todo_eligibility import (
        eligible_learning_todo_enrollment_ids,
    )

    session = Session.objects.filter(
        id=int(session_id),
        lecture__tenant=tenant,
    ).select_related("lecture").first()
    if session is None:
        return set()
    return eligible_learning_todo_enrollment_ids(
        tenant=tenant,
        session=session,
    )


def active_session_enrollments_for_session(*, tenant: Any, session_id: int):
    from apps.support.attendance.learning_todo_eligibility import (
        eligible_session_enrollments,
    )

    return eligible_session_enrollments(
        tenant=tenant,
        session_id=session_id,
    )


def session_exists_for_tenant(*, session_id: int, tenant: Any) -> bool:
    from apps.domains.lectures.models import Session

    return Session.objects.filter(id=session_id, lecture__tenant=tenant).exists()


def get_session_for_homework_enrollment(
    *,
    session_id: int,
    tenant: Any,
    for_update: bool = False,
) -> Any | None:
    """Return the tenant-owned session that serializes roster replacement."""
    from apps.domains.lectures.models import Session

    queryset = Session.objects.filter(
        id=session_id,
        lecture__tenant=tenant,
    )
    if for_update:
        queryset = queryset.select_for_update(
            no_key=True,
            of=("self",),
        )
    return queryset.first()


def recalc_scores_for_policy_change(*, policy: Any) -> None:
    from apps.domains.homework_results.services.policy_recalc import recalc_scores_for_policy_change as _recalc

    _recalc(policy=policy)


def recalc_scores_for_homework_change(*, homework: Any) -> None:
    from apps.domains.homework_results.services.policy_recalc import (
        recalc_scores_for_homework_change as _recalc,
    )

    _recalc(homework=homework)


def minimum_live_homework_max_score(*, session: Any) -> tuple[float, str] | None:
    from apps.domains.homework_results.models import Homework

    homeworks = (
        Homework.objects
        .filter(
            session=session,
            cutline_mode__isnull=True,
            cutline_value__isnull=True,
        )
        .exclude(meta__removed_from_session_at__isnull=False)
        .order_by("id")
    )
    values = [
        (homework.default_max_score, str(homework.title))
        for homework in homeworks
    ]
    return min(values, key=lambda item: item[0]) if values else None
