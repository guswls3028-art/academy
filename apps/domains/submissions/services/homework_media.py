from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
import mimetypes
from pathlib import PurePath
import re
import unicodedata
import uuid

from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework.exceptions import APIException, ValidationError

from apps.core.r2_paths import ai_submission_key
from apps.domains.submissions.models import Submission, SubmissionMedia
from apps.infrastructure.storage.r2 import upload_fileobj_to_r2


logger = logging.getLogger(__name__)

MAX_HOMEWORK_MEDIA_FILES = 20
MAX_HOMEWORK_MEDIA_FILE_SIZE = 100 * 1024 * 1024
MAX_HOMEWORK_MEDIA_TOTAL_SIZE = 500 * 1024 * 1024

_ACTIVE_SUBMISSION_STATUSES = (
    Submission.Status.SUBMITTED,
    Submission.Status.DISPATCHED,
    Submission.Status.EXTRACTING,
    Submission.Status.ANSWERS_READY,
    Submission.Status.GRADING,
)
_HOMEWORK_MEDIA_SOURCES = (
    Submission.Source.HOMEWORK_IMAGE,
    Submission.Source.HOMEWORK_VIDEO,
)
_SAFE_NAME_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class HomeworkMediaConflict(APIException):
    status_code = 409
    default_code = "homework_media_conflict"

    def __init__(self, *, code: str, detail: str):
        super().__init__({"code": code, "detail": detail})


class HomeworkMediaUploadFailed(APIException):
    status_code = 503
    default_code = "homework_media_upload_failed"

    def __init__(self):
        super().__init__(
            {
                "code": "HOMEWORK_MEDIA_UPLOAD_FAILED",
                "detail": "파일을 저장하지 못했습니다. 성공한 파일은 유지되며 이 파일만 다시 시도할 수 있습니다.",
            }
        )


@dataclass(frozen=True)
class ValidatedHomeworkMedia:
    original_filename: str
    extension: str
    media_kind: str
    mime_type: str
    size: int
    fingerprint: str


def _safe_display_name(name: str, extension: str) -> str:
    candidate = unicodedata.normalize("NFC", PurePath(str(name or "")).name)
    candidate = _SAFE_NAME_CHARS.sub("", candidate).replace("/", "").replace("\\", "").strip()
    if not candidate:
        candidate = f"제출 파일.{extension}"
    return candidate[:255]


def _fingerprint(upload_file) -> str:
    position = upload_file.tell() if hasattr(upload_file, "tell") else 0
    digest = hashlib.sha256()
    try:
        upload_file.seek(0)
        chunks = upload_file.chunks() if hasattr(upload_file, "chunks") else iter(lambda: upload_file.read(1024 * 1024), b"")
        for chunk in chunks:
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        try:
            upload_file.seek(position)
        except Exception:
            upload_file.seek(0)


_MEDIA_BY_EXTENSION = {
    "jpg": ("image", "image/jpeg", "jpg"),
    "jpeg": ("image", "image/jpeg", "jpg"),
    "png": ("image", "image/png", "png"),
    "gif": ("image", "image/gif", "gif"),
    "webp": ("image", "image/webp", "webp"),
    "heic": ("image", "image/heic", "heic"),
    "heif": ("image", "image/heif", "heif"),
    "avif": ("image", "image/avif", "avif"),
    "mp4": ("video", "video/mp4", "mp4"),
    "m4v": ("video", "video/mp4", "m4v"),
    "mov": ("video", "video/quicktime", "mov"),
    "webm": ("video", "video/webm", "webm"),
}
_MEDIA_BY_DECLARED_MIME = {
    "image/jpeg": _MEDIA_BY_EXTENSION["jpg"],
    "image/jpg": _MEDIA_BY_EXTENSION["jpg"],
    "image/png": _MEDIA_BY_EXTENSION["png"],
    "image/gif": _MEDIA_BY_EXTENSION["gif"],
    "image/webp": _MEDIA_BY_EXTENSION["webp"],
    "image/heic": _MEDIA_BY_EXTENSION["heic"],
    "image/heif": _MEDIA_BY_EXTENSION["heif"],
    "image/heic-sequence": _MEDIA_BY_EXTENSION["heic"],
    "image/heif-sequence": _MEDIA_BY_EXTENSION["heif"],
    "image/avif": _MEDIA_BY_EXTENSION["avif"],
    "video/mp4": _MEDIA_BY_EXTENSION["mp4"],
    "video/x-m4v": _MEDIA_BY_EXTENSION["m4v"],
    "video/quicktime": _MEDIA_BY_EXTENSION["mov"],
    "video/webm": _MEDIA_BY_EXTENSION["webm"],
}


def validate_homework_media_file(upload_file) -> ValidatedHomeworkMedia:
    name = str(getattr(upload_file, "name", "") or "")
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    declared_mime = str(getattr(upload_file, "content_type", "") or "").split(";", 1)[0].lower()
    media = _MEDIA_BY_EXTENSION.get(extension) or _MEDIA_BY_DECLARED_MIME.get(declared_mime)
    if media is None:
        raise ValidationError(
            {
                "code": "HOMEWORK_MEDIA_TYPE",
                "detail": "JPG, PNG, GIF, WebP, HEIC/HEIF, AVIF, MP4, MOV, M4V, WebM 파일만 올릴 수 있습니다.",
            }
        )
    size = int(getattr(upload_file, "size", 0) or 0)
    if size <= 0:
        raise ValidationError({"code": "HOMEWORK_MEDIA_EMPTY", "detail": "빈 파일은 올릴 수 없습니다."})
    if size > MAX_HOMEWORK_MEDIA_FILE_SIZE:
        raise ValidationError(
            {
                "code": "HOMEWORK_MEDIA_FILE_SIZE",
                "detail": "파일 하나는 100MB까지 올릴 수 있습니다.",
            }
        )

    media_kind, canonical_mime, storage_extension = media

    return ValidatedHomeworkMedia(
        original_filename=_safe_display_name(name, storage_extension),
        extension=storage_extension,
        media_kind=media_kind,
        mime_type=canonical_mime,
        size=size,
        fingerprint=_fingerprint(upload_file),
    )


def _parse_uuid(value, *, field_name: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError):
        raise ValidationError({field_name: "올바른 파일 식별자가 아닙니다."})


def _parse_position(value) -> int:
    try:
        position = int(value)
    except (TypeError, ValueError):
        raise ValidationError({"position": "파일 순서가 올바르지 않습니다."})
    if position < 0 or position >= MAX_HOMEWORK_MEDIA_FILES:
        raise ValidationError({"position": f"파일 순서는 0부터 {MAX_HOMEWORK_MEDIA_FILES - 1}까지입니다."})
    return position


def _ensure_parent_submission(
    *,
    tenant,
    user,
    submitted_by_user,
    enrollment_id: int,
    homework_id: int,
) -> Submission:
    parent = (
        Submission.objects.filter(
            tenant=tenant,
            user=user,
            enrollment_id=enrollment_id,
            target_type=Submission.TargetType.HOMEWORK,
            target_id=homework_id,
            source__in=_HOMEWORK_MEDIA_SOURCES,
            status__in=_ACTIVE_SUBMISSION_STATUSES,
        )
        .order_by("-id")
        .first()
    )
    if parent:
        return parent

    try:
        with transaction.atomic():
            submission_meta = None
            if getattr(user, "id", None) != getattr(submitted_by_user, "id", None):
                submission_meta = {"submitted_by_user_id": submitted_by_user.id}
            return Submission.objects.create(
                tenant=tenant,
                user=user,
                enrollment_id=enrollment_id,
                target_type=Submission.TargetType.HOMEWORK,
                target_id=homework_id,
                # Keep the existing single-file source contract so the active-parent
                # uniqueness constraint remains valid for old and new API instances.
                source=Submission.Source.HOMEWORK_IMAGE,
                meta=submission_meta,
                status=Submission.Status.SUBMITTED,
            )
    except IntegrityError:
        parent = (
            Submission.objects.filter(
                tenant=tenant,
                user=user,
                target_type=Submission.TargetType.HOMEWORK,
                target_id=homework_id,
                source__in=_HOMEWORK_MEDIA_SOURCES,
                status__in=_ACTIVE_SUBMISSION_STATUSES,
            )
            .order_by("-id")
            .first()
        )
        if not parent or int(parent.enrollment_id or 0) != int(enrollment_id):
            raise HomeworkMediaConflict(
                code="HOMEWORK_MEDIA_PARENT_CONFLICT",
                detail="현재 제출 정보를 다시 불러온 뒤 시도해 주세요.",
            )
        return parent


def _legacy_is_active(parent: Submission) -> bool:
    meta = parent.meta if isinstance(parent.meta, dict) else {}
    return bool(parent.file_key) and not meta.get("homework_media_legacy_removed_at")


def _prepare_media(
    *,
    parent: Submission,
    tenant,
    client_upload_id: uuid.UUID,
    upload_batch_id: uuid.UUID,
    position: int,
    validated: ValidatedHomeworkMedia,
) -> tuple[SubmissionMedia, bool]:
    with transaction.atomic():
        parent = Submission.objects.select_for_update().get(pk=parent.pk, tenant=tenant)
        reused_anywhere = (
            SubmissionMedia.objects.select_for_update()
            .filter(tenant=tenant, client_upload_id=client_upload_id)
            .first()
        )
        if reused_anywhere:
            if reused_anywhere.submission_id != parent.id:
                raise HomeworkMediaConflict(
                    code="HOMEWORK_MEDIA_CLIENT_ID_REUSED",
                    detail="같은 파일 식별자를 다른 과제에 사용할 수 없습니다.",
                )
            if reused_anywhere.fingerprint != validated.fingerprint:
                raise HomeworkMediaConflict(
                    code="HOMEWORK_MEDIA_CLIENT_ID_REUSED",
                    detail="같은 파일 식별자에 다른 파일을 다시 올릴 수 없습니다.",
                )
            if reused_anywhere.removed_at:
                raise HomeworkMediaConflict(
                    code="HOMEWORK_MEDIA_REMOVED",
                    detail="삭제한 파일은 새 파일로 다시 선택해 주세요.",
                )
            if reused_anywhere.status == SubmissionMedia.Status.UPLOADED:
                return reused_anywhere, True
            reused_anywhere.status = SubmissionMedia.Status.UPLOADING
            reused_anywhere.error_message = ""
            reused_anywhere.failed_at = None
            reused_anywhere.upload_started_at = timezone.now()
            reused_anywhere.save(
                update_fields=[
                    "status",
                    "error_message",
                    "failed_at",
                    "upload_started_at",
                    "updated_at",
                ]
            )
            return reused_anywhere, False

        uploaded_duplicate = (
            SubmissionMedia.objects.select_for_update()
            .filter(
                tenant=tenant,
                submission=parent,
                fingerprint=validated.fingerprint,
                status=SubmissionMedia.Status.UPLOADED,
                removed_at__isnull=True,
            )
            .order_by("position", "id")
            .first()
        )
        if uploaded_duplicate:
            return uploaded_duplicate, True

        active_media = SubmissionMedia.objects.filter(
            submission=parent,
            removed_at__isnull=True,
        )
        active_count = active_media.count() + (1 if _legacy_is_active(parent) else 0)
        active_size = int(active_media.aggregate(total=Sum("size"))["total"] or 0)
        if _legacy_is_active(parent):
            active_size += int(parent.file_size or 0)
        if active_count >= MAX_HOMEWORK_MEDIA_FILES:
            raise ValidationError(
                {
                    "code": "HOMEWORK_MEDIA_LIMIT",
                    "detail": f"과제 하나에는 파일을 {MAX_HOMEWORK_MEDIA_FILES}개까지 올릴 수 있습니다.",
                }
            )
        if active_size + validated.size > MAX_HOMEWORK_MEDIA_TOTAL_SIZE:
            raise ValidationError(
                {
                    "code": "HOMEWORK_MEDIA_TOTAL_SIZE",
                    "detail": "과제 파일 전체 용량은 500MB까지입니다.",
                }
            )
        if (_legacy_is_active(parent) and position == 0) or active_media.filter(position=position).exists():
            raise HomeworkMediaConflict(
                code="HOMEWORK_MEDIA_POSITION_CONFLICT",
                detail="파일 순서가 겹쳤습니다. 목록을 다시 불러온 뒤 시도해 주세요.",
            )

        media = SubmissionMedia.objects.create(
            tenant=tenant,
            submission=parent,
            client_upload_id=client_upload_id,
            upload_batch_id=upload_batch_id,
            fingerprint=validated.fingerprint,
            object_key="pending",
            original_filename=validated.original_filename,
            media_kind=validated.media_kind,
            mime_type=validated.mime_type,
            size=validated.size,
            position=position,
            status=SubmissionMedia.Status.UPLOADING,
        )
        media.object_key = ai_submission_key(
            tenant_id=tenant.id,
            submission_id=parent.id,
            unique_id=f"media-{media.id}-{client_upload_id.hex}",
            ext=validated.extension,
        )
        media.save(update_fields=["object_key", "updated_at"])
        return media, False


def store_homework_media(
    *,
    tenant,
    user,
    submitted_by_user,
    enrollment_id: int,
    homework_id: int,
    upload_file,
    client_file_id,
    upload_batch_id,
    position,
) -> tuple[SubmissionMedia, bool]:
    validated = validate_homework_media_file(upload_file)
    client_upload_id = _parse_uuid(client_file_id, field_name="client_file_id")
    batch_id = _parse_uuid(upload_batch_id, field_name="upload_batch_id")
    parsed_position = _parse_position(position)
    parent = _ensure_parent_submission(
        tenant=tenant,
        user=user,
        submitted_by_user=submitted_by_user,
        enrollment_id=enrollment_id,
        homework_id=homework_id,
    )
    media, deduplicated = _prepare_media(
        parent=parent,
        tenant=tenant,
        client_upload_id=client_upload_id,
        upload_batch_id=batch_id,
        position=parsed_position,
        validated=validated,
    )
    if deduplicated:
        return media, True

    try:
        upload_file.seek(0)
        upload_fileobj_to_r2(
            fileobj=upload_file,
            key=media.object_key,
            content_type=media.mime_type,
        )
    except Exception:
        logger.exception("Homework media upload failed media_id=%s", media.id)
        _best_effort_mark_failed(media_id=media.pk, message="파일 저장 실패")
        raise HomeworkMediaUploadFailed()

    uploaded_at = timezone.now()
    try:
        updated = SubmissionMedia.objects.filter(pk=media.pk).update(
            status=SubmissionMedia.Status.UPLOADED,
            error_message="",
            uploaded_at=uploaded_at,
            failed_at=None,
            updated_at=uploaded_at,
        )
        if updated != 1:
            raise RuntimeError("homework media row disappeared during upload finalization")
    except Exception:
        # The object is not deleted blindly: it remains under this row's deterministic
        # immutable key, so the same client id can safely overwrite/reconcile it on retry.
        logger.exception("Homework media finalization failed media_id=%s", media.id)
        _best_effort_mark_failed(media_id=media.pk, message="파일 저장 확인 실패")
        raise HomeworkMediaUploadFailed()
    media.refresh_from_db()
    return media, False


def _best_effort_mark_failed(*, media_id: int, message: str) -> None:
    now = timezone.now()
    try:
        SubmissionMedia.objects.filter(pk=media_id).update(
            status=SubmissionMedia.Status.FAILED,
            error_message=message,
            failed_at=now,
            updated_at=now,
        )
    except Exception:
        logger.exception("Homework media failure status persistence failed media_id=%s", media_id)


def serialize_homework_media(media: SubmissionMedia) -> dict:
    return {
        "id": str(media.id),
        "legacy": False,
        "client_file_id": str(media.client_upload_id),
        "upload_batch_id": str(media.upload_batch_id),
        "position": int(media.position),
        "original_filename": media.original_filename,
        "media_kind": media.media_kind,
        "mime_type": media.mime_type,
        "file_size": int(media.size),
        "status": media.status,
        "error_message": media.error_message,
        "upload_started_at": media.upload_started_at.isoformat() if media.upload_started_at else None,
        "uploaded_at": media.uploaded_at.isoformat() if media.uploaded_at else None,
        "failed_at": media.failed_at.isoformat() if media.failed_at else None,
        "removed_at": media.removed_at.isoformat() if media.removed_at else None,
        "created_at": media.created_at.isoformat() if media.created_at else None,
    }


def legacy_homework_media_removed_at(submission: Submission):
    meta = submission.meta if isinstance(submission.meta, dict) else {}
    return meta.get("homework_media_legacy_removed_at")


def serialize_legacy_homework_media(submission: Submission) -> dict:
    extension = str(submission.file_key or "").rsplit(".", 1)[-1].lower()
    mime_type = str(submission.file_type or "")
    if "/" not in mime_type:
        mime_type = mimetypes.guess_type(f"file.{extension}")[0] or "application/octet-stream"
    meta = submission.meta if isinstance(submission.meta, dict) else {}
    original_filename = str(meta.get("original_filename") or "").strip()
    if not original_filename:
        original_filename = f"기존 제출 파일.{extension or 'bin'}"
    removed_at = legacy_homework_media_removed_at(submission)
    is_video = (
        submission.source == Submission.Source.HOMEWORK_VIDEO
        or mime_type.startswith("video/")
        or extension in {"mp4", "m4v", "mov", "webm"}
    )
    projected_status = SubmissionMedia.Status.UPLOADED
    if removed_at:
        projected_status = SubmissionMedia.Status.REMOVED
    elif submission.status == Submission.Status.FAILED:
        projected_status = SubmissionMedia.Status.FAILED
    return {
        "id": f"legacy-{submission.id}",
        "legacy": True,
        "client_file_id": None,
        "upload_batch_id": None,
        "position": 0,
        "original_filename": _safe_display_name(original_filename, extension or "bin"),
        "media_kind": SubmissionMedia.Kind.VIDEO if is_video else SubmissionMedia.Kind.IMAGE,
        "mime_type": mime_type,
        "file_size": int(submission.file_size or 0),
        "status": projected_status,
        "error_message": submission.error_message,
        "upload_started_at": submission.created_at.isoformat() if submission.created_at else None,
        "uploaded_at": submission.created_at.isoformat() if submission.created_at else None,
        "failed_at": submission.updated_at.isoformat() if submission.status == Submission.Status.FAILED else None,
        "removed_at": removed_at,
        "created_at": submission.created_at.isoformat() if submission.created_at else None,
    }


def homework_media_set_fingerprint_for_submissions(submissions) -> str:
    """Fingerprint the active media evidence represented by submission rows."""
    evidence: list[dict] = []
    for submission in submissions:
        if (
            submission.source not in _HOMEWORK_MEDIA_SOURCES
            or submission.status == Submission.Status.SUPERSEDED
        ):
            continue
        if _legacy_is_active(submission):
            evidence.append(
                {
                    "kind": "legacy",
                    "submission_id": int(submission.id),
                    "source": str(submission.source),
                    "object_key": str(submission.file_key),
                    "file_type": str(submission.file_type or ""),
                    "file_size": int(submission.file_size or 0),
                    "status": str(submission.status),
                }
            )
        for media in submission.media_files.all():
            if media.removed_at is not None:
                continue
            evidence.append(
                {
                    "kind": "media",
                    "submission_id": int(submission.id),
                    "media_id": int(media.id),
                    "client_upload_id": str(media.client_upload_id),
                    "fingerprint": str(media.fingerprint),
                    "object_key": str(media.object_key),
                    "original_filename": str(media.original_filename),
                    "media_kind": str(media.media_kind),
                    "mime_type": str(media.mime_type),
                    "size": int(media.size),
                    "position": int(media.position),
                    "status": str(media.status),
                }
            )
    evidence.sort(
        key=lambda item: (
            int(item["submission_id"]),
            0 if item["kind"] == "legacy" else 1,
            int(item.get("position", 0)),
            int(item.get("media_id", 0)),
        )
    )
    encoded = json.dumps(
        evidence,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def homework_media_set_fingerprint(
    *,
    tenant,
    enrollment_id: int,
    homework_id: int,
) -> str:
    submissions = list(
        Submission.objects.filter(
            tenant=tenant,
            enrollment_id=int(enrollment_id),
            target_type=Submission.TargetType.HOMEWORK,
            target_id=int(homework_id),
            source__in=_HOMEWORK_MEDIA_SOURCES,
        )
        .exclude(status=Submission.Status.SUPERSEDED)
        .prefetch_related("media_files")
        .order_by("id")
    )
    return homework_media_set_fingerprint_for_submissions(submissions)


def homework_media_limits_payload() -> dict:
    return {
        "max_files": MAX_HOMEWORK_MEDIA_FILES,
        "max_file_size_bytes": MAX_HOMEWORK_MEDIA_FILE_SIZE,
        "max_total_size_bytes": MAX_HOMEWORK_MEDIA_TOTAL_SIZE,
    }
