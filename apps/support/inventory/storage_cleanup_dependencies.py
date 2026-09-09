"""Submission-lifecycle boundary used by inventory Storage uploads."""

from __future__ import annotations


def ensure_storage_inventory_key_attachable(*, tenant_id: int, key: str) -> None:
    from apps.domains.submissions.services.lifecycle import (
        ensure_storage_inventory_key_attachable as _ensure,
    )

    _ensure(tenant_id=tenant_id, key=key)


def schedule_unreferenced_storage_object_cleanup(
    *,
    tenant_id: int,
    key: str,
) -> int | None:
    from apps.domains.submissions.services.lifecycle import (
        schedule_unreferenced_storage_object_cleanup as _schedule,
    )

    return _schedule(tenant_id=tenant_id, key=key)
