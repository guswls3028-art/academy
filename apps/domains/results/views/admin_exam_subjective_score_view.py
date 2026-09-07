# PATH: apps/domains/results/views/admin_exam_subjective_score_view.py
"""
PATCH /results/admin/exams/{exam_id}/enrollments/{enrollment_id}/subjective/

주관식 점수(합계)만 입력. total_score = objective_score + subjective_score 로 동기화.
ResultItem은 실제 ExamQuestion FK만 허용하므로 합계 입력은 Result.total_score로 보존한다.
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
from apps.domains.results.services.omr_subjective_completion import (
    finalize_omr_result_if_ready,
)
from apps.support.omr.score_shape import get_exam_score_shape
from apps.support.results.admin_exam_dependencies import (
    dispatch_progress_pipeline,
    get_latest_exam_submission_id,
    get_regular_active_exam_for_tenant,
)
from django.db.models import Max

class AdminExamSubjectiveScoreView(APIView):
    permission_classes = [IsAuthenticated, IsTeacherOrAdmin]

    @transaction.atomic
    def patch(self, request, exam_id: int, enrollment_id: int):
        exam_id = int(exam_id)
        enrollment_id = int(enrollment_id)

        # ✅ tenant isolation: verify exam belongs to tenant
        exam = get_regular_active_exam_for_tenant(
            exam_id=exam_id,
            tenant=request.tenant,
        )
        require_score_edit_lease_from_headers(
            request,
            exam_id=exam_id,
            enrollment_id=enrollment_id,
            sub="subjective",
        )

        # ✅ tenant isolation: verify enrollment belongs to tenant
        from apps.domains.results.guards.enrollment_tenant_guard import validate_enrollment_belongs_to_tenant
        enrollment = validate_enrollment_belongs_to_tenant(enrollment_id, request.tenant)
        validate_exam_enrollment_assigned(exam, enrollment_id)

        if "score" not in request.data:
            raise ValidationError({"detail": "score is required", "code": "INVALID"})

        new_subjective = parse_finite_score(request.data.get("score"))

        if new_subjective < 0:
            raise ValidationError({"detail": "score must be >= 0", "code": "INVALID"})

        score_shape = get_exam_score_shape(exam)
        max_score = float(
            score_shape.total_max_score
            or getattr(exam, "max_score", 100.0)
            or 100.0
        )
        subjective_max = float(score_shape.subjective_max_score or 0.0)
        if subjective_max <= 0 and score_shape.shape_source != "no_sheet" and new_subjective > 0:
            raise ValidationError(
                {"detail": "이 시험에는 채점 대상 서술형 문항이 없습니다.", "code": "INVALID"}
            )
        if subjective_max > 0 and new_subjective > subjective_max:
            raise ValidationError(
                {"detail": f"score must be between 0 and {subjective_max}", "code": "INVALID"}
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
            # attempt_index=1: 1차 점수 스냅샷. Subjective는 이 시점 객관식=0이므로
            # total_score = new_subjective (1차 기준 고정).
            _initial_meta = None
            if next_index == 1:
                _initial_meta = {
                    "initial_snapshot": {
                        "total_score": float(new_subjective),
                        "max_score": float(max_score),
                        "submitted_at": timezone.now().isoformat(),
                        "source": "admin_manual_subjective",
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
                result.save(update_fields=["attempt_id", "max_score", "updated_at"])

        attempt = ExamAttempt.objects.filter(id=int(result.attempt_id)).first()
        if not attempt:
            raise NotFound({"detail": "attempt not found", "code": "NOT_FOUND"})
        if attempt.status == "grading":
            return Response(
                {"detail": "attempt is grading", "code": "LOCKED"},
                status=drf_status.HTTP_409_CONFLICT,
            )

        objective = float(getattr(result, "objective_score", 0.0) or 0.0)
        new_total = objective + new_subjective
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
            score=float(new_subjective),
            max_score=float(subjective_max or max_score),
            source="manual_subjective",
            meta={
                "manual_subjective": True,
                "subjective_score": new_subjective,
                "subjective_max_score": subjective_max,
                "edited_at": timezone.now().isoformat(),
            },
        )

        result.total_score = float(new_total)
        result.max_score = float(max_score)
        result.save(update_fields=["total_score", "max_score", "updated_at"])

        if attempt and attempt.is_representative:
            attempt.meta = attempt.meta or {}
            attempt.meta["total_score"] = float(new_total)
            attempt.meta["subjective_score"] = float(new_subjective)
            attempt.meta["synced_from_result"] = True
            attempt.meta.pop("status", None)
            attempt.save(update_fields=["meta", "updated_at"])

        finalization = finalize_omr_result_if_ready(result_id=int(result.id))

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
        if finalization.projection_ready:
            transaction.on_commit(_dispatch_progress)

        # 정책 SSOT: messaging-policy.md "저장과 발송은 분리" — 점수 저장 자체는 알림 트리거 아님.
        # exam_score_published = MANUAL_DEFAULT. 학원장이 명시적으로 발송 버튼 클릭(preview→confirm)할 때만 발송.

        return Response(
            {
                "ok": finalization.projection_ready,
                "saved": True,
                "projection_ready": finalization.projection_ready,
                "exam_id": exam_id,
                "enrollment_id": enrollment_id,
                "objective_score": float(result.objective_score or 0.0),
                "subjective_score": float(new_subjective),
                "subjective_max_score": float(subjective_max),
                "total_score": float(result.total_score or 0.0),
                "max_score": float(result.max_score or 0.0),
                "grading_status": finalization.pending_reason,
            },
            status=drf_status.HTTP_200_OK,
        )
