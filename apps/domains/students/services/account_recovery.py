# PATH: apps/domains/students/services/account_recovery.py
"""
Public account recovery for student/parent login accounts.

Canonical flow:
- caller proves knowledge of tenant + student name + registered phone
- API response never returns the username/password directly
- delivery happens only to the verified phone number
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from django.db.models import Q

from apps.core.models.user import user_display_username
from apps.core.services.password import generate_temp_password
from apps.domains.students.models import Student
from apps.support.students.account_recovery_dependencies import (
    account_recovery_delivery_disabled,
    find_parent_recovery_account,
    send_account_recovery_alimtalk,
)


RECOVERY_MODES = ("username", "password")
RECOVERY_TARGETS = ("student", "parent")


class AccountRecoveryError(Exception):
    """Base recovery error with a user-facing detail."""

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


class AccountRecoveryDeliveryError(AccountRecoveryError):
    """Raised when a matched account could not be notified."""


class AccountRecoveryValidationError(AccountRecoveryError):
    """Raised for invalid request payloads."""


@dataclass(frozen=True)
class RecoveryAccount:
    target: str
    student: Student
    user: object
    send_to: str
    display_name: str
    display_username: str


def normalize_recovery_phone(value: object) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits if len(digits) == 11 and digits.startswith("010") else ""


def validate_recovery_payload(*, mode: str, target: str, name: str, phone: str) -> tuple[str, str, str, str]:
    mode = str(mode or "").strip().lower()
    target = str(target or "").strip().lower()
    name = str(name or "").strip()
    phone = normalize_recovery_phone(phone)

    if mode not in RECOVERY_MODES:
        raise AccountRecoveryValidationError("복구 유형을 선택해 주세요.")
    if target not in RECOVERY_TARGETS:
        raise AccountRecoveryValidationError("대상을 선택해 주세요. (학생 / 학부모)")
    if not name:
        raise AccountRecoveryValidationError("학생 이름을 입력해 주세요.")
    if not phone:
        raise AccountRecoveryValidationError("휴대폰 번호를 010 11자리로 입력해 주세요.")
    return mode, target, name, phone


def _find_unique_student(*, tenant, name: str, phone: str, parent_only: bool) -> Student | None:
    qs = Student.objects.filter(
        tenant=tenant,
        deleted_at__isnull=True,
        name__iexact=name,
    )
    if parent_only:
        qs = qs.filter(parent_phone=phone)
    else:
        qs = qs.filter(Q(phone=phone) | Q(parent_phone=phone))

    matches = list(qs.select_related("user").order_by("id")[:2])
    if len(matches) != 1:
        return None
    return matches[0]


def resolve_recovery_account(*, tenant, target: str, name: str, phone: str) -> RecoveryAccount | None:
    """Return a uniquely matched account, or None for no/ambiguous matches."""

    if target == "student":
        student = _find_unique_student(tenant=tenant, name=name, phone=phone, parent_only=False)
        if not student or not getattr(student, "user_id", None):
            return None
        return RecoveryAccount(
            target="student",
            student=student,
            user=student.user,
            send_to=phone,
            display_name=student.name or name,
            display_username=student.ps_number or user_display_username(student.user),
        )

    student = _find_unique_student(tenant=tenant, name=name, phone=phone, parent_only=True)
    if not student:
        return None

    parent = find_parent_recovery_account(
        tenant=tenant,
        parent_phone=phone,
    )
    if not parent or not getattr(parent, "user_id", None):
        return None

    return RecoveryAccount(
        target="parent",
        student=student,
        user=parent.user,
        send_to=phone,
        display_name=parent.name or f"{student.name} 학부모",
        display_username=phone,
    )


def _site_url() -> str:
    return getattr(settings, "SITE_URL", "") or "https://hakwonplus.com"


def _account_recovery_delivery_disabled(source_tenant_id: int) -> bool:
    return account_recovery_delivery_disabled(source_tenant_id)


def _send_owner_alimtalk(
    *,
    source_tenant_id: int,
    trigger: str,
    to: str,
    replacements: dict[str, str],
    log_target_id: str,
    log_target_name: str,
) -> bool:
    if _account_recovery_delivery_disabled(source_tenant_id):
        return True
    return send_account_recovery_alimtalk(
        trigger=trigger,
        to=to,
        replacements=replacements,
        source_tenant_id=source_tenant_id,
        log_target_type="account",
        log_target_id=log_target_id,
        log_target_name=log_target_name,
    )


def _log_target_id(account: RecoveryAccount) -> str:
    if account.target == "parent":
        return f"parent:{account.student.id}:{account.send_to}"
    return f"student:{account.student.id}"


def _password_replacements(account: RecoveryAccount, password: str) -> dict[str, str]:
    notice = "로그인 후 설정에서 비밀번호를 변경하실 수 있습니다."
    replacements = {
        "학생이름": account.display_name or "",
        "학생아이디": account.display_username or "",
        "학생비밀번호": password,
        "아이디": account.display_username or "",
        "임시비밀번호": password,
        "비밀번호안내": notice,
        "사이트링크": _site_url(),
    }
    if account.target == "parent":
        replacements["학생이름"] = account.student.name or ""
        replacements["학부모아이디"] = account.display_username or ""
        replacements["학부모비밀번호"] = password
    return replacements


def send_username_recovery(account: RecoveryAccount) -> None:
    """Send username only. Password is not changed."""

    notice = "비밀번호를 잊으셨다면 비밀번호 찾기에서 임시 비밀번호를 받아 주세요."
    if account.target == "parent":
        ok = _send_owner_alimtalk(
            source_tenant_id=account.student.tenant_id,
            trigger="registration_approved_parent",
            to=account.send_to,
            replacements={
                "학생이름": account.student.name or "",
                "학생아이디": account.student.ps_number or "",
                "학생비밀번호": "변경되지 않음",
                "학부모아이디": account.display_username,
                "학부모비밀번호": "변경되지 않음",
                "사이트링크": _site_url(),
                "비밀번호안내": notice,
            },
            log_target_id=_log_target_id(account),
            log_target_name=account.student.name or account.display_name,
        )
    else:
        ok = _send_owner_alimtalk(
            source_tenant_id=account.student.tenant_id,
            trigger="registration_approved_student",
            to=account.send_to,
            replacements={
                "학생이름": account.display_name,
                "학생아이디": account.display_username,
                "학생비밀번호": "변경되지 않음",
                "사이트링크": _site_url(),
                "비밀번호안내": notice,
            },
            log_target_id=_log_target_id(account),
            log_target_name=account.student.name or account.display_name,
        )

    if not ok:
        raise AccountRecoveryDeliveryError("아이디 안내 발송에 실패했습니다. 잠시 후 다시 시도해 주세요.")


def send_password_recovery(account: RecoveryAccount, *, temp_password: str | None = None) -> None:
    """Create a pending temporary password and notify the verified phone."""

    password = (temp_password or "").strip() or generate_temp_password()
    if len(password) < 4:
        raise AccountRecoveryValidationError("임시 비밀번호는 최소 4자 이상이어야 합니다.")

    if _account_recovery_delivery_disabled(account.student.tenant_id):
        return

    from django.contrib.auth import get_user_model
    from django.db import transaction

    from apps.core.services.password import create_pending_password_reset

    # Serialize recovery requests for one account. Otherwise two callers can
    # both send a different temporary password while only the last DB write is
    # usable, and a failed caller can overwrite a successful caller's pending
    # state during manual rollback.
    with transaction.atomic():
        User = get_user_model()
        user = User.objects.select_for_update().get(pk=account.user.pk)
        create_pending_password_reset(user, password)

        trigger = "password_reset_parent" if account.target == "parent" else "password_reset_student"
        if not _send_owner_alimtalk(
            source_tenant_id=account.student.tenant_id,
            trigger=trigger,
            to=account.send_to,
            replacements=_password_replacements(account, password),
            log_target_id=_log_target_id(account),
            log_target_name=account.student.name or account.display_name,
        ):
            raise AccountRecoveryDeliveryError(
                "임시 비밀번호 발송에 실패했습니다. 잠시 후 다시 시도해 주세요."
            )


def resolve_staff_password_reset_account(
    *,
    tenant,
    target: str,
    student_name: str,
    student_ps_number: str = "",
    student_phone: str = "",
    parent_phone: str = "",
) -> RecoveryAccount:
    target = str(target or "").strip().lower()
    name = str(student_name or "").strip()
    student_ps_number = str(student_ps_number or "").strip()
    student_phone = normalize_recovery_phone(student_phone)
    parent_phone = normalize_recovery_phone(parent_phone)

    if target not in RECOVERY_TARGETS:
        raise AccountRecoveryValidationError("대상을 선택해 주세요. (학생 / 학부모)")
    if not name:
        raise AccountRecoveryValidationError("학생 이름을 입력해 주세요.")

    if target == "student":
        if not student_ps_number and not student_phone:
            raise AccountRecoveryValidationError("학생 아이디 또는 학생 전화번호를 입력해 주세요.")
        qs = Student.objects.filter(
            tenant=tenant,
            deleted_at__isnull=True,
            name__iexact=name,
        ).select_related("user")
        if student_ps_number:
            student = qs.filter(ps_number=student_ps_number).first()
        else:
            matches = list(qs.filter(Q(phone=student_phone) | Q(parent_phone=student_phone)).order_by("id")[:2])
            if len(matches) > 1:
                raise AccountRecoveryValidationError("동명이인 또는 공유번호가 있어 학생 아이디로 다시 시도해 주세요.")
            student = matches[0] if matches else None
        if not student or not getattr(student, "user_id", None):
            raise AccountRecoveryValidationError("해당 학생 정보를 찾을 수 없습니다.")

        send_to = normalize_recovery_phone(student.phone) or normalize_recovery_phone(student.parent_phone)
        if not send_to:
            raise AccountRecoveryValidationError("등록된 휴대번호가 없어 발송할 수 없습니다. 학원에 문의해 주세요.")
        return RecoveryAccount(
            target="student",
            student=student,
            user=student.user,
            send_to=send_to,
            display_name=student.name or name,
            display_username=student.ps_number or user_display_username(student.user),
        )

    if not parent_phone:
        raise AccountRecoveryValidationError("학부모 전화번호를 010 11자리로 입력해 주세요.")

    account = resolve_recovery_account(
        tenant=tenant,
        target="parent",
        name=name,
        phone=parent_phone,
    )
    if account is None:
        raise AccountRecoveryValidationError("해당 학생 이름과 학부모 전화번호로 등록된 정보가 없습니다.")
    return account


def resolve_staff_account_for_student(*, student: Student, target: str) -> RecoveryAccount:
    """Resolve an exact tenant-scoped student/parent account for staff guidance."""

    target = str(target or "").strip().lower()
    if target not in RECOVERY_TARGETS:
        raise AccountRecoveryValidationError("대상을 선택해 주세요. (학생 / 학부모)")

    if target == "student":
        if not getattr(student, "user_id", None):
            raise AccountRecoveryValidationError("학생 로그인 계정이 없어 안내할 수 없습니다.")
        send_to = normalize_recovery_phone(student.phone) or normalize_recovery_phone(student.parent_phone)
        if not send_to:
            raise AccountRecoveryValidationError("등록된 휴대번호가 없어 발송할 수 없습니다.")
        return RecoveryAccount(
            target="student",
            student=student,
            user=student.user,
            send_to=send_to,
            display_name=student.name or "학생",
            display_username=student.ps_number or user_display_username(student.user),
        )

    parent_phone = normalize_recovery_phone(student.parent_phone)
    if not parent_phone:
        raise AccountRecoveryValidationError("등록된 학부모 휴대번호가 없어 발송할 수 없습니다.")
    parent = find_parent_recovery_account(
        tenant=student.tenant,
        parent_phone=parent_phone,
    )
    if not parent or not getattr(parent, "user_id", None):
        raise AccountRecoveryValidationError("학부모 로그인 계정이 없어 안내할 수 없습니다.")
    return RecoveryAccount(
        target="parent",
        student=student,
        user=parent.user,
        send_to=parent_phone,
        display_name=parent.name or f"{student.name} 학부모",
        display_username=parent_phone,
    )


def reset_staff_password(
    account: RecoveryAccount,
    *,
    temp_password: str,
) -> str:
    password = str(temp_password or "").strip()
    if len(password) < 4:
        raise AccountRecoveryValidationError("설정할 임시 비밀번호를 4자 이상 입력해 주세요.")

    from django.contrib.auth import get_user_model
    from django.db import transaction

    from apps.core.services.password import clear_pending_password_reset, force_reset_password

    with transaction.atomic():
        User = get_user_model()
        user = User.objects.select_for_update().get(pk=account.user.pk)
        force_reset_password(user, password)
        clear_pending_password_reset(user)

        if _account_recovery_delivery_disabled(account.student.tenant_id):
            message = "임시 비밀번호가 발송되었습니다. (테스트 환경에서는 실제 발송이 생략됩니다.)"
        else:
            trigger = "password_reset_parent" if account.target == "parent" else "password_reset_student"
            if not _send_owner_alimtalk(
                source_tenant_id=account.student.tenant_id,
                trigger=trigger,
                to=account.send_to,
                replacements=_password_replacements(account, password),
                log_target_id=_log_target_id(account),
                log_target_name=account.student.name or account.display_name,
            ):
                raise AccountRecoveryDeliveryError(
                    "임시 비밀번호 발송에 실패했습니다. 잠시 후 다시 시도해 주세요."
                )
            message = "임시 비밀번호가 발송되었습니다. 알림톡을 확인해 주세요."

    for field in ("password", "token_version", "must_change_password"):
        setattr(account.user, field, getattr(user, field))
    return message


def list_recent_account_notification_logs(student: Student, *, limit: int = 5) -> list[dict[str, object]]:
    from academy.adapters.db.django.repositories_messaging import list_account_notification_logs

    target_ids = [f"student:{student.id}"]
    parent_phone = normalize_recovery_phone(student.parent_phone)
    if parent_phone:
        target_ids.append(f"parent:{student.id}")
        # Read legacy rows during the retention window; repository responses
        # sanitize the historical phone suffix before returning it.
        target_ids.append(f"parent:{student.id}:{parent_phone}")

    return list_account_notification_logs(
        source_tenant_id=student.tenant_id,
        target_ids=target_ids,
        limit=limit,
    )
