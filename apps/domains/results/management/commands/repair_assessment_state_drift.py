"""
Repair assessment lifecycle drift.

Default mode is dry-run. Use --apply to mutate data.
"""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.domains.progress.dispatcher import resolve_removed_source_clinic_links
from apps.domains.results.utils.clinic import classify_source_links


class Command(BaseCommand):
    help = "Repair stale assessment lifecycle state. Dry-run unless --apply is provided."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", type=int, default=None)
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--json", action="store_true", dest="as_json")
        parser.add_argument("--sample", type=int, default=10)

    def handle(self, *args, **options):
        Exam = apps.get_model("exams", "Exam")
        Homework = apps.get_model("homework_results", "Homework")
        ClinicLink = apps.get_model("progress", "ClinicLink")
        Tenant = apps.get_model("core", "Tenant")

        tenant_id = options.get("tenant")
        apply = bool(options.get("apply"))
        sample_size = max(1, int(options.get("sample") or 10))

        exam_qs = Exam.objects.all()
        homework_qs = Homework.objects.all()
        link_qs = ClinicLink.objects.filter(is_auto=True, resolved_at__isnull=True)
        if tenant_id is not None:
            exam_qs = exam_qs.filter(tenant_id=tenant_id)
            homework_qs = homework_qs.filter(tenant_id=tenant_id)
            link_qs = link_qs.filter(tenant_id=tenant_id)

        inactive_linked_exams = list(
            exam_qs.filter(
                exam_type="regular",
                is_active=False,
                sessions__isnull=False,
            ).distinct().prefetch_related("sessions")
        )
        template_linked_exams = list(
            exam_qs.filter(
                exam_type="template",
                sessions__isnull=False,
            ).distinct().prefetch_related("sessions")
        )

        detached_pairs: list[dict[str, int]] = []
        for exam in [*inactive_linked_exams, *template_linked_exams]:
            for session_id in exam.sessions.values_list("id", flat=True):
                detached_pairs.append({
                    "exam_id": int(exam.id),
                    "session_id": int(session_id),
                    "tenant_id": int(exam.tenant_id),
                })

        def source_id(link: Any, source_type: str) -> int | None:
            meta = link.meta if isinstance(getattr(link, "meta", None), dict) else {}
            if link.source_type == source_type:
                raw = link.source_id or meta.get(f"{source_type}_id")
            elif link.source_type is None:
                raw = meta.get(f"{source_type}_id")
            else:
                raw = None
            try:
                return int(raw) if raw is not None else None
            except (TypeError, ValueError):
                return None

        links = list(link_qs.only(
            "id",
            "tenant_id",
            "session_id",
            "enrollment_id",
            "source_type",
            "source_id",
            "meta",
            "resolution_history",
        ).order_by("id"))
        links_by_tenant: dict[int, list[Any]] = defaultdict(list)
        for link in links:
            links_by_tenant[int(link.tenant_id)].append(link)
        tenants = Tenant.objects.in_bulk(links_by_tenant.keys())
        non_live_reasons: dict[int, str] = {}
        for link_tenant_id, tenant_links in links_by_tenant.items():
            for link, reason in classify_source_links(
                tenant_links,
                tenant=tenants.get(link_tenant_id),
            ):
                if reason is not None:
                    non_live_reasons[int(link.id)] = reason

        ghost_links = []
        for link in links:
            non_live_reason = non_live_reasons.get(int(link.id))
            if non_live_reason is None:
                continue
            exam_id = source_id(link, "exam")
            if exam_id is not None:
                ghost_links.append((link, "exam", exam_id, non_live_reason))
                continue

            homework_id = source_id(link, "homework")
            if homework_id is not None:
                ghost_links.append((link, "homework", homework_id, non_live_reason))

        resolved_link_ids: list[int] = []
        if apply:
            with transaction.atomic():
                for exam in [*inactive_linked_exams, *template_linked_exams]:
                    session_ids = list(exam.sessions.values_list("id", flat=True))
                    if session_ids:
                        exam.sessions.remove(*session_ids)

                repair_groups: dict[tuple[int, int, str, int], set[int]] = defaultdict(set)
                for link, source_type, source_id_value, _ in ghost_links:
                    repair_groups[
                        (
                            int(link.tenant_id),
                            int(link.session_id),
                            source_type,
                            int(source_id_value),
                        )
                    ].add(int(link.enrollment_id))
                for (
                    link_tenant_id,
                    link_session_id,
                    source_type,
                    source_id_value,
                ), enrollment_ids in sorted(repair_groups.items()):
                    resolve_removed_source_clinic_links(
                        tenant_id=link_tenant_id,
                        session_id=link_session_id,
                        source_type=source_type,
                        source_id=source_id_value,
                        enrollment_ids=sorted(enrollment_ids),
                        reason="assessment_state_drift_repair",
                    )
                resolved_link_ids = list(
                    ClinicLink.objects.filter(
                        id__in=[int(link.id) for link, _, _, _ in ghost_links],
                        resolved_at__isnull=False,
                        resolution_type=ClinicLink.ResolutionType.SOURCE_REMOVED,
                    )
                    .order_by("id")
                    .values_list("id", flat=True)
                )

        report = {
            "tenant": tenant_id if tenant_id is not None else "all",
            "mode": "apply" if apply else "dry-run",
            "detachable_exam_session_pair_count": len(detached_pairs),
            "resolved_non_live_source_clinic_link_count": len(resolved_link_ids) if apply else len(ghost_links),
            "samples": {
                "detachable_exam_session_pairs": detached_pairs[:sample_size],
                "non_live_source_clinic_link_ids": [
                    int(link.id) for link, _, _, _ in ghost_links[:sample_size]
                ],
                "non_live_source_clinic_links": [
                    {
                        "id": int(link.id),
                        "tenant_id": int(link.tenant_id),
                        "session_id": int(link.session_id),
                        "enrollment_id": int(link.enrollment_id),
                        "source_type": source_type,
                        "source_id": int(source_id_value),
                        "state_reason": non_live_reason,
                    }
                    for link, source_type, source_id_value, non_live_reason
                    in ghost_links[:sample_size]
                ],
                "resolved_non_live_source_clinic_link_ids": resolved_link_ids[:sample_size],
            },
        }

        if options.get("as_json"):
            self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
            return

        self.stdout.write("Assessment lifecycle repair report")
        self.stdout.write(f"tenant={report['tenant']}")
        self.stdout.write(f"mode={report['mode']}")
        self.stdout.write(
            f"detachable_exam_session_pair_count={report['detachable_exam_session_pair_count']}"
        )
        self.stdout.write(
            "resolved_non_live_source_clinic_link_count="
            f"{report['resolved_non_live_source_clinic_link_count']}"
        )
        self.stdout.write("samples=" + json.dumps(report["samples"], ensure_ascii=False))
