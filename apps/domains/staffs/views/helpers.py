# PATH: apps/domains/staffs/views/helpers.py
# 원칙: 1테넌트 = 1프로그램. 도메인(테넌트)별 완전 격리. 조회/생성/수정/삭제는 항상 request.tenant 기준.

from django.db import transaction
from django.db.models import Sum

from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination

from academy.adapters.db.django import repositories_staffs as staff_repo
from academy.adapters.db.django import repositories_core as core_repo
from apps.core.models import TenantMembership
from apps.core.permissions import (
    TenantResolvedAndPayrollManager,
    can_manage_staff_payroll,
)


def _owner_display_for_tenant(tenant, request=None):
    """테넌트 원장(owner) 표시용 딕셔너리. 직원 목록 상단 노출용."""
    def owner_payload(name, phone):
        return {
            "id": None,
            "name": name,
            "phone": phone,
            "role": "OWNER",
            "account_role": "OWNER",
            "position": "OWNER",
            "position_label": "대표",
            "can_manage_staff": True,
            "is_owner": True,
        }

    if not tenant:
        return None
    # 1) TenantMembership role=owner
    m = (
        TenantMembership.objects.filter(
            tenant=tenant, role="owner", is_active=True
        )
        .select_related("user")
        .first()
    )
    if m:
        name = (getattr(m.user, "name", None) or "").strip() or m.user.username
        phone = (getattr(m.user, "phone", None) or "").strip() or None
        return owner_payload(name, phone)
    # 2) tenant.owner_name (+ tenant.phone 있으면 원장 연락처로)
    if (getattr(tenant, "owner_name", None) or "").strip():
        name = (tenant.owner_name or "").strip()
        phone = (getattr(tenant, "phone", None) or "").strip() or None
        return owner_payload(name, phone)
    # 3) 현재 사용자가 이 테넌트 owner 멤버십 보유
    if request and request.user and request.user.is_authenticated:
        from academy.adapters.db.django import repositories_core as core_repo
        if core_repo.membership_exists_staff(tenant=tenant, user=request.user, staff_roles=("owner",)):
            name = (getattr(request.user, "name", None) or "").strip() or request.user.username
            phone = (getattr(request.user, "phone", None) or "").strip() or None
            return owner_payload(name, phone)
    return None

# ===========================
# Permissions
# ===========================

def can_access_staff_management(user, tenant=None) -> bool:
    """
    직원관리 페이지 접근 권한.
    - 현재 tenant의 owner, admin 역할만 True
    - `Staff.is_manager`는 직위/업무 위임 정보이며 급여 접근권을 만들지 않음
    """
    return can_manage_staff_payroll(user, tenant)


IsPayrollManager = TenantResolvedAndPayrollManager


class StaffDomainPagination(PageNumberPagination):
    """Bounded lists with an explicit staff-domain page-size contract."""

    page_size = 100
    page_size_query_param = "page_size"
    max_page_size = 500

# ===========================
# Helpers
# ===========================

def is_month_locked(staff, date):
    return staff_repo.is_month_locked(staff, date.year, date.month)


def can_manage_payroll(user, tenant=None) -> bool:
    """테넌트 전체 직원·급여 관리 접근 가능 여부."""
    return can_access_staff_management(user, tenant)


def generate_payroll_snapshot(staff, year, month, user):
    if staff_repo.payroll_snapshot_exists_staff(staff, year, month):
        raise ValidationError("이미 급여 스냅샷이 생성된 월입니다.")

    with transaction.atomic():
        wr_qs = staff_repo.work_record_queryset_staff_date_ym(staff, year, month)
        er_qs = staff_repo.expense_record_queryset_staff_date_ym(staff, year, month, status="APPROVED")

        work_hours = wr_qs.aggregate(total=Sum("work_hours"))["total"] or 0
        work_amount = wr_qs.aggregate(total=Sum("amount"))["total"] or 0
        approved_expense_amount = er_qs.aggregate(total=Sum("amount"))["total"] or 0
        total_amount = work_amount + approved_expense_amount

        staff_repo.payroll_snapshot_create_full(
            tenant=staff.tenant,
            staff=staff,
            year=year,
            month=month,
            work_hours=work_hours,
            work_amount=work_amount,
            approved_expense_amount=approved_expense_amount,
            total_amount=total_amount,
            generated_by=user,
        )
