from __future__ import annotations

import logging

from django.conf import settings
from django.db import DatabaseError
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.api.common.query_params import parse_query_int
from apps.core.permissions import IsPlatformAdmin
from apps.core.product_analytics.constants import MAX_BATCH_BYTES, SURFACES
from apps.core.product_analytics.queries import build_overview
from apps.core.product_analytics.serializers import ProductUsageBatchSerializer
from apps.core.product_analytics.services import (
    active_membership,
    analytics_enabled,
    request_is_impersonated,
    store_events,
)

logger = logging.getLogger(__name__)


class ProductUsageBatchView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        tenant = getattr(request, "tenant", None)
        if tenant is None:
            return Response(
                {"code": "tenant_required", "detail": "학원 정보를 확인할 수 없습니다."},
                status=status.HTTP_403_FORBIDDEN,
            )

        membership = active_membership(tenant=tenant, user=request.user)
        if membership is None:
            return Response(
                {"code": "membership_required", "detail": "활성 멤버십이 필요합니다."},
                status=status.HTTP_403_FORBIDDEN,
            )

        content_length = request.META.get("CONTENT_LENGTH")
        try:
            declared_length = int(content_length or 0)
        except (TypeError, ValueError):
            declared_length = 0
        if declared_length > MAX_BATCH_BYTES:
            return Response(
                {"code": "payload_too_large", "detail": "요청 본문이 너무 큽니다."},
                status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
        if len(request._request.body) > MAX_BATCH_BYTES:
            return Response(
                {"code": "payload_too_large", "detail": "요청 본문이 너무 큽니다."},
                status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )

        if not analytics_enabled(tenant):
            return Response(
                {"accepted": 0, "duplicates": 0, "ignored": "feature_disabled"},
                status=status.HTTP_202_ACCEPTED,
            )

        if not getattr(settings, "PRODUCT_ANALYTICS_HASH_KEY", ""):
            logger.error(
                "product analytics disabled: hash key missing tenant_id=%s",
                tenant.id,
            )
            return Response(
                {
                    "code": "analytics_unavailable",
                    "detail": "사용 분석을 일시적으로 저장할 수 없습니다.",
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        serializer = ProductUsageBatchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            accepted, duplicates = store_events(
                tenant=tenant,
                user=request.user,
                role=membership.role,
                events=serializer.validated_data["events"],
                is_impersonated=request_is_impersonated(request),
            )
        except DatabaseError:
            logger.exception(
                "product analytics batch persistence failed tenant_id=%s events=%s",
                tenant.id,
                len(serializer.validated_data["events"]),
            )
            return Response(
                {
                    "code": "analytics_unavailable",
                    "detail": "사용 분석을 일시적으로 저장할 수 없습니다.",
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(
            {"accepted": accepted, "duplicates": duplicates},
            status=status.HTTP_202_ACCEPTED,
        )


class ProductUsageOverviewView(APIView):
    permission_classes = [IsAuthenticated, IsPlatformAdmin]

    def get(self, request):
        days = parse_query_int(request.query_params, "days", default=28)
        if days not in (7, 28, 90):
            return Response(
                {"detail": "days는 7, 28, 90 중 하나여야 합니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        tenant_id = request.query_params.get("tenant_id")
        if tenant_id:
            try:
                tenant_id = int(tenant_id)
            except (TypeError, ValueError):
                return Response(
                    {"detail": "tenant_id가 올바르지 않습니다."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        else:
            tenant_id = None

        valid_roles = {"owner", "admin", "teacher", "staff", "student", "parent"}
        role = (request.query_params.get("role") or "").strip()
        surface = (request.query_params.get("surface") or "").strip()
        if role and role not in valid_roles:
            return Response(
                {"detail": "role이 올바르지 않습니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if surface and surface not in SURFACES:
            return Response(
                {"detail": "surface가 올바르지 않습니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        payload = build_overview(
            days=days,
            tenant_id=tenant_id,
            role=role,
            surface=surface,
        )
        logger.info(
            "product_analytics.overview_read days=%s tenant_filter=%s role=%s surface=%s",
            days,
            "selected" if tenant_id is not None else "all",
            role or "all",
            surface or "all",
        )
        return Response(payload)
