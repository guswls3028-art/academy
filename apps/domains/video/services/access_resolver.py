
"""
Single Source of Truth for video access mode resolution.

Business Logic (SSOT):
- Default: FREE_REVIEW (unlimited review, no monitoring, no DB session/event writes)
- PROCTORED_CLASS: Only for students with Attendance.status == "ONLINE" for that session
  - First watch = class substitute: strict policy + monitoring + audit
  - After completion (VideoProgress.completed OR VideoAccess.proctored_completed_at):
    -> automatically FREE_REVIEW for that video
- Offline students: majority, never monitored
"""

from typing import Iterable, Mapping, Optional

from apps.domains.video.models import Video, AccessMode
from apps.domains.video.policy import is_video_progress_complete
from academy.adapters.db.django import repositories_video as video_repo


def _resolve_access_mode_loaded(
    *,
    perm,
    attendance_status: Optional[str],
    progress,
) -> AccessMode:
    if perm and (
        perm.access_mode == AccessMode.BLOCKED
        or getattr(perm, "rule", None) == "blocked"
    ):
        return AccessMode.BLOCKED

    if attendance_status != "ONLINE":
        return AccessMode.FREE_REVIEW

    # 완료 기준 이상 시청 = 의무 완수. FREE_REVIEW로 자동 전환.
    # 동기화는 progress_views.VideoProgressViewSet.perform_update에서 proctored_completed_at에 시각을
    # 박아 두므로, Redis/DB lag 상황에도 다음 resolve가 안정적으로 FREE_REVIEW를 반환한다.
    if progress and is_video_progress_complete(progress.progress, progress.completed):
        return AccessMode.FREE_REVIEW

    if perm and perm.proctored_completed_at is not None:
        return AccessMode.FREE_REVIEW

    return AccessMode.PROCTORED_CLASS


def resolve_access_mode(
    *,
    video: Video,
    enrollment,
    session_id: Optional[int] = None,
) -> AccessMode:
    """
    Resolve access mode for a video-enrollment pair (SSOT).

    Given: video, enrollment (session from video.session)
    1) BLOCKED if explicit VideoAccess override says so
    2) Attendance.status == "ONLINE" => candidate for PROCTORED_CLASS
    3) Else => FREE_REVIEW
    4) If ONLINE: check completion (VideoProgress.completed OR VideoAccess.proctored_completed_at)
       - Completed => FREE_REVIEW
       - Not completed => PROCTORED_CLASS
    """
    perm = video_repo.video_access_get(video, enrollment)

    if perm and (
        perm.access_mode == AccessMode.BLOCKED
        or getattr(perm, "rule", None) == "blocked"
    ):
        return AccessMode.BLOCKED

    session = video.session
    attendance = video_repo.attendance_filter_session_enrollment(session, enrollment).first()
    attendance_status = attendance.status if attendance else None

    progress = None
    if attendance_status == "ONLINE":
        progress = video_repo.video_progress_get(video, enrollment)

    return _resolve_access_mode_loaded(
        perm=perm,
        attendance_status=attendance_status,
        progress=progress,
    )


def resolve_access_modes_prefetched(
    *,
    video: Video,
    enrollments: Iterable,
    progresses_by_enrollment_id: Mapping[int, object],
    access_by_enrollment_id: Mapping[int, object],
    attendance_status_by_enrollment_id: Mapping[int, Optional[str]],
    session_id: Optional[int] = None,
) -> dict[int, AccessMode]:
    """
    Bulk resolve access modes from already-loaded stats maps.

    This preserves resolve_access_mode's decision order while avoiding
    per-enrollment permission/progress/attendance queries in list views.
    """
    return {
        enrollment.id: _resolve_access_mode_loaded(
            perm=access_by_enrollment_id.get(enrollment.id),
            attendance_status=attendance_status_by_enrollment_id.get(enrollment.id),
            progress=progresses_by_enrollment_id.get(enrollment.id),
        )
        for enrollment in enrollments
    }


def resolve_access_modes_for_videos_prefetched(
    *,
    videos: Iterable[Video],
    enrollment,
    progresses_by_video_id: Mapping[int, object],
    access_by_video_id: Mapping[int, object],
    attendance_status: Optional[str],
) -> dict[int, AccessMode]:
    """
    Bulk resolve access modes for one enrollment across many videos.

    Student session video lists already have the video, progress, permission,
    and session attendance rows loaded. Reusing the loaded inputs keeps the
    access-mode decision identical to resolve_access_mode while avoiding one
    permission/progress/attendance query per video.
    """
    return {
        video.id: _resolve_access_mode_loaded(
            perm=access_by_video_id.get(video.id),
            attendance_status=attendance_status,
            progress=progresses_by_video_id.get(video.id),
        )
        for video in videos
    }


def get_effective_access_mode(
    *,
    video: Video,
    enrollment,
    session_id: Optional[int] = None,
) -> AccessMode:
    """
    Effective access mode considering admin overrides.
    """
    if (
        video.visibility != Video.Visibility.PUBLIC
        and not video_repo.session_enrollment_exists(video.session, enrollment)
    ):
        return AccessMode.BLOCKED

    perm = video_repo.video_access_get(video, enrollment)

    if perm and (
        perm.access_mode == AccessMode.BLOCKED
        or getattr(perm, "rule", None) == "blocked"
    ):
        return AccessMode.BLOCKED

    if getattr(enrollment, "status", None) != "ACTIVE":
        from apps.domains.video.services.inactive_entitlements import (
            get_active_inactive_video_entitlement,
        )

        entitlement = get_active_inactive_video_entitlement(
            video=video,
            enrollment=enrollment,
        )
        return (
            AccessMode(entitlement.access_mode)
            if entitlement is not None
            else AccessMode.BLOCKED
        )

    if perm and perm.is_override:
        return AccessMode(perm.access_mode)

    return resolve_access_mode(
        video=video,
        enrollment=enrollment,
        session_id=session_id,
    )
