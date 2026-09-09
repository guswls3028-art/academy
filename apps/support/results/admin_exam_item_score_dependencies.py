"""Cross-domain dependencies for admin exam item scoring."""

from __future__ import annotations

from typing import Any

from apps.support.results.admin_exam_dependencies import (
    dispatch_progress_pipeline,
    get_enrollment_for_tenant,
    get_latest_exam_submission_id,
    lock_regular_active_exam_for_tenant,
)


def get_answer_key_value(*, template_exam_id: int, question_id: int) -> Any | None:
    from apps.domains.exams.models import AnswerKey

    answer_key = AnswerKey.objects.filter(exam_id=template_exam_id).first()
    if not answer_key or not isinstance(answer_key.answers, dict):
        return None
    return answer_key.answers.get(str(question_id))


def get_exam_question_for_item_score(
    *,
    question_id: int,
    exam_ids: set[int],
    tenant: Any,
) -> Any | None:
    from apps.domains.exams.models import ExamQuestion

    return ExamQuestion.objects.filter(
        id=question_id,
        sheet__exam_id__in=exam_ids,
        sheet__exam__tenant=tenant,
    ).first()
