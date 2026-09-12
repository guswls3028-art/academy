"""Protocol2: exact-session receipts, audit, counters and terminal state commit together."""
import hashlib
import json

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from academy.adapters.db.django import repositories_video as video_repo
from academy.application.use_cases.student_video_access_context import lecture_allows_student_learning
from apps.domains.video.models import AccessMode, Video, VideoPlaybackEvent, VideoPlaybackEventBatch, VideoPlaybackSession
from apps.domains.video.services.access_resolver import get_effective_access_mode
from apps.domains.video.services.playback_policy import build_effective_playback_policy
from apps.domains.video.services.playback_session import get_tenant_session_limits, should_revoke_by_stats


class PlaybackBatchError(Exception):
    def __init__(self, detail, status_code=409):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def normalized_batch_bytes(batch):
    # Token/renewal and server-assigned timestamps must not change a retry's identity.
    return json.dumps(
        {"batch_id": str(batch["batch_id"]), "events": [
            {"type": event["type"], "occurred_at": event.get("occurred_at"), "payload": event.get("payload", {})}
            for event in batch["events"]
        ]}, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"),
    ).encode("utf-8")


def _exact_sessions(*, tenant_id, student_id, payload):
    return VideoPlaybackSession.objects.filter(
        session_id=payload.get("session_id"), video_id=payload.get("video_id"),
        enrollment_id=payload.get("enrollment_id"), video__tenant_id=tenant_id,
        video__session__lecture__tenant_id=tenant_id,
        enrollment__tenant_id=tenant_id, enrollment__student_id=student_id,
        enrollment__student__tenant_id=tenant_id,
        enrollment__lecture_id=F("video__session__lecture_id"),
    )


def _lock_write_scope(reference, tenant_id, student_id, payload):
    """Acquire upper-scope locks before the playback row, as renewal/lecture close do."""
    if reference.enrollment.status == "INACTIVE":
        from .inactive_entitlements import InactiveVideoEntitlementError, lock_and_revalidate_inactive_video_write_access

        try:
            locked = lock_and_revalidate_inactive_video_write_access(
                tenant_id=tenant_id, enrollment_id=reference.enrollment_id,
                video_id=reference.video_id, expected_policy_version=payload.get("pv"),
            )
        except InactiveVideoEntitlementError as exc:
            raise PlaybackBatchError("policy_changed", 403) from exc
        return locked.video, locked.enrollment
    lecture, lesson, enrollment, video = video_repo.lock_active_playback_renewal_scope(
        tenant_id=tenant_id, lecture_id=reference.video.session.lecture_id,
        session_id=reference.video.session_id, enrollment_id=reference.enrollment_id,
        student_id=student_id, video_id=reference.video_id,
    )
    if lecture is None or lesson is None or enrollment is None or video is None:
        raise PlaybackBatchError("policy_changed", 403)
    return video, enrollment


def _policy(video, enrollment, payload):
    if (
        not lecture_allows_student_learning(video.session.lecture)
        or video.policy_version != payload.get("pv")
        or payload.get("access_mode") != AccessMode.PROCTORED_CLASS.value
        or get_effective_access_mode(video=video, enrollment=enrollment) != AccessMode.PROCTORED_CLASS
        or (video.visibility != Video.Visibility.PUBLIC and not video_repo.session_enrollment_exists(video.session, enrollment))
    ):
        raise PlaybackBatchError("policy_changed", 403)
    policy = build_effective_playback_policy(
        video=video, access_mode=AccessMode.PROCTORED_CLASS,
        permission=video_repo.video_access_get(video, enrollment),
        progress=video_repo.video_progress_get(video, enrollment),
    )
    max_sessions, max_devices = get_tenant_session_limits(video.tenant)
    policy["concurrency"] = {"max_sessions": max_sessions, "max_devices": max_devices}
    return policy


def _violation(event_type, policy):
    if event_type == "SEEK_ATTEMPT":
        mode = (policy.get("seek") or {}).get("mode")
        if not policy.get("allow_seek", True) or mode in ("blocked", "bounded_forward", "budgeted_forward"):
            return True, f"seek_{mode or 'blocked'}"
    if event_type == "SPEED_CHANGE_ATTEMPT":
        rate = policy.get("playback_rate") or {}
        if not rate.get("ui_control", True) or float(rate.get("max", 1.0) or 1.0) <= 1.0:
            return True, "speed_blocked"
    return False, ""


@transaction.atomic
def apply_event_batches(*, tenant_id, user_id, student_id, payload, batches, finalize=False):
    if (
        payload.get("event_protocol_version") != 2
        or payload.get("monitoring_enabled") is not True
        or payload.get("tenant_id") != tenant_id
        or payload.get("user_id") != user_id
        or payload.get("student_id") != student_id
    ):
        raise PlaybackBatchError("event_protocol_scope_mismatch", 403)
    prepared = []
    for batch in batches:
        encoded = normalized_batch_bytes(batch)
        if not 1 <= len(batch["events"]) <= 50 or len(encoded) > 8 * 1024:
            raise PlaybackBatchError("batch_too_large", 413)
        prepared.append((batch, hashlib.sha256(encoded).hexdigest()))
    ids = [batch["batch_id"] for batch in batches]
    if len(ids) != len(set(ids)) or len(ids) > 8 or sum(len(batch["events"]) for batch in batches) > 200:
        raise PlaybackBatchError("batch_too_large", 413)

    sessions = _exact_sessions(tenant_id=tenant_id, student_id=student_id, payload=payload)
    reference = sessions.select_related("video__session", "enrollment").first()
    if reference is None:
        raise PlaybackBatchError("session_scope_mismatch", 403)
    if reference.event_protocol_version != 2:
        raise PlaybackBatchError("event_protocol_mismatch")
    # Known receipts / empty disposal need no policy-derived writes. Never take upper
    # locks after this branch takes the session lock (important for lecture close).
    all_known = reference.event_batches.filter(batch_id__in=ids).count() == len(ids)
    video = enrollment = None
    if not all_known:
        video, enrollment = _lock_write_scope(reference, tenant_id, student_id, payload)
    session = sessions.select_for_update(of=("self",)).filter(pk=reference.pk).first()
    if session is None:
        raise PlaybackBatchError("session_scope_mismatch", 403)
    if session.event_protocol_version != 2:
        raise PlaybackBatchError("event_protocol_mismatch")
    receipts = {r.batch_id: r for r in session.event_batches.filter(batch_id__in=ids)}
    new_batches = []
    acknowledgements = []
    for batch, fingerprint in prepared:
        receipt = receipts.get(batch["batch_id"])
        if receipt is not None and receipt.payload_sha256 != fingerprint:
            raise PlaybackBatchError("batch_payload_conflict")
        if receipt is None:
            new_batches.append((batch, fingerprint))
        acknowledgements.append({
            "batch_id": batch["batch_id"], "event_count": len(batch["events"]), "duplicate": receipt is not None,
        })
    now = timezone.now()
    active = session.status == VideoPlaybackSession.Status.ACTIVE and not session.is_revoked
    expired = session.expires_at is not None and session.expires_at <= now
    if new_batches and (not active or expired):
        raise PlaybackBatchError("session_inactive")
    if finalize and active and expired:
        raise PlaybackBatchError("session_inactive")
    inserted = violated_count = 0
    if new_batches:
        policy = _policy(video, enrollment, payload)
        rows = []
        for batch, fingerprint in new_batches:
            batch_violations = 0
            for event in batch["events"]:
                violated, reason = _violation(event["type"], policy)
                batch_violations += int(violated)
                rows.append(VideoPlaybackEvent(
                    video_id=session.video_id, enrollment_id=session.enrollment_id,
                    session_id=session.session_id, user_id=user_id,
                    event_type=event["type"], event_payload=event.get("payload", {}),
                    policy_snapshot=policy, violated=violated, violation_reason=reason, occurred_at=now,
                ))
            VideoPlaybackEventBatch.objects.create(
                playback_session=session, batch_id=batch["batch_id"], payload_sha256=fingerprint,
                event_count=len(batch["events"]), violated_count=batch_violations,
            )
            violated_count += batch_violations
        video_repo.playback_event_bulk_create(rows, batch_size=200)
        inserted = len(rows)
        session.total_count += inserted
        session.violated_count += violated_count
        if should_revoke_by_stats(violated=session.violated_count, total=session.total_count):
            session.status = VideoPlaybackSession.Status.REVOKED
            session.is_revoked = True
            session.ended_at = now
    if finalize and session.status == VideoPlaybackSession.Status.ACTIVE and not session.is_revoked:
        session.status = VideoPlaybackSession.Status.ENDED
        session.ended_at = now
    if inserted or (finalize and active):
        session.last_seen = now
        session.save(update_fields=["total_count", "violated_count", "status", "is_revoked", "ended_at", "last_seen", "updated_at"])
    return {
        "protocol_version": 2, "session_status": session.status,
        "inserted_count": inserted, "acknowledgements": acknowledgements,
    }
