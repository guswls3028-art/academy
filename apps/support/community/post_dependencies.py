"""Cross-domain dependencies for community post views."""

from __future__ import annotations

from typing import Any, Callable


def get_request_student(request: Any) -> Any | None:
    from apps.domains.student_app.permissions import get_request_student as _get_request_student

    return _get_request_student(request, require_explicit_parent_child=False)


def visible_scope_node_ids_for_students(*, tenant: Any, student_ids: list[int]) -> set[int]:
    from apps.domains.community.models import ScopeNode
    from apps.domains.enrollment.models import Enrollment

    lecture_ids = Enrollment.objects.filter(
        tenant=tenant,
        student_id__in=student_ids,
        status="ACTIVE",
    ).values_list("lecture_id", flat=True)
    return set(
        ScopeNode.objects.filter(
            tenant=tenant,
            lecture_id__in=lecture_ids,
        ).values_list("id", flat=True)
    )


def student_activity_rank(*, tenant: Any, since: Any, my_score: int) -> tuple[int | None, int]:
    from django.db.models import Count, F, Q

    from apps.domains.students.models import Student

    ranking_qs = (
        Student.objects.filter(tenant=tenant, deleted_at__isnull=True)
        .annotate(
            pc=Count(
                "post_entities",
                filter=Q(post_entities__created_at__gte=since),
                distinct=True,
            ),
            rc=Count(
                "post_replies",
                filter=Q(post_replies__created_at__gte=since),
                distinct=True,
            ),
        )
        .annotate(activity_score=F("pc") + F("rc"))
        .filter(activity_score__gt=0)
    )
    total_active = ranking_qs.count()
    if my_score <= 0:
        return None, total_active
    higher = ranking_qs.filter(activity_score__gt=my_score).count()
    return higher + 1, total_active


def top_active_students_by_community_activity(*, tenant: Any, since: Any, limit: int) -> list[dict[str, Any]]:
    from django.db.models import Count, F, Q

    from apps.domains.students.models import Student

    students = (
        Student.objects.filter(tenant=tenant, deleted_at__isnull=True)
        .annotate(
            post_count=Count(
                "post_entities",
                filter=Q(post_entities__created_at__gte=since),
                distinct=True,
            ),
            reply_count=Count(
                "post_replies",
                filter=Q(post_replies__created_at__gte=since),
                distinct=True,
            ),
        )
        .annotate(activity_score=F("post_count") + F("reply_count"))
        .filter(activity_score__gt=0)
        .order_by("-activity_score", "name")[: int(limit)]
    )
    return [
        {
            "id": student.id,
            "name": student.name,
            "post_count": student.post_count,
            "reply_count": student.reply_count,
            "score": student.activity_score,
        }
        for student in students
    ]


def active_student_ids_for_tenant(*, tenant_id: int) -> list[int]:
    from apps.domains.students.models import Student

    return list(
        Student.objects.filter(
            tenant_id=int(tenant_id),
            deleted_at__isnull=True,
        ).values_list("id", flat=True)
    )


def active_student_summaries_for_tenant(*, tenant_id: int, limit: int) -> list[dict[str, Any]]:
    from apps.domains.students.models import Student

    return list(
        Student.objects.filter(
            tenant_id=int(tenant_id),
            deleted_at__isnull=True,
        ).values("id", "name")[: int(limit)]
    )


def active_student_for_assignment(*, student_id: int) -> Any | None:
    from apps.domains.students.models import Student

    return (
        Student.objects
        .filter(id=int(student_id), deleted_at__isnull=True)
        .select_related("tenant")
        .first()
    )


def student_user_for_qna_e2e(
    *,
    tenant_id: int,
    student_id: int | None = None,
) -> Any | None:
    from apps.domains.students.models import Student

    students = (
        Student.objects.filter(
            tenant_id=int(tenant_id),
            deleted_at__isnull=True,
            user__isnull=False,
            user__is_active=True,
        )
        .select_related("user")
        .order_by("id")
    )
    if student_id is not None:
        student = students.filter(id=int(student_id)).first()
    else:
        candidates = list(students[:2])
        student = candidates[0] if len(candidates) == 1 else None
    if student is None:
        return None
    return student.user


def get_reply_event_notifier() -> Callable[..., Any]:
    from apps.domains.messaging.services import send_event_notification

    return send_event_notification


def dispatch_qna_matchup_search(*, post: Any, attachment: Any, tenant: Any) -> Any:
    from apps.domains.ai.gateway import dispatch_job
    from apps.domains.community.services.attachment_urls import build_attachment_download_url

    download_url = build_attachment_download_url(
        attachment,
        expires_in=3600,
        force_download=False,
    )
    return dispatch_job(
        job_type="matchup_search_qna",
        payload={
            "download_url": download_url,
            "post_id": str(post.id),
            "attachment_id": str(attachment.id),
            "r2_key": attachment.r2_key,
            "tenant_id": str(tenant.id),
        },
        tenant_id=str(tenant.id),
        source_domain="community_qna",
        source_id=str(post.id),
    )
