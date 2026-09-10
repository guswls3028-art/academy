# PATH: apps/domains/students/services/creation.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from django.db import IntegrityError, transaction

from academy.adapters.db.django import repositories_students as student_repo
from apps.core.models import TenantMembership
from apps.support.students.lifecycle_dependencies import ensure_parent_account_for_student
from apps.support.students.namespace_lock import (
    lock_student_creation_tenant_reference,
)
from apps.domains.students.models import (
    StudentInventoryNamespaceChanged,
    StudentInventoryNamespaceConflict,
)

from .account_notice import stage_pending_account_notice
from .identity import (
    StudentIdentityError,
    canonical_student_phone,
    derive_student_omr_code,
    phone_digits,
    resolve_student_login_id,
    student_login_id_taken,
)


@dataclass(frozen=True)
class StudentAccountCreationResult:
    student: Any
    user: Any
    parent: Any | None
    parent_phone: str
    parent_password_for_notice: str
    parent_user_created: bool

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

    data = dict(student_data)
    parent_phone = str(data.get("parent_phone") or "").strip()
    name = str(data.get("name") or "").strip()
    ps_number = str(data.get("ps_number") or "").strip()
    if not ps_number:
        raise ValueError("ps_number is required")

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
    if (
        student_phone is None
        and normalized_parent_phone
        and ps_number == normalized_parent_phone
    ):
        ps_number = resolve_student_login_id(tenant=tenant)
        data["ps_number"] = ps_number
        data["uses_identifier"] = True

    for attempt in range(3):
        try:
            with transaction.atomic():
                lock_student_creation_tenant_reference(tenant_id=tenant.id)
                parent = None
                parent_password_for_notice = ""
                parent_user_created = False
                if parent_phone:
                    parent_result = ensure_parent_account_for_student(
                        tenant=tenant,
                        parent_phone=parent_phone,
                        student_name=name,
                        initial_password=password,
                    )
                    parent = parent_result.parent
                    parent_password_for_notice = parent_result.password_for_notice
                    parent_user_created = parent_result.user_created

                # Reserving the unique login before Student.save's namespace lock
                # matches rename/restore. Soft-delete can therefore release the
                # same login without a unique-index <-> namespace deadlock cycle.
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
        except StudentInventoryNamespaceChanged as exc:
            if attempt < 2:
                continue
            raise StudentIdentityError(
                {"ps_number": "학생 아이디 변경이 진행 중입니다. 다시 시도해 주세요."}
            ) from exc
        except StudentInventoryNamespaceConflict as exc:
            raise StudentIdentityError(
                {"ps_number": "이전 저장자료 소유권을 확인한 뒤 다시 시도해 주세요."}
            ) from exc
        except IntegrityError as exc:
            if student_login_id_taken(
                tenant=tenant,
                display_username=ps_number,
            ):
                raise StudentIdentityError(
                    {"ps_number": "이미 사용 중인 학생 아이디입니다."}
                ) from exc
            raise

        return StudentAccountCreationResult(
            student=student,
            user=user,
            parent=parent,
            parent_phone=parent_phone,
            parent_password_for_notice=parent_password_for_notice,
            parent_user_created=parent_user_created,
        )

    raise AssertionError("unreachable student account creation retry state")
