# apps/domains/progress/services/clinic_trigger_service.py
"""
V1.1.2: 시험/과제별 개별 ClinicLink 생성

핵심 변경:
- auto_create_if_failed: SessionProgress.completed 대신 exam_meta의 개별 시험 pass/fail로 판정
- auto_create_if_exam_risk: source_type/source_id로 시험별 개별 ClinicLink 생성
- 세션 MAX 집계와 독립적으로 개별 시험 불합격을 추적

동시성 보장:
- transaction.atomic + unique constraint fallback으로 idempotent 생성
- 동시 파이프라인 실행 시에도 중복 생성 방지
"""
from __future__ import annotations

import logging

from django.db import IntegrityError, transaction
from django.db.models import Max

from apps.domains.progress.models import ClinicLink, SessionProgress
from apps.domains.progress.services.clinic_exam_rule_service import ClinicExamRuleService
from apps.support.progress.clinic_trigger_dependencies import (
    get_enrollment_tenant_id,
    is_representative_exam_not_submitted,
    lock_enrollment_for_clinic_trigger,
    locked_representative_exam_is_not_submitted,
)
from apps.support.progress.assessment_correction_dependencies import (
    is_current_teacher_exam_resolution,
)
from apps.support.attendance.learning_todo_eligibility import (
    enrollment_session_is_learning_todo_eligible,
)

logger = logging.getLogger(__name__)


_CLOSED_SOURCE_RESOLUTION_TYPES = {
    ClinicLink.ResolutionType.MANUAL_OVERRIDE,
    ClinicLink.ResolutionType.WAIVED,
    ClinicLink.ResolutionType.SOURCE_REMOVED,
}


def _resolve_tenant_id(enrollment_id: int) -> int | None:
    """enrollment_id에서 tenant_id를 조회한다."""
    return get_enrollment_tenant_id(enrollment_id)


def _latest_source_resolution_type(
    *,
    enrollment_id: int,
    session,
    source_type: str,
    source_id: int,
) -> str | None:
    return (
        ClinicLink.objects.filter(
            enrollment_id=enrollment_id,
            session=session,
            source_type=source_type,
            source_id=source_id,
            resolved_at__isnull=False,
        )
        .order_by("-resolved_at", "-id")
        .values_list("resolution_type", flat=True)
        .first()
    )


def _reopen_stale_teacher_exam_resolution(
    *,
    enrollment_id: int,
    session,
    exam_id: int,
) -> None:
    """Reopen a teacher pass when its saved score/answer fingerprint is stale."""
    link = (
        ClinicLink.objects.filter(
            enrollment_id=enrollment_id,
            session=session,
            source_type="exam",
            source_id=exam_id,
            resolved_at__isnull=False,
            resolution_type=ClinicLink.ResolutionType.MANUAL_OVERRIDE,
        )
        .order_by("-resolved_at", "-id")
        .first()
    )
    evidence = link.resolution_evidence if link and isinstance(link.resolution_evidence, dict) else {}
    correction_id = int(evidence.get("assessment_correction_id") or 0)
    if not link or not correction_id:
        return
    if is_current_teacher_exam_resolution(
        tenant_id=int(link.tenant_id),
        enrollment_id=int(enrollment_id),
        session_id=int(session.id),
        exam_id=int(exam_id),
        correction_id=correction_id,
    ):
        return

    from apps.domains.progress.services.clinic_resolution_service import (
        ClinicResolutionService,
    )

    ClinicResolutionService.unresolve(clinic_link_id=int(link.id))


def _idempotent_create_clinic_link(
    *,
    enrollment_id: int,
    session,
    source_type: str,
    source_id: int,
    reason: str,
    is_auto: bool = True,
    meta: dict | None = None,
) -> ClinicLink | None:
    """
    ClinicLink를 idempotent하게 생성.
    - 미해소 link가 있으면 skip
    - unique constraint 충돌 시 기존 link 반환 (race condition 방어)
    - transaction.atomic으로 check-then-create 원자성 보장
    """
    with transaction.atomic():
        # 미해소 link가 있으면 skip
        existing = ClinicLink.objects.filter(
            enrollment_id=enrollment_id,
            session=session,
            source_type=source_type,
            source_id=source_id,
            resolved_at__isnull=True,
        ).exists()
        if existing:
            return None

        latest_resolution_type = _latest_source_resolution_type(
            enrollment_id=enrollment_id,
            session=session,
            source_type=source_type,
            source_id=source_id,
        )
        if latest_resolution_type in _CLOSED_SOURCE_RESOLUTION_TYPES:
            return None

        max_cycle = ClinicLink.objects.filter(
            enrollment_id=enrollment_id,
            session=session,
            source_type=source_type,
            source_id=source_id,
        ).aggregate(Max("cycle_no"))["cycle_no__max"] or 0
        next_cycle = max(max_cycle + 1, 1)

        try:
            tenant_id = _resolve_tenant_id(enrollment_id)
            return ClinicLink.objects.create(
                enrollment_id=enrollment_id,
                session=session,
                source_type=source_type,
                source_id=source_id,
                reason=reason,
                is_auto=is_auto,
                approved=False,
                cycle_no=next_cycle,
                meta=meta,
                tenant_id=tenant_id,
            )
        except IntegrityError:
            # unique constraint 충돌 = 동시 실행으로 이미 생성됨
            logger.info(
                "clinic_trigger: duplicate ClinicLink skipped "
                "(enrollment=%s, session=%s, source=%s:%s)",
                enrollment_id, session, source_type, source_id,
            )
            return None


class ClinicTriggerService:
    """
    클리닉 '필요 상태'를 생성하는 트리거 서비스 (저장만)
    """

    @staticmethod
    def auto_create_per_exam(session_progress: SessionProgress) -> None:
        """
        V1.1.2: 개별 시험 단위로 ClinicLink 생성.
        세션 집계(completed)와 독립적으로, 각 시험의 pass/fail을 개별 판정.
        """
        tenant_id = _resolve_tenant_id(int(session_progress.enrollment_id))
        if tenant_id is None or not enrollment_session_is_learning_todo_eligible(
            tenant_id=int(tenant_id),
            enrollment_id=int(session_progress.enrollment_id),
            session_id=int(session_progress.session_id),
        ):
            return

        exam_meta = session_progress.exam_meta or {}
        exam_rows = exam_meta.get("exams", [])

        for exam_row in exam_rows:
            exam_id = int(exam_row.get("exam_id", 0) or 0)
            if not exam_id:
                continue

            _reopen_stale_teacher_exam_resolution(
                enrollment_id=int(session_progress.enrollment_id),
                session=session_progress.session,
                exam_id=exam_id,
            )

            # Explicit NOT_SUBMITTED is projected separately as a reviewable
            # absence row. A genuinely missing ONLINE/PRESENT result is still
            # an actionable learning todo and needs a source-specific link.
            if exam_row.get("meta_status") == "NOT_SUBMITTED":
                continue

            if exam_row.get("no_result") or exam_row.get("score") is None:
                _idempotent_create_clinic_link(
                    enrollment_id=session_progress.enrollment_id,
                    session=session_progress.session,
                    source_type="exam",
                    source_id=exam_id,
                    reason=ClinicLink.Reason.AUTO_FAILED,
                    meta={
                        "kind": "EXAM_MISSING",
                        "kinds": ["EXAM_MISSING"],
                        "exam_id": exam_id,
                        "score": None,
                        "pass_score": exam_row.get("pass_score"),
                    },
                )
                continue

            passed = exam_row.get("passed", True)
            if passed:
                continue  # 이 시험은 합격 → ClinicLink 불필요

            with transaction.atomic():
                lock_enrollment_for_clinic_trigger(
                    enrollment_id=int(session_progress.enrollment_id),
                )
                if locked_representative_exam_is_not_submitted(
                    enrollment_id=int(session_progress.enrollment_id),
                    exam_id=exam_id,
                ):
                    continue

                latest_resolution_type = _latest_source_resolution_type(
                    enrollment_id=session_progress.enrollment_id,
                    session=session_progress.session,
                    source_type="exam",
                    source_id=exam_id,
                )
                if latest_resolution_type == ClinicLink.ResolutionType.EXAM_PASS:
                    continue

                _idempotent_create_clinic_link(
                    enrollment_id=session_progress.enrollment_id,
                    session=session_progress.session,
                    source_type="exam",
                    source_id=exam_id,
                    reason=ClinicLink.Reason.AUTO_FAILED,
                    meta={
                        "kind": "EXAM_FAILED",
                        "kinds": ["EXAM_FAILED"],
                        "exam_id": exam_id,
                        "score": exam_row.get("score"),
                        "pass_score": exam_row.get("pass_score"),
                    },
                )

    @staticmethod
    def auto_create_if_failed(session_progress: SessionProgress) -> None:
        """
        V1.1.2: 개별 시험 단위 ClinicLink로 전환.
        SessionProgress.completed를 보지 않고, exam_meta의 개별 pass/fail로 판정.
        """
        # 개별 시험 단위 생성으로 위임
        ClinicTriggerService.auto_create_per_exam(session_progress)

    @staticmethod
    def manual_create(
        *,
        enrollment_id: int,
        session_id: int,
        reason: str,
        memo: str | None = None,
        source_type: str | None = None,
        source_id: int | None = None,
    ) -> ClinicLink:
        tenant_id = _resolve_tenant_id(enrollment_id)
        return ClinicLink.objects.create(
            enrollment_id=enrollment_id,
            session_id=session_id,
            reason=reason,
            is_auto=False,
            memo=memo,
            source_type=source_type,
            source_id=source_id,
            tenant_id=tenant_id,
        )

    @staticmethod
    def auto_create_if_exam_risk(
        *,
        enrollment_id: int,
        session,
        exam_id: int,
    ) -> None:
        """
        V1.1.2: source_type/source_id로 시험별 개별 ClinicLink 생성.
        동시성: atomic + IntegrityError fallback으로 idempotent
        """
        if is_representative_exam_not_submitted(
            enrollment_id=int(enrollment_id),
            exam_id=int(exam_id),
        ):
            return

        reasons = ClinicExamRuleService.evaluate(
            enrollment_id=int(enrollment_id),
            exam_id=int(exam_id),
            lecture_id=getattr(session, "lecture_id", None),
        )
        if not reasons:
            return

        # 단일 atomic: select_for_update lock ↔ create 간 race window 차단.
        # (분리된 atomic 두 블록 사이에서 lock 이 풀려 동시 worker 가 동일 row 를
        # 둘 다 신규 생성으로 진입할 수 있던 결함을 제거.)
        with transaction.atomic():
            lock_enrollment_for_clinic_trigger(enrollment_id=int(enrollment_id))
            if locked_representative_exam_is_not_submitted(
                enrollment_id=int(enrollment_id),
                exam_id=int(exam_id),
            ):
                return

            existing = ClinicLink.objects.select_for_update().filter(
                enrollment_id=int(enrollment_id),
                session=session,
                source_type="exam",
                source_id=int(exam_id),
                resolved_at__isnull=True,
            ).order_by("id").first()

            if existing:
                # meta merge: 기존 kind 보존 + kinds 배열에 EXAM_RISK 누적
                merged = dict(existing.meta or {})
                kinds = list(merged.get("kinds") or [])
                legacy_kind = merged.get("kind")
                if legacy_kind and legacy_kind not in kinds:
                    kinds.append(legacy_kind)
                if "EXAM_RISK" not in kinds:
                    kinds.append("EXAM_RISK")
                merged["kinds"] = kinds
                merged.setdefault("kind", "EXAM_RISK")  # 하위 호환
                merged["exam_id"] = int(exam_id)
                merged["exam_reasons"] = reasons
                existing.meta = merged
                existing.is_auto = True
                existing.save(update_fields=["meta", "is_auto", "updated_at"])
                return

            # 같은 atomic 안에서 새로 생성. unique constraint 위반 시 helper 가
            # IntegrityError 를 catch 해 None 반환 (idempotent).
            latest_resolution_type = _latest_source_resolution_type(
                enrollment_id=int(enrollment_id),
                session=session,
                source_type="exam",
                source_id=int(exam_id),
            )
            if latest_resolution_type == ClinicLink.ResolutionType.EXAM_PASS:
                return

            _idempotent_create_clinic_link(
                enrollment_id=int(enrollment_id),
                session=session,
                source_type="exam",
                source_id=int(exam_id),
                reason=ClinicLink.Reason.AUTO_FAILED,
                meta={
                    "kind": "EXAM_RISK",
                    "kinds": ["EXAM_RISK"],
                    "exam_id": int(exam_id),
                    "exam_reasons": reasons,
                },
            )
