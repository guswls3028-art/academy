"""Transactional metadata deletion with durable, post-commit object cleanup."""

from django.db import transaction

from apps.domains.inventory.models import InventoryFile, InventoryFolder
from apps.support.inventory.matchup_dependencies import inventory_matchup_delete_plan
from apps.support.inventory.storage_cleanup_dependencies import (
    inventory_storage_cleanup_status,
    schedule_inventory_storage_cleanup,
)
from apps.support.results.student_reported_scores import (
    inventory_file_has_reported_score,
    inventory_files_have_any_reported_score,
)


class InventoryDeleteScopeError(ValueError):
    """The exact cascade or object namespace cannot be safely identified."""


def _same_scope(row, *, tenant, scope, student_ps):
    return row.tenant_id == tenant.id and row.scope == scope and (
        scope != "student" or row.student_ps == student_ps
    )


def _prepare_delete(*, tenant, files, single_file=False):
    file_ids = [file.id for file in files]
    protected_score = (
        inventory_file_has_reported_score(tenant=tenant, file_id=file_ids[0])
        if single_file else inventory_files_have_any_reported_score(tenant=tenant, file_ids=file_ids)
    )
    if protected_score:
        return set(), 0, {
            "ok": False, "status": 409,
            "code": "reported_score_evidence_protected",
            "detail": "검수 기록과 연결된 성적표 원본은 삭제할 수 없습니다.",
        }
    try:
        document_count, keys, protection = inventory_matchup_delete_plan(
            tenant=tenant, inventory_file_ids=file_ids,
        )
    except ValueError as exc:
        raise InventoryDeleteScopeError("inventory matchup scope is invalid") from exc
    if protection:
        return set(), document_count, protection
    keys.update(file.r2_key for file in files if file.r2_key)
    prefix = f"tenants/{tenant.id}/"
    if any(not isinstance(key, str) or not key or (key.startswith("tenants/") and not key.startswith(prefix)) for key in keys):
        raise InventoryDeleteScopeError("inventory object tenant does not match deletion scope")
    return keys, document_count, None


def _result(*, tenant, intent_ids, folders, files, matchup_docs):
    cleanup = inventory_storage_cleanup_status(tenant_id=tenant.id, intent_ids=intent_ids)
    result = {
        "ok": True,
        "deleted": {"folders": folders, "files": files, "matchup_docs": matchup_docs, "r2_objects": cleanup["cleaned"]},
        "storage_cleanup": cleanup,
    }
    # Nested callers cannot run the callback before their outer transaction
    # commits. This is an accepted DB transition, not proof that R2 is cleaned.
    if not transaction.get_connection().in_atomic_block and (cleanup["pending"] or cleanup["failed"]):
        result.update(
            ok=False, status=502, code="inventory_storage_cleanup_pending",
            detail="목록에서 삭제되었습니다. 원본 파일 정리는 재시도 대기 중입니다. 새로고침으로 목록을 확인해 주세요.",
        )
    return result


def delete_file(*, tenant, file_id, scope, student_ps):
    with transaction.atomic():
        file = InventoryFile.objects.select_for_update().filter(tenant=tenant, id=file_id).first()
        if file is None:
            return {"ok": False, "status": 404, "detail": "Not found"}
        if not _same_scope(file, tenant=tenant, scope=scope, student_ps=student_ps):
            return {"ok": False, "status": 403, "detail": "Forbidden"}
        keys, document_count, protection = _prepare_delete(tenant=tenant, files=[file], single_file=True)
        if protection:
            return protection
        file.delete()
        intent_ids = schedule_inventory_storage_cleanup(tenant_id=tenant.id, object_keys=keys)
    return _result(tenant=tenant, intent_ids=intent_ids, folders=0, files=1, matchup_docs=document_count)


def delete_folder_recursive(*, tenant, folder, scope, student_ps, recursive=True):
    with transaction.atomic():
        root = InventoryFolder.objects.select_for_update().filter(tenant=tenant, id=folder.id).first()
        if root is None:
            return {"ok": False, "status": 404, "detail": "Not found"}
        folders, files, seen_ids = [root], [], {root.id}
        for current in folders:
            if not _same_scope(current, tenant=tenant, scope=scope, student_ps=student_ps):
                raise InventoryDeleteScopeError("inventory folder scope changed")
            children = list(InventoryFolder.objects.select_for_update().filter(parent=current).order_by("id"))
            if any(child.id in seen_ids for child in children):
                raise InventoryDeleteScopeError("inventory folder cycle")
            seen_ids.update(child.id for child in children)
            folders.extend(children)
            current_files = list(InventoryFile.objects.select_for_update().filter(folder=current).order_by("id"))
            if any(not _same_scope(file, tenant=tenant, scope=scope, student_ps=student_ps) for file in current_files):
                raise InventoryDeleteScopeError("inventory file scope changed")
            files.extend(current_files)
            if not recursive and (children or current_files):
                return {
                    "ok": False, "status": 400, "code": "folder_not_empty",
                    "detail": "비어있지 않은 폴더는 지울 수 없습니다. 먼저 하위 파일·폴더를 비우거나 삭제하세요.",
                }
        keys, document_count, protection = _prepare_delete(tenant=tenant, files=files)
        if protection:
            return protection
        root.delete()
        intent_ids = schedule_inventory_storage_cleanup(tenant_id=tenant.id, object_keys=keys)
    return _result(
        tenant=tenant, intent_ids=intent_ids, folders=len(folders), files=len(files), matchup_docs=document_count,
    )
