"""Recompute stale progress/clinic projections for incomplete mixed OMR results."""

from __future__ import annotations

import json

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from apps.domains.progress.dispatcher import dispatch_progress_pipeline
from apps.domains.results.models import Result
from apps.domains.results.services.omr_subjective_completion import (
    pending_omr_result_ids,
)


class Command(BaseCommand):
    help = (
        "Find mixed OMR results still awaiting subjective grading and reconcile "
        "their derived progress/clinic rows. Dry-run unless --apply is provided."
    )

    def add_arguments(self, parser):
        parser.add_argument("--tenant", type=int, required=True)
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--json", action="store_true", dest="as_json")
        parser.add_argument("--sample", type=int, default=10)

    def handle(self, *args, **options):
        tenant_id = int(options["tenant"])
        apply = bool(options["apply"])
        sample_size = max(1, int(options.get("sample") or 10))

        Tenant = apps.get_model("core", "Tenant")
        Session = apps.get_model("lectures", "Session")
        SessionProgress = apps.get_model("progress", "SessionProgress")
        ClinicLink = apps.get_model("progress", "ClinicLink")

        if not Tenant.objects.filter(id=tenant_id).exists():
            raise CommandError(f"tenant not found: {tenant_id}")

        results = list(
            Result.objects.filter(
                target_type="exam",
                enrollment__tenant_id=tenant_id,
            ).select_related("enrollment")
        )
        pending_ids = pending_omr_result_ids(results)
        pending_results = [
            result for result in results if int(result.id) in pending_ids
        ]

        targets: set[tuple[int, int, int]] = set()
        for result in pending_results:
            session_ids = Session.objects.filter(
                lecture__tenant_id=tenant_id,
                lecture_id=int(result.enrollment.lecture_id),
                exams__id=int(result.target_id),
            ).values_list("id", flat=True)
            targets.update(
                (
                    int(result.target_id),
                    int(result.enrollment_id),
                    int(session_id),
                )
                for session_id in session_ids
            )

        progress_scope = Q(pk__in=[])
        clinic_scope = Q(pk__in=[])
        for exam_id, enrollment_id, session_id in targets:
            progress_scope |= Q(
                enrollment_id=enrollment_id,
                session_id=session_id,
            )
            clinic_scope |= Q(
                enrollment_id=enrollment_id,
                session_id=session_id,
            ) & (
                Q(source_type="exam", source_id=exam_id)
                | Q(source_type__isnull=True, meta__exam_id=exam_id)
            )
        stale_progress_count = SessionProgress.objects.filter(
            progress_scope,
        ).exclude(exam_aggregate_score__isnull=True).count()
        stale_clinic_count = ClinicLink.objects.filter(
            clinic_scope,
            tenant_id=tenant_id,
            is_auto=True,
            resolved_at__isnull=True,
        ).count()

        reconciled_count = 0
        if apply:
            for _exam_id, enrollment_id, session_id in sorted(targets):
                dispatch_progress_pipeline(
                    enrollment_id=enrollment_id,
                    session_id=session_id,
                )
                reconciled_count += 1

        payload = {
            "ok": True,
            "dry_run": not apply,
            "tenant_id": tenant_id,
            "pending_result_count": len(pending_results),
            "target_count": len(targets),
            "stale_progress_count": stale_progress_count,
            "stale_clinic_count": stale_clinic_count,
            "reconciled_count": reconciled_count,
            "sample": [
                {
                    "exam_id": exam_id,
                    "enrollment_id": enrollment_id,
                    "session_id": session_id,
                }
                for exam_id, enrollment_id, session_id in sorted(targets)[:sample_size]
            ],
        }
        rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if options.get("as_json"):
            self.stdout.write(rendered)
        else:
            self.stdout.write(self.style.SUCCESS(rendered))
