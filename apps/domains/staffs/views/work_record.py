# PATH: apps/domains/staffs/views/work_record.py

from django.db import IntegrityError, transaction
from django.utils import timezone

from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.filters import OrderingFilter
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError

from academy.adapters.db.django import repositories_staffs as staff_repo

from ..models import WorkRecord
from ..serializers import StaffWorkEndRequestSerializer, WorkRecordSerializer
from ..services import (
    end_work_break,
    end_work_record,
    OpenWorkRecordConflict,
    has_open_work_record_conflict,
    start_work_break,
)
from ..filters import WorkRecordFilter
from apps.core.permissions import TenantResolvedAndStaff
from .helpers import (
    IsPayrollManager,
    StaffDomainPagination,
    can_manage_payroll,
    is_month_locked,
)

# ===========================
# WorkRecord (Record 기준: 휴게/종료만)
# ===========================


class WorkRecordAuditUnavailable(APIException):
    status_code = 503
    default_detail = "근무기록 감사 이력을 저장하지 못했습니다. 잠시 후 다시 시도해 주세요."
    default_code = "work_record_audit_unavailable"


def _work_record_audit_payload(record) -> dict:
    def serialize_time(value):
        return value.isoformat() if value is not None else None

    return {
        "source": "payroll_manager_manual",
        "work_record_id": record.id,
        "staff_id": record.staff_id,
        "work_type_id": record.work_type_id,
        "date": str(record.date),
        "start_time": serialize_time(record.start_time),
        "end_time": serialize_time(record.end_time),
        "break_minutes": record.break_minutes,
        "break_total_seconds": record.break_total_seconds,
        "current_break_started_at": serialize_time(record.current_break_started_at),
        "meal_minutes": record.meal_minutes,
        "work_hours": str(record.work_hours) if record.work_hours is not None else None,
        "amount": record.amount,
        "resolved_hourly_wage": record.resolved_hourly_wage,
        "adjustment_amount": record.adjustment_amount,
        "is_manually_edited": record.is_manually_edited,
        "created_local_date": str(timezone.localdate(record.created_at)),
    }


def _record_required_work_record_audit(request, *, action: str, payload: dict):
    from apps.core.services.ops_audit import record_audit

    audit = record_audit(
        request,
        action=action,
        target_tenant=request.tenant,
        summary=f"work_record_id={payload['work_record_id']}",
        payload=payload,
    )
    if audit is None:
        raise WorkRecordAuditUnavailable()
    return audit


class WorkRecordViewSet(viewsets.ModelViewSet):
    serializer_class = WorkRecordSerializer
    permission_classes = [IsAuthenticated, IsPayrollManager]
    pagination_class = StaffDomainPagination

    filter_backends = (DjangoFilterBackend, OrderingFilter)
    filterset_class = WorkRecordFilter
    ordering_fields = ["date", "created_at", "amount"]

    def get_permissions(self):
        if self.action in ("start_break", "end_break", "end_work"):
            return [IsAuthenticated(), TenantResolvedAndStaff()]
        return super().get_permissions()

    def get_queryset(self):
        return (
            WorkRecord.objects
            .filter(tenant=self.request.tenant)
            .select_related("staff", "work_type")
            .order_by("-date", "-start_time")
        )

    def _assert_self_service_or_manager(self, record):
        if can_manage_payroll(self.request.user, record.tenant):
            return
        if record.staff.user_id == self.request.user.id:
            return
        raise PermissionDenied("본인 근무 기록만 처리할 수 있습니다.")

    def perform_create(self, serializer):
        tenant = getattr(self.request, "tenant", None)
        if not tenant:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("Tenant is required.")

        staff = serializer.validated_data.get("staff")
        date = serializer.validated_data.get("date")
        if staff is None or date is None:
            raise ValidationError("staff와 date는 필수입니다.")

        try:
            with transaction.atomic():
                locked_staff = staff_repo.staff_get_for_update(
                    tenant.id,
                    staff.pk,
                )
                if is_month_locked(locked_staff, date):
                    raise ValidationError(
                        "마감된 월입니다. 근무기록을 추가할 수 없습니다."
                    )
                if (
                    serializer.validated_data.get("end_time") is None
                    and has_open_work_record_conflict(staff=locked_staff)
                ):
                    raise OpenWorkRecordConflict()
                saved_record = serializer.save(
                    tenant_id=tenant.id,
                    staff=locked_staff,
                )
                _record_required_work_record_audit(
                    self.request,
                    action="staff.work_record_created",
                    payload=_work_record_audit_payload(saved_record),
                )
        except IntegrityError as exc:
            if (
                serializer.validated_data.get("end_time") is None
                and staff is not None
                and has_open_work_record_conflict(staff=staff)
            ):
                raise OpenWorkRecordConflict() from exc
            raise

    def perform_destroy(self, instance):
        with transaction.atomic():
            instance = staff_repo.work_record_get_for_update(
                instance.tenant_id,
                instance.id,
            )
            locked_staff = staff_repo.staff_get_for_update(
                instance.tenant_id,
                instance.staff_id,
            )
            if is_month_locked(locked_staff, instance.date):
                raise ValidationError("마감된 월입니다. 근무기록을 삭제할 수 없습니다.")
            payload = _work_record_audit_payload(instance)
            instance.delete()
            _record_required_work_record_audit(
                self.request,
                action="staff.work_record_deleted",
                payload=payload,
            )

    def perform_update(self, serializer):
        # Direct override fields: admin explicitly sets the final work_hours or amount
        override_fields = {"work_hours", "amount"}
        # Input fields: calculation inputs that should trigger auto-recalculation
        input_fields = {"meal_minutes", "adjustment_amount", "break_minutes",
                        "start_time", "end_time"}

        changed_keys = set(serializer.validated_data.keys())
        has_override = bool(override_fields & changed_keys)
        has_input_change = bool(input_fields & changed_keys)

        try:
            with transaction.atomic():
                stale_instance = serializer.instance
                instance = staff_repo.work_record_get_for_update(
                    stale_instance.tenant_id,
                    stale_instance.id,
                )
                serializer.instance = instance
                resulting_staff = serializer.validated_data.get(
                    "staff",
                    instance.staff,
                )
                resulting_date = serializer.validated_data.get(
                    "date",
                    instance.date,
                )
                audited_fields = sorted(
                    changed_keys
                    & {
                        "staff",
                        "work_type",
                        "date",
                        "start_time",
                        "end_time",
                        "break_minutes",
                        "meal_minutes",
                        "work_hours",
                        "amount",
                        "adjustment_amount",
                    }
                )
                old_values = {
                    field: str(
                        getattr(
                            instance,
                            f"{field}_id"
                            if field in {"staff", "work_type"}
                            else field,
                        )
                    )
                    for field in audited_fields
                }
                locked_staff_by_id = staff_repo.staff_map_for_update(
                    instance.tenant_id,
                    [instance.staff_id, resulting_staff.pk],
                )
                source_staff = locked_staff_by_id[instance.staff_id]
                locked_staff = locked_staff_by_id[resulting_staff.pk]
                if is_month_locked(source_staff, instance.date):
                    raise ValidationError("마감된 월입니다.")
                if is_month_locked(locked_staff, resulting_date):
                    raise ValidationError(
                        "변경하려는 직원의 해당 월은 마감되어 근무기록을 이동할 수 없습니다."
                    )
                resulting_end_time = serializer.validated_data.get(
                    "end_time",
                    instance.end_time,
                )
                if (
                    resulting_end_time
                    and instance.current_break_started_at
                    and "end_time" in serializer.validated_data
                ):
                    raise ValidationError(
                        "휴게 중인 근무는 퇴근 처리에서 종료해 주세요."
                    )
                if resulting_end_time is None and has_open_work_record_conflict(
                    staff=locked_staff,
                    exclude_record_id=instance.id,
                ):
                    raise OpenWorkRecordConflict()
                save_kwargs = {}
                if "staff" in serializer.validated_data:
                    save_kwargs["staff"] = locked_staff
                if has_override:
                    # Admin directly set work_hours or amount → mark as manually edited
                    saved_record = serializer.save(
                        is_manually_edited=True,
                        **save_kwargs,
                    )
                elif has_input_change and resulting_end_time:
                    # 급여 입력 변경은 명시적으로만 재계산한다. 메모 등 비급여
                    # PATCH가 기존 확정 금액을 새 산식으로 바꾸면 안 된다.
                    saved_record = serializer.save(
                        is_manually_edited=False,
                        **save_kwargs,
                    )
                    saved_record.save(recalculate_payroll=True)
                else:
                    saved_record = serializer.save(**save_kwargs)
                if audited_fields:
                    from apps.core.services.ops_audit import record_audit

                    record_audit(
                        self.request,
                        action="staff.work_record_updated",
                        target_tenant=self.request.tenant,
                        summary=f"work_record_id={saved_record.id}",
                        payload={
                            "work_record_id": saved_record.id,
                            "fields": audited_fields,
                            "old": old_values,
                            "new": {
                                field: str(
                                    getattr(
                                        saved_record,
                                        f"{field}_id"
                                        if field in {"staff", "work_type"}
                                        else field,
                                    )
                                )
                                for field in audited_fields
                            },
                        },
                    )
        except IntegrityError as exc:
            resulting_staff = serializer.validated_data.get(
                "staff",
                serializer.instance.staff,
            )
            resulting_end_time = serializer.validated_data.get(
                "end_time",
                serializer.instance.end_time,
            )
            if resulting_end_time is None and has_open_work_record_conflict(
                staff=resulting_staff,
                exclude_record_id=serializer.instance.id,
            ):
                raise OpenWorkRecordConflict() from exc
            raise

    @action(detail=True, methods=["post"], url_path="recalculate")
    @transaction.atomic
    def recalculate(self, request, pk=None):
        """수동 수정 플래그를 해제하고 자동 재계산. 관리자가 '자동 계산으로 복원' 시 사용."""
        record_ref = self.get_object()
        record = staff_repo.work_record_get_for_update(
            record_ref.tenant_id,
            record_ref.id,
        )
        self._assert_self_service_or_manager(record)
        locked_staff = staff_repo.staff_get_for_update(
            record.tenant_id,
            record.staff_id,
        )

        if is_month_locked(locked_staff, record.date):
            raise ValidationError("마감된 월입니다.")

        if not record.end_time:
            raise ValidationError("퇴근 시간이 없어 계산할 수 없습니다.")

        record.is_manually_edited = False
        record.save(recalculate_payroll=True)

        return Response(WorkRecordSerializer(record).data)

    @action(detail=True, methods=["post"])
    def start_break(self, request, pk=None):
        record_ref = self.get_object()
        self._assert_self_service_or_manager(record_ref)
        start_work_break(
            record=record_ref,
            started_at=timezone.localtime(timezone.now()),
        )

        return Response({"status": "BREAK_STARTED"})

    @action(detail=True, methods=["post"])
    def end_break(self, request, pk=None):
        record_ref = self.get_object()
        self._assert_self_service_or_manager(record_ref)
        end_work_break(
            record=record_ref,
            ended_at=timezone.localtime(timezone.now()),
        )

        return Response({"status": "BREAK_ENDED"})

    @action(detail=True, methods=["post"])
    def end_work(self, request, pk=None):
        record_ref = self.get_object()
        self._assert_self_service_or_manager(record_ref)
        request_serializer = StaffWorkEndRequestSerializer(data=request.data)
        request_serializer.is_valid(raise_exception=True)
        record = end_work_record(
            record=record_ref,
            ended_at=timezone.localtime(timezone.now()),
            meal_minutes=request_serializer.validated_data.get("meal_minutes"),
            adjustment_amount=request_serializer.validated_data.get(
                "adjustment_amount"
            ),
            allow_adjustment=can_manage_payroll(request.user, record_ref.tenant),
        )

        return Response(WorkRecordSerializer(record).data)
