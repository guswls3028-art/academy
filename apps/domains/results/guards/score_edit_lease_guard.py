from __future__ import annotations

from rest_framework.exceptions import APIException, NotFound

from apps.domains.results.guards.score_edit_lease_state import (
    EDIT_LEASE_TTL as EDIT_LEASE_TTL,
    active_score_edit_drafts,
    invalidate_score_edit_leases_for_exam as invalidate_score_edit_leases_for_exam,
    score_edit_active_homework_key,
    score_edit_homework_keys,
    score_edit_lease_payload as score_edit_lease_payload,
    score_edit_payload_active_cell,
    score_edit_payload_is_invalidated,
    score_edit_payload_parts,
)
from apps.support.results.progress_read_dependencies import (
    lock_score_edit_scope_for_exam,
    lock_score_edit_scope_for_session,
)


EDIT_CLIENT_HEADER = "HTTP_X_SCORE_EDITOR_CLIENT"
EDIT_SESSION_HEADER = "HTTP_X_SCORE_SESSION_ID"


class ScoreEditLeaseConflict(APIException):
    status_code = 409
    default_detail = {
        "detail": "이 차시는 다른 화면에서 수정 중입니다.",
        "code": "SCORE_EDIT_LOCKED",
    }
    default_code = "SCORE_EDIT_LOCKED"


class ScoreEditLeaseStale(APIException):
    status_code = 409
    default_detail = {
        "detail": "시험 제출 또는 다른 안전한 작업으로 서버 점수가 변경되었습니다.",
        "code": "SCORE_EDIT_STALE",
    }
    default_code = "SCORE_EDIT_STALE"


def score_edit_client_id(request) -> str:
    value = str(request.META.get(EDIT_CLIENT_HEADER, "") or "").strip()
    return value[:128] or f"legacy-user-{request.user.id}"


def _same_editor(draft, *, user_id: int, client_id: str) -> bool:
    stored_client_id, _ = score_edit_payload_parts(draft.payload)
    draft_client_id = str(getattr(draft, "client_id", "") or stored_client_id or "")
    return (
        int(draft.editor_user_id) == int(user_id)
        and draft_client_id in ("", client_id)
    )


def require_score_edit_lease(request, *, session_id: int, exam_id: int | None = None):
    tenant = getattr(request, "tenant", None)
    if not tenant:
        raise ScoreEditLeaseConflict()

    try:
        session, scope_ids = lock_score_edit_scope_for_session(
            session_id=int(session_id),
            tenant=tenant,
        )
    except NotFound:
        raise NotFound({"detail": "session not found", "code": "NOT_FOUND"}) from None
    if exam_id is not None and not session.exams.filter(id=int(exam_id)).exists():
        raise ScoreEditLeaseConflict()

    client_id = score_edit_client_id(request)
    drafts = list(
        active_score_edit_drafts(scope_ids=scope_ids, tenant_id=tenant.id)
        .select_for_update()
        .order_by("session_id", "id")
    )
    draft = next(
        (
            candidate
            for candidate in drafts
            if int(candidate.session_id) == int(session_id)
            and _same_editor(
                candidate,
                user_id=request.user.id,
                client_id=client_id,
            )
        ),
        None,
    )
    if draft is not None and score_edit_payload_is_invalidated(draft.payload):
        raise ScoreEditLeaseStale()
    for candidate in drafts:
        if score_edit_payload_is_invalidated(candidate.payload):
            continue
        _, candidate_changes = score_edit_payload_parts(candidate.payload)
        same_editor = _same_editor(
            candidate,
            user_id=request.user.id,
            client_id=client_id,
        )
        if not same_editor and (
            candidate_changes or score_edit_payload_active_cell(candidate.payload)
        ):
            raise ScoreEditLeaseConflict()
    if draft is None:
        raise ScoreEditLeaseConflict()
    if not _same_editor(draft, user_id=request.user.id, client_id=client_id):
        raise ScoreEditLeaseConflict()
    return session


def require_homework_score_edit_lease(
    request,
    *,
    session_id: int,
    enrollment_id: int,
    homework_id: int,
):
    """Allow assistants to edit disjoint homework cells while fencing collisions."""
    tenant = getattr(request, "tenant", None)
    if not tenant:
        raise ScoreEditLeaseConflict()

    try:
        session, scope_ids = lock_score_edit_scope_for_session(
            session_id=int(session_id),
            tenant=tenant,
        )
    except NotFound:
        raise NotFound({"detail": "session not found", "code": "NOT_FOUND"}) from None

    client_id = score_edit_client_id(request)
    target_key = (int(enrollment_id), int(homework_id))
    drafts = list(
        active_score_edit_drafts(scope_ids=scope_ids, tenant_id=tenant.id)
        .select_for_update()
        .order_by("session_id", "id")
    )
    draft = next(
        (
            candidate
            for candidate in drafts
            if int(candidate.session_id) == int(session_id)
            and _same_editor(
                candidate,
                user_id=request.user.id,
                client_id=client_id,
            )
        ),
        None,
    )
    if draft is not None and score_edit_payload_is_invalidated(draft.payload):
        raise ScoreEditLeaseStale()
    for candidate in drafts:
        _, candidate_changes = score_edit_payload_parts(candidate.payload)
        same_editor = _same_editor(
            candidate,
            user_id=request.user.id,
            client_id=client_id,
        )
        if score_edit_payload_is_invalidated(candidate.payload):
            continue
        if same_editor or not candidate_changes:
            homework_keys = frozenset()
        else:
            homework_keys = score_edit_homework_keys(candidate_changes)
            if homework_keys is None:
                raise ScoreEditLeaseConflict()
        active_key = score_edit_active_homework_key(
            score_edit_payload_active_cell(candidate.payload)
        )
        if not same_editor and (target_key in homework_keys or target_key == active_key):
            raise ScoreEditLeaseConflict(
                {
                    "detail": "같은 학생의 같은 과제 점수를 다른 조교가 수정 중입니다.",
                    "code": "SCORE_EDIT_LOCKED",
                }
            )

    if draft is None:
        raise ScoreEditLeaseConflict()
    if not _same_editor(draft, user_id=request.user.id, client_id=client_id):
        raise ScoreEditLeaseConflict()
    return session


def require_score_edit_lease_from_headers(request, *, exam_id: int | None = None):
    raw_session_id = str(request.META.get(EDIT_SESSION_HEADER, "") or "").strip()
    try:
        session_id = int(raw_session_id)
    except (TypeError, ValueError):
        raise ScoreEditLeaseConflict() from None
    return require_score_edit_lease(
        request,
        session_id=session_id,
        exam_id=exam_id,
    )


def require_score_edit_scope_available_for_exam(*, exam, tenant) -> list[int]:
    """Fence administrative score writers against active manual editors."""
    scope_ids = lock_score_edit_scope_for_exam(
        exam_id=int(exam.id),
        tenant=tenant,
    )
    active = list(
        active_score_edit_drafts(scope_ids=scope_ids, tenant_id=tenant.id)
        .select_for_update()
        .order_by("session_id", "id")
    )
    if any(
        not score_edit_payload_is_invalidated(draft.payload)
        and bool(score_edit_payload_parts(draft.payload)[1])
        for draft in active
    ):
        raise ScoreEditLeaseConflict()
    return scope_ids
