# PATH: apps/domains/results/views/admin_exam_item_score_view.py
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
from apps.domains.results.models import Result, ResultItem, ResultFact, ExamAttempt
from apps.domains.results.validation import parse_finite_score
from apps.domains.results.guards.exam_enrollment_guard import validate_exam_enrollment_assigned
from apps.domains.results.guards.score_edit_lease_guard import (
    require_score_edit_lease_from_headers,
)
from apps.domains.results.guards.enrollment_tenant_guard import validate_enrollment_belongs_to_tenant
from apps.domains.results.services.answer_matching import answer_matches, correct_answer_sets
from apps.domains.results.services.manual_subjective_score import (
    explicit_manual_subjective_score_for_result,
)
from apps.support.omr.score_shape import get_exam_score_shape
from apps.support.results.admin_exam_item_score_dependencies import (
    dispatch_progress_pipeline,
    get_answer_key_value,
    get_exam_question_for_item_score,
    get_latest_exam_submission_id,
    get_regular_active_exam_for_tenant,
)

_OBJECTIVE_CHOICE_LABELS = {"1", "2", "3", "4", "5"}


def _objective_correct_answer_for_question(*, exam, question_id: int):
    value = get_answer_key_value(
        template_exam_id=int(exam.effective_template_exam_id),
        question_id=question_id,
    )
    if value is None:
        return None

    answer_sets = correct_answer_sets(value)
    if not answer_sets:
        return None

    if all(
        token in _OBJECTIVE_CHOICE_LABELS
        for answer_set in answer_sets
        for token in answer_set
    ):
        return value
    return None


def _score_objective_answer_from_key(
    *,
    exam,
    question_id: int,
    answer,
    requested_score: float,
    max_score: float,
) -> tuple[float, bool | None]:
    if answer is None:
        return requested_score, None

    correct_answer = _objective_correct_answer_for_question(
        exam=exam,
        question_id=question_id,
    )
    if correct_answer is None:
        return requested_score, None

    is_correct = answer_matches(answer, correct_answer)
    return (float(max_score) if is_correct else 0.0), is_correct


class AdminExamItemScoreView(APIView):
    """
    PATCH /results/admin/exams/{exam_id}/enrollments/{enrollment_id}/items/{question_id}/

    ✅ PHASE 7 기준 (고정)
    - 수동 채점은 ResultFact(append-only) + ResultItem 스냅샷 갱신으로 기록한다.
    - total_score/max_score는 ResultItem 합으로 재계산한다.
    - 변경 즉시 progress pipeline을 트리거하여 SessionProgress/ClinicLink 등 파생 결과를 최신화한다.

    🚫 금지
    - 모델/마이그레이션 유발 변경
    - 프론트 계약 변경
    """

    permission_classes = [IsAuthenticated, IsTeacherOrAdmin]

    @transaction.atomic
    def patch(
        self,
        request,
        exam_id: int,
        enrollment_id: int,
        question_id: int,
    ):
        exam_id = int(exam_id)
        enrollment_id = int(enrollment_id)
        question_id = int(question_id)

        # ✅ tenant isolation: verify exam belongs to tenant
        exam = get_regular_active_exam_for_tenant(
            exam_id=exam_id,
            tenant=request.tenant,
        )
        require_score_edit_lease_from_headers(
            request,
            exam_id=exam_id,
            enrollment_id=enrollment_id,
            sub="item",
            question_id=question_id,
        )
        score_shape = get_exam_score_shape(exam)

        # ✅ tenant isolation: verify enrollment belongs to tenant
        enrollment_obj = validate_enrollment_belongs_to_tenant(
            enrollment_id,
            request.tenant,
        )
        validate_exam_enrollment_assigned(exam, enrollment_id)

        if "score" not in request.data:
            raise ValidationError({"detail": "score is required", "code": "INVALID"})

        new_score = parse_finite_score(request.data.get("score"))

        # ✅ 답안 필드 (수동 입력용, 선택 사항)
        new_answer = request.data.get("answer")  # None이면 미변경

        # -------------------------------------------------
        # 1️⃣ Result (대표 스냅샷) — 없으면 자동 생성 (수동 입력용)
        # -------------------------------------------------
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
        if not result:
            attempt, _ = ExamAttempt.objects.get_or_create(
                exam_id=exam_id,
                enrollment_id=enrollment_id,
                attempt_index=1,
                defaults={
                    "submission_id": 0,
                    "is_retake": False,
                    "is_representative": True,
                    "status": "done",
                    "meta": {"source": "manual_entry"},
                },
            )

            result, _ = Result.objects.get_or_create(
                target_type="exam",
                target_id=exam_id,
                enrollment=enrollment_obj,
                defaults={
                    "attempt": attempt,
                    "total_score": 0,
                    "max_score": float(exam.max_score or 0),
                    "objective_score": 0,
                },
            )
            # Re-fetch with lock
            result = (
                Result.objects
                .select_for_update()
                .filter(id=result.id)
                .first()
            )

        if not result.attempt_id:
            raise ValidationError(
                {"detail": "representative attempt not set", "code": "INVALID"}
            )

        # -------------------------------------------------
        # 2️⃣ Attempt 상태 확인 (LOCK)
        # -------------------------------------------------
        attempt = ExamAttempt.objects.filter(id=int(result.attempt_id)).first()
        if not attempt:
            raise NotFound({"detail": "attempt not found", "code": "NOT_FOUND"})

        if attempt.status == "grading":
            return Response(
                {"detail": "attempt is grading", "code": "LOCKED"},
                status=drf_status.HTTP_409_CONFLICT,
            )

        # -------------------------------------------------
        # 3️⃣ ResultItem (문항 스냅샷) — 없으면 생성(주관식 수동 입력용)
        # -------------------------------------------------
        item = (
            ResultItem.objects
            .select_for_update()
            .filter(result=result, question_id=question_id)
            .first()
        )
        if not item:
            allowed_exam_ids = {int(exam.id), int(exam.effective_template_exam_id)}
            exam_question = get_exam_question_for_item_score(
                question_id=question_id,
                exam_ids=allowed_exam_ids,
                tenant=request.tenant,
            )
            if not exam_question:
                raise NotFound({"detail": "question not found", "code": "NOT_FOUND"})
            max_score = score_shape.question_max_score(
                question_id,
                getattr(exam_question, "score", 0),
            )
            if new_score < 0 or new_score > max_score:
                raise ValidationError(
                    {
                        "detail": f"score must be between 0 and {max_score}",
                        "code": "INVALID",
                    }
                )
            new_score, server_is_correct = _score_objective_answer_from_key(
                exam=exam,
                question_id=question_id,
                answer=new_answer,
                requested_score=new_score,
                max_score=max_score,
            )
            item_is_correct = (
                server_is_correct
                if server_is_correct is not None
                else bool(new_score >= max_score)
            )
            item = ResultItem.objects.create(
                result=result,
                question_id=question_id,
                answer=new_answer if new_answer is not None else "",
                is_correct=item_is_correct,
                score=float(new_score),
                max_score=max_score,
                source="manual",
            )
            item_created = True
        else:
            item_created = False
            max_score = score_shape.question_max_score(question_id, item.max_score)
            if new_score < 0 or new_score > max_score:
                raise ValidationError(
                    {
                        "detail": f"score must be between 0 and {max_score}",
                        "code": "INVALID",
                    }
                )
            new_score, server_is_correct = _score_objective_answer_from_key(
                exam=exam,
                question_id=question_id,
                answer=new_answer,
                requested_score=new_score,
                max_score=max_score,
            )
            item_is_correct = (
                server_is_correct
                if server_is_correct is not None
                else bool(new_score >= max_score)
            )

        # -------------------------------------------------
        # 4️⃣ ResultFact (append-only 로그)
        # -------------------------------------------------
        ResultFact.objects.create(
            target_type="exam",
            target_id=exam_id,
            enrollment_id=enrollment_id,
            submission_id=0,              # 수동 채점이므로 0
            attempt_id=int(result.attempt_id),

            question_id=question_id,
            answer=new_answer if new_answer is not None else (item.answer or ""),
            is_correct=item_is_correct,
            score=float(new_score),
            max_score=max_score,
            source="manual",
            meta={
                "manual": True,
                "edited_at": timezone.now().isoformat(),
            },
        )

        # -------------------------------------------------
        # 5️⃣ ResultItem 업데이트 (기존 건만; 신규 생성 건은 이미 score 반영됨)
        # -------------------------------------------------
        if not item_created:
            item.score = float(new_score)
            item.is_correct = item_is_correct
            item.source = "manual"
            update_fields = ["score", "is_correct", "source"]
            if float(item.max_score or 0.0) != float(max_score):
                item.max_score = float(max_score)
                update_fields.append("max_score")
            if new_answer is not None:
                item.answer = new_answer
                update_fields.append("answer")
            item.save(update_fields=update_fields)

        # -------------------------------------------------
        # 6️⃣ total_score 재계산
        # OMR 계약의 문항 종류를 기준으로 객관식/서술형 점수를 분리한다.
        # 합계형 서술 점수는 명시적 수동 서술 근거가 있을 때만 보존한다.
        # -------------------------------------------------
        agg_items = list(ResultItem.objects.filter(result=result))
        items_sum = sum(float(x.score or 0.0) for x in agg_items)
        items_max_sum = sum(float(x.max_score or 0.0) for x in agg_items)

        choice_items_sum = 0.0
        essay_items_sum = 0.0
        has_choice_items = False
        has_essay_items = False
        has_unknown_items = False
        for score_item in agg_items:
            kind = score_shape.question_kind(int(score_item.question_id))
            if kind == "choice":
                choice_items_sum += float(score_item.score or 0.0)
                has_choice_items = True
            elif kind == "essay":
                essay_items_sum += float(score_item.score or 0.0)
                has_essay_items = True
            else:
                has_unknown_items = True

        if has_unknown_items:
            objective_score = float(result.objective_score or 0.0)
            total_score = items_sum
            max_total = items_max_sum
        else:
            previous_objective = float(result.objective_score or 0.0)
            explicit_subjective = explicit_manual_subjective_score_for_result(
                result=result,
                attempt=attempt,
                score_shape=score_shape,
            )
            objective_score = choice_items_sum if has_choice_items else previous_objective
            subjective_score = essay_items_sum if has_essay_items else explicit_subjective
            total_score = objective_score + subjective_score
            max_total = float(
                score_shape.total_max_score
                or getattr(exam, "max_score", 0.0)
                or items_max_sum
                or 0.0
            )

        if max_total > 0 and total_score > max_total:
            raise ValidationError(
                {"detail": f"total score must be between 0 and {max_total}", "code": "INVALID"}
            )

        result.objective_score = float(objective_score)
        result.total_score = float(total_score)
        result.max_score = float(max_total)
        result.save(update_fields=["objective_score", "total_score", "max_score", "updated_at"])

        if attempt and attempt.is_representative:
            attempt.meta = attempt.meta or {}
            attempt.meta["total_score"] = float(total_score)
            attempt.meta["synced_from_result"] = True
            attempt.meta.pop("status", None)
            attempt.save(update_fields=["meta", "updated_at"])

        # -------------------------------------------------
        # 7️⃣ progress pipeline 즉시 트리거
        # -------------------------------------------------
        # Submission에는 session_id 없음 → exam+enrollment 기준 최신 제출 조회
        submission_id = get_latest_exam_submission_id(
            enrollment_id=enrollment_id,
            exam_id=exam_id,
        )
        if submission_id:
            dispatch_progress_pipeline(submission_id=submission_id)
        else:
            dispatch_progress_pipeline(exam_id=int(exam_id))

        # 정책 SSOT: messaging-policy.md "저장과 발송은 분리" — 점수 저장 자체는 알림 트리거 아님.
        # exam_score_published = MANUAL_DEFAULT. 학원장이 명시적으로 발송 버튼 클릭(preview→confirm)할 때만 발송.

        return Response(
            {
                "ok": True,
                "exam_id": exam_id,
                "enrollment_id": enrollment_id,
                "question_id": question_id,
                "score": float(new_score),
                "objective_score": float(result.objective_score or 0.0),
                "total_score": float(total_score),
                "max_score": float(max_total),
            },
            status=drf_status.HTTP_200_OK,
        )
