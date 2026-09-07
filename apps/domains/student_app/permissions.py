# apps/domains/student_app/permissions.py
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission

from apps.core.services.tenant_access import (
    get_active_membership_role,
    get_authorized_tenant_role,
)
from apps.support.student_app.permission_dependencies import (
    active_students_for_parent,
    student_for_tenant_user,
)


class IsStudent(BasePermission):
    """
    활성 학생 로그인 전용
    """
    def has_permission(self, request, view):
        return get_authorized_tenant_role(
            getattr(request, "user", None),
            getattr(request, "tenant", None),
        ) == "student"


class IsStudentOrParent(BasePermission):
    """
    학생 또는 학부모 로그인 전용
    - 학생: request.tenant 기준 active Student 존재
    - 학부모: TenantMembership role=parent
    """
    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        tenant = getattr(request, "tenant", None)
        if not tenant:
            return False
        return get_authorized_tenant_role(user, tenant) in ("student", "parent")


def is_request_parent(request) -> bool:
    """request tenant 안에서 학부모 역할인 요청인지 fail-closed 판별한다."""
    user = getattr(request, "user", None)
    tenant = getattr(request, "tenant", None)
    if not user or not user.is_authenticated or not tenant:
        return False

    return get_authorized_tenant_role(user, tenant) == "parent"


def get_request_student(request, *, require_explicit_parent_child: bool = True):
    """
    요청자에 해당하는 Student 반환
    - 학생: request.tenant 기준 active Student
    - 학부모: 학습 화면은 X-Student-Id로 명시한 활성 연결 자녀만 허용
    - 커뮤니티의 기존 읽기 전용 호환 경로만 헤더 누락 시 결정적인 첫 자녀를 사용
    - 명시된 헤더가 비정상/미소유면 다른 자녀로 대체하지 않고 PermissionDenied
    """
    user = request.user
    tenant = getattr(request, "tenant", None)
    if not tenant:
        return None
    role = get_active_membership_role(user, tenant)
    student = student_for_tenant_user(tenant, user, deleted="active")
    if student and role == "student":
        return student
    has_explicit_child = "HTTP_X_STUDENT_ID" in request.META
    if role != "parent":
        return None
    parent = getattr(user, "parent_profile", None)
    if not parent or getattr(parent, "tenant_id", None) != tenant.id:
        raise PermissionDenied("선택한 자녀 정보를 확인할 수 없습니다.")
    active_students = active_students_for_parent(tenant, parent)
    if not has_explicit_child and require_explicit_parent_child:
        raise PermissionDenied("자녀를 선택한 뒤 다시 시도해 주세요.")
    if not has_explicit_child:
        return active_students.order_by("-id").first()
    header_id = request.META.get("HTTP_X_STUDENT_ID")
    try:
        sid = int(header_id)
    except (TypeError, ValueError):
        raise PermissionDenied("선택한 자녀 정보를 확인할 수 없습니다.")
    selected = active_students.filter(id=sid).first()
    if selected is None:
        raise PermissionDenied("선택한 자녀 정보를 확인할 수 없습니다.")
    return selected
