"""Tenant-scoped read model for the staff student performance console."""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from datetime import date, timedelta
from math import isfinite
from statistics import fmean
from typing import Any

from django.core.cache import cache
from django.db.models import Count, Max, Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.domains.enrollment.models import Enrollment
from apps.domains.exams.models import Exam
from apps.domains.lectures.models import Lecture, Session
from apps.domains.results.models import Result, StudentReportedScore
from apps.domains.results.services.omr_subjective_completion import (
    pending_omr_result_ids_for_ids,
)
from apps.domains.results.utils.initial_exam_score import (
    load_initial_exam_scores,
    project_initial_exam_score,
)
from apps.domains.students.models import Student
from apps.support.results.student_reported_scores import serialize_reported_score


PERFORMANCE_DAY_OPTIONS = (30, 90, 180, 365)


def performance_lecture_exists(*, tenant: Any, lecture_id: int) -> bool:
    return Lecture.objects.filter(
        tenant=tenant,
        id=lecture_id,
        is_system=False,
    ).exists()


def normalize_performance_days(raw: Any, *, default: int = 180) -> int | None:
    """Accept the console's bounded period options or the explicit all-time value."""
    if isinstance(raw, str) and raw.strip().lower() == "all":
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value in PERFORMANCE_DAY_OPTIONS else default


def _score_pct(score: Any, max_score: Any) -> float | None:
    try:
        score_value = float(score)
        max_score_value = float(max_score)
    except (TypeError, ValueError, OverflowError):
        return None
    if (
        not isfinite(score_value)
        or not isfinite(max_score_value)
        or score_value < 0
        or max_score_value <= 0
    ):
        return None
    value = (score_value / max_score_value) * 100
    return round(value, 1) if isfinite(value) else None


def _school_name(row: dict[str, Any]) -> str | None:
    school_type = row.get("school_type")
    if school_type == "ELEMENTARY":
        return row.get("elementary_school")
    if school_type == "MIDDLE":
        return row.get("middle_school")
    return row.get("high_school")


def _score_band(latest: float | None) -> str:
    if latest is None:
        return "unscored"
    if latest < 60:
        return "under_60"
    if latest < 80:
        return "60_to_79"
    return "80_plus"


def _trend_direction(change: float | None) -> str:
    if change is None:
        return "insufficient"
    if change > 0:
        return "up"
    if change < 0:
        return "down"
    return "flat"


def _summarize_points(points: list[dict[str, Any]]) -> dict[str, Any]:
    values = [float(point["score_pct"]) for point in points if point.get("score_pct") is not None]
    latest = values[-1] if values else None
    previous = values[-2] if len(values) > 1 else None
    change = round(latest - previous, 1) if latest is not None and previous is not None else None
    first_to_latest = round(latest - values[0], 1) if latest is not None and len(values) > 1 else None
    return {
        "scored_count": len(values),
        "average_score_pct": round(fmean(values), 1) if values else None,
        "latest_score_pct": latest,
        "change_pct_points": change,
        "first_to_latest_pct_points": first_to_latest,
        "best_score_pct": max(values) if values else None,
        "score_band": _score_band(latest),
        "trend_direction": _trend_direction(change),
    }


def _reported_effective_date(row: dict[str, Any]) -> date | None:
    if row.get("exam_date"):
        try:
            return date.fromisoformat(str(row["exam_date"])[:10])
        except ValueError:
            return None
    year = row.get("academic_year")
    try:
        if row.get("source_group") == "mock" and row.get("exam_month"):
            return date(int(year), int(row["exam_month"]), 1)
        if row.get("source_group") == "school":
            school_month = {
                (1, "first"): 4,
                (1, "second"): 7,
                (2, "first"): 10,
                (2, "second"): 12,
            }.get((row.get("semester"), row.get("exam_round")))
            if school_month:
                return date(int(year), school_month, 1)
    except (TypeError, ValueError):
        return None
    return None


def _display_names(student_rows: list[dict[str, Any]]) -> dict[int, str]:
    name_counts = Counter(str(row.get("name") or "") for row in student_rows)
    name_indexes: defaultdict[str, int] = defaultdict(int)
    output = {}
    for row in sorted(student_rows, key=lambda item: int(item["id"])):
        name = str(row.get("name") or "학생")
        if name_counts[name] > 1:
            name_indexes[name] += 1
            output[int(row["id"])] = f"{name}{name_indexes[name]}"
        else:
            output[int(row["id"])] = name
    return output


def _build_student_performance_console_uncached(
    *,
    tenant: Any,
    days: int | None = 180,
    lecture_id: int | None = None,
    student_id: int | None = None,
    search: str = "",
    grade: int | None = None,
    source: str = "overall",
    session_type: str = "all",
    subject: str = "",
    score_band: str = "all",
    trend: str = "all",
    sort: str = "attention",
    page: int = 1,
    page_size: int = 30,
    review_page: int = 1,
    review_page_size: int = 20,
) -> dict[str, Any]:
    """Build filtered summaries and return one bounded roster page."""
    base_student_query = Student.objects.filter(
        tenant=tenant,
        deleted_at__isnull=True,
        is_managed=True,
    )
    grade_options = sorted(
        int(value)
        for value in base_student_query.exclude(grade__isnull=True).order_by()
        .values_list("grade", flat=True)
        .distinct()
    )
    student_query = base_student_query
    if student_id is not None:
        student_query = student_query.filter(id=student_id)
    if grade is not None:
        student_query = student_query.filter(grade=grade)
    normalized_search = search.strip()
    if normalized_search:
        student_query = student_query.filter(
            Q(name__icontains=normalized_search)
            | Q(elementary_school__icontains=normalized_search)
            | Q(middle_school__icontains=normalized_search)
            | Q(high_school__icontains=normalized_search)
            | Q(enrollments__tenant=tenant, enrollments__lecture__title__icontains=normalized_search)
        ).distinct()
    student_rows = list(
        student_query.values(
            "id",
            "name",
            "grade",
            "school_type",
            "elementary_school",
            "middle_school",
            "high_school",
        )
    )
    all_student_ids = {int(row["id"]) for row in student_rows}
    student_meta_by_id = {int(row["id"]): row for row in student_rows}
    display_names = _display_names(student_rows)

    enrollment_rows = list(
        Enrollment.objects.filter(
            tenant=tenant,
            student_id__in=all_student_ids,
            lecture__tenant=tenant,
            lecture__is_system=False,
        ).values(
            "id",
            "student_id",
            "status",
            "lecture_id",
            "lecture__title",
            "lecture__color",
            "lecture__chip_label",
            "lecture__is_active",
        )
    )

    lecture_options_by_id: dict[int, dict[str, Any]] = {}
    lectures_by_student: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    enrollment_student_lecture: dict[int, tuple[int, int]] = {}
    selected_enrollment_ids: set[int] = set()
    lecture_student_ids: set[int] = set()
    for enrollment in enrollment_rows:
        enrollment_id = int(enrollment["id"])
        student_id = int(enrollment["student_id"])
        row_lecture_id = int(enrollment["lecture_id"])
        enrollment_student_lecture[enrollment_id] = (student_id, row_lecture_id)
        lecture = {
            "id": row_lecture_id,
            "title": enrollment["lecture__title"],
            "color": enrollment["lecture__color"],
            "chip_label": enrollment["lecture__chip_label"],
            "is_active": bool(enrollment["lecture__is_active"]),
        }
        lecture_options_by_id.setdefault(row_lecture_id, lecture)
        lectures_by_student[student_id].append({
            **lecture,
            "enrollment_id": enrollment_id,
            "enrollment_status": enrollment["status"],
        })
        if lecture_id is None or row_lecture_id == lecture_id:
            selected_enrollment_ids.add(enrollment_id)
        if lecture_id is not None and row_lecture_id == lecture_id:
            lecture_student_ids.add(student_id)

    if lecture_id is not None:
        student_rows = [row for row in student_rows if int(row["id"]) in lecture_student_ids]
    selected_student_ids = {int(row["id"]) for row in student_rows}
    selected_enrollment_ids = {
        enrollment_id
        for enrollment_id in selected_enrollment_ids
        if enrollment_student_lecture[enrollment_id][0] in selected_student_ids
    }

    exam_rows = Exam.objects.filter(
        tenant=tenant,
        exam_type=Exam.ExamType.REGULAR,
    ).values("id", "title")
    exam_titles = {int(row["id"]): row["title"] for row in exam_rows}

    session_types_by_exam_and_lecture: defaultdict[tuple[int, int], set[str]] = defaultdict(set)
    for row in Session.objects.filter(
        lecture__tenant=tenant,
        lecture_id__in={lecture_id for _student_id, lecture_id in enrollment_student_lecture.values()},
        exams__id__in=exam_titles,
    ).values("exams__id", "lecture_id", "session_type"):
        session_types_by_exam_and_lecture[
            (int(row["exams__id"]), int(row["lecture_id"]))
        ].add(str(row["session_type"]))

    def classified_session_type(*, exam_id: int, row_lecture_id: int) -> str:
        linked_types = session_types_by_exam_and_lecture.get((exam_id, row_lecture_id), set())
        if linked_types == {Session.SessionType.REGULAR}:
            return Session.SessionType.REGULAR
        if linked_types == {Session.SessionType.SUPPLEMENT}:
            return Session.SessionType.SUPPLEMENT
        return "UNCLASSIFIED"

    result_query = (
        Result.objects.filter(
            target_type="exam",
            target_id__in=exam_titles,
            enrollment_id__in=selected_enrollment_ids,
            enrollment__tenant=tenant,
            enrollment__student_id__in=selected_student_ids,
        )
        .annotate(recorded_at=Coalesce("submitted_at", "created_at"))
        .order_by("recorded_at", "id")
        .values(
            "id",
            "target_id",
            "enrollment_id",
            "total_score",
            "max_score",
            "recorded_at",
            "attempt__meta",
        )
    )
    result_rows = list(result_query)
    pending_result_ids = pending_omr_result_ids_for_ids(
        row["id"] for row in result_rows
    )
    result_rows = [
        row for row in result_rows if int(row["id"]) not in pending_result_ids
    ]
    initial_scores = load_initial_exam_scores(
        exam_ids=exam_titles,
        enrollment_ids=selected_enrollment_ids,
    )
    period_start_at = timezone.now() - timedelta(days=days) if days is not None else None

    latest_result_by_exam: dict[tuple[int, int], dict[str, Any]] = {}
    for result in result_rows:
        enrollment_meta = enrollment_student_lecture.get(int(result["enrollment_id"]))
        if not enrollment_meta:
            continue
        student_id, row_lecture_id = enrollment_meta
        attempt_meta = result.get("attempt__meta")
        initial_score = project_initial_exam_score(
            state=initial_scores.get((int(result["target_id"]), int(result["enrollment_id"]))),
            fallback_score=result.get("total_score"),
            fallback_max_score=result.get("max_score"),
            fallback_not_submitted=(
                isinstance(attempt_meta, dict)
                and attempt_meta.get("status") == "NOT_SUBMITTED"
            ),
            fallback_recorded_at=result.get("recorded_at"),
        )
        if initial_score.not_submitted:
            continue
        recorded_at = initial_score.recorded_at or result.get("recorded_at")
        if period_start_at is not None and (
            recorded_at is None or recorded_at < period_start_at
        ):
            continue
        score_pct = _score_pct(initial_score.total_score, initial_score.max_score)
        if score_pct is None:
            continue
        latest_result_by_exam[(student_id, int(result["target_id"]))] = {
            "exam_id": int(result["target_id"]),
            "lecture_id": row_lecture_id,
            "session_type": classified_session_type(
                exam_id=int(result["target_id"]),
                row_lecture_id=row_lecture_id,
            ),
            "title": exam_titles[int(result["target_id"])],
            "score_pct": score_pct,
            "recorded_at": recorded_at,
        }

    session_type_result_count = {
        "all": len(latest_result_by_exam),
        Session.SessionType.REGULAR: 0,
        Session.SessionType.SUPPLEMENT: 0,
        "UNCLASSIFIED": 0,
    }
    results_by_student: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    for (student_id, _exam_id), result in latest_result_by_exam.items():
        session_type_result_count[result["session_type"]] += 1
        if session_type != "all" and result["session_type"] != session_type:
            continue
        results_by_student[student_id].append(result)

    reported_rows = list(
        StudentReportedScore.objects.filter(
            tenant=tenant,
            student_id__in=selected_student_ids,
            student__tenant=tenant,
        ).filter(
            Q(evidence_file__tenant=tenant)
            | Q(
                evidence_file__isnull=True,
                status__in=(
                    StudentReportedScore.Status.REJECTED,
                    StudentReportedScore.Status.VOIDED,
                ),
            )
        ).select_related("evidence_file")
    )
    reported_by_student: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    reported_subjects: set[str] = set()
    pending_reported_scores = []
    period_start = (timezone.now() - timedelta(days=days)).date() if days is not None else None
    for reported_row in reported_rows:
        serialized = serialize_reported_score(reported_row)
        reported_subjects.add(serialized["subject"])
        reported_by_student[reported_row.student_id].append(serialized)
        if reported_row.status == StudentReportedScore.Status.PENDING:
            student_meta = student_meta_by_id.get(reported_row.student_id)
            if student_meta:
                pending_reported_scores.append({
                    **serialized,
                    "student_name": display_names[reported_row.student_id],
                    "school": _school_name(student_meta),
                    "grade": student_meta.get("grade"),
                })

    output_students = []
    for student in student_rows:
        student_id = int(student["id"])
        points = sorted(
            results_by_student.get(student_id, []),
            key=lambda item: (item["recorded_at"], item["exam_id"]),
        )
        academy_summary = _summarize_points(points)
        student_reported = sorted(
            reported_by_student.get(student_id, []),
            key=lambda item: (_reported_effective_date(item) or date.min, item["id"]),
        )
        verified_reported = [
            item
            for item in student_reported
            if item["status"] == StudentReportedScore.Status.VERIFIED
            and (period_start is None or (_reported_effective_date(item) or date.min) >= period_start)
        ]
        school_points = [item for item in verified_reported if item["source_group"] == "school"]
        mock_points = [item for item in verified_reported if item["source_group"] == "mock"]
        school_summary = _summarize_points(school_points)
        mock_summary = _summarize_points(mock_points)
        school_subject_summaries = {
            subject: _summarize_points([item for item in school_points if item["subject"] == subject])
            for subject in sorted({item["subject"] for item in school_points})
        }
        mock_subject_summaries = {
            subject: _summarize_points([item for item in mock_points if item["subject"] == subject])
            for subject in sorted({item["subject"] for item in mock_points})
        }
        combined_points = sorted(
            [
                *({**point, "effective_date": point["recorded_at"].date()} for point in points),
                *({**item, "effective_date": _reported_effective_date(item) or date.min} for item in verified_reported),
            ],
            key=lambda item: (item["effective_date"], item.get("exam_id") or item.get("id") or 0),
        )
        overall_summary = _summarize_points(combined_points)
        student_lectures = sorted(
            lectures_by_student.get(student_id, []),
            key=lambda item: (not item["is_active"], item["title"], item["id"]),
        )
        output_students.append({
            "student_id": student_id,
            "name": student["name"],
            "display_name": display_names[student_id],
            "grade": student["grade"],
            "school_type": student["school_type"],
            "school": _school_name(student),
            "lectures": student_lectures,
            "scored_count": academy_summary["scored_count"],
            "average_score_pct": academy_summary["average_score_pct"],
            "latest_score_pct": academy_summary["latest_score_pct"],
            "change_pct_points": academy_summary["change_pct_points"],
            "first_to_latest_pct_points": academy_summary["first_to_latest_pct_points"],
            "best_score_pct": academy_summary["best_score_pct"],
            "latest_exam_title": points[-1]["title"] if points else None,
            "last_recorded_at": points[-1]["recorded_at"].isoformat() if points else None,
            "score_band": academy_summary["score_band"],
            "trend_direction": academy_summary["trend_direction"],
            "source_summaries": {
                "overall": overall_summary,
                "academy": academy_summary,
                "school": school_summary,
                "mock": mock_summary,
            },
            "subject_summaries": {
                "school": school_subject_summaries,
                "mock": mock_subject_summaries,
            },
            "reported_scores": student_reported,
            "pending_reported_score_count": sum(
                1 for item in student_reported if item["status"] == StudentReportedScore.Status.PENDING
            ),
        })

    def selected_summary(row: dict[str, Any]) -> dict[str, Any]:
        if source in ("school", "mock") and subject:
            return row["subject_summaries"][source].get(subject, _summarize_points([]))
        return row["source_summaries"].get(source, row["source_summaries"]["overall"])

    filtered_students = [
        row
        for row in output_students
        if (score_band == "all" or selected_summary(row)["score_band"] == score_band)
        and (trend == "all" or selected_summary(row)["trend_direction"] == trend)
    ]
    if source == "overall":
        filtered_students.sort(
            key=lambda row: (-row["pending_reported_score_count"], row["display_name"])
        )
    elif sort == "name":
        filtered_students.sort(key=lambda row: row["display_name"])
    elif sort == "latest_desc":
        filtered_students.sort(
            key=lambda row: (
                -selected_summary(row)["latest_score_pct"]
                if selected_summary(row)["latest_score_pct"] is not None
                else float("inf"),
                row["display_name"],
            )
        )
    elif sort == "change_desc":
        filtered_students.sort(
            key=lambda row: (
                -selected_summary(row)["change_pct_points"]
                if selected_summary(row)["change_pct_points"] is not None
                else float("inf"),
                row["display_name"],
            )
        )
    else:
        filtered_students.sort(
            key=lambda row: (
                selected_summary(row)["latest_score_pct"]
                if selected_summary(row)["latest_score_pct"] is not None
                else float("inf"),
                row["display_name"],
            )
        )

    total_count = len(filtered_students)
    filtered_student_ids = {row["student_id"] for row in filtered_students}
    pending_reported_scores = [
        row for row in pending_reported_scores if row["student_id"] in filtered_student_ids
    ]
    pending_reported_scores.sort(
        key=lambda item: item.get("created_at") or "",
        reverse=True,
    )
    pending_groups: dict[str, list[dict[str, Any]]] = {}
    for row in pending_reported_scores:
        group_key = (
            f"evidence:{row['evidence_file_id']}"
            if row.get("evidence_file_id")
            else f"score:{row['id']}"
        )
        pending_groups.setdefault(group_key, []).append(row)
    pending_group_rows = list(pending_groups.values())
    pending_total_rows = len(pending_reported_scores)
    pending_total_groups = len(pending_group_rows)
    review_total_pages = max(1, (pending_total_groups + review_page_size - 1) // review_page_size)
    review_page = min(review_page, review_total_pages)
    review_page_start = (review_page - 1) * review_page_size
    paged_pending_reported_scores = [
        row
        for group in pending_group_rows[review_page_start:review_page_start + review_page_size]
        for row in group
    ]
    total_pages = max(1, (total_count + page_size - 1) // page_size)
    page = min(page, total_pages)
    page_start = (page - 1) * page_size
    paged_students = filtered_students[page_start:page_start + page_size]
    scored_students = [row for row in filtered_students if selected_summary(row)["scored_count"] > 0]
    selected_averages = [
        selected_summary(row)["average_score_pct"]
        for row in scored_students
        if selected_summary(row)["average_score_pct"] is not None
    ]
    return {
        "period": {
            "days": days,
            "from": (timezone.now() - timedelta(days=days)).date().isoformat() if days is not None else None,
            "to": timezone.now().date().isoformat(),
        },
        "summary": {
            "student_count": total_count,
            "scored_student_count": len(scored_students),
            "result_count": sum(selected_summary(row)["scored_count"] for row in filtered_students),
            "average_score_pct": round(fmean(selected_averages), 1) if selected_averages else None,
            "under_60_student_count": sum(
                1 for row in filtered_students if selected_summary(row)["score_band"] == "under_60"
            ),
            "improving_student_count": sum(
                1 for row in filtered_students if selected_summary(row)["trend_direction"] == "up"
            ),
            "declining_student_count": sum(
                1 for row in filtered_students if selected_summary(row)["trend_direction"] == "down"
            ),
            "pending_reported_score_count": pending_total_rows,
            "academy_student_count": sum(
                1 for row in filtered_students if row["source_summaries"]["academy"]["scored_count"] > 0
            ),
            "school_student_count": sum(
                1 for row in filtered_students if row["source_summaries"]["school"]["scored_count"] > 0
            ),
            "mock_student_count": sum(
                1 for row in filtered_students if row["source_summaries"]["mock"]["scored_count"] > 0
            ),
            "verified_school_score_count": sum(
                row["source_summaries"]["school"]["scored_count"] for row in filtered_students
            ),
            "verified_mock_score_count": sum(
                row["source_summaries"]["mock"]["scored_count"] for row in filtered_students
            ),
            "session_type_result_count": session_type_result_count,
        },
        "filter_options": {
            "lectures": sorted(
                lecture_options_by_id.values(),
                key=lambda item: (not item["is_active"], item["title"], item["id"]),
            ),
            "grades": grade_options,
            "reported_subjects": sorted(reported_subjects),
        },
        "pending_reported_scores": paged_pending_reported_scores,
        "review_pagination": {
            "page": review_page,
            "page_size": review_page_size,
            "total_count": pending_total_groups,
            "total_rows": pending_total_rows,
            "total_pages": review_total_pages,
        },
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total_count": total_count,
            "total_pages": total_pages,
        },
        "students": paged_students,
    }


def _performance_data_version(*, tenant: Any) -> tuple[tuple[int, str], ...]:
    """Cheap tenant stamp so cached console pages invalidate on relevant writes."""

    def stamp(queryset) -> tuple[int, str]:
        values = queryset.aggregate(row_count=Count("pk"), latest_update=Max("updated_at"))
        return int(values["row_count"] or 0), str(values["latest_update"] or "")

    session_exam_relations = Exam.sessions.through.objects.filter(exam__tenant=tenant).aggregate(
        row_count=Count("pk"),
        latest_id=Max("pk"),
    )
    return (
        stamp(Student.objects.filter(tenant=tenant)),
        stamp(Enrollment.objects.filter(tenant=tenant)),
        stamp(Exam.objects.filter(tenant=tenant, exam_type=Exam.ExamType.REGULAR)),
        stamp(Session.objects.filter(lecture__tenant=tenant)),
        (
            int(session_exam_relations["row_count"] or 0),
            str(session_exam_relations["latest_id"] or ""),
        ),
        stamp(Result.objects.filter(enrollment__tenant=tenant, target_type="exam")),
        stamp(StudentReportedScore.objects.filter(tenant=tenant)),
    )


def build_student_performance_console(
    *,
    tenant: Any,
    days: int | None = 180,
    lecture_id: int | None = None,
    student_id: int | None = None,
    search: str = "",
    grade: int | None = None,
    source: str = "overall",
    session_type: str = "all",
    subject: str = "",
    score_band: str = "all",
    trend: str = "all",
    sort: str = "attention",
    page: int = 1,
    page_size: int = 30,
    review_page: int = 1,
    review_page_size: int = 20,
) -> dict[str, Any]:
    """Return a versioned five-minute cache page for repeated polling/filter reads."""
    data_version = _performance_data_version(tenant=tenant)
    key_parts = (
        tenant.id,
        data_version,
        days,
        lecture_id,
        student_id,
        search,
        grade,
        source,
        session_type,
        subject,
        score_band,
        trend,
        sort,
        page,
        page_size,
        review_page,
        review_page_size,
    )
    digest = hashlib.sha256(repr(key_parts).encode("utf-8")).hexdigest()
    cache_key = f"student-performance-console:v4:{tenant.id}:{digest}"
    cached = cache.get(cache_key)
    if isinstance(cached, dict):
        return cached
    response = _build_student_performance_console_uncached(
        tenant=tenant,
        days=days,
        lecture_id=lecture_id,
        student_id=student_id,
        search=search,
        grade=grade,
        source=source,
        session_type=session_type,
        subject=subject,
        score_band=score_band,
        trend=trend,
        sort=sort,
        page=page,
        page_size=page_size,
        review_page=review_page,
        review_page_size=review_page_size,
    )
    cache.set(cache_key, response, timeout=300)
    return response
