from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.domains.results.guards.exam_enrollment_guard import (
    validate_exam_enrollment_assigned,
)
from apps.domains.results.guards.score_edit_lease_guard import (
    require_score_edit_scope_available_for_exam,
)
from apps.domains.results.models import ExamAttempt, Result, ResultFact, ResultItem
from apps.domains.results.services.exam_result_excel_import import (
    Candidate,
    CorrectnessMark,
    ExamResultWorkbookError,
    QuestionSpec,
    _exam_candidates,
    _locked_result_and_attempt,
    _question_specs,
    _score_adjustments,
    _score_row,
)
from apps.domains.results.services.initial_attempt_snapshot import (
    sync_confirmed_initial_snapshot_meta,
)
from apps.support.exams.numeric_short_answer import (
    math_numeric_short_answer_question_ids,
)
from apps.support.omr.score_shape import get_exam_score_shape
from apps.support.results.admin_exam_dependencies import (
    dispatch_progress_pipeline,
    resolve_exam_not_submitted_clinic_links,
)
from apps.support.results.exam_result_excel_import_dependencies import (
    get_answer_key_answers,
    get_locked_enrollment_for_tenant,
)
from apps.support.results.manual_exam_grading_dependencies import (
    get_locked_exam_questions_for_manual_grading,
)


logger = logging.getLogger(__name__)
MAX_MANUAL_ROWS = 2_000


class ManualExamGradingError(ValueError):
    pass


@dataclass(frozen=True)
class ManualPlannedRow:
    candidate: Candidate
    expected_version: str | None
    is_not_submitted: bool
    marks: dict[int, CorrectnessMark]
    total_score: float
    max_score: float
    correct_count: int
    wrong_question_numbers: tuple[int, ...]
    review_question_numbers: tuple[int, ...]
    will_overwrite: bool


@dataclass
class ManualGradePlan:
    exam: Any
    questions: list[QuestionSpec]
    question_score_updates: dict[int, float] = field(default_factory=dict)
    original_question_scores: dict[int, float] = field(default_factory=dict)
    rows: list[ManualPlannedRow] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)

    @property
    def can_apply(self) -> bool:
        return bool(self.rows) and not self.errors

    def as_payload(self, *, applied: bool = False) -> dict[str, Any]:
        return {
            "ok": self.can_apply,
            "applied": bool(applied),
            "exam_id": int(self.exam.id),
            "exam_title": str(self.exam.title or ""),
            "grading_mode": str(self.exam.grading_mode),
            "manual_grading_method": str(self.exam.manual_grading_method),
            "matched_count": len(self.rows),
            "question_count": len(self.questions),
            "overwrite_count": sum(1 for row in self.rows if row.will_overwrite),
            "not_submitted_count": sum(
                1 for row in self.rows if row.is_not_submitted
            ),
            "errors": self.errors,
            "rows": [
                {
                    "enrollment_id": row.candidate.enrollment_id,
                    "student_name": row.candidate.student_name,
                    "lectures": row.candidate.lectures_payload,
                    "correct_count": row.correct_count,
                    "wrong_count": len(row.wrong_question_numbers),
                    "wrong_questions": list(row.wrong_question_numbers),
                    "review_count": len(row.review_question_numbers),
                    "review_questions": list(row.review_question_numbers),
                    "total_score": row.total_score,
                    "max_score": row.max_score,
                    "will_overwrite": row.will_overwrite,
                    "is_not_submitted": row.is_not_submitted,
                    "exam_not_submitted_count": (
                        row.candidate.exam_not_submitted_count
                    ),
                }
                for row in self.rows
            ],
        }


def build_manual_grading_sheet(*, exam: Any, tenant: Any) -> dict[str, Any]:
    try:
        questions = _question_specs(exam=exam, tenant=tenant)
    except ExamResultWorkbookError as exc:
        if "시험 문항을 먼저 등록해 주세요." not in str(exc):
            raise
        questions = []
    candidates = _exam_candidates(exam=exam, tenant=tenant)
    editable_ids = _editable_question_ids(exam=exam, questions=questions)
    question_answer_types = _question_answer_types(
        exam=exam,
        questions=questions,
    )
    _, score_adjustment_total = _score_adjustments(
        exam=exam,
        questions=questions,
    )

    results = {
        int(result.enrollment_id): result
        for result in Result.objects.filter(
            target_type="exam",
            target_id=int(exam.id),
            enrollment_id__in=[
                candidate.enrollment_id for candidate in candidates
            ],
        )
        .select_related("attempt")
        .prefetch_related("items")
    }

    rows = []
    for candidate in candidates:
        result = results.get(candidate.enrollment_id)
        items = {
            int(item.question_id): item
            for item in (result.items.all() if result is not None else [])
        }
        attempt_meta = (
            result.attempt.meta
            if result is not None
            and result.attempt is not None
            and isinstance(result.attempt.meta, dict)
            else {}
        )
        rows.append(
            {
                "enrollment_id": candidate.enrollment_id,
                "student_name": candidate.student_name,
                "school": candidate.school,
                "lectures": candidate.lectures_payload,
                "expected_version": (
                    result.updated_at.isoformat() if result is not None else None
                ),
                "is_not_submitted": (
                    attempt_meta.get("status") == "NOT_SUBMITTED"
                ),
                "exam_not_submitted_count": (
                    candidate.exam_not_submitted_count
                ),
                "cells": {
                    str(question.question_id): _item_cell_payload(
                        item=items.get(question.question_id),
                        editable=question.question_id in editable_ids,
                        method=str(exam.manual_grading_method),
                    )
                    for question in questions
                },
            }
        )

    return {
        "exam_id": int(exam.id),
        "exam_title": str(exam.title or ""),
        "grading_mode": str(exam.grading_mode),
        "manual_grading_method": str(exam.manual_grading_method),
        "has_manual_questions": bool(editable_ids),
        "exam_max_score": float(exam.max_score or 0.0),
        "question_score_total": round(
            sum(question.max_score for question in questions),
            2,
        ),
        "score_adjustment_total": round(score_adjustment_total, 2),
        "questions": [
            {
                "question_id": question.question_id,
                "number": question.number,
                "kind": question.kind,
                "answer_type": question_answer_types[question.question_id],
                "max_score": question.max_score,
                "editable": question.question_id in editable_ids,
                "entry_method": (
                    str(exam.manual_grading_method)
                    if question.question_id in editable_ids
                    else "omr"
                ),
            }
            for question in questions
        ],
        "rows": rows,
    }


def _question_answer_types(
    *,
    exam: Any,
    questions: list[QuestionSpec],
) -> dict[int, str]:
    score_shape = get_exam_score_shape(exam)
    answers = get_answer_key_answers(
        template_exam_id=score_shape.template_exam_id,
    )
    question_kind_by_id = {
        int(question.question_id): str(question.kind)
        for question in questions
    }
    numeric_question_ids = math_numeric_short_answer_question_ids(
        exam=exam,
        question_ids=question_kind_by_id,
        question_kind=lambda question_id: question_kind_by_id.get(
            int(question_id)
        ),
        answers=answers,
    )
    return {
        question_id: (
            "numeric_short_answer"
            if question_id in numeric_question_ids
            else "choice"
            if kind == "choice"
            else "written"
        )
        for question_id, kind in question_kind_by_id.items()
    }


def plan_manual_grading(
    *,
    exam: Any,
    tenant: Any,
    payload: Any,
) -> ManualGradePlan:
    questions = _question_specs(exam=exam, tenant=tenant)
    candidates = _exam_candidates(exam=exam, tenant=tenant)
    editable_ids = _editable_question_ids(exam=exam, questions=questions)
    questions, question_score_updates, original_question_scores, score_errors = (
        _question_score_overrides(
            exam=exam,
            questions=questions,
            editable_ids=editable_ids,
            payload=payload,
        )
    )
    plan = ManualGradePlan(
        exam=exam,
        questions=questions,
        question_score_updates=question_score_updates,
        original_question_scores=original_question_scores,
        errors=score_errors,
    )
    if not editable_ids:
        plan.errors.append(
            _error(None, "exam", "이 시험은 OMR 채점 대상입니다.")
        )
        return plan

    raw_rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(raw_rows, list) or not raw_rows:
        plan.errors.append(
            _error(None, "rows", "반영할 학생 채점 행을 입력해 주세요.")
        )
        return plan
    if len(raw_rows) > MAX_MANUAL_ROWS:
        plan.errors.append(
            _error(None, "rows", "한 번에 2,000명까지 반영할 수 있습니다.")
        )
        return plan

    candidates_by_id = {
        candidate.enrollment_id: candidate for candidate in candidates
    }
    question_by_id = {
        question.question_id: question for question in questions
    }
    results = {
        int(result.enrollment_id): result
        for result in Result.objects.filter(
            target_type="exam",
            target_id=int(exam.id),
            enrollment_id__in=list(candidates_by_id),
        ).prefetch_related("items")
    }
    seen: set[int] = set()

    for row_index, raw_row in enumerate(raw_rows, start=1):
        if not isinstance(raw_row, dict):
            plan.errors.append(
                _error(row_index, "row", "학생 채점 행 형식을 확인해 주세요.")
            )
            continue
        try:
            enrollment_id = int(raw_row.get("enrollment_id"))
        except (TypeError, ValueError):
            plan.errors.append(
                _error(row_index, "enrollment_id", "학생 정보를 확인해 주세요.")
            )
            continue
        candidate = candidates_by_id.get(enrollment_id)
        if candidate is None:
            plan.errors.append(
                _error(
                    row_index,
                    "enrollment_id",
                    "이 시험의 응시 대상이 아닌 학생입니다.",
                )
            )
            continue
        if enrollment_id in seen:
            plan.errors.append(
                _error(row_index, "enrollment_id", "같은 학생이 두 번 들어 있습니다.")
            )
            continue
        seen.add(enrollment_id)

        attendance = str(raw_row.get("attendance") or "present").strip().lower()
        if attendance not in {"present", "absent"}:
            plan.errors.append(
                _error(
                    row_index,
                    "attendance",
                    "응시 여부는 present 또는 absent여야 합니다.",
                )
            )
            continue
        is_not_submitted = attendance == "absent"
        result = results.get(enrollment_id)
        current_items = {
            int(item.question_id): item
            for item in (result.items.all() if result is not None else [])
        }

        marks: dict[int, CorrectnessMark] = {}
        row_has_error = False
        if not is_not_submitted:
            raw_cells = raw_row.get("cells")
            if not isinstance(raw_cells, dict):
                plan.errors.append(
                    _error(row_index, "cells", "문항별 채점값을 입력해 주세요.")
                )
                continue

            for question_id in sorted(editable_ids):
                question = question_by_id[question_id]
                raw_cell = raw_cells.get(str(question_id), raw_cells.get(question_id))
                mark, cell_error = _parse_manual_cell(
                    raw_cell=raw_cell,
                    method=str(exam.manual_grading_method),
                    max_score=question.max_score,
                )
                if cell_error:
                    plan.errors.append(
                        _error(
                            row_index,
                            f"question_{question.number}",
                            f"{question.number}번: {cell_error}",
                        )
                    )
                    row_has_error = True
                elif mark is not None:
                    marks[question_id] = mark

            locked_ids = set(question_by_id) - editable_ids
            missing_omr = [
                question_by_id[question_id].number
                for question_id in locked_ids
                if question_id not in current_items
            ]
            if missing_omr:
                plan.errors.append(
                    _error(
                        row_index,
                        "omr",
                        (
                            "선택형 OMR 채점을 먼저 완료해 주세요: "
                            + ", ".join(map(str, sorted(missing_omr)))
                            + "번"
                        ),
                    )
                )
                row_has_error = True
        if row_has_error:
            continue

        all_scores: dict[int, float] = {
            question_id: float(item.score or 0.0)
            for question_id, item in current_items.items()
            if question_id in question_by_id and question_id not in editable_ids
        }
        all_scores.update(
            {
                question_id: float(mark.earned_score or 0.0)
                for question_id, mark in marks.items()
            }
        )
        _, max_score = _score_row(
            exam=exam,
            questions=questions,
            correctness={
                question.number: CorrectnessMark(is_correct=False)
                for question in questions
            },
        )
        _, total_adjustment = _score_adjustments(
            exam=exam,
            questions=questions,
        )
        total_score = (
            0.0
            if is_not_submitted
            else round(sum(all_scores.values()) + total_adjustment, 2)
        )
        plan.rows.append(
            ManualPlannedRow(
                candidate=candidate,
                expected_version=_normalize_expected_version(
                    raw_row.get("expected_version")
                ),
                is_not_submitted=is_not_submitted,
                marks=marks,
                total_score=total_score,
                max_score=max_score,
                correct_count=sum(
                    1 for mark in marks.values() if mark.is_correct
                ),
                wrong_question_numbers=tuple(
                    sorted(
                        question_by_id[question_id].number
                        for question_id, mark in marks.items()
                        if not mark.is_correct
                    )
                ),
                review_question_numbers=tuple(
                    sorted(
                        question_by_id[question_id].number
                        for question_id, mark in marks.items()
                        if mark.is_correct and mark.include_in_wrong_note
                    )
                ),
                will_overwrite=result is not None,
            )
        )

    return plan


@transaction.atomic
def apply_manual_grading(
    *,
    plan: ManualGradePlan,
    user_id: int | None = None,
) -> dict[str, Any]:
    if not plan.can_apply:
        raise ManualExamGradingError(
            "오류가 있는 채점표는 반영할 수 없습니다."
        )

    exam = plan.exam
    require_score_edit_scope_available_for_exam(
        exam=exam,
        tenant=exam.tenant,
    )
    _apply_question_score_updates(plan=plan)
    questions_by_id = {
        question.question_id: question for question in plan.questions
    }
    question_ids = set(questions_by_id)
    now = timezone.now()

    for planned_row in plan.rows:
        enrollment_id = planned_row.candidate.enrollment_id
        validate_exam_enrollment_assigned(exam, enrollment_id)
        enrollment = get_locked_enrollment_for_tenant(
            enrollment_id=enrollment_id,
            tenant=exam.tenant,
        )
        if enrollment is None:
            raise ManualExamGradingError(
                f"{planned_row.candidate.student_name} 학생의 수강 정보를 찾을 수 없습니다."
            )

        current_result = (
            Result.objects.select_for_update()
            .filter(
                target_type="exam",
                target_id=int(exam.id),
                enrollment_id=enrollment_id,
            )
            .first()
        )
        current_version = (
            current_result.updated_at.isoformat()
            if current_result is not None
            else None
        )
        if current_version != planned_row.expected_version:
            raise ManualExamGradingError(
                (
                    f"{planned_row.candidate.student_name} 학생의 결과가 다른 화면에서 "
                    "변경됐습니다. 채점표를 새로 불러와 주세요."
                )
            )

        result, attempt = _locked_result_and_attempt(
            exam=exam,
            enrollment=enrollment,
            initial_total=planned_row.total_score,
            initial_max=planned_row.max_score,
            is_not_submitted=planned_row.is_not_submitted,
            now=now,
        )
        if attempt.status == "grading":
            raise ManualExamGradingError(
                f"{planned_row.candidate.student_name} 학생은 현재 채점 중입니다."
            )

        if planned_row.is_not_submitted:
            ResultItem.objects.select_for_update().filter(result=result).delete()
            ResultFact.objects.create(
                target_type="exam",
                target_id=int(exam.id),
                enrollment_id=enrollment_id,
                submission_id=0,
                attempt_id=int(attempt.id),
                question_id=0,
                answer="",
                is_correct=False,
                score=0.0,
                max_score=float(planned_row.max_score),
                source="manual_grid",
                meta={
                    "manual_grid": True,
                    "status": "NOT_SUBMITTED",
                    "published_at": now.isoformat(),
                },
            )
            _save_result_and_attempt(
                result=result,
                attempt=attempt,
                total_score=0.0,
                objective_score=0.0,
                max_score=planned_row.max_score,
                now=now,
                is_not_submitted=True,
            )
            resolve_exam_not_submitted_clinic_links(
                tenant_id=int(exam.tenant_id),
                enrollment_id=enrollment_id,
                exam_id=int(exam.id),
                attempt_id=int(attempt.id),
                user_id=int(user_id) if user_id is not None else None,
            )
            continue

        for question_id, mark in planned_row.marks.items():
            question = questions_by_id[question_id]
            existing_item = (
                ResultItem.objects.select_for_update()
                .filter(result=result, question_id=question_id)
                .first()
            )
            earned = float(mark.earned_score or 0.0)
            changed = (
                existing_item is None
                or bool(existing_item.is_correct) != mark.is_correct
                or bool(existing_item.include_in_wrong_note)
                != mark.include_in_wrong_note
                or abs(float(existing_item.score or 0.0) - earned) > 0.0001
                or abs(
                    float(existing_item.max_score or 0.0)
                    - float(question.max_score)
                )
                > 0.0001
            )
            if changed:
                ResultFact.objects.create(
                    target_type="exam",
                    target_id=int(exam.id),
                    enrollment_id=enrollment_id,
                    submission_id=0,
                    attempt_id=int(attempt.id),
                    question_id=question_id,
                    answer=(
                        str(existing_item.answer or "")
                        if existing_item is not None
                        else ""
                    ),
                    is_correct=mark.is_correct,
                    score=earned,
                    max_score=float(question.max_score),
                    source="manual_grid",
                    meta={
                        "manual_grid": True,
                        "include_in_wrong_note": (
                            mark.include_in_wrong_note
                        ),
                        "published_at": now.isoformat(),
                    },
                )
            ResultItem.objects.update_or_create(
                result=result,
                question_id=question_id,
                defaults={
                    "answer": (
                        str(existing_item.answer or "")
                        if existing_item is not None
                        else ""
                    ),
                    "is_correct": mark.is_correct,
                    "include_in_wrong_note": mark.include_in_wrong_note,
                    "score": earned,
                    "max_score": float(question.max_score),
                    "source": "manual_grid",
                },
            )

        items = list(
            ResultItem.objects.select_for_update().filter(
                result=result,
                question_id__in=question_ids,
            )
        )
        if {int(item.question_id) for item in items} != question_ids:
            raise ManualExamGradingError(
                (
                    f"{planned_row.candidate.student_name} 학생의 선택형 OMR 결과가 "
                    "완전하지 않습니다."
                )
            )
        objective_adjustment, total_adjustment = _score_adjustments(
            exam=exam,
            questions=plan.questions,
        )
        item_total = sum(float(item.score or 0.0) for item in items)
        objective_total = sum(
            float(item.score or 0.0)
            for item in items
            if questions_by_id[int(item.question_id)].kind == "choice"
        )
        total_score = round(item_total + total_adjustment, 2)
        objective_score = round(
            objective_total + objective_adjustment,
            2,
        )
        _save_result_and_attempt(
            result=result,
            attempt=attempt,
            total_score=total_score,
            objective_score=objective_score,
            max_score=planned_row.max_score,
            now=now,
            is_not_submitted=False,
        )

    exam_id = int(exam.id)

    def _dispatch_progress() -> None:
        try:
            dispatch_progress_pipeline(exam_id=exam_id)
        except Exception:
            logger.exception(
                "progress pipeline dispatch failed after manual grading "
                "(exam=%s)",
                exam_id,
            )

    transaction.on_commit(_dispatch_progress)
    return plan.as_payload(applied=True)


def _editable_question_ids(
    *,
    exam: Any,
    questions: list[QuestionSpec],
) -> set[int]:
    if exam.grading_mode == "choice":
        return set()
    if exam.grading_mode == "written":
        return {question.question_id for question in questions}
    return {
        question.question_id
        for question in questions
        if question.kind == "essay"
    }


def _question_score_overrides(
    *,
    exam: Any,
    questions: list[QuestionSpec],
    editable_ids: set[int],
    payload: Any,
) -> tuple[
    list[QuestionSpec],
    dict[int, float],
    dict[int, float],
    list[dict[str, Any]],
]:
    if not isinstance(payload, dict) or "question_scores" not in payload:
        return questions, {}, {}, []

    raw_scores = payload.get("question_scores")
    raw_expected = payload.get("expected_question_scores")
    if not isinstance(raw_scores, dict) or not isinstance(raw_expected, dict):
        return (
            questions,
            {},
            {},
            [
                _error(
                    None,
                    "question_scores",
                    "문항 배점 정보를 새로 불러와 주세요.",
                )
            ],
        )

    question_by_id = {
        question.question_id: question for question in questions
    }
    updates: dict[int, float] = {}
    originals: dict[int, float] = {}
    errors: list[dict[str, Any]] = []

    for raw_question_id, raw_score in raw_scores.items():
        try:
            question_id = int(raw_question_id)
        except (TypeError, ValueError):
            errors.append(
                _error(None, "question_scores", "문항 배점 대상을 확인해 주세요.")
            )
            continue
        question = question_by_id.get(question_id)
        if question is None or question_id not in editable_ids:
            errors.append(
                _error(
                    None,
                    f"question_{question_id}",
                    "이 채점표에서 수정할 수 없는 문항입니다.",
                )
            )
            continue
        try:
            score = round(float(raw_score), 2)
            expected_score = round(
                float(
                    raw_expected.get(
                        str(question_id),
                        raw_expected.get(question_id),
                    )
                ),
                2,
            )
        except (TypeError, ValueError):
            errors.append(
                _error(
                    None,
                    f"question_{question.number}",
                    f"{question.number}번 배점을 0점 이상으로 입력해 주세요.",
                )
            )
            continue
        if score < 0:
            errors.append(
                _error(
                    None,
                    f"question_{question.number}",
                    f"{question.number}번 배점을 0점 이상으로 입력해 주세요.",
                )
            )
            continue
        if abs(expected_score - float(question.max_score)) > 0.001:
            errors.append(
                _error(
                    None,
                    f"question_{question.number}",
                    (
                        f"{question.number}번 배점이 다른 화면에서 변경됐습니다. "
                        "채점표를 새로 불러와 주세요."
                    ),
                )
            )
            continue
        if abs(score - float(question.max_score)) > 0.001:
            updates[question_id] = score
            originals[question_id] = expected_score

    effective_questions = [
        replace(
            question,
            max_score=updates.get(question.question_id, question.max_score),
        )
        for question in questions
    ]
    if updates and not errors:
        _, score_adjustment_total = _score_adjustments(
            exam=exam,
            questions=effective_questions,
        )
        configured_total = round(
            sum(question.max_score for question in effective_questions)
            + score_adjustment_total,
            2,
        )
        exam_max_score = round(float(exam.max_score or 0.0), 2)
        if abs(configured_total - exam_max_score) > 0.01:
            errors.append(
                _error(
                    None,
                    "question_scores",
                    (
                        f"문항 배점 합계는 시험 만점 {exam_max_score:g}점과 "
                        f"같아야 합니다. 현재 {configured_total:g}점입니다."
                    ),
                )
            )

    return effective_questions, updates, originals, errors


def _apply_question_score_updates(*, plan: ManualGradePlan) -> None:
    if not plan.question_score_updates:
        return

    locked_questions = get_locked_exam_questions_for_manual_grading(
        question_ids=set(plan.question_score_updates),
        tenant=plan.exam.tenant,
    )
    if set(locked_questions) != set(plan.question_score_updates):
        raise ManualExamGradingError(
            "문항 배점 대상을 찾지 못했습니다. 채점표를 새로 불러와 주세요."
        )
    if any(
        int(question.sheet.exam_id) != int(plan.exam.id)
        for question in locked_questions.values()
    ):
        raise ManualExamGradingError(
            "공유 중인 시험 문항은 여기서 배점을 바꿀 수 없습니다. 답안 등록에서 문항을 먼저 준비해 주세요."
        )

    for question_id, next_score in plan.question_score_updates.items():
        question = locked_questions[question_id]
        expected_score = plan.original_question_scores[question_id]
        if abs(float(question.score or 0.0) - expected_score) > 0.001:
            raise ManualExamGradingError(
                (
                    f"{question.number}번 배점이 다른 화면에서 변경됐습니다. "
                    "채점표를 새로 불러와 주세요."
                )
            )
        question.score = next_score
        question.save(update_fields=["score", "updated_at"])


def _item_cell_payload(
    *,
    item: Any | None,
    editable: bool,
    method: str,
) -> dict[str, Any]:
    state = None
    if item is not None:
        if bool(item.is_correct) and bool(item.include_in_wrong_note):
            state = "review"
        elif bool(item.is_correct):
            state = "correct"
        else:
            state = "incorrect"
    return {
        "editable": editable,
        "entry_method": method if editable else "omr",
        "state": state,
        "score": float(item.score or 0.0) if item is not None else None,
        "include_in_wrong_note": (
            bool(item.include_in_wrong_note) if item is not None else False
        ),
    }


def _parse_manual_cell(
    *,
    raw_cell: Any,
    method: str,
    max_score: float,
) -> tuple[CorrectnessMark | None, str | None]:
    if not isinstance(raw_cell, dict):
        return None, "채점값을 입력해 주세요."

    if method == "correctness":
        state = str(raw_cell.get("state") or "").strip().lower()
        if state == "correct":
            return CorrectnessMark(
                is_correct=True,
                earned_score=float(max_score),
            ), None
        if state == "review":
            return CorrectnessMark(
                is_correct=True,
                include_in_wrong_note=True,
                earned_score=float(max_score),
            ), None
        if state == "incorrect":
            return CorrectnessMark(
                is_correct=False,
                include_in_wrong_note=True,
                earned_score=0.0,
            ), None
        return None, "O, X, 0 중 하나를 선택해 주세요."

    try:
        score = float(raw_cell.get("score"))
    except (TypeError, ValueError):
        return None, "0점 이상인 점수를 입력해 주세요."
    if score < 0 or score > float(max_score):
        return None, f"0점부터 {float(max_score):g}점까지 입력할 수 있습니다."
    is_correct = abs(score - float(max_score)) <= 0.0001
    return CorrectnessMark(
        is_correct=is_correct,
        include_in_wrong_note=(
            bool(raw_cell.get("include_in_wrong_note")) or not is_correct
        ),
        earned_score=score,
    ), None


def _save_result_and_attempt(
    *,
    result: Result,
    attempt: ExamAttempt,
    total_score: float,
    objective_score: float,
    max_score: float,
    now: Any,
    is_not_submitted: bool,
) -> None:
    result.attempt = attempt
    result.objective_score = float(objective_score)
    result.total_score = float(total_score)
    result.max_score = float(max_score)
    result.submitted_at = now
    result.save(
        update_fields=[
            "attempt",
            "objective_score",
            "total_score",
            "max_score",
            "submitted_at",
            "updated_at",
        ]
    )

    meta = dict(attempt.meta or {}) if isinstance(attempt.meta, dict) else {}
    if is_not_submitted:
        meta["status"] = "NOT_SUBMITTED"
    else:
        meta.pop("status", None)
    meta["total_score"] = float(total_score)
    meta["max_score"] = float(max_score)
    meta["synced_from_result"] = True
    meta["last_manual_grid_publish"] = {"published_at": now.isoformat()}
    if not is_not_submitted:
        meta = sync_confirmed_initial_snapshot_meta(
            attempt=attempt,
            meta=meta,
            total_score=float(total_score),
            max_score=float(max_score),
            confirmed_at=now,
            fallback_source="manual_grading_grid",
        )
    attempt.meta = meta
    attempt.status = "done"
    attempt.save(update_fields=["meta", "status", "updated_at"])


def _normalize_expected_version(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _error(
    row: int | None,
    field: str,
    message: str,
) -> dict[str, Any]:
    return {"row": row, "field": field, "message": message}
