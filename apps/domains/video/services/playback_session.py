
import uuid
from typing import Dict, Any, Tuple
from datetime import timedelta, timezone as datetime_timezone

from django.conf import settings
from django.utils import timezone
from django.db import transaction

from academy.adapters.db.django import repositories_video as video_repo
from academy.adapters.cache.redis_playback_session_buffer import (
    buffer_heartbeat_session_ttl,
    buffer_session_event,
    flush_session_buffer,
    flush_session_stats,
    get_session_violation_stats_redis,
    has_session_meta,
    init_session_redis,
    is_redis_available,
)
from apps.domains.video.models import VideoPlaybackSession

# =======================================================
# DB-based Session Management (Redis 제거됨)
# =======================================================


def _session_now():
    return timezone.now()


def _receipt_session(session_id, student_id=None):
    # Classify ownership before student filtering; a wrong owner must not turn a
    # known protocol2 session into a legacy Redis fallback.
    session = VideoPlaybackSession.objects.filter(session_id=session_id, event_protocol_version=2).first()
    if session is not None and student_id is not None and session.enrollment.student_id != student_id:
        from rest_framework.exceptions import PermissionDenied

        raise PermissionDenied("session_scope_mismatch")
    return session


def _cleanup_expired_sessions(student_id: int) -> None:
    video_repo.playback_session_cleanup_expired(student_id)


def get_tenant_session_limits(tenant) -> Tuple[int, int]:
    """
    테넌트 정책 우선, 미설정(0)이면 settings fallback.
    Returns (max_sessions, max_devices).
    """
    fallback_sessions = int(getattr(settings, "VIDEO_MAX_SESSIONS", 9999))
    fallback_devices = int(getattr(settings, "VIDEO_MAX_DEVICES", 9999))
    if tenant is None:
        return fallback_sessions, fallback_devices
    ms = int(getattr(tenant, "video_max_sessions", 0) or 0)
    md = int(getattr(tenant, "video_max_devices", 0) or 0)
    return (
        ms if ms > 0 else fallback_sessions,
        md if md > 0 else fallback_devices,
    )


def issue_session(
    *,
    student_id: int,
    device_id: str,
    ttl_seconds: int,
    max_sessions: int,
    max_devices: int,
    expires_at: int | None = None,
) -> Tuple[bool, Dict[str, Any] | None, str | None]:
    """
    DB 기반 재생 세션 발급
    """
    if not device_id:
        return False, None, "device_id_required"

    _cleanup_expired_sessions(student_id)

    now = _session_now()
    if expires_at is None:
        expires_at_timestamp = int(
            (now + timedelta(seconds=ttl_seconds)).timestamp()
        )
    else:
        expires_at_timestamp = int(expires_at)
        if expires_at_timestamp <= int(now.timestamp()):
            return False, None, "access_expired"

    qs_active = video_repo.playback_session_filter_active(student_id, now, now)
    active_devices = qs_active.values_list("device_id", flat=True).distinct()
    unique_devices = set(active_devices)
    if device_id not in unique_devices and len(unique_devices) >= int(max_devices):
        return False, None, "device_limit_exceeded"

    active_count = qs_active.count()

    if active_count >= int(max_sessions):
        return False, None, "concurrency_limit_exceeded"

    session_id = str(uuid.uuid4())

    return True, {"session_id": session_id, "expires_at": expires_at_timestamp}, None


def heartbeat_session(*, student_id: int, session_id: str, ttl_seconds: int) -> bool:
    """
    세션 TTL 연장
    - Redis 사용 시: DB 쓰기 없이 Redis에만 버퍼링 (Write-Behind)
    - Redis 미사용/장애 시: 기존 DB 기반 로직
    """
    receipt_session = _receipt_session(session_id, student_id)
    if receipt_session is not None:
        with transaction.atomic():
            session = VideoPlaybackSession.objects.select_for_update().filter(pk=receipt_session.pk).first()
            now = timezone.now()
            if session is None or session.status != VideoPlaybackSession.Status.ACTIVE or session.is_revoked or (
                session.expires_at is not None and session.expires_at <= now
            ):
                return False
            # Only permission-revalidated renewal extends the lease. An old
            # heartbeat can neither exceed an entitlement nor shorten a new lease.
            session.last_seen = now
            session.save(update_fields=["last_seen"])
        return True
    if is_redis_available():
        ok = buffer_heartbeat_session_ttl(session_id=session_id, ttl_seconds=ttl_seconds)
        if ok:
            return True
        # Redis 실패 시 DB fallback

    now = timezone.now()
    new_expires_at = now + timedelta(seconds=ttl_seconds)

    try:
        session = video_repo.playback_session_get_by_session_id_and_student(session_id, student_id)
    except VideoPlaybackSession.DoesNotExist:
        return False

    if session.is_revoked:
        return False

    session.expires_at = new_expires_at
    session.last_seen = now
    session.save(update_fields=["expires_at", "last_seen"])

    return True


def end_session(*, student_id: int, session_id: str) -> None:
    """
    명시적 세션 종료
    - Redis 사용 시: Write-Behind flush (Redis stats → DB) 후 종료
    - Redis 미사용 시: 기존 DB 기반
    """
    if _receipt_session(session_id, student_id) is not None:
        raise ValueError("protocol2 end requires the receipt-aware boundary")
    now = timezone.now()

    if is_redis_available():
        stats = get_session_violation_stats_redis(session_id)
        if stats is not None:
            # Persist final counters for both ACTIVE and already-REVOKED rows.
            # The status transition remains ACTIVE-only so disposal cannot
            # downgrade a server revocation to ENDED.
            video_repo.playback_session_filter_update_any(
                session_id, student_id,
                last_seen=now,
                violated_count=stats.get("violated", 0),
                total_count=stats.get("total", 0),
            )
            video_repo.playback_session_filter_update_active(
                session_id, student_id,
                status=VideoPlaybackSession.Status.ENDED,
                ended_at=now,
            )
            flush_session_stats(session_id)
            flush_session_buffer(session_id)
            return

    video_repo.playback_session_filter_update_active(
        session_id, student_id,
        status=VideoPlaybackSession.Status.ENDED,
        ended_at=now,
    )


def revoke_session(*, student_id: int, session_id: str) -> None:
    """
    서버 강제 차단
    - Redis 사용 시: Write-Behind flush 후 차단
    """
    receipt_session = _receipt_session(session_id, student_id)
    if receipt_session is not None:
        with transaction.atomic():
            session = VideoPlaybackSession.objects.select_for_update().filter(pk=receipt_session.pk).first()
            if session is not None and session.status == VideoPlaybackSession.Status.ACTIVE:
                session.status = VideoPlaybackSession.Status.REVOKED
                session.is_revoked = True
                session.ended_at = timezone.now()
                session.save(update_fields=["status", "is_revoked", "ended_at"])
        return
    now = timezone.now()

    if is_redis_available():
        stats = get_session_violation_stats_redis(session_id)
        if stats is not None:
            video_repo.playback_session_filter_update_any(
                session_id, student_id,
                last_seen=now,
                violated_count=stats.get("violated", 0),
                total_count=stats.get("total", 0),
                status=VideoPlaybackSession.Status.REVOKED,
                is_revoked=True,
                ended_at=now,
            )
            flush_session_stats(session_id)
            flush_session_buffer(session_id)
            return

    video_repo.playback_session_filter_update_any(
        session_id, student_id,
        status=VideoPlaybackSession.Status.REVOKED,
        is_revoked=True,
        ended_at=now,
    )


def is_session_active(*, student_id: int, session_id: str) -> bool:
    """
    세션 활성 여부 확인
    - Redis 사용 시: session:{session_id}:meta 존재 여부 (heartbeat로 TTL 연장)
    - Redis 미사용/장애 시: DB 기반
    """
    receipt_session = _receipt_session(session_id, student_id)
    if receipt_session is not None:
        return (
            receipt_session.status == VideoPlaybackSession.Status.ACTIVE
            and not receipt_session.is_revoked
            and (receipt_session.expires_at is None or receipt_session.expires_at > timezone.now())
        )
    if is_redis_available():
        if has_session_meta(session_id):
            return True
        # Redis에 키 없으면 DB fallback (세션 시작 직후 첫 heartbeat 전)

    now = timezone.now()

    try:
        session = video_repo.playback_session_get_by_session_id_and_student_any(session_id, student_id)
    except VideoPlaybackSession.DoesNotExist:
        return False

    if session.is_revoked:
        return False

    if session.status != VideoPlaybackSession.Status.ACTIVE:
        return False

    if session.expires_at and session.expires_at <= now:
        session.status = VideoPlaybackSession.Status.EXPIRED
        session.ended_at = now
        session.save(update_fields=["status", "ended_at"])
        return False

    return True


# =======================================================
# 세션 단위 위반 누적 (DB 기반)
# =======================================================

def record_session_event(
    *,
    student_id: int,
    session_id: str,
    violated: bool,
    reason: str = "",
) -> Dict[str, int]:
    """
    세션 단위 누적 카운터
    - Redis 사용 시: DB 쓰기 없이 Redis에만 버퍼링 (Write-Behind)
    - Redis 미사용/장애 시: DB 기반
    """
    if _receipt_session(session_id, student_id) is not None:
        raise ValueError("protocol2 events require the receipt-aware boundary")
    if is_redis_available():
        ok, stats = buffer_session_event(
            session_id=session_id,
            user_id=student_id,
            violated=violated,
            reason=reason,
        )
        if ok:
            return stats
        # Redis 실패 시 DB fallback

    try:
        session = video_repo.playback_session_get_by_session_id_and_student_any(session_id, student_id)
    except VideoPlaybackSession.DoesNotExist:
        return {"total": 0, "violated": 0}

    with transaction.atomic():
        session.refresh_from_db()
        session.total_count += 1
        if violated:
            session.violated_count += 1
        session.save(update_fields=["total_count", "violated_count"])

    return {
        "total": session.total_count,
        "violated": session.violated_count,
    }


def get_session_violation_stats(*, session_id: str) -> Dict[str, int]:
    """
    세션 위반 통계 조회
    - Redis 사용 시: Redis에서 조회 (실시간)
    - Redis 미사용/장애 시: DB 기반
    """
    receipt_session = _receipt_session(session_id)
    if receipt_session is not None:
        return {"total": receipt_session.total_count, "violated": receipt_session.violated_count}
    if is_redis_available():
        stats = get_session_violation_stats_redis(session_id)
        if stats is not None:
            return stats

    try:
        session = video_repo.playback_session_get_by_session_id(session_id)
        return {
            "total": session.total_count,
            "violated": session.violated_count,
        }
    except VideoPlaybackSession.DoesNotExist:
        return {"total": 0, "violated": 0}


def should_revoke_by_stats(*, violated: int, total: int) -> bool:
    """
    보수적 차단 기준:
    - violated >= threshold
    - 또는 violated/total 비율이 너무 높으면 차단
    """
    threshold = int(getattr(settings, "VIDEO_VIOLATION_REVOKE_THRESHOLD", 3))
    ratio = float(getattr(settings, "VIDEO_VIOLATION_REVOKE_RATIO", 0.5))

    if int(violated) >= threshold:
        return True
    if int(total) > 0 and (float(violated) / float(total)) >= ratio:
        return True
    return False


# =======================================================
# Facade API (Student ONLY) - 기존 유지
# =======================================================

def create_playback_session(
    *,
    user,
    video_id: int,
    enrollment_id: int,
    device_id: str,
) -> dict:
    """
    학생 전용 Facade API

    책임:
    - "재생 세션 생성"만 담당
    - 권한 / 수강 검증은 View에서 선행되어야 함
    """

    # 🚫 강사 / 운영자 차단
    if getattr(user, "is_instructor", False) or getattr(user, "is_staff", False):
        return {
            "ok": False,
            "error": "instructor_must_use_play_api",
        }

    if not device_id:
        return {"ok": False, "error": "device_id_required"}

    video = video_repo.video_get_by_id_with_relations(video_id)
    enrollment = video_repo.enrollment_get_by_id_active_with_student_lecture(enrollment_id)

    # 🛡️ 안전 가드 (View 누락 방지용)
    if enrollment.lecture_id != video.session.lecture_id:
        return {
            "ok": False,
            "error": "enrollment_lecture_mismatch",
        }

    ttl = int(getattr(settings, "VIDEO_PLAYBACK_TTL_SECONDS", 600))

    tenant = (
        getattr(getattr(getattr(video, "session", None), "lecture", None), "tenant", None)
        or getattr(video, "tenant", None)
    )
    max_sessions, max_devices = get_tenant_session_limits(tenant)

    ok, sess, err = issue_session(
        student_id=enrollment.student_id,
        device_id=device_id,
        ttl_seconds=ttl,
        max_sessions=max_sessions,
        max_devices=max_devices,
    )

    if not ok:
        return {
            "ok": False,
            "error": err,
        }

    session_id = str(sess["session_id"])
    expires_at_timestamp = int(sess["expires_at"])
    expires_at = timezone.datetime.fromtimestamp(expires_at_timestamp, tz=datetime_timezone.utc)

    video_repo.playback_session_create(
        video=video,
        enrollment=enrollment,
        session_id=session_id,
        device_id=device_id,
        status=VideoPlaybackSession.Status.ACTIVE,
        started_at=timezone.now(),
        expires_at=expires_at,
        last_seen=timezone.now(),
        violated_count=0,
        total_count=0,
        is_revoked=False,
    )

    return {
        "ok": True,
        "video_id": video.id,
        "enrollment_id": enrollment.id,
        "session_id": session_id,
        "expires_at": expires_at_timestamp,
    }
