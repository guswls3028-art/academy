"""Public cross-domain parent fixtures for tests."""

from __future__ import annotations

from apps.domains.parents.services import ensure_parent_account_for_student
from apps.domains.parents.models import Parent


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


def parent_account_fixture_exists(*, tenant, parent_phone: str) -> bool:
    return Parent.objects.filter(tenant=tenant, phone=parent_phone).exists()


__all__ = ["create_parent_account_fixture", "parent_account_fixture_exists"]
