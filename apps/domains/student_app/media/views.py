import logging
from typing import Any, Dict, Optional

from django.db.models import Prefetch, Q
from django.http import Http404
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, PolymorphicProxySerializer, extend_schema
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework import status

from apps.domains.student_app.permissions import IsStudentOrParent, get_request_student
from apps.support.student_app.video_dependencies import (
    active_inactive_video_entitlements_for_student,
    active_enrollments_for_student,
    get_lecture_models,
    get_media_models,
    get_inactive_video_entitlement_error_type,
    get_video_access_mode,
    get_video_comment_models,
    get_video_like_models,
    get_video_model,
    get_video_progress_model,
    resolve_active_inactive_video_entitlement,
    resolve_access_modes_for_videos_prefetched,
    update_inactive_entitled_video_progress,
)
from apps.support.student_app.video_media import (
    bounded_direct_media_expiry,
    bounded_inactive_media_expiry,
    build_thumbnail_url,
    issue_direct_playback_access_grant,
    issue_playback_access_grant,
    pick_video_urls,
)
from apps.domains.video.contracts import (
    build_effective_playback_policy,
    consume_video_forward_skip,
    sort_videos_for_playlist,
    youtube_embed_url,
)
from academy.application.use_cases.student_video_access_context import (
    StudentVideoAccessError,
    direct_entitlements_for_request_student,
    ensure_student_video_watch_allowed,
    is_video_progress_complete,
    normalize_video_progress,
    resolve_student_session_video_context,
    resolve_student_video_access_context,
    student_can_access_video,
)
from apps.domains.enrollment.selectors import learning_history_enrollments_for_student
from apps.api.common.query_params import parse_query_bool
from .serializers import (
    StudentVideoAccessCheckSerializer,
    StudentVideoForwardSkipRequestSerializer,
    StudentVideoForwardSkipResponseSerializer,
    StudentVideoListItemSerializer,
    StudentVideoPlaybackSerializer,
    StudentVideoPlaybackRequestSerializer,
)


logger = logging.getLogger(__name__)
AccessMode = get_video_access_mode()
InactiveVideoEntitlementError = get_inactive_video_entitlement_error_type()


def _student_access_error_payload(error: StudentVideoAccessError) -> dict:
    payload = {"detail": error.detail}
    if error.code:
        payload["code"] = error.code
    return payload


# ======================================================
# 내부 유틸 (운영 안정성 우선)
# ======================================================

def _import_media_models():
    return get_media_models()


def _coerce_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _get_explicit_enrollment_id(request, *, include_body: bool = False) -> Optional[int]:
    sources = [getattr(request, "query_params", None)]
    if include_body:
        sources.append(getattr(request, "data", None))
    for source in sources:
        if source is None:
            continue
        for key in ("enrollment", "enrollment_id"):
            if key not in source:
                continue
            value = _coerce_int(source.get(key))
            if value is None:
                raise ValidationError({key: "양의 정수 수강 ID가 필요합니다."})
            return value
    return None


def _safe_video_progress(value: Any) -> float:
    return normalize_video_progress(value)


def _safe_video_duration(value: Any) -> int:
    try:
        duration = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, duration)


def _safe_video_position(value: Any) -> int:
    try:
        position = int(float(value or 0))
    except (TypeError, ValueError):
        return 0
    return max(0, position)


def _safe_video_completed(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in {"", "0", "false", "f", "no", "n", "off", "none", "null"}:
        return False
    return True


def _effective_rule(video_permission_obj) -> str:
    if not video_permission_obj:
        return "free"

    rule = getattr(video_permission_obj, "rule", None) or getattr(
        video_permission_obj, "effective_rule", None
    )
    return rule if rule in ("free", "once", "blocked") else "free"


def _policy_from_video(video) -> Dict[str, Any]:
    return {
        "allow_skip": bool(getattr(video, "allow_skip", False)),
        "max_speed": float(getattr(video, "max_speed", 1.0) or 1.0),
        "show_watermark": bool(getattr(video, "show_watermark", True)),
    }


def _source_type_from_video(video) -> str:
    source_type = (getattr(video, "source_type", "") or "").strip()
    return source_type or ("s3" if getattr(video, "file_key", "") else "unknown")


def _is_youtube_video(video) -> bool:
    try:
        return _source_type_from_video(video) == video.SourceType.YOUTUBE
    except Exception:
        return _source_type_from_video(video) == "youtube"


def _video_source_payload(video) -> Dict[str, Any]:
    return {
        "source_type": _source_type_from_video(video),
        "youtube_video_id": (getattr(video, "youtube_video_id", "") or "").strip(),
        "youtube_url": (getattr(video, "youtube_url", "") or "").strip(),
    }


# ======================================================
# Views
# ======================================================


class StudentPublicSessionView(APIView):
    """
    GET /student/video/public-session/
    테넌트별 공개 영상 세션 ID 반환. 같은 테넌트 학생만 호출 가능.
    """

    permission_classes = [IsAuthenticated, IsStudentOrParent]

    def get(self, request):
        Lecture, Session = get_lecture_models()

        tenant = getattr(request, "tenant", None)
        student = get_request_student(request)
        if not tenant or not student:
            return Response(
                {"detail": "tenant or student required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # 공개 영상: 수강등록 없이 해당 테넌트 소속 학생이면 허용 (1테넌트=1프로그램)
        if getattr(student, "tenant_id", None) != getattr(tenant, "id", None):
            return Response(
                {"detail": "공개 영상은 해당 학원 소속 학생만 이용할 수 있습니다."},
                status=status.HTTP_403_FORBIDDEN,
            )
        lecture = (
            Lecture.objects.filter(tenant=tenant)
            .filter(Q(is_system=True) | Q(title="전체공개영상"))
            .order_by("-is_system", "id")
            .first()
        )
        session = (
            Session.objects.filter(lecture=lecture, order=1).first()
            if lecture is not None
            else None
        )
        if lecture is None or session is None:
            return Response(
                {"detail": "공개 영상 세션이 없습니다."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(
            {"session_id": session.id, "lecture_id": lecture.id},
            status=status.HTTP_200_OK,
        )


class StudentVideoMeView(APIView):
    """
    GET /student/video/me/
    영상 탭용: 전체공개 세션 정보 + 수강 중인 강의별 차시 목록.
    """

    permission_classes = [IsAuthenticated, IsStudentOrParent]

    def get(self, request):
        Lecture, Session = get_lecture_models()

        tenant = getattr(request, "tenant", None)
        student = get_request_student(request)
        if not tenant or not student:
            return Response(
                {"detail": "tenant or student required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        enrollments = (
            active_enrollments_for_student(tenant=tenant, student=student)
            .order_by("lecture__title")
        )
        enrollment_by_lecture = {e.lecture_id: e.id for e in enrollments}
        active_lecture_ids = set(enrollment_by_lecture)
        inactive_entitlements = []
        for candidate in active_inactive_video_entitlements_for_student(
            tenant=tenant,
            student=student,
        ):
            entitlement = resolve_active_inactive_video_entitlement(
                video=candidate.video,
                enrollment=candidate.enrollment,
            )
            if entitlement is not None and entitlement.enrollment.lecture_id not in active_lecture_ids:
                inactive_entitlements.append(entitlement)
        inactive_video_ids_by_lecture: dict[int, set[int]] = {}
        inactive_session_ids_by_lecture: dict[int, set[int]] = {}
        inactive_first_video_by_lecture = {}
        inactive_first_entitlement_by_lecture = {}
        for entitlement in inactive_entitlements:
            lecture_id = entitlement.enrollment.lecture_id
            enrollment_by_lecture[lecture_id] = entitlement.enrollment_id
            inactive_video_ids_by_lecture.setdefault(lecture_id, set()).add(entitlement.video_id)
            inactive_session_ids_by_lecture.setdefault(lecture_id, set()).add(
                entitlement.video.session_id
            )
            inactive_first_video_by_lecture.setdefault(lecture_id, entitlement.video)
            inactive_first_entitlement_by_lecture.setdefault(lecture_id, entitlement)
        direct_entitlements = direct_entitlements_for_request_student(
            request,
            student=student,
        )
        direct_video_ids_by_lecture: dict[int, set[int]] = {}
        direct_session_ids_by_lecture: dict[int, set[int]] = {}
        direct_first_video_by_lecture = {}
        direct_first_entitlement_by_lecture = {}
        for entitlement in direct_entitlements:
            lecture_id = entitlement.video.session.lecture_id
            enrollment_by_lecture.setdefault(lecture_id, None)
            direct_video_ids_by_lecture.setdefault(lecture_id, set()).add(
                entitlement.video_id
            )
            direct_session_ids_by_lecture.setdefault(lecture_id, set()).add(
                entitlement.video.session_id
            )
            direct_first_video_by_lecture.setdefault(lecture_id, entitlement.video)
            direct_first_entitlement_by_lecture.setdefault(lecture_id, entitlement)
        lecture_ids = list(enrollment_by_lecture.keys())
        history_enrollments = list(
            learning_history_enrollments_for_student(
                tenant=tenant,
                student=student,
            ).order_by("lecture__title")
        )
        archived_enrollments = [
            enrollment
            for enrollment in history_enrollments
            if not enrollment.lecture.is_active
        ]
        archived_lecture_ids = [
            enrollment.lecture_id for enrollment in archived_enrollments
        ]
        lectures_qs = (
            Lecture.objects.filter(
                id__in=lecture_ids,
                tenant=tenant,
            )
            .filter(Q(is_active=True) | Q(is_system=True))
            .prefetch_related("sessions")
            .order_by("title")
        )

        # 공개 영상 컨테이너는 선생님의 업로드 시작 시 생성된다. 학생 조회는
        # 저장 상태를 바꾸지 않으며, 아직 컨테이너가 없으면 null을 반환한다.
        public_lecture = (
            Lecture.objects.filter(tenant=tenant)
            .filter(Q(is_system=True) | Q(title="전체공개영상"))
            .order_by("-is_system", "id")
            .first()
        )
        public_session = (
            Session.objects.filter(lecture=public_lecture, order=1).first()
            if public_lecture is not None
            else None
        )

        # 강의별 영상 요약을 한 번의 쿼리로 가져오기 (N+1 방지)
        from django.db.models import Count, Sum
        Video = get_video_model()

        # 수강 강의 세션 + 전체공개영상 세션 모두 포함
        all_lecture_ids = lecture_ids + archived_lecture_ids
        if public_lecture is not None:
            all_lecture_ids.append(public_lecture.id)
        session_ids_all = list(
            Session.objects.filter(lecture_id__in=all_lecture_ids)
            .values_list("id", flat=True)
        )
        video_summary_by_session = {}
        first_video_by_lecture = {}
        if session_ids_all:
            summaries = (
                Video.objects.filter(
                    session_id__in=session_ids_all,
                    status=Video.Status.READY,
                )
                .values("session_id")
                .annotate(
                    video_count=Count("id"),
                    total_duration=Sum("duration"),
                )
            )
            for s in summaries:
                video_summary_by_session[s["session_id"]] = {
                    "video_count": s["video_count"],
                    "total_duration": s["total_duration"] or 0,
                }
            # 강의별 첫 번째 영상 (썸네일용, 한 번의 쿼리)
            first_videos = (
                Video.objects.filter(
                    session_id__in=session_ids_all,
                    status=Video.Status.READY,
                )
                .select_related("session__lecture__tenant")
                .order_by("session__lecture_id", "title", "created_at", "id")
            )
            first_video_by_lecture = {}
            for v in first_videos:
                lid = v.session.lecture_id
                if lid not in first_video_by_lecture:
                    first_video_by_lecture[lid] = v

        lectures_data = []
        for lec in lectures_qs:
            sessions = sorted(lec.sessions.all(), key=lambda x: (x.order, x.id))
            inactive_session_ids = inactive_session_ids_by_lecture.get(lec.id)
            direct_session_ids = direct_session_ids_by_lecture.get(lec.id)
            exact_session_ids = inactive_session_ids or direct_session_ids
            if exact_session_ids is not None:
                sessions = [s for s in sessions if s.id in exact_session_ids]
            sessions_data = [
                {
                    "id": s.id,
                    "title": s.title or s.display_label,
                    "order": s.order,
                    "session_type": s.session_type,
                    "regular_order": s.regular_order,
                    "display_label": s.display_label,
                    "date": s.date.isoformat() if s.date else None,
                }
                for s in sessions
            ]
            # 강의 내 전체 영상 수/시간 합산
            lec_video_count = 0
            lec_total_duration = 0
            for s in sessions:
                exact_video_ids = (
                    inactive_video_ids_by_lecture.get(lec.id)
                    or direct_video_ids_by_lecture.get(lec.id)
                )
                if exact_video_ids is not None:
                    entitled_videos = [
                        entitlement.video
                        for entitlement in [*inactive_entitlements, *direct_entitlements]
                        if entitlement.video_id in exact_video_ids
                        and entitlement.video.session_id == s.id
                    ]
                    lec_video_count += len(entitled_videos)
                    lec_total_duration += sum(int(v.duration or 0) for v in entitled_videos)
                else:
                    info = video_summary_by_session.get(s.id)
                    if info:
                        lec_video_count += info["video_count"]
                        lec_total_duration += info["total_duration"]

            # 썸네일 URL (student-app support helper 재사용)
            first_vid = (
                inactive_first_video_by_lecture.get(lec.id)
                or direct_first_video_by_lecture.get(lec.id)
                or first_video_by_lecture.get(lec.id)
            )
            inactive_first_entitlement = inactive_first_entitlement_by_lecture.get(lec.id)
            direct_first_entitlement = direct_first_entitlement_by_lecture.get(lec.id)
            thumb_url = (
                build_thumbnail_url(
                    first_vid,
                    expires_at=(
                        bounded_inactive_media_expiry(inactive_first_entitlement)
                        if inactive_first_entitlement is not None
                        else (
                            bounded_direct_media_expiry(direct_first_entitlement)
                            if direct_first_entitlement is not None
                            else None
                        )
                    ),
                )
                if first_vid
                else None
            )

            lectures_data.append({
                "id": lec.id,
                "title": lec.title or lec.name or "강의",
                "sessions": sessions_data,
                "enrollment_id": enrollment_by_lecture.get(lec.id),
                "video_count": lec_video_count,
                "total_duration": lec_total_duration,
                "thumbnail_url": thumb_url,
            })

        # 전체공개영상 세션 영상 요약
        public_data = None
        if public_lecture is not None and public_session is not None:
            pub_summary = video_summary_by_session.get(public_session.id)
            pub_video_count = pub_summary["video_count"] if pub_summary else 0
            pub_total_duration = pub_summary["total_duration"] if pub_summary else 0
            pub_first_vid = first_video_by_lecture.get(public_lecture.id)
            pub_thumb_url = build_thumbnail_url(pub_first_vid) if pub_first_vid else None

            public_data = {
                "session_id": public_session.id,
                "lecture_id": public_lecture.id,
                "video_count": pub_video_count,
                "total_duration": pub_total_duration,
                "thumbnail_url": pub_thumb_url,
            }

        archived_data = []
        VideoProgress = get_video_progress_model()
        archived_videos_by_lecture: Dict[int, list[dict[str, Any]]] = {}
        archived_video_ids: list[int] = []
        if archived_lecture_ids:
            archived_videos = list(
                Video.objects.filter(
                    tenant=tenant,
                    session__lecture_id__in=archived_lecture_ids,
                    session__lecture__tenant=tenant,
                    status=Video.Status.READY,
                ).values("id", "duration", "session__lecture_id")
            )
            for video in archived_videos:
                archived_videos_by_lecture.setdefault(
                    int(video["session__lecture_id"]), []
                ).append(video)
                archived_video_ids.append(int(video["id"]))
        archived_progress_map = {}
        archived_open_count_by_video: dict[int, int] = {}
        if archived_video_ids:
            archived_progress_map = {
                (int(progress["enrollment_id"]), int(progress["video_id"])): progress
                for progress in VideoProgress.objects.filter(
                    enrollment_id__in=[item.id for item in archived_enrollments],
                    video_id__in=archived_video_ids,
                ).values("enrollment_id", "video_id", "progress", "completed")
            }
            from apps.core.models import OpsAuditLog

            archived_open_count_by_video = {
                int(row["payload__target_id"]): int(row["open_count"])
                for row in (
                    OpsAuditLog.objects
                    .filter(
                        target_tenant=tenant,
                        target_user=student.user,
                        action="student_activity.target_open",
                        payload__actor_mode="student",
                        payload__category="video",
                        payload__target_type="video",
                        payload__target_id__in=[
                            str(video_id) for video_id in archived_video_ids
                        ],
                    )
                    .values("payload__target_id")
                    .annotate(open_count=Count("id"))
                )
            }
        for enrollment in archived_enrollments:
            lecture_videos = archived_videos_by_lecture.get(enrollment.lecture_id, [])
            progresses = [
                (video, archived_progress_map.get((enrollment.id, int(video["id"]))))
                for video in lecture_videos
            ]
            completed_count = sum(
                1
                for _, progress in progresses
                if progress
                if is_video_progress_complete(
                    progress.get("progress"), progress.get("completed", False)
                )
            )
            watch_duration = sum(
                int(
                    _safe_video_progress(progress.get("progress"))
                    * _safe_video_duration(video.get("duration"))
                )
                for video, progress in progresses
                if progress
            )
            archived_data.append(
                {
                    "id": enrollment.lecture_id,
                    "title": enrollment.lecture.title or "종료 강의",
                    "video_count": len(lecture_videos),
                    "completed_count": completed_count,
                    "watch_duration": watch_duration,
                    "play_count": sum(
                        archived_open_count_by_video.get(int(video["id"]), 0)
                        for video in lecture_videos
                    ),
                }
            )

        return Response({
            "public": public_data,  # null이어도 항상 필드 제공
            "lectures": lectures_data,
            "archived_lectures": archived_data,
        }, status=status.HTTP_200_OK)


class StudentVideoStatsView(APIView):
    """
    GET /student/video/me/stats/
    학생 영상 시청 통계 — 전체 진도율, 완료 영상 수, 강좌별 진도.
    활성 수강 강좌의 READY 영상 전체를 분모로 삼고, VideoProgress는 진도만 보강한다.
    """

    permission_classes = [IsAuthenticated, IsStudentOrParent]

    def get(self, request):
        Video = get_video_model()
        VideoProgress = get_video_progress_model()

        tenant = getattr(request, "tenant", None)
        student = get_request_student(request)

        if not tenant or not student:
            return Response({
                "total_videos": 0,
                "completed_videos": 0,
                "completion_rate": 0,
                "total_watch_duration": 0,
                "total_content_duration": 0,
                "lectures": [],
            })

        # 활성 수강 목록
        enrollments = list(
            active_enrollments_for_student(
                tenant=tenant,
                student=student,
                include_system=True,
            ).filter(Q(lecture__is_active=True) | Q(lecture__is_system=True))
        )

        enrollment_ids = [e.id for e in enrollments]

        if not enrollment_ids:
            return Response({
                "total_videos": 0,
                "completed_videos": 0,
                "completion_rate": 0,
                "total_watch_duration": 0,
                "total_content_duration": 0,
                "lectures": [],
            })

        enrollments_by_lecture: Dict[int, list] = {}
        for enrollment in enrollments:
            enrollments_by_lecture.setdefault(enrollment.lecture_id, []).append(enrollment)

        videos = list(
            Video.objects.filter(
                tenant=tenant,
                session__lecture_id__in=list(enrollments_by_lecture.keys()),
                session__lecture__tenant=tenant,
                status=Video.Status.READY,
            ).values("id", "duration", "session__lecture_id")
        )

        progress_map = {}
        if videos:
            video_ids = [v["id"] for v in videos]
            progresses = VideoProgress.objects.filter(
                enrollment_id__in=enrollment_ids,
                video_id__in=video_ids,
                video__status=Video.Status.READY,
            ).values("enrollment_id", "video_id", "progress", "completed")
            progress_map = {
                (p["enrollment_id"], p["video_id"]): p
                for p in progresses
            }

        total_videos = 0
        completed_videos = 0
        total_watch_duration = 0  # progress * duration 추정
        total_content_duration = 0

        # 강좌별 집계
        lecture_stats: Dict[int, Dict[str, Any]] = {}

        for video in videos:
            lecture_id = video["session__lecture_id"]
            for enrollment in enrollments_by_lecture.get(lecture_id, []):
                duration = _safe_video_duration(video.get("duration", 0))
                progress_obj = progress_map.get((enrollment.id, video["id"]))
                progress = _safe_video_progress(progress_obj.get("progress", 0)) if progress_obj else 0
                completed = is_video_progress_complete(
                    progress,
                    bool(progress_obj.get("completed", False)) if progress_obj else False,
                )

                total_videos += 1
                total_content_duration += duration
                total_watch_duration += int(progress * duration)
                if completed:
                    completed_videos += 1

                if lecture_id not in lecture_stats:
                    lecture_title = getattr(getattr(enrollment, "lecture", None), "title", None)
                    lecture_stats[lecture_id] = {
                        "lecture_id": lecture_id,
                        "title": lecture_title or f"강좌 {lecture_id}",
                        "video_count": 0,
                        "completed_count": 0,
                        "total_duration": 0,
                        "watch_duration": 0,
                    }

                ls = lecture_stats[lecture_id]
                ls["video_count"] += 1
                ls["total_duration"] += duration
                ls["watch_duration"] += int(progress * duration)
                if completed:
                    ls["completed_count"] += 1

        # 강좌별 진도율 계산
        lectures_data = []
        for ls in sorted(lecture_stats.values(), key=lambda x: str(x.get("title") or "")):
            progress_pct = (
                round((ls["completed_count"] / ls["video_count"]) * 100)
                if ls["video_count"] > 0
                else 0
            )
            lectures_data.append({
                "lecture_id": ls["lecture_id"],
                "title": ls["title"],
                "video_count": ls["video_count"],
                "completed_count": ls["completed_count"],
                "total_duration": ls["total_duration"],
                "progress_pct": progress_pct,
            })

        completion_rate = (
            round((completed_videos / total_videos) * 100, 1)
            if total_videos > 0
            else 0
        )

        return Response({
            "total_videos": total_videos,
            "completed_videos": completed_videos,
            "completion_rate": completion_rate,
            "total_watch_duration": total_watch_duration,
            "total_content_duration": total_content_duration,
            "lectures": lectures_data,
        })


def _progress_echo_response(*, video_id: int, enrollment_id: int, request) -> Response:
    """Return the standard progress payload without writing VideoProgress."""
    p = _safe_video_progress(request.data.get("progress"))
    completed = _safe_video_completed(request.data.get("completed", False))
    return Response({
        "id": 0,
        "video_id": video_id,
        "enrollment_id": enrollment_id,
        "progress": p,
        "progress_percent": round(p * 100, 1),
        "completed": is_video_progress_complete(p, completed),
        "last_position": _safe_video_position(request.data.get("last_position")),
    }, status=status.HTTP_200_OK)


class StudentSessionVideoListView(APIView):
    """
    GET /student/video/sessions/{session_id}/videos/
    """

    permission_classes = [IsAuthenticated, IsStudentOrParent]

    def get(self, request, session_id: int):
        _Lecture, SessionModel = get_lecture_models()

        Video, VideoPermission = _import_media_models()
        explicit_enrollment_id = _get_explicit_enrollment_id(request)

        # ✅ 테넌트 early-filter (defense-in-depth) — 후속 access context도
        #    tenant 비교를 하지만, 1차 차단으로 cross-tenant lookup을 금지한다.
        tenant = getattr(request, "tenant", None)
        session_qs = SessionModel.objects.select_related("lecture__tenant")
        if tenant is not None:
            session_qs = session_qs.filter(lecture__tenant=tenant)
        session = session_qs.filter(id=session_id).first()
        if session is None:
            raise Http404

        try:
            access_context = resolve_student_session_video_context(
                request,
                session,
                explicit_enrollment_id=explicit_enrollment_id,
            )
        except StudentVideoAccessError as e:
            if e.status_code == status.HTTP_400_BAD_REQUEST or e.code:
                return Response(_student_access_error_payload(e), status=e.status_code)
            raise PermissionDenied(e.detail)

        enrollment_obj = access_context.enrollment
        inactive_entitlements = access_context.inactive_entitlements_by_video_id or {}
        direct_entitlements = access_context.direct_entitlements_by_video_id or {}

        videos = list(
            Video.objects
            .filter(session_id=session_id, deleted_at__isnull=True)
            .select_related("tenant", "session__lecture__tenant")
            .order_by("order", "title", "id")
        )
        videos = sort_videos_for_playlist(videos)
        exact_entitlements = inactive_entitlements or direct_entitlements
        if exact_entitlements:
            videos = [v for v in videos if v.id in exact_entitlements]

        # 진행률 + 권한을 일괄 조회 (N+1 방지)
        from academy.adapters.db.django import repositories_video as video_repo

        video_ids = [v.id for v in videos]
        progress_map = {}
        perm_map = {}
        attendance_status = None
        if enrollment_obj and video_ids:
            progresses = video_repo.video_progress_filter_video_enrollment_ids(
                video=None,
                enrollment_ids=[enrollment_obj.id],
            ).filter(video_id__in=video_ids)
            progress_map = {p.video_id: p for p in progresses}

            if VideoPermission:
                perms = VideoPermission.objects.filter(
                    video_id__in=video_ids,
                    enrollment_id=enrollment_obj.id,
                )
                perm_map = {p.video_id: p for p in perms}

            attendance = (
                video_repo.attendance_filter_session_enrollment(session, enrollment_obj)
                .only("status")
                .first()
            )
            attendance_status = attendance.status if attendance else None

        access_mode_map = {}
        if enrollment_obj and videos:
            if inactive_entitlements:
                access_mode_map = {
                    video_id: AccessMode(entitlement.access_mode)
                    for video_id, entitlement in inactive_entitlements.items()
                }
            else:
                access_mode_map = resolve_access_modes_for_videos_prefetched(
                    videos=videos,
                    enrollment=enrollment_obj,
                    progresses_by_video_id=progress_map,
                    access_by_video_id=perm_map,
                    attendance_status=attendance_status,
                )
        elif direct_entitlements:
            access_mode_map = {
                video_id: AccessMode.FREE_REVIEW
                for video_id in direct_entitlements
            }

        items = []
        for v in videos:
            perm_obj = perm_map.get(v.id)

            inactive_entitlement = inactive_entitlements.get(v.id)
            direct_entitlement = direct_entitlements.get(v.id)
            thumb = build_thumbnail_url(
                v,
                expires_at=(
                    bounded_inactive_media_expiry(inactive_entitlement)
                    if inactive_entitlement is not None
                    else (
                        bounded_direct_media_expiry(direct_entitlement)
                        if direct_entitlement is not None
                        else None
                    )
                ),
            )

            access_mode_value = None
            if enrollment_obj or direct_entitlement is not None:
                access_mode = access_mode_map.get(v.id)
                access_mode_value = access_mode.value if access_mode else None

            # 진행률 계산 (0-100)
            progress_obj = progress_map.get(v.id)
            progress_ratio = (
                normalize_video_progress(getattr(progress_obj, "progress", 0))
                if progress_obj
                else 0
            )
            progress_percent = round(progress_ratio * 100, 1)
            completed = (
                is_video_progress_complete(
                    progress_ratio,
                    bool(getattr(progress_obj, "completed", False)),
                )
                if progress_obj
                else False
            )

            items.append({
                "id": int(v.id),
                "session_id": int(v.session_id),
                "enrollment_id": int(enrollment_obj.id) if enrollment_obj else None,
                "title": str(v.title),
                "status": str(getattr(v, "status", "READY")),
                **_video_source_payload(v),
                "thumbnail_url": thumb,
                "duration": getattr(v, "duration", None),
                "progress": progress_percent,  # 0-100
                "completed": completed,
                "last_position": int(getattr(progress_obj, "last_position", 0) or 0) if progress_obj else 0,
                "updated_at": v.updated_at.isoformat() if hasattr(v, "updated_at") and v.updated_at else None,
                "created_at": v.created_at.isoformat() if hasattr(v, "created_at") and v.created_at else None,
                "order": getattr(v, "order", 0),
                "view_count": getattr(v, "view_count", 0),
                "like_count": getattr(v, "like_count", 0),
                "comment_count": getattr(v, "comment_count", 0),
                **_policy_from_video(v),
                "effective_rule": _effective_rule(perm_obj),  # Legacy field
                "access_mode": access_mode_value,  # New field
            })

        return Response({
            "items": StudentVideoListItemSerializer(items, many=True).data
        })


class StudentVideoPlaybackView(APIView):
    """
    GET access check / POST playback bootstrap for one student video.
    """

    permission_classes = [IsAuthenticated, IsStudentOrParent]

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name="access_check",
                type=bool,
                location=OpenApiParameter.QUERY,
                required=False,
                description=(
                    "Revalidate exact tenant, enrollment, video, and policy access "
                    "without issuing playback media or recording activity."
                ),
            )
        ],
        responses={
            200: PolymorphicProxySerializer(
                component_name="StudentVideoPlaybackOrAccessCheck",
                serializers=[
                    StudentVideoPlaybackSerializer,
                    StudentVideoAccessCheckSerializer,
                ],
                resource_type_field_name=None,
            )
        },
    )
    def get(self, request, video_id: int):
        if not parse_query_bool(
            request.query_params,
            "access_check",
            default=False,
        ):
            return Response(
                {"detail": "재생 시작은 POST 요청을 사용해 주세요."},
                status=status.HTTP_405_METHOD_NOT_ALLOWED,
            )
        return self._resolve_playback(request, video_id)

    @extend_schema(
        request=StudentVideoPlaybackRequestSerializer,
        responses={200: StudentVideoPlaybackSerializer},
    )
    def post(self, request, video_id: int):
        serializer = StudentVideoPlaybackRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return self._resolve_playback(
            request, video_id, event_protocol_version=serializer.validated_data["event_protocol_version"],
        )

    def _resolve_playback(self, request, video_id: int, *, event_protocol_version=1):
        Video, VideoPermission = _import_media_models()
        explicit_enrollment_id = _get_explicit_enrollment_id(request)
        access_check = parse_query_bool(
            request.query_params,
            "access_check",
            default=False,
        )

        try:
            video_qs = Video.objects.select_related(
                "tenant",
                "session__lecture__tenant",
            ).filter(deleted_at__isnull=True)
            tenant = getattr(request, "tenant", None)
            if tenant is not None:
                video_qs = video_qs.filter(tenant=tenant)
            video = video_qs.get(id=video_id)
        except Video.DoesNotExist:
            raise Http404

        try:
            access_context = resolve_student_video_access_context(
                request,
                video,
                explicit_enrollment_id=explicit_enrollment_id,
                allow_public_enrollment_write=not access_check,
            )
            ensure_student_video_watch_allowed(access_context)
        except StudentVideoAccessError as e:
            if e.status_code == status.HTTP_400_BAD_REQUEST or e.code:
                return Response(_student_access_error_payload(e), status=e.status_code)
            raise PermissionDenied(e.detail)

        enrollment_obj = access_context.enrollment
        access_mode_value = access_context.access_mode_value
        video_access_mode_value = access_mode_value
        inactive_entitlement = access_context.inactive_entitlement
        direct_entitlement = access_context.direct_entitlement
        inactive_media_expires_at = None
        if inactive_entitlement is not None:
            if _is_youtube_video(video):
                raise PermissionDenied(
                    "퇴원 후 개별 영상 권한은 YouTube 영상에 적용할 수 없습니다."
                )
            inactive_media_expires_at = bounded_inactive_media_expiry(
                inactive_entitlement
            )
            if inactive_media_expires_at <= int(timezone.now().timestamp()):
                raise PermissionDenied("영상 권한이 만료되었습니다.")
        elif direct_entitlement is not None:
            if _is_youtube_video(video):
                raise PermissionDenied(
                    "수강 등록 없는 개별 권한은 YouTube 영상에 적용할 수 없습니다."
                )
            inactive_media_expires_at = bounded_direct_media_expiry(
                direct_entitlement
            )

        perm_obj = None
        if VideoPermission and enrollment_obj:
            perm_obj = (
                VideoPermission.objects
                .filter(video_id=video.id, enrollment_id=enrollment_obj.id)
                .first()
            )

        rule = _effective_rule(perm_obj)
        if rule == "blocked":
            raise PermissionDenied("이 영상은 시청이 제한되었습니다.")

        if access_check:
            current_policy = build_effective_playback_policy(
                video=video,
                access_mode=access_mode_value,
                permission=perm_obj,
            )
            return Response(
                StudentVideoAccessCheckSerializer(
                    {
                        "ok": True,
                        "access_mode": current_policy["access_mode"],
                        "monitoring_enabled": current_policy["monitoring_enabled"],
                        "policy_version": int(
                            getattr(video, "policy_version", 1) or 1
                        ),
                    }
                ).data,
                status=status.HTTP_200_OK,
            )

        # 비디오 상태 확인 및 로깅
        video_status = getattr(video, "status", None)
        hls_path = getattr(video, "hls_path", None)
        file_key = getattr(video, "file_key", None)
        
        logger.info(
            f"[StudentVideoPlaybackView] Video {video_id} playback request: "
            f"status={video_status}, hls_path={hls_path}, file_key={file_key}, enrollment_id={getattr(enrollment_obj, 'id', None)}"
        )
        
        # 비디오가 READY 상태가 아니면 에러 반환
        if video_status != video.Status.READY:
            logger.warning(
                f"[StudentVideoPlaybackView] Video {video_id} is not READY: status={video_status}"
            )
            return Response(
                {
                    "detail": f"비디오가 아직 준비되지 않았습니다. (상태: {video_status})",
                    "video_status": str(video_status),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        
        hls_url = None
        mp4_url = None
        play_url = None
        is_youtube = _is_youtube_video(video)
        if is_youtube:
            youtube_video_id = (getattr(video, "youtube_video_id", "") or "").strip()
            if not youtube_video_id:
                logger.error(
                    "[StudentVideoPlaybackView] YouTube video %s has no youtube_video_id",
                    video_id,
                )
                return Response(
                    {
                        "detail": "YouTube 영상 정보가 올바르지 않습니다. 선생님에게 다시 등록을 요청해 주세요.",
                        "video_status": str(video_status),
                    },
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            play_url = youtube_embed_url(youtube_video_id)
        else:
            hls_url, mp4_url = pick_video_urls(
                video,
                request,
                expires_at=inactive_media_expires_at,
            )
            play_url = hls_url or mp4_url

            # 재생 URL이 없으면 에러 반환
            if not play_url:
                logger.error(
                    f"[StudentVideoPlaybackView] Failed to generate playback URL for video {video_id}: "
                    f"hls_path={hls_path}, file_key={file_key}"
                )
                return Response(
                    {
                        "detail": "비디오 재생 URL을 생성할 수 없습니다. 비디오 파일이 처리 중이거나 업로드되지 않았을 수 있습니다.",
                        "video_status": str(video_status),
                        "hls_path": hls_path,
                        "has_file_key": bool(file_key),
                    },
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
        
        thumb = build_thumbnail_url(
            video,
            expires_at=inactive_media_expires_at,
        )

        logger.info(
            "[StudentVideoPlaybackView] Playback media prepared video_id=%s "
            "status=%s expires_at=%s",
            video_id,
            video_status,
            inactive_media_expires_at,
        )

        progress_obj = None
        if enrollment_obj:
            from academy.adapters.db.django import repositories_video as video_repo

            progress_obj = video_repo.video_progress_get(video, enrollment_obj)
        playback_token = None
        playback_session_id = None
        playback_expires_at = None
        playback_policy_version = int(getattr(video, "policy_version", 1) or 1)
        playback_event_protocol_version = 1
        if enrollment_obj or direct_entitlement is not None:
            try:
                if direct_entitlement is not None:
                    playback_grant = issue_direct_playback_access_grant(
                        video=video,
                        entitlement=direct_entitlement,
                        user=request.user,
                    )
                else:
                    playback_grant = issue_playback_access_grant(
                        video=video,
                        enrollment=enrollment_obj,
                        user=request.user,
                        device_id=str(request.headers.get("X-Device-Id") or request.user.id),
                        event_protocol_version=event_protocol_version,
                    )
                playback_token = playback_grant.token
                playback_session_id = playback_grant.session_id
                playback_expires_at = playback_grant.expires_at
                playback_event_protocol_version = playback_grant.event_protocol_version
                playback_policy_version = (
                    playback_grant.policy_version or playback_policy_version
                )
                if not playback_token:
                    return Response(
                        {
                            "detail": (
                                playback_grant.error
                                or "영상 재생 권한이 없습니다."
                            )
                        },
                        status=playback_grant.status_code,
                    )
                access_mode_value = playback_grant.access_mode
                if not access_context.is_public_video:
                    video_access_mode_value = access_mode_value
                if inactive_entitlement is not None or direct_entitlement is not None:
                    inactive_media_expires_at = playback_grant.expires_at
                    hls_url, mp4_url = pick_video_urls(
                        video,
                        request,
                        expires_at=inactive_media_expires_at,
                    )
                    play_url = hls_url or mp4_url
                    thumb = build_thumbnail_url(
                        video,
                        expires_at=inactive_media_expires_at,
                    )
                    if not play_url:
                        return Response(
                            {"detail": "영상 재생 URL을 생성할 수 없습니다."},
                            status=status.HTTP_503_SERVICE_UNAVAILABLE,
                        )
            except Exception as _e:
                logger.exception(
                    "[StudentVideoPlaybackView] access grant issue failed: %s",
                    _e,
                )
                return Response(
                    {
                        "detail": (
                            "영상 재생 권한 확인에 실패했습니다. "
                            "잠시 후 다시 시도해 주세요."
                        )
                    },
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )

        playback_policy = build_effective_playback_policy(
            video=video,
            access_mode=access_mode_value,
            permission=perm_obj,
            progress=progress_obj,
        )

        student = get_request_student(request)
        if student and direct_entitlement is None:
            from apps.domains.students.activity import record_student_target_open

            record_student_target_open(
                request=request,
                student=student,
                screen_id="student.video.player",
                target_type="video",
                target_id=video.id,
                target_label=video.title,
            )

        # Record a view only after the locked grant succeeds.
        from django.core.cache import cache as _cache
        from django.db.models import F as _F

        _view_key = f"video_view:{video_id}:{request.user.id}"
        if direct_entitlement is None and not _cache.get(_view_key):
            Video.objects.filter(id=video_id).update(view_count=_F("view_count") + 1)
            _cache.set(_view_key, 1, 300)

        # 좋아요 여부 확인
        is_liked = False
        student = get_request_student(request)
        if student:
            video_tenant_id = getattr(video, "tenant_id", None)
            if video_tenant_id is None:
                video_tenant_id = (
                    getattr(video.session.lecture, "tenant_id", None)
                    if video.session and video.session.lecture else None
                )
            if video_tenant_id:
                from academy.adapters.db.django import repositories_video as video_repo

                is_liked = video_repo.video_like_exists(
                    video_id=video_id,
                    student=student,
                    tenant_id=video_tenant_id,
                )

        progress_percent = (
            round(normalize_video_progress(getattr(progress_obj, "progress", 0)) * 100, 1)
            if progress_obj
            else 0
        )
        completed = (
            is_video_progress_complete(
                getattr(progress_obj, "progress", 0),
                bool(getattr(progress_obj, "completed", False)),
            )
            if progress_obj
            else False
        )

        payload = {
            "video": {
                "id": int(video.id),
                "session_id": int(video.session_id) if video.session_id is not None else None,
                "enrollment_id": int(enrollment_obj.id) if enrollment_obj else None,
                "title": str(video.title),
                "status": str(getattr(video, "status", "READY")),
                **_video_source_payload(video),
                "thumbnail_url": thumb,
                "duration": getattr(video, "duration", None),
                "progress": progress_percent,
                "completed": completed,
                "last_position": int(getattr(progress_obj, "last_position", 0) or 0) if progress_obj else 0,
                "view_count": getattr(video, "view_count", 0),
                "like_count": getattr(video, "like_count", 0),
                "comment_count": getattr(video, "comment_count", 0),
                "is_liked": is_liked,
                "created_at": video.created_at.isoformat() if hasattr(video, "created_at") and video.created_at else None,
                **_policy_from_video(video),
                "effective_rule": rule,
                "access_mode": video_access_mode_value,
            },
            "hls_url": hls_url,
            "mp4_url": mp4_url,
            "play_url": play_url,
            "playback_token": playback_token,
            "playback_session_id": playback_session_id,
            "playback_expires_at": playback_expires_at,
            "policy_version": playback_policy_version,
            "event_protocol_version": playback_event_protocol_version,
            "policy": {
                **playback_policy,
                "source": {
                    "type": _source_type_from_video(video),
                    "provider": "youtube" if is_youtube else "uploaded",
                    "youtube_video_id": (getattr(video, "youtube_video_id", "") or "").strip(),
                },
                **_policy_from_video(video),
                "effective_rule": rule,
            },
        }

        return Response(
            StudentVideoPlaybackSerializer(payload).data,
            status=status.HTTP_200_OK,
        )


class StudentVideoForwardSkipView(APIView):
    """Grant one server-counted forward skip step for a limited student watch."""

    permission_classes = [IsAuthenticated, IsStudentOrParent]

    @extend_schema(
        request=StudentVideoForwardSkipRequestSerializer,
        responses={200: StudentVideoForwardSkipResponseSerializer},
    )
    def post(self, request, video_id: int):
        Video, _VideoPermission = _import_media_models()
        explicit_enrollment_id = _get_explicit_enrollment_id(request, include_body=True)

        try:
            video_qs = Video.objects.select_related("tenant", "session__lecture")
            tenant = getattr(request, "tenant", None)
            if tenant is not None:
                video_qs = video_qs.filter(tenant=tenant)
            video = video_qs.get(id=video_id)
        except Video.DoesNotExist:
            raise Http404

        try:
            access_context = resolve_student_video_access_context(
                request,
                video,
                explicit_enrollment_id=explicit_enrollment_id,
            )
            ensure_student_video_watch_allowed(access_context)
        except StudentVideoAccessError as e:
            return Response(_student_access_error_payload(e), status=e.status_code)

        enrollment = access_context.enrollment
        if enrollment is None:
            return Response(
                {"detail": "수강 정보가 있는 영상에서만 사용할 수 있습니다.", "code": "enrollment_required"},
                status=status.HTTP_409_CONFLICT,
            )
        if getattr(request.user, "parent_profile", None) is not None:
            return Response(
                {"detail": "앞으로 건너뛰기는 학생 계정에서 사용할 수 있습니다.", "code": "student_only"},
                status=status.HTTP_403_FORBIDDEN,
            )
        progress = get_video_progress_model().objects.filter(
            video=video,
            enrollment=enrollment,
        ).first()
        permission = _VideoPermission.objects.filter(
            video=video,
            enrollment=enrollment,
        ).first() if _VideoPermission else None
        policy = build_effective_playback_policy(
            video=video,
            access_mode=access_context.access_mode_value,
            permission=permission,
            progress=progress,
        )
        if (policy.get("seek") or {}).get("mode") != "budgeted_forward":
            return Response(
                {"detail": "이 영상에는 제한형 건너뛰기가 적용되지 않습니다.", "code": "skip_budget_not_applicable"},
                status=status.HTTP_409_CONFLICT,
            )

        try:
            budget = consume_video_forward_skip(
                video=video,
                enrollment=enrollment,
                require_inactive_entitlement=(
                    access_context.inactive_entitlement is not None
                ),
                expected_policy_version=int(video.policy_version or 1),
            )
        except InactiveVideoEntitlementError as exc:
            return Response(
                {"detail": exc.detail, "code": exc.code},
                status=status.HTTP_403_FORBIDDEN,
            )
        logger.info(
            "student_video_forward_skip video_id=%s enrollment_id=%s granted_seconds=%s used_seconds=%s limit_seconds=%s",
            video.id,
            enrollment.id,
            budget["granted_seconds"],
            budget["used_seconds"],
            budget["limit_seconds"],
        )
        return Response(budget, status=status.HTTP_200_OK)


class StudentVideoProgressView(APIView):
    """
    POST /student/video/videos/{video_id}/progress/
    비디오 진행률 업데이트 (수강 완료, 다시보기 등)
    """

    permission_classes = [IsAuthenticated, IsStudentOrParent]

    def post(self, request, video_id: int):
        Video, _VideoPermission = _import_media_models()
        VideoProgress = get_video_progress_model()
        request_student = get_request_student(request)

        explicit_enrollment_id = _get_explicit_enrollment_id(request, include_body=True)

        try:
            video_qs = Video.objects.select_related("tenant", "session__lecture")
            tenant = getattr(request, "tenant", None)
            if tenant is not None:
                video_qs = video_qs.filter(tenant=tenant)
            video = video_qs.get(id=video_id)
        except Video.DoesNotExist:
            raise Http404

        try:
            access_context = resolve_student_video_access_context(
                request,
                video,
                explicit_enrollment_id=explicit_enrollment_id,
            )
            ensure_student_video_watch_allowed(access_context)
        except StudentVideoAccessError as e:
            return Response(_student_access_error_payload(e), status=e.status_code)

        enrollment = access_context.enrollment
        if enrollment is None:
            return _progress_echo_response(video_id=video.id, enrollment_id=0, request=request)
        if enrollment.student_id != request_student.id:
            return Response(
                {"detail": "선택한 자녀의 수강 정보가 아닙니다.", "code": "student_mismatch"},
                status=status.HTTP_403_FORBIDDEN,
            )

        # 진행률 업데이트 또는 생성
        progress_value = request.data.get("progress", None)  # 0-1 또는 0-100
        completed = request.data.get("completed", None)  # boolean
        last_position = request.data.get("last_position", None)  # seconds

        defaults = {}
        if progress_value is not None:
            defaults["progress"] = _safe_video_progress(progress_value)
        if completed is not None:
            defaults["completed"] = _safe_video_completed(completed)
        if last_position is not None:
            defaults["last_position"] = _safe_video_position(last_position)

        if access_context.inactive_entitlement is not None:
            try:
                progress_obj, created = update_inactive_entitled_video_progress(
                    tenant_id=video.tenant_id,
                    enrollment_id=enrollment.id,
                    video_id=video.id,
                    expected_policy_version=int(video.policy_version or 1),
                    defaults=defaults,
                )
            except InactiveVideoEntitlementError as exc:
                return Response(
                    {"detail": exc.detail, "code": exc.code},
                    status=status.HTTP_403_FORBIDDEN,
                )
        else:
            progress_obj, created = VideoProgress.objects.update_or_create(
                video=video,
                enrollment=enrollment,
                defaults=defaults,
            )

        return Response({
            "id": progress_obj.id,
            "video_id": video.id,
            "enrollment_id": enrollment.id,
            "progress": progress_obj.progress,
            "progress_percent": round(float(progress_obj.progress) * 100, 1),
            "completed": is_video_progress_complete(progress_obj.progress, progress_obj.completed),
            "last_position": progress_obj.last_position,
        }, status=status.HTTP_200_OK)


# ========================================================
# VideoLike (좋아요 토글)
# ========================================================

class StudentVideoLikeView(APIView):
    """
    POST /student/video/videos/{video_id}/like/
    좋아요 토글 (있으면 삭제, 없으면 생성)
    """
    permission_classes = [IsAuthenticated, IsStudentOrParent]

    def post(self, request, video_id: int):
        from django.db.models import F
        Video, VideoLike = get_video_like_models()

        student = get_request_student(request)
        if not student:
            return Response({"detail": "학생 정보가 필요합니다."}, status=status.HTTP_400_BAD_REQUEST)

        tenant = getattr(request, "tenant", None)
        if not tenant:
            return Response({"detail": "tenant 정보가 필요합니다."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            video = Video.objects.select_related("session__lecture").get(id=video_id)
        except Video.DoesNotExist:
            raise Http404

        if not student_can_access_video(request, video):
            raise PermissionDenied("접근 권한이 없습니다.")

        from django.db import transaction, IntegrityError
        from django.db.models.functions import Greatest

        try:
            with transaction.atomic():
                # select_for_update on video row to serialize like toggles
                Video.objects.select_for_update().filter(id=video_id).first()
                existing = VideoLike.objects.filter(video=video, student=student, tenant_id=tenant.id).first()
                if existing:
                    existing.delete()
                    Video.objects.filter(id=video_id).update(
                        like_count=Greatest(F("like_count") - 1, 0)
                    )
                    video.refresh_from_db(fields=["like_count"])
                    return Response({"liked": False, "like_count": video.like_count})
                else:
                    VideoLike.objects.create(video=video, student=student, tenant_id=tenant.id)
                    Video.objects.filter(id=video_id).update(like_count=F("like_count") + 1)
                    video.refresh_from_db(fields=["like_count"])
                    return Response({"liked": True, "like_count": video.like_count})
        except IntegrityError:
            # 동시 요청으로 중복 좋아요 시도 — 현재 상태 반환
            is_liked = VideoLike.objects.filter(video=video, student=student, tenant_id=tenant.id).exists()
            video.refresh_from_db(fields=["like_count"])
            return Response({"liked": is_liked, "like_count": video.like_count})


# ========================================================
# VideoComment (댓글 CRUD)
# ========================================================

class StudentVideoCommentListView(APIView):
    """
    GET  /student/video/videos/{video_id}/comments/  — 댓글 목록 (대댓글 포함)
    POST /student/video/videos/{video_id}/comments/  — 댓글 작성
    """
    permission_classes = [IsAuthenticated, IsStudentOrParent]

    def get(self, request, video_id: int):
        Video, VideoComment = get_video_comment_models()

        tenant = getattr(request, "tenant", None)
        if not tenant:
            return Response({"detail": "tenant required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            video = Video.objects.select_related("session__lecture").get(id=video_id)
        except Video.DoesNotExist:
            raise Http404

        if not student_can_access_video(request, video):
            raise PermissionDenied("접근 권한이 없습니다.")

        # 최상위 댓글만 (parent=None), 대댓글은 prefetch
        # 삭제된 학생의 댓글 제외 (고스트 데이터 방지)
        from django.db.models import Q
        _active_author = Q(author_student__isnull=True) | Q(author_student__deleted_at__isnull=True)
        _active_reply_author = Q(author_student__isnull=True) | Q(author_student__deleted_at__isnull=True)
        comments = (
            VideoComment.objects
            .filter(video=video, tenant_id=tenant.id, parent__isnull=True)
            .filter(_active_author)
            .select_related("author_student", "author_staff")
            .prefetch_related(
                Prefetch(
                    "replies",
                    queryset=VideoComment.objects.filter(_active_reply_author).select_related("author_student", "author_staff"),
                )
            )
            .order_by("-created_at")[:100]
        )

        student = get_request_student(request)

        def _get_comment_photo_url(author_student):
            """댓글 작성자 프로필 사진 URL (R2 presigned only, 로컬 fallback 제거)"""
            if not author_student:
                return None
            r2_key = getattr(author_student, "profile_photo_r2_key", None) or ""
            if r2_key:
                try:
                    from django.conf import settings as _s
                    from academy.adapters.storage.r2_presign import create_presigned_get_url
                    return create_presigned_get_url(r2_key, expires_in=3600, bucket=_s.R2_STORAGE_BUCKET)
                except Exception:
                    pass
            return None

        def _serialize_comment(c):
            photo_url = _get_comment_photo_url(c.author_student) if c.author_student else None
            if not photo_url and c.author_staff and hasattr(c.author_staff, "profile_photo") and c.author_staff.profile_photo:
                try:
                    photo_url = request.build_absolute_uri(c.author_staff.profile_photo.url)
                except Exception:
                    pass

            is_mine = False
            if student and c.author_student_id == student.id:
                is_mine = True

            return {
                "id": c.id,
                "content": c.content if not c.is_deleted else "",
                "author_type": c.author_type,
                "author_name": c.author_name,
                "author_photo_url": photo_url,
                "is_edited": c.is_edited,
                "is_deleted": c.is_deleted,
                "is_mine": is_mine,
                "created_at": c.created_at.isoformat(),
                "reply_count": len(active_replies := [r for r in c.replies.all() if not r.is_deleted]) if not c.is_deleted else 0,
                "replies": [_serialize_comment(r) for r in sorted(
                    active_replies if not c.is_deleted else [],
                    key=lambda r: r.created_at
                )[:20]],
            }

        data = [_serialize_comment(c) for c in comments]
        return Response({"comments": data, "total": len(data)})

    def post(self, request, video_id: int):
        from django.db.models import F
        Video, VideoComment = get_video_comment_models()

        tenant = getattr(request, "tenant", None)
        student = get_request_student(request)

        if not tenant:
            return Response({"detail": "tenant required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            video = Video.objects.select_related("session__lecture").get(id=video_id)
        except Video.DoesNotExist:
            raise Http404

        if not student_can_access_video(request, video):
            raise PermissionDenied("접근 권한이 없습니다.")

        content = str(request.data.get("content", "")).strip()
        if not content:
            return Response({"detail": "댓글 내용을 입력해 주세요."}, status=status.HTTP_400_BAD_REQUEST)
        if len(content) > 2000:
            return Response({"detail": "댓글은 2000자까지 입력할 수 있습니다."}, status=status.HTTP_400_BAD_REQUEST)

        parent_id = request.data.get("parent_id")
        parent = None
        if parent_id:
            parent = VideoComment.objects.filter(id=parent_id, video=video, tenant_id=tenant.id, parent__isnull=True).first()
            if not parent:
                return Response({"detail": "대댓글 대상을 찾을 수 없습니다."}, status=status.HTTP_400_BAD_REQUEST)

        comment = VideoComment.objects.create(
            video=video,
            tenant_id=tenant.id,
            author_student=student,
            parent=parent,
            content=content,
        )

        Video.objects.filter(id=video_id).update(comment_count=F("comment_count") + 1)

        # R2 presigned URL for profile photo (same logic as comment list)
        photo_url = None
        if student:
            r2_key = getattr(student, "profile_photo_r2_key", None) or ""
            if r2_key:
                try:
                    from django.conf import settings as _s
                    from academy.adapters.storage.r2_presign import create_presigned_get_url
                    photo_url = create_presigned_get_url(r2_key, expires_in=3600, bucket=_s.R2_STORAGE_BUCKET)
                except Exception:
                    pass

        return Response({
            "id": comment.id,
            "content": comment.content,
            "author_type": "student",
            "author_name": student.name if student else "",
            "author_photo_url": photo_url,
            "is_edited": False,
            "is_deleted": False,
            "is_mine": True,
            "created_at": comment.created_at.isoformat(),
            "reply_count": 0,
            "replies": [],
        }, status=status.HTTP_201_CREATED)


class StudentVideoCommentDetailView(APIView):
    """
    PATCH  /student/video/comments/{comment_id}/  — 수정
    DELETE /student/video/comments/{comment_id}/  — 삭제
    """
    permission_classes = [IsAuthenticated, IsStudentOrParent]

    def patch(self, request, comment_id: int):
        _Video, VideoComment = get_video_comment_models()

        tenant = getattr(request, "tenant", None)
        student = get_request_student(request)
        if not tenant or not student:
            return Response({"detail": "접근 권한이 없습니다."}, status=status.HTTP_403_FORBIDDEN)

        try:
            comment = VideoComment.objects.get(id=comment_id, tenant_id=tenant.id)
        except VideoComment.DoesNotExist:
            raise Http404

        if comment.author_student_id != student.id:
            raise PermissionDenied("본인 댓글만 수정할 수 있습니다.")

        content = str(request.data.get("content", "")).strip()
        if not content:
            return Response({"detail": "댓글 내용을 입력해 주세요."}, status=status.HTTP_400_BAD_REQUEST)
        if len(content) > 2000:
            return Response({"detail": "댓글은 2000자까지 입력할 수 있습니다."}, status=status.HTTP_400_BAD_REQUEST)

        comment.content = content
        comment.is_edited = True
        comment.save(update_fields=["content", "is_edited", "updated_at"])

        return Response({"id": comment.id, "content": comment.content, "is_edited": True})

    def delete(self, request, comment_id: int):
        from django.db.models import F
        Video, VideoComment = get_video_comment_models()

        tenant = getattr(request, "tenant", None)
        student = get_request_student(request)
        if not tenant or not student:
            return Response({"detail": "접근 권한이 없습니다."}, status=status.HTTP_403_FORBIDDEN)

        try:
            comment = VideoComment.objects.get(id=comment_id, tenant_id=tenant.id)
        except VideoComment.DoesNotExist:
            raise Http404

        if comment.author_student_id != student.id:
            raise PermissionDenied("본인 댓글만 삭제할 수 있습니다.")

        # 멱등 가드: 이미 삭제된 댓글에 대한 재호출은 카운터를 추가 감소시키지 않음.
        # update(is_deleted=False) 절에서만 1회 감소 → 동시 DELETE 호출도 안전.
        updated = VideoComment.objects.filter(id=comment.id, is_deleted=False).update(
            is_deleted=True
        )
        if updated:
            from django.db.models.functions import Greatest
            Video.objects.filter(id=comment.video_id).update(
                comment_count=Greatest(F("comment_count") - 1, 0)
            )

        return Response({"deleted": True})
