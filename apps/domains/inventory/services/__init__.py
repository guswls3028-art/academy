# PATH: apps/domains/inventory/services.py
# 이동 로직: R2 Copy → DB 업데이트 → R2 Delete (실패 시 원본 삭제 안 함)

from __future__ import annotations

import json
import uuid
from django.db import transaction

from ..models import InventoryFolder, InventoryFile
from ..r2_path import build_r2_key, folder_path_string, safe_filename
from apps.core.models import Tenant
from academy.adapters.db.django import repositories_inventory as inv_repo
from apps.support.inventory.matchup_dependencies import (
    cleanup_matchup_problem_images,
    get_matchup_document_for_inventory_file,
    matchup_delete_protection_result,
)
from apps.support.inventory.student_dependencies import active_student_id_for_storage
from apps.support.results.student_reported_scores import inventory_files_have_any_reported_score
from apps.support.students.namespace_lock import lock_student_ps_namespaces

try:
    from apps.infrastructure.storage.r2 import (
        copy_object_r2_storage,
        delete_object_r2_storage,
    )
except ImportError:
    copy_object_r2_storage = None
    delete_object_r2_storage = None


def _folder_path_parts(folder: InventoryFolder | None, tenant: Tenant, scope: str, student_ps: str) -> list[str]:
    """폴더부터 루트까지 이름 리스트 (루트가 마지막)."""
    parts = []
    f = folder
    while f:
        parts.append(f.name)
        f = f.parent
    return list(reversed(parts))


def _get_folder_path_str(folder: InventoryFolder | None, tenant: Tenant, scope: str, student_ps: str) -> str:
    return folder_path_string(_folder_path_parts(folder, tenant, scope, student_ps))


def _filename_from_r2_key(r2_key: str) -> str:
    """R2 key에서 마지막 파일명만 추출."""
    return r2_key.split("/")[-1] if r2_key else ""


def _move_backup_key(tenant: Tenant, original_key: str) -> str:
    filename = safe_filename(_filename_from_r2_key(original_key) or "object")
    return f"tenants/{tenant.id}/inventory/.move-backup/{uuid.uuid4().hex}/{filename}"


def _cleanup_backup_keys(backup_plans: list[tuple[str, str]]) -> None:
    for _, backup_key in backup_plans:
        try:
            delete_object_r2_storage(key=backup_key)
        except Exception:
            pass


def _restore_backups(backup_plans: list[tuple[str, str]]) -> bool:
    restored = True
    for original_key, backup_key in backup_plans:
        try:
            copy_object_r2_storage(source_key=backup_key, dest_key=original_key)
        except Exception:
            restored = False
    return restored


def _cleanup_uncommitted_copies(copied_keys: list[str], backup_plans: list[tuple[str, str]]) -> None:
    backup_original_keys = {original_key for original_key, _ in backup_plans}
    for copied_key in copied_keys:
        if copied_key in backup_original_keys:
            continue
        try:
            delete_object_r2_storage(key=copied_key)
        except Exception:
            pass


def _check_duplicate_file(target_folder_id: int | None, tenant: Tenant, scope: str, student_ps: str, display_name: str):
    qs = inv_repo.inventory_file_filter_folder(tenant, scope, target_folder_id)
    if scope == "student":
        qs = qs.filter(student_ps=student_ps)
    return qs.filter(display_name=display_name).order_by("id").first()


def _matchup_delete_protection_result(files: list[InventoryFile]) -> dict | None:
    return matchup_delete_protection_result(files)


def move_file(
    *,
    tenant: Tenant,
    scope: str,
    student_ps: str,
    source_file_id: int,
    target_folder_id: int | None,
    on_duplicate: str | None = None,
) -> dict:
    """
    파일 이동: R2 Copy → DB 업데이트 → R2 Delete.
    실패 시 원본 R2 객체는 삭제하지 않음.
    """
    if not copy_object_r2_storage or not delete_object_r2_storage:
        return {"ok": False, "detail": "R2 storage not configured"}

    source = inv_repo.inventory_file_get_by_id(tenant, source_file_id)
    if not source or source.scope != scope or (scope == "student" and source.student_ps != student_ps):
        return {"ok": False, "detail": "Source file not found", "status": 404}
    if inventory_files_have_any_reported_score(tenant=tenant, file_ids=[source.id]):
        return {
            "ok": False,
            "detail": "검수 기록과 연결된 성적표 원본은 이동할 수 없습니다.",
            "code": "reported_score_evidence_protected",
            "status": 409,
        }

    target_folder = None
    if target_folder_id:
        target_folder = inv_repo.inventory_folder_get_by_id(tenant, target_folder_id)
        if not target_folder or target_folder.scope != scope or (scope == "student" and target_folder.student_ps != student_ps):
            return {"ok": False, "detail": "Target folder not found", "status": 404}

    if source.folder_id == target_folder_id:
        return {"ok": True, "detail": "Already in target"}

    target_path = _get_folder_path_str(target_folder, tenant, scope, student_ps)
    current_filename = _filename_from_r2_key(source.r2_key)
    display_name = source.display_name

    existing = _check_duplicate_file(target_folder_id, tenant, scope, student_ps, display_name)
    overwrite_existing = None
    if existing and existing.id != source_file_id:
        if on_duplicate == "overwrite":
            overwrite_existing = existing
        elif on_duplicate == "rename":
            base, ext = "", ""
            if "." in display_name:
                idx = display_name.rfind(".")
                base, ext = display_name[:idx], display_name[idx:]
            else:
                base = display_name
            display_name = f"{base}_복사본{ext}" if ext else f"{base}_복사본"
            current_filename = safe_filename(display_name)
        else:
            return {"ok": False, "status": 409, "code": "duplicate", "existing_name": display_name, "detail": "File with same name exists"}

    if overwrite_existing:
        protection_result = _matchup_delete_protection_result([overwrite_existing])
        if protection_result:
            return protection_result
        if inventory_files_have_any_reported_score(
            tenant=tenant,
            file_ids=[overwrite_existing.id],
        ):
            return {
                "ok": False,
                "detail": "검수 기록과 연결된 성적표 원본은 덮어쓸 수 없습니다.",
                "code": "reported_score_evidence_protected",
                "status": 409,
            }

    new_key = build_r2_key(
        tenant_id=tenant.id,
        scope=scope,
        student_ps=student_ps,
        folder_path=target_path,
        file_name=current_filename,
    )

    old_key = source.r2_key
    backup_plans: list[tuple[str, str]] = []
    if overwrite_existing:
        existing_key = overwrite_existing.r2_key
        backup_key = _move_backup_key(tenant, existing_key)
        try:
            copy_object_r2_storage(source_key=existing_key, dest_key=backup_key)
        except Exception as e:
            return {"ok": False, "detail": f"R2 backup failed: {e}", "status": 502}
        backup_plans.append((existing_key, backup_key))

    try:
        copy_object_r2_storage(source_key=old_key, dest_key=new_key)
    except Exception as e:
        _cleanup_backup_keys(backup_plans)
        return {"ok": False, "detail": f"R2 copy failed: {e}", "status": 502}

    try:
        with transaction.atomic():
            if scope == "student":
                lock_student_ps_namespaces(
                    tenant_id=tenant.id,
                    ps_numbers=(student_ps,),
                )
                if active_student_id_for_storage(
                    tenant_id=tenant.id,
                    ps_number=student_ps,
                ) is None:
                    raise ValueError("student storage owner changed during move")
                if not InventoryFile.objects.select_for_update().filter(
                    tenant=tenant,
                    id=source.id,
                    scope="student",
                    student_ps=student_ps,
                    r2_key=old_key,
                ).exists():
                    raise ValueError("student storage file changed during move")
                if target_folder_id and not InventoryFolder.objects.select_for_update().filter(
                    tenant=tenant,
                    id=target_folder_id,
                    scope="student",
                    student_ps=student_ps,
                ).exists():
                    raise ValueError("student storage folder changed during move")
                if overwrite_existing and not InventoryFile.objects.select_for_update().filter(
                    tenant=tenant,
                    id=overwrite_existing.id,
                    scope="student",
                    student_ps=student_ps,
                ).exists():
                    raise ValueError("student storage overwrite target changed during move")
            if overwrite_existing:
                overwrite_existing.delete()
            source.folder_id = target_folder_id
            source.r2_key = new_key
            source.display_name = display_name
            source.save(update_fields=["folder_id", "r2_key", "display_name", "updated_at"])
    except Exception as e:
        restored = _restore_backups(backup_plans)
        _cleanup_backup_keys(backup_plans)
        detail = f"DB update failed: {e}"
        if not restored:
            detail = f"{detail}; destination restore failed"
        return {"ok": False, "detail": detail, "status": 500}

    try:
        delete_object_r2_storage(key=old_key)
    except Exception as e:
        return {"ok": False, "detail": f"R2 delete failed (data updated): {e}", "status": 500}

    _cleanup_backup_keys(backup_plans)
    return {"ok": True}


def _collect_folder_tree(folder: InventoryFolder, tenant: Tenant, scope: str, student_ps: str):
    """폴더와 그 하위 모든 폴더·파일 수집."""
    folders = [folder]
    files = list(inv_repo.inventory_file_filter_scope_folder(tenant, scope, folder))
    if scope == "student":
        files = [f for f in files if f.student_ps == student_ps]
    for child in inv_repo.inventory_folder_filter_parent(tenant, folder):
        if scope == "student" and child.student_ps != student_ps:
            continue
        sub_f, sub_files = _collect_folder_tree(child, tenant, scope, student_ps)
        folders.extend(sub_f)
        files.extend(sub_files)
    return folders, files


def _file_folder_path(inv_file: InventoryFile, tenant: Tenant, scope: str, student_ps: str) -> str:
    """파일이 속한 폴더의 경로 문자열 (prefix 제외)."""
    return _get_folder_path_str(inv_file.folder, tenant, scope, student_ps)


def _delete_folder_tree_r2_and_db(folder: InventoryFolder, tenant: Tenant, scope: str, student_ps: str) -> None:
    """폴더와 하위 모든 파일·폴더를 R2 및 DB에서 삭제 (이동 시 덮어쓰기용)."""
    for child in inv_repo.inventory_folder_filter_parent(tenant, folder):
        if scope == "student" and child.student_ps != student_ps:
            continue
        _delete_folder_tree_r2_and_db(child, tenant, scope, student_ps)
    files = list(inv_repo.inventory_file_filter_scope_folder(tenant, scope, folder))
    if scope == "student":
        files = [f for f in files if f.student_ps == student_ps]
    for inv_file in files:
        try:
            delete_object_r2_storage(key=inv_file.r2_key)
        except Exception:
            pass
        inv_file.delete()
    folder.delete()


def delete_folder_recursive(
    *,
    tenant: Tenant,
    folder: InventoryFolder,
    scope: str,
    student_ps: str,
) -> dict:
    """폴더 + 하위 모든 폴더/파일 + R2 객체 + 매치업 cascade 한방 삭제.

    순서:
      1. 트리 수집 (folder + 모든 자식 폴더·파일)
      2. 각 파일의 매치업 problem 이미지 R2 cleanup (먼저 — orphan 방지)
      3. 각 파일의 원본 R2 객체 삭제 (하나라도 실패하면 DB cascade 중단)
      4. 루트 폴더 .delete() — Django CASCADE로 자식 폴더 + InventoryFile +
         매치업 doc/problem 모두 정리

    Returns: {"ok": True, "deleted": {folders, files, matchup_docs, r2_objects}}
    """
    import logging
    log = logging.getLogger(__name__)

    folders, files = _collect_folder_tree(folder, tenant, scope, student_ps)
    matchup_doc_count = 0
    r2_deleted = 0

    protection_result = _matchup_delete_protection_result(files)
    if protection_result:
        return protection_result
    if inventory_files_have_any_reported_score(
        tenant=tenant,
        file_ids=[inventory_file.id for inventory_file in files],
    ):
        return {
            "ok": False,
            "detail": "검수 기록과 연결된 성적표 원본이 포함되어 폴더를 삭제할 수 없습니다.",
            "code": "reported_score_evidence_protected",
            "status": 409,
        }

    # 매치업 problem 이미지 cleanup (cascade 전 — InventoryFile cascade는 problem
    # 이미지 R2 객체를 알지 못함)
    for inv_file in files:
        matchup_doc = get_matchup_document_for_inventory_file(inv_file)
        if matchup_doc is not None:
            matchup_doc_count += 1
            try:
                cleanup_matchup_problem_images(matchup_doc)
            except Exception:
                log.warning(
                    "matchup problem images cleanup failed for inv_file %s",
                    inv_file.id, exc_info=True,
                )

    r2_files = [inv_file for inv_file in files if inv_file.r2_key]
    if r2_files and delete_object_r2_storage is None:
        return {
            "ok": False,
            "detail": "파일 저장소를 사용할 수 없습니다.",
            "code": "storage_unavailable",
            "status": 503,
        }
    for inv_file in r2_files:
        try:
            delete_object_r2_storage(key=inv_file.r2_key)
            r2_deleted += 1
        except Exception:
            log.exception("Failed to delete R2 object: %s", inv_file.r2_key)
            return {
                "ok": False,
                "detail": "원본 파일 삭제에 실패했습니다. 잠시 후 다시 시도해 주세요.",
                "code": "storage_delete_failed",
                "status": 502,
            }

    # DB cascade 삭제 — 루트 .delete()로 자식 폴더/파일 + 매치업 doc/problem 한 번에 정리.
    folder.delete()

    return {
        "ok": True,
        "deleted": {
            "folders": len(folders),
            "files": len(files),
            "matchup_docs": matchup_doc_count,
            "r2_objects": r2_deleted,
        },
    }


def move_folder(
    *,
    tenant: Tenant,
    scope: str,
    student_ps: str,
    source_folder_id: int,
    target_folder_id: int | None,
    on_duplicate: str | None = None,
) -> dict:
    """
    폴더 이동: 하위 모든 파일에 대해 R2 Copy → DB 업데이트 → R2 Delete.
    폴더 자체의 parent_id 업데이트 포함. 실패 시 원본 삭제 안 함.
    """
    if not copy_object_r2_storage or not delete_object_r2_storage:
        return {"ok": False, "detail": "R2 storage not configured"}

    source_folder = inv_repo.inventory_folder_get_by_id(tenant, source_folder_id)
    if not source_folder or source_folder.scope != scope or (scope == "student" and source_folder.student_ps != student_ps):
        return {"ok": False, "detail": "Source folder not found", "status": 404}

    target_folder = None
    if target_folder_id:
        target_folder = inv_repo.inventory_folder_get_by_id(tenant, target_folder_id)
        if not target_folder or target_folder.scope != scope or (scope == "student" and target_folder.student_ps != student_ps):
            return {"ok": False, "detail": "Target folder not found", "status": 404}
        f = target_folder
        while f:
            if f.id == source_folder_id:
                return {"ok": False, "detail": "Cannot move folder into itself or descendant", "status": 400}
            f = f.parent

    folders, files = _collect_folder_tree(source_folder, tenant, scope, student_ps)
    if inventory_files_have_any_reported_score(
        tenant=tenant,
        file_ids=[inventory_file.id for inventory_file in files],
    ):
        return {
            "ok": False,
            "detail": "검수 기록과 연결된 성적표 원본이 포함되어 폴더를 이동할 수 없습니다.",
            "code": "reported_score_evidence_protected",
            "status": 409,
        }

    if source_folder.parent_id == target_folder_id:
        return {"ok": True, "detail": "Already in target"}

    overwrite_files: list[InventoryFile] = []
    q = inv_repo.inventory_folder_filter_parent_id_name(tenant, target_folder_id, source_folder.name).filter(scope=scope)
    if scope == "student":
        q = q.filter(student_ps=student_ps)
    existing_sibling = q.order_by("id").first()
    overwrite_folder = None
    source_folder_renamed = False
    if existing_sibling and existing_sibling.id != source_folder_id:
        if on_duplicate == "overwrite":
            overwrite_folder = existing_sibling
        elif on_duplicate == "rename":
            source_folder.name = f"{source_folder.name}_복사본"
            source_folder_renamed = True
        else:
            return {"ok": False, "status": 409, "code": "duplicate", "existing_name": source_folder.name, "detail": "Folder with same name exists"}

    if overwrite_folder:
        _, overwrite_files = _collect_folder_tree(overwrite_folder, tenant, scope, student_ps)
        protection_result = _matchup_delete_protection_result(overwrite_files)
        if protection_result:
            return protection_result
        if inventory_files_have_any_reported_score(
            tenant=tenant,
            file_ids=[inventory_file.id for inventory_file in overwrite_files],
        ):
            return {
                "ok": False,
                "detail": "검수 기록과 연결된 성적표 원본이 포함되어 폴더를 덮어쓸 수 없습니다.",
                "code": "reported_score_evidence_protected",
                "status": 409,
            }

    source_folder_path = _get_folder_path_str(source_folder, tenant, scope, student_ps)
    target_path_str = _get_folder_path_str(target_folder, tenant, scope, student_ps)

    copy_plans = []
    for inv_file in files:
        old_key = inv_file.r2_key
        file_name = _filename_from_r2_key(old_key)
        current_folder_path = _file_folder_path(inv_file, tenant, scope, student_ps)
        if current_folder_path.startswith(source_folder_path):
            rel = current_folder_path[len(source_folder_path):].lstrip("/")
        else:
            rel = ""
        if rel:
            new_folder_path = f"{target_path_str}/{source_folder.name}/{rel}" if target_path_str else f"{source_folder.name}/{rel}"
        else:
            new_folder_path = f"{target_path_str}/{source_folder.name}" if target_path_str else source_folder.name
        new_key = build_r2_key(
            tenant_id=tenant.id,
            scope=scope,
            student_ps=student_ps,
            folder_path=new_folder_path,
            file_name=file_name,
        )
        copy_plans.append((inv_file, old_key, new_key))

    backup_plans: list[tuple[str, str]] = []
    if overwrite_folder:
        for existing_file in overwrite_files:
            existing_key = existing_file.r2_key
            backup_key = _move_backup_key(tenant, existing_key)
            try:
                copy_object_r2_storage(source_key=existing_key, dest_key=backup_key)
            except Exception as e:
                _cleanup_backup_keys(backup_plans)
                return {"ok": False, "detail": f"R2 backup failed: {e}", "status": 502}
            backup_plans.append((existing_key, backup_key))

    copied_keys: list[str] = []
    for inv_file, old_key, new_key in copy_plans:
        try:
            copy_object_r2_storage(source_key=old_key, dest_key=new_key)
            copied_keys.append(new_key)
        except Exception as e:
            _restore_backups(backup_plans)
            _cleanup_uncommitted_copies(copied_keys, backup_plans)
            _cleanup_backup_keys(backup_plans)
            return {"ok": False, "detail": f"R2 copy failed: {e}", "status": 502}

    try:
        with transaction.atomic():
            if scope == "student":
                lock_student_ps_namespaces(
                    tenant_id=tenant.id,
                    ps_numbers=(student_ps,),
                )
                if active_student_id_for_storage(
                    tenant_id=tenant.id,
                    ps_number=student_ps,
                ) is None:
                    raise ValueError("student storage owner changed during move")
                folder_ids = [item.id for item in folders]
                file_ids = [item.id for item in files]
                if InventoryFolder.objects.select_for_update().filter(
                    tenant=tenant,
                    id__in=folder_ids,
                    scope="student",
                    student_ps=student_ps,
                ).count() != len(folder_ids):
                    raise ValueError("student storage folder tree changed during move")
                if InventoryFile.objects.select_for_update().filter(
                    tenant=tenant,
                    id__in=file_ids,
                    scope="student",
                    student_ps=student_ps,
                ).count() != len(file_ids):
                    raise ValueError("student storage file tree changed during move")
                if target_folder_id and not InventoryFolder.objects.select_for_update().filter(
                    tenant=tenant,
                    id=target_folder_id,
                    scope="student",
                    student_ps=student_ps,
                ).exists():
                    raise ValueError("student storage target folder changed during move")
            if overwrite_folder:
                overwrite_folder.delete()
            for inv_file, old_key, new_key in copy_plans:
                inv_file.r2_key = new_key
                inv_file.save(update_fields=["r2_key", "updated_at"])
            source_folder.parent = target_folder
            source_folder_fields = ["parent_id", "updated_at"]
            if source_folder_renamed:
                source_folder_fields.append("name")
            source_folder.save(update_fields=source_folder_fields)
    except Exception as e:
        _restore_backups(backup_plans)
        _cleanup_uncommitted_copies(copied_keys, backup_plans)
        _cleanup_backup_keys(backup_plans)
        return {"ok": False, "detail": f"DB update failed: {e}", "status": 500}

    for inv_file, old_key, new_key in copy_plans:
        try:
            delete_object_r2_storage(key=old_key)
        except Exception:
            pass
    copied_key_set = set(copied_keys)
    for original_key, _ in backup_plans:
        if original_key in copied_key_set:
            continue
        try:
            delete_object_r2_storage(key=original_key)
        except Exception:
            pass
    _cleanup_backup_keys(backup_plans)

    return {"ok": True}
