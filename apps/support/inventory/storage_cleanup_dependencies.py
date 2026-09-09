"""Submission-lifecycle boundary used by inventory Storage uploads."""

from __future__ import annotations


def ensure_storage_inventory_key_attachable(*, tenant_id: int, key: str) -> None:
    from apps.domains.submissions.services.lifecycle import (
        ensure_storage_inventory_key_attachable as _ensure,
    )

    _ensure(tenant_id=tenant_id, key=key)


def compensate_unattached_storage_object(
    *,
    tenant_id: int,
    key: str,
    uncertain_write: bool = False,
) -> str:
    from apps.domains.submissions.services.lifecycle import (
        compensate_unattached_storage_object as _compensate,
    )

    return _compensate(
        tenant_id=tenant_id,
        key=key,
        uncertain_write=uncertain_write,
    )
