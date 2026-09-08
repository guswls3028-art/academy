
import logging
from uuid import uuid4
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db import models, transaction
from django.db.utils import IntegrityError
from django.utils import timezone

from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.filters import SearchFilter
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet
from rest_framework.parsers import (
    JSONParser,
    MultiPartParser,
    FormParser,
)
from django_filters.rest_framework import DjangoFilterBackend

from apps.core.models import Tenant
from apps.core.parsing import parse_bool
from apps.api.common.query_params import parse_query_int

from academy.adapters.storage.r2_presign import (
    create_presigned_put_url,
    create_presigned_get_url,
    create_multipart_upload,
    create_presigned_upload_part_url,
    complete_multipart_upload,
    abort_multipart_upload,
)
from academy.adapters.storage.r2_objects import head_object


from apps.core.r2_paths import video_raw_key
from apps.core.permissions import IsStudent, TenantResolvedAndStaff
from academy.adapters.db.django import repositories_video as video_repo
from apps.support.video.view_dependencies import (
    clinic_highlight_map_for_video_stats,
    get_or_create_public_video_session,
    get_staff_for_video_upload,
    lock_session_for_video_upload,
)
from ..models import (
    Video,
    VideoFolder,
)
from ..serializers import VideoSerializer, VideoDetailSerializer, VideoFolderSerializer
from ..policy import is_video_progress_complete, normalize_video_max_speed
from ..services.access_resolver import resolve_access_modes_prefetched
from ..services.video_encoding import REASON_SUBMIT_FAILED, create_job_and_submit_batch
from ..youtube import extract_youtube_video_id, youtube_watch_url
from .playback_mixin import VideoPlaybackMixin

# 로거 설정
logger = logging.getLogger(__name__)

# ==================================================
# utils
# ==================================================
def _safe_int(v, default=None):
    try:
        return int(v)
    except Exception:
        return default


def _cache_uploaded_status(video: Video) -> None:
    """upload_complete 직후 Redis-only progress endpoint가 UPLOADED를 반환하도록 동기화."""
    tenant_id = getattr(video, "tenant_id", None)
    if not tenant_id:
        return
    try:
        from apps.domains.video.redis_status_cache import cache_video_status
        cache_video_status(
            tenant_id=tenant_id,
            video_id=video.id,
            status=Video.Status.UPLOADED,
            duration=video.duration,
            ttl=21600,
        )
    except Exception as e:
        logger.debug("VIDEO_UPLOAD_STATUS_CACHE_FAILED | video_id=%s | %s", getattr(video, "id", None), e)


def _cache_failed_status(video: Video, reason: str) -> None:
    tenant_id = getattr(video, "tenant_id", None)
    if not tenant_id:
        return
    try:
        from apps.domains.video.redis_status_cache import cache_video_status
        cache_video_status(
            tenant_id=tenant_id,
            video_id=video.id,
            status=Video.Status.FAILED,
            error_reason=reason,
            ttl=None,
        )
    except Exception as e:
        logger.debug("VIDEO_UPLOAD_FAILED_STATUS_CACHE_FAILED | video_id=%s | %s", getattr(video, "id", None), e)


def _enqueue_uploaded_video_or_response(video: Video, *, context_reason: str = ""):
    result = create_job_and_submit_batch(video)
    if result.job:
        return None
    if result.reject_reason == REASON_SUBMIT_FAILED:
        video.status = Video.Status.FAILED
        video.error_reason = REASON_SUBMIT_FAILED
        video.save(update_fields=["status", "error_reason", "updated_at"])
        _cache_failed_status(video, REASON_SUBMIT_FAILED)
        logger.error(
            "VIDEO_UPLOAD_ENQUEUE_FAILED | video_id=%s | context=%s | reject=%s",
            video.id, context_reason, result.reject_reason,
        )
        return Response(
            {
                "detail": "영상 인코딩 작업을 시작하지 못했습니다. 잠시 후 다시 시도하세요.",
                "code": "VIDEO_BATCH_SUBMIT_FAILED",
            },
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    logger.warning(
        "VIDEO_UPLOAD_ENQUEUE_DEFERRED | video_id=%s | context=%s | reject=%s | "
        "video is UPLOADED; will be picked up by reconciliation or manual retry",
        video.id, context_reason, result.reject_reason,
    )
    return None


def _validate_source_media_via_ffprobe(url: str) -> tuple[bool, dict, str]:
    """
    upload_complete 최소 무결성 검증
    """
    from academy.adapters.video.source_probe import validate_source_media_via_ffprobe

    return validate_source_media_via_ffprobe(url)


# ==================================================
# ✅ EC2 자동 시작 로직 제거됨 (SQS 기반 아키텍처로 전환)
#
# SQS 기반 아키텍처에서는:
# - Worker는 ECS/Fargate에서 자동으로 관리됨
# - 작업이 SQS에 있으면 Worker가 자동으로 처리
# - EC2 인스턴스 수동 관리 불필요
# ==================================================


# ==================================================
# ViewSet
# ==================================================
class VideoViewSet(VideoPlaybackMixin, ModelViewSet):
    """
    Video 관리 + 통계 + 학생 목록
    """

    serializer_class = VideoSerializer

    parser_classes = [JSONParser]

    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        tenant = getattr(self.request, "tenant", None)
        if not tenant:
            from apps.domains.video.models import Video
            return Video.objects.none()
        return video_repo.get_video_queryset_with_relations().filter(
            tenant=tenant
        )

    # 테넌트 스태프(owner/admin/staff/teacher)만 허용 — Django is_staff 없어도 오너·원장 업로드 가능
    STAFF_ACTIONS = {
        "upload_init",
        "upload_complete",
        "upload_refresh_url",
        "upload_multipart_init",
        "upload_multipart_presign",
        "upload_multipart_complete",
        "upload_multipart_abort",
        "retry",
        "create_youtube",
        "create",
        "update",
        "partial_update",
        "reorder",
        "bulk_policy",
        "destroy",
        "public_session",
        "delete_folder",
        "stats",
        "summary",
    }
    # folders: GET=학생 허용(목록), POST=스태프만(생성)
    # list/retrieve: 스태프 전용 (학생은 /student/video/* endpoint만 사용해야 함)
    def get_permissions(self):
        if self.action == "folders":
            if getattr(self.request, "method", "").upper() in ("GET", "HEAD"):
                return [IsAuthenticated()]
            return [IsAuthenticated(), TenantResolvedAndStaff()]
        if self.action in self.STAFF_ACTIONS or self.action in ("list", "retrieve"):
            return [IsAuthenticated(), TenantResolvedAndStaff()]
        return [IsAuthenticated()]

    filter_backends = [DjangoFilterBackend, SearchFilter]
    filterset_fields = ["session", "status", "folder"]
    search_fields = ["title"]

    @transaction.atomic
    def update(self, request, *args, **kwargs):
        return super().update(request, *args, **kwargs)

    def perform_update(self, serializer):
        locked = Video.objects.select_for_update().get(
            pk=serializer.instance.pk,
            tenant=self.request.tenant,
        )
        serializer.instance = locked
        policy_changed = any(
            field in serializer.validated_data
            and serializer.validated_data[field] != getattr(locked, field)
            for field in ("allow_skip", "max_speed", "show_watermark")
        )
        serializer.save(
            policy_version=locked.policy_version + (1 if policy_changed else 0),
        )

    def perform_destroy(self, instance):
        """
        영상 소프트 삭제: 진행 중 Job에 대해 AWS Batch Terminate 호출 → Job DEAD 처리 → soft-delete.
        R2 스토리지 정리는 6개월 보관 후 purge_deleted_videos 커맨드에서 수행.
        Terminate 실패해도 삭제 요청은 성공.
        """
        video_id = instance.id
        # Kill ALL active TranscodeJobs for this video (not just current_job).
        # Soft-delete doesn't trigger CASCADE, so zombie RETRY_WAIT jobs can
        # block tenant concurrency slots indefinitely.
        try:
            from apps.domains.video.models import VideoTranscodeJob
            from apps.domains.video.services.batch_control import terminate_aws_batch_job
            from academy.adapters.db.django.repositories_video import job_mark_dead_if_active

            active_jobs = VideoTranscodeJob.objects.filter(
                video_id=video_id,
                state__in=[
                    VideoTranscodeJob.State.QUEUED,
                    VideoTranscodeJob.State.RUNNING,
                    VideoTranscodeJob.State.RETRY_WAIT,
                ],
            )
            for cur in active_jobs:
                aws_batch_job_id = (getattr(cur, "aws_batch_job_id", None) or "").strip()
                if aws_batch_job_id:
                    try:
                        terminate_aws_batch_job(
                            aws_batch_job_id,
                            "video_deleted",
                            video_id=video_id,
                            job_id=str(cur.id),
                        )
                    except Exception as e:
                        logger.warning("Video delete: Batch terminate failed video_id=%s job_id=%s: %s", video_id, cur.id, e)
                _, rows = job_mark_dead_if_active(
                    str(cur.id),
                    error_code="VIDEO_DELETED",
                    error_message="Video deleted; job marked DEAD",
                )
                if rows:
                    logger.info("Video delete: DEAD_UPDATED video_id=%s job_id=%s", video_id, cur.id)
                else:
                    logger.info("Video delete: DEAD_SKIPPED_ALREADY_TERMINAL video_id=%s job_id=%s", video_id, cur.id)
        except Exception as e:
            logger.warning("Video delete: job cleanup failed video_id=%s: %s", video_id, e)
        # Soft-delete: sets deleted_at, keeps row + R2 files for 6-month retention
        instance.delete()
        logger.info("Video soft-deleted: video_id=%s", video_id)

    @transaction.atomic
    @action(detail=False, methods=["post"], url_path="reorder")
    def reorder(self, request):
        """Atomically update video ordering within the current tenant."""
        from apps.domains.video.models import Video

        items = request.data.get("items")
        if not isinstance(items, list) or not items:
            return Response(
                {"detail": "items에 한 개 이상의 영상 순서를 보내 주세요."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(items) > 500:
            return Response(
                {"detail": "한 번에 최대 500개 영상의 순서를 변경할 수 있습니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        normalized = []
        for item in items:
            if not isinstance(item, dict):
                return Response(
                    {"detail": "각 순서 항목은 id와 order를 포함해야 합니다."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            video_id = item.get("id")
            order = item.get("order")
            if type(video_id) is not int or type(order) is not int or video_id <= 0 or order <= 0:
                return Response(
                    {"detail": "id와 order는 1 이상의 정수여야 합니다."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            normalized.append((video_id, order))

        video_ids = [video_id for video_id, _ in normalized]
        orders = [order for _, order in normalized]
        if len(set(video_ids)) != len(video_ids):
            return Response(
                {"detail": "같은 영상이 순서 요청에 두 번 포함되었습니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(set(orders)) != len(orders):
            return Response(
                {"detail": "영상 순서는 중복될 수 없습니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        requested_videos = list(
            Video.objects
            .filter(tenant=request.tenant, id__in=video_ids)
            .only("id", "session_id", "folder_id")
            .order_by("id")
        )
        if len(requested_videos) != len(video_ids):
            return Response(
                {"detail": "존재하지 않거나 다른 학원 소속인 영상이 포함되어 있습니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        collection_keys = {
            (
                ("folder", video.folder_id)
                if video.folder_id is not None
                else ("session", video.session_id)
                if video.session_id is not None
                else ("orphan", None)
            )
            for video in requested_videos
        }
        if len(collection_keys) != 1:
            return Response(
                {"detail": "서로 다른 차시 또는 폴더의 영상 순서를 한 번에 변경할 수 없습니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        collection_type, collection_id = next(iter(collection_keys))
        if collection_type == "orphan":
            return Response(
                {"detail": "영상은 차시 또는 폴더에 속해야 합니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # All reorder requests for a collection lock the same parent first. This
        # prevents disjoint subsets of one collection from acquiring Video rows
        # in opposite order and deadlocking each other.
        if collection_type == "session":
            locked_parents = video_repo.lock_sessions_by_ids([collection_id])
            collection_filter = {
                "session_id": collection_id,
                "folder_id__isnull": True,
            }
        else:
            locked_parents = video_repo.lock_video_folders_by_ids([collection_id])
            # Public-folder uploads intentionally retain session for legacy
            # access resolution. Folder is the ordering SSOT when present.
            collection_filter = {"folder_id": collection_id}
        if len(locked_parents) != 1:
            return Response(
                {"detail": "영상이 속한 차시 또는 폴더를 찾을 수 없습니다."},
                status=status.HTTP_409_CONFLICT,
            )

        collection_qs = Video.objects.select_for_update().filter(
            tenant=request.tenant,
            **collection_filter,
        )
        collection = list(
            collection_qs.only("id", "order", "session_id", "folder_id").order_by("id")
        )
        collection_by_id = {video.id: video for video in collection}
        if any(video_id not in collection_by_id for video_id in video_ids):
            return Response(
                {"detail": "영상 소속이 변경되어 순서를 저장하지 못했습니다. 다시 시도해 주세요."},
                status=status.HTTP_409_CONFLICT,
            )

        order_by_id = dict(normalized)
        resulting_orders = [
            order_by_id.get(video.id, video.order)
            for video in collection
        ]
        if len(set(resulting_orders)) != len(resulting_orders):
            return Response(
                {
                    "detail": (
                        "변경하지 않은 영상을 포함하면 순서가 중복됩니다. "
                        "같은 차시 또는 폴더의 충돌하는 영상 순서를 함께 보내 주세요."
                    )
                },
                status=status.HTTP_409_CONFLICT,
            )

        # A direct 1↔2 bulk_update violates an immediate PostgreSQL unique
        # constraint row-by-row.  Move the whole locked collection to a
        # collision-free range first, then write the validated final mapping.
        final_order_by_id = {
            video.id: order_by_id.get(video.id, video.order)
            for video in collection
        }
        temporary_base = (
            max(
                [video.order for video in collection]
                + list(final_order_by_id.values())
            )
            + len(collection)
            + 1
        )
        for index, video in enumerate(collection, start=1):
            video.order = temporary_base + index
        Video.objects.bulk_update(collection, ["order"], batch_size=500)
        for video in collection:
            video.order = final_order_by_id[video.id]
        Video.objects.bulk_update(collection, ["order"], batch_size=500)

        return Response({"updated": len(video_ids)}, status=status.HTTP_200_OK)

    @transaction.atomic
    @extend_schema(
        request=inline_serializer(
            name="VideoBulkPolicy",
            fields={
                "session_id": serializers.IntegerField(min_value=1),
                "video_ids": serializers.ListField(
                    child=serializers.IntegerField(min_value=1),
                    allow_empty=False,
                    min_length=1,
                    max_length=500,
                ),
                "allow_skip": serializers.BooleanField(required=False),
                "max_speed": serializers.FloatField(
                    required=False,
                    min_value=0.25,
                    max_value=5.0,
                ),
            },
        ),
        responses={
            200: inline_serializer(
                name="VideoBulkPolicyResponse",
                fields={
                    "updated": serializers.IntegerField(min_value=0),
                    "changed": serializers.IntegerField(min_value=0),
                },
            ),
            400: OpenApiResponse(
                description="Invalid policy fields or a non-exact tenant/session/video target",
            ),
        },
    )
    @action(detail=False, methods=["post"], url_path="bulk-policy")
    def bulk_policy(self, request):
        """Atomically update playback defaults for videos in one tenant session."""
        session_id = request.data.get("session_id")
        video_ids = request.data.get("video_ids")

        if type(session_id) is not int or session_id <= 0:
            return Response(
                {"detail": "session_id는 1 이상의 정수여야 합니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not isinstance(video_ids, list) or not video_ids:
            return Response(
                {"detail": "video_ids에 한 개 이상의 영상을 보내 주세요."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(video_ids) > 500:
            return Response(
                {"detail": "한 번에 최대 500개 영상의 정책을 변경할 수 있습니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if any(type(video_id) is not int or video_id <= 0 for video_id in video_ids):
            return Response(
                {"detail": "video_ids는 1 이상의 정수 목록이어야 합니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(set(video_ids)) != len(video_ids):
            return Response(
                {"detail": "같은 영상을 두 번 선택할 수 없습니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        has_allow_skip = "allow_skip" in request.data
        has_max_speed = "max_speed" in request.data
        if not has_allow_skip and not has_max_speed:
            return Response(
                {"detail": "건너뛰기 또는 최대 배속 중 하나 이상을 선택해 주세요."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        allow_skip = request.data.get("allow_skip")
        if has_allow_skip and type(allow_skip) is not bool:
            return Response(
                {"detail": "allow_skip은 true 또는 false여야 합니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        max_speed = request.data.get("max_speed")
        if has_max_speed:
            if isinstance(max_speed, bool):
                return Response(
                    {"detail": "max_speed는 숫자여야 합니다."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            try:
                max_speed = normalize_video_max_speed(max_speed)
            except ValueError:
                return Response(
                    {"detail": "최대 배속은 0.25 이상 5 이하의 숫자여야 합니다."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        locked_session_ids = list(
            video_repo.session_all_queryset()
            .select_for_update()
            .filter(pk=session_id, lecture__tenant=request.tenant)
            .values_list("id", flat=True)
        )
        if len(locked_session_ids) != 1:
            return Response(
                {"detail": "해당 차시를 찾을 수 없습니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        videos = list(
            Video.objects.select_for_update()
            .filter(
                tenant=request.tenant,
                session_id=session_id,
                id__in=video_ids,
            )
            .only(
                "id",
                "allow_skip",
                "max_speed",
                "policy_version",
                "updated_at",
            )
            .order_by("id")
        )
        if len(videos) != len(video_ids):
            return Response(
                {
                    "detail": (
                        "존재하지 않거나 삭제되었거나 다른 차시 또는 학원 소속인 "
                        "영상이 포함되어 있습니다."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        changed = []
        changed_at = timezone.now()
        for video in videos:
            next_allow_skip = allow_skip if has_allow_skip else video.allow_skip
            next_max_speed = max_speed if has_max_speed else video.max_speed
            if (
                next_allow_skip == video.allow_skip
                and next_max_speed == video.max_speed
            ):
                continue
            video.allow_skip = next_allow_skip
            video.max_speed = next_max_speed
            video.policy_version += 1
            video.updated_at = changed_at
            changed.append(video)

        if changed:
            Video.objects.bulk_update(
                changed,
                ["allow_skip", "max_speed", "policy_version", "updated_at"],
                batch_size=500,
            )

        return Response(
            {"updated": len(videos), "changed": len(changed)},
            status=status.HTTP_200_OK,
        )

    # ==================================================
    # upload/init
    # ==================================================
    @transaction.atomic
    @action(
        detail=False,
        methods=["post"],
        url_path="upload/init",
        parser_classes=[JSONParser],
    )
    def upload_init(self, request):
        session_id = request.data.get("session")
        title = request.data.get("title")
        filename = request.data.get("filename")

        allow_skip = parse_bool(request.data.get("allow_skip", False), field_name="allow_skip")
        try:
            max_speed = normalize_video_max_speed(request.data.get("max_speed", 1.0))
        except ValueError as exc:
            raise ValidationError({"max_speed": str(exc)}) from exc
        show_watermark = parse_bool(
            request.data.get("show_watermark", True),
            field_name="show_watermark",
        )

        if not session_id or not title or not filename:
            return Response(
                {"detail": "session, title, filename required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            session = video_repo.get_session_by_id_with_lecture_tenant(session_id)
        except ObjectDoesNotExist:
            return Response(
                {"detail": "해당 차시를 찾을 수 없습니다. 페이지를 새로고침한 뒤 다시 시도하세요."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        tenant = session.lecture.tenant
        request_tenant = getattr(request, "tenant", None)
        if request_tenant and tenant.id != request_tenant.id:
            return Response(
                {"detail": "다른 프로그램의 차시에는 업로드할 수 없습니다."},
                status=status.HTTP_403_FORBIDDEN,
            )
        tenant_id = tenant.id

        ext = filename.split(".")[-1].lower() if "." in filename else "mp4"
        key = video_raw_key(
            tenant_id=tenant_id,
            session_id=session_id,
            unique_id=str(uuid4()),
            ext=ext,
        )

        # 시스템 강의(공개 영상 컨테이너)인 경우 visibility=PUBLIC 자동 설정
        is_public = getattr(session.lecture, "is_system", False)
        folder_id = request.data.get("folder")
        if folder_id not in (None, ""):
            if not is_public:
                return Response(
                    {"detail": "공개 영상 폴더는 전체공개영상에만 사용할 수 있습니다."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            try:
                # Folder is the ordering SSOT.  Resolve and lock it before any
                # session lock so upload and reorder serialize on one parent.
                folder = VideoFolder.objects.select_for_update().get(
                    pk=folder_id,
                    tenant=tenant,
                )
            except (TypeError, ValueError, VideoFolder.DoesNotExist):
                return Response(
                    {"detail": "폴더를 찾을 수 없습니다."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if folder.session_id and folder.session_id != session.id:
                return Response(
                    {"detail": "선택한 폴더가 이 공개 영상 영역에 속하지 않습니다."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            order = (
                Video.objects.filter(tenant=tenant, folder=folder)
                .aggregate(max_order=models.Max("order"))
                .get("max_order")
                or 0
            ) + 1
        else:
            folder = None
            session = lock_session_for_video_upload(session)
            order = (
                Video.objects.filter(
                    tenant=tenant,
                    session=session,
                    folder__isnull=True,
                )
                .aggregate(max_order=models.Max("order"))
                .get("max_order")
                or 0
            ) + 1

        # uploaded_by: 업로드한 스태프 (인코딩 완료 알림 대상)
        uploaded_by_staff = None
        try:
            uploaded_by_staff = get_staff_for_video_upload(user=request.user, tenant=tenant)
        except Exception:
            pass

        video = video_repo.create_video(
            session=session,
            title=title,
            file_key=key,
            order=order,
            status=Video.Status.PENDING,
            allow_skip=allow_skip,
            max_speed=max_speed,
            show_watermark=show_watermark,
            visibility=Video.Visibility.PUBLIC if is_public else Video.Visibility.ENROLLED,
            tenant=tenant,
            uploaded_by=uploaded_by_staff,
            folder=folder,
        )

        content_type = (request.data.get("content_type") or "video/mp4").split(";")[0]
        upload_url = create_presigned_put_url(key=key, content_type=content_type)

        return Response(
            {
                "video": VideoSerializer(video).data,
                "upload_url": upload_url,
                "file_key": key,
                "content_type": content_type,
            },
            status=status.HTTP_201_CREATED,
        )

    # ==================================================
    # youtube — YouTube 링크 영상 생성 (인코딩 없음)
    # ==================================================
    @transaction.atomic
    @action(
        detail=False,
        methods=["post"],
        url_path="youtube",
        parser_classes=[JSONParser],
    )
    def create_youtube(self, request):
        session_id = request.data.get("session")
        title = (request.data.get("title") or "").strip()
        raw_url = request.data.get("url") or request.data.get("youtube_url") or request.data.get("link")

        if not session_id or not title or not raw_url:
            return Response(
                {"detail": "session, title, url required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            youtube_video_id = extract_youtube_video_id(str(raw_url))
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        allow_skip = parse_bool(request.data.get("allow_skip", False), field_name="allow_skip")
        try:
            max_speed = normalize_video_max_speed(request.data.get("max_speed", 1.0))
        except ValueError as exc:
            raise ValidationError({"max_speed": str(exc)}) from exc
        show_watermark = parse_bool(
            request.data.get("show_watermark", True),
            field_name="show_watermark",
        )

        try:
            session = video_repo.get_session_by_id_with_lecture_tenant(session_id)
        except ObjectDoesNotExist:
            return Response(
                {"detail": "해당 차시를 찾을 수 없습니다. 페이지를 새로고침한 뒤 다시 시도하세요."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        tenant = session.lecture.tenant
        request_tenant = getattr(request, "tenant", None)
        if request_tenant and tenant.id != request_tenant.id:
            return Response(
                {"detail": "다른 프로그램의 차시에는 업로드할 수 없습니다."},
                status=status.HTTP_403_FORBIDDEN,
            )

        is_public = getattr(session.lecture, "is_system", False)
        folder_id = request.data.get("folder")
        if folder_id not in (None, ""):
            if not is_public:
                return Response(
                    {"detail": "공개 영상 폴더는 전체공개영상에만 사용할 수 있습니다."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            try:
                folder = VideoFolder.objects.select_for_update().get(
                    pk=folder_id,
                    tenant=tenant,
                )
            except (TypeError, ValueError, VideoFolder.DoesNotExist):
                return Response(
                    {"detail": "폴더를 찾을 수 없습니다."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if folder.session_id and folder.session_id != session.id:
                return Response(
                    {"detail": "선택한 폴더가 이 공개 영상 영역에 속하지 않습니다."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            order = (
                Video.objects.filter(tenant=tenant, folder=folder)
                .aggregate(max_order=models.Max("order"))
                .get("max_order")
                or 0
            ) + 1
        else:
            folder = None
            session = lock_session_for_video_upload(session)
            order = (
                Video.objects.filter(
                    tenant=tenant,
                    session=session,
                    folder__isnull=True,
                )
                .aggregate(max_order=models.Max("order"))
                .get("max_order")
                or 0
            ) + 1

        uploaded_by_staff = None
        try:
            uploaded_by_staff = get_staff_for_video_upload(user=request.user, tenant=tenant)
        except Exception:
            pass

        video = video_repo.create_video(
            session=session,
            title=title,
            file_key="",
            order=order,
            status=Video.Status.READY,
            allow_skip=allow_skip,
            max_speed=max_speed,
            show_watermark=show_watermark,
            visibility=Video.Visibility.PUBLIC if is_public else Video.Visibility.ENROLLED,
            tenant=tenant,
            uploaded_by=uploaded_by_staff,
            folder=folder,
            source_type=Video.SourceType.YOUTUBE,
            youtube_video_id=youtube_video_id,
            youtube_url=youtube_watch_url(youtube_video_id),
        )

        logger.info(
            "VIDEO_YOUTUBE_CREATED | video_id=%s | tenant_id=%s | session_id=%s | youtube_video_id=%s",
            video.id,
            tenant.id,
            session.id,
            youtube_video_id,
        )

        return Response(
            {"video": VideoSerializer(video, context={"request": request}).data},
            status=status.HTTP_201_CREATED,
        )

    # ==================================================
    # public_session — 공개 영상 업로드/목록용 세션 (테넌트당 1개)
    # ==================================================
    @transaction.atomic
    @action(
        detail=False,
        methods=["get"],
        url_path="public-session",
        url_name="public-session",
    )
    def public_session(self, request):
        """
        테넌트당 공개 영상 전용 시스템 Lecture + Session을 get_or_create 하고
        session_id, lecture_id 를 반환합니다.
        이 세션에 올린 영상은 visibility=PUBLIC으로 설정되어
        프로그램(테넌트)에 등록된 모든 학생이 시청 가능합니다.
        """
        tenant = getattr(request, "tenant", None)
        if not tenant:
            return Response(
                {"detail": "테넌트를 확인할 수 없습니다. X-Tenant-Code 헤더가 필요합니다. 같은 도메인(예: tchul.com)으로 접속했는지 확인하세요."},
                status=status.HTTP_403_FORBIDDEN,
            )
        lecture, session = get_or_create_public_video_session(
            tenant=tenant,
        )
        return Response(
            {"session_id": session.id, "lecture_id": lecture.id},
            status=status.HTTP_200_OK,
        )

    # ==================================================
    # upload/refresh-url — 만료된 presigned PUT URL 재발급
    # ==================================================
    @action(
        detail=True,
        methods=["post"],
        url_path="upload/refresh-url",
        parser_classes=[JSONParser],
    )
    def upload_refresh_url(self, request, pk=None):
        """
        R2 PUT용 presigned URL 재발급.
        대용량 파일 업로드 시 기존 URL 만료 대비.
        PENDING 상태 영상에서만 허용.
        """
        try:
            video = self.get_object()
        except Exception:
            return Response(
                {"detail": "영상을 찾을 수 없습니다."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if video.status != Video.Status.PENDING:
            return Response(
                {"detail": f"URL 재발급은 PENDING 상태에서만 가능합니다. (현재: {video.status})"},
                status=status.HTTP_409_CONFLICT,
            )

        if not video.file_key:
            return Response(
                {"detail": "파일 키가 없습니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        content_type = request.data.get("content_type", "video/mp4").split(";")[0]
        upload_url = create_presigned_put_url(
            key=video.file_key,
            content_type=content_type,
        )

        return Response({
            "upload_url": upload_url,
            "video_id": video.id,
        })

    # ==================================================
    # upload/multipart/init — R2 multipart upload 시작
    # ==================================================
    @action(
        detail=True,
        methods=["post"],
        url_path="upload/multipart/init",
        parser_classes=[JSONParser],
    )
    def upload_multipart_init(self, request, pk=None):
        """
        R2 multipart upload 생성.
        대용량 파일(>100MB)은 multipart로 업로드해야 안정적이며,
        5GiB 초과 파일은 반드시 multipart 필요 (R2 단일 PUT 한계).
        """
        try:
            video = self.get_object()
        except Exception:
            return Response(
                {"detail": "영상을 찾을 수 없습니다."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if video.status != Video.Status.PENDING:
            return Response(
                {"detail": f"PENDING 상태에서만 가능합니다. (현재: {video.status})"},
                status=status.HTTP_409_CONFLICT,
            )

        if not video.file_key:
            return Response(
                {"detail": "파일 키가 없습니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        content_type = request.data.get("content_type", "video/mp4").split(";")[0]

        try:
            upload_id = create_multipart_upload(
                key=video.file_key,
                content_type=content_type,
            )
        except Exception as e:
            logger.exception("MULTIPART_INIT_ERROR | video_id=%s | %s", video.id, e)
            return Response(
                {"detail": "multipart upload 생성에 실패했습니다."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response({
            "upload_id": upload_id,
            "video_id": video.id,
            "file_key": video.file_key,
        })

    # ==================================================
    # upload/multipart/presign — 파트별 presigned URL 발급
    # ==================================================
    @action(
        detail=True,
        methods=["post"],
        url_path="upload/multipart/presign",
        parser_classes=[JSONParser],
    )
    def upload_multipart_presign(self, request, pk=None):
        """
        파트 번호 목록에 대한 presigned URL 일괄 발급.
        요청: { "upload_id": "...", "part_numbers": [1, 2, 3] }
        응답: { "urls": { "1": "https://...", "2": "https://...", ... } }
        """
        try:
            video = self.get_object()
        except Exception:
            return Response(
                {"detail": "영상을 찾을 수 없습니다."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if video.status != Video.Status.PENDING:
            return Response(
                {"detail": f"PENDING 상태에서만 가능합니다. (현재: {video.status})"},
                status=status.HTTP_409_CONFLICT,
            )

        upload_id = request.data.get("upload_id")
        part_numbers = request.data.get("part_numbers", [])

        if not upload_id or not isinstance(part_numbers, list) or not part_numbers:
            return Response(
                {"detail": "upload_id와 part_numbers 필요"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if len(part_numbers) > 100:
            return Response(
                {"detail": "한 번에 최대 100개 파트까지 요청 가능"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            normalized_part_numbers = [
                int(part_number) if not isinstance(part_number, bool) else 0
                for part_number in part_numbers
            ]
        except (TypeError, ValueError):
            normalized_part_numbers = []
        if (
            len(normalized_part_numbers) != len(part_numbers)
            or len(set(normalized_part_numbers)) != len(normalized_part_numbers)
            or any(part_number < 1 or part_number > 10_000 for part_number in normalized_part_numbers)
        ):
            return Response(
                {"detail": "part_numbers는 중복 없는 1~10000 정수여야 합니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            urls = {}
            for pn in normalized_part_numbers:
                urls[str(pn)] = create_presigned_upload_part_url(
                    key=video.file_key,
                    upload_id=upload_id,
                    part_number=pn,
                )
            return Response({"urls": urls})
        except Exception as e:
            logger.exception("MULTIPART_PRESIGN_ERROR | video_id=%s | %s", video.id, e)
            return Response(
                {"detail": "presigned URL 생성에 실패했습니다."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

    # ==================================================
    # upload/multipart/complete — R2 multipart upload 완료
    # ==================================================
    @action(
        detail=True,
        methods=["post"],
        url_path="upload/multipart/complete",
        parser_classes=[JSONParser],
    )
    def upload_multipart_complete(self, request, pk=None):
        """
        R2 multipart upload 완료.
        요청: { "upload_id": "...", "parts": [{"ETag": "...", "PartNumber": 1}, ...] }
        완료 후 기존 upload/complete와 동일한 인코딩 파이프라인 진입.
        """
        try:
            video = self.get_object()
        except Exception:
            return Response(
                {"detail": "영상을 찾을 수 없습니다."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if video.status != Video.Status.PENDING:
            return Response(
                {"detail": f"PENDING 상태에서만 가능합니다. (현재: {video.status})"},
                status=status.HTTP_409_CONFLICT,
            )

        upload_id = request.data.get("upload_id")
        parts = request.data.get("parts", [])

        if not upload_id or not isinstance(parts, list) or not parts:
            return Response(
                {"detail": "upload_id와 parts 필요"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        normalized_parts = []
        try:
            for part in parts:
                raw_part_number = part["PartNumber"]
                part_number = int(raw_part_number) if not isinstance(raw_part_number, bool) else 0
                etag = part["ETag"]
                if not isinstance(etag, str) or not etag or part_number < 1 or part_number > 10_000:
                    raise ValueError
                normalized_parts.append({"ETag": etag, "PartNumber": part_number})
        except (KeyError, TypeError, ValueError):
            return Response(
                {"detail": "parts는 유효한 ETag와 1~10000 PartNumber가 필요합니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len({part["PartNumber"] for part in normalized_parts}) != len(normalized_parts):
            return Response(
                {"detail": "PartNumber는 중복될 수 없습니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        normalized_parts.sort(key=lambda part: part["PartNumber"])

        try:
            complete_multipart_upload(
                key=video.file_key,
                upload_id=upload_id,
                parts=normalized_parts,
            )
        except Exception as e:
            logger.exception("MULTIPART_COMPLETE_ERROR | video_id=%s | %s", video.id, e)
            # 실패 시 abort 시도
            abort_multipart_upload(key=video.file_key, upload_id=upload_id)
            return Response(
                {"detail": f"multipart 완료 실패: {str(e)[:200]}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        # 성공 — 기존 upload_complete 로직으로 인코딩 파이프라인 진입
        return self._trigger_upload_complete_pipeline(video)

    # ==================================================
    # upload/multipart/abort — R2 multipart upload 중단
    # ==================================================
    @action(
        detail=True,
        methods=["post"],
        url_path="upload/multipart/abort",
        parser_classes=[JSONParser],
    )
    def upload_multipart_abort(self, request, pk=None):
        """
        R2 multipart upload 중단 — 불완전 파트 정리.
        요청: { "upload_id": "..." }
        """
        try:
            video = self.get_object()
        except Exception:
            return Response(
                {"detail": "영상을 찾을 수 없습니다."},
                status=status.HTTP_404_NOT_FOUND,
            )

        upload_id = request.data.get("upload_id")
        if not upload_id:
            return Response(
                {"detail": "upload_id 필요"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        abort_multipart_upload(key=video.file_key, upload_id=upload_id)
        logger.info("MULTIPART_ABORTED | video_id=%s | upload_id=%s", video.id, upload_id)
        return Response({"detail": "aborted"}, status=status.HTTP_200_OK)

    def _trigger_upload_complete_pipeline(self, video):
        """multipart 완료 후 기존 upload_complete 파이프라인 재사용."""
        try:
            with transaction.atomic():
                return self._upload_complete_impl(video)
        except Exception as e:
            logger.exception(
                "VIDEO_UPLOAD_COMPLETE_ERROR | video_id=%s | type=%s | %s",
                video.id, type(e).__name__, e,
            )
            return Response(
                {"detail": "업로드 완료 처리 중 오류가 발생했습니다."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

    # ==================================================
    # upload/complete
    # ==================================================
    @transaction.atomic
    @action(
        detail=True,
        methods=["post"],
        url_path="upload/complete",
        parser_classes=[MultiPartParser, FormParser, JSONParser],
    )
    def upload_complete(self, request, pk=None):
        try:
            video = self.get_object()
        except Exception as e:
            logger.exception("VIDEO_UPLOAD_COMPLETE_ERROR | get_object | %s", e)
            return Response(
                {"detail": "영상을 찾을 수 없습니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if getattr(video, "source_type", None) == Video.SourceType.YOUTUBE:
            return Response(
                {"detail": "YouTube 링크 영상은 파일 업로드 완료 처리 대상이 아닙니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not getattr(video, "session", None) or not getattr(video.session, "lecture", None):
            return Response(
                {"detail": "영상이 차시/강의에 연결되어 있지 않아 업로드 완료할 수 없습니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not getattr(video.session.lecture, "tenant", None):
            return Response(
                {"detail": "강의의 프로그램(테넌트) 정보가 없어 업로드 완료할 수 없습니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            return self._upload_complete_impl(video)
        except Exception as e:
            logger.exception(
                "VIDEO_UPLOAD_COMPLETE_ERROR | video_id=%s | type=%s | %s",
                getattr(video, "id", None),
                type(e).__name__,
                e,
            )
            return Response(
                {"detail": "업로드 완료 처리 중 오류가 발생했습니다. 잠시 후 다시 시도하세요."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

    def _upload_complete_impl(self, video):
        """upload_complete 실제 처리 (예외 시 호출부에서 503 반환)."""
        video_id = video.id
        # [TRACE] upload_complete entry
        _tenant_id = getattr(getattr(getattr(video, "session", None), "lecture", None), "tenant_id", None)
        logger.info(
            "VIDEO_UPLOAD_TRACE | upload_complete entry | video_id=%s tenant_id=%s source_path=%s status=%s execution=1_ENTRY",
            video.id,
            _tenant_id,
            video.file_key or "",
            video.status,
        )

        if video.status != Video.Status.PENDING:
            return Response(
                {"detail": f"Invalid status: {video.status}"},
                status=status.HTTP_409_CONFLICT,
            )

        try:
            exists, size = head_object(video.file_key)
        except Exception as e:
            logger.exception("VIDEO_UPLOAD_COMPLETE_ERROR | head_object | video_id=%s | %s", video_id, e)
            return Response(
                {"detail": "저장소 확인 중 오류가 발생했습니다. 잠시 후 다시 시도하세요."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        logger.info(
            "VIDEO_UPLOAD_TRACE | head_object ok | video_id=%s exists=%s size=%s execution=1b_HEAD_OK",
            video_id, exists, size,
        )
        if not exists or size == 0:
            reason = "source_not_found_or_empty"
            video.status = Video.Status.FAILED
            video.error_reason = reason
            video.save(update_fields=["status", "error_reason", "updated_at"])
            _cache_failed_status(video, reason)
            return Response(
                {"detail": "S3 object not found"},
                status=status.HTTP_409_CONFLICT,
            )

        try:
            src_url = create_presigned_get_url(key=video.file_key, expires_in=600)
        except Exception as e:
            logger.exception("VIDEO_UPLOAD_COMPLETE_ERROR | create_presigned_get_url | video_id=%s | %s", video_id, e)
            reason = f"presigned_get_failed:{str(e)[:200]}"
            video.status = Video.Status.FAILED
            video.error_reason = reason
            video.save(update_fields=["status", "error_reason", "updated_at"])
            _cache_failed_status(video, reason)
            return Response(
                {"detail": "presigned_get_failed"},
                status=status.HTTP_409_CONFLICT,
            )
        logger.info("VIDEO_UPLOAD_TRACE | presigned_get ok | video_id=%s execution=1c_PRESIGN_OK", video_id)

        ok, meta, reason = None, {}, ""
        try:
            ok, meta, reason = _validate_source_media_via_ffprobe(src_url)
        except Exception as e:
            logger.exception(
                "VIDEO_UPLOAD_COMPLETE_ERROR | ffprobe raised | video_id=%s | %s | FALLBACK: skip validation, enqueue",
                video_id, e,
            )
            ok, meta, reason = False, {"duration": 0}, "ffprobe_exception_fallback"

        # ffprobe 실패 시(ffmpeg_module_missing, ffprobe_failed, ffprobe_exception 등) 반드시 enqueue
        # duration=None fallback → Encoding worker에서 재검증
        # 절대 enqueue를 스킵하지 않음 (video.status PENDING 유지 방지)
        if not ok:
            duration = _safe_int(meta.get("duration"), None) if meta else None
            video.duration = duration
            video.status = Video.Status.UPLOADED
            video.error_reason = ""
            video.save(update_fields=["status", "duration", "error_reason"])
            _cache_uploaded_status(video)
            logger.info(
                "VIDEO_UPLOAD_TRACE | before enqueue (ffprobe_fail reason=%s duration=%s) | video_id=%s execution=2_BEFORE_ENQUEUE",
                reason, duration, video_id,
            )
            enqueue_error = _enqueue_uploaded_video_or_response(video, context_reason=reason)
            if enqueue_error is not None:
                return enqueue_error
            return Response(VideoSerializer(video).data)

        min_dur = _safe_int(getattr(settings, "VIDEO_MIN_DURATION_SECONDS", 3), 3)
        duration = _safe_int(meta.get("duration"), None)

        if duration is not None and duration < int(min_dur):
            video.duration = duration
            video.status = Video.Status.UPLOADED
            video.error_reason = ""
            video.save(update_fields=["status", "duration", "error_reason"])
            _cache_uploaded_status(video)
            _tid = getattr(getattr(getattr(video, "session", None), "lecture", None), "tenant_id", None)
            logger.info(
                "VIDEO_UPLOAD_TRACE | before enqueue (duration<min branch) | video_id=%s tenant_id=%s source_path=%s execution=2_BEFORE_ENQUEUE",
                video.id, _tid, video.file_key or "",
            )
            enqueue_error = _enqueue_uploaded_video_or_response(video, context_reason="duration_below_min")
            if enqueue_error is not None:
                return enqueue_error
            return Response(VideoSerializer(video).data)

        video.duration = duration
        video.status = Video.Status.UPLOADED
        video.error_reason = ""
        video.save(update_fields=["status", "duration", "error_reason"])
        _cache_uploaded_status(video)
        _tid = getattr(getattr(getattr(video, "session", None), "lecture", None), "tenant_id", None)
        logger.info(
            "VIDEO_UPLOAD_TRACE | before enqueue (normal branch) | video_id=%s tenant_id=%s source_path=%s execution=2_BEFORE_ENQUEUE",
            video.id, _tid, video.file_key or "",
        )
        enqueue_error = _enqueue_uploaded_video_or_response(video, context_reason="normal")
        if enqueue_error is not None:
            return enqueue_error
        return Response(VideoSerializer(video).data)

    # ==================================================
    # retry (Job 기반 re-encode)
    # ==================================================
    # 새 VideoTranscodeJob 생성 + Video.current_job 교체 + enqueue(job_id).
    # 기존 RUNNING Job은 cancel_requested 플래그로 협력적 취소.
    @transaction.atomic
    @action(detail=True, methods=["post"], url_path="retry")
    def retry(self, request, pk=None):
        from academy.adapters.db.django.repositories_video import job_mark_dead, job_set_cancel_requested
        from apps.domains.video.models import VideoTranscodeJob
        from apps.domains.video.services.batch_submit import terminate_video_job

        try:
            video = Video.objects.select_for_update(of=("self",)).select_related("tenant", "session__lecture__tenant").get(
                pk=self.get_object().pk
            )
        except Video.DoesNotExist:
            raise ValidationError("해당 영상을 찾을 수 없습니다.")

        if not getattr(video, "session", None) or not getattr(video.session, "lecture", None):
            raise ValidationError("영상이 차시/강의에 연결되어 있지 않아 재처리할 수 없습니다.")
        if not getattr(video.session.lecture, "tenant", None):
            raise ValidationError("강의의 프로그램(테넌트) 정보가 없어 재처리할 수 없습니다.")

        if getattr(video, "source_type", None) == Video.SourceType.YOUTUBE:
            raise ValidationError("YouTube 링크 영상은 인코딩 재시도 대상이 아닙니다.")

        # PENDING + file_key: upload-complete may have failed (network/timeout); retry = re-run upload-complete
        if video.status == Video.Status.PENDING:
            if video.file_key:
                return self._upload_complete_impl(video)
            raise ValidationError("업로드가 완료되지 않았습니다. 파일을 먼저 업로드해 주세요.")

        try:
            STALE_QUEUED_THRESHOLD = getattr(
                settings, "VIDEO_RETRY_STALE_QUEUED_HOURS", 1
            )
            STALE_RUNNING_THRESHOLD = getattr(
                settings, "VIDEO_RETRY_STALE_RUNNING_MINUTES", 30
            )
            now = timezone.now()
            stale_cutoff = now - timedelta(hours=STALE_QUEUED_THRESHOLD)
            stale_running_cutoff = now - timedelta(minutes=STALE_RUNNING_THRESHOLD)

            cur = None
            if video.current_job_id:
                cur = VideoTranscodeJob.objects.filter(pk=video.current_job_id).first()
                if cur and cur.state == VideoTranscodeJob.State.RUNNING and not getattr(cur, "cancel_requested", False):
                    # Stale RUNNING detection: if no heartbeat or last activity too old, treat as dead
                    last_activity = cur.last_heartbeat_at or cur.updated_at
                    if last_activity < stale_running_cutoff:
                        logger.warning(
                            "VIDEO_RETRY_STALE_RUNNING | job_id=%s | video_id=%s | last_activity=%s",
                            cur.id, video.id, last_activity,
                        )
                        terminate_video_job(str(cur.id), reason="stale_running_superseded")
                        job_mark_dead(
                            str(cur.id),
                            error_code="STALE_RUNNING",
                            error_message=f"Stale RUNNING job (last activity: {last_activity}), superseded via retry",
                        )
                        video.refresh_from_db()
                        video.current_job_id = None
                        video.status = Video.Status.UPLOADED
                        video.save(update_fields=["current_job_id", "status", "updated_at"])
                    else:
                        return Response(
                            {"detail": "Cannot retry: a job is currently RUNNING. Request cancel first or wait for it to finish."},
                            status=status.HTTP_409_CONFLICT,
                        )
                if cur and cur.state in (VideoTranscodeJob.State.QUEUED, VideoTranscodeJob.State.RETRY_WAIT):
                    has_batch_id = bool((getattr(cur, "aws_batch_job_id", None) or "").strip())
                    if not has_batch_id:
                        job_mark_dead(
                            str(cur.id),
                            error_code="STALE_NO_BATCH_ID",
                            error_message="Stale; no aws_batch_job_id (submit failed or lost), re-enqueued via retry",
                        )
                        video.refresh_from_db()
                        video.current_job_id = None
                        video.status = Video.Status.UPLOADED
                        video.save(update_fields=["current_job_id", "status", "updated_at"])
                    elif cur.updated_at >= stale_cutoff:
                        raise ValidationError("Already in backlog (job queued or retry wait)")
                    else:
                        terminate_video_job(str(cur.id), reason="superseded")
                        job_mark_dead(
                            str(cur.id),
                            error_code="STALE_RETRY",
                            error_message="Stale; re-enqueued via retry (was QUEUED/RETRY_WAIT too long)",
                        )
                        video.refresh_from_db()
                        video.current_job_id = None
                        video.status = Video.Status.UPLOADED
                        video.save(update_fields=["current_job_id", "status", "updated_at"])
                elif cur and cur.state == VideoTranscodeJob.State.RUNNING:
                    terminate_video_job(str(cur.id), reason="superseded")
                    job_set_cancel_requested(cur.id)

            if video.status not in (Video.Status.READY, Video.Status.FAILED):
                if video.status not in (Video.Status.UPLOADED, Video.Status.PROCESSING):
                    raise ValidationError(
                        f"Cannot retry: status must be READY or FAILED (current: {video.status})"
                    )

            # Re-enqueue: ensure source object exists so job does not fail later
            file_key = (video.file_key or "").strip()
            if not file_key:
                raise ValidationError(
                    "업로드된 파일 정보가 없습니다. 삭제 후 다시 업로드해 주세요."
                )
            try:
                exists, size = head_object(file_key)
            except Exception as e:
                logger.exception("VIDEO_RETRY_HEAD_OBJECT_ERROR | video_id=%s | %s", video.id, e)
                raise ValidationError(
                    "저장소 확인 중 오류가 발생했습니다. 잠시 후 다시 시도하세요."
                )
            if not exists or size == 0:
                video.error_reason = "source_not_found_or_empty"
                video.save(update_fields=["error_reason"])
                return Response(
                    {"detail": "S3 object not found"},
                    status=status.HTTP_409_CONFLICT,
                )

            video.status = Video.Status.UPLOADED
            video.save(update_fields=["status", "updated_at"])

            from apps.domains.video.services.video_encoding import REASON_TENANT_LIMIT, REASON_GLOBAL_LIMIT
            result = create_job_and_submit_batch(video)
            if not result.job:
                if result.reject_reason == REASON_TENANT_LIMIT:
                    raise ValidationError(
                        "현재 처리 중인 영상이 많아 대기열이 가득 찼습니다. 처리 완료 후 자동으로 시작되거나, 잠시 후 다시 시도해 주세요."
                    )
                if result.reject_reason == REASON_GLOBAL_LIMIT:
                    raise ValidationError(
                        "전체 처리 대기열이 가득 찼습니다. 잠시 후 다시 시도해 주세요."
                    )
                raise ValidationError(
                    "비디오 작업 등록 실패. API 서버 AWS Batch 설정을 확인하세요."
                )

            logger.info(
                "VIDEO_RETRY_ENQUEUED | job_id=%s | video_id=%s | tenant_id=%s",
                result.job.id, video.id,
                getattr(getattr(getattr(video, "session", None), "lecture", None), "tenant_id", None),
            )
            return Response(
                {"detail": "Video reprocessing queued (Batch)", "job_id": str(result.job.id)},
                status=status.HTTP_202_ACCEPTED,
            )
        except ValidationError:
            raise
        except IntegrityError:
            existing = VideoTranscodeJob.objects.filter(
                video=video,
                state__in=[VideoTranscodeJob.State.QUEUED, VideoTranscodeJob.State.RUNNING, VideoTranscodeJob.State.RETRY_WAIT],
            ).first()
            if existing:
                return Response(
                    {"detail": "Job already active for this video", "job_id": str(existing.id)},
                    status=status.HTTP_200_OK,
                )
            raise ValidationError(
                "재처리 요청 처리 중 충돌이 발생했습니다. 잠시 후 다시 시도해 주세요."
            )
        except Exception as e:
            logger.exception("VIDEO_RETRY_ERROR | video_id=%s | %s", getattr(video, "id", None), e)
            raise ValidationError(
                "재처리 요청 처리 중 오류가 발생했습니다. 잠시 후 다시 시도하거나 관리자에게 문의하세요."
            )

    # ==================================================
    # stats
    # ==================================================
    @action(detail=True, methods=["get"], url_path="stats")
    def stats(self, request, pk=None):
        from apps.core.permissions import is_effective_staff
        if not is_effective_staff(request.user, getattr(request, 'tenant', None)):
            return Response({"detail": "Staff access required."}, status=status.HTTP_403_FORBIDDEN)

        video = self.get_object()
        session = video.session
        lecture = session.lecture if session else None

        if not lecture:
            return Response(
                {"video": VideoDetailSerializer(video).data, "students": [], "total_filtered": 0}
            )

        enrollments = video_repo.get_enrollments_for_lecture_active(lecture)

        progresses = {
            p.enrollment_id: p
            for p in video_repo.get_video_progresses_for_video(video)
        }
        perms = {
            p.enrollment_id: p
            for p in video_repo.get_video_access_for_video(video)
        }
        attendance = {
            a.enrollment_id: a.status
            for a in video_repo.get_attendance_for_session(session)
        }
        access_modes = resolve_access_modes_prefetched(
            video=video,
            enrollments=enrollments,
            progresses_by_enrollment_id=progresses,
            access_by_enrollment_id=perms,
            attendance_status_by_enrollment_id=attendance,
        )

        # ✅ 클리닉 하이라이트 일괄 계산
        tenant = getattr(request, "tenant", None)
        highlight_map = clinic_highlight_map_for_video_stats(
            tenant=tenant,
            enrollment_ids=set(e.id for e in enrollments),
        )

        students = []
        for e in enrollments:
            vp = progresses.get(e.id)
            perm = perms.get(e.id)
            access_mode = access_modes[e.id]
            completed = (
                is_video_progress_complete(vp.progress, vp.completed)
                if vp
                else False
            )

            # Legacy rule for backward compatibility
            rule = perm.rule if perm else "free"
            effective_rule = rule
            if rule == "once" and completed:
                effective_rule = "free"

            lecture = getattr(video.session, "lecture", None) if video.session else None

            # profile_photo_url
            profile_photo_url = None
            r2_key = getattr(e.student, "profile_photo_r2_key", None) or ""
            if r2_key:
                try:
                    from django.conf import settings as _settings
                    from academy.adapters.storage.r2_presign import create_presigned_get_url
                    profile_photo_url = create_presigned_get_url(r2_key, expires_in=3600, bucket=_settings.R2_STORAGE_BUCKET)
                except Exception:
                    pass

            students.append(
                {
                    "enrollment": e.id,
                    "student_id": e.student_id,
                    "student_name": e.student.name,
                    "attendance_status": attendance.get(e.id),
                    "lecture_title": lecture.title if lecture else None,
                    "lecture_color": getattr(lecture, "color", None) if lecture else None,
                    "progress": vp.progress if vp else 0,
                    "completed": completed,
                    "rule": rule,  # Legacy field
                    "effective_rule": effective_rule,  # Legacy field
                    "access_mode": access_mode.value,  # New field
                    "parent_phone": getattr(e.student, "parent_phone", None),
                    "student_phone": getattr(e.student, "phone", None),
                    "school": getattr(e.student, "school", None),
                    "grade": getattr(e.student, "grade", None),
                    "profile_photo_url": profile_photo_url,
                    "name_highlight_clinic_target": highlight_map.get(e.id, False),
                }
            )

        return Response(
            {
                "video": VideoDetailSerializer(video).data,
                "students": students,
                "total_filtered": len(students),
            }
        )

    # ==================================================
    # summary
    # ==================================================
    @action(detail=True, methods=["get"], url_path="summary")
    def summary(self, request, pk=None):
        video = self.get_object()
        lecture = video.session.lecture

        range_key = request.query_params.get("range", "7d")
        now = timezone.now()

        since = None
        if range_key == "24h":
            since = now - timedelta(hours=24)
        elif range_key == "7d":
            since = now - timedelta(days=7)

        enrollments = video_repo.get_enrollments_for_lecture(lecture)
        total = enrollments.count()

        progresses = video_repo.get_video_progresses_for_video(video)
        progress_rows = list(progresses)
        completed_count = sum(
            1 for p in progress_rows
            if is_video_progress_complete(p.progress, p.completed)
        )

        duration = int(video.duration or 0)

        watched_seconds = 0
        for p in progress_rows:
            watched_seconds += int(float(p.progress or 0) * duration)

        completion_rate = (completed_count / total) if total else 0.0

        ev_qs = video_repo.get_playback_events_queryset_for_video(video, since=since)

        weights = {
            "VISIBILITY_HIDDEN": 1,
            "VISIBILITY_VISIBLE": 0,
            "FOCUS_LOST": 2,
            "FOCUS_GAINED": 0,
            "SEEK_ATTEMPT": 3,
            "SPEED_CHANGE_ATTEMPT": 3,
            "FULLSCREEN_ENTER": 0,
            "FULLSCREEN_EXIT": 0,
            "PLAYER_ERROR": 1,
        }

        agg = {}
        for ev in ev_qs.iterator():
            eid = ev.enrollment_id
            if eid not in agg:
                agg[eid] = {
                    "enrollment": eid,
                    "student_name": ev.enrollment.student.name,
                    "score": 0,
                }

            score = int(weights.get(ev.event_type, 1))
            if ev.violated:
                score *= 2
            if ev.violation_reason:
                score += 1

            agg[eid]["score"] += score

        risk_top = sorted(
            agg.values(),
            key=lambda x: x["score"],
            reverse=True,
        )[:5]

        return Response(
            {
                "video_id": video.id,
                "range": range_key,
                "total_students": total,
                "completed_count": completed_count,
                "completion_rate": completion_rate,
                "watched_seconds_est": watched_seconds,
                "risk_top": risk_top,
            }
        )

    # ==================================================
    # student list
    # ==================================================
    @action(
        detail=False,
        methods=["get"],
        url_path="student",
        permission_classes=[IsAuthenticated, IsStudent],
    )
    def student_list(self, request):
        return self._student_list_impl(request)

    # ==================================================
    # video folders — 공개 영상 폴더 관리
    # ==================================================
    @action(
        detail=False,
        methods=["get", "post"],
        url_path="folders",
    )
    def folders(self, request):
        """GET: 폴더 목록 조회. POST: 폴더 생성. (동일 url_path에 메서드별 분기)"""
        if request.method == "GET":
            return self._list_folders_impl(request)
        return self._create_folder_impl(request)

    def _list_folders_impl(self, request):
        """공개 영상 폴더 목록 조회. session_id(레거시) 또는 tenant 기반."""
        tenant = getattr(request, "tenant", None)
        if not tenant:
            return Response(
                {"detail": "Tenant required"},
                status=status.HTTP_403_FORBIDDEN,
            )
        session_id = parse_query_int(request.query_params, "session_id", min_value=1)
        if session_id is not None:
            # 레거시 호환: session_id가 전달되면 해당 세션의 폴더 조회
            try:
                session = video_repo.get_session_by_id_with_lecture_tenant(session_id)
            except ObjectDoesNotExist:
                return Response(
                    {"detail": "Session not found"},
                    status=status.HTTP_404_NOT_FOUND,
                )
            if session.lecture.tenant_id != tenant.id:
                return Response(
                    {"detail": "Session not found"},
                    status=status.HTTP_404_NOT_FOUND,
                )
            folders = VideoFolder.objects.filter(
                models.Q(session=session) | models.Q(tenant=tenant, session__isnull=True)
            ).order_by("order", "name")
        else:
            # 신규: tenant 기반 폴더 조회
            folders = VideoFolder.objects.filter(tenant=tenant).order_by("order", "name")
        return Response(VideoFolderSerializer(folders, many=True).data)

    def _create_folder_impl(self, request):
        """공개 영상 폴더 생성."""
        tenant = getattr(request, "tenant", None)
        if not tenant:
            return Response(
                {"detail": "Tenant required"},
                status=status.HTTP_403_FORBIDDEN,
            )
        session_id = request.data.get("session_id")
        name = request.data.get("name")
        parent_id = request.data.get("parent_id")  # null이면 루트 폴더

        if not name:
            return Response(
                {"detail": "name required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        session = None
        if session_id:
            try:
                session = video_repo.get_session_by_id_with_lecture_tenant(session_id)
            except ObjectDoesNotExist:
                return Response(
                    {"detail": "Session not found"},
                    status=status.HTTP_404_NOT_FOUND,
                )
            if session.lecture.tenant_id != tenant.id:
                return Response(
                    {"detail": "Session not found"},
                    status=status.HTTP_404_NOT_FOUND,
                )

        try:
            with transaction.atomic():
                parent = None
                if parent_id:
                    try:
                        parent = VideoFolder.objects.select_for_update().get(id=parent_id, tenant=tenant)
                    except VideoFolder.DoesNotExist:
                        return Response(
                            {"detail": "Parent folder not found"},
                            status=status.HTTP_404_NOT_FOUND,
                        )
                else:
                    Tenant.objects.select_for_update().get(pk=tenant.pk)

                # 같은 이름의 폴더가 이미 있는지 확인
                if VideoFolder.objects.filter(tenant=tenant, parent=parent, name=name).exists():
                    return Response(
                        {"detail": "Folder with this name already exists"},
                        status=status.HTTP_409_CONFLICT,
                    )

                folder = VideoFolder.objects.create(
                    tenant=tenant,
                    session=session,
                    parent=parent,
                    name=name,
                    order=VideoFolder.objects.filter(tenant=tenant, parent=parent).count(),
                )
        except IntegrityError:
            return Response(
                {"detail": "Folder with this name already exists"},
                status=status.HTTP_409_CONFLICT,
            )

        return Response(VideoFolderSerializer(folder).data, status=status.HTTP_201_CREATED)

    @transaction.atomic
    @action(
        detail=False,
        methods=["delete"],
        url_path="folders/(?P<folder_id>[^/.]+)",
    )
    def delete_folder(self, request, folder_id=None):
        """공개 영상 폴더 삭제."""
        tenant = getattr(request, "tenant", None)
        if not tenant:
            return Response(
                {"detail": "Tenant required"},
                status=status.HTTP_403_FORBIDDEN,
            )
        try:
            folder = VideoFolder.objects.select_for_update().get(
                id=folder_id,
                tenant=tenant,
            )
        except VideoFolder.DoesNotExist:
            return Response(
                {"detail": "Folder not found"},
                status=status.HTTP_404_NOT_FOUND,
            )
        
        # 하위 폴더나 영상이 있는지 확인
        if folder.children.exists():
            return Response(
                {"detail": "Cannot delete folder with subfolders"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if folder.videos.exists():
            return Response(
                {"detail": "Cannot delete folder with videos"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        
        folder.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
