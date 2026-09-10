# PATH: apps/domains/students/services/creation.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from django.db import transaction

from academy.adapters.db.django import repositories_students as student_repo
from apps.core.models import TenantMembership
from apps.core.services.login_identifier import normalize_login_identifier
from apps.support.students.lifecycle_dependencies import ensure_parent_account_for_student

from .account_notice import stage_pending_account_notice
from .identity import (
    StudentIdentityError,
    canonical_student_phone,
    derive_student_omr_code,
    phone_digits,
)


@dataclass(frozen=True)
class StudentAccountCreationResult:
    student: Any
    user: Any
    parent: Any | None
    parent_phone: str
    parent_password_for_notice: str
    parent_credentials_initialized: bool

    @property
    def parent_password_by_phone(self) -> dict[str, str]:
        if not self.parent_phone:
            return {}
        return {self.parent_phone: self.parent_password_for_notice}


def create_student_account(
    *,
    tenant,
    student_data: Mapping[str, Any],
    password: str | None = None,
    password_hash: str | None = None,
    must_change_password: bool = False,
    account_notice_student_password: str | None = None,
    account_notice_parent_password: str | None = None,
    account_notice_origin_type: str = "",
    account_notice_origin_id: str = "",
) -> StudentAccountCreationResult:
    """
    Create the canonical student account graph for one tenant.

    Owns the durable graph and encrypted first-enrollment notice staging:
    Parent ensure -> User -> Student -> TenantMembership(student) -> pending notice.

    Callers keep validation, duplicate/deleted-student policy, API response
    shape, and message dispatch so existing surfaces can migrate safely.
    """
    if password is None and password_hash is None:
        raise ValueError("password or password_hash is required")
    if password is not None and password_hash is not None:
        raise ValueError("password and password_hash are mutually exclusive")
    if password is not None and len(str(password)) < 4:
        raise ValueError("초기 비밀번호는 4자 이상이어야 합니다.")

    data = dict(student_data)
    parent_phone = str(data.get("parent_phone") or "").strip()
    name = str(data.get("name") or "").strip()
    ps_number = normalize_login_identifier(data.get("ps_number"))
    if not ps_number:
        raise ValueError("ps_number is required")
    data["ps_number"] = ps_number

    original_phone = phone_digits(data.get("phone"))
    student_phone = canonical_student_phone(
        phone=data.get("phone"),
        parent_phone=parent_phone,
    )
    shared_parent_phone = bool(original_phone and student_phone is None)
    data["phone"] = student_phone
    if shared_parent_phone:
        data["uses_identifier"] = True
        data["omr_code"] = derive_student_omr_code(
            phone=None,
            parent_phone=parent_phone,
            current=data.get("omr_code"),
        )
    normalized_parent_phone = phone_digits(parent_phone)
    if normalized_parent_phone and ps_number == normalized_parent_phone:
        raise StudentIdentityError(
            {"ps_number": "학부모 전화번호는 학생 로그인 아이디로 사용할 수 없습니다."}
        )

    with transaction.atomic():
        parent = None
        parent_password_for_notice = ""
        parent_credentials_initialized = False
        if parent_phone:
            parent_result = ensure_parent_account_for_student(
                tenant=tenant,
                parent_phone=parent_phone,
                student_name=name,
                initial_password=password,
                initial_password_hash=password_hash,
                initial_password_notice=(
                    account_notice_parent_password
                    or account_notice_student_password
                    or password
                ),
            )
            parent = parent_result.parent
            parent_password_for_notice = parent_result.password_for_notice
            parent_credentials_initialized = parent_result.credentials_initialized

        user = student_repo.user_create_user(
            username=ps_number,
            tenant=tenant,
            phone=student_phone or "",
            name=name,
        )
        if password_hash is not None:
            user.password = password_hash
        else:
            user.set_password(password)
        user.must_change_password = must_change_password
        user.save()

        student = student_repo.student_create(
            tenant=tenant,
            user=user,
            parent=parent,
            **data,
        )

        TenantMembership.ensure_active(
            tenant=tenant,
            user=user,
            role="student",
        )

        notice_student_password = account_notice_student_password or password
        if not notice_student_password:
            raise ValueError(
                "account_notice_student_password is required with password_hash"
            )
        stage_pending_account_notice(
            student=student,
            student_password=notice_student_password,
            parent_password=parent_password_for_notice or "변경되지 않음",
            origin_type=account_notice_origin_type,
            origin_id=account_notice_origin_id,
        )

    return StudentAccountCreationResult(
        student=student,
        user=user,
        parent=parent,
        parent_phone=parent_phone,
        parent_password_for_notice=parent_password_for_notice,
        parent_credentials_initialized=parent_credentials_initialized,
    )
