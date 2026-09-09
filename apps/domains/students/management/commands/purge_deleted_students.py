# PATH: apps/domains/students/management/commands/purge_deleted_students.py
"""
삭제된 학생 중 30일 경과분 영구 삭제.

- soft delete된 학생(deleted_at 설정) 중 deleted_at이 30일 초과된 경우 영구 삭제
- cron으로 매일 실행 권장

사용:
  python manage.py purge_deleted_students
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from academy.adapters.db.django import repositories_students as student_repo
from apps.domains.students.services import permanently_delete_students
from apps.support.students.lifecycle_dependencies import (
    process_pending_submission_storage_cleanup,
)


class Command(BaseCommand):
    help = "30일 초과된 삭제된 학생을 영구 삭제합니다."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=30,
            help="이 기간(일) 초과 시 삭제 (기본 30)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="실제 삭제 없이 대상만 출력",
        )

    def handle(self, *args, **options):
        days = options["days"]
        dry_run = options["dry_run"]
        cutoff = timezone.now() - timedelta(days=days)
        to_purge = list(student_repo.student_filter_deleted_before_cutoff(cutoff))

        if not dry_run:
            cleanup = process_pending_submission_storage_cleanup(limit=1000)
            self.stdout.write(
                "제출 저장소 재시도: "
                f"cleaned={cleanup.cleaned} failed={cleanup.failed} deferred={cleanup.deferred}"
            )

        if not to_purge:
            self.stdout.write(f"삭제 대상 없음 (deleted_at < {cutoff})")
            return

        self.stdout.write(f"영구 삭제 대상: {len(to_purge)}명")
        for s in to_purge[:5]:
            self.stdout.write(f"  - {s.name} (id={s.id}, deleted_at={s.deleted_at})")
        if len(to_purge) > 5:
            self.stdout.write(f"  ... 외 {len(to_purge) - 5}명")

        if dry_run:
            self.stdout.write(self.style.WARNING("--dry-run: 실제 삭제하지 않음"))
            return

        deleted = 0
        by_tenant = {}
        for student in to_purge:
            by_tenant.setdefault(student.tenant_id, {"tenant": student.tenant, "ids": []})
            by_tenant[student.tenant_id]["ids"].append(student.id)

        for group in by_tenant.values():
            result = permanently_delete_students(
                tenant=group["tenant"],
                student_ids=group["ids"],
            )
            deleted += result.deleted_count

        cleanup = process_pending_submission_storage_cleanup(limit=1000)
        self.stdout.write(
            "제출 저장소 후속 정리: "
            f"cleaned={cleanup.cleaned} failed={cleanup.failed} deferred={cleanup.deferred}"
        )

        self.stdout.write(self.style.SUCCESS(f"영구 삭제 완료: {deleted}명"))
