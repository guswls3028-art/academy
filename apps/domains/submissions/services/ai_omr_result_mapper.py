from __future__ import annotations

from typing import Any, Dict, Optional
import logging
from datetime import datetime, timezone

from django.db import transaction

from apps.domains.submissions.models import OMRStudentMatch, Submission
from apps.domains.submissions.services.omr_submission_guards import (
    duplicate_conflict_payload,
)
from apps.domains.submissions.services.lifecycle import (
    STUCK_RECOVERABLE_STATUSES,
    fail_submission_in_memory,
    mark_answers_ready_in_memory,
    mark_needs_identification,
    reopen_for_regrade_in_memory,
)
from apps.support.omr.exam_structure import load_submission_exam_structure

logger = logging.getLogger(__name__)

# 멱등성: 이미 처리된 submission은 callback을 무시한다
_ALREADY_PROCESSED_STATUSES = frozenset({
    Submission.Status.ANSWERS_READY,
    Submission.Status.GRADING,
    Submission.Status.DONE,
    Submission.Status.SUPERSEDED,
})


def _extract_worker_result(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    AI worker result body extraction.

    callbacks.py can pass the worker body either nested under ``result`` or
    flattened at the top level. Keep this in one helper so the idempotency
    guard can inspect late results before deciding to skip.
    """
    nested = payload.get("result")
    if isinstance(nested, dict) and nested:
        return nested

    exclude = {"submission_id", "status", "error"}
    return {k: v for k, v in payload.items() if k not in exclude}


def _can_hydrate_late_ai_answers(
    *,
    submission: Submission,
    result: Dict[str, Any],
) -> bool:
    """
    Allow a narrow late-callback recovery path.

    Operators may manually identify a student while the worker is still
    running. The old flow immediately graded the submission with zero answers,
    then the later AI callback was ignored because status=DONE. If the operator
    already selected a valid enrollment and no answers exist yet, it is safe to
    hydrate the worker answers and re-run grading.
    """
    if submission.source != Submission.Source.OMR_SCAN:
        return False
    if submission.status == Submission.Status.FAILED:
        recovery = (submission.meta or {}).get("state_recovery") or {}
        recovered_from = str(recovery.get("from_status") or "")
        if (
            recovered_from not in STUCK_RECOVERABLE_STATUSES
            or submission.error_message != f"stuck:{recovered_from}_timeout"
        ):
            return False
    elif submission.status not in (
        Submission.Status.ANSWERS_READY,
        Submission.Status.DONE,
    ):
        return False
    if not submission.enrollment_id:
        return False

    answers = result.get("answers")
    if not isinstance(answers, list) or not answers:
        return False

    from apps.domains.submissions.models import SubmissionAnswer

    return not SubmissionAnswer.objects.filter(submission=submission).exists()


def _validate_worker_contract(
    submission: Submission, payload: Dict[str, Any]
) -> Optional[int]:
    """
    AI worker callback payload 를 OMRWorkerCallback schema 로 검증.

    Returns:
        None  schema 통과 → caller 는 정상 흐름 계속.
        submission.id  schema 위반 → manual_review 마킹 + meta 기록 후 caller 종료.

    silent failure 차단: 새 worker version, 필수 키 누락, 타입 오류가 prod 에 들어와도
    학원장 화면에 "검토 필요" 표시가 뜨고 audit log 가 남는다.
    """
    from apps.domains.submissions.omr_pipeline.contracts import parse_worker_callback

    callback, err = parse_worker_callback(payload)
    if callback is not None:
        return None

    logger.error(
        "OMR_WORKER_CONTRACT_VIOLATION | submission_id=%s | error=%s",
        submission.id, err,
    )
    now_iso = datetime.now(timezone.utc).isoformat()
    meta = dict(submission.meta or {})
    meta.setdefault("manual_review", {})
    meta["manual_review"]["required"] = True
    reasons = list(meta["manual_review"].get("reasons") or [])
    if "WORKER_CONTRACT_VIOLATION" not in reasons:
        reasons.append("WORKER_CONTRACT_VIOLATION")
    meta["manual_review"]["reasons"] = sorted(set(reasons))
    meta["manual_review"]["updated_at"] = now_iso
    meta["worker_contract_violation"] = {
        "at": now_iso,
        "error": (err or "")[:2000],
    }
    submission.meta = meta
    submission.save(update_fields=["meta", "updated_at"])
    return submission.id


@transaction.atomic
def apply_omr_ai_result(payload: Dict[str, Any]) -> Optional[int]:
    """
    OMR AI 결과를 Submission에 반영. 순서: identifier 매칭 → NEEDS_IDENTIFICATION 결정 → 답안 저장.
    AI job 최종 상태(DONE/REVIEW_REQUIRED/FAILED)는 InternalAIJobResultView + status_resolver에서 결정.
    """
    submission_id = payload.get("submission_id")
    if not submission_id:
        return None

    try:
        submission = Submission.objects.select_for_update().get(id=int(submission_id))
    except Submission.DoesNotExist:
        logger.warning("apply_omr_ai_result: submission %s not found", submission_id)
        return None

    # ── worker contract 검증 (silent failure 차단) ──────────────────────────
    # 새 worker version / 깨진 payload / 빠진 필수 키 들어오면 즉시 manual_review
    # 강제하고 자동 채점은 막는다. 학원장이 빈 시트나 잘못된 점수를 받는 것보다
    # "검토 필요" 표시가 안전하다.
    _violation_id = _validate_worker_contract(submission, payload)
    if _violation_id is not None:
        return _violation_id

    # 🔐 tenant 교차검증: AI job의 tenant_id와 submission의 tenant_id 일치 확인
    job_id = payload.get("job_id")
    payload_tenant_id = payload.get("tenant_id")
    if payload_tenant_id and hasattr(submission, "tenant_id") and submission.tenant_id:
        if str(payload_tenant_id) != str(submission.tenant_id):
            logger.error(
                "TENANT_ISOLATION_VIOLATION | apply_omr_ai_result | "
                "job_id=%s | payload_tenant=%s | submission_tenant=%s | submission_id=%s",
                job_id, payload_tenant_id, submission.tenant_id, submission_id,
            )
            return None

    status = payload.get("status")
    error = payload.get("error")

    # AI worker는 version/answers/identifier를 payload 최상위에 보낸다.
    # 구버전 호환: payload["result"] 하위에 있을 수도 있다.
    result = _extract_worker_result(payload)

    late_answer_hydration = _can_hydrate_late_ai_answers(
        submission=submission,
        result=result,
    )
    if submission.status in _ALREADY_PROCESSED_STATUSES:
        if not late_answer_hydration:
            logger.info(
                "apply_omr_ai_result: submission %s already %s, skipping (idempotent)",
                submission_id, submission.status,
            )
            return submission.id
        logger.warning(
            "OMR_LATE_AI_RESULT_HYDRATE | submission_id=%s | status=%s | enrollment_id=%s",
            submission.id,
            submission.status,
            submission.enrollment_id,
        )

    meta = dict(submission.meta or {})
    meta["ai_result"] = {
        "status": status,
        "result": result,
        "error": error,
        "received_at": datetime.now(timezone.utc).isoformat(),
        "kind": "omr_scan",
    }
    submission.meta = meta

    if status in {"FAILED", "REJECTED_BAD_INPUT", "FALLBACK_TO_GPU"} or error:
        fail_submission_in_memory(
            submission,
            error_message=error or "AI worker failed",
            actor="ai_omr_mapper",
        )
        submission.save(update_fields=["meta", "status", "error_message", "updated_at"])
        return submission.id

    answers = result.get("answers") or []
    identifier = result.get("identifier")

    # ── answer 영속화 + 검토 메타 (Phase C: omr_pipeline.services.answer_persister) ──
    # 책임 분리: question_number → ExamQuestion.id 매핑, SubmissionAnswer upsert,
    # answer_stats 집계, manual_review reasons (ANSWER_* + ALIGNMENT_FAILED +
    # ANSWER_QNUM_NOT_IN_SHEET) 는 모두 persist_answers 안에서 결정한다.
    exam_structure = load_submission_exam_structure(submission)
    from apps.domains.submissions.omr_pipeline.services.answer_persister import (
        persist_answers,
    )
    from apps.domains.submissions.omr_pipeline.services.facts import (
        get_current_student_match,
        record_recognition_fact,
        record_student_match_fact,
    )

    persist_result = persist_answers(
        submission=submission,
        answers_payload=answers if isinstance(answers, list) else [],
        worker_result_meta=result if isinstance(result, dict) else {},
        exam_structure=exam_structure,
    )
    record_recognition_fact(
        submission=submission,
        job_id=str(job_id or ""),
        status=str(status or ""),
        error=str(error or "") if error else None,
        worker_result=result if isinstance(result, dict) else {},
        exam_structure=exam_structure,
    )

    answer_stats = persist_result.answer_stats
    reasons = list(persist_result.manual_review_reasons)
    unmapped_questions = persist_result.unmapped_questions
    manual_required = persist_result.manual_required

    # ✅ 식별 + ExamEnrollment 락 + duplicate 검사 (Phase E: enrollment_finalizer)
    # IdentifierMatcher → lock_exam_enrollment_candidate → find_conflicting_exam_submission
    # 의 한 묶음을 단일 함수가 처리한다. mapper 는 결과 dataclass 만 보고 meta / transit 결정.
    from apps.domains.submissions.omr_pipeline.services.enrollment_finalizer import (
        finalize_enrollment,
        finalize_existing_enrollment,
    )

    current_match = get_current_student_match(submission)
    preserve_existing_match = bool(
        submission.enrollment_id
        and (
            late_answer_hydration
            or (
                current_match is not None
                and current_match.status == OMRStudentMatch.Status.CONFIRMED
            )
            or str(meta.get("identifier_status") or "").startswith("matched")
        )
    )

    if preserve_existing_match:
        enrollment_id = int(submission.enrollment_id)
        enroll_result = finalize_existing_enrollment(
            submission=submission,
            enrollment_id=enrollment_id,
            identifier_payload=identifier,
            identifier_status=str(meta.get("identifier_status") or "matched"),
            identifier_match_kind=str(
                meta.get("identifier_match_kind") or "preserved_manual"
            ),
        )
        enrollment_id = enroll_result.enrollment_id
        identifier_status = enroll_result.identifier_status
        identifier_match_kind = enroll_result.identifier_match_kind
        identifier_ok = enroll_result.identifier_ok
        duplicate_conflict = enroll_result.duplicate_conflict
        if enroll_result.manual_required:
            manual_required = True
        reasons.extend(enroll_result.review_reasons)
    else:
        enroll_result = finalize_enrollment(
            submission=submission, identifier_payload=identifier,
        )
        enrollment_id = enroll_result.enrollment_id
        identifier_status = enroll_result.identifier_status
        identifier_match_kind = enroll_result.identifier_match_kind
        identifier_ok = enroll_result.identifier_ok
        duplicate_conflict = enroll_result.duplicate_conflict
        if enroll_result.manual_required:
            manual_required = True
        reasons.extend(enroll_result.review_reasons)

    meta.setdefault("manual_review", {})
    meta["manual_review"]["required"] = bool(manual_required or not identifier_ok)
    meta["manual_review"]["reasons"] = sorted(set(reasons))
    meta["manual_review"]["updated_at"] = datetime.now(timezone.utc).isoformat()
    if unmapped_questions:
        meta["manual_review"]["unmapped_questions"] = sorted(set(unmapped_questions))
    meta["identifier_status"] = identifier_status
    meta["identifier_match_kind"] = identifier_match_kind
    meta["answer_stats"] = answer_stats
    if duplicate_conflict:
        meta["duplicate_conflict"] = duplicate_conflict_payload(duplicate_conflict)

    submission.meta = meta

    if not identifier_ok:
        record_student_match_fact(
            submission=submission,
            enrollment_id=None,
            status=OMRStudentMatch.Status.UNMATCHED,
            method=OMRStudentMatch.Method.AUTO_IDENTIFIER,
            actor="ai_omr_mapper",
            identifier_status=identifier_status,
            identifier_payload=identifier,
        )
        mark_needs_identification(submission, actor="ai_omr_mapper")
        submission.save(update_fields=["meta", "status", "error_message", "updated_at"])
        return submission.id

    if duplicate_conflict:
        record_student_match_fact(
            submission=submission,
            enrollment_id=int(enrollment_id) if enrollment_id else None,
            status=OMRStudentMatch.Status.DUPLICATE,
            method=(
                OMRStudentMatch.Method.PRESERVED_MANUAL
                if preserve_existing_match
                else OMRStudentMatch.Method.AUTO_IDENTIFIER
            ),
            actor="ai_omr_mapper",
            identifier_status=identifier_status,
            identifier_payload=identifier,
        )
        mark_needs_identification(
            submission,
            error_message="duplicate_enrollment",
            actor="ai_omr_mapper",
        )
        submission.save(update_fields=["meta", "status", "error_message", "updated_at"])
        return submission.id

    submission.enrollment_id = int(enrollment_id)
    record_student_match_fact(
        submission=submission,
        enrollment_id=int(enrollment_id),
        status=OMRStudentMatch.Status.CONFIRMED,
        method=(
            OMRStudentMatch.Method.PRESERVED_MANUAL
            if preserve_existing_match
            else OMRStudentMatch.Method.AUTO_IDENTIFIER
        ),
        actor="ai_omr_mapper",
        identifier_status=identifier_status,
        identifier_payload=identifier,
    )
    if late_answer_hydration and submission.status in (
        Submission.Status.DONE,
        Submission.Status.FAILED,
    ):
        reopen_for_regrade_in_memory(submission, actor="ai_omr_mapper.late_result")
    elif not late_answer_hydration:
        mark_answers_ready_in_memory(submission, actor="ai_omr_mapper")
    submission.save(update_fields=["meta", "status", "enrollment_id", "error_message", "updated_at"])

    return submission.id


# 콜백에서 사용 (apply_omr_ai_result와 동일, 네이밍 일관용)
apply_ai_result = apply_omr_ai_result
