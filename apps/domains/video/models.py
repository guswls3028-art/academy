import uuid

from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.core.models.base import TimestampModel


# ========================================================
# Soft-delete managers for Video
# ========================================================

class VideoManager(models.Manager):
    """Default manager: excludes soft-deleted videos."""

    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)


class VideoAllManager(models.Manager):
    """Manager that includes soft-deleted videos."""
    pass


# ========================================================
# Access Mode (Video Access Policy)
# ========================================================

class AccessMode(models.TextChoices):
    """
    Video access mode enum.
    
    - FREE_REVIEW: Review mode without monitoring; saved playback controls still apply
    - PROCTORED_CLASS: Proctored class mode (restrictions apply)
    - BLOCKED: Access blocked
    """
    FREE_REVIEW = "FREE_REVIEW", "복습"
    PROCTORED_CLASS = "PROCTORED_CLASS", "온라인 수업 대체"
    BLOCKED = "BLOCKED", "제한"


# ========================================================
# Video (영상 메타데이터)
# ========================================================

class Video(TimestampModel):
    class Status(models.TextChoices):
        PENDING = "PENDING", "업로드 대기"
        UPLOADED = "UPLOADED", "업로드 완료"
        PROCESSING = "PROCESSING", "처리중"
        READY = "READY", "사용 가능"
        FAILED = "FAILED", "실패"

    class SourceType(models.TextChoices):
        UPLOADED = "s3", "직접 업로드"
        YOUTUBE = "youtube", "YouTube 링크"

    class Visibility(models.TextChoices):
        ENROLLED = "ENROLLED", "수강생 전용"
        PUBLIC = "PUBLIC", "전체 공개"

    # --------------------------------------------------
    # Soft-delete support (6-month retention)
    # --------------------------------------------------
    deleted_at = models.DateTimeField(
        null=True,
        blank=True,
        default=None,
        db_index=True,
        help_text="Soft-delete timestamp. Non-null means the video is logically deleted.",
    )

    objects = VideoManager()
    all_with_deleted = VideoAllManager()

    visibility = models.CharField(
        max_length=10,
        choices=Visibility.choices,
        default=Visibility.ENROLLED,
        db_index=True,
        help_text="접근 범위: ENROLLED(수강생 전용) / PUBLIC(테넌트 내 전체 학생)",
    )

    tenant = models.ForeignKey(
        "core.Tenant",
        on_delete=models.CASCADE,
        related_name="videos",
        db_index=True,
        help_text="영상 소유 테넌트 (SSOT, session→lecture→tenant 체인 대체)",
    )

    session = models.ForeignKey(
        "lectures.Session",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="videos",
        help_text="SET_NULL: Session 삭제 시 soft-delete 보존 (6개월 retention 보장)",
    )

    folder = models.ForeignKey(
        "VideoFolder",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="videos",
        help_text="공개 영상 폴더 (일반 차시 영상은 null)",
    )

    title = models.CharField(max_length=255)

    # ===============================
    # SaaS upload (Source of Truth)
    # ===============================
    source_type = models.CharField(
        max_length=20,
        choices=SourceType.choices,
        default=SourceType.UPLOADED,
        db_index=True,
        help_text="영상 소스: s3(직접 업로드) / youtube(YouTube 링크)",
    )

    file_key = models.CharField(
        max_length=500,
        blank=True,
        help_text="S3 object key (presigned upload)",
    )

    youtube_video_id = models.CharField(
        max_length=32,
        blank=True,
        default="",
        db_index=True,
        help_text="YouTube video id. source_type=youtube일 때만 사용.",
    )

    youtube_url = models.URLField(
        max_length=500,
        blank=True,
        default="",
        help_text="Canonical YouTube watch URL. source_type=youtube일 때만 사용.",
    )

    duration = models.PositiveIntegerField(null=True, blank=True)
    order = models.PositiveIntegerField(default=1)

    # 조회수 / 좋아요 (denormalized counters)
    view_count = models.PositiveIntegerField(default=0, help_text="재생 시작 횟수")
    like_count = models.PositiveIntegerField(default=0, help_text="좋아요 수")
    comment_count = models.PositiveIntegerField(default=0, help_text="댓글 수")

    # 썸네일은 Worker가 생성
    # legacy ImageField (Django default storage 가정). 신규 코드는 thumbnail_r2_key 사용.
    thumbnail = models.ImageField(
        upload_to="thumbnails/",
        null=True,
        blank=True,
    )

    # Worker가 R2에 올린 thumbnail.jpg 의 key (SSOT). complete_video 시 채워짐.
    thumbnail_r2_key = models.CharField(
        max_length=500,
        blank=True,
        default="",
        help_text="R2 key of generated thumbnail. Worker invariant: 항상 채워짐.",
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )

    uploaded_by = models.ForeignKey(
        "staffs.Staff",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_videos",
        help_text="영상을 업로드한 스태프. 인코딩 완료 알림 발송 대상.",
    )

    # --------------------------------------------------
    # 기본 재생 정책 (비디오 단위 default)
    # --------------------------------------------------
    allow_skip = models.BooleanField(default=False)
    max_speed = models.FloatField(default=1.0)
    show_watermark = models.BooleanField(default=True)

    # --------------------------------------------------
    # 정책 변경 즉시 반영을 위한 버전 (token versioning)
    # - 기존 API 계약 깨지지 않게 default=1
    # - 정책/권한 변경 시 증가시키면, 기존 토큰 즉시 무효화 가능
    # --------------------------------------------------
    policy_version = models.PositiveIntegerField(
        default=1,
        db_index=True,
        help_text="Increment on policy/permission changes to invalidate existing tokens",
    )

    # --------------------------------------------------
    # Worker 실패 사유 기록
    # --------------------------------------------------
    error_reason = models.TextField(blank=True, null=True, default="")

    # ===============================
    # HLS Output (Worker 결과)
    # ===============================
    hls_path = models.CharField(
        max_length=500,
        blank=True,
        help_text="HLS master playlist path (relative to CDN root)",
    )

    # --------------------------------------------------
    # Worker Lease (다중 노드 중복 처리 방지용)
    # - Job 기반 마이그레이션 후에도 일부 경로에서 사용 가능.
    # --------------------------------------------------
    processing_started_at = models.DateTimeField(null=True, blank=True)
    leased_until = models.DateTimeField(null=True, blank=True)
    leased_by = models.CharField(max_length=64, blank=True, default="")

    # --------------------------------------------------
    # Job 기반 실행 (Enterprise)
    # - current_job: 최신/진행 중 transcoding Job. 결과 반영 시 SUCCEEDED/FAILED.
    # - "processing" 의미: current_job 존재 + state in (QUEUED, RUNNING, RETRY_WAIT)
    # --------------------------------------------------
    current_job = models.ForeignKey(
        "VideoTranscodeJob",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="현재 transcoding Job (진행 중 또는 최종)",
    )

    class Meta:
        ordering = ["order", "title", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "folder", "order"],
                condition=Q(deleted_at__isnull=True, folder__isnull=False),
                name="uniq_active_video_order_per_folder",
            ),
            models.UniqueConstraint(
                fields=["tenant", "session", "order"],
                condition=Q(
                    deleted_at__isnull=True,
                    folder__isnull=True,
                    session__isnull=False,
                ),
                name="uniq_active_video_order_per_session",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "updated_at"]),
            models.Index(fields=["leased_until", "status"]),
        ]

    def __str__(self):
        return f"[{self.status}] {self.title}"

    def delete(self, using=None, keep_parents=False):
        """Soft-delete: set deleted_at instead of removing the row."""
        self.deleted_at = timezone.now()
        self.save(update_fields=["deleted_at", "updated_at"])

    def hard_delete(self, using=None, keep_parents=False):
        """Permanently remove the row from the database."""
        super().delete(using=using, keep_parents=keep_parents)

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    @property
    def is_youtube_source(self) -> bool:
        return self.source_type == self.SourceType.YOUTUBE


# ========================================================
# VideoTranscodeJob (Job 기반 실행)
# ========================================================


class VideoTranscodeJob(models.Model):
    """
    Transcoding 실행 단위. Video는 Resource, Job은 Execution.
    SQS 메시지에 job_id 포함 → Worker는 job_id 기반으로 claim/처리.
    """

    class State(models.TextChoices):
        QUEUED = "QUEUED", "대기"
        RUNNING = "RUNNING", "실행중"
        SUCCEEDED = "SUCCEEDED", "완료"
        FAILED = "FAILED", "실패"
        RETRY_WAIT = "RETRY_WAIT", "재시도대기"
        DEAD = "DEAD", "격리"
        CANCELLED = "CANCELLED", "취소됨"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    video = models.ForeignKey(
        Video,
        on_delete=models.CASCADE,
        related_name="transcode_jobs",
    )
    tenant = models.ForeignKey(
        "core.Tenant",
        on_delete=models.CASCADE,
        related_name="video_transcode_jobs",
        db_index=True,
    )

    state = models.CharField(
        max_length=20,
        choices=State.choices,
        default=State.QUEUED,
        db_index=True,
    )
    attempt_count = models.PositiveIntegerField(default=1)
    cancel_requested = models.BooleanField(default=False)
    locked_by = models.CharField(max_length=64, blank=True)
    locked_until = models.DateTimeField(null=True, blank=True)
    last_heartbeat_at = models.DateTimeField(null=True, blank=True)

    error_code = models.CharField(max_length=64, blank=True)
    error_message = models.TextField(blank=True)

    # AWS Batch 제출 추적 (디버깅/관측용)
    aws_batch_job_id = models.CharField(max_length=256, blank=True, db_index=True)
    # 동일 AWS Batch 실패를 reconcile/scan_stuck/worker가 중복 카운트하지 않기 위한 marker
    last_counted_failure_aws_batch_job_id = models.CharField(max_length=256, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["state", "updated_at"]),
            models.Index(fields=["tenant_id", "state"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["video"],
                condition=Q(state__in=["QUEUED", "RUNNING", "RETRY_WAIT"]),
                name="unique_video_active_job",
            ),
        ]

    def __str__(self):
        return f"Job {self.id} [{self.state}] video={self.video_id}"


# ========================================================
# OpsEvent (운영 이벤트 — 관측/알림)
# ========================================================


class VideoOpsEvent(models.Model):
    """운영 이벤트: JOB_DEAD, BATCH_DESYNC, UPLOAD_INTEGRITY_FAIL, ORPHAN_CANCELLED, TENANT_LIMIT_EXCEEDED 등."""

    class EventType(models.TextChoices):
        JOB_DEAD = "JOB_DEAD", "Job DEAD"
        BATCH_DESYNC = "BATCH_DESYNC", "Batch-DB 불일치"
        UPLOAD_INTEGRITY_FAIL = "UPLOAD_INTEGRITY_FAIL", "R2 업로드 무결성 실패"
        ORPHAN_CANCELLED = "ORPHAN_CANCELLED", "Orphan Batch job 취소"
        TENANT_LIMIT_EXCEEDED = "TENANT_LIMIT_EXCEEDED", "테넌트 동시 실행 한도 초과"
        VIDEO_DELETE_TERMINATE_REQUESTED = "VIDEO_DELETE_TERMINATE_REQUESTED", "영상 삭제 시 Batch 종료 요청"
        VIDEO_DELETE_TERMINATE_FAILED = "VIDEO_DELETE_TERMINATE_FAILED", "영상 삭제 시 Batch 종료 실패"

    class Severity(models.TextChoices):
        INFO = "INFO", "Info"
        WARNING = "WARNING", "Warning"
        ERROR = "ERROR", "Error"

    type = models.CharField(max_length=64, choices=EventType.choices, db_index=True)
    severity = models.CharField(max_length=16, choices=Severity.choices, default=Severity.WARNING, db_index=True)
    tenant = models.ForeignKey(
        "core.Tenant",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="video_ops_events",
        db_index=True,
    )
    video_id = models.PositiveIntegerField(null=True, blank=True, db_index=True)  # 로그 보존: 삭제된 video 참조 허용
    job_id = models.UUIDField(null=True, blank=True, db_index=True)
    aws_batch_job_id = models.CharField(max_length=256, blank=True, db_index=True)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["type", "created_at"]),
        ]

    def __str__(self):
        return f"{self.type} {self.created_at} video={self.video_id} job={self.job_id}"
# - Replaces VideoPermission semantics (SSOT)
# - DB table kept as video_videopermission for migration safety
# ========================================================

class VideoAccess(models.Model):
    video = models.ForeignKey(
        Video,
        on_delete=models.CASCADE,
        related_name="permissions",
    )
    enrollment = models.ForeignKey(
        "enrollment.Enrollment",
        on_delete=models.CASCADE,
        related_name="video_permissions",
    )

    # Legacy field (deprecated, use access_mode instead)
    rule = models.CharField(
        max_length=20,
        choices=[
            ("free", "무제한"),
            ("once", "1회 제한"),
            ("blocked", "제한"),
        ],
        default="free",
        null=True,
        blank=True,
        help_text="DEPRECATED: Use access_mode instead",
    )

    access_mode = models.CharField(
        max_length=20,
        choices=AccessMode.choices,
        default=AccessMode.FREE_REVIEW,
        db_index=True,
        help_text="Access mode: FREE_REVIEW, PROCTORED_CLASS, or BLOCKED",
    )

    allow_skip_override = models.BooleanField(null=True, blank=True)
    max_speed_override = models.FloatField(null=True, blank=True)
    show_watermark_override = models.BooleanField(null=True, blank=True)

    block_speed_control = models.BooleanField(default=False)
    block_seek = models.BooleanField(default=False)

    is_override = models.BooleanField(default=False)

    # Set when PROCTORED_CLASS watch is completed -> auto-upgrade to FREE_REVIEW
    proctored_completed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the monitored class-substitute watch was completed",
    )

    class Meta:
        db_table = "video_videopermission"  # Keep existing table name
        constraints = [
            models.UniqueConstraint(
                fields=["video", "enrollment"],
                name="unique_video_permission",
            )
        ]

    def __str__(self):
        return f"{self.enrollment.student.name} {self.video.title} ({self.access_mode})"


# Backward compatibility alias
VideoPermission = VideoAccess


class InactiveVideoEntitlement(TimestampModel):
    """Explicit, auditable access to one video on an inactive enrollment."""

    class Source(models.TextChoices):
        STAFF_AUTHORIZATION = "STAFF_AUTHORIZATION", "교직원 명시 승인"

    tenant = models.ForeignKey(
        "core.Tenant",
        on_delete=models.CASCADE,
        related_name="inactive_video_entitlements",
    )
    student = models.ForeignKey(
        "students.Student",
        on_delete=models.CASCADE,
        related_name="inactive_video_entitlements",
    )
    enrollment = models.ForeignKey(
        "enrollment.Enrollment",
        on_delete=models.CASCADE,
        related_name="inactive_video_entitlements",
    )
    video = models.ForeignKey(
        Video,
        on_delete=models.CASCADE,
        related_name="inactive_enrollment_entitlements",
    )
    access_mode = models.CharField(
        max_length=20,
        choices=(
            (AccessMode.FREE_REVIEW, AccessMode.FREE_REVIEW.label),
            (AccessMode.PROCTORED_CLASS, AccessMode.PROCTORED_CLASS.label),
        ),
    )
    source = models.CharField(max_length=32, choices=Source.choices)
    source_reference = models.CharField(max_length=128)
    reason = models.TextField()
    granted_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="granted_inactive_video_entitlements",
    )
    granted_by_reference = models.CharField(max_length=128)
    granted_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="revoked_inactive_video_entitlements",
    )
    revoked_by_reference = models.CharField(max_length=128, blank=True, default="")
    revoke_reason = models.TextField(blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "enrollment", "video"],
                condition=Q(revoked_at__isnull=True),
                name="uniq_current_inactive_video_entitlement",
            ),
            models.CheckConstraint(
                condition=(
                    Q(access_mode=AccessMode.FREE_REVIEW)
                    | Q(access_mode=AccessMode.PROCTORED_CLASS)
                ),
                name="inactive_video_entitlement_mode_valid",
            ),
            models.CheckConstraint(
                condition=Q(source="STAFF_AUTHORIZATION"),
                name="inactive_video_entitlement_source_valid",
            ),
            models.CheckConstraint(
                condition=Q(expires_at__isnull=True) | Q(expires_at__gt=models.F("granted_at")),
                name="inactive_video_entitlement_expiry_valid",
            ),
            models.CheckConstraint(
                condition=~Q(source_reference=""),
                name="inactive_video_entitlement_source_ref_present",
            ),
            models.CheckConstraint(
                condition=~Q(reason=""),
                name="inactive_video_entitlement_reason_present",
            ),
            models.CheckConstraint(
                condition=~Q(granted_by_reference=""),
                name="inactive_video_entitlement_grant_ref_present",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        revoked_at__isnull=True,
                        revoked_by_reference="",
                        revoke_reason="",
                    )
                    | (
                        Q(revoked_at__isnull=False)
                        & ~Q(revoked_by_reference="")
                        & ~Q(revoke_reason="")
                    )
                ),
                name="inactive_video_entitlement_revoke_meta_present",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "student", "revoked_at"]),
            models.Index(fields=["enrollment", "video"]),
            models.Index(fields=["expires_at"]),
        ]

    def __str__(self):
        return f"inactive-entitlement enrollment={self.enrollment_id} video={self.video_id}"


class DirectVideoEntitlement(TimestampModel):
    """Explicit staff grant for one student and one video without enrollment."""

    class Source(models.TextChoices):
        STAFF_AUTHORIZATION = "STAFF_AUTHORIZATION", "교직원 명시 승인"

    tenant = models.ForeignKey(
        "core.Tenant",
        on_delete=models.CASCADE,
        related_name="direct_video_entitlements",
    )
    student = models.ForeignKey(
        "students.Student",
        on_delete=models.CASCADE,
        related_name="direct_video_entitlements",
    )
    video = models.ForeignKey(
        Video,
        on_delete=models.CASCADE,
        related_name="direct_student_entitlements",
    )
    source = models.CharField(max_length=32, choices=Source.choices)
    source_reference = models.CharField(max_length=128)
    reason = models.TextField()
    granted_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="granted_direct_video_entitlements",
    )
    granted_by_reference = models.CharField(max_length=128)
    granted_at = models.DateTimeField(default=timezone.now)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="revoked_direct_video_entitlements",
    )
    revoked_by_reference = models.CharField(max_length=128, blank=True, default="")
    revoke_reason = models.TextField(blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "student", "video"],
                condition=Q(revoked_at__isnull=True),
                name="uniq_current_direct_video_entitlement",
            ),
            models.CheckConstraint(
                condition=Q(source="STAFF_AUTHORIZATION"),
                name="direct_video_entitlement_source_valid",
            ),
            models.CheckConstraint(
                condition=~Q(source_reference=""),
                name="direct_video_entitlement_source_ref_present",
            ),
            models.CheckConstraint(
                condition=~Q(reason=""),
                name="direct_video_entitlement_reason_present",
            ),
            models.CheckConstraint(
                condition=~Q(granted_by_reference=""),
                name="direct_video_entitlement_grant_ref_present",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        revoked_at__isnull=True,
                        revoked_by_reference="",
                        revoke_reason="",
                    )
                    | (
                        Q(revoked_at__isnull=False)
                        & ~Q(revoked_by_reference="")
                        & ~Q(revoke_reason="")
                    )
                ),
                name="direct_video_entitlement_revoke_meta_present",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "student", "revoked_at"]),
            models.Index(fields=["video", "revoked_at"]),
        ]

    def __str__(self):
        return f"direct-entitlement student={self.student_id} video={self.video_id}"


# ========================================================
# Video Progress
# ========================================================

class VideoProgress(models.Model):
    video = models.ForeignKey(
        Video,
        on_delete=models.CASCADE,
        related_name="progresses",
    )
    enrollment = models.ForeignKey(
        "enrollment.Enrollment",
        on_delete=models.CASCADE,
        related_name="video_progress",
    )

    progress = models.FloatField(default=0)
    last_position = models.IntegerField(default=0)
    completed = models.BooleanField(default=False)
    forward_skip_seconds_used = models.PositiveIntegerField(
        default=0,
        db_default=0,
        help_text="온라인 수업 대체 모드에서 서버가 승인한 앞으로 건너뛰기 누적 초",
    )

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["video", "enrollment"],
                name="unique_video_progress",
            )
        ]

    def __str__(self):
        return (
            f"{self.enrollment.student.name} - "
            f"{self.video.title} ({self.progress * 100:.1f}%)"
        )


# ========================================================
# Video Playback Session (세션 / 감사)
# ========================================================

class VideoPlaybackSession(TimestampModel):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "활성"
        ENDED = "ENDED", "종료"
        REVOKED = "REVOKED", "차단"
        EXPIRED = "EXPIRED", "만료"

    video = models.ForeignKey(
        Video,
        on_delete=models.CASCADE,
        related_name="playback_sessions",
    )
    enrollment = models.ForeignKey(
        "enrollment.Enrollment",
        on_delete=models.CASCADE,
        related_name="playback_sessions",
    )

    session_id = models.CharField(max_length=64, db_index=True)
    device_id = models.CharField(max_length=128, db_index=True)

    # Retain the DB default: old API processes omit this column during rolling deployment.
    event_protocol_version = models.PositiveSmallIntegerField(default=1, db_default=1)

    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.ACTIVE,
        db_index=True,
    )

    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    
    # DB-based session management fields (Redis removal)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True, help_text="Session expiration time")
    last_seen = models.DateTimeField(null=True, blank=True, help_text="Last heartbeat time")
    violated_count = models.IntegerField(default=0, help_text="Number of violations")
    total_count = models.IntegerField(default=0, help_text="Total event count")
    is_revoked = models.BooleanField(default=False, db_index=True, help_text="Whether session is revoked")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["session_id"],
                name="uniq_video_playback_session_id",
            ),
            models.CheckConstraint(
                condition=models.Q(event_protocol_version__in=[1, 2]),
                name="playback_event_protocol_valid",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "started_at"]),
            models.Index(fields=["video", "enrollment", "status"]),
            models.Index(fields=["status", "expires_at"]),
            models.Index(fields=["enrollment", "status"]),
        ]

    def __str__(self):
        return f"{self.video_id}/{self.enrollment_id} {self.session_id} {self.status}"


# ========================================================
# Video Playback Event Batch (v2: exact-session idempotency)
# ========================================================

class VideoPlaybackEventBatch(models.Model):
    playback_session = models.ForeignKey(
        VideoPlaybackSession, on_delete=models.CASCADE, related_name="event_batches",
    )
    batch_id = models.UUIDField()
    payload_sha256 = models.CharField(max_length=64)
    event_count = models.PositiveSmallIntegerField()
    violated_count = models.PositiveSmallIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["playback_session", "batch_id"], name="playback_batch_identity_unique",
            ),
            models.CheckConstraint(
                condition=models.Q(event_count__gte=1, event_count__lte=50),
                name="playback_batch_count_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(violated_count__lte=models.F("event_count")),
                name="playback_batch_violations_valid",
            ),
        ]


# ========================================================
# Video Playback Event (audit)
# ========================================================

class VideoPlaybackEvent(TimestampModel):
    class EventType(models.TextChoices):
        VISIBILITY_HIDDEN = "VISIBILITY_HIDDEN", "탭 숨김"
        VISIBILITY_VISIBLE = "VISIBILITY_VISIBLE", "탭 노출"
        FOCUS_LOST = "FOCUS_LOST", "포커스 이탈"
        FOCUS_GAINED = "FOCUS_GAINED", "포커스 복귀"
        SEEK_ATTEMPT = "SEEK_ATTEMPT", "탐색 시도"
        SPEED_CHANGE_ATTEMPT = "SPEED_CHANGE_ATTEMPT", "배속 변경 시도"
        FULLSCREEN_ENTER = "FULLSCREEN_ENTER", "전체화면 진입"
        FULLSCREEN_EXIT = "FULLSCREEN_EXIT", "전체화면 종료"
        PLAYER_ERROR = "PLAYER_ERROR", "플레이어 오류"

    video = models.ForeignKey(
        Video,
        on_delete=models.CASCADE,
        related_name="playback_events",
    )
    enrollment = models.ForeignKey(
        "enrollment.Enrollment",
        on_delete=models.CASCADE,
        related_name="video_playback_events",
    )

    session_id = models.CharField(max_length=64, db_index=True)
    user_id = models.BigIntegerField(db_index=True)

    event_type = models.CharField(
        max_length=32,
        choices=EventType.choices,
        db_index=True,
    )

    event_payload = models.JSONField(default=dict, blank=True)
    policy_snapshot = models.JSONField(default=dict, blank=True)

    violated = models.BooleanField(default=False, db_index=True)
    violation_reason = models.CharField(max_length=64, blank=True, default="")

    occurred_at = models.DateTimeField(default=timezone.now)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["video", "enrollment", "session_id"], name="vpe_session_idx"),
            models.Index(fields=["user_id", "session_id"], name="video_playback_event_user_idx"),
            # 부분 인덱스: 위반 이벤트만 인덱싱 (INSERT 성능 향상, 인덱스 크기 50% 감소)
            models.Index(
                fields=["event_type", "received_at"],
                condition=models.Q(violated=True),
                name="vpe_violated_idx",
            ),
        ]
        ordering = ["-received_at", "-id"]

    def __str__(self):
        return f"{self.session_id} {self.event_type} v={self.violated}"


# ========================================================
# Video Folder (전체공개영상 내 폴더 구조)
# ========================================================

class VideoFolder(TimestampModel):
    """공개 영상 폴더 구조 (재귀 구조)."""

    tenant = models.ForeignKey(
        "core.Tenant",
        on_delete=models.CASCADE,
        related_name="video_folders",
        db_index=True,
        help_text="폴더 소유 테넌트",
    )
    session = models.ForeignKey(
        "lectures.Session",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="video_folders",
        help_text="레거시: 공개 영상 세션 (신규 폴더는 tenant 직접 참조)",
    )
    parent = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="children",
        help_text="상위 폴더 (null이면 루트 폴더)",
    )
    name = models.CharField(max_length=255, help_text="폴더 이름")
    order = models.PositiveIntegerField(default=0, help_text="정렬 순서")

    class Meta:
        ordering = ["order", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "name"],
                condition=Q(parent__isnull=True),
                name="uniq_root_video_folder_name_per_tenant",
            ),
            models.UniqueConstraint(
                fields=["tenant", "parent", "name"],
                condition=Q(parent__isnull=False),
                name="uniq_child_video_folder_name_per_tenant",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "parent"]),
        ]

    def __str__(self):
        return f"{self.tenant.code} / {self.name}"


# ========================================================
# VideoLike (영상 좋아요)
# ========================================================

class VideoLike(models.Model):
    video = models.ForeignKey(
        Video,
        on_delete=models.CASCADE,
        related_name="likes",
    )
    student = models.ForeignKey(
        "students.Student",
        on_delete=models.CASCADE,
        related_name="video_likes",
    )
    tenant = models.ForeignKey(
        "core.Tenant",
        on_delete=models.CASCADE,
        related_name="video_likes",
        db_index=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["video", "student"],
                name="unique_video_like",
            )
        ]
        indexes = [
            models.Index(fields=["tenant", "video"]),
        ]

    def __str__(self):
        return f"Like: {self.student_id} → {self.video_id}"


# ========================================================
# VideoComment (영상 댓글 · 대댓글)
# ========================================================

class VideoComment(models.Model):
    video = models.ForeignKey(
        Video,
        on_delete=models.CASCADE,
        related_name="comments",
    )
    tenant = models.ForeignKey(
        "core.Tenant",
        on_delete=models.CASCADE,
        related_name="video_comments",
        db_index=True,
    )

    # 작성자: 학생 또는 선생님 중 하나
    author_student = models.ForeignKey(
        "students.Student",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="video_comments",
    )
    author_staff = models.ForeignKey(
        "staffs.Staff",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="video_comments",
    )

    # 대댓글 (1단계만)
    parent = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="replies",
    )

    content = models.TextField(max_length=2000)
    is_edited = models.BooleanField(default=False)
    is_deleted = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["video", "created_at"]),
            models.Index(fields=["tenant_id", "video"]),
            models.Index(fields=["parent"]),
        ]

    def __str__(self):
        author = "student" if self.author_student_id else "staff"
        return f"Comment #{self.id} ({author}) on video {self.video_id}"

    @property
    def author_type(self) -> str:
        return "teacher" if self.author_staff_id else "student"

    @property
    def author_name(self) -> str:
        if self.author_staff:
            return self.author_staff.name
        if self.author_student:
            return self.author_student.name
        return ""

    @property
    def author_photo_url(self):
        # R2 presigned URL 사용 (로컬 .url은 프로덕션 404)
        if self.author_student:
            r2_key = getattr(self.author_student, "profile_photo_r2_key", None) or ""
            if r2_key:
                try:
                    from django.conf import settings as _s
                    from academy.adapters.storage.r2_presign import create_presigned_get_url
                    return create_presigned_get_url(r2_key, expires_in=3600, bucket=_s.R2_STORAGE_BUCKET)
                except Exception:
                    pass
        if self.author_staff:
            r2_key = getattr(self.author_staff, "profile_photo_r2_key", None) or ""
            if r2_key:
                try:
                    from django.conf import settings as _s
                    from academy.adapters.storage.r2_presign import create_presigned_get_url
                    return create_presigned_get_url(r2_key, expires_in=3600, bucket=_s.R2_STORAGE_BUCKET)
                except Exception:
                    pass
        return None
