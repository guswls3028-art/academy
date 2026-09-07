from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from django.db import transaction
from django.utils import timezone

from apps.domains.results.models import (
    ExamAttempt,
    ExamResult,
    Result,
    ResultFact,
    ResultItem,
)
from apps.support.omr.score_shape import get_exam_score_shape
from apps.support.results.exam_policy_dependencies import effective_exam_pass_score
from apps.support.results.omr_subjective_completion_dependencies import (
    enrollments_by_id,
    exams_by_id,
    submissions_by_id,
)


@dataclass(frozen=True)
class OmrSubjectiveCompletionState:
    result_id: int
    submission_id: int | None
    is_omr: bool
    scope_valid: bool
    manual_review_required: bool
    required_question_ids: frozenset[int]
    completed_question_ids: frozenset[int]
    aggregate_score_recorded: bool

    @property
    def subjective_complete(self) -> bool:
        if not self.required_question_ids:
            return True
        return self.aggregate_score_recorded or self.required_question_ids.issubset(
            self.completed_question_ids
        )

    @property
    def projection_ready(self) -> bool:
        return bool(
            self.is_omr
            and self.scope_valid
            and not self.manual_review_required
            and self.subjective_complete
        )

    @property
    def pending(self) -> bool:
        return bool(self.is_omr and not self.projection_ready)


@dataclass(frozen=True)
class OmrFinalizationDecision:
    projection_ready: bool
    transitioned: bool
    pending_reason: str | None = None


def _attempt_has_aggregate_subjective_score(attempt: ExamAttempt) -> bool:
    meta = attempt.meta if isinstance(attempt.meta, dict) else {}
    if "subjective_score" in meta:
        return True

    initial = (
        meta.get("initial_snapshot")
        if isinstance(meta.get("initial_snapshot"), dict)
        else {}
    )
    if initial.get("source") in {"admin_manual_subjective", "admin_manual_total"}:
        return True

    placeholder = (
        meta.get("manual_score_placeholder")
        if isinstance(meta.get("manual_score_placeholder"), dict)
        else {}
    )
    previous_initial = (
        placeholder.get("previous_initial_snapshot")
        if isinstance(placeholder.get("previous_initial_snapshot"), dict)
        else {}
    )
    return previous_initial.get("source") == "admin_manual_subjective"


def omr_subjective_completion_states(
    results: Iterable[Result],
) -> dict[int, OmrSubjectiveCompletionState]:
    """Return fail-closed projection readiness for canonical result snapshots."""

    result_rows = [result for result in results if result and result.id]
    if not result_rows:
        return {}

    attempt_ids = {
        int(result.attempt_id)
        for result in result_rows
        if result.attempt_id
    }
    attempts = ExamAttempt.objects.filter(id__in=attempt_ids).in_bulk()
    submission_ids = {
        int(attempt.submission_id)
        for attempt in attempts.values()
        if attempt.submission_id
    }
    submissions = submissions_by_id(submission_ids)
    omr_result_ids = {
        int(result.id)
        for result in result_rows
        if (
            (attempt := attempts.get(int(result.attempt_id or 0))) is not None
            and (submission := submissions.get(int(attempt.submission_id or 0)))
            is not None
            and submission.source == "omr_scan"
        )
    }
    omr_exam_ids = {
        int(result.target_id)
        for result in result_rows
        if int(result.id) in omr_result_ids and result.target_type == "exam"
    }
    exams = exams_by_id(omr_exam_ids)
    omr_enrollment_ids = {
        int(result.enrollment_id)
        for result in result_rows
        if int(result.id) in omr_result_ids and result.enrollment_id
    }
    enrollments = enrollments_by_id(omr_enrollment_ids)

    completed_by_result: dict[int, set[int]] = defaultdict(set)
    for result_id, question_id in ResultItem.objects.filter(
        result_id__in=omr_result_ids,
    ).values_list("result_id", "question_id"):
        completed_by_result[int(result_id)].add(int(question_id))

    omr_attempt_ids = {
        int(result.attempt_id)
        for result in result_rows
        if int(result.id) in omr_result_ids and result.attempt_id
    }
    aggregate_attempt_ids = set(
        ResultFact.objects.filter(
            attempt_id__in=omr_attempt_ids,
            source__in=("manual_subjective", "manual_total"),
        ).values_list("attempt_id", flat=True)
    )

    required_by_exam: dict[int, frozenset[int]] = {}
    for exam_id, exam in exams.items():
        score_shape = get_exam_score_shape(exam)
        required_by_exam[int(exam_id)] = frozenset(
            int(question_id)
            for question_id, kind in score_shape.question_kind_by_id.items()
            if kind == "essay" and score_shape.subjective_max_score > 0
        )

    states: dict[int, OmrSubjectiveCompletionState] = {}
    for result in result_rows:
        attempt = attempts.get(int(result.attempt_id or 0))
        submission = (
            submissions.get(int(attempt.submission_id or 0))
            if attempt is not None
            else None
        )
        exam = exams.get(int(result.target_id))
        enrollment = enrollments.get(int(result.enrollment_id or 0))
        is_omr = bool(
            submission is not None
            and submission.source == "omr_scan"
        )
        scope_valid = bool(
            is_omr
            and result.target_type == "exam"
            and attempt is not None
            and exam is not None
            and enrollment is not None
            and submission.target_type == "exam"
            and int(submission.target_id) == int(result.target_id)
            and int(submission.enrollment_id or 0) == int(result.enrollment_id or 0)
            and int(attempt.exam_id) == int(result.target_id)
            and int(attempt.enrollment_id) == int(result.enrollment_id or 0)
            and int(submission.tenant_id) == int(exam.tenant_id)
            and int(enrollment.tenant_id) == int(exam.tenant_id)
        )
        manual_review = (
            submission.meta.get("manual_review")
            if is_omr
            and isinstance(submission.meta, dict)
            and isinstance(submission.meta.get("manual_review"), dict)
            else {}
        )
        aggregate_recorded = bool(
            attempt is not None
            and (
                int(attempt.id) in aggregate_attempt_ids
                or _attempt_has_aggregate_subjective_score(attempt)
            )
        )
        states[int(result.id)] = OmrSubjectiveCompletionState(
            result_id=int(result.id),
            submission_id=int(submission.id) if submission is not None else None,
            is_omr=is_omr,
            scope_valid=scope_valid,
            manual_review_required=bool(manual_review.get("required") is True),
            required_question_ids=(
                required_by_exam.get(int(result.target_id), frozenset())
                if is_omr
                else frozenset()
            ),
            completed_question_ids=frozenset(
                completed_by_result.get(int(result.id), set())
            ),
            aggregate_score_recorded=aggregate_recorded,
        )
    return states


def pending_omr_result_ids(results: Iterable[Result]) -> set[int]:
    return {
        result_id
        for result_id, state in omr_subjective_completion_states(results).items()
        if state.pending
    }


def pending_omr_result_ids_for_ids(result_ids: Iterable[int]) -> set[int]:
    """Batch-load result rows and return mixed OMR snapshots not safe to publish."""

    normalized_ids = {int(result_id) for result_id in result_ids if result_id}
    if not normalized_ids:
        return set()
    return pending_omr_result_ids(
        Result.objects.filter(id__in=normalized_ids, target_type="exam")
    )


def pending_omr_enrollment_ids_for_exams(
    *,
    exam_ids: Iterable[int],
    tenant_id: int,
    enrollment_ids: Iterable[int] | None = None,
) -> set[int]:
    """Return enrollments whose current exam snapshot is not projection-ready."""

    normalized_exam_ids = {int(exam_id) for exam_id in exam_ids if exam_id}
    if not normalized_exam_ids:
        return set()
    queryset = Result.objects.filter(
        target_type="exam",
        target_id__in=normalized_exam_ids,
        enrollment__tenant_id=int(tenant_id),
    )
    if enrollment_ids is not None:
        normalized_enrollment_ids = {
            int(enrollment_id) for enrollment_id in enrollment_ids if enrollment_id
        }
        if not normalized_enrollment_ids:
            return set()
        queryset = queryset.filter(enrollment_id__in=normalized_enrollment_ids)
    results = list(queryset)
    pending_result_ids = pending_omr_result_ids(results)
    return {
        int(result.enrollment_id)
        for result in results
        if int(result.id) in pending_result_ids
    }


@transaction.atomic
def finalize_omr_result_if_ready(*, result_id: int) -> OmrFinalizationDecision:
    """Finalize one OMR snapshot only after every required grading component exists."""

    result = (
        Result.objects.select_for_update()
        .get(id=int(result_id), target_type="exam")
    )
    if result.attempt_id:
        ExamAttempt.objects.select_for_update().get(id=int(result.attempt_id))
    state = omr_subjective_completion_states([result])[int(result.id)]
    if not state.is_omr:
        return OmrFinalizationDecision(projection_ready=True, transitioned=False)
    if not state.scope_valid:
        return OmrFinalizationDecision(
            projection_ready=False,
            transitioned=False,
            pending_reason="invalid_omr_scope",
        )
    if state.manual_review_required:
        return OmrFinalizationDecision(
            projection_ready=False,
            transitioned=False,
            pending_reason="manual_review_required",
        )
    if not state.subjective_complete:
        return OmrFinalizationDecision(
            projection_ready=False,
            transitioned=False,
            pending_reason="subjective_pending",
        )

    legacy = (
        ExamResult.objects.select_for_update()
        .filter(
            submission_id=int(state.submission_id or 0),
            exam_id=int(result.target_id),
        )
        .first()
    )
    if legacy is None:
        return OmrFinalizationDecision(
            projection_ready=False,
            transitioned=False,
            pending_reason="exam_result_missing",
        )

    transitioned = legacy.status != ExamResult.Status.FINAL
    objective_score = float(result.objective_score or 0.0)
    total_score = float(result.total_score or 0.0)
    max_score = float(result.max_score or 0.0)
    pass_score = effective_exam_pass_score(
        exam=legacy.exam,
        lecture_id=getattr(result.enrollment, "lecture_id", None),
    )
    legacy.objective_score = objective_score
    legacy.subjective_score = max(0.0, total_score - objective_score)
    legacy.total_score = total_score
    legacy.max_score = max_score
    legacy.is_passed = total_score >= pass_score if pass_score > 0 else True
    legacy.status = ExamResult.Status.FINAL
    if transitioned or legacy.finalized_at is None:
        legacy.finalized_at = timezone.now()
    legacy.save(
        update_fields=[
            "objective_score",
            "subjective_score",
            "total_score",
            "max_score",
            "is_passed",
            "status",
            "finalized_at",
            "updated_at",
        ]
    )
    return OmrFinalizationDecision(
        projection_ready=True,
        transitioned=transitioned,
    )
