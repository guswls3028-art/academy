# PATH: apps/domains/clinic/views/idcard_views.py
"""
학생 클리닉 인증(차시별 합불) 전용 API
GET /api/v1/clinic/idcard/
- 단일 진실: progress.ClinicLink(is_auto=True, resolved_at__isnull=True)
- 서버 기준 오늘 날짜 반환 (위조 방지)
"""
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from apps.core.permissions import TenantResolved
from apps.domains.clinic.color_utils import get_effective_clinic_colors
from apps.domains.clinic.models import SessionParticipant
from apps.domains.clinic.services.passcard_state import (
    passcard_booking_covers_requirements,
    passcard_required_booking_date,
    passcard_tenant_booking_q,
    passcard_visible_booking_q,
)
from apps.support.clinic.idcard_dependencies import (
    active_enrollments_for_student,
    clinic_link_source_projection,
    ordered_sessions_by_enrollment,
    student_for_idcard_user,
    unresolved_auto_clinic_links,
)


BOOKING_STATUS_LABELS = {
    "none": "예약 없음",
    "required": "예약 필요",
    "pending": "승인 대기",
    "booked": "예약 확정",
    "attended": "클리닉 진행 중",
    "completed": "클리닉 진행 완료",
}


def _participant_schedule(participant):
    session = getattr(participant, "session", None)
    return (
        getattr(session, "date", None) or participant.requested_date,
        getattr(session, "start_time", None) or participant.requested_start_time,
        getattr(session, "location", None),
        getattr(session, "title", "") or "",
    )


def _valid_booking_projection(*, tenant, student, local_date, required_date=None):
    """Project pending/confirmed bookings that still affect the passcard."""
    participants = list(
        SessionParticipant.objects.filter(
            tenant=tenant,
            student=student,
        )
        .filter(passcard_tenant_booking_q(tenant=tenant))
        .filter(passcard_visible_booking_q(local_date=local_date))
        .select_related("session")
        .order_by("id")
    )
    projected = []
    for participant in participants:
        if not passcard_booking_covers_requirements(
            participant=participant, required_date=required_date,
        ):
            continue
        schedule_date, start_time, location, title = _participant_schedule(participant)
        if participant.status in (
            SessionParticipant.Status.PENDING,
            SessionParticipant.Status.BOOKED,
        ):
            is_valid = bool(schedule_date and schedule_date >= local_date)
        else:
            # Existing work may remain in progress after midnight, but cannot
            # cover an assessment newer than this clinic's scheduled date.
            is_valid = participant.completed_at is None
        if not is_valid:
            continue

        status = participant.status
        projected.append({
            "participant_id": int(participant.id),
            "session_id": int(participant.session_id) if participant.session_id else None,
            "title": title,
            "status": status,
            "status_label": BOOKING_STATUS_LABELS[status],
            "date": schedule_date.isoformat() if schedule_date else None,
            "start_time": start_time.isoformat() if start_time else None,
            "location": location,
        })

    status_order = {"attended": 0, "booked": 1, "pending": 2}
    projected.sort(key=lambda booking: (
        booking["date"] or "9999-12-31",
        booking["start_time"] or "23:59:59",
        status_order[booking["status"]],
        booking["participant_id"],
    ))
    return projected


def _profile_photo_url(request, student):
    if not getattr(student, "profile_photo", None):
        return None
    try:
        return request.build_absolute_uri(student.profile_photo.url)
    except (ValueError, AttributeError, Exception):
        return None


def _response_payload(
    *,
    student_name: str = "",
    profile_photo_url: str | None = None,
    colors: list[str],
    histories: list[dict] | None = None,
    current_targets: list[dict] | None = None,
    lectures: list[dict] | None = None,
    valid_bookings: list[dict] | None = None,
    server_now=None,
):
    now = server_now or timezone.localtime(timezone.now())
    histories = histories or []
    current_targets = current_targets or []
    valid_bookings = valid_bookings or []
    clinic_required = bool(
        current_targets or any(h["clinic_required"] for h in histories)
    )
    return_protecting_bookings = [
        booking
        for booking in valid_bookings
        if booking["status"] in {"booked", "attended"}
    ]
    current_booking = (
        return_protecting_bookings[0]
        if clinic_required and return_protecting_bookings
        else valid_bookings[0] if valid_bookings else None
    )
    if not clinic_required:
        passcard_state = "PASSED"
    elif return_protecting_bookings:
        passcard_state = "BOOKING_CONFIRMED"
    else:
        passcard_state = "CLINIC_REQUIRED"
    booking_status = (
        current_booking["status"]
        if current_booking
        else "required" if clinic_required else "none"
    )
    return {
        "student_name": student_name,
        "profile_photo_url": profile_photo_url,
        "background_colors": colors[:3],
        "server_date": now.date().isoformat(),
        "server_datetime": now.isoformat(),
        "histories": histories,
        "current_targets": current_targets,
        "lectures": lectures or [],
        # ClinicLink verdict and temporary departure protection are separate axes.
        "current_result": "FAIL" if clinic_required else "SUCCESS",
        "passcard_state": passcard_state,
        "can_leave": passcard_state != "CLINIC_REQUIRED",
        "booking_status": booking_status,
        "booking_status_label": BOOKING_STATUS_LABELS[booking_status],
        "current_booking": current_booking,
        "valid_bookings": valid_bookings,
    }


class StudentClinicIdcardView(APIView):
    """
    GET /clinic/idcard/
    학생 본인 차시별 합불 + 클리닉 대상 여부.
    """
    permission_classes = [IsAuthenticated, TenantResolved]

    def get(self, request):
        user = request.user
        tenant = request.tenant
        student = student_for_idcard_user(tenant=tenant, user=user)
        local_now = timezone.localtime(timezone.now())

        # 패스카드 배경 색상 (매일 자동 3색 또는 저장값)
        colors = get_effective_clinic_colors(tenant) if tenant else ["#ef4444", "#3b82f6", "#22c55e"]

        if not student:
            return Response(_response_payload(colors=colors, server_now=local_now))

        # tenant is guaranteed by TenantResolved permission
        # 활성 강의 전체를 기준으로 집계한다. 한 학생이 여러 강의를 수강해도
        # 다른 강의의 미해결 ClinicLink가 패스카드에서 누락되면 안 된다.
        enrollments = active_enrollments_for_student(
            tenant=tenant,
            student=student,
        )
        enrollment_ids = [int(enrollment.id) for enrollment in enrollments]
        clinic_links = unresolved_auto_clinic_links(
            tenant=tenant,
            enrollment_ids=enrollment_ids,
        )
        valid_bookings = _valid_booking_projection(
            tenant=tenant,
            student=student,
            local_date=local_now.date(),
            required_date=passcard_required_booking_date(clinic_links),
        )

        if not enrollments:
            return Response(
                _response_payload(
                    student_name=getattr(student, "name", "") or "",
                    profile_photo_url=_profile_photo_url(request, student),
                    colors=colors,
                    valid_bookings=valid_bookings,
                    server_now=local_now,
                )
            )

        source_projection = clinic_link_source_projection(
            tenant=tenant,
            clinic_links=clinic_links,
        )
        unresolved_pairs = {
            (int(link.enrollment_id), int(link.session_id))
            for link in clinic_links
        }
        sessions_by_enrollment = ordered_sessions_by_enrollment(
            tenant=tenant,
            enrollments=enrollments,
        )

        histories = []
        lectures = []
        for enrollment in enrollments:
            lecture = enrollment.lecture
            lectures.append({
                "id": int(lecture.id),
                "title": lecture.title,
                "color": getattr(lecture, "color", None),
                "chip_label": getattr(lecture, "chip_label", None),
            })
            # section_mode 대응: 학생이 배정된 반의 세션만 조회
            for sess in sessions_by_enrollment.get(int(enrollment.id), []):
                clinic_required = (
                    int(enrollment.id),
                    int(sess.id),
                ) in unresolved_pairs
                histories.append({
                    "enrollment_id": int(enrollment.id),
                    "lecture_id": int(lecture.id),
                    "lecture_title": lecture.title,
                    "lecture_color": getattr(lecture, "color", None),
                    "lecture_chip_label": getattr(lecture, "chip_label", None),
                    "session_id": int(sess.id),
                    "session_order": sess.order,
                    "session_title": sess.title or "",
                    "passed": not clinic_required,
                    "clinic_required": clinic_required,
                })

        current_targets = []
        for link in clinic_links:
            sess = link.session
            lecture = sess.lecture
            source = source_projection.get(int(link.id), {})
            current_targets.append({
                "clinic_link_id": int(link.id),
                "enrollment_id": int(link.enrollment_id),
                "lecture_id": int(lecture.id),
                "lecture_title": lecture.title,
                "lecture_color": getattr(lecture, "color", None),
                "lecture_chip_label": getattr(lecture, "chip_label", None),
                "session_id": int(sess.id),
                "session_order": sess.order,
                "session_title": sess.title or "",
                "source_type": getattr(link, "source_type", None),
                "source_id": getattr(link, "source_id", None),
                "source_title": source.get("source_title"),
                "source_scope": source.get("source_scope"),
                "created_at": getattr(link, "created_at", None),
            })

        # 프로필 사진 URL (신원 확인용) - 기존 방식 사용
        return Response(
            _response_payload(
                student_name=getattr(student, "name", "") or "",
                profile_photo_url=_profile_photo_url(request, student),
                colors=colors,
                histories=histories,
                current_targets=current_targets,
                lectures=lectures,
                valid_bookings=valid_bookings,
                server_now=local_now,
            )
        )
