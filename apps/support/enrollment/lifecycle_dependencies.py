"""Cross-domain helpers for enrollment lifecycle use cases."""

from __future__ import annotations

from typing import Any


def ensure_session_roster_membership(
    *, tenant: Any, session: Any, enrollment: Any,
    allow_session_reregistration: bool = False,
):
    from apps.domains.attendance.services import (
        ensure_session_roster_membership as ensure,
    )

    return ensure(
        tenant=tenant, session=session, enrollment=enrollment,
        allow_session_reregistration=allow_session_reregistration,
    )


def auto_assign_fees_on_enrollment(tenant: Any, student: Any, lecture: Any, enrollment: Any):
    from apps.domains.fees.services import auto_assign_fees_on_enrollment as assign

    return assign(tenant, student, lecture, enrollment)


def deactivate_fees_for_enrollment(enrollment: Any):
    from apps.domains.fees.services import deactivate_fees_for_enrollment as deactivate

    return deactivate(enrollment)


def schedule_pending_account_notice(*, student_id: int) -> None:
    from apps.domains.students.services.account_notice import (
        schedule_pending_account_notice as schedule,
    )

    schedule(student_id=student_id)


def get_exam_learning_access_models():
    from apps.domains.exams.models import Exam, ExamEnrollment

    return Exam, ExamEnrollment


def get_homework_learning_access_models():
    from apps.domains.homework.models import HomeworkAssignment
    from apps.domains.homework_results.models import Homework

    return HomeworkAssignment, Homework
