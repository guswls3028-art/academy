"""Keep the confirmed first-attempt projection aligned with its current result."""

from __future__ import annotations

from typing import Any


def sync_confirmed_initial_snapshot_meta(
    *,
    attempt: Any,
    meta: Any,
    total_score: float,
    max_score: float,
    confirmed_at: Any,
    fallback_source: str,
) -> dict[str, Any]:
    """Synchronize attempt 1 while preserving its original OMR provenance."""
    normalized = dict(meta or {}) if isinstance(meta, dict) else {}
    if int(getattr(attempt, "attempt_index", 0) or 0) != 1:
        return normalized

    existing = normalized.get("initial_snapshot")
    snapshot = dict(existing) if isinstance(existing, dict) else {}
    snapshot["total_score"] = float(total_score)
    snapshot["max_score"] = float(max_score)
    if not snapshot.get("submitted_at"):
        snapshot["submitted_at"] = confirmed_at.isoformat()
    if not snapshot.get("source"):
        snapshot["source"] = fallback_source
    normalized["initial_snapshot"] = snapshot
    return normalized
