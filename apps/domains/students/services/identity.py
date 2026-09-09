from __future__ import annotations

from typing import Any, Literal

from django.contrib.auth import get_user_model
from django.db.models import Q

from apps.core.models.user import user_display_username, user_internal_username
from apps.domains.students.models import Student
from apps.domains.students.ps_number import _generate_unique_ps_number
from apps.support.students.lifecycle_dependencies import inventory_student_ps_metadata_exists


class StudentIdentityError(ValueError):
    def __init__(self, detail: str | dict[str, str]) -> None:
        self.detail = detail
        super().__init__(str(detail))


def phone_digits(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def normalize_student_phone(
    value: Any,
    *,
    required: bool = False,
    field_name: str = "phone",
    field_label: str = "전화번호",
) -> str | None:
    digits = phone_digits(value)
    if not digits:
        if required:
            raise StudentIdentityError({field_name: "필수입니다."})
        return None
    if len(digits) != 11 or not digits.startswith("010"):
        raise StudentIdentityError({field_name: f"{field_label}는 010XXXXXXXX 11자리여야 합니다."})
    return digits


def canonical_student_phone(*, phone: Any, parent_phone: Any) -> str | None:
    """Return the real student phone, treating an exact parent match as absent."""
    student_phone = normalize_student_phone(
        phone,
        required=False,
        field_name="phone",
        field_label="학생 전화번호",
    )
    normalized_parent_phone = normalize_student_phone(
        parent_phone,
        required=False,
        field_name="parent_phone",
        field_label="학부모 전화번호",
    )
    if student_phone and student_phone == normalized_parent_phone:
        return None
    return student_phone


def derive_student_omr_code(
    *,
    phone: Any,
    parent_phone: Any,
    current: str | None = None,
    required: bool = True,
) -> str:
    student_tail = phone_digits(phone)
    parent_tail = phone_digits(parent_phone)
    if len(student_tail) >= 8:
        return student_tail[-8:]
    if len(parent_tail) >= 8:
        return parent_tail[-8:]
    if current:
        return str(current)
    if required:
        raise StudentIdentityError({"omr_code": "학생 전화번호 또는 학부모 전화번호가 필요합니다."})
    return ""


def student_login_id_taken(
    *,
    tenant,
    display_username: str,
    exclude_student_id: int | None = None,
    exclude_user_id: int | None = None,
) -> bool:
    username = str(display_username or "").strip()
    if not username:
        return False

    student_qs = Student.objects.filter(tenant=tenant, ps_number=username)
    if exclude_student_id:
        student_qs = student_qs.exclude(pk=exclude_student_id)
    if student_qs.exists():
        return True

    if inventory_student_ps_metadata_exists(
        tenant_id=tenant.id,
        ps_number=username,
    ):
        predecessors = tuple(
            student_id
            for student_id, ps_number in Student.objects.filter(
                tenant=tenant,
                deleted_at__isnull=False,
                ps_number__endswith=f"_{username}",
            ).values_list("id", "ps_number")
            if ps_number == f"_del_{student_id}_{username}"
        )
        if len(predecessors) != 1:
            return True

    user_qs = get_user_model().objects.filter(
        username=user_internal_username(tenant, username),
    )
    if exclude_user_id:
        user_qs = user_qs.exclude(pk=exclude_user_id)
    if user_qs.exists():
        return True

    active_login_users = (
        get_user_model()
        .objects.filter(
            tenant_memberships__tenant=tenant,
            tenant_memberships__is_active=True,
        )
        .filter(Q(username=username) | Q(username__endswith=f"_{username}"))
        .distinct()
    )
    if exclude_user_id:
        active_login_users = active_login_users.exclude(pk=exclude_user_id)
    return any(
        user_display_username(user) == username
        for user in active_login_users.only("id", "username")
    )


RequestedConflictPolicy = Literal["error", "fallback"]


def resolve_student_login_id(
    *,
    tenant,
    requested_id: Any = "",
    phone: Any = "",
    requested_conflict: RequestedConflictPolicy = "error",
    exclude_student_id: int | None = None,
    exclude_user_id: int | None = None,
) -> str:
    requested = str(requested_id or "").strip()
    if requested:
        if not student_login_id_taken(
            tenant=tenant,
            display_username=requested,
            exclude_student_id=exclude_student_id,
            exclude_user_id=exclude_user_id,
        ):
            return requested
        if requested_conflict == "error":
            raise StudentIdentityError({"ps_number": "이미 사용 중인 아이디입니다."})

    phone_id = normalize_student_phone(phone, required=False, field_name="phone", field_label="학생 전화번호")
    if phone_id and not student_login_id_taken(
        tenant=tenant,
        display_username=phone_id,
        exclude_student_id=exclude_student_id,
        exclude_user_id=exclude_user_id,
    ):
        return phone_id

    try:
        return _generate_unique_ps_number(tenant=tenant)
    except ValueError as exc:
        raise StudentIdentityError({"ps_number": str(exc)}) from exc
