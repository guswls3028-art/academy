"""Transaction-scoped serialization for one tenant's student PS namespaces."""

from __future__ import annotations

from collections.abc import Iterable

from django.db import connection, transaction

STUDENT_PS_NAMESPACE_LOCK_VERSION = "academy:student-ps-namespace:v1"


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
