"""Cross-domain dependencies for community scope node reads."""

from __future__ import annotations

from typing import Any


def get_request_student(request: Any) -> Any | None:
    from apps.domains.student_app.permissions import get_request_student as _get_request_student

    return _get_request_student(request)


def active_lecture_ids_for_student(*, tenant: Any, student: Any):
    from apps.domains.enrollment.models import Enrollment

    return Enrollment.objects.filter(
        tenant=tenant,
        student=student,
        status="ACTIVE",
    ).values_list("lecture_id", flat=True)
