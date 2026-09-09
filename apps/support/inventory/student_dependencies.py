"""Student ownership boundary used by inventory workflows."""

from __future__ import annotations

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


def student_storage_namespace_has_legacy_conflict(
    *,
    tenant_id: int,
    student_id: int,
    ps_number: str,
) -> bool:
    from apps.domains.inventory.models import InventoryFile, InventoryFolder
    from apps.domains.students.models import Student

    active_student = (
        Student.objects.filter(
            id=int(student_id),
            tenant_id=int(tenant_id),
            ps_number=ps_number,
            deleted_at__isnull=True,
        )
        .only("id", "created_at")
        .first()
    )
    if active_student is None:
        return True
    metadata_filters = {
        "tenant_id": int(tenant_id),
        "scope": "student",
        "student_ps": ps_number,
    }
    folders = InventoryFolder.objects.filter(**metadata_filters)
    files = InventoryFile.objects.filter(**metadata_filters)
    if not folders.exists() and not files.exists():
        return False
    return folders.filter(created_at__lt=active_student.created_at).exists() or files.filter(
        created_at__lt=active_student.created_at
    ).exists()
