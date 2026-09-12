# apps/domains/progress/services/clinic_resolution_service.py
"""
클리닉 해소 서비스 (SSOT)

해소 트리거:
1. 시험 재시험 통과 (EXAM_PASS) — auto
2. 과제 재제출/재채점 통과 (HOMEWORK_PASS) — auto
3. 관리자 수동 해소 (MANUAL_OVERRIDE)
4. 면제 (WAIVED)
5. 원본 시험/과제 제거 (SOURCE_REMOVED)
6. 명시적 시험 미응시 전환 (NOT_SUBMITTED, 알림 없음)
7. 과거 부분 채점 투영 철회 (GRADING_RETRACTED, 알림 없음)

절대 금지:
- 예약(booking)으로 해소
- 출석(attended)으로 해소
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.domains.progress.models import ClinicLink
from apps.support.clinic.session_dependencies import (
    deactivate_resolved_clinic_link_plan_items,
)
from apps.support.progress.clinic_resolution_notification_dependencies import (
    send_clinic_resolution_notification,
)
from apps.support.progress.session_calculator_dependencies import (
    homework_progress_enrollment_ids,
)

logger = logging.getLogger(__name__)


def _append_history(link, *, action: str, at=None) -> None:
    """
    ClinicLink.resolution_history에 현재 상태 snapshot을 append.
    transition 직전에 호출. 저장은 호출자 책임 (update_fields에 resolution_history 포함).
    """
    at = at or timezone.now()
    history = list(link.resolution_history or [])
    history.append({
        "at": at.isoformat(),
        "action": action,
        "prev_resolution_type": link.resolution_type,
        "prev_resolved_at": link.resolved_at.isoformat() if link.resolved_at else None,
        "prev_evidence": link.resolution_evidence,
    })
    link.resolution_history = history


def _deactivate_today_plan(link: ClinicLink) -> None:
    if link.resolved_at is None:
        return
    deactivate_resolved_clinic_link_plan_items(
        clinic_link_ids=[link.id],
        resolution_type=link.resolution_type or "RESOLVED",
        removed_at=link.resolved_at,
    )


def _dispatch_progress_for_link(link: ClinicLink) -> None:
    """
    해당 ClinicLink 기반으로 SessionProgress 재계산을 on_commit으로 예약.

    필요 이유:
      admin의 manual resolve / waive / unresolve / carry_over / 재시험 점수 수정이
      SessionProgress.exam_passed·completed·clinic_required 등 집계 상태를 바꾸지만,
      파이프라인을 명시적으로 재실행하지 않으면 학생/교사 화면의 파생 지표가
      stale 상태로 남아 드리프트가 재발한다.

    Dispatch 전략 (우선순위):
      1) source_type="exam" + source_id 유효 → dispatch_progress_pipeline(exam_id=...)
         (해당 시험 응시자 전체 재계산 — risk 평가까지 정확)
      2) source_type=NULL legacy + meta.exam_id 유효 → exam_id path 동일
      3) 그 외 (homework, exam_id 미상) + enrollment_id + session_id 존재
         → dispatch_progress_pipeline(enrollment_id=..., session_id=...)
         (특정 학생 × 세션 한 점 재계산)
      4) 위 조건 모두 실패 → skip + debug log
    """
    exam_id: int | None = None
    if link.source_type == "exam" and link.source_id:
        try:
            exam_id = int(link.source_id)
        except (TypeError, ValueError):
            exam_id = None
    elif link.source_type is None and isinstance(link.meta, dict):
        raw = link.meta.get("exam_id")
        if raw is not None:
            try:
                exam_id = int(raw)
            except (TypeError, ValueError):
                exam_id = None

    # Path 1/2: exam_id 경로
    if exam_id is not None:
        _eid = exam_id

        def _dispatch_by_exam() -> None:
            try:
                from apps.domains.progress.dispatcher import dispatch_progress_pipeline
                dispatch_progress_pipeline(exam_id=_eid)
            except Exception:
                logger.exception(
                    "clinic_resolution: pipeline dispatch failed (link=%s, exam=%s)",
                    link.id, _eid,
                )

        transaction.on_commit(_dispatch_by_exam)
        return

    # Path 3: enrollment + session 점 재계산
    if link.enrollment_id and link.session_id:
        _enr = int(link.enrollment_id)
        _sid = int(link.session_id)

        def _dispatch_by_enrollment_session() -> None:
            try:
                from apps.domains.progress.dispatcher import dispatch_progress_pipeline
                dispatch_progress_pipeline(enrollment_id=_enr, session_id=_sid)
            except Exception:
                logger.exception(
                    "clinic_resolution: pipeline dispatch failed "
                    "(link=%s, enrollment=%s, session=%s)",
                    link.id, _enr, _sid,
                )

        transaction.on_commit(_dispatch_by_enrollment_session)
        return

    logger.debug(
        "clinic_resolution: pipeline dispatch skipped — insufficient keys "
        "(link=%s, source_type=%s, enrollment=%s, session=%s)",
        link.id, link.source_type, link.enrollment_id, link.session_id,
    )


def _dispatch_progress_for_enrollment_session(*, enrollment_id: int, session_id: int) -> None:
    """
    특정 enrollment × session 진행 상태를 on_commit에서 강제 재계산한다.
    source가 삭제된 뒤에는 exam_id 경로로 해당 session을 찾을 수 없으므로
    session 스코프를 직접 지정해야 한다.
    """
    _enr = int(enrollment_id)
    _sid = int(session_id)

    def _dispatch() -> None:
        try:
            from apps.domains.progress.dispatcher import dispatch_progress_pipeline
            dispatch_progress_pipeline(enrollment_id=_enr, session_id=_sid)
        except Exception:
            logger.exception(
                "clinic_resolution: pipeline dispatch failed "
                "(enrollment=%s, session=%s)",
                _enr, _sid,
            )

    transaction.on_commit(_dispatch)


def _send_resolution_notification(enrollment_id: int, session_id: int, resolution_type: str):
    """클리닉 해소 완료 알림 (best-effort, on_commit에서 호출)."""
    try:
        send_clinic_resolution_notification(
            enrollment_id=enrollment_id,
            session_id=session_id,
            resolution_type=resolution_type,
        )
    except Exception:
        logger.debug("clinic_result_notification failed (enrollment=%s)", enrollment_id, exc_info=True)


class ClinicResolutionService:
    """
    ClinicLink 해소 단일 진실 서비스.
    모든 해소/복원은 이 서비스를 통해야 한다.
    """

    @staticmethod
    @transaction.atomic
    def set_teacher_assessment_resolution(
        *,
        tenant_id: int,
        enrollment_id: int,
        session_id: int,
        source_type: str,
        source_id: int,
        completed: bool,
        correction_id: int,
        user_id: Optional[int],
        memo: str,
        source_fingerprint: str | None = None,
    ) -> Optional[ClinicLink]:
        """Apply or reopen a source-specific teacher decision without changing score.

        ``AssessmentCorrection`` owns the teacher decision. ``ClinicLink`` keeps
        the existing remediation resolution/read-model contract and append-only
        transition history. A missing link is materialized as an already-resolved
        source record so a later async progress run cannot recreate the target.
        """
        if source_type not in {"exam", "homework"}:
            raise ValueError(f"Unsupported assessment source_type: {source_type}")

        tenant_id = int(tenant_id)
        enrollment_id = int(enrollment_id)
        session_id = int(session_id)
        source_id = int(source_id)
        correction_id = int(correction_id)
        normalized_memo = str(memo or "").strip()

        links = list(
            ClinicLink.objects.select_for_update()
            .filter(
                tenant_id=tenant_id,
                enrollment_id=enrollment_id,
                session_id=session_id,
                source_type=source_type,
                source_id=source_id,
            )
            .order_by("-cycle_no", "-id")
        )
        latest = links[0] if links else None
        latest_evidence = (
            latest.resolution_evidence
            if latest and isinstance(latest.resolution_evidence, dict)
            else {}
        )
        latest_is_teacher_resolution = bool(
            latest
            and latest.resolution_type == ClinicLink.ResolutionType.MANUAL_OVERRIDE
            and int(latest_evidence.get("assessment_correction_id") or 0)
            == correction_id
        )

        if not completed:
            if latest and latest_is_teacher_resolution and latest.resolved_at:
                link = latest
                _append_history(link, action="unresolve_teacher_assessment")
                link.resolved_at = None
                link.resolution_type = None
                link.resolution_evidence = None
                link.save(
                    update_fields=[
                        "resolved_at",
                        "resolution_type",
                        "resolution_evidence",
                        "resolution_history",
                        "updated_at",
                    ]
                )
            elif latest:
                if latest.resolved_at:
                    # A factual retake/pass or unrelated waiver is stronger than
                    # this review control and must not be reopened here.
                    if source_type == "homework":
                        _dispatch_progress_for_link(latest)
                    return latest
                link = latest
            else:
                link = ClinicLink.objects.create(
                    tenant_id=tenant_id,
                    enrollment_id=enrollment_id,
                    session_id=session_id,
                    source_type=source_type,
                    source_id=source_id,
                    reason=ClinicLink.Reason.AUTO_FAILED,
                    is_auto=True,
                    approved=False,
                    cycle_no=1,
                    meta={
                        "kind": "TEACHER_ASSESSMENT_INCOMPLETE",
                        f"{source_type}_id": source_id,
                        "assessment_correction_id": correction_id,
                    },
                )
            _dispatch_progress_for_link(link)
            return link

        if len(normalized_memo) < 2:
            raise ValueError("Teacher assessment resolution requires a reason")

        # Preserve a factual retake/source pass, waiver or unrelated manual
        # closure. The correction row records this review, but the teacher
        # toggle must not replace or later reopen an already-resolved source.
        if latest and latest.resolved_at and not latest_is_teacher_resolution:
            if source_type == "homework":
                _dispatch_progress_for_link(latest)
            return latest

        link = latest
        if link is None:
            max_cycle = max((int(item.cycle_no or 0) for item in links), default=0)
            link = ClinicLink.objects.create(
                tenant_id=tenant_id,
                enrollment_id=enrollment_id,
                session_id=session_id,
                source_type=source_type,
                source_id=source_id,
                reason=ClinicLink.Reason.AUTO_FAILED,
                is_auto=True,
                approved=True,
                cycle_no=max(max_cycle + 1, 1),
                meta={
                    "kind": "TEACHER_ASSESSMENT_RESOLUTION",
                    f"{source_type}_id": source_id,
                },
            )

        was_teacher_resolved = bool(
            link.resolved_at and latest_is_teacher_resolution
        )
        now = timezone.now()
        _append_history(
            link,
            action=(
                "confirm_teacher_assessment"
                if link.resolved_at
                else "resolve_teacher_assessment"
            ),
            at=now,
        )
        link.resolved_at = link.resolved_at or now
        link.resolution_type = ClinicLink.ResolutionType.MANUAL_OVERRIDE
        link.resolution_evidence = {
            "user_id": user_id,
            "memo": normalized_memo,
            "assessment_correction_id": correction_id,
            "source_type": source_type,
            "source_id": source_id,
            "source_fingerprint": source_fingerprint,
        }
        link.memo = normalized_memo
        link.approved = True
        link.save(
            update_fields=[
                "resolved_at",
                "resolution_type",
                "resolution_evidence",
                "resolution_history",
                "memo",
                "approved",
                "updated_at",
            ]
        )
        _deactivate_today_plan(link)
        _dispatch_progress_for_link(link)
        if not was_teacher_resolved:
            _eid, _sid = enrollment_id, session_id
            transaction.on_commit(
                lambda: _send_resolution_notification(
                    _eid,
                    _sid,
                    ClinicLink.ResolutionType.MANUAL_OVERRIDE,
                )
            )
        return link

    @staticmethod
    @transaction.atomic
    def resolve_by_removed_source(
        *,
        tenant_id: int,
        session_id: int,
        source_type: str,
        source_id: int,
        enrollment_ids: Optional[Iterable[int]] = None,
        user_id: Optional[int] = None,
        reason: str = "source_removed_from_session",
    ) -> int:
        """
        시험/과제가 차시에서 제거될 때 해당 source가 만든 미해소 ClinicLink를 닫는다.

        운영 노출의 단일 진실은 unresolved ClinicLink이므로, 원본이 사라진 링크를
        SOURCE_REMOVED로 전이해야 성적/클리닉 대상 UI에 유령 대상자가 남지 않는다.
        """
        if source_type not in {"exam", "homework"}:
            raise ValueError(f"Unsupported clinic source_type: {source_type}")

        source_id = int(source_id)
        session_id = int(session_id)
        tenant_id = int(tenant_id)
        source_meta_key = "exam_id" if source_type == "exam" else "homework_id"
        source_filter = (
            Q(source_type=source_type, source_id=source_id)
            | Q(source_type__isnull=True, **{f"meta__{source_meta_key}": source_id})
        )
        normalized_enrollment_ids: list[int] | None = None
        if enrollment_ids is not None:
            normalized_enrollment_ids = sorted(
                {int(enrollment_id) for enrollment_id in enrollment_ids}
            )
            if not normalized_enrollment_ids:
                return 0

        link_qs = (
            ClinicLink.objects.select_for_update()
            .filter(
                tenant_id=tenant_id,
                session_id=session_id,
                resolved_at__isnull=True,
            )
            .filter(source_filter)
        )
        if normalized_enrollment_ids is not None:
            link_qs = link_qs.filter(enrollment_id__in=normalized_enrollment_ids)

        affected_pairs: set[tuple[int, int]] = set()
        if source_type == "homework":
            # Assignment removal is a progress event even without a failed link.
            # Whole-source removal captures the roster before its rows are deleted.
            homework_enrollment_ids = (
                normalized_enrollment_ids
                if normalized_enrollment_ids is not None
                else homework_progress_enrollment_ids(
                    tenant_id=tenant_id, session_id=session_id, homework_id=source_id,
                )
            )
            affected_pairs.update((int(enrollment_id), session_id) for enrollment_id in homework_enrollment_ids)

        links = list(link_qs.order_by("id"))
        if not links and not affected_pairs:
            return 0

        now = timezone.now()
        count = 0
        for link in links:
            _append_history(link, action="resolve_source_removed", at=now)
            link.resolved_at = now
            link.resolution_type = ClinicLink.ResolutionType.SOURCE_REMOVED
            link.resolution_evidence = {
                "reason": reason,
                "source_type": source_type,
                "source_id": source_id,
                "session_id": session_id,
                "enrollment_ids": normalized_enrollment_ids,
                "user_id": user_id,
            }
            link.save(update_fields=[
                "resolved_at", "resolution_type", "resolution_evidence",
                "resolution_history", "updated_at",
            ])
            _deactivate_today_plan(link)
            if link.enrollment_id and link.session_id:
                affected_pairs.add((int(link.enrollment_id), int(link.session_id)))
            count += 1

        for enrollment_id, affected_session_id in sorted(affected_pairs):
            _dispatch_progress_for_enrollment_session(
                enrollment_id=enrollment_id,
                session_id=affected_session_id,
            )

        logger.info(
            "clinic_resolution: SOURCE_REMOVED resolved %d links "
            "(tenant=%s, session=%s, source=%s:%s, enrollments=%s)",
            count, tenant_id, session_id, source_type, source_id,
            normalized_enrollment_ids,
        )
        return count

    @staticmethod
    @transaction.atomic
    def resolve_by_exam_not_submitted(
        *,
        tenant_id: int,
        enrollment_id: int,
        exam_id: int,
        attempt_id: int,
        user_id: Optional[int] = None,
    ) -> int:
        """Close only stale failure links invalidated by an explicit absence.

        The remediation history stays intact and any active today-plan selection
        is audit-closed. Clinic bookings, arrivals, and completions are separate
        operational facts and are deliberately left untouched. This transition
        never sends a resolution notification.
        """
        tenant_id = int(tenant_id)
        enrollment_id = int(enrollment_id)
        exam_id = int(exam_id)
        attempt_id = int(attempt_id)
        source_filter = (
            Q(source_type="exam", source_id=exam_id)
            | Q(source_type__isnull=True, meta__exam_id=exam_id)
        )
        links = list(
            ClinicLink.objects.select_for_update()
            .filter(
                tenant_id=tenant_id,
                enrollment_id=enrollment_id,
                resolved_at__isnull=True,
            )
            .filter(source_filter)
            .order_by("session_id", "id")
        )
        if not links:
            return 0

        now = timezone.now()
        for link in links:
            _append_history(link, action="resolve_exam_not_submitted", at=now)
            link.resolved_at = now
            link.resolution_type = ClinicLink.ResolutionType.NOT_SUBMITTED
            link.resolution_evidence = {
                "status": "NOT_SUBMITTED",
                "exam_id": exam_id,
                "attempt_id": attempt_id,
                "enrollment_id": enrollment_id,
                "user_id": user_id,
                "transitioned_at": now.isoformat(),
            }
            link.save(
                update_fields=[
                    "resolved_at",
                    "resolution_type",
                    "resolution_evidence",
                    "resolution_history",
                    "updated_at",
                ]
            )
            _deactivate_today_plan(link)

        logger.info(
            "clinic_resolution: NOT_SUBMITTED resolved %d links "
            "(tenant=%s, enrollment=%s, exam=%s, attempt=%s)",
            len(links),
            tenant_id,
            enrollment_id,
            exam_id,
            attempt_id,
        )
        return len(links)

    @staticmethod
    @transaction.atomic
    def resolve_by_pending_grading(
        *,
        tenant_id: int,
        enrollment_id: int,
        exam_id: int,
    ) -> int:
        """Audit-close automatic clinic projections from an incomplete OMR score."""

        tenant_id = int(tenant_id)
        enrollment_id = int(enrollment_id)
        exam_id = int(exam_id)
        source_filter = (
            Q(source_type="exam", source_id=exam_id)
            | Q(source_type__isnull=True, meta__exam_id=exam_id)
        )
        links = list(
            ClinicLink.objects.select_for_update()
            .filter(
                tenant_id=tenant_id,
                enrollment_id=enrollment_id,
                is_auto=True,
                resolved_at__isnull=True,
            )
            .filter(source_filter)
            .order_by("session_id", "id")
        )
        if not links:
            return 0

        now = timezone.now()
        for link in links:
            _append_history(link, action="resolve_pending_grading", at=now)
            link.resolved_at = now
            link.resolution_type = ClinicLink.ResolutionType.GRADING_RETRACTED
            link.resolution_evidence = {
                "grading_status": "subjective_pending",
                "exam_id": exam_id,
                "enrollment_id": enrollment_id,
                "transitioned_at": now.isoformat(),
            }
            link.save(
                update_fields=[
                    "resolved_at",
                    "resolution_type",
                    "resolution_evidence",
                    "resolution_history",
                    "updated_at",
                ]
            )
            _deactivate_today_plan(link)

        logger.info(
            "clinic_resolution: GRADING_RETRACTED resolved %d links "
            "(tenant=%s, enrollment=%s, exam=%s)",
            len(links),
            tenant_id,
            enrollment_id,
            exam_id,
        )
        return len(links)

    @staticmethod
    @transaction.atomic
    def resolve_by_exam_pass(
        *,
        enrollment_id: int,
        session_id: int,
        exam_id: int,
        attempt_id: Optional[int] = None,
        score: Optional[float] = None,
        pass_score: Optional[float] = None,
        max_score: Optional[float] = None,
    ) -> int:
        """
        시험 통과 시 해당 enrollment+session의 미해소 ClinicLink를 해소.
        Returns: 해소된 ClinicLink 수
        """
        evidence = {
            "exam_id": exam_id,
            "score": score,
            "pass_score": pass_score,
            "max_score": max_score,
        }
        if attempt_id:
            evidence["attempt_id"] = attempt_id

        now = timezone.now()

        # V1.1.2: source-specific 해소 (시험별 개별)
        # history 보존 위해 row 순회 (자동 해소는 보통 1~2건)
        target_links = list(
            ClinicLink.objects.select_for_update().filter(
                enrollment_id=enrollment_id,
                session_id=session_id,
                source_type="exam",
                source_id=int(exam_id),
                resolved_at__isnull=True,
            )
        )
        count = 0
        for link in target_links:
            _append_history(link, action="resolve_exam_pass", at=now)
            link.resolved_at = now
            link.resolution_type = ClinicLink.ResolutionType.EXAM_PASS
            link.resolution_evidence = evidence
            link.save(update_fields=[
                "resolved_at", "resolution_type", "resolution_evidence",
                "resolution_history", "updated_at",
            ])
            _deactivate_today_plan(link)
            count += 1

        # update_kwargs는 legacy fallback에서 재사용 (기존 의미 유지)
        update_kwargs = dict(
            resolved_at=now,
            resolution_type=ClinicLink.ResolutionType.EXAM_PASS,
            resolution_evidence=evidence,
        )

        # Fallback: legacy links (source_type=NULL, V1.1.1 이전 데이터)
        # 엄격 매치: meta.exam_id가 현재 exam_id와 일치하는 링크만 해소.
        # exam_id 미상인 legacy는 자동 해소 대상에서 제외하여 과매칭 방지.
        if count == 0:
            legacy_link = ClinicLink.objects.filter(
                enrollment_id=enrollment_id,
                session_id=session_id,
                source_type__isnull=True,
                resolved_at__isnull=True,
                meta__exam_id=int(exam_id),
            ).order_by("id").first()
            if legacy_link:
                _append_history(legacy_link, action="resolve_exam_pass_legacy", at=now)
                for k, v in update_kwargs.items():
                    setattr(legacy_link, k, v)
                legacy_link.save(update_fields=list(update_kwargs.keys()) + [
                    "resolution_history", "updated_at",
                ])
                _deactivate_today_plan(legacy_link)
                count = 1
            else:
                # meta.exam_id 매칭 실패 — 불명확한 legacy 링크는 건드리지 않음
                ambiguous = ClinicLink.objects.filter(
                    enrollment_id=enrollment_id,
                    session_id=session_id,
                    source_type__isnull=True,
                    resolved_at__isnull=True,
                ).count()
                if ambiguous:
                    logger.warning(
                        "clinic_resolution: EXAM_PASS skipped %d legacy link(s) "
                        "without matching meta.exam_id=%s (enrollment=%s, session=%s) — "
                        "manual backfill recommended",
                        ambiguous, exam_id, enrollment_id, session_id,
                    )

        if count > 0:
            logger.info(
                "clinic_resolution: EXAM_PASS resolved %d links "
                "(enrollment=%s, session=%s, exam=%s, score=%s)",
                count, enrollment_id, session_id, exam_id, score,
            )
            _eid, _sid = enrollment_id, session_id
            transaction.on_commit(lambda: _send_resolution_notification(_eid, _sid, "EXAM_PASS"))
        return count

    @staticmethod
    @transaction.atomic
    def resolve_by_homework_pass(
        *,
        enrollment_id: int,
        session_id: int,
        homework_id: int,
        score: Optional[float] = None,
        max_score: Optional[float] = None,
    ) -> int:
        """
        과제 통과 시 해당 enrollment+session+homework의 미해소 ClinicLink를 해소.
        Returns: 해소된 ClinicLink 수
        """
        evidence = {
            "homework_id": homework_id,
            "score": score,
            "max_score": max_score,
        }

        now = timezone.now()

        # V1.1.2: source-specific 해소 (과제별 개별) — exam과 동일 패턴
        target_links = list(
            ClinicLink.objects.select_for_update().filter(
                enrollment_id=enrollment_id,
                session_id=session_id,
                source_type="homework",
                source_id=int(homework_id),
                resolved_at__isnull=True,
            )
        )
        count = 0
        for link in target_links:
            _append_history(link, action="resolve_homework_pass", at=now)
            link.resolved_at = now
            link.resolution_type = ClinicLink.ResolutionType.HOMEWORK_PASS
            link.resolution_evidence = evidence
            link.save(update_fields=[
                "resolved_at", "resolution_type", "resolution_evidence",
                "resolution_history", "updated_at",
            ])
            _deactivate_today_plan(link)
            count += 1

        update_kwargs = dict(
            resolved_at=now,
            resolution_type=ClinicLink.ResolutionType.HOMEWORK_PASS,
            resolution_evidence=evidence,
        )

        # Fallback: legacy links (source_type=NULL, V1.1.1 이전 데이터)
        # 엄격 매치: meta.homework_id 일치하는 링크만 해소. 과매칭 방지.
        if count == 0:
            legacy_link = ClinicLink.objects.filter(
                enrollment_id=enrollment_id,
                session_id=session_id,
                source_type__isnull=True,
                resolved_at__isnull=True,
                meta__homework_id=int(homework_id),
            ).order_by("id").first()
            if legacy_link:
                _append_history(legacy_link, action="resolve_homework_pass_legacy", at=now)
                for k, v in update_kwargs.items():
                    setattr(legacy_link, k, v)
                legacy_link.save(update_fields=list(update_kwargs.keys()) + [
                    "resolution_history", "updated_at",
                ])
                _deactivate_today_plan(legacy_link)
                count = 1
            else:
                ambiguous = ClinicLink.objects.filter(
                    enrollment_id=enrollment_id,
                    session_id=session_id,
                    source_type__isnull=True,
                    resolved_at__isnull=True,
                ).count()
                if ambiguous:
                    logger.warning(
                        "clinic_resolution: HOMEWORK_PASS skipped %d legacy link(s) "
                        "without matching meta.homework_id=%s (enrollment=%s, session=%s) — "
                        "manual backfill recommended",
                        ambiguous, homework_id, enrollment_id, session_id,
                    )

        if count > 0:
            logger.info(
                "clinic_resolution: HOMEWORK_PASS resolved %d links "
                "(enrollment=%s, session=%s, homework=%s)",
                count, enrollment_id, session_id, homework_id,
            )
            _eid, _sid = enrollment_id, session_id
            transaction.on_commit(lambda: _send_resolution_notification(_eid, _sid, "HOMEWORK_PASS"))
        return count

    @staticmethod
    @transaction.atomic
    def resolve_manually(
        *,
        clinic_link_id: int,
        user_id: Optional[int] = None,
        memo: Optional[str] = None,
    ) -> Optional[ClinicLink]:
        """
        관리자 수동 해소.
        """
        try:
            link = ClinicLink.objects.select_for_update().get(
                id=clinic_link_id,
                resolved_at__isnull=True,
            )
        except ClinicLink.DoesNotExist:
            logger.warning("clinic_resolution: manual resolve failed, link not found or already resolved (id=%s)", clinic_link_id)
            return None

        now = timezone.now()
        _append_history(link, action="resolve_manual", at=now)
        link.resolved_at = now
        link.resolution_type = ClinicLink.ResolutionType.MANUAL_OVERRIDE
        link.resolution_evidence = {"user_id": user_id, "memo": memo}
        if memo:
            link.memo = memo
        link.save(update_fields=[
            "resolved_at", "resolution_type", "resolution_evidence",
            "resolution_history", "memo", "updated_at",
        ])
        _deactivate_today_plan(link)

        logger.info("clinic_resolution: MANUAL_OVERRIDE (link=%s, user=%s)", clinic_link_id, user_id)
        _eid, _sid = link.enrollment_id, link.session_id
        transaction.on_commit(lambda: _send_resolution_notification(_eid, _sid, "MANUAL_OVERRIDE"))
        # ✅ SessionProgress 재계산: manual resolve는 외부 진입점 → pipeline 재실행 필요.
        _dispatch_progress_for_link(link)
        return link

    @staticmethod
    @transaction.atomic
    def waive(
        *,
        clinic_link_id: int,
        user_id: Optional[int] = None,
        memo: Optional[str] = None,
    ) -> Optional[ClinicLink]:
        """
        면제 처리.
        """
        try:
            link = ClinicLink.objects.select_for_update().get(
                id=clinic_link_id,
                resolved_at__isnull=True,
            )
        except ClinicLink.DoesNotExist:
            logger.warning("clinic_resolution: waive failed, link not found or already resolved (id=%s)", clinic_link_id)
            return None

        now = timezone.now()
        _append_history(link, action="waive", at=now)
        link.resolved_at = now
        link.resolution_type = ClinicLink.ResolutionType.WAIVED
        link.resolution_evidence = {"user_id": user_id, "memo": memo}
        if memo:
            link.memo = memo
        link.save(update_fields=[
            "resolved_at", "resolution_type", "resolution_evidence",
            "resolution_history", "memo", "updated_at",
        ])
        _deactivate_today_plan(link)

        logger.info("clinic_resolution: WAIVED (link=%s, user=%s)", clinic_link_id, user_id)
        _eid, _sid = link.enrollment_id, link.session_id
        transaction.on_commit(lambda: _send_resolution_notification(_eid, _sid, "WAIVED"))
        # ✅ SessionProgress 재계산: WAIVED는 clinic_required 지표를 바꾸므로 pipeline 재실행.
        _dispatch_progress_for_link(link)
        return link

    @staticmethod
    @transaction.atomic
    def unresolve(
        *,
        clinic_link_id: int,
    ) -> Optional[ClinicLink]:
        """
        해소 취소 (되돌리기). 재시험 실패 시 등에 사용.
        """
        try:
            link = ClinicLink.objects.select_for_update().get(id=clinic_link_id)
        except ClinicLink.DoesNotExist:
            return None

        if not link.resolved_at:
            return link  # already unresolved

        # 이전 해소 정보를 history에 보존한 후 리셋
        _append_history(link, action="unresolve")
        link.resolved_at = None
        link.resolution_type = None
        link.resolution_evidence = None
        link.save(update_fields=[
            "resolved_at", "resolution_type", "resolution_evidence",
            "resolution_history", "updated_at",
        ])

        logger.info("clinic_resolution: UNRESOLVED (link=%s)", clinic_link_id)
        # ✅ SessionProgress 재계산: 해소 취소 시 exam_passed/completed 상태가 다시
        # 미완료로 복원돼야 한다. 파이프라인 재실행 없이는 드리프트 재발.
        _dispatch_progress_for_link(link)
        return link

    @staticmethod
    @transaction.atomic
    def carry_over(
        *,
        clinic_link_id: int,
    ) -> Optional[ClinicLink]:
        """
        미해소 case를 다음 cycle로 이월.
        현재 link를 CARRIED_OVER로 닫고, 새 link를 cycle_no+1로 생성.
        """
        try:
            link = ClinicLink.objects.select_for_update().get(
                id=clinic_link_id,
                resolved_at__isnull=True,
            )
        except ClinicLink.DoesNotExist:
            logger.warning("clinic_resolution: carry_over failed, link not found (id=%s)", clinic_link_id)
            return None

        # Close current with CARRIED_OVER resolution (별도 enum — WAIVED/면제와 구분)
        now = timezone.now()
        link.resolved_at = now
        link.resolution_type = ClinicLink.ResolutionType.CARRIED_OVER
        link.resolution_evidence = {"carried_over": True, "carried_at": now.isoformat()}
        _append_history(link, action="carry_over", at=now)
        link.save(update_fields=[
            "resolved_at", "resolution_type", "resolution_evidence",
            "resolution_history", "updated_at",
        ])
        _deactivate_today_plan(link)

        # Create next cycle (source + tenant 전파)
        new_link = ClinicLink.objects.create(
            enrollment_id=link.enrollment_id,
            session_id=link.session_id,
            source_type=link.source_type,
            source_id=link.source_id,
            reason=link.reason,
            is_auto=link.is_auto,
            approved=False,
            cycle_no=link.cycle_no + 1,
            memo=f"{link.cycle_no}차 이월",
            meta=link.meta,
            tenant_id=link.tenant_id,
        )

        logger.info(
            "clinic_resolution: CARRIED_OVER (old=%s→new=%s, cycle=%d→%d)",
            clinic_link_id, new_link.id, link.cycle_no, new_link.cycle_no,
        )
        # ✅ SessionProgress 재계산: 이월로 현재 cycle은 닫히고 새 cycle이 열림.
        # clinic_required 미해소 집계가 바뀌므로 pipeline 재실행.
        _dispatch_progress_for_link(new_link)
        return new_link
