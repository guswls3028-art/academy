"""Transaction-scoped serialization for one tenant's student PS namespaces."""

from __future__ import annotations

from collections.abc import Iterable

from django.db import connection, transaction

STUDENT_PS_NAMESPACE_LOCK_VERSION = "academy:student-ps-namespace:v1"


def lock_student_creation_tenant_reference(*, tenant_id: int) -> None:
    """Acquire the Tenant FK-compatible gate before a new Student namespace."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("student creation tenant lock requires an atomic transaction")
    if connection.vendor != "postgresql":
        return

    from apps.core.models import Tenant

    table_name = connection.ops.quote_name(Tenant._meta.db_table)
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT id FROM {table_name} WHERE id = %s FOR KEY SHARE",
            [int(tenant_id)],
        )
        if cursor.fetchone() is None:
            raise Tenant.DoesNotExist


def lock_student_creation_user_reference(*, user_id: int) -> None:
    """Acquire the User FK-compatible gate before a new Student namespace."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("student creation user lock requires an atomic transaction")
    if connection.vendor != "postgresql":
        return

    from django.contrib.auth import get_user_model

    user_model = get_user_model()
    table_name = connection.ops.quote_name(user_model._meta.db_table)
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT id FROM {table_name} WHERE id = %s FOR KEY SHARE",
            [int(user_id)],
        )
        if cursor.fetchone() is None:
            raise user_model.DoesNotExist


def lock_student_ps_namespaces(
    *,
    tenant_id: int,
    ps_numbers: Iterable[str],
) -> tuple[str, ...]:
    """Lock exact student namespaces in stable order for the current transaction."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("student PS namespace lock requires an atomic transaction")

    normalized = tuple(
        sorted(
            {
                str(ps_number or "").strip()
                for ps_number in ps_numbers
                if str(ps_number or "").strip()
            }
        )
    )
    if connection.vendor == "postgresql" and normalized:
        with connection.cursor() as cursor:
            for ps_number in normalized:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    [
                        f"{STUDENT_PS_NAMESPACE_LOCK_VERSION}:"
                        f"{int(tenant_id)}:{ps_number}"
                    ],
                )
    return normalized
