"""
Enrollment / Lecture / Session / Student / Attendance 등 DB 조회·저장 — .objects. 접근을 adapters 내부로 한정 (Gate 7).
"""
from __future__ import annotations


def get_lecture_by_id_tenant(lecture_id, tenant):
    from apps.domains.lectures.models import Lecture
    return Lecture.objects.filter(id=lecture_id, tenant=tenant).first()


def lecture_filter_tenant(tenant):
    from apps.domains.lectures.models import Lecture
    return Lecture.objects.filter(tenant=tenant)


def active_non_system_lecture_count(tenant) -> int:
    from apps.domains.lectures.models import Lecture
    return Lecture.objects.filter(tenant=tenant, is_active=True, is_system=False).count()


def enrollment_filter_lecture_active_students(tenant, lecture):
    from apps.domains.enrollment.models import Enrollment
    return (
        Enrollment.objects.filter(
            tenant=tenant,
            lecture=lecture,
            status="ACTIVE",
        )
        .filter(student__deleted_at__isnull=True)
        .select_related("student")
    )


def session_queryset_select_related_lecture():
    from apps.domains.lectures.models import Session
    return Session.objects.select_related("lecture")


def session_aggregate_max_order(lecture):
    from django.db.models import Max
    from apps.domains.lectures.models import Session
    return Session.objects.filter(lecture=lecture).aggregate(max_order=Max("order"))


def get_session_enrollment_enrollment_ids_by_lecture(lecture):
    from apps.domains.enrollment.models import SessionEnrollment
    return (
        SessionEnrollment.objects.filter(session__lecture=lecture)
        .values_list("enrollment_id", flat=True)
        .distinct()
    )


def get_attendances_for_lecture_by_lecture(lecture, enrollments):
    from apps.domains.attendance.models import Attendance
    return Attendance.objects.filter(
        session__lecture=lecture,
        enrollment__in=enrollments,
    )


def student_exists_for_tenant(student_id, tenant, *, for_update=False):
    from apps.domains.students.models import Student
    queryset = Student.objects.filter(
        id=student_id,
        tenant=tenant,
        deleted_at__isnull=True,
    )
    if for_update:
        queryset = queryset.select_for_update()
    return queryset.exists()


def lock_active_student_ids_for_tenant(student_ids, tenant) -> tuple[int, ...]:
    """Lock active students in one stable order for overlapping batch writes."""
    from apps.domains.students.models import Student

    normalized_ids = sorted({int(student_id) for student_id in student_ids})
    if not normalized_ids:
        return ()
    return tuple(
        Student.objects.select_for_update()
        .filter(
            id__in=normalized_ids,
            tenant=tenant,
            deleted_at__isnull=True,
        )
        .order_by("id")
        .values_list("id", flat=True)
    )


def enrollment_get_or_create(tenant, lecture, student_id, defaults):
    from apps.domains.enrollment.models import Enrollment
    return Enrollment.objects.get_or_create(
        tenant=tenant,
        lecture=lecture,
        student_id=student_id,
        defaults=defaults,
    )


def session_enrollment_filter_delete(tenant, enrollment):
    from apps.domains.enrollment.models import SessionEnrollment
    return SessionEnrollment.objects.filter(
        tenant=tenant,
        enrollment=enrollment,
    ).delete()


def get_lecture_by_id_tenant_raw(lecture_id, tenant):
    from apps.domains.lectures.models import Lecture
    return Lecture.objects.filter(id=int(lecture_id), tenant=tenant).first()


def get_session_by_id_lecture(session_id, lecture):
    from apps.domains.lectures.models import Session
    return Session.objects.filter(id=session_id, lecture=lecture).first()


def get_session_by_id_with_lecture(session_id):
    from apps.domains.lectures.models import Session
    return Session.objects.select_related("lecture").filter(id=session_id).first()


def get_enrollment_by_id_with_lecture(enrollment_id, tenant):
    from apps.domains.enrollment.models import Enrollment
    return Enrollment.objects.select_related("lecture").filter(id=enrollment_id, tenant=tenant).first()


def get_enrollments_by_ids_with_lecture(enrollment_ids, tenant):
    from apps.domains.enrollment.models import Enrollment

    normalized_ids = sorted({int(enrollment_id) for enrollment_id in enrollment_ids})
    return list(
        Enrollment.objects.select_related("lecture", "student")
        .filter(id__in=normalized_ids, tenant=tenant)
        .order_by("id")
    )


def lock_enrollments_by_ids_with_lecture(enrollment_ids, tenant):
    """Lock enrollment rows after their Student rows have been locked."""
    from apps.domains.enrollment.models import Enrollment

    normalized_ids = sorted({int(enrollment_id) for enrollment_id in enrollment_ids})
    if not normalized_ids:
        return []
    return list(
        Enrollment.objects.select_for_update(of=("self",))
        .select_related("lecture", "student")
        .filter(id__in=normalized_ids, tenant=tenant)
        .order_by("id")
    )


def lock_enrollment_by_id_with_lecture(enrollment_id, tenant):
    rows = lock_enrollments_by_ids_with_lecture([enrollment_id], tenant)
    return rows[0] if rows else None


def lock_attendance_parent_rows(*, tenant, student_ids, enrollment_ids):
    """Lock attendance parents without imposing registration eligibility rules."""
    from apps.domains.students.models import Student

    locked_students = tuple(
        Student.objects.select_for_update(of=("self",))
        .filter(tenant=tenant, id__in=sorted(set(student_ids)))
        .order_by("id")
        .values_list("id", flat=True)
    )
    locked_enrollments = lock_enrollments_by_ids_with_lecture(enrollment_ids, tenant)
    return locked_students, tuple(row.id for row in locked_enrollments)


def session_enrollment_get_or_create(session, enrollment, defaults):
    from apps.domains.enrollment.models import SessionEnrollment
    return SessionEnrollment.objects.get_or_create(
        session=session,
        enrollment=enrollment,
        defaults=defaults,
    )


def attendance_get_or_create(session, enrollment, defaults):
    from apps.domains.attendance.models import Attendance
    return Attendance.objects.get_or_create(
        session=session,
        enrollment=enrollment,
        defaults=defaults,
    )


# --- enrollment/services.py 및 students 등에서 사용 ---

def get_tenant_by_id(tenant_id):
    from apps.core.models import Tenant
    return Tenant.objects.filter(id=tenant_id).first()


def get_lecture_by_id_tenant_id(lecture_id, tenant):
    from apps.domains.lectures.models import Lecture
    return Lecture.objects.filter(id=lecture_id, tenant=tenant).first()


def get_lecture_by_id_and_tenant_id(lecture_id, tenant_id):
    """tenant_id는 PK 값 (엑셀 핸들러 등)."""
    from apps.domains.lectures.models import Lecture
    return Lecture.objects.filter(id=int(lecture_id), tenant_id=tenant_id).first()


def active_student_exists(sid, tenant):
    from apps.domains.students.models import Student
    return Student.objects.filter(
        id=sid,
        tenant=tenant,
        deleted_at__isnull=True,
    ).exists()


def enrollment_get_or_create_ret(tenant, lecture, student_id, defaults):
    from apps.domains.enrollment.models import Enrollment
    return Enrollment.objects.get_or_create(
        tenant=tenant,
        lecture=lecture,
        student_id=student_id,
        defaults=defaults,
    )


def get_session_by_lecture_order(lecture, order):
    from apps.domains.lectures.models import Session
    return (
        Session.objects.filter(
            lecture=lecture,
            session_type=Session.SessionType.REGULAR,
            regular_order=order,
        )
        .order_by("order", "id")
        .first()
    )


def create_session(lecture, order):
    from apps.domains.lectures.models import Session
    return Session.objects.create(
        lecture=lecture,
        order=order,
        session_type=Session.SessionType.REGULAR,
        regular_order=order,
    )


def create_session_with_meta(lecture, order, title, date):
    from apps.domains.lectures.models import Session
    return Session.objects.create(
        lecture=lecture,
        order=order,
        session_type=Session.SessionType.REGULAR,
        regular_order=order,
        title=title,
        date=date,
    )


def session_enrollment_get_or_create_simple(session, enrollment, defaults):
    from apps.domains.enrollment.models import SessionEnrollment
    return SessionEnrollment.objects.get_or_create(
        session=session,
        enrollment=enrollment,
        defaults=defaults,
    )


def attendance_get_or_create_simple(session, enrollment, defaults):
    from apps.domains.attendance.models import Attendance
    return Attendance.objects.get_or_create(
        session=session,
        enrollment=enrollment,
        defaults=defaults,
    )


def session_enrollment_get_or_create_tenant(tenant, session, enrollment):
    from apps.domains.enrollment.models import SessionEnrollment
    return SessionEnrollment.objects.get_or_create(
        tenant=tenant,
        session=session,
        enrollment=enrollment,
    )


def attendance_get_or_create_tenant(tenant, enrollment, session, defaults, *, for_update=False):
    from apps.domains.attendance.models import Attendance
    queryset = Attendance.objects.select_for_update(of=("self",)) if for_update else Attendance.objects
    return queryset.get_or_create(
        tenant=tenant,
        enrollment=enrollment,
        session=session,
        defaults=defaults,
    )


def get_session_with_lecture_by_id_for_tenant(session_id, tenant):
    from apps.domains.lectures.models import Session
    return (
        Session.objects.select_related("lecture")
        .filter(id=session_id, lecture__tenant_id=tenant.id)
        .order_by("id")
        .first()
    )


def attendance_filter_session_tenant_statuses(session_id, tenant, statuses):
    from apps.domains.attendance.models import Attendance
    return (
        Attendance.objects.filter(
            session_id=session_id,
            session__lecture__tenant_id=tenant.id,
            status__in=statuses,
        )
        .exclude(enrollment__status="INACTIVE")
        .select_related("enrollment__student", "session__lecture")
        .order_by("id")
    )


def get_sessions_by_lecture(lecture):
    from apps.domains.lectures.models import Session
    return Session.objects.filter(lecture=lecture)


def get_sessions_filter(lecture, **kwargs):
    from apps.domains.lectures.models import Session
    return Session.objects.filter(lecture=lecture, **kwargs)


def get_session_by_id(session_id):
    from apps.domains.lectures.models import Session
    return Session.objects.filter(id=session_id).first()


def get_sessions_for_lecture_ordered(lecture):
    from apps.domains.lectures.models import Session
    return Session.objects.filter(lecture=lecture).order_by("order", "id")


def get_session_enrollment_enrollment_ids(tenant, lecture):
    from apps.domains.enrollment.models import SessionEnrollment
    return (
        SessionEnrollment.objects.filter(tenant=tenant, session__lecture=lecture)
        .values_list("enrollment_id", flat=True)
        .distinct()
    )


def get_enrollments_by_ids_active(enrollment_ids, tenant):
    from apps.domains.enrollment.models import Enrollment
    return (
        Enrollment.objects.filter(id__in=enrollment_ids, status="ACTIVE", tenant=tenant)
        .filter(student__deleted_at__isnull=True)
        .select_related("student")
        .order_by("student__name", "id")
    )


def get_enrollments_by_ids_all(enrollment_ids, tenant):
    """Matrix용: INACTIVE(퇴원) 포함 전체 조회"""
    from apps.domains.enrollment.models import Enrollment
    return (
        Enrollment.objects.filter(id__in=enrollment_ids, tenant=tenant)
        .filter(student__deleted_at__isnull=True)
        .select_related("student")
        .order_by("student__name", "id")
    )


def enrollment_student_map_by_ids(enrollment_ids, tenant=None):
    from apps.domains.enrollment.models import Enrollment

    if not enrollment_ids:
        return {}
    queryset = Enrollment.objects.select_related("student").filter(id__in=enrollment_ids)
    if tenant is not None:
        queryset = queryset.filter(tenant=tenant)
    return {
        enrollment.id: enrollment
        for enrollment in queryset.order_by("id")
    }


def get_attendances_for_lecture(tenant, lecture, enrollments):
    from apps.domains.attendance.models import Attendance
    return Attendance.objects.filter(
        tenant=tenant,
        session__lecture=lecture,
        enrollment__in=enrollments,
    )


def get_attendances_filter(session, **kwargs):
    from apps.domains.attendance.models import Attendance
    return Attendance.objects.filter(session=session, **kwargs)


def enrollment_get_or_create_session(session, lecture, tenant, student_id, defaults):
    from apps.domains.enrollment.models import Enrollment
    return Enrollment.objects.get_or_create(
        lecture=lecture,
        tenant=tenant,
        student_id=student_id,
        defaults=defaults,
    )


def session_enrollment_get_or_create_session_enrollment(session, enrollment, tenant, defaults):
    from apps.domains.enrollment.models import SessionEnrollment
    return SessionEnrollment.objects.get_or_create(
        session=session,
        enrollment=enrollment,
        tenant=tenant,
        defaults=defaults,
    )


def attendance_get_or_create_session(session, enrollment, tenant, defaults):
    from apps.domains.attendance.models import Attendance
    return Attendance.objects.get_or_create(
        session=session,
        enrollment=enrollment,
        tenant=tenant,
        defaults=defaults,
    )
