# apps/domains/results/utils/ranking.py
"""
석차(등수) 계산 유틸리티.

설계 원칙:
- "석차=1차 점수" 정책 (score-clinic philosophy).
  · 1차 attempt의 `meta.initial_snapshot.total_score`를 최우선 소스로 사용.
  · `sync_result_from_exam_submission`이 attempt_index=1 생성 시 해당 snapshot을
    저장하므로, 이후 ONLINE 재응시로 Result.total_score가 덮어쓰여져도 1차 점수는
    불변으로 유지된다.
  · initial_snapshot이 없는 legacy 데이터는 Result.total_score로 fallback
    (attempt_index=1 도입 이전 데이터 호환).
- competition_rank: 동점이면 같은 등수, 다음 등수는 동점자 수만큼 건너뜀.
  예: 19, 14, 14, 12점은 1, 2, 2, 4등.
- query-time 계산: Result 모델에 필드 추가 없음.
- tenant isolation: 모든 공개 계산 함수가 tenant를 필수로 받아 enrollment 관계까지 제한.
- NOT_SUBMITTED(미응시) 학생은 석차 계산에서 제외.
"""
from __future__ import annotations

from math import isfinite
from typing import Any, Dict, Iterable, List, Optional

from apps.domains.results.utils.result_queries import latest_results_per_enrollment
from apps.domains.results.models import Result, ExamAttempt
from apps.domains.results.services.omr_subjective_completion import (
    pending_omr_result_ids,
)
from apps.domains.results.utils.initial_exam_score import load_initial_exam_scores
from django.db.models import Max, Subquery


def _safe_nonnegative_score(value) -> float | None:
    """Return a JSON-safe score, preserving valid zero and bonus scores."""
    try:
        score = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return score if isfinite(score) and score >= 0 else None


def _not_submitted_attempt_pairs(attempt_ids: list) -> dict[int, tuple[int, int]]:
    """Return exact ``attempt_id -> (exam_id, enrollment_id)`` absent pairs."""
    if not attempt_ids:
        return {}
    return {
        int(row["id"]): (int(row["exam_id"]), int(row["enrollment_id"]))
        for row in ExamAttempt.objects.filter(
            id__in=attempt_ids,
            meta__status="NOT_SUBMITTED",
        ).values("id", "exam_id", "enrollment_id")
    }


def _first_attempt_state(
    *,
    exam_ids: Iterable[int],
    enrollment_ids: Optional[Iterable[int]] = None,
) -> tuple[Dict[int, Dict[int, float]], set[tuple[int, int]]]:
    """
    {exam_id: {enrollment_id: 1차 점수}} 매핑을 반환.

    `ExamAttempt(attempt_index=1).meta["initial_snapshot"]["total_score"]`를 소스로 사용.
    값이 없는 경우는 이 맵에 없음 → 호출부에서 Result.total_score로 fallback.
    NOT_SUBMITTED attempt는 맵에 포함하지 않아 자연스럽게 제외된다.
    """
    exam_ids_int = [int(x) for x in exam_ids]
    if not exam_ids_int:
        return {}, set()

    enrollment_ids_int = (
        [int(x) for x in enrollment_ids]
        if enrollment_ids is not None
        else list(
            ExamAttempt.objects.filter(
                exam_id__in=exam_ids_int,
                attempt_index=1,
            ).values_list("enrollment_id", flat=True)
        )
    )
    states = load_initial_exam_scores(
        exam_ids=exam_ids_int,
        enrollment_ids=enrollment_ids_int,
    )

    override: Dict[int, Dict[int, float]] = {eid: {} for eid in exam_ids_int}
    not_submitted_pairs: set[tuple[int, int]] = set()
    for (exam_id, enrollment_id), state in states.items():
        if state.not_submitted:
            not_submitted_pairs.add((exam_id, enrollment_id))
            continue
        if state.total_score is None:
            continue
        score = _safe_nonnegative_score(state.total_score)
        if score is not None:
            override[exam_id][enrollment_id] = score
    return override, not_submitted_pairs


def _competition_rank(
    rows: List[dict],
) -> Dict[int, dict]:
    """JSON-safe nonnegative scores only, then deterministic competition rank."""
    normalized_rows = []
    for row in rows:
        score = _safe_nonnegative_score(row.get("total_score"))
        if score is None:
            continue
        normalized_rows.append({
            "enrollment_id": int(row["enrollment_id"]),
            "total_score": score,
        })
    normalized_rows.sort(
        key=lambda row: (-row["total_score"], row["enrollment_id"]),
    )
    if not normalized_rows:
        return {}

    cohort_size = len(normalized_rows)
    total = sum(row["total_score"] for row in normalized_rows)
    cohort_avg = round(total / cohort_size, 2) if cohort_size else 0.0

    result_map: Dict[int, dict] = {}
    prev_score: Optional[float] = None
    current_rank = 0
    for position, r in enumerate(normalized_rows, start=1):
        score = r["total_score"]
        eid = int(r["enrollment_id"])
        if prev_score is None or score != prev_score:
            current_rank = position
            prev_score = score
        percentile = 100.0 if cohort_size <= 1 else round((current_rank / cohort_size) * 100, 1)
        result_map[eid] = {
            "rank": current_rank,
            "ranking_score": score,
            "percentile": percentile,
            "cohort_size": cohort_size,
            "cohort_avg": cohort_avg,
        }
    return result_map


def compute_exam_rankings(
    *,
    exam_id: int,
    tenant: Any,
    lecture_ids: Optional[Iterable[int]] = None,
) -> Dict[int, dict]:
    """
    시험별 enrollment_id → {rank, percentile, cohort_size, cohort_avg} 맵 반환.

    - rank: competition rank (동점 = 같은 등수, 다음 등수는 동점 인원 반영)
    - percentile: 상위 % (1등/20명 = 5%, 낮을수록 좋음)
    - cohort_size: 총 응시자 수 (미응시 제외)
    - cohort_avg: 평균 점수 (1차 점수 기준)

    NOT_SUBMITTED(미응시) 학생은 석차 계산에서 제외된다.
    """
    exam_id = int(exam_id)
    rs = latest_results_per_enrollment(target_type="exam", target_id=exam_id)
    base_qs = (
        rs.exclude(enrollment_id__isnull=True)
        .filter(
            enrollment__tenant=tenant,
            enrollment__lecture__tenant=tenant,
            enrollment__lecture__is_system=False,
        )
    )
    if lecture_ids is not None:
        base_qs = base_qs.filter(
            enrollment__lecture_id__in=[int(value) for value in lecture_ids]
        )
    pending_result_ids = pending_omr_result_ids(
        base_qs.select_related("attempt")
    )
    if pending_result_ids:
        base_qs = base_qs.exclude(id__in=pending_result_ids)

    # NOT_SUBMITTED attempt 제외
    attempt_ids = list(
        base_qs.exclude(attempt_id__isnull=True)
        .values_list("attempt_id", flat=True)
    )
    not_submitted_attempts = _not_submitted_attempt_pairs(attempt_ids)

    raw_rows = list(
        base_qs.values("enrollment_id", "total_score", "attempt_id")
    )

    # 1차 점수 override: attempt_index=1.meta.initial_snapshot 우선
    cohort_enrollment_ids = list(
        base_qs.values_list("enrollment_id", flat=True).distinct()
    )
    override_map, first_not_submitted_pairs = _first_attempt_state(
        exam_ids=[exam_id],
        enrollment_ids=cohort_enrollment_ids,
    )
    override = override_map.get(exam_id, {})
    rows = []
    for r in raw_rows:
        eid = int(r["enrollment_id"])
        if (exam_id, eid) in first_not_submitted_pairs:
            continue
        attempt_id = int(r["attempt_id"]) if r.get("attempt_id") else None
        if attempt_id and not_submitted_attempts.get(attempt_id) == (exam_id, eid):
            continue
        score = override.get(eid)
        if score is None:
            score = _safe_nonnegative_score(r.get("total_score"))
        if score is None:
            continue
        rows.append({"enrollment_id": eid, "total_score": score})

    return _competition_rank(rows)


def compute_exam_rankings_batch(
    *,
    exam_ids: List[int],
    tenant: Any,
    enrollment_ids: Optional[List[int]] = None,
) -> Dict[int, Dict[int, dict]]:
    """
    여러 시험의 석차를 한 번에 계산. N+1 쿼리 방지용.

    반환: { exam_id: { enrollment_id: {rank, percentile, cohort_size, cohort_avg} } }

    enrollment_ids가 주어지면 해당 enrollment의 석차만 반환 (코호트 계산은 전체 대상).
    코호트와 1차 점수 override는 필수 tenant의 enrollment로 제한한다.
    NOT_SUBMITTED(미응시) 학생은 석차 계산에서 제외된다.
    1차 점수(attempt_index=1 initial_snapshot) 우선, fallback Result.
    """
    if not exam_ids:
        return {}

    exam_ids_int = [int(eid) for eid in exam_ids]
    target_enrollment_set: Optional[set[int]] = (
        set(int(x) for x in enrollment_ids) if enrollment_ids is not None else None
    )

    # 모든 시험의 latest Result를 한 번에 조회 (코호트 = 응시자 전체)
    base = Result.objects.filter(
        target_type="exam",
        target_id__in=exam_ids_int,
    ).exclude(enrollment_id__isnull=True)
    base = base.filter(
        enrollment__tenant=tenant,
        enrollment__lecture__tenant=tenant,
        enrollment__lecture__is_system=False,
    )

    latest_ids = (
        base.values("target_id", "enrollment_id")
        .annotate(last_id=Max("id"))
        .values("last_id")
    )
    latest_results = Result.objects.filter(id__in=Subquery(latest_ids))
    pending_result_ids = pending_omr_result_ids(
        latest_results.select_related("attempt")
    )
    if pending_result_ids:
        latest_results = latest_results.exclude(id__in=pending_result_ids)

    # NOT_SUBMITTED attempt 제외
    attempt_ids = list(
        latest_results.exclude(attempt_id__isnull=True)
        .values_list("attempt_id", flat=True)
    )
    not_submitted_attempts = _not_submitted_attempt_pairs(attempt_ids)
    raw_rows = list(
        latest_results.values(
            "target_id",
            "enrollment_id",
            "total_score",
            "attempt_id",
        )
    )

    # 1차 점수 override (exam_id × enrollment_id)
    cohort_enrollment_ids = list(
        base.values_list("enrollment_id", flat=True).distinct()
    )
    override_map, first_not_submitted_pairs = _first_attempt_state(
        exam_ids=exam_ids_int,
        enrollment_ids=cohort_enrollment_ids,
    )

    # exam_id별 그룹핑 + score override
    by_exam: Dict[int, list] = {}
    for r in raw_rows:
        eid_exam = int(r["target_id"])
        eid_enroll = int(r["enrollment_id"])
        if (eid_exam, eid_enroll) in first_not_submitted_pairs:
            continue
        attempt_id = int(r["attempt_id"]) if r.get("attempt_id") else None
        if attempt_id and not_submitted_attempts.get(attempt_id) == (eid_exam, eid_enroll):
            continue
        score = override_map.get(eid_exam, {}).get(eid_enroll)
        if score is None:
            score = _safe_nonnegative_score(r.get("total_score"))
        if score is None:
            continue
        by_exam.setdefault(eid_exam, []).append({
            "enrollment_id": eid_enroll,
            "total_score": score,
        })

    result: Dict[int, Dict[int, dict]] = {}
    for exam_id_key in exam_ids_int:
        exam_rows = by_exam.get(exam_id_key, [])
        rank_map = _competition_rank(exam_rows)
        if target_enrollment_set is None:
            result[exam_id_key] = rank_map
        else:
            result[exam_id_key] = {
                eid: info for eid, info in rank_map.items()
                if eid in target_enrollment_set
            }

    return result
