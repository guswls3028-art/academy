from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import APIException, NotFound, PermissionDenied, ValidationError

from apps.domains.clinic.models import (
    Session,
    SessionParticipant,
    SessionParticipantPlanItem,
)
from apps.domains.clinic.time_ranges import (
    booking_window,
    is_supported_time_range_session,
    ranges_overlap,
    session_window,
)
from apps.support.clinic.session_dependencies import (
    active_students_for_clinic_tenant,
    active_enrolled_lecture_ids_for_student,
    cancel_pending_clinic_participant_reminders,
    clinic_enrollment_for_tenant,
    clinic_reason_for_unresolved_auto_links,
    clinic_reasons_for_unresolved_auto_links,
    enrollments_for_clinic_tenant,
    locked_clinic_links_for_participant_plan,
    preferred_active_enrollment_id_for_student_session,
    send_clinic_event_notification,
    student_has_current_required_clinic_target,
)


class Conflict(APIException):
    status_code = 409
    default_detail = "요청이 현재 데이터 상태와 충돌합니다."
    default_code = "conflict"


class ClinicNotificationOutboxUnavailable(APIException):
    status_code = 503
    default_detail = "취소 안내 접수를 완료하지 못했습니다. 잠시 후 다시 시도해 주세요."
    default_code = "clinic_notification_outbox_unavailable"


def cancel_active_participants_for_student(*, tenant, student, changed_at) -> int:
    return SessionParticipant.objects.filter(
        student=student,
        tenant=tenant,
        status__in=[SessionParticipant.Status.PENDING, SessionParticipant.Status.BOOKED],
    ).update(status=SessionParticipant.Status.CANCELLED, status_changed_at=changed_at)


@dataclass(frozen=True)
class ClinicNotificationEvent:
    trigger: str
    student: Any
    context: dict[str, Any]


@dataclass(frozen=True)
class ParticipantTransitionResult:
    participant: SessionParticipant
    notification: ClinicNotificationEvent | None = None
    notification_result: dict[str, Any] | None = None


@dataclass(frozen=True)
class ParticipantWriteResult:
    participant: SessionParticipant
    notification: ClinicNotificationEvent | None = None


@dataclass(frozen=True)
class ParticipantBulkWriteResult:
    participants: tuple[SessionParticipant, ...]
    notifications: tuple[ClinicNotificationEvent, ...]


STAFF_STATUS_TRANSITIONS = {
    SessionParticipant.Status.PENDING: {
        SessionParticipant.Status.BOOKED,
        SessionParticipant.Status.REJECTED,
        SessionParticipant.Status.CANCELLED,
    },
    SessionParticipant.Status.BOOKED: {
        SessionParticipant.Status.ATTENDED,
        SessionParticipant.Status.NO_SHOW,
        SessionParticipant.Status.CANCELLED,
    },
    SessionParticipant.Status.ATTENDED: {
        SessionParticipant.Status.BOOKED,
        SessionParticipant.Status.NO_SHOW,
    },
    SessionParticipant.Status.NO_SHOW: {
        SessionParticipant.Status.BOOKED,
        SessionParticipant.Status.ATTENDED,
    },
    SessionParticipant.Status.REJECTED: set(),
    SessionParticipant.Status.CANCELLED: set(),
}

STUDENT_STATUS_TRANSITIONS = {
    SessionParticipant.Status.PENDING: {
        SessionParticipant.Status.CANCELLED,
    },
    SessionParticipant.Status.BOOKED: {
        SessionParticipant.Status.CANCELLED,
    },
    SessionParticipant.Status.ATTENDED: set(),
    SessionParticipant.Status.NO_SHOW: set(),
    SessionParticipant.Status.REJECTED: set(),
    SessionParticipant.Status.CANCELLED: set(),
}

SELF_CANCEL_ACTIVE_STATUSES = (
    SessionParticipant.Status.PENDING,
    SessionParticipant.Status.BOOKED,
)


@dataclass(frozen=True)
class ParticipantSelfCancelPolicy:
    allowed: bool
    reason: str


def _participant_booking_end_at(participant: SessionParticipant) -> datetime.datetime:
    session = participant.session
    session_start = datetime.datetime.combine(session.date, session.start_time)
    if participant.booking_end_time is not None:
        end_at = datetime.datetime.combine(session.date, participant.booking_end_time)
        booking_start = participant.booking_start_time or session.start_time
        if participant.booking_end_time <= booking_start:
            end_at += datetime.timedelta(days=1)
    else:
        end_at = session_start + datetime.timedelta(
            minutes=int(session.duration_minutes or 0)
        )
    if timezone.is_naive(end_at):
        end_at = timezone.make_aware(end_at, timezone.get_current_timezone())
    return end_at


def _has_remaining_active_week_booking(
    *,
    tenant,
    participant: SessionParticipant,
    at: datetime.datetime,
) -> bool:
    session_date = participant.session.date
    week_start = session_date - datetime.timedelta(days=session_date.weekday())
    week_end = week_start + datetime.timedelta(days=6)
    candidates = SessionParticipant.objects.filter(
        tenant=tenant,
        student=participant.student,
        student__deleted_at__isnull=True,
        session__tenant=tenant,
        session__date__range=(week_start, week_end),
        status__in=SELF_CANCEL_ACTIVE_STATUSES,
        checked_out_at__isnull=True,
        completed_at__isnull=True,
    ).exclude(pk=participant.pk).select_related("session")
    return any(_participant_booking_end_at(candidate) > at for candidate in candidates)


def participant_self_cancel_policy(
    *,
    tenant,
    participant: SessionParticipant,
    has_current_required_target: bool | None = None,
) -> ParticipantSelfCancelPolicy:
    if participant.status not in SELF_CANCEL_ACTIVE_STATUSES:
        return ParticipantSelfCancelPolicy(
            allowed=False,
            reason="이미 종료된 예약은 취소할 수 없습니다.",
        )
    if not participant.session_id:
        return ParticipantSelfCancelPolicy(
            allowed=True,
            reason="아직 확정 일정이 없는 예약 신청은 직접 취소할 수 있습니다.",
        )
    if has_current_required_target is None:
        has_current_required_target = student_has_current_required_clinic_target(
            tenant=tenant,
            student=participant.student,
        )
    if not has_current_required_target:
        return ParticipantSelfCancelPolicy(
            allowed=True,
            reason="직접 취소할 수 있습니다. 취소 시 학생과 학부모님께 안내됩니다.",
        )

    if _has_remaining_active_week_booking(
        tenant=tenant,
        participant=participant,
        at=timezone.now(),
    ):
        return ParticipantSelfCancelPolicy(
            allowed=True,
            reason="같은 주의 다른 예약을 남기고 이 예약을 취소할 수 있습니다.",
        )
    return ParticipantSelfCancelPolicy(
        allowed=False,
        reason=(
            "필수 클리닉 대상자는 같은 주에 예약을 최소 1개 유지해야 합니다. "
            "다른 일정을 먼저 예약하면 이 예약을 취소할 수 있습니다."
        ),
    )

COMPLETE_ALLOWED_STATUSES = {
    SessionParticipant.Status.ATTENDED,
}

CLINIC_REMINDER_MIN_INTERVAL_MINUTES = 10
CLINIC_REMINDER_MAX_OCCURRENCES = 12
CLINIC_REMINDER_LATEST_TIME = datetime.time(22, 0)

SESSION_CHANGE_NOTICE_STATUSES = (
    SessionParticipant.Status.PENDING,
    SessionParticipant.Status.BOOKED,
    SessionParticipant.Status.ATTENDED,
    SessionParticipant.Status.NO_SHOW,
)


def _locked_participant(*, tenant, participant_id: int) -> SessionParticipant:
    try:
        return (
            SessionParticipant.objects
            .select_for_update()
            # `session` is nullable; joining it in the lock query makes PostgreSQL
            # reject FOR UPDATE on the nullable side of an outer join.
            .select_related("student")
            .get(pk=participant_id, tenant=tenant, student__deleted_at__isnull=True)
        )
    except SessionParticipant.DoesNotExist as exc:
        raise NotFound("예약을 찾을 수 없습니다.") from exc


def _plan_link_is_valid_for_participant(*, participant, clinic_link, target_lecture_ids) -> bool:
    enrollment = clinic_link.enrollment
    lecture_session = clinic_link.session
    return bool(
        participant.session_id
        and participant.status not in {
            SessionParticipant.Status.CANCELLED,
            SessionParticipant.Status.REJECTED,
        }
        and clinic_link.tenant_id == participant.tenant_id
        and clinic_link.is_auto
        and clinic_link.resolved_at is None
        and enrollment.tenant_id == participant.tenant_id
        and enrollment.student_id == participant.student_id
        and enrollment.status == "ACTIVE"
        and lecture_session.lecture_id == enrollment.lecture_id
        and (
            not target_lecture_ids
            or enrollment.lecture_id in target_lecture_ids
        )
    )


def planned_clinic_link_ids_for_participant(participant: SessionParticipant) -> list[int]:
    """Project only still-valid active plan rows; retain invalid rows for audit."""
    if not participant.session_id:
        return []
    target_lecture_ids = {
        int(lecture.id) for lecture in participant.session.target_lectures.all()
    }
    items = getattr(participant, "_active_plan_items", None)
    if items is None:
        items = list(
            SessionParticipantPlanItem.objects.filter(
                participant=participant,
                removed_at__isnull=True,
            ).select_related(
                "clinic_link__enrollment",
                "clinic_link__session",
            )
        )
    return sorted(
        {
            int(item.clinic_link_id)
            for item in items
            if _plan_link_is_valid_for_participant(
                participant=participant,
                clinic_link=item.clinic_link,
                target_lecture_ids=target_lecture_ids,
            )
        }
    )


@transaction.atomic
def replace_participant_clinic_plan(
    *,
    tenant,
    participant_id: int,
    clinic_link_ids,
    actor,
) -> SessionParticipant:
    """Replace the exact participant-scoped plan while retaining removal history."""
    try:
        normalized_ids = [int(link_id) for link_id in clinic_link_ids]
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            {"planned_clinic_link_ids": "ClinicLink ID는 양의 정수여야 합니다."}
        ) from exc
    if any(link_id <= 0 for link_id in normalized_ids):
        raise ValidationError(
            {"planned_clinic_link_ids": "ClinicLink ID는 양의 정수여야 합니다."}
        )
    if len(normalized_ids) != len(set(normalized_ids)):
        raise ValidationError(
            {"planned_clinic_link_ids": "중복 ClinicLink ID를 저장할 수 없습니다."}
        )
    normalized_ids = sorted(normalized_ids)

    participant = _locked_participant(tenant=tenant, participant_id=participant_id)
    if not participant.session_id:
        raise ValidationError(
            {"planned_clinic_link_ids": "확정된 클리닉 세션 참가자만 오늘 할 범위를 저장할 수 있습니다."}
        )
    participant.session = (
        Session.objects.filter(id=participant.session_id, tenant=tenant)
        .prefetch_related("target_lectures")
        .get()
    )
    target_lecture_ids = {
        int(lecture.id) for lecture in participant.session.target_lectures.all()
    }

    links = locked_clinic_links_for_participant_plan(
        clinic_link_ids=normalized_ids,
    )
    valid_ids = {
        int(link.id)
        for link in links
        if _plan_link_is_valid_for_participant(
            participant=participant,
            clinic_link=link,
            target_lecture_ids=target_lecture_ids,
        )
    }
    if valid_ids != set(normalized_ids):
        raise ValidationError(
            {
                "planned_clinic_link_ids": (
                    "현재 학생·테넌트·클리닉 세션에 유효한 미해결 항목만 저장할 수 있습니다."
                )
            }
        )

    current_items = list(
        SessionParticipantPlanItem.objects.select_for_update()
        .filter(participant=participant, removed_at__isnull=True)
        .order_by("clinic_link_id", "id")
    )
    current_by_link_id = {int(item.clinic_link_id): item for item in current_items}
    removed_ids = sorted(set(current_by_link_id) - valid_ids)
    if removed_ids:
        SessionParticipantPlanItem.objects.filter(
            id__in=[current_by_link_id[link_id].id for link_id in removed_ids]
        ).update(
            removed_at=timezone.now(),
            removed_by=actor,
            removal_reason="staff_replace",
        )

    SessionParticipantPlanItem.objects.bulk_create(
        [
            SessionParticipantPlanItem(
                participant=participant,
                clinic_link_id=link_id,
                selected_by=actor,
            )
            for link_id in normalized_ids
            if link_id not in current_by_link_id
        ]
    )
    if hasattr(participant, "_active_plan_items"):
        delattr(participant, "_active_plan_items")
    return participant


def _deactivate_participant_plan(*, participant, actor, reason: str, removed_at=None) -> int:
    return SessionParticipantPlanItem.objects.filter(
        participant=participant,
        removed_at__isnull=True,
    ).update(
        removed_at=removed_at or timezone.now(),
        removed_by=actor,
        removal_reason=reason,
    )


def _move_participant_plan(*, old_participant, new_participant, actor) -> int:
    """Move only links still valid for the replacement session, preserving old audit rows."""
    current_items = list(
        SessionParticipantPlanItem.objects.select_for_update()
        .filter(participant=old_participant, removed_at__isnull=True)
        .select_related("clinic_link__enrollment", "clinic_link__session")
        .order_by("id")
    )
    if not current_items:
        return 0

    target_lecture_ids = {
        int(lecture_id)
        for lecture_id in new_participant.session.target_lectures.values_list("id", flat=True)
    }
    valid_items = [
        item
        for item in current_items
        if _plan_link_is_valid_for_participant(
            participant=new_participant,
            clinic_link=item.clinic_link,
            target_lecture_ids=target_lecture_ids,
        )
    ]
    changed_at = timezone.now()
    SessionParticipantPlanItem.objects.filter(
        id__in=[item.id for item in current_items]
    ).update(
        removed_at=changed_at,
        removed_by=actor,
        removal_reason="booking_changed",
    )
    SessionParticipantPlanItem.objects.bulk_create(
        [
            SessionParticipantPlanItem(
                participant=new_participant,
                clinic_link_id=item.clinic_link_id,
                selected_by_id=item.selected_by_id or getattr(actor, "id", None),
            )
            for item in valid_items
        ]
    )
    return len(valid_items)


def _actor_label(actor) -> str:
    if not actor:
        return "시스템"
    full_name = ""
    if hasattr(actor, "get_full_name"):
        full_name = actor.get_full_name()
    return (
        full_name
        or getattr(actor, "name", "")
        or getattr(actor, "username", "")
        or "시스템"
    )


def _session_schedule_text(session) -> str:
    if not session:
        return ""
    date = str(session.date) if getattr(session, "date", None) else ""
    start_time = str(session.start_time)[:5] if getattr(session, "start_time", None) else ""
    location = getattr(session, "location", "") or ""
    return " ".join(part for part in (date, start_time, location) if part)


def build_session_change_notification_context(
    *,
    session,
    actor=None,
    old_session=None,
    domain_object_id: str | None = None,
) -> dict[str, Any]:
    location = getattr(session, "location", "") if session else ""
    date = str(session.date) if session and session.date else ""
    start_time = str(session.start_time)[:5] if session and getattr(session, "start_time", None) else ""
    old_schedule = (
        _session_schedule_text(old_session)
        if old_session is not None
        else "이전 안내된 클리닉 일정"
    )
    if not old_schedule:
        old_schedule = "-"

    context = {
        "클리닉명": getattr(session, "title", "") if session else "",
        "장소": f"[변경] {location}" if location else "[변경]",
        "날짜": date,
        "시간": start_time,
        "클리닉장소": location,
        "클리닉날짜": date,
        "클리닉시간": start_time,
        "클리닉기존일정": old_schedule,
        "클리닉변동사항": _session_schedule_text(session) or "일정 변경",
        "클리닉수정자": _actor_label(actor),
    }
    if domain_object_id:
        context["_domain_object_id"] = domain_object_id
    return context


def session_change_notice_student_ids(*, tenant, session) -> list[int]:
    ids = SessionParticipant.objects.filter(
        tenant=tenant,
        session=session,
        status__in=SESSION_CHANGE_NOTICE_STATUSES,
        student__deleted_at__isnull=True,
    ).order_by("student_id").values_list("student_id", flat=True)
    return list(dict.fromkeys(ids))


def _reservation_notification(participant: SessionParticipant) -> ClinicNotificationEvent | None:
    if participant.status not in (
        SessionParticipant.Status.BOOKED,
        SessionParticipant.Status.PENDING,
    ):
        return None
    session = participant.session
    return ClinicNotificationEvent(
        trigger="clinic_reservation_created",
        student=participant.student,
        context={
            "클리닉명": getattr(session, "title", "") if session else "",
            "장소": getattr(session, "location", "") if session else "",
            "날짜": str(session.date) if session and session.date else "",
            "시간": str(session.start_time)[:5] if session and getattr(session, "start_time", None) else "",
            "_domain_object_id": f"clinic_participant:{participant.pk}:reservation",
        },
    )


def _booking_change_notification(
    *,
    new_booking: SessionParticipant,
    old_session,
    actor,
) -> ClinicNotificationEvent:
    new_session = new_booking.session
    return ClinicNotificationEvent(
        trigger="clinic_reservation_changed",
        student=new_booking.student,
        context=build_session_change_notification_context(
            session=new_session,
            old_session=old_session,
            actor=actor,
            domain_object_id=f"clinic_participant:{new_booking.pk}:booking_change",
        ),
    )


def _status_notification(
    participant: SessionParticipant,
    next_status: str,
    *,
    actor=None,
) -> ClinicNotificationEvent | None:
    trigger_map = {
        SessionParticipant.Status.CANCELLED: "clinic_cancelled",
        SessionParticipant.Status.ATTENDED: "clinic_check_in",
        SessionParticipant.Status.NO_SHOW: "clinic_absent",
    }
    trigger = trigger_map.get(next_status)
    if not trigger:
        return None

    session = participant.session
    location = getattr(session, "location", "") if session else ""
    date = str(session.date) if session and session.date else ""
    start_time = (
        str(session.start_time)[:5]
        if session and getattr(session, "start_time", None)
        else ""
    )
    is_cancel = next_status == SessionParticipant.Status.CANCELLED
    is_absent = next_status == SessionParticipant.Status.NO_SHOW
    context = {
        "클리닉명": getattr(session, "title", "") if session else "",
        "장소": f"[취소] {location}" if is_cancel else f"[결석] {location}" if is_absent else location,
        "날짜": date,
        "시간": f"취소({start_time})" if is_cancel else f"결석({start_time})" if is_absent else start_time,
        "_domain_object_id": (
            f"clinic_participant:{participant.pk}:{trigger}:"
            f"{int((participant.status_changed_at or timezone.now()).timestamp())}"
        ),
    }
    if is_cancel:
        context.update(
            {
                "클리닉기존일정": _session_schedule_text(session) or "-",
                "클리닉변동사항": "예약 취소",
                "클리닉수정자": _actor_label(actor),
            }
        )
    if next_status in (SessionParticipant.Status.ATTENDED, SessionParticipant.Status.NO_SHOW):
        now_hm = timezone.now().strftime("%H:%M")
        context["도착시간"] = now_hm
        context["_actual_time"] = now_hm
    if next_status == SessionParticipant.Status.ATTENDED:
        context["등원구분"] = "지각" if participant.is_late else "정상"
    return ClinicNotificationEvent(
        trigger=trigger,
        student=participant.student,
        context=context,
    )


def _persist_self_cancel_notification_outboxes(
    *,
    tenant,
    event: ClinicNotificationEvent,
) -> dict[str, Any]:
    context = dict(event.context)
    context.setdefault("_source_domain", "clinic")
    context.setdefault("_source_use_case", "clinic.clinic_cancelled")
    targets = []
    for target in ("parent", "student"):
        try:
            requested = send_clinic_event_notification(
                tenant=tenant,
                trigger="clinic_cancelled",
                student=event.student,
                send_to=target,
                context=context,
            )
        except Exception as exc:
            raise ClinicNotificationOutboxUnavailable() from exc
        if not requested:
            raise ClinicNotificationOutboxUnavailable()
        targets.append({"target": target, "requested": True})
    return {
        "requested": 2,
        "failed": 0,
        "send_to": "both",
        "targets": targets,
    }


def _already_cancelled_notification_result() -> dict[str, Any]:
    return {
        "requested": 0,
        "failed": 0,
        "send_to": "both",
        "targets": [
            {"target": target, "requested": False, "already_requested": True}
            for target in ("parent", "student")
        ],
    }


def _complete_notification(participant: SessionParticipant) -> ClinicNotificationEvent:
    session = participant.session
    now = timezone.now()
    return ClinicNotificationEvent(
        trigger="clinic_self_study_completed",
        student=participant.student,
        context={
            "클리닉명": getattr(session, "title", "") if session else "",
            "장소": getattr(session, "location", "") if session else "",
            "날짜": str(session.date) if session and session.date else now.strftime("%Y-%m-%d"),
            "시간": now.strftime("%H:%M"),
            "_domain_object_id": (
                f"clinic_participant:{participant.pk}:completed:"
                f"{int((participant.completed_at or now).timestamp())}"
            ),
        },
    )


def _checkout_notification(participant: SessionParticipant) -> ClinicNotificationEvent:
    session = participant.session
    checked_out_at = participant.checked_out_at or timezone.now()
    return ClinicNotificationEvent(
        trigger="clinic_check_out",
        student=participant.student,
        context={
            "클리닉명": getattr(session, "title", "") if session else "",
            "장소": getattr(session, "location", "") if session else "",
            "날짜": (
                str(session.date)
                if session and session.date
                else checked_out_at.strftime("%Y-%m-%d")
            ),
            "시간": checked_out_at.strftime("%H:%M"),
            "_actual_time": checked_out_at.strftime("%H:%M"),
            "_domain_object_id": (
                f"clinic_participant:{participant.pk}:checkout:"
                f"{int(checked_out_at.timestamp())}"
            ),
        },
    )


@transaction.atomic
def change_participant_status(
    *,
    tenant,
    participant_id: int,
    next_status: str,
    actor,
    request_student=None,
    staff_memo=None,
    is_late: bool = False,
) -> ParticipantTransitionResult:
    allowed_statuses = {choice[0] for choice in SessionParticipant.Status.choices}
    if next_status not in allowed_statuses:
        raise ValidationError({"detail": f"Invalid status: {next_status}"})

    if request_student:
        request_student = (
            active_students_for_clinic_tenant(tenant)
            .select_for_update()
            .get(pk=request_student.pk)
        )
    participant = _locked_participant(tenant=tenant, participant_id=participant_id)
    if request_student and participant.student_id != request_student.id:
        raise PermissionDenied("다른 학생의 예약을 수정할 수 없습니다.")
    if (
        request_student
        and next_status == SessionParticipant.Status.CANCELLED
        and participant.status == SessionParticipant.Status.CANCELLED
    ):
        return ParticipantTransitionResult(
            participant=participant,
            notification_result=_already_cancelled_notification_result(),
        )
    if participant.checked_out_at is not None:
        raise ValidationError({"detail": "하원 처리된 참가자의 등원 상태는 변경할 수 없습니다."})
    transitions = STUDENT_STATUS_TRANSITIONS if request_student else STAFF_STATUS_TRANSITIONS
    valid_next = transitions.get(participant.status, set())
    if next_status not in valid_next:
        raise ValidationError(
            {"detail": f"'{participant.status}'에서 '{next_status}'(으)로 변경할 수 없습니다."}
        )

    if request_student:
        if next_status != SessionParticipant.Status.CANCELLED:
            raise PermissionDenied("학생은 예약 취소만 가능합니다.")
        cancel_policy = participant_self_cancel_policy(
            tenant=tenant,
            participant=participant,
        )
        if not cancel_policy.allowed:
            raise Conflict(cancel_policy.reason)

    changed_at = timezone.now()
    participant.status = next_status
    participant.status_changed_at = changed_at
    participant.status_changed_by = actor
    # Attendance corrections and study completion are separate facts.
    # Never erase the teacher-confirmed self-study result while correcting arrival.
    if next_status == SessionParticipant.Status.ATTENDED:
        participant.checked_in_at = changed_at
        participant.is_late = bool(is_late)
    else:
        participant.checked_in_at = None
        participant.is_late = False
    if staff_memo is not None:
        participant.staff_memo = staff_memo
    update_fields = [
        "status",
        "staff_memo",
        "status_changed_at",
        "status_changed_by",
        "checked_in_at",
        "is_late",
        "updated_at",
    ]
    participant.save(update_fields=update_fields)
    if next_status in (
        SessionParticipant.Status.ATTENDED,
        SessionParticipant.Status.NO_SHOW,
        SessionParticipant.Status.CANCELLED,
        SessionParticipant.Status.REJECTED,
    ):
        cancel_pending_clinic_participant_reminders(
            tenant_id=tenant.id,
            participant_id=participant.id,
        )
    if next_status in (
        SessionParticipant.Status.CANCELLED,
        SessionParticipant.Status.REJECTED,
    ):
        _deactivate_participant_plan(
            participant=participant,
            actor=actor,
            reason=f"booking_{next_status}",
            removed_at=changed_at,
        )
    notification = _status_notification(participant, next_status, actor=actor)
    if request_student and next_status == SessionParticipant.Status.CANCELLED:
        if notification is None:
            raise ClinicNotificationOutboxUnavailable()
        return ParticipantTransitionResult(
            participant=participant,
            notification_result=_persist_self_cancel_notification_outboxes(
                tenant=tenant,
                event=notification,
            ),
        )
    return ParticipantTransitionResult(
        participant=participant,
        notification=notification,
    )


@transaction.atomic
def update_participant_staff_memo(*, tenant, participant_id: int, staff_memo: str):
    participant = _locked_participant(tenant=tenant, participant_id=participant_id)
    participant.staff_memo = staff_memo
    participant.save(update_fields=["staff_memo", "updated_at"])
    return participant


@transaction.atomic
def complete_participant(*, tenant, participant_id: int, actor) -> ParticipantTransitionResult:
    participant = _locked_participant(tenant=tenant, participant_id=participant_id)
    if participant.completed_at:
        raise ValidationError({"detail": "이미 완료 처리된 참가자입니다."})
    if participant.status not in COMPLETE_ALLOWED_STATUSES:
        raise ValidationError(
            {"detail": f"'{participant.get_status_display()}' 상태의 참가자는 완료 처리할 수 없습니다."}
        )

    completed_at = timezone.now()
    participant.completed_at = completed_at
    participant.completed_by = actor
    participant.completion_history = [
        *(participant.completion_history or []),
        {
            "action": "complete",
            "at": completed_at.isoformat(),
            "actor_id": getattr(actor, "id", None),
            "completed_at": completed_at.isoformat(),
        },
    ]
    participant.save(
        update_fields=[
            "completed_at",
            "completed_by",
            "completion_history",
            "updated_at",
        ]
    )
    cancel_pending_clinic_participant_reminders(
        tenant_id=tenant.id,
        participant_id=participant.id,
    )
    return ParticipantTransitionResult(
        participant=participant,
        notification=_complete_notification(participant),
    )


@transaction.atomic
def checkout_participant(
    *,
    tenant,
    participant_id: int,
    actor,
    confirm_without_arrival: bool = False,
    expected_session_id: int | None = None,
    expected_student_id: int | None = None,
) -> ParticipantTransitionResult:
    participant = _locked_participant(tenant=tenant, participant_id=participant_id)
    if expected_session_id is not None and participant.session_id != expected_session_id:
        raise Conflict("선택한 클리닉 일정이 변경되었습니다. 다시 확인해 주세요.")
    if expected_student_id is not None and participant.student_id != expected_student_id:
        raise Conflict("선택한 학생 정보가 변경되었습니다. 다시 확인해 주세요.")
    if participant.checked_out_at is not None:
        return ParticipantTransitionResult(participant=participant)

    arrival_recorded = (
        participant.status == SessionParticipant.Status.ATTENDED
        and participant.checked_in_at is not None
    )
    if not arrival_recorded:
        if not confirm_without_arrival:
            raise ValidationError(
                {"detail": "등원 기록 없이 하원하려면 현재 학생과 일정을 명시적으로 확인해 주세요."}
            )
        if expected_session_id is None or expected_student_id is None:
            raise ValidationError(
                {"detail": "등원 기록 없는 하원은 현재 클리닉 일정과 학생을 다시 확인해야 합니다."}
            )
        if participant.session_id is None or participant.status != SessionParticipant.Status.BOOKED:
            raise ValidationError(
                {"detail": "예약 확정 상태의 클리닉 참가자만 등원 기록 없이 하원 처리할 수 있습니다."}
            )

    participant.checked_out_at = timezone.now()
    participant.checked_out_by = actor
    participant.checkout_mode = (
        SessionParticipant.CheckoutMode.ARRIVAL_RECORDED
        if arrival_recorded
        else SessionParticipant.CheckoutMode.ARRIVAL_NOT_RECORDED
    )
    participant.save(
        update_fields=[
            "checked_out_at",
            "checked_out_by",
            "checkout_mode",
            "updated_at",
        ]
    )
    cancel_pending_clinic_participant_reminders(
        tenant_id=tenant.id,
        participant_id=participant.id,
    )
    return ParticipantTransitionResult(
        participant=participant,
        notification=_checkout_notification(participant),
    )


@transaction.atomic
def uncomplete_participant(*, tenant, participant_id: int, actor) -> ParticipantTransitionResult:
    participant = _locked_participant(tenant=tenant, participant_id=participant_id)
    if not participant.completed_at:
        raise ValidationError({"detail": "완료 처리되지 않은 참가자입니다."})

    changed_at = timezone.now()
    previous_completed_at = participant.completed_at
    previous_completed_by_id = participant.completed_by_id
    participant.completion_history = [
        *(participant.completion_history or []),
        {
            "action": "uncomplete",
            "at": changed_at.isoformat(),
            "actor_id": getattr(actor, "id", None),
            "previous_completed_at": previous_completed_at.isoformat(),
            "previous_completed_by_id": previous_completed_by_id,
        },
    ]
    participant.completed_at = None
    participant.completed_by = None
    participant.save(update_fields=[
        "completed_at",
        "completed_by",
        "completion_history",
        "updated_at",
    ])
    return ParticipantTransitionResult(participant=participant)


def build_clinic_reminder_send_times(
    *,
    now: datetime.datetime,
    interval_minutes: int,
    repeat_until: datetime.datetime,
) -> list[datetime.datetime]:
    """Build bounded same-day repeat times for one manual reminder plan."""
    if timezone.is_naive(now) or timezone.is_naive(repeat_until):
        raise ValidationError({"detail": "재촉 시작/종료 시각에는 시간대 정보가 필요합니다."})
    try:
        interval_minutes = int(interval_minutes)
    except (TypeError, ValueError) as exc:
        raise ValidationError({"detail": "반복 간격은 분 단위 숫자여야 합니다."}) from exc
    if interval_minutes < CLINIC_REMINDER_MIN_INTERVAL_MINUTES:
        raise ValidationError(
            {"detail": f"반복 간격은 최소 {CLINIC_REMINDER_MIN_INTERVAL_MINUTES}분입니다."}
        )

    local_tz = timezone.get_current_timezone()
    current = now.astimezone(local_tz)
    until = repeat_until.astimezone(local_tz)
    if until.date() != current.date():
        raise ValidationError({"detail": "반복 재촉은 시작한 날짜 안에서만 설정할 수 있습니다."})
    if until.timetz().replace(tzinfo=None) > CLINIC_REMINDER_LATEST_TIME:
        raise ValidationError({"detail": "반복 재촉 종료는 밤 10시를 넘길 수 없습니다."})
    if until <= current:
        raise ValidationError({"detail": "반복 재촉 종료 시각은 현재보다 늦어야 합니다."})

    result: list[datetime.datetime] = []
    next_send = current + datetime.timedelta(minutes=interval_minutes)
    while next_send <= until:
        result.append(next_send)
        if len(result) > CLINIC_REMINDER_MAX_OCCURRENCES:
            raise ValidationError(
                {"detail": f"반복 재촉은 즉시 발송을 제외하고 최대 {CLINIC_REMINDER_MAX_OCCURRENCES}회까지 설정할 수 있습니다."}
            )
        next_send += datetime.timedelta(minutes=interval_minutes)
    if not result:
        raise ValidationError({"detail": "종료 전 발송할 반복 재촉 시각이 없습니다."})
    return result


def _ensure_active_student_for_tenant(*, tenant, student) -> None:
    if not student:
        raise ValidationError({"detail": "student가 필요합니다."})
    if getattr(student, "tenant_id", None) != getattr(tenant, "id", None):
        raise PermissionDenied("해당 학생에 접근할 권한이 없습니다.")
    if getattr(student, "deleted_at", None):
        raise ValidationError({"detail": "삭제된 학생은 클리닉 예약 대상이 될 수 없습니다."})


def _lock_active_student_for_booking(*, tenant, student):
    """Serialize every booking write for one tenant-scoped student."""
    _ensure_active_student_for_tenant(tenant=tenant, student=student)
    locked_student = (
        active_students_for_clinic_tenant(tenant)
        .select_for_update()
        .filter(pk=student.pk)
        .first()
    )
    if locked_student is None:
        raise ValidationError({"detail": "삭제된 학생은 클리닉 예약 대상이 될 수 없습니다."})
    return locked_student


def _validate_student_session_eligibility(*, tenant, student, session) -> None:
    if not session:
        return
    if getattr(session, "tenant_id", None) != getattr(tenant, "id", None):
        raise PermissionDenied("해당 세션에 접근할 권한이 없습니다.")
    if session.date < timezone.localdate():
        raise ValidationError({"detail": "지난 날짜의 클리닉은 예약할 수 없습니다."})
    if session.target_grade:
        if not student.grade or session.target_grade != student.grade:
            raise PermissionDenied("해당 클리닉은 다른 학년 대상입니다. 본인 학년의 클리닉만 신청할 수 있습니다.")
    if session.target_school_type and session.target_school_type.strip():
        if not student.school_type or session.target_school_type != student.school_type:
            raise PermissionDenied("해당 클리닉은 다른 학교 유형 대상입니다.")
    target_lecture_ids = set(session.target_lectures.values_list("id", flat=True))
    if target_lecture_ids:
        enrolled_lecture_ids = active_enrolled_lecture_ids_for_student(tenant, student)
        if not target_lecture_ids & enrolled_lecture_ids:
            raise PermissionDenied("해당 클리닉은 특정 강의 수강생 대상입니다.")


def _validated_time_preference(
    *,
    session: Session | None,
    preferred_start_time,
    preferred_end_time,
) -> tuple[datetime.time | None, datetime.time | None]:
    if preferred_start_time is None and preferred_end_time is None:
        return None, None
    if preferred_start_time is None or preferred_end_time is None:
        raise ValidationError(
            {"preferred_time": "희망 시작과 종료 시간을 함께 입력해 주세요."}
        )
    if session is None or not session.allow_time_preference:
        raise ValidationError(
            {"preferred_time": "이 클리닉은 희망 시간을 받지 않습니다."}
        )

    session_start = datetime.datetime.combine(session.date, session.start_time)
    session_end = session_start + datetime.timedelta(minutes=session.duration_minutes)
    preferred_start = datetime.datetime.combine(session.date, preferred_start_time)
    preferred_end = datetime.datetime.combine(session.date, preferred_end_time)
    if session_end.date() != session.date:
        raise ValidationError(
            {"preferred_time": "자정을 넘는 클리닉은 희망 시간 입력을 지원하지 않습니다."}
        )
    if not session_start <= preferred_start < preferred_end <= session_end:
        raise ValidationError(
            {
                "preferred_time": (
                    "희망 시간은 클리닉 운영 시간 안에서 시작이 종료보다 빨라야 합니다."
                )
            }
        )
    return preferred_start_time, preferred_end_time


def _validated_booking_range(
    *,
    session: Session | None,
    booking_start_time,
    booking_end_time,
) -> tuple[datetime.time | None, datetime.time | None]:
    """Validate the actual reservation interval independently from preferences."""

    if session is None or session.booking_mode == "fixed_slot":
        if booking_start_time is not None or booking_end_time is not None:
            raise ValidationError(
                {"booking_time": "고정 시간대 클리닉에는 실제 예약 시간을 따로 입력하지 않습니다."}
            )
        return None, None
    if booking_start_time is None or booking_end_time is None:
        raise ValidationError(
            {"booking_time": "예약 시작과 종료 시간을 함께 입력해 주세요."}
        )

    session_start, session_end = session_window(session)
    booking_start, booking_end = booking_window(
        session=session,
        start_time=booking_start_time,
        end_time=booking_end_time,
    )
    if not is_supported_time_range_session(session):
        raise ValidationError(
            {"booking_time": "자정 이후까지 이어지는 클리닉은 시간 범위 예약을 지원하지 않습니다."}
        )
    if not session_start <= booking_start < booking_end <= session_end:
        raise ValidationError(
            {"booking_time": "예약 시간은 클리닉 운영 시간 안에서 시작이 종료보다 빨라야 합니다."}
        )
    interval = int(session.booking_interval_minutes)
    start_offset = int((booking_start - session_start).total_seconds() // 60)
    end_offset = int((booking_end - session_start).total_seconds() // 60)
    if interval not in (30, 60) or start_offset % interval or end_offset % interval:
        raise ValidationError(
            {"booking_time": f"예약 시간은 세션 시작 기준 {interval}분 간격에 맞춰 주세요."}
        )
    if end_offset - start_offset > int(session.booking_max_stay_minutes):
        raise ValidationError(
            {"booking_time": f"한 번에 최대 {session.booking_max_stay_minutes}분까지 예약할 수 있습니다."}
        )
    return booking_start_time, booking_end_time


def _preferred_active_enrollment_id(
    *,
    tenant,
    student,
    session=None,
    preferred_enrollment_id: int | None = None,
) -> int | None:
    return preferred_active_enrollment_id_for_student_session(
        tenant,
        student,
        session,
        preferred_enrollment_id=preferred_enrollment_id,
    )


def _clinic_reason_for_enrollment(*, tenant, enrollment_id: int | None) -> str | None:
    return clinic_reason_for_unresolved_auto_links(tenant, enrollment_id)


def _assert_session_capacity(
    *,
    tenant,
    session,
    booking_start_time=None,
    booking_end_time=None,
) -> None:
    if not session or session.max_participants is None:
        return
    active = SessionParticipant.objects.filter(
        tenant=tenant,
        session=session,
        status__in=(
            SessionParticipant.Status.BOOKED,
            SessionParticipant.Status.PENDING,
        ),
    )
    if session.booking_mode == "fixed_slot":
        if active.count() >= session.max_participants:
            raise Conflict("해당 클리닉은 정원이 마감되었습니다.")
        return

    if booking_start_time is None or booking_end_time is None:
        raise ValidationError({"booking_time": "예약 시작과 종료 시간을 입력해 주세요."})
    ranged_active = list(SessionParticipant.objects.filter(
        tenant=tenant,
        session=session,
        status__in=(
            SessionParticipant.Status.PENDING,
            SessionParticipant.Status.BOOKED,
            SessionParticipant.Status.ATTENDED,
        ),
    ).only("booking_start_time", "booking_end_time"))
    cursor, end = booking_window(
        session=session,
        start_time=booking_start_time,
        end_time=booking_end_time,
    )
    step = datetime.timedelta(minutes=int(session.booking_interval_minutes))
    while cursor < end:
        next_cursor = min(cursor + step, end)
        concurrent = 0
        for participant in ranged_active:
            if participant.booking_start_time is None or participant.booking_end_time is None:
                concurrent += 1
                continue
            participant_start, participant_end = booking_window(
                session=session,
                start_time=participant.booking_start_time,
                end_time=participant.booking_end_time,
            )
            concurrent += ranges_overlap(
                participant_start,
                participant_end,
                cursor,
                next_cursor,
            )
        if concurrent >= session.max_participants:
            raise Conflict("해당 클리닉은 선택한 시간의 정원이 마감되었습니다.")
        cursor = next_cursor


def booking_availability_for_session(*, tenant, session: Session) -> dict[str, Any]:
    """Return tenant-scoped interval capacity without exposing participant identity."""

    interval = int(session.booking_interval_minutes)
    start, end = session_window(session)
    slots = []
    active = getattr(session, "booking_capacity_participants", None)
    if active is None or session.booking_mode == "fixed_slot":
        active = list(SessionParticipant.objects.filter(
            tenant=tenant,
            session=session,
            status__in=(
                SessionParticipant.Status.PENDING,
                SessionParticipant.Status.BOOKED,
                *([SessionParticipant.Status.ATTENDED] if session.booking_mode == "time_range" else []),
            ),
        ).only("booking_start_time", "booking_end_time"))
    cursor = start
    while cursor < end:
        next_cursor = min(cursor + datetime.timedelta(minutes=interval), end)
        if session.booking_mode == "time_range":
            used = 0
            for participant in active:
                if participant.booking_start_time is None or participant.booking_end_time is None:
                    used += 1
                    continue
                participant_start, participant_end = booking_window(
                    session=session,
                    start_time=participant.booking_start_time,
                    end_time=participant.booking_end_time,
                )
                used += ranges_overlap(
                    participant_start,
                    participant_end,
                    cursor,
                    next_cursor,
                )
        else:
            used = len(active)
        slots.append({
            "start_time": cursor.time().strftime("%H:%M"),
            "end_time": next_cursor.time().strftime("%H:%M"),
            "remaining_capacity": max(int(session.max_participants) - used, 0),
        })
        cursor = next_cursor
    return {
        "booking_mode": session.booking_mode,
        "interval_minutes": interval,
        "max_stay_minutes": int(session.booking_max_stay_minutes),
        "window": {
            "start_time": start.time().strftime("%H:%M"),
            "end_time": end.time().strftime("%H:%M"),
        },
        "slots": slots,
    }


def _assert_no_active_duplicate(
    *,
    tenant,
    student,
    session=None,
    requested_date=None,
    requested_start_time=None,
    exclude_participant_id=None,
) -> None:
    active_statuses = [
        SessionParticipant.Status.PENDING,
        SessionParticipant.Status.BOOKED,
    ]
    active = SessionParticipant.objects.filter(
        tenant=tenant,
        student=student,
        status__in=active_statuses,
    )
    if exclude_participant_id is not None:
        active = active.exclude(pk=exclude_participant_id)

    if session:
        exists = active.filter(
            tenant=tenant,
            session=session,
        ).exists()
        if exists:
            raise Conflict("이미 해당 세션에 예약된 학생입니다.")

        same_date = active.filter(
            Q(session__date=session.date)
            | Q(session__isnull=True, requested_date=session.date)
        )
        if not same_date.exists():
            return
        if not session.allow_multi_slot_booking:
            raise Conflict("이 클리닉은 같은 날 한 시간대만 예약할 수 있습니다.")
        if same_date.filter(
            Q(session__isnull=True) | Q(session__allow_multi_slot_booking=False)
        ).exists():
            raise Conflict("기존 예약 중 같은 날 여러 시간대를 허용하지 않는 클리닉이 있습니다.")
    elif requested_date and requested_start_time:
        exists = active.filter(
            session__isnull=True,
            requested_date=requested_date,
            requested_start_time=requested_start_time,
        ).exists()
        if exists:
            raise Conflict("이미 해당 시간에 예약 신청이 있습니다.")
        if active.filter(
            Q(session__date=requested_date)
            | Q(session__isnull=True, requested_date=requested_date)
        ).exists():
            raise Conflict("같은 날 한 시간대만 예약할 수 있습니다.")


@transaction.atomic
def create_participant(
    *,
    tenant,
    validated_data: dict[str, Any],
    request_student=None,
) -> ParticipantWriteResult:
    session = validated_data.get("session")
    requested_date = validated_data.get("requested_date")
    requested_start_time = validated_data.get("requested_start_time")
    student = validated_data.get("student")
    enrollment = validated_data.get("enrollment")
    enrollment_id = enrollment.id if enrollment else None
    source = validated_data.get("source") or SessionParticipant.Source.MANUAL
    requested_status = validated_data.get("status")
    memo = validated_data.get("memo") or ""
    student_request_memo = validated_data.get("student_request_memo") or ""

    if not session and not (requested_date and requested_start_time):
        raise ValidationError({"detail": "session 또는 (requested_date + requested_start_time) 중 하나는 필수입니다."})
    if session and (requested_date or requested_start_time):
        raise ValidationError({"detail": "session과 requested_date/requested_start_time을 동시에 사용할 수 없습니다."})
    if session and getattr(session, "tenant_id", None) != getattr(tenant, "id", None):
        raise PermissionDenied("해당 세션에 접근할 권한이 없습니다.")
    if session and session.date < timezone.localdate():
        raise ValidationError({"detail": "지난 날짜의 클리닉은 예약할 수 없습니다."})

    if request_student:
        if student and student.id != request_student.id:
            raise PermissionDenied("다른 학생의 예약을 신청할 수 없습니다.")
        student = request_student
        source = SessionParticipant.Source.STUDENT_REQUEST
        if not session:
            raise ValidationError({"detail": "등록 가능한 클리닉을 선택해주세요. 해당 날짜에 열린 클리닉만 신청할 수 있습니다."})
        _validate_student_session_eligibility(tenant=tenant, student=request_student, session=session)
        requested_status = SessionParticipant.Status.PENDING
        if getattr(tenant, "clinic_auto_approve_booking", False):
            requested_status = SessionParticipant.Status.BOOKED
        if not student_request_memo:
            student_request_memo = memo
        memo = ""

    if not student and enrollment_id:
        enrollment = clinic_enrollment_for_tenant(tenant, enrollment_id)
        if not enrollment:
            raise ValidationError({"detail": "해당 수강 등록 정보를 찾을 수 없습니다."})
        student = enrollment.student
    elif student and enrollment_id:
        enrollment = clinic_enrollment_for_tenant(tenant, enrollment_id)
        if not enrollment:
            raise ValidationError({"detail": "해당 수강 등록 정보를 찾을 수 없습니다."})
        if enrollment.student_id != student.id:
            raise ValidationError({"detail": "enrollment_id와 student가 일치하지 않습니다."})

    student = _lock_active_student_for_booking(tenant=tenant, student=student)

    if session:
        session = (
            Session.objects
            .filter(tenant=tenant)
            .select_for_update()
            .get(pk=session.pk)
        )

    booking_start_time, booking_end_time = _validated_booking_range(
        session=session,
        booking_start_time=validated_data.get("booking_start_time"),
        booking_end_time=validated_data.get("booking_end_time"),
    )
    if session:
        _assert_session_capacity(
            tenant=tenant,
            session=session,
            booking_start_time=booking_start_time,
            booking_end_time=booking_end_time,
        )

    preferred_start_time, preferred_end_time = _validated_time_preference(
        session=session,
        preferred_start_time=validated_data.get("preferred_start_time"),
        preferred_end_time=validated_data.get("preferred_end_time"),
    )

    _assert_no_active_duplicate(
        tenant=tenant,
        student=student,
        session=session,
        requested_date=requested_date,
        requested_start_time=requested_start_time,
    )

    if source == SessionParticipant.Source.MANUAL:
        participant_role = "manual"
    elif source == SessionParticipant.Source.STUDENT_REQUEST:
        participant_role = "manual"
    else:
        participant_role = "target"
    participant_role = validated_data.get("participant_role") or participant_role

    if not enrollment_id and student:
        enrollment_id = _preferred_active_enrollment_id(
            tenant=tenant,
            student=student,
            session=session,
        )

    clinic_reason = (
        validated_data["clinic_reason"]
        if "clinic_reason" in validated_data
        else _clinic_reason_for_enrollment(
            tenant=tenant,
            enrollment_id=enrollment_id,
        )
    )

    if not requested_status:
        if source in (SessionParticipant.Source.MANUAL, SessionParticipant.Source.AUTO):
            default_status = SessionParticipant.Status.BOOKED
        elif getattr(tenant, "clinic_auto_approve_booking", False):
            default_status = SessionParticipant.Status.BOOKED
        else:
            default_status = SessionParticipant.Status.PENDING
    else:
        default_status = requested_status

    try:
        participant = SessionParticipant.objects.create(
            tenant=tenant,
            session=session,
            requested_date=requested_date,
            requested_start_time=requested_start_time,
            student=student,
            source=source,
            status=default_status,
            enrollment_id=enrollment_id,
            participant_role=participant_role,
            clinic_reason=clinic_reason,
            memo=memo,
            student_request_memo=student_request_memo,
            preferred_start_time=preferred_start_time,
            preferred_end_time=preferred_end_time,
            booking_start_time=booking_start_time,
            booking_end_time=booking_end_time,
        )
    except IntegrityError as exc:
        raise Conflict("이미 해당 세션에 예약된 학생입니다.") from exc

    return ParticipantWriteResult(
        participant=participant,
        notification=_reservation_notification(participant),
    )


@transaction.atomic
def create_participants_bulk(
    *,
    tenant,
    session_ids: list[int],
    student_ids: list[int] | None = None,
    enrollment_ids: list[int] | None = None,
    request_student=None,
    student_request_memo: str = "",
    memo: str = "",
    preferred_start_time=None,
    preferred_end_time=None,
    booking_start_time=None,
    booking_end_time=None,
) -> ParticipantBulkWriteResult:
    """Create the complete same-day session/student selection or create nothing."""
    requested_session_ids = [int(session_id) for session_id in session_ids]
    requested_student_ids = [int(student_id) for student_id in (student_ids or [])]
    requested_enrollment_ids = [
        int(enrollment_id) for enrollment_id in (enrollment_ids or [])
    ]
    if request_student is not None:
        if requested_student_ids or requested_enrollment_ids:
            raise PermissionDenied("다른 학생의 클리닉 예약을 신청할 수 없습니다.")
        booking_targets = [(
            _lock_active_student_for_booking(
                tenant=tenant,
                student=request_student,
            ),
            None,
        )]
    else:
        if requested_student_ids and requested_enrollment_ids:
            raise ValidationError(
                {"detail": "student_ids와 enrollment_ids 중 하나만 선택해 주세요."}
            )
        if not requested_student_ids and not requested_enrollment_ids:
            raise ValidationError({"student_ids": "추가할 학생을 한 명 이상 선택해 주세요."})
        if requested_enrollment_ids:
            enrollments = list(
                enrollments_for_clinic_tenant(tenant)
                .filter(id__in=requested_enrollment_ids, status="ACTIVE")
                .select_for_update()
                .order_by("id")
            )
            if len(enrollments) != len(requested_enrollment_ids):
                raise NotFound("선택한 수강 대상을 찾을 수 없습니다.")
            if len({enrollment.student_id for enrollment in enrollments}) != len(enrollments):
                raise ValidationError(
                    {"enrollment_ids": "같은 학생의 수강 대상은 한 번만 선택해 주세요."}
                )
            students = list(
                active_students_for_clinic_tenant(tenant)
                .filter(id__in=[enrollment.student_id for enrollment in enrollments])
                .select_for_update()
                .order_by("id")
            )
            if len(students) != len(enrollments):
                raise NotFound("선택한 수강 대상의 학생을 찾을 수 없습니다.")
            student_by_id = {student.id: student for student in students}
            enrollment_by_id = {enrollment.id: enrollment for enrollment in enrollments}
            booking_targets = [
                (
                    student_by_id[enrollment_by_id[enrollment_id].student_id],
                    enrollment_by_id[enrollment_id],
                )
                for enrollment_id in requested_enrollment_ids
            ]
        else:
            students = list(
                active_students_for_clinic_tenant(tenant)
                .filter(id__in=requested_student_ids)
                .select_for_update()
                .order_by("id")
            )
            if len(students) != len(requested_student_ids):
                raise NotFound("선택한 학생을 찾을 수 없습니다.")
            student_by_id = {student.id: student for student in students}
            booking_targets = [
                (student_by_id[student_id], None)
                for student_id in requested_student_ids
            ]

    sessions = list(
        Session.objects
        .filter(tenant=tenant, id__in=requested_session_ids)
        .select_for_update()
        .prefetch_related("target_lectures")
        .order_by("date", "start_time", "id")
    )
    if len(sessions) != len(requested_session_ids):
        raise NotFound("선택한 클리닉 시간대를 찾을 수 없습니다.")
    if len({session.date for session in sessions}) != 1:
        raise ValidationError(
            {"session_ids": "한 번에 신청하는 시간대는 같은 날짜여야 합니다."}
        )
    if len(sessions) > 1 and any(
        not session.allow_multi_slot_booking for session in sessions
    ):
        raise Conflict("선택한 클리닉 중 같은 날 여러 시간대 예약을 허용하지 않는 세션이 있습니다.")
    if len(sessions) > 1 and any(session.booking_mode == "time_range" for session in sessions):
        raise ValidationError(
            {"session_ids": "시간 범위 방식은 한 세션씩 예약해 주세요."}
        )
    for previous, current in zip(sessions, sessions[1:]):
        previous_start = datetime.datetime.combine(
            previous.date,
            previous.start_time,
        )
        previous_end = previous_start + datetime.timedelta(
            minutes=previous.duration_minutes,
        )
        current_start = datetime.datetime.combine(current.date, current.start_time)
        if previous_end != current_start:
            raise ValidationError(
                {"session_ids": "여러 시간대는 빈 시간 없이 연속해서 선택해 주세요."}
            )

    participants: list[SessionParticipant] = []
    notifications: list[ClinicNotificationEvent] = []
    exact_enrollment_reasons = clinic_reasons_for_unresolved_auto_links(
        tenant,
        [enrollment.id for _, enrollment in booking_targets if enrollment is not None],
    )
    for student, enrollment in booking_targets:
        for session in sessions:
            validated_data = {
                "session": session,
                "student": student,
                "enrollment": enrollment,
                "student_request_memo": student_request_memo,
                "memo": memo,
                "preferred_start_time": preferred_start_time,
                "preferred_end_time": preferred_end_time,
                "booking_start_time": booking_start_time,
                "booking_end_time": booking_end_time,
            }
            if enrollment is not None:
                validated_data["clinic_reason"] = exact_enrollment_reasons.get(
                    enrollment.id
                )
            result = create_participant(
                tenant=tenant,
                validated_data=validated_data,
                request_student=request_student,
            )
            participants.append(result.participant)
            if result.notification:
                notifications.append(result.notification)

    return ParticipantBulkWriteResult(
        participants=tuple(participants),
        notifications=tuple(notifications),
    )


@transaction.atomic
def change_participant_booking(
    *,
    tenant,
    participant_id: int,
    new_session_id: int | str,
    request_student,
    actor,
    memo=None,
    student_request_memo=None,
    preferred_start_time=None,
    preferred_end_time=None,
    booking_start_time=None,
    booking_end_time=None,
) -> ParticipantWriteResult:
    if not new_session_id:
        raise ValidationError({"detail": "new_session_id가 필요합니다."})
    try:
        new_session_id = int(new_session_id)
    except (TypeError, ValueError) as exc:
        raise ValidationError({"detail": "new_session_id는 숫자여야 합니다."}) from exc
    try:
        booking_identity = (
            SessionParticipant.objects
            .only("student_id")
            .get(pk=participant_id, tenant=tenant, student__deleted_at__isnull=True)
        )
    except SessionParticipant.DoesNotExist as exc:
        raise NotFound("예약을 찾을 수 없습니다.") from exc

    is_staff_change = request_student is None
    booking_student = request_student
    if is_staff_change:
        booking_student = (
            active_students_for_clinic_tenant(tenant)
            .filter(pk=booking_identity.student_id)
            .first()
        )
        if booking_student is None:
            raise NotFound("예약을 찾을 수 없습니다.")
    booking_student = _lock_active_student_for_booking(
        tenant=tenant,
        student=booking_student,
    )
    try:
        old_booking = (
            SessionParticipant.objects
            .select_for_update()
            .get(pk=participant_id, tenant=tenant, student__deleted_at__isnull=True)
        )
    except SessionParticipant.DoesNotExist as exc:
        raise NotFound("예약을 찾을 수 없습니다.") from exc
    if not is_staff_change:
        if old_booking.student_id != request_student.id:
            raise PermissionDenied("다른 학생의 예약을 변경할 수 없습니다.")
        if old_booking.status != SessionParticipant.Status.PENDING:
            raise PermissionDenied("승인 대기 중인 예약만 변경할 수 있습니다.")
    elif old_booking.status not in (
        SessionParticipant.Status.PENDING,
        SessionParticipant.Status.BOOKED,
        SessionParticipant.Status.NO_SHOW,
    ):
        raise ValidationError({"detail": "대기·예약·결석 상태에서만 보충 일정을 만들 수 있습니다."})
    if old_booking.session_id == new_session_id:
        raise ValidationError({"detail": "같은 세션으로는 변경할 수 없습니다."})

    try:
        new_session = (
            Session.objects
            .filter(tenant=tenant)
            .select_for_update()
            .get(pk=new_session_id)
        )
    except Session.DoesNotExist as exc:
        raise NotFound("변경할 세션을 찾을 수 없습니다.") from exc

    _validate_student_session_eligibility(tenant=tenant, student=booking_student, session=new_session)
    booking_start_time, booking_end_time = _validated_booking_range(
        session=new_session,
        booking_start_time=booking_start_time,
        booking_end_time=booking_end_time,
    )
    _assert_session_capacity(
        tenant=tenant,
        session=new_session,
        booking_start_time=booking_start_time,
        booking_end_time=booking_end_time,
    )
    _assert_no_active_duplicate(
        tenant=tenant,
        student=booking_student,
        session=new_session,
        exclude_participant_id=old_booking.id,
    )
    preferred_start_time, preferred_end_time = _validated_time_preference(
        session=new_session,
        preferred_start_time=preferred_start_time,
        preferred_end_time=preferred_end_time,
    )

    new_status = SessionParticipant.Status.BOOKED if is_staff_change else SessionParticipant.Status.PENDING
    if not is_staff_change and getattr(tenant, "clinic_auto_approve_booking", False):
        new_status = SessionParticipant.Status.BOOKED

    if is_staff_change:
        next_student_request_memo = old_booking.student_request_memo
        next_staff_memo = memo if memo is not None else old_booking.staff_memo
    else:
        next_student_request_memo = student_request_memo
        if next_student_request_memo is None:
            next_student_request_memo = memo
        if next_student_request_memo is None:
            next_student_request_memo = old_booking.student_request_memo
        next_staff_memo = old_booking.staff_memo

    enrollment_id = _preferred_active_enrollment_id(
        tenant=tenant,
        student=booking_student,
        session=new_session,
        preferred_enrollment_id=old_booking.enrollment_id,
    )

    try:
        new_booking = SessionParticipant.objects.create(
            tenant=tenant,
            session=new_session,
            student=booking_student,
            status=new_status,
            source=(
                SessionParticipant.Source.MANUAL
                if is_staff_change
                else SessionParticipant.Source.STUDENT_REQUEST
            ),
            enrollment_id=enrollment_id,
            participant_role="manual",
            memo=old_booking.memo or "",
            student_request_memo=next_student_request_memo or "",
            staff_memo=next_staff_memo or "",
            preferred_start_time=preferred_start_time,
            preferred_end_time=preferred_end_time,
            booking_start_time=booking_start_time,
            booking_end_time=booking_end_time,
        )
    except IntegrityError as exc:
        raise Conflict("이미 해당 세션에 예약된 학생입니다.") from exc

    old_session = old_booking.session
    if old_booking.status != SessionParticipant.Status.NO_SHOW:
        old_booking.status = SessionParticipant.Status.CANCELLED
        old_booking.status_changed_at = timezone.now()
        old_booking.status_changed_by = actor
        old_booking.save(
            update_fields=["status", "status_changed_at", "status_changed_by", "updated_at"]
        )

    cancel_pending_clinic_participant_reminders(
        tenant_id=tenant.id,
        participant_id=old_booking.id,
    )
    _move_participant_plan(
        old_participant=old_booking,
        new_participant=new_booking,
        actor=actor,
    )

    return ParticipantWriteResult(
        participant=new_booking,
        notification=_booking_change_notification(
            new_booking=new_booking,
            old_session=old_session,
            actor=actor,
        ),
    )
