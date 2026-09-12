from __future__ import annotations

import re
from datetime import datetime

from django.utils import timezone

from apps.domains.clinic.models import SessionParticipant
from apps.domains.clinic.time_ranges import booking_window, session_window


def is_clinic_participant_reminder_active(*, participant_id: int, tenant_id: int) -> bool:
    """Return whether a manual reminder still belongs to an active booking."""
    return SessionParticipant.objects.filter(
        id=participant_id,
        tenant_id=tenant_id,
        status=SessionParticipant.Status.BOOKED,
        session__isnull=False,
        student__deleted_at__isnull=True,
    ).exists()


def is_clinic_booking_reminder_active(*, tenant_id: int, origin_id: str, now=None) -> bool:
    """Validate an exact time-range reminder again before queue/provider dispatch."""
    match = re.fullmatch(r"clinic_booking:(\d+):(\d+):(\d{8}):(\d{4})", origin_id)
    if not match:
        return False
    participant_id, session_id, date_text, time_text = match.groups()
    try:
        start = timezone.make_aware(datetime.strptime(date_text + time_text, "%Y%m%d%H%M"))
    except ValueError:
        return False
    if start <= (now or timezone.now()):
        return False
    participant = SessionParticipant.objects.select_related("session").filter(
        id=int(participant_id), tenant_id=tenant_id,
        session_id=int(session_id), session__tenant_id=tenant_id,
        session__date=start.date(), session__booking_mode="time_range",
        booking_start_time=start.time(), booking_end_time__isnull=False,
        status=SessionParticipant.Status.BOOKED,
        student__tenant_id=tenant_id, student__deleted_at__isnull=True,
        checked_out_at__isnull=True,
    ).first()
    if not participant:
        return False
    session = participant.session
    opening, closing = session_window(session)
    _booking_start, ending = booking_window(
        session=session,
        start_time=participant.booking_start_time,
        end_time=participant.booking_end_time,
    )
    return opening <= start.replace(tzinfo=None) < ending <= closing


__all__ = ["is_clinic_participant_reminder_active", "is_clinic_booking_reminder_active"]
