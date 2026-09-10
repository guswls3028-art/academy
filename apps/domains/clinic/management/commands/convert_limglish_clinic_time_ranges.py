from __future__ import annotations

import datetime
import hashlib
import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.core.models import Tenant
from apps.domains.clinic.models import Session, SessionParticipant
from apps.domains.clinic.time_ranges import is_supported_time_range_session, session_window


TENANT_CODE = "limglish"
INTERVAL_MINUTES = 60
MAX_STAY_MINUTES = 600


def _parse_from_date(value: str) -> datetime.date:
    try:
        return datetime.date.fromisoformat(value)
    except ValueError as exc:
        raise CommandError("--from-date must use YYYY-MM-DD") from exc


def _confirmation_token(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(b"limglish-clinic-time-range-v1\0" + encoded).hexdigest()[:32]


def _exact_tenant(*, lock: bool):
    queryset = Tenant.objects
    if lock:
        queryset = queryset.select_for_update()
    matches = list(queryset.filter(code=TENANT_CODE))
    if len(matches) != 1:
        raise CommandError(f"tenant code {TENANT_CODE!r} must resolve exactly one row")
    return matches[0]


def _build_plan(*, tenant, from_date: datetime.date, lock: bool) -> dict:
    sessions = Session.objects.filter(tenant=tenant, date__gte=from_date).order_by("id")
    if lock:
        sessions = sessions.select_for_update()
    sessions = list(sessions)
    for session in sessions:
        if not is_supported_time_range_session(session):
            raise CommandError(
                f"session {session.id} ends 자정 이후; no rows were changed"
            )
        if session.duration_minutes % INTERVAL_MINUTES:
            raise CommandError(
                f"session {session.id} duration is not a {INTERVAL_MINUTES}-minute multiple"
            )

    participants = SessionParticipant.objects.filter(
        tenant=tenant,
        session__in=sessions,
    ).order_by("id")
    if lock:
        participants = participants.select_for_update()
    participants = list(participants)
    session_by_id = {session.id: session for session in sessions}
    target_session_ids = [
        session.id
        for session in sessions
        if any((
            session.booking_mode != "time_range",
            session.booking_interval_minutes != INTERVAL_MINUTES,
            session.booking_max_stay_minutes != MAX_STAY_MINUTES,
            session.allow_multi_slot_booking,
            session.allow_time_preference,
        ))
    ]
    range_backfill_session_id_set = {
        session.id for session in sessions if session.booking_mode != "time_range"
    }
    target_participant_ids = []
    fingerprint_participants = []
    for participant in participants:
        pair = (
            participant.booking_start_time is not None,
            participant.booking_end_time is not None,
        )
        if pair[0] != pair[1]:
            raise CommandError(
                f"participant {participant.id} has a partial booking range; no rows were changed"
            )
        if participant.session_id in range_backfill_session_id_set:
            if pair[0]:
                raise CommandError(
                    f"fixed session participant {participant.id} already has a booking range; no rows were changed"
                )
            target_participant_ids.append(participant.id)
        elif not pair[0]:
            raise CommandError(
                f"time-range participant {participant.id} has no booking range; no rows were changed"
            )
        fingerprint_participants.append({
            "id": participant.id,
            "session_id": participant.session_id,
            "status": participant.status,
            "start": participant.booking_start_time.isoformat() if participant.booking_start_time else None,
            "end": participant.booking_end_time.isoformat() if participant.booking_end_time else None,
        })

    tenant_default_change_required = any((
        tenant.clinic_booking_mode != "time_range",
        tenant.clinic_booking_interval_minutes != INTERVAL_MINUTES,
        tenant.clinic_booking_max_stay_minutes != MAX_STAY_MINUTES,
        tenant.clinic_allow_multi_slot_booking_default,
    ))
    fingerprint = {
        "tenant": {
            "id": tenant.id,
            "mode": tenant.clinic_booking_mode,
            "interval": tenant.clinic_booking_interval_minutes,
            "max_stay": tenant.clinic_booking_max_stay_minutes,
            "multi": tenant.clinic_allow_multi_slot_booking_default,
        },
        "from_date": from_date.isoformat(),
        "sessions": [
            {
                "id": session.id,
                "date": session.date.isoformat(),
                "start": session.start_time.isoformat(),
                "duration": session.duration_minutes,
                "mode": session.booking_mode,
                "interval": session.booking_interval_minutes,
                "max_stay": session.booking_max_stay_minutes,
                "multi": session.allow_multi_slot_booking,
                "preference": session.allow_time_preference,
            }
            for session in sessions
        ],
        "participants": fingerprint_participants,
    }
    return {
        "sessions": sessions,
        "participants": participants,
        "session_by_id": session_by_id,
        "target_session_ids": target_session_ids,
        "target_participant_ids": target_participant_ids,
        "tenant_default_change_required": tenant_default_change_required,
        "fingerprint_sha256": hashlib.sha256(
            json.dumps(fingerprint, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "confirmation_token": _confirmation_token(fingerprint),
    }


def _public_report(*, plan: dict, from_date: datetime.date, mode: str) -> dict:
    return {
        "mode": mode,
        "tenant_code": TENANT_CODE,
        "from_date": from_date.isoformat(),
        "target_session_ids": plan["target_session_ids"],
        "target_participant_count": len(plan["target_participant_ids"]),
        "tenant_default_change_required": plan["tenant_default_change_required"],
        "fingerprint_sha256": plan["fingerprint_sha256"],
        "required_confirmation_token": plan["confirmation_token"],
    }


class Command(BaseCommand):
    help = (
        "Convert current/future limglish clinic sessions to time-range booking. "
        "Dry-run is the default."
    )

    def add_arguments(self, parser):
        parser.add_argument("--from-date", required=True)
        parser.add_argument("--execute", action="store_true")
        parser.add_argument("--confirm", default="")

    def handle(self, *args, **options):
        from_date = _parse_from_date(options["from_date"])
        if not options["execute"]:
            tenant = _exact_tenant(lock=False)
            plan = _build_plan(tenant=tenant, from_date=from_date, lock=False)
            self.stdout.write(json.dumps(
                _public_report(plan=plan, from_date=from_date, mode="dry-run"),
                sort_keys=True,
            ))
            return

        with transaction.atomic():
            tenant = _exact_tenant(lock=True)
            plan = _build_plan(tenant=tenant, from_date=from_date, lock=True)
            if options["confirm"] != plan["confirmation_token"]:
                raise CommandError("--confirm does not match the current exact conversion plan")

            participant_by_id = {
                participant.id: participant for participant in plan["participants"]
            }
            changed_at = timezone.now()
            for participant_id in plan["target_participant_ids"]:
                participant = participant_by_id[participant_id]
                session = plan["session_by_id"][participant.session_id]
                _start, end = session_window(session)
                participant.booking_start_time = session.start_time
                participant.booking_end_time = end.time()
                participant.updated_at = changed_at
            if plan["target_participant_ids"]:
                SessionParticipant.objects.bulk_update(
                    [participant_by_id[item] for item in plan["target_participant_ids"]],
                    ["booking_start_time", "booking_end_time", "updated_at"],
                )

            session_by_id = plan["session_by_id"]
            for session_id in plan["target_session_ids"]:
                session = session_by_id[session_id]
                session.booking_mode = "time_range"
                session.booking_interval_minutes = INTERVAL_MINUTES
                session.booking_max_stay_minutes = MAX_STAY_MINUTES
                session.allow_multi_slot_booking = False
                session.allow_time_preference = False
                session.updated_at = changed_at
            if plan["target_session_ids"]:
                Session.objects.bulk_update(
                    [session_by_id[item] for item in plan["target_session_ids"]],
                    [
                        "booking_mode",
                        "booking_interval_minutes",
                        "booking_max_stay_minutes",
                        "allow_multi_slot_booking",
                        "allow_time_preference",
                        "updated_at",
                    ],
                )

            tenant.clinic_booking_mode = "time_range"
            tenant.clinic_booking_interval_minutes = INTERVAL_MINUTES
            tenant.clinic_booking_max_stay_minutes = MAX_STAY_MINUTES
            tenant.clinic_allow_multi_slot_booking_default = False
            tenant.save(update_fields=[
                "clinic_booking_mode",
                "clinic_booking_interval_minutes",
                "clinic_booking_max_stay_minutes",
                "clinic_allow_multi_slot_booking_default",
            ])

        report = _public_report(plan=plan, from_date=from_date, mode="execute")
        report.update({
            "converted_session_count": len(plan["target_session_ids"]),
            "backfilled_participant_count": len(plan["target_participant_ids"]),
        })
        self.stdout.write(json.dumps(report, sort_keys=True))
