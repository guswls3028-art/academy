# apps/domains/results/utils/clinic.py
from __future__ import annotations

from typing import Any, Iterable, Set

from django.apps import apps

from apps.support.results.progress_read_dependencies import (
    clinic_link_queryset_for_session,
    completed_enrollment_ids_for_session,
    completed_session_progress_pairs,
)


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _link_source_id(link: Any, source_type: str) -> int | None:
    meta = link.meta if isinstance(getattr(link, "meta", None), dict) else {}
    if getattr(link, "source_type", None) == source_type:
        return _int_or_none(
            getattr(link, "source_id", None) or meta.get(f"{source_type}_id")
        )
    if getattr(link, "source_type", None) is None:
        return _int_or_none(meta.get(f"{source_type}_id"))
    return None


def filter_live_source_links(
    links: Iterable[Any],
    *,
    tenant: Any,
) -> list[Any]:
    """
    ClinicLink read-side guard.

    운영 노출의 SSOT는 unresolved ClinicLink지만, 원본 시험/과제가 이미 차시에서
    제거된 링크는 SOURCE_REMOVED 전이가 누락돼도 화면/통계에서 fail closed 한다.
    """
    links_list = list(links)
    if not links_list or tenant is None:
        return []

    session_ids = {
        int(getattr(link, "session_id", 0) or 0)
        for link in links_list
        if getattr(link, "session_id", None)
    }
    exam_ids = {
        exam_id
        for link in links_list
        for exam_id in [_link_source_id(link, "exam")]
        if exam_id is not None
    }
    homework_ids = {
        homework_id
        for link in links_list
        for homework_id in [_link_source_id(link, "homework")]
        if homework_id is not None
    }

    live_exam_pairs: set[tuple[int, int]] = set()
    explicitly_targeted_exam_ids: set[int] = set()
    live_exam_target_pairs: set[tuple[int, int]] = set()
    pending_omr_pairs: set[tuple[int, int]] = set()
    if exam_ids and session_ids:
        Exam = apps.get_model("exams", "Exam")
        ExamEnrollment = apps.get_model("exams", "ExamEnrollment")
        live_exam_pairs = {
            (int(exam_id), int(session_id))
            for exam_id, session_id in Exam.objects.filter(
                tenant=tenant,
                exam_type="regular",
                is_active=True,
                id__in=exam_ids,
                sessions__id__in=session_ids,
            ).values_list("id", "sessions__id")
        }
        explicitly_targeted_exam_ids = {
            int(exam_id)
            for exam_id in ExamEnrollment.objects.filter(
                exam_id__in=exam_ids,
                exam__tenant=tenant,
            ).values_list("exam_id", flat=True)
        }
        live_exam_target_pairs = {
            (int(exam_id), int(enrollment_id))
            for exam_id, enrollment_id in ExamEnrollment.objects.filter(
                exam_id__in=exam_ids,
                exam__tenant=tenant,
                enrollment__tenant=tenant,
            ).values_list("exam_id", "enrollment_id")
        }
        Result = apps.get_model("results", "Result")
        candidate_results = list(
            Result.objects.filter(
                target_type="exam",
                target_id__in=exam_ids,
                enrollment_id__in={
                    int(getattr(link, "enrollment_id", 0) or 0)
                    for link in links_list
                },
                enrollment__tenant=tenant,
            )
        )
        from apps.domains.results.services.omr_subjective_completion import (
            pending_omr_result_ids,
        )

        pending_result_ids = pending_omr_result_ids(candidate_results)
        pending_omr_pairs = {
            (int(result.target_id), int(result.enrollment_id))
            for result in candidate_results
            if int(result.id) in pending_result_ids
        }

    live_homework_pairs: set[tuple[int, int]] = set()
    live_homework_assignment_triples: set[tuple[int, int, int]] = set()
    if homework_ids and session_ids:
        Homework = apps.get_model("homework_results", "Homework")
        live_homework_pairs = {
            (int(homework_id), int(session_id))
            for homework_id, session_id in Homework.objects.filter(
                tenant=tenant,
                homework_type="regular",
                id__in=homework_ids,
                session_id__in=session_ids,
            )
            .exclude(meta__removed_from_session_at__isnull=False)
            .values_list("id", "session_id")
        }
        HomeworkAssignment = apps.get_model("homework", "HomeworkAssignment")
        live_homework_assignment_triples = {
            (int(homework_id), int(session_id), int(enrollment_id))
            for homework_id, session_id, enrollment_id in HomeworkAssignment.objects.filter(
                tenant=tenant,
                homework_id__in=homework_ids,
                session_id__in=session_ids,
            ).values_list("homework_id", "session_id", "enrollment_id")
        }

    live_links: list[Any] = []
    for link in links_list:
        source_type = getattr(link, "source_type", None)
        session_id = int(getattr(link, "session_id", 0) or 0)

        exam_id = _link_source_id(link, "exam")
        if exam_id is not None:
            enrollment_id = int(getattr(link, "enrollment_id", 0) or 0)
            if (
                (exam_id, session_id) in live_exam_pairs
                and (exam_id, enrollment_id) not in pending_omr_pairs
                and (
                    exam_id not in explicitly_targeted_exam_ids
                    or (exam_id, enrollment_id) in live_exam_target_pairs
                )
            ):
                live_links.append(link)
            continue

        homework_id = _link_source_id(link, "homework")
        if homework_id is not None:
            enrollment_id = int(getattr(link, "enrollment_id", 0) or 0)
            if (
                (homework_id, session_id) in live_homework_pairs
                and (homework_id, session_id, enrollment_id)
                in live_homework_assignment_triples
            ):
                live_links.append(link)
            continue

        # Ambiguous legacy automatic links without source metadata remain visible.
        if source_type is None:
            live_links.append(link)

    return live_links


def filter_current_clinic_links(clinic_links: Iterable[Any], *, tenant: Any) -> list[Any]:
    """Apply the shared current-target source and completed-progress contract."""
    live_links = filter_live_source_links(clinic_links, tenant=tenant)
    if not live_links:
        return []

    session_ids = list({int(link.session_id or 0) for link in live_links} - {0})
    enrollment_ids = list({int(link.enrollment_id or 0) for link in live_links} - {0})
    completed_pairs = completed_session_progress_pairs(
        session_ids=session_ids,
        enrollment_ids=enrollment_ids,
    )
    return [
        link
        for link in live_links
        if (int(link.enrollment_id), int(link.session_id)) not in completed_pairs
    ]


def filter_tenant_consistent_source_links(
    links: Iterable[Any],
    *,
    tenant: Any,
) -> list[Any]:
    """Reject a polymorphic source ID when it currently belongs to another tenant.

    Missing sources are retained so resolved SOURCE_REMOVED history remains visible;
    unresolved callers apply ``filter_live_source_links`` afterwards.
    """
    links_list = list(links)
    if not links_list or tenant is None:
        return []

    exam_ids = {
        source_id
        for link in links_list
        for source_id in [_link_source_id(link, "exam")]
        if source_id is not None
    }
    homework_ids = {
        source_id
        for link in links_list
        for source_id in [_link_source_id(link, "homework")]
        if source_id is not None
    }

    foreign_exam_ids: set[int] = set()
    if exam_ids:
        Exam = apps.get_model("exams", "Exam")
        foreign_exam_ids = {
            int(exam_id)
            for exam_id in Exam.objects.filter(id__in=exam_ids)
            .exclude(tenant=tenant)
            .values_list("id", flat=True)
        }

    foreign_homework_ids: set[int] = set()
    if homework_ids:
        Homework = apps.get_model("homework_results", "Homework")
        foreign_homework_ids = {
            int(homework_id)
            for homework_id in Homework.objects.filter(id__in=homework_ids)
            .exclude(tenant=tenant)
            .values_list("id", flat=True)
        }

    consistent: list[Any] = []
    for link in links_list:
        exam_id = _link_source_id(link, "exam")
        if exam_id is not None and exam_id in foreign_exam_ids:
            continue
        homework_id = _link_source_id(link, "homework")
        if homework_id is not None and homework_id in foreign_homework_ids:
            continue
        consistent.append(link)
    return consistent


def get_clinic_enrollment_ids_for_session(
    *,
    session: Any,
    include_manual: bool = False,
    exclude_completed: bool = True,
) -> Set[int]:
    """
    ✅ Clinic 단일 규칙 제공

    기본 정책(권장/안전):
    - 운영에서 clinic_required/clinic_rate는 '자동 트리거' 기준으로 통일한다.
      -> include_manual=False (default)

    왜냐하면:
    - 수동 클리닉(강사 추천/요청)은 UX/운영 정책에 따라 케이스가 달라서
      통계에 섞이면 화면마다 "왜 다르냐" 문제가 반복된다.

    필요하면 include_manual=True로
    수동까지 포함한 '전체 clinic 대상'을 만들 수 있다.

    tenant 격리: session FK는 이미 tenant-scoped이지만,
    ClinicLink.tenant FK로 명시 필터하여 암묵적 의존 제거.
    """
    # tenant 격리: session.lecture.tenant로 명시 필터
    tenant_id = getattr(getattr(session, "lecture", None), "tenant_id", None)
    qs = clinic_link_queryset_for_session(
        session=session,
        tenant_id=tenant_id,
        include_manual=include_manual,
    )

    links = filter_live_source_links(
        qs.order_by("id"),
        tenant=getattr(getattr(session, "lecture", None), "tenant", None),
    )
    ids = {
        int(getattr(link, "enrollment_id", 0) or 0)
        for link in links
        if getattr(link, "enrollment_id", None)
    }
    if not exclude_completed or not ids:
        return ids

    completed_ids = completed_enrollment_ids_for_session(
        session=session,
        enrollment_ids=ids,
    )
    return ids - {int(enrollment_id) for enrollment_id in completed_ids}


def is_clinic_required(
    *,
    session: Any,
    enrollment_id: int,
    include_manual: bool = False,
) -> bool:
    """
    ✅ enrollment 단위 clinic 여부 (단일 진실)
    """
    enrollment_id = int(enrollment_id)
    ids = get_clinic_enrollment_ids_for_session(
        session=session,
        include_manual=include_manual,
    )
    return enrollment_id in ids
