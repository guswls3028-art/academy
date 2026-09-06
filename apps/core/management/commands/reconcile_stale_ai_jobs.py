from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from academy.adapters.db.django.repositories_ai import cache_terminal_job_status
from apps.domains.ai.models import AIJobModel, AIResultModel
from apps.domains.matchup.models import MatchupDocument


TERMINAL_REASON_PREFIX = "stale_running_reconciled"
DEFAULT_TERMINAL_SOURCE_STATUSES = {"done", "failed"}


@dataclass(frozen=True)
class ReconcileCandidate:
    job_id: str
    source_id: str | None
    reason: str
    action: str
    tenant_id: str | None
    job_type: str
    source_domain: str | None
    observed_status: str
    observed_updated_at: datetime
    observed_lease_expires_at: datetime | None
    observed_locked_by: str | None
    observed_source_status: str | None
    observed_source_job_id: str | None


def _candidate(
    job: AIJobModel,
    *,
    reason: str,
    action: str = "fail_job",
    source_status: str | None = None,
    source_job_id: str | None = None,
) -> ReconcileCandidate:
    return ReconcileCandidate(
        job_id=job.job_id,
        source_id=job.source_id,
        reason=reason,
        action=action,
        tenant_id=job.tenant_id,
        job_type=job.job_type,
        source_domain=job.source_domain,
        observed_status=job.status,
        observed_updated_at=job.updated_at,
        observed_lease_expires_at=job.lease_expires_at,
        observed_locked_by=job.locked_by,
        observed_source_status=source_status,
        observed_source_job_id=source_job_id,
    )


def _is_stale(job: AIJobModel, cutoff) -> bool:
    lease = job.lease_expires_at
    reference = lease or job.updated_at or job.created_at
    return reference <= cutoff


def iter_stale_matchup_candidates(
    *,
    older_than_hours: int,
    limit: int,
    include_processing_source: bool = False,
    terminal_source_statuses: Iterable[str] = DEFAULT_TERMINAL_SOURCE_STATUSES,
    job_id: str | None = None,
    expected_updated_at: datetime | None = None,
) -> list[ReconcileCandidate]:
    cutoff = timezone.now() - timezone.timedelta(hours=older_than_hours)
    terminal_statuses = {str(s).lower() for s in terminal_source_statuses}
    candidates: list[ReconcileCandidate] = []

    qs = (
        AIJobModel.objects
        .filter(status="RUNNING", source_domain="matchup", job_type="matchup_analysis")
        .order_by("created_at", "id")
    )
    if job_id:
        qs = qs.filter(job_id=job_id)
    if expected_updated_at is not None:
        qs = qs.filter(updated_at=expected_updated_at)
    for job in qs.iterator():
        if len(candidates) >= limit:
            break
        if not _is_stale(job, cutoff):
            continue

        source_id = str(job.source_id or "")
        if not source_id.isdigit():
            candidates.append(_candidate(job, reason="invalid_source_id"))
            continue

        doc = MatchupDocument.objects.filter(id=int(source_id)).only("id", "status", "ai_job_id").first()
        if doc is None:
            candidates.append(_candidate(job, reason="orphan_source"))
            continue
        if not job.tenant_id or str(doc.tenant_id) != str(job.tenant_id):
            candidates.append(_candidate(
                job,
                reason="source_tenant_scope_mismatch",
                action="manual_review",
                source_status=str(doc.status),
                source_job_id=str(doc.ai_job_id or ""),
            ))
            continue

        current_job_id = str(doc.ai_job_id or "")
        if current_job_id and current_job_id != str(job.job_id) and str(doc.status).lower() in terminal_statuses:
            candidates.append(_candidate(
                job,
                reason=f"superseded_source:{doc.status}",
                source_status=str(doc.status),
                source_job_id=current_job_id,
            ))
            continue

        if current_job_id == str(job.job_id) and str(doc.status).lower() in terminal_statuses:
            action = "mark_done_from_terminal_source" if str(doc.status).lower() == "done" else "fail_job"
            candidates.append(_candidate(
                job,
                reason=f"current_source_terminal:{doc.status}",
                action=action,
                source_status=str(doc.status),
                source_job_id=current_job_id,
            ))
            continue

        if (
            include_processing_source
            and current_job_id == str(job.job_id)
            and str(doc.status).lower() == "processing"
        ):
            candidates.append(_candidate(
                job,
                reason="expired_processing_source",
                action="retry_processing_source",
                source_status=str(doc.status),
                source_job_id=current_job_id,
            ))

    return candidates


def reconcile_candidates(candidates: Iterable[ReconcileCandidate], *, execute: bool) -> int:
    if not execute:
        return 0

    updated = 0
    now = timezone.now()
    for candidate in candidates:
        if candidate.action == "manual_review":
            continue
        error = f"{TERMINAL_REASON_PREFIX}:{candidate.reason}"
        with transaction.atomic():
            job = AIJobModel.objects.select_for_update().filter(
                job_id=candidate.job_id,
                tenant_id=candidate.tenant_id,
                job_type=candidate.job_type,
                source_domain=candidate.source_domain,
                source_id=candidate.source_id,
                status=candidate.observed_status,
                updated_at=candidate.observed_updated_at,
                lease_expires_at=candidate.observed_lease_expires_at,
                locked_by=candidate.observed_locked_by,
            ).first()
            if not job:
                continue
            if str(candidate.source_id or "").isdigit():
                source_query = MatchupDocument.objects.select_for_update().filter(
                    id=int(str(candidate.source_id)),
                    tenant_id=candidate.tenant_id,
                )
                if candidate.observed_source_status is None:
                    if source_query.exists():
                        continue
                elif not source_query.filter(
                    status=candidate.observed_source_status,
                    ai_job_id=candidate.observed_source_job_id,
                ).exists():
                    continue
            if candidate.action == "mark_done_from_terminal_source":
                job.status = "DONE"
                job.error_message = ""
                job.last_error = ""
            else:
                job.status = "FAILED"
                job.error_message = error
                job.last_error = error
            job.locked_by = None
            job.locked_at = None
            job.lease_expires_at = None
            job.completed_at = now
            job.updated_at = now
            job.save(update_fields=[
                "status",
                "error_message",
                "last_error",
                "locked_by",
                "locked_at",
                "lease_expires_at",
                "completed_at",
                "updated_at",
            ])
            if candidate.action == "mark_done_from_terminal_source":
                AIResultModel.objects.get_or_create(
                    job=job,
                    defaults={
                        "payload": {
                            "reconciled_from_source": True,
                            "source_domain": "matchup",
                            "source_id": str(candidate.source_id or ""),
                            "reason": error,
                        },
                    },
                )
            result_payload = None
            if candidate.action == "mark_done_from_terminal_source":
                result_payload = AIResultModel.objects.filter(job=job).values_list(
                    "payload",
                    flat=True,
                ).first()
            transaction.on_commit(
                lambda job=job, result_payload=result_payload: cache_terminal_job_status(
                    job,
                    result_payload=result_payload,
                )
            )
            if candidate.action == "retry_processing_source" and str(job.source_id or "").isdigit():
                doc = MatchupDocument.objects.select_for_update().filter(
                    id=int(str(job.source_id)),
                    ai_job_id=job.job_id,
                    status="processing",
                ).first()
                if doc:
                    doc.status = "failed"
                    doc.error_message = error
                    doc.save(update_fields=["status", "error_message", "updated_at"])
            updated += 1
            if candidate.action == "retry_processing_source" and str(job.source_id or "").isdigit():
                transaction.on_commit(lambda doc_id=int(str(job.source_id)): _retry_failed_matchup_document(doc_id))
    return updated


def _retry_failed_matchup_document(doc_id: int) -> None:
    from apps.domains.matchup.services import retry_document

    doc = MatchupDocument.objects.get(id=doc_id)
    retry_document(doc, require_failed=True)


class Command(BaseCommand):
    help = "Reconcile stale RUNNING matchup AI jobs that no longer own their source document."

    def add_arguments(self, parser):
        parser.add_argument("--older-than-hours", type=int, default=24)
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument(
            "--job-id",
            help="Restrict dry-run to one exact job. Required with --execute.",
        )
        parser.add_argument(
            "--expected-updated-at",
            help=(
                "Exact ISO-8601 updated_at emitted by dry-run. Required with --execute "
                "so a refreshed lease cannot be reconciled from a stale plan."
            ),
        )
        parser.add_argument(
            "--include-processing-source",
            action="store_true",
            help=(
                "Also recover expired RUNNING jobs whose source document is still processing "
                "and still points at that job. With --execute, marks the stale job/doc failed "
                "and immediately dispatches a fresh retry job."
            ),
        )
        parser.add_argument("--execute", action="store_true")

    def handle(self, *args, **options):
        older_than_hours = int(options["older_than_hours"])
        limit = int(options["limit"])
        execute = bool(options["execute"])
        job_id = str(options.get("job_id") or "").strip() or None
        expected_updated_at_raw = str(options.get("expected_updated_at") or "").strip()
        expected_updated_at = (
            parse_datetime(expected_updated_at_raw)
            if expected_updated_at_raw
            else None
        )
        if older_than_hours <= 0:
            raise CommandError("--older-than-hours must be positive")
        if limit <= 0:
            raise CommandError("--limit must be positive")
        if expected_updated_at_raw and expected_updated_at is None:
            raise CommandError("--expected-updated-at must be an ISO-8601 datetime")
        if execute and (not job_id or expected_updated_at is None):
            raise CommandError(
                "--execute requires exact --job-id and --expected-updated-at from dry-run"
            )

        candidates = iter_stale_matchup_candidates(
            older_than_hours=older_than_hours,
            limit=limit,
            include_processing_source=bool(options["include_processing_source"]),
            job_id=job_id,
            expected_updated_at=expected_updated_at,
        )
        if execute and len(candidates) != 1:
            raise CommandError(
                "exact stale RUNNING candidate not found; run dry-run again before retrying"
            )
        if execute and candidates[0].action == "manual_review":
            raise CommandError("candidate requires manual review and cannot be executed")
        for candidate in candidates:
            audit_record = {
                "action": candidate.action,
                "job_id": candidate.job_id,
                "job_type": candidate.job_type,
                "lease_expires_at": (
                    candidate.observed_lease_expires_at.isoformat()
                    if candidate.observed_lease_expires_at
                    else None
                ),
                "locked_by": candidate.observed_locked_by,
                "mode": "execute" if execute else "dry-run",
                "reason": candidate.reason,
                "source_domain": candidate.source_domain,
                "source_id": candidate.source_id,
                "source_job_id": candidate.observed_source_job_id,
                "source_status": candidate.observed_source_status,
                "status": candidate.observed_status,
                "tenant_id": candidate.tenant_id,
                "updated_at": (
                    candidate.observed_updated_at.isoformat()
                    if candidate.observed_updated_at
                    else None
                ),
            }
            self.stdout.write(json.dumps(audit_record, sort_keys=True))

        updated = reconcile_candidates(candidates, execute=execute)
        if execute and updated != 1:
            raise CommandError(
                "exact candidate changed after planning; run dry-run again before retrying"
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"stale_ai_jobs candidates={len(candidates)} updated={updated} execute={execute}"
            )
        )
