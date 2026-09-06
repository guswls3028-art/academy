# PATH: apps/domains/results/views/score_draft_view.py
"""
Score Edit Draft API — 임시 저장/복원. 최종 반영은 프론트 "편집 종료" 시 patch API로만 수행.

GET  /results/admin/sessions/<session_id>/score-draft/
     → 200 { changes: [...] } or 404

PUT  /results/admin/sessions/<session_id>/score-draft/
     body: { "changes": [ { type, examId?, enrollmentId, homeworkId?, score?, metaStatus? }, ... ] }
     → 200

POST /results/admin/sessions/<session_id>/score-draft/commit/
     → 204 (draft 삭제; 실제 점수 반영은 프론트가 patch API로 이미 수행한 뒤 호출)
"""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status as drf_status

from apps.core.parsing import parse_bool
from apps.core.permissions import TenantResolvedAndStaff
from apps.core.models.user import user_display_username
from apps.domains.results.guards.score_edit_lease_guard import (
    EDIT_LEASE_TTL,
    ScoreEditLeaseConflict,
    ScoreEditLeaseStale,
    score_edit_client_id,
    score_edit_lease_payload,
    score_edit_payload_parts,
    score_edit_payload_is_invalidated,
)
from apps.domains.results.guards.score_edit_lease_state import (
    active_score_edit_drafts,
    normalize_score_active_cell,
    score_edit_active_cell_key,
    score_edit_cell_keys,
    score_edit_changes_are_exclusive,
    score_edit_changes_conflict,
    score_edit_payload_active_cell,
)
from apps.domains.results.models import ScoreEditDraft
from apps.support.results.progress_read_dependencies import (
    get_session_for_tenant_or_404,
    lock_score_edit_scope_for_session,
    score_edit_scope_session_ids,
)


def _locked_response() -> Response:
    error = ScoreEditLeaseConflict()
    return Response(error.detail, status=drf_status.HTTP_409_CONFLICT)


def _stale_response() -> Response:
    error = ScoreEditLeaseStale()
    return Response(error.detail, status=drf_status.HTTP_409_CONFLICT)


def _lock_session(*, session_id: int, tenant):
    return lock_score_edit_scope_for_session(
        session_id=int(session_id),
        tenant=tenant,
    )


def _draft_client_id(draft) -> str:
    stored_client_id, _ = score_edit_payload_parts(draft.payload)
    return str(getattr(draft, "client_id", "") or stored_client_id or "")


def _is_current_editor(draft, *, user_id: int, client_id: str) -> bool:
    return (
        int(draft.editor_user_id) == int(user_id)
        and _draft_client_id(draft) in ("", client_id)
    )


def _editor_name(user) -> str:
    return str(
        getattr(user, "name", "")
        or user.get_full_name()
        or user_display_username(user)
        or "다른 직원"
    ).strip()


def _active_editors(*, session_id: int, tenant_id: int, user_id: int, client_id: str):
    editors = []
    drafts = (
        active_score_edit_drafts(scope_ids=[int(session_id)], tenant_id=tenant_id)
        .select_related("editor_user")
        .order_by("editor_user_id", "client_id", "id")
    )
    for draft in drafts:
        if _is_current_editor(draft, user_id=user_id, client_id=client_id):
            continue
        if score_edit_payload_is_invalidated(draft.payload):
            continue
        active_cell = score_edit_payload_active_cell(draft.payload)
        if active_cell is None:
            continue
        editors.append(
            {
                "client_id": _draft_client_id(draft),
                "editor_user_id": int(draft.editor_user_id),
                "editor_name": _editor_name(draft.editor_user),
                "active_cell": active_cell,
            }
        )
    return editors


def _draft_response(*, changes, stale, session_id, tenant_id, user_id, client_id):
    return {
        "changes": changes,
        "stale": stale,
        "active_editors": _active_editors(
            session_id=session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            client_id=client_id,
        ),
    }


class ScoreDraftView(APIView):
    permission_classes = [IsAuthenticated, TenantResolvedAndStaff]

    def get(self, request, session_id: int):
        tenant = getattr(request, "tenant", None)
        if not tenant:
            return Response({"detail": "Tenant required"}, status=403)
        get_session_for_tenant_or_404(session_id=int(session_id), tenant=tenant)
        client_id = score_edit_client_id(request)
        scope_ids = score_edit_scope_session_ids(
            session_id=int(session_id),
            tenant=tenant,
        )
        active_since = timezone.now() - EDIT_LEASE_TTL
        active_drafts = ScoreEditDraft.objects.filter(
            session_id__in=scope_ids,
            tenant_id=tenant.id,
            updated_at__gte=active_since,
        )
        for active in active_drafts:
            if _is_current_editor(
                active,
                user_id=request.user.id,
                client_id=client_id,
            ):
                continue
            if score_edit_payload_is_invalidated(active.payload):
                continue
            _, active_changes = score_edit_payload_parts(active.payload)
            if score_edit_changes_are_exclusive(active_changes):
                return _locked_response()

        draft = ScoreEditDraft.objects.filter(
            session_id=int(session_id),
            tenant_id=tenant.id,
            editor_user_id=request.user.id,
            client_id=client_id,
        ).first()
        if draft is None:
            draft = ScoreEditDraft.objects.filter(
                session_id=int(session_id),
                tenant_id=tenant.id,
                editor_user_id=request.user.id,
                client_id="",
            ).first()
        if draft is None:
            # A closed/lost device cannot release its client-scoped draft.
            # Surface the latest expired draft to the same account so the
            # existing explicit recovery UI can preserve or discard it.
            draft = (
                ScoreEditDraft.objects.filter(
                    session_id=int(session_id),
                    tenant_id=tenant.id,
                    editor_user_id=request.user.id,
                    updated_at__lt=active_since,
                )
                .order_by("-updated_at", "-id")
                .first()
            )
        same_user_expired_recovery = bool(
            draft is not None
            and int(draft.editor_user_id) == int(request.user.id)
            and draft.updated_at < active_since
        )
        if (
            draft is not None
            and not same_user_expired_recovery
            and not _is_current_editor(
                draft,
                user_id=request.user.id,
                client_id=client_id,
            )
        ):
            draft = None
        if not draft:
            return Response(
                _draft_response(
                    changes=[],
                    stale=False,
                    session_id=session_id,
                    tenant_id=tenant.id,
                    user_id=request.user.id,
                    client_id=client_id,
                )
            )
        _, changes = score_edit_payload_parts(draft.payload)
        return Response(
            _draft_response(
                changes=changes,
                stale=score_edit_payload_is_invalidated(draft.payload),
                session_id=session_id,
                tenant_id=tenant.id,
                user_id=request.user.id,
                client_id=client_id,
            )
        )

    def put(self, request, session_id: int):
        tenant = getattr(request, "tenant", None)
        if not tenant:
            return Response({"detail": "Tenant required"}, status=403)
        changes = request.data.get("changes")
        if not isinstance(changes, list):
            return Response({"detail": "changes must be a list"}, status=400)
        raw_active_cell = request.data.get("active_cell")
        active_cell = normalize_score_active_cell(raw_active_cell)
        if raw_active_cell is not None and active_cell is None:
            return Response({"detail": "active_cell must be a score cell"}, status=400)
        acknowledge_stale = parse_bool(
            request.data.get("acknowledge_stale", False),
            field_name="acknowledge_stale",
        )
        take_over_same_user = parse_bool(
            request.data.get("take_over_same_user", False),
            field_name="take_over_same_user",
        )
        client_id = score_edit_client_id(request)
        with transaction.atomic():
            _, scope_ids = _lock_session(session_id=int(session_id), tenant=tenant)
            active_since = timezone.now() - EDIT_LEASE_TTL
            drafts = list(
                ScoreEditDraft.objects.select_for_update().filter(
                    session_id__in=scope_ids,
                    tenant_id=tenant.id,
                ).order_by("-updated_at", "-id")
            )
            handoff_drafts = []
            for existing in drafts:
                if existing.updated_at < active_since:
                    continue
                if score_edit_payload_is_invalidated(existing.payload):
                    continue
                if _is_current_editor(
                    existing,
                    user_id=request.user.id,
                    client_id=client_id,
                ):
                    continue
                _, existing_changes = score_edit_payload_parts(existing.payload)
                existing_active_key = score_edit_active_cell_key(
                    score_edit_payload_active_cell(existing.payload)
                )
                incoming_active_key = score_edit_active_cell_key(active_cell)
                existing_cell_keys = score_edit_cell_keys(existing_changes)
                incoming_cell_keys = score_edit_cell_keys(changes)
                presence_conflicts = (
                    incoming_active_key is not None
                    and (
                        score_edit_changes_are_exclusive(existing_changes)
                        or incoming_active_key == existing_active_key
                        or (
                            existing_cell_keys is not None
                            and incoming_active_key in existing_cell_keys
                        )
                    )
                ) or (
                    existing_active_key is not None
                    and (
                        score_edit_changes_are_exclusive(changes)
                        or (
                            incoming_cell_keys is not None
                            and existing_active_key in incoming_cell_keys
                        )
                    )
                )
                has_conflict = (
                    score_edit_changes_conflict(existing_changes, changes)
                    or presence_conflicts
                )
                if not has_conflict:
                    continue
                if (
                    take_over_same_user
                    and changes
                    and int(existing.editor_user_id) == int(request.user.id)
                ):
                    handoff_drafts.append(existing)
                    continue
                return _locked_response()

            draft = next(
                (
                    item
                    for item in drafts
                    if int(item.session_id) == int(session_id)
                    and _is_current_editor(
                        item,
                        user_id=request.user.id,
                        client_id=client_id,
                    )
                ),
                None,
            )
            if draft is None:
                expired_same_user_drafts = [
                    item
                    for item in drafts
                    if item.updated_at < active_since
                    and int(item.session_id) == int(session_id)
                    and int(item.editor_user_id) == int(request.user.id)
                ]
                for same_user_draft in expired_same_user_drafts:
                    _, previous_changes = score_edit_payload_parts(
                        same_user_draft.payload
                    )
                    # An expired lease with no score changes has nothing to
                    # recover. Reuse it even when its old device left an
                    # active-cell marker or automatic grading invalidated it.
                    reusable_empty_lease = not previous_changes
                    if not reusable_empty_lease:
                        if take_over_same_user and changes:
                            handoff_drafts.append(same_user_draft)
                            continue
                        return _locked_response()
                    draft = same_user_draft
                    break
            if draft is not None and score_edit_payload_is_invalidated(draft.payload):
                _, invalidated_changes = score_edit_payload_parts(draft.payload)
                if invalidated_changes and not acknowledge_stale:
                    return _stale_response()
            for previous in handoff_drafts:
                previous_client_id, previous_changes = score_edit_payload_parts(
                    previous.payload
                )
                previous.payload = score_edit_lease_payload(
                    client_id=previous_client_id or _draft_client_id(previous),
                    changes=previous_changes,
                    active_cell=score_edit_payload_active_cell(previous.payload),
                    invalidated=True,
                    invalidated_reason="SAME_ACCOUNT_HANDOFF",
                )
                previous.save(update_fields=["payload", "updated_at"])
            payload = score_edit_lease_payload(
                client_id=client_id,
                changes=changes,
                active_cell=active_cell,
            )
            if draft is None:
                draft = ScoreEditDraft.objects.create(
                    session_id=int(session_id),
                    tenant_id=tenant.id,
                    editor_user_id=request.user.id,
                    client_id=client_id,
                    payload=payload,
                )
            else:
                draft.client_id = client_id
                draft.payload = payload
                draft.save(update_fields=["client_id", "payload", "updated_at"])
        return Response(
            _draft_response(
                changes=changes,
                stale=False,
                session_id=session_id,
                tenant_id=tenant.id,
                user_id=request.user.id,
                client_id=client_id,
            )
        )


class ScoreDraftCommitView(APIView):
    """편집 종료 시 프론트가 patch 적용 후 호출 — draft 삭제."""
    permission_classes = [IsAuthenticated, TenantResolvedAndStaff]

    def post(self, request, session_id: int):
        tenant = getattr(request, "tenant", None)
        if not tenant:
            return Response({"detail": "Tenant required"}, status=403)
        client_id = score_edit_client_id(request)
        release_lease = parse_bool(
            request.data.get("release_lease", True),
            field_name="release_lease",
        )
        with transaction.atomic():
            _lock_session(session_id=int(session_id), tenant=tenant)
            draft = (
                ScoreEditDraft.objects.select_for_update()
                .filter(
                    session_id=int(session_id),
                    tenant_id=tenant.id,
                    editor_user_id=request.user.id,
                    client_id__in=["", client_id],
                )
                .first()
            )
            if draft is None:
                return Response(status=204)
            if not _is_current_editor(
                draft,
                user_id=request.user.id,
                client_id=client_id,
            ):
                return _locked_response()
            if (
                not release_lease
                and score_edit_payload_is_invalidated(draft.payload)
            ):
                return _stale_response()
            if release_lease:
                draft.delete()
            else:
                draft.payload = score_edit_lease_payload(
                    client_id=client_id,
                    changes=[],
                    active_cell=score_edit_payload_active_cell(draft.payload),
                )
                draft.save(update_fields=["payload", "updated_at"])
        return Response(status=204)
