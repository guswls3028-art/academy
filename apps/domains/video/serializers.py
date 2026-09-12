
from django.conf import settings
from django.utils import timezone
from rest_framework import serializers

from .models import (
    Video,
    AccessMode,
    VideoAccess,
    InactiveVideoEntitlement,
    VideoProgress,
    VideoPlaybackEvent,
    VideoPlaybackSession,
    VideoFolder,
)
from .encoding_progress import (
    get_video_encoding_snapshot,
)
from academy.adapters.db.django import repositories_video as video_repo
from .policy import normalize_video_max_speed
from .youtube import youtube_thumbnail_url

# ========================================================
# Video
# ========================================================

class PublicVideoSessionSerializer(serializers.Serializer):
    session_id = serializers.IntegerField(min_value=1)
    lecture_id = serializers.IntegerField(min_value=1)


class VideoSerializer(serializers.ModelSerializer):
    """
    ✅ FINAL SEALED VERSION (SaaS production)

    - DB 저장 필드와 API 노출 필드 분리
    - CDN 기반 URL 동적 생성
    - Cache-busting 지원
    - Legacy 경로 normalize
    """

    # write — queryset is overridden in __init__ to enforce tenant isolation
    session = serializers.PrimaryKeyRelatedField(
        queryset=video_repo.session_all_queryset(),
        write_only=True,
    )

    # read
    session_id = serializers.IntegerField(
        source="session.id",
        read_only=True,
    )
    folder = serializers.IntegerField(source="folder_id", read_only=True)

    # CDN derived
    thumbnail_url = serializers.SerializerMethodField()
    hls_url = serializers.SerializerMethodField()

    # Redis 기반 인코딩 진행률 (status=PROCESSING 일 때만 유의미)
    encoding_progress = serializers.SerializerMethodField()
    encoding_remaining_seconds = serializers.SerializerMethodField()
    # 구간별 진행률 (n/7) 단계명 + 구간 내 0~100%
    encoding_step_index = serializers.SerializerMethodField()
    encoding_step_total = serializers.SerializerMethodField()
    encoding_step_name = serializers.SerializerMethodField()
    encoding_step_percent = serializers.SerializerMethodField()

    class Meta:
        model = Video
        fields = [
            "id",
            "session",
            "session_id",
            "folder",
            "title",
            "source_type",
            "file_key",
            "youtube_video_id",
            "youtube_url",
            "duration",
            "order",
            "status",
            "error_reason",
            "encoding_progress",
            "encoding_remaining_seconds",
            "encoding_step_index",
            "encoding_step_total",
            "encoding_step_name",
            "encoding_step_percent",
            "allow_skip",
            "max_speed",
            "show_watermark",
            "thumbnail",
            "thumbnail_r2_key",
            "thumbnail_url",
            "hls_path",
            "hls_url",
            "visibility",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "session_id",
            "source_type",
            "youtube_video_id",
            "youtube_url",
            "created_at",
            "updated_at",
            "thumbnail",
            "thumbnail_r2_key",
            "hls_path",
            "thumbnail_url",
            "hls_url",
            "error_reason",
            "encoding_progress",
            "encoding_remaining_seconds",
            "encoding_step_index",
            "encoding_step_total",
            "encoding_step_name",
            "encoding_step_percent",
        ]
        ref_name = "SealedVideo"

    def validate_session(self, value):
        """🔐 크로스 테넌트 세션 연결 방지: 세션이 요청 테넌트 소속인지 확인."""
        request = self.context.get("request")
        if request:
            tenant = getattr(request, "tenant", None)
            if tenant and hasattr(value, "lecture") and value.lecture:
                if getattr(value.lecture, "tenant_id", None) != tenant.id:
                    raise serializers.ValidationError(
                        "Session does not belong to your program."
                    )
        return value

    def validate_max_speed(self, value):
        try:
            return normalize_video_max_speed(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc

    # ---------------------------
    # helpers
    # ---------------------------

    def get_encoding_progress(self, obj):
        """PROCESSING 상태일 때만 Redis에서 진행률 조회 (0..100 또는 null)."""
        return self._get_encoding_snapshot(obj).get("progress")

    def get_encoding_remaining_seconds(self, obj):
        """PROCESSING 상태일 때만 Redis에서 예상 남은 시간(초) 조회."""
        return self._get_encoding_snapshot(obj).get("remaining_seconds")

    def get_encoding_step_index(self, obj):
        d = self._get_encoding_step_detail(obj)
        return d.get("step_index") if d else None

    def get_encoding_step_total(self, obj):
        d = self._get_encoding_step_detail(obj)
        return d.get("step_total") if d else None

    def get_encoding_step_name(self, obj):
        d = self._get_encoding_step_detail(obj)
        return d.get("step_name_display") if d else None

    def get_encoding_step_percent(self, obj):
        d = self._get_encoding_step_detail(obj)
        return d.get("step_percent") if d else None

    def _get_encoding_step_detail(self, obj):
        return self._get_encoding_snapshot(obj).get("step_detail")

    def _get_encoding_tenant_id(self, obj):
        tenant_id = getattr(obj, "tenant_id", None)
        if tenant_id is not None:
            return tenant_id
        try:
            return (
                obj.session.lecture.tenant_id
                if hasattr(obj, "session")
                and obj.session
                and hasattr(obj.session, "lecture")
                and obj.session.lecture
                else None
            )
        except Exception:
            return None

    def _get_encoding_snapshot(self, obj):
        if obj.status != Video.Status.PROCESSING:
            return {
                "progress": None,
                "remaining_seconds": None,
                "step_detail": None,
            }

        tenant_id = self._get_encoding_tenant_id(obj)
        cache = self.context.setdefault("_encoding_snapshot_cache", {})
        key = (tenant_id, int(obj.id))
        if key not in cache:
            cache[key] = get_video_encoding_snapshot(int(obj.id), tenant_id=tenant_id)
        return cache[key]

    def _cdn_base(self) -> str | None:
        base = getattr(settings, "CDN_HLS_BASE_URL", None)
        return base.rstrip("/") if base else None

    def _normalize_media_path(self, path: str) -> str:
        path = path.lstrip("/")

        if path.startswith("media/"):
            return path

        if path.startswith("storage/media/"):
            return path[len("storage/"):]

        return path

    def _cache_version(self, obj) -> int:
        try:
            return int(obj.updated_at.timestamp())
        except Exception:
            return 0

    def _build_cdn_url(self, cdn: str, rel_path: str, *, version: int) -> str:
        """
        cdn base + path 를 합치고, 서명 시크릿이 있으면 HMAC 쿼리를 부착한다.
        CDN Worker 가 sig/exp 를 검증하므로, signing_secret 가 비어 있으면
        unsigned URL 을 그대로 돌려 보내 R2 public 단계와 호환 유지.
        """
        path = "/" + rel_path.lstrip("/")
        secret = getattr(settings, "CDN_HLS_SIGNING_SECRET", "") or ""
        if not secret:
            return f"{cdn}{path}?v={version}"
        from .cdn.cloudflare_signing import CloudflareSignedURL
        from django.utils import timezone
        ttl = int(getattr(settings, "CDN_HLS_LIST_URL_TTL_SECONDS", 6 * 3600))
        expires_at = int(timezone.now().timestamp()) + ttl
        signer = CloudflareSignedURL(
            secret=str(secret),
            key_id=str(getattr(settings, "CDN_HLS_SIGNING_KEY_ID", "v1")),
        )
        return signer.build_url(
            cdn_base=cdn,
            path=path,
            expires_at=expires_at,
            user_id=None,
            extra_query={"v": str(version)},
        )

    # ---------------------------
    # CDN fields
    # ---------------------------

    def get_thumbnail_url(self, obj):
        if getattr(obj, "source_type", None) == Video.SourceType.YOUTUBE:
            video_id = (getattr(obj, "youtube_video_id", "") or "").strip()
            if video_id:
                return youtube_thumbnail_url(video_id)

        cdn = self._cdn_base()
        if not cdn:
            return None

        version = self._cache_version(obj)

        # 1️⃣ thumbnail_r2_key (SSOT — Worker가 항상 채움)
        r2_key = (getattr(obj, "thumbnail_r2_key", "") or "").strip()
        if r2_key:
            return self._build_cdn_url(cdn, self._normalize_media_path(r2_key), version=version)

        # 2️⃣ legacy ImageField (deprecated, 점진 제거)
        if obj.thumbnail:
            return self._build_cdn_url(cdn, self._normalize_media_path(obj.thumbnail.name), version=version)

        # 3️⃣ READY fallback — video.tenant_id 직접 (V1.1 SSOT). session 체인 폐기.
        if obj.status == obj.Status.READY:
            tenant_id = getattr(obj, "tenant_id", None)
            if tenant_id is None:
                return None
            from apps.core.r2_paths import video_hls_prefix
            rel = self._normalize_media_path(
                f"{video_hls_prefix(tenant_id=tenant_id, video_id=obj.id)}/thumbnail.jpg"
            )
            return self._build_cdn_url(cdn, rel, version=version)

        return None

    def get_hls_url(self, obj):
        if getattr(obj, "source_type", None) == Video.SourceType.YOUTUBE:
            return None

        if not obj.hls_path:
            return None

        cdn = self._cdn_base()
        if not cdn:
            return None

        return self._build_cdn_url(
            cdn,
            self._normalize_media_path(str(obj.hls_path)),
            version=self._cache_version(obj),
        )


class VideoDetailSerializer(VideoSerializer):
    can_retry = serializers.SerializerMethodField()

    class Meta(VideoSerializer.Meta):
        fields = VideoSerializer.Meta.fields + ["can_retry"]
        ref_name = "SealedVideoDetail"

    def get_can_retry(self, obj):
        """
        Retry 버튼 표시 여부를 서버에서 판단.
        - PENDING + file_key → True (upload-complete 재실행)
        - FAILED, UPLOADED → True
        - PROCESSING/READY + current_job RUNNING(not cancel_requested) → False
        - PROCESSING/READY + no active job or stale job → True
        """
        if getattr(obj, "source_type", None) == Video.SourceType.YOUTUBE:
            return False

        RETRY_ALLOWED = {
            Video.Status.PENDING,
            Video.Status.FAILED,
            Video.Status.UPLOADED,
            Video.Status.PROCESSING,
            Video.Status.READY,
        }

        st = obj.status
        if st not in RETRY_ALLOWED:
            return False

        if st == Video.Status.PENDING:
            return bool((obj.file_key or "").strip())

        # Check current job state
        job_id = getattr(obj, "current_job_id", None)
        if job_id:
            try:
                from datetime import timedelta
                from django.conf import settings
                from django.utils import timezone
                from apps.domains.video.models import VideoTranscodeJob
                cur = VideoTranscodeJob.objects.filter(pk=job_id).only(
                    "state", "cancel_requested", "last_heartbeat_at", "updated_at", "aws_batch_job_id",
                ).first()
                if cur and cur.state == VideoTranscodeJob.State.RUNNING and not getattr(cur, "cancel_requested", False):
                    # Allow retry if RUNNING job is stale (no heartbeat for too long)
                    stale_minutes = getattr(settings, "VIDEO_RETRY_STALE_RUNNING_MINUTES", 30)
                    last_activity = cur.last_heartbeat_at or cur.updated_at
                    if last_activity >= timezone.now() - timedelta(minutes=stale_minutes):
                        return False
                    # Stale RUNNING → allow retry
                if cur and cur.state in (VideoTranscodeJob.State.QUEUED, VideoTranscodeJob.State.RETRY_WAIT):
                    if not (cur.aws_batch_job_id or "").strip():
                        return True
                    stale_hours = getattr(settings, "VIDEO_RETRY_STALE_QUEUED_HOURS", 1)
                    if cur.updated_at < timezone.now() - timedelta(hours=stale_hours):
                        return True
                    return False
            except Exception:
                pass

        return True


# ========================================================
# Permission / Progress
# ========================================================

class VideoAccessSerializer(serializers.ModelSerializer):
    """API uses access_mode (SSOT). DB table kept as video_videopermission."""
    student_name = serializers.CharField(
        source="enrollment.student.name",
        read_only=True,
    )

    class Meta:
        model = VideoAccess
        fields = "__all__"
        ref_name = "SealedVideoAccess"


# Backward compat alias
VideoPermissionSerializer = VideoAccessSerializer


class InactiveVideoEntitlementGrantSerializer(serializers.Serializer):
    student_id = serializers.IntegerField(min_value=1)
    enrollment_id = serializers.IntegerField(min_value=1)
    video_id = serializers.IntegerField(min_value=1)
    access_mode = serializers.ChoiceField(
        choices=[AccessMode.FREE_REVIEW, AccessMode.PROCTORED_CLASS],
    )
    source = serializers.ChoiceField(
        choices=InactiveVideoEntitlement.Source.choices,
    )
    source_reference = serializers.CharField(max_length=128, allow_blank=False)
    reason = serializers.CharField(max_length=2000, allow_blank=False)
    expires_at = serializers.DateTimeField(required=False, allow_null=True)


class InactiveVideoEntitlementRevokeSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=2000, allow_blank=False)


class InactiveVideoEntitlementSerializer(serializers.ModelSerializer):
    state = serializers.SerializerMethodField()

    class Meta:
        model = InactiveVideoEntitlement
        fields = (
            "id",
            "tenant_id",
            "student_id",
            "enrollment_id",
            "video_id",
            "access_mode",
            "source",
            "source_reference",
            "reason",
            "granted_by_id",
            "granted_by_reference",
            "granted_at",
            "expires_at",
            "revoked_at",
            "revoked_by_id",
            "revoked_by_reference",
            "revoke_reason",
            "state",
        )
        read_only_fields = fields

    def get_state(self, obj) -> str:
        if obj.revoked_at is not None:
            return "REVOKED"
        if obj.expires_at is not None and obj.expires_at <= timezone.now():
            return "EXPIRED"
        if obj.source != InactiveVideoEntitlement.Source.STAFF_AUTHORIZATION:
            return "INELIGIBLE"
        if obj.enrollment.status != "INACTIVE":
            return "STAGED"
        from apps.domains.video.services.inactive_entitlements import (
            get_active_inactive_video_entitlement,
        )

        active = get_active_inactive_video_entitlement(
            video=obj.video,
            enrollment=obj.enrollment,
        )
        return "ACTIVE" if active is not None else "INELIGIBLE"


class InactiveVideoEntitlementMutationSerializer(serializers.Serializer):
    entitlement = InactiveVideoEntitlementSerializer()
    created = serializers.BooleanField()
    changed = serializers.BooleanField()


class InactiveVideoEntitlementErrorSerializer(serializers.Serializer):
    code = serializers.CharField(
        help_text=(
            "Stable machine code. YouTube grant/runtime failures use "
            "video_source_unsupported."
        )
    )
    detail = serializers.CharField()


class VideoProgressSerializer(serializers.ModelSerializer):
    student_name = serializers.CharField(
        source="enrollment.student.name",
        read_only=True,
    )

    # ✅ 진행률 % (프론트 표시용)
    progress_percent = serializers.SerializerMethodField()

    class Meta:
        model = VideoProgress
        fields = "__all__"
        ref_name = "SealedVideoProgress"

    def get_progress_percent(self, obj):
        try:
            return round(float(obj.progress or 0) * 100, 1)
        except Exception:
            return 0.0


# ========================================================
# Playback API
# ========================================================

class PlaybackStartRequestSerializer(serializers.Serializer):
    enrollment_id = serializers.IntegerField()
    device_id = serializers.CharField(max_length=128)
    event_protocol_version = serializers.ChoiceField(choices=[1, 2], default=1)


class PlaybackRefreshRequestSerializer(serializers.Serializer):
    token = serializers.CharField()


class PlaybackHeartbeatRequestSerializer(serializers.Serializer):
    token = serializers.CharField()


class PlaybackEndRequestSerializer(serializers.Serializer):
    token = serializers.CharField()


class PlaybackResponseSerializer(serializers.Serializer):
    token = serializers.CharField()
    event_protocol_version = serializers.ChoiceField(choices=[1, 2], default=1)
    session_id = serializers.CharField(allow_null=True, required=False)  # None for FREE_REVIEW
    expires_at = serializers.IntegerField(allow_null=True, required=False)  # None for FREE_REVIEW
    access_mode = serializers.ChoiceField(
        choices=["FREE_REVIEW", "PROCTORED_CLASS", "BLOCKED"],
        required=True,
    )
    monitoring_enabled = serializers.BooleanField()
    policy = serializers.JSONField()
    play_url = serializers.CharField()


class PlaybackRenewResponseSerializer(serializers.Serializer):
    ok = serializers.BooleanField()
    event_protocol_version = serializers.ChoiceField(choices=[1, 2], default=1)
    playback_token = serializers.CharField()
    playback_session_id = serializers.CharField(allow_null=True)
    playback_expires_at = serializers.IntegerField()
    access_mode = serializers.ChoiceField(
        choices=["FREE_REVIEW", "PROCTORED_CLASS"],
    )
    monitoring_enabled = serializers.BooleanField()
    policy_version = serializers.IntegerField(min_value=1)
    play_url = serializers.CharField(allow_null=True, required=False)


# ========================================================
# Events
# ========================================================

class PlaybackEventItemSerializer(serializers.Serializer):
    type = serializers.ChoiceField(
        choices=VideoPlaybackEvent.EventType.choices
    )
    occurred_at = serializers.IntegerField(required=False)
    payload = serializers.JSONField(required=False)


class PlaybackEventBatchRequestSerializer(serializers.Serializer):
    token = serializers.CharField()
    events = PlaybackEventItemSerializer(many=True)


class PlaybackEventBatchResponseSerializer(serializers.Serializer):
    stored = serializers.IntegerField()


class PlaybackV2StrictSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        if isinstance(data, dict) and set(data) - set(self.fields):
            raise serializers.ValidationError({"non_field_errors": ["알 수 없는 재생 기록 필드입니다."]})
        return super().to_internal_value(data)


class PlaybackV2EventSerializer(PlaybackV2StrictSerializer):
    type = serializers.ChoiceField(choices=VideoPlaybackEvent.EventType.choices)
    occurred_at = serializers.IntegerField(required=False)
    payload = serializers.JSONField(default=dict)

    def validate_payload(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("이벤트 내용은 객체여야 합니다.")
        return value


class PlaybackV2BatchSerializer(PlaybackV2StrictSerializer):
    batch_id = serializers.UUIDField()
    events = PlaybackV2EventSerializer(many=True, allow_empty=False, max_length=50)

    def validate(self, attrs):
        from .services.playback_event_batch import normalized_batch_bytes

        try:
            size = len(normalized_batch_bytes(attrs))
        except (TypeError, ValueError):
            raise serializers.ValidationError("이벤트 내용은 유효한 JSON이어야 합니다.")
        if size > 8 * 1024:
            raise serializers.ValidationError("재생 기록 배치가 8KiB를 초과했습니다.")
        return attrs


class PlaybackV2EventsRequestSerializer(PlaybackV2StrictSerializer):
    token = serializers.CharField()
    batch = PlaybackV2BatchSerializer()


class PlaybackV2EndRequestSerializer(PlaybackV2StrictSerializer):
    token = serializers.CharField()
    batches = PlaybackV2BatchSerializer(many=True, allow_empty=True, max_length=8)

    def validate_batches(self, value):
        ids = [batch["batch_id"] for batch in value]
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError("종료 요청에 같은 배치가 중복되었습니다.")
        if sum(len(batch["events"]) for batch in value) > 200:
            raise serializers.ValidationError("종료 요청은 이벤트 200건까지 허용합니다.")
        return value


class PlaybackV2AcknowledgementSerializer(serializers.Serializer):
    batch_id = serializers.UUIDField()
    event_count = serializers.IntegerField()
    duplicate = serializers.BooleanField()


class PlaybackV2ResponseSerializer(serializers.Serializer):
    protocol_version = serializers.IntegerField()
    session_status = serializers.ChoiceField(choices=VideoPlaybackSession.Status.choices)
    inserted_count = serializers.IntegerField()
    acknowledgements = PlaybackV2AcknowledgementSerializer(many=True)


# ========================================================
# Event List (Admin Analytics)
# ========================================================

class VideoPlaybackEventListSerializer(serializers.ModelSerializer):
    student_name = serializers.CharField(
        source="enrollment.student.name",
        read_only=True,
    )
    enrollment_id = serializers.IntegerField(
        source="enrollment.id",
        read_only=True,
    )

    severity = serializers.SerializerMethodField()
    score = serializers.SerializerMethodField()

    class Meta:
        model = VideoPlaybackEvent
        fields = [
            "id",
            "video",
            "enrollment_id",
            "student_name",
            "session_id",
            "user_id",
            "event_type",
            "violated",
            "violation_reason",
            "event_payload",
            "policy_snapshot",
            "occurred_at",
            "received_at",
            "severity",
            "score",
        ]
        ref_name = "SealedVideoPlaybackEventList"

    # ---------------------------
    # Risk classification
    # ---------------------------

    def get_severity(self, obj):
        base = {
            "SEEK_ATTEMPT": "warn",
            "SPEED_CHANGE_ATTEMPT": "warn",
            "FOCUS_LOST": "warn",
            "VISIBILITY_HIDDEN": "info",
            "PLAYER_ERROR": "info",
        }.get(obj.event_type, "info")

        return "danger" if obj.violated else base

    def get_score(self, obj):
        weights = {
            "VISIBILITY_HIDDEN": 1,
            "FOCUS_LOST": 2,
            "SEEK_ATTEMPT": 3,
            "SPEED_CHANGE_ATTEMPT": 3,
            "PLAYER_ERROR": 1,
        }

        w = int(weights.get(obj.event_type, 1))

        if obj.violated:
            w *= 2
        if obj.violation_reason:
            w += 1

        return w


# ========================================================
# Aggregated Risk Row
# ========================================================

class VideoRiskRowSerializer(serializers.Serializer):
    enrollment_id = serializers.IntegerField()
    student_name = serializers.CharField()
    score = serializers.IntegerField()
    danger = serializers.IntegerField()
    warn = serializers.IntegerField()
    info = serializers.IntegerField()
    last_occurred_at = serializers.DateTimeField(allow_null=True)


# ========================================================
# Video Folder
# ========================================================

class VideoFolderSerializer(serializers.ModelSerializer):
    """공개 영상 폴더 Serializer."""

    parent_id = serializers.IntegerField(source="parent.id", read_only=True, allow_null=True)
    session_id = serializers.IntegerField(source="session.id", read_only=True, allow_null=True)

    class Meta:
        model = VideoFolder
        fields = [
            "id",
            "name",
            "session_id",
            "parent_id",
            "order",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]
