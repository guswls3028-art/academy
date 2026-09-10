"""Create only missing legacy parent accounts without changing existing credentials.

The command is dry-run by default and requires an exact tenant confirmation for
execution. Existing Parent users and their passwords are never changed.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from academy.adapters.db.django import repositories_core as core_repo
from apps.domains.students.models import Student
from apps.support.students.lifecycle_dependencies import (
    ensure_parent_account_for_student,
)
from apps.support.students.namespace_lock import (
    lock_student_creation_tenant_reference,
)


def _normalize_phone(raw: str) -> str:
    return "".join(character for character in str(raw or "") if character.isdigit())


def _is_recovery_phone(phone: str) -> bool:
    return len(phone) == 11 and phone.startswith("010")


class Command(BaseCommand):
    help = (
        "Dry-run by default. Create missing parent accounts for one exact tenant "
        "without changing existing parent passwords."
    )

    def add_arguments(self, parser):
        parser.add_argument("--tenant", required=True, help="Exact active Tenant.code")
        parser.add_argument("--execute", action="store_true")
        parser.add_argument(
            "--confirm",
            default="",
            help="For --execute, repeat the exact tenant code",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Compatibility flag; dry-run is already the default",
        )

    def handle(self, *args, **options):
        tenant_code = str(options["tenant"] or "").strip()
        execute = bool(options["execute"])
        if execute and options.get("dry_run"):
            raise CommandError("choose_exactly_one:--execute_or_--dry-run")
        if execute and str(options.get("confirm") or "").strip() != tenant_code:
            raise CommandError("confirmation_required:--confirm must equal --tenant")

        tenant = core_repo.tenant_get_by_code(tenant_code)
        if tenant is None:
            raise CommandError(f"tenant_not_found_or_inactive:{tenant_code}")

        students_qs = (
            Student.objects.filter(
                tenant=tenant,
                deleted_at__isnull=True,
            )
            .exclude(parent_phone__isnull=True)
            .exclude(parent_phone="")
            .select_related("tenant")
            .order_by("id")
        )
        candidates, invalid_count, already_linked = self._candidate_rows(
            tenant=tenant,
            students=list(students_qs),
        )
        mode = "execute" if execute else "dry-run"
        self.stdout.write(
            f"parent_account_repair tenant={tenant.code} mode={mode} "
            f"candidates={len(candidates)} invalid_phone={invalid_count} "
            f"already_linked={already_linked}"
        )
        if not execute or not candidates:
            return

        try:
            with transaction.atomic():
                lock_student_creation_tenant_reference(tenant_id=tenant.id)
                locked_students = list(students_qs.select_for_update())
                locked_candidates, locked_invalid, locked_linked = self._candidate_rows(
                    tenant=tenant,
                    students=locked_students,
                )
                if (
                    [phone for phone, _student in locked_candidates]
                    != [phone for phone, _student in candidates]
                    or locked_invalid != invalid_count
                    or locked_linked != already_linked
                ):
                    raise CommandError("repair_candidates_changed:rerun_dry_run")

                created = 0
                for parent_phone, student in locked_candidates:
                    result = ensure_parent_account_for_student(
                        tenant=tenant,
                        parent_phone=parent_phone,
                        student_name=student.name or "학생",
                    )
                    if not result.parent.user_id:
                        raise CommandError(
                            f"parent_user_missing_after_ensure:student_id={student.id}"
                        )
                    if result.user_created:
                        created += 1
                if created != len(locked_candidates):
                    raise CommandError(
                        "repair_result_changed:existing account appeared during execution"
                    )
        except CommandError:
            raise
        except Exception as exc:
            raise CommandError(f"parent_account_repair_failed:{exc}") from exc

        self.stdout.write(
            self.style.SUCCESS(
                f"parent_account_repair_complete tenant={tenant.code} created={created} "
                "existing_passwords_changed=0"
            )
        )

    @staticmethod
    def _candidate_rows(*, tenant, students: list[Student]):
        candidates_by_phone: dict[str, Student] = {}
        invalid_count = 0
        already_linked = 0
        for student in students:
            phone = _normalize_phone(student.parent_phone)
            if not _is_recovery_phone(phone):
                invalid_count += 1
                continue
            existing_parent = core_repo.parent_get_by_tenant_phone(tenant, phone)
            if existing_parent and existing_parent.user_id:
                already_linked += 1
                continue
            candidates_by_phone.setdefault(phone, student)
        return (
            sorted(candidates_by_phone.items(), key=lambda item: item[0]),
            invalid_count,
            already_linked,
        )
