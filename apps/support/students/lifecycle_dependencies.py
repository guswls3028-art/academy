"""Cross-domain lifecycle dependencies for students."""

from __future__ import annotations

from typing import Any


def ensure_parent_for_student(
    *,
    tenant: Any,
    parent_phone: str,
    student_name: str,
) -> Any | None:
    from apps.domains.parents.services import ensure_parent_for_student as _ensure_parent

    return _ensure_parent(
        tenant=tenant,
        parent_phone=parent_phone,
        student_name=student_name,
    )


def ensure_parent_account_for_student(
    *,
    tenant: Any,
    parent_phone: str,
    student_name: str,
    initial_password: str | None = None,
) -> Any:
    from apps.domains.parents.services import ensure_parent_account_for_student as _ensure_parent_account

    return _ensure_parent_account(
        tenant=tenant,
        parent_phone=parent_phone,
        student_name=student_name,
        initial_password=initial_password,
    )


def parent_for_password_reset(*, tenant_id: int, phone: str) -> Any | None:
    from apps.domains.parents.models import Parent

    return Parent.objects.filter(tenant_id=int(tenant_id), phone=phone).first()


def locked_parent_account_by_phone_for_registration(*, tenant_id: int, phone: str) -> Any | None:
    from apps.domains.parents.models import Parent

    return (
        Parent.objects.select_for_update()
        .filter(tenant_id=int(tenant_id), phone=phone)
        .first()
    )


def locked_parent_account_for_registration(*, tenant_id: int, parent_id: int) -> Any | None:
    from apps.domains.parents.models import Parent

    return (
        Parent.objects.select_for_update()
        .filter(tenant_id=int(tenant_id), pk=int(parent_id))
        .first()
    )


def deactivate_enrollments_for_student(*, tenant: Any, student: Any) -> int:
    from apps.domains.enrollment.services.lifecycle import deactivate_enrollments_for_student as _deactivate

    return _deactivate(tenant=tenant, student=student)


def restore_enrollments_after_student_restore(*, tenant: Any, student: Any):
    from apps.domains.enrollment.services.lifecycle import (
        restore_enrollments_after_student_restore as _restore,
    )

    return _restore(tenant=tenant, student=student)


def cancel_active_participants_for_student(
    *,
    tenant: Any,
    student: Any,
    changed_at: Any,
) -> int:
    from apps.domains.clinic.services.lifecycle import cancel_active_participants_for_student as _cancel

    return _cancel(tenant=tenant, student=student, changed_at=changed_at)


def update_inventory_student_ps(*, tenant: Any, old_ps: str, new_ps: str) -> None:
    from apps.domains.inventory.models import InventoryFile, InventoryFolder

    InventoryFolder.objects.filter(tenant=tenant, student_ps=old_ps).update(student_ps=new_ps)
    InventoryFile.objects.filter(tenant=tenant, student_ps=old_ps).update(student_ps=new_ps)


def delete_submission_storage_for_permanent_delete(
    *,
    tenant_id: int,
    submission_ids: list[int],
    wrong_note_pdf_ids: list[int] | tuple[int, ...] = tuple(),
) -> tuple[int, ...]:
    from apps.domains.submissions.services.lifecycle import (
        delete_submission_storage_for_permanent_delete as _delete_submission_storage,
    )

    return _delete_submission_storage(
        tenant_id=tenant_id,
        submission_ids=submission_ids,
        wrong_note_pdf_ids=wrong_note_pdf_ids,
    )


def submission_storage_cleanup_status_counts(
    *,
    intent_ids: tuple[int, ...],
) -> tuple[int, int]:
    from apps.domains.submissions.models import SubmissionStorageCleanupIntent

    if not intent_ids:
        return 0, 0
    rows = SubmissionStorageCleanupIntent.objects.filter(id__in=intent_ids).values_list(
        "status",
        flat=True,
    )
    pending = 0
    failed = 0
    for status in rows:
        if status == SubmissionStorageCleanupIntent.Status.FAILED:
            failed += 1
        elif status != SubmissionStorageCleanupIntent.Status.CLEANED:
            pending += 1
    return pending, failed


def process_pending_submission_storage_cleanup(
    *,
    intent_ids: tuple[int, ...] | None = None,
    limit: int = 100,
):
    from apps.domains.submissions.services.lifecycle import (
        process_submission_storage_cleanup_intents,
    )

    return process_submission_storage_cleanup_intents(
        intent_ids=intent_ids,
        limit=limit,
    )


def submission_storage_reference_fields() -> frozenset[tuple[str, str, str, str]]:
    from apps.domains.submissions.services.lifecycle import (
        STORAGE_OBJECT_REFERENCE_FIELDS,
    )

    return STORAGE_OBJECT_REFERENCE_FIELDS


def active_wrong_note_pdf_exists_for_students(
    *,
    tenant: Any,
    student_ids: tuple[int, ...],
) -> bool:
    from apps.domains.results.models import WrongNotePDF

    return WrongNotePDF.objects.filter(
        enrollment__tenant=tenant,
        enrollment__student_id__in=student_ids,
        status__in=[
            WrongNotePDF.Status.PENDING,
            WrongNotePDF.Status.RUNNING,
        ],
    ).exists()
