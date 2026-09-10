from __future__ import annotations

from datetime import datetime, timedelta

from django.db import IntegrityError, transaction
from rest_framework.exceptions import APIException
from rest_framework.exceptions import PermissionDenied
from rest_framework.exceptions import ValidationError

from academy.adapters.db.django import repositories_staffs as staff_repo


class OpenWorkRecordConflict(APIException):
    status_code = 409
    default_detail = "이미 근무 중입니다."
    default_code = "open_work_record_exists"


def has_open_work_record_conflict(*, staff, exclude_record_id: int | None = None) -> bool:
    """Re-check the invariant after a failed transaction has rolled back."""
    return staff_repo.work_record_open_exists(
        staff,
        exclude_record_id=exclude_record_id,
    )


def start_work_record(
    *,
    staff,
    work_type_id: int,
    date,
    start_time,
    require_assignment: bool = False,
):
    """Create one open clock-in record or fail with a deterministic conflict."""
    try:
        with transaction.atomic():
            # Phase-A rolling-deploy invariant: every sanctioned writer locks
            # the canonical Staff row before checking/creating an open record.
            # This remains correct before the phase-B partial unique constraint
            # is installed.
            locked_staff = staff_repo.staff_get_for_update(
                staff.tenant_id,
                staff.pk,
            )
            if not locked_staff.is_active:
                raise ValidationError(
                    "퇴사 처리된 직원은 근무를 시작할 수 없습니다."
                )
            if not staff_repo.work_type_get_active_for_update(
                locked_staff.tenant_id,
                work_type_id,
            ):
                raise ValidationError(
                    {"work_type": "선택한 근무 유형이 유효하지 않습니다."}
                )
            if (
                require_assignment
                and not staff_repo.staff_work_type_assignment_exists(
                    locked_staff,
                    work_type_id,
                )
            ):
                raise ValidationError(
                    {"work_type": "본인에게 배정된 근무 유형만 선택할 수 있습니다."}
                )
            if staff_repo.is_month_locked(
                locked_staff,
                date.year,
                date.month,
            ):
                raise ValidationError(
                    "마감된 월입니다. 근무기록을 추가할 수 없습니다."
                )
            if staff_repo.work_record_filter_open(locked_staff).exists():
                raise OpenWorkRecordConflict()
            return staff_repo.work_record_create_start(
                staff=locked_staff,
                work_type_id=work_type_id,
                date=date,
                start_time=start_time,
            )
    except IntegrityError as exc:
        # Preserve the original error unless the database now contains a
        # conflicting open record (including after the phase-B constraint).
        if has_open_work_record_conflict(staff=staff):
            raise OpenWorkRecordConflict() from exc
        raise


def _locked_open_record(record):
    locked_record = staff_repo.work_record_get_for_update(
        record.tenant_id,
        record.id,
    )
    locked_staff = staff_repo.staff_get_for_update(
        locked_record.tenant_id,
        locked_record.staff_id,
    )
    if staff_repo.is_month_locked(
        locked_staff,
        locked_record.date.year,
        locked_record.date.month,
    ):
        raise ValidationError("마감된 월입니다.")
    if locked_record.end_time:
        raise ValidationError("이미 종료된 근무입니다.")
    return locked_record


def _finish_active_break(record, *, ended_at):
    if not record.current_break_started_at:
        return
    break_seconds = max(
        0,
        int((ended_at - record.current_break_started_at).total_seconds()),
    )
    record.break_total_seconds += break_seconds
    record.break_minutes = record.break_total_seconds // 60
    record.current_break_started_at = None


def start_work_break(*, record, started_at):
    """Start a break on the canonical open work record."""
    with transaction.atomic():
        locked_record = _locked_open_record(record)
        if locked_record.current_break_started_at:
            raise ValidationError("이미 휴게 중입니다.")
        locked_record.current_break_started_at = started_at
        locked_record.save(update_fields=["current_break_started_at"])
        return locked_record


def end_work_break(*, record, ended_at):
    """Accumulate the active break using second precision and resume work."""
    with transaction.atomic():
        locked_record = _locked_open_record(record)
        if not locked_record.current_break_started_at:
            raise ValidationError("휴게 중이 아닙니다.")
        _finish_active_break(locked_record, ended_at=ended_at)
        locked_record.save(
            update_fields=[
                "break_minutes",
                "break_total_seconds",
                "current_break_started_at",
            ]
        )
        return locked_record


def end_work_record(
    *,
    record,
    ended_at,
    meal_minutes: int | None = None,
    adjustment_amount: int | None = None,
    allow_adjustment: bool = False,
):
    """Close one work session and persist its server-owned payroll result."""
    with transaction.atomic():
        locked_record = _locked_open_record(record)
        _finish_active_break(locked_record, ended_at=ended_at)

        if meal_minutes is not None:
            if meal_minutes < 0:
                raise ValidationError("meal_minutes는 0 이상의 정수여야 합니다.")
            locked_record.meal_minutes = meal_minutes

        if adjustment_amount is not None:
            if not allow_adjustment:
                raise PermissionDenied("급여 조정액은 관리자만 입력할 수 있습니다.")
            locked_record.adjustment_amount = adjustment_amount

        locked_record.end_time = ended_at.time()
        start_dt = datetime.combine(
            locked_record.date,
            locked_record.start_time,
        )
        end_dt = datetime.combine(
            locked_record.date,
            locked_record.end_time,
        )
        if end_dt <= start_dt:
            end_dt += timedelta(days=1)
        elapsed_minutes = int((end_dt - start_dt).total_seconds() // 60)
        if locked_record.break_minutes + locked_record.meal_minutes >= elapsed_minutes:
            raise ValidationError(
                {
                    "meal_minutes": (
                        "휴게·식사시간 합계는 전체 근무시간보다 짧아야 합니다."
                    )
                }
            )

        # WorkRecord.save() freezes the resolved wage and calculates the
        # canonical work_hours/amount when end_time is present.
        locked_record.save(recalculate_payroll=True)
        return locked_record
