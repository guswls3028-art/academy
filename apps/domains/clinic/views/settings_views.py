# PATH: apps/domains/clinic/views/settings_views.py
from django.db import transaction
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.core.models import Tenant
from apps.core.parsing import parse_bool
from apps.core.permissions import TenantResolvedAndStaff
from apps.core.services.tenant_access import get_authorized_tenant_role
from ..capabilities import clinic_capabilities_for
from ..color_utils import get_effective_clinic_colors


# ============================================================
# Clinic Settings (패스카드 색상 등)
# ============================================================
class ClinicSettingsView(APIView):
    """
    GET/PATCH /clinic/settings/
    클리닉 설정 (패스카드 배경 색상 등)
    """
    permission_classes = [IsAuthenticated, TenantResolvedAndStaff]

    @staticmethod
    def _payload(request, tenant):
        saved = getattr(tenant, "clinic_idcard_colors", None)
        if not saved or not isinstance(saved, list) or len(saved) < 3:
            saved = ["#ef4444", "#3b82f6", "#22c55e"]
        return {
            "colors": get_effective_clinic_colors(tenant)[:3],
            "use_daily_random": getattr(tenant, "clinic_use_daily_random", False),
            "auto_approve_booking": getattr(tenant, "clinic_auto_approve_booking", False),
            "multi_slot_booking_default": getattr(
                tenant, "clinic_allow_multi_slot_booking_default", False
            ),
            "booking_mode": getattr(tenant, "clinic_booking_mode", "fixed_slot"),
            "booking_interval_minutes": getattr(
                tenant, "clinic_booking_interval_minutes", 60
            ),
            "booking_max_stay_minutes": getattr(
                tenant, "clinic_booking_max_stay_minutes", 240
            ),
            "capabilities": clinic_capabilities_for(getattr(request, "user", None), tenant),
            "saved_colors": saved[:3],
        }

    def get(self, request):
        tenant = getattr(request, "tenant", None)
        if not tenant:
            return Response({"detail": "tenant가 필요합니다."}, status=status.HTTP_400_BAD_REQUEST)

        return Response(self._payload(request, tenant))

    def patch(self, request):
        tenant = getattr(request, "tenant", None)
        if not tenant:
            return Response({"detail": "tenant가 필요합니다."}, status=status.HTTP_400_BAD_REQUEST)
        policy_fields = {
            "booking_mode",
            "booking_interval_minutes",
            "booking_max_stay_minutes",
        }
        if policy_fields.intersection(request.data) and get_authorized_tenant_role(
            request.user,
            tenant,
        ) not in {"owner", "admin"}:
            raise PermissionDenied("예약 정책은 대표 또는 관리자만 변경할 수 있습니다.")

        with transaction.atomic():
            tenant = Tenant.objects.select_for_update().get(pk=tenant.pk)
            update_fields = []
            if "use_daily_random" in request.data:
                tenant.clinic_use_daily_random = parse_bool(
                    request.data["use_daily_random"], field_name="use_daily_random",
                )
                update_fields.append("clinic_use_daily_random")

            if "auto_approve_booking" in request.data:
                tenant.clinic_auto_approve_booking = parse_bool(
                    request.data["auto_approve_booking"], field_name="auto_approve_booking",
                )
                update_fields.append("clinic_auto_approve_booking")

            if policy_fields.intersection(request.data):
                next_mode = request.data.get(
                    "booking_mode", tenant.clinic_booking_mode
                )
                if next_mode not in {"fixed_slot", "time_range"}:
                    raise ValidationError({
                        "booking_mode": "fixed_slot 또는 time_range만 사용할 수 있습니다."
                    })
                try:
                    next_interval = int(request.data.get(
                        "booking_interval_minutes",
                        tenant.clinic_booking_interval_minutes,
                    ))
                    next_max_stay = int(request.data.get(
                        "booking_max_stay_minutes",
                        tenant.clinic_booking_max_stay_minutes,
                    ))
                except (TypeError, ValueError) as exc:
                    raise ValidationError({
                        "booking_policy": "예약 간격과 최대 체류 시간은 숫자여야 합니다."
                    }) from exc
                if next_interval not in {30, 60}:
                    raise ValidationError({
                        "booking_interval_minutes": "예약 간격은 30분 또는 60분이어야 합니다."
                    })
                if next_max_stay < next_interval or next_max_stay % next_interval:
                    raise ValidationError({
                        "booking_max_stay_minutes": "최대 체류 시간은 예약 간격의 양의 배수여야 합니다."
                    })

                if "booking_mode" in request.data:
                    tenant.clinic_booking_mode = next_mode
                    update_fields.append("clinic_booking_mode")
                if "booking_interval_minutes" in request.data:
                    tenant.clinic_booking_interval_minutes = next_interval
                    update_fields.append("clinic_booking_interval_minutes")
                if "booking_max_stay_minutes" in request.data:
                    tenant.clinic_booking_max_stay_minutes = next_max_stay
                    update_fields.append("clinic_booking_max_stay_minutes")

            colors = request.data.get("colors")
            if colors is not None:
                if not isinstance(colors, list) or len(colors) != 3:
                    return Response(
                        {"detail": "colors는 3개의 색상 코드 배열이어야 합니다. (예: [\"#ef4444\", \"#3b82f6\", \"#22c55e\"])"},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                import re
                hex_pattern = re.compile(r"^#[0-9A-Fa-f]{6}$")
                for c in colors:
                    if not isinstance(c, str) or not hex_pattern.match(c):
                        return Response(
                            {"detail": f"잘못된 색상 코드: {c}. #RRGGBB 형식이어야 합니다."},
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                tenant.clinic_idcard_colors = colors[:3]
                update_fields.append("clinic_idcard_colors")

            if update_fields:
                tenant.save(update_fields=update_fields)

        return Response(self._payload(request, tenant))
