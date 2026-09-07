"""Public cross-domain parent fixtures for tests."""

from __future__ import annotations

from apps.domains.parents.services import ensure_parent_account_for_student


def create_parent_account_fixture(
    *,
    tenant,
    parent_phone: str,
    student_name: str,
    initial_password: str,
):
    return ensure_parent_account_for_student(
        tenant=tenant,
        parent_phone=parent_phone,
        student_name=student_name,
        initial_password=initial_password,
    )


__all__ = ["create_parent_account_fixture"]
