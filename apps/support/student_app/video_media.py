"""Student-app video media URL helpers."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timezone as datetime_timezone
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


def _playback_access_now():
    return timezone.now()


@dataclass(frozen=True)
class PlaybackAccessGrant:
    token: str | None = None
    session_id: str | None = None
    expires_at: int | None = None
    access_mode: str | None = None
    monitoring_enabled: bool = False
    policy_version: int | None = None
    error: str | None = None
    status_code: int = 403


def bounded_inactive_media_expiry(entitlement, *, now=None) -> int:
    """Bound inactive media to the access TTL and optional entitlement expiry."""
    now = now or timezone.now()
    ttl = min(
        600,
        max(1, int(getattr(settings, "VIDEO_PLAYBACK_TTL_SECONDS", 600))),
    )
    expires_at = int(now.timestamp()) + ttl
    entitlement_expiry = getattr(entitlement, "expires_at", None)
    if entitlement_expiry is not None:
        expires_at = min(expires_at, int(entitlement_expiry.timestamp()))
    return expires_at


def bounded_direct_media_expiry(entitlement, *, now=None) -> int:
    """Direct access is always re-signed within the same 600 second ceiling."""
    return bounded_inactive_media_expiry(entitlement, now=now)


def bounded_ordinary_media_expiry(video, *, now=None) -> int:
    """Keep ordinary playback credentials valid for one complete viewing."""
    now = now or timezone.now()
    access_grace_seconds = max(
        1,
        int(getattr(settings, "VIDEO_PLAYBACK_TTL_SECONDS", 600)),
    )
    try:
        media_duration_seconds = max(0, int(video.duration or 0))
    except (TypeError, ValueError):
        media_duration_seconds = 0
    if media_duration_seconds == 0:
        media_duration_seconds = max(0, 24 * 60 * 60 - access_grace_seconds)
    return (
        int(now.timestamp())
        + media_duration_seconds
        + access_grace_seconds
    )


def build_thumbnail_url(video, *, expires_at: int | None = None) -> str | None:
    """Build the same thumbnail URL shape used by VideoSerializer."""
    if not video:
        return None

    try:
        from apps.domains.video.models import Video
        from apps.domains.video.youtube import youtube_thumbnail_url

        if getattr(video, "source_type", None) == Video.SourceType.YOUTUBE:
            video_id = (getattr(video, "youtube_video_id", "") or "").strip()
            return youtube_thumbnail_url(video_id) if video_id else None
    except Exception:
        pass

    cdn = getattr(settings, "CDN_HLS_BASE_URL", None)
    if not cdn:
        return None
    cdn = cdn.rstrip("/")

    def norm(path: str) -> str:
        path = path.lstrip("/")
        if path.startswith("storage/media/"):
            return path[len("storage/"):]
        return path

    def version() -> int:
        try:
            return int(video.updated_at.timestamp())
        except Exception:
            return 0

    def build(rel_path: str) -> str:
        path = "/" + rel_path.lstrip("/")
        version_value = version()
        secret = getattr(settings, "CDN_HLS_SIGNING_SECRET", "") or ""
        if not secret:
            return f"{cdn}{path}?v={version_value}"

        from apps.domains.video.cdn.cloudflare_signing import CloudflareSignedURL

        signed_expires_at = expires_at
        if signed_expires_at is None:
            ttl = int(getattr(settings, "CDN_HLS_LIST_URL_TTL_SECONDS", 6 * 3600))
            signed_expires_at = int(timezone.now().timestamp()) + ttl
        signer = CloudflareSignedURL(
            secret=str(secret),
            key_id=str(getattr(settings, "CDN_HLS_SIGNING_KEY_ID", "v1")),
        )
        return signer.build_url(
            cdn_base=cdn,
            path=path,
            expires_at=signed_expires_at,
            user_id=None,
            extra_query={"v": str(version_value)},
        )

    if getattr(video, "thumbnail", None):
        return build(norm(video.thumbnail.name))

    if getattr(video, "status", None) == video.Status.READY:
        try:
            session = getattr(video, "session", None)
            lecture = getattr(session, "lecture", None) if session else None
            tenant = getattr(lecture, "tenant", None) if lecture else None
            if tenant is None:
                return None
            tenant_id = getattr(tenant, "id", None) or getattr(tenant, "pk", None)
            from apps.core.r2_paths import video_hls_prefix

            return build(
                norm(f"{video_hls_prefix(tenant_id=tenant_id, video_id=video.id)}/thumbnail.jpg")
            )
        except Exception:
            return None

    return None


def pick_video_urls(
    video,
    request=None,
    *,
    expires_at: int | None = None,
) -> tuple[str | None, str | None]:
    """Return the public HLS URL and optional MP4 URL for student playback."""
    from apps.domains.video.views.playback_mixin import VideoPlaybackMixin

    if not hasattr(video, "status") or video.status != video.Status.READY:
        logger.warning(
            "[pick_video_urls] Video %s is not READY (status: %s)",
            getattr(video, "id", None),
            getattr(video, "status", "UNKNOWN"),
        )
        return None, None

    if expires_at is None:
        expires_at = bounded_ordinary_media_expiry(video)
    user = getattr(request, "user", None) if request else None
    user_id = getattr(user, "id", 0) if user and getattr(user, "is_authenticated", False) else 0

    try:
        tenant_id: Any = None
        try:
            session = getattr(video, "session", None)
            lecture = getattr(session, "lecture", None) if session else None
            tenant_id = getattr(lecture, "tenant_id", None) if lecture else None
        except Exception:
            pass

        logger.info(
            "[pick_video_urls] Generating URL for video %s: hls_path=%s, "
            "file_key=%s, tenant_id=%s, expires_at=%s, user_id=%s",
            getattr(video, "id", None),
            getattr(video, "hls_path", None),
            getattr(video, "file_key", None),
            tenant_id,
            expires_at,
            user_id,
        )

        hls_url = VideoPlaybackMixin()._public_play_url(
            video=video,
            expires_at=expires_at,
            user_id=user_id,
        )
        logger.info(
            "[pick_video_urls] Signed playback URL ready for video %s expires_at=%s",
            getattr(video, "id", None),
            expires_at,
        )
        if not hls_url:
            logger.warning(
                "[pick_video_urls] _public_play_url returned None for video %s",
                getattr(video, "id", None),
            )
            return None, None
    except Exception as exc:
        logger.error(
            "[pick_video_urls] Failed to generate HLS URL for video %s: %s",
            getattr(video, "id", None),
            exc,
            exc_info=True,
        )
        hls_url = None

    return hls_url, None


def issue_playback_access_grant(
    *,
    video,
    enrollment,
    user,
    device_id: str,
    request_id: str | None = None,
) -> PlaybackAccessGrant:
    """Atomically issue current access and a monitored session when required."""
    from academy.adapters.db.django import repositories_video as video_repo
    from academy.application.use_cases.student_video_access_context import (
        lecture_allows_student_learning,
    )
    from apps.domains.enrollment.models import Enrollment
    from apps.domains.lectures.models import Lecture
    from apps.domains.video.drm import create_playback_token
    from apps.domains.video.models import AccessMode, Video, VideoPlaybackSession
    from apps.domains.video.services.access_resolver import get_effective_access_mode
    from apps.domains.video.services.playback_session import (
        get_tenant_session_limits,
        init_session_redis,
        issue_session,
    )

    lecture_id = getattr(getattr(video, "session", None), "lecture_id", None)
    tenant_id = getattr(video, "tenant_id", None) or getattr(enrollment, "tenant_id", None)
    if not lecture_id or not tenant_id:
        return PlaybackAccessGrant(error="access_denied")

    with transaction.atomic():
        lecture = (
            Lecture.objects.select_for_update()
            .filter(id=lecture_id, tenant_id=tenant_id)
            .first()
        )
        if not lecture_allows_student_learning(lecture):
            return PlaybackAccessGrant(error="lecture_inactive")

        locked_enrollment = (
            Enrollment.objects.select_for_update(of=("self",))
            .filter(
                id=enrollment.id,
                tenant_id=tenant_id,
                lecture_id=lecture_id,
                status__in=("ACTIVE", "INACTIVE"),
            )
            .first()
        )
        if locked_enrollment is None:
            return PlaybackAccessGrant(error="enrollment_unavailable")

        current_video = (
            Video.objects.select_for_update(of=("self",))
            .filter(
                id=video.id,
                tenant_id=tenant_id,
                session__lecture_id=lecture_id,
                status=Video.Status.READY,
                deleted_at__isnull=True,
            )
            .first()
        )
        if current_video is None:
            return PlaybackAccessGrant(error="video_not_ready", status_code=404)

        access_mode = get_effective_access_mode(
            video=current_video,
            enrollment=locked_enrollment,
        )
        if access_mode == AccessMode.BLOCKED:
            return PlaybackAccessGrant(error="access_blocked")

        ttl = int(getattr(settings, "VIDEO_PLAYBACK_TTL_SECONDS", 600))
        access_now = _playback_access_now()
        expires_at = int(access_now.timestamp()) + ttl
        inactive_expires_at = None
        if locked_enrollment.status == "INACTIVE":
            from apps.domains.video.services.inactive_entitlements import (
                get_active_inactive_video_entitlement,
            )

            entitlement = get_active_inactive_video_entitlement(
                video=current_video,
                enrollment=locked_enrollment,
            )
            if entitlement is None:
                return PlaybackAccessGrant(error="access_blocked")
            now_timestamp = int(access_now.timestamp())
            inactive_expires_at = bounded_inactive_media_expiry(
                entitlement,
                now=access_now,
            )
            ttl = inactive_expires_at - now_timestamp
            if ttl <= 0:
                return PlaybackAccessGrant(error="access_expired")
        monitoring_enabled = access_mode == AccessMode.PROCTORED_CLASS
        playback_session_id = None
        if inactive_expires_at is not None:
            expires_at = inactive_expires_at
        if monitoring_enabled:
            max_sessions, max_devices = get_tenant_session_limits(lecture.tenant)
            ok, session_payload, error = issue_session(
                student_id=locked_enrollment.student_id,
                device_id=str(device_id),
                ttl_seconds=ttl,
                max_sessions=max_sessions,
                max_devices=max_devices,
                expires_at=inactive_expires_at,
            )
            if not ok or not session_payload:
                return PlaybackAccessGrant(
                    access_mode=access_mode.value,
                    monitoring_enabled=True,
                    policy_version=int(
                        getattr(current_video, "policy_version", 1) or 1
                    ),
                    error=error or "session_limit_exceeded",
                    status_code=409,
                )

            playback_session_id = session_payload["session_id"]
            session_expires_at = int(session_payload["expires_at"])
            session_expires_at_dt = timezone.datetime.fromtimestamp(
                session_expires_at,
                tz=datetime_timezone.utc,
            )
            video_repo.playback_session_create(
                video=current_video,
                enrollment=locked_enrollment,
                session_id=playback_session_id,
                device_id=str(device_id),
                status=VideoPlaybackSession.Status.ACTIVE,
                started_at=timezone.now(),
                expires_at=session_expires_at_dt,
                last_seen=timezone.now(),
                violated_count=0,
                total_count=0,
                is_revoked=False,
            )
        token_payload = {
            "video_id": current_video.id,
            "enrollment_id": locked_enrollment.id,
            "session_id": playback_session_id,
            "user_id": user.id,
            "student_id": locked_enrollment.student_id,
            "tenant_id": tenant_id,
            "access_mode": access_mode.value,
            "monitoring_enabled": monitoring_enabled,
            "pv": int(getattr(current_video, "policy_version", 1) or 1),
        }
        if request_id:
            token_payload["rid"] = request_id
        try:
            token = create_playback_token(
                payload=token_payload,
                expires_at=expires_at,
            )
        except ValueError:
            transaction.set_rollback(True)
            return PlaybackAccessGrant(error="access_expired")
        if playback_session_id:
            redis_ttl = ttl
            if inactive_expires_at is not None:
                redis_ttl = inactive_expires_at - int(timezone.now().timestamp())
                if redis_ttl <= 0:
                    transaction.set_rollback(True)
                    return PlaybackAccessGrant(error="access_expired")
            init_session_redis(
                session_id=playback_session_id,
                ttl_seconds=redis_ttl,
            )
        return PlaybackAccessGrant(
            token=token,
            session_id=playback_session_id,
            expires_at=expires_at,
            access_mode=access_mode.value,
            monitoring_enabled=monitoring_enabled,
            policy_version=int(getattr(current_video, "policy_version", 1) or 1),
        )


@transaction.atomic
def issue_direct_playback_access_grant(
    *,
    video,
    entitlement,
    user,
    request_id: str | None = None,
) -> PlaybackAccessGrant:
    """Issue a short-lived FREE_REVIEW token without enrollment or session writes."""
    from apps.domains.video.drm import create_playback_token
    from apps.domains.video.models import AccessMode
    from apps.domains.video.services.direct_entitlements import (
        DirectVideoEntitlementError,
        lock_and_revalidate_direct_video_access,
    )

    tenant = getattr(entitlement, "tenant", None)
    if tenant is None:
        return PlaybackAccessGrant(error="access_denied")
    try:
        locked = lock_and_revalidate_direct_video_access(
            tenant=tenant,
            student_id=entitlement.student_id,
            video_id=video.id,
            entitlement_id=entitlement.id,
        )
    except DirectVideoEntitlementError as exc:
        return PlaybackAccessGrant(error=exc.code)

    access_now = _playback_access_now()
    expires_at = bounded_direct_media_expiry(
        locked.entitlement,
        now=access_now,
    )
    payload = {
        "aud": "student-video-direct",
        "access_source": "DIRECT_VIDEO_ENTITLEMENT",
        "direct_entitlement_id": locked.entitlement.id,
        "video_id": locked.video.id,
        "user_id": user.id,
        "student_id": locked.student.id,
        "tenant_id": tenant.id,
        "access_mode": AccessMode.FREE_REVIEW,
        "monitoring_enabled": False,
        "pv": int(getattr(locked.video, "policy_version", 1) or 1),
    }
    if request_id:
        payload["rid"] = request_id
    try:
        token = create_playback_token(payload=payload, expires_at=expires_at)
    except ValueError:
        return PlaybackAccessGrant(error="access_expired")
    return PlaybackAccessGrant(
        token=token,
        session_id=None,
        expires_at=expires_at,
        access_mode=AccessMode.FREE_REVIEW,
        monitoring_enabled=False,
        policy_version=int(getattr(locked.video, "policy_version", 1) or 1),
    )
