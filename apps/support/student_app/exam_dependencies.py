"""Student-exam cross-domain dependencies.

The student app is a transport facade. Exam/submission internals stay behind
this support boundary while broader domain cutover is in progress.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.db.models import F, Q
from django.utils import timezone

logger = logging.getLogger(__name__)
MISSING_EXAM_ENROLLMENT: tuple[None, None] = (None, None)


class StudentExamSubmitError(Exception):
    def __init__(self, detail: str, status_code: int):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def _missing_exam_enrollment(reason: str) -> tuple[None, None]:
    logger.debug("student exam enrollment lookup missed: %s", reason)
    return MISSING_EXAM_ENROLLMENT


def student_exam_queryset(student, tenant, *, include_upcoming_days: int = 0):
    from django.db.models import Exists, OuterRef

    from apps.domains.attendance.models import Attendance
    from apps.domains.enrollment.selectors import active_enrollment_ids_for_student
    from apps.domains.enrollment.models import SessionEnrollment
    from apps.domains.exams.models import Exam
    from apps.domains.results.models import ExamAttempt, Result
    from apps.domains.submissions.models import Submission

    now = timezone.now()
    latest_open_at = now
    if include_upcoming_days > 0:
        latest_open_at = now + timedelta(days=include_upcoming_days)
    enrollment_ids = active_enrollment_ids_for_student(tenant=tenant, student=student)
    if not enrollment_ids:
        return Exam.objects.none()
    absent_session_ids = Attendance.objects.filter(
        tenant=tenant,
        enrollment_id__in=enrollment_ids,
        status="ABSENT",
    ).values("session_id")
    eligible_session = (
        SessionEnrollment.objects.filter(
            tenant=tenant,
            enrollment_id__in=enrollment_ids,
            enrollment__tenant=tenant,
            enrollment__status="ACTIVE",
            enrollment__student__deleted_at__isnull=True,
            enrollment__lecture_id=F("session__lecture_id"),
            session__lecture__tenant=tenant,
            session__exams__id=OuterRef("pk"),
        )
        .exclude(session_id__in=absent_session_ids)
    )
    historical_result = Result.objects.filter(
        target_type="exam",
        target_id=OuterRef("pk"),
        enrollment_id__in=enrollment_ids,
        enrollment__tenant=tenant,
    )
    historical_attempt = ExamAttempt.objects.filter(
        exam_id=OuterRef("pk"),
        enrollment_id__in=enrollment_ids,
        enrollment__tenant=tenant,
    )
    historical_submission = Submission.objects.filter(
        tenant=tenant,
        target_type=Submission.TargetType.EXAM,
        target_id=OuterRef("pk"),
        enrollment_id__in=enrollment_ids,
    )
    absent_linked_attendance = Attendance.objects.filter(
        tenant=tenant,
        enrollment_id__in=enrollment_ids,
        enrollment__tenant=tenant,
        enrollment__status="ACTIVE",
        enrollment__student__deleted_at__isnull=True,
        enrollment__lecture_id=F("session__lecture_id"),
        session__lecture__tenant=tenant,
        session__exams__id=OuterRef("pk"),
        status="ABSENT",
    )
    return (
        Exam.objects.filter(
            exam_type=Exam.ExamType.REGULAR,
            exam_enrollments__enrollment_id__in=enrollment_ids,
            exam_enrollments__enrollment__lecture__is_active=True,
            sessions__lecture_id=F("exam_enrollments__enrollment__lecture_id"),
            sessions__lecture__tenant=tenant,
            is_active=True,
        )
        .filter(
            Q(open_at__isnull=True) | Q(open_at__lte=latest_open_at),
            Q(close_at__isnull=True) | Q(close_at__gte=now),
        )
        .annotate(
            learning_todo_eligible=Exists(eligible_session),
            has_historical_result=Exists(historical_result),
            has_historical_attempt=Exists(historical_attempt),
            has_historical_submission=Exists(historical_submission),
            has_absent_linked_attendance=Exists(absent_linked_attendance),
        )
        .filter(
            Q(learning_todo_eligible=True)
            | Q(has_absent_linked_attendance=False)
            | Q(has_historical_result=True)
            | Q(has_historical_attempt=True)
            | Q(has_historical_submission=True)
        )
        .distinct()
        .order_by("open_at", "id")
    )


def submission_status_map_for_student_exams(*, tenant, student, exams) -> dict[int, dict[str, int | bool]]:
    exam_ids = [int(exam.id) for exam in exams]
    if not exam_ids:
        return {}

    from apps.domains.enrollment.selectors import active_enrollment_ids_for_student
    from apps.domains.submissions.models.submission import Submission

    enrollment_ids = active_enrollment_ids_for_student(tenant=tenant, student=student)
    if not enrollment_ids:
        return {}

    submission_status_map: dict[int, dict[str, int | bool]] = {}
    subs = Submission.objects.filter(
        enrollment_id__in=enrollment_ids,
        target_type=Submission.TargetType.EXAM,
        target_id__in=exam_ids,
    ).values_list("target_id", "status")
    for target_id, sub_status in subs:
        entry = submission_status_map.setdefault(
            int(target_id),
            {"has_result": False, "attempt_count": 0},
        )
        entry["attempt_count"] = int(entry["attempt_count"]) + 1
        if sub_status == Submission.Status.DONE:
            entry["has_result"] = True
    return submission_status_map


def student_exam_questions(exam):
    from apps.domains.exams.models import AnswerKey, ExamQuestion
    from apps.domains.exams.services.template_resolver import resolve_template_exam
    from apps.support.exams.numeric_short_answer import (
        NUMERIC_SHORT_ANSWER_FORMAT,
        math_numeric_short_answer_question_ids,
    )
    from apps.support.omr.score_shape import get_exam_score_shape

    template = resolve_template_exam(exam)
    questions = list(
        ExamQuestion.objects.filter(sheet__exam=template)
        .order_by("number")
        .values("id", "number", "score")
    )
    answer_key = AnswerKey.objects.filter(exam=template).only("answers").first()
    score_shape = get_exam_score_shape(exam)
    numeric_question_ids = math_numeric_short_answer_question_ids(
        subject=getattr(template, "subject", None),
        exam=exam,
        question_ids=(int(question["id"]) for question in questions),
        question_kind=score_shape.question_kind,
        answers=getattr(answer_key, "answers", None),
    )
    for question in questions:
        question["answer_format"] = (
            NUMERIC_SHORT_ANSWER_FORMAT
            if int(question["id"]) in numeric_question_ids
            else "text"
        )
    return questions


def normalize_student_exam_answers(*, exam, answers) -> list[dict[str, int | str]]:
    from apps.support.exams.numeric_short_answer import (
        NUMERIC_SHORT_ANSWER_FORMAT,
        normalize_numeric_short_answer,
    )

    if isinstance(answers, dict):
        raw_answers = [
            {"exam_question_id": key, "answer": value}
            for key, value in answers.items()
        ]
    elif isinstance(answers, list):
        raw_answers = answers
    else:
        raise StudentExamSubmitError("answers 필드가 필요합니다 (리스트 또는 객체).", 400)

    question_specs = student_exam_questions(exam)
    question_by_id = {int(question["id"]): question for question in question_specs}
    normalized_answers: list[dict[str, int | str]] = []
    seen_question_ids: set[int] = set()
    for raw_answer in raw_answers:
        if not isinstance(raw_answer, dict):
            raise StudentExamSubmitError("답안 형식이 올바르지 않습니다.", 400)
        try:
            question_id = int(raw_answer.get("exam_question_id"))
        except (TypeError, ValueError):
            raise StudentExamSubmitError("문항 번호가 올바르지 않습니다.", 400)
        question = question_by_id.get(question_id)
        if question is None:
            raise StudentExamSubmitError("이 시험에 없는 문항은 제출할 수 없습니다.", 400)
        if question_id in seen_question_ids:
            raise StudentExamSubmitError("같은 문항의 답안을 중복 제출할 수 없습니다.", 400)
        seen_question_ids.add(question_id)

        answer = str(raw_answer.get("answer", "")).strip()
        if question["answer_format"] == NUMERIC_SHORT_ANSWER_FORMAT:
            answer = normalize_numeric_short_answer(answer)
            if answer is None:
                raise StudentExamSubmitError(
                    f"{question['number']}번 답은 0~999 사이의 정수로 입력해 주세요.",
                    400,
                )
        normalized_answers.append({
            "exam_question_id": question_id,
            "answer": answer,
        })

    if not normalized_answers:
        raise StudentExamSubmitError("최소 1개 문항의 답을 입력하세요.", 400)
    return normalized_answers


def get_enrollment_for_student_exam(student, exam_id, tenant=None):
    from apps.domains.exams.models import ExamEnrollment

    if not student:
        return _missing_exam_enrollment("student is required")
    if not tenant:
        return _missing_exam_enrollment("tenant is required")
    if getattr(student, "tenant_id", None) != tenant.id:
        return _missing_exam_enrollment("student tenant mismatch")
    exam_enrollments = (
        ExamEnrollment.objects.filter(
            exam_id=int(exam_id),
            enrollment__student=student,
            enrollment__tenant=tenant,
            enrollment__status="ACTIVE",
            enrollment__lecture__is_active=True,
            exam__sessions__lecture_id=F("enrollment__lecture_id"),
            exam__sessions__lecture__tenant=tenant,
        )
        .select_related("enrollment", "enrollment__tenant")
        .order_by("id")
    )
    from apps.support.attendance.learning_todo_eligibility import (
        exam_is_learning_todo_eligible,
    )

    for exam_enrollment in exam_enrollments:
        if not exam_enrollment.enrollment:
            continue
        if exam_is_learning_todo_eligible(
            tenant_id=int(tenant.id),
            enrollment_id=int(exam_enrollment.enrollment_id),
            exam_id=int(exam_id),
        ):
            return (
                exam_enrollment.enrollment,
                getattr(exam_enrollment.enrollment, "tenant", None),
            )
    return _missing_exam_enrollment("eligible active exam enrollment not found")


def create_online_exam_submission(
    *,
    request_user,
    request_student,
    tenant,
    exam,
    enrollment,
    answers,
):
    from django.db import IntegrityError, transaction

    from apps.domains.exams.models import ExamEnrollment
    from apps.domains.results.models import ExamAttempt
    from apps.domains.submissions.models import Submission
    from apps.domains.submissions.services.lifecycle import (
        IN_PROGRESS_STATUSES,
        supersede_done_submissions,
    )

    try:
        with transaction.atomic():
            ExamEnrollment.objects.select_for_update().only("id").get(
                exam_id=int(exam.id),
                enrollment_id=enrollment.id,
            )
            attempt_indexes = list(
                ExamAttempt.objects.select_for_update()
                .filter(exam_id=int(exam.id), enrollment_id=enrollment.id)
                .values_list("attempt_index", flat=True)
            )
            prev_submissions = list(
                Submission.objects.select_for_update().filter(
                    enrollment_id=enrollment.id,
                    target_type=Submission.TargetType.EXAM,
                    target_id=int(exam.id),
                    status__in=[*IN_PROGRESS_STATUSES, Submission.Status.DONE],
                )
            )
            in_progress = [s for s in prev_submissions if s.status in IN_PROGRESS_STATUSES]
            done_submissions = [s for s in prev_submissions if s.status == Submission.Status.DONE]

            if in_progress:
                raise StudentExamSubmitError("이미 제출된 시험입니다.", 409)

            if done_submissions:
                allow_retake = getattr(exam, "allow_retake", False)
                max_attempts = getattr(exam, "max_attempts", 1) or 1
                attempt_count = max(attempt_indexes, default=len(done_submissions))
                if not allow_retake or attempt_count >= max_attempts:
                    raise StudentExamSubmitError("재응시가 허용되지 않는 시험입니다.", 409)
                supersede_done_submissions(
                    Submission.objects.filter(id__in=[s.id for s in done_submissions]),
                    actor="student.exam_submit.retake",
                )

            submission_user = request_student.user if request_student.user_id else request_user
            submission_meta = None
            if getattr(submission_user, "id", None) != getattr(request_user, "id", None):
                submission_meta = {"submitted_by_user_id": request_user.id}
            return Submission.objects.create(
                tenant=tenant,
                user=submission_user,
                enrollment_id=enrollment.id,
                target_type=Submission.TargetType.EXAM,
                target_id=int(exam.id),
                source=Submission.Source.ONLINE,
                payload={"answers": answers},
                meta=submission_meta,
                status=Submission.Status.SUBMITTED,
            )
    except IntegrityError as exc:
        if "unique_active_submission_per_target" in str(exc):
            raise StudentExamSubmitError("이미 제출된 시험입니다.", 409) from exc
        raise


def dispatch_student_exam_submission(submission) -> None:
    from apps.domains.submissions.services.dispatcher import dispatch_submission

    dispatch_submission(submission)
