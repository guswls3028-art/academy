"""Read-only business-state inspection with separate operational receipts."""

import json

from django.core.management.base import BaseCommand, CommandError

from apps.core.services.state_detector import run_state_detector


class Command(BaseCommand):
    help = "Inspect canonical session exam projections for one explicit tenant."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", type=int, required=True)
        parser.add_argument(
            "--limit", type=int, default=200,
            help="Rows per keyset page (1..1000, default 200); all pages share one bounded read-only snapshot.",
        )
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        report = run_state_detector(tenant_id=options["tenant"], limit=options["limit"], dry_run=options["dry_run"])
        self.stdout.write(json.dumps(report, sort_keys=True))
        if report["inspection_status"] == "failed" or report["delivery_status"] in {"failed", "unknown"}:
            raise CommandError("State inspection or alert delivery failed; see the PII-free report.")
