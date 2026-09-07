# PATH: apps/domains/submissions/views/exam_omr_batch_upload_view.py
from __future__ import annotations

from datetime import timedelta
import hashlib
import logging
from uuid import UUID

from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import TenantResolvedAndStaff
from apps.domains.submissions.models import (
    OmrUploadBatch,
    OmrUploadBatchItem,
    Submission,
)
from apps.domains.submissions.serializers.submission import SubmissionCreateSerializer
from apps.domains.submissions.services.dispatcher import (
    dispatch_submission,
    resolve_omr_sheet_for_exam,
)
from apps.domains.submissions.services.lifecycle import (
    InvalidTransitionError,
    retry_failed_submission,
)
from apps.infrastructure.storage.r2 import delete_object_r2_storage
from apps.support.submissions.dependencies import exam_belongs_to_tenant


MAX_FILES = 100
MAX_FILE_SIZE = 10 * 1024 * 1024
ALLOWED_CONTENT_TYPES = {
    "image/jpeg",
    "image/png",
    "image/tiff",
    "application/pdf",
}
OMR_UPLOAD_FILE_PROPERTIES = {
    "file": {"type": "string", "format": "binary"},
    "files": {
        "type": "array",
        "items": {"type": "string", "format": "binary"},
        "maxItems": MAX_FILES,
    },
    "sheet_id": {"type": "integer", "minimum": 1},
    "session_id": {"type": "integer", "minimum": 1},
}
OMR_UPLOAD_REQUEST_SCHEMA = {
    "anyOf": [
        {
            "title": "Legacy OMR single-file upload",
            "type": "object",
            "properties": OMR_UPLOAD_FILE_PROPERTIES,
            "required": ["file"],
            "additionalProperties": False,
        },
        {
            "title": "Legacy OMR multi-file upload",
            "type": "object",
            "properties": OMR_UPLOAD_FILE_PROPERTIES,
            "required": ["files"],
            "additionalProperties": False,
        },
        {
            "title": "Durable OMR batch single-file upload",
            "type": "object",
            "properties": {
                **OMR_UPLOAD_FILE_PROPERTIES,
                "batch_id": {"type": "string", "format": "uuid"},
                "item_ordinals": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 1, "maximum": MAX_FILES},
                    "minItems": 1,
                    "maxItems": MAX_FILES,
                },
            },
            "required": ["batch_id", "item_ordinals", "file"],
            "additionalProperties": False,
        },
        {
            "title": "Durable OMR batch multi-file upload",
            "type": "object",
            "properties": {
                **OMR_UPLOAD_FILE_PROPERTIES,
                "batch_id": {"type": "string", "format": "uuid"},
                "item_ordinals": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 1, "maximum": MAX_FILES},
                    "minItems": 1,
                    "maxItems": MAX_FILES,
                },
            },
            "required": ["batch_id", "item_ordinals", "files"],
            "additionalProperties": False,
        },
    ]
}
PROCESSING_STATUSES = {
    Submission.Status.DISPATCHED,
    Submission.Status.EXTRACTING,
    Submission.Status.ANSWERS_READY,
    Submission.Status.GRADING,
}

logger = logging.getLogger(__name__)


class OmrUploadBatchItemSummarySerializer(serializers.Serializer):
    id = serializers.CharField()
    ordinal = serializers.IntegerField()
    admission_status = serializers.CharField()
    status = serializers.CharField()
    submission_id = serializers.IntegerField(allow_null=True)
    duplicate_of_submission_id = serializers.IntegerField(allow_null=True)
    submission_status = serializers.CharField(allow_null=True)
    identifier_status = serializers.CharField(allow_null=True)
    failure_code = serializers.CharField(allow_null=True)
    failure_message = serializers.CharField(allow_null=True)


class OmrUploadBatchSummarySerializer(serializers.Serializer):
    id = serializers.UUIDField()
    exam_id = serializers.IntegerField()
    session_id = serializers.IntegerField(allow_null=True)
    lecture_id = serializers.IntegerField(allow_null=True)
    total_count = serializers.IntegerField()
    counts = serializers.DictField(child=serializers.IntegerField())
    pending_admission_ordinals = serializers.ListField(child=serializers.IntegerField())
    failed_ordinals = serializers.ListField(child=serializers.IntegerField())
    admission_failed_ordinals = serializers.ListField(child=serializers.IntegerField())
    duplicate_ordinals = serializers.ListField(child=serializers.IntegerField())
    items = OmrUploadBatchItemSummarySerializer(many=True)
    terminal = serializers.BooleanField()
    overall_status = serializers.CharField()
    completion_notice_claimed = serializers.BooleanField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()


class OmrUploadBatchUploadResultSerializer(OmrUploadBatchSummarySerializer):
    created_count = serializers.IntegerField()
    submission_ids = serializers.ListField(child=serializers.IntegerField())


class OmrUploadBatchRetryResultSerializer(OmrUploadBatchSummarySerializer):
    retried_ordinals = serializers.ListField(child=serializers.IntegerField())
    requires_file_ordinals = serializers.ListField(child=serializers.IntegerField())
    skipped_ordinals = serializers.ListField(child=serializers.IntegerField())


def _pdf_page_count(upload_file) -> int:
    pos = upload_file.tell() if hasattr(upload_file, "tell") else 0
    try:
        upload_file.seek(0)
        data = upload_file.read()
        from academy.adapters.tools.pymupdf_renderer import get_page_count_from_bytes

        return get_page_count_from_bytes(data)
    finally:
        try:
            upload_file.seek(pos)
        except Exception:
            upload_file.seek(0)


def _positive_int(value, *, field: str, required: bool = True) -> int | None:
    if value in (None, ""):
        if required:
            raise ValueError(f"{field} is required")
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return parsed


def _batch_context(*, tenant, exam_id: int, session_id: int | None):
    exam_model = apps.get_model("exams", "Exam")
    exam = exam_model.objects.filter(id=exam_id, tenant=tenant).first()
    if exam is None:
        raise ValueError("해당 시험을 찾을 수 없습니다.")
    if session_id is None:
        return exam, None, None
    session = exam.sessions.filter(id=session_id, lecture__tenant=tenant).values("id", "lecture_id").first()
    if session is None:
        raise ValueError("시험과 차시 정보를 확인할 수 없습니다.")
    return exam, int(session["id"]), int(session["lecture_id"])


def _create_batch(*, tenant, user, exam_id: int, total_count: int, session_id: int | None):
    if total_count < 1 or total_count > MAX_FILES:
        raise ValueError(f"total_count must be between 1 and {MAX_FILES}")
    _, resolved_session_id, lecture_id = _batch_context(
        tenant=tenant,
        exam_id=exam_id,
        session_id=session_id,
    )
    with transaction.atomic():
        batch = OmrUploadBatch.objects.create(
            tenant=tenant,
            created_by=user,
            exam_id=exam_id,
            session_id=resolved_session_id,
            lecture_id=lecture_id,
            total_count=total_count,
        )
        OmrUploadBatchItem.objects.bulk_create(
            [
                OmrUploadBatchItem(
                    tenant=tenant,
                    exam_id=exam_id,
                    batch=batch,
                    ordinal=ordinal,
                )
                for ordinal in range(1, total_count + 1)
            ]
        )
    return batch


def _owned_batch(request, batch_id: str | UUID, *, exam_id: int | None = None):
    tenant = getattr(request, "tenant", None)
    if tenant is None:
        return None
    filters = {
        "id": batch_id,
        "tenant": tenant,
        "created_by": request.user,
    }
    if exam_id is not None:
        filters["exam_id"] = exam_id
    try:
        return OmrUploadBatch.objects.filter(**filters).first()
    except (TypeError, ValueError, ValidationError):
        return None


def _batch_item_status(item: OmrUploadBatchItem) -> str:
    submission = item.submission
    if item.admission_status == OmrUploadBatchItem.AdmissionStatus.PENDING:
        return "pending_admission"
    if item.admission_status == OmrUploadBatchItem.AdmissionStatus.DUPLICATE:
        return "duplicate"
    if item.admission_status == OmrUploadBatchItem.AdmissionStatus.FAILED or submission is None:
        return "failed"
    if submission.status == Submission.Status.SUBMITTED:
        return "received"
    if submission.status in PROCESSING_STATUSES:
        return "processing"
    if submission.status == Submission.Status.DONE:
        return "completed"
    if submission.status == Submission.Status.NEEDS_IDENTIFICATION:
        return "needs_identification"
    if submission.status == Submission.Status.SUPERSEDED:
        return "superseded"
    if submission.status == Submission.Status.FAILED:
        return "failed"
    return "processing"


def _batch_item_summary(batch: OmrUploadBatch, item: OmrUploadBatchItem) -> dict:
    submission = item.submission
    submission_meta = submission.meta if submission and isinstance(submission.meta, dict) else {}
    return {
        "id": f"{batch.id}:{int(item.ordinal)}",
        "ordinal": int(item.ordinal),
        "admission_status": item.admission_status,
        "status": _batch_item_status(item),
        "submission_id": int(item.submission_id) if item.submission_id else None,
        "duplicate_of_submission_id": (
            int(item.duplicate_of_submission_id) if item.duplicate_of_submission_id else None
        ),
        "submission_status": submission.status if submission else None,
        "identifier_status": submission_meta.get("identifier_status") or None,
        "failure_code": item.failure_code or None,
        "failure_message": item.failure_message or None,
    }


def _batch_summary(batch: OmrUploadBatch) -> dict:
    prefetched_items = getattr(batch, "_prefetched_objects_cache", {}).get("items")
    items = sorted(
        list(prefetched_items)
        if prefetched_items is not None
        else list(batch.items.select_related("submission").order_by("ordinal")),
        key=lambda item: int(item.ordinal),
    )
    counts = {
        "pending_admission": 0,
        "received": 0,
        "duplicate": 0,
        "processing": 0,
        "completed": 0,
        "needs_identification": 0,
        "failed": 0,
        "superseded": 0,
    }
    pending_admission_ordinals: list[int] = []
    failed_ordinals: list[int] = []
    admission_failed_ordinals: list[int] = []
    duplicate_ordinals: list[int] = []

    for item in items:
        item_status = _batch_item_status(item)
        if item_status == "pending_admission":
            counts["pending_admission"] += 1
            pending_admission_ordinals.append(int(item.ordinal))
            continue
        if item_status == "duplicate":
            counts["duplicate"] += 1
            duplicate_ordinals.append(int(item.ordinal))
            continue
        if item_status == "received":
            counts["received"] += 1
        elif item_status == "processing":
            counts["processing"] += 1
        elif item_status == "completed":
            counts["completed"] += 1
        elif item_status == "needs_identification":
            counts["needs_identification"] += 1
        elif item_status == "superseded":
            counts["superseded"] += 1
        elif item_status == "failed":
            counts["failed"] += 1
            failed_ordinals.append(int(item.ordinal))
            if (
                item.admission_status == OmrUploadBatchItem.AdmissionStatus.FAILED
                or item.submission is None
            ):
                admission_failed_ordinals.append(int(item.ordinal))

    terminal = (
        len(items) == int(batch.total_count)
        and counts["pending_admission"] == 0
        and counts["received"] == 0
        and counts["processing"] == 0
    )
    if not terminal:
        overall_status = "receiving" if counts["pending_admission"] > 0 or counts["received"] > 0 else "processing"
    elif counts["failed"] > 0:
        overall_status = "failed"
    elif counts["needs_identification"] > 0:
        overall_status = "needs_identification"
    else:
        overall_status = "completed"

    return {
        "id": str(batch.id),
        "exam_id": int(batch.exam_id),
        "session_id": int(batch.session_id) if batch.session_id else None,
        "lecture_id": int(batch.lecture_id) if batch.lecture_id else None,
        "total_count": int(batch.total_count),
        "counts": counts,
        "pending_admission_ordinals": pending_admission_ordinals,
        "failed_ordinals": failed_ordinals,
        "admission_failed_ordinals": admission_failed_ordinals,
        "duplicate_ordinals": duplicate_ordinals,
        "items": [_batch_item_summary(batch, item) for item in items],
        "terminal": terminal,
        "overall_status": overall_status,
        "completion_notice_claimed": batch.completion_notice_claimed_at is not None,
        "created_at": batch.created_at,
        "updated_at": batch.updated_at,
    }


def _file_validation_error(upload_file) -> tuple[str, str] | None:
    if upload_file.size <= 0:
        return "empty_file", "빈 파일은 접수할 수 없습니다."
    if upload_file.size > MAX_FILE_SIZE:
        return "file_too_large", "파일 크기가 10MB를 초과합니다."
    if upload_file.content_type not in ALLOWED_CONTENT_TYPES:
        return "unsupported_file_type", "허용되지 않는 파일 형식입니다."
    if upload_file.content_type == "application/pdf":
        try:
            page_count = _pdf_page_count(upload_file)
        except Exception:
            return "invalid_pdf", "PDF를 읽을 수 없습니다."
        if page_count != 1:
            return "multipage_pdf", f"{page_count}페이지 PDF입니다. 답안지 1장당 1개 파일로 업로드해 주세요."
    return None


def _uploaded_file_sha256(upload_file) -> str:
    """Hash an upload without consuming the stream used by the serializer."""
    pos = upload_file.tell() if hasattr(upload_file, "tell") else 0
    digest = hashlib.sha256()
    try:
        upload_file.seek(0)
        for chunk in upload_file.chunks():
            digest.update(chunk)
    finally:
        try:
            upload_file.seek(pos)
        except Exception:
            upload_file.seek(0)
    return digest.hexdigest()


def _mark_duplicate_admission(
    *,
    item_id: int,
    tenant,
    exam_id: int,
    content_sha256: str,
) -> bool:
    with transaction.atomic():
        existing = (
            OmrUploadBatchItem.objects
            .filter(
                tenant=tenant,
                exam_id=int(exam_id),
                content_sha256=content_sha256,
                admission_status=OmrUploadBatchItem.AdmissionStatus.RECEIVED,
                submission_id__isnull=False,
            )
            .exclude(id=int(item_id))
            .order_by("id")
            .first()
        )
        if existing is None:
            return False
        item = OmrUploadBatchItem.objects.select_for_update().get(id=int(item_id))
        if item.admission_status == OmrUploadBatchItem.AdmissionStatus.RECEIVED:
            return False
        item.admission_status = OmrUploadBatchItem.AdmissionStatus.DUPLICATE
        item.submission = None
        item.duplicate_of_submission_id = int(existing.submission_id)
        item.content_sha256 = content_sha256
        item.failure_code = ""
        item.failure_message = ""
        item.save(update_fields=[
            "admission_status",
            "submission",
            "duplicate_of_submission",
            "content_sha256",
            "failure_code",
            "failure_message",
            "updated_at",
        ])
        return True


def _mark_admission_failed(item_id: int, *, code: str, message: str) -> bool:
    with transaction.atomic():
        item = (
            OmrUploadBatchItem.objects.select_for_update()
            .filter(id=item_id)
            .first()
        )
        if item is None:
            return False
        if (
            item.admission_status == OmrUploadBatchItem.AdmissionStatus.RECEIVED
            and item.submission_id is not None
        ):
            return False
        item.admission_status = OmrUploadBatchItem.AdmissionStatus.FAILED
        item.submission = None
        item.failure_code = code[:64]
        item.failure_message = message[:300]
        item.save(
            update_fields=[
                "admission_status",
                "submission",
                "failure_code",
                "failure_message",
                "updated_at",
            ]
        )
        return True


def _delete_rolled_back_upload(*, key: str, batch_id: UUID, ordinal: int) -> None:
    try:
        delete_object_r2_storage(key=key)
    except Exception:
        logger.exception(
            "Failed to compensate rolled-back OMR upload",
            extra={"batch_id": str(batch_id), "ordinal": int(ordinal)},
        )


class ExamOMRBatchInitializeView(APIView):
    permission_classes = [IsAuthenticated, TenantResolvedAndStaff]

    @extend_schema(
        request=inline_serializer(
            name="OmrUploadBatchInitializeRequest",
            fields={
                "total_count": serializers.IntegerField(min_value=1, max_value=MAX_FILES),
                "session_id": serializers.IntegerField(required=False, allow_null=True),
            },
        ),
        responses={201: OmrUploadBatchSummarySerializer},
    )
    def post(self, request, exam_id: int):
        tenant = getattr(request, "tenant", None)
        if tenant is None:
            return Response({"detail": "Tenant required"}, status=status.HTTP_403_FORBIDDEN)
        try:
            total_count = _positive_int(request.data.get("total_count"), field="total_count")
            session_id = _positive_int(
                request.data.get("session_id"),
                field="session_id",
                required=False,
            )
            batch = _create_batch(
                tenant=tenant,
                user=request.user,
                exam_id=int(exam_id),
                total_count=int(total_count),
                session_id=session_id,
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(_batch_summary(batch), status=status.HTTP_201_CREATED)


class ExamOMRBatchUploadView(APIView):
    permission_classes = [IsAuthenticated, TenantResolvedAndStaff]

    @extend_schema(
        request={"multipart/form-data": OMR_UPLOAD_REQUEST_SCHEMA},
        responses={201: OmrUploadBatchUploadResultSerializer},
    )
    def post(self, request, exam_id: int):
        tenant = getattr(request, "tenant", None)
        if tenant is None:
            return Response({"detail": "Tenant required"}, status=status.HTTP_403_FORBIDDEN)

        files = request.FILES.getlist("files") or []
        if not files:
            single_file = request.FILES.get("file")
            if single_file:
                files = [single_file]
        if not files:
            return Response({"detail": "files required"}, status=status.HTTP_400_BAD_REQUEST)
        if len(files) > MAX_FILES:
            return Response(
                {"detail": f"한 번에 최대 {MAX_FILES}개 파일까지 업로드할 수 있습니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not exam_belongs_to_tenant(exam_id=int(exam_id), tenant=tenant):
            return Response({"detail": "해당 시험을 찾을 수 없습니다."}, status=status.HTTP_400_BAD_REQUEST)

        sheet_id = request.data.get("sheet_id")
        requested_sheet_id = None
        if sheet_id not in (None, ""):
            try:
                requested_sheet_id = int(sheet_id)
            except (TypeError, ValueError):
                return Response({"detail": "sheet_id must be integer"}, status=status.HTTP_400_BAD_REQUEST)

        batch_id = request.data.get("batch_id")
        explicit_batch = batch_id not in (None, "")
        if explicit_batch:
            batch = _owned_batch(request, batch_id, exam_id=int(exam_id))
            if batch is None:
                return Response({"detail": "OMR 등록 작업을 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)
            raw_ordinals = request.data.getlist("item_ordinals")
            try:
                ordinals = [_positive_int(value, field="item_ordinals") for value in raw_ordinals]
            except ValueError as exc:
                return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
            if len(ordinals) != len(files):
                return Response(
                    {"detail": "각 파일의 item_ordinals를 정확히 지정해 주세요."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        else:
            validation_errors = [_file_validation_error(upload_file) for upload_file in files]
            first_error = next((error for error in validation_errors if error), None)
            if first_error is not None:
                return Response({"detail": first_error[1]}, status=status.HTTP_400_BAD_REQUEST)
            try:
                session_id = _positive_int(
                    request.data.get("session_id"),
                    field="session_id",
                    required=False,
                )
                sheet = resolve_omr_sheet_for_exam(
                    tenant=tenant,
                    exam_id=int(exam_id),
                    requested_sheet_id=requested_sheet_id,
                )
                batch = _create_batch(
                    tenant=tenant,
                    user=request.user,
                    exam_id=int(exam_id),
                    total_count=len(files),
                    session_id=session_id,
                )
            except ValueError as exc:
                return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
            ordinals = list(range(1, len(files) + 1))

        if len(set(ordinals)) != len(ordinals) or any(
            ordinal is None or ordinal > batch.total_count for ordinal in ordinals
        ):
            return Response({"detail": "item_ordinals 범위를 확인해 주세요."}, status=status.HTTP_400_BAD_REQUEST)

        if explicit_batch:
            try:
                sheet = resolve_omr_sheet_for_exam(
                    tenant=tenant,
                    exam_id=int(exam_id),
                    requested_sheet_id=requested_sheet_id,
                )
            except ValueError as exc:
                return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        payload = {"sheet_id": int(sheet.id)}

        created_ids: list[int] = []
        for upload_file, ordinal in zip(files, ordinals, strict=True):
            item = batch.items.filter(ordinal=ordinal).first()
            if item is None:
                continue

            uploaded_key = ""
            content_sha256 = ""
            try:
                with transaction.atomic():
                    locked_item = (
                        OmrUploadBatchItem.objects.select_for_update()
                        .get(id=item.id, batch=batch)
                    )
                    if (
                        locked_item.admission_status == OmrUploadBatchItem.AdmissionStatus.RECEIVED
                        and locked_item.submission_id is not None
                    ):
                        continue
                    validation_error = _file_validation_error(upload_file)
                    if validation_error is not None:
                        locked_item.admission_status = OmrUploadBatchItem.AdmissionStatus.FAILED
                        locked_item.submission = None
                        locked_item.failure_code = validation_error[0][:64]
                        locked_item.failure_message = validation_error[1][:300]
                        locked_item.save(
                            update_fields=[
                                "admission_status",
                                "submission",
                                "failure_code",
                                "failure_message",
                                "updated_at",
                            ]
                        )
                        continue
                    content_sha256 = _uploaded_file_sha256(upload_file)
                    duplicate = (
                        OmrUploadBatchItem.objects
                        .filter(
                            tenant=tenant,
                            exam_id=int(exam_id),
                            content_sha256=content_sha256,
                            admission_status=OmrUploadBatchItem.AdmissionStatus.RECEIVED,
                            submission_id__isnull=False,
                        )
                        .exclude(id=locked_item.id)
                        .order_by("id")
                        .first()
                    )
                    if duplicate is not None:
                        locked_item.admission_status = OmrUploadBatchItem.AdmissionStatus.DUPLICATE
                        locked_item.submission = None
                        locked_item.duplicate_of_submission_id = int(duplicate.submission_id)
                        locked_item.content_sha256 = content_sha256
                        locked_item.failure_code = ""
                        locked_item.failure_message = ""
                        locked_item.save(update_fields=[
                            "admission_status",
                            "submission",
                            "duplicate_of_submission",
                            "content_sha256",
                            "failure_code",
                            "failure_message",
                            "updated_at",
                        ])
                        continue
                    serializer = SubmissionCreateSerializer(
                        data={
                            "enrollment_id": None,
                            "target_type": Submission.TargetType.EXAM,
                            "target_id": int(exam_id),
                            "source": Submission.Source.OMR_SCAN,
                            "payload": payload,
                            "file": upload_file,
                        }
                    )
                    serializer.is_valid(raise_exception=True)
                    submission = serializer.save(user=request.user, tenant=tenant)
                    uploaded_key = str(submission.file_key or "")
                    locked_item.submission = submission
                    locked_item.duplicate_of_submission = None
                    locked_item.admission_status = OmrUploadBatchItem.AdmissionStatus.RECEIVED
                    locked_item.content_sha256 = content_sha256
                    locked_item.failure_code = ""
                    locked_item.failure_message = ""
                    locked_item.save(
                        update_fields=[
                            "submission",
                            "duplicate_of_submission",
                            "admission_status",
                            "content_sha256",
                            "failure_code",
                            "failure_message",
                            "updated_at",
                        ]
                    )
                    dispatch_submission(submission)
                    created_ids.append(int(submission.id))
            except IntegrityError:
                if uploaded_key:
                    _delete_rolled_back_upload(
                        key=uploaded_key,
                        batch_id=batch.id,
                        ordinal=int(ordinal),
                    )
                marked_duplicate = bool(content_sha256) and _mark_duplicate_admission(
                    item_id=item.id,
                    tenant=tenant,
                    exam_id=int(exam_id),
                    content_sha256=content_sha256,
                )
                if not marked_duplicate:
                    logger.exception(
                        "OMR batch item uniqueness admission failed",
                        extra={"batch_id": str(batch.id), "ordinal": int(ordinal)},
                    )
                    _mark_admission_failed(
                        item.id,
                        code="admission_integrity_failed",
                        message="파일 접수 충돌을 확인하지 못했습니다. 해당 항목만 다시 선택해 주세요.",
                    )
            except Exception:
                if uploaded_key:
                    _delete_rolled_back_upload(
                        key=uploaded_key,
                        batch_id=batch.id,
                        ordinal=int(ordinal),
                    )
                logger.exception(
                    "OMR batch item admission failed",
                    extra={
                        "batch_id": str(batch.id),
                        "exam_id": int(exam_id),
                        "ordinal": int(ordinal),
                    },
                )
                _mark_admission_failed(
                    item.id,
                    code="admission_failed",
                    message="파일 접수를 완료하지 못했습니다. 해당 항목만 다시 선택해 주세요.",
                )

        response_data = _batch_summary(batch)
        response_data.update(
            {
                "created_count": len(created_ids),
                "submission_ids": created_ids,
            }
        )
        return Response(response_data, status=status.HTTP_201_CREATED)


class OmrUploadBatchListView(APIView):
    permission_classes = [IsAuthenticated, TenantResolvedAndStaff]

    @extend_schema(responses={200: OmrUploadBatchSummarySerializer(many=True)})
    def get(self, request):
        tenant = getattr(request, "tenant", None)
        if tenant is None:
            return Response([], status=status.HTTP_200_OK)
        cutoff = timezone.now() - timedelta(days=7)
        batches = (
            OmrUploadBatch.objects.filter(
                tenant=tenant,
                created_by=request.user,
                created_at__gte=cutoff,
            )
            .prefetch_related("items__submission")
            .order_by("-created_at")[:100]
        )
        return Response([_batch_summary(batch) for batch in batches], status=status.HTTP_200_OK)


class OmrUploadBatchDetailView(APIView):
    permission_classes = [IsAuthenticated, TenantResolvedAndStaff]

    @extend_schema(responses={200: OmrUploadBatchSummarySerializer})
    def get(self, request, batch_id: UUID):
        batch = _owned_batch(request, batch_id)
        if batch is None:
            return Response({"detail": "OMR 등록 작업을 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)
        return Response(_batch_summary(batch), status=status.HTTP_200_OK)


class OmrUploadBatchRetryView(APIView):
    permission_classes = [IsAuthenticated, TenantResolvedAndStaff]

    @extend_schema(
        request=inline_serializer(
            name="OmrUploadBatchRetryRequest",
            fields={
                "item_ordinals": serializers.ListField(
                    child=serializers.IntegerField(min_value=1, max_value=MAX_FILES)
                ),
            },
        ),
        responses={200: OmrUploadBatchRetryResultSerializer},
    )
    def post(self, request, batch_id: UUID):
        raw_ordinals = request.data.get("item_ordinals")
        if not isinstance(raw_ordinals, list) or not raw_ordinals:
            return Response({"detail": "item_ordinals is required"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            ordinals = [_positive_int(value, field="item_ordinals") for value in raw_ordinals]
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        if len(set(ordinals)) != len(ordinals):
            return Response({"detail": "item_ordinals must be unique"}, status=status.HTTP_400_BAD_REQUEST)

        retried: list[int] = []
        requires_file: list[int] = []
        skipped: list[int] = []
        with transaction.atomic():
            batch = (
                OmrUploadBatch.objects.select_for_update()
                .filter(
                    id=batch_id,
                    tenant=getattr(request, "tenant", None),
                    created_by=request.user,
                )
                .first()
            )
            if batch is None:
                return Response({"detail": "OMR 등록 작업을 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)
            locked_items = list(
                OmrUploadBatchItem.objects.select_for_update()
                .filter(batch=batch, ordinal__in=ordinals)
                .order_by("ordinal")
            )
            items = {int(item.ordinal): item for item in locked_items}
            submission_ids = sorted(
                item.submission_id
                for item in locked_items
                if item.submission_id is not None
            )
            submissions = {
                int(submission.id): submission
                for submission in Submission.objects.select_for_update()
                .filter(id__in=submission_ids, tenant=batch.tenant)
                .order_by("id")
            }
            for ordinal in ordinals:
                item = items.get(int(ordinal))
                if item is None:
                    skipped.append(int(ordinal))
                    continue
                submission = submissions.get(int(item.submission_id)) if item.submission_id else None
                if item.admission_status == OmrUploadBatchItem.AdmissionStatus.FAILED or submission is None:
                    requires_file.append(int(ordinal))
                    continue
                if submission.status != Submission.Status.FAILED or not submission.file_key:
                    skipped.append(int(ordinal))
                    continue
                try:
                    retry_failed_submission(submission, actor="admin.omr_batch_retry")
                except InvalidTransitionError:
                    skipped.append(int(ordinal))
                    continue
                dispatch_submission(submission)
                retried.append(int(ordinal))

        response_data = _batch_summary(batch)
        response_data.update(
            {
                "retried_ordinals": retried,
                "requires_file_ordinals": requires_file,
                "skipped_ordinals": skipped,
            }
        )
        return Response(response_data, status=status.HTTP_200_OK)


class OmrUploadBatchCompletionClaimView(APIView):
    permission_classes = [IsAuthenticated, TenantResolvedAndStaff]

    @extend_schema(
        request=None,
        responses={
            200: inline_serializer(
                name="OmrUploadBatchCompletionClaimResponse",
                fields={
                    "notify": serializers.BooleanField(),
                    "batch": OmrUploadBatchSummarySerializer(),
                },
            )
        },
    )
    def post(self, request, batch_id: UUID):
        with transaction.atomic():
            batch = (
                OmrUploadBatch.objects.select_for_update()
                .filter(
                    id=batch_id,
                    tenant=getattr(request, "tenant", None),
                    created_by=request.user,
                )
                .first()
            )
            if batch is None:
                return Response({"detail": "OMR 등록 작업을 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)
            summary = _batch_summary(batch)
            if not summary["terminal"]:
                return Response(
                    {"detail": "OMR 처리가 아직 끝나지 않았습니다.", "batch": summary},
                    status=status.HTTP_409_CONFLICT,
                )
            notify = batch.completion_notice_claimed_at is None
            if notify:
                batch.completion_notice_claimed_at = timezone.now()
                batch.save(update_fields=["completion_notice_claimed_at", "updated_at"])
                summary = _batch_summary(batch)
        return Response({"notify": notify, "batch": summary}, status=status.HTTP_200_OK)
