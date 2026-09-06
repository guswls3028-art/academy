# apps/support/messaging/views/info_views.py
"""
메시징 정보/설정 관련 뷰 — 잔액, 충전, 채널 확인, 발신번호 인증, 연동 테스트
"""

from rest_framework import status
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from drf_spectacular.utils import extend_schema

from apps.core.permissions import TenantResolvedAndStaff
from apps.domains.messaging.models import MessageTemplate
from apps.domains.messaging.serializers import (
    MessagingActivationSerializer,
    MessagingInfoSerializer,
)
from apps.domains.messaging.permissions import can_manage_messaging_settings
from apps.domains.messaging.policy import (
    get_messaging_disabled_reason,
    get_owner_tenant_id,
    is_messaging_disabled,
    is_messaging_ops_held,
    resolve_kakao_channel,
)
from apps.core.services.ops_audit import record_audit
from apps.domains.messaging.tenant_channels import get_tenant_channel_status


class MessagingInfoView(APIView):
    """공용 알림톡 상태와 학원별 전체 사용 설정."""
    permission_classes = [IsAuthenticated, TenantResolvedAndStaff]

    @staticmethod
    def _response_data(request, tenant):
        serializer = MessagingInfoSerializer(tenant)
        data = dict(serializer.data)
        # 과거 테넌트별 연동 값은 데이터 보존용일 뿐 제품 발송 계약이 아니다.
        # API에는 새 검증 채널 계약만 노출해 오래된 클라이언트가 보존 데이터를
        # 실행 가능한 설정으로 오인하지 않게 한다.
        data.update({
            "kakao_pfid": "",
            "messaging_sender": "",
            "messaging_provider": "solapi",
            "own_solapi_api_key": "",
            "own_solapi_api_secret": "",
            "own_ppurio_api_key": "",
            "own_ppurio_account": "",
            "has_own_credentials": False,
            "delivery_policy": "verified_tenant_or_common_alimtalk",
        })
        # 정책 SSOT 기반: 과거 Tenant 필드는 무시하고 새 검증 바인딩만 투영한다.
        channel = resolve_kakao_channel(tenant.id)
        channel_status = get_tenant_channel_status(tenant.id)
        data.update(channel_status)
        resolved_pf_id = (channel.get("pf_id") or "").strip()
        data["resolved_pf_id"] = ""
        messaging_disabled = is_messaging_disabled(tenant.id)
        data["tenant_messaging_enabled"] = bool(tenant.messaging_is_active)
        data["messaging_ops_hold"] = is_messaging_ops_held(tenant.id)
        data["can_manage_messaging"] = can_manage_messaging_settings(request, tenant)
        data["messaging_disabled"] = messaging_disabled
        data["messaging_disabled_reason"] = get_messaging_disabled_reason(tenant.id)
        from apps.domains.messaging.alimtalk_content_builders import (
            TEMPLATE_TYPE_TO_SOLAPI_ID,
        )

        has_registered_unified_envelope = any(
            bool((template_id or "").strip())
            for template_id in TEMPLATE_TYPE_TO_SOLAPI_ID.values()
        )
        # 수동 발송이 실제 사용하는 통합 봉투가 하나라도 등록돼야 한다.
        common_available = bool(
            not messaging_disabled
            and resolved_pf_id
            and has_registered_unified_envelope
        )
        data["alimtalk_available"] = bool(
            common_available
            and not channel_status.get("custom_channel_routing_blocked", False)
        )
        return data

    @extend_schema(responses=MessagingInfoSerializer)
    def get(self, request):
        return Response(self._response_data(request, request.tenant))

    @extend_schema(
        request=MessagingActivationSerializer,
        responses=MessagingInfoSerializer,
    )
    def patch(self, request):
        tenant = request.tenant
        if not can_manage_messaging_settings(request, tenant):
            return Response(
                {"detail": "알림톡 전체 사용 설정은 대표 또는 관리자만 변경할 수 있습니다."},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = MessagingActivationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        enabled = serializer.validated_data["tenant_messaging_enabled"]
        previous = bool(tenant.messaging_is_active)
        if previous != enabled:
            tenant.messaging_is_active = enabled
            tenant.save(update_fields=["messaging_is_active"])
            record_audit(
                request,
                action="messaging.tenant_activation",
                target_tenant=tenant,
                summary=f"tenant messaging {'enabled' if enabled else 'disabled'}",
                payload={"previous": previous, "enabled": enabled},
            )
        return Response(self._response_data(request, tenant))

class ChannelCheckView(APIView):
    """GET: effective tenant channel readiness."""
    permission_classes = [IsAuthenticated, TenantResolvedAndStaff]

    def get(self, request):
        channel_status = get_tenant_channel_status(request.tenant.id)
        if channel_status["custom_channel_registered"]:
            active = channel_status["custom_channel_status"] == "active"
            suspended = channel_status["custom_channel_status"] == "suspended"
            return Response({
                "shared": active,
                "message": (
                    "우리 학원 카카오 채널이 연결되어 있습니다."
                    if active
                    else (
                        "우리 학원 카카오 채널 발송을 중지하고 양식 상태를 확인하고 있습니다."
                        if suspended
                        else "우리 학원 카카오 채널의 알림톡 양식을 검수 중입니다."
                    )
                ),
            })
        channel = resolve_kakao_channel(request.tenant.id)
        if not (channel.get("pf_id") or "").strip():
            return Response({"shared": False, "message": "공용 알림톡 채널 미연동"})
        return Response({"shared": True, "message": "공용 알림톡 채널 연동됨"})


class TestCredentialsView(APIView):
    """POST: 공용 솔라피 알림톡 연동 상태를 검증한다."""
    permission_classes = [IsAuthenticated, TenantResolvedAndStaff]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "messaging_diagnostic"

    def post(self, request):
        import logging as _logging
        _logger = _logging.getLogger(__name__)
        from django.conf import settings

        tenant = request.tenant
        if not can_manage_messaging_settings(request, tenant):
            return Response(
                {"detail": "알림톡 연동 진단은 대표 또는 관리자만 실행할 수 있습니다."},
                status=status.HTTP_403_FORBIDDEN,
            )
        provider = "solapi"

        results = {"provider": provider, "checks": []}

        messaging_disabled = is_messaging_disabled(tenant.id)
        results["messaging_disabled"] = messaging_disabled
        results["messaging_disabled_reason"] = get_messaging_disabled_reason(tenant.id)
        results["checks"].append({
            "test": "operational_policy",
            "ok": not messaging_disabled,
            "message": (
                results["messaging_disabled_reason"]
                if messaging_disabled
                else "이 학원의 알림톡 발송 정책이 활성 상태입니다."
            ),
        })

        if provider == "solapi":
            # Solapi: 공용 시스템 키만 실발송에 사용
            api_key = getattr(settings, "SOLAPI_API_KEY", None) or ""
            api_secret = getattr(settings, "SOLAPI_API_SECRET", None) or ""
            key_source = "common_owner"

            if not api_key or not api_secret:
                results["checks"].append({
                    "test": "api_credentials",
                    "ok": False,
                    "message": "공용 솔라피 API 키가 설정되지 않았습니다. 운영자에게 문의하세요.",
                })
            else:
                # 발신번호 목록 조회로 인증 테스트
                try:
                    from apps.domains.messaging.solapi_sender_client import get_active_sender_numbers
                    numbers = get_active_sender_numbers(api_key, api_secret)
                    results["checks"].append({
                        "test": "api_credentials",
                        "ok": True,
                        "message": "공용 솔라피 API와 발신번호가 정상 연결되어 있습니다.",
                    })
                except ValueError:
                    results["checks"].append({
                        "test": "api_credentials",
                        "ok": False,
                        "message": "공용 솔라피 인증을 확인하지 못했습니다. 운영자에게 문의하세요.",
                    })
                except Exception:
                    _logger.exception("test_credentials solapi error")
                    results["checks"].append({
                        "test": "api_credentials",
                        "ok": False,
                        "message": "공용 솔라피 연결을 확인하지 못했습니다. 잠시 후 다시 시도해 주세요.",
                    })

            # 발신번호 확인
            sender = getattr(settings, "SOLAPI_SENDER", "") or ""
            if sender:
                results["checks"].append({
                    "test": "sender_number",
                    "ok": True,
                    "message": "공용 발신번호가 등록되어 있습니다.",
                })
            else:
                results["checks"].append({
                    "test": "sender_number",
                    "ok": False,
                    "message": "공용 알림톡 발신번호가 등록되지 않았습니다.",
                })

        # 공통: 새 tenant binding이 있으면 이를 우선 표시하되, legacy 필드는 무시한다.
        channel_status = get_tenant_channel_status(tenant.id)
        if channel_status["custom_channel_registered"]:
            channel_ready = channel_status["custom_channel_status"] == "active"
            channel_suspended = (
                channel_status["custom_channel_status"] == "suspended"
            )
            results["checks"].append({
                "test": "alimtalk_channel",
                "ok": channel_ready,
                "message": (
                    "우리 학원 카카오 채널이 연결되어 있습니다."
                    if channel_ready
                    else (
                        "우리 학원 카카오 채널 발송을 중지하고 양식 상태를 확인하고 있습니다."
                        if channel_suspended
                        else "우리 학원 카카오 채널은 확인됐고 알림톡 양식을 검수 중입니다."
                    )
                ),
            })
        else:
            channel = resolve_kakao_channel(tenant.id)
            pf_id = (channel.get("pf_id") or "").strip()
            results["checks"].append({
                "test": "alimtalk_channel",
                "ok": bool(pf_id),
                "message": (
                    "공용 알림톡 채널 연동됨"
                    if pf_id
                    else "공용 알림톡 채널이 미연동입니다."
                ),
            })

        # 승인된 템플릿 수
        approved_count = (
            channel_status["custom_channel_approved_templates"]
            if channel_status["custom_channel_registered"]
            else MessageTemplate.objects.filter(
                tenant_id=get_owner_tenant_id(), solapi_status="APPROVED"
            ).count()
        )
        required_count = channel_status["custom_channel_required_templates"]
        results["checks"].append({
            "test": "approved_templates",
            "ok": (
                approved_count >= required_count
                if channel_status["custom_channel_registered"]
                else approved_count > 0
            ),
            "message": (
                f"우리 학원 승인 양식: {approved_count}/{required_count}개"
                if channel_status["custom_channel_registered"]
                else f"검수 승인된 공용 양식: {approved_count}개"
            ),
        })

        all_ok = all(c["ok"] for c in results["checks"])
        results["all_ok"] = all_ok
        results["summary"] = (
            "모든 설정이 정상입니다. 알림톡을 발송할 수 있습니다."
            if all_ok
            else (
                results["messaging_disabled_reason"]
                if messaging_disabled
                else "일부 설정이 필요합니다. 위 항목을 확인해 주세요."
            )
        )

        return Response(results)
