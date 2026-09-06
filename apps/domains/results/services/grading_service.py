from __future__ import annotations

from django.db import transaction

from apps.domains.results.models import ExamResult
from apps.domains.results.services.exam_grading_service import ExamGradingService
from apps.domains.results.services.sync_result_from_submission import (
    sync_result_from_exam_submission,
)
from apps.domains.results.services.omr_subjective_completion import (
    finalize_omr_result_if_ready,
)
from apps.support.results.grading_dependencies import (
    dispatch_progress_pipeline,
    get_submission_for_grading,
    is_omr_manual_review_required,
)


@transaction.atomic
def grade_submission(submission_id: int, *, force_regrade: bool = False) -> ExamResult:
    service = ExamGradingService()
    result = service.auto_grade_objective(
        submission_id=int(submission_id),
        force_regrade=force_regrade,
    )

    submission = get_submission_for_grading(submission_id=int(submission_id))

    if is_omr_manual_review_required(submission):
        # 검토 필요 OMR은 관리자 DRAFT 점수만 계산하고, 학생 Result/진도/클리닉
        # 스냅샷에는 운영자가 확인 저장한 뒤 반영한다.
        return result

    # ✅ 모든 source(ONLINE, OMR_SCAN 등)에서 Result/ResultItem 동기화 (학생 결과 API용)
    canonical_result = None
    try:
        canonical_result = sync_result_from_exam_submission(submission_id)
    except Exception:
        import logging
        logging.getLogger(__name__).exception(
            "Result sync failed for submission %s", submission_id
        )
        raise

    # A fully recognized OMR submission has no remaining teacher-owned grading
    # step. Publish its compatibility snapshot before progress/analytics read it.
    # OMR rows explicitly marked for manual review remain DRAFT above.
    if submission.source == submission.Source.OMR_SCAN:
        if canonical_result is None:
            return result
        decision = finalize_omr_result_if_ready(result_id=int(canonical_result.id))
        if not decision.projection_ready:
            return result
        result.refresh_from_db()

    # ✅ 시험 채점 완료 → progress / clinic 자동 갱신
    dispatch_progress_pipeline(submission_id=int(submission_id))

    return result
