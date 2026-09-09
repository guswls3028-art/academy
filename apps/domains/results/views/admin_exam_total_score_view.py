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
from apps.domains.results.models import ExamAttempt, Result, ResultFact, ResultItem
from apps.domains.results.validation import parse_finite_score
from apps.support.results.exam_policy_dependencies import (
    effective_exam_pass_score,
)

from apps.domains.results.guards.exam_enrollment_guard import validate_exam_enrollment_assigned
from apps.domains.results.guards.score_edit_lease_guard import (
    require_score_edit_lease_from_headers,
)

from apps.support.results.admin_exam_dependencies import (
    dispatch_progress_pipeline,
    get_latest_exam_submission_id,
    lock_regular_active_exam_for_tenant,
    lock_enrollment_for_exam_state_transition,
    resolve_exam_not_submitted_clinic_links,
)
from django.db.models import Max


class AdminExamTotalScoreView(APIView):
    """
    PATCH /results/admin/exams/{exam_id}/enrollments/{enrollment_id}/score/

    ✅ 목적
    - 성적 탭에서 시험 "합산 점수"를 직접 입력 (Result.total_score override)

    ⚠️ 주의
    - ResultItem 합과 total_score가 불일치할 수 있다.
      (문항별 채점 모드와 합산 입력 모드가 동시에 사용될 수 있으므로, 모드 선택은 프론트 UX로 제어)
    """

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
            sub="total",
        )

        # ✅ tenant isolation: verify enrollment belongs to tenant
        from apps.domains.results.guards.enrollment_tenant_guard import validate_enrollment_belongs_to_tenant
        enrollment = validate_enrollment_belongs_to_tenant(enrollment_id, request.tenant)
        validate_exam_enrollment_assigned(exam, enrollment_id)

        # ── 미응시 처리: meta_status="NOT_SUBMITTED" ──
        meta_status = request.data.get("meta_status")
        if meta_status == "NOT_SUBMITTED":
            return self._handle_not_submitted(request, exam, exam_id, enrollment_id)

        if "score" not in request.data:
            raise ValidationError({"detail": "score is required", "code": "INVALID"})

        new_score = parse_finite_score(request.data.get("score"))

        if new_score < 0:
            raise ValidationError({"detail": "score must be >= 0", "code": "INVALID"})

        # max_score는 시험 정책의 단일 진실이다. 오래 열린 화면이 과거 만점을
        # 보내더라도 학생별 Result가 서로 다른 만점으로 저장되면 안 된다.
        # 필드가 있으면 형식만 검증해 기존 요청 오류 계약은 유지한다.
        req_max = request.data.get("max_score")
        if req_max is not None:
            parse_finite_score(req_max, field_name="max_score")
        max_score = float(getattr(exam, "max_score", 100.0) or 100.0)

        # -------------------------------------------------
        # 1️⃣ Result (대표 스냅샷)
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
        if not result or not result.attempt_id:
            # 과제 quick patch처럼 "없으면 생성" (수동 입력용 attempt/result 생성)
            qs = (
                ExamAttempt.objects
                .select_for_update()
                .filter(exam_id=exam_id, enrollment_id=enrollment_id)
            )
            last = qs.aggregate(Max("attempt_index")).get("attempt_index__max") or 0
            next_index = int(last) + 1
            qs.filter(is_representative=True).update(is_representative=False)
            # attempt_index=1이면 1차 점수 불변 스냅샷 저장 (석차 정책 SSOT).
            # 이후 ONLINE 재응시가 Result.total_score를 덮어써도 석차는 이 값 고정.
            _initial_meta = None
            if next_index == 1:
                _initial_meta = {
                    "initial_snapshot": {
                        "total_score": float(new_score),
                        "max_score": float(max_score),
                        "submitted_at": timezone.now().isoformat(),
                        "source": "admin_manual_total",
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

        # -------------------------------------------------
        # 2️⃣ Attempt LOCK 상태 확인
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
        # 3️⃣ 점수 범위 검증
        # -------------------------------------------------
        effective_max = float(max_score)
        if new_score > float(effective_max):
            raise ValidationError(
                {"detail": f"score must be between 0 and {effective_max}", "code": "INVALID"}
            )

        # -------------------------------------------------
        # 4️⃣ ResultFact (append-only 로그)
        # -------------------------------------------------
        pass_score = effective_exam_pass_score(
            exam=exam,
            lecture_id=getattr(enrollment, "lecture_id", None),
        )

        # submission은 있을 수도/없을 수도 있음 (오프라인 입력 허용)
        # Submission 모델에는 session_id 없음 → exam+enrollment 기준으로 최신 제출 조회
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
            question_id=0,  # total override marker
            answer="",
            is_correct=bool(float(new_score) >= float(pass_score)),
            score=float(new_score),
            max_score=float(effective_max),
            source="manual_total",
            meta={
                "manual_total": True,
                "edited_at": timezone.now().isoformat(),
            },
        )

        # -------------------------------------------------
        # 5️⃣ Result 업데이트 (합산 입력 시 total만 변경, objective_score 유지)
        # -------------------------------------------------
        result.total_score = float(new_score)
        result.max_score = float(max_score)
        result.save(update_fields=["total_score", "max_score", "updated_at"])

        # -------------------------------------------------
        # 5-b) Representative ExamAttempt 점수 동기화 + NOT_SUBMITTED 해제
        # -------------------------------------------------
        if attempt and attempt.is_representative:
            attempt_meta = dict(attempt.meta or {})
            attempt_meta["total_score"] = float(new_score)
            attempt_meta["max_score"] = float(max_score)
            attempt_meta["synced_from_result"] = True
            attempt_meta.pop("status", None)  # 정상 점수 입력 시 NOT_SUBMITTED 해제
            if int(attempt.attempt_index) == 1:
                initial_snapshot = dict(
                    attempt_meta.get("initial_snapshot")
                    if isinstance(attempt_meta.get("initial_snapshot"), dict)
                    else {}
                )
                initial_snapshot["total_score"] = float(new_score)
                initial_snapshot["max_score"] = float(max_score)
                initial_snapshot.setdefault(
                    "submitted_at",
                    timezone.now().isoformat(),
                )
                initial_snapshot.setdefault("source", "admin_manual_total")
                attempt_meta["initial_snapshot"] = initial_snapshot
            attempt.meta = attempt_meta
            attempt.save(update_fields=["meta", "updated_at"])

        # -------------------------------------------------
        # 6️⃣ progress pipeline (best-effort, 실패해도 점수 저장은 유지)
        # Submission이 있으면 submission 기반, 없으면 exam_id 기반으로 dispatch
        # -------------------------------------------------
        # progress pipeline: 동기 dispatch (Result commit 후 즉시 실행)
        progress_ok = False
        progress_error = None
        progress_debug = {}
        try:
            # 디버그: pipeline 실행 전 상태 확인
            from apps.domains.results.models import Result as _R
            from apps.domains.results.utils.session_exam import get_session_ids_for_exam
            _results_count = _R.objects.filter(target_type="exam", target_id=int(exam_id)).count()
            _session_ids = get_session_ids_for_exam(int(exam_id))
            progress_debug = {
                "results_for_exam": _results_count,
                "sessions_for_exam": _session_ids,
            }

            if submission_id:
                dispatch_progress_pipeline(submission_id=int(submission_id))
            else:
                dispatch_progress_pipeline(exam_id=int(exam_id))
            progress_ok = True
        except Exception as exc:
            logger.exception("progress pipeline failed (exam=%s, submission=%s)", exam_id, submission_id)
            progress_error = str(exc)[:200]

        # 정책 SSOT: messaging-policy.md "저장과 발송은 분리" — 점수 저장 자체는 알림 트리거 아님.
        # exam_score_published = MANUAL_DEFAULT. 학원장이 명시적으로 발송 버튼 클릭(preview→confirm)할 때만 발송.

        return Response(
            {
                "ok": True,
                "exam_id": exam_id,
                "enrollment_id": enrollment_id,
                "total_score": float(result.total_score or 0.0),
                "max_score": float(result.max_score or 0.0),
                "progress": {"dispatched": progress_ok, "error": progress_error, "debug": progress_debug},
            },
            status=drf_status.HTTP_200_OK,
        )

    # ──────────────────────────────────────────────────
    # 미응시 처리 (/ + Enter)
    # ──────────────────────────────────────────────────
    @transaction.atomic
    def _handle_not_submitted(self, request, exam, exam_id: int, enrollment_id: int):
        """
        시험 미응시 처리.
        - ExamAttempt.meta.status = "NOT_SUBMITTED"
        - Result 점수와 문항 snapshot 제거
        - 이전 실패 ClinicLink는 감사 이력을 보존해 NOT_SUBMITTED로 해소
        - 프론트/API에서 meta.status로 "미응시" 표시
        """
        max_score = float(getattr(exam, "max_score", 100.0) or 100.0)
        state_changed = False
        lock_enrollment_for_exam_state_transition(
            enrollment_id=enrollment_id,
            tenant=request.tenant,
        )

        result = (
            Result.objects.select_for_update()
            .filter(target_type="exam", target_id=exam_id, enrollment_id=enrollment_id)
            .first()
        )
        if not result or not result.attempt_id:
            qs = ExamAttempt.objects.select_for_update().filter(
                exam_id=exam_id, enrollment_id=enrollment_id
            )
            last = qs.aggregate(Max("attempt_index")).get("attempt_index__max") or 0
            next_index = int(last) + 1
            qs.filter(is_representative=True).update(is_representative=False)
            attempt = ExamAttempt.objects.create(
                exam_id=exam_id, enrollment_id=enrollment_id,
                submission_id=0, attempt_index=next_index,
                is_retake=(last > 0), is_representative=True,
                status="done",
                meta={
                    "status": "NOT_SUBMITTED",
                    "total_score": 0.0,
                    "max_score": max_score,
                    "synced_from_result": True,
                },
            )
            state_changed = True
            if not result:
                result = Result.objects.create(
                    target_type="exam", target_id=exam_id,
                    enrollment_id=enrollment_id, attempt_id=int(attempt.id),
                    total_score=0.0, max_score=max_score, objective_score=0.0,
                )
            else:
                result.attempt_id = int(attempt.id)
                result.total_score = 0.0
                result.max_score = max_score
                result.objective_score = 0.0
                result.save(
                    update_fields=[
                        "attempt_id",
                        "total_score",
                        "max_score",
                        "objective_score",
                        "updated_at",
                    ]
                )
        else:
            attempt = (
                ExamAttempt.objects.select_for_update()
                .filter(id=int(result.attempt_id))
                .first()
            )
            if attempt:
                meta = dict(attempt.meta or {})
                normalized_meta = {
                    **meta,
                    "status": "NOT_SUBMITTED",
                    "total_score": 0.0,
                    "max_score": max_score,
                    "synced_from_result": True,
                }
                if meta != normalized_meta or attempt.status != "done":
                    state_changed = True
                    attempt.meta = normalized_meta
                    attempt.status = "done"
                    attempt.save(update_fields=["meta", "status", "updated_at"])
            if (
                float(result.total_score or 0.0) != 0.0
                or float(result.objective_score or 0.0) != 0.0
                or float(result.max_score or 0.0) != max_score
            ):
                state_changed = True
                result.total_score = 0.0
                result.objective_score = 0.0
                result.max_score = max_score
                result.save(
                    update_fields=[
                        "total_score",
                        "objective_score",
                        "max_score",
                        "updated_at",
                    ]
                )

        deleted_items, _ = (
            ResultItem.objects.select_for_update().filter(result=result).delete()
        )
        state_changed = state_changed or bool(deleted_items)
        resolved_clinic_links = resolve_exam_not_submitted_clinic_links(
            tenant_id=int(request.tenant.id),
            enrollment_id=enrollment_id,
            exam_id=exam_id,
            attempt_id=int(result.attempt_id),
            user_id=int(request.user.id),
        )
        state_changed = state_changed or bool(resolved_clinic_links)

        # audit
        audit_exists = ResultFact.objects.filter(
            target_type="exam",
            target_id=exam_id,
            enrollment_id=enrollment_id,
            attempt_id=int(result.attempt_id),
            source="manual_not_submitted",
            meta__status="NOT_SUBMITTED",
        ).exists()
        if state_changed or not audit_exists:
            ResultFact.objects.create(
                target_type="exam", target_id=exam_id,
                enrollment_id=enrollment_id, submission_id=0,
                attempt_id=int(result.attempt_id), question_id=0,
                answer="", is_correct=False, score=0.0, max_score=max_score,
                source="manual_not_submitted",
                meta={
                    "status": "NOT_SUBMITTED",
                    "edited_at": timezone.now().isoformat(),
                    "user_id": int(request.user.id),
                    "deleted_result_items": int(deleted_items),
                    "resolved_clinic_links": int(resolved_clinic_links),
                },
            )

        # progress pipeline (clinic 판정)
        progress_ok = False
        try:
            dispatch_progress_pipeline(exam_id=int(exam_id))
            progress_ok = True
        except Exception:
            logger.exception("progress pipeline failed for NOT_SUBMITTED (exam=%s)", exam_id)

        return Response(
            {"ok": True, "exam_id": exam_id, "enrollment_id": enrollment_id,
             "total_score": 0.0, "max_score": max_score,
             "meta_status": "NOT_SUBMITTED",
             "progress": {"dispatched": progress_ok}},
            status=drf_status.HTTP_200_OK,
        )
