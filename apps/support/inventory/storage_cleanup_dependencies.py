"""Inventory deletion uses the existing durable Storage cleanup outbox."""


def schedule_inventory_storage_cleanup(*, tenant_id: int, object_keys):
    from apps.domains.submissions.services.lifecycle import schedule_inventory_storage_cleanup as schedule

    return schedule(tenant_id=tenant_id, object_keys=object_keys)


def inventory_storage_cleanup_status(*, tenant_id: int, intent_ids):
    from apps.domains.submissions.services.lifecycle import inventory_storage_cleanup_status as status

    return status(tenant_id=tenant_id, intent_ids=intent_ids)
