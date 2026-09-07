"""Cross-domain helpers used by clinic session adapters/services.

The clinic Django CRUD layer should not import other domain internals directly.
This support module is the compatibility boundary while the broader cutover is
in progress.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable
from uuid import uuid4


def empty_lecture_queryset():
    from apps.domains.lectures.models import Lecture

    return Lecture.objects.none()


def empty_enrollment_queryset():
    from apps.domains.enrollment.models import Enrollment

    return Enrollment.objects.none()


def lectures_for_tenant(tenant):
    from apps.domains.lectures.models import Lecture

    return Lecture.objects.filter(tenant=tenant)


def sections_for_tenant(tenant):
    from apps.domains.lectures.models import Section

    return Section.objects.filter(tenant=tenant)


def enrollments_for_clinic_tenant(tenant):
    from apps.domains.enrollment.selectors import enrollments_for_tenant

    return enrollments_for_tenant(tenant)


def clinic_enrollment_for_tenant(tenant, enrollment_id: int | None):
    if not enrollment_id:
        return None

    from apps.domains.enrollment.selectors import enrollments_for_tenant

    return enrollments_for_tenant(tenant).filter(id=enrollment_id).first()


def active_enrolled_lecture_ids_for_student(tenant, student) -> set[int]:
    from apps.domains.enrollment.selectors import enrollments_for_tenant

    return set(
        enrollments_for_tenant(tenant)
        .filter(student=student, status="ACTIVE")
        .values_list("lecture_id", flat=True)
    )


def preferred_active_enrollment_id_for_student_session(
    tenant,
    student,
    session=None,
    *,
    preferred_enrollment_id: int | None = None,
) -> int | None:
    """Choose the active enrollment that owns this clinic booking.

    A student can have multiple active enrollments. Prefer an unresolved clinic
    target and respect any lecture restriction on the destination session so a
    booking never points at the student's latest unrelated course.
    """
    from apps.domains.enrollment.selectors import enrollments_for_tenant
    from apps.domains.progress.models import ClinicLink

    enrollments = enrollments_for_tenant(tenant).filter(
        student=student,
        status="ACTIVE",
    )
    if session is not None:
        target_lecture_ids = list(
            session.target_lectures.values_list("id", flat=True)
        )
        if target_lecture_ids:
            enrollments = enrollments.filter(lecture_id__in=target_lecture_ids)

    candidate_ids = list(enrollments.values_list("id", flat=True))
    if not candidate_ids:
        return None

    preferred_id = (
        int(preferred_enrollment_id)
        if preferred_enrollment_id in candidate_ids
        else None
    )
    unresolved = ClinicLink.objects.filter(
        tenant=tenant,
        enrollment_id__in=candidate_ids,
        is_auto=True,
        resolved_at__isnull=True,
    )
    if preferred_id and unresolved.filter(enrollment_id=preferred_id).exists():
        return preferred_id

    unresolved_id = (
        unresolved.order_by("-created_at", "-id")
        .values_list("enrollment_id", flat=True)
        .first()
    )
    if unresolved_id:
        return int(unresolved_id)
    if preferred_id:
        return preferred_id

    enrollment_id = (
        enrollments.order_by("-enrolled_at", "-id")
        .values_list("id", flat=True)
        .first()
    )
    return int(enrollment_id) if enrollment_id else None


def active_students_for_clinic_tenant(tenant):
    from apps.domains.students.selectors import students_for_tenant

    return students_for_tenant(tenant, deleted="active")


def get_student_for_clinic_request(request):
    from apps.domains.student_app.permissions import get_request_student

    return get_request_student(request)


def clinic_highlight_map_for_enrollments(
    *,
    tenant,
    enrollment_ids: Iterable[int],
) -> dict[int, bool]:
    from apps.domains.results.utils.clinic_highlight import compute_clinic_highlight_map

    ids = {int(enrollment_id) for enrollment_id in enrollment_ids if enrollment_id}
    return compute_clinic_highlight_map(
        tenant=tenant,
        enrollment_ids=ids,
    ) if ids else {}


def clinic_reasons_for_unresolved_auto_links(
    tenant,
    enrollment_ids: Iterable[int],
) -> dict[int, str | None]:
    from apps.domains.progress.models import ClinicLink
    from apps.domains.results.utils.clinic import filter_current_clinic_links

    ids = {int(enrollment_id) for enrollment_id in enrollment_ids if enrollment_id}
    if not ids:
        return {}

    links = filter_current_clinic_links(
        ClinicLink.objects.filter(
            tenant=tenant,
            enrollment_id__in=ids,
            is_auto=True,
            resolved_at__isnull=True,
        ).order_by("id"),
        tenant=tenant,
    )
    source_types_by_enrollment: dict[int, set[str]] = {
        enrollment_id: set() for enrollment_id in ids
    }
    for link in links:
        if link.source_type in {"exam", "homework"}:
            source_types_by_enrollment[int(link.enrollment_id)].add(link.source_type)

    reasons: dict[int, str | None] = {}
    for enrollment_id, source_types in source_types_by_enrollment.items():
        if {"exam", "homework"}.issubset(source_types):
            reasons[enrollment_id] = "both"
        elif "exam" in source_types:
            reasons[enrollment_id] = "exam"
        elif "homework" in source_types:
            reasons[enrollment_id] = "homework"
        else:
            reasons[enrollment_id] = None
    return reasons


def clinic_reason_for_unresolved_auto_links(tenant, enrollment_id: int | None) -> str | None:
    if not enrollment_id:
        return None
    return clinic_reasons_for_unresolved_auto_links(
        tenant,
        [enrollment_id],
    ).get(int(enrollment_id))


def storage_presigned_get_url(r2_key: str, *, expires_in: int = 3600) -> str:
    from django.conf import settings
    from academy.adapters.storage.r2_presign import create_presigned_get_url

    return create_presigned_get_url(
        r2_key,
        expires_in=expires_in,
        bucket=settings.R2_STORAGE_BUCKET,
    )


def send_clinic_session_reminder(*, session_id: int):
    return send_clinic_reminder_for_students(session_id=session_id)


def _dispatched_clinic_reminder_student_ids(*, tenant_id: int, session_id: int) -> set[int]:
    """Return students already represented by the durable automatic outbox."""
    from apps.domains.messaging.models import ScheduledNotification

    origin_id = f"clinic_session:{int(session_id)}:reminder"
    payloads = ScheduledNotification.objects.filter(
        tenant_id=int(tenant_id),
        trigger="clinic_reminder",
        origin_id=origin_id,
    ).values_list("payload", flat=True)
    student_ids: set[int] = set()
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        try:
            student_ids.add(int(payload.get("target_id")))
        except (TypeError, ValueError):
            continue
    return student_ids


def _clinic_reminder_context(*, session, domain_object_id: str, source_use_case: str, actor_id=None) -> dict:
    context = {
        "클리닉명": (session.title or "클리닉").strip(),
        "장소": session.location or "",
        "날짜": session.date.isoformat() if session.date else "",
        "시간": session.start_time.strftime("%H:%M") if session.start_time else "",
        "_domain_object_id": domain_object_id,
        "_source_domain": "clinic",
        "_source_use_case": source_use_case,
    }
    if actor_id:
        context["_actor_id"] = str(actor_id)
    return context


def cancel_pending_clinic_participant_reminders(*, tenant_id: int, participant_id: int) -> int:
    """Cancel only future manual reminders for the exact clinic participant."""
    from django.db import transaction

    from apps.domains.messaging.models import ScheduledNotification
    from apps.domains.messaging.security import redact_terminal_delivery_payload

    prefix = f"clinic_participant:{int(participant_id)}:manual_reminder:"
    with transaction.atomic():
        rows = list(
            ScheduledNotification.objects.select_for_update().filter(
                tenant_id=int(tenant_id),
                trigger="clinic_reminder",
                status=ScheduledNotification.Status.PENDING,
                origin_id__startswith=prefix,
            )
        )
        for row in rows:
            row.status = ScheduledNotification.Status.CANCELLED
            row.next_attempt_at = None
            row.payload = redact_terminal_delivery_payload(
                trigger=row.trigger,
                payload=row.payload,
            )
            row.error_message = "clinic_participant_closed"
            row.save(update_fields=["status", "next_attempt_at", "payload", "error_message"])
    return len(rows)


def deactivate_resolved_clinic_link_plan_items(
    *,
    clinic_link_ids,
    resolution_type: str,
    removed_at=None,
) -> int:
    """Audit-close active today-plan selections when their ClinicLink is resolved."""
    from django.utils import timezone

    from apps.domains.clinic.models import SessionParticipantPlanItem

    normalized_ids = sorted({int(link_id) for link_id in clinic_link_ids})
    if not normalized_ids:
        return 0
    return SessionParticipantPlanItem.objects.filter(
        clinic_link_id__in=normalized_ids,
        removed_at__isnull=True,
    ).update(
        removed_at=removed_at or timezone.now(),
        removal_reason=f"clinic_link_resolved:{resolution_type}"[:80],
    )


def locked_clinic_links_for_participant_plan(*, clinic_link_ids):
    """Return exact ClinicLinks in deterministic lock order for clinic planning."""
    from apps.domains.progress.models import ClinicLink

    return list(
        ClinicLink.objects.select_for_update()
        .filter(id__in=clinic_link_ids)
        .select_related("enrollment", "session")
        .order_by("id")
    )


def _clinic_recipient_targets(send_to: str) -> tuple[str, ...]:
    if send_to == "both":
        return ("parent", "student")
    if send_to in ("parent", "student"):
        return (send_to,)
    raise ValueError(f"unsupported clinic recipient target: {send_to!r}")


def send_clinic_reminder_for_participant(
    *,
    tenant_id: int,
    participant_id: int,
    actor_id: int | None = None,
    send_to: str = "student",
    repeat_interval_minutes: int | None = None,
    repeat_until=None,
    now=None,
):
    """Send one reminder now and optionally persist bounded same-day repeats."""
    from django.utils import timezone

    from apps.domains.clinic.services.lifecycle import build_clinic_reminder_send_times
    from apps.domains.clinic.models import SessionParticipant
    from apps.domains.messaging.services.notification_service import send_event_notification

    participant = (
        SessionParticipant.objects
        .select_related("student", "session", "tenant")
        .filter(
            id=int(participant_id),
            tenant_id=int(tenant_id),
            student__deleted_at__isnull=True,
        )
        .first()
    )
    if not participant or not participant.session:
        return {"status": "not_found", "sent": 0, "skipped": 1}
    if participant.status != SessionParticipant.Status.BOOKED:
        return {"status": "invalid_status", "sent": 0, "skipped": 1}

    current = timezone.localtime(now or timezone.now())
    targets = _clinic_recipient_targets(send_to)
    plan_key = f"{current.strftime('%Y%m%d%H%M%S%f')}:{uuid4().hex}"
    repeat_times = []
    if repeat_interval_minutes is not None or repeat_until is not None:
        if repeat_interval_minutes is None or repeat_until is None:
            return {"status": "invalid_schedule", "sent": 0, "scheduled": 0, "skipped": len(targets)}
        repeat_times = build_clinic_reminder_send_times(
            now=current,
            interval_minutes=repeat_interval_minutes,
            repeat_until=repeat_until,
        )

    sent = 0
    skipped = 0
    for target in targets:
        immediate_context = _clinic_reminder_context(
            session=participant.session,
            domain_object_id=(
                f"clinic_participant:{participant.id}:manual_reminder:"
                f"{plan_key}:now:{target}"
            ),
            source_use_case="clinic.manual_reminder",
            actor_id=actor_id,
        )
        if send_event_notification(
            tenant=participant.tenant,
            trigger="clinic_reminder",
            student=participant.student,
            send_to=target,
            context=immediate_context,
        ):
            sent += 1
        else:
            skipped += 1

    scheduled = 0
    for send_at in repeat_times:
        for target in targets:
            scheduled_context = _clinic_reminder_context(
                session=participant.session,
                domain_object_id=(
                    f"clinic_participant:{participant.id}:manual_reminder:"
                    f"{plan_key}:{send_at.strftime('%Y%m%d%H%M')}:{target}"
                ),
                source_use_case="clinic.manual_reminder_repeat",
                actor_id=actor_id,
            )
            if send_event_notification(
                tenant=participant.tenant,
                trigger="clinic_reminder",
                student=participant.student,
                send_to=target,
                context=scheduled_context,
                send_at=send_at,
            ):
                scheduled += 1
            else:
                skipped += 1
    result = {
        "status": "ok" if sent or scheduled else "delivery_failed",
        "sent": sent,
        "skipped": skipped,
    }
    if repeat_times:
        result["scheduled"] = scheduled
    return result


def send_clinic_reminder_for_students(*, session_id: int):
    """
    Send the clinic reminder Alimtalk for booked participants in one session.

    Clinic owns the session/participant selection. Messaging only owns the
    notification dispatch path.
    """
    from apps.domains.clinic.models import Session as ClinicSession, SessionParticipant
    from apps.domains.messaging.services.notification_service import send_event_notification

    session = (
        ClinicSession.objects
        .select_related("tenant")
        .filter(id=int(session_id))
        .first()
    )
    if not session:
        return {"status": "not_found", "message": "클리닉 세션을 찾을 수 없습니다."}

    participants = (
        SessionParticipant.objects
        .select_related("student")
        .filter(
            tenant_id=session.tenant_id,
            session_id=session.id,
            status=SessionParticipant.Status.BOOKED,
        )
    )

    context = _clinic_reminder_context(
        session=session,
        domain_object_id=f"clinic_session:{session.id}:reminder",
        source_use_case="clinic.reminder",
    )

    dispatched_student_ids = _dispatched_clinic_reminder_student_ids(
        tenant_id=session.tenant_id,
        session_id=session.id,
    )
    attempted = 0
    sent = 0
    deduplicated = 0
    for participant in participants:
        student = participant.student
        if not student:
            continue
        if int(student.id) in dispatched_student_ids:
            deduplicated += 1
            continue
        attempted += 1
        if send_event_notification(
            tenant=session.tenant,
            trigger="clinic_reminder",
            student=student,
            send_to="student",
            context=context,
        ):
            sent += 1

    return {
        "status": "ok",
        "attempted": attempted,
        "sent": sent,
        "skipped": max(0, attempted - sent),
        "deduplicated": deduplicated,
    }


def _send_due_range_clinic_reminders(*, session, minutes_before, current, window, dry_run):
    from django.utils import timezone

    from apps.domains.clinic.contracts import is_clinic_booking_reminder_active
    from apps.domains.clinic.models import SessionParticipant
    from apps.domains.messaging.models import ScheduledNotification
    from apps.domains.messaging.services.notification_service import send_event_notification

    stats = {"sessions_due": 0, "attempted": 0, "sent": 0, "skipped": 0, "deduplicated": 0}
    participants = SessionParticipant.objects.filter(
        tenant_id=session.tenant_id, session=session,
        student__tenant_id=session.tenant_id, student__deleted_at__isnull=True,
        status=SessionParticipant.Status.BOOKED,
        booking_start_time__isnull=False, booking_end_time__isnull=False,
        checked_out_at__isnull=True,
    ).select_related("student").order_by("booking_start_time", "id")
    # Preserve old accepted/attempted history; correcting timing never replays it.
    legacy_students = _dispatched_clinic_reminder_student_ids(
        tenant_id=session.tenant_id, session_id=session.id,
    )
    origins = set(ScheduledNotification.objects.filter(
        tenant_id=session.tenant_id, trigger="clinic_reminder",
        origin_id__startswith="clinic_booking:",
        created_at__gte=current - timedelta(days=2),
    ).values_list("origin_id", flat=True))
    for participant in participants:
        start = timezone.make_aware(datetime.combine(session.date, participant.booking_start_time))
        if not current - window <= start - timedelta(minutes=minutes_before) <= current:
            continue
        origin = f"clinic_booking:{participant.id}:{session.id}:{start:%Y%m%d:%H%M}"
        if not is_clinic_booking_reminder_active(
            tenant_id=session.tenant_id, origin_id=origin, now=current,
        ):
            continue
        if participant.student_id in legacy_students or origin in origins:
            stats["deduplicated"] += 1
            continue
        stats["attempted"] += 1
        stats["sessions_due"] = 1
        if dry_run:
            continue
        context = _clinic_reminder_context(
            session=session, domain_object_id=origin, source_use_case="clinic.booking_reminder",
        )
        context["시간"] = start.strftime("%H:%M")
        if send_event_notification(
            tenant=session.tenant, trigger="clinic_reminder", student=participant.student,
            send_to="student", context=context,
        ):
            stats["sent"] += 1
        else:
            stats["skipped"] += 1
    return stats


def send_due_clinic_reminders(
    *,
    now=None,
    tenant_id: int | None = None,
    window_minutes: int = 5,
    dry_run: bool = False,
) -> dict:
    """
    Send clinic reminders whose due time has arrived.

    Fixed slots use session start; time ranges use the exact participant start.
    The default window catches small scheduler delays while avoiding old sessions.
    """
    from django.db.models import Count, Q
    from django.utils import timezone

    from apps.domains.clinic.models import Session as ClinicSession, SessionParticipant
    from apps.domains.messaging.models import AutoSendConfig

    current = timezone.localtime(now or timezone.now())
    try:
        window = timedelta(minutes=max(0, int(window_minutes)))
    except (TypeError, ValueError):
        window = timedelta(minutes=5)

    configs = (
        AutoSendConfig.objects
        .filter(
            trigger="clinic_reminder",
            enabled=True,
            minutes_before__isnull=False,
            tenant__is_active=True,
        )
        .select_related("tenant")
        .order_by("tenant_id")
    )
    if tenant_id is not None:
        configs = configs.filter(tenant_id=int(tenant_id))

    stats = {
        "status": "ok",
        "dry_run": bool(dry_run),
        "configs": 0,
        "sessions_checked": 0,
        "sessions_due": 0,
        "attempted": 0,
        "sent": 0,
        "skipped": 0,
        "deduplicated": 0,
    }

    tz = timezone.get_current_timezone()
    for config in configs:
        stats["configs"] += 1
        try:
            minutes_before = int(config.minutes_before)
        except (TypeError, ValueError):
            stats["skipped"] += 1
            continue
        if minutes_before < 0:
            stats["skipped"] += 1
            continue

        earliest_start = current + timedelta(minutes=minutes_before) - window
        latest_start = current + timedelta(minutes=minutes_before)
        sessions = (
            ClinicSession.objects
            .filter(
                tenant_id=config.tenant_id,
                date__gte=earliest_start.date(),
                date__lte=latest_start.date(),
            )
            .annotate(
                booked_count=Count(
                    "participants",
                    filter=Q(participants__status=SessionParticipant.Status.BOOKED),
                    distinct=True,
                )
            )
            .filter(booked_count__gt=0)
            .order_by("date", "start_time", "id")
        )

        for session in sessions:
            stats["sessions_checked"] += 1
            if session.booking_mode == "time_range":
                range_stats = _send_due_range_clinic_reminders(
                    session=session, minutes_before=minutes_before, current=current,
                    window=window, dry_run=dry_run,
                )
                for key, value in range_stats.items():
                    stats[key] += value
                continue
            if not session.date or not session.start_time:
                stats["skipped"] += 1
                continue
            start_at = datetime.combine(session.date, session.start_time)
            if timezone.is_naive(start_at):
                start_at = timezone.make_aware(start_at, tz)
            start_at = timezone.localtime(start_at)
            due_at = start_at - timedelta(minutes=minutes_before)
            if not (current - window <= due_at <= current):
                continue
            if start_at < current:
                continue

            if dry_run:
                booked_student_ids = set(
                    SessionParticipant.objects.filter(
                        tenant_id=config.tenant_id,
                        session_id=session.id,
                        status=SessionParticipant.Status.BOOKED,
                    ).values_list("student_id", flat=True)
                )
                dispatched_student_ids = _dispatched_clinic_reminder_student_ids(
                    tenant_id=config.tenant_id,
                    session_id=session.id,
                )
                outstanding = booked_student_ids - dispatched_student_ids
                if outstanding:
                    stats["sessions_due"] += 1
                stats["attempted"] += len(outstanding)
                stats["deduplicated"] += len(
                    booked_student_ids & dispatched_student_ids
                )
                continue

            result = send_clinic_reminder_for_students(session_id=session.id)
            attempted = int(result.get("attempted") or 0)
            if attempted:
                stats["sessions_due"] += 1
            stats["attempted"] += attempted
            stats["sent"] += int(result.get("sent") or 0)
            stats["skipped"] += int(result.get("skipped") or 0)
            stats["deduplicated"] += int(result.get("deduplicated") or 0)

    return stats


def send_clinic_event_notification(*, tenant, trigger: str, student, send_to: str, context: dict):
    from apps.domains.messaging.services import send_event_notification

    return send_event_notification(
        tenant=tenant,
        trigger=trigger,
        student=student,
        send_to=send_to,
        context=context,
    )


def retry_failed_clinic_notification(*, tenant, participant, log_id: int, actor_id: int) -> dict:
    """Retry one exact failed clinic Alimtalk through the messaging boundary."""
    from apps.domains.messaging.models import ScheduledNotification
    from apps.domains.messaging.scheduled import dispatch_notification_now
    from apps.domains.messaging.selectors import notification_logs_for_business_tenant

    prefix = f"clinic_participant:{participant.id}:"
    log = notification_logs_for_business_tenant(tenant).filter(
        pk=log_id,
        message_mode__in=("alimtalk", ""),
        origin_id__startswith=prefix,
    ).first()
    if log is None:
        return {"result": "not_found", "detail": "재시도할 발송 기록을 찾을 수 없습니다."}
    if log.status not in {"failed", "retryable_failed"}:
        return {
            "result": "conflict",
            "detail": "확정 실패 또는 재시도 가능 실패만 다시 보낼 수 있습니다.",
        }
    if log.target_type not in {"student", "parent"} or str(log.target_id) != str(
        participant.student_id
    ):
        return {"result": "conflict", "detail": "발송 대상이 현재 학생과 일치하지 않습니다."}

    original = ScheduledNotification.objects.filter(
        tenant=tenant,
        business_idempotency_key=log.business_idempotency_key,
    ).order_by("id").first()
    if original is None or not isinstance(original.payload, dict):
        return {
            "result": "conflict",
            "detail": "원본 발송 자료를 확인할 수 없어 안전하게 재시도할 수 없습니다.",
        }
    payload = dict(original.payload)
    if str(payload.get("message_mode") or "").lower() != "alimtalk":
        return {"result": "conflict", "detail": "알림톡 원본만 재시도할 수 있습니다."}
    if str(payload.get("target_type") or "") != log.target_type or str(
        payload.get("target_id") or ""
    ) != str(participant.student_id):
        return {"result": "conflict", "detail": "원본 발송 대상이 현재 학생과 일치하지 않습니다."}

    retry_origin = f"{prefix}retry:{log.id}"
    outbox = ScheduledNotification.objects.filter(
        tenant=tenant,
        origin_id=retry_origin,
    ).order_by("id").first()
    if outbox is None:
        payload.update({
            "occurrence_key": f"clinic-retry:{log.id}",
            "domain_object_id": retry_origin,
            "origin_type": "clinic_notification_retry",
            "origin_id": retry_origin,
            "source_domain": "clinic",
            "source_use_case": "notification_retry",
            "actor_id": actor_id,
        })
        outbox = dispatch_notification_now(
            tenant_id=tenant.id,
            trigger=original.trigger,
            payload=payload,
        )
    return {
        "result": "accepted",
        "status": "accepted",
        "outbox_id": outbox.id,
        "origin_id": retry_origin,
    }


def unresolve_legacy_booking_links_for_session_delete(*, tenant, session) -> None:
    from django.db.models import Q
    from apps.domains.clinic.models import SessionParticipant
    from apps.domains.progress.models import ClinicLink
    from apps.domains.progress.services.clinic_resolution_service import ClinicResolutionService

    enrollment_ids = list(
        SessionParticipant.objects.filter(
            tenant=tenant,
            session=session,
            enrollment_id__isnull=False,
            status__in=[
                SessionParticipant.Status.BOOKED,
                SessionParticipant.Status.PENDING,
            ],
        ).values_list("enrollment_id", flat=True)
    )
    if not enrollment_ids:
        return

    target_lecture_ids = list(session.target_lectures.values_list("id", flat=True))
    link_filter = Q(
        tenant=tenant,
        enrollment_id__in=enrollment_ids,
        is_auto=True,
        resolution_type="BOOKING_LEGACY",
        resolved_at__isnull=False,
        session__lecture__tenant=tenant,
    )
    if target_lecture_ids:
        link_filter &= Q(session__lecture_id__in=target_lecture_ids)

    for link in ClinicLink.objects.filter(link_filter):
        ClinicResolutionService.unresolve(clinic_link_id=link.id)
