"""Student-app result read models."""

from __future__ import annotations

from math import isfinite
from typing import Any

from django.db.models import F, Max

from apps.domains.enrollment.selectors import learning_history_enrollments_for_student
from apps.domains.homework.models import HomeworkAssignment
from apps.domains.homework_results.models import HomeworkScore
from apps.core.services.student_grade_report_layout import (
    get_student_grade_report_layout,
)
from apps.domains.progress.models import AssessmentCorrection, ClinicLink
from apps.domains.results.services.student_result_service import get_my_exam_result_data
from apps.domains.results.services.assessment_correction_status import (
    assessment_correction_payload,
    exam_correction_fingerprint,
)
from apps.domains.results.utils.ranking import compute_exam_rankings_batch
from apps.support.results.student_grade_history import (
    build_exam_progression,
    build_student_exam_history,
    empty_exam_summary,
)
from apps.support.results.admin_student_grades_dependencies import (
    submitted_homework_keys_for_grades,
)


def get_student_exam_result_data(request: Any, exam_id: int, *, tenant: Any):
    return get_my_exam_result_data(request, exam_id, tenant=tenant)


def _empty_result_item_analysis():
    return {
        "total_questions": 0,
        "correct_count": 0,
        "wrong_count": 0,
        "accuracy_rate": None,
        "wrong_question_numbers": [],
    }


def _summarize_grade_result_items(result, *, structure_exam_id: int):
    total = 0
    correct = 0
    wrong_numbers = []

    for item in result.items.all():
        question = getattr(item, "question", None)
        sheet = getattr(question, "sheet", None) if question else None
        if (
            not sheet
            or int(getattr(sheet, "exam_id", 0) or 0) != int(structure_exam_id)
        ):
            continue
        total += 1
        if item.is_correct:
            correct += 1
            continue

        raw_number = getattr(question, "number", None)
        try:
            wrong_numbers.append(int(raw_number))
        except (TypeError, ValueError):
            continue

    wrong_numbers.sort()
    return {
        "total_questions": total,
        "correct_count": correct,
        "wrong_count": max(total - correct, 0),
        "accuracy_rate": round((correct / total) * 100, 1) if total else None,
        "wrong_question_numbers": wrong_numbers,
    }


def _session_titles(session: Any) -> tuple[str | None, str | None]:
    if not session:
        return None, None
    session_title = getattr(session, "title", None) or getattr(session, "display_label", "")
    lecture = getattr(session, "lecture", None)
    lecture_title = getattr(lecture, "title", None) if lecture else None
    return session_title, lecture_title


def _homework_session_metadata(session: Any) -> dict[str, Any]:
    if not session:
        return {
            "session_id": None,
            "session_order": None,
            "session_regular_order": None,
            "session_type": None,
            "lecture_id": None,
            "lecture_color": None,
            "lecture_chip_label": None,
        }
    lecture = getattr(session, "lecture", None)
    return {
        "session_id": int(session.id),
        "session_order": int(session.order),
        "session_regular_order": (
            int(session.regular_order)
            if getattr(session, "regular_order", None) is not None
            else None
        ),
        "session_type": getattr(session, "session_type", None),
        "lecture_id": int(session.lecture_id),
        "lecture_color": getattr(lecture, "color", None) if lecture else None,
        "lecture_chip_label": getattr(lecture, "chip_label", None) if lecture else None,
    }


def _homework_history_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    session_order = row.get("session_order")
    return (
        (row.get("lecture_title") or "").casefold(),
        session_order is None,
        -(int(session_order) if session_order is not None else 0),
        int(row.get("display_order") or 0),
        -int(row["homework_id"]),
    )


def _default_homework_max_score(homework: Any) -> float | None:
    meta = getattr(homework, "meta", None) or {}
    if not isinstance(meta, dict):
        return None
    default_max_score = meta.get("default_max_score")
    if default_max_score is None:
        return None
    try:
        value = float(default_max_score)
    except (TypeError, ValueError):
        return None
    return value if isfinite(value) and value > 0 else None


def _safe_homework_number(value: Any, *, positive: bool = False) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    minimum_ok = parsed > 0 if positive else parsed >= 0
    return parsed if isfinite(parsed) and minimum_ok else None


def build_student_grades_summary(*, tenant: Any, student: Any) -> dict[str, Any]:
    report_layout = get_student_grade_report_layout(tenant=tenant)
    history_enrollments = list(
        learning_history_enrollments_for_student(
            tenant=tenant,
            student=student,
        ).order_by("lecture_id")
    )
    enrollment_ids = [int(enrollment.id) for enrollment in history_enrollments]
    lecture_active_by_enrollment = {
        int(enrollment.id): bool(enrollment.lecture.is_active)
        for enrollment in history_enrollments
    }
    lecture_options = [
        {
            "id": int(enrollment.lecture_id),
            "title": enrollment.lecture.title,
            "color": enrollment.lecture.color,
            "chip_label": enrollment.lecture.chip_label,
            "is_active": bool(enrollment.lecture.is_active),
        }
        for enrollment in history_enrollments
    ]
    if not enrollment_ids:
        return {
            "exams": [],
            "homeworks": [],
            "exam_trend": [],
            "exam_summary": empty_exam_summary(),
            "lecture_options": [],
            "report_layout": report_layout,
        }

    exam_list, exam_trend, exam_summary = build_student_exam_history(
        tenant=tenant,
        enrollment_ids=enrollment_ids,
        published_results_only=True,
    )
    ready_exams = [
        exam
        for exam in exam_list
        if exam.get("grading_status") != "subjective_pending"
    ]
    exam_ids = [int(exam["exam_id"]) for exam in ready_exams]
    result_ids = [int(exam["_result_id"]) for exam in ready_exams]
    structure_exam_id_by_result_id = {
        int(exam["_result_id"]): int(exam["_structure_exam_id"])
        for exam in ready_exams
    }
    result_analysis_map = {}
    if result_ids:
        from apps.domains.results.models import Result

        result_rows = list(
            Result.objects
            .filter(id__in=result_ids)
            .prefetch_related("items__question__sheet")
        )
        result_by_id = {int(result.id): result for result in result_rows}
        result_analysis_map = {
            int(result.id): _summarize_grade_result_items(
                result,
                structure_exam_id=structure_exam_id_by_result_id[int(result.id)],
            )
            for result in result_rows
        }
        result_fingerprint_map = {
            int(result.id): exam_correction_fingerprint(
                result=result,
                items=result.items.all(),
            )
            for result in result_rows
        }
    else:
        result_by_id = {}
        result_fingerprint_map = {}

    correction_map = {}
    correction_session_ids = [
        int(exam["session_id"])
        for exam in ready_exams
        if exam.get("session_id") is not None
    ]
    if correction_session_ids and exam_ids:
        correction_map = {
            (
                int(correction.enrollment_id),
                int(correction.session_id),
                int(correction.source_id),
            ): correction
            for correction in AssessmentCorrection.objects.filter(
                tenant=tenant,
                enrollment_id__in=enrollment_ids,
                session_id__in=correction_session_ids,
                source_type=AssessmentCorrection.SourceType.EXAM,
                source_id__in=exam_ids,
            )
        }

    exam_rank_maps = compute_exam_rankings_batch(
        exam_ids=exam_ids,
        enrollment_ids=enrollment_ids,
        tenant=tenant,
    )

    for exam in exam_list:
        result_id = int(exam.pop("_result_id"))
        exam.pop("_structure_exam_id")
        exam_id = int(exam["exam_id"])
        enrollment_id = int(exam["enrollment_id"])
        if exam.get("grading_status") == "subjective_pending":
            exam.update({
                "rank": None,
                "percentile": None,
                "cohort_size": None,
                "cohort_avg": None,
                "total_questions": 0,
                "correct_count": 0,
                "wrong_count": 0,
                "accuracy_rate": None,
                "wrong_question_numbers": [],
                "correction_status": None,
                "teacher_resolved": False,
                "lecture_active": lecture_active_by_enrollment.get(
                    enrollment_id,
                    False,
                ),
            })
            continue
        rank_info = exam_rank_maps.get(exam_id, {}).get(enrollment_id, {})
        item_analysis = result_analysis_map.get(result_id) or _empty_result_item_analysis()
        session_id = exam.get("session_id")
        correction = correction_map.get((
            enrollment_id,
            int(session_id),
            exam_id,
        )) if session_id is not None else None
        correction_payload = assessment_correction_payload(
            source_type=AssessmentCorrection.SourceType.EXAM,
            score=exam.get("total_score"),
            max_score=exam.get("max_score"),
            source_fingerprint=(
                result_fingerprint_map.get(result_id)
                if result_by_id.get(result_id) is not None
                else None
            ),
            correction=correction,
        )
        exam.update({
            "rank": rank_info.get("rank"),
            "percentile": rank_info.get("percentile"),
            "cohort_size": rank_info.get("cohort_size"),
            "cohort_avg": rank_info.get("cohort_avg"),
            "total_questions": item_analysis["total_questions"],
            "correct_count": item_analysis["correct_count"],
            "wrong_count": item_analysis["wrong_count"],
            "accuracy_rate": item_analysis["accuracy_rate"],
            "wrong_question_numbers": item_analysis["wrong_question_numbers"],
            "correction_status": correction_payload["correction_status"],
            "teacher_resolved": correction_payload["teacher_resolved"],
            "lecture_active": lecture_active_by_enrollment.get(enrollment_id, False),
        })
    exam_trend, exam_summary = build_exam_progression(exam_list)

    homework_scores = (
        HomeworkScore.objects.filter(
            enrollment_id__in=enrollment_ids,
            enrollment__tenant=tenant,
            homework__tenant=tenant,
            session__lecture__tenant=tenant,
            session__lecture_id=F("enrollment__lecture_id"),
            homework__session_id=F("session_id"),
            attempt_index=1,
        )
        .exclude(score__isnull=True)
        .exclude(homework__meta__removed_from_session_at__isnull=False)
        .exclude(session__lecture__is_system=True)
        .select_related("homework", "session", "session__lecture")
        .order_by("-updated_at")
    )
    assigned_homeworks = list(
        HomeworkAssignment.objects
        .filter(
            tenant=tenant,
            enrollment_id__in=enrollment_ids,
            enrollment__tenant=tenant,
            homework__tenant=tenant,
            session__lecture__tenant=tenant,
            session__lecture_id=F("enrollment__lecture_id"),
            homework__session_id=F("session_id"),
        )
        .exclude(homework__meta__removed_from_session_at__isnull=False)
        .exclude(session__lecture__is_system=True)
        .select_related("homework", "session", "session__lecture")
        .order_by("-homework__updated_at", "-homework_id")
    )
    assigned_homework_ids = {assignment.homework_id for assignment in assigned_homeworks}
    homework_ids = list(
        {score.homework_id for score in homework_scores} | assigned_homework_ids
    )
    resolved_homework_links = {}
    if homework_ids and enrollment_ids:
        for link in ClinicLink.objects.filter(
            tenant=tenant,
            enrollment_id__in=enrollment_ids,
            source_type="homework",
            source_id__in=homework_ids,
            resolved_at__isnull=False,
            resolution_type__in=["EXAM_PASS", "HOMEWORK_PASS", "MANUAL_OVERRIDE"],
        ).order_by(
            "enrollment_id",
            "source_id",
            "-resolved_at",
            "-id",
        ).values("enrollment_id", "source_id", "resolution_type"):
            key = (link["enrollment_id"], link["source_id"])
            if key not in resolved_homework_links:
                resolved_homework_links[key] = link["resolution_type"]

    homework_retake_counts = {}
    if homework_ids and enrollment_ids:
        for row in (
            HomeworkScore.objects
            .filter(
                homework_id__in=homework_ids,
                enrollment_id__in=enrollment_ids,
                enrollment__tenant=tenant,
                homework__tenant=tenant,
                session__lecture__tenant=tenant,
                session__lecture_id=F("enrollment__lecture_id"),
                homework__session_id=F("session_id"),
            )
            .values("homework_id", "enrollment_id")
            .annotate(max_attempt=Max("attempt_index"))
        ):
            homework_retake_counts[(row["enrollment_id"], row["homework_id"])] = row["max_attempt"]

    homework_list = []
    seen_homework_key = set()
    for score in homework_scores:
        safe_score = _safe_homework_number(score.score)
        if safe_score is None:
            continue
        key = (score.homework_id, score.session_id, score.enrollment_id)
        if key in seen_homework_key:
            continue
        seen_homework_key.add(key)
        session_title, lecture_title = _session_titles(score.session)

        is_pass_1st = bool(score.passed)
        resolution = resolved_homework_links.get((score.enrollment_id, score.homework_id))
        max_attempt = homework_retake_counts.get((score.enrollment_id, score.homework_id), 1)

        if is_pass_1st:
            achievement = "PASS"
        elif resolution in ("EXAM_PASS", "HOMEWORK_PASS", "MANUAL_OVERRIDE"):
            achievement = "REMEDIATED"
        else:
            achievement = "FAIL"

        effective_max = _safe_homework_number(score.max_score, positive=True)
        if effective_max is None and score.homework:
            effective_max = _default_homework_max_score(score.homework)
        session_metadata = _homework_session_metadata(score.session)

        homework_list.append({
            "homework_id": score.homework_id,
            "enrollment_id": score.enrollment_id,
            "title": score.homework.title if score.homework else f"과제 #{score.homework_id}",
            "score": safe_score,
            "max_score": effective_max,
            "passed": is_pass_1st,
            "achievement": achievement,
            "teacher_resolved": resolution == "MANUAL_OVERRIDE",
            "retake_count": max_attempt,
            "grading_mode": score.homework.grading_mode,
            "display_order": score.homework.display_order,
            "session_title": session_title,
            "lecture_title": lecture_title,
            "recorded_at": score.updated_at.isoformat(),
            "lecture_active": bool(score.session.lecture.is_active),
            **session_metadata,
        })

    submitted_homework_keys = set()
    if assigned_homework_ids:
        submitted_homework_keys = submitted_homework_keys_for_grades(
            tenant=tenant,
            enrollment_ids=enrollment_ids,
            homework_ids=list(assigned_homework_ids),
        )
    for assignment in assigned_homeworks:
        homework = assignment.homework
        session = assignment.session
        key = (assignment.homework_id, assignment.session_id, assignment.enrollment_id)
        if key in seen_homework_key:
            continue
        seen_homework_key.add(key)

        effective_max = _default_homework_max_score(homework)
        assignment_session_title, assignment_lecture_title = _session_titles(session)
        was_submitted = (
            assignment.enrollment_id,
            assignment.homework_id,
        ) in submitted_homework_keys
        resolution = resolved_homework_links.get((
            assignment.enrollment_id,
            assignment.homework_id,
        ))
        teacher_resolved = resolution == "MANUAL_OVERRIDE"
        session_metadata = _homework_session_metadata(session)

        homework_list.append({
            "homework_id": assignment.homework_id,
            "enrollment_id": assignment.enrollment_id,
            "title": homework.title if homework else f"과제 #{assignment.homework_id}",
            "score": None,
            "max_score": effective_max,
            "passed": None if was_submitted else False,
            "achievement": (
                "REMEDIATED"
                if teacher_resolved
                else None if was_submitted else "NOT_SUBMITTED"
            ),
            "teacher_resolved": teacher_resolved,
            "retake_count": 0,
            "grading_mode": homework.grading_mode,
            "display_order": homework.display_order,
            "session_title": assignment_session_title,
            "lecture_title": assignment_lecture_title,
            "recorded_at": assignment.created_at.isoformat(),
            "lecture_active": bool(session.lecture.is_active),
            **session_metadata,
        })

    homework_list.sort(key=_homework_history_sort_key)

    return {
        "exams": exam_list,
        "homeworks": homework_list,
        "exam_trend": exam_trend,
        "exam_summary": exam_summary,
        "lecture_options": lecture_options,
        "labels": {
            "pass": (getattr(tenant, "pass_label", None) or "").strip(),
            "fail": (getattr(tenant, "fail_label", None) or "").strip(),
        },
        "report_layout": report_layout,
    }
