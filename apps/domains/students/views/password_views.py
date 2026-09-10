# PATH: apps/domains/students/views/password_views.py

import logging

from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from apps.core.permissions import TenantResolved
from apps.api.common.throttles import AlimtalkEndpointThrottle, StaffPasswordResetThrottle
from apps.core.models import TenantMembership


logger = logging.getLogger(__name__)


LEGACY_PASSWORD_FIND_GONE = (
    "이전 인증번호 방식 비밀번호 찾기는 중단되었습니다. "
    "로그인 화면의 아이디/비밀번호 찾기에서 카카오 알림톡 임시 비밀번호를 받아 주세요."
)


def _pw_reset_cache_key(tenant_id, phone: str) -> str:
    return f"pw_reset:{tenant_id}:{phone}"


class StudentPasswordFindRequestView(APIView):
    """Legacy OTP password recovery endpoint, sealed in favor of account recovery."""
    permission_classes = [AllowAny, TenantResolved]
    throttle_classes = [AlimtalkEndpointThrottle]

    def get_authenticators(self):
        return []  # 비로그인 요청 허용, 만료 JWT 시 401 방지

    def post(self, request):
        return Response({"detail": LEGACY_PASSWORD_FIND_GONE}, status=410)


class StudentPasswordFindVerifyView(APIView):
    """Legacy OTP verification endpoint, sealed in favor of account recovery."""
    permission_classes = [AllowAny, TenantResolved]
    throttle_classes = [AlimtalkEndpointThrottle]

    def get_authenticators(self):
        return []  # 비로그인 요청 허용, 만료 JWT 시 401 방지

    def post(self, request):
        return Response({"detail": LEGACY_PASSWORD_FIND_GONE}, status=410)


def _normalize_phone_for_reset(value):
    """전화번호 정규화 (하이픈 제거, 11자리)."""
    s = (value or "").replace(" ", "").replace("-", "").replace(".", "").strip()
    return s if len(s) == 11 and s.startswith("010") else ""


def _is_staff_password_reset_request(request) -> bool:
    user = getattr(request, "user", None)
    tenant = getattr(request, "tenant", None)
    return bool(
        user
        and user.is_authenticated
        and tenant
        and TenantMembership.objects.filter(
            user=user,
            tenant=tenant,
            role__in=["owner", "admin", "teacher", "staff"],
            is_active=True,
        ).exists()
    )


class StudentPasswordResetSendView(APIView):
    """
    POST: 학생 또는 학부모 비밀번호 재설정.
    공개 요청은 pending 임시 비밀번호, staff 요청은 즉시 변경 경로로 정본 서비스에 위임한다.
    """
    permission_classes = [AllowAny, TenantResolved]
    throttle_classes = [AlimtalkEndpointThrottle]

    def get_authenticators(self):
        """AllowAny이지만 JWT가 있으면 파싱해 staff reset 권한을 판별한다."""
        from apps.core.authentication import TokenVersionJWTAuthentication

        return [TokenVersionJWTAuthentication()]

    def get_throttles(self):
        if _is_staff_password_reset_request(getattr(self, "request", None)):
            return [StaffPasswordResetThrottle()]
        return [AlimtalkEndpointThrottle()]

    def post(self, request):
        tenant = getattr(request, "tenant", None)
        if not tenant:
            return Response({"detail": "Tenant를 확인할 수 없습니다."}, status=400)

        target = (request.data.get("target") or "").strip().lower()
        student_name = (request.data.get("student_name") or "").strip()
        student_ps_number = (request.data.get("student_ps_number") or "").strip()
        parent_phone = _normalize_phone_for_reset(request.data.get("parent_phone") or "")

        if target not in ("student", "parent"):
            return Response({"detail": "대상을 선택해 주세요. (학생 / 학부모)"}, status=400)
        if not student_name:
            return Response({"detail": "학생 이름을 입력해 주세요."}, status=400)

        is_staff_request = _is_staff_password_reset_request(request)

        if not is_staff_request:
            from apps.domains.students.services.account_recovery import (
                AccountRecoveryDeliveryError,
                AccountRecoveryValidationError,
                resolve_recovery_account,
                send_password_recovery,
                validate_recovery_payload,
            )

            verified_phone = parent_phone
            if target == "student":
                verified_phone = _normalize_phone_for_reset(request.data.get("student_phone") or "") or parent_phone

            try:
                _, recovery_target, name, phone = validate_recovery_payload(
                    mode="password",
                    target=target,
                    name=student_name,
                    phone=verified_phone,
                )
            except AccountRecoveryValidationError as e:
                return Response({"detail": e.detail}, status=400)

            account = resolve_recovery_account(
                tenant=tenant,
                target=recovery_target,
                name=name,
                phone=phone,
            )
            message = "입력한 정보가 등록되어 있다면 해당 번호로 임시 비밀번호 알림톡이 발송됩니다."
            if account is None:
                return Response({"message": message}, status=200)

            try:
                send_password_recovery(account)
            except (AccountRecoveryValidationError, AccountRecoveryDeliveryError):
                logger.warning(
                    "Legacy public password recovery could not reserve delivery tenant_id=%s target=%s",
                    tenant.id,
                    recovery_target,
                    exc_info=True,
                )
            return Response({"message": message}, status=200)

        from apps.domains.students.services.account_recovery import (
            AccountRecoveryDeliveryError,
            AccountRecoveryValidationError,
            reset_staff_password,
            resolve_staff_password_reset_account,
        )

        try:
            account = resolve_staff_password_reset_account(
                tenant=tenant,
                target=target,
                student_name=student_name,
                student_ps_number=student_ps_number,
                student_phone=request.data.get("student_phone") or "",
                parent_phone=parent_phone,
            )
            message = reset_staff_password(
                account,
                temp_password=(request.data.get("temp_password") or "").strip(),
            )
        except AccountRecoveryValidationError as e:
            return Response({"detail": e.detail}, status=400)
        except AccountRecoveryDeliveryError as e:
            return Response({"detail": e.detail}, status=503)

        return Response({"message": message}, status=200)
