"""Cross-domain helpers for submission-facing orchestration.

Submission views/services should keep status transitions in the submissions
lifecycle while cross-domain lookups stay behind this support boundary.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExamEnrollmentCandidate:
    ok: bool
    detail: str = ""
    should_create: bool = False


@dataclass(frozen=True)
class ExamQuestionIdMap:
    question_number_to_pk: dict[int, int]

    @property
    def pk_set(self) -> set[int]:
        return set(self.question_number_to_pk.values())

    @property
    def question_number_set(self) -> set[int]:
        return set(self.question_number_to_pk.keys())


def request_is_parent(request: Any) -> bool:
    from apps.domains.student_app.permissions import is_request_parent

    return is_request_parent(request)


def request_student(request: Any):
    from apps.domains.student_app.permissions import get_request_student

    return get_request_student(request)


def grade_submission_objective(submission_id: int, *, force_regrade: bool = False):
    from apps.domains.results.services.grading_service import grade_submission

    return grade_submission(int(submission_id), force_regrade=force_regrade)


def lock_submission_score_edit_scope_before_write(*, submission) -> list[int]:
    """Lock an exam submission's Exam -> Session scope before mutable rows."""

    from apps.support.results.grading_dependencies import (
        lock_score_edit_scope_before_submission_grading,
    )

    return lock_score_edit_scope_before_submission_grading(submission=submission)


def rebind_representative_omr_submission(
    *,
    exam_id: int,
    enrollment_id: int,
    replacement_submission_id: int,
    allowed_previous_submission_ids: set[int],
    actor: str,
):
    """Rebind a replacement scan to the same physical exam sitting."""
    from django.core.exceptions import ValidationError
    from django.db import transaction
    from django.utils import timezone

    from apps.domains.results.models import ExamAttempt
    from apps.domains.submissions.models import Submission

    with transaction.atomic():
        attempt = (
            ExamAttempt.objects.select_for_update()
            .filter(
                exam_id=int(exam_id),
                enrollment_id=int(enrollment_id),
                is_representative=True,
            )
            .first()
        )
        if attempt is None:
            return None
        if int(attempt.submission_id or 0) == int(replacement_submission_id):
            return attempt
        previous_submission_id = int(attempt.submission_id or 0)
        if previous_submission_id not in {int(value) for value in allowed_previous_submission_ids}:
            raise ValidationError("Representative attempt is not owned by the OMR replacement set.")

        submissions = {
            int(row.id): row
            for row in Submission.objects.select_for_update().filter(
                id__in=[previous_submission_id, int(replacement_submission_id)]
            )
        }
        previous = submissions.get(previous_submission_id)
        replacement = submissions.get(int(replacement_submission_id))
        if previous is None or replacement is None:
            raise ValidationError("OMR replacement submissions could not be resolved.")
        expected = (
            previous.tenant_id,
            previous.target_type,
            int(previous.target_id),
            int(previous.enrollment_id or 0),
            previous.source,
        )
        actual = (
            replacement.tenant_id,
            replacement.target_type,
            int(replacement.target_id),
            int(replacement.enrollment_id or 0),
            replacement.source,
        )
        if (
            expected != actual
            or actual[1] != Submission.TargetType.EXAM
            or actual[4] != Submission.Source.OMR_SCAN
        ):
            raise ValidationError("OMR replacement scope mismatch.")
        if actual[2] != int(exam_id) or actual[3] != int(enrollment_id):
            raise ValidationError("OMR replacement exam or enrollment mismatch.")
        if (
            ExamAttempt.objects.select_for_update()
            .filter(submission_id=int(replacement_submission_id))
            .exclude(id=attempt.id)
            .exists()
        ):
            raise ValidationError("Replacement submission already belongs to another attempt.")

        now = timezone.now().isoformat()
        meta = dict(attempt.meta or {}) if isinstance(attempt.meta, dict) else {}
        replacements = list(meta.get("omr_scan_replacements") or [])
        replacements.append(
            {
                "previous_submission_id": previous_submission_id,
                "replacement_submission_id": int(replacement_submission_id),
                "reason": "same_exam_rescan_not_retake",
                "actor": actor,
                "at": now,
            }
        )
        meta["omr_scan_replacements"] = replacements
        initial = meta.get("initial_snapshot")
        if (
            isinstance(initial, dict)
            and int(initial.get("submission_id") or 0) == previous_submission_id
        ):
            initial["submission_id"] = int(replacement_submission_id)
            initial["previous_submission_id"] = previous_submission_id
            initial["scan_rebound_at"] = now
            meta["initial_snapshot"] = initial
        attempt.submission_id = int(replacement_submission_id)
        attempt.status = "pending"
        attempt.meta = meta
        attempt.save(update_fields=["submission_id", "status", "meta", "updated_at"])
        return attempt


def dispatch_submission_ai_job(**kwargs: Any) -> Any:
    from apps.domains.ai.gateway import dispatch_job

    return dispatch_job(**kwargs)


def exam_belongs_to_tenant(*, exam_id: int, tenant: Any) -> bool:
    from apps.domains.exams.models import Exam

    return Exam.objects.filter(id=int(exam_id), tenant=tenant).exists()


def complete_submission_after_auto_grade(submission, *, actor: str) -> None:
    from django.core.exceptions import ValidationError

    from apps.domains.submissions.models import Submission
    from apps.domains.submissions.services.lifecycle import (
        can_mark_done,
        mark_done,
        mark_grading,
    )

    if submission.status == Submission.Status.ANSWERS_READY:
        mark_grading(submission, actor=actor)
        mark_done(submission, actor=actor)
        return

    if can_mark_done(submission.status):
        mark_done(submission, actor=actor)
        return

    logger.error(
        "Submission %s in status '%s' cannot transition to 'done'; "
        "aborting grading to preserve data consistency.",
        submission.id,
        submission.status,
    )
    raise ValidationError(
        f"Submission {submission.id} in status '{submission.status}' "
        f"cannot be graded - invalid state for transition to 'done'."
    )


def regrade_exam_submissions(*, tenant, exam_id: int, actor: str) -> dict[str, Any]:
    from django.db import transaction

    from apps.domains.enrollment.models import Enrollment
    from apps.domains.results.models import ExamAttempt, Result
    from apps.domains.submissions.models import Submission
    from apps.domains.submissions.services.lifecycle import reopen_for_regrade
    from apps.support.results.grading_dependencies import (
        lock_exam_and_score_edit_scope_for_grading,
    )

    regradable_statuses = {
        Submission.Status.DONE,
        Submission.Status.ANSWERS_READY,
    }
    submissions = list(
        Submission.objects.filter(
            tenant=tenant,
            target_type=Submission.TargetType.EXAM,
            target_id=int(exam_id),
        )
        .exclude(status=Submission.Status.SUPERSEDED)
        .order_by("id")
        .values_list("id", "status")
    )

    graded = 0
    skipped = 0
    failed: list[dict[str, object]] = []

    for submission_id, current_status in submissions:
        if current_status not in regradable_statuses:
            skipped += 1
            continue
        try:
            with transaction.atomic():
                lock_exam_and_score_edit_scope_for_grading(
                    exam_id=int(exam_id),
                    tenant_id=int(tenant.id),
                )
                submission = Submission.objects.select_for_update().get(id=int(submission_id))
                if submission.status not in regradable_statuses:
                    skipped += 1
                    continue
                enrollment = Enrollment.objects.select_for_update().get(
                    id=int(submission.enrollment_id),
                    tenant=tenant,
                )
                Result.objects.select_for_update().filter(
                    target_type="exam",
                    target_id=int(exam_id),
                    enrollment_id=int(enrollment.id),
                ).first()
                attempt = (
                    ExamAttempt.objects.select_for_update()
                    .filter(
                        exam_id=int(exam_id),
                        submission_id=int(submission_id),
                        enrollment__tenant=tenant,
                    )
                    .first()
                )
                if (
                    attempt is not None
                    and isinstance(attempt.meta, dict)
                    and attempt.meta.get("status") == "NOT_SUBMITTED"
                ):
                    skipped += 1
                    continue
                if submission.status != Submission.Status.ANSWERS_READY:
                    reopen_for_regrade(submission, actor=actor)
                grade_submission_objective(int(submission_id), force_regrade=True)
            graded += 1
        except Exception as exc:
            failed.append(
                {
                    "submission_id": int(submission_id),
                    "status": str(current_status),
                    "detail": str(exc) or exc.__class__.__name__,
                }
            )

    return {
        "exam_id": int(exam_id),
        "total": len(submissions),
        "graded": graded,
        "skipped": skipped,
        "failed": failed,
    }


def target_belongs_to_tenant(target_type, target_id, tenant) -> bool:
    from apps.domains.submissions.models import Submission

    try:
        target_id_i = int(target_id)
        if target_type == Submission.TargetType.EXAM:
            from apps.domains.exams.models import Exam

            return Exam.objects.filter(
                id=target_id_i,
                tenant=tenant,
                sessions__lecture__tenant=tenant,
            ).exists()
        if target_type == Submission.TargetType.HOMEWORK:
            from apps.domains.homework_results.models import Homework

            return (
                Homework.objects.filter(
                    id=target_id_i,
                    session__lecture__tenant=tenant,
                )
                .exclude(meta__removed_from_session_at__isnull=False)
                .exists()
            )
    except Exception:
        return False
    return False


def homework_submission_target_exists(*, homework_id: int, tenant) -> bool:
    from apps.domains.homework_results.models import Homework

    return Homework.objects.filter(
        id=int(homework_id),
        session__lecture__tenant=tenant,
    ).exists()


def clinic_highlight_map_for_enrollments(*, tenant, enrollment_ids: set[int]) -> dict[int, bool]:
    if not enrollment_ids:
        return {}

    from apps.domains.results.utils.clinic_highlight import compute_clinic_highlight_map

    return compute_clinic_highlight_map(
        tenant=tenant,
        enrollment_ids=enrollment_ids,
    )


def enrollment_map_for_submission_list(*, tenant, enrollment_ids: set[int]) -> dict[int, Any]:
    if not enrollment_ids:
        return {}

    from apps.domains.enrollment.models import Enrollment

    return {
        enrollment.id: enrollment
        for enrollment in (
            Enrollment.objects.select_related("student", "lecture")
            .filter(id__in=enrollment_ids, tenant=tenant)
        )
    }


def exam_submission_list_allowed(*, tenant, exam_id: int) -> bool:
    from apps.domains.exams.models import Exam
    from apps.domains.submissions.models import Submission

    exam_id_i = int(exam_id)
    exam_qs = Exam.objects.filter(id=exam_id_i)
    if exam_qs.filter(sessions__lecture__tenant=tenant).exists():
        return True
    if hasattr(Exam, "tenant") and exam_qs.filter(tenant=tenant).exists():
        return True
    return Submission.objects.filter(
        tenant=tenant,
        target_type=Submission.TargetType.EXAM,
        target_id=exam_id_i,
    ).exists()


def score_map_for_exam_submission_list(*, submission_ids: list[int]) -> dict[int, float]:
    if not submission_ids:
        return {}

    from apps.domains.results.models import ExamAttempt, Result

    attempt_ids = list(
        ExamAttempt.objects.filter(submission_id__in=submission_ids)
        .values_list("id", flat=True)
    )
    if not attempt_ids:
        return {}

    score_map: dict[int, float] = {}
    results_qs = (
        Result.objects.filter(attempt_id__in=attempt_ids)
        .select_related("attempt")
        .only("id", "attempt_id", "attempt__submission_id", "total_score")
        .order_by("-id")
    )
    for result in results_qs:
        attempt = result.attempt
        if not attempt or not attempt.submission_id:
            continue
        submission_id = int(attempt.submission_id)
        if submission_id not in score_map and result.total_score is not None:
            score_map[submission_id] = float(result.total_score)
    return score_map


def enrollment_belongs_to_tenant(*, enrollment_id, tenant) -> bool:
    from apps.domains.enrollment.models import Enrollment

    return Enrollment.objects.filter(id=enrollment_id, tenant=tenant).exists()


def student_owns_enrollment(*, enrollment_id, student, tenant) -> bool:
    from apps.domains.enrollment.models import Enrollment

    return Enrollment.objects.filter(
        id=enrollment_id,
        student=student,
        tenant=tenant,
    ).exists()


def target_enrollment_assignment_exists(
    target_type,
    target_id,
    enrollment_id,
    tenant,
    *,
    ensure_exam_enrollment: bool = False,
) -> bool:
    from apps.domains.submissions.models import Submission

    try:
        target_id_i = int(target_id)
        enrollment_id_i = int(enrollment_id)
    except (TypeError, ValueError):
        return False

    from apps.domains.enrollment.models import Enrollment, SessionEnrollment

    enrollment = (
        Enrollment.objects.filter(
            id=enrollment_id_i,
            tenant=tenant,
            status="ACTIVE",
            lecture__is_active=True,
            student__deleted_at__isnull=True,
        )
        .select_related("lecture")
        .first()
    )
    if not enrollment:
        return False

    if target_type == Submission.TargetType.EXAM:
        from apps.domains.exams.models import ExamEnrollment

        in_exam = ExamEnrollment.objects.filter(
            exam_id=target_id_i,
            exam__tenant=tenant,
            enrollment_id=enrollment_id_i,
            enrollment__tenant=tenant,
        ).exists()
        if in_exam:
            return True

        if ExamEnrollment.objects.filter(
            exam_id=target_id_i,
            exam__tenant=tenant,
        ).exists():
            return False

        in_session = SessionEnrollment.objects.filter(
            tenant=tenant,
            session__exams__id=target_id_i,
            session__exams__tenant=tenant,
            enrollment_id=enrollment_id_i,
            enrollment__status="ACTIVE",
            enrollment__student__deleted_at__isnull=True,
        ).exists()
        if in_session and ensure_exam_enrollment:
            ExamEnrollment.objects.get_or_create(
                exam_id=target_id_i,
                enrollment_id=enrollment_id_i,
            )
        return in_session

    if target_type == Submission.TargetType.HOMEWORK:
        from apps.domains.homework_results.models import Homework

        return (
            Homework.objects.filter(
                id=target_id_i,
                session__lecture_id=enrollment.lecture_id,
                session__lecture__tenant=tenant,
            )
            .exclude(meta__removed_from_session_at__isnull=False)
            .filter(
                assignments__tenant=tenant,
                assignments__enrollment_id=enrollment_id_i,
            )
            .exists()
        )

    return False


def homework_submission_is_teacher_reviewed(
    *,
    tenant,
    enrollment_id: int,
    homework_id: int,
) -> bool:
    """Return whether the current passing/completed result locks media changes."""
    from apps.domains.homework_results.models import HomeworkScore

    latest_score = (
        HomeworkScore.objects.filter(
            enrollment_id=enrollment_id,
            enrollment__tenant=tenant,
            homework_id=homework_id,
            homework__tenant=tenant,
        )
        .only("passed", "attempt_index", "updated_at")
        .order_by("-attempt_index", "-updated_at", "-id")
        .first()
    )
    if latest_score is not None:
        return bool(latest_score.passed)

    from apps.domains.progress.models import AssessmentCorrection

    return AssessmentCorrection.objects.filter(
        tenant=tenant,
        enrollment_id=enrollment_id,
        source_type=AssessmentCorrection.SourceType.HOMEWORK,
        source_id=homework_id,
        completed=True,
    ).exists()


def homework_submission_review_map(
    *,
    tenant,
    enrollment_ids: set[int],
    homework_id: int,
) -> dict[int, dict[str, Any]]:
    """Return the teacher-owned review projection for homework submissions."""
    if not enrollment_ids:
        return {}

    normalized_ids = {int(enrollment_id) for enrollment_id in enrollment_ids}
    from apps.domains.homework_results.models import HomeworkScore
    from apps.domains.progress.models import AssessmentCorrection

    correction_map = {
        int(correction.enrollment_id): correction
        for correction in AssessmentCorrection.objects.filter(
            tenant=tenant,
            enrollment_id__in=normalized_ids,
            source_type=AssessmentCorrection.SourceType.HOMEWORK,
            source_id=int(homework_id),
        )
    }
    score_map = {}
    for score in (
        HomeworkScore.objects.filter(
            enrollment_id__in=normalized_ids,
            enrollment__tenant=tenant,
            homework_id=int(homework_id),
            homework__tenant=tenant,
        )
        .only("enrollment_id", "updated_at", "attempt_index", "passed")
        .order_by("enrollment_id", "-attempt_index", "-updated_at", "-id")
    ):
        score_map.setdefault(int(score.enrollment_id), score)

    review_map: dict[int, dict[str, Any]] = {}
    for enrollment_id in normalized_ids:
        score = score_map.get(enrollment_id)
        correction = correction_map.get(enrollment_id)
        manual_completed = bool(correction and correction.completed)
        score_passed = bool(score and score.passed)
        manual_completed_without_score = manual_completed and score is None
        teacher_reviewed = score_passed or manual_completed_without_score
        review_source = (
            "score"
            if score_passed
            else "manual"
            if manual_completed_without_score
            else None
        )
        reviewed_at = (
            score.updated_at
            if score_passed
            else correction.completed_at
            if manual_completed_without_score
            else None
        )
        review_map[enrollment_id] = {
            "teacher_reviewed": teacher_reviewed,
            "teacher_review_source": review_source,
            "teacher_review_note": (
                correction.note if manual_completed_without_score else ""
            ),
            "teacher_reviewed_at": reviewed_at.isoformat() if reviewed_at else None,
            "teacher_review_updated_at": (
                correction.updated_at.isoformat() if correction else None
            ),
        }
    return review_map


def validate_exam_enrollment_candidate(
    *,
    tenant,
    exam_id: int,
    enrollment_id: int,
) -> ExamEnrollmentCandidate:
    from apps.domains.enrollment.models import Enrollment, SessionEnrollment
    from apps.domains.exams.models import ExamEnrollment

    if not Enrollment.objects.filter(id=enrollment_id, tenant=tenant).exists():
        return ExamEnrollmentCandidate(
            ok=False,
            detail=f"enrollment_id={enrollment_id}는 현재 학원의 학생이 아닙니다.",
        )

    if not exam_id:
        return ExamEnrollmentCandidate(ok=True)

    in_exam = ExamEnrollment.objects.filter(
        exam_id=exam_id,
        enrollment_id=enrollment_id,
    ).exists()
    if in_exam:
        return ExamEnrollmentCandidate(ok=True)

    if ExamEnrollment.objects.filter(
        exam_id=exam_id,
        exam__tenant=tenant,
    ).exists():
        return ExamEnrollmentCandidate(
            ok=False,
            detail="해당 시험의 대상 학생이 아닙니다.",
        )

    in_session = SessionEnrollment.objects.filter(
        tenant=tenant,
        session__exams__id=exam_id,
        enrollment_id=enrollment_id,
        enrollment__status="ACTIVE",
        enrollment__student__deleted_at__isnull=True,
    ).exists()
    if not in_session:
        return ExamEnrollmentCandidate(
            ok=False,
            detail="해당 시험에 등록되지 않은 학생입니다.",
        )
    return ExamEnrollmentCandidate(ok=True, should_create=True)


def create_exam_enrollment_assignment(*, exam_id: int, enrollment_id: int) -> bool:
    from apps.domains.exams.models import ExamEnrollment

    _, created = ExamEnrollment.objects.get_or_create(
        exam_id=int(exam_id),
        enrollment_id=int(enrollment_id),
    )
    return bool(created)


def exam_question_number_by_id(*, tenant, question_ids: list[int]) -> dict[int, int]:
    if not question_ids:
        return {}

    from apps.domains.exams.models import ExamQuestion

    return {
        int(qid): int(number)
        for qid, number in ExamQuestion.objects.filter(
            id__in=question_ids,
            sheet__exam__tenant=tenant,
        ).values_list("id", "number")
    }


def allowed_manual_exam_question_ids(*, tenant, exam_id: int) -> set[int] | None:
    from apps.domains.exams.models import Exam, ExamQuestion

    exam = (
        Exam.objects.filter(id=int(exam_id or 0), tenant=tenant)
        .select_related("template_exam")
        .first()
    )
    if not exam:
        return None

    sheet_exam_ids = [exam.id]
    if exam.template_exam_id:
        sheet_exam_ids.append(exam.template_exam_id)

    return set(
        ExamQuestion.objects.filter(
            sheet__exam_id__in=sheet_exam_ids,
            sheet__exam__tenant=tenant,
        ).values_list("id", flat=True)
    )


def question_id_map_for_exam(*, exam_id: int) -> ExamQuestionIdMap | None:
    from apps.domains.exams.models import Exam, ExamQuestion, Sheet
    from apps.domains.exams.services.template_resolver import resolve_template_exam

    exam = Exam.objects.filter(id=int(exam_id)).first()
    if not exam:
        return None

    template_exam = resolve_template_exam(exam)
    sheet = Sheet.objects.filter(exam=template_exam).first()
    if not sheet:
        return None

    questions = list(ExamQuestion.objects.filter(sheet=sheet).only("id", "number"))
    if not questions:
        return None

    return ExamQuestionIdMap(
        question_number_to_pk={int(question.number): int(question.id) for question in questions},
    )


def latest_ai_job_for_submission(*, submission_id: int) -> Any | None:
    from apps.domains.ai.models import AIJobModel

    return (
        AIJobModel.objects
        .filter(source_domain="submissions", source_id=str(submission_id))
        .order_by("-created_at")
        .first()
    )


def ai_result_payload_for_job(ai_job: Any) -> dict:
    from apps.domains.ai.models import AIResultModel

    ai_result = AIResultModel.objects.filter(job=ai_job).first()
    payload = ai_result.payload if ai_result else {}
    return payload if isinstance(payload, dict) else {}


def dispatch_ai_result_to_submissions_domain(
    *,
    job_id: str,
    status: str,
    result_payload: dict,
    error: str | None,
    source_id: str,
    tier: str,
) -> None:
    from apps.domains.ai.callbacks import dispatch_ai_result_to_domain

    dispatch_ai_result_to_domain(
        job_id=job_id,
        status=status,
        result_payload=result_payload,
        error=error,
        source_domain="submissions",
        source_id=source_id,
        tier=tier,
    )


def get_synced_exam_score(
    *,
    tenant,
    target_id: int,
    enrollment_id: int,
) -> tuple[int | None, float | None, float | None]:
    try:
        from apps.domains.results.models import Result

        result = (
            Result.objects.filter(
                target_type="exam",
                target_id=int(target_id),
                enrollment_id=int(enrollment_id),
                enrollment__tenant=tenant,
            )
            .only("id", "total_score", "max_score")
            .order_by("-id")
            .first()
        )
        if result:
            return (
                int(result.id),
                float(result.total_score or 0.0),
                float(result.max_score or 0.0),
            )
    except Exception:
        return None, None, None
    return None, None, None


def finalize_omr_result_projection(*, result_id: int):
    """Finalize an OMR result through the results-domain service boundary."""

    from apps.domains.results.services.omr_subjective_completion import (
        finalize_omr_result_if_ready,
    )

    return finalize_omr_result_if_ready(result_id=int(result_id))
