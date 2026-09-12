"""Exact community metadata deletion and durable, post-commit object cleanup."""

from apps.domains.community.models import PostAttachment
from apps.support.community.storage_cleanup_dependencies import (
    lock_storage_object_keys,
    schedule_detached_storage_cleanup,
    storage_cleanup_status,
)


class CommunityDeleteScopeError(ValueError):
    """The attachment graph or object namespace differs from its owning post."""


def delete_post_content(*, post, attachment=None):
    """Caller holds the same post row lock as upload and has checked permissions."""
    attachments = [attachment] if attachment is not None else list(
        PostAttachment.objects.select_for_update().filter(post=post).order_by("id")
    )
    if any(att.tenant_id != post.tenant_id or att.post_id != post.id for att in attachments):
        raise CommunityDeleteScopeError("attachment tenant does not match its post")
    try:
        keys = lock_storage_object_keys(
            tenant_id=post.tenant_id, object_keys=[att.r2_key for att in attachments],
        )
    except ValueError as exc:
        raise CommunityDeleteScopeError("attachment object namespace differs from its tenant") from exc
    if attachment is None:
        post.delete()
    else:
        attachment.delete()
    intent_ids = schedule_detached_storage_cleanup(tenant_id=post.tenant_id, object_keys=keys)
    return {"posts": int(attachment is None), "attachments": len(attachments)}, intent_ids


def deletion_result(*, tenant_id, deleted, intent_ids):
    cleanup = storage_cleanup_status(tenant_id=tenant_id, intent_ids=intent_ids)
    result = {
        "deleted": {**deleted, "r2_objects": cleanup["cleaned"]},
        "storage_cleanup": cleanup,
    }
    if cleanup["pending"] or cleanup["failed"]:
        result.update(
            code="community_storage_cleanup_pending",
            detail="목록에서 삭제되었습니다. 원본 파일 정리는 재시도 대기 중입니다.",
        )
    return result
