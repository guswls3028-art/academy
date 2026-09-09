from __future__ import annotations

from typing import Iterable, Optional

from apps.domains.submissions.models import Submission, SubmissionMedia
from apps.domains.submissions.services.transition import (
    InvalidTransitionError,
    bulk_transit,
    can_transit,
    transit,
    transit_save,
)

S = Submission.Status

IN_PROGRESS_STATUSES: tuple[str, ...] = (
    S.SUBMITTED,
    S.DISPATCHED,
    S.EXTRACTING,
    S.ANSWERS_READY,
    S.GRADING,
)

CASCADE_DISCARD_STATUSES: tuple[str, ...] = (
    S.SUBMITTED,
    S.DISPATCHED,
    S.EXTRACTING,
    S.NEEDS_IDENTIFICATION,
    S.ANSWERS_READY,
    S.GRADING,
    S.FAILED,
)

OMR_CONFLICT_STATUSES: tuple[str, ...] = (
    S.SUBMITTED,
    S.DISPATCHED,
    S.EXTRACTING,
    S.NEEDS_IDENTIFICATION,
    S.ANSWERS_READY,
    S.GRADING,
    S.DONE,
)

STUCK_RECOVERABLE_STATUSES: tuple[str, ...] = (
    S.SUBMITTED,
    S.DISPATCHED,
    S.EXTRACTING,
    S.GRADING,
)


def delete_submission_storage_for_permanent_delete(
    *,
    tenant_id: int,
    submission_ids: Iterable[int],
) -> None:
    """Delete exact submission-owned objects and media rows before raw parent deletion.

    The caller owns the surrounding database transaction. Object keys still
    referenced by any non-target submission or media row are deliberately kept.
    """
    ids = tuple(dict.fromkeys(int(value) for value in submission_ids if int(value) > 0))
    if not ids:
        return

    submissions = list(
        Submission.objects.select_for_update()
        .filter(tenant_id=tenant_id, id__in=ids)
        .only("id", "file_key")
        .order_by("id")
    )
    exact_submission_ids = tuple(submission.id for submission in submissions)
    if not exact_submission_ids:
        return
    if SubmissionMedia.objects.filter(submission_id__in=exact_submission_ids).exclude(
        tenant_id=tenant_id
    ).exists():
        raise ValueError("submission media tenant does not match its parent submission")

    media = list(
        SubmissionMedia.objects.select_for_update()
        .filter(tenant_id=tenant_id, submission_id__in=exact_submission_ids)
        .only("id", "object_key")
        .order_by("id")
    )
    media_ids = tuple(item.id for item in media)
    candidate_keys = {
        str(key).strip()
        for key in [
            *(submission.file_key for submission in submissions),
            *(item.object_key for item in media),
        ]
        if str(key or "").strip() not in {"", "pending"}
    }
    shared_keys: set[str] = set()
    if candidate_keys:
        shared_keys.update(
            str(key)
            for key in Submission.objects.filter(file_key__in=candidate_keys)
            .exclude(id__in=exact_submission_ids)
            .values_list("file_key", flat=True)
        )
        outside_media = SubmissionMedia.objects.filter(object_key__in=candidate_keys)
        if media_ids:
            outside_media = outside_media.exclude(id__in=media_ids)
        shared_keys.update(str(key) for key in outside_media.values_list("object_key", flat=True))

        from apps.infrastructure.storage.r2 import delete_object_r2_storage

        for key in sorted(candidate_keys - shared_keys):
            delete_object_r2_storage(key=key)

    if media_ids:
        SubmissionMedia.objects.filter(
            tenant_id=tenant_id,
            submission_id__in=exact_submission_ids,
            id__in=media_ids,
        ).delete()


def mark_dispatched(
    submission: Submission,
    *,
    actor: str,
    extra_update_fields: Optional[list[str]] = None,
) -> None:
    transit_save(
        submission,
        S.DISPATCHED,
        actor=actor,
        extra_update_fields=extra_update_fields,
    )


def mark_answers_ready(
    submission: Submission,
    *,
    actor: str,
    admin_override: bool = False,
    extra_update_fields: Optional[list[str]] = None,
) -> None:
    transit_save(
        submission,
        S.ANSWERS_READY,
        actor=actor,
        admin_override=admin_override,
        extra_update_fields=extra_update_fields,
    )


def mark_answers_ready_in_memory(
    submission: Submission,
    *,
    actor: str,
    admin_override: bool = False,
) -> None:
    transit(
        submission,
        S.ANSWERS_READY,
        actor=actor,
        admin_override=admin_override,
    )


def mark_needs_identification(
    submission: Submission,
    *,
    actor: str,
    error_message: str = "",
) -> None:
    transit(
        submission,
        S.NEEDS_IDENTIFICATION,
        actor=actor,
        error_message=error_message,
    )


def mark_grading(submission: Submission, *, actor: str) -> None:
    transit_save(submission, S.GRADING, actor=actor)


def mark_done(submission: Submission, *, actor: str) -> None:
    transit_save(submission, S.DONE, actor=actor)


def can_mark_done(status: str) -> bool:
    return can_transit(status, S.DONE)


def can_fail_submission(status: str) -> bool:
    return can_transit(status, S.FAILED)


def fail_submission(
    submission: Submission,
    *,
    error_message: str,
    actor: str,
    admin_override: bool = False,
    extra_update_fields: Optional[list[str]] = None,
) -> None:
    transit_save(
        submission,
        S.FAILED,
        error_message=error_message,
        actor=actor,
        admin_override=admin_override,
        extra_update_fields=extra_update_fields,
    )


def fail_submission_in_memory(
    submission: Submission,
    *,
    error_message: str,
    actor: str,
) -> None:
    transit(submission, S.FAILED, error_message=error_message, actor=actor)


def retry_failed_submission(submission: Submission, *, actor: str) -> None:
    transit_save(submission, S.SUBMITTED, actor=actor)


def reopen_for_regrade(submission: Submission, *, actor: str) -> None:
    mark_answers_ready(submission, actor=actor, admin_override=True)


def reopen_for_regrade_in_memory(submission: Submission, *, actor: str) -> None:
    mark_answers_ready_in_memory(submission, actor=actor, admin_override=True)


def supersede_submission(submission: Submission, *, actor: str) -> None:
    transit_save(submission, S.SUPERSEDED, actor=actor)


def supersede_done_submissions(queryset, *, actor: str = "") -> int:
    # Bulk path is intentionally limited to DONE -> SUPERSEDED and keeps the
    # lower-level guard. Per-row audit belongs in the caller when needed.
    _ = actor
    return bulk_transit(queryset, S.SUPERSEDED, from_status=S.DONE)
