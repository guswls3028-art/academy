"""Student ownership boundary used by inventory workflows."""

from __future__ import annotations

from typing import Any


def active_student_id_for_storage(*, tenant_id: int, ps_number: str) -> int | None:
    from apps.domains.students.models import Student

    return (
        Student.objects.filter(
            tenant_id=int(tenant_id),
            ps_number=ps_number,
            deleted_at__isnull=True,
        )
        .values_list("id", flat=True)
        .first()
    )


def soft_delete_student_for_storage(student: Any, *, tenant: Any):
    from apps.domains.students.services import soft_delete_student

    return soft_delete_student(student, tenant=tenant)


def permanently_delete_students_for_storage(
    *,
    tenant: Any,
    student_ids: list[int],
):
    from apps.domains.students.services import permanently_delete_students

    return permanently_delete_students(tenant=tenant, student_ids=student_ids)
