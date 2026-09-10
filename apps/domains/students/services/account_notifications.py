# PATH: apps/domains/students/services/account_notifications.py
from __future__ import annotations

import logging
from typing import Any

from django.conf import settings

from apps.core.models.user import user_display_username
from apps.domains.students.models import Student
from apps.support.students.account_recovery_dependencies import (
    account_recovery_delivery_disabled,
    send_account_recovery_alimtalk,
)

logger = logging.getLogger(__name__)

UNCHANGED_PASSWORD_NOTICE = "변경되지 않음"


class AccountNotificationDeliveryError(Exception):
    """Raised when a credential change cannot be delivered."""


def _site_url() -> str:
    return getattr(settings, "SITE_URL", "") or "https://hakwonplus.com"


def _normalize_phone(value: Any) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits if len(digits) >= 10 else ""


def _send_owner_account_notice(
    *,
    source_tenant_id: int,
    trigger: str,
    to: str,
    replacements: dict[str, str],
    log_target_id: str,
    log_target_name: str,
    origin_type: str = "system_account",
    origin_id: str | None = None,
) -> bool:
    if account_recovery_delivery_disabled(source_tenant_id):
        return True
    return send_account_recovery_alimtalk(
        trigger=trigger,
        to=to,
        replacements=replacements,
        source_tenant_id=source_tenant_id,
        log_target_type="account",
        log_target_id=log_target_id,
        log_target_name=log_target_name,
        origin_type=origin_type,
        origin_id=origin_id,
    )


def _student_target_id(student: Student) -> str:
    return f"student:{student.id}" if getattr(student, "id", None) else (student.ps_number or "")


def _parent_target_id(student: Student) -> str:
    student_id = getattr(student, "id", None)
    if student_id:
        return f"parent:{student_id}"
    fallback = str(getattr(student, "ps_number", "") or "").strip()
    return f"parent:{fallback}" if fallback else "parent"


def send_student_account_credentials_notice(
    *,
    student: Student,
    password: str | None = None,
    to: str | None = None,
    origin_type: str = "system_account",
    origin_id: str | None = None,
) -> bool:
    """Send the current student login ID and the changed password if known."""

    send_to = _normalize_phone(to) or _normalize_phone(student.phone) or _normalize_phone(student.parent_phone)
    if not send_to:
        logger.info("student account notice skipped: no recipient student_id=%s", getattr(student, "id", None))
        return False

    display_username = student.ps_number or user_display_username(getattr(student, "user", None))
    replacements = {
        "학생이름": student.name or "",
        "학생아이디": display_username or "",
        "학생비밀번호": (password or "").strip() or UNCHANGED_PASSWORD_NOTICE,
        "사이트링크": _site_url(),
        "비밀번호안내": "로그인 정보가 변경되었습니다. 변경된 정보로 로그인해 주세요.",
    }
    return _send_owner_account_notice(
        source_tenant_id=student.tenant_id,
        trigger="registration_approved_student",
        to=send_to,
        replacements=replacements,
        log_target_id=_student_target_id(student),
        log_target_name=student.name or "",
        origin_type=origin_type,
        origin_id=origin_id,
    )


def send_parent_account_credentials_notice(
    *,
    student: Student,
    parent: Any | None = None,
    parent_password: str | None = None,
    student_password: str | None = None,
    to: str | None = None,
    origin_type: str = "system_account",
    origin_id: str | None = None,
) -> bool:
    """Send parent login information, including the linked student account ID."""

    parent_obj = parent or getattr(student, "parent", None)
    parent_phone = _normalize_phone(to) or _normalize_phone(getattr(parent_obj, "phone", None)) or _normalize_phone(student.parent_phone)
    if not parent_phone:
        logger.info("parent account notice skipped: no recipient student_id=%s", getattr(student, "id", None))
        return False

    parent_username = user_display_username(getattr(parent_obj, "user", None)) or parent_phone
    replacements = {
        "학생이름": student.name or "",
        "학생아이디": student.ps_number or "",
        "학생비밀번호": (student_password or "").strip() or UNCHANGED_PASSWORD_NOTICE,
        "학부모아이디": parent_username,
        "학부모비밀번호": (parent_password or "").strip() or UNCHANGED_PASSWORD_NOTICE,
        "사이트링크": _site_url(),
        "비밀번호안내": "로그인 정보가 변경되었습니다. 변경된 정보로 로그인해 주세요.",
    }
    return _send_owner_account_notice(
        source_tenant_id=student.tenant_id,
        trigger="registration_approved_parent",
        to=parent_phone,
        replacements=replacements,
        log_target_id=_parent_target_id(student),
        log_target_name=student.name or "",
        origin_type=origin_type,
        origin_id=origin_id,
    )


def send_student_password_changed_notice(*, student: Student, password: str, to: str | None = None) -> bool:
    send_to = _normalize_phone(to) or _normalize_phone(student.phone) or _normalize_phone(student.parent_phone)
    if not send_to:
        logger.info("student password notice skipped: no recipient student_id=%s", getattr(student, "id", None))
        return False
    replacements = {
        "학생이름": student.name or "",
        "학생아이디": student.ps_number or user_display_username(getattr(student, "user", None)),
        "학생비밀번호": password,
        "아이디": student.ps_number or user_display_username(getattr(student, "user", None)),
        "임시비밀번호": password,
        "비밀번호안내": "변경된 비밀번호로 로그인해 주세요.",
        "사이트링크": _site_url(),
    }
    return _send_owner_account_notice(
        source_tenant_id=student.tenant_id,
        trigger="password_reset_student",
        to=send_to,
        replacements=replacements,
        log_target_id=_student_target_id(student),
        log_target_name=student.name or "",
    )


def send_parent_password_changed_notice(*, parent: Any, password: str, student: Student | None = None) -> bool:
    parent_phone = _normalize_phone(getattr(parent, "phone", None))
    if not parent_phone:
        logger.info("parent password notice skipped: no recipient parent_id=%s", getattr(parent, "id", None))
        return False
    linked_student = student
    student_name = getattr(linked_student, "name", "") or ""
    parent_username = user_display_username(getattr(parent, "user", None)) or parent_phone
    replacements = {
        "학생이름": student_name,
        "학생아이디": getattr(linked_student, "ps_number", "") or "",
        "학생비밀번호": UNCHANGED_PASSWORD_NOTICE,
        "학부모아이디": parent_username,
        "학부모비밀번호": password,
        "아이디": parent_username,
        "임시비밀번호": password,
        "비밀번호안내": "변경된 비밀번호로 로그인해 주세요.",
        "사이트링크": _site_url(),
    }
    log_target_id = (
        _parent_target_id(linked_student)
        if linked_student
        else f"parent-account:{parent.id}"
    )
    return _send_owner_account_notice(
        source_tenant_id=parent.tenant_id,
        trigger="password_reset_parent",
        to=parent_phone,
        replacements=replacements,
        log_target_id=log_target_id,
        log_target_name=student_name or getattr(parent, "name", "") or "",
    )


def send_user_password_changed_notice(*, user: Any, password: str) -> bool:
    student = getattr(user, "student_profile", None)
    if student is not None and getattr(student, "deleted_at", None) is None:
        return send_student_password_changed_notice(student=student, password=password)

    parent = getattr(user, "parent_profile", None)
    if parent is not None:
        return send_parent_password_changed_notice(parent=parent, password=password)

    return True
