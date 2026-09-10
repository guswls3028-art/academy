"""Cross-domain read helpers for session progress calculation."""

from __future__ import annotations


def get_result_attempt_models():
    from apps.domains.results.models import ExamAttempt, Result

    return Result, ExamAttempt


def get_exam_model():
    from apps.domains.exams.models import Exam

    return Exam


def get_exam_ids_for_session(session) -> list[int]:
    from apps.domains.results.utils.session_exam import (
        get_exam_ids_for_session as get_ids,
    )

    return get_ids_for_session_as_ints(get_ids(session))


def get_target_exam_ids_for_session_enrollment(*, session, enrollment_id: int) -> list[int]:
    """Return only exams that apply to this enrollment in the session.

    Legacy exams without any explicit ``ExamEnrollment`` rows continue to use
    the session roster. Once an exam has an explicit target list, that list is
    authoritative and non-target students must not inherit the exam merely
    because they attend the same session.
    """
    from apps.domains.exams.models import ExamEnrollment
    from apps.domains.results.utils.session_exam import get_exam_ids_for_session

    exam_ids = get_ids_for_session_as_ints(get_exam_ids_for_session(session))
    if not exam_ids:
        return []

    explicit_rows = list(
        ExamEnrollment.objects.filter(exam_id__in=exam_ids)
        .values_list("exam_id", "enrollment_id")
    )
    return target_exam_ids_from_rows(
        exam_ids=exam_ids, explicit_rows=explicit_rows, enrollment_id=enrollment_id,
    )


def target_exam_ids_from_rows(*, exam_ids, explicit_rows, enrollment_id: int) -> list[int]:
    """Canonical explicit/legacy membership for ORM and bounded snapshot readers."""
    explicitly_targeted_exam_ids = {int(exam_id) for exam_id, _ in explicit_rows}
    enrollment_exam_ids = {
        int(exam_id)
        for exam_id, row_enrollment_id in explicit_rows
        if int(row_enrollment_id) == int(enrollment_id)
    }
    return [
        exam_id
        for exam_id in exam_ids
        if exam_id not in explicitly_targeted_exam_ids
        or exam_id in enrollment_exam_ids
    ]


def get_exam_target_enrollment_pairs_for_session(
    *,
    session,
    enrollment_ids: list[int] | set[int],
) -> set[tuple[int, int]]:
    """Return canonical ``(enrollment_id, exam_id)`` target pairs for a session."""
    from apps.domains.exams.models import ExamEnrollment
    from apps.domains.results.utils.session_exam import get_exam_ids_for_session

    exam_ids = get_ids_for_session_as_ints(get_exam_ids_for_session(session))
    roster_ids = {int(enrollment_id) for enrollment_id in enrollment_ids}
    if not exam_ids or not roster_ids:
        return set()

    explicit_rows = list(
        ExamEnrollment.objects.filter(
            exam_id__in=exam_ids,
            enrollment_id__in=roster_ids,
        ).values_list("enrollment_id", "exam_id")
    )
    explicitly_targeted_exam_ids = set(
        ExamEnrollment.objects.filter(exam_id__in=exam_ids)
        .values_list("exam_id", flat=True)
        .distinct()
    )
    pairs = {
        (int(enrollment_id), int(exam_id))
        for enrollment_id, exam_id in explicit_rows
    }
    pairs.update(
        (enrollment_id, exam_id)
        for enrollment_id in roster_ids
        for exam_id in exam_ids
        if exam_id not in explicitly_targeted_exam_ids
    )
    return pairs


def get_ids_for_session_as_ints(exam_ids) -> list[int]:
    return [int(exam_id) for exam_id in exam_ids]


def homework_score_exists(**filters) -> bool:
    from apps.domains.homework_results.models import HomeworkScore

    return HomeworkScore.objects.filter(**filters).exists()
