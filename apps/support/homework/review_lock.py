"""Shared serialization lock for homework evidence and teacher decisions."""

from __future__ import annotations

from typing import Any


def lock_homework_review_target(
    *,
    tenant: Any,
    enrollment_id: int,
    homework_id: int,
    session_id: int | None = None,
):
    """Lock the canonical assignment before touching review-dependent state."""
    from apps.domains.homework.models import HomeworkAssignment

    filters = {
        "tenant": tenant,
        "enrollment_id": int(enrollment_id),
        "homework_id": int(homework_id),
    }
    if session_id is not None:
        filters["session_id"] = int(session_id)
    return (
        HomeworkAssignment.objects.select_for_update()
        .only("id", "session_id", "homework_id", "enrollment_id")
        .filter(**filters)
        .first()
    )


__all__ = ["lock_homework_review_target"]
