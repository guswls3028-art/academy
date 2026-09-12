from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Iterable, Optional

from django.apps import apps as django_apps
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from apps.domains.submissions.models import (
    Submission,
    SubmissionMedia,
    SubmissionStorageCleanupIntent,
)
from apps.domains.submissions.services.transition import (
    InvalidTransitionError,
    bulk_transit,
    can_transit,
    transit,
    transit_save,
)

S = Submission.Status
logger = logging.getLogger(__name__)

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

SUBMISSION_MEDIA_UPLOAD_LEASE = timedelta(hours=1)


@dataclass(frozen=True)
class SubmissionStorageCleanupResult:
    cleaned: int
    failed: int
    deferred: int


# Scalar database fields that own or retain object-store keys. This executable
# registry is intentionally complete; its focused contract test fails when a
# new storage-key field is introduced without shared-reference protection.
STORAGE_OBJECT_REFERENCE_FIELDS = frozenset({
    ("ai", "exams", "ExamAsset", "file_key"),
    ("ai", "submissions", "Submission", "file_key"),
    ("ai", "submissions", "SubmissionMedia", "object_key"),
    ("storage", "community", "PostAttachment", "r2_key"),
    ("storage", "exams", "ExamAsset", "file_key"),
    ("storage", "exams", "ExamQuestion", "image_key"),
    ("storage", "exams", "ExamQuestionProposal", "explanation_image_key"),
    ("storage", "exams", "ExamQuestionProposal", "problem_image_key"),
    ("storage", "exams", "QuestionExplanation", "image_key"),
    ("storage", "inventory", "InventoryFile", "r2_key"),
    ("storage", "landing_public", "PublicMatchupShowcase", "snapshot_pdf_key"),
    ("storage", "landing_public", "PublicProblemReviewShowcase", "snapshot_pdf_key"),
    ("storage", "matchup", "MatchupDocument", "r2_key"),
    ("storage", "matchup", "MatchupProblem", "image_key"),
    ("storage", "matchup", "ProblemSegmentationProposal", "image_key"),
    ("storage", "problem_studio", "ProblemReviewArtifact", "r2_key"),
    ("storage", "problem_studio", "ProblemStudioBetaRun", "checkpoint_key"),
    ("storage", "problem_studio", "ProblemStudioBetaRun", "result_key"),
    ("storage", "problem_studio", "ProblemStudioBetaRun", "solutions_key"),
    ("storage", "problem_studio", "ProblemStudioBetaRun", "source_archive_key"),
    ("storage", "problem_studio", "ProblemStudioFontAsset", "r2_key"),
    ("storage", "results", "WrongNotePDF", "file_path"),
    ("storage", "students", "Student", "profile_photo_r2_key"),
    ("video", "video", "Video", "file_key"),
    ("video", "video", "Video", "hls_path"),
    ("video", "video", "Video", "thumbnail_r2_key"),
})


def _submission_owned_object_key(
    *,
    tenant_id: int,
    key: str,
    submission_id: int | None = None,
) -> bool:
    prefix = f"tenants/{tenant_id}/ai/submissions/"
    if key.startswith(prefix) and len(key) > len(prefix):
        return True
    if submission_id is None:
        return False
    historical_prefix = f"submissions/{int(submission_id)}/"
    return key.startswith(historical_prefix) and len(key) > len(historical_prefix)


def _wrong_note_owned_object_key(*, tenant_id: int, key: str) -> bool:
    prefix = f"tenants/{tenant_id}/results/wrong-notes/"
    return key.startswith(prefix) and len(key) > len(prefix)


def _lock_object_key(*, bucket: str, key: str) -> None:
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("object-key lock requires an atomic transaction")
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                [f"academy-storage:{bucket}:{key}"],
            )


def ensure_ai_submission_key_attachable(*, tenant_id: int, key: str) -> None:
    """Serialize caller-supplied/reused AI-key attachment against cleanup."""
    _lock_object_key(bucket=SubmissionStorageCleanupIntent.Bucket.AI, key=key)
    if SubmissionStorageCleanupIntent.objects.filter(
        tenant_id=tenant_id,
        bucket=SubmissionStorageCleanupIntent.Bucket.AI,
        object_key=key,
    ).exists():
        raise ValueError("submission object key is already scheduled for cleanup")


def schedule_unreferenced_ai_object_cleanup(*, tenant_id: int, key: str) -> int | None:
    """Durably retry AI upload compensation when immediate deletion fails."""
    if not _submission_owned_object_key(tenant_id=tenant_id, key=key):
        raise ValueError("submission file key is outside its canonical namespace")
    with transaction.atomic():
        _lock_object_key(bucket=SubmissionStorageCleanupIntent.Bucket.AI, key=key)
        if _other_storage_owner_references(
            bucket=SubmissionStorageCleanupIntent.Bucket.AI,
            key=key,
        ):
            return None
        intent, _ = SubmissionStorageCleanupIntent.objects.get_or_create(
            tenant_id=tenant_id,
            bucket=SubmissionStorageCleanupIntent.Bucket.AI,
            object_key=key,
        )
        if intent.status == SubmissionStorageCleanupIntent.Status.CLEANED:
            intent.status = SubmissionStorageCleanupIntent.Status.PENDING
            intent.cleaned_at = None
            intent.last_error = ""
            intent.save(update_fields=["status", "cleaned_at", "last_error", "updated_at"])
        transaction.on_commit(
            lambda intent_ids=(intent.id,): _process_submission_storage_cleanup_safely(
                intent_ids
            )
        )
        return intent.id


def _object_key_is_still_referenced(*, bucket: str, key: str) -> bool:
    return _other_storage_owner_references(bucket=bucket, key=key)


def _other_storage_owner_references(
    *,
    bucket: str,
    key: str,
    excluded_submission_ids: tuple[int, ...] = tuple(),
    excluded_submission_media_ids: tuple[int, ...] = tuple(),
    excluded_wrong_note_pdf_ids: tuple[int, ...] = tuple(),
) -> bool:
    for field_bucket, app_label, model_name, field_name in STORAGE_OBJECT_REFERENCE_FIELDS:
        if field_bucket != bucket:
            continue
        model = django_apps.get_model(app_label, model_name)
        references = model._base_manager.filter(**{field_name: key})
        owner = (app_label, model_name)
        if owner == ("submissions", "Submission"):
            references = references.exclude(id__in=excluded_submission_ids)
        elif owner == ("submissions", "SubmissionMedia"):
            references = references.exclude(id__in=excluded_submission_media_ids)
        elif owner == ("results", "WrongNotePDF"):
            references = references.exclude(id__in=excluded_wrong_note_pdf_ids)
        if references.exists():
            return True
    if bucket == SubmissionStorageCleanupIntent.Bucket.STORAGE:
        # These image owners live in JSON rather than the scalar-key registry.
        # Inventory cascade cleanup includes both, so surviving documents and
        # problems must protect them just like their ordinary image_key fields.
        problem_model = django_apps.get_model("matchup", "MatchupProblem")
        if problem_model._base_manager.filter(
            meta__public_cleanup__public_image_key=key,
        ).exists():
            return True
        document_model = django_apps.get_model("matchup", "MatchupDocument")
        page_key_lists = document_model._base_manager.filter(
            meta__page_image_keys__icontains=key,
        ).values_list("meta__page_image_keys", flat=True)
        if any(isinstance(keys, list) and key in keys for keys in page_key_lists):
            return True
    return False


def _finish_claimed_storage_cleanup(
    *,
    intent_id: int,
    tenant_id: int,
    bucket: str,
    object_key: str,
    claim_token: uuid.UUID,
    attempted_at,
) -> str | None:
    """Finish one claimed intent while excluding concurrent key attachment."""
    with transaction.atomic():
        _lock_object_key(bucket=bucket, key=object_key)
        owned = SubmissionStorageCleanupIntent.objects.filter(
            id=intent_id,
            tenant_id=tenant_id,
            status=SubmissionStorageCleanupIntent.Status.PROCESSING,
            claim_token=claim_token,
        )
        if not owned.exists():
            return None
        if _object_key_is_still_referenced(bucket=bucket, key=object_key):
            updated = owned.update(
                status=SubmissionStorageCleanupIntent.Status.PENDING,
                claim_token=None,
                last_attempt_at=attempted_at,
                last_error="object_key_still_referenced",
                updated_at=attempted_at,
            )
            return "deferred" if updated == 1 else None
        try:
            from apps.infrastructure.storage.r2 import (
                delete_object_r2_ai,
                delete_object_r2_storage,
            )

            if bucket == SubmissionStorageCleanupIntent.Bucket.AI:
                delete_object_r2_ai(key=object_key)
            else:
                delete_object_r2_storage(key=object_key)
        except Exception:
            logger.warning(
                "Submission storage cleanup failed intent_id=%s",
                intent_id,
                exc_info=True,
            )
            updated = owned.update(
                status=SubmissionStorageCleanupIntent.Status.FAILED,
                claim_token=None,
                last_attempt_at=attempted_at,
                last_error="storage_delete_failed",
                updated_at=attempted_at,
            )
            return "failed" if updated == 1 else None
        updated = owned.update(
            status=SubmissionStorageCleanupIntent.Status.CLEANED,
            claim_token=None,
            last_attempt_at=attempted_at,
            last_error="",
            cleaned_at=attempted_at,
            updated_at=attempted_at,
        )
        return "cleaned" if updated == 1 else None


def process_submission_storage_cleanup_intents(
    *,
    intent_ids: Iterable[int] | None = None,
    limit: int = 100,
) -> SubmissionStorageCleanupResult:
    """Retry pending/failed committed cleanup intents one key at a time."""
    queryset = SubmissionStorageCleanupIntent.objects.filter(
        Q(
            status__in=[
                SubmissionStorageCleanupIntent.Status.PENDING,
                SubmissionStorageCleanupIntent.Status.FAILED,
            ]
        )
        | Q(
            status=SubmissionStorageCleanupIntent.Status.PROCESSING,
            last_attempt_at__lt=timezone.now() - timedelta(minutes=15),
        )
        | Q(
            status=SubmissionStorageCleanupIntent.Status.PROCESSING,
            last_attempt_at__isnull=True,
        )
    ).order_by("id")
    if intent_ids is not None:
        ids = tuple(dict.fromkeys(int(value) for value in intent_ids))
        queryset = queryset.filter(id__in=ids)
    selected_ids = list(queryset.values_list("id", flat=True)[: max(1, min(limit, 1000))])

    cleaned = 0
    failed = 0
    deferred = 0
    for intent_id in selected_ids:
        attempted_at = timezone.now()
        claim_token = uuid.uuid4()
        with transaction.atomic():
            intent = (
                SubmissionStorageCleanupIntent.objects.select_for_update()
                .filter(id=intent_id)
                .first()
            )
            if not intent or intent.status == SubmissionStorageCleanupIntent.Status.CLEANED:
                continue
            if (
                intent.status == SubmissionStorageCleanupIntent.Status.PROCESSING
                and intent.last_attempt_at
                and intent.last_attempt_at >= attempted_at - timedelta(minutes=15)
            ):
                continue
            intent.status = SubmissionStorageCleanupIntent.Status.PROCESSING
            intent.attempt_count += 1
            intent.claim_token = claim_token
            intent.last_attempt_at = attempted_at
            intent.last_error = ""
            intent.save(
                update_fields=[
                    "status",
                    "attempt_count",
                    "claim_token",
                    "last_attempt_at",
                    "last_error",
                    "updated_at",
                ]
            )
        outcome = _finish_claimed_storage_cleanup(
            intent_id=intent.id,
            tenant_id=intent.tenant_id,
            bucket=intent.bucket,
            object_key=intent.object_key,
            claim_token=claim_token,
            attempted_at=attempted_at,
        )
        cleaned += int(outcome == "cleaned")
        failed += int(outcome == "failed")
        deferred += int(outcome == "deferred")
    return SubmissionStorageCleanupResult(cleaned=cleaned, failed=failed, deferred=deferred)


def _process_submission_storage_cleanup_safely(intent_ids: tuple[int, ...]) -> None:
    try:
        process_submission_storage_cleanup_intents(intent_ids=intent_ids)
    except Exception:
        logger.exception("Submission storage cleanup callback failed before intent processing")


def lock_storage_object_keys(*, tenant_id: int, object_keys: Iterable[str]) -> tuple[str, ...]:
    """Serialize exact storage keys with cleanup before a caller changes ownership."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("storage object ownership requires an atomic transaction")
    keys = tuple(object_keys)
    prefix = f"tenants/{int(tenant_id)}/"
    if any(not isinstance(key, str) or not key or (key.startswith("tenants/") and not key.startswith(prefix)) for key in keys):
        raise ValueError("storage cleanup key is outside its tenant namespace")
    keys = tuple(sorted(set(keys)))
    for key in keys:
        _lock_object_key(bucket=SubmissionStorageCleanupIntent.Bucket.STORAGE, key=key)
    return keys


def schedule_detached_storage_cleanup(*, tenant_id: int, object_keys: Iterable[str]) -> tuple[int, ...]:
    """Record exact detached object keys in the caller's transaction.

    The caller validates and deletes its metadata graph in this same transaction.
    Surviving owners are never deleted; the processor checks ownership again
    after the outer commit and retains failed/deferred work for the existing retry.
    """
    keys = lock_storage_object_keys(tenant_id=tenant_id, object_keys=object_keys)
    bucket = SubmissionStorageCleanupIntent.Bucket.STORAGE
    intent_ids = []
    for key in keys:
        if _other_storage_owner_references(bucket=bucket, key=key):
            continue
        intent, created = SubmissionStorageCleanupIntent.objects.select_for_update().get_or_create(
            tenant_id=tenant_id, bucket=bucket, object_key=key,
        )
        if not created and intent.status == SubmissionStorageCleanupIntent.Status.CLEANED:
            intent.status = SubmissionStorageCleanupIntent.Status.PENDING
            intent.cleaned_at = None
            intent.last_error = ""
            intent.save(update_fields=["status", "cleaned_at", "last_error", "updated_at"])
        intent_ids.append(intent.id)
    if intent_ids:
        transaction.on_commit(
            lambda ids=tuple(intent_ids): _process_submission_storage_cleanup_safely(ids)
        )
    return tuple(intent_ids)


def storage_cleanup_status(*, tenant_id: int, intent_ids: Iterable[int]) -> dict[str, int]:
    states = list(SubmissionStorageCleanupIntent.objects.filter(
        tenant_id=tenant_id, id__in=tuple(intent_ids),
    ).values_list("status", flat=True))
    return {
        "pending": sum(state not in {"cleaned", "failed"} for state in states),
        "failed": states.count("failed"),
        "cleaned": states.count("cleaned"),
    }


def schedule_inventory_storage_cleanup(*, tenant_id: int, object_keys: Iterable[str]) -> tuple[int, ...]:
    """Compatibility entry for the inventory deletion owner."""
    return schedule_detached_storage_cleanup(tenant_id=tenant_id, object_keys=object_keys)


def inventory_storage_cleanup_status(*, tenant_id: int, intent_ids: Iterable[int]) -> dict[str, int]:
    return storage_cleanup_status(tenant_id=tenant_id, intent_ids=intent_ids)


def delete_submission_storage_for_permanent_delete(
    *,
    tenant_id: int,
    submission_ids: Iterable[int],
    wrong_note_pdf_ids: Iterable[int] = tuple(),
) -> tuple[int, ...]:
    """Persist cleanup intents and delete media rows before raw parent deletion.

    The caller owns the surrounding database transaction. R2 deletion only runs
    after that transaction commits; failed keys remain retryable in the intent table.
    """
    ids = tuple(dict.fromkeys(int(value) for value in submission_ids if int(value) > 0))
    wrong_note_ids = tuple(
        dict.fromkeys(int(value) for value in wrong_note_pdf_ids if int(value) > 0)
    )
    if not ids and not wrong_note_ids:
        return tuple()

    submissions = list(
        Submission.objects.select_for_update()
        .filter(tenant_id=tenant_id, id__in=ids)
        .only("id", "file_key")
        .order_by("id")
    )
    exact_submission_ids = tuple(submission.id for submission in submissions)
    if len(exact_submission_ids) != len(ids):
        raise ValueError("submission tenant does not match permanent-delete scope")
    if SubmissionMedia.objects.filter(submission_id__in=exact_submission_ids).exclude(
        tenant_id=tenant_id
    ).exists():
        raise ValueError("submission media tenant does not match its parent submission")

    media = list(
        SubmissionMedia.objects.select_for_update()
        .filter(tenant_id=tenant_id, submission_id__in=exact_submission_ids)
        .only("id", "object_key", "status", "upload_started_at")
        .order_by("id")
    )
    upload_lease_cutoff = timezone.now() - SUBMISSION_MEDIA_UPLOAD_LEASE
    if any(
        item.status == SubmissionMedia.Status.UPLOADING
        and item.upload_started_at > upload_lease_cutoff
        for item in media
    ):
        raise ValueError("submission media upload is still in progress")
    media_ids = tuple(item.id for item in media)
    candidate_keys_by_bucket: dict[str, set[str]] = {
        SubmissionStorageCleanupIntent.Bucket.AI: set(),
        SubmissionStorageCleanupIntent.Bucket.STORAGE: set(),
    }
    for submission in submissions:
        key = str(submission.file_key or "").strip()
        if key in {"", "pending"}:
            continue
        if not _submission_owned_object_key(
            tenant_id=tenant_id,
            key=key,
            submission_id=submission.id,
        ):
            raise ValueError("submission file key is outside its canonical namespace")
        candidate_keys_by_bucket[SubmissionStorageCleanupIntent.Bucket.AI].add(key)
    for item in media:
        key = str(item.object_key or "").strip()
        if key in {"", "pending"}:
            continue
        if not _submission_owned_object_key(
            tenant_id=tenant_id,
            key=key,
            submission_id=item.submission_id,
        ):
            raise ValueError("submission media key is outside its canonical namespace")
        candidate_keys_by_bucket[SubmissionStorageCleanupIntent.Bucket.AI].add(key)

    wrong_note_pdf_model = django_apps.get_model("results", "WrongNotePDF")
    wrong_note_pdfs = list(
        wrong_note_pdf_model._base_manager.select_for_update()
        .filter(id__in=wrong_note_ids, enrollment__tenant_id=tenant_id)
        .only("id", "file_path")
        .order_by("id")
    )
    exact_wrong_note_ids = tuple(item.id for item in wrong_note_pdfs)
    if len(exact_wrong_note_ids) != len(wrong_note_ids):
        raise ValueError("wrong-note PDF tenant does not match permanent-delete scope")
    for item in wrong_note_pdfs:
        key = str(item.file_path or "").strip()
        if not key:
            continue
        if not _wrong_note_owned_object_key(tenant_id=tenant_id, key=key):
            raise ValueError("wrong-note PDF key is outside its canonical namespace")
        candidate_keys_by_bucket[SubmissionStorageCleanupIntent.Bucket.STORAGE].add(key)
    intent_ids: list[int] = []
    for bucket, candidate_keys in candidate_keys_by_bucket.items():
        if not candidate_keys:
            continue
        for key in sorted(candidate_keys):
            _lock_object_key(bucket=bucket, key=key)
        shared_keys = {
            key
            for key in candidate_keys
            if _other_storage_owner_references(
                bucket=bucket,
                key=key,
                excluded_submission_ids=exact_submission_ids,
                excluded_submission_media_ids=media_ids,
                excluded_wrong_note_pdf_ids=exact_wrong_note_ids,
            )
        }

        for key in sorted(candidate_keys - shared_keys):
            intent, created = SubmissionStorageCleanupIntent.objects.select_for_update().get_or_create(
                tenant_id=tenant_id,
                bucket=bucket,
                object_key=key,
            )
            if not created and intent.status == SubmissionStorageCleanupIntent.Status.CLEANED:
                intent.status = SubmissionStorageCleanupIntent.Status.PENDING
                intent.last_error = ""
                intent.cleaned_at = None
                intent.save(update_fields=["status", "last_error", "cleaned_at", "updated_at"])
            intent_ids.append(intent.id)
    if intent_ids:
        transaction.on_commit(
            lambda ids=tuple(intent_ids): _process_submission_storage_cleanup_safely(ids)
        )

    if media_ids:
        SubmissionMedia.objects.filter(
            tenant_id=tenant_id,
            submission_id__in=exact_submission_ids,
            id__in=media_ids,
        ).delete()
    return tuple(intent_ids)


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
