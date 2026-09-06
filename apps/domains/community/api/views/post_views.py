import hashlib
import logging
import uuid

from django.db import transaction
from apps.domains.community.services.html_sanitizer import sanitize_html
from django.db.models import Exists, OuterRef, Q
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError

from apps.api.common.query_params import parse_query_int

from apps.domains.community.api.serializers import (
    PostEntitySerializer,
    PostReplySerializer,
    PostAttachmentSerializer,
)
from apps.domains.community.selectors import (
    get_posts_for_node,
    get_post_by_id,
    get_all_posts_for_tenant,
    get_empty_post_queryset,
    get_posts_by_type_for_tenant,
    get_post_counts_by_node,
)
from apps.domains.community.services import CommunityService
from apps.domains.community.models import PostEntity, PostReply, PostAttachment, PostLike, PostReplyLike, CommunityReport, CommunityUserBlock, CommunityNotification
from apps.core.permissions import TenantResolvedAndMember
from apps.core.parsing import parse_bool
from apps.support.community.post_dependencies import (
    dispatch_qna_matchup_search,
    get_reply_event_notifier,
    get_request_student,
    student_activity_rank,
    visible_scope_node_ids_for_students,
)

from ._common import (
    _get_tenant_from_request,
    MAX_ATTACHMENT_SIZE,
    MAX_ATTACHMENTS_PER_POST,
    is_attachment_allowed,
    normalize_idempotency_key,
    sanitize_filename,
)

logger = logging.getLogger(__name__)


class PostViewSet(viewsets.ModelViewSet):
    """Post CRUD. tenant from request. list: ?node_id= or admin list."""
    serializer_class = PostEntitySerializer
    permission_classes = [TenantResolvedAndMember]
    STUDENT_WRITABLE_POST_TYPES = {"board", "qna", "counsel"}
    STUDENT_UPDATE_FIELDS = {"title", "content", "category_label"}

    def update(self, request, *args, **kwargs):
        """학생은 본인 글만 수정 가능. 학부모는 수정 불가."""
        # 학부모 write 차단
        if getattr(request.user, "parent_profile", None) is not None:
            return Response(
                {"detail": "학부모 계정은 글 수정이 제한됩니다.", "code": "parent_read_only"},
                status=status.HTTP_403_FORBIDDEN,
            )
        instance = self.get_object()
        request_student = get_request_student(request)
        if request_student is not None and getattr(instance, "created_by_id", None) != request_student.id:
            return Response({"detail": "권한이 없습니다."}, status=status.HTTP_403_FORBIDDEN)
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        """학생은 본인 글만 삭제 가능. 학부모는 삭제 불가."""
        # 학부모 write 차단
        if getattr(request.user, "parent_profile", None) is not None:
            return Response(
                {"detail": "학부모 계정은 글 삭제가 제한됩니다.", "code": "parent_read_only"},
                status=status.HTTP_403_FORBIDDEN,
            )
        instance = self.get_object()
        request_student = get_request_student(request)
        if request_student is not None and getattr(instance, "created_by_id", None) != request_student.id:
            return Response({"detail": "권한이 없습니다."}, status=status.HTTP_403_FORBIDDEN)
        return super().destroy(request, *args, **kwargs)

    def _is_user_blocked(self, request) -> bool:
        """사용자 커뮤니티 차단 check (#49 G2). 차단된 사용자는 write/reaction 차단."""
        tenant = getattr(request, "tenant", None)
        user = getattr(request, "user", None)
        if not tenant or not user or not getattr(user, "is_authenticated", False):
            return False
        return CommunityUserBlock.objects.filter(tenant=tenant, user=user).exists()

    @staticmethod
    def _parent_read_only_response(action_label: str = "커뮤니티 활동"):
        return Response(
            {"detail": f"학부모 계정은 {action_label}이 제한됩니다.", "code": "parent_read_only"},
            status=status.HTTP_403_FORBIDDEN,
        )

    def _limited_reader_student_ids(self, request) -> list[int]:
        """학생/학부모가 볼 수 있는 학생 id 목록. staff는 이 helper를 쓰지 않는다."""
        request_student = get_request_student(request)
        if request_student is not None:
            return [request_student.id]

        return []

    def _visible_node_ids_for_request(self, request) -> set[int]:
        tenant = getattr(request, "tenant", None)
        student_ids = self._limited_reader_student_ids(request)
        if not tenant or not student_ids:
            return set()

        return visible_scope_node_ids_for_students(
            tenant=tenant,
            student_ids=student_ids,
        )

    def _own_student_post_filter(self, request) -> Q:
        student_ids = self._limited_reader_student_ids(request)
        if not student_ids:
            return Q(pk__in=[])
        return Q(created_by_id__in=student_ids)

    def _post_visible_to_request(self, request, post) -> bool:
        """단건 가시성 SSOT — retrieve/replies/like/reply_like/reply_detail 공용.
        staff: 모두 OK. 학생/학부모: published & 공개 타입 & 수강 매핑 가시성.

        2026-05-11 보안 리뷰 결과: like/reply_like/replies/reply_detail에서 visibility 우회 가능했음.
        helper로 일관 적용해서 학생 권한 누출(student A → student B의 QnA reaction) 차단.
        """
        from apps.domains.community.models import support_kind_for_post

        if support_kind_for_post(post):
            return self._is_staff_request(request)
        if self._is_staff_request(request):
            return True
        student_ids = self._limited_reader_student_ids(request)
        is_own = getattr(post, "created_by_id", None) in student_ids
        if is_own:
            return True
        if getattr(post, "status", "") != "published":
            return False
        from apps.domains.community.models.post import STUDENT_PUBLIC_POST_TYPES
        if getattr(post, "post_type", "") not in STUDENT_PUBLIC_POST_TYPES:
            return False

        mappings = list(getattr(post, "mappings", []).all()) if hasattr(getattr(post, "mappings", None), "all") else []
        if not mappings:
            return True

        visible_node_ids = self._visible_node_ids_for_request(request)
        if not visible_node_ids:
            return False
        return any(getattr(mapping, "node_id", None) in visible_node_ids for mapping in mappings)

    def retrieve(self, request, *args, **kwargs):
        """단건 조회: 학생/학부모는 published 공개 타입 또는 본인 작성 글만 허용."""
        tenant = _get_tenant_from_request(request)
        if not tenant:
            return Response({"detail": "tenant required"}, status=status.HTTP_403_FORBIDDEN)
        pk = int(kwargs.get("pk", 0))
        post = get_post_by_id(tenant, pk)
        if not post or not self._post_visible_to_request(request, post):
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = self.get_serializer(post)
        return Response(serializer.data)

    def _is_staff_request(self, request) -> bool:
        """staff/admin 여부를 TenantMembership 역할로 판단. 학부모는 staff가 아님."""
        user = request.user
        if not user or not user.is_authenticated:
            return False
        tenant = getattr(request, "tenant", None)
        if not tenant:
            return False
        from apps.core.models import TenantMembership
        if TenantMembership.objects.filter(
            tenant=tenant, user=user, is_active=True,
            role__in=["owner", "admin", "staff", "teacher"],
        ).exists():
            return True
        return bool(
            (user.is_superuser or user.is_staff)
            and getattr(user, "tenant_id", None) == tenant.id
        )

    def _can_manage_post_nodes(self, request) -> bool:
        user = request.user
        if not user or not user.is_authenticated:
            return False
        tenant = getattr(request, "tenant", None)
        if not tenant:
            return False
        from apps.core.models import TenantMembership
        if TenantMembership.objects.filter(
            tenant=tenant,
            user=user,
            is_active=True,
            role__in=["owner", "admin", "staff"],
        ).exists():
            return True
        return bool(
            (user.is_superuser or user.is_staff)
            and getattr(user, "tenant_id", None) == tenant.id
        )

    def _with_is_liked(self, qs, request):
        user = getattr(request, "user", None)
        tenant = getattr(request, "tenant", None)
        if not user or not getattr(user, "is_authenticated", False) or not tenant:
            return qs
        return qs.annotate(
            is_liked_anno=Exists(
                PostLike.objects.filter(
                    tenant=tenant,
                    post_id=OuterRef("pk"),
                    user_id=user.id,
                )
            )
        )

    def get_queryset(self):
        tenant = getattr(self.request, "tenant", None)
        if not tenant:
            return get_empty_post_queryset()
        request_student = get_request_student(self.request)
        is_staff = self._is_staff_request(self.request)
        is_limited_reader = request_student is not None or getattr(self.request.user, "parent_profile", None) is not None
        raw = self.request.query_params.get("node_id")
        try:
            node_id = int(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            node_id = None
        if node_id is not None:
            if is_limited_reader and node_id not in self._visible_node_ids_for_request(self.request):
                return get_empty_post_queryset()
            qs = get_posts_for_node(tenant, node_id, include_inherited=True, include_unpublished=is_staff)
            if is_limited_reader:
                from apps.domains.community.models.post import STUDENT_PUBLIC_POST_TYPES
                public_filter = Q(post_type__in=STUDENT_PUBLIC_POST_TYPES) | self._own_student_post_filter(self.request)
                qs = qs.filter(public_filter)
        else:
            qs = get_all_posts_for_tenant(tenant, include_unpublished=is_staff)
            # 학생/학부모 요청 시 node_id 없으면 본인/자녀 작성 글만 반환 (학생 앱 "내 질문" 목록)
            if is_limited_reader:
                qs = qs.filter(self._own_student_post_filter(self.request))

        # F4: post_type server-side filter
        from apps.domains.community.models.post import VALID_POST_TYPES
        post_type_param = (self.request.query_params.get("post_type") or "").strip().lower()
        if post_type_param:
            if post_type_param not in VALID_POST_TYPES:
                from rest_framework.exceptions import ValidationError
                raise ValidationError({"post_type": f"허용되지 않는 타입입니다: {post_type_param}"})
            qs = qs.filter(post_type=post_type_param)

        q = (self.request.query_params.get("q") or "").strip()[:100]
        if q:
            qs = qs.filter(
                Q(title__icontains=q)
                | Q(content__icontains=q)
                | Q(author_display_name__icontains=q)
                | Q(category_label__icontains=q)
            ).distinct()

        return self._with_is_liked(qs, self.request)

    def list(self, request, *args, **kwargs):
        # 학생 "내 질문" 목록: node_id 없이 호출 시 페이지네이션 없이 전체 반환 (학생 앱에서 한 번에 조회)
        request_student = get_request_student(request)
        if request_student is not None and request.query_params.get("node_id") in (None, ""):
            qs = self.filter_queryset(self.get_queryset())
            serializer = self.get_serializer(qs, many=True)
            return Response(serializer.data)
        return super().list(request, *args, **kwargs)

    @action(detail=False, methods=["get"], url_path="notices")
    def notices(self, request):
        """GET /community/posts/notices/ — 공지 목록."""
        return self._list_by_type(request, "notice")

    def _list_by_type(self, request, post_type: str):
        """
        공용: post_type별 목록 (notices/board/materials 공통).
        학생 요청 시 가시성 정책 적용:
          - mapping 없음(전체글) → 보임
          - mapping 있음 → 학생이 수강 중인 강의/세션 node에 매핑된 것만 보임
        """
        tenant = getattr(request, "tenant", None)
        if not tenant:
            return Response({"detail": "tenant required"}, status=status.HTTP_403_FORBIDDEN)
        qs = get_posts_by_type_for_tenant(tenant, post_type)

        # 학생이면 수강 기반 스코프 필터링
        request_student = get_request_student(request)
        is_limited_reader = (
            request_student is not None
            or getattr(request.user, "parent_profile", None) is not None
        )
        if is_limited_reader:
            visible_node_ids = self._visible_node_ids_for_request(request)
            qs = qs.filter(
                Q(mappings__isnull=True) | Q(mappings__node_id__in=visible_node_ids)
            ).distinct()

        # 검색 q — 제목/내용 icontains. tenant scope는 이미 위에서 적용됨.
        # 2026-05-11 보안 리뷰 M2: frontend가 q 파라미터를 보내지만 server는 무시했음.
        q = (request.query_params.get("q") or "").strip()[:100]
        if q:
            qs = qs.filter(
                Q(title__icontains=q)
                | Q(content__icontains=q)
                | Q(author_display_name__icontains=q)
                | Q(category_label__icontains=q)
            ).distinct()

        # 정렬 ordering — latest(default) / replies / likes
        # P3 follow-up: like_count_anno/replies_count는 _base_queryset 이미 annotate.
        ordering = (request.query_params.get("ordering") or "").strip().lower()
        if ordering == "likes":
            qs = qs.order_by("-like_count_anno", "-created_at")
        elif ordering == "replies":
            qs = qs.order_by("-replies_count", "-created_at")

        page_size = min(
            parse_query_int(
                request.query_params,
                "page_size",
                default=50,
                min_value=1,
            ),
            200,
        )
        page = parse_query_int(
            request.query_params,
            "page",
            default=1,
            min_value=1,
        )
        total = qs.count()
        offset = (page - 1) * page_size
        page_qs = self._with_is_liked(qs, request)[offset : offset + page_size]
        serializer = self.get_serializer(page_qs, many=True)
        return Response({
            "count": total,
            "results": serializer.data,
        })

    @action(detail=False, methods=["get"], url_path="board")
    def board(self, request):
        """GET /community/posts/board/ — 게시판 목록."""
        return self._list_by_type(request, "board")

    @action(detail=False, methods=["get"], url_path="materials")
    def materials(self, request):
        """GET /community/posts/materials/ — 자료실 목록."""
        return self._list_by_type(request, "materials")
    @action(detail=False, methods=["get"], url_path="my-activity")
    def my_activity(self, request):
        """GET /community/posts/my-activity/?days=30
        본인 활동 카운트 + 학생 ranking 등수(학생인 경우만). 학생/학부모/staff 모두 사용 가능.
        SSOT: AdminCommunityStatsView의 top_students 알고리즘 재사용.
        """
        from datetime import timedelta
        from django.utils import timezone
        from apps.domains.community.models import PostLike, PostReplyLike
        tenant = getattr(request, "tenant", None)
        if not tenant:
            return Response({"detail": "tenant required"}, status=status.HTTP_403_FORBIDDEN)
        days = max(
            1,
            min(
                parse_query_int(request.query_params, "days", default=30),
                365,
            ),
        )
        since = timezone.now() - timedelta(days=days)

        request_student = get_request_student(request)
        if request_student is None:
            # staff 본인: 본인 PostEntity(created_by=null이지만 author_display_name + author_role=staff) 카운트는 별도. 단순화: staff는 본인 활동 X.
            return Response({"is_student": False, "days": days, "post_count": 0, "reply_count": 0, "received_likes": 0, "rank": None, "total_active_students": 0})

        # 학생 본인 카운트
        post_count = PostEntity.objects.filter(tenant=tenant, created_by=request_student, created_at__gte=since).count()
        reply_count = PostReply.objects.filter(tenant=tenant, created_by=request_student, created_at__gte=since).count()
        # 받은 좋아요 (본인 글 + 댓글)
        received_post_likes = PostLike.objects.filter(tenant=tenant, post__created_by=request_student, created_at__gte=since).count()
        received_reply_likes = PostReplyLike.objects.filter(tenant=tenant, reply__created_by=request_student, created_at__gte=since).count()
        received_likes = received_post_likes + received_reply_likes

        # 누적 카운트 (전체 기간) — 배지 산정용 (#52)
        lifetime_post = PostEntity.objects.filter(tenant=tenant, created_by=request_student).count()
        lifetime_reply = PostReply.objects.filter(tenant=tenant, created_by=request_student).count()
        lifetime_received_likes = (
            PostLike.objects.filter(tenant=tenant, post__created_by=request_student).count()
            + PostReplyLike.objects.filter(tenant=tenant, reply__created_by=request_student).count()
        )
        # 배지 규칙 (단순 threshold)
        badges = []
        if lifetime_post >= 1: badges.append({"key": "first_post", "label": "✏️ 첫 글 작성"})
        if lifetime_post >= 10: badges.append({"key": "active_writer", "label": "🔥 활동 작가 (글 10+)"})
        if lifetime_post >= 50: badges.append({"key": "veteran_writer", "label": "📚 베테랑 (글 50+)"})
        if lifetime_reply >= 1: badges.append({"key": "first_reply", "label": "💬 첫 댓글"})
        if lifetime_reply >= 20: badges.append({"key": "helpful_commenter", "label": "🤝 적극 참여 (댓글 20+)"})
        if lifetime_received_likes >= 10: badges.append({"key": "loved", "label": "❤️ 사랑받는 (♥ 10+)"})
        if lifetime_received_likes >= 50: badges.append({"key": "popular", "label": "🌟 인기 (♥ 50+)"})

        # ranking — 본인 score + 본인보다 높은 점수 학생 수
        my_score = post_count + reply_count
        rank, total_active = student_activity_rank(
            tenant=tenant,
            since=since,
            my_score=my_score,
        )
        return Response({
            "is_student": True,
            "days": days,
            "post_count": post_count,
            "reply_count": reply_count,
            "received_likes": received_likes,
            "score": my_score,
            "rank": rank,
            "total_active_students": total_active,
            "lifetime": {
                "post_count": lifetime_post,
                "reply_count": lifetime_reply,
                "received_likes": lifetime_received_likes,
            },
            "badges": badges,
        })

    @action(detail=False, methods=["get"], url_path="counts")
    def counts(self, request):
        """GET /community/posts/counts/?post_type=notice — 트리 카운트 집계.

        프론트가 500건 풀 페치하지 않도록 노드/강의별 카운트만 단일 query로 반환.
        """
        tenant = getattr(request, "tenant", None)
        if not tenant:
            return Response({"detail": "tenant required"}, status=status.HTTP_403_FORBIDDEN)
        post_type = (request.query_params.get("post_type") or "").strip().lower()
        from apps.domains.community.models.post import STUDENT_PUBLIC_POST_TYPES, VALID_POST_TYPES
        if post_type not in VALID_POST_TYPES:
            return Response(
                {"detail": f"허용되지 않는 post_type입니다: {post_type}"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        visible_node_ids = None
        if not self._is_staff_request(request):
            if post_type not in STUDENT_PUBLIC_POST_TYPES:
                return Response({
                    "total": 0,
                    "by_node_id": {},
                    "by_lecture_id": {},
                    "global_count": 0,
                })
            visible_node_ids = self._visible_node_ids_for_request(request)
        data = get_post_counts_by_node(tenant, post_type, visible_node_ids=visible_node_ids)
        return Response(data)

    def create(self, request, *args, **kwargs):
        # 사용자 커뮤니티 차단 check(#49)
        if self._is_user_blocked(request):
            return Response({"detail": "학원 운영진에 의해 커뮤니티 작성이 제한되었습니다.", "code": "user_blocked"}, status=status.HTTP_403_FORBIDDEN)
        # 학부모 write 차단 — 학부모는 읽기 전용
        if getattr(request.user, "parent_profile", None) is not None:
            return Response(
                {"detail": "학부모 계정은 글 작성이 제한됩니다.", "code": "parent_read_only"},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        tenant = getattr(request, "tenant", None)
        request_student = get_request_student(request)
        if not tenant:
            return Response(
                {"detail": "tenant required", "code": "tenant_required"},
                status=status.HTTP_403_FORBIDDEN,
            )
        node_ids = request.data.get("node_ids") or []
        if not isinstance(node_ids, list):
            return Response({"detail": "node_ids must be a list"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            node_ids = [int(node_id) for node_id in node_ids]
        except (TypeError, ValueError):
            return Response({"detail": "node_ids must be integers"}, status=status.HTTP_400_BAD_REQUEST)
        from apps.domains.community.models import ScopeNode
        valid_count = ScopeNode.objects.filter(tenant=tenant, id__in=node_ids).count()
        if valid_count != len(set(node_ids)):
            return Response({"detail": "현재 학원에 속하지 않는 노드가 포함되어 있습니다."}, status=status.HTTP_400_BAD_REQUEST)
        created_by = serializer.validated_data.get("created_by")
        if request_student is not None:
            created_by = request_student
        elif created_by is None and getattr(request.user, "student_profile", None):
            created_by = request.user.student_profile

        # Resolve post_type from request data (block_type FK 제거됨 — post_type SSOT)
        post_type = (request.data.get("post_type") or "").strip().lower()
        from apps.domains.community.models.post import VALID_POST_TYPES
        if post_type not in VALID_POST_TYPES:
            post_type = "board"

        if request_student is not None:
            if post_type not in self.STUDENT_WRITABLE_POST_TYPES:
                return Response(
                    {"detail": "학생 계정은 자유게시판, 질문, 상담 신청만 등록할 수 있습니다."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            if node_ids:
                return Response(
                    {"detail": "학생 계정은 게시 대상을 직접 지정할 수 없습니다."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            admin_only_fields = {"status", "is_urgent", "is_pinned", "published_at"} & set(request.data.keys())
            if admin_only_fields:
                return Response(
                    {"detail": "학생 계정은 운영 필드를 지정할 수 없습니다."},
                    status=status.HTTP_403_FORBIDDEN,
                )

        # QnA는 작성자(created_by) 필수. 프로필 로드 전 제출 시 null 저장 방지.
        if post_type == "qna" and created_by is None:
            return Response(
                {
                    "detail": "프로필을 불러온 후 다시 시도해 주세요.",
                    "code": "profile_required",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        # 작성자 정보 resolve
        author_display_name = None
        author_role = "staff"
        if created_by is not None:
            author_display_name = getattr(created_by, "name", None)
            # 학부모가 자녀 컨텍스트로 작성한 경우 author_role=parent (답변 알림 대상 결정용)
            if getattr(request.user, "parent_profile", None) is not None:
                author_role = "parent"
            else:
                author_role = "student"
        elif request.user and request.user.is_authenticated:
            # 관리자/강사 작성
            staff = getattr(request.user, "staff", None) or getattr(request.user, "staff_profile", None)
            if staff and getattr(staff, "name", None):
                author_display_name = staff.name
            elif getattr(request.user, "first_name", None) or getattr(request.user, "last_name", None):
                author_display_name = f"{request.user.last_name}{request.user.first_name}".strip() or None

        # Sanitize HTML content server-side
        raw_content = serializer.validated_data["content"]
        safe_content = sanitize_html(raw_content) if raw_content else ""

        try:
            is_urgent = parse_bool(request.data.get("is_urgent", False), field_name="is_urgent")
            is_pinned = parse_bool(request.data.get("is_pinned", False), field_name="is_pinned")
        except ValidationError as e:
            return Response(e.detail, status=status.HTTP_400_BAD_REQUEST)
        post_status = str(request.data.get("status", "published") or "published").strip().lower()
        if post_status not in {"draft", "published", "archived"}:
            return Response({"detail": f"허용되지 않는 status: {post_status}"}, status=status.HTTP_400_BAD_REQUEST)
        if request_student is not None:
            is_urgent = False
            is_pinned = False
            post_status = "published"

        data = {
            "post_type": post_type,
            "title": serializer.validated_data["title"],
            "content": safe_content,
            "category_label": request.data.get("category_label"),
            "created_by": created_by,
            "author_display_name": author_display_name,
            "author_role": author_role,
            "is_urgent": is_urgent,
            "is_pinned": is_pinned,
            "status": post_status,
            "published_at": request.data.get("published_at") or None,
        }
        svc = CommunityService(tenant)
        try:
            post = svc.create_post(data, node_ids)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        if post_type == "qna" and author_role in ("student", "parent") and created_by is not None:
            try:
                from apps.domains.community.services.qna_notifications import notify_qna_created

                notify_qna_created(post, actor_user=request.user)
            except Exception as e:
                logger.warning("community qna created notification dispatch failed: post_id=%s err=%s", post.id, e)
        return Response(self.get_serializer(post).data, status=status.HTTP_201_CREATED)

    def perform_update(self, serializer):
        """PATCH /posts/:id/ — sanitize content on update, enforce parent read-only."""
        request = self.request
        # 학부모 write 차단
        if getattr(request.user, "parent_profile", None) is not None:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("학부모 계정은 수정이 제한됩니다.")
        request_student = get_request_student(request)
        if request_student is not None and not self._is_staff_request(request):
            forbidden = set(serializer.validated_data.keys()) - self.STUDENT_UPDATE_FIELDS
            if forbidden:
                from rest_framework.exceptions import PermissionDenied
                raise PermissionDenied("학생 계정은 제목, 내용, 분류만 수정할 수 있습니다.")
        # Content sanitization
        if "content" in serializer.validated_data:
            serializer.validated_data["content"] = sanitize_html(
                serializer.validated_data["content"]
            )
        serializer.save()

    @action(detail=True, methods=["patch"], url_path="nodes")
    def update_nodes(self, request, pk=None):
        """PATCH /posts/:id/nodes/ body: { node_ids: [1,2,3] }"""
        tenant = getattr(request, "tenant", None)
        if not tenant:
            return Response({"detail": "tenant required"}, status=status.HTTP_403_FORBIDDEN)
        if not self._can_manage_post_nodes(request):
            return Response({"detail": "권한이 없습니다."}, status=status.HTTP_403_FORBIDDEN)
        node_ids = request.data.get("node_ids") or []
        if not isinstance(node_ids, list):
            return Response({"detail": "node_ids must be a list"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            node_ids = [int(node_id) for node_id in node_ids]
        except (TypeError, ValueError):
            return Response({"detail": "node_ids must be integers"}, status=status.HTTP_400_BAD_REQUEST)
        from apps.domains.community.models import ScopeNode
        valid_count = ScopeNode.objects.filter(tenant=tenant, id__in=node_ids).count()
        if valid_count != len(set(node_ids)):
            return Response({"detail": "현재 학원에 속하지 않는 노드가 포함되어 있습니다."}, status=status.HTTP_400_BAD_REQUEST)
        svc = CommunityService(tenant)
        svc.update_post_nodes(int(pk), node_ids)
        post = get_post_by_id(tenant, int(pk))
        if not post:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(self.get_serializer(post).data)

    @action(detail=True, methods=["get", "post"], url_path="replies")
    def replies(self, request, pk=None):
        """GET/POST /posts/:id/replies/ — 답변 목록 조회, 답변 등록(선생/관리자)."""
        tenant = getattr(request, "tenant", None)
        if not tenant:
            return Response({"detail": "tenant required"}, status=status.HTTP_403_FORBIDDEN)
        post = get_post_by_id(tenant, int(pk))
        # 가시성 게이트 — 학생이 다른 학생의 QnA/counsel 댓글 조회/작성 차단(2026-05-11 보안 리뷰).
        if not post or not self._post_visible_to_request(request, post):
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        if request.method == "GET":
            # like_count annotation 추가(2026-05-11 latency 최적화 — N+1 제거)
            from django.db.models import Count
            qs = (
                PostReply.objects.filter(post=post, tenant=tenant)
                .select_related("created_by")
                .annotate(
                    like_count_anno=Count("likes", distinct=True),
                    is_liked_anno=Exists(
                        PostReplyLike.objects.filter(
                            tenant=tenant,
                            reply_id=OuterRef("pk"),
                            user_id=request.user.id,
                        )
                    ),
                )
                .order_by("created_at")
            )
            serializer = PostReplySerializer(qs, many=True, context={"request": request})
            return Response(serializer.data)

        # POST: 답변 등록
        if getattr(request.user, "parent_profile", None) is not None:
            return self._parent_read_only_response("댓글 작성")
        # 사용자 커뮤니티 차단 check(#49)
        if self._is_user_blocked(request):
            return Response({"detail": "학원 운영진에 의해 커뮤니티 작성이 제한되었습니다.", "code": "user_blocked"}, status=status.HTTP_403_FORBIDDEN)
        # 자료실은 일방향 다운로드용 — 모든 사용자(staff 포함) 댓글 차단
        # (정책 SSOT: models/post.py:DOWNLOAD_ONLY_POST_TYPES)
        from apps.domains.community.models.post import DOWNLOAD_ONLY_POST_TYPES
        post_type = getattr(post, "post_type", "")
        if post_type in DOWNLOAD_ONLY_POST_TYPES:
            return Response(
                {"detail": "자료실에는 댓글을 등록할 수 없습니다."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # 학생 권한:
        #   - 공지/게시판: 댓글 가능
        #   - QnA/Counsel: 답변은 staff 전용 — 본인 글이라도 self-reply 차단 (B-2)
        request_student = get_request_student(request)
        if request_student is not None:
            from apps.domains.community.models.post import STUDENT_PUBLIC_POST_TYPES
            is_public = post_type in STUDENT_PUBLIC_POST_TYPES
            if not is_public:
                return Response(
                    {"detail": "QnA·상담 신청에는 학생이 답변을 등록할 수 없습니다."},
                    status=status.HTTP_403_FORBIDDEN,
                )

        serializer = PostReplySerializer(
            data=request.data,
            partial=False,
            context={"request": request, "view": self, "post": post},
        )
        serializer.is_valid(raise_exception=True)

        # 답글 nesting (2026-05-11): parent_reply_id 받아서 답글로 저장.
        # 잘못된 ID/타 post의 reply 차단 — 같은 post의 reply만 허용. depth 1까지만 (답글의 답글 X).
        parent_reply = serializer.validated_data.get("parent_reply") if hasattr(serializer, "validated_data") else None
        if parent_reply is not None:
            if parent_reply.post_id != post.id or parent_reply.tenant_id != tenant.id:
                return Response({"detail": "답글 대상이 잘못되었습니다."}, status=status.HTTP_400_BAD_REQUEST)
            # depth 제한: 답글에 답글은 차단 (UI 복잡도 + nesting 폭주 방지)
            if parent_reply.parent_reply_id is not None:
                return Response({"detail": "답글에는 다시 답글을 등록할 수 없습니다."}, status=status.HTTP_400_BAD_REQUEST)

        created_by = None
        author_display_name = None
        author_role = "staff"
        if request_student is not None:
            created_by = request_student
            author_display_name = getattr(request_student, "name", None)
            author_role = "student"
        else:
            # Staff: 이름 resolve
            staff = getattr(request.user, "staff", None) or getattr(request.user, "staff_profile", None)
            if staff and getattr(staff, "name", None):
                author_display_name = staff.name
            elif request.user:
                author_display_name = f"{request.user.last_name}{request.user.first_name}".strip() or None

        # 🔐 XSS 방지: 댓글 content sanitize (2026-05-11 보안 리뷰 H2: 무조건 적용)
        # frontend는 plain text textarea를 보내고 HTML 렌더이므로 빈 문자열도 sanitize 통과 — defense in depth.
        serializer.validated_data["content"] = sanitize_html(serializer.validated_data.get("content") or "")

        reply = serializer.save(
            post=post, tenant=tenant, created_by=created_by,
            author_display_name=author_display_name, author_role=author_role,
            parent_reply=parent_reply,
        )

        # 알림톡: staff가 학생/학부모 글(QnA/상담)에 답변 등록 시 발송.
        # 발송 대상은 글 작성자(author_role)에 따라 분기:
        #   - student 작성: QnA→학생, 상담→학생+학부모
        #   - parent 작성: QnA·상담 모두 학부모만 (학부모가 본인 자격으로 쓴 글)
        suppress_qna_notification = False
        if post.post_type == "qna":
            try:
                from apps.domains.community.services.qna_notifications import should_suppress_qna_notification

                suppress_qna_notification = should_suppress_qna_notification(post)
            except Exception as e:
                logger.warning("community qna notification suppression check failed: post_id=%s err=%s", post.id, e)

        if (
            author_role == "staff"
            and post.post_type in ("qna", "counsel")
            and post.created_by_id
            and not suppress_qna_notification
        ):
            category_fallback = "QnA" if post.post_type == "qna" else "상담"
            ctx = {
                "강의명": (post.category_label or category_fallback),
                "차시명": (post.title or ""),
                "_domain_object_id": f"reply:{reply.id}",
                "_source_domain": "community",
                "_source_use_case": f"community.{post.post_type}_answered",
                "_actor_id": str(getattr(request.user, "id", "") or ""),
            }
            trigger = "qna_answered" if post.post_type == "qna" else "counsel_answered"
            post_author_role = getattr(post, "author_role", "") or "student"
            if post_author_role == "parent":
                send_targets = ("parent",)
            elif post.post_type == "counsel":
                send_targets = ("student", "parent")
            else:  # qna + student
                send_targets = ("student",)

            try:
                send_event_notification = get_reply_event_notifier()
            except Exception as e:
                logger.warning("community reply primary notification unavailable: post_id=%s err=%s", post.id, e)
                send_event_notification = None

            for send_to in send_targets:
                if send_event_notification is not None:
                    try:
                        send_event_notification(
                            tenant=tenant, trigger=trigger,
                            student=post.created_by, send_to=send_to, context=ctx,
                        )
                    except Exception as e:
                        logger.warning(
                            "community reply primary notification failed: post_id=%s trigger=%s send_to=%s err=%s",
                            post.id,
                            trigger,
                            send_to,
                            e,
                        )

        return Response(PostReplySerializer(reply).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="attachments")
    def upload_attachments(self, request, pk=None):
        """POST /posts/:id/attachments/ — 첨부파일 업로드 (multipart)."""
        tenant = _get_tenant_from_request(request)
        if not tenant:
            return Response({"detail": "tenant required"}, status=status.HTTP_403_FORBIDDEN)
        if getattr(request.user, "parent_profile", None) is not None:
            return self._parent_read_only_response("첨부파일 업로드")
        post = get_post_by_id(tenant, int(pk))
        if not post or not self._post_visible_to_request(request, post):
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        # 학생은 본인 글에만 첨부 가능
        request_student = get_request_student(request)
        if request_student is not None and post.created_by_id != request_student.id:
            return Response({"detail": "권한이 없습니다."}, status=status.HTTP_403_FORBIDDEN)

        files = request.FILES.getlist("files")
        if not files:
            return Response({"detail": "파일이 없습니다."}, status=status.HTTP_400_BAD_REQUEST)

        # Step 1: Pre-validate ALL files (size + MIME + extension) before any upload
        for f in files:
            if f.size > MAX_ATTACHMENT_SIZE:
                return Response(
                    {"detail": f"파일 '{f.name}'이(가) 50MB를 초과합니다."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            allowed, reason = is_attachment_allowed(f.name or "", f.content_type or "")
            if not allowed:
                return Response(
                    {"detail": f"파일 '{f.name}': {reason}"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        try:
            supplied_key = normalize_idempotency_key(
                request.data.get("idempotency_key")
            )
        except ValueError:
            return Response(
                {"detail": "첨부 재시도 키 형식이 올바르지 않습니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        upload_key = supplied_key or uuid.uuid4().hex
        batch_prefix = (
            f"tenants/{tenant.id}/community/posts/{post.id}/uploads/{upload_key}/"
        )
        planned_uploads = []
        for index, f in enumerate(files):
            safe_name = sanitize_filename(f.name)
            content_hasher = hashlib.sha256()
            for chunk in f.chunks():
                content_hasher.update(chunk)
            f.seek(0)
            content_hash = content_hasher.hexdigest()
            name_hash = hashlib.sha256(safe_name.encode()).hexdigest()[:8]
            r2_key = (
                f"{batch_prefix}{index}_{content_hash[:16]}_{name_hash}_{safe_name}"
            )
            planned_uploads.append(
                (r2_key, safe_name, f.size, f.content_type, f)
            )

        from apps.infrastructure.storage.r2 import upload_fileobj_to_r2_storage, delete_object_r2_storage

        uploaded = []  # list of (r2_key, original_name, size_bytes, content_type)
        try:
            # Serialize attachment batches per post. This makes a concurrent retry
            # wait until the first transaction can be reused or rolled back.
            with transaction.atomic():
                PostEntity.objects.select_for_update().get(
                    pk=post.pk,
                    tenant=tenant,
                )
                existing_batch = list(
                    PostAttachment.objects.filter(
                        post=post,
                        tenant=tenant,
                        r2_key__startswith=batch_prefix,
                    ).order_by("r2_key")
                )
                if existing_batch:
                    expected = {
                        r2_key: (
                            safe_name,
                            size_bytes,
                            content_type or "application/octet-stream",
                        )
                        for (
                            r2_key,
                            safe_name,
                            size_bytes,
                            content_type,
                            _,
                        ) in planned_uploads
                    }
                    actual = {
                        attachment.r2_key: (
                            attachment.original_name,
                            attachment.size_bytes,
                            attachment.content_type,
                        )
                        for attachment in existing_batch
                    }
                    if actual != expected:
                        return Response(
                            {
                                "detail": (
                                    "같은 첨부 재시도 키가 다른 파일에 "
                                    "사용되었습니다."
                                )
                            },
                            status=status.HTTP_409_CONFLICT,
                        )
                    created = existing_batch
                    response_status = status.HTTP_200_OK
                else:
                    existing_count = PostAttachment.objects.filter(
                        post=post,
                        tenant=tenant,
                    ).count()
                    if existing_count + len(files) > MAX_ATTACHMENTS_PER_POST:
                        return Response(
                            {
                                "detail": (
                                    "첨부파일은 최대 "
                                    f"{MAX_ATTACHMENTS_PER_POST}개까지 가능합니다."
                                )
                            },
                            status=status.HTTP_400_BAD_REQUEST,
                        )

                    created = []
                    for (
                        r2_key,
                        safe_name,
                        file_size,
                        content_type,
                        f,
                    ) in planned_uploads:
                        upload_fileobj_to_r2_storage(
                            fileobj=f,
                            key=r2_key,
                            content_type=(
                                content_type or "application/octet-stream"
                            ),
                        )
                        uploaded.append(
                            (r2_key, safe_name, file_size, content_type)
                        )
                        att = PostAttachment.objects.create(
                            tenant=tenant,
                            post=post,
                            r2_key=r2_key,
                            original_name=safe_name,
                            size_bytes=file_size,
                            content_type=(
                                content_type or "application/octet-stream"
                            ),
                        )
                        created.append(att)
                        logger.info(
                            "PostAttachment created: post=%s, file=%s, key=%s",
                            post.id,
                            safe_name,
                            r2_key,
                        )
                    response_status = status.HTTP_201_CREATED

            if post.post_type == "qna" and response_status == status.HTTP_201_CREATED:
                _dispatch_qna_matchup(post, created, tenant)

            serializer = PostAttachmentSerializer(created, many=True)
            return Response(serializer.data, status=response_status)
        except Exception:
            # Reacquire the same post lock before cleanup. A waiting retry either
            # uploads after cleanup, or commits first and makes us skip its key.
            with transaction.atomic():
                PostEntity.objects.select_for_update().get(
                    pk=post.pk,
                    tenant=tenant,
                )
                for r2_key, *_ in uploaded:
                    if PostAttachment.objects.filter(r2_key=r2_key).exists():
                        continue
                    try:
                        delete_object_r2_storage(key=r2_key)
                    except Exception:
                        logger.warning("R2 orphan cleanup failed: key=%s", r2_key)
            raise

    @action(detail=True, methods=["get"], url_path=r"attachments/(?P<att_id>[^/.]+)/download")
    def download_attachment(self, request, pk=None, att_id=None):
        """GET /posts/:id/attachments/:att_id/download/ — presigned download URL 리다이렉트."""
        tenant = _get_tenant_from_request(request)
        if not tenant:
            return Response({"detail": "tenant required"}, status=status.HTTP_403_FORBIDDEN)
        post = get_post_by_id(tenant, int(pk))
        if not post or not self._post_visible_to_request(request, post):
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        try:
            att = PostAttachment.objects.get(id=int(att_id), post=post, tenant=tenant)
        except (PostAttachment.DoesNotExist, ValueError, TypeError):
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        from apps.domains.community.services.attachment_urls import build_attachment_download_url

        url = build_attachment_download_url(att, expires_in=3600, force_download=True)
        return Response({"url": url, "original_name": att.original_name})

    @action(detail=True, methods=["delete"], url_path=r"attachments/(?P<att_id>[^/.]+)")
    def delete_attachment(self, request, pk=None, att_id=None):
        """DELETE /posts/:id/attachments/:att_id/ — 첨부파일 삭제."""
        tenant = _get_tenant_from_request(request)
        if not tenant:
            return Response({"detail": "tenant required"}, status=status.HTTP_403_FORBIDDEN)
        if getattr(request.user, "parent_profile", None) is not None:
            return self._parent_read_only_response("첨부파일 삭제")
        post = get_post_by_id(tenant, int(pk))
        if not post or not self._post_visible_to_request(request, post):
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        # 학생은 본인 글의 첨부파일만 삭제 가능
        request_student = get_request_student(request)
        if request_student is not None and post.created_by_id != request_student.id:
            return Response({"detail": "권한이 없습니다."}, status=status.HTTP_403_FORBIDDEN)

        try:
            att = PostAttachment.objects.get(id=int(att_id), post=post, tenant=tenant)
        except (PostAttachment.DoesNotExist, ValueError, TypeError):
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        from apps.domains.community.models import support_kind_for_post
        if support_kind_for_post(post):
            return Response(
                {"detail": "지원 문의 첨부파일은 제출 후 삭제할 수 없습니다."},
                status=status.HTTP_403_FORBIDDEN,
            )

        from apps.infrastructure.storage.r2 import delete_object_r2_storage
        try:
            delete_object_r2_storage(key=att.r2_key)
        except Exception:
            logger.warning("R2 delete failed for key=%s, removing DB record anyway", att.r2_key)
        att.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["patch", "delete"], url_path=r"replies/(?P<reply_id>[^/.]+)")
    def reply_detail(self, request, pk=None, reply_id=None):
        """PATCH/DELETE /posts/:id/replies/:reply_id/ — 답변 수정/삭제."""
        tenant = getattr(request, "tenant", None)
        if not tenant:
            return Response({"detail": "tenant required"}, status=status.HTTP_403_FORBIDDEN)
        if getattr(request.user, "parent_profile", None) is not None:
            return self._parent_read_only_response("댓글 수정/삭제")
        post = get_post_by_id(tenant, int(pk))
        # 가시성 게이트 — 학생이 타인 비공개 글 댓글 수정/삭제 차단(2026-05-11 보안 리뷰).
        if not post or not self._post_visible_to_request(request, post):
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        try:
            reply = PostReply.objects.get(post=post, id=int(reply_id), tenant=tenant)
        except (PostReply.DoesNotExist, ValueError, TypeError):
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        from apps.domains.community.models import support_kind_for_post
        if support_kind_for_post(post):
            return Response(
                {"detail": "지원 문의의 대화 기록은 제출 후 수정하거나 삭제할 수 없습니다."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # 학생은 본인 답변만 수정/삭제 가능
        request_student = get_request_student(request)
        if request_student is not None and reply.created_by_id != request_student.id:
            return Response({"detail": "권한이 없습니다."}, status=status.HTTP_403_FORBIDDEN)

        if request.method == "DELETE":
            reply.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        # PATCH
        serializer = PostReplySerializer(
            reply,
            data=request.data,
            partial=True,
            context={"request": request, "view": self, "post": post},
        )
        serializer.is_valid(raise_exception=True)
        # 🔐 XSS 방지: 댓글 수정 시에도 content sanitize (2026-05-11 보안 리뷰 H2: 무조건 적용)
        if "content" in serializer.validated_data:
            serializer.validated_data["content"] = sanitize_html(serializer.validated_data.get("content") or "")
        serializer.save()
        return Response(serializer.data)

    @action(detail=True, methods=["post", "delete"], url_path="like")
    def like(self, request, pk=None):
        """POST/DELETE /posts/:id/like/ — 글 좋아요 토글.

        - 인증 필수(TenantResolvedAndMember). 비로그인 외부인 차단.
        - 가시성 게이트(2026-05-11 보안 리뷰): retrieve와 동일한 visibility 적용 →
          타인 QnA/counsel 같은 비공개 글에 학생이 reaction 우회 차단.
        - unique (post, user): 같은 사용자가 같은 글에 좋아요 1회.
        - POST: 좋아요 생성(이미 있으면 그대로). DELETE: 좋아요 제거.
        - 응답: {liked: bool, count: int}
        """
        tenant = getattr(request, "tenant", None)
        if not tenant:
            return Response({"detail": "tenant required"}, status=status.HTTP_403_FORBIDDEN)
        post = get_post_by_id(tenant, int(pk))
        if not post or not self._post_visible_to_request(request, post):
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        user = request.user
        if not user or not user.is_authenticated:
            return Response({"detail": "authentication required"}, status=status.HTTP_401_UNAUTHORIZED)
        if getattr(request.user, "parent_profile", None) is not None:
            return self._parent_read_only_response("좋아요")
        # 차단 사용자 reaction 차단(#49) — POST에서만 (DELETE는 본인 좋아요 취소 허용)
        if request.method == "POST" and self._is_user_blocked(request):
            return Response({"detail": "학원 운영진에 의해 커뮤니티 활동이 제한되었습니다.", "code": "user_blocked"}, status=status.HTTP_403_FORBIDDEN)

        if request.method == "DELETE":
            PostLike.objects.filter(post=post, user=user, tenant=tenant).delete()
            liked = False
        else:
            # 방어적: tenant까지 lookup에 포함해 cross-tenant 잔존 row가 reuse되지 않도록.
            PostLike.objects.get_or_create(post=post, user=user, tenant=tenant)
            liked = True

        count = PostLike.objects.filter(post=post, tenant=tenant).count()
        return Response({"liked": liked, "count": count})

    @action(detail=True, methods=["post", "delete"], url_path=r"replies/(?P<reply_id>[^/.]+)/like")
    def reply_like(self, request, pk=None, reply_id=None):
        """POST/DELETE /posts/:id/replies/:reply_id/like/ — 댓글 좋아요 토글.

        post visibility 게이트 동일 적용(2026-05-11 보안 리뷰).
        """
        tenant = getattr(request, "tenant", None)
        if not tenant:
            return Response({"detail": "tenant required"}, status=status.HTTP_403_FORBIDDEN)
        post = get_post_by_id(tenant, int(pk))
        if not post or not self._post_visible_to_request(request, post):
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        try:
            reply = PostReply.objects.get(post=post, id=int(reply_id), tenant=tenant)
        except (PostReply.DoesNotExist, ValueError, TypeError):
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        user = request.user
        if not user or not user.is_authenticated:
            return Response({"detail": "authentication required"}, status=status.HTTP_401_UNAUTHORIZED)
        if getattr(request.user, "parent_profile", None) is not None:
            return self._parent_read_only_response("댓글 좋아요")
        # 차단 사용자 reaction 차단(#49) — POST에서만
        if request.method == "POST" and self._is_user_blocked(request):
            return Response({"detail": "학원 운영진에 의해 커뮤니티 활동이 제한되었습니다.", "code": "user_blocked"}, status=status.HTTP_403_FORBIDDEN)

        if request.method == "DELETE":
            PostReplyLike.objects.filter(reply=reply, user=user, tenant=tenant).delete()
            liked = False
        else:
            PostReplyLike.objects.get_or_create(reply=reply, user=user, tenant=tenant)
            liked = True

        count = PostReplyLike.objects.filter(reply=reply, tenant=tenant).count()
        return Response({"liked": liked, "count": count})

    @action(detail=True, methods=["get"], url_path="neighbors")
    def neighbors(self, request, pk=None):
        """GET /posts/:id/neighbors/ — 같은 post_type/published 내 prev/next 글 id+title.

        cafe.naver 스타일 글 상세 하단 "◀ 이전글 / 다음글 ▶". 가시성 게이트 적용.
        ordering: -created_at(default). 학생 시점은 STUDENT_PUBLIC_POST_TYPES만 보임.
        """
        tenant = getattr(request, "tenant", None)
        if not tenant:
            return Response({"detail": "tenant required"}, status=status.HTTP_403_FORBIDDEN)
        post = get_post_by_id(tenant, int(pk))
        if not post or not self._post_visible_to_request(request, post):
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        # 학생 권한 시 STUDENT_PUBLIC_POST_TYPES만, staff는 모든 post_type 동일 그룹.
        from apps.domains.community.models.post import STUDENT_PUBLIC_POST_TYPES
        from apps.domains.community.models import PostEntity, platform_support_q
        from django.db.models import Q as _Q
        siblings = PostEntity.objects.filter(
            tenant=tenant,
            post_type=post.post_type,
            status="published",
        ).exclude(platform_support_q())
        if not self._is_staff_request(request):
            visible_node_ids = self._visible_node_ids_for_request(request)
            siblings = siblings.filter(
                _Q(mappings__isnull=True) | _Q(mappings__node_id__in=visible_node_ids)
            ).distinct()
            # 학생/외부: STUDENT_PUBLIC_POST_TYPES만 그룹화 + 자신 작성 외 비공개 차단
            if post.post_type not in STUDENT_PUBLIC_POST_TYPES:
                # qna/counsel은 본인 작성만 — 본인 글 그룹 내 prev/next
                rs = get_request_student(request)
                if rs is None:
                    return Response({"prev": None, "next": None})
                siblings = siblings.filter(created_by=rs)
            siblings = siblings.filter(_Q(created_by__isnull=True) | _Q(created_by__deleted_at__isnull=True))
        # prev = 더 이전(작은 created_at), next = 더 이후(큰 created_at)
        prev_post = (
            siblings.filter(created_at__lt=post.created_at)
            .order_by("-created_at").values("id", "title").first()
        )
        next_post = (
            siblings.filter(created_at__gt=post.created_at)
            .order_by("created_at").values("id", "title").first()
        )
        return Response({
            "prev": prev_post,
            "next": next_post,
        })

    @action(detail=True, methods=["post"], url_path="report")
    def report_post(self, request, pk=None):
        """POST /posts/:id/report/ — 글 신고. body: {reason, detail?}. unique(post, user)."""
        tenant = getattr(request, "tenant", None)
        if not tenant:
            return Response({"detail": "tenant required"}, status=status.HTTP_403_FORBIDDEN)
        post = get_post_by_id(tenant, int(pk))
        if not post or not self._post_visible_to_request(request, post):
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        user = request.user
        if not user or not user.is_authenticated:
            return Response({"detail": "authentication required"}, status=status.HTTP_401_UNAUTHORIZED)
        if getattr(request.user, "parent_profile", None) is not None:
            return self._parent_read_only_response("신고")
        reason = (request.data.get("reason") or CommunityReport.REASON_OTHER).strip()[:20]
        if reason not in dict(CommunityReport.REASON_CHOICES):
            reason = CommunityReport.REASON_OTHER
        detail = (request.data.get("detail") or "").strip()[:1000]
        _, created = CommunityReport.objects.get_or_create(
            tenant=tenant,
            target_type=CommunityReport.TARGET_POST,
            target_id=post.id,
            reporter=user,
            defaults={"reason": reason, "detail": detail},
        )
        return Response({"reported": True, "duplicate": not created})

    @action(detail=True, methods=["post"], url_path=r"replies/(?P<reply_id>[^/.]+)/report")
    def report_reply(self, request, pk=None, reply_id=None):
        """POST /posts/:id/replies/:reply_id/report/ — 댓글 신고."""
        tenant = getattr(request, "tenant", None)
        if not tenant:
            return Response({"detail": "tenant required"}, status=status.HTTP_403_FORBIDDEN)
        post = get_post_by_id(tenant, int(pk))
        if not post or not self._post_visible_to_request(request, post):
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        try:
            reply = PostReply.objects.get(post=post, id=int(reply_id), tenant=tenant)
        except (PostReply.DoesNotExist, ValueError, TypeError):
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        user = request.user
        if not user or not user.is_authenticated:
            return Response({"detail": "authentication required"}, status=status.HTTP_401_UNAUTHORIZED)
        if getattr(request.user, "parent_profile", None) is not None:
            return self._parent_read_only_response("댓글 신고")
        reason = (request.data.get("reason") or CommunityReport.REASON_OTHER).strip()[:20]
        if reason not in dict(CommunityReport.REASON_CHOICES):
            reason = CommunityReport.REASON_OTHER
        detail = (request.data.get("detail") or "").strip()[:1000]
        _, created = CommunityReport.objects.get_or_create(
            tenant=tenant,
            target_type=CommunityReport.TARGET_REPLY,
            target_id=reply.id,
            reporter=user,
            defaults={"reason": reason, "detail": detail},
        )
        return Response({"reported": True, "duplicate": not created})


def _dispatch_qna_matchup(post, attachments, tenant):
    """Q&A 이미지 첨부 시 자동 매치업 검색 디스패치."""
    image_atts = [a for a in attachments if (a.content_type or "").startswith("image/")]
    if not image_atts:
        return

    att = image_atts[0]  # 첫 번째 이미지만 검색

    try:
        result = dispatch_qna_matchup_search(
            post=post,
            attachment=att,
            tenant=tenant,
        )
        logger.info(
            "QNA_MATCHUP_DISPATCHED | post_id=%s | att_id=%s | ok=%s",
            post.id, att.id, result.get("ok") if isinstance(result, dict) else True,
        )
    except Exception:
        logger.warning("QNA_MATCHUP_DISPATCH_FAILED | post_id=%s", post.id, exc_info=True)
