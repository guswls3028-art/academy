# PATH: apps/domains/students/services/profile.py
"""
Canonical student profile/identity write helpers.

Phase 1 keeps deployed URLs and serializers stable while routing profile
invariants through this service: phone normalization, OMR derivation, parent
relink, and PS/login identity sync.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from django.db import transaction

from apps.core.models import Program
from apps.core.models.user import user_display_username
from apps.domains.students.models import Student, StudentInventoryNamespaceConflict
from apps.domains.students.services.identity import (
    StudentIdentityError,
    canonical_student_phone,
    derive_student_omr_code,
    normalize_student_phone,
    student_login_id_taken,
)
from apps.domains.students.services.school import (
    ALL_SCHOOL_TYPES,
    get_valid_grades,
    get_valid_school_types,
    is_valid_grade,
)
from apps.support.students.lifecycle_dependencies import ensure_parent_account_for_student
from apps.support.students.namespace_lock import (
    lock_student_creation_tenant_reference,
)


class StudentProfileUpdateError(ValueError):
    def __init__(self, detail: str | dict[str, str]):
        self.detail = detail
        super().__init__(str(detail))


@dataclass(frozen=True)
class StudentProfileUpdateResult:
    student: Student
    changed_fields: tuple[str, ...]
    parent_relinked: bool = False
    parent_password_for_notice: str = ""
    parent_user_created: bool = False


PHONE_FIELDS = {"phone", "parent_phone"}
PROFILE_FIELDS = {
    "name",
    "phone",
    "parent_phone",
    "gender",
    "address",
    "school_type",
    "elementary_school",
    "high_school",
    "middle_school",
    "origin_middle_school",
    "grade",
    "high_school_class",
    "major",
    "memo",
    "custom_fields",
    "is_managed",
    "uses_identifier",
}
STRING_LIMITS = {
    "name": 100,
    "phone": 20,
    "parent_phone": 20,
    "gender": 10,
    "address": 255,
    "elementary_school": 100,
    "high_school": 100,
    "middle_school": 100,
    "origin_middle_school": 100,
    "high_school_class": 100,
    "major": 50,
    "memo": None,
}


def normalize_phone(value: Any, *, required: bool = False, field_label: str = "전화번호") -> str | None:
    field_name = "parent_phone" if "학부모" in field_label else "phone"
    try:
        return normalize_student_phone(
            value,
            required=required,
            field_name=field_name,
            field_label=field_label,
        )
    except StudentIdentityError as exc:
        raise StudentProfileUpdateError(exc.detail) from exc


def derive_omr_code(*, phone: str | None, parent_phone: str | None, current: str) -> str:
    try:
        return derive_student_omr_code(
            phone=phone,
            parent_phone=parent_phone,
            current=current,
            required=False,
        )
    except StudentIdentityError as exc:
        raise StudentProfileUpdateError(exc.detail) from exc


def _validate_school(tenant, *, school_type: str | None, grade: Any) -> int | None:
    if school_type and school_type not in ALL_SCHOOL_TYPES:
        raise StudentProfileUpdateError(
            {"detail": f"school_type은 {sorted(ALL_SCHOOL_TYPES)} 중 하나여야 합니다."}
        )

    program = Program.objects.filter(tenant=tenant).first()
    slm = program.feature_flags.get("school_level_mode") if program and program.feature_flags else None
    valid_types = get_valid_school_types(slm)
    if school_type and school_type not in valid_types:
        labels = {"ELEMENTARY": "초등", "MIDDLE": "중등", "HIGH": "고등"}
        allowed = ", ".join(labels.get(t, t) for t in sorted(valid_types))
        raise StudentProfileUpdateError({"school_type": f"이 학원에서는 {allowed}만 선택할 수 있습니다."})

    if grade is None or grade == "":
        return None
    try:
        grade_val = int(grade)
    except (TypeError, ValueError):
        raise StudentProfileUpdateError({"detail": "grade는 정수여야 합니다."})

    if school_type:
        allowed_grades = get_valid_grades(school_type)
        if grade_val not in allowed_grades:
            raise StudentProfileUpdateError(
                {"detail": f"grade는 {sorted(allowed_grades)} 중 하나여야 합니다."}
            )
        if not is_valid_grade(school_type, grade_val):
            raise StudentProfileUpdateError({"grade": "허용되지 않는 학년입니다."})
    return grade_val


def _normalize_string_field(field: str, value: Any) -> Any:
    if value is None:
        return None
    if field not in STRING_LIMITS:
        return value
    if not isinstance(value, str):
        raise StudentProfileUpdateError({"detail": f"{field}은(는) 문자열이어야 합니다."})
    limit = STRING_LIMITS[field]
    value = value.strip()
    return value[:limit] if limit else (value or None)


def _validate_identity(student: Student, tenant, display_username: str) -> str:
    username = str(display_username or "").strip()
    if not username:
        return ""
    if student_login_id_taken(
        tenant=tenant,
        display_username=username,
        exclude_student_id=student.pk,
        exclude_user_id=student.user_id,
    ):
        raise StudentProfileUpdateError({"detail": "이미 사용 중인 아이디입니다."})
    return username


def _append_unique(fields: list[str], items: Iterable[str]) -> None:
    for item in items:
        if item not in fields:
            fields.append(item)


def _update_student_profile_after_tenant_gate(
    *,
    student: Student,
    tenant,
    data: dict[str, Any],
    identity_field: str | None = None,
    strict_school_validation: bool = True,
    ignore_blank_name: bool = False,
) -> StudentProfileUpdateResult:
    """
    Update student profile fields through one invariant path.

    `data` may be serializer-validated admin data or raw student-app PATCH data.
    The caller keeps response-shape and permission responsibility.
    """
    if tenant is None:
        raise StudentProfileUpdateError({"detail": "Tenant가 resolve되지 않았습니다."})
    if student.tenant_id != tenant.id:
        raise StudentProfileUpdateError({"detail": "학생이 현재 테넌트에 속하지 않습니다."})

    changed: list[str] = []
    data = dict(data)
    old_phone = student.phone or ""
    old_parent_phone = student.parent_phone or ""
    old_ps_number = student.ps_number or ""
    old_uses_identifier = bool(student.uses_identifier)

    if any(field in data for field in PHONE_FIELDS):
        try:
            data["phone"] = canonical_student_phone(
                phone=data.get("phone", student.phone),
                parent_phone=data.get("parent_phone", student.parent_phone),
            )
        except StudentIdentityError as exc:
            raise StudentProfileUpdateError(exc.detail) from exc
        data["uses_identifier"] = not bool(data["phone"])

    requested_identity_changed = False

    if identity_field and identity_field in data:
        new_username = _validate_identity(student, tenant, data.get(identity_field))
        if new_username and student.user_id and new_username != user_display_username(student.user):
            student.ps_number = new_username
            _append_unique(changed, ["ps_number"])
            requested_identity_changed = True

    for field in PROFILE_FIELDS:
        if field not in data:
            continue
        value = data[field]
        if field == "custom_fields":
            if not isinstance(value, dict):
                raise StudentProfileUpdateError(
                    {"custom_fields": "객체 형식이어야 합니다."}
                )
            merged = dict(student.custom_fields or {})
            for key, custom_value in value.items():
                if custom_value is None:
                    merged.pop(str(key), None)
                else:
                    merged[str(key)] = custom_value
            value = merged
        elif field in PHONE_FIELDS:
            value = normalize_phone(value, required=(field == "parent_phone"), field_label=("학부모 전화번호" if field == "parent_phone" else "전화번호"))
            if field == "parent_phone" and not value:
                continue
        elif field == "gender":
            value = (str(value or "").strip().upper()[:1] or None)
            value = value if value in ("M", "F") else None
        elif field == "grade":
            school_type = data.get("school_type", student.school_type)
            if strict_school_validation:
                value = _validate_school(tenant, school_type=school_type, grade=value)
            elif value is not None and value != "":
                value = int(value)
            else:
                value = None
        elif field == "school_type" and strict_school_validation:
            _validate_school(tenant, school_type=str(value or "").strip().upper(), grade=data.get("grade", student.grade))
            value = str(value or "").strip().upper()
        elif field == "name" and ignore_blank_name and not str(value or "").strip():
            continue
        else:
            value = _normalize_string_field(field, value)

        if getattr(student, field) != value:
            setattr(student, field, value)
            changed.append(field)

    phone_identity_owned = bool(
        (old_uses_identifier and not old_phone)
        or (old_phone and old_ps_number == old_phone)
    )
    if (
        identity_field
        and "phone" in data
        and student.phone
        and student.phone != old_phone
        and phone_identity_owned
        and not requested_identity_changed
    ):
        student.ps_number = _validate_identity(student, tenant, student.phone)
        _append_unique(changed, ["ps_number"])

    if any(field in data for field in PHONE_FIELDS):
        new_omr = derive_omr_code(
            phone=student.phone,
            parent_phone=student.parent_phone,
            current=student.omr_code,
        )
        if new_omr != student.omr_code:
            student.omr_code = new_omr
            changed.append("omr_code")

    if changed:
        try:
            student.save(update_fields=changed)
        except StudentInventoryNamespaceConflict as exc:
            raise StudentProfileUpdateError(
                {"detail": "학생 아이디의 이전 저장자료 소유권을 확인한 뒤 다시 시도해 주세요."}
            ) from exc

    if "phone" in data and student.user_id and student.user.phone != student.phone:
        student.user.phone = student.phone
        student.user.save(update_fields=["phone"])

    parent_relinked = False
    parent_password_for_notice = ""
    parent_user_created = False
    if "parent_phone" in data:
        new_parent_phone = student.parent_phone or ""
        if new_parent_phone and new_parent_phone != old_parent_phone:
            parent_result = ensure_parent_account_for_student(
                tenant=tenant,
                parent_phone=new_parent_phone,
                student_name=student.name,
            )
            parent = parent_result.parent
            parent_password_for_notice = parent_result.password_for_notice
            parent_user_created = parent_result.user_created
            if parent and student.parent_id != parent.id:
                student.parent = parent
                student.save(update_fields=["parent_id", "updated_at"])
                parent_relinked = True

    return StudentProfileUpdateResult(
        student=student,
        changed_fields=tuple(changed),
        parent_relinked=parent_relinked,
        parent_password_for_notice=parent_password_for_notice,
        parent_user_created=parent_user_created,
    )


def update_student_profile(
    *,
    student: Student,
    tenant,
    data: dict[str, Any],
    identity_field: str | None = None,
    strict_school_validation: bool = True,
    ignore_blank_name: bool = False,
) -> StudentProfileUpdateResult:
    """Update one profile under the tenant-first lifecycle lock order."""
    if tenant is None:
        raise StudentProfileUpdateError({"detail": "Tenant가 resolve되지 않았습니다."})
    if student.tenant_id != tenant.id:
        raise StudentProfileUpdateError({"detail": "학생이 현재 테넌트에 속하지 않습니다."})

    with transaction.atomic():
        lock_student_creation_tenant_reference(tenant_id=tenant.id)
        if not Student.objects.filter(pk=student.pk, tenant=tenant).exists():
            raise StudentProfileUpdateError({"detail": "학생을 찾을 수 없습니다."})
        return _update_student_profile_after_tenant_gate(
            student=student,
            tenant=tenant,
            data=data,
            identity_field=identity_field,
            strict_school_validation=strict_school_validation,
            ignore_blank_name=ignore_blank_name,
        )
