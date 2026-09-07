from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from statistics import median, pstdev
from typing import Any, Iterable

from django.db.models import Q
from django.utils import timezone

from apps.domains.exams.models import Exam, ExamLecturePolicy
from apps.domains.results.models import Result, ResultFact, ResultItem
from apps.domains.results.services.omr_subjective_completion import (
    pending_omr_result_ids_for_ids,
)
from apps.domains.results.utils.initial_exam_score import (
    load_initial_exam_scores,
    project_initial_exam_score,
)
from apps.domains.submissions.models import Submission
from apps.support.results.admin_student_grades_dependencies import explicit_exam_target_scope
from apps.support.student_app.results_summary import build_student_grades_summary


AUTO_GRADE_SOURCES = (
    Submission.Source.OMR_SCAN,
    Submission.Source.ONLINE,
    Submission.Source.AI_MATCH,
)
MANUAL_FACT_SOURCES = (
    "manual",
    "manual_total",
    "manual_objective",
    "manual_subjective",
    "manual_not_submitted",
)
_SYNTHETIC_EXAM_PREFIX_RE = re.compile(
    r"^(?:\[?e2e(?:[-_\]\s]|$)|\[?local-demo(?:[-_\]\s]|$)|\[?demo(?:[-_\]\s]|$))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _ExamInfo:
    id: int
    title: str
    pass_score: float
    max_score: float


def _now_iso() -> str:
    return timezone.localtime(timezone.now()).isoformat()


def _bounded_days(raw: Any, *, default: int = 180) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(30, min(value, 730))


def normalize_analytics_days(raw: Any, *, default: int = 180) -> int:
    return _bounded_days(raw, default=default)


def _round(value: float | None, digits: int = 1) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(float(value), digits)


def _score_pct(score: Any, max_score: Any) -> float | None:
    try:
        score_value = float(score)
        max_value = float(max_score)
    except (TypeError, ValueError):
        return None
    if max_value <= 0:
        return None
    return (score_value / max_value) * 100


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * pct
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def _stats(values: Iterable[float]) -> dict[str, float | int | None]:
    rows = [float(value) for value in values if math.isfinite(float(value))]
    if not rows:
        return {
            "count": 0,
            "avg": None,
            "median": None,
            "p10": None,
            "p25": None,
            "p75": None,
            "p90": None,
            "std": None,
        }
    return {
        "count": len(rows),
        "avg": _round(sum(rows) / len(rows)),
        "median": _round(float(median(rows))),
        "p10": _round(_percentile(rows, 0.10)),
        "p25": _round(_percentile(rows, 0.25)),
        "p75": _round(_percentile(rows, 0.75)),
        "p90": _round(_percentile(rows, 0.90)),
        "std": _round(float(pstdev(rows))) if len(rows) > 1 else 0.0,
    }


def _month_key(dt: Any) -> str | None:
    if not dt:
        return None
    if timezone.is_aware(dt):
        dt = timezone.localtime(dt)
    return f"{dt.year:04d}-{dt.month:02d}"


def _month_keys_between(start_at: Any, end_at: Any) -> list[str]:
    start = timezone.localtime(start_at).date() if timezone.is_aware(start_at) else start_at.date()
    end = timezone.localtime(end_at).date() if timezone.is_aware(end_at) else end_at.date()
    cursor = date(start.year, start.month, 1)
    last = date(end.year, end.month, 1)
    keys: list[str] = []
    while cursor <= last:
        keys.append(f"{cursor.year:04d}-{cursor.month:02d}")
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)
    return keys


def _is_test_exam_title(title: str | None) -> bool:
    value = (title or "").strip()
    # Synthetic fixtures are required to carry a structured prefix.  Ordinary
    # academy titles such as "주간 테스트" or "Level Test" are real results.
    return bool(_SYNTHETIC_EXAM_PREFIX_RE.search(value))


def _is_not_submitted(row: dict[str, Any]) -> bool:
    if row.get("_initial_not_submitted") is True:
        return True
    meta = row.get("attempt__meta")
    return isinstance(meta, dict) and meta.get("status") == "NOT_SUBMITTED"


def _activity_at(row: dict[str, Any]) -> Any:
    return row.get("submitted_at") or row.get("updated_at") or row.get("created_at")


def _as_iso(dt: Any) -> str | None:
    if not dt:
        return None
    if timezone.is_aware(dt):
        return timezone.localtime(dt).isoformat()
    return dt.isoformat()


def _submission_needs_review(row: dict[str, Any]) -> bool:
    if row.get("status") == Submission.Status.NEEDS_IDENTIFICATION:
        return True
    meta = row.get("meta") or {}
    if not isinstance(meta, dict):
        return False
    manual_review = meta.get("manual_review") or {}
    return isinstance(manual_review, dict) and bool(manual_review.get("required"))


def _exam_info_maps(tenant: Any) -> tuple[dict[int, _ExamInfo], set[int], set[int]]:
    all_exam_infos: dict[int, _ExamInfo] = {}
    test_exam_ids: set[int] = set()
    clean_exam_ids: set[int] = set()
    exams = (
        Exam.objects
        .filter(tenant=tenant, exam_type=Exam.ExamType.REGULAR)
        .only("id", "title", "pass_score", "max_score")
    )
    for exam in exams:
        info = _ExamInfo(
            id=int(exam.id),
            title=exam.title,
            pass_score=float(exam.pass_score or 0),
            max_score=float(exam.max_score or 0),
        )
        all_exam_infos[info.id] = info
        if _is_test_exam_title(info.title):
            test_exam_ids.add(info.id)
        else:
            clean_exam_ids.add(info.id)
    return all_exam_infos, clean_exam_ids, test_exam_ids


def _build_top_exams(rows: list[dict[str, Any]], exam_infos: dict[int, _ExamInfo]) -> list[dict[str, Any]]:
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[int(row["target_id"])].append(row)

    output = []
    for exam_id, exam_rows in grouped.items():
        info = exam_infos.get(exam_id)
        score_pcts = []
        pass_denominator = 0
        pass_count = 0
        absent_count = 0
        for row in exam_rows:
            if _is_not_submitted(row):
                absent_count += 1
                continue
            pct = _score_pct(row.get("total_score"), row.get("max_score"))
            if pct is None:
                continue
            score_pcts.append(pct)
            pass_score = float(row.get("_pass_score") or 0)
            if info and pass_score > 0:
                pass_denominator += 1
                if float(row.get("total_score") or 0) >= pass_score:
                    pass_count += 1
        stat = _stats(score_pcts)
        output.append({
            "exam_id": exam_id,
            "title": info.title if info else f"시험 #{exam_id}",
            "result_count": len(exam_rows),
            "scored_count": stat["count"],
            "absent_count": absent_count,
            "avg_score_pct": stat["avg"],
            "median_score_pct": stat["median"],
            "p10_score_pct": stat["p10"],
            "p90_score_pct": stat["p90"],
            "std_score_pct": stat["std"],
            "pass_rate_pct": _round((pass_count / pass_denominator) * 100) if pass_denominator else None,
        })
    return sorted(output, key=lambda item: (item["scored_count"], item["avg_score_pct"] or -1), reverse=True)[:10]


def _build_weak_questions(result_ids: list[int]) -> list[dict[str, Any]]:
    if not result_ids:
        return []
    grouped: dict[tuple[int, int], dict[str, Any]] = {}
    items = (
        ResultItem.objects
        .filter(result_id__in=result_ids)
        .select_related("result", "question")
        .only(
            "is_correct",
            "score",
            "max_score",
            "result__target_id",
            "question__number",
        )
    )
    for item in items:
        exam_id = int(item.result.target_id)
        question_number = int(item.question.number)
        key = (exam_id, question_number)
        row = grouped.setdefault(
            key,
            {
                "exam_id": exam_id,
                "question_number": question_number,
                "attempts": 0,
                "correct_count": 0,
                "wrong_count": 0,
                "score_rates": [],
            },
        )
        row["attempts"] += 1
        if item.is_correct:
            row["correct_count"] += 1
        else:
            row["wrong_count"] += 1
        pct = _score_pct(item.score, item.max_score)
        if pct is not None:
            row["score_rates"].append(pct)

    output = []
    for row in grouped.values():
        attempts = int(row["attempts"])
        if attempts <= 0:
            continue
        output.append({
            "exam_id": row["exam_id"],
            "question_number": row["question_number"],
            "attempts": attempts,
            "accuracy_pct": _round((row["correct_count"] / attempts) * 100),
            "wrong_count": row["wrong_count"],
            "avg_score_pct": _stats(row["score_rates"])["avg"],
        })
    return sorted(output, key=lambda item: (item["accuracy_pct"] or 101, -item["wrong_count"]))[:10]


def build_teacher_enterprise_analytics(*, tenant: Any, days: int = 180) -> dict[str, Any]:
    days = _bounded_days(days)
    now = timezone.now()
    start_at = now - timedelta(days=days)
    all_exam_infos, clean_exam_ids, test_exam_ids = _exam_info_maps(tenant)
    all_exam_ids = set(all_exam_infos)

    if not all_exam_ids:
        return {
            "tenant": {"id": tenant.id, "name": tenant.name, "code": tenant.code},
            "date_range": {"days": days, "from": _as_iso(start_at), "to": _as_iso(now)},
            "summary": {
                "exam_result_count": 0,
                "scored_count": 0,
                "avg_score_pct": None,
                "median_score_pct": None,
                "pass_rate_pct": None,
                "absent_count": 0,
                "generated_at": _now_iso(),
            },
            "usage": {
                "manual_score_events": 0,
                "manual_active_days": 0,
                "auto_grade_submissions": 0,
                "auto_grade_done": 0,
                "auto_grade_failed": 0,
                "auto_grade_review": 0,
                "auto_completion_rate_pct": None,
                "latest_activity_at": None,
                "activity_level": "none",
                "source_breakdown": {},
            },
            "trends": [{"period": key, "scored_count": 0, "manual_score_events": 0, "auto_grade_submissions": 0} for key in _month_keys_between(start_at, now)],
            "top_exams": [],
            "weak_questions": [],
            "data_quality": {
                "tenant_exam_count": 0,
                "clean_exam_count": 0,
                "filtered_test_exam_count": 0,
                "total_exam_results": 0,
                "clean_exam_results": 0,
                "filtered_test_exam_results": 0,
                "no_enrollment_exam_results": 0,
                "foreign_enrollment_exam_results": 0,
            },
        }

    result_rows = list(
        Result.objects
        .filter(
            target_type="exam",
            target_id__in=clean_exam_ids,
            enrollment__tenant=tenant,
            enrollment__student__deleted_at__isnull=True,
        )
        .filter(Q(submitted_at__gte=start_at) | Q(submitted_at__isnull=True, updated_at__gte=start_at))
        .select_related("attempt")
        .values(
            "id",
            "target_id",
            "enrollment_id",
            "enrollment__lecture_id",
            "total_score",
            "max_score",
            "submitted_at",
            "created_at",
            "updated_at",
            "attempt__meta",
        )
    )
    explicit_exam_ids, explicit_target_pairs = explicit_exam_target_scope(
        tenant=tenant,
        exam_ids=set(clean_exam_ids),
        enrollment_ids={int(row["enrollment_id"]) for row in result_rows},
    )
    result_rows = [
        row
        for row in result_rows
        if int(row["target_id"]) not in explicit_exam_ids
        or (int(row["enrollment_id"]), int(row["target_id"])) in explicit_target_pairs
    ]
    pending_result_ids = pending_omr_result_ids_for_ids(
        row["id"] for row in result_rows
    )
    result_rows = [
        row for row in result_rows if int(row["id"]) not in pending_result_ids
    ]
    lecture_pass_scores = {
        (int(row["exam_id"]), int(row["lecture_id"])): float(row["pass_score"])
        for row in ExamLecturePolicy.objects.filter(
            exam_id__in=clean_exam_ids,
            exam__tenant=tenant,
            lecture__tenant=tenant,
        ).values("exam_id", "lecture_id", "pass_score")
    }
    initial_scores = load_initial_exam_scores(
        exam_ids=clean_exam_ids,
        enrollment_ids=[row["enrollment_id"] for row in result_rows],
    )
    for row in result_rows:
        fallback_not_submitted = _is_not_submitted(row)
        initial_score = project_initial_exam_score(
            state=initial_scores.get((int(row["target_id"]), int(row["enrollment_id"]))),
            fallback_score=row.get("total_score"),
            fallback_max_score=row.get("max_score"),
            fallback_not_submitted=fallback_not_submitted,
        )
        row["total_score"] = initial_score.total_score
        row["max_score"] = initial_score.max_score
        row["_initial_not_submitted"] = initial_score.not_submitted
        info = all_exam_infos.get(int(row["target_id"]))
        row["_pass_score"] = lecture_pass_scores.get(
            (int(row["target_id"]), int(row["enrollment__lecture_id"])),
            info.pass_score if info else 0.0,
        )

    score_pcts = []
    pass_denominator = 0
    pass_count = 0
    absent_count = 0
    for row in result_rows:
        if _is_not_submitted(row):
            absent_count += 1
            continue
        pct = _score_pct(row.get("total_score"), row.get("max_score"))
        if pct is None:
            continue
        score_pcts.append(pct)
        pass_score = float(row.get("_pass_score") or 0)
        if pass_score > 0:
            pass_denominator += 1
            if float(row.get("total_score") or 0) >= pass_score:
                pass_count += 1

    manual_rows = list(
        ResultFact.objects
        .filter(
            target_type="exam",
            target_id__in=clean_exam_ids,
            enrollment__tenant=tenant,
            source__in=MANUAL_FACT_SOURCES,
            created_at__gte=start_at,
        )
        .values("target_id", "created_at")
    )
    auto_rows = list(
        Submission.objects
        .filter(
            tenant=tenant,
            target_type=Submission.TargetType.EXAM,
            target_id__in=clean_exam_ids,
            source__in=AUTO_GRADE_SOURCES,
            created_at__gte=start_at,
        )
        .values("target_id", "source", "status", "created_at", "updated_at", "meta")
    )

    latest_candidates = [_activity_at(row) for row in result_rows] + [row["created_at"] for row in manual_rows + auto_rows]
    latest_activity_at = max([dt for dt in latest_candidates if dt], default=None)
    manual_days = {timezone.localtime(row["created_at"]).date().isoformat() for row in manual_rows if row.get("created_at")}
    auto_done = sum(1 for row in auto_rows if row.get("status") == Submission.Status.DONE)
    auto_failed = sum(1 for row in auto_rows if row.get("status") == Submission.Status.FAILED)
    auto_review = sum(1 for row in auto_rows if _submission_needs_review(row))
    source_breakdown = Counter(row.get("source") or "unknown" for row in auto_rows)
    activity_score = len(manual_rows) + len(auto_rows)
    active_days = len(manual_days | {
        timezone.localtime(row["created_at"]).date().isoformat() for row in auto_rows if row.get("created_at")
    })
    if activity_score >= 100 or active_days >= 8:
        activity_level = "high"
    elif activity_score >= 20 or active_days >= 3:
        activity_level = "regular"
    elif activity_score > 0:
        activity_level = "light"
    else:
        activity_level = "none"

    month_map: dict[str, dict[str, Any]] = {
        key: {
            "period": key,
            "scored_count": 0,
            "avg_score_pct": None,
            "median_score_pct": None,
            "pass_rate_pct": None,
            "manual_score_events": 0,
            "auto_grade_submissions": 0,
            "auto_grade_done": 0,
            "auto_completion_rate_pct": None,
            "_scores": [],
            "_pass_denominator": 0,
            "_pass_count": 0,
        }
        for key in _month_keys_between(start_at, now)
    }
    for row in result_rows:
        if _is_not_submitted(row):
            continue
        key = _month_key(_activity_at(row))
        if not key:
            continue
        bucket = month_map.setdefault(key, {"period": key, "_scores": [], "_pass_denominator": 0, "_pass_count": 0})
        pct = _score_pct(row.get("total_score"), row.get("max_score"))
        if pct is None:
            continue
        bucket["_scores"].append(pct)
        pass_score = float(row.get("_pass_score") or 0)
        if pass_score > 0:
            bucket["_pass_denominator"] += 1
            if float(row.get("total_score") or 0) >= pass_score:
                bucket["_pass_count"] += 1
    for row in manual_rows:
        key = _month_key(row.get("created_at"))
        if key:
            month_map.setdefault(key, {"period": key, "_scores": [], "_pass_denominator": 0, "_pass_count": 0})
            month_map[key]["manual_score_events"] = month_map[key].get("manual_score_events", 0) + 1
    for row in auto_rows:
        key = _month_key(row.get("created_at"))
        if key:
            month_map.setdefault(key, {"period": key, "_scores": [], "_pass_denominator": 0, "_pass_count": 0})
            month_map[key]["auto_grade_submissions"] = month_map[key].get("auto_grade_submissions", 0) + 1
            if row.get("status") == Submission.Status.DONE:
                month_map[key]["auto_grade_done"] = month_map[key].get("auto_grade_done", 0) + 1

    trends = []
    for key in sorted(month_map):
        bucket = month_map[key]
        scores = bucket.pop("_scores", [])
        pass_bucket_denominator = bucket.pop("_pass_denominator", 0)
        pass_bucket_count = bucket.pop("_pass_count", 0)
        stat = _stats(scores)
        auto_count = bucket.get("auto_grade_submissions", 0)
        auto_done_count = bucket.get("auto_grade_done", 0)
        bucket["scored_count"] = stat["count"]
        bucket["avg_score_pct"] = stat["avg"]
        bucket["median_score_pct"] = stat["median"]
        bucket["pass_rate_pct"] = _round((pass_bucket_count / pass_bucket_denominator) * 100) if pass_bucket_denominator else None
        bucket["auto_completion_rate_pct"] = _round((auto_done_count / auto_count) * 100) if auto_count else None
        trends.append(bucket)

    total_exam_results = Result.objects.filter(target_type="exam", target_id__in=all_exam_ids).count()
    clean_exam_results = Result.objects.filter(
        target_type="exam",
        target_id__in=clean_exam_ids,
        enrollment__tenant=tenant,
    ).count()
    no_enrollment_exam_results = Result.objects.filter(
        target_type="exam",
        target_id__in=clean_exam_ids,
        enrollment__isnull=True,
    ).count()
    foreign_enrollment_exam_results = Result.objects.filter(
        target_type="exam",
        target_id__in=clean_exam_ids,
        enrollment__isnull=False,
    ).exclude(enrollment__tenant=tenant).count()
    filtered_test_exam_results = Result.objects.filter(target_type="exam", target_id__in=test_exam_ids).count()
    summary_stats = _stats(score_pcts)

    return {
        "tenant": {"id": tenant.id, "name": tenant.name, "code": tenant.code},
        "date_range": {"days": days, "from": _as_iso(start_at), "to": _as_iso(now)},
        "summary": {
            "exam_result_count": len(result_rows),
            "scored_count": summary_stats["count"],
            "avg_score_pct": summary_stats["avg"],
            "median_score_pct": summary_stats["median"],
            "p10_score_pct": summary_stats["p10"],
            "p25_score_pct": summary_stats["p25"],
            "p75_score_pct": summary_stats["p75"],
            "p90_score_pct": summary_stats["p90"],
            "std_score_pct": summary_stats["std"],
            "pass_rate_pct": _round((pass_count / pass_denominator) * 100) if pass_denominator else None,
            "absent_count": absent_count,
            "generated_at": _now_iso(),
        },
        "usage": {
            "manual_score_events": len(manual_rows),
            "manual_active_days": len(manual_days),
            "auto_grade_submissions": len(auto_rows),
            "auto_grade_done": auto_done,
            "auto_grade_failed": auto_failed,
            "auto_grade_review": auto_review,
            "auto_completion_rate_pct": _round((auto_done / len(auto_rows)) * 100) if auto_rows else None,
            "latest_activity_at": _as_iso(latest_activity_at),
            "activity_level": activity_level,
            "source_breakdown": dict(source_breakdown),
        },
        "trends": trends,
        "top_exams": _build_top_exams(result_rows, all_exam_infos),
        "weak_questions": _build_weak_questions([int(row["id"]) for row in result_rows if row.get("id")]),
        "data_quality": {
            "tenant_exam_count": len(all_exam_ids),
            "clean_exam_count": len(clean_exam_ids),
            "filtered_test_exam_count": len(test_exam_ids),
            "total_exam_results": total_exam_results,
            "clean_exam_results": clean_exam_results,
            "filtered_test_exam_results": filtered_test_exam_results,
            "no_enrollment_exam_results": no_enrollment_exam_results,
            "foreign_enrollment_exam_results": foreign_enrollment_exam_results,
        },
    }


def _student_trend_key(exam: dict[str, Any]) -> str:
    return str(exam.get("submitted_at") or exam.get("recorded_at") or "")


def _student_row_in_window(row: dict[str, Any], *, start_at: datetime, now: datetime) -> bool:
    raw = row.get("submitted_at") or row.get("recorded_at")
    if not raw:
        return False
    try:
        parsed = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return False
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return start_at <= parsed <= now


def build_student_enterprise_analytics(*, tenant: Any, student: Any, days: int = 365) -> dict[str, Any]:
    days = _bounded_days(days, default=365)
    now = timezone.now()
    start_at = now - timedelta(days=days)
    summary = build_student_grades_summary(tenant=tenant, student=student)
    all_exams = summary.get("exams", [])
    non_test_exams = [
        exam for exam in all_exams
        if not _is_test_exam_title(exam.get("title"))
    ]
    exams = [
        exam for exam in non_test_exams
        if _student_row_in_window(exam, start_at=start_at, now=now)
    ]
    homeworks = [
        homework for homework in summary.get("homeworks", [])
        if _student_row_in_window(homework, start_at=start_at, now=now)
    ]

    scored_exams = []
    for exam in exams:
        if exam.get("meta_status") == "NOT_SUBMITTED" or exam.get("total_score") is None:
            continue
        pct = _score_pct(exam.get("total_score"), exam.get("max_score"))
        if pct is None:
            continue
        scored_exams.append({**exam, "score_pct": pct})

    score_pcts = [float(exam["score_pct"]) for exam in scored_exams]
    stats = _stats(score_pcts)
    pass_count = 0
    judged_count = 0
    for exam in exams:
        achievement = exam.get("achievement")
        if achievement in ("PASS", "REMEDIATED"):
            pass_count += 1
            judged_count += 1
        elif achievement == "FAIL":
            judged_count += 1
        elif exam.get("is_pass") is not None:
            judged_count += 1
            if exam.get("is_pass"):
                pass_count += 1

    trends = []
    for exam in sorted(scored_exams, key=_student_trend_key):
        cohort_avg_pct = _score_pct(exam.get("cohort_avg"), exam.get("max_score"))
        trends.append({
            "exam_id": exam.get("exam_id"),
            "title": exam.get("title"),
            "lecture_title": exam.get("lecture_title"),
            "submitted_at": exam.get("submitted_at"),
            "score_pct": _round(exam.get("score_pct")),
            "cohort_avg_pct": _round(cohort_avg_pct),
            "rank": exam.get("rank"),
            "percentile": exam.get("percentile"),
            "cohort_size": exam.get("cohort_size"),
        })

    lecture_map: dict[str, list[float]] = defaultdict(list)
    for exam in scored_exams:
        lecture_map[str(exam.get("lecture_title") or "기타")].append(float(exam["score_pct"]))
    lecture_breakdown = [
        {
            "lecture_title": lecture,
            "exam_count": len(values),
            "avg_score_pct": _stats(values)["avg"],
        }
        for lecture, values in lecture_map.items()
    ]
    lecture_breakdown.sort(key=lambda row: (row["avg_score_pct"] is None, row["avg_score_pct"] or 0))

    wrong_counter: Counter[int] = Counter()
    for exam in exams:
        for number in exam.get("wrong_question_numbers") or []:
            try:
                wrong_counter[int(number)] += 1
            except (TypeError, ValueError):
                continue
    weak_questions = [
        {"question_number": number, "wrong_count": count}
        for number, count in wrong_counter.most_common(8)
    ]

    graded_homeworks = [h for h in homeworks if h.get("score") is not None]
    homework_score_pcts = [
        pct for pct in (_score_pct(h.get("score"), h.get("max_score")) for h in graded_homeworks) if pct is not None
    ]
    homework_pass_count = sum(1 for h in graded_homeworks if h.get("achievement") in ("PASS", "REMEDIATED") or h.get("passed"))

    best_exam = max(scored_exams, key=lambda item: item["score_pct"], default=None)
    weakest_exam = min(scored_exams, key=lambda item: item["score_pct"], default=None)
    latest_exam = max(scored_exams, key=_student_trend_key, default=None)
    if stats["count"] == 0:
        risk_level = "insufficient"
    elif (stats["avg"] or 0) < 45 or (judged_count and (pass_count / judged_count) < 0.5):
        risk_level = "attention"
    elif (stats["avg"] or 0) < 70:
        risk_level = "watch"
    else:
        risk_level = "stable"

    insights = []
    if latest_exam:
        insights.append(f"최근 시험 득점률은 {_round(latest_exam['score_pct'])}%입니다.")
    if best_exam:
        insights.append(f"가장 강한 시험은 {best_exam.get('title')}입니다.")
    if weakest_exam and weakest_exam is not best_exam:
        insights.append(f"{weakest_exam.get('title')}은 보완 우선순위가 높습니다.")
    if weak_questions:
        insights.append(f"자주 틀린 문항은 {weak_questions[0]['question_number']}번입니다.")

    return {
        "student": {"id": student.id, "name": student.name},
        "date_range": {"days": days, "from": _as_iso(start_at), "to": _as_iso(now)},
        "summary": {
            "exam_count": len(exams),
            "scored_exam_count": stats["count"],
            "avg_score_pct": stats["avg"],
            "median_score_pct": stats["median"],
            "p25_score_pct": stats["p25"],
            "p75_score_pct": stats["p75"],
            "pass_rate_pct": _round((pass_count / judged_count) * 100) if judged_count else None,
            "not_submitted_count": sum(1 for exam in exams if exam.get("meta_status") == "NOT_SUBMITTED"),
            "risk_level": risk_level,
            "generated_at": _now_iso(),
        },
        "trends": trends,
        "lecture_breakdown": lecture_breakdown,
        "weak_questions": weak_questions,
        "homework": {
            "assigned_count": len(homeworks),
            "graded_count": len(graded_homeworks),
            "avg_score_pct": _stats(homework_score_pcts)["avg"],
            "pass_rate_pct": _round((homework_pass_count / len(graded_homeworks)) * 100) if graded_homeworks else None,
        },
        "highlights": {
            "latest_exam": {
                "exam_id": latest_exam.get("exam_id"),
                "title": latest_exam.get("title"),
                "score_pct": _round(latest_exam.get("score_pct")),
            } if latest_exam else None,
            "best_exam": {
                "exam_id": best_exam.get("exam_id"),
                "title": best_exam.get("title"),
                "score_pct": _round(best_exam.get("score_pct")),
            } if best_exam else None,
            "weakest_exam": {
                "exam_id": weakest_exam.get("exam_id"),
                "title": weakest_exam.get("title"),
                "score_pct": _round(weakest_exam.get("score_pct")),
            } if weakest_exam else None,
        },
        "insights": insights,
        "data_quality": {
            "filtered_test_exam_count": len(all_exams) - len(non_test_exams),
        },
    }
