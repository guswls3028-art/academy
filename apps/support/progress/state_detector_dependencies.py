"""Bounded, read-only checks of the canonical session exam projection.

This does not infer remediation, checkout, or self-study completion from scores.
The existing calculator's read method remains the owner of exam pass policy.
"""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import timedelta
import hashlib
import json
import math
import time

from django.db import connection, transaction
from django.utils import timezone

from apps.core.models import Tenant
from apps.domains.exams.models import ExamEnrollment, ExamLecturePolicy
from apps.domains.progress.models import ClinicLink, ProgressPolicy, SessionProgress
from apps.domains.progress.services.session_calculator import ExamAggregationReadSources, SessionProgressCalculator
from apps.domains.results.models import ExamAttempt, Result
from apps.domains.results.utils.session_exam import get_all_exams_for_session
from apps.domains.submissions.models import Submission
from apps.support.progress.session_calculator_dependencies import get_target_exam_ids_for_session_enrollment
from apps.support.progress.state_detector_page import StateDetectorPage

RULE = "session_exam_projection_v1"
SOURCE_LIMIT = 500
SCAN_TIMEOUT_SECONDS = 30
SETTLE_GRACE = timedelta(minutes=5)
EXPLICIT_RESOLUTIONS = {
    "MANUAL_OVERRIDE",
    "WAIVED",
    "CARRIED_OVER",
    "SOURCE_REMOVED",
    "NOT_SUBMITTED",
    "BOOKING_LEGACY",
}


class InspectionFailure(Exception):
    """A fixed, PII-free error code; never include object or provider payloads."""


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _limited(queryset):
    rows = list(queryset[: SOURCE_LIMIT + 1])
    if len(rows) > SOURCE_LIMIT:
        raise InspectionFailure("source_limit_exceeded")
    return rows


def _deny_writes(execute, sql, params, many, context):
    # ORM reads used here are SELECTs. Refuse accidental calls into calculators
    # or lifecycle writers even on SQLite test databases.
    if not sql.lstrip().upper().startswith("SELECT "):
        raise InspectionFailure("business_write_refused")
    return execute(sql, params, many, context)


@contextmanager
def _read_snapshot():
    nested = connection.in_atomic_block
    with transaction.atomic():
        if connection.vendor == "postgresql":
            if nested:
                raise InspectionFailure("nested_snapshot_refused")
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                cursor.execute("SET LOCAL statement_timeout = '5s'")
        with connection.execute_wrapper(_deny_writes):
            yield


def _inspect_row(progress, *, tenant_id, cutoff, sources=None):
    enrollment = progress.enrollment
    session = progress.session
    if (
        any(
            value != tenant_id
            for value in (
                enrollment.tenant_id,
                enrollment.student.tenant_id,
                enrollment.lecture.tenant_id,
                session.lecture.tenant_id,
            )
        )
        or enrollment.lecture_id != session.lecture_id
    ):
        raise InspectionFailure("invalid_tenant_graph")
    if enrollment.status not in {"ACTIVE", "INACTIVE", "PENDING"}:
        raise InspectionFailure("unknown_enrollment_state")
    if enrollment.status != "ACTIVE" or enrollment.student.deleted_at:
        return "excluded", None
    policy = sources.policies.get(session.lecture_id) if sources is not None else ProgressPolicy.objects.filter(lecture_id=session.lecture_id).first()
    if policy is None:
        raise InspectionFailure("missing_policy")
    if (
        policy.exam_pass_source not in ProgressPolicy.ExamPassSource.values
        or policy.exam_aggregate_strategy not in ProgressPolicy.ExamAggregateStrategy.values
    ):
        raise InspectionFailure("unknown_exam_policy")
    if (
        not math.isfinite(policy.exam_pass_score)
        or policy.exam_pass_score < 0
        or policy.exam_start_session_order > policy.exam_end_session_order
    ):
        raise InspectionFailure("invalid_exam_policy")
    if sources is not None:
        exams = sources.exams[session.pk]
        targets = sources.target_rows(session.pk)
    else:
        exams = _limited(get_all_exams_for_session(session).order_by("id"))
        targets = _limited(ExamEnrollment.objects.filter(exam_id__in=[exam.pk for exam in exams]).select_related("enrollment").order_by("id"))
    if any(exam.tenant_id != tenant_id for exam in exams):
        raise InspectionFailure("invalid_tenant_graph")
    exam_ids = [exam.pk for exam in exams]
    if any(target.enrollment.tenant_id != tenant_id for target in targets):
        raise InspectionFailure("invalid_tenant_graph")
    target_ids = sources.target_ids(session_id=session.pk, enrollment_id=enrollment.pk) if sources is not None else get_target_exam_ids_for_session_enrollment(session=session, enrollment_id=enrollment.pk)
    order = SessionProgressCalculator._regular_order_for_policy(session)
    if order is None or not policy.exam_start_session_order <= order <= policy.exam_end_session_order or not target_ids:
        return "excluded", None
    if not set(target_ids).issubset(exam_ids):
        raise InspectionFailure("missing_exam_source")
    links = sources.check_group(sources.links[(enrollment.pk, session.pk)]) if sources is not None else _limited(
        ClinicLink.objects.filter(enrollment_id=enrollment.pk, session_id=session.pk).order_by("-cycle_no", "-id")
    )
    if any(link.tenant_id != tenant_id for link in links):
        raise InspectionFailure("invalid_tenant_graph")
    current_links = {}
    for link in links:
        current_links.setdefault((link.source_type, link.source_id), link)
        if link.resolved_at and link.resolution_type not in ClinicLink.ResolutionType.values:
            raise InspectionFailure("unknown_resolution")
    if any(
        link.resolved_at
        and link.resolution_type in EXPLICIT_RESOLUTIONS
        and (
            (link.source_type == "exam" and link.source_id in target_ids)
            or (link.source_type is None and link.source_id is None)
        )
        for link in current_links.values()
    ):
        # Conservative v1 boundary: do not reinterpret a manual/legacy exception
        # as score-based session completion, even if another exam is attached.
        return "excluded", None
    attempts = sources.check_group([row for row in sources.attempts[enrollment.pk] if row.exam_id in target_ids]) if sources is not None else _limited(
        ExamAttempt.objects.filter(enrollment_id=enrollment.pk, exam_id__in=target_ids).order_by(
            "exam_id", "-attempt_index"
        )
    )
    latest = {}
    representatives = {}
    for attempt in attempts:
        if attempt.status not in {"pending", "grading", "done", "failed"}:
            raise InspectionFailure("unknown_attempt_state")
        latest.setdefault(attempt.exam_id, attempt)
        if attempt.is_representative:
            if attempt.exam_id in representatives:
                raise InspectionFailure("ambiguous_representative")
            representatives[attempt.exam_id] = attempt
    if any(attempt.status in {"pending", "grading"} for attempt in [*latest.values(), *representatives.values()]):
        return "deferred", None
    submissions = sources.check_group([row for row in sources.submissions[enrollment.pk] if row.target_id in target_ids]) if sources is not None else _limited(
        Submission.objects.filter(enrollment_id=enrollment.pk, target_type="exam", target_id__in=target_ids).order_by("id")
    )
    if any(item.tenant_id != tenant_id for item in submissions):
        raise InspectionFailure("invalid_tenant_graph")
    if any(item.status not in Submission.Status.values for item in submissions):
        raise InspectionFailure("unknown_submission_state")
    if any(
        item.status not in {Submission.Status.DONE, Submission.Status.FAILED, Submission.Status.SUPERSEDED}
        for item in submissions
    ):
        return "deferred", None
    results = sources.check_group([row for row in sources.results[enrollment.pk] if row.target_id in target_ids]) if sources is not None else _limited(
        Result.objects.filter(enrollment_id=enrollment.pk, target_type="exam", target_id__in=target_ids)
        .select_related("attempt")
        .order_by("id")
    )
    for result in results:
        if not all(math.isfinite(value) for value in (result.total_score, result.max_score)):
            raise InspectionFailure("invalid_result_score")
        if result.attempt_id and (
            result.attempt.enrollment_id != enrollment.pk
            or result.attempt.exam_id != result.target_id
            or not result.attempt.is_representative
        ):
            raise InspectionFailure("invalid_attempt_relation")
    overrides = sources.check_group([row for row in sources.overrides[session.lecture_id] if row.exam_id in target_ids]) if sources is not None else _limited(
        ExamLecturePolicy.objects.filter(lecture_id=session.lecture_id, exam_id__in=target_ids).order_by("id")
    )
    if any(not math.isfinite(item.pass_score) or item.pass_score < 0 for item in [*exams, *overrides]):
        raise InspectionFailure("invalid_exam_policy")
    timestamps = [
        item.updated_at
        for item in [progress, session, policy, *exams, *attempts, *results, *submissions, *links, *overrides]
    ]
    timestamps.extend(item.created_at for item in targets)
    if any(stamp is None for stamp in timestamps):
        raise InspectionFailure("missing_source_timestamp")
    if max(timestamps) > cutoff:
        return "deferred", None
    if progress.calculated_at is None:
        raise InspectionFailure("missing_calculation")
    _, _, expected, _ = SessionProgressCalculator._aggregate_exam_results(
        enrollment_id=enrollment.pk, session=session, policy=policy,
        read_sources=ExamAggregationReadSources(
            exam_ids=target_ids,
            results=results,
            exams={exam.pk: exam for exam in exams},
            lecture_pass_scores={row.exam_id: float(row.pass_score) for row in overrides},
            attempt_counts=dict(Counter(row.exam_id for row in attempts)),
            not_submitted_exam_ids={
                row.exam_id for row in attempts
                if row.is_representative and isinstance(row.meta, dict) and row.meta.get("status") == "NOT_SUBMITTED"
            },
        ) if sources is not None else None,
    )
    return "checked", None if bool(progress.exam_passed) == expected else [bool(progress.exam_passed), expected]


def inspect_session_exam_state(*, tenant_id, limit=200, now=None) -> dict:
    """Scan every existing projection; limit bounds one keyset page, not coverage."""
    started = time.monotonic()
    report = {
        "rule": RULE,
        "inspection_status": "complete",
        "state": "healthy",
        "checked": 0,
        "excluded": 0,
        "deferred": 0,
        "finding_count": 0,
        "source_count": None,
        "scanned": 0,
        "page_count": 0,
        "errors": [],
        "_covered_subjects": [],
        "_finding_subjects": [],
    }
    findings = []
    try:
        if isinstance(tenant_id, bool) or not isinstance(tenant_id, int) or tenant_id <= 0:
            raise InspectionFailure("tenant_required")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise InspectionFailure("invalid_limit")
        report["page_size"] = limit
        cutoff = (now or timezone.now()) - SETTLE_GRACE
        deadline = started + SCAN_TIMEOUT_SECONDS
        with _read_snapshot():
            if not Tenant.objects.filter(pk=tenant_id).exists():
                raise InspectionFailure("tenant_not_found")
            source = SessionProgress.objects.filter(enrollment__tenant_id=tenant_id)
            report["source_count"] = source.count()
            last_id = 0
            while report["scanned"] < report["source_count"]:
                if time.monotonic() > deadline:
                    raise InspectionFailure("scan_timeout")
                rows = list(
                    source.filter(pk__gt=last_id)
                    .select_related("enrollment__student", "enrollment__lecture", "session__lecture")
                    .order_by("id")[:limit]
                )
                if not rows:
                    raise InspectionFailure("incomplete_coverage")
                report["page_count"] += 1
                sources = StateDetectorPage(
                    rows, source_limit=SOURCE_LIMIT, failure=InspectionFailure, deadline=deadline,
                )
                for progress in rows:
                    if time.monotonic() > deadline:
                        raise InspectionFailure("scan_timeout")
                    if progress.pk <= last_id:
                        raise InspectionFailure("invalid_page_order")
                    subject = _digest([RULE, tenant_id, progress.pk])
                    status, mismatch = _inspect_row(progress, tenant_id=tenant_id, cutoff=cutoff, sources=sources)
                    report[status] += 1
                    report["scanned"] += 1
                    report["_covered_subjects"].append(subject)
                    last_id = progress.pk
                    if mismatch is not None:
                        report["_finding_subjects"].append(subject)
                        findings.append([subject, *mismatch])
            if report["scanned"] != report["source_count"]:
                raise InspectionFailure("incomplete_coverage")
            if time.monotonic() > deadline:
                raise InspectionFailure("scan_timeout")
    except InspectionFailure as exc:
        report["errors"].append(str(exc))
    except Exception as exc:
        # Queries/imports can fail. Never emit raw database, tenant, or user data.
        report["errors"].append("inspection_error:" + type(exc).__name__)
    report["finding_count"] = len(findings)
    report["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    report["fingerprint"] = _digest([RULE, tenant_id, findings])
    report["inspection_status"] = "failed" if report["errors"] else "deferred" if report["deferred"] else "complete"
    report["state"] = (
        "contradiction" if findings else "unknown" if report["inspection_status"] != "complete" else "healthy"
    )
    return report
