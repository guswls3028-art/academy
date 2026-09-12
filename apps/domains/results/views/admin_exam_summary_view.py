# apps/domains/results/views/admin_exam_summary_view.py
from __future__ import annotations

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import ValidationError

from apps.api.common.query_params import parse_query_int
from apps.support.results.exam_policy_dependencies import (
    effective_exam_pass_scores,
)
from apps.domains.results.permissions import IsTeacherOrAdmin
from apps.domains.results.serializers.admin_exam_summary import AdminExamSummarySerializer

# ✅ 단일 진실 유틸
from apps.domains.results.utils.clinic import get_clinic_enrollment_ids_for_session
from apps.domains.results.utils.result_queries import latest_results_per_enrollment
from apps.domains.results.utils.initial_exam_score import (
    load_initial_exam_scores,
    project_initial_exam_score,
)
from apps.domains.results.services.omr_subjective_completion import (
    pending_omr_result_ids,
)
from apps.support.results.admin_exam_dependencies import get_regular_active_exam_for_tenant


class AdminExamSummaryView(APIView):
    """
    LEGACY COMPAT
    GET /results/admin/exams/<exam_id>/summary/

    ✅ 계약 유지(프론트 안정성):
    - participant_count, avg/min/max, pass_count/fail_count/pass_rate, clinic_count

    ✅ 정합성 강화:
    - Result 중복 enrollment 방어: 최신 Result만 집계
    - clinic_count 기준 통일: ClinicLink(is_auto=True) enrollment distinct
    - Session↔Exam 매핑 단일화(utils.session_exam)
    """

    permission_classes = [IsAuthenticated, IsTeacherOrAdmin]

    def get(self, request, exam_id: int):
        exam_id = int(exam_id)

        EMPTY = {
            "participant_count": 0,
            "avg_score": 0.0,
            "min_score": 0.0,
            "max_score": 0.0,
            "pass_count": 0,
            "fail_count": 0,
            "pass_rate": 0.0,
            "clinic_count": 0,
        }

        # ✅ tenant isolation: verify exam belongs to tenant
        exam = get_regular_active_exam_for_tenant(
            exam_id=exam_id,
            tenant=request.tenant,
        )
        linked_lecture_ids = set(
            exam.sessions.filter(lecture__tenant=request.tenant)
            .values_list("lecture_id", flat=True)
        )
        lecture_id = parse_query_int(
            request.query_params,
            "lecture_id",
            min_value=1,
        )
        if lecture_id is not None and lecture_id not in linked_lecture_ids:
            raise ValidationError(
                {"lecture_id": "이 시험에 연결되지 않은 강의입니다."}
            )
        scoped_lecture_ids = (
            {lecture_id} if lecture_id is not None else linked_lecture_ids
        )
        pass_scores_by_lecture = effective_exam_pass_scores(
            exam=exam,
            lecture_ids=linked_lecture_ids,
        )

        # ✅ 중복 방어: enrollment당 최신 Result만.
        # participant_count는 결시 기록도 포함하는 기존 계약을 유지하되,
        # 미응시는 점수가 아니므로 점수·합불 집계에서는 제외한다.
        rs = latest_results_per_enrollment(
            target_type="exam",
            target_id=exam_id,
        ).filter(
            enrollment__tenant=request.tenant,
            enrollment__lecture_id__in=scoped_lecture_ids,
        )
        results = list(rs.select_related("attempt", "enrollment"))
        participant_count = len(results)
        if participant_count == 0:
            return Response(AdminExamSummarySerializer(EMPTY).data)

        initial_scores = load_initial_exam_scores(
            exam_ids=[exam_id],
            enrollment_ids=[result.enrollment_id for result in results],
        )
        pending_result_ids = pending_omr_result_ids(results)
        scored_rows = []
        for result in results:
            if int(result.id) in pending_result_ids:
                continue
            initial_state = initial_scores.get((exam_id, int(result.enrollment_id)))
            legacy_scored = bool(
                result.attempt_id is None
                or (
                    result.attempt.status == "done"
                    and not (
                        isinstance(result.attempt.meta, dict)
                        and result.attempt.meta.get("status") == "NOT_SUBMITTED"
                    )
                )
            )
            projected = project_initial_exam_score(
                state=initial_state,
                fallback_score=result.total_score,
                fallback_max_score=result.max_score,
                fallback_not_submitted=not legacy_scored,
            )
            if projected.total_score is not None and not projected.not_submitted:
                scored_rows.append((
                    projected.total_score,
                    int(result.enrollment.lecture_id),
                ))

        pass_count = sum(
            score >= pass_scores_by_lecture.get(lecture_id_value, 0.0)
            for score, lecture_id_value in scored_rows
            if pass_scores_by_lecture.get(lecture_id_value, 0.0) > 0
        )
        fail_count = sum(
            score < pass_scores_by_lecture.get(lecture_id_value, 0.0)
            for score, lecture_id_value in scored_rows
            if pass_scores_by_lecture.get(lecture_id_value, 0.0) > 0
        )
        scored_count = pass_count + fail_count
        pass_rate = (pass_count / scored_count) if scored_count else 0.0

        # ✅ clinic_count는 session 기반으로만 계산 가능(시험만으론 clinic이 정의되지 않음)
        clinic_ids = set()
        for session in exam.sessions.filter(
            lecture_id__in=scoped_lecture_ids,
            lecture__tenant=request.tenant,
        ):
            clinic_ids.update(
                get_clinic_enrollment_ids_for_session(
                    session=session,
                    include_manual=False,
                )
            )
        clinic_count = len(clinic_ids)
        scores = [score for score, _lecture_id in scored_rows]

        payload = {
            "participant_count": int(participant_count),
            "avg_score": float(sum(scores) / len(scores)) if scores else 0.0,
            "min_score": float(min(scores)) if scores else 0.0,
            "max_score": float(max(scores)) if scores else 0.0,
            "pass_count": int(pass_count),
            "fail_count": int(fail_count),
            "pass_rate": round(float(pass_rate), 4),
            "clinic_count": int(clinic_count),
        }

        return Response(AdminExamSummarySerializer(payload).data)
