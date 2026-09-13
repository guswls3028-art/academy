"""
Video / Session / Enrollment 등 DB 조회 — .objects. 접근을 adapters 내부로 한정 (Gate 7).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from apps.domains.video.models import VideoTranscodeJob  # noqa: F401  (forward-ref)

logger = logging.getLogger(__name__)


def get_video_status(video_id: int) -> Optional[str]:
    """Video 상태만 조회 (worker에서 이미 READY인지 확인용)."""
    from apps.domains.video.models import Video
    row = Video.objects.filter(pk=video_id).values_list("status", flat=True).first()
    return row


def get_video_for_update(video_id: int):
    """select_for_update로 Video 1건 조회 (tenant_id 추출을 위한 select_related 포함)."""
    from apps.domains.video.models import Video
    return Video.objects.select_for_update(of=("self",)).select_related(
        "session", "session__lecture", "session__lecture__tenant"
    ).filter(id=int(video_id)).first()


def lock_direct_video_session_scope(*, tenant, session_id: int):
    """Lock the lecture then session for one same-tenant direct-video scope."""
    from apps.domains.lectures.models import Lecture, Session

    session_ref = (
        Session.objects.filter(id=int(session_id), lecture__tenant=tenant)
        .values("id", "lecture_id")
        .first()
    )
    if session_ref is None:
        return None, None
    lecture = (
        Lecture.objects.select_for_update()
        .filter(id=session_ref["lecture_id"], tenant=tenant)
        .first()
    )
    if lecture is None:
        return None, None
    session = (
        Session.objects.select_for_update()
        .filter(id=session_ref["id"], lecture=lecture)
        .first()
    )
    return lecture, session


def lock_active_playback_renewal_scope(
    *,
    tenant_id: int,
    lecture_id: int,
    session_id: int,
    enrollment_id: int,
    student_id: int,
    video_id: int,
):
    """Lock one exact active-enrollment playback graph in canonical order."""
    from apps.domains.enrollment.models import Enrollment
    from apps.domains.lectures.models import Lecture, Session
    from apps.domains.video.models import Video

    try:
        tenant_id = int(tenant_id)
        lecture_id = int(lecture_id)
        session_id = int(session_id)
        enrollment_id = int(enrollment_id)
        student_id = int(student_id)
        video_id = int(video_id)
    except (TypeError, ValueError):
        return None, None, None, None

    lecture = (
        Lecture.objects.select_for_update()
        .filter(id=lecture_id, tenant_id=tenant_id)
        .first()
    )
    if lecture is None:
        return None, None, None, None
    session = (
        Session.objects.select_for_update()
        .filter(id=session_id, lecture=lecture)
        .first()
    )
    if session is None:
        return lecture, None, None, None
    enrollment = (
        Enrollment.objects.select_for_update(of=("self",))
        .select_related("student", "lecture")
        .filter(
            id=enrollment_id,
            tenant_id=tenant_id,
            student_id=student_id,
            lecture=lecture,
            status="ACTIVE",
        )
        .first()
    )
    video = (
        Video.objects.select_for_update(of=("self",))
        .filter(
            id=video_id,
            tenant_id=tenant_id,
            session=session,
            status=Video.Status.READY,
            deleted_at__isnull=True,
        )
        .first()
    )
    return lecture, session, enrollment, video


def lock_direct_video_student(*, tenant, student_id: int, include_deleted: bool = False):
    """Lock one same-tenant student used by a direct-video transaction."""
    from apps.domains.students.models import Student

    queryset = (
        Student.objects.select_for_update(of=("self",))
        .select_related("user")
        .filter(id=int(student_id), tenant=tenant)
    )
    if not include_deleted:
        queryset = queryset.filter(deleted_at__isnull=True)
    return queryset.first()


def any_enrollment_exists_for_student_lecture(
    *,
    tenant_id: int,
    student_id: int,
    lecture_id: int,
) -> bool:
    """Return whether any enrollment status exists for the exact lecture scope."""
    from apps.domains.enrollment.models import Enrollment

    return Enrollment.objects.filter(
        tenant_id=int(tenant_id),
        student_id=int(student_id),
        lecture_id=int(lecture_id),
    ).exists()


def get_video_queryset_with_relations():
    """VideoViewSet 기본 queryset. upload_complete enqueue 시 video.session.lecture.tenant 필요."""
    from apps.domains.video.models import Video
    return Video.objects.all().select_related(
        "session", "session__lecture", "session__lecture__tenant"
    )


def get_video_by_pk_with_relations(pk):
    """Video 1건 (session, lecture, tenant 포함). perform_destroy 등에서 tenant_id 사용."""
    from apps.domains.video.models import Video
    return Video.objects.select_related(
        "session", "session__lecture", "session__lecture__tenant"
    ).filter(pk=pk).first()


def get_session_by_id_with_lecture_tenant(session_id):
    from apps.domains.lectures.models import Session
    return Session.objects.select_related("lecture", "lecture__tenant").get(id=session_id)


def lock_sessions_by_ids(session_ids: list[int]) -> list[int]:
    """Lock Session rows in deterministic order inside the caller transaction."""
    from apps.domains.lectures.models import Session

    return list(
        Session.objects.select_for_update()
        .filter(id__in=session_ids)
        .order_by("id")
        .values_list("id", flat=True)
    )


def lock_video_folders_by_ids(folder_ids: list[int]) -> list[int]:
    """Lock VideoFolder rows in deterministic order inside the caller transaction."""
    from apps.domains.video.models import VideoFolder

    return list(
        VideoFolder.objects.select_for_update()
        .filter(id__in=folder_ids)
        .order_by("id")
        .values_list("id", flat=True)
    )


def create_video(
    session,
    title,
    file_key,
    order,
    status,
    allow_skip=False,
    max_speed=1.0,
    show_watermark=True,
    visibility=None,
    tenant=None,
    uploaded_by=None,
    folder=None,
    source_type=None,
    youtube_video_id="",
    youtube_url="",
):
    from apps.domains.video.models import Video
    kwargs = dict(
        session=session,
        title=title,
        file_key=file_key,
        order=order,
        status=status,
        allow_skip=allow_skip,
        max_speed=max_speed,
        show_watermark=show_watermark,
        youtube_video_id=youtube_video_id or "",
        youtube_url=youtube_url or "",
    )
    if source_type is not None:
        kwargs["source_type"] = source_type
    if uploaded_by is not None:
        kwargs["uploaded_by"] = uploaded_by
    if folder is not None:
        kwargs["folder"] = folder
    if visibility is not None:
        kwargs["visibility"] = visibility
    # tenant: 명시적 전달 우선, 없으면 session→lecture→tenant 자동 추출
    if tenant is not None:
        kwargs["tenant"] = tenant
    elif session:
        try:
            kwargs["tenant_id"] = session.lecture.tenant_id
        except Exception:
            raise ValueError(
                f"Cannot determine tenant for video: session={session.id}, "
                f"lecture={getattr(session, 'lecture_id', None)}. "
                "Pass tenant explicitly."
            )
    else:
        raise ValueError("tenant is required for Video creation.")
    return Video.objects.create(**kwargs)


def get_enrollments_for_lecture_active(lecture):
    from apps.domains.enrollment.models import Enrollment
    from apps.domains.students.ordering import student_name_codepoint_ordering

    return (
        Enrollment.objects.filter(lecture=lecture, status="ACTIVE")
        .select_related("student")
        .order_by(*student_name_codepoint_ordering(
            ("student__name", "student_id", "id"),
            name_field="student__name",
        ))
    )


def get_video_progresses_for_video(video):
    from apps.domains.video.models import VideoProgress
    return VideoProgress.objects.filter(video=video)


def get_video_access_for_video(video):
    from apps.domains.video.models import VideoAccess
    return VideoAccess.objects.filter(video=video)


def get_attendance_for_session(session):
    from apps.domains.attendance.models import Attendance
    return Attendance.objects.filter(session=session)


def get_enrollments_for_lecture(lecture):
    from apps.domains.enrollment.models import Enrollment
    return Enrollment.objects.filter(lecture=lecture)


def get_playback_events_queryset_for_video(video, since=None):
    from apps.domains.video.models import VideoPlaybackEvent
    qs = VideoPlaybackEvent.objects.filter(video=video).select_related(
        "enrollment", "enrollment__student"
    )
    if since is not None:
        qs = qs.filter(occurred_at__gte=since)
    return qs


def video_filter_by_lecture(lecture):
    from apps.domains.video.models import Video
    from apps.domains.video.sorting import sort_videos_for_playlist
    return sort_videos_for_playlist(
        Video.objects.filter(session__lecture=lecture).order_by("order", "title", "id").distinct()
    )


def video_filter_by_session_ready(session_id):
    from apps.domains.video.models import Video
    from apps.domains.video.sorting import sort_videos_for_playlist
    return sort_videos_for_playlist(
        Video.objects.filter(
            session_id=session_id,
            status=Video.Status.READY,
        ).select_related("session__lecture").order_by("order", "title", "id")
    )


def enrollment_get_by_student_lecture_active(student, lecture):
    from apps.domains.enrollment.models import Enrollment
    return Enrollment.objects.filter(
        student=student,
        lecture=lecture,
        status="ACTIVE",
    ).first()


def video_progress_get(video, enrollment):
    from apps.domains.video.models import VideoProgress
    return VideoProgress.objects.filter(video=video, enrollment=enrollment).first()


def video_like_exists(*, video_id: int, student, tenant_id: int) -> bool:
    from apps.domains.video.models import VideoLike

    return VideoLike.objects.filter(
        video_id=int(video_id),
        student=student,
        tenant_id=int(tenant_id),
    ).exists()


def session_all_queryset():
    from apps.domains.lectures.models import Session
    return Session.objects.select_related("lecture").all()


def session_get_by_id_with_lecture(session_id):
    from apps.domains.lectures.models import Session
    return Session.objects.select_related("lecture").get(id=session_id)


def video_session_memberships(*, tenant_id, enrollment_ids):
    """Exact tenant/lecture/session membership; attendance never grants access."""
    from django.db.models import F
    from apps.domains.enrollment.models import SessionEnrollment

    return SessionEnrollment.objects.filter(
        tenant_id=tenant_id,
        enrollment_id__in=enrollment_ids,
        enrollment__tenant_id=tenant_id,
        enrollment__student__tenant_id=tenant_id,
        enrollment__student__deleted_at__isnull=True,
        session__lecture__tenant_id=tenant_id,
        session__lecture_id=F("enrollment__lecture_id"),
    )


def session_enrollment_exists(session, enrollment) -> bool:
    if session is None or enrollment is None:
        return False
    return video_session_memberships(
        tenant_id=enrollment.tenant_id, enrollment_ids=[enrollment.id],
    ).filter(session=session).exists()


def video_access_get(video, enrollment):
    from apps.domains.video.models import VideoAccess
    return VideoAccess.objects.filter(video=video, enrollment=enrollment).first()


def video_access_filter(video, enrollment=None):
    from apps.domains.video.models import VideoAccess
    qs = VideoAccess.objects.filter(video=video)
    if enrollment is not None:
        qs = qs.filter(enrollment=enrollment)
    return qs


def video_access_update_or_create_by_ids(video_id, enrollment_id, defaults):
    from apps.domains.video.models import VideoAccess
    return VideoAccess.objects.update_or_create(
        video_id=video_id,
        enrollment_id=enrollment_id,
        defaults=defaults,
    )


def video_access_all():
    from apps.domains.video.models import VideoAccess
    return VideoAccess.objects.all()


def video_progress_all():
    from apps.domains.video.models import VideoProgress
    return VideoProgress.objects.all()


def video_progress_filter(video):
    from apps.domains.video.models import VideoProgress
    return VideoProgress.objects.filter(video=video)


def video_progress_filter_video_enrollment_ids(video, enrollment_ids):
    from apps.domains.video.models import VideoProgress
    qs = VideoProgress.objects.filter(enrollment_id__in=enrollment_ids)
    if video:
        qs = qs.filter(video=video)
    return qs


def video_progress_filter_video_ids_enrollment_ids(video_ids, enrollment_ids):
    from apps.domains.video.models import VideoProgress
    return VideoProgress.objects.filter(
        video_id__in=list(video_ids),
        enrollment_id__in=list(enrollment_ids),
    )


def video_get_by_id(video_id):
    from apps.domains.video.models import Video
    return Video.objects.filter(id=int(video_id)).first()


def video_get_by_id_only_policy(video_id):
    from apps.domains.video.models import Video
    return Video.objects.filter(id=video_id).only("id", "policy_version").first()


def video_get_by_id_with_relations(video_id):
    from apps.domains.video.models import Video
    return Video.objects.select_related("session", "session__lecture").get(id=video_id)


def video_get_by_id_with_session(video_id):
    from apps.domains.video.models import Video
    return Video.objects.select_related("session").get(id=video_id)


def video_update(video_id, **kwargs):
    from apps.domains.video.models import Video
    return Video.objects.filter(id=video_id).update(**kwargs)


def enrollment_filter_by_lecture_active(lecture):
    from apps.domains.enrollment.models import Enrollment
    return Enrollment.objects.filter(lecture=lecture, status="ACTIVE").select_related("student")


def enrollment_get_by_id(enrollment_id):
    from apps.domains.enrollment.models import Enrollment
    return Enrollment.objects.filter(id=enrollment_id).first()


def enrollment_get_by_id_with_relations(enrollment_id):
    from apps.domains.enrollment.models import Enrollment
    return Enrollment.objects.select_related("lecture").get(id=enrollment_id)


def enrollment_get_by_id_active_with_student_lecture(enrollment_id):
    from apps.domains.enrollment.models import Enrollment
    return Enrollment.objects.select_related("student", "lecture").get(
        id=enrollment_id,
        status="ACTIVE",
    )


def enrollment_select_related_lecture_filter(id=None):
    from apps.domains.enrollment.models import Enrollment
    qs = Enrollment.objects.select_related("lecture")
    if id is not None:
        qs = qs.filter(id=id)
    return qs


def attendance_filter_session(session):
    from apps.domains.attendance.models import Attendance
    return Attendance.objects.filter(session=session)


def attendance_filter_session_enrollment(session, enrollment):
    from apps.domains.attendance.models import Attendance
    return Attendance.objects.filter(session=session, enrollment=enrollment)


def attendance_filter_session_status(session, status):
    from apps.domains.attendance.models import Attendance
    return Attendance.objects.filter(session=session, status=status)


# ---- VideoPlaybackSession ----
def playback_session_cleanup_expired(student_id):
    from django.utils import timezone
    from apps.domains.video.models import VideoPlaybackSession
    now = timezone.now()
    return VideoPlaybackSession.objects.filter(
        enrollment__student_id=student_id,
        status=VideoPlaybackSession.Status.ACTIVE,
        expires_at__lt=now,
    ).update(status=VideoPlaybackSession.Status.EXPIRED, ended_at=now)


def playback_session_filter_active(student_id, now, expires_at_gt):
    from apps.domains.video.models import VideoPlaybackSession
    return VideoPlaybackSession.objects.filter(
        enrollment__student_id=student_id,
        status=VideoPlaybackSession.Status.ACTIVE,
        expires_at__gt=expires_at_gt,
    )


def playback_session_create(**kwargs):
    from apps.domains.video.models import VideoPlaybackSession
    return VideoPlaybackSession.objects.create(**kwargs)


def playback_session_get_by_session_id(session_id):
    from apps.domains.video.models import VideoPlaybackSession
    return VideoPlaybackSession.objects.get(session_id=session_id)


def playback_session_filter_update_active(session_id, student_id, **update_kwargs):
    from apps.domains.video.models import VideoPlaybackSession
    return VideoPlaybackSession.objects.filter(
        session_id=session_id,
        enrollment__student_id=student_id,
        status=VideoPlaybackSession.Status.ACTIVE,
    ).update(**update_kwargs)


def playback_session_select_related_get(session_id):
    from apps.domains.video.models import VideoPlaybackSession
    return VideoPlaybackSession.objects.select_related(
        "enrollment", "enrollment__student", "video"
    ).get(session_id=session_id)


def playback_session_select_related_filter(**kwargs):
    from apps.domains.video.models import VideoPlaybackSession
    return VideoPlaybackSession.objects.select_related(
        "enrollment", "enrollment__student", "video"
    ).filter(**kwargs)


def playback_session_filter(**kwargs):
    from apps.domains.video.models import VideoPlaybackSession
    return VideoPlaybackSession.objects.filter(**kwargs)


def playback_session_end_by_session_id(session_id):
    from django.utils import timezone
    from apps.domains.video.models import VideoPlaybackSession
    now = timezone.now()
    return VideoPlaybackSession.objects.filter(session_id=session_id).update(
        status=VideoPlaybackSession.Status.ENDED,
        ended_at=now,
    )


def playback_session_get_by_session_id_and_student(session_id, student_id):
    from apps.domains.video.models import VideoPlaybackSession
    return VideoPlaybackSession.objects.select_related("enrollment").get(
        session_id=session_id,
        enrollment__student_id=student_id,
        status=VideoPlaybackSession.Status.ACTIVE,
    )


def playback_session_get_by_session_id_and_student_any(session_id, student_id):
    from apps.domains.video.models import VideoPlaybackSession
    return VideoPlaybackSession.objects.select_related("enrollment").get(
        session_id=session_id,
        enrollment__student_id=student_id,
    )


def playback_session_filter_update(session_id, student_id, **update_kwargs):
    from apps.domains.video.models import VideoPlaybackSession
    return VideoPlaybackSession.objects.filter(
        session_id=session_id,
        enrollment__student_id=student_id,
        status=VideoPlaybackSession.Status.ACTIVE,
    ).update(**update_kwargs)


def playback_session_filter_update_any(session_id, student_id, **update_kwargs):
    from apps.domains.video.models import VideoPlaybackSession
    return VideoPlaybackSession.objects.filter(
        session_id=session_id,
        enrollment__student_id=student_id,
    ).update(**update_kwargs)


def playback_session_revoke_active_for_lecture(lecture_id, *, ended_at):
    from apps.domains.video.models import VideoPlaybackSession

    return VideoPlaybackSession.objects.filter(
        video__session__lecture_id=lecture_id,
        status=VideoPlaybackSession.Status.ACTIVE,
    ).update(
        status=VideoPlaybackSession.Status.REVOKED,
        is_revoked=True,
        ended_at=ended_at,
    )


def playback_session_update_expired(now):
    from apps.domains.video.models import VideoPlaybackSession
    return VideoPlaybackSession.objects.filter(
        status=VideoPlaybackSession.Status.ACTIVE,
        expires_at__lt=now,
    ).update(status=VideoPlaybackSession.Status.EXPIRED, ended_at=now)


# ---- VideoPlaybackEvent ----
def playback_event_filter_by_video_id(video_id, since=None):
    from apps.domains.video.models import VideoPlaybackEvent
    qs = VideoPlaybackEvent.objects.filter(video_id=video_id).select_related(
        "enrollment", "enrollment__student"
    )
    if since is not None:
        qs = qs.filter(occurred_at__gte=since)
    return qs


def playback_event_bulk_create(objs, batch_size=500):
    from apps.domains.video.models import VideoPlaybackEvent
    return VideoPlaybackEvent.objects.bulk_create(objs, batch_size=batch_size)


# ---- Video Worker용 Repository (IVideoRepository 호환, Gate 7: ORM을 adapters 내부로) ----


def _tenant_id_from_video(video) -> Optional[int]:
    """Video에서 tenant_id 추출. video.tenant_id 우선, fallback으로 session.lecture.tenant_id."""
    if not video:
        return None
    # 직접 FK 우선
    tid = getattr(video, "tenant_id", None)
    if tid is not None:
        return tid
    # fallback: 간접 체인
    if getattr(video, "session", None) and getattr(video.session, "lecture", None):
        return getattr(video.session.lecture, "tenant_id", None)
    return None


def _cache_video_status_safe(
    video_id: int,
    tenant_id: Optional[int],
    status: str,
    *,
    hls_path: Optional[str] = None,
    duration: Optional[int] = None,
    error_reason: Optional[str] = None,
    ttl: Optional[int] = None,
) -> None:
    """Redis에 비디오 상태 기록 (Worker 완료/실패 시 progress API가 동일 상태 반환하도록)."""
    if not tenant_id:
        return
    try:
        from apps.domains.video.redis_status_cache import cache_video_status
        cache_video_status(
            tenant_id=tenant_id,
            video_id=video_id,
            status=status,
            hls_path=hls_path,
            duration=duration,
            error_reason=error_reason,
            ttl=ttl,
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("Failed to cache video status in Redis: %s", e)


class DjangoVideoRepository:
    """
    Video 상태 업데이트 — mark_processing / complete_video / fail_video.
    Worker는 이 adapter만 사용 (src.infrastructure.db.video_repository 대체).
    완료/실패 시 Redis에도 동기화하여 progress API(GET /media/videos/{id}/progress/)가
    READY/FAILED를 반환하도록 함.
    """

    def mark_processing(self, video_id: int) -> bool:
        from django.db import transaction
        from django.utils import timezone
        from apps.domains.video.models import Video

        with transaction.atomic():
            video = get_video_for_update(video_id)
            if not video:
                return False
            if video.status == Video.Status.PROCESSING:
                return True
            if video.status != Video.Status.UPLOADED:
                import logging
                logging.getLogger(__name__).warning(
                    "Cannot mark video %s as PROCESSING: status=%s", video_id, video.status,
                )
                return False
            video.status = Video.Status.PROCESSING
            if hasattr(video, "processing_started_at"):
                video.processing_started_at = timezone.now()
            update_fields = ["status"]
            if hasattr(video, "processing_started_at"):
                update_fields.append("processing_started_at")
            video.save(update_fields=update_fields)
            tenant_id = _tenant_id_from_video(video)
        _cache_video_status_safe(
            video_id, tenant_id,
            getattr(Video.Status.PROCESSING, "value", "PROCESSING"),
            ttl=21600,
        )
        return True

    def complete_video(
        self,
        video_id: int,
        hls_path: str,
        duration: Optional[int] = None,
        thumbnail_r2_key: Optional[str] = None,
    ) -> tuple[bool, str]:
        from django.db import transaction
        from apps.domains.video.models import Video

        with transaction.atomic():
            video = get_video_for_update(video_id)
            if not video:
                import logging
                _log = logging.getLogger(__name__)
                exists = Video.objects.filter(pk=video_id).exists()
                _log.warning(
                    "complete_video: video_id=%s not found (row exists=%s). "
                    "Possible: row/parent deleted during encode, or worker DB differs from API.",
                    video_id,
                    exists,
                )
                return False, "not_found"
            if video.status == Video.Status.READY and bool(video.hls_path):
                return True, "idempotent"
            if video.status != Video.Status.PROCESSING:
                import logging
                logging.getLogger(__name__).warning(
                    "Video %s status is %s (expected PROCESSING)", video_id, video.status,
                )
            video.hls_path = str(hls_path)
            if duration is not None and duration >= 0:
                video.duration = int(duration)
            if thumbnail_r2_key:
                video.thumbnail_r2_key = str(thumbnail_r2_key)[:500]
            video.status = Video.Status.READY
            if hasattr(video, "leased_until"):
                video.leased_until = None
            if hasattr(video, "leased_by"):
                video.leased_by = ""
            update_fields = ["hls_path", "status"]
            if duration is not None and duration >= 0:
                update_fields.append("duration")
            if thumbnail_r2_key:
                update_fields.append("thumbnail_r2_key")
            if hasattr(video, "leased_until"):
                update_fields.append("leased_until")
            if hasattr(video, "leased_by"):
                update_fields.append("leased_by")
            video.save(update_fields=update_fields)
            tenant_id = _tenant_id_from_video(video)
        _cache_video_status_safe(
            video_id, tenant_id,
            getattr(Video.Status.READY, "value", "READY"),
            hls_path=str(hls_path),
            duration=int(duration) if duration is not None and duration >= 0 else None,
            ttl=None,
        )
        return True, "ok"

    def fail_video(self, video_id: int, reason: str) -> tuple[bool, str]:
        from django.db import transaction
        from apps.domains.video.models import Video

        with transaction.atomic():
            video = get_video_for_update(video_id)
            if not video:
                return False, "not_found"
            if video.status == Video.Status.FAILED:
                return True, "idempotent"
            video.status = Video.Status.FAILED
            if hasattr(video, "error_reason"):
                video.error_reason = str(reason)[:2000]
            if hasattr(video, "leased_until"):
                video.leased_until = None
            if hasattr(video, "leased_by"):
                video.leased_by = ""
            update_fields = ["status"]
            if hasattr(video, "error_reason"):
                update_fields.append("error_reason")
            if hasattr(video, "leased_until"):
                update_fields.append("leased_until")
            if hasattr(video, "leased_by"):
                update_fields.append("leased_by")
            video.save(update_fields=update_fields)
            tenant_id = _tenant_id_from_video(video)
        _cache_video_status_safe(
            video_id, tenant_id,
            getattr(Video.Status.FAILED, "value", "FAILED"),
            error_reason=str(reason)[:2000],
            ttl=None,
        )
        return True, "ok"


# ---- VideoTranscodeJob 기반 Repository (Enterprise Job System) ----


def job_get_by_id(job_id) -> Optional["VideoTranscodeJob"]:
    """Job 조회 (video, session, lecture 포함)."""
    from apps.domains.video.models import VideoTranscodeJob
    return VideoTranscodeJob.objects.select_related(
        "video", "video__session", "video__session__lecture", "video__session__lecture__tenant"
    ).filter(pk=job_id).first()


def job_set_running(job_id: str) -> bool:
    """
    Job QUEUED/RETRY_WAIT → RUNNING. Keep the parent Video status in sync so
    list/detail APIs do not show a running Batch job as "처리 대기".
    """
    from django.db import transaction
    from django.utils import timezone
    from apps.domains.video.models import Video, VideoTranscodeJob

    now = timezone.now()
    with transaction.atomic():
        n = VideoTranscodeJob.objects.filter(
            pk=job_id,
            state__in=[VideoTranscodeJob.State.QUEUED, VideoTranscodeJob.State.RETRY_WAIT],
        ).update(
            state=VideoTranscodeJob.State.RUNNING,
            locked_by="batch",
            locked_until=now,
            last_heartbeat_at=now,
            updated_at=now,
        )
        if n != 1:
            return False

        job = VideoTranscodeJob.objects.only("video_id", "tenant_id").get(pk=job_id)
        Video.objects.filter(pk=job.video_id, status=Video.Status.UPLOADED).update(
            status=Video.Status.PROCESSING,
            processing_started_at=now,
            updated_at=now,
        )

    _cache_video_status_safe(
        job.video_id,
        job.tenant_id,
        getattr(Video.Status.PROCESSING, "value", "PROCESSING"),
        ttl=21600,
    )
    return True


def job_heartbeat(job_id, lease_seconds: int = 3600) -> bool:
    """RUNNING Job의 last_heartbeat_at 및 locked_until 갱신."""
    from django.utils import timezone
    from datetime import timedelta
    from apps.domains.video.models import VideoTranscodeJob

    now = timezone.now()
    locked_until = now + timedelta(seconds=lease_seconds)
    n = VideoTranscodeJob.objects.filter(
        pk=job_id,
        state=VideoTranscodeJob.State.RUNNING,
    ).update(
        last_heartbeat_at=now,
        locked_until=locked_until,
    )
    return n == 1


def job_complete(
    job_id: str,
    hls_path: str,
    duration: Optional[int] = None,
    thumbnail_r2_key: Optional[str] = None,
) -> tuple[bool, str]:
    """
    Job SUCCEEDED + Video READY commit. Transactional.
    업로드+검증 완료 후 마지막에 READY 조건부 업데이트 (같은 트랜잭션 내에서 job state·hls_path 반영 후 status=READY).
    Idempotent: 이미 SUCCEEDED+READY이면 True 반환 (중복 실행 시 안전).

    thumbnail_r2_key: Worker가 R2에 올린 thumbnail.jpg 의 key. 비어 있으면
    모바일 카드 UI에 회색 placeholder 만 보이므로 invariant 상 항상 전달돼야 한다.
    """
    from django.db import transaction
    from apps.domains.video.models import Video, VideoTranscodeJob

    with transaction.atomic():
        job = VideoTranscodeJob.objects.select_for_update(of=("self",)).select_related("video").filter(pk=job_id).first()
        if not job:
            return False, "job_not_found"
        if job.state == VideoTranscodeJob.State.SUCCEEDED:
            video = get_video_for_update(job.video_id)
            if video and video.status == Video.Status.READY and video.hls_path:
                return True, "idempotent"
            return False, "job_already_succeeded"
        # QUEUED, RETRY_WAIT, RUNNING 모두 허용 (Batch는 intermediate RUNNING 없이 바로 complete)
        if job.state not in (
            VideoTranscodeJob.State.QUEUED,
            VideoTranscodeJob.State.RETRY_WAIT,
            VideoTranscodeJob.State.RUNNING,
        ):
            return False, "job_not_runnable"
        video = get_video_for_update(job.video_id)
        if not video:
            return False, "video_not_found"
        if str(getattr(video, "current_job_id", "") or "") != str(job.id):
            logger.warning(
                "job_complete stale job ignored: video_id=%s current_job_id=%s job_id=%s",
                video.id,
                getattr(video, "current_job_id", None),
                job.id,
            )
            return False, "stale_job"
        from apps.core.r2_paths import video_hls_master_path, video_hls_prefix
        expected_hls_path = video_hls_master_path(
            tenant_id=video.tenant_id,
            video_id=video.id,
        )
        normalized_hls_path = str(hls_path or "").strip().lstrip("/")
        if normalized_hls_path != expected_hls_path:
            logger.warning(
                "job_complete invalid HLS path: video_id=%s tenant_id=%s path=%s",
                video.id,
                video.tenant_id,
                normalized_hls_path,
            )
            return False, "invalid_hls_path"

        normalized_thumbnail_key = ""
        if thumbnail_r2_key:
            normalized_thumbnail_key = str(thumbnail_r2_key).strip().lstrip("/")
            expected_thumbnail_key = (
                f"{video_hls_prefix(video.tenant_id, video.id)}/thumbnail.jpg"
            )
            if normalized_thumbnail_key != expected_thumbnail_key:
                logger.warning(
                    "job_complete invalid thumbnail path: video_id=%s tenant_id=%s path=%s",
                    video.id,
                    video.tenant_id,
                    normalized_thumbnail_key,
                )
                return False, "invalid_thumbnail_path"

        video.hls_path = normalized_hls_path
        if duration is not None and duration >= 0:
            video.duration = int(duration)
        if normalized_thumbnail_key:
            video.thumbnail_r2_key = normalized_thumbnail_key
        video.status = Video.Status.READY
        video.error_reason = ""
        if hasattr(video, "leased_until"):
            video.leased_until = None
        if hasattr(video, "leased_by"):
            video.leased_by = ""
        update_fields = ["hls_path", "duration", "status", "error_reason", "leased_until", "leased_by"]
        if normalized_thumbnail_key:
            update_fields.append("thumbnail_r2_key")
        video.save(update_fields=update_fields)
        job.state = VideoTranscodeJob.State.SUCCEEDED
        job.locked_by = ""
        job.locked_until = None
        job.save(update_fields=["state", "locked_by", "locked_until", "updated_at"])
        try:
            from apps.domains.video.services.video_job_lock import release as lock_release
            lock_release(video.id)
        except Exception:
            pass
        tenant_id = _tenant_id_from_video(video)
    _cache_video_status_safe(
        video.id, tenant_id,
        getattr(Video.Status.READY, "value", "READY"),
        hls_path=normalized_hls_path,
        duration=int(duration) if duration is not None and duration >= 0 else None,
        ttl=None,
    )
    try:
        from apps.domains.video.redis_status_cache import delete_video_progress_key
        delete_video_progress_key(tenant_id, video.id)
    except Exception:
        pass
    # 영상 인코딩 완료 알림톡 발송 (업로더에게)
    try:
        _notify_video_encoding_complete(video, tenant_id)
    except Exception as e:
        logger.warning("video_encoding_complete notification failed video_id=%s: %s", video.id, e)
    return True, "ok"


def _notify_video_encoding_complete(video, tenant_id: int) -> None:
    """
    영상 인코딩 완료 시 업로더(스태프)에게 알림톡 발송.
    SMS fallback 금지. 승인된 알림톡 템플릿이 없으면 발송하지 않는다.
    """
    import logging
    import re
    _log = logging.getLogger(__name__)

    uploaded_by = getattr(video, "uploaded_by", None)
    if not uploaded_by:
        # uploaded_by가 없으면 select로 가져오기 시도
        from apps.domains.video.models import Video
        v = Video.objects.filter(pk=video.id).select_related("uploaded_by").first()
        uploaded_by = getattr(v, "uploaded_by", None) if v else None
    if not uploaded_by:
        _log.info("video_encoding_complete: no uploaded_by for video_id=%s, skip", video.id)
        return

    phone = getattr(uploaded_by, "phone", None) or ""
    phone = phone.replace("-", "").strip()
    if not phone:
        _log.info("video_encoding_complete: uploaded_by has no phone, video_id=%s staff_id=%s", video.id, uploaded_by.id)
        return

    # 강의명/차시명 추출
    lecture_name = ""
    session_name = ""
    try:
        if video.session:
            session_name = video.session.title or ""
            if video.session.lecture:
                lecture_name = video.session.lecture.name or ""
    except Exception:
        pass

    from apps.domains.messaging.selectors import get_auto_send_config
    from apps.domains.messaging.alimtalk_content_builders import (
        get_solapi_template_id,
        build_unified_replacements,
    )

    trigger = "video_encoding_complete"
    config = get_auto_send_config(tenant_id, trigger)
    if not config:
        _log.info("video_encoding_complete: no auto-send config for tenant %s", tenant_id)
        return
    if not config.enabled:
        _log.info("video_encoding_complete: trigger disabled for tenant %s", tenant_id)
        return

    # 템플릿 body 가져오기
    template_body = "영상 인코딩이 완료되었습니다.\n앱에서 영상을 확인해 주세요."
    if config and config.template:
        template_body = config.template.body or template_body

    # 테넌트 정보
    tenant_name = ""
    site_url = "https://hakwonplus.com"
    try:
        from apps.core.models import Tenant
        t = Tenant.objects.filter(pk=tenant_id).first()
        if t:
            tenant_name = t.name or ""
            if t.code:
                site_url = f"https://{t.code}.hakwonplus.com"
    except Exception:
        pass

    staff_name = getattr(uploaded_by, "name", "") or ""
    context = {
        "강의명": lecture_name,
        "차시명": session_name or video.title or "",
        "영상명": video.title or "",
    }

    unified_tid = get_solapi_template_id(trigger)
    approved_template_id = ""
    if config and config.template:
        approved_template_id = (getattr(config.template, "solapi_template_id", "") or "").strip()
        if getattr(config.template, "solapi_status", "") != "APPROVED":
            approved_template_id = ""

    alimtalk_tid = unified_tid or approved_template_id
    if not alimtalk_tid:
        _log.warning(
            "video_encoding_complete: approved alimtalk template missing, skip (tenant=%s video_id=%s)",
            tenant_id,
            video.id,
        )
        return

    # 발송 payload 구성
    if unified_tid:
        replacements = build_unified_replacements(
            trigger=trigger,
            content_body=template_body,
            context=context,
            tenant_name=tenant_name,
            student_name=staff_name,  # 수신자 이름 (스태프)
            site_url=site_url,
        )
        enqueue_text = template_body
    else:
        values = {
            "학원명": tenant_name,
            "학원이름": tenant_name,
            "학생이름": staff_name,
            "학생이름2": staff_name[-2:] if len(staff_name) >= 2 else staff_name,
            "학생이름3": staff_name,
            "선생님이름": staff_name,
            "스태프이름": staff_name,
            "강의명": lecture_name,
            "차시명": session_name or video.title or "",
            "영상명": video.title or "",
            "사이트링크": site_url,
        }
        enqueue_text = template_body
        replacements = []
        for key, value in values.items():
            text_value = str(value or "")
            placeholder = f"#{{{key}}}"
            if placeholder in enqueue_text:
                replacements.append({"key": key, "value": text_value})
            enqueue_text = enqueue_text.replace(placeholder, text_value)
        remaining = re.findall(r"#\{([^}]+)\}", enqueue_text)
        if remaining:
            _log.warning(
                "video_encoding_complete: unresolved template vars, skip (tenant=%s video_id=%s vars=%s)",
                tenant_id,
                video.id,
                ",".join(sorted(set(remaining))),
            )
            return

    message_kwargs = dict(
        tenant_id=tenant_id,
        to=phone,
        text=enqueue_text,
        message_mode="alimtalk",
        template_id=alimtalk_tid,
        alimtalk_replacements=replacements,
        event_type=trigger,
        target_type="staff",
        target_id=getattr(uploaded_by, "id", None),
        target_name=staff_name,
        occurrence_key=f"{trigger}:video:{video.id}",
        source_domain="video",
        source_use_case="encoding_complete",
        domain_object_id=f"video:{video.id}",
        actor_id=getattr(uploaded_by, "id", None),
    )

    # delay_mode 분기: immediate / delay_minutes / scheduled_hour
    delay_mode = getattr(config, "delay_mode", "immediate")
    delay_value = getattr(config, "delay_value", None)

    if delay_mode == "immediate" or not delay_value:
        # 즉시 발송도 durable outbox를 먼저 확보한다.
        from apps.domains.messaging.scheduled import dispatch_notification_now

        dispatch_notification_now(
            tenant_id=tenant_id,
            trigger=trigger,
            payload=message_kwargs,
        )
        _log.info("video_encoding_complete: enqueued alimtalk to %s staff_id=%s video_id=%s",
                   phone[:4] + "****", uploaded_by.id, video.id)
    else:
        # 예약/지연 발송 → ScheduledNotification에 저장
        from apps.domains.messaging.scheduled import schedule_notification
        schedule_notification(
            tenant_id=tenant_id,
            trigger=trigger,
            delay_mode=delay_mode,
            delay_value=delay_value,
            payload=message_kwargs,
        )
        _log.info("video_encoding_complete: scheduled alimtalk (%s=%s) to %s staff_id=%s video_id=%s",
                   delay_mode, delay_value, phone[:4] + "****", uploaded_by.id, video.id)


def job_fail_retry(job_id: str, reason: str) -> tuple[bool, str]:
    """Job FAILED + attempt_count++ + state=RETRY_WAIT. Video는 변경 없음. Terminal 상태는 보호."""
    from django.db import transaction
    from django.db.models import F
    from apps.domains.video.models import VideoTranscodeJob

    TERMINAL_STATES = {
        VideoTranscodeJob.State.SUCCEEDED,
        VideoTranscodeJob.State.FAILED,
        VideoTranscodeJob.State.DEAD,
        VideoTranscodeJob.State.CANCELLED,
    }

    with transaction.atomic():
        job = VideoTranscodeJob.objects.select_for_update().filter(pk=job_id).first()
        if not job:
            return False, "job_not_found"
        if job.state in TERMINAL_STATES:
            import logging
            logging.getLogger(__name__).warning(
                "job_fail_retry: job %s already in terminal state %s, skipping", job_id, job.state,
            )
            return False, "already_terminal"
        failed_aws_id = (getattr(job, "aws_batch_job_id", "") or "").strip()
        job.state = VideoTranscodeJob.State.RETRY_WAIT
        job.attempt_count = F("attempt_count") + 1
        job.error_message = str(reason)[:2000]
        job.locked_by = ""
        job.locked_until = None
        update_fields = ["state", "attempt_count", "error_message", "locked_by", "locked_until", "updated_at"]
        if failed_aws_id:
            job.last_counted_failure_aws_batch_job_id = failed_aws_id
            update_fields.append("last_counted_failure_aws_batch_job_id")
        job.save(update_fields=update_fields)
    return True, "ok"


def job_set_cancel_requested(job_id) -> bool:
    """RUNNING Job에 cancel_requested=True 설정 (retry API에서 협력적 취소용)."""
    from django.utils import timezone
    from apps.domains.video.models import VideoTranscodeJob

    n = VideoTranscodeJob.objects.filter(
        pk=job_id,
        state=VideoTranscodeJob.State.RUNNING,
    ).update(cancel_requested=True, updated_at=timezone.now())
    return n == 1


def job_is_cancel_requested(job_id) -> bool:
    """Job.cancel_requested 여부."""
    from apps.domains.video.models import VideoTranscodeJob

    job = VideoTranscodeJob.objects.filter(pk=job_id).values("cancel_requested").first()
    return bool(job and job.get("cancel_requested"))


def job_cancel(job_id: str) -> bool:
    """Job CANCELLED (재시도 버튼으로 사용자가 취소 요청 시). Terminal 상태는 보호.

    CANCELLED는 terminal이므로 video_id에 걸린 DDB lock도 해제. 안 풀면 12h TTL이
    만료되기 전엔 같은 video를 재업로드해도 새 job 생성이 거부된다 (existing 락 충돌).
    """
    from django.utils import timezone
    from apps.domains.video.models import VideoTranscodeJob

    job = VideoTranscodeJob.objects.filter(pk=job_id).first()
    if not job:
        return False

    n = VideoTranscodeJob.objects.filter(
        pk=job_id,
        state__in=[
            VideoTranscodeJob.State.QUEUED,
            VideoTranscodeJob.State.RUNNING,
            VideoTranscodeJob.State.RETRY_WAIT,
        ],
    ).update(
        state=VideoTranscodeJob.State.CANCELLED,
        locked_by="",
        locked_until=None,
        updated_at=timezone.now(),
    )
    if n == 1:
        try:
            from apps.domains.video.services.video_job_lock import release as lock_release
            lock_release(job.video_id)
        except Exception:
            pass
    return n == 1


def job_mark_dead(job_id: str, error_code: str = "", error_message: str = "") -> bool:
    """Job DEAD. Transactional: Job + Video 원자적 업데이트. Terminal 상태(SUCCEEDED, DEAD, CANCELLED)는 보호."""
    from django.db import transaction
    from apps.domains.video.models import Video, VideoTranscodeJob

    TERMINAL_STATES = {
        VideoTranscodeJob.State.SUCCEEDED,
        VideoTranscodeJob.State.DEAD,
        VideoTranscodeJob.State.CANCELLED,
    }

    err_msg = str(error_message)[:2000]
    err_code = str(error_code)[:64]
    with transaction.atomic():
        job = VideoTranscodeJob.objects.select_for_update().filter(pk=job_id).first()
        if not job:
            return False
        if job.state in TERMINAL_STATES:
            import logging
            logging.getLogger(__name__).warning(
                "job_mark_dead: job %s already in terminal state %s, skipping", job_id, job.state,
            )
            return False
        job.state = VideoTranscodeJob.State.DEAD
        job.error_code = err_code
        job.error_message = err_msg
        job.locked_by = ""
        job.locked_until = None
        job.save(update_fields=["state", "error_code", "error_message", "locked_by", "locked_until", "updated_at"])
        Video.objects.filter(current_job_id=job_id).update(
            status=Video.Status.FAILED,
            error_reason=err_msg or job.error_message,
        )
    try:
        from apps.domains.video.services.video_job_lock import release as lock_release
        lock_release(job.video_id)
    except Exception:
        pass
    try:
        from apps.domains.video.services.ops_events import emit_ops_event
        emit_ops_event(
            "JOB_DEAD",
            severity="ERROR",
            tenant_id=job.tenant_id,
            video_id=job.video_id,
            job_id=job_id,
            aws_batch_job_id=job.aws_batch_job_id or "",
            payload={"error_code": err_code, "error_message": err_msg[:500]},
        )
    except Exception:
        pass
    _cache_video_status_safe(
        job.video_id,
        job.tenant_id,
        getattr(Video.Status.FAILED, "value", "FAILED"),
        error_reason=err_msg or job.error_message,
        ttl=None,
    )
    try:
        from apps.domains.video.redis_status_cache import delete_video_progress_key
        delete_video_progress_key(job.tenant_id, job.video_id)
    except Exception:
        pass
    return True


def job_mark_dead_if_active(
    job_id: str,
    error_code: str = "",
    error_message: str = "",
) -> tuple[bool, int]:
    """
    Mark job DEAD only if state is QUEUED, RUNNING, or RETRY_WAIT (conditional UPDATE).
    Prevents overwriting SUCCEEDED with DEAD during video delete (race with worker job_complete).
    Returns (success, rows_updated). Log DEAD_UPDATED (rows=1) vs DEAD_SKIPPED_ALREADY_TERMINAL (rows=0).
    """
    from django.db import transaction
    from django.utils import timezone
    from apps.domains.video.models import Video, VideoTranscodeJob

    err_msg = str(error_message)[:2000]
    err_code = str(error_code)[:64]
    with transaction.atomic():
        # Conditional update: only non-terminal states
        n = VideoTranscodeJob.objects.filter(
            pk=job_id,
            state__in=[
                VideoTranscodeJob.State.QUEUED,
                VideoTranscodeJob.State.RUNNING,
                VideoTranscodeJob.State.RETRY_WAIT,
            ],
        ).update(
            state=VideoTranscodeJob.State.DEAD,
            error_code=err_code,
            error_message=err_msg,
            locked_by="",
            locked_until=None,
            updated_at=timezone.now(),
        )
        if n == 0:
            return True, 0
        Video.objects.filter(current_job_id=job_id).update(
            status=Video.Status.FAILED,
            error_reason=err_msg,
        )
        job = VideoTranscodeJob.objects.filter(pk=job_id).first()
        if job:
            try:
                from apps.domains.video.services.video_job_lock import release as lock_release
                lock_release(job.video_id)
            except Exception:
                pass
            try:
                from apps.domains.video.services.ops_events import emit_ops_event
                emit_ops_event(
                    "JOB_DEAD",
                    severity="ERROR",
                    tenant_id=job.tenant_id,
                    video_id=job.video_id,
                    job_id=job_id,
                    aws_batch_job_id=job.aws_batch_job_id or "",
                    payload={"error_code": err_code, "error_message": err_msg[:500]},
                )
            except Exception:
                pass
            _cache_video_status_safe(
                job.video_id,
                job.tenant_id,
                getattr(Video.Status.FAILED, "value", "FAILED"),
                error_reason=err_msg,
                ttl=None,
            )
            try:
                from apps.domains.video.redis_status_cache import delete_video_progress_key
                delete_video_progress_key(job.tenant_id, job.video_id)
            except Exception:
                pass
    return True, n


def job_compute_backlog_score() -> float:
    """
    BacklogScore = SUM(CASE WHEN state='QUEUED' THEN 1 WHEN state='RETRY_WAIT' THEN 2 END).
    CloudWatch Metric 교체용 (TargetTracking).
    """
    from django.db.models import Case, IntegerField, Sum, Value, When
    from apps.domains.video.models import VideoTranscodeJob

    score_expr = Case(
        When(state=VideoTranscodeJob.State.QUEUED, then=Value(1)),
        When(state=VideoTranscodeJob.State.RETRY_WAIT, then=Value(2)),
        default=Value(0),
        output_field=IntegerField(),
    )
    result = VideoTranscodeJob.objects.filter(
        state__in=[
            VideoTranscodeJob.State.QUEUED,
            VideoTranscodeJob.State.RETRY_WAIT,
        ]
    ).aggregate(total=Sum(score_expr))
    return float(result["total"] or 0)
