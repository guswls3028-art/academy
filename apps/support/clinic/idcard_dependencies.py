"""Cross-domain dependencies for the student clinic ID card API."""

from __future__ import annotations

from typing import Any


def student_for_idcard_user(*, tenant: Any, user: Any) -> Any | None:
    from apps.domains.students.selectors import student_for_tenant_user

    return student_for_tenant_user(tenant, user, deleted="active")


def active_enrollments_for_student(*, tenant: Any, student: Any) -> list[Any]:
    from apps.domains.enrollment.selectors import enrollments_for_tenant

    return list(
        enrollments_for_tenant(tenant)
        .filter(student=student, status="ACTIVE")
        .select_related("lecture")
        .order_by("lecture__title", "id")
    )


def passcard_confirmed_enrollment_ids(
    *,
    tenant: Any,
    enrollment_ids: set[int],
) -> set[int]:
    """Map the passcard's student-level booking state onto enrollments."""
    if not enrollment_ids:
        return set()

    from apps.domains.enrollment.models import Enrollment

    enrollment_student_map = dict(
        Enrollment.objects.filter(
            tenant=tenant,
            id__in=enrollment_ids,
            student__tenant=tenant,
        ).values_list("id", "student_id")
    )
    from apps.domains.clinic.services.passcard_state import (
        passcard_confirmed_student_ids,
    )

    confirmed_student_ids = passcard_confirmed_student_ids(
        tenant=tenant,
        student_ids=set(enrollment_student_map.values()),
    )
    return {
        int(enrollment_id)
        for enrollment_id, student_id in enrollment_student_map.items()
        if student_id in confirmed_student_ids
    }


def ordered_sessions_by_enrollment(
    *,
    tenant: Any,
    enrollments: list[Any],
) -> dict[int, list[Any]]:
    if not enrollments:
        return {}

    from apps.domains.lectures.models import SectionAssignment
    from apps.domains.lectures.models import Session as LectureSession

    enrollment_ids = [int(enrollment.id) for enrollment in enrollments]
    lecture_ids = {int(enrollment.lecture_id) for enrollment in enrollments}
    section_by_enrollment = dict(
        SectionAssignment.objects.filter(
            tenant=tenant,
            enrollment_id__in=enrollment_ids,
        ).values_list("enrollment_id", "class_section_id")
    )
    sessions_by_lecture: dict[int, list[Any]] = {}
    for session in (
        LectureSession.objects.filter(
            lecture_id__in=lecture_ids,
            lecture__tenant=tenant,
        )
        .select_related("lecture")
        .order_by("lecture_id", "order")
    ):
        sessions_by_lecture.setdefault(int(session.lecture_id), []).append(session)

    result: dict[int, list[Any]] = {}
    for enrollment in enrollments:
        sessions = sessions_by_lecture.get(int(enrollment.lecture_id), [])
        section_id = section_by_enrollment.get(int(enrollment.id))
        if section_id:
            sessions = [
                session
                for session in sessions
                if int(session.section_id or 0) == int(section_id)
            ]
        result[int(enrollment.id)] = sessions
    return result


def unresolved_auto_clinic_links(
    *,
    tenant: Any,
    enrollment_ids: list[int],
) -> list[Any]:
    if not enrollment_ids:
        return []

    from apps.domains.progress.models import ClinicLink

    clinic_links = (
        ClinicLink.objects.filter(
            tenant=tenant,
            enrollment_id__in=enrollment_ids,
            is_auto=True,
            resolved_at__isnull=True,
            session__lecture__tenant=tenant,
        )
        .select_related("session__lecture")
        .order_by("-created_at", "-id")
    )
    from apps.domains.results.utils.clinic import filter_current_clinic_links

    return filter_current_clinic_links(clinic_links, tenant=tenant)


def passcard_required_dates_by_student(*, tenant: Any, student_ids: set[int]) -> dict:
    """Use all active enrollments, even for a single-session highlight request."""
    from apps.domains.clinic.services.passcard_state import passcard_required_booking_date
    from apps.domains.enrollment.selectors import enrollments_for_tenant

    enrollment_students = dict(
        enrollments_for_tenant(tenant).filter(
            status="ACTIVE",
            student_id__in=student_ids,
            student__tenant=tenant,
            lecture__tenant=tenant,
        ).values_list("id", "student_id")
    )
    links_by_student: dict[int, list[Any]] = {}
    for link in unresolved_auto_clinic_links(
        tenant=tenant, enrollment_ids=list(enrollment_students),
    ):
        student_id = enrollment_students[link.enrollment_id]
        links_by_student.setdefault(student_id, []).append(link)
    return {
        student_id: passcard_required_booking_date(links)
        for student_id, links in links_by_student.items()
    }


def clinic_link_source_projection(
    *,
    tenant: Any,
    clinic_links: list[Any],
) -> dict[int, dict[str, str | None]]:
    """Return tenant-safe source titles without inferring scope from filenames."""
    if not clinic_links:
        return {}

    from apps.domains.exams.models import Exam
    from apps.domains.homework_results.models import Homework

    session_ids = {
        int(link.session_id)
        for link in clinic_links
        if getattr(link, "session_id", None)
    }
    exam_ids = {
        int(link.source_id)
        for link in clinic_links
        if getattr(link, "source_type", None) == "exam"
        and getattr(link, "source_id", None)
    }
    homework_ids = {
        int(link.source_id)
        for link in clinic_links
        if getattr(link, "source_type", None) == "homework"
        and getattr(link, "source_id", None)
    }

    exams_by_source_session: dict[tuple[int, int], Any] = {}
    if exam_ids:
        exams = (
            Exam.objects.filter(
                id__in=exam_ids,
                tenant=tenant,
                exam_type=Exam.ExamType.REGULAR,
                is_active=True,
                sessions__id__in=session_ids,
            )
            .prefetch_related("sessions")
            .distinct()
        )
        for exam in exams:
            for session in exam.sessions.all():
                session_id = int(session.id)
                if session_id in session_ids:
                    exams_by_source_session[(int(exam.id), session_id)] = exam

    homeworks_by_source_session: dict[tuple[int, int], Any] = {}
    if homework_ids:
        homeworks = (
            Homework.objects.filter(
                id__in=homework_ids,
                tenant=tenant,
                homework_type=Homework.HomeworkType.REGULAR,
                session_id__in=session_ids,
            )
            .exclude(meta__removed_from_session_at__isnull=False)
        )
        homeworks_by_source_session = {
            (int(homework.id), int(homework.session_id)): homework
            for homework in homeworks
        }

    projections: dict[int, dict[str, str | None]] = {}
    for link in clinic_links:
        source_id = int(getattr(link, "source_id", 0) or 0)
        session_id = int(getattr(link, "session_id", 0) or 0)
        source = None
        if getattr(link, "source_type", None) == "exam":
            source = exams_by_source_session.get((source_id, session_id))
        elif getattr(link, "source_type", None) == "homework":
            source = homeworks_by_source_session.get((source_id, session_id))

        title = str(getattr(source, "title", "") or "").strip() or None
        projections[int(link.id)] = {
            "source_title": title,
            # Exam/Homework currently have no dedicated unit/range field.
            # Descriptions and source filenames are not scope SSOTs.
            "source_scope": None,
        }
    return projections
