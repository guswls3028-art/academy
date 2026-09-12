# PATH: apps/domains/results/aggregations/session_results.py
from __future__ import annotations

from dataclasses import dataclass
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
    progress_policy_meta_for_lecture,
    session_by_id,
    session_progress_queryset_for_session,
)


@dataclass(frozen=True)
class SessionExamStatRow:
    exam_id: int
    title: str
    pass_score: float

    participant_count: int
    avg_score: float
    min_score: float
    max_score: float

    pass_count: int
    fail_count: int
    pass_rate: float


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return float(default)


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return int(default)


def _safe_str(v: Any) -> str:
    try:
        return str(v or "")
    except Exception:
        return ""


def _policy_meta_for_session(session: Any) -> Dict[str, str]:
    """
    ProgressPolicy는 progress 도메인의 단일 진실.
    단, results 집계는 "표시용 메타"만 가져온다.
    """
    return progress_policy_meta_for_lecture(session.lecture)


def build_session_results_snapshot(*, session_id: int) -> Dict[str, Any]:
    """
    ✅ Session 단위 시험 요약 스냅샷 (집계)

    - participant_count: SessionProgress 기준
    - pass_rate: SessionProgress.exam_passed 기준 (세션 집계 단일 진실)
    - clinic_rate: ClinicLink(is_auto=True) enrollment distinct 기준 (단일 진실)
    - exams[]: 시험 단위 통계는 Result 기반 (단, enrollment 중복 방어 latest_results_per_enrollment)

    반환 스키마(고정):
    {
      "session_id": int,
      "participant_count": int,
      "pass_rate": float,
      "clinic_rate": float,
      "strategy": str,
      "pass_source": str,
      "exams": [ ... ],
      "generated_at": "iso"
    }
    """
    session = session_by_id(_safe_int(session_id))
    if not session:
        return {
            "session_id": _safe_int(session_id),
            "participant_count": 0,
            "pass_rate": 0.0,
            "clinic_rate": 0.0,
            "strategy": "MAX",
            "pass_source": "EXAM",
            "exams": [],
            "generated_at": timezone.now().isoformat(),
        }

    # 정책 메타 (표시용)
    meta = _policy_meta_for_session(session)
    strategy = meta["strategy"]
    pass_source = meta["pass_source"]

    exams = list(get_exams_for_session(session))
    pending_enrollment_ids = pending_omr_enrollment_ids_for_exams(
        exam_ids=[int(exam.id) for exam in exams],
        tenant_id=int(session.lecture.tenant_id),
    )

    # 세션 모수/통과율(집계 단일 진실)
    sp_qs = session_progress_queryset_for_session(session)
    participant_count = sp_qs.count()

    pass_eligible_qs = sp_qs.exclude(enrollment_id__in=pending_enrollment_ids)
    pass_denominator = pass_eligible_qs.count()
    pass_count = pass_eligible_qs.filter(exam_passed=True).count()
    pass_rate = (pass_count / pass_denominator) if pass_denominator else 0.0

    # clinic_rate(단일 진실)
    clinic_count = len(
        get_clinic_enrollment_ids_for_session(
            session=session,
            include_manual=False,
        )
    )
    clinic_rate = (clinic_count / participant_count) if participant_count else 0.0

    # 시험 단위 통계 (Result 기반, enrollment 중복 방어)
    exam_rows: List[Dict[str, Any]] = []

    for ex in exams:
        exid = _safe_int(getattr(ex, "id", 0))
        if not exid:
            continue

        results = list(latest_results_per_enrollment(
            target_type="exam",
            target_id=exid,
        ).filter(
            enrollment__tenant=getattr(session.lecture, "tenant", None),
            enrollment__lecture_id=getattr(session, "lecture_id", None),
        ).select_related("attempt"))
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
            lecture_id=getattr(session, "lecture_id", None),
        )
        # pass_score=0 → 합격 기준 미설정 → 합/불 집계 제외
        if pass_score > 0:
            pcount = sum(score >= pass_score for score in scores)
            fcount = sum(score < pass_score for score in scores)
        else:
            pcount = 0
            fcount = 0

        p_total = len(results)
        scored_total = pcount + fcount
        p_rate = (pcount / scored_total) if scored_total else 0.0

        exam_rows.append(
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

    return {
        "session_id": int(session.id),
        "participant_count": int(participant_count),
        "pass_rate": round(float(pass_rate), 4),
        "clinic_rate": round(float(clinic_rate), 4),
        "strategy": str(strategy),
        "pass_source": str(pass_source),
        "exams": exam_rows,
        "generated_at": timezone.now().isoformat(),
    }


def build_session_scores_matrix_snapshot(*, session_id: int) -> Dict[str, Any]:
    """
    ✅ Session 성적 탭용 "행렬 스냅샷"

    주의:
    - 이 함수는 SessionScoresView의 '집계 로직'을 재사용하고 싶을 때 쓰는 목적.
    - results 도메인에서 "원본 데이터/정책"을 만들지 않는다.
    - 여기서는 View를 import해서 호출하지 않고, 필요한 최소 조합만 제공한다.

    반환(고정):
    {
      "session_id": int,
      "exam_ids": [...],
      "participant_count": int,
      "generated_at": "iso"
    }

    (실제 테이블 rows는 SessionScoresView가 이미 제공하므로 여기서는 메타만 제공)
    """
    session = session_by_id(_safe_int(session_id))
    if not session:
        return {
            "session_id": _safe_int(session_id),
            "exam_ids": [],
            "participant_count": 0,
            "generated_at": timezone.now().isoformat(),
        }

    exams = list(get_exams_for_session(session))
    exam_ids = [int(getattr(e, "id", 0) or 0) for e in exams if int(getattr(e, "id", 0) or 0)]

    participant_count = session_progress_queryset_for_session(session).count()

    return {
        "session_id": int(session.id),
        "exam_ids": exam_ids,
        "participant_count": int(participant_count),
        "generated_at": timezone.now().isoformat(),
    }
