from __future__ import annotations

from rest_framework.exceptions import ValidationError

from apps.support.results.grading_dependencies import (
    exam_enrollment_exists,
    exam_has_explicit_targets,
    linked_session_enrollment_exists,
    materialize_exam_enrollment_from_linked_session,
)


def validate_exam_enrollment_readable(exam, enrollment_id: int) -> None:
    """Validate score-detail access without materializing assignment rows."""
    if exam.exam_type == exam.ExamType.TEMPLATE:
        raise ValidationError({"detail": "템플릿 시험의 성적은 조회할 수 없습니다."})
    if exam_enrollment_exists(exam_id=exam.id, enrollment_id=enrollment_id):
        return
    if exam_has_explicit_targets(exam_id=exam.id):
        raise ValidationError(
            {"enrollment_id": "이 시험의 대상 학생이 아닙니다."}
        )
    if linked_session_enrollment_exists(exam=exam, enrollment_id=enrollment_id):
        return
    raise ValidationError(
        {"enrollment_id": "이 시험이 연결된 차시의 수강생만 조회할 수 있습니다."}
    )


def validate_exam_enrollment_assigned(exam, enrollment_id: int) -> None:
    """
    Manual score writes may create Result/Attempt rows, so they must be limited
    to students assigned to the exam or to the session roster of the linked exam.

    OMR 운영 SSOT: 차시에 붙은 학생은 OMR 채점 대상이다. Explicit
    ExamEnrollment가 아직 없으면, 같은 tenant의 linked SessionEnrollment를
    확인한 뒤 ExamEnrollment를 materialize한다.
    """
    if exam.exam_type == exam.ExamType.TEMPLATE:
        raise ValidationError({"detail": "템플릿 시험에는 점수를 입력할 수 없습니다."})

    from apps.support.attendance.learning_todo_eligibility import (
        exam_is_learning_todo_eligible,
    )

    if not exam_is_learning_todo_eligible(
        tenant_id=int(exam.tenant_id),
        enrollment_id=int(enrollment_id),
        exam_id=int(exam.id),
    ):
        raise ValidationError(
            {"enrollment_id": "실제 결석 차시의 학생에게는 점수를 입력할 수 없습니다."}
        )

    if exam_enrollment_exists(
        exam_id=exam.id,
        enrollment_id=enrollment_id,
    ):
        return

    if materialize_exam_enrollment_from_linked_session(
        exam=exam,
        enrollment_id=enrollment_id,
    ):
        return

    raise ValidationError(
        {"enrollment_id": "이 시험이 연결된 차시의 수강생만 점수를 입력할 수 있습니다."}
    )
