from __future__ import annotations

import logging
import math

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status as drf_status
from rest_framework.exceptions import ValidationError, NotFound

from apps.domains.results.permissions import IsTeacherOrAdmin
from apps.domains.results.models import ExamAttempt, Result, ResultItem, ResultFact
from apps.domains.results.guards.score_edit_lease_guard import (
    require_score_edit_scope_available_for_exam,
)
from apps.support.omr.score_shape import get_exam_score_shape

from apps.domains.results.utils.session_exam import (
    get_unambiguous_session_for_exam_lecture,
)
from apps.support.results.admin_exam_dependencies import (
    dispatch_progress_pipeline,
    get_enrollment_for_tenant,
    get_latest_exam_submission_id,
    lock_regular_active_exam_for_tenant,
)

logger = logging.getLogger(__name__)


def _score_float(value) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError, OverflowError):
        return math.nan


class AdminRepresentativeAttemptView(APIView):
    """
    POST /results/admin/exams/<exam_id>/representative-attempt/

    ✅ PHASE 7 기준 (고정)
    - 대표 attempt 변경은 "is_representative"만 바꾸는 행위가 아니다.
    - Result 스냅샷(Result/ResultItem)은 선택된 attempt의 Fact(append-only)에서 즉시 재구성한다.
    - 이후 progress pipeline을 즉시 트리거하여 파생 결과를 최신화한다.

    🚫 금지
    - 모델/마이그레이션 유발 변경
    - 프론트 계약 변경
    """

    permission_classes = [IsAuthenticated, IsTeacherOrAdmin]

    @staticmethod
    def _resolve_result_snapshot_from_attempt(
        *,
        result: Result,
        attempt: ExamAttempt,
        exam,
    ) -> tuple[list[ResultFact], float, float, object]:
        attempt_facts = ResultFact.objects.filter(
            target_type="exam",
            target_id=result.target_id,
            enrollment_id=result.enrollment_id,
            attempt_id=attempt.id,
        )
        ordered_facts = list(attempt_facts.order_by("id"))

        score_shape = get_exam_score_shape(exam)
        attempt_meta = attempt.meta if isinstance(attempt.meta, dict) else {}
        final_snapshot = (
            attempt_meta.get("final_result_snapshot")
            if isinstance(attempt_meta.get("final_result_snapshot"), dict)
            else {}
        )
        initial_snapshot = (
            attempt_meta.get("initial_snapshot")
            if isinstance(attempt_meta.get("initial_snapshot"), dict)
            else {}
        )

        seed_total = next(
            (
                value
                for value in (
                    attempt_meta.get("total_score"),
                    final_snapshot.get("total_score"),
                    initial_snapshot.get("total_score"),
                )
                if value is not None
            ),
            None,
        )
        if not ordered_facts and seed_total is None:
            raise ValidationError(
                {
                    "detail": "no score state for this attempt; cannot rebuild snapshot",
                    "code": "INVALID",
                }
            )

        total_score = _score_float(seed_total)
        objective_value = next(
            (
                value
                for value in (
                    final_snapshot.get("objective_score"),
                    attempt_meta.get("objective_score"),
                )
                if value is not None
            ),
            None,
        )
        if objective_value is None:
            initial_source = str(initial_snapshot.get("source") or "")
            objective_score = (
                total_score
                if initial_source
                in {"admin_manual_objective", "submission_sync", "omr"}
                else 0.0
            )
        else:
            objective_score = _score_float(objective_value)
        explicit_subjective = _score_float(
            attempt_meta.get(
                "subjective_score",
                max(0.0, total_score - objective_score),
            )
            or 0.0
        )
        question_facts: dict[int, ResultFact] = {}

        for fact in ordered_facts:
            question_id = int(fact.question_id)
            source = str(fact.source or "")
            score = _score_float(fact.score)
            meta = fact.meta if isinstance(fact.meta, dict) else {}
            snapshot = meta.get("result_snapshot")
            if isinstance(snapshot, dict) and {
                "total_score",
                "objective_score",
            }.issubset(snapshot):
                total_score = _score_float(snapshot["total_score"])
                objective_score = _score_float(snapshot["objective_score"])
                if source == "manual_subjective":
                    explicit_subjective = score
                continue

            if question_id > 0:
                question_facts[question_id] = fact
                choice_total = sum(
                    _score_float(item.score)
                    for item in question_facts.values()
                    if score_shape.question_kind(int(item.question_id)) == "choice"
                )
                essay_total = sum(
                    _score_float(item.score)
                    for item in question_facts.values()
                    if score_shape.question_kind(int(item.question_id)) == "essay"
                )
                unknown_facts = [
                    item
                    for item in question_facts.values()
                    if score_shape.question_kind(int(item.question_id)) is None
                ]
                if unknown_facts:
                    total_score = sum(
                        _score_float(item.score)
                        for item in question_facts.values()
                    )
                else:
                    if any(
                        score_shape.question_kind(int(item.question_id)) == "choice"
                        for item in question_facts.values()
                    ):
                        objective_score = choice_total
                    subjective_score = (
                        essay_total
                        if any(
                            score_shape.question_kind(int(item.question_id)) == "essay"
                            for item in question_facts.values()
                        )
                        else explicit_subjective
                    )
                    total_score = objective_score + subjective_score
            elif source == "manual_objective":
                subjective_score = max(0.0, total_score - objective_score)
                objective_score = _score_float(meta.get("objective_score", score))
                total_score = objective_score + subjective_score
            elif source == "manual_subjective":
                explicit_subjective = _score_float(meta.get("subjective_score", score))
                total_score = objective_score + explicit_subjective
            elif source == "manual_total":
                total_score = score

        submitted_at_value = (
            final_snapshot.get("submitted_at")
            or initial_snapshot.get("submitted_at")
        )
        submitted_at = (
            parse_datetime(submitted_at_value)
            if isinstance(submitted_at_value, str)
            else submitted_at_value
        ) or attempt.updated_at or attempt.created_at or timezone.now()
        return (
            list(question_facts.values()),
            total_score,
            objective_score,
            submitted_at,
        )

    @staticmethod
    def _apply_result_snapshot(
        *,
        result: Result,
        attempt_id: int,
        question_facts: list[ResultFact],
        total_score: float,
        objective_score: float,
        current_max_score: float,
        submitted_at,
    ) -> Result:
        ResultItem.objects.select_for_update().filter(result=result).delete()
        for fact in question_facts:
            ResultItem.objects.create(
                result=result,
                question_id=int(fact.question_id),
                answer=str(fact.answer or ""),
                is_correct=bool(fact.is_correct),
                include_in_wrong_note=bool(
                    (fact.meta or {}).get("include_in_wrong_note")
                    if isinstance(fact.meta, dict)
                    else False
                ),
                score=float(fact.score or 0.0),
                max_score=float(fact.max_score or 0.0),
                source=str(fact.source or ""),
            )

        result.attempt_id = int(attempt_id)
        result.total_score = float(total_score)
        result.objective_score = float(objective_score)
        result.max_score = float(current_max_score)
        result.submitted_at = submitted_at
        result.save(
            update_fields=[
                "attempt_id",
                "total_score",
                "objective_score",
                "max_score",
                "submitted_at",
                "updated_at",
            ]
        )

        return result

    @transaction.atomic
    def post(self, request, exam_id: int):
        exam_id = int(exam_id)

        # ✅ tenant isolation: verify exam belongs to tenant
        exam = lock_regular_active_exam_for_tenant(
            exam_id=exam_id,
            tenant=request.tenant,
        )
        require_score_edit_scope_available_for_exam(
            exam=exam,
            tenant=request.tenant,
        )

        enrollment_id = request.data.get("enrollment_id")
        attempt_id = request.data.get("attempt_id")

        if enrollment_id is None or attempt_id is None:
            raise ValidationError({"detail": "enrollment_id and attempt_id are required", "code": "INVALID"})

        enrollment_id = int(enrollment_id)
        attempt_id = int(attempt_id)
        enrollment = get_enrollment_for_tenant(
            enrollment_id=enrollment_id,
            tenant=request.tenant,
        )
        if enrollment is None:
            raise NotFound({"detail": "enrollment not found", "code": "NOT_FOUND"})
        session = get_unambiguous_session_for_exam_lecture(
            exam_id=exam_id,
            lecture_id=getattr(enrollment, "lecture_id", None),
        )
        if session is None:
            return Response(
                {
                    "detail": "one exact session is required for this exam and lecture",
                    "code": "INVALID",
                },
                status=drf_status.HTTP_409_CONFLICT,
            )

        result = (
            Result.objects.select_for_update()
            .filter(
                target_type="exam",
                target_id=exam_id,
                enrollment_id=enrollment_id,
            )
            .first()
        )
        if not result:
            raise NotFound(
                {"detail": "result snapshot not found", "code": "NOT_FOUND"}
            )

        attempts_qs = (
            ExamAttempt.objects
            .select_for_update()
            .filter(exam_id=exam_id, enrollment_id=enrollment_id)
        )

        if not attempts_qs.exists():
            raise NotFound({"detail": "attempts not found for this exam/enrollment", "code": "NOT_FOUND"})

        target = attempts_qs.filter(id=attempt_id).first()
        if not target:
            raise NotFound({"detail": "attempt not found for this exam/enrollment", "code": "NOT_FOUND"})

        if (target.status or "").lower() == "grading":
            return Response(
                {"detail": "attempt is grading; cannot switch representative", "code": "LOCKED"},
                status=drf_status.HTTP_409_CONFLICT,
            )

        question_facts, total_score, objective_score, submitted_at = (
            self._resolve_result_snapshot_from_attempt(
                result=result,
                attempt=target,
                exam=exam,
            )
        )
        current_max_score = float(exam.max_score or 100.0)
        if (
            not math.isfinite(total_score)
            or total_score < 0
            or total_score > current_max_score
            or not math.isfinite(objective_score)
            or objective_score < 0
            or objective_score > current_max_score
            or objective_score > total_score
            or any(
                not math.isfinite(_score_float(fact.score))
                or not math.isfinite(_score_float(fact.max_score))
                or _score_float(fact.score) < 0
                or _score_float(fact.max_score) < 0
                or _score_float(fact.score) > _score_float(fact.max_score)
                for fact in question_facts
            )
        ):
            raise ValidationError(
                {
                    "detail": (
                        "selected attempt score must be finite and between "
                        f"0 and {current_max_score}"
                    ),
                    "code": "INVALID",
                }
            )

        attempts_qs.filter(is_representative=True).update(is_representative=False)
        if not target.is_representative:
            target.is_representative = True
            target.save(update_fields=["is_representative"])

        self._apply_result_snapshot(
            result=result,
            attempt_id=attempt_id,
            question_facts=question_facts,
            total_score=total_score,
            objective_score=objective_score,
            current_max_score=current_max_score,
            submitted_at=submitted_at,
        )

        submission_id = get_latest_exam_submission_id(
            enrollment_id=enrollment_id,
            exam_id=exam_id,
        )
        def _dispatch_progress() -> None:
            try:
                if submission_id:
                    dispatch_progress_pipeline(submission_id=int(submission_id))
                else:
                    dispatch_progress_pipeline(exam_id=exam_id)
            except Exception:
                logger.exception(
                    "representative progress dispatch failed (exam=%s, submission=%s)",
                    exam_id,
                    submission_id,
                )

        transaction.on_commit(_dispatch_progress)

        return Response(
            {
                "ok": True,
                "exam_id": exam_id,
                "enrollment_id": enrollment_id,
                "attempt_id": attempt_id,
            },
            status=drf_status.HTTP_200_OK,
        )
