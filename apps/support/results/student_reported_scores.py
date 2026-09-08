"""Cross-domain workflow for student-submitted school and mock-exam scores."""

from __future__ import annotations

import json
import time
from datetime import date
from decimal import Decimal, InvalidOperation
from math import isfinite
from typing import Any, Mapping

from django.db import transaction
from django.utils import timezone

from apps.domains.inventory.models import InventoryFile
from apps.domains.results.models import StudentReportedScore
from apps.domains.students.models import Student


SCHOOL_SOURCES = {StudentReportedScore.Source.SCHOOL_EXAM}
MOCK_SOURCES = {
    StudentReportedScore.Source.NATIONAL_MOCK,
    StudentReportedScore.Source.KICE_MOCK,
}
SCORE_ITEM_FIELDS = {
    "subject",
    "score",
    "max_score",
    "standard_score",
    "percentile",
    "grade_rank",
    "grade_scale",
    "achievement_level",
    "subject_average",
    "standard_deviation",
    "cohort_size",
}


class ReportedScoreTransitionConflict(ValueError):
    """Raised when another reviewer already changed the requested score state."""


def _required_text(payload: Mapping[str, Any], key: str, *, max_length: int) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise ValueError(f"{key} required")
    if len(value) > max_length:
        raise ValueError(f"{key} too long")
    return value


def _integer(
    payload: Mapping[str, Any],
    key: str,
    *,
    minimum: int,
    maximum: int,
    required: bool = True,
) -> int | None:
    raw = payload.get(key)
    if raw in (None, ""):
        if required:
            raise ValueError(f"{key} required")
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be integer") from None
    if value < minimum or value > maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}")
    return value


def _decimal(
    payload: Mapping[str, Any],
    key: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
    required: bool = True,
) -> Decimal | None:
    raw = payload.get(key)
    if raw in (None, ""):
        if required:
            raise ValueError(f"{key} required")
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{key} must be number") from None
    if not isfinite(float(value)):
        raise ValueError(f"{key} must be finite")
    if minimum is not None and value < minimum:
        raise ValueError(f"{key} is below minimum")
    if maximum is not None and value > maximum:
        raise ValueError(f"{key} is above maximum")
    return value


def _date(payload: Mapping[str, Any], key: str) -> date | None:
    raw = str(payload.get(key) or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise ValueError(f"{key} must be YYYY-MM-DD") from None


def validate_student_score_submission(
    *,
    tenant: Any,
    user: Any,
    student_ps: str,
    payload: Mapping[str, Any],
    student: Student | None = None,
) -> dict[str, Any]:
    """Validate before R2 upload so invalid score metadata causes no storage write."""
    if student is None:
        student = Student.objects.filter(
            tenant=tenant,
            user=user,
            ps_number=student_ps,
            deleted_at__isnull=True,
        ).first()
    if not student:
        raise ValueError("학생 본인만 자기 성적표를 제출할 수 있습니다.")

    source = str(payload.get("score_source") or "").strip()
    allowed_sources = {choice for choice, _label in StudentReportedScore.Source.choices}
    if source not in allowed_sources:
        raise ValueError("score_source invalid")

    academic_year = _integer(payload, "academic_year", minimum=2000, maximum=2100)
    subject = _required_text(payload, "subject", max_length=50)
    score = _decimal(payload, "score", minimum=Decimal("0"))
    max_score = _decimal(payload, "max_score", minimum=Decimal("0.01"))
    if score is not None and max_score is not None and score > max_score:
        raise ValueError("score cannot exceed max_score")

    standard_score = _decimal(payload, "standard_score", minimum=Decimal("0"), required=False)
    percentile = _decimal(
        payload,
        "percentile",
        minimum=Decimal("0"),
        maximum=Decimal("100"),
        required=False,
    )
    grade_rank = _integer(payload, "grade_rank", minimum=1, maximum=9, required=False)
    grade_scale = str(payload.get("grade_scale") or "").strip()
    if grade_scale and grade_scale not in {choice for choice, _label in StudentReportedScore.GradeScale.choices}:
        raise ValueError("grade_scale invalid")
    if grade_rank is not None and not grade_scale:
        raise ValueError("등급을 입력할 때는 5등급제 또는 9등급제를 선택해야 합니다.")
    if grade_rank is not None and grade_scale == StudentReportedScore.GradeScale.FIVE and grade_rank > 5:
        raise ValueError("5등급제 등급은 1~5여야 합니다.")
    achievement_level = str(payload.get("achievement_level") or "").strip().upper()
    if achievement_level and achievement_level not in {"A", "B", "C", "D", "E"}:
        raise ValueError("achievement_level must be A to E")
    subject_average = _decimal(payload, "subject_average", minimum=Decimal("0"), required=False)
    standard_deviation = _decimal(payload, "standard_deviation", minimum=Decimal("0"), required=False)
    cohort_size = _integer(payload, "cohort_size", minimum=1, maximum=100000, required=False)
    exam_date = _date(payload, "exam_date")

    semester = None
    exam_round = ""
    exam_name = ""
    exam_month = None
    if source in SCHOOL_SOURCES:
        semester = _integer(payload, "semester", minimum=1, maximum=2)
        exam_round = str(payload.get("exam_round") or "").strip()
        if exam_round not in {choice for choice, _label in StudentReportedScore.ExamRound.choices}:
            raise ValueError("exam_round invalid")
        if exam_round in {
            StudentReportedScore.ExamRound.PERFORMANCE,
            StudentReportedScore.ExamRound.OTHER,
        }:
            exam_name = _required_text(payload, "exam_name", max_length=80)
            if exam_date is None:
                raise ValueError("수행평가·기타 학교 평가는 시험일을 입력해야 합니다.")
        if standard_score is not None or percentile is not None:
            raise ValueError("표준점수와 백분위는 모의고사에만 입력할 수 있습니다.")
    else:
        exam_month = _integer(payload, "exam_month", minimum=1, maximum=12)
        if achievement_level or subject_average is not None or standard_deviation is not None or cohort_size is not None:
            raise ValueError("학교 성적 지표는 학교 내신에만 입력할 수 있습니다.")

    return {
        "tenant": tenant,
        "student": student,
        "submitted_by": user,
        "source": source,
        "academic_year": academic_year,
        "semester": semester,
        "exam_round": exam_round,
        "exam_name": exam_name,
        "exam_month": exam_month,
        "exam_date": exam_date,
        "subject": subject,
        "score": score,
        "max_score": max_score,
        "standard_score": standard_score,
        "percentile": percentile,
        "grade_rank": grade_rank,
        "grade_scale": grade_scale,
        "achievement_level": achievement_level,
        "subject_average": subject_average,
        "standard_deviation": standard_deviation,
        "cohort_size": cohort_size,
    }


def validate_student_score_submissions(
    *,
    tenant: Any,
    user: Any,
    student_ps: str,
    payload: Mapping[str, Any],
    student: Student | None = None,
) -> list[dict[str, Any]]:
    """Validate one report containing up to 20 subject rows with shared exam metadata."""
    if student is None:
        student = Student.objects.filter(
            tenant=tenant,
            user=user,
            ps_number=student_ps,
            deleted_at__isnull=True,
        ).first()
    elif (
        student.tenant_id != tenant.id
        or student.ps_number != student_ps
        or student.deleted_at is not None
    ):
        student = None
    if not student:
        raise ValueError("학생 본인만 자기 성적표를 제출할 수 있습니다.")

    raw_items = str(payload.get("score_items") or "").strip()
    if not raw_items:
        item_payloads: list[Mapping[str, Any]] = [payload]
    else:
        try:
            parsed_items = json.loads(raw_items)
        except (TypeError, ValueError):
            raise ValueError("score_items must be valid JSON") from None
        if not isinstance(parsed_items, list) or not parsed_items:
            raise ValueError("score_items must be a non-empty array")
        if len(parsed_items) > 20:
            raise ValueError("한 성적표에는 최대 20개 과목을 제출할 수 있습니다.")
        if not all(isinstance(item, dict) for item in parsed_items):
            raise ValueError("score_items entries must be objects")
        if any(set(item).difference(SCORE_ITEM_FIELDS) for item in parsed_items):
            raise ValueError("score_items에는 과목별 성적 항목만 입력할 수 있습니다.")
        common_payload = {key: payload.get(key) for key in payload}
        item_payloads = [
            {**common_payload, **{key: item.get(key) for key in SCORE_ITEM_FIELDS if key in item}}
            for item in parsed_items
        ]

    validated_rows = [
        validate_student_score_submission(
            tenant=tenant,
            user=user,
            student_ps=student_ps,
            payload=item_payload,
            student=student,
        )
        for item_payload in item_payloads
    ]
    normalized_subjects = [row["subject"].casefold() for row in validated_rows]
    if len(normalized_subjects) != len(set(normalized_subjects)):
        raise ValueError("같은 성적표에 동일한 과목을 두 번 입력할 수 없습니다.")
    return validated_rows


def _validate_evidence_link(*, evidence_file: Any, validated_rows: list[Mapping[str, Any]]) -> None:
    if not validated_rows:
        raise ValueError("제출할 과목 성적이 없습니다.")
    first = validated_rows[0]
    tenant = first.get("tenant")
    student = first.get("student")
    submitted_by = first.get("submitted_by")
    if (
        not tenant
        or not student
        or evidence_file.tenant_id != tenant.id
        or student.tenant_id != tenant.id
        or evidence_file.scope != "student"
        or evidence_file.student_ps != student.ps_number
        or submitted_by is None
        or (
            student.user_id != submitted_by.id
            and getattr(student.parent, "user_id", None) != submitted_by.id
        )
        or any(
            row.get("tenant") != tenant
            or row.get("student") != student
            or row.get("submitted_by") != submitted_by
            for row in validated_rows
        )
    ):
        raise ValueError("성적표 원본과 학생·학원 정보가 일치하지 않습니다.")


@transaction.atomic
def create_student_score_submissions(
    *,
    evidence_file: Any,
    validated_rows: list[Mapping[str, Any]],
) -> list[StudentReportedScore]:
    _validate_evidence_link(evidence_file=evidence_file, validated_rows=validated_rows)
    return [
        StudentReportedScore.objects.create(evidence_file=evidence_file, **dict(validated))
        for validated in validated_rows
    ]


def create_student_score_submission(*, evidence_file: Any, validated: Mapping[str, Any]) -> StudentReportedScore:
    """Compatibility entry for the legacy single-subject caller."""
    return create_student_score_submissions(
        evidence_file=evidence_file,
        validated_rows=[validated],
    )[0]


def score_pct(row: StudentReportedScore | Mapping[str, Any]) -> float | None:
    score = row.score if isinstance(row, StudentReportedScore) else row.get("score")
    max_score = row.max_score if isinstance(row, StudentReportedScore) else row.get("max_score")
    try:
        score_value = float(score)
        max_value = float(max_score)
    except (TypeError, ValueError, OverflowError):
        return None
    if not isfinite(score_value) or not isfinite(max_value) or score_value < 0 or max_value <= 0:
        return None
    return round((score_value / max_value) * 100, 1)


def reported_score_label(row: StudentReportedScore | Mapping[str, Any]) -> str:
    get = (lambda key: getattr(row, key)) if isinstance(row, StudentReportedScore) else row.get
    year = get("academic_year")
    source = get("source")
    if source == StudentReportedScore.Source.SCHOOL_EXAM:
        semester = get("semester")
        exam_round = get("exam_round")
        round_label = {
            StudentReportedScore.ExamRound.FIRST: "1차 지필평가(중간)",
            StudentReportedScore.ExamRound.SECOND: "2차 지필평가(기말)",
            StudentReportedScore.ExamRound.PERFORMANCE: get("exam_name") or "수행평가",
            StudentReportedScore.ExamRound.OTHER: get("exam_name") or "기타 학교 평가",
        }.get(exam_round, "학교 평가")
        return f"{year}년 {semester}학기 {round_label}"
    month = get("exam_month")
    if source == StudentReportedScore.Source.KICE_MOCK:
        return f"{year}년 {month}월 평가원 모의평가"
    return f"{year}년 {month}월 전국연합학력평가"


def serialize_reported_score(row: StudentReportedScore | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(row, StudentReportedScore):
        values = {
            "id": row.id,
            "student_id": row.student_id,
            "source": row.source,
            "academic_year": row.academic_year,
            "semester": row.semester,
            "exam_round": row.exam_round,
            "exam_name": row.exam_name,
            "exam_month": row.exam_month,
            "exam_date": row.exam_date,
            "subject": row.subject,
            "score": row.score,
            "max_score": row.max_score,
            "standard_score": row.standard_score,
            "percentile": row.percentile,
            "grade_rank": row.grade_rank,
            "grade_scale": row.grade_scale,
            "achievement_level": row.achievement_level,
            "subject_average": row.subject_average,
            "standard_deviation": row.standard_deviation,
            "cohort_size": row.cohort_size,
            "status": row.status,
            "review_note": row.review_note,
            "evidence_file_id": row.evidence_file_id,
            "evidence_r2_key": row.evidence_file.r2_key if row.evidence_file else "",
            "created_at": row.created_at,
            "reviewed_at": row.reviewed_at,
        }
    else:
        values = dict(row)
    return {
        "id": int(values["id"]),
        "student_id": int(values["student_id"]),
        "source": values["source"],
        "source_group": "school" if values["source"] in SCHOOL_SOURCES else "mock",
        "label": reported_score_label(values),
        "academic_year": int(values["academic_year"]),
        "semester": values.get("semester"),
        "exam_round": values.get("exam_round") or None,
        "exam_name": values.get("exam_name") or None,
        "exam_month": values.get("exam_month"),
        "exam_date": values.get("exam_date").isoformat() if values.get("exam_date") else None,
        "subject": values["subject"],
        "score": float(values["score"]),
        "max_score": float(values["max_score"]),
        "score_pct": score_pct(values),
        "standard_score": float(values["standard_score"]) if values.get("standard_score") is not None else None,
        "percentile": float(values["percentile"]) if values.get("percentile") is not None else None,
        "grade_rank": values.get("grade_rank"),
        "grade_scale": values.get("grade_scale") or None,
        "achievement_level": values.get("achievement_level") or None,
        "subject_average": float(values["subject_average"]) if values.get("subject_average") is not None else None,
        "standard_deviation": float(values["standard_deviation"]) if values.get("standard_deviation") is not None else None,
        "cohort_size": values.get("cohort_size"),
        "status": values["status"],
        "review_note": values.get("review_note") or "",
        "evidence_file_id": int(values["evidence_file_id"]) if values.get("evidence_file_id") else None,
        "evidence_r2_key": values.get("evidence_r2_key") or "",
        "created_at": values.get("created_at").isoformat() if values.get("created_at") else None,
        "reviewed_at": values.get("reviewed_at").isoformat() if values.get("reviewed_at") else None,
    }


def score_submission_map_for_inventory_files(*, tenant: Any, file_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    if not file_ids:
        return {}
    rows = StudentReportedScore.objects.filter(
        tenant=tenant,
        evidence_file_id__in=file_ids,
    ).select_related("evidence_file").order_by("evidence_file_id", "id")
    output: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        output.setdefault(row.evidence_file_id, []).append(serialize_reported_score(row))
    return output


def inventory_file_has_reported_score(*, tenant: Any, file_id: int) -> bool:
    return StudentReportedScore.objects.filter(
        tenant=tenant,
        evidence_file_id=file_id,
        status__in=(StudentReportedScore.Status.PENDING, StudentReportedScore.Status.VERIFIED),
    ).exists()


def inventory_files_have_reported_score(*, tenant: Any, file_ids: list[int]) -> bool:
    if not file_ids:
        return False
    return StudentReportedScore.objects.filter(
        tenant=tenant,
        evidence_file_id__in=file_ids,
        status__in=(StudentReportedScore.Status.PENDING, StudentReportedScore.Status.VERIFIED),
    ).exists()


def inventory_files_have_any_reported_score(*, tenant: Any, file_ids: list[int]) -> bool:
    """Use for bulk/overwrite paths that must route score evidence through audited single delete."""
    if not file_ids:
        return False
    return StudentReportedScore.objects.filter(
        tenant=tenant,
        evidence_file_id__in=file_ids,
    ).exists()


def recover_reported_score_canary(
    *,
    tenant: Any,
    marker: str,
    wait_seconds: int = 20,
) -> None:
    """Terminalize and remove one exact canary report after an ambiguous upload."""
    # Lazy import avoids the inventory services ↔ score protection support cycle.
    from apps.domains.inventory.services import delete_object_r2_storage

    def marker_rows():
        return StudentReportedScore.objects.filter(
            tenant=tenant,
            exam_name=marker,
            subject__startswith=marker,
        )

    deadline = time.monotonic() + max(0, min(wait_seconds, 60))
    while not marker_rows().exists() and time.monotonic() < deadline:
        time.sleep(1)
    if not marker_rows().exists():
        raise ValueError(
            "exact marker was not found; cleanup cannot be proven after an ambiguous upload"
        )

    with transaction.atomic():
        student_ids = list(marker_rows().values_list("student_id", flat=True).distinct())
        list(
            Student.objects.select_for_update()
            .filter(tenant=tenant, id__in=student_ids)
            .values_list("id", flat=True)
        )
        locked_rows = list(marker_rows().select_for_update().order_by("id"))
        evidence_ids = {row.evidence_file_id for row in locked_rows if row.evidence_file_id}
        if evidence_ids and StudentReportedScore.objects.filter(
            evidence_file_id__in=evidence_ids,
        ).exclude(id__in=[row.id for row in locked_rows]).exists():
            raise ValueError("canary evidence is linked to non-marker score rows")
        now = timezone.now()
        for row in locked_rows:
            if row.status == StudentReportedScore.Status.PENDING:
                row.status = StudentReportedScore.Status.REJECTED
            elif row.status == StudentReportedScore.Status.VERIFIED:
                row.status = StudentReportedScore.Status.VOIDED
            else:
                continue
            row.reviewed_at = now
            row.review_note = f"운영 canary 복구 정리 {marker}"
            row.save(update_fields=["status", "reviewed_at", "review_note", "updated_at"])

    for evidence in InventoryFile.objects.filter(tenant=tenant, id__in=evidence_ids):
        try:
            delete_object_r2_storage(key=evidence.r2_key)
        except Exception as exc:
            raise ValueError(f"R2 cleanup failed for evidence={evidence.id}: {exc}") from exc
        evidence.delete()


@transaction.atomic
def review_student_scores(
    *,
    tenant: Any,
    score_id: int,
    action: str,
    reviewed_by: Any,
    review_note: str = "",
    review_all_evidence: bool = False,
    grade_scale_confirmed: bool = False,
) -> list[StudentReportedScore] | None:
    if action not in ("verify", "reject", "void"):
        raise ValueError("action must be verify, reject or void")
    normalized_note = (review_note or "").strip()[:300]
    if action == "void" and not normalized_note:
        raise ValueError("통계 제외 사유를 입력해야 합니다.")

    expected_status = (
        StudentReportedScore.Status.VERIFIED
        if action == "void"
        else StudentReportedScore.Status.PENDING
    )

    target_reference = StudentReportedScore.objects.filter(
        tenant=tenant,
        id=score_id,
    ).values("student_id").first()
    if target_reference is None:
        return None

    # 모든 검수는 학생 행 → 성적 행 순서로 잠근다. 같은 학생의 서로 다른
    # 정정본을 동시에 처리해도 중복 승인과 교착을 함께 방지한다.
    Student.objects.select_for_update().filter(
        tenant=tenant,
        id=target_reference["student_id"],
    ).exists()
    target = StudentReportedScore.objects.select_for_update().filter(
        tenant=tenant,
        id=score_id,
        student_id=target_reference["student_id"],
    ).values("student_id", "evidence_file_id", "status").first()
    if target is None:
        return None
    if target["status"] != expected_status:
        raise ReportedScoreTransitionConflict(
            "확인 완료된 성적만 통계에서 제외할 수 있습니다."
            if action == "void"
            else "검토 대기 중인 성적만 승인하거나 반려할 수 있습니다."
        )
    row_query = (
        StudentReportedScore.objects.select_for_update(of=("self",))
        .select_related("evidence_file")
        .filter(tenant=tenant, student_id=target["student_id"])
    )
    if review_all_evidence and target["evidence_file_id"]:
        # 한 원본 안에서 일부 과목이 이미 처리된 경우에도 남은 같은 상태의
        # 과목만 묶어 처리한다. 이미 승인/반려된 행을 다시 쓰지는 않는다.
        row_query = row_query.filter(
            evidence_file_id=target["evidence_file_id"],
            status=expected_status,
        )
    else:
        row_query = row_query.filter(id=score_id, status=expected_status)
    rows = list(row_query.order_by("id"))
    if not rows:
        return None

    if action == "verify" and any(row.grade_rank is not None for row in rows) and not grade_scale_confirmed:
        raise ValueError("성적표 원본에서 등급 체계를 확인해 주세요.")

    now = timezone.now()
    for row in rows:
        if action == "verify":
            replacement_identity = {
                "tenant": tenant,
                "student": row.student,
                "source": row.source,
                "academic_year": row.academic_year,
                "semester": row.semester,
                "exam_round": row.exam_round,
                "exam_name": row.exam_name,
                "exam_month": row.exam_month,
                "subject": row.subject,
                "status": StudentReportedScore.Status.VERIFIED,
            }
            if row.exam_round in (
                StudentReportedScore.ExamRound.PERFORMANCE,
                StudentReportedScore.ExamRound.OTHER,
            ):
                replacement_identity["exam_date"] = row.exam_date
            StudentReportedScore.objects.filter(
                **replacement_identity,
            ).exclude(id=row.id).update(
                status=StudentReportedScore.Status.REJECTED,
                reviewed_by=reviewed_by,
                reviewed_at=now,
                review_note="새로 확인된 성적표로 대체됨",
            )
            row.status = StudentReportedScore.Status.VERIFIED
        elif action == "reject":
            row.status = StudentReportedScore.Status.REJECTED
        else:
            row.status = StudentReportedScore.Status.VOIDED
        row.reviewed_by = reviewed_by
        row.reviewed_at = now
        row.review_note = normalized_note
        row.save(update_fields=["status", "reviewed_by", "reviewed_at", "review_note", "updated_at"])
    return rows


def review_student_score(
    *,
    tenant: Any,
    score_id: int,
    action: str,
    reviewed_by: Any,
    review_note: str = "",
    grade_scale_confirmed: bool = False,
) -> StudentReportedScore | None:
    """Compatibility entry for a single score-row review."""
    rows = review_student_scores(
        tenant=tenant,
        score_id=score_id,
        action=action,
        reviewed_by=reviewed_by,
        review_note=review_note,
        grade_scale_confirmed=grade_scale_confirmed,
    )
    return rows[0] if rows else None
