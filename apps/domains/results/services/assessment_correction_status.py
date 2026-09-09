"""Shared read contract for teacher-confirmed exam/homework correction state."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from apps.domains.results.models import Result


def _float_or_none(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def assessment_correction_payload(
    *,
    source_type: str,
    score: Optional[float],
    max_score: Optional[float],
    source_fingerprint: Optional[str],
    correction: Optional[Any],
) -> dict[str, Any]:
    note = correction.note if correction else ""
    if source_type == "homework" and correction:
        return {
            "correction_status": "COMPLETED" if correction.completed else "PENDING",
            "correction_completed_at": correction.completed_at if correction.completed else None,
            "correction_note": note,
            "correction_updated_at": correction.updated_at,
            "teacher_resolved": bool(correction.completed),
        }
    if score is None or max_score is None or max_score <= 0:
        return {
            "correction_status": None,
            "correction_completed_at": None,
            "correction_note": note,
            "correction_updated_at": correction.updated_at if correction else None,
            "teacher_resolved": False,
        }
    if score >= max_score:
        return {
            "correction_status": "NOT_REQUIRED",
            "correction_completed_at": None,
            "correction_note": note,
            "correction_updated_at": correction.updated_at if correction else None,
            "teacher_resolved": False,
        }
    is_current_completion = bool(
        correction
        and correction.completed
        and (
            not correction.source_fingerprint
            or correction.source_fingerprint == source_fingerprint
        )
    )
    return {
        "correction_status": "COMPLETED" if is_current_completion else "PENDING",
        "correction_completed_at": correction.completed_at if is_current_completion else None,
        "correction_note": note,
        "correction_updated_at": correction.updated_at if correction else None,
        "teacher_resolved": is_current_completion,
    }


def exam_correction_fingerprint(
    *,
    result: Result,
    items,
    current_max_score: float,
) -> str:
    """Meaningful current-policy result version, excluding timestamp rewrites."""
    payload = {
        "result_id": int(result.id),
        "attempt_id": int(result.attempt_id) if result.attempt_id else None,
        "total_score": _float_or_none(result.total_score),
        "max_score": _float_or_none(current_max_score),
        "objective_score": _float_or_none(result.objective_score),
        "items": [
            {
                "question_id": int(item.question_id),
                "answer": str(item.answer or ""),
                "is_correct": bool(item.is_correct),
                "include_in_wrong_note": bool(item.include_in_wrong_note),
                "score": _float_or_none(item.score),
                "max_score": _float_or_none(item.max_score),
            }
            for item in sorted(items, key=lambda candidate: (candidate.question_id, candidate.id))
        ],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
