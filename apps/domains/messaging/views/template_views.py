"""메시지 문구 CRUD와 폐기된 공급사 템플릿 경계."""

import re

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from django.utils import timezone

from apps.core.permissions import TenantResolvedAndStaff
from apps.domains.messaging.models import MessageTemplate
from apps.domains.messaging.serializers import MessageTemplateSerializer


COPY_PREFIX = "복사 - "
COPY_PREFIX_RE = re.compile(r"^(?:복사\s*-\s*)+")
LEGACY_COPY_SUFFIX_RE = re.compile(r"(?:\s*\(복사본\))+$")
MAX_TEMPLATE_NAME_LENGTH = 120


def build_duplicate_template_name(source_name: str) -> str:
    base = COPY_PREFIX_RE.sub("", (source_name or "").strip())
    base = LEGACY_COPY_SUFFIX_RE.sub("", base).strip() or "새 문구"
    return f"{COPY_PREFIX}{base[:MAX_TEMPLATE_NAME_LENGTH - len(COPY_PREFIX)]}"


class MessageTemplateListCreateView(APIView):
    """GET: 문구 목록. POST: 사용자 문구 생성."""

    permission_classes = [IsAuthenticated, TenantResolvedAndStaff]

    def get(self, request):
        queryset = MessageTemplate.objects.filter(
            tenant=request.tenant,
            retired_at__isnull=True,
        ).order_by("-updated_at")
        category = (request.query_params.get("category") or "").strip().lower()
        valid_categories = {choice.value for choice in MessageTemplate.Category}
        if category and category in valid_categories:
            queryset = queryset.filter(category=category)
        return Response(MessageTemplateSerializer(queryset, many=True).data)

    def post(self, request):
        serializer = MessageTemplateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(tenant=request.tenant, is_system=False)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class MessageTemplateDetailView(APIView):
    """GET/PATCH/DELETE: 단일 문구. 시스템 문구는 읽기 전용."""

    permission_classes = [IsAuthenticated, TenantResolvedAndStaff]

    @staticmethod
    def _get_template(request, pk):
        return MessageTemplate.objects.filter(
            tenant=request.tenant,
            pk=pk,
            retired_at__isnull=True,
        ).first()

    def get(self, request, pk):
        template = self._get_template(request, pk)
        if not template:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(MessageTemplateSerializer(template).data)

    def patch(self, request, pk):
        template = self._get_template(request, pk)
        if not template:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        if template.is_system:
            return Response(
                {"detail": "시스템 기본 문구는 수정할 수 없습니다. '복제' 후 수정해 주세요."},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = MessageTemplateSerializer(template, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        data = serializer.data
        used_variables = set(re.findall(r"#\{([^}]+)\}", template.body or ""))
        known_variables = {
            "학원이름", "학원명", "학생이름", "학생이름2", "학생이름3",
            "사이트링크", "강의명", "차시명", "날짜", "시간", "장소",
            "클리닉장소", "클리닉날짜", "클리닉시간", "클리닉명",
            "클리닉기존일정", "클리닉변동사항", "클리닉수정자",
            "강의날짜", "강의시간", "시험명", "과제명", "성적", "시험성적",
            "클리닉합불", "납부금액", "청구월", "반이름", "공지내용", "내용",
            "선생님메모", "학생아이디", "학생비밀번호", "학부모아이디",
            "학부모비밀번호", "비밀번호안내", "인증번호",
        }
        unknown_variables = used_variables - known_variables
        if unknown_variables:
            data["warnings"] = [
                f"인식할 수 없는 변수: #{{{variable}}} — 발송 시 빈 값으로 대체됩니다."
                for variable in sorted(unknown_variables)
            ]
        return Response(data)

    def delete(self, request, pk):
        template = self._get_template(request, pk)
        if not template:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        if template.is_system:
            return Response(
                {"detail": "시스템 기본 문구는 삭제할 수 없습니다."},
                status=status.HTTP_403_FORBIDDEN,
            )
        template.is_user_default = False
        template.retired_at = timezone.now()
        template.save(update_fields=["is_user_default", "retired_at", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class MessageTemplateSetDefaultView(APIView):
    """POST: tenant+category당 사용자 기본 문구 하나를 토글한다."""

    permission_classes = [IsAuthenticated, TenantResolvedAndStaff]

    def post(self, request, pk):
        template = MessageTemplate.objects.filter(
            tenant=request.tenant,
            pk=pk,
            retired_at__isnull=True,
        ).first()
        if not template:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        MessageTemplate.objects.filter(
            tenant=request.tenant,
            category=template.category,
            is_user_default=True,
        ).exclude(pk=pk).update(is_user_default=False)
        template.is_user_default = not template.is_user_default
        template.save(update_fields=["is_user_default"])
        return Response(MessageTemplateSerializer(template).data)


class MessageTemplateDuplicateView(APIView):
    """POST: 기존 문구를 사용자 문구로 복제한다."""

    permission_classes = [IsAuthenticated, TenantResolvedAndStaff]

    def post(self, request, pk):
        source = MessageTemplate.objects.filter(
            tenant=request.tenant,
            pk=pk,
            retired_at__isnull=True,
        ).first()
        if not source:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        name = (request.data.get("name") or "").strip() or build_duplicate_template_name(
            source.name
        )
        serializer = MessageTemplateSerializer(
            data={
                "category": source.category,
                "name": name,
                "subject": source.subject,
                "body": source.body,
            }
        )
        serializer.is_valid(raise_exception=True)
        serializer.save(
            tenant=request.tenant,
            is_system=False,
            is_user_default=False,
        )
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class MessageTemplateSubmitReviewView(APIView):
    """폐기된 테넌트별 카카오 템플릿 생성 API."""

    permission_classes = [IsAuthenticated, TenantResolvedAndStaff]

    def post(self, request, pk):
        return Response(
            {
                "detail": (
                    "새 카카오 템플릿 검수는 지원하지 않습니다. "
                    "저장한 문구를 기존 승인 알림톡 봉투에 넣어 발송해 주세요."
                ),
                "code": "new_kakao_template_disabled",
            },
            status=status.HTTP_410_GONE,
        )


class SolapiSyncTemplatesView(APIView):
    """폐기된 테넌트 공급사 템플릿 동기화 API."""

    permission_classes = [IsAuthenticated, TenantResolvedAndStaff]

    def post(self, request):
        return Response(
            {
                "detail": "공급사 템플릿 동기화는 운영 배포 절차에서만 수행합니다.",
                "code": "provider_template_sync_disabled",
            },
            status=status.HTTP_410_GONE,
        )
