# PATH: apps/domains/results/services/sync_result_from_submission.py
"""
ONLINE 제출 채점 후 Result/ResultItem 동기화.
학생 결과 API(get_my_exam_result_data)가 Result를 사용하므로,
ExamResult만 있으면 404가 나는 문제를 해결하기 위해 호출.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from apps.domains.results.models import ExamResult, Result, ResultFact, ResultItem
from apps.domains.results.guards.grading_contract import GradingContractGuard
from apps.domains.results.guards.score_edit_lease_state import (
    invalidate_score_edit_leases_for_exam,
)
from apps.domains.results.services.attempt_service import ExamAttemptService
from apps.domains.results.services.answer_matching import (
    answer_matches,
    format_answer_for_display,
)
from apps.domains.results.services.manual_subjective_score import (
    explicit_manual_subjective_score_for_result,
)
from apps.domains.results.services.submission_answer_map import (
    build_submission_answers_map,
    require_complete_omr_answers,
)
from apps.domains.results.services.submission_scope_guard import validate_exam_submission_scope
from apps.domains.submissions.selectors import is_omr_scan_submission
from apps.support.results.exam_policy_dependencies import (
    effective_exam_pass_score,
)
from apps.support.omr.score_adjustment import get_score_adjustment_from_answers
from apps.support.omr.score_shape import get_exam_score_shape
from apps.support.exams.numeric_short_answer import (
    math_numeric_short_answer_question_ids,
    numeric_short_answer_matches,
)
from apps.support.results.grading_dependencies import (
    get_exam_for_result_sync,
    get_submission_for_result_sync,
)


def _sync_legacy_exam_result_snapshot(
    *,
    submission,
    exam,
    items_payload: list[dict],
    objective_score: float,
    objective_max_score: float,
) -> None:
    legacy = (
        ExamResult.objects
        .select_for_update()
        .filter(submission=submission)
        .first()
    )
    if legacy and legacy.status == ExamResult.Status.FINAL:
        return

    is_new = legacy is None
    legacy = legacy or ExamResult(
        submission=submission,
        exam=exam,
        status=ExamResult.Status.DRAFT,
    )
    breakdown = {
        str(item["question_number"]): {
            "question_id": item["question_id"],
            "correct": item["is_correct"],
            "earned": item["score"],
            "answer": item["answer"],
            "correct_answer": format_answer_for_display(item["correct_answer"]),
        }
        for item in items_payload
    }
    enrollment = getattr(submission, "enrollment", None)
    pass_score = effective_exam_pass_score(
        exam=exam,
        lecture_id=getattr(enrollment, "lecture_id", None),
    )
    legacy.exam = exam
    legacy.total_score = round(float(objective_score), 2)
    legacy.max_score = round(float(objective_max_score), 2)
    legacy.objective_score = round(float(objective_score), 2)
    legacy.breakdown = breakdown
    legacy.is_passed = legacy.total_score >= pass_score if pass_score > 0 else True
    legacy.status = ExamResult.Status.DRAFT
    if is_new:
        legacy.save()
    else:
        legacy.save(update_fields=[
            "exam",
            "total_score",
            "max_score",
            "objective_score",
            "breakdown",
            "is_passed",
            "status",
            "updated_at",
        ])


def _repair_attempt_initial_snapshot_for_submission(
    *,
    attempt,
    submission,
    total_score: float,
    max_score: float,
) -> None:
    if not attempt or int(getattr(attempt, "attempt_index", 0) or 0) != 1:
        return

    meta = dict(attempt.meta or {}) if isinstance(attempt.meta, dict) else {}
    snapshot = meta.get("initial_snapshot")
    if not isinstance(snapshot, dict):
        return

    try:
        snapshot_submission_id = int(snapshot.get("submission_id") or 0)
    except (TypeError, ValueError):
        return
    if snapshot_submission_id != int(submission.id):
        return

    if str(snapshot.get("source") or "") not in {
        "submission_sync",
        "omr_attached_manual_subjective",
        "omr_attached_manual_essay_items",
        "omr_replaced_manual_zero",
    }:
        return

    repaired_total = round(float(total_score), 2)
    repaired_max = round(float(max_score), 2)
    current_total = round(float(snapshot.get("total_score") or 0.0), 2)
    current_max = round(float(snapshot.get("max_score") or 0.0), 2)
    if current_total == repaired_total and current_max == repaired_max:
        return

    snapshot["total_score"] = repaired_total
    snapshot["max_score"] = repaired_max
    snapshot["repaired_at"] = timezone.now().isoformat()
    snapshot["repair_source"] = "sync_result_from_exam_submission"
    meta["initial_snapshot"] = snapshot
    attempt.meta = meta
    attempt.save(update_fields=["meta", "updated_at"])


@transaction.atomic
def sync_result_from_exam_submission(submission_id: int) -> Result | None:
    """
    Submission(ONLINE) 채점 완료 후 Result/ResultItem 생성·갱신.
    enrollment_id는 Submission에서 가져옴.
    """
    submission = get_submission_for_result_sync(submission_id=int(submission_id))
    if submission.target_type != "exam":
        return None

    exam = get_exam_for_result_sync(exam_id=int(submission.target_id))
    enrollment_id = getattr(submission, "enrollment_id", None)
    if not enrollment_id:
        return None
    enrollment = validate_exam_submission_scope(submission=submission, exam=exam)
    enrollment_id = int(enrollment.id)

    try:
        sheet, answer_key = GradingContractGuard.validate_exam_for_grading(exam)
    except Exception:
        return None
    invalidate_score_edit_leases_for_exam(
        exam=exam,
        tenant=exam.tenant,
        reason="AUTOMATIC_GRADING_COMPLETED",
    )
    score_shape = get_exam_score_shape(exam)

    key_map = {
        int(k): v
        for k, v in (answer_key.answers or {}).items()
        if str(k).isdigit()
    }
    score_adjustment = get_score_adjustment_from_answers(answer_key.answers or {})
    questions = list(sheet.questions.all().order_by("number"))
    numeric_short_answer_ids = math_numeric_short_answer_question_ids(
        exam=exam,
        question_ids=(int(q.id) for q in questions),
        question_kind=score_shape.question_kind,
        answers=answer_key.answers,
    )
    auto_score_questions = [
        q
        for q in questions
        if score_shape.question_kind(int(q.id)) != "essay"
        or int(q.id) in numeric_short_answer_ids
    ]
    essay_question_ids = {
        int(q.id)
        for q in questions
        if score_shape.question_kind(int(q.id)) == "essay"
    }
    question_number_to_id = {int(q.number): int(q.id) for q in questions}
    answers_map = build_submission_answers_map(
        submission=submission,
        question_number_to_id=question_number_to_id,
    )

    existing_result_prev = Result.objects.filter(
        target_type="exam",
        target_id=int(exam.id),
        enrollment_id=int(enrollment_id),
    ).first()
    require_complete_omr_answers(
        submission=submission,
        answers_map=answers_map,
        expected_question_ids={int(q.id) for q in auto_score_questions},
        context="sync_result_from_exam_submission",
        protect_existing_score=existing_result_prev is not None,
    )
    items_payload = []
    for q in auto_score_questions:
        qid = int(q.id)
        ans = answers_map.get(qid, "")
        correct_key = key_map.get(qid, "")
        is_correct = (
            numeric_short_answer_matches(ans, correct_key)
            if qid in numeric_short_answer_ids
            else answer_matches(ans, correct_key)
        )
        max_score = float(
            score_shape.question_max_score(qid, getattr(q, "score", 0))
        )
        score = max_score if is_correct else 0.0
        items_payload.append({
            "question_id": qid,
            "question_number": int(q.number),
            "answer": ans,
            "is_correct": is_correct,
            "score": score,
            "max_score": max_score,
            "correct_answer": correct_key,
            "source": "online",
        })

    total = 0.0
    max_total = 0.0
    for item in items_payload:
        total += item["score"]
        max_total += item["max_score"]

    auto_adjustment = score_adjustment.objective if auto_score_questions else 0.0
    if numeric_short_answer_ids:
        auto_adjustment += score_adjustment.subjective
    if auto_adjustment > 0:
        total += auto_adjustment
        max_total += auto_adjustment
    total = round(float(total), 2)
    max_total = round(float(max_total), 2)

    result_max_score = float(
        score_shape.total_max_score
        or getattr(exam, "max_score", 0.0)
        or max_total
        or 0.0
    )

    # 먼저 attempt 정책을 통과시킨다. 재응시 불가/최대 횟수 초과라면 Result를 건드리지 않는다.
    from apps.domains.results.models import ExamAttempt

    attempt = (
        ExamAttempt.objects
        .select_for_update()
        .filter(submission_id=submission.id)
        .first()
    )
    attached_placeholder = False
    created_attempt = False
    if not attempt and not essay_question_ids:
        attempt = ExamAttemptService.attach_matching_manual_objective_placeholder_for_submission(
            exam_id=int(exam.id),
            enrollment_id=int(enrollment_id),
            submission_id=int(submission.id),
            expected_items=items_payload,
            expected_objective_score=total,
            expected_max_score=result_max_score,
        )
        attached_placeholder = attempt is not None

    if not attempt:
        attempt = ExamAttemptService.attach_manual_score_placeholder_for_submission(
            exam_id=int(exam.id),
            enrollment_id=int(enrollment_id),
            submission_id=int(submission.id),
        )
        attached_placeholder = attempt is not None

    if not attempt:
        attempt = ExamAttemptService.create_for_submission(
            exam_id=int(exam.id),
            enrollment_id=int(enrollment_id),
            submission_id=int(submission.id),
            enforce_availability_window=not is_omr_scan_submission(submission),
        )
        created_attempt = True

    preserve_existing_subjective = bool(
        existing_result_prev
        and existing_result_prev.attempt_id
        and int(existing_result_prev.attempt_id) == int(attempt.id)
    )
    existing_subjective = 0.0
    if preserve_existing_subjective:
        existing_subjective = explicit_manual_subjective_score_for_result(
            result=existing_result_prev,
            attempt=attempt,
            score_shape=score_shape,
        )
    result_total = round(float(total) + float(existing_subjective), 2)

    attempt.status = "done"
    if int(attempt.attempt_index) == 1 and (created_attempt or attached_placeholder):
        attempt.meta = dict(attempt.meta or {}) if isinstance(attempt.meta, dict) else {}
        attempt.meta["initial_snapshot"] = {
            "total_score": float(result_total),
            "max_score": float(result_max_score),
            "submitted_at": timezone.now().isoformat(),
            "submission_id": int(submission.id),
        }
        if attached_placeholder:
            previous = attempt.meta.get("manual_score_placeholder")
            previous_initial = (
                previous.get("previous_initial_snapshot")
                if isinstance(previous, dict)
                else None
            )
            previous_source = (
                previous_initial.get("source")
                if isinstance(previous_initial, dict)
                else None
            )
            previous_meta_source = (
                previous.get("previous_meta_source")
                if isinstance(previous, dict)
                else None
            )
            reconciliation = (
                previous.get("reconciliation")
                if isinstance(previous, dict)
                else None
            )
            if reconciliation == "exact_manual_objective_match":
                attempt.meta["initial_snapshot"]["source"] = "omr_attached_matching_manual_result"
            elif previous_source == "admin_manual_subjective" and existing_subjective > 0:
                attempt.meta["initial_snapshot"]["source"] = "omr_attached_manual_subjective"
            elif previous_meta_source == "manual_entry" and existing_subjective > 0:
                attempt.meta["initial_snapshot"]["source"] = "omr_attached_manual_essay_items"
            else:
                attempt.meta["initial_snapshot"]["source"] = "omr_replaced_manual_zero"
        else:
            attempt.meta["initial_snapshot"]["source"] = "submission_sync"
        attempt.save(update_fields=["status", "meta", "updated_at"])
    else:
        attempt.save(update_fields=["status", "updated_at"])

    # 기존 Result가 있으면 이전 대표 attempt의 meta에 최종 snapshot 보존.
    # 재응시로 Result가 덮어쓰여도 이전 점수 이력이 손실되지 않음.
    if existing_result_prev and existing_result_prev.attempt_id:
        from apps.domains.results.models import ExamAttempt as _EA
        prev_rep = (
            _EA.objects.select_for_update()
            .filter(id=int(existing_result_prev.attempt_id))
            .first()
        )
        if prev_rep:
            m = dict(prev_rep.meta or {}) if isinstance(prev_rep.meta, dict) else {}
            m["final_result_snapshot"] = {
                "total_score": existing_result_prev.total_score,
                "max_score": existing_result_prev.max_score,
                "objective_score": existing_result_prev.objective_score,
                "submitted_at": (
                    existing_result_prev.submitted_at.isoformat()
                    if existing_result_prev.submitted_at else None
                ),
                "archived_at": timezone.now().isoformat(),
                "replaced_by_submission": int(submission.id),
            }
            prev_rep.meta = m
            prev_rep.save(update_fields=["meta", "updated_at"])

    # ✅ Legacy backfill: 기존 attempt_index=1에 initial_snapshot이 없으면
    # 재응시로 Result가 덮어써지기 전의 현재 Result 값으로 1회 backfill.
    # 관리자 수동 점수 입력 경로는 initial_snapshot 저장을 추가했지만,
    # 이 패치 이전 생성된 attempt_index=1은 snapshot이 없어 ranking이
    # Result.total_score(=재응시로 덮여쓸 값)로 fallback되어 "석차=1차" 정책이 깨진다.
    # sync 호출 직전 Result가 1차 값이라는 보장은 없으나, 아직 덮여쓰이기 전
    # 마지막 기회이므로 "없는 것보다는 낫다" 원칙으로 현재 Result 값을 잠근다.
    if existing_result_prev:
        from apps.domains.results.models import ExamAttempt as _EA
        prev_idx1 = (
            _EA.objects.select_for_update()
            .filter(
                exam_id=int(exam.id),
                enrollment_id=int(enrollment_id),
                attempt_index=1,
            )
            .first()
        )
        if prev_idx1:
            m1 = dict(prev_idx1.meta or {}) if isinstance(prev_idx1.meta, dict) else {}
            if "initial_snapshot" not in m1:
                m1["initial_snapshot"] = {
                    "total_score": existing_result_prev.total_score,
                    "max_score": existing_result_prev.max_score,
                    "submitted_at": (
                        existing_result_prev.submitted_at.isoformat()
                        if existing_result_prev.submitted_at else None
                    ),
                    "source": "legacy_backfill",
                    "backfilled_at": timezone.now().isoformat(),
                }
                prev_idx1.meta = m1
                prev_idx1.save(update_fields=["meta", "updated_at"])

    result, _ = Result.objects.get_or_create(
        target_type="exam",
        target_id=int(exam.id),
        enrollment_id=int(enrollment_id),
        defaults={"total_score": 0, "max_score": 0},
    )

    if essay_question_ids:
        ResultItem.objects.filter(
            result=result,
            question_id__in=essay_question_ids,
            source__in=["online", "omr"],
        ).delete()

    for item in items_payload:
        ResultFact.objects.create(
            target_type="exam",
            target_id=int(exam.id),
            enrollment_id=int(enrollment_id),
            submission_id=int(submission.id),
            attempt_id=int(attempt.id),
            question_id=item["question_id"],
            answer=item["answer"],
            is_correct=item["is_correct"],
            score=item["score"],
            max_score=item["max_score"],
            source=item["source"],
        )
        ResultItem.objects.update_or_create(
            result=result,
            question_id=item["question_id"],
            defaults={
                "answer": item["answer"],
                "is_correct": item["is_correct"],
                "include_in_wrong_note": False,
                "score": item["score"],
                "max_score": item["max_score"],
                "source": item["source"],
            },
        )
    result.total_score = result_total
    result.max_score = result_max_score
    result.objective_score = total  # 자동채점 문항 합산 = objective score
    result.submitted_at = timezone.now()
    result.save(update_fields=["total_score", "max_score", "objective_score", "submitted_at", "updated_at"])

    result.attempt_id = attempt.id
    result.save(update_fields=["attempt_id", "updated_at"])

    _sync_legacy_exam_result_snapshot(
        submission=submission,
        exam=exam,
        items_payload=items_payload,
        objective_score=total,
        objective_max_score=max_total,
    )
    _repair_attempt_initial_snapshot_for_submission(
        attempt=attempt,
        submission=submission,
        total_score=result_total,
        max_score=result_max_score,
    )

    return result
