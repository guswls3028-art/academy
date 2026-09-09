"""Read boundary for validating score-versioned teacher exam resolutions."""

from __future__ import annotations

from typing import Any


def stale_teacher_exam_resolution_link_ids(
    *,
    links: list[dict[str, Any]],
    tenant_id: int,
) -> set[int]:
    """Return teacher-manual links whose correction no longer matches results.

    This bulk read is shared by every exam-achievement surface, so a score or
    answer edit cannot briefly remain green while the asynchronous progress
    pipeline is reopening the Clinic target.
    """
    from apps.domains.progress.models import AssessmentCorrection, ClinicLink
    from apps.domains.exams.models import Exam
    from apps.domains.results.services.assessment_correction_status import (
        exam_correction_fingerprint,
    )
    from apps.domains.results.utils.result_queries import (
        latest_results_for_targets_per_enrollment,
    )

    teacher_links = []
    correction_ids: set[int] = set()
    exam_ids: set[int] = set()
    enrollment_ids: set[int] = set()
    for link in links:
        if link.get("resolution_type") != ClinicLink.ResolutionType.MANUAL_OVERRIDE:
            continue
        evidence = link.get("resolution_evidence")
        evidence = evidence if isinstance(evidence, dict) else {}
        correction_id = int(evidence.get("assessment_correction_id") or 0)
        if not correction_id:
            # Legacy/manual Clinic resolutions are not score-versioned teacher
            # assessment decisions and keep their established contract.
            continue
        teacher_links.append((link, correction_id))
        correction_ids.add(correction_id)
        exam_ids.add(int(link["source_id"]))
        enrollment_ids.add(int(link["enrollment_id"]))
    if not teacher_links:
        return set()

    corrections = {
        int(correction.id): correction
        for correction in AssessmentCorrection.objects.filter(
            id__in=correction_ids,
            tenant_id=int(tenant_id),
            source_type=AssessmentCorrection.SourceType.EXAM,
            completed=True,
        ).only(
            "id",
            "enrollment_id",
            "session_id",
            "source_id",
            "source_fingerprint",
        )
    }
    results = {
        (int(result.enrollment_id), int(result.target_id)): result
        for result in latest_results_for_targets_per_enrollment(
            target_type="exam",
            target_ids=exam_ids,
        )
        .filter(
            enrollment_id__in=enrollment_ids,
            enrollment__tenant_id=int(tenant_id),
        )
        .select_related("attempt")
        .prefetch_related("items")
    }
    current_max_score_by_exam_id = {
        int(exam_id): float(max_score or 0.0)
        for exam_id, max_score in Exam.objects.filter(
            id__in=exam_ids,
            tenant_id=int(tenant_id),
        ).values_list("id", "max_score")
    }

    stale: set[int] = set()
    for link, correction_id in teacher_links:
        link_id = int(link["id"])
        enrollment_id = int(link["enrollment_id"])
        exam_id = int(link["source_id"])
        session_id = int(link["session_id"])
        correction = corrections.get(correction_id)
        if not correction or (
            int(correction.enrollment_id) != enrollment_id
            or int(correction.session_id) != session_id
            or int(correction.source_id) != exam_id
        ):
            stale.add(link_id)
            continue
        result = results.get((enrollment_id, exam_id))
        current_max_score = current_max_score_by_exam_id.get(exam_id)
        attempt_meta = (
            result.attempt.meta
            if result and result.attempt and isinstance(result.attempt.meta, dict)
            else {}
        )
        if (
            not result
            or current_max_score is None
            or attempt_meta.get("status") == "NOT_SUBMITTED"
        ):
            stale.add(link_id)
            continue
        if correction.source_fingerprint and (
            correction.source_fingerprint
            != exam_correction_fingerprint(
                result=result,
                items=result.items.all(),
                current_max_score=current_max_score,
            )
        ):
            stale.add(link_id)
    return stale


def is_current_teacher_exam_resolution(
    *,
    tenant_id: int,
    enrollment_id: int,
    session_id: int,
    exam_id: int,
    correction_id: int,
) -> bool:
    from apps.domains.progress.models import AssessmentCorrection
    from apps.domains.exams.models import Exam
    from apps.domains.results.services.assessment_correction_status import (
        exam_correction_fingerprint,
    )
    from apps.domains.results.utils.result_queries import latest_results_per_enrollment

    correction = (
        AssessmentCorrection.objects.filter(
            id=int(correction_id),
            tenant_id=int(tenant_id),
            enrollment_id=int(enrollment_id),
            session_id=int(session_id),
            source_type=AssessmentCorrection.SourceType.EXAM,
            source_id=int(exam_id),
            completed=True,
        )
        .only("id", "source_fingerprint")
        .first()
    )
    if not correction:
        return False

    current_max_score = (
        Exam.objects.filter(
            id=int(exam_id),
            tenant_id=int(tenant_id),
        ).values_list("max_score", flat=True).first()
    )
    if current_max_score is None:
        return False

    result = (
        latest_results_per_enrollment(
            target_type="exam",
            target_id=int(exam_id),
        )
        .prefetch_related("items")
        .filter(enrollment_id=int(enrollment_id))
        .first()
    )
    if not result:
        return False
    attempt_meta = (
        result.attempt.meta
        if result.attempt and isinstance(result.attempt.meta, dict)
        else {}
    )
    if attempt_meta.get("status") == "NOT_SUBMITTED":
        return False
    if not correction.source_fingerprint:
        return True
    return correction.source_fingerprint == exam_correction_fingerprint(
        result=result,
        items=result.items.all(),
        current_max_score=float(current_max_score),
    )
