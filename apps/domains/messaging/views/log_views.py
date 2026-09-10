# apps/support/messaging/views/log_views.py
"""
발송 로그 뷰
"""

import re

from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from drf_spectacular.utils import OpenApiParameter, OpenApiTypes, extend_schema
from apps.core.permissions import TenantResolvedAndStaff
from apps.api.common.query_params import parse_query_int
from apps.core.services.tenant_access import STAFF_ROLES, get_authorized_tenant_role
from apps.domains.messaging.models import NotificationLog
from apps.domains.messaging.policy import CLINIC_NOTIFICATION_TRIGGERS
from apps.domains.messaging.provider_delivery import get_provider_delivery_status
from apps.domains.messaging.security import (
    SENSITIVE_MESSAGE_PLACEHOLDER,
    sanitize_notification_target_id,
)
from apps.domains.messaging.selectors import notification_logs_for_business_tenant


_PRIVILEGED_LOG_ROLES = frozenset(STAFF_ROLES)


def _alimtalk_logs_for_business_tenant(tenant):
    return notification_logs_for_business_tenant(tenant).filter(message_mode__in=("alimtalk", ""))


def _masked_provider_reference(value: str) -> str:
    """Return UI evidence without disclosing the exact provider identifier."""

    provider_id = str(value or "").strip()
    if not provider_id:
        return ""
    if len(provider_id) <= 6:
        return "확인됨"
    return f"•••• {provider_id[-6:]}"


def _body_visibility(log: NotificationLog, *, privileged: bool) -> str:
    body = str(log.message_body or "")
    if not body:
        return "not_recorded"
    if body == SENSITIVE_MESSAGE_PLACEHOLDER:
        return "sensitive_redacted"
    return "available" if privileged else "restricted"


def _safe_failure_projection(log: NotificationLog) -> tuple[str, str]:
    """Classify a stored provider reason without returning provider/PII text."""

    raw = str(log.failure_reason or "").strip().lower()
    status_value = str(log.status or "").strip().lower()
    if status_value == "ambiguous":
        return (
            "provider_unconfirmed",
            "공급사 접수 결과를 자동 확인하지 못했습니다. 관리자 확인이 필요합니다.",
        )
    if not raw:
        return "", ""
    if any(marker in raw for marker in ("notenoughbalance", "insufficient_balance", "balance")):
        return "insufficient_balance", "알림톡 잔액이 부족해 발송하지 못했습니다."
    if any(marker in raw for marker in ("recipient_blocked", "denylist", "recipient_not_allowed")):
        return "recipient_blocked", "운영 정책에 따라 이 수신자에게 발송하지 않았습니다."
    if "template" in raw and any(marker in raw for marker in ("missing", "unapproved", "not found", "absent")):
        return "template_unavailable", "승인된 알림톡 양식을 확인하지 못해 발송하지 않았습니다."
    if any(marker in raw for marker in ("sms_disabled", "non_alimtalk", "channel_policy")):
        return "policy_blocked", "알림톡 전용 발송 정책에 맞지 않아 발송하지 않았습니다."
    if "tenant" in raw and any(marker in raw for marker in ("disabled", "hold", "inactive")):
        return "messaging_disabled", "이 학원의 알림톡 발송이 중지되어 있습니다."
    if any(marker in raw for marker in ("timeout", "unknown", "ambiguous")):
        return (
            "provider_unconfirmed",
            "공급사 접수 결과를 자동 확인하지 못했습니다. 관리자 확인이 필요합니다.",
        )
    if any(marker in raw for marker in ("network", "temporary", "retry")):
        return "temporary_failure", "일시적인 연결 문제로 발송을 완료하지 못했습니다."
    return "failed", "알림톡 발송을 완료하지 못했습니다. 관리자에게 문의해 주세요."


def _project_log(
    log: NotificationLog,
    *,
    privileged: bool,
    include_body: bool,
) -> dict[str, object]:
    body_visibility = _body_visibility(log, privileged=privileged)
    stored_body = str(log.message_body or "")
    visible_body = stored_body if include_body and body_visibility in {"available", "sensitive_redacted"} else ""
    provider_id = str(log.provider_message_id or "")
    failure_code, failure_summary = _safe_failure_projection(log)
    delivery_status = (
        "provider_accepted"
        if provider_id and (log.status == "sent" or log.success)
        else "unavailable"
    )
    return {
        "id": log.id,
        "sent_at": log.sent_at,
        "success": log.success,
        "status": log.status or ("sent" if log.success else "failed"),
        "claimed_at": log.claimed_at,
        "amount_deducted": log.amount_deducted,
        "recipient_summary": log.recipient_summary or "",
        "template_summary": log.template_summary or "",
        "provider_message_id": provider_id if privileged else "",
        "provider_evidence": bool(provider_id),
        "provider_message_reference": _masked_provider_reference(provider_id),
        "provider_delivery_status": delivery_status,
        "provider_status_code": "",
        "provider_delivery_checked_at": None,
        "provider_delivery_updated_at": None,
        "provider_delivery_failure_reason": "",
        "failure_code": failure_code,
        "failure_reason": failure_summary,
        "body_visibility": body_visibility,
        "message_body_included": bool(visible_body),
        "message_body": visible_body,
        "message_mode": log.message_mode or "",
        "notification_type": log.notification_type or "",
        "origin_id": log.origin_id or "",
        "source_tenant_id": log.source_tenant_id,
        "target_type": log.target_type or "",
        "target_id": sanitize_notification_target_id(log.target_id),
        "target_name": log.target_name or "",
    }


class NotificationLogListView(APIView):
    """GET: 발송 로그 목록 (페이지네이션)"""

    permission_classes = [IsAuthenticated, TenantResolvedAndStaff]

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "origin_id_prefix",
                OpenApiTypes.STR,
                OpenApiParameter.QUERY,
                description="PII가 없는 원천 식별자 접두사. 클리닉 참가자별 발송 이력 조회에 사용합니다.",
            ),
        ]
    )
    def get(self, request):
        page = parse_query_int(request.query_params, "page", default=1, min_value=1)
        page_size = min(
            parse_query_int(request.query_params, "page_size", default=20, min_value=1),
            50,
        )
        offset = (page - 1) * page_size
        # status 필터: success / failure / all (기본 all)
        status_filter = (request.query_params.get("status") or "").strip().lower()
        base_qs = _alimtalk_logs_for_business_tenant(request.tenant)
        origin_id_prefix = (request.query_params.get("origin_id_prefix") or "").strip()
        if origin_id_prefix:
            if len(origin_id_prefix) > 128 or not re.fullmatch(
                r"[A-Za-z0-9:_-]+",
                origin_id_prefix,
            ):
                return Response(
                    {"origin_id_prefix": "유효한 원천 식별자 접두사를 입력해 주세요."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            base_qs = base_qs.filter(origin_id__startswith=origin_id_prefix)
        if (request.query_params.get("scope") or "").strip().lower() == "clinic":
            base_qs = base_qs.filter(notification_type__in=CLINIC_NOTIFICATION_TRIGGERS)
        if status_filter == "success":
            base_qs = base_qs.filter(success=True)
        elif status_filter == "failure":
            base_qs = base_qs.filter(success=False).exclude(
                status__in=["processing", "sending", "retryable_failed", "ambiguous"]
            )
        elif status_filter == "active":
            base_qs = base_qs.filter(status__in=["processing", "sending", "retryable_failed"])
        elif status_filter == "attention":
            base_qs = base_qs.filter(status="ambiguous")
        elif status_filter in {choice[0] for choice in NotificationLog._meta.get_field("status").choices}:
            base_qs = base_qs.filter(status=status_filter)
        qs = base_qs.order_by("-sent_at")[offset : offset + page_size]
        count = base_qs.count()
        role = get_authorized_tenant_role(request.user, request.tenant)
        privileged = role in _PRIVILEGED_LOG_ROLES
        items = [_project_log(r, privileged=privileged, include_body=False) for r in qs]
        return Response({"results": items, "count": count})


class NotificationLogDetailView(APIView):
    """GET: 발송 로그 단건 상세"""

    permission_classes = [IsAuthenticated, TenantResolvedAndStaff]

    def get(self, request, pk):
        log = _alimtalk_logs_for_business_tenant(request.tenant).filter(pk=pk).first()
        if not log:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        role = get_authorized_tenant_role(request.user, request.tenant)
        item = _project_log(
            log,
            privileged=role in _PRIVILEGED_LOG_ROLES,
            include_body=True,
        )
        if (request.query_params.get("verify_provider") or "").strip().lower() in {
            "1",
            "true",
            "yes",
        }:
            provider = get_provider_delivery_status(log)
            item.update(
                provider_delivery_status=provider["status"],
                provider_status_code=provider["status_code"],
                provider_delivery_checked_at=provider["checked_at"],
                provider_delivery_updated_at=provider["updated_at"],
                provider_delivery_failure_reason=provider["failure_reason"],
            )
        return Response(item)
