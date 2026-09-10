# PATH: apps/domains/students/services/import_students.py
"""Canonical student import row orchestration.

Excel/student import and lecture-enrollment import both need the same student
identity policy: resolve a unique same-tenant student phone first, then an
exact opaque name + parent phone, restore matching deleted students, or create
the account graph through create_student_account().
HTTP views and workers keep their own transport contracts; this module owns the
row-level student decision.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Literal

from django.db import transaction

from academy.adapters.db.django import repositories_core as core_repo
from academy.adapters.db.django import repositories_enrollment as enroll_repo
from academy.adapters.db.django import repositories_students as student_repo
from apps.support.students.lifecycle_dependencies import ensure_parent_account_for_student

from .account_notifications import send_parent_account_credentials_notice
from .creation import create_student_account
from .custom_fields import (
    StudentCustomFieldError,
    active_custom_field_definitions,
    custom_field_values_from_import_row,
)
from .identity import (
    StudentIdentityError,
    canonical_student_phone,
    derive_student_omr_code,
    normalize_student_phone,
    phone_digits,
    resolve_student_login_id,
)
from .import_passwords import StudentImportPasswordError, build_student_import_password_policy
from .lifecycle import permanently_delete_students, restore_student
from .school import get_valid_school_types, is_valid_grade, normalize_school_from_name

logger = logging.getLogger(__name__)

StudentImportIdentityPolicy = Literal["phone_if_available", "random"]


class StudentImportRowError(ValueError):
    def __init__(self, detail: str, *, conflict_student_id: int | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.conflict_student_id = conflict_student_id


@dataclass(frozen=True)
class StudentImportRowResolution:
    student: Any
    created: bool
    restored: bool
    duplicate: bool
    parent_phone: str
    parent_password_for_notice: str


@dataclass(frozen=True)
class _NormalizedImportRow:
    name: str
    parent_phone: str
    phone: str | None
    student_data: dict[str, Any]
    restore_data: dict[str, Any]


def _digits(value: Any) -> str:
    return phone_digits(value)


def _valid_student_phone(value: Any) -> str | None:
    if not str(value or "").strip():
        return None
    try:
        return normalize_student_phone(
            value,
            required=False,
            field_name="phone",
            field_label="학생 전화번호",
        )
    except StudentIdentityError as exc:
        detail = exc.detail.get("phone", str(exc.detail)) if isinstance(exc.detail, dict) else exc.detail
        raise StudentImportRowError(detail) from exc


def _valid_parent_phone(value: Any) -> str:
    try:
        phone = normalize_student_phone(
            value,
            required=True,
            field_name="parent_phone",
            field_label="학부모 전화번호",
        )
    except StudentIdentityError as exc:
        detail = exc.detail.get("parent_phone", str(exc.detail)) if isinstance(exc.detail, dict) else exc.detail
        raise StudentImportRowError(detail) from exc
    if not phone:
        raise StudentImportRowError("학부모 전화번호는 010XXXXXXXX 11자리여야 합니다.")
    return phone


def _grade_value(value: Any, school_type: str) -> int | None:
    if value is None or value == "":
        return None
    try:
        grade = int(value)
    except (TypeError, ValueError) as exc:
        raise StudentImportRowError("학년은 숫자여야 합니다.") from exc
    if not is_valid_grade(school_type, grade):
        raise StudentImportRowError(f"{school_type} 학생의 학년이 허용 범위를 벗어났습니다.")
    return grade


def student_import_valid_school_types(tenant) -> frozenset[str]:
    program = core_repo.program_get_by_tenant_only_feature_flags(tenant)
    school_level_mode = (
        program.feature_flags.get("school_level_mode")
        if program and program.feature_flags
        else None
    )
    return frozenset(get_valid_school_types(school_level_mode))


def _validate_school_level(
    *,
    valid_school_types: frozenset[str],
    school_type: str,
    grade: int | None,
) -> None:
    if school_type not in valid_school_types:
        labels = {"ELEMENTARY": "초등", "MIDDLE": "중등", "HIGH": "고등"}
        allowed = ", ".join(labels.get(t, t) for t in sorted(valid_school_types))
        raise StudentImportRowError(f"이 학원에서는 {allowed} 학생만 등록할 수 있습니다.")
    if grade is not None and not is_valid_grade(school_type, grade):
        raise StudentImportRowError("허용되지 않는 학년입니다.")


def _normalize_import_row(
    *,
    tenant,
    raw: dict[str, Any],
    valid_school_types: frozenset[str],
    custom_field_definitions=None,
) -> _NormalizedImportRow:
    name = str(raw.get("name") or "").strip()
    if not name:
        raise StudentImportRowError("학생 이름은 필수입니다.")

    parent_phone = _valid_parent_phone(raw.get("parent_phone") or raw.get("parentPhone"))
    phone = _valid_student_phone(raw.get("phone") or raw.get("studentPhone"))
    phone = canonical_student_phone(phone=phone, parent_phone=parent_phone)

    school_val = str(raw.get("school") or "").strip() or None
    school_type, elementary_school, high_school, middle_school = normalize_school_from_name(
        school_val,
        raw.get("school_type"),
    )
    grade = _grade_value(raw.get("grade"), school_type)
    _validate_school_level(
        valid_school_types=valid_school_types,
        school_type=school_type,
        grade=grade,
    )

    high_school_class = (
        str(raw.get("high_school_class") or raw.get("schoolClass") or "").strip() or None
        if school_type == "HIGH"
        else None
    )
    major = (
        str(raw.get("major") or "").strip() or None
        if school_type == "HIGH"
        else None
    )
    gender = str(raw.get("gender") or "").strip().upper()[:1] or None
    if gender not in ("M", "F", None):
        gender = None
    try:
        custom_fields = custom_field_values_from_import_row(
            tenant=tenant,
            row=raw,
            definitions=custom_field_definitions,
        )
    except StudentCustomFieldError as exc:
        if isinstance(exc.detail, dict) and exc.detail:
            detail = str(next(iter(exc.detail.values())))
        else:
            detail = str(exc.detail)
        raise StudentImportRowError(detail) from exc

    base_profile = {
        "name": name,
        "phone": phone,
        "parent_phone": parent_phone,
        "uses_identifier": bool(raw.get("uses_identifier", False)) or not bool(phone),
        "gender": gender,
        "school_type": school_type,
        "elementary_school": elementary_school,
        "high_school": high_school,
        "middle_school": middle_school,
        "high_school_class": high_school_class,
        "major": major,
        "grade": grade,
        "memo": str(raw.get("memo") or "").strip() or None,
        "custom_fields": custom_fields,
        "is_managed": raw.get("is_managed", True),
    }
    restore_data = {
        **base_profile,
        "school": school_val,
        "schoolClass": high_school_class,
    }
    return _NormalizedImportRow(
        name=name,
        parent_phone=parent_phone,
        phone=phone,
        student_data=base_profile,
        restore_data=restore_data,
    )


def _choose_ps_number(
    *,
    tenant,
    phone: str | None,
    identity_policy: StudentImportIdentityPolicy,
    requested_id: Any = "",
) -> str:
    try:
        return resolve_student_login_id(
            tenant=tenant,
            requested_id=requested_id,
            phone=phone if identity_policy == "phone_if_available" else "",
        )
    except StudentIdentityError as exc:
        detail = exc.detail.get("ps_number", str(exc.detail)) if isinstance(exc.detail, dict) else exc.detail
        raise StudentImportRowError(detail) from exc


def _unique_import_candidate(queryset, *, ambiguous_detail: str):
    candidates = list(queryset.order_by("id")[:2])
    if len(candidates) > 1:
        raise StudentImportRowError(ambiguous_detail)
    return candidates[0] if candidates else None


def resolve_student_import_row(
    tenant,
    row: dict[str, Any],
    initial_password: str,
    *,
    identity_policy: StudentImportIdentityPolicy = "phone_if_available",
    valid_school_types: frozenset[str] | None = None,
    custom_field_definitions=None,
    source_job_id: str = "",
) -> StudentImportRowResolution:
    """Resolve one imported row to an active student in one tenant."""
    initial_password = (initial_password or "").strip()

    normalized = _normalize_import_row(
        tenant=tenant,
        raw=row,
        valid_school_types=valid_school_types or student_import_valid_school_types(tenant),
        custom_field_definitions=custom_field_definitions,
    )

    tenant_students = student_repo.student_filter_tenant(tenant)

    existing = None
    if normalized.phone:
        existing = _unique_import_candidate(
            tenant_students.filter(
                deleted_at__isnull=True,
                phone=normalized.phone,
            ),
            ambiguous_detail=(
                "학생 전화번호가 같은 활성 학생이 여러 명입니다. "
                "학생 목록에서 중복을 정리한 뒤 다시 등록해 주세요."
            ),
        )
    if existing:
        return StudentImportRowResolution(
            student=existing,
            created=False,
            restored=False,
            duplicate=True,
            parent_phone=normalized.parent_phone,
            parent_password_for_notice="",
        )

    existing = _unique_import_candidate(
        tenant_students.filter(
            deleted_at__isnull=True,
            name=normalized.name,
            parent_phone=normalized.parent_phone,
        ),
        ambiguous_detail=(
            "이름과 학부모 전화번호가 같은 활성 학생이 여러 명입니다. "
            "학생 목록에서 중복을 정리한 뒤 다시 등록해 주세요."
        ),
    )
    if existing:
        return StudentImportRowResolution(
            student=existing,
            created=False,
            restored=False,
            duplicate=True,
            parent_phone=normalized.parent_phone,
            parent_password_for_notice="",
        )

    deleted_student = _unique_import_candidate(
        tenant_students.filter(
            deleted_at__isnull=False,
            name=normalized.name,
            parent_phone=normalized.parent_phone,
        ),
        ambiguous_detail=(
            "이름과 학부모 전화번호가 같은 삭제 학생이 여러 명입니다. "
            "삭제된 학생 목록에서 중복을 정리한 뒤 다시 등록해 주세요."
        ),
    )
    if deleted_student:
        restored_result = restore_student(
            deleted_student,
            tenant=tenant,
            profile_data=normalized.restore_data,
        )
        return StudentImportRowResolution(
            student=restored_result.student,
            created=False,
            restored=True,
            duplicate=False,
            parent_phone=normalized.parent_phone,
            parent_password_for_notice="",
        )

    ps_number = _choose_ps_number(
        tenant=tenant,
        phone=normalized.phone,
        identity_policy=identity_policy,
        requested_id=row.get("ps_number") or row.get("psNumber") or "",
    )
    try:
        omr_code = derive_student_omr_code(
            phone=normalized.phone,
            parent_phone=normalized.parent_phone,
        )
    except StudentIdentityError as exc:
        detail = exc.detail.get("omr_code", str(exc.detail)) if isinstance(exc.detail, dict) else exc.detail
        raise StudentImportRowError(detail) from exc
    student_data = {
        **normalized.student_data,
        "ps_number": ps_number,
        "omr_code": omr_code,
    }
    if len(initial_password) < 4:
        raise StudentImportRowError("신규 학생 초기 비밀번호는 4자 이상 입력해 주세요.")

    with transaction.atomic():
        if normalized.phone:
            conflict_deleted = _unique_import_candidate(
                tenant_students.filter(
                    deleted_at__isnull=False,
                    phone=normalized.phone,
                ),
                ambiguous_detail=(
                    "학생 전화번호가 같은 삭제 학생이 여러 명입니다. "
                    "삭제된 학생 목록에서 중복을 정리한 뒤 다시 등록해 주세요."
                ),
            )
            if conflict_deleted:
                raise StudentImportRowError(
                    "삭제된 학생과 전화번호 충돌. 복원 또는 삭제 후 재등록을 선택하세요.",
                    conflict_student_id=conflict_deleted.id,
                )
            if student_repo.user_filter_phone_active(normalized.phone, tenant=tenant).exists():
                raise StudentImportRowError("이미 사용 중인 전화번호입니다.")
        if student_repo.student_filter_tenant_ps_number(tenant, ps_number).exists():
            raise StudentImportRowError("이미 사용 중인 PS 번호입니다.")

        created = create_student_account(
            tenant=tenant,
            password=initial_password,
            student_data=student_data,
            must_change_password=True,
            account_notice_origin_type=("excel_import" if source_job_id else ""),
            account_notice_origin_id=source_job_id,
        )

    return StudentImportRowResolution(
        student=created.student,
        created=True,
        restored=False,
        duplicate=False,
        parent_phone=created.parent_phone,
        parent_password_for_notice=created.parent_password_for_notice,
    )


def import_students_from_rows(
    *,
    tenant_id: int,
    students_data: list[dict],
    initial_password: str,
    password_mode: str = "fixed",
    on_row_progress: Callable[[int, int], None] | None = None,
    source_job_id: str = "",
) -> dict:
    """
    Import parsed student rows without lecture enrollment.

    Returns the existing worker contract:
    {
      "created": int,
      "created_rows": [{"row", "name", "student_id"}],
      "failed": [{"row", "name", "error", "conflict_student_id"?}],
      "duplicates": [{"row", "name", "student_id"}],
      "restored": [{"row", "name", "student_id"}],
      "total": int,
      "processed_by": "worker",
    }
    """
    tenant = enroll_repo.get_tenant_by_id(tenant_id)
    if not tenant:
        raise ValueError("tenant_id not found")

    password_policy = build_student_import_password_policy(
        password_mode=password_mode,
        initial_password=initial_password,
    )
    created_students: list[Any] = []
    created_rows: list[dict[str, Any]] = []
    random_credentials: list[dict[str, str]] = []
    failed: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    restored: list[dict[str, Any]] = []
    total = len(students_data)
    skipped_empty = 0
    valid_school_types = student_import_valid_school_types(tenant)
    custom_field_definitions = active_custom_field_definitions(tenant)

    for row_index, raw in enumerate(students_data, start=1):
        if on_row_progress and total > 0:
            on_row_progress(row_index, total)

        row = dict(raw) if isinstance(raw, dict) else {}
        source_row = row.get("_excel_row")
        display_row = source_row if isinstance(source_row, int) and source_row > 0 else row_index
        raw_name = str(row.get("name") or "").strip()
        display_name = raw_name or "(이름 없음)"
        parent_phone = _digits(row.get("parent_phone") or row.get("parentPhone"))
        if not raw_name and not parent_phone:
            skipped_empty += 1
            continue

        try:
            row_password = password_policy.password_for_row(row)
            resolved = resolve_student_import_row(
                tenant,
                row,
                row_password,
                identity_policy="phone_if_available",
                valid_school_types=valid_school_types,
                custom_field_definitions=custom_field_definitions,
                source_job_id=source_job_id,
            )
        except (StudentImportRowError, StudentImportPasswordError) as exc:
            error_detail = exc.detail if isinstance(exc, StudentImportRowError) else str(exc)
            reason_code = "password_policy" if isinstance(exc, StudentImportPasswordError) else "invalid_row"
            if error_detail == "이미 사용 중인 전화번호입니다.":
                reason_code = "phone_in_use"
            elif error_detail == "이미 사용 중인 PS 번호입니다.":
                reason_code = "ps_number_in_use"
            failed.append({
                "row": display_row,
                "name": display_name,
                "error": error_detail,
                "reason_code": reason_code,
                "conflict_student_id": (
                    exc.conflict_student_id if isinstance(exc, StudentImportRowError) else None
                ),
            })
            continue
        except Exception as exc:
            logger.warning(
                "import_students_from_rows row=%s name=%r: %s",
                row_index,
                display_name,
                exc,
                exc_info=True,
            )
            failed.append({
                "row": display_row,
                "name": display_name,
                "error": "처리 중 오류가 발생했습니다. 입력값을 확인한 뒤 다시 시도해 주세요.",
                "reason_code": "processing_error",
                "conflict_student_id": None,
            })
            continue

        if resolved.created:
            created_students.append(resolved.student)
            created_rows.append({
                "row": display_row,
                "name": display_name,
                "student_id": resolved.student.id,
            })
            if password_policy.mode == "random":
                random_credentials.append({
                    "name": resolved.student.name or display_name,
                    "login_id": resolved.student.ps_number or "",
                    "password": row_password,
                })
        elif resolved.restored:
            restored.append({
                "row": display_row,
                "name": display_name,
                "student_id": resolved.student.id,
            })
        elif resolved.duplicate:
            duplicates.append({
                "row": display_row,
                "name": display_name,
                "student_id": resolved.student.id,
            })

    if not created_students and not failed and not duplicates and not restored and total > 0:
        logger.error(
            "[student_import] ALL students skipped: total=%s skipped_empty=%s",
            total,
            skipped_empty,
        )
        raise ValueError(
            f"등록할 수 있는 학생이 없습니다. "
            f"전체 {total}행 중 {skipped_empty}행이 이름·전화 모두 비어 건너뜀. "
            f"이름·학부모 전화번호(010 11자리)를 확인해 주세요."
        )

    result = {
        "created": len(created_students),
        "created_rows": created_rows,
        "failed": failed,
        "duplicates": duplicates,
        "restored": restored,
        "total": total,
        "processed_by": "worker",
    }
    if random_credentials:
        result["credentials"] = random_credentials
    return result


def resolve_student_import_conflicts(
    *,
    tenant,
    resolutions: list[dict],
    initial_password: str,
) -> dict:
    """
    Resolve deleted-student import conflicts through the same row policy.

    Existing HTTP contract:
    {
      "created": int,
      "restored": int,
      "failed": [{"row", "name", "error", "conflict_student_id"?}],
    }
    """
    initial_password = (initial_password or "").strip()
    if len(initial_password) < 4:
        raise ValueError("initial_password는 4자 이상이어야 합니다.")
    if not isinstance(resolutions, (list, tuple)):
        raise ValueError("resolutions는 배열이어야 합니다.")

    created_count = 0
    restored_count = 0
    resolved_rows: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    valid_school_types = student_import_valid_school_types(tenant)
    custom_field_definitions = active_custom_field_definitions(tenant)

    for resolution in resolutions:
        item = resolution if isinstance(resolution, dict) else {}
        row = item.get("row")
        student_id = item.get("student_id")
        action = item.get("action")
        student_data = item.get("student_data") or {}
        if not isinstance(student_data, dict):
            student_data = {}
        display_name = str(student_data.get("name") or "").strip()

        if not student_id or action not in ("restore", "delete"):
            failed.append({
                "row": row,
                "name": display_name,
                "error": "잘못된 resolution",
                "conflict_student_id": None,
            })
            continue

        try:
            deleted_student = student_repo.student_filter_tenant_id_deleted_first(
                tenant,
                student_id,
            )
            if not deleted_student:
                failed.append({
                    "row": row,
                    "name": display_name,
                    "error": "삭제된 학생을 찾을 수 없습니다.",
                    "conflict_student_id": None,
                })
                continue

            if action == "restore":
                existing_school = (
                    deleted_student.elementary_school
                    or deleted_student.middle_school
                    or deleted_student.high_school
                    or ""
                )
                restoration_row = {
                    "name": deleted_student.name,
                    "parent_phone": deleted_student.parent_phone,
                    "phone": deleted_student.phone,
                    "uses_identifier": deleted_student.uses_identifier,
                    "gender": deleted_student.gender,
                    "school_type": deleted_student.school_type,
                    "school": existing_school,
                    "high_school_class": deleted_student.high_school_class,
                    "major": deleted_student.major,
                    "grade": deleted_student.grade,
                    "memo": deleted_student.memo,
                    **student_data,
                }
                normalized = _normalize_import_row(
                    tenant=tenant,
                    raw=restoration_row,
                    valid_school_types=valid_school_types,
                    custom_field_definitions=custom_field_definitions,
                )
                with transaction.atomic():
                    parent_result = ensure_parent_account_for_student(
                        tenant=tenant,
                        parent_phone=normalized.parent_phone,
                        student_name=normalized.name,
                        initial_password=initial_password,
                    )
                    restored_result = restore_student(
                        deleted_student,
                        tenant=tenant,
                        profile_data=normalized.restore_data,
                    )
                    if parent_result.credentials_initialized:
                        delivered = send_parent_account_credentials_notice(
                            student=restored_result.student,
                            parent=parent_result.parent,
                            parent_password=parent_result.password_for_notice,
                            origin_type="student_import_conflict_restore",
                            origin_id=str(row or deleted_student.id),
                        )
                        if not delivered:
                            raise StudentImportRowError(
                                "학부모 계정 안내 알림톡을 보내지 못해 복원을 취소했습니다."
                            )
                restored_count += 1
                resolved_rows.append(
                    {
                        "row": row,
                        "student_id": restored_result.student.id,
                        "state": "restored",
                    }
                )
                continue

            with transaction.atomic():
                permanently_delete_students(
                    tenant=tenant,
                    student_ids=[deleted_student.id],
                )
                resolved = resolve_student_import_row(
                    tenant,
                    student_data,
                    initial_password,
                    identity_policy="phone_if_available",
                    valid_school_types=valid_school_types,
                    custom_field_definitions=custom_field_definitions,
                )
            if resolved.created:
                created_count += 1
                resolved_rows.append(
                    {
                        "row": row,
                        "student_id": resolved.student.id,
                        "state": "created",
                    }
                )
            elif resolved.restored:
                restored_count += 1
                resolved_rows.append(
                    {
                        "row": row,
                        "student_id": resolved.student.id,
                        "state": "restored",
                    }
                )
            elif resolved.duplicate:
                failed.append({
                    "row": row,
                    "name": display_name,
                    "error": "이미 있는 학생입니다.",
                    "conflict_student_id": getattr(resolved.student, "id", None),
                })
        except StudentImportRowError as exc:
            failed.append({
                "row": row,
                "name": display_name,
                "error": exc.detail,
                "conflict_student_id": exc.conflict_student_id,
            })
        except Exception as exc:
            logger.warning(
                "resolve_student_import_conflicts row=%s name=%r: %s",
                row,
                display_name,
                exc,
                exc_info=True,
            )
            failed.append({
                "row": row,
                "name": display_name,
                "error": str(exc)[:500],
                "conflict_student_id": None,
            })

    return {
        "created": created_count,
        "restored": restored_count,
        "resolved": resolved_rows,
        "failed": failed,
    }
