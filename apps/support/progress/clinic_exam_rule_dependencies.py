"""Cross-domain reads for clinic exam rule evaluation."""

from __future__ import annotations

from django.db.models import Count


def exam_result_for_rule(*, enrollment_id: int, exam_id: int):
    from apps.domains.results.models import Result
    from apps.domains.results.services.omr_subjective_completion import (
        pending_omr_result_ids,
    )

    result = Result.objects.filter(
        enrollment_id=enrollment_id,
        target_type="exam",
        target_id=exam_id,
    ).first()
    if result and int(result.id) in pending_omr_result_ids([result]):
        return None
    return result


def exam_pass_score(*, exam_id: int) -> float:
    from apps.domains.exams.models import Exam

    exam = Exam.objects.filter(id=exam_id).first()
    return float(getattr(exam, "pass_score", 0) or 0)


def low_confidence_fact_count(*, enrollment_id: int, exam_id: int) -> int:
    from apps.domains.results.models import ResultFact

    return ResultFact.objects.filter(
        enrollment_id=enrollment_id,
        target_type="exam",
        target_id=exam_id,
        meta__grading__invalid_reason__in=["LOW_CONFIDENCE", "AMBIGUOUS_SINGLE"],
    ).count()


def repeated_wrong_question_ids(*, enrollment_id: int, exam_id: int) -> list[int]:
    from apps.domains.results.models import ResultFact

    repeated = (
        ResultFact.objects
        .filter(
            enrollment_id=enrollment_id,
            target_type="exam",
            target_id=exam_id,
            is_correct=False,
        )
        .values("question_id")
        .annotate(cnt=Count("attempt_id", distinct=True))
        .filter(cnt__gte=2)
    )
    return [row["question_id"] for row in repeated]
