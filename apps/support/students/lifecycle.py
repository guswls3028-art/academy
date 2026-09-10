"""Stable cross-domain entry points for the canonical student lifecycle."""

from __future__ import annotations

from typing import Any


def soft_delete_student(student: Any, *, tenant: Any):
    from apps.domains.students.services import soft_delete_student as _soft_delete

    return _soft_delete(student, tenant=tenant)


def permanently_delete_students(*, tenant: Any, student_ids: list[int]):
    from apps.domains.students.services import (
        permanently_delete_students as _permanently_delete,
    )

    return _permanently_delete(tenant=tenant, student_ids=student_ids)
