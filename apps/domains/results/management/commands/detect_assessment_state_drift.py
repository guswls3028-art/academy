"""
Read-only assessment lifecycle drift report.

Business SSOT:
- live exam = regular + is_active + linked to session
- live exam target = explicit ExamEnrollment, or the legacy roster only when
  the exam has no explicit targets at all
- live homework = regular + session + not removed_from_session_at
- live homework target = exact HomeworkAssignment for the student and session
- clinic target = unresolved automatic ClinicLink whose source and student
  assignment are both live
"""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from django.apps import apps
from django.core.management.base import BaseCommand

from apps.domains.results.utils.clinic import classify_source_links


class Command(BaseCommand):
    help = "Read-only drift report for assessment lifecycle state."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", type=int, default=None)
        parser.add_argument("--json", action="store_true", dest="as_json")
        parser.add_argument("--sample", type=int, default=10)

    def handle(self, *args, **options):
        Exam = apps.get_model("exams", "Exam")
        HomeworkAssignment = apps.get_model("homework", "HomeworkAssignment")
        Homework = apps.get_model("homework_results", "Homework")
        ClinicLink = apps.get_model("progress", "ClinicLink")
        Tenant = apps.get_model("core", "Tenant")

        tenant_id = options.get("tenant")
        sample_size = max(1, int(options.get("sample") or 10))

        exam_qs = Exam.objects.all()
        homework_qs = Homework.objects.all()
        link_qs = ClinicLink.objects.filter(is_auto=True, resolved_at__isnull=True)
        if tenant_id is not None:
            exam_qs = exam_qs.filter(tenant_id=tenant_id)
            homework_qs = homework_qs.filter(tenant_id=tenant_id)
            link_qs = link_qs.filter(tenant_id=tenant_id)

        inactive_linked_exams = exam_qs.filter(
            exam_type="regular",
            is_active=False,
            sessions__isnull=False,
        ).distinct()
        template_linked_exams = exam_qs.filter(
            exam_type="template",
            sessions__isnull=False,
        ).distinct()
        removed_homework_with_assignments = homework_qs.exclude(
            meta__removed_from_session_at__isnull=False
        ).none()
        removed_homework_ids = list(
            homework_qs.exclude(meta__removed_from_session_at__isnull=True)
            .values_list("id", flat=True)
        )
        if removed_homework_ids:
            assignment_homework_ids = set(
                HomeworkAssignment.objects.filter(
                    homework_id__in=removed_homework_ids
                ).values_list("homework_id", flat=True)
            )
            removed_homework_with_assignments = homework_qs.filter(
                id__in=assignment_homework_ids
            )

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

        ghost_links: list[dict[str, Any]] = []
        for link in links:
            non_live_reason = non_live_reasons.get(int(link.id))
            if non_live_reason is None:
                continue
            exam_id = source_id(link, "exam")
            if exam_id is not None:
                ghost_links.append({
                    "id": int(link.id),
                    "tenant_id": int(link.tenant_id),
                    "session_id": int(link.session_id),
                    "enrollment_id": int(link.enrollment_id),
                    "source_type": "exam",
                    "source_id": exam_id,
                    "state_reason": non_live_reason,
                })
                continue

            homework_id = source_id(link, "homework")
            if homework_id is not None:
                ghost_links.append({
                    "id": int(link.id),
                    "tenant_id": int(link.tenant_id),
                    "session_id": int(link.session_id),
                    "enrollment_id": int(link.enrollment_id),
                    "source_type": "homework",
                    "source_id": homework_id,
                    "state_reason": non_live_reason,
                })

        reason_counts: dict[str, int] = defaultdict(int)
        for row in ghost_links:
            reason_counts[str(row["state_reason"])] += 1

        report = {
            "tenant": tenant_id if tenant_id is not None else "all",
            "inactive_regular_linked_exam_count": inactive_linked_exams.count(),
            "template_linked_exam_count": template_linked_exams.count(),
            "removed_homework_with_assignment_count": removed_homework_with_assignments.count(),
            "unresolved_non_live_source_clinic_link_count": len(ghost_links),
            "unresolved_non_live_source_clinic_link_reason_counts": dict(
                sorted(reason_counts.items())
            ),
            "samples": {
                "inactive_regular_linked_exam_ids": list(
                    inactive_linked_exams.values_list("id", flat=True)[:sample_size]
                ),
                "template_linked_exam_ids": list(
                    template_linked_exams.values_list("id", flat=True)[:sample_size]
                ),
                "removed_homework_with_assignment_ids": list(
                    removed_homework_with_assignments.values_list("id", flat=True)[:sample_size]
                ),
                "unresolved_non_live_source_clinic_links": ghost_links[:sample_size],
            },
        }

        if options.get("as_json"):
            self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
            return

        self.stdout.write("Assessment lifecycle drift report")
        self.stdout.write(f"tenant={report['tenant']}")
        for key, value in report.items():
            if key in {"tenant", "samples"}:
                continue
            self.stdout.write(f"{key}={value}")
        self.stdout.write("samples=" + json.dumps(report["samples"], ensure_ascii=False))
        self.stdout.write("Report complete. No data was modified.")
