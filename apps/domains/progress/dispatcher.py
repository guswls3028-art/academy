# apps/domains/progress/dispatcher.py
from __future__ import annotations

from typing import Iterable, Optional
import logging

from apps.domains.progress.services.progress_pipeline import ProgressPipelineService

logger = logging.getLogger(__name__)


def refresh_exam_target_projections(*, exam):
    """Projection-only context for a transactional exam creation/roster edit."""
    from apps.domains.progress.services.exam_target_projection import (
        refresh_exam_target_projections as refresh,
    )

    return refresh(exam=exam)


def dispatch_progress_pipeline(
    *,
    exam_id: Optional[int] = None,
    submission_id: Optional[int] = None,
    enrollment_id: Optional[int] = None,
    session_id: Optional[int] = None,
) -> None:
    """
    ✅ Results/Clinic → Progress 진입점 (SSOT, 동기)

    지원 경로:
    - submission_id : 제출(Submission) 하나 기준 재계산
    - exam_id       : 해당 시험을 본 모든 응시자 재계산
    - enrollment_id + session_id : 특정 학생 × 특정 세션 한 점 재계산
      (homework ClinicLink 해소, admin manual resolve 등 exam_id 가 없는 진입점용)

    - Celery/Queue 금지
    - Idempotent service만 호출
    - 실패 시 예외를 올려서 상위 orchestrator가 관측/재시도 정책을 가진다.
    """
    ProgressPipelineService().apply(
        exam_id=exam_id,
        submission_id=submission_id,
        enrollment_id=enrollment_id,
        session_id=session_id,
    )


def schedule_homework_target_progress(*, session_id: int, enrollment_ids: Iterable[int]) -> None:
    """Refresh validated homework roster additions only after the edit commits."""
    from apps.domains.progress.services.clinic_resolution_service import (
        _dispatch_progress_for_enrollment_session,
    )

    for enrollment_id in sorted({int(value) for value in enrollment_ids}):
        _dispatch_progress_for_enrollment_session(
            enrollment_id=enrollment_id, session_id=int(session_id),
        )


def resolve_removed_source_clinic_links(
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
    Public progress-domain entrypoint for closing ClinicLinks whose source was removed.

    Callers outside the progress domain must use this dispatcher-level API instead
    of importing progress.services internals directly.
    """
    from apps.domains.progress.services.clinic_resolution_service import ClinicResolutionService

    return ClinicResolutionService.resolve_by_removed_source(
        tenant_id=tenant_id,
        session_id=session_id,
        source_type=source_type,
        source_id=source_id,
        enrollment_ids=enrollment_ids,
        user_id=user_id,
        reason=reason,
    )


def resolve_exam_not_submitted_clinic_links(
    *,
    tenant_id: int,
    enrollment_id: int,
    exam_id: int,
    attempt_id: int,
    user_id: int | None = None,
) -> int:
    """Audit-close failure links invalidated by an explicit absent action."""
    from apps.domains.progress.services.clinic_resolution_service import (
        ClinicResolutionService,
    )

    return ClinicResolutionService.resolve_by_exam_not_submitted(
        tenant_id=tenant_id,
        enrollment_id=enrollment_id,
        exam_id=exam_id,
        attempt_id=attempt_id,
        user_id=user_id,
    )


def set_teacher_assessment_resolution(
    *,
    tenant_id: int,
    enrollment_id: int,
    session_id: int,
    source_type: str,
    source_id: int,
    completed: bool,
    correction_id: int,
    user_id: int | None,
    memo: str,
    source_fingerprint: str | None = None,
):
    """Public progress entrypoint for score-preserving teacher decisions."""
    from apps.domains.progress.services.clinic_resolution_service import ClinicResolutionService

    return ClinicResolutionService.set_teacher_assessment_resolution(
        tenant_id=tenant_id,
        enrollment_id=enrollment_id,
        session_id=session_id,
        source_type=source_type,
        source_id=source_id,
        completed=completed,
        correction_id=correction_id,
        user_id=user_id,
        memo=memo,
        source_fingerprint=source_fingerprint,
    )
