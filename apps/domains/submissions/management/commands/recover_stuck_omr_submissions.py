"""
OMR pipeline state recovery management command.

5분마다 cron / EventBridge 에서 호출. 30 분 이상 SUBMITTED / DISPATCHED /
EXTRACTING / GRADING 에 박혀있는 OMR submission 을 FAILED 로 자동 전환하고
운영 알람용 audit 로깅을 남긴다.

사용:
    python manage.py recover_stuck_omr_submissions          # 실행
    python manage.py recover_stuck_omr_submissions --dry-run  # 점검
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from academy.application.use_cases.omr.late_answer_recovery import (
    recover_late_ai_answers,
)
from apps.domains.submissions.omr_pipeline.services.state_recovery import (
    recover_stuck_submissions,
)


class Command(BaseCommand):
    help = "Auto-fail OMR submissions stuck in pending states beyond timeout."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Only report stuck submissions, do not change status.",
        )
        parser.add_argument(
            "--actor",
            default="cron.recover_stuck_omr",
            help="Audit actor tag written to meta.state_recovery.actor.",
        )
        parser.add_argument(
            "--skip-late-ai-answer-recovery",
            action="store_true",
            help="Skip recovering DONE/ANSWERS_READY OMR submissions with late AI answers.",
        )
        parser.add_argument(
            "--late-lookback-days",
            type=int,
            default=14,
            help="Days to scan for late AI answer recovery candidates.",
        )
        parser.add_argument(
            "--late-limit",
            type=int,
            default=100,
            help="Max late AI answer recovery candidates per command run.",
        )

    def handle(self, *args, **options):
        report = recover_stuck_submissions(
            actor=options["actor"],
            dry_run=options["dry_run"],
        )
        self.stdout.write(
            f"detected={len(report.detected)} "
            f"recovered={len(report.recovered)} "
            f"skipped={len(report.skipped)} "
            f"failed_transitions={len(report.failed_transitions)}"
        )
        for alert in report.detected:
            self.stdout.write(
                f"  sub={alert.submission_id} status={alert.status} "
                f"age_min={alert.age_min} tenant={alert.tenant_id}"
            )
        for sub_id, err in report.failed_transitions[:20]:
            self.stdout.write(
                self.style.WARNING(f"  transition failed sub={sub_id} reason={err}")
            )

        if options["skip_late_ai_answer_recovery"]:
            return

        late_report = recover_late_ai_answers(
            actor=options["actor"],
            dry_run=options["dry_run"],
            lookback_days=options["late_lookback_days"],
            limit=options["late_limit"],
        )
        self.stdout.write(
            f"late_ai_detected={len(late_report.detected)} "
            f"late_ai_recovered={len(late_report.recovered)} "
            f"late_ai_skipped={len(late_report.skipped)} "
            f"late_ai_failed={len(late_report.failed)}"
        )
        for candidate in late_report.detected[:20]:
            self.stdout.write(
                f"  late_ai sub={candidate.submission_id} status={candidate.status} "
                f"answers={candidate.answers_count} job={candidate.ai_job_id} "
                f"tenant={candidate.tenant_id}"
            )
        for sub_id, reason in late_report.skipped[:20]:
            self.stdout.write(
                self.style.WARNING(f"  late_ai skipped sub={sub_id} reason={reason}")
            )
        for sub_id, err in late_report.failed[:20]:
            self.stdout.write(
                self.style.WARNING(f"  late_ai failed sub={sub_id} reason={err}")
            )
