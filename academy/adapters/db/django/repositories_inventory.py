"""
Inventory 도메인 DB 조회·저장 — .objects. 접근을 adapters 내부로 한정 (Gate 7).
"""
from __future__ import annotations

from django.db.models import Sum


def inventory_file_aggregate_size(tenant):
    from apps.domains.inventory.models import InventoryFile
    return InventoryFile.objects.filter(tenant=tenant).aggregate(s=Sum("size_bytes"))["s"] or 0


def inventory_folder_filter(tenant, scope, student_ps=None):
    from apps.domains.inventory.models import InventoryFolder
    qs = InventoryFolder.objects.filter(tenant=tenant, scope=scope)
    if student_ps is not None and student_ps != "":
        qs = qs.filter(student_ps=student_ps)
    return qs


def inventory_file_filter(tenant, scope, student_ps=None):
    from apps.domains.inventory.models import InventoryFile
    qs = InventoryFile.objects.filter(tenant=tenant, scope=scope)
    if student_ps is not None and student_ps != "":
        qs = qs.filter(student_ps=student_ps)
    return qs


def inventory_folder_get(tenant, folder_id):
    from apps.domains.inventory.models import InventoryFolder
    return InventoryFolder.objects.filter(tenant=tenant, id=folder_id).first()


def inventory_folder_lock(tenant, folder_id):
    from apps.domains.inventory.models import InventoryFolder
    return (
        InventoryFolder.objects.select_for_update()
        .filter(tenant=tenant, id=folder_id)
        .first()
    )


def inventory_folder_create(tenant, parent_id, name, scope, student_ps=""):
    from apps.domains.inventory.models import InventoryFolder
    parent = (
        InventoryFolder.objects.filter(tenant=tenant, id=parent_id).first()
        if parent_id is not None
        else None
    )
    return InventoryFolder.objects.create(
        tenant=tenant,
        parent=parent,
        name=name,
        scope=scope,
        student_ps=student_ps or "",
    )


def inventory_file_create(tenant, **kwargs):
    from apps.domains.inventory.models import InventoryFile
    return InventoryFile.objects.create(tenant=tenant, **kwargs)


def inventory_folder_has_children(tenant, folder) -> bool:
    from apps.domains.inventory.models import InventoryFolder
    return InventoryFolder.objects.filter(tenant=tenant, parent=folder).exists()


def inventory_folder_has_files(tenant, folder) -> bool:
    from apps.domains.inventory.models import InventoryFile
    return InventoryFile.objects.filter(tenant=tenant, folder=folder).exists()


def inventory_file_get(tenant, file_id):
    from apps.domains.inventory.models import InventoryFile
    return InventoryFile.objects.filter(tenant=tenant, id=file_id).first()


# ---- services ----
def inventory_file_filter_folder(tenant, scope, folder_id):
    from apps.domains.inventory.models import InventoryFile
    return InventoryFile.objects.filter(tenant=tenant, scope=scope, folder_id=folder_id)


def inventory_file_get_by_id(tenant, file_id):
    from apps.domains.inventory.models import InventoryFile
    return InventoryFile.objects.filter(tenant=tenant, id=file_id).first()


def inventory_folder_get_by_id(tenant, folder_id):
    from apps.domains.inventory.models import InventoryFolder
    return InventoryFolder.objects.filter(tenant=tenant, id=folder_id).first()


def inventory_file_filter_scope_folder(tenant, scope, folder):
    from apps.domains.inventory.models import InventoryFile
    return InventoryFile.objects.filter(tenant=tenant, scope=scope, folder=folder)


def inventory_folder_filter_parent(tenant, parent):
    from apps.domains.inventory.models import InventoryFolder
    return InventoryFolder.objects.filter(tenant=tenant, parent=parent)


def inventory_folder_filter_parent_id_name(tenant, parent_id, name):
    from apps.domains.inventory.models import InventoryFolder
    return InventoryFolder.objects.filter(tenant=tenant, parent_id=parent_id, name=name)


def inventory_folder_get_or_create(tenant, *, scope, student_ps="", parent=None, name):
    from apps.domains.inventory.models import InventoryFolder
    return InventoryFolder.objects.get_or_create(
        tenant=tenant,
        scope=scope,
        student_ps=student_ps or "",
        parent=parent,
        name=name,
    )


def iter_inventory_r2_keys(*, tenant_id: int | None = None):
    from apps.domains.inventory.models import InventoryFile

    qs = InventoryFile.objects.exclude(r2_key="")
    if tenant_id is not None:
        qs = qs.filter(tenant_id=tenant_id)
    yield from qs.values_list("r2_key", flat=True)
