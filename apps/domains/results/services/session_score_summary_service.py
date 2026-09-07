# apps/domains/results/services/session_score_summary_service.py
from __future__ import annotations

from django.db.models import Count

from apps.domains.results.models import ExamAttempt
from apps.domains.results.services.omr_subjective_completion import (
    pending_omr_enrollment_ids_for_exams,
    pending_omr_result_ids,
)

# ✅ 단일 진실 유틸
from apps.domains.results.utils.clinic import get_clinic_enrollment_ids_for_session
from apps.domains.results.utils.session_exam import get_exam_ids_for_session
from apps.domains.results.utils.result_queries import (
    latest_results_for_targets_per_enrollment,
)
from apps.domains.results.utils.initial_exam_score import (
    load_initial_exam_scores,
    project_initial_exam_score,
)
from apps.support.results.progress_read_dependencies import (
    session_by_id,
    session_progress_queryset_for_session,
    session_score_enrollment_ids,
)
from apps.support.progress.session_calculator_dependencies import (
    get_exam_target_enrollment_pairs_for_session,
)


class SessionScoreSummaryService:
    """
    ✅ Session 단위 성적 통계 (운영/대시보드)

    단일 진실 규칙:
    - 점수 통계: Result(단, enrollment 중복 방어 적용)
    - 세션 통과율: SessionProgress.completed(혹은 정책에 따라 exam_passed) 중 무엇인지 '정의'가 필요하지만
      기존 원본은 completed를 사용했으므로 원본 의미를 존중한다.
    - 클리닉: unresolved automatic ClinicLink + live source enrollment distinct

    ⚠️ 세션1:시험N 구조 반영:
    - session에 연결된 exam_id들을 모두 가져와서 통계를 만든다.
    - 다만 "세션 전체 점수"를 1개 숫자로 만들 때는 집계 전략이 필요함.
      이 서비스는 "세션 운영 통계" 성격이므로:
        - 점수 집계는 우선 exams 전체 Result를 합쳐 평균/최소/최대를 구하는 보수적 방식으로 제공.
      (정교한 전략은 AdminSessionExamsSummaryView에서 exam별로 제공하는 것이 정석)
    """

    @staticmethod
    def build(*, session_id: int) -> dict:
        EMPTY_SUMMARY = {
            "participant_count": 0,
            "avg_score": 0.0,
            "min_score": 0.0,
            "max_score": 0.0,
            "pass_rate": 0.0,
            "clinic_rate": 0.0,
            "attempt_stats": {
                "avg_attempts": 0.0,
                "retake_ratio": 0.0,
            },
        }

        session = session_by_id(int(session_id))
        if not session:
            return EMPTY_SUMMARY

        tenant_id = int(session.lecture.tenant_id)
        progresses = session_progress_queryset_for_session(session).filter(
            enrollment__tenant_id=tenant_id,
        )
        participant_enrollment_ids = list(
            progresses.values_list("enrollment_id", flat=True)
        )
        participant_enrollment_id_set = set(participant_enrollment_ids)
        participant_count = len(participant_enrollment_ids)

        exam_ids = get_exam_ids_for_session(session)
        if not exam_ids:
            # 세션에 시험이 없으면 점수 통계는 0, pass/clinic은 progress로만 판단 가능
            pass_count = progresses.filter(completed=True).count()
            clinic_count = len(
                participant_enrollment_id_set.intersection(
                    get_clinic_enrollment_ids_for_session(
                        session=session,
                        include_manual=False,
                    )
                )
            )
            return {
                **EMPTY_SUMMARY,
                "participant_count": int(participant_count),
                "pass_rate": round((pass_count / participant_count), 4) if participant_count else 0.0,
                "clinic_rate": round((clinic_count / participant_count), 4) if participant_count else 0.0,
            }

        roster_enrollment_ids = session_score_enrollment_ids(
            tenant=session.lecture.tenant,
            session=session,
        )
        target_pairs = get_exam_target_enrollment_pairs_for_session(
            session=session,
            enrollment_ids=roster_enrollment_ids,
        )
        pending_enrollment_ids = pending_omr_enrollment_ids_for_exams(
            exam_ids=exam_ids,
            tenant_id=tenant_id,
            enrollment_ids=roster_enrollment_ids,
        )

        # -------------------------------------------------
        # pass_rate: 원본은 SessionProgress.completed 기준
        # -------------------------------------------------
        pass_eligible_qs = progresses.exclude(
            enrollment_id__in=pending_enrollment_ids
        )
        pass_denominator = pass_eligible_qs.count()
        pass_count = pass_eligible_qs.filter(completed=True).count()
        pass_rate = (pass_count / pass_denominator) if pass_denominator else 0.0

        # -------------------------------------------------
        # clinic_rate: ClinicLink 중 현재 참여자만 집계
        # -------------------------------------------------
        clinic_count = len(
            participant_enrollment_id_set.intersection(
                get_clinic_enrollment_ids_for_session(
                    session=session,
                    include_manual=False,
                )
            )
        )
        clinic_rate = (clinic_count / participant_count) if participant_count else 0.0

        # -------------------------------------------------
        # 점수 통계:
        # - 세션에 연결된 모든 시험의 Result를 모아서 통계
        # - enrollment 중복 방어: 여러 시험의 최신 대표 결과를 한 번에 선택
        # - NOT_SUBMITTED 대표 시도는 점수 통계에서 제외
        # -------------------------------------------------
        all_results = [
            result
            for result in latest_results_for_targets_per_enrollment(
                target_type="exam",
                target_ids=exam_ids,
            )
            .filter(
                enrollment_id__in=roster_enrollment_ids,
                enrollment__tenant_id=tenant_id,
            )
            .select_related("attempt")
            if (int(result.enrollment_id), int(result.target_id)) in target_pairs
        ]
        initial_scores = load_initial_exam_scores(
            exam_ids=exam_ids,
            enrollment_ids=roster_enrollment_ids,
        )
        pending_result_ids = pending_omr_result_ids(all_results)
        projected_scores = [
            project_initial_exam_score(
                state=initial_scores.get((int(result.target_id), int(result.enrollment_id))),
                fallback_score=result.total_score,
                fallback_max_score=result.max_score,
                fallback_not_submitted=bool(
                    result.attempt_id
                    and isinstance(result.attempt.meta, dict)
                    and result.attempt.meta.get("status") == "NOT_SUBMITTED"
                ),
            )
            for result in all_results
            if int(result.id) not in pending_result_ids
        ]
        scores = [
            projected.total_score
            for projected in projected_scores
            if projected.total_score is not None and not projected.not_submitted
        ]

        if not scores:
            score_summary = {"avg_score": 0.0, "min_score": 0.0, "max_score": 0.0}
        else:
            score_summary = {
                "avg_score": (sum(scores) / len(scores)) if scores else 0.0,
                "min_score": min(scores) if scores else 0.0,
                "max_score": max(scores) if scores else 0.0,
            }

        # -------------------------------------------------
        # Attempt 통계(재시험 비율):
        # - 세션에 연결된 모든 시험을 대상으로 attempt 통계
        # -------------------------------------------------
        attempts = ExamAttempt.objects.filter(
            exam_id__in=[int(x) for x in exam_ids],
            enrollment_id__in=roster_enrollment_ids,
            enrollment__tenant_id=tenant_id,
        )

        per_exam_enrollment = [
            row
            for row in attempts.values("exam_id", "enrollment_id")
            .annotate(cnt=Count("id"))
            if (int(row["enrollment_id"]), int(row["exam_id"])) in target_pairs
        ]

        total_attempts = sum(int(r["cnt"] or 0) for r in per_exam_enrollment)
        attempted_pairs = len(per_exam_enrollment)
        retake_pairs = sum(1 for r in per_exam_enrollment if int(r["cnt"] or 0) > 1)

        avg_attempts = (total_attempts / attempted_pairs) if attempted_pairs else 0.0
        retake_ratio = (retake_pairs / attempted_pairs) if attempted_pairs else 0.0

        return {
            "participant_count": int(participant_count),
            "avg_score": float(score_summary["avg_score"]),
            "min_score": float(score_summary["min_score"]),
            "max_score": float(score_summary["max_score"]),
            "pass_rate": round(float(pass_rate), 4),
            "clinic_rate": round(float(clinic_rate), 4),
            "attempt_stats": {
                "avg_attempts": round(float(avg_attempts), 2),
                "retake_ratio": round(float(retake_ratio), 4),
            },
        }
