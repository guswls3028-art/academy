# PATH: apps/domains/staffs/views/staff.py

from calendar import monthrange
from datetime import date, datetime

from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from django_filters.rest_framework import DjangoFilterBackend
try:
    from drf_spectacular.utils import extend_schema
except ModuleNotFoundError as exc:
    if exc.name != "drf_spectacular":
        raise

    def extend_schema(*args, **kwargs):  # type: ignore[no-redef]
        """Keep runtime views importable when schema-only tooling is absent."""

        def decorator(view):
            return view

        return decorator
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.filters import SearchFilter, OrderingFilter
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied, ValidationError

from ..models import (
    ExpenseRecord,
    PayrollReferenceDeductionPolicy,
    PayrollSnapshot,
    Staff,
    StaffWorkType,
    WorkMonthLock,
    WorkRecord,
    WorkType,
)
from ..serializers import (
    StaffListSerializer,
    StaffDetailSerializer,
    StaffCreateUpdateSerializer,
    CurrentlyWorkingStaffSerializer,
    StaffWorkCurrentStatusSerializer,
    StaffWorkRangeQuerySerializer,
    StaffWorkStartRequestSerializer,
    StaffWorkSummarySerializer,
    StaffPayrollOverviewQuerySerializer,
    StaffPayrollOverviewSerializer,
    WorkRecordSerializer,
)
from ..services import start_work_record
from ..selectors import (
    current_work_record_for_staff,
    open_work_records_for_tenant,
    work_current_status,
    work_records_for_staff_range,
)
from academy.adapters.db.django import repositories_staffs as staff_repo
from academy.adapters.db.django import repositories_core as core_repo
from academy.adapters.db.django import repositories_teachers as teacher_repo
from ..filters import StaffFilter
from apps.core.models import TenantMembership
from apps.core.permissions import TenantResolvedAndMember, TenantResolvedAndStaff
from .helpers import (
    _owner_display_for_tenant,
    IsPayrollManager,
    StaffDomainPagination,
    can_manage_payroll,
)

# ===========================
# Staff
# ===========================

class StaffViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, IsPayrollManager]
    pagination_class = StaffDomainPagination

    filter_backends = (DjangoFilterBackend, SearchFilter, OrderingFilter)
    filterset_class = StaffFilter
    search_fields = ["name", "phone"]
    ordering_fields = ["name", "created_at", "is_active"]

    def get_permissions(self):
        if self.action in (
            "work_current",
            "work_records",
            "summary",
            "start_work",
        ):
            return [IsAuthenticated(), TenantResolvedAndStaff()]
        if self.action == "me":
            return [IsAuthenticated(), TenantResolvedAndMember()]
        return super().get_permissions()

    def get_serializer_class(self):
        if self.action == "list":
            return StaffListSerializer
        if self.action == "retrieve":
            return StaffDetailSerializer
        return StaffCreateUpdateSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Staff.objects.none()
        qs = staff_repo.staff_queryset_tenant(self.request.tenant)
        # list 액션: 오너 Staff 제외 (owner 영역에서 별도 표시하므로 중복 방지)
        if self.action == "list":
            tenant = getattr(self.request, "tenant", None)
            if tenant:
                owner_user_ids = TenantMembership.objects.filter(
                    tenant=tenant, role="owner", is_active=True
                ).values_list("user_id", flat=True)
                qs = qs.exclude(user_id__in=owner_user_ids)
        return qs

    def get_serializer_context(self):
        # list action role lookup uses memberships as SSOT for account-backed
        # staff, plus a legacy Teacher key set for rows without an account.
        ctx = super().get_serializer_context()
        if self.action == "list":
            tenant = getattr(self.request, "tenant", None)
            if tenant:
                ctx["membership_roles"] = dict(
                    TenantMembership.objects.filter(
                        tenant=tenant,
                        is_active=True,
                    )
                    .values_list("user_id", "role")
                )
                ctx["teacher_keys"] = self._unambiguous_legacy_teacher_keys(
                    tenant
                )
        return ctx

    def _unambiguous_legacy_teacher_keys(self, tenant):
        staff_key_counts = {
            (row["name"], row["phone"] or ""): row["row_count"]
            for row in (
                Staff.objects.filter(tenant=tenant, user__isnull=True)
                .values("name", "phone")
                .annotate(row_count=Count("id"))
            )
        }
        return {
            key
            for key in teacher_repo.teacher_name_phone_keys_tenant(tenant)
            if staff_key_counts.get(key) == 1
        }

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        tenant = getattr(request, "tenant", None)
        owner = _owner_display_for_tenant(tenant, request)
        # Pagination 없으면 response.data 가 list 이므로 dict 로 감싼 뒤 owner 추가
        if isinstance(response.data, list):
            response.data = {"results": response.data, "owner": owner}
        else:
            response.data["owner"] = owner
        return response

    @extend_schema(
        operation_id="staffs_payroll_overview_retrieve",
        parameters=[StaffPayrollOverviewQuerySerializer],
        responses=StaffPayrollOverviewSerializer,
    )
    @action(detail=False, methods=["get"], url_path="payroll-overview")
    def payroll_overview(self, request):
        """전 직원 월 정산 현황. 금액·블로커·마감 상태를 한 번에 반환한다."""
        query = StaffPayrollOverviewQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        year = query.validated_data["year"]
        month = query.validated_data["month"]
        date_from = date(year, month, 1)
        date_to = date(year, month, monthrange(year, month)[1])
        tenant = request.tenant

        staffs = list(
            Staff.objects.filter(tenant=tenant)
            .filter(
                Q(is_active=True)
                | Q(work_records__date__range=(date_from, date_to))
                | Q(expense_records__date__range=(date_from, date_to))
                | Q(payroll_snapshots__year=year, payroll_snapshots__month=month)
            )
            .select_related("user")
            .distinct()
        )
        staff_ids = [staff.id for staff in staffs]
        user_ids = [staff.user_id for staff in staffs if staff.user_id]
        membership_roles = dict(
            TenantMembership.objects.filter(
                tenant=tenant,
                user_id__in=user_ids,
                is_active=True,
            ).values_list("user_id", "role")
        )

        work_by_staff = {
            row["staff_id"]: row
            for row in (
                WorkRecord.objects.filter(
                    tenant=tenant,
                    staff_id__in=staff_ids,
                    date__range=(date_from, date_to),
                )
                .values("staff_id")
                .annotate(
                    work_hours_total=Sum("work_hours"),
                    work_amount_total=Sum("amount"),
                    open_work_record_count=Count(
                        "id",
                        filter=Q(end_time__isnull=True),
                    ),
                    incomplete_work_record_count=Count(
                        "id",
                        filter=(
                            Q(end_time__isnull=False)
                            & (Q(work_hours__isnull=True) | Q(amount__isnull=True))
                        ),
                    ),
                )
            )
        }
        work_type_by_staff: dict[int, list[dict]] = {}
        work_type_totals: dict[int, dict] = {}
        for item in (
            WorkRecord.objects.filter(
                tenant=tenant,
                staff_id__in=staff_ids,
                date__range=(date_from, date_to),
            )
            .values(
                "staff_id",
                "work_type_id",
                "work_type__name",
                "work_type__color",
            )
            .annotate(
                record_count=Count("id"),
                work_hours=Sum("work_hours"),
                work_amount=Sum("amount"),
            )
            .order_by("work_type__name", "work_type_id")
        ):
            breakdown = {
                "work_type_id": item["work_type_id"],
                "work_type_name": item["work_type__name"],
                "color": item["work_type__color"],
                "record_count": int(item["record_count"] or 0),
                "work_hours": float(item["work_hours"] or 0),
                "work_amount": int(item["work_amount"] or 0),
            }
            work_type_by_staff.setdefault(item["staff_id"], []).append(breakdown)
            total = work_type_totals.setdefault(
                item["work_type_id"],
                {
                    "work_type_id": item["work_type_id"],
                    "work_type_name": item["work_type__name"],
                    "color": item["work_type__color"],
                    "record_count": 0,
                    "work_hours": 0.0,
                    "work_amount": 0,
                },
            )
            total["record_count"] += breakdown["record_count"]
            total["work_hours"] += breakdown["work_hours"]
            total["work_amount"] += breakdown["work_amount"]

        duplicate_by_staff: dict[int, int] = {}
        for item in (
            WorkRecord.objects.filter(
                tenant=tenant,
                staff_id__in=staff_ids,
                date__range=(date_from, date_to),
            )
            .values("staff_id", "date", "start_time", "end_time", "work_type_id")
            .annotate(row_count=Count("id"))
            .filter(row_count__gt=1)
        ):
            duplicate_by_staff[item["staff_id"]] = (
                duplicate_by_staff.get(item["staff_id"], 0)
                + int(item["row_count"])
            )
        abnormal_long_by_staff = dict(
            WorkRecord.objects.filter(
                tenant=tenant,
                staff_id__in=staff_ids,
                date__range=(date_from, date_to),
                end_time__isnull=False,
                work_hours__gte=12,
            )
            .values("staff_id")
            .annotate(row_count=Count("id"))
            .values_list("staff_id", "row_count")
        )
        manually_edited_by_staff = dict(
            WorkRecord.objects.filter(
                tenant=tenant,
                staff_id__in=staff_ids,
                date__range=(date_from, date_to),
                is_manually_edited=True,
            )
            .values("staff_id")
            .annotate(row_count=Count("id"))
            .values_list("staff_id", "row_count")
        )
        expense_by_staff = {
            row["staff_id"]: row
            for row in (
                ExpenseRecord.objects.filter(
                    tenant=tenant,
                    staff_id__in=staff_ids,
                    date__range=(date_from, date_to),
                )
                .values("staff_id")
                .annotate(
                    approved_expense_amount=Sum(
                        "amount",
                        filter=Q(status="APPROVED"),
                    ),
                    pending_expense_amount=Sum(
                        "amount",
                        filter=Q(status="PENDING"),
                    ),
                    pending_expense_count=Count(
                        "id",
                        filter=Q(status="PENDING"),
                    ),
                )
            )
        }
        assigned_work_type_counts = dict(
            StaffWorkType.objects.filter(
                tenant=tenant,
                staff_id__in=staff_ids,
                work_type__is_active=True,
            )
            .values("staff_id")
            .annotate(row_count=Count("id"))
            .values_list("staff_id", "row_count")
        )
        locked_staff_ids = set(
            WorkMonthLock.objects.filter(
                tenant=tenant,
                staff_id__in=staff_ids,
                year=year,
                month=month,
                is_locked=True,
            ).values_list("staff_id", flat=True)
        )
        snapshot_staff_ids = set(
            PayrollSnapshot.objects.filter(
                tenant=tenant,
                staff_id__in=staff_ids,
                year=year,
                month=month,
            ).values_list("staff_id", flat=True)
        )

        account_role_codes = {
            "owner": "OWNER",
            "admin": "ADMIN",
            "teacher": "TEACHER",
            "staff": "STAFF",
        }
        rows = []
        totals = {
            "staff_count": len(staffs),
            "work_hours": 0.0,
            "work_amount": 0,
            "approved_expense_amount": 0,
            "pending_expense_amount": 0,
            "total_amount": 0,
            "reference_business_income_tax": 0,
            "reference_local_income_tax": 0,
            "reference_deduction_total": 0,
            "reference_net_work_amount": 0,
            "reference_transfer_amount": 0,
            "advisory_issue_count": 0,
            "needs_review_count": 0,
            "closed_count": 0,
        }
        for staff in staffs:
            work = work_by_staff.get(staff.id, {})
            expenses = expense_by_staff.get(staff.id, {})
            work_hours = float(work.get("work_hours_total") or 0)
            work_amount = int(work.get("work_amount_total") or 0)
            approved_expense_amount = int(
                expenses.get("approved_expense_amount") or 0
            )
            pending_expense_amount = int(
                expenses.get("pending_expense_amount") or 0
            )
            pending_expense_count = int(
                expenses.get("pending_expense_count") or 0
            )
            open_work_record_count = int(
                work.get("open_work_record_count") or 0
            )
            incomplete_work_record_count = int(
                work.get("incomplete_work_record_count") or 0
            )
            assigned_work_type_count = int(
                assigned_work_type_counts.get(staff.id, 0)
            )
            locked = staff.id in locked_staff_ids
            snapshot_exists = staff.id in snapshot_staff_ids
            reconciliation_required = locked != snapshot_exists
            if reconciliation_required:
                needs_review = True
                settlement_status = "RECONCILIATION_REQUIRED"
            elif locked:
                # A complete lock/snapshot pair is immutable historical truth.
                # Current work-type assignments must not retroactively turn a
                # closed month into a review item.
                needs_review = False
                settlement_status = "CLOSED"
            else:
                needs_review = bool(
                    open_work_record_count
                    or incomplete_work_record_count
                    or pending_expense_count
                    or (staff.is_active and assigned_work_type_count == 0)
                    or staff.pay_type == "MONTHLY"
                )
                settlement_status = "NEEDS_REVIEW" if needs_review else "OPEN"

            membership_role = membership_roles.get(staff.user_id)
            account_role = account_role_codes.get(membership_role, "NONE")
            total_amount = work_amount + approved_expense_amount
            reference = PayrollReferenceDeductionPolicy.calculate(work_amount)
            reference_transfer_amount = (
                reference["net_work_amount"] + approved_expense_amount
            )
            duplicate_work_record_count = int(
                duplicate_by_staff.get(staff.id, 0)
            )
            abnormal_long_work_record_count = int(
                abnormal_long_by_staff.get(staff.id, 0)
            )
            manually_edited_work_record_count = int(
                manually_edited_by_staff.get(staff.id, 0)
            )
            advisory_issue_count = (
                duplicate_work_record_count
                + abnormal_long_work_record_count
                + open_work_record_count
                + incomplete_work_record_count
                + manually_edited_work_record_count
            )
            row = {
                "staff_id": staff.id,
                "name": staff.name,
                "position": staff.position,
                "position_label": staff.get_position_display(),
                "account_role": account_role,
                "is_active": staff.is_active,
                "can_manage_staff": (
                    account_role in ("OWNER", "ADMIN")
                    or bool(
                        staff.is_active
                        and account_role in ("TEACHER", "STAFF")
                        and staff.is_manager
                    )
                ),
                "pay_type": staff.pay_type,
                "work_hours": work_hours,
                "work_amount": work_amount,
                "approved_expense_amount": approved_expense_amount,
                "pending_expense_amount": pending_expense_amount,
                "pending_expense_count": pending_expense_count,
                "total_amount": total_amount,
                "reference_business_income_tax": reference["business_income_tax"],
                "reference_local_income_tax": reference["local_income_tax"],
                "reference_deduction_total": reference["deduction_total"],
                "reference_net_work_amount": reference["net_work_amount"],
                "reference_transfer_amount": reference_transfer_amount,
                "work_type_breakdown": work_type_by_staff.get(staff.id, []),
                "open_work_record_count": open_work_record_count,
                "incomplete_work_record_count": incomplete_work_record_count,
                "duplicate_work_record_count": duplicate_work_record_count,
                "abnormal_long_work_record_count": abnormal_long_work_record_count,
                "manually_edited_work_record_count": manually_edited_work_record_count,
                "advisory_issue_count": advisory_issue_count,
                "assigned_work_type_count": assigned_work_type_count,
                "settlement_status": settlement_status,
                "can_close": not needs_review and not locked,
            }
            rows.append(row)
            totals["work_hours"] += work_hours
            totals["work_amount"] += work_amount
            totals["approved_expense_amount"] += approved_expense_amount
            totals["pending_expense_amount"] += pending_expense_amount
            totals["total_amount"] += total_amount
            totals["reference_business_income_tax"] += reference["business_income_tax"]
            totals["reference_local_income_tax"] += reference["local_income_tax"]
            totals["reference_deduction_total"] += reference["deduction_total"]
            totals["reference_net_work_amount"] += reference["net_work_amount"]
            totals["reference_transfer_amount"] += reference_transfer_amount
            totals["advisory_issue_count"] += advisory_issue_count
            totals["needs_review_count"] += int(needs_review)
            totals["closed_count"] += int(locked and snapshot_exists)

        position_order = {
            "DIRECTOR": 0,
            "INSTRUCTOR": 1,
            "ASSISTANT": 2,
            "STAFF": 3,
        }
        rows.sort(
            key=lambda row: (
                position_order.get(row["position"], 9),
                row["name"],
            )
        )
        totals["work_hours"] = round(totals["work_hours"], 2)
        totals["work_type_breakdown"] = sorted(
            work_type_totals.values(),
            key=lambda item: (item["work_type_name"], item["work_type_id"]),
        )
        for item in totals["work_type_breakdown"]:
            item["work_hours"] = round(item["work_hours"], 2)

        return Response(
            {
                "year": year,
                "month": month,
                "date_from": date_from,
                "date_to": date_to,
                "totals": totals,
                "rows": rows,
            }
        )

    def perform_create(self, serializer):
        staff = serializer.save(tenant=self.request.tenant)
        from apps.core.services.ops_audit import record_audit
        record_audit(
            self.request,
            action="staff.created",
            target_tenant=self.request.tenant,
            target_user=staff.user,
            summary=f"staff_id={staff.id}",
            payload={
                "staff_id": staff.id,
                "role": self.request.data.get("role"),
                "has_login": bool(staff.user_id),
            },
        )

    def perform_update(self, serializer):
        staff = serializer.save()
        from apps.core.services.ops_audit import record_audit
        record_audit(
            self.request,
            action="staff.updated",
            target_tenant=self.request.tenant,
            target_user=staff.user,
            summary=f"staff_id={staff.id}",
            payload={
                "staff_id": staff.id,
                "fields": sorted(
                    key
                    for key in self.request.data.keys()
                    if key != "password"
                ),
            },
        )

    def perform_destroy(self, instance):
        target_user = instance.user
        staff_id = instance.id
        serializer = self.get_serializer(instance)
        serializer.delete(instance)
        from apps.core.services.ops_audit import record_audit
        record_audit(
            self.request,
            action="staff.deleted_without_history",
            target_tenant=self.request.tenant,
            target_user=target_user,
            summary=f"staff_id={staff_id}",
            payload={"staff_id": staff_id},
        )

    @action(detail=True, methods=["post"], url_path="change-password")
    def change_password(self, request, pk=None):
        """직원 비밀번호 변경. Body: { "password": "..." }"""
        new_password = (request.data.get("password") or "").strip()
        if not new_password:
            raise ValidationError({"password": "새 비밀번호를 입력하세요."})
        if len(new_password) < 4:
            raise ValidationError({"password": "비밀번호는 4자 이상이어야 합니다."})

        with transaction.atomic():
            staff_ref = self.get_object()
            staff = (
                Staff.objects.select_for_update(of=("self",))
                .select_related("user")
                .get(tenant=request.tenant, id=staff_ref.id)
            )
            if not staff.user_id:
                raise ValidationError("이 직원에게 연결된 계정이 없습니다.")

            from django.contrib.auth import get_user_model

            target_user = get_user_model().objects.select_for_update().get(
                id=staff.user_id
            )
            memberships = {
                membership.user_id: membership
                for membership in TenantMembership.objects.select_for_update()
                .filter(
                    tenant=request.tenant,
                    user_id__in={request.user.id, target_user.id},
                )
                .order_by("user_id")
            }
            actor_membership = memberships.get(request.user.id)
            target_membership = memberships.get(target_user.id)
            if target_membership and target_membership.role == "owner":
                raise PermissionDenied(
                    "대표 계정 비밀번호는 직원관리에서 변경할 수 없습니다."
                )
            if (
                target_membership
                and target_membership.role == "admin"
                and (
                    not actor_membership
                    or actor_membership.role != "owner"
                )
            ):
                raise PermissionDenied(
                    "관리자 계정 비밀번호는 대표만 변경할 수 있습니다."
                )

            from apps.core.services.password import change_password

            change_password(target_user, new_password)
        from apps.core.services.ops_audit import record_audit
        record_audit(
            request,
            action="staff.password_reset",
            target_tenant=request.tenant,
            target_user=target_user,
            summary=f"staff_id={staff.id}",
            payload={"staff_id": staff.id},
        )
        return Response({"detail": "비밀번호가 변경되었습니다."})

    @action(detail=False, methods=["get"], url_path="me", permission_classes=[IsAuthenticated, TenantResolvedAndMember])
    def me(self, request):
        import logging
        logger = logging.getLogger(__name__)
        tenant = getattr(request, "tenant", None)
        try:
            from academy.adapters.db.django import repositories_core as core_repo
            is_owner = bool(
                tenant
                and request.user.is_authenticated
                and core_repo.membership_exists_staff(tenant=tenant, user=request.user, staff_roles=("owner",))
            )
            owner_display_name = None
            owner_phone = None
            if is_owner and request.user:
                owner_display_name = (getattr(request.user, "name", None) or "").strip() or getattr(request.user, "username", "") or "원장"
                owner_phone = (getattr(request.user, "phone", None) or "").strip() or None

            payload = {
                "is_authenticated": True,
                "is_superuser": bool(request.user.is_superuser),
                "is_staff": bool(request.user.is_staff),
                "is_payroll_manager": can_manage_payroll(request.user, tenant),
                "is_owner": is_owner,
                "owner_display_name": owner_display_name,
                "owner_phone": owner_phone,
            }

            # 직원(Staff)으로 로그인한 경우: 출근/퇴근용 staff_id, default_work_type_id
            # 오너여도 동일하게 출퇴근 기록 가능하도록: 오너인데 Staff가 없으면 해당 테넌트에 Staff 생성 후 연결
            # staff_profile = OneToOneField reverse → 다른 테넌트 Staff일 수 있으므로 tenant_id 확인 필수
            staff_profile = getattr(request.user, "staff_profile", None)
            staff_in_tenant = (
                staff_profile
                if staff_profile and tenant and getattr(staff_profile, "tenant_id", None) == tenant.id
                else None
            )
            if staff_in_tenant:
                payload["staff_id"] = staff_in_tenant.id
                assigned_work_types = list(
                    staff_in_tenant.staff_work_types.filter(
                        work_type__is_active=True,
                    )
                    .select_related("work_type")
                    .order_by("id")
                )
                payload["assigned_work_types"] = [
                    {
                        "id": swt.work_type_id,
                        "name": swt.work_type.name,
                        "hourly_wage": swt.effective_hourly_wage,
                    }
                    for swt in assigned_work_types
                ]
                if len(assigned_work_types) == 1:
                    payload["default_work_type_id"] = (
                        assigned_work_types[0].work_type_id
                    )
            elif is_owner and tenant and request.user:
                # staff_profile이 다른 테넌트에 있거나 없을 때: 현재 테넌트에서 Staff 조회/생성
                from apps.domains.staffs.models import Staff, StaffWorkType, WorkType
                owner_staff = Staff.objects.filter(tenant=tenant, user=request.user).first()
                if not owner_staff:
                    # user OneToOne이 이미 다른 테넌트 Staff에 연결된 경우 user=None으로 생성
                    can_link_user = staff_profile is None
                    owner_name = (getattr(request.user, "name", None) or "").strip() or getattr(request.user, "username", "") or "원장"
                    owner_phone = (getattr(request.user, "phone", None) or "").strip() or ""
                    with transaction.atomic():
                        owner_staff, _created = Staff.objects.get_or_create(
                            tenant=tenant,
                            name=owner_name,
                            phone=owner_phone or "",
                            defaults={
                                "user": request.user if can_link_user else None,
                                "is_manager": True,
                            },
                        )
                first_wt = WorkType.objects.filter(tenant=tenant, is_active=True).order_by("id").first()
                if not first_wt:
                    first_wt = WorkType.objects.create(
                        tenant=tenant,
                        name="기본",
                        base_hourly_wage=0,
                        is_active=True,
                    )
                if not owner_staff.staff_work_types.exists():
                    StaffWorkType.objects.get_or_create(
                        tenant=tenant,
                        staff=owner_staff,
                        work_type=first_wt,
                        defaults={"hourly_wage": None},
                    )
                payload["staff_id"] = owner_staff.id
                payload["default_work_type_id"] = first_wt.id

            return Response(payload)
        except Exception as e:
            logger.warning("staffs/me error: %s", e, exc_info=True)
            return Response(
                {
                    "is_authenticated": True,
                    "is_superuser": bool(getattr(request.user, "is_superuser", False)),
                    "is_staff": bool(getattr(request.user, "is_staff", False)),
                    "is_payroll_manager": False,
                    "is_owner": False,
                    "owner_display_name": None,
                    "owner_phone": None,
                }
            )

    def _staff_display_role(
        self,
        tenant,
        staff,
        *,
        membership_roles=None,
        teacher_keys=None,
    ) -> str:
        """직원관리 목록·헤더 근무자 아바타와 동일한 직급 판별: owner(대표) / TEACHER(강사) / ASSISTANT(조교)."""
        if getattr(staff, "user_id", None):
            membership_role = (
                membership_roles.get(staff.user_id)
                if membership_roles is not None
                else None
            )
            if membership_roles is None:
                membership = core_repo.membership_get(tenant, staff.user)
                membership_role = membership.role if membership else None
            if membership_role == "owner":
                return "owner"
            if membership_role == "teacher":
                return "TEACHER"
            if membership_role in ("staff", "admin"):
                return "ASSISTANT"
        from academy.adapters.db.django import repositories_teachers as teacher_repo
        is_teacher = (
            (staff.name, staff.phone or "") in teacher_keys
            if teacher_keys is not None
            else teacher_repo.teacher_exists_tenant_name_phone(
                tenant,
                staff.name,
                staff.phone or "",
            )
        )
        if is_teacher:
            return "TEACHER"
        return "ASSISTANT"

    @extend_schema(responses=CurrentlyWorkingStaffSerializer(many=True))
    @action(
        detail=False,
        methods=["get"],
        url_path="currently-working",
        permission_classes=[IsAuthenticated, TenantResolvedAndStaff],
        pagination_class=None,
        filter_backends=[],
    )
    def currently_working(self, request):
        """현재 근무 중인 직원 목록 (end_time 이 null 인 WorkRecord 가 있는 직원). 직급(role) + 근무 시작 시각·휴식 정보(드롭다운용)."""
        tenant = getattr(request, "tenant", None)
        if not tenant:
            return Response([])
        records = open_work_records_for_tenant(tenant=tenant)
        seen_staff = set()
        record_by_staff = {}
        for rec in records:
            if rec.staff_id not in seen_staff:
                seen_staff.add(rec.staff_id)
                record_by_staff[rec.staff_id] = rec
        staff_ids = list(record_by_staff.keys())
        staffs = list(
            Staff.objects.filter(id__in=staff_ids)
            .select_related("user")
            .only("id", "name", "phone", "tenant_id", "user_id")
        )
        user_ids = [staff.user_id for staff in staffs if staff.user_id]
        membership_roles = dict(
            TenantMembership.objects.filter(
                tenant=tenant,
                user_id__in=user_ids,
                is_active=True,
            ).values_list("user_id", "role")
        )
        teacher_keys = self._unambiguous_legacy_teacher_keys(tenant)
        out = []
        for s in staffs:
            try:
                role = self._staff_display_role(
                    tenant,
                    s,
                    membership_roles=membership_roles,
                    teacher_keys=teacher_keys,
                )
            except Exception:
                role = "ASSISTANT"
            rec = record_by_staff.get(s.id)
            item = {"staff_id": s.id, "staff_name": s.name, "role": role}
            if rec:
                item["date"] = rec.date.isoformat()
                item["started_at"] = rec.start_time.strftime("%H:%M:%S") if hasattr(rec.start_time, "strftime") else str(rec.start_time)
                item["work_type"] = rec.work_type_id
                item["work_type_name"] = rec.work_type.name
                item["break_minutes"] = getattr(rec, "break_minutes", 0) or 0
                item["break_total_seconds"] = getattr(rec, "break_total_seconds", 0) or (item["break_minutes"] * 60)
                if getattr(rec, "current_break_started_at", None):
                    item["break_started_at"] = rec.current_break_started_at.isoformat()
            out.append(item)
        return Response(out)

    # ===========================
    # 실시간 근무 (Staff 기준)
    # ===========================

    @staticmethod
    def _assert_self_service_or_manager(request, staff):
        if can_manage_payroll(request.user, staff.tenant):
            return
        if staff.user_id == request.user.id:
            return
        raise PermissionDenied("본인 근무 기록만 조회할 수 있습니다.")

    @staticmethod
    def _parse_work_range(request):
        date_from = request.query_params.get("date_from")
        date_to = request.query_params.get("date_to")
        if not date_from or not date_to:
            raise ValidationError("date_from, date_to는 필수입니다.")
        try:
            parsed_from = datetime.strptime(date_from, "%Y-%m-%d").date()
            parsed_to = datetime.strptime(date_to, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValidationError(
                "date_from, date_to는 YYYY-MM-DD 형식이어야 합니다."
            ) from exc
        if parsed_from > parsed_to:
            raise ValidationError("date_from은 date_to 이전이어야 합니다.")
        return parsed_from, parsed_to

    @extend_schema(
        operation_id="staffs_personal_work_records_list",
        parameters=[StaffWorkRangeQuerySerializer],
        responses=WorkRecordSerializer(many=True),
    )
    @action(
        detail=True,
        methods=["get"],
        url_path="work-records",
        permission_classes=[IsAuthenticated, TenantResolvedAndStaff],
        filter_backends=[],
    )
    def work_records(self, request, pk=None):
        """본인 또는 급여 관리자가 조회하는 기간별 정본 근무 기록."""
        staff = self.get_object()
        self._assert_self_service_or_manager(request, staff)
        date_from, date_to = self._parse_work_range(request)
        records = work_records_for_staff_range(
            tenant=request.tenant,
            staff=staff,
            date_from=date_from,
            date_to=date_to,
        )
        page = self.paginate_queryset(records)
        if page is not None:
            serializer = WorkRecordSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        return Response(WorkRecordSerializer(records, many=True).data)

    @extend_schema(responses=StaffWorkCurrentStatusSerializer)
    @action(
        detail=True,
        methods=["get"],
        url_path="work-records/current",
        permission_classes=[IsAuthenticated, TenantResolvedAndStaff],
    )
    def work_current(self, request, pk=None):
        staff = self.get_object()
        self._assert_self_service_or_manager(request, staff)

        record = current_work_record_for_staff(
            tenant=request.tenant,
            staff=staff,
        )
        return Response(work_current_status(record))

    @extend_schema(
        request=StaffWorkStartRequestSerializer,
        responses={201: WorkRecordSerializer},
    )
    @action(
        detail=True,
        methods=["post"],
        url_path="work-records/start-work",
        permission_classes=[IsAuthenticated, TenantResolvedAndStaff],
    )
    def start_work(self, request, pk=None):
        staff = self.get_object()
        is_manager = can_manage_payroll(request.user, staff.tenant)
        if not is_manager and staff.user_id != request.user.id:
            raise PermissionDenied("본인 근무만 시작할 수 있습니다.")
        request_serializer = StaffWorkStartRequestSerializer(data=request.data)
        request_serializer.is_valid(raise_exception=True)
        now = timezone.localtime(timezone.now())

        record = start_work_record(
            staff=staff,
            work_type_id=request_serializer.validated_data["work_type"],
            date=now.date(),
            start_time=now.time(),
            require_assignment=not is_manager,
        )

        return Response(WorkRecordSerializer(record).data, status=201)

    @extend_schema(
        parameters=[StaffWorkRangeQuerySerializer],
        responses=StaffWorkSummarySerializer,
    )
    @action(detail=True, methods=["get"], url_path="summary")
    def summary(self, request, pk=None):
        """직원별 기간 집계. 쿼리: date_from, date_to (YYYY-MM-DD)."""
        staff = self.get_object()
        self._assert_self_service_or_manager(request, staff)
        df, dt = self._parse_work_range(request)

        from django.db.models import Sum
        wr_qs = WorkRecord.objects.filter(
            staff=staff, tenant=staff.tenant, date__gte=df, date__lte=dt
        )
        er_qs = ExpenseRecord.objects.filter(
            staff=staff, tenant=staff.tenant,
            date__gte=df, date__lte=dt, status="APPROVED",
        )

        work_agg = wr_qs.aggregate(total_hours=Sum("work_hours"), total_amount=Sum("amount"))
        expense_agg = er_qs.aggregate(total=Sum("amount"))

        work_hours = float(work_agg["total_hours"] or 0)
        work_amount = int(work_agg["total_amount"] or 0)
        expense_amount = int(expense_agg["total"] or 0)
        total_amount = work_amount + expense_amount
        reference = PayrollReferenceDeductionPolicy.calculate(work_amount)

        return Response({
            "staff_id": staff.id,
            "work_hours": work_hours,
            "work_amount": work_amount,
            "expense_amount": expense_amount,
            "total_amount": total_amount,
            "reference_business_income_tax": reference["business_income_tax"],
            "reference_local_income_tax": reference["local_income_tax"],
            "reference_deduction_total": reference["deduction_total"],
            "reference_net_work_amount": reference["net_work_amount"],
            "reference_transfer_amount": reference["net_work_amount"] + expense_amount,
        })
