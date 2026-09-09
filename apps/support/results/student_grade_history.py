"""Canonical tenant-scoped exam history for staff and student grade views."""

from __future__ import annotations

from math import isfinite
from statistics import fmean
from typing import Any

from django.db.models import Max
from django.db.models.functions import Coalesce

from apps.domains.results.models import ExamAttempt, Result
from apps.domains.results.utils.exam_achievement import compute_exam_achievement_bulk
from apps.domains.results.utils.initial_exam_score import (
    load_initial_exam_scores,
    project_initial_exam_score,
)
from apps.support.results.admin_student_grades_dependencies import (
    enrollment_lecture_metadata_by_id,
    exam_metadata_by_id,
    explicit_exam_target_scope,
    primary_session_metadata_by_exam_and_lecture,
)


def empty_exam_summary() -> dict[str, int | float | None]:
    return {
        "scored_count": 0,
        "average_score_pct": None,
        "latest_score_pct": None,
        "change_pct_points": None,
        "best_score_pct": None,
    }


def is_json_safe_number(value: Any) -> bool:
    if value is None:
        return True
    try:
        return isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _trend_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    recorded_at = str(row.get("recorded_at") or "")
    session_date = str(row.get("session_date") or "")
    return (
        session_date or recorded_at[:10] or "9999-12-31",
        int(row.get("session_regular_order") or row.get("session_order") or 10**9),
        recorded_at,
        int(row.get("lecture_id") or 0),
        int(row.get("exam_id") or 0),
    )


def build_exam_progression(exams: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Normalize valid scores and assign stable 1-based chronological rounds."""
    scored: list[dict[str, Any]] = []
    for exam in exams:
        score = exam.get("total_score")
        max_score = exam.get("max_score")
        if score is None or max_score is None:
            continue
        try:
            score_value = float(score)
            max_score_value = float(max_score)
        except (TypeError, ValueError, OverflowError):
            continue
        if (
            not isfinite(score_value)
            or not isfinite(max_score_value)
            or score_value < 0
            or max_score_value <= 0
        ):
            continue
        score_pct = (score_value / max_score_value) * 100
        if not isfinite(score_pct):
            continue
        scored.append({
            "exam_id": exam["exam_id"],
            "enrollment_id": exam["enrollment_id"],
            "title": exam["title"],
            "score": score_value,
            "max_score": max_score_value,
            # Bonus-point exams may legitimately exceed 100 percent.
            "score_pct": round(score_pct, 1),
            "recorded_at": exam.get("recorded_at"),
            "session_id": exam.get("session_id"),
            "session_title": exam.get("session_title"),
            "session_order": exam.get("session_order"),
            "session_regular_order": exam.get("session_regular_order"),
            "session_type": exam.get("session_type"),
            "session_date": exam.get("session_date"),
            "lecture_id": exam.get("lecture_id"),
            "lecture_title": exam.get("lecture_title"),
            "lecture_color": exam.get("lecture_color"),
            "lecture_chip_label": exam.get("lecture_chip_label"),
            "retake_count": exam.get("retake_count", 1),
            "archived": bool(exam.get("archived")),
            "rank": exam.get("rank"),
            "percentile": exam.get("percentile"),
            "cohort_size": exam.get("cohort_size"),
            "cohort_avg": exam.get("cohort_avg"),
        })

    scored.sort(key=_trend_sort_key)
    trend = [{**row, "round_index": index} for index, row in enumerate(scored, start=1)]
    if not trend:
        return [], empty_exam_summary()

    percentages = [float(row["score_pct"]) for row in trend]
    latest = percentages[-1]
    previous = percentages[-2] if len(percentages) > 1 else None
    return trend, {
        "scored_count": len(percentages),
        "average_score_pct": round(fmean(percentages), 1),
        "latest_score_pct": latest,
        "change_pct_points": round(latest - previous, 1) if previous is not None else None,
        "best_score_pct": max(percentages),
    }


def build_student_exam_history(
    *,
    tenant: Any,
    enrollment_ids: list[int],
    published_results_only: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Build deduplicated exam rows and progression inside an authorized scope.

    Callers own the enrollment visibility policy. Staff can pass all of a
    student's tenant enrollments; the student app passes active enrollments.
    """
    if not enrollment_ids:
        return [], [], empty_exam_summary()

    results = list(
        Result.objects.filter(
            enrollment_id__in=enrollment_ids,
            target_type="exam",
        )
        .annotate(recorded_at=Coalesce("submitted_at", "created_at"))
        .order_by("-recorded_at", "-id")
        .values(
            "id",
            "target_id",
            "enrollment_id",
            "total_score",
            "max_score",
            "submitted_at",
            "recorded_at",
            "attempt_id",
        )
    )
    result_exam_ids = {int(row["target_id"]) for row in results}
    explicit_exam_ids, explicit_target_pairs = explicit_exam_target_scope(
        tenant=tenant,
        exam_ids=result_exam_ids,
        enrollment_ids={int(enrollment_id) for enrollment_id in enrollment_ids},
    )
    results = [
        row
        for row in results
        if int(row["target_id"]) not in explicit_exam_ids
        or (int(row["enrollment_id"]), int(row["target_id"])) in explicit_target_pairs
    ]
    exam_ids = list({row["target_id"] for row in results})
    initial_scores = load_initial_exam_scores(
        exam_ids=exam_ids,
        enrollment_ids=enrollment_ids,
    )
    exams_map = exam_metadata_by_id(tenant=tenant, exam_ids=exam_ids) if exam_ids else {}

    retake_counts: dict[tuple[int, int], int] = {}
    if exam_ids:
        for attempt in (
            ExamAttempt.objects.filter(
                exam_id__in=exam_ids,
                enrollment_id__in=enrollment_ids,
                exam__tenant=tenant,
                enrollment__tenant=tenant,
            )
            .values("exam_id", "enrollment_id")
            .annotate(max_attempt=Max("attempt_index"))
        ):
            retake_counts[(attempt["enrollment_id"], attempt["exam_id"])] = attempt["max_attempt"]

    enrollment_lecture_map = enrollment_lecture_metadata_by_id(
        tenant=tenant,
        enrollment_ids=enrollment_ids,
    )
    exam_lecture_pairs = {
        (int(row["target_id"]), int(enrollment_lecture_map[row["enrollment_id"]]["lecture_id"]))
        for row in results
        if row["target_id"] in exams_map
        and row["enrollment_id"] in enrollment_lecture_map
        and enrollment_lecture_map[row["enrollment_id"]].get("lecture_id") is not None
    }
    primary_session_map = primary_session_metadata_by_exam_and_lecture(
        tenant=tenant,
        exam_lecture_pairs=exam_lecture_pairs,
    )

    canonical_rows: list[dict[str, Any]] = []
    seen_exam_ids: set[int] = set()
    for result in results:
        exam_id = result["target_id"]
        if exam_id in seen_exam_ids:
            continue
        info = exams_map.get(exam_id)
        if not info:
            continue
        if published_results_only and not info["student_results_published"]:
            continue
        enrollment_info = enrollment_lecture_map.get(result["enrollment_id"])
        if not enrollment_info or enrollment_info.get("lecture_is_system"):
            continue
        enrollment_lecture_id = enrollment_info.get("lecture_id")
        session_meta = (
            primary_session_map.get((int(exam_id), int(enrollment_lecture_id)))
            if enrollment_lecture_id is not None
            else None
        ) or {}
        if session_meta.get("lecture_is_system"):
            continue
        initial_score = project_initial_exam_score(
            state=initial_scores.get((int(exam_id), int(result["enrollment_id"]))),
            fallback_score=result.get("total_score"),
            fallback_max_score=result.get("max_score"),
        )
        if (
            initial_score.total_score is None
            and not initial_score.not_submitted
        ) or not is_json_safe_number(initial_score.max_score):
            continue

        lecture_id = session_meta.get("lecture_id") or enrollment_info["lecture_id"]
        lecture_title = session_meta.get("lecture_title") or enrollment_info["lecture_title"]
        lecture_color = session_meta.get("lecture_color") or enrollment_info["lecture_color"]
        lecture_chip_label = session_meta.get("lecture_chip_label") or enrollment_info["lecture_chip_label"]
        seen_exam_ids.add(exam_id)
        canonical_rows.append({
            "result": result,
            "initial_score": initial_score,
            "exam_id": exam_id,
            "info": info,
            "session_id": session_meta.get("session_id"),
            "session_title": session_meta.get("session_title"),
            "session_order": session_meta.get("session_order"),
            "session_regular_order": session_meta.get("session_regular_order"),
            "session_type": session_meta.get("session_type"),
            "session_date": session_meta.get("session_date"),
            "lecture_id": lecture_id,
            "lecture_title": lecture_title,
            "lecture_color": lecture_color,
            "lecture_chip_label": lecture_chip_label,
        })

    achievements = compute_exam_achievement_bulk(
        items=[
            {
                "enrollment_id": row["result"]["enrollment_id"],
                "exam_id": row["exam_id"],
                "total_score": row["initial_score"].total_score,
                "pass_score": row["info"]["pass_score_by_lecture"].get(
                    int(row["lecture_id"]),
                    row["info"]["pass_score"],
                ),
                "attempt_id": row["result"].get("attempt_id"),
                "session": None,
            }
            for row in canonical_rows
        ],
        use_session_filter=False,
        tenant=tenant,
    )

    exam_list: list[dict[str, Any]] = []
    for row in canonical_rows:
        result = row["result"]
        exam_id = row["exam_id"]
        enrollment_id = result["enrollment_id"]
        achievement = achievements.get((int(enrollment_id), int(exam_id)), {})
        is_not_submitted = achievement.get("meta_status") == "NOT_SUBMITTED"
        session_date = row["session_date"]
        exam_list.append({
            "_result_id": result["id"],
            "_structure_exam_id": row["info"]["effective_structure_exam_id"],
            "_current_max_score": row["info"]["max_score"],
            "exam_id": exam_id,
            "enrollment_id": enrollment_id,
            "title": row["info"]["title"],
            "total_score": None if is_not_submitted else row["initial_score"].total_score,
            "max_score": row["initial_score"].max_score,
            "is_pass": achievement.get("is_pass"),
            "achievement": achievement.get("achievement"),
            "meta_status": achievement.get("meta_status"),
            "retake_count": retake_counts.get((enrollment_id, exam_id), 1),
            "session_id": row["session_id"],
            "session_title": row["session_title"],
            "session_order": row["session_order"],
            "session_regular_order": row["session_regular_order"],
            "session_type": row["session_type"],
            "session_date": session_date.isoformat() if session_date else None,
            "lecture_id": row["lecture_id"],
            "lecture_title": row["lecture_title"],
            "lecture_color": row["lecture_color"],
            "lecture_chip_label": row["lecture_chip_label"],
            "submitted_at": result["submitted_at"].isoformat() if result.get("submitted_at") else None,
            "recorded_at": result["recorded_at"].isoformat(),
            "archived": not row["info"]["is_active"],
        })

    trend, summary = build_exam_progression(exam_list)
    return exam_list, trend, summary
