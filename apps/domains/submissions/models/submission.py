# PATH: apps/domains/submissions/models/submission.py
from __future__ import annotations

from django.db import models
from django.conf import settings
from django.utils import timezone
from apps.api.common.models import TimestampModel
from apps.core.models import Tenant


class Submission(TimestampModel):
    class TargetType(models.TextChoices):
        EXAM = "exam", "Exam"
        HOMEWORK = "homework", "Homework"

    class Source(models.TextChoices):
        OMR_SCAN = "omr_scan", "OMR Scan"
        OMR_MANUAL = "omr_manual", "OMR Manual Input"
        ONLINE = "online", "Online"
        HOMEWORK_IMAGE = "homework_image", "Homework Image"
        HOMEWORK_VIDEO = "homework_video", "Homework Video"
        AI_MATCH = "ai_match", "AI Image Match"

    class Status(models.TextChoices):
        SUBMITTED = "submitted", "Submitted"
        DISPATCHED = "dispatched", "Dispatched"
        EXTRACTING = "extracting", "Extracting"
        ANSWERS_READY = "answers_ready", "Answers Ready"
        GRADING = "grading", "Grading"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"
        NEEDS_IDENTIFICATION = "needs_identification", "Needs Identification"
        SUPERSEDED = "superseded", "Superseded"  # 재응시로 대체됨

    # ──────────────────────────────────────────────
    # 상태 전이표 SSOT -> apps/domains/submissions/services/transition.py
    # 런타임 상태 변경은 services/lifecycle.py 의 의미 있는 public API로 수행.
    # ──────────────────────────────────────────────

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="submissions",
        db_index=True,  # ✅ tenant_id 인덱스 추가
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="submissions",
    )

    enrollment = models.ForeignKey(
        "enrollment.Enrollment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="enrollment_id",
        related_name="submissions",
    )

    target_type = models.CharField(max_length=20, choices=TargetType.choices)
    target_id = models.PositiveIntegerField()

    source = models.CharField(max_length=30, choices=Source.choices)

    file_key = models.CharField(max_length=512, null=True, blank=True)
    file_type = models.CharField(max_length=50, null=True, blank=True)
    file_size = models.PositiveIntegerField(null=True, blank=True)

    payload = models.JSONField(null=True, blank=True)

    status = models.CharField(
        max_length=30,
        choices=Status.choices,
        default=Status.SUBMITTED,
    )
    error_message = models.TextField(blank=True)
    meta = models.JSONField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["target_type", "target_id"]),
            models.Index(fields=["enrollment", "created_at"]),
            models.Index(fields=["user", "created_at"]),
            models.Index(fields=["status"]),
            models.Index(fields=["source"]),
            models.Index(fields=["tenant", "created_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "target_type", "target_id"],
                condition=models.Q(
                    status__in=[
                        "submitted", "dispatched", "extracting",
                        "answers_ready", "grading",
                    ],
                    # OMR batch upload: staff가 여러 학생 답안지를 업로드하므로
                    # 같은 user+exam에 복수 active submission 허용 필요
                    source__in=["online", "omr_manual", "homework_image",
                                "homework_video", "ai_match"],
                ),
                name="unique_active_submission_per_target",
            ),
        ]


class SubmissionMedia(TimestampModel):
    """Ordered, independently retryable media owned by one homework submission."""

    class Kind(models.TextChoices):
        IMAGE = "image", "Image"
        VIDEO = "video", "Video"

    class Status(models.TextChoices):
        UPLOADING = "uploading", "Uploading"
        UPLOADED = "uploaded", "Uploaded"
        FAILED = "failed", "Failed"
        REMOVED = "removed", "Removed"

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="submission_media",
    )
    submission = models.ForeignKey(
        Submission,
        on_delete=models.CASCADE,
        related_name="media_files",
    )
    client_upload_id = models.UUIDField()
    upload_batch_id = models.UUIDField()
    fingerprint = models.CharField(max_length=64)
    object_key = models.CharField(max_length=512)
    original_filename = models.CharField(max_length=255)
    media_kind = models.CharField(max_length=10, choices=Kind.choices)
    mime_type = models.CharField(max_length=100)
    size = models.PositiveBigIntegerField()
    position = models.PositiveSmallIntegerField()
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.UPLOADING,
    )
    error_message = models.TextField(blank=True)
    upload_started_at = models.DateTimeField(default=timezone.now)
    uploaded_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)
    removed_at = models.DateTimeField(null=True, blank=True)
    removed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="removed_submission_media",
    )

    class Meta:
        indexes = [
            models.Index(fields=["tenant", "submission", "status"]),
            models.Index(fields=["submission", "position", "id"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "client_upload_id"],
                name="uniq_submission_media_client_upload",
            ),
            models.UniqueConstraint(
                fields=["submission", "position"],
                condition=models.Q(removed_at__isnull=True),
                name="uniq_active_submission_media_position",
            ),
        ]


class SubmissionStorageCleanupIntent(TimestampModel):
    """Durable post-commit request to remove an unreferenced lifecycle object."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        FAILED = "failed", "Failed"
        CLEANED = "cleaned", "Cleaned"

    class Bucket(models.TextChoices):
        AI = "ai", "AI"
        STORAGE = "storage", "Storage"

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="submission_storage_cleanup_intents",
    )
    bucket = models.CharField(max_length=16, choices=Bucket.choices)
    object_key = models.CharField(max_length=512)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    attempt_count = models.PositiveIntegerField(default=0)
    claim_token = models.UUIDField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    cleaned_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "submissions_storage_cleanup_intent"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "bucket", "object_key"],
                name="uniq_sub_cleanup_tenant_bucket_key",
            ),
        ]
        indexes = [
            models.Index(
                fields=["tenant", "status", "created_at"],
                name="sub_cleanup_tenant_status_idx",
            ),
        ]
