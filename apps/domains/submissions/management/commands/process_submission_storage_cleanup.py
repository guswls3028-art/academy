from django.core.management.base import BaseCommand

from apps.domains.submissions.services.lifecycle import (
    process_submission_storage_cleanup_intents,
)


class Command(BaseCommand):
    help = "Retry durable submission R2 cleanup intents."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100, help="Maximum intents per run")

    def handle(self, *args, **options):
        result = process_submission_storage_cleanup_intents(limit=options["limit"])
        self.stdout.write(
            self.style.SUCCESS(
                "submission storage cleanup "
                f"cleaned={result.cleaned} failed={result.failed} deferred={result.deferred}"
            )
        )
