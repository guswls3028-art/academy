from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import TenantResolvedAndStaff
from apps.domains.messaging.permissions import can_send_messages
from apps.domains.messaging.serializers import SendMessageRequestSerializer
from apps.domains.messaging.services.preflight import (
    build_messaging_operations_status,
    build_send_preflight,
)


class SendMessagePreflightView(APIView):
    """POST: 수동/예약 알림톡 발송 전 수신자·템플릿·한도 검수."""

    permission_classes = [IsAuthenticated, TenantResolvedAndStaff]

    def post(self, request):
        tenant = request.tenant
        if not can_send_messages(request, tenant):
            return Response(
                {"detail": "알림톡 발송 권한이 없습니다. 관리자·강사·조교 권한이 필요합니다."},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = SendMessageRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(
            build_send_preflight(
                tenant,
                serializer.validated_data,
                actor_id=request.user.pk,
            )
        )


class MessagingOperationsStatusView(APIView):
    """GET: 메시징 운영 상태(워커/예약/로그/템플릿/자동발송)를 한 번에 조회."""

    permission_classes = [IsAuthenticated, TenantResolvedAndStaff]

    def get(self, request):
        return Response(build_messaging_operations_status(request.tenant))
