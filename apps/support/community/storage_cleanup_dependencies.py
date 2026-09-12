"""Community metadata transitions use the existing durable storage cleanup owner."""


def lock_storage_object_keys(*, tenant_id: int, object_keys):
    from apps.domains.submissions.services.lifecycle import lock_storage_object_keys as lock

    return lock(tenant_id=tenant_id, object_keys=object_keys)


def schedule_detached_storage_cleanup(*, tenant_id: int, object_keys):
    from apps.domains.submissions.services.lifecycle import schedule_detached_storage_cleanup as schedule

    return schedule(tenant_id=tenant_id, object_keys=object_keys)


def storage_cleanup_status(*, tenant_id: int, intent_ids):
    from apps.domains.submissions.services.lifecycle import storage_cleanup_status as cleanup_status

    return cleanup_status(tenant_id=tenant_id, intent_ids=intent_ids)
