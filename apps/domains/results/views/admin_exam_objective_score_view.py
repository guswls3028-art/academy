# PATH: apps/domains/results/views/admin_exam_objective_score_view.py
"""
PATCH /results/admin/exams/{exam_id}/enrollments/{enrollment_id}/objective/

객관식 점수만 입력. total_score = objective_score + 기존 주관식 합계로 동기화.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status as drf_status
from rest_framework.exceptions import ValidationError, NotFound

from apps.domains.results.permissions import IsTeacherOrAdmin
from apps.domains.results.models import Result, ResultFact, ExamAttempt
from apps.domains.results.validation import parse_finite_score
from apps.support.results.exam_policy_dependencies import (
    effective_exam_pass_score,
)
from apps.domains.results.guards.exam_enrollment_guard import validate_exam_enrollment_assigned
from apps.domains.results.guards.score_edit_lease_guard import (
    require_score_edit_lease_from_headers,
)
from apps.support.omr.score_shape import get_exam_score_shape
from apps.support.results.admin_exam_dependencies import (
    dispatch_progress_pipeline,
    get_latest_exam_submission_id,
    lock_regular_active_exam_for_tenant,
)
from django.db.models import Max


class AdminExamObjectiveScoreView(APIView):
    permission_classes = [IsAuthenticated, IsTeacherOrAdmin]

    @transaction.atomic
    def patch(self, request, exam_id: int, enrollment_id: int):
        exam_id = int(exam_id)
        enrollment_id = int(enrollment_id)

        # ✅ tenant isolation: verify exam belongs to tenant
        exam = lock_regular_active_exam_for_tenant(
            exam_id=exam_id,
            tenant=request.tenant,
        )
        require_score_edit_lease_from_headers(
            request,
            exam_id=exam_id,
            enrollment_id=enrollment_id,
            sub="objective",
        )

        # ✅ tenant isolation: verify enrollment belongs to tenant
        from apps.domains.results.guards.enrollment_tenant_guard import validate_enrollment_belongs_to_tenant
        enrollment = validate_enrollment_belongs_to_tenant(enrollment_id, request.tenant)
        validate_exam_enrollment_assigned(exam, enrollment_id)

        if "score" not in request.data:
            raise ValidationError({"detail": "score is required", "code": "INVALID"})

        new_objective = parse_finite_score(request.data.get("score"))

        if new_objective < 0:
            raise ValidationError({"detail": "score must be >= 0", "code": "INVALID"})

        score_shape = get_exam_score_shape(exam)
        max_score = float(getattr(exam, "max_score", 100.0) or 100.0)
        objective_max = float(score_shape.objective_max_score)
        if objective_max <= 0 and score_shape.shape_source != "no_sheet" and new_objective > 0:
            raise ValidationError(
                {"detail": "이 시험에는 채점 대상 선택형 문항이 없습니다.", "code": "INVALID"}
            )
        if objective_max > 0 and new_objective > objective_max:
            raise ValidationError(
                {"detail": f"score must be between 0 and {objective_max}", "code": "INVALID"}
            )

        result = (
            Result.objects
            .select_for_update()
            .filter(
                target_type="exam",
                target_id=exam_id,
                enrollment_id=enrollment_id,
            )
            .first()
        )
        if not result or not result.attempt_id:
            qs = (
                ExamAttempt.objects
                .select_for_update()
                .filter(exam_id=exam_id, enrollment_id=enrollment_id)
            )
            last = qs.aggregate(Max("attempt_index")).get("attempt_index__max") or 0
            next_index = int(last) + 1
            qs.filter(is_representative=True).update(is_representative=False)
            # attempt_index=1: 1차 점수 스냅샷. Objective는 이 시점 주관식=0이므로
            # total_score = new_objective (이후 subjective 입력 시 Result만 갱신되고 snapshot은 불변).
            _initial_meta = None
            if next_index == 1:
                _initial_meta = {
                    "initial_snapshot": {
                        "total_score": float(new_objective),
                        "max_score": float(max_score),
                        "submitted_at": timezone.now().isoformat(),
                        "source": "admin_manual_objective",
                    }
                }
            attempt = ExamAttempt.objects.create(
                exam_id=exam_id,
                enrollment_id=enrollment_id,
                submission_id=0,
                attempt_index=next_index,
                is_retake=(last > 0),
                is_representative=True,
                status="done",
                meta=_initial_meta,
            )
            if not result:
                result = Result.objects.create(
                    target_type="exam",
                    target_id=exam_id,
                    enrollment_id=enrollment_id,
                    attempt_id=int(attempt.id),
                    total_score=0.0,
                    max_score=float(max_score),
                    objective_score=0.0,
                )
            else:
                result.attempt_id = int(attempt.id)
                result.max_score = float(max_score)
                result.objective_score = 0.0
                result.save(update_fields=["attempt_id", "max_score", "objective_score", "updated_at"])

        attempt = (
            ExamAttempt.objects.select_for_update()
            .filter(id=int(result.attempt_id))
            .first()
        )
        if not attempt:
            raise NotFound({"detail": "attempt not found", "code": "NOT_FOUND"})
        if attempt.status == "grading":
            return Response(
                {"detail": "attempt is grading", "code": "LOCKED"},
                status=drf_status.HTTP_409_CONFLICT,
            )

        current_objective = float(getattr(result, "objective_score", 0.0) or 0.0)
        current_total = float(getattr(result, "total_score", 0.0) or 0.0)
        current_subjective = max(0.0, current_total - current_objective)
        new_total = new_objective + current_subjective
        if max_score > 0 and new_total > max_score:
            raise ValidationError(
                {"detail": f"total score must be between 0 and {max_score}", "code": "INVALID"}
            )
        pass_score = effective_exam_pass_score(
            exam=exam,
            lecture_id=getattr(enrollment, "lecture_id", None),
        )

        submission_id = get_latest_exam_submission_id(
            enrollment_id=enrollment_id,
            exam_id=exam_id,
        ) or 0

        ResultFact.objects.create(
            target_type="exam",
            target_id=exam_id,
            enrollment_id=enrollment_id,
            submission_id=submission_id,
            attempt_id=int(result.attempt_id),
            question_id=0,
            answer="",
            is_correct=bool(float(new_total) >= float(pass_score)),
            score=float(new_objective),
            max_score=float(objective_max),
            source="manual_objective",
            meta={
                "manual_objective": True,
                "objective_score": new_objective,
                "objective_max_score": objective_max,
                "result_snapshot": {
                    "total_score": new_total,
                    "objective_score": new_objective,
                    "max_score": max_score,
                },
                "edited_at": timezone.now().isoformat(),
            },
        )

        result.objective_score = float(new_objective)
        result.total_score = float(new_total)
        result.max_score = float(max_score)
        result.save(update_fields=["objective_score", "total_score", "max_score", "updated_at"])

        if attempt and attempt.is_representative:
            attempt.meta = attempt.meta or {}
            attempt.meta["total_score"] = float(new_total)
            attempt.meta["synced_from_result"] = True
            attempt.meta.pop("status", None)
            attempt.save(update_fields=["meta", "updated_at"])

        _sid = int(submission_id) if submission_id else 0
        _eid = int(exam_id)
        def _dispatch_progress():
            try:
                if _sid:
                    dispatch_progress_pipeline(submission_id=_sid)
                else:
                    dispatch_progress_pipeline(exam_id=_eid)
            except Exception:
                logger.exception("progress pipeline dispatch failed (exam=%s, submission=%s)", _eid, _sid)
        transaction.on_commit(_dispatch_progress)

        # 정책 SSOT: messaging-policy.md "저장과 발송은 분리" — 점수 저장 자체는 알림 트리거 아님.
        # exam_score_published = MANUAL_DEFAULT. 학원장이 명시적으로 발송 버튼 클릭(preview→confirm)할 때만 발송.

        return Response(
            {
                "ok": True,
                "exam_id": exam_id,
                "enrollment_id": enrollment_id,
                "objective_score": float(result.objective_score or 0.0),
                "objective_max_score": float(objective_max),
                "total_score": float(result.total_score or 0.0),
                "max_score": float(result.max_score or 0.0),
            },
            status=drf_status.HTTP_200_OK,
        )
