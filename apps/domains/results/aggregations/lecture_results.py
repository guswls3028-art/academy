# PATH: apps/domains/results/aggregations/lecture_results.py
from __future__ import annotations

from typing import Any, Dict, List

from django.utils import timezone

from apps.domains.results.utils.clinic import get_clinic_enrollment_ids_for_session
from apps.domains.results.utils.session_exam import get_exams_for_session
from apps.support.results.exam_policy_dependencies import (
    effective_exam_pass_score,
)
from apps.domains.results.utils.result_queries import latest_results_per_enrollment
from apps.domains.results.utils.initial_exam_score import (
    load_initial_exam_scores,
    project_initial_exam_score,
)
from apps.domains.results.services.omr_subjective_completion import (
    pending_omr_enrollment_ids_for_exams,
    pending_omr_result_ids,
)
from apps.support.results.progress_read_dependencies import (
    lecture_by_id,
    progress_policy_meta_for_lecture,
    session_progress_queryset_for_session,
    sessions_for_lecture,
)


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return int(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return float(default)


def _safe_str(v: Any) -> str:
    try:
        return str(v or "")
    except Exception:
        return ""


def _policy_meta_for_lecture(lecture: Any) -> Dict[str, str]:
    return progress_policy_meta_for_lecture(lecture)


def build_lecture_results_snapshot(
    *,
    lecture_id: int,
    include_exam_level_stats: bool = False,
) -> Dict[str, Any]:
    """
    ✅ Lecture 단위 스냅샷 (세션 집계 기반)

    단일 진실:
    - 세션별 pass_rate: SessionProgress.exam_passed 기준
    - 세션별 clinic_rate: ClinicLink(is_auto=True) 기준
    - (옵션) 시험 단위 통계는 Result 기반 + latest_results_per_enrollment

    반환 스키마(고정):
    {
      "lecture_id": int,
      "strategy": str,
      "pass_source": str,
      "session_count": int,
      "sessions": [
        {
          "session_id": int,
          "order": int|None,
          "participant_count": int,
          "pass_rate": float,
          "clinic_rate": float,
          "exams": [ ... ] | []
        }
      ],
      "generated_at": "iso"
    }
    """
    lecture = lecture_by_id(_safe_int(lecture_id))
    if not lecture:
        return {
            "lecture_id": _safe_int(lecture_id),
            "strategy": "MAX",
            "pass_source": "EXAM",
            "session_count": 0,
            "sessions": [],
            "generated_at": timezone.now().isoformat(),
        }

    meta = _policy_meta_for_lecture(lecture)

    sessions_qs = sessions_for_lecture(lecture)

    rows: List[Dict[str, Any]] = []
    for s in sessions_qs:
        exams = list(get_exams_for_session(s))
        pending_enrollment_ids = pending_omr_enrollment_ids_for_exams(
            exam_ids=[int(exam.id) for exam in exams],
            tenant_id=int(s.lecture.tenant_id),
        )
        sp_qs = session_progress_queryset_for_session(s)
        participant_count = sp_qs.count()

        pass_eligible_qs = sp_qs.exclude(enrollment_id__in=pending_enrollment_ids)
        pass_denominator = pass_eligible_qs.count()
        pass_count = pass_eligible_qs.filter(exam_passed=True).count()
        pass_rate = (pass_count / pass_denominator) if pass_denominator else 0.0

        clinic_count = len(
            get_clinic_enrollment_ids_for_session(
                session=s,
                include_manual=False,
            )
        )
        clinic_rate = (clinic_count / participant_count) if participant_count else 0.0

        ex_rows: List[Dict[str, Any]] = []
        if include_exam_level_stats:
            for ex in exams:
                exid = _safe_int(getattr(ex, "id", 0))
                if not exid:
                    continue

                results = list(
                    latest_results_per_enrollment(
                        target_type="exam",
                        target_id=exid,
                    ).filter(
                        enrollment__tenant=getattr(s.lecture, "tenant", None),
                        enrollment__lecture_id=getattr(s, "lecture_id", None),
                    ).select_related("attempt")
                )
                initial_scores = load_initial_exam_scores(
                    exam_ids=[exid],
                    enrollment_ids=[result.enrollment_id for result in results],
                )
                pending_result_ids = pending_omr_result_ids(results)
                projected_scores = [
                    project_initial_exam_score(
                        state=initial_scores.get((exid, int(result.enrollment_id))),
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

                pass_score = effective_exam_pass_score(
                    exam=ex,
                    lecture_id=getattr(s, "lecture_id", None),
                )
                if pass_score > 0:
                    pcount = sum(score >= pass_score for score in scores)
                    fcount = sum(score < pass_score for score in scores)
                else:
                    pcount = 0
                    fcount = 0

                p_total = len(results)
                scored_total = pcount + fcount
                p_rate = (pcount / scored_total) if scored_total else 0.0

                ex_rows.append(
                    {
                        "exam_id": exid,
                        "title": _safe_str(getattr(ex, "title", "")),
                        "pass_score": float(pass_score),
                        "participant_count": int(p_total),
                        "avg_score": float(sum(scores) / len(scores)) if scores else 0.0,
                        "min_score": float(min(scores)) if scores else 0.0,
                        "max_score": float(max(scores)) if scores else 0.0,
                        "pass_count": int(pcount),
                        "fail_count": int(fcount),
                        "pass_rate": round(float(p_rate), 4),
                    }
                )

        rows.append(
            {
                "session_id": int(s.id),
                "order": _safe_int(getattr(s, "order", None), 0) if getattr(s, "order", None) is not None else None,
                "participant_count": int(participant_count),
                "pass_rate": round(float(pass_rate), 4),
                "clinic_rate": round(float(clinic_rate), 4),
                "exams": ex_rows,
            }
        )

    return {
        "lecture_id": int(lecture.id),
        "strategy": str(meta["strategy"]),
        "pass_source": str(meta["pass_source"]),
        "session_count": int(sessions_qs.count()),
        "sessions": rows,
        "generated_at": timezone.now().isoformat(),
    }
