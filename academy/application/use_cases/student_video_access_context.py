from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

from rest_framework import status
from academy.adapters.db.django import repositories_video as video_repo

from apps.domains.enrollment.selectors import active_enrollments_for_students
from apps.domains.student_app.permissions import get_request_student
from apps.domains.video.models import AccessMode
from apps.domains.video.policy import (
    is_video_progress_complete,
    normalize_video_progress,
)
from apps.domains.video.services.access_resolver import get_effective_access_mode


class StudentVideoAccessError(Exception):
    def __init__(
        self,
        detail: str,
        status_code: int = status.HTTP_403_FORBIDDEN,
        *,
        code: str | None = None,
    ):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code
        self.code = code


@dataclass(frozen=True)
class StudentSessionVideoContext:
    enrollment: object | None
    is_public_session: bool
    inactive_entitlements_by_video_id: Mapping[int, object] | None = None
    direct_entitlements_by_video_id: Mapping[int, object] | None = None


@dataclass(frozen=True)
class StudentVideoAccessContext:
    enrollment: object | None
    access_mode: AccessMode | None
    is_public_video: bool
    inactive_entitlement: object | None = None
    direct_entitlement: object | None = None

    @property
    def access_mode_value(self) -> str | None:
        return self.access_mode.value if self.access_mode else None

    @property
    def is_blocked(self) -> bool:
        return self.access_mode == AccessMode.BLOCKED


def get_students_for_request(request):
    tenant = getattr(request, "tenant", None)
    if not tenant or not getattr(request.user, "is_active", False):
        return []
    student = get_request_student(request)
    return [student] if student else []


def direct_entitlements_for_request_student(request, *, student=None):
    from apps.domains.video.services.direct_entitlements import (
        active_direct_video_entitlements_for_student,
    )

    tenant = getattr(request, "tenant", None)
    selected = student or get_request_student(request)
    if not tenant or not selected or getattr(selected, "tenant_id", None) != tenant.id:
        return []
    return active_direct_video_entitlements_for_student(
        tenant=tenant,
        student=selected,
    )


def get_enrollment_for_student(request, enrollment_id: Optional[int], lecture_id: Optional[int] = None):
    if not enrollment_id:
        return None
    students = get_students_for_request(request)
    if not students:
        raise StudentVideoAccessError("학생 정보를 확인할 수 없습니다.")
    tenant = getattr(request, "tenant", None)
    if not tenant:
        raise StudentVideoAccessError("tenant required")

    enrollment = (
        active_enrollments_for_students(
            tenant=tenant,
            students=students,
            include_system=True,
        )
        .filter(id=enrollment_id)
        .first()
    )
    if not enrollment:
        raise StudentVideoAccessError("해당 수강 정보에 접근할 수 없습니다.")
    if not _lecture_allows_student_learning(enrollment.lecture):
        raise StudentVideoAccessError("종료된 강의의 영상은 시청할 수 없습니다.")
    if lecture_id is not None and enrollment.lecture_id != lecture_id:
        raise StudentVideoAccessError(
            "수강 정보가 해당 강의와 일치하지 않습니다.",
            status.HTTP_400_BAD_REQUEST,
        )
    return enrollment


def find_active_enrollment_for_lecture(request, lecture_id: Optional[int], explicit_enrollment_id: Optional[int] = None):
    if explicit_enrollment_id:
        return get_enrollment_for_student(request, explicit_enrollment_id, lecture_id=lecture_id)

    students = get_students_for_request(request)
    tenant = getattr(request, "tenant", None)
    if not students or not tenant or not lecture_id:
        return None

    enrollment = (
        active_enrollments_for_students(
            tenant=tenant,
            students=students,
            include_system=True,
        )
        .filter(lecture_id=lecture_id)
        .order_by("-id")
        .first()
    )
    if enrollment and not _lecture_allows_student_learning(enrollment.lecture):
        return None
    return enrollment


def find_active_enrollment_for_video(request, video, explicit_enrollment_id: Optional[int] = None):
    session = getattr(video, "session", None)
    lecture_id = getattr(session, "lecture_id", None) if session else None
    return find_active_enrollment_for_lecture(
        request,
        lecture_id,
        explicit_enrollment_id=explicit_enrollment_id,
    )


def ensure_public_lecture_enrollment(request, lecture):
    from apps.domains.enrollment.models import Enrollment

    tenant = getattr(request, "tenant", None)
    student = get_request_student(request)
    if not tenant or not student:
        raise StudentVideoAccessError("학생 정보를 확인할 수 없습니다.")
    if getattr(lecture, "tenant_id", None) != tenant.id or getattr(student, "tenant_id", None) != tenant.id:
        raise StudentVideoAccessError("공개 영상은 해당 학원 소속 학생만 이용할 수 있습니다.")

    enrollment, _ = Enrollment.objects.get_or_create(
        tenant=tenant,
        student=student,
        lecture=lecture,
        defaults={"status": "ACTIVE"},
    )
    if enrollment.status != "ACTIVE":
        enrollment.status = "ACTIVE"
        enrollment.save(update_fields=["status", "updated_at"])
    return enrollment


def ensure_public_video_enrollment(request, video):
    session = getattr(video, "session", None)
    lecture = getattr(session, "lecture", None) if session else None
    if not lecture:
        tenant = getattr(request, "tenant", None) or getattr(video, "tenant", None)
        if not tenant:
            raise StudentVideoAccessError("공개 영상의 강의 정보를 확인할 수 없습니다.")
        from apps.domains.lectures.models import Lecture, Session
        from apps.domains.video.models import Video

        lecture = Lecture.get_or_create_system_lecture(tenant)
        session, _ = Session.objects.get_or_create(
            lecture=lecture,
            order=1,
            defaults={"title": "전체공개영상", "date": None},
        )
        if getattr(video, "session_id", None) is None:
            Video.objects.filter(id=video.id, tenant=tenant, session__isnull=True).update(session=session)
            video.session = session
            video.session_id = session.id
    return ensure_public_lecture_enrollment(request, lecture)


def student_can_access_session(request, session) -> bool:
    lecture = getattr(session, "lecture", None)
    if not lecture:
        return False
    if not _lecture_allows_student_learning(lecture):
        return False
    tenant = getattr(lecture, "tenant", None)
    if not tenant:
        return False
    tenant_id = getattr(tenant, "id", None)
    students = get_students_for_request(request)
    if not students:
        return False

    if getattr(lecture, "is_system", False):
        return any(getattr(s, "tenant_id", None) == tenant_id for s in students)

    return active_enrollments_for_students(
        tenant=tenant,
        students=students,
    ).filter(
        lecture=lecture,
        session_enrollments__session=session,
        session_enrollments__tenant=tenant,
    ).exists()


def _video_tenant_id(video) -> int | None:
    video_tenant_id = getattr(video, "tenant_id", None)
    if video_tenant_id is not None:
        return video_tenant_id
    session = getattr(video, "session", None)
    lecture = getattr(session, "lecture", None) if session else None
    return getattr(lecture, "tenant_id", None) if lecture else None


def _lecture_allows_student_learning(lecture) -> bool:
    """Keep the tenant-wide system library open while closing ended paid lectures."""
    return bool(
        lecture
        and (
            getattr(lecture, "is_system", False)
            or getattr(lecture, "is_active", False)
        )
    )


def lecture_allows_student_learning(lecture) -> bool:
    """Public policy boundary shared by playback locking and access resolution."""
    return _lecture_allows_student_learning(lecture)


def is_public_video(video) -> bool:
    from apps.domains.video.models import Video

    return getattr(video, "visibility", Video.Visibility.ENROLLED) == Video.Visibility.PUBLIC


def student_can_access_video(request, video) -> bool:
    tenant = getattr(request, "tenant", None)
    if not tenant:
        return False
    if _video_tenant_id(video) != tenant.id:
        return False

    session = getattr(video, "session", None)
    lecture = getattr(session, "lecture", None) if session else None
    if lecture and not _lecture_allows_student_learning(lecture):
        return False

    if is_public_video(video):
        return any(getattr(student, "tenant_id", None) == tenant.id for student in get_students_for_request(request))

    return bool(session and student_can_access_session(request, session))


def resolve_student_session_video_context(
    request,
    session,
    *,
    explicit_enrollment_id: Optional[int] = None,
) -> StudentSessionVideoContext:
    lecture = getattr(session, "lecture", None)
    is_public = bool(lecture and getattr(lecture, "is_system", False))

    if is_public and student_can_access_session(request, session):
        return StudentSessionVideoContext(
            enrollment=ensure_public_lecture_enrollment(request, lecture),
            is_public_session=True,
        )

    lecture_id = getattr(lecture, "id", None)
    active_error = None
    try:
        enrollment = find_active_enrollment_for_lecture(
            request,
            lecture_id,
            explicit_enrollment_id=explicit_enrollment_id,
        )
    except StudentVideoAccessError as exc:
        active_error = exc
        enrollment = None
    if enrollment is not None and not video_repo.session_enrollment_exists(session, enrollment):
        raise StudentVideoAccessError("이 차시의 영상을 볼 수 있는 권한이 없습니다.")
    inactive_entitlements = {}
    direct_entitlements = {}
    if enrollment is None and not is_public:
        enrollment, inactive_entitlements = _find_inactive_entitlements_for_session(
            request,
            session,
            explicit_enrollment_id=explicit_enrollment_id,
        )
    if enrollment is None and not inactive_entitlements and not is_public:
        direct_entitlements = _find_direct_entitlements_for_session(
            request,
            session,
            explicit_enrollment_id=explicit_enrollment_id,
        )
    if (
        enrollment is None
        and not direct_entitlements
        and not student_can_access_session(request, session)
    ):
        if active_error is not None and active_error.status_code == status.HTTP_400_BAD_REQUEST:
            raise active_error
        detail = (
            "공개 영상은 해당 학원 소속 학생만 이용할 수 있습니다."
            if is_public
            else "이 차시의 영상을 볼 수 있는 권한이 없습니다."
        )
        raise StudentVideoAccessError(detail)

    return StudentSessionVideoContext(
        enrollment=enrollment,
        is_public_session=is_public,
        inactive_entitlements_by_video_id=inactive_entitlements,
        direct_entitlements_by_video_id=direct_entitlements,
    )


def _find_inactive_entitlements_for_session(
    request,
    session,
    *,
    explicit_enrollment_id: Optional[int] = None,
):
    from apps.domains.video.services.inactive_entitlements import (
        active_entitlements_for_student,
        get_active_inactive_video_entitlement,
    )

    tenant = getattr(request, "tenant", None)
    students = get_students_for_request(request)
    if not tenant or not students:
        return None, {}

    matches = []
    for student in students:
        candidates = active_entitlements_for_student(tenant=tenant, student=student).filter(
            video__session=session,
        )
        if explicit_enrollment_id is not None:
            candidates = candidates.filter(enrollment_id=explicit_enrollment_id)
        for candidate in candidates:
            if candidate.video.source_type == candidate.video.SourceType.YOUTUBE:
                raise StudentVideoAccessError(
                    "퇴원 후 개별 영상 권한은 YouTube 영상에 적용할 수 없습니다.",
                    code="video_source_unsupported",
                )
            active = get_active_inactive_video_entitlement(
                video=candidate.video,
                enrollment=candidate.enrollment,
            )
            if active is not None:
                matches.append(active)

    enrollment_ids = {item.enrollment_id for item in matches}
    if len(enrollment_ids) != 1:
        return None, {}
    enrollment = matches[0].enrollment
    return enrollment, {item.video_id: item for item in matches}


def _find_direct_entitlements_for_session(
    request,
    session,
    *,
    explicit_enrollment_id: Optional[int] = None,
):
    from apps.domains.video.services.direct_entitlements import (
        active_direct_video_entitlements_for_student,
    )

    if explicit_enrollment_id is not None:
        return {}
    tenant = getattr(request, "tenant", None)
    students = get_students_for_request(request)
    if not tenant or not students:
        return {}
    matches = []
    for student in students:
        matches.extend(
            entitlement
            for entitlement in active_direct_video_entitlements_for_student(
                tenant=tenant,
                student=student,
            )
            if entitlement.video.session_id == session.id
        )
    student_ids = {item.student_id for item in matches}
    if len(student_ids) != 1:
        return {}
    return {item.video_id: item for item in matches}


def _find_inactive_entitlement_for_video(
    request,
    video,
    *,
    explicit_enrollment_id: Optional[int] = None,
):
    from apps.domains.video.services.inactive_entitlements import (
        active_entitlements_for_student,
        get_active_inactive_video_entitlement,
    )

    tenant = getattr(request, "tenant", None)
    students = get_students_for_request(request)
    if not tenant or not students:
        return None, None

    matches = []
    for student in students:
        candidates = active_entitlements_for_student(tenant=tenant, student=student).filter(
            video=video,
        )
        if explicit_enrollment_id is not None:
            candidates = candidates.filter(enrollment_id=explicit_enrollment_id)
        for candidate in candidates:
            if candidate.video.source_type == candidate.video.SourceType.YOUTUBE:
                raise StudentVideoAccessError(
                    "퇴원 후 개별 영상 권한은 YouTube 영상에 적용할 수 없습니다.",
                    code="video_source_unsupported",
                )
            active = get_active_inactive_video_entitlement(
                video=video,
                enrollment=candidate.enrollment,
            )
            if active is not None:
                matches.append(active)

    if len(matches) != 1:
        return None, None
    return matches[0].enrollment, matches[0]


def _find_direct_entitlement_for_video(
    request,
    video,
    *,
    explicit_enrollment_id: Optional[int] = None,
):
    from apps.domains.video.services.direct_entitlements import (
        get_active_direct_video_entitlement,
    )

    if explicit_enrollment_id is not None:
        return None
    tenant = getattr(request, "tenant", None)
    students = get_students_for_request(request)
    if not tenant or not students:
        return None
    matches = [
        entitlement
        for student in students
        if (
            entitlement := get_active_direct_video_entitlement(
                tenant=tenant,
                student=student,
                video=video,
            )
        )
        is not None
    ]
    return matches[0] if len(matches) == 1 else None


def resolve_student_video_access_context(
    request,
    video,
    *,
    explicit_enrollment_id: Optional[int] = None,
    allow_public_enrollment_write: bool = True,
) -> StudentVideoAccessContext:
    if is_public_video(video):
        if not student_can_access_video(request, video):
            raise StudentVideoAccessError("공개 영상은 해당 학원 소속 학생만 이용할 수 있습니다.")
        enrollment = (
            ensure_public_video_enrollment(request, video)
            if allow_public_enrollment_write
            else find_active_enrollment_for_video(
                request,
                video,
                explicit_enrollment_id=explicit_enrollment_id,
            )
        )
        return StudentVideoAccessContext(
            enrollment=enrollment,
            access_mode=None,
            is_public_video=True,
        )

    active_error = None
    try:
        enrollment = find_active_enrollment_for_video(
            request,
            video,
            explicit_enrollment_id=explicit_enrollment_id,
        )
    except StudentVideoAccessError as exc:
        active_error = exc
        enrollment = None
    if enrollment is not None and not video_repo.session_enrollment_exists(video.session, enrollment):
        raise StudentVideoAccessError("이 차시의 영상을 볼 수 있는 권한이 없습니다.")
    inactive_entitlement = None
    direct_entitlement = None
    if enrollment is None:
        enrollment, inactive_entitlement = _find_inactive_entitlement_for_video(
            request,
            video,
            explicit_enrollment_id=explicit_enrollment_id,
        )
    if enrollment is None and inactive_entitlement is None:
        direct_entitlement = _find_direct_entitlement_for_video(
            request,
            video,
            explicit_enrollment_id=explicit_enrollment_id,
        )
    if not enrollment and direct_entitlement is None:
        if active_error is not None and active_error.status_code == status.HTTP_400_BAD_REQUEST:
            raise active_error
        raise StudentVideoAccessError("이 영상을 볼 수 있는 수강 또는 개별 권한이 필요합니다.")

    return StudentVideoAccessContext(
        enrollment=enrollment,
        access_mode=(
            AccessMode.FREE_REVIEW
            if direct_entitlement is not None
            else get_effective_access_mode(video=video, enrollment=enrollment)
        ),
        is_public_video=False,
        inactive_entitlement=inactive_entitlement,
        direct_entitlement=direct_entitlement,
    )


def ensure_student_video_watch_allowed(context: StudentVideoAccessContext) -> None:
    if context.is_blocked:
        raise StudentVideoAccessError("이 영상은 시청이 제한되었습니다.")
