# JWT 발급 시 테넌트별 User 조회. 1테넌트=1프로그램 격리.
from __future__ import annotations

from collections.abc import Mapping
import re
import unicodedata

from django.conf import settings
from django.contrib.auth import get_user_model

from academy.adapters.db.django import repositories_core as core_repo
from rest_framework_simplejwt.exceptions import AuthenticationFailed
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer, TokenRefreshSerializer
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from rest_framework import serializers

from apps.core.services.password import (
    consume_pending_password_reset,
    pending_password_reset_matches,
)


_PHONE_LOGIN_SEPARATORS_RE = re.compile(r"[\s\-().]+")


def _normalize_login_identifier(value: object) -> str:
    """Normalize only an unambiguous Korean mobile-number login ID.

    Staff and student identifiers may legitimately contain punctuation, so the
    auth boundary must not apply a general punctuation-stripping rule.
    """
    raw = unicodedata.normalize("NFKC", str(value or "")).strip()
    compact = _PHONE_LOGIN_SEPARATORS_RE.sub("", raw)
    return compact if re.fullmatch(r"010\d{8}", compact) else raw


def _extract_tenant_code(*sources) -> str:
    for source in sources:
        if not source:
            continue
        if isinstance(source, Mapping):
            raw = source.get("tenant_code") or source.get("tenant")
        else:
            getter = getattr(source, "get", None)
            raw = getter("tenant_code") or getter("tenant") if callable(getter) else None
        if isinstance(raw, str):
            raw = raw.strip()
        if raw:
            return str(raw).strip()
    return ""


def _is_tenant_code_allowed_host(host: str) -> bool:
    allowed_hosts = getattr(
        settings,
        "TENANT_HEADER_CODE_ALLOWED_HOSTS",
        ("api.hakwonplus.com",),
    )
    return host in allowed_hosts or host.endswith(".elb.amazonaws.com")


def _tenant_for_auth(request, *payload_sources):
    """
    로그인 시 테넌트 결정.

    /api/v1/token/ 은 TenantMiddleware bypass 경로라 request.tenant 가 None.
    여기서 다음 우선순위로 직접 resolve:

    1) request.tenant: 만약 미들웨어가 이미 결정한 경우 그대로 사용 (안전망).
    2) host 가 header-allowed 목록(api.hakwonplus.com / localhost / *.elb.amazonaws.com)인
       경우에만 X-Tenant-Code 헤더 또는 body tenant_code 를 사용.
       중앙 API는 여러 테넌트 SPA가 공유하므로 이 경로가 Host 매핑보다 우선이다.
    3) host → TenantDomain 매핑이 있으면 그 테넌트 (운영 SSOT — tchul.com / limglish.kr 등).
    4) 그 외 host 에서 헤더/body 입력은 무시 — 임의 도메인에서 다른 테넌트 계정으로의
       로그인 시도(테넌트 enumeration / 브루트포스 표면 확장)를 차단.
    """
    tenant = getattr(request, "tenant", None)
    if tenant is not None:
        return tenant

    host = ""
    try:
        host = (request.get_host() or "").split(":")[0].strip().lower()
    except Exception:
        host = ""

    raw = (
        (request.META.get("HTTP_X_TENANT_CODE") or "").strip()
        or _extract_tenant_code(*payload_sources, getattr(request, "data", None))
    )

    # 2) 중앙 API/API ALB에서는 body/header tenant_code가 Host 매핑보다 우선이다.
    #    api.hakwonplus.com은 여러 테넌트 SPA가 공유하는 API entrypoint라 TenantDomain 상태에 의존하면 안 된다.
    if raw and _is_tenant_code_allowed_host(host):
        return core_repo.tenant_get_by_code(raw)

    # 3) host → TenantDomain (운영 tenant 도메인은 여기서 결정)
    if host:
        try:
            from apps.core.tenant.resolver import _resolve_tenant_from_host
            t = _resolve_tenant_from_host(host)
            if t is not None:
                return t
        except Exception:
            pass

    # 4) 그 외 host 에서 헤더/body 입력은 무시
    if not _is_tenant_code_allowed_host(host):
        return None

    return core_repo.tenant_get_by_code(raw) if raw else None


class TenantAwareTokenObtainPairSerializer(TokenObtainPairSerializer):
    """테넌트별 User만 로그인 허용. tenant=null 계정은 로그인 불가."""

    @staticmethod
    def _password_matches(user, password: str, *, consume_pending: bool = True) -> bool:
        if not user:
            return False
        if user.check_password(password):
            return True
        if consume_pending:
            return consume_pending_password_reset(user, password)
        return pending_password_reset_matches(user, password)

    def validate(self, attrs):
        request = self.context.get("request")
        username = _normalize_login_identifier(attrs.get("username"))
        password = attrs.get("password") or ""

        tenant = _tenant_for_auth(request, getattr(self, "initial_data", None), attrs) if request else None
        if not tenant:
            raise serializers.ValidationError(
                {"detail": "테넌트(학원) 정보가 필요합니다. 로그인 페이지에서 학원을 선택해 주세요."},
                code="authorization",
            )

        candidates = core_repo.user_list_by_tenant_login_identifier(tenant, username)
        # 학부모: ID = 학부모 전화번호. 레거시 Parent 계정에 활성 membership이
        # 없어도 아래 권한 검사에서 명시적으로 차단되도록 후보에는 포함한다.
        parent = core_repo.parent_get_by_tenant_phone(tenant, username)
        if parent and parent.user_id and all(candidate.id != parent.user_id for candidate in candidates):
            candidates.append(parent.user)
        password_matches = [
            candidate
            for candidate in candidates
            if self._password_matches(candidate, password, consume_pending=False)
        ]
        user = password_matches[0] if len(password_matches) == 1 else None
        if not user:
            raise serializers.ValidationError(
                {"detail": "로그인 아이디 또는 비밀번호가 올바르지 않습니다."},
                code="authorization",
            )
        from apps.core.services.tenant_access import user_has_active_tenant_access
        if not user_has_active_tenant_access(user, tenant):
            raise serializers.ValidationError(
                {"detail": "로그인할 수 없는 계정입니다."},
                code="authorization",
            )
        if not user.is_active:
            raise serializers.ValidationError(
                {"detail": "비활성화된 계정입니다."},
                code="authorization",
            )
        if not self._password_matches(user, password):
            raise serializers.ValidationError(
                {"detail": "로그인 아이디 또는 비밀번호가 올바르지 않습니다."},
                code="authorization",
            )

        refresh = self.get_token(user)
        # JWT에 tenant_id 클레임 추가 — 크로스테넌트 헤더 조작 방어
        refresh["tenant_id"] = tenant.id
        refresh.access_token["tenant_id"] = tenant.id
        # token_version: 비밀번호 변경 시 기존 토큰 무효화용
        tv = getattr(user, "token_version", 0) or 0
        refresh["token_version"] = tv
        refresh.access_token["token_version"] = tv
        # mcp(must_change_password) — 클라이언트의 초기/임시 비밀번호 변경 권장 UI에 사용.
        # change_password 후 토큰 무효화 → 새 토큰엔 mcp=0 으로 자동 반영.
        mcp = bool(getattr(user, "must_change_password", False))
        refresh["mcp"] = mcp
        refresh.access_token["mcp"] = mcp
        from apps.domains.students.services.activity import record_student_login

        record_student_login(request=request, tenant=tenant, user=user)
        return {
            "refresh": str(refresh),
            "access": str(refresh.access_token),
        }


class TenantAwareTokenObtainPairView(TokenObtainPairView):
    serializer_class = TenantAwareTokenObtainPairSerializer
    throttle_classes = []  # __init__에서 설정

    def get_throttles(self):
        if self.request.method.upper() != "POST":
            return []
        from apps.api.common.throttles import LoginThrottle
        return [LoginThrottle()]


class TenantAwareTokenRefreshSerializer(TokenRefreshSerializer):
    """Reject refresh tokens whose account or tenant authorization is stale."""

    def validate(self, attrs):
        refresh = self.token_class(attrs["refresh"])
        user_id = refresh.payload.get(api_settings.USER_ID_CLAIM)
        tenant_id = refresh.payload.get("tenant_id")
        token_version = refresh.payload.get("token_version")
        if user_id is None or tenant_id is None or token_version is None:
            raise AuthenticationFailed("유효하지 않은 로그인 세션입니다.", code="invalid_session")

        User = get_user_model()
        user = User.objects.filter(**{api_settings.USER_ID_FIELD: user_id}).first()
        tenant = core_repo.tenant_get_by_id(tenant_id)
        if user is None or tenant is None or not user.is_active:
            raise AuthenticationFailed("로그인 세션이 만료되었습니다.", code="session_expired")

        current_version = int(getattr(user, "token_version", 0) or 0)
        try:
            presented_version = int(token_version)
        except (TypeError, ValueError):
            presented_version = -1
        if presented_version != current_version:
            raise AuthenticationFailed("로그인 세션이 만료되었습니다.", code="session_expired")

        from apps.core.services.tenant_access import user_has_active_tenant_access
        if not user_has_active_tenant_access(user, tenant):
            raise AuthenticationFailed("로그인할 수 없는 계정입니다.", code="no_active_account")

        return super().validate(attrs)


class TenantAwareTokenRefreshView(TokenRefreshView):
    serializer_class = TenantAwareTokenRefreshSerializer
