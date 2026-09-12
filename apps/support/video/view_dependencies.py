"""Cross-domain dependencies for video views."""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.db.models import Q


def get_public_video_session(*, tenant: Any) -> tuple[Any, Any]:
    """Read the existing container, including legacy IDs, without normalizing it."""
    from apps.domains.lectures.models import Lecture, Session

    lecture = (
        Lecture.objects.filter(tenant=tenant)
        .filter(Q(is_system=True) | Q(title="전체공개영상"))
        .order_by("-is_system", "id")
        .first()
    )
    session = (
        Session.objects.filter(lecture=lecture, order=1).first()
        if lecture is not None else None
    )
    return lecture, session


def lock_session_for_video_upload(session: Any) -> Any:
    from apps.domains.lectures.models import Session

    return Session.objects.select_for_update().select_related("lecture__tenant").get(pk=session.pk)


@transaction.atomic
def get_or_create_public_video_session(*, tenant: Any) -> tuple[Any, Any]:
    from apps.core.models import Tenant
    from apps.domains.lectures.models import Lecture, Session

    # All explicit preparation requests serialize before the existing owner
    # checks/normalizes legacy containers. Existing DB uniqueness stays intact.
    tenant = Tenant.objects.select_for_update().get(pk=tenant.pk)
    lecture = Lecture.get_or_create_system_lecture(tenant)
    session, _ = Session.objects.get_or_create(
        lecture=lecture,
        order=1,
        defaults={"title": "전체공개영상", "date": None},
    )
    return lecture, session


def prepare_legacy_public_video_upload_session(session: Any) -> Any:
    """Keep old clients' explicit upload POSTs public without writing on GET."""
    from apps.core.models import Tenant

    if session.lecture.is_system or session.lecture.title != "전체공개영상" or session.order != 1:
        return session
    with transaction.atomic():
        tenant = Tenant.objects.select_for_update().get(pk=session.lecture.tenant_id)
        _lecture, existing = get_public_video_session(tenant=tenant)
        if existing is None or existing.pk != session.pk:
            return session
        _lecture, prepared = get_or_create_public_video_session(tenant=tenant)
        return prepared


def get_staff_for_video_upload(*, user: Any, tenant: Any) -> Any | None:
    from apps.domains.staffs.models import Staff

    return Staff.objects.filter(user=user, tenant=tenant).first()


def get_staff_for_video_social(*, user: Any, tenant: Any) -> Any | None:
    return get_staff_for_video_upload(user=user, tenant=tenant)


def clinic_highlight_map_for_video_stats(*, tenant: Any, enrollment_ids: set[int]) -> dict[int, bool]:
    from apps.domains.results.utils.clinic_highlight import compute_clinic_highlight_map

    if not tenant or not enrollment_ids:
        return {}
    return compute_clinic_highlight_map(
        tenant=tenant,
        enrollment_ids=enrollment_ids,
    )
