"""Canonical enrollment write/use-case entrypoints."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError

from academy.adapters.db.django import repositories_enrollment as enroll_repo
from apps.domains.enrollment.models import Enrollment, SessionEnrollment
from apps.domains.enrollment.selectors import (
    get_active_enrollment_for_student_lecture,
    get_lecture_for_tenant_or_404,
    get_session_for_lecture_or_404,
    get_student_for_tenant_or_404,
    require_tenant,
)
from apps.support.enrollment.lifecycle_dependencies import (
    auto_assign_fees_on_enrollment,
    deactivate_fees_for_enrollment,
    ensure_session_roster_membership,
    get_exam_learning_access_models,
    get_homework_learning_access_models,
    schedule_pending_account_notice,
)


@dataclass(frozen=True)
class DisposableEnrollmentImpact:
    enrollment_id: int
    session_enrollments: int
    removable_unset_attendances: int
    protected_attendances: int
    protected_dependencies: dict[str, int]

    @property
    def can_remove(self) -> bool:
        return self.protected_attendances == 0 and not self.protected_dependencies

    def as_dict(self) -> dict:
        return {**asdict(self), "can_remove": self.can_remove}


@dataclass(frozen=True)
class StudentEnrollmentRestoreResult:
    processed_count: int
    active_count: int
    pending_count: int
    inactive_count: int


def assess_disposable_enrollment(*, tenant, enrollment) -> DisposableEnrollmentImpact:
    """Count every cascade boundary before an assistant may undo a wrong enrollment."""
    tenant = require_tenant(tenant)
    if enrollment.tenant_id != tenant.id:
        raise ValidationError({"detail": "다른 학원의 수강 등록입니다."})

    attendances = enrollment.attendances.all()
    removable_attendances = attendances.filter(
        status="UNSET",
        memo="",
        planned_arrival_date__isnull=True,
        planned_arrival_time__isnull=True,
        attended_section__isnull=True,
    ).count()
    protected_attendances = attendances.count() - removable_attendances

    protected_dependencies: dict[str, int] = {}
    allowed_accessors = {"attendances", "session_enrollments"}
    for relation in Enrollment._meta.related_objects:
        accessor = relation.get_accessor_name()
        if not accessor or accessor.endswith("+"):
            continue
        if accessor in allowed_accessors:
            continue
        try:
            related = getattr(enrollment, accessor)
        except (AttributeError, ObjectDoesNotExist):
            continue
        count = related.count() if hasattr(related, "count") else int(related is not None)
        if count:
            protected_dependencies[relation.related_model._meta.label_lower] = count

    return DisposableEnrollmentImpact(
        enrollment_id=enrollment.id,
        session_enrollments=enrollment.session_enrollments.count(),
        removable_unset_attendances=removable_attendances,
        protected_attendances=protected_attendances,
        protected_dependencies=protected_dependencies,
    )


def delete_disposable_enrollment(*, tenant, enrollment_id: int, student_id: int) -> DisposableEnrollmentImpact:
    """Delete only an exact locked enrollment whose whole impact is disposable."""
    tenant = require_tenant(tenant)
    enrollment = (
        Enrollment.objects.select_for_update()
        .filter(
            tenant=tenant,
            id=enrollment_id,
            student_id=student_id,
        )
        .first()
    )
    if enrollment is None:
        raise ValidationError({"detail": "교정할 수강 등록을 찾지 못했습니다."})
    impact = assess_disposable_enrollment(tenant=tenant, enrollment=enrollment)
    if not impact.can_remove:
        raise ValidationError(
            {
                "detail": "사용자 입력 또는 학습·결제 데이터가 있어 수강 등록을 자동으로 삭제할 수 없습니다.",
                "impact": impact.as_dict(),
            }
        )
    delete_enrollment(enrollment)
    return impact


def _validate_id_list(value, *, field_name: str, allow_empty: bool = False) -> list[int]:
    if not isinstance(value, list) or (not allow_empty and not value):
        raise ValidationError({"detail": f"{field_name}(list)는 필수입니다"})
    if len(value) > 200:
        raise ValidationError({"detail": "최대 200건까지 일괄 처리할 수 있습니다."})
    try:
        return [int(v) for v in value]
    except (TypeError, ValueError) as exc:
        raise ValidationError({"detail": f"{field_name} 값이 잘못되었습니다."}) from exc


def sync_enrollment_status_side_effects(
    enrollment: Enrollment,
    *,
    schedule_account_notice: bool = True,
) -> None:
    if enrollment.status != "ACTIVE":
        deactivate_fees_for_enrollment(enrollment)
        return
    auto_assign_fees_on_enrollment(
        enrollment.tenant,
        enrollment.student,
        enrollment.lecture,
        enrollment,
    )
    if schedule_account_notice:
        schedule_pending_account_notice(student_id=enrollment.student_id)


def delete_enrollment(enrollment: Enrollment) -> None:
    enroll_repo.session_enrollment_filter_delete(enrollment.tenant, enrollment)
    deactivate_fees_for_enrollment(enrollment)
    enrollment.delete()


def deactivate_enrollments_for_student(*, tenant, student) -> int:
    enrollments = list(
        Enrollment.objects.select_for_update()
        .filter(student=student, tenant=tenant)
        .order_by("id")
    )
    for enrollment in enrollments:
        enrollment.status_before_student_deletion = enrollment.status
        enrollment.status = "INACTIVE"
        enrollment.save(
            update_fields=[
                "status",
                "status_before_student_deletion",
                "updated_at",
            ]
        )
        deactivate_fees_for_enrollment(enrollment)
    return len(enrollments)


def restore_enrollments_after_student_restore(*, tenant, student) -> StudentEnrollmentRestoreResult:
    """Restore only enrollment status changes made by the student's soft deletion."""
    enrollments = list(
        Enrollment.objects.select_for_update()
        .select_related("lecture")
        .filter(
            student=student,
            tenant=tenant,
            status_before_student_deletion__isnull=False,
        )
        .order_by("id")
    )
    today = timezone.localdate()
    counts = {"ACTIVE": 0, "PENDING": 0, "INACTIVE": 0}

    for enrollment in enrollments:
        target_status = enrollment.status_before_student_deletion or "INACTIVE"
        if target_status not in counts:
            target_status = "INACTIVE"
        lecture = enrollment.lecture
        lecture_accepts_restore = bool(lecture.is_active) and (
            lecture.end_date is None or lecture.end_date >= today
        )
        if target_status in {"ACTIVE", "PENDING"} and not lecture_accepts_restore:
            target_status = "INACTIVE"

        enrollment.status = target_status
        enrollment.status_before_student_deletion = None
        enrollment.save(
            update_fields=[
                "status",
                "status_before_student_deletion",
                "updated_at",
            ]
        )
        sync_enrollment_status_side_effects(
            enrollment,
            schedule_account_notice=False,
        )
        counts[target_status] += 1

    return StudentEnrollmentRestoreResult(
        processed_count=len(enrollments),
        active_count=counts["ACTIVE"],
        pending_count=counts["PENDING"],
        inactive_count=counts["INACTIVE"],
    )


@transaction.atomic
def bulk_create_enrollments(*, tenant, lecture_id, student_ids) -> list[Enrollment]:
    tenant = require_tenant(tenant)
    if not lecture_id:
        raise ValidationError({"detail": "lecture, students(list)는 필수입니다"})
    student_ids = _validate_id_list(student_ids, field_name="students")

    lecture = enroll_repo.get_lecture_by_id_tenant(lecture_id, tenant)
    if not lecture:
        raise ValidationError({"detail": "해당 학원의 강의가 아닙니다."})

    locked_student_ids = set(
        enroll_repo.lock_active_student_ids_for_tenant(student_ids, tenant)
    )
    invalid_student_id = next(
        (student_id for student_id in student_ids if student_id not in locked_student_ids),
        None,
    )
    if invalid_student_id is not None:
        raise ValidationError(
            {"detail": f"학생(id={invalid_student_id})은 현재 학원 소속이 아닙니다."}
        )

    created: list[Enrollment] = []
    for sid in student_ids:
        obj, created_new = enroll_repo.enrollment_get_or_create(
            tenant=tenant,
            lecture=lecture,
            student_id=sid,
            defaults={"status": "ACTIVE"},
        )
        if not created_new and obj.status != "ACTIVE":
            obj.status = "ACTIVE"
            obj.save(update_fields=["status"])
        created.append(obj)

        student = getattr(obj, "student", None)
        if student:
            auto_assign_fees_on_enrollment(tenant, student, lecture, obj)
            schedule_pending_account_notice(student_id=student.id)

    return created


@transaction.atomic
def bulk_create_session_enrollments(*, tenant, session_id, enrollment_ids) -> list[SessionEnrollment]:
    tenant = require_tenant(tenant)
    if not session_id:
        raise ValidationError({"detail": "session, enrollments(list)는 필수입니다"})
    enrollment_ids = _validate_id_list(enrollment_ids, field_name="enrollments", allow_empty=True)

    session = enroll_repo.get_session_by_id_with_lecture(session_id)
    if session is None:
        raise ValidationError({"detail": "세션을 찾을 수 없습니다."})
    if session.lecture.tenant_id != tenant.id:
        raise ValidationError({"detail": "다른 학원의 세션입니다."})

    candidate_enrollments = enroll_repo.get_enrollments_by_ids_with_lecture(
        enrollment_ids,
        tenant,
    )
    candidate_by_id = {enrollment.id: enrollment for enrollment in candidate_enrollments}
    missing_enrollment_id = next(
        (enrollment_id for enrollment_id in enrollment_ids if enrollment_id not in candidate_by_id),
        None,
    )
    if missing_enrollment_id is not None:
        raise ValidationError(
            {"detail": f"수강 등록을 찾을 수 없습니다: {missing_enrollment_id}"}
        )

    student_ids = [enrollment.student_id for enrollment in candidate_enrollments]
    locked_student_ids = set(
        enroll_repo.lock_active_student_ids_for_tenant(student_ids, tenant)
    )
    invalid_student_id = next(
        (student_id for student_id in student_ids if student_id not in locked_student_ids),
        None,
    )
    if invalid_student_id is not None:
        raise ValidationError({"detail": "삭제된 학생은 차시 수강 명단에 추가할 수 없습니다."})

    locked_enrollments = enroll_repo.lock_enrollments_by_ids_with_lecture(
        enrollment_ids,
        tenant,
    )
    locked_by_id = {enrollment.id: enrollment for enrollment in locked_enrollments}
    if any(
        enrollment.student_id not in locked_student_ids
        for enrollment in locked_enrollments
    ):
        raise ValidationError({"detail": "수강 등록의 학생 정보가 변경되었습니다."})

    created: list[SessionEnrollment] = []
    for eid in enrollment_ids:
        enrollment = locked_by_id.get(eid)
        if enrollment is None:
            raise ValidationError({"detail": f"수강 등록을 찾을 수 없습니다: {eid}"})
        if enrollment.lecture_id != session.lecture_id:
            raise ValidationError({"detail": "다른 강의 수강자는 이 세션에 추가할 수 없습니다."})

        membership = ensure_session_roster_membership(
            tenant=tenant,
            session=session,
            enrollment=enrollment,
            allow_session_reregistration=True,
        )
        created.append(membership.session_enrollment)

    return created


def toggle_student_learning_access(
    *,
    tenant,
    student_id: int,
    lecture_id: int,
    target_type: str,
    target_id: int,
    action: str,
) -> dict:
    Exam, ExamEnrollment = get_exam_learning_access_models()
    HomeworkAssignment, Homework = get_homework_learning_access_models()

    tenant = require_tenant(tenant)
    student = get_student_for_tenant_or_404(tenant=tenant, student_id=student_id)
    lecture = get_lecture_for_tenant_or_404(tenant=tenant, lecture_id=lecture_id)
    enrollment = get_active_enrollment_for_student_lecture(
        tenant=tenant,
        student=student,
        lecture=lecture,
    )
    if not enrollment:
        raise ValidationError({"detail": "강의 등록 없음"})

    if target_type == "session":
        session = get_session_for_lecture_or_404(lecture=lecture, session_id=target_id)
        if action == "add":
            SessionEnrollment.objects.get_or_create(
                tenant=tenant,
                enrollment=enrollment,
                session=session,
            )
        else:
            SessionEnrollment.objects.filter(
                tenant=tenant,
                enrollment=enrollment,
                session=session,
            ).delete()
    elif target_type == "exam":
        exam = Exam.objects.filter(id=target_id, tenant=tenant, sessions__lecture=lecture).distinct().first()
        if exam is None:
            raise NotFound("시험을 찾을 수 없습니다")

        if action == "add":
            first_session_id = (
                exam.sessions.filter(lecture=lecture).order_by("order", "id").values_list("id", flat=True).first()
            )
            if first_session_id:
                SessionEnrollment.objects.get_or_create(
                    tenant=tenant,
                    enrollment=enrollment,
                    session_id=first_session_id,
                )
            ExamEnrollment.objects.get_or_create(exam=exam, enrollment=enrollment)
        else:
            ExamEnrollment.objects.filter(exam=exam, enrollment=enrollment).delete()
    elif target_type == "homework":
        homework = (
            Homework.objects.select_related("session", "session__lecture")
            .filter(id=target_id, tenant=tenant, session__lecture=lecture)
            .first()
        )
        if homework is None:
            raise NotFound("과제를 찾을 수 없습니다")

        if action == "add":
            SessionEnrollment.objects.get_or_create(
                tenant=tenant,
                enrollment=enrollment,
                session_id=homework.session_id,
            )
            HomeworkAssignment.objects.get_or_create(
                tenant=tenant,
                homework=homework,
                session_id=homework.session_id,
                enrollment=enrollment,
            )
        else:
            HomeworkAssignment.objects.filter(
                tenant=tenant,
                homework=homework,
                session_id=homework.session_id,
                enrollment=enrollment,
            ).delete()
    else:
        raise ValidationError({"detail": "target_type 잘못됨"})

    return {"ok": True, "target_type": target_type, "target_id": target_id, "action": action}
