"""Initial-password policy for student Excel imports."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any, Literal

StudentImportPasswordMode = Literal["fixed", "random"]

FIXED_PASSWORD_MODE: StudentImportPasswordMode = "fixed"
RANDOM_PASSWORD_MODE: StudentImportPasswordMode = "random"
VALID_PASSWORD_MODES = frozenset(
    {
        FIXED_PASSWORD_MODE,
        RANDOM_PASSWORD_MODE,
    }
)


class StudentImportPasswordError(ValueError):
    """Raised when an Excel import password policy cannot be applied safely."""


@dataclass(frozen=True)
class StudentImportPasswordPolicy:
    mode: StudentImportPasswordMode
    fixed_password: str = ""

    def password_for_row(self, row: dict[str, Any]) -> str:
        if self.mode == FIXED_PASSWORD_MODE:
            return self.fixed_password
        return f"{secrets.randbelow(1_000_000):06d}"


def build_student_import_password_policy(
    *,
    password_mode: str | None,
    initial_password: str | None,
) -> StudentImportPasswordPolicy:
    normalized_mode = str(password_mode or FIXED_PASSWORD_MODE).strip().lower()
    if normalized_mode not in VALID_PASSWORD_MODES:
        raise StudentImportPasswordError(
            "password_mode는 fixed 또는 random이어야 합니다."
        )

    fixed_password = str(initial_password or "").strip()
    if normalized_mode == FIXED_PASSWORD_MODE and len(fixed_password) < 4:
        raise StudentImportPasswordError("공통 초기 비밀번호는 4자 이상이어야 합니다.")

    return StudentImportPasswordPolicy(
        mode=normalized_mode,
        fixed_password=fixed_password if normalized_mode == FIXED_PASSWORD_MODE else "",
    )
