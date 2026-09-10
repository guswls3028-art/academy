"""Matchup integration helpers for inventory workflows."""

from __future__ import annotations

from typing import Any


def promoted_matchup_document_map(
    *,
    tenant: Any,
    inventory_file_ids: list[int],
) -> dict[int, dict]:
    if not inventory_file_ids:
        return {}

    from apps.domains.matchup.models import MatchupDocument

    documents = MatchupDocument.objects.filter(
        tenant=tenant,
        inventory_file_id__in=inventory_file_ids,
    ).values("id", "inventory_file_id", "status", "problem_count")
    return {doc["inventory_file_id"]: doc for doc in documents}


def promote_inventory_file_to_matchup(
    inventory_file: Any,
    *,
    title: str,
    subject: str = "",
    grade_level: str = "",
) -> Any:
    from apps.domains.matchup.services import promote_inventory_to_matchup

    return promote_inventory_to_matchup(
        inventory_file,
        title=title,
        subject=subject,
        grade_level=grade_level,
    )


def get_matchup_document_for_inventory_file(inventory_file: Any) -> Any | None:
    try:
        return getattr(inventory_file, "matchup_document", None)
    except Exception:
        return None


def document_has_protected_matchup_problems(matchup_document: Any) -> bool:
    from apps.domains.matchup.services import document_has_protected_matchup_problems

    return document_has_protected_matchup_problems(matchup_document)


def protected_matchup_document_delete_detail() -> str:
    from apps.domains.matchup.services import PROTECTED_MATCHUP_DOCUMENT_DELETE_DETAIL

    return PROTECTED_MATCHUP_DOCUMENT_DELETE_DETAIL


def cleanup_matchup_problem_images(matchup_document: Any) -> None:
    from apps.domains.matchup.services import cleanup_matchup_problem_images

    cleanup_matchup_problem_images(matchup_document)


def matchup_delete_protection_result(files: list[Any]) -> dict | None:
    protected_file_ids: list[int] = []
    for inventory_file in files:
        matchup_document = get_matchup_document_for_inventory_file(inventory_file)
        if matchup_document is None:
            continue
        if document_has_protected_matchup_problems(matchup_document):
            protected_file_ids.append(inventory_file.id)

    if not protected_file_ids:
        return None

    return {
        "ok": False,
        "detail": protected_matchup_document_delete_detail(),
        "code": "protected_matchup_document",
        "protected_file_ids": protected_file_ids,
        "status": 409,
    }


def lock_matchup_delete_protection_result(
    *,
    tenant: Any,
    inventory_file_ids: list[int],
) -> dict | None:
    """Re-read and lock the current matchup protection graph before deletion."""
    if not inventory_file_ids:
        return None

    from apps.domains.matchup.models import MatchupDocument, MatchupProblem

    documents = list(
        MatchupDocument.objects.select_for_update()
        .filter(
            tenant=tenant,
            inventory_file_id__in=inventory_file_ids,
        )
        .order_by("id")
    )
    if documents:
        list(
            MatchupProblem.objects.select_for_update()
            .filter(
                tenant=tenant,
                document_id__in=[document.id for document in documents],
            )
            .order_by("id")
        )

    protected_file_ids = [
        document.inventory_file_id
        for document in documents
        if document_has_protected_matchup_problems(document)
    ]
    if not protected_file_ids:
        return None

    return {
        "ok": False,
        "detail": protected_matchup_document_delete_detail(),
        "code": "protected_matchup_document",
        "protected_file_ids": protected_file_ids,
        "status": 409,
    }
