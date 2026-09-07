# apps/domains/results/views/admin_session_exams_summary_view.py
from __future__ import annotations

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from apps.domains.results.permissions import IsTeacherOrAdmin
from apps.domains.results.serializers.session_exams_summary import SessionExamsSummarySerializer

# ✅ 단일 진실 유틸
from apps.domains.results.utils.clinic import get_clinic_enrollment_ids_for_session
from apps.domains.results.utils.session_exam import get_exams_for_session
from apps.domains.results.utils.result_queries import (
    latest_results_for_targets_per_enrollment,
)
from apps.domains.results.utils.initial_exam_score import (
    load_initial_exam_scores,
    project_initial_exam_score,
)
from apps.domains.results.services.omr_subjective_completion import (
    pending_omr_enrollment_ids_for_exams,
    pending_omr_result_ids,
)
from apps.support.results.admin_student_grades_dependencies import explicit_exam_target_scope
from apps.support.results.exam_policy_dependencies import exam_pass_score_overrides
from apps.support.results.progress_read_dependencies import (
    progress_policy_meta_for_lecture,
    session_score_enrollment_ids,
    session_for_tenant,
    session_progress_queryset_for_session,
)


class AdminSessionExamsSummaryView(APIView):
    """
    ✅ Session 기준 시험 요약 API (1 Session : N Exams)

    GET /results/admin/sessions/{session_id}/exams/summary/

    단일 진실 규칙:
    - 세션 단위 pass_rate: SessionProgress.exam_passed 기반 (집계 결과)
    - 세션 단위 clinic_rate: unresolved automatic ClinicLink + live source 기반
    - 시험 단위 점수 통계: Result(단, enrollment 중복 방어)
    """

    permission_classes = [IsAuthenticated, IsTeacherOrAdmin]

    def get(self, request, session_id: int):
        # ✅ tenant isolation: verify session belongs to tenant
        session = session_for_tenant(session_id=int(session_id), tenant=request.tenant)
        if not session:
            return Response(
                SessionExamsSummarySerializer({
                    "session_id": int(session_id),
                    "participant_count": 0,
                    "pass_rate": 0.0,
                    "clinic_rate": 0.0,
                    "strategy": "MAX",
                    "pass_source": "EXAM",
                    "exams": [],
                }).data
            )

        # 정책(표시용)
        policy_meta = progress_policy_meta_for_lecture(session.lecture)
        strategy = policy_meta["strategy"]
        pass_source = policy_meta["pass_source"]

        # ✅ 세션에 연결된 exams (단일 진실)
        exams = list(get_exams_for_session(session))
        exam_ids = [int(e.id) for e in exams]

        # -----------------------------
        # session-level participant/pass/clinic
        # -----------------------------
        sp_qs = session_progress_queryset_for_session(session).filter(
            enrollment__tenant=request.tenant,
        )
        participant_enrollment_ids = list(
            sp_qs.values_list("enrollment_id", flat=True)
        )
        participant_enrollment_id_set = set(participant_enrollment_ids)
        participant_count = len(participant_enrollment_ids)
        roster_enrollment_ids = session_score_enrollment_ids(
            tenant=request.tenant,
            session=session,
        )
        pending_enrollment_ids = pending_omr_enrollment_ids_for_exams(
            exam_ids=exam_ids,
            tenant_id=int(request.tenant.id),
            enrollment_ids=roster_enrollment_ids,
        )

        # 세션 단위 시험 통과율(집계 결과)
        pass_eligible_qs = sp_qs.exclude(
            enrollment_id__in=pending_enrollment_ids
        )
        pass_denominator = pass_eligible_qs.count()
        pass_count = pass_eligible_qs.filter(exam_passed=True).count()
        pass_rate = (pass_count / pass_denominator) if pass_denominator else 0.0

        # clinic_rate(단일 규칙)
        clinic_count = len(
            participant_enrollment_id_set.intersection(
                get_clinic_enrollment_ids_for_session(
                    session=session,
                    include_manual=False,
                )
            )
        )
        clinic_rate = (clinic_count / participant_count) if participant_count else 0.0

        # -----------------------------
        # exam-level stats (Result 기반, enrollment 중복 방어)
        # -----------------------------
        results_by_exam = {exam_id: [] for exam_id in exam_ids}
        latest_results = list(
            latest_results_for_targets_per_enrollment(
                target_type="exam",
                target_ids=exam_ids,
            )
            .filter(
                enrollment_id__in=roster_enrollment_ids,
                enrollment__tenant=request.tenant,
            )
            .select_related("attempt")
        )
        explicit_exam_ids, explicit_target_pairs = explicit_exam_target_scope(
            tenant=request.tenant,
            exam_ids=set(exam_ids),
            enrollment_ids=set(roster_enrollment_ids),
        )
        for result in latest_results:
            if (
                int(result.target_id) in explicit_exam_ids
                and (int(result.enrollment_id), int(result.target_id)) not in explicit_target_pairs
            ):
                continue
            results_by_exam[int(result.target_id)].append(result)
        initial_scores = load_initial_exam_scores(
            exam_ids=exam_ids,
            enrollment_ids=roster_enrollment_ids,
        )

        pass_score_map = {
            int(exam.id): float(getattr(exam, "pass_score", 0.0) or 0.0)
            for exam in exams
        }
        pass_score_map.update(
            exam_pass_score_overrides(
                exam_ids=exam_ids,
                lecture_id=int(session.lecture_id),
            )
        )
        exam_rows = []
        for ex in exams:
            results = results_by_exam.get(int(ex.id), [])
            pending_result_ids = pending_omr_result_ids(results)
            projected_scores = [
                project_initial_exam_score(
                    state=initial_scores.get((int(ex.id), int(result.enrollment_id))),
                    fallback_score=result.total_score,
                    fallback_max_score=result.max_score,
                    fallback_not_submitted=bool(
                        result.attempt_id
                        and isinstance(result.attempt.meta, dict)
                        and result.attempt.meta.get("status") == "NOT_SUBMITTED"
                    ),
                )
                for result in results
                if int(result.id) not in pending_result_ids
            ]
            scores = [
                projected.total_score
                for projected in projected_scores
                if projected.total_score is not None and not projected.not_submitted
            ]
            pass_score = pass_score_map.get(int(ex.id), 0.0)
            if pass_score > 0:
                pcount = sum(score >= pass_score for score in scores)
                fcount = sum(score < pass_score for score in scores)
            else:
                pcount = 0
                fcount = 0

            p_total = len(scores)
            p_rate = (pcount / p_total) if p_total else 0.0

            exam_rows.append({
                "exam_id": int(ex.id),
                "title": str(getattr(ex, "title", "") or ""),
                "pass_score": float(pass_score),

                "participant_count": p_total,
                "avg_score": (sum(scores) / len(scores)) if scores else 0.0,
                "min_score": min(scores) if scores else 0.0,
                "max_score": float(getattr(ex, "max_score", 0.0) or 0.0),
                "highest_score": max(scores) if scores else 0.0,

                "pass_count": int(pcount),
                "fail_count": int(fcount),
                "pass_rate": round(float(p_rate), 4),
            })

        payload = {
            "session_id": int(session.id),
            "participant_count": int(participant_count),

            # ✅ 의미 고정:
            # pass_rate = SessionProgress.exam_passed 기반 (집계 결과)
            "pass_rate": round(float(pass_rate), 4),

            # ✅ 의미 고정:
            # clinic_rate = unresolved automatic ClinicLink + live source 기준
            "clinic_rate": round(float(clinic_rate), 4),

            "strategy": strategy,
            "pass_source": pass_source,
            "exams": exam_rows,

            # (권장) pass_rate_source 같은 메타를 serializer에 추가하면 사고 방지에 큰 도움
            # "pass_rate_source": "SESSION_PROGRESS",
            # "clinic_rate_source": "CLINIC_LINK_AUTO",
        }

        return Response(SessionExamsSummarySerializer(payload).data)
