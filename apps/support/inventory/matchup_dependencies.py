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


def inventory_matchup_delete_plan(*, tenant: Any, inventory_file_ids: list[int]):
    """Lock the exact cascade graph and enumerate its keys without deleting R2."""
    from apps.domains.matchup.models import MatchupDocument, MatchupProblem, ProblemSegmentationProposal

    documents = list(MatchupDocument.objects.select_for_update().filter(
        inventory_file_id__in=inventory_file_ids,
    ).order_by("id"))
    if any(document.tenant_id != tenant.id for document in documents):
        raise ValueError("matchup document tenant does not match inventory")
    document_ids = [document.id for document in documents]
    problems = list(MatchupProblem.objects.select_for_update().filter(
        document_id__in=document_ids,
    ).order_by("id"))
    proposals = list(ProblemSegmentationProposal.objects.select_for_update().filter(
        document_id__in=document_ids,
    ).order_by("id"))
    if any(row.tenant_id != tenant.id for row in [*problems, *proposals]):
        raise ValueError("matchup image tenant does not match inventory")
    if any((problem.meta or {}).get("manual") is True or (problem.meta or {}).get("manual_owner_pinned") is True for problem in problems):
        return len(documents), set(), {
            "ok": False,
            "detail": protected_matchup_document_delete_detail(),
            "code": "protected_matchup_document",
            "status": 409,
        }
    keys = {document.r2_key for document in documents if document.r2_key}
    keys.update(row.image_key for row in [*problems, *proposals] if row.image_key)
    for problem in problems:
        cleanup = (problem.meta or {}).get("public_cleanup")
        if isinstance(cleanup, dict) and cleanup.get("public_image_key"):
            if not isinstance(cleanup["public_image_key"], str):
                raise ValueError("matchup public key is not an exact key")
            keys.add(cleanup["public_image_key"])
    for document in documents:
        page_keys = (document.meta or {}).get("page_image_keys") or []
        if not isinstance(page_keys, list) or any(not isinstance(key, str) for key in page_keys):
            raise ValueError("matchup page keys are not an exact key list")
        keys.update(key for key in page_keys if key)
    return len(documents), keys, None
