# PATH: apps/core/permissions.py
import ipaddress
from rest_framework.permissions import BasePermission

from django.conf import settings



def _get_client_ip(request):
    """클라이언트 IP 추출.

    TRUSTED_PROXY_CIDRS 가 설정된 경우:
      - REMOTE_ADDR 가 신뢰 프록시 범위 안일 때만 XFF 첫 값을 신뢰.
      - 신뢰 범위 밖이면 XFF는 위조 가능하므로 무시하고 REMOTE_ADDR 사용.
    설정이 비어 있으면 후방 호환을 위해 XFF 첫 값을 그대로 사용.
    """
    from apps.core.services.client_ip import get_client_ip

    return get_client_ip(request)


def _ip_in_allowed_cidrs(ip_str: str, allow_ips_setting: str) -> bool:
    """INTERNAL_API_ALLOW_IPS(CIDR 목록)에 ip_str이 포함되는지. 비어 있으면 True(검사 생략)."""
    if not allow_ips_setting:
        return True
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    for part in allow_ips_setting.split(","):
        cidr = part.strip()
        if not cidr:
            continue
        try:
            net = ipaddress.ip_network(cidr)
            if ip in net:
                return True
        except ValueError:
            continue
    return False


class IsLambdaInternal(BasePermission):
    """
    Lambda 전용 internal API 인증.
    - X-Internal-Key 헤더가 LAMBDA_INTERNAL_API_KEY와 일치.
    - INTERNAL_API_ALLOW_IPS 설정 시, 클라이언트 IP가 해당 CIDR 중 하나에 포함되어야 함 (Lambda VPC 10.1.0.0/16, API VPC 172.30.0.0/16).
    LAMBDA_INTERNAL_API_KEY 미설정 시 모든 요청 차단.
    """

    message = "Lambda internal API key required."

    def has_permission(self, request, view):
        key = getattr(settings, "LAMBDA_INTERNAL_API_KEY", None)
        if not key:
            return False
        if request.headers.get("X-Internal-Key") != key:
            return False
        allow_ips = getattr(settings, "INTERNAL_API_ALLOW_IPS", "") or ""
        return _ip_in_allowed_cidrs(_get_client_ip(request), allow_ips)


def is_effective_staff(user, tenant=None):
    """
    테넌트 내 슈퍼유저급 권한: 해당 테넌트 스태프(owner/admin/staff/teacher).
    superuser/staff라도 해당 테넌트에 멤버십이 있거나 tenant_id가 일치해야 함.
    크로스테넌트 접근 원천 차단.
    """
    if not user or not user.is_authenticated:
        return False
    if not tenant:
        return False
    from apps.core.services.tenant_access import user_has_active_staff_access
    return user_has_active_staff_access(user, tenant)


class IsAdminOrStaff(BasePermission):
    """
    Django admin / staff 계정 전용
    (테넌트 무관, 내부 관리용)
    """

    def has_permission(self, request, view):
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and (user.is_superuser or user.is_staff)
        )


class IsSuperuserOnly(BasePermission):
    """
    슈퍼유저 전용 (개발자 전용).
    dev_app 등 본인만 쓰는 관리 기능용.
    """

    message = "Superuser only."

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.is_superuser)


class IsStudent(BasePermission):
    """
    학생 전용 Permission
    - 로그인 필수
    - User ↔ Student OneToOne 연결 필수
    """

    message = "Student account required."

    def has_permission(self, request, view):
        from apps.core.services.tenant_access import get_authorized_tenant_role
        return get_authorized_tenant_role(
            getattr(request, "user", None),
            getattr(request, "tenant", None),
        ) == "student"


class TenantResolved(BasePermission):
    """
    ✅ Tenant Resolve only (SSOT)

    - request.tenant 가 resolve 되어야 함
    - 인증/멤버십은 요구하지 않음

    사용처:
    - 로그인 전 Public bootstrap (Program config, Tenant-bound public metadata)
    """

    message = "Tenant must be resolved."

    def has_permission(self, request, view):
        tenant = getattr(request, "tenant", None)
        return bool(tenant)


class TenantResolvedAndMember(BasePermission):
    """
    ✅ Core 기본 Permission (SSOT)

    - request.tenant 가 resolve 되어야 함
    - 인증된 사용자
    - 활성 TenantMembership 및 role별 유효 프로필 존재

    ❗ role 은 여기서 절대 해석하지 않음
    """

    message = "이 학원에 소속되어 있지 않습니다."

    def has_permission(self, request, view):
        tenant = getattr(request, "tenant", None)
        user = request.user

        if not tenant:
            return False

        if not user or not user.is_authenticated:
            return False

        from apps.core.services.tenant_access import user_has_active_tenant_access
        return user_has_active_tenant_access(user, tenant)


class TenantResolvedAndStaff(BasePermission):
    """
    ✅ 운영레벨 Staff 전용 Permission (오너 = 테넌트 내 슈퍼유저급)

    허용:
    - request.tenant 기준 owner / admin / staff / teacher (멤버십 필수)
    - superuser/staff도 멤버십 또는 tenant_id 일치 필요 (크로스테넌트 차단)

    학생 / 학부모 접근 차단용
    """

    message = "Staff membership required."

    STAFF_ROLES = ("owner", "admin", "staff", "teacher")

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        tenant = getattr(request, "tenant", None)
        if not tenant:
            return False
        from apps.core.services.tenant_access import user_has_active_staff_access
        return user_has_active_staff_access(user, tenant)


def can_manage_staff_payroll(user, tenant=None) -> bool:
    """Canonical tenant-wide payroll and staff-management authorization."""
    if not user or not user.is_authenticated or not tenant:
        return False
    from apps.core.services.tenant_access import get_authorized_tenant_role
    role = get_authorized_tenant_role(user, tenant)
    return role in ("owner", "admin")


class TenantResolvedAndPayrollManager(BasePermission):
    """Owner/admin access to tenant-wide employment and payroll data."""

    message = "Payroll manager permission required."

    def has_permission(self, request, view):
        return can_manage_staff_payroll(
            getattr(request, "user", None),
            getattr(request, "tenant", None),
        )


class TenantResolvedAndOwner(BasePermission):
    """
    ✅ Owner 전용 Permission

    dev_app 등 owner만 접근 가능한 기능용.
    """

    message = "Owner membership required."

    def has_permission(self, request, view):
        tenant = getattr(request, "tenant", None)
        user = request.user

        if not tenant:
            return False

        if not user or not user.is_authenticated:
            return False

        from academy.adapters.db.django import repositories_core as core_repo
        return core_repo.membership_exists_staff(tenant=tenant, user=user, staff_roles=("owner",))


def is_platform_admin_tenant(request) -> bool:
    """
    request.tenant이 플랫폼 관리 테넌트(OWNER_TENANT_ID)인지 확인.
    크로스 테넌트 관리 기능은 반드시 이 검증을 통과해야 한다.
    """
    tenant = getattr(request, "tenant", None)
    if not tenant:
        return False
    owner_tenant_id = getattr(settings, "OWNER_TENANT_ID", None)
    return tenant.id == owner_tenant_id


class IsPlatformAdmin(BasePermission):
    """
    ✅ /dev/* 운영 콘솔 단일 게이트.

    조건:
    - 인증됨
    - request.tenant 가 OWNER_TENANT_ID
    - 해당 테넌트의 owner 멤버십 OR superuser

    매 뷰마다 `if not is_platform_admin_tenant(request): return 403` 보일러플레이트 제거.
    """

    message = "Platform admin only."

    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            return False
        if not is_platform_admin_tenant(request):
            return False
        if user.is_superuser:
            from apps.core.services.tenant_access import user_has_active_tenant_access
            return user_has_active_tenant_access(user, getattr(request, "tenant", None))
        from academy.adapters.db.django import repositories_core as core_repo
        tenant = getattr(request, "tenant", None)
        return core_repo.membership_exists_staff(
            tenant=tenant, user=user, staff_roles=("owner",),
        )
