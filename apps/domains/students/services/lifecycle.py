from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.utils import timezone

from apps.core.models import Tenant, TenantMembership
from apps.domains.students.models import Student
from apps.domains.students.services.school import is_valid_grade, normalize_school_from_name
from apps.support.students.lifecycle_dependencies import (
    active_wrong_note_pdf_exists_for_students,
    cancel_active_participants_for_student,
    deactivate_enrollments_for_student,
    delete_submission_storage_for_permanent_delete,
    ensure_parent_for_student,
    restore_enrollments_after_student_restore,
    submission_storage_cleanup_status_counts,
)


PERMANENT_DELETE_STUDENT_RELATIONS = frozenset({
    ("students_studenttag", "student_id"),
    ("student_support_session", "student_id"),
    ("students_studentregistrationrequest", "student_id"),
    ("enrollment_enrollment", "student_id"),
    ("community_postentity", "created_by_id"),
    ("community_postreply", "created_by_id"),
    ("results_student_reported_score", "student_id"),
    ("clinic_sessionparticipant", "student_id"),
    ("clinic_submission", "student_id"),
    ("student_fee", "student_id"),
    ("student_invoice", "student_id"),
    ("fee_payment", "student_id"),
    ("video_inactivevideoentitlement", "student_id"),
    ("video_directvideoentitlement", "student_id"),
    ("video_videolike", "student_id"),
    ("video_videocomment", "author_student_id"),
})

PERMANENT_DELETE_SUBMISSION_RELATIONS = frozenset({
    ("submissions_submissionmedia", "submission_id"),
    ("submissions_submissionanswer", "submission_id"),
    ("submissions_omr_recognition_run", "submission_id"),
    ("submissions_omr_detected_answer", "submission_id"),
    ("submissions_omr_student_match", "submission_id"),
    ("submissions_omruploadbatchitem", "submission_id"),
    ("submissions_omruploadbatchitem", "duplicate_of_submission_id"),
    ("results_exam_result", "submission_id"),
})

PERMANENT_DELETE_ENROLLMENT_TENANT_PATHS = {
    "attendance.Attendance": (("tenant_id", False),),
    "clinic.SessionParticipant": (("tenant_id", False),),
    "enrollment.SessionEnrollment": (("tenant_id", False),),
    "exams.ExamEnrollment": (("exam__tenant_id", False),),
    "fees.StudentFee": (("tenant_id", False),),
    "homework.HomeworkAssignment": (("tenant_id", False),),
    "homework.HomeworkEnrollment": (("tenant_id", False),),
    "homework_results.HomeworkScore": (("homework__tenant_id", False),),
    "lectures.SectionAssignment": (("tenant_id", False),),
    "progress.AssessmentCorrection": (("tenant_id", False),),
    "progress.ClinicLink": (("tenant_id", False),),
    "progress.LectureProgress": (("lecture__tenant_id", False),),
    "progress.RiskLog": (("session__lecture__tenant_id", True),),
    "progress.SessionProgress": (("session__lecture__tenant_id", False),),
    "results.ExamAttempt": (("exam__tenant_id", False),),
    "results.Result": (("attempt__exam__tenant_id", True),),
    "results.ResultFact": (("attempt__exam__tenant_id", True),),
    "results.WrongNotePDF": (
        ("lecture__tenant_id", True),
        ("exam__tenant_id", True),
    ),
    "submissions.OMRStudentMatch": (("tenant_id", False),),
    "submissions.Submission": (("tenant_id", False),),
    "video.InactiveVideoEntitlement": (("tenant_id", False),),
    "video.VideoAccess": (("video__tenant_id", False),),
    "video.VideoPlaybackEvent": (("video__tenant_id", False),),
    "video.VideoPlaybackSession": (("video__tenant_id", False),),
    "video.VideoProgress": (("video__tenant_id", False),),
}


class StudentLifecycleError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class StudentSoftDeleteResult:
    student: Student
    enrollment_count: int
    clinic_participant_count: int
    user_deactivated: bool


@dataclass(frozen=True)
class StudentRestoreResult:
    student: Student
    restored_ps_number: str | None
    changed_fields: tuple[str, ...]
    user_reactivated: bool
    parent_relinked: bool
    enrollment_count: int
    active_enrollment_count: int
    pending_enrollment_count: int
    inactive_enrollment_count: int


@dataclass(frozen=True)
class StudentPermanentDeleteResult:
    deleted_count: int
    student_ids: tuple[int, ...]
    user_ids: tuple[int, ...]
    storage_cleanup_pending_count: int = 0
    storage_cleanup_failed_count: int = 0


def _append_unique(fields: list[str], field: str) -> None:
    if field not in fields:
        fields.append(field)


def _normalize_digits(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _valid_student_phone(value: Any) -> str | None:
    phone = _normalize_digits(value)
    if len(phone) == 11 and phone.startswith("010"):
        return phone
    return None


def _valid_parent_phone(value: Any) -> str | None:
    phone = _normalize_digits(value)
    if len(phone) >= 11 and phone.startswith("010"):
        return phone[:20]
    return None


def _grade_value(value: Any, school_type: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        grade = int(value)
    except (TypeError, ValueError):
        return None
    return grade if is_valid_grade(school_type or "HIGH", grade) else None


def _deleted_ps_original(ps_number: str | None) -> str | None:
    if not ps_number or not ps_number.startswith("_del_"):
        return None
    parts = ps_number.split("_", 3)
    if len(parts) < 4:
        return None
    return parts[3] or None


def _apply_restore_profile(student: Student, profile_data: dict[str, Any] | None) -> list[str]:
    if not profile_data:
        return []

    changed: list[str] = []

    name = str(profile_data.get("name") or "").strip()
    if name and student.name != name:
        student.name = name
        changed.append("name")

    if "phone" in profile_data or "studentPhone" in profile_data:
        phone = _valid_student_phone(profile_data.get("phone") or profile_data.get("studentPhone"))
        if phone and student.phone != phone:
            student.phone = phone
            changed.append("phone")

    if "parent_phone" in profile_data or "parentPhone" in profile_data:
        parent_phone = _valid_parent_phone(
            profile_data.get("parent_phone") or profile_data.get("parentPhone")
        )
        if parent_phone and student.parent_phone != parent_phone:
            student.parent_phone = parent_phone
            changed.append("parent_phone")

    has_school_data = "school" in profile_data or "school_type" in profile_data
    school_val = str(profile_data.get("school") or "").strip() or None
    if has_school_data:
        st, elementary_school, high_school, middle_school = normalize_school_from_name(
            school_val,
            profile_data.get("school_type"),
        )
        school_updates = {
            "school_type": st,
            "elementary_school": elementary_school,
            "high_school": high_school,
            "middle_school": middle_school,
            "high_school_class": (
                str(profile_data.get("high_school_class") or "").strip() or None
                if st == "HIGH"
                else None
            ),
            "major": (
                str(profile_data.get("major") or "").strip() or None
                if st == "HIGH"
                else None
            ),
        }
        for field, value in school_updates.items():
            if getattr(student, field) != value:
                setattr(student, field, value)
                changed.append(field)

    grade_school_type = student.school_type or "HIGH"
    grade = _grade_value(profile_data.get("grade"), grade_school_type)
    if grade is not None and student.grade != grade:
        student.grade = grade
        changed.append("grade")

    if "memo" in profile_data:
        memo = str(profile_data.get("memo") or "").strip() or None
        if student.memo != memo:
            student.memo = memo
            changed.append("memo")

    if "custom_fields" in profile_data:
        incoming = profile_data.get("custom_fields")
        if isinstance(incoming, dict):
            merged = dict(student.custom_fields or {})
            for key, value in incoming.items():
                if value is None:
                    merged.pop(str(key), None)
                else:
                    merged[str(key)] = value
            if student.custom_fields != merged:
                student.custom_fields = merged
                changed.append("custom_fields")

    if "gender" in profile_data:
        gender = str(profile_data.get("gender") or "").strip().upper()[:1] or None
        gender = gender if gender in ("M", "F") else None
        if student.gender != gender:
            student.gender = gender
            changed.append("gender")

    if "uses_identifier" in profile_data:
        uses_identifier = bool(profile_data.get("uses_identifier"))
        if student.uses_identifier != uses_identifier:
            student.uses_identifier = uses_identifier
            changed.append("uses_identifier")

    return changed


def soft_delete_student(
    student: Student,
    *,
    tenant,
    deleted_at=None,
) -> StudentSoftDeleteResult:
    with transaction.atomic():
        if not tenant or student.tenant_id != tenant.id:
            raise StudentLifecycleError("tenant_mismatch", "학생 테넌트가 일치하지 않습니다.")

        original_username = None
        locked_user = None
        if student.user_id:
            locked_user = get_user_model().objects.select_for_update().get(pk=student.user_id)
            original_username = locked_user.username
        student = Student.objects.select_for_update().select_related("user").get(pk=student.pk)
        if locked_user is not None:
            if student.user_id != locked_user.id:
                raise StudentLifecycleError("user_changed", "학생 계정 연결이 변경되었습니다.")
            student.user = locked_user
        if student.deleted_at:
            raise StudentLifecycleError("already_deleted", "이미 삭제된 학생입니다.")

        deleted_at = deleted_at or timezone.now()
        student.deleted_at = deleted_at
        update_fields = ["deleted_at"]

        if student.ps_number and not student.ps_number.startswith("_del_"):
            student.ps_number = f"_del_{student.id}_{student.ps_number}"
            update_fields.append("ps_number")
        if student.parent_id is not None:
            student.parent_id = None
            update_fields.append("parent")
        student.save(update_fields=update_fields)

        user_deactivated = False
        if student.user:
            cleanup_user_ids = _tenant_account_cleanup_user_ids(
                tenant=tenant,
                user_ids=[student.user_id],
                exclude_student_ids=[student.id],
            )
            if student.user_id in cleanup_user_ids:
                # Single-tenant deleted identities are mangled so a replacement
                # student can reuse the identifier. Multi-tenant identities must
                # remain stable for their surviving memberships.
                has_other_membership = TenantMembership.objects.filter(
                    user=student.user,
                    is_active=True,
                ).exclude(tenant=tenant).exists()
                if has_other_membership and student.user.username != original_username:
                    student.user.username = original_username
                    student.user.save(update_fields=["username"])
                from apps.core.services.tenant_access import deactivate_tenant_membership
                reconciliation = deactivate_tenant_membership(
                    user=student.user,
                    tenant=tenant,
                    allowed_roles=("student",),
                )
                if reconciliation and reconciliation.user_deactivated:
                    user_update = []
                    if student.user.phone:
                        student.user.phone = None
                        user_update.append("phone")
                    if user_update:
                        student.user.save(update_fields=user_update)
                    user_deactivated = True

        enrollment_count = deactivate_enrollments_for_student(tenant=tenant, student=student)
        clinic_participant_count = cancel_active_participants_for_student(
            tenant=tenant,
            student=student,
            changed_at=deleted_at,
        )

        return StudentSoftDeleteResult(
            student=student,
            enrollment_count=enrollment_count,
            clinic_participant_count=clinic_participant_count,
            user_deactivated=user_deactivated,
        )


def restore_student(
    student: Student,
    *,
    tenant,
    profile_data: dict[str, Any] | None = None,
) -> StudentRestoreResult:
    with transaction.atomic():
        if not tenant or student.tenant_id != tenant.id:
            raise StudentLifecycleError("tenant_mismatch", "학생 테넌트가 일치하지 않습니다.")
        locked_user = None
        if student.user_id:
            locked_user = get_user_model().objects.select_for_update().get(pk=student.user_id)
        student = Student.objects.select_for_update().select_related("user").get(pk=student.pk)
        if locked_user is not None:
            if student.user_id != locked_user.id:
                raise StudentLifecycleError("user_changed", "학생 계정 연결이 변경되었습니다.")
            student.user = locked_user
        if not student.deleted_at:
            raise StudentLifecycleError("not_deleted", "삭제된 학생이 아닙니다.")

        changed = _apply_restore_profile(student, profile_data)

        restored_ps_number = _deleted_ps_original(student.ps_number)
        if restored_ps_number:
            if Student.objects.filter(
                tenant=tenant,
                ps_number=restored_ps_number,
                deleted_at__isnull=True,
            ).exclude(pk=student.pk).exists():
                raise StudentLifecycleError(
                    "ps_number_conflict",
                    f"아이디 '{restored_ps_number}'를 이미 사용 중인 활성 학생이 있습니다.",
                )
            student.ps_number = restored_ps_number
            _append_unique(changed, "ps_number")

        student.deleted_at = None
        _append_unique(changed, "deleted_at")
        student.save(update_fields=changed)

        user_reactivated = False
        if student.user:
            user_update = []
            user_reactivated = not student.user.is_active
            if not student.user.phone and student.phone:
                student.user.phone = student.phone
                user_update.append("phone")
            if user_update:
                student.user.save(update_fields=user_update)
            TenantMembership.ensure_active(tenant=tenant, user=student.user, role="student")
            from apps.core.services.tenant_access import reconcile_user_tenant_access
            reconcile_user_tenant_access(student.user)

        parent_relinked = False
        if student.parent_phone:
            parent = ensure_parent_for_student(
                tenant=tenant,
                parent_phone=student.parent_phone,
                student_name=student.name,
            )
            if parent and student.parent_id != parent.id:
                student.parent = parent
                student.save(update_fields=["parent"])
                parent_relinked = True

        enrollment_restore = restore_enrollments_after_student_restore(
            tenant=tenant,
            student=student,
        )

        return StudentRestoreResult(
            student=student,
            restored_ps_number=restored_ps_number,
            changed_fields=tuple(changed),
            user_reactivated=user_reactivated,
            parent_relinked=parent_relinked,
            enrollment_count=enrollment_restore.processed_count,
            active_enrollment_count=enrollment_restore.active_count,
            pending_enrollment_count=enrollment_restore.pending_count,
            inactive_enrollment_count=enrollment_restore.inactive_count,
        )


def permanently_delete_students(
    *,
    tenant,
    student_ids: Iterable[int],
) -> StudentPermanentDeleteResult:
    ids = []
    for value in student_ids or []:
        try:
            sid = int(value)
        except (TypeError, ValueError):
            continue
        if sid > 0 and sid not in ids:
            ids.append(sid)

    if not tenant:
        raise StudentLifecycleError("tenant_required", "tenant가 필요합니다.")
    if not ids:
        return StudentPermanentDeleteResult(0, tuple(), tuple())

    with transaction.atomic():
        Tenant.objects.select_for_update().get(pk=tenant.pk)
        candidate_user_ids = tuple(
            Student.objects.filter(
                tenant=tenant,
                id__in=ids,
                deleted_at__isnull=False,
                user__isnull=False,
            ).values_list("user_id", flat=True)
        )
        if candidate_user_ids:
            list(
                get_user_model().objects.select_for_update()
                .filter(id__in=candidate_user_ids)
                .order_by("id")
            )
        to_delete = list(
            Student.objects.select_for_update().filter(
                tenant=tenant,
                id__in=ids,
                deleted_at__isnull=False,
            ).select_related("user")
        )
        if not to_delete:
            return StudentPermanentDeleteResult(0, tuple(), tuple())

        selected_student_ids = tuple(s.id for s in to_delete)
        selected_user_ids = tuple(s.user_id for s in to_delete if s.user_id)
        if active_wrong_note_pdf_exists_for_students(
            tenant=tenant,
            student_ids=selected_student_ids,
        ):
            raise StudentLifecycleError(
                "wrong_note_pdf_running",
                "오답노트 PDF 생성이 끝난 뒤 학생을 영구 삭제해 주세요.",
            )

        cleanup_intent_ids = _permanently_delete_selected_students(
            tenant=tenant,
            student_ids=selected_student_ids,
            user_ids=selected_user_ids,
        )

    cleanup_pending, cleanup_failed = submission_storage_cleanup_status_counts(
        intent_ids=cleanup_intent_ids,
    )
    return StudentPermanentDeleteResult(
        deleted_count=len(selected_student_ids),
        student_ids=selected_student_ids,
        user_ids=selected_user_ids,
        storage_cleanup_pending_count=cleanup_pending,
        storage_cleanup_failed_count=cleanup_failed,
    )


def _deletable_orphan_user_ids(user_ids: Iterable[int]) -> tuple[int, ...]:
    ids = tuple(dict.fromkeys(int(uid) for uid in user_ids or [] if uid))
    if not ids:
        return tuple()

    blocked = set(Student.objects.filter(user_id__in=ids).values_list("user_id", flat=True))
    for app_label, model_name in [
        ("parents", "Parent"),
        ("staffs", "Staff"),
    ]:
        model = apps.get_model(app_label, model_name)
        blocked.update(model.objects.filter(user_id__in=ids).values_list("user_id", flat=True))

    return tuple(uid for uid in ids if uid not in blocked)


def _tenant_account_cleanup_user_ids(
    *,
    tenant,
    user_ids: Iterable[int],
    exclude_student_ids: Iterable[int] = (),
) -> tuple[int, ...]:
    ids = tuple(dict.fromkeys(int(uid) for uid in user_ids or [] if uid))
    if not ids:
        return tuple()

    excluded_students = tuple(dict.fromkeys(int(sid) for sid in exclude_student_ids or [] if sid))
    student_qs = Student.objects.filter(tenant=tenant, user_id__in=ids)
    if excluded_students:
        student_qs = student_qs.exclude(id__in=excluded_students)
    blocked = set(
        student_qs.values_list("user_id", flat=True)
    )
    for app_label, model_name in [
        ("parents", "Parent"),
        ("staffs", "Staff"),
    ]:
        model = apps.get_model(app_label, model_name)
        blocked.update(
            model.objects.filter(tenant=tenant, user_id__in=ids).values_list("user_id", flat=True)
        )
    blocked.update(
        TenantMembership.objects.filter(
            tenant=tenant,
            user_id__in=ids,
            is_active=True,
        ).exclude(role="student").values_list("user_id", flat=True)
    )

    return tuple(uid for uid in ids if uid not in blocked)


def _reactivate_preserved_users_with_active_membership(user_ids: Iterable[int]) -> None:
    ids = tuple(dict.fromkeys(int(uid) for uid in user_ids or [] if uid))
    if not ids:
        return

    users = (
        get_user_model().objects.filter(
            id__in=ids,
            is_active=False,
            tenant_memberships__is_active=True,
        )
        .distinct()
        .only("id")
    )
    from apps.core.services.tenant_access import reconcile_user_tenant_access
    for user in users:
        reconcile_user_tenant_access(user)


def _permanently_delete_selected_students(
    *,
    tenant,
    student_ids: tuple[int, ...],
    user_ids: tuple[int, ...],
) -> tuple[int, ...]:
    _SAFE_TABLES = frozenset({
        "results_result_item", "results_result", "results_exam_attempt",
        "results_fact", "results_wrong_note_pdf", "results_exam_result",
        "exams_exam",
        "submissions_omr_detected_answer", "submissions_omr_student_match",
        "submissions_omr_recognition_run", "submissions_submissionanswer",
        "submissions_submissionmedia", "submissions_omruploadbatchitem",
        "submissions_submission",
        "results_student_reported_score", "student_support_session",
        "homework_results_homeworkscore", "homework_assignment", "homework_enrollment",
        "lectures_sectionassignment",
        "student_fee", "student_invoice", "student_invoice_item", "fee_payment",
        "attendance_attendance", "enrollment_sessionenrollment",
        "exams_exam_enrollment", "video_videopermission", "video_videoprogress",
        "video_inactivevideoentitlement", "video_directvideoentitlement",
        "video_videoplaybacksession", "video_videoplaybackevent",
        "progress_sessionprogress", "progress_lectureprogress",
        "progress_cliniclink", "progress_risklog",
        "enrollment_enrollment", "students_studenttag",
        "students_studentregistrationrequest",
        "clinic_sessionparticipant", "clinic_submission",
        "video_videocomment", "video_videolike",
        "community_postentity", "community_postreply",
        "students_student", "accounts_user", "core_tenantmembership",
        "core_pending_password_reset",
        "token_blacklist_blacklistedtoken", "token_blacklist_outstandingtoken",
    })
    _SAFE_COLS = frozenset({
        "enrollment_id", "student_id", "author_student_id",
        "created_by_id", "user_id", "submission_id", "duplicate_of_submission_id",
    })

    def _safe_tbl(name: str) -> str:
        if name not in _SAFE_TABLES:
            raise StudentLifecycleError("unsafe_delete_table", f"Unexpected table: {name}")
        return name

    def _safe_col(name: str) -> str:
        if name not in _SAFE_COLS:
            raise StudentLifecycleError("unsafe_delete_column", f"Unexpected column: {name}")
        return name

    def _in_clause(values: Iterable[int]) -> tuple[str, list[int]]:
        values = list(values or [])
        if not values:
            return "(NULL)", []
        return "(" + ", ".join(["%s"] * len(values)) + ")", values

    orphan_user_ids: list[int] = []
    with connection.cursor() as cursor:
        table_names_cache = None

        def _table_exists(tbl: str) -> bool:
            nonlocal table_names_cache
            if table_names_cache is None:
                table_names_cache = set(connection.introspection.table_names(cursor))
            return tbl in table_names_cache

        def _assert_no_cross_tenant_student_refs(
            student_id_clause: str,
            student_id_params: list[int],
        ) -> None:
            for tbl, col in [
                ("enrollment_enrollment", "student_id"),
                ("students_studentregistrationrequest", "student_id"),
                ("clinic_sessionparticipant", "student_id"),
                ("clinic_submission", "student_id"),
                ("student_fee", "student_id"),
                ("student_invoice", "student_id"),
                ("fee_payment", "student_id"),
                ("video_videolike", "student_id"),
                ("video_videocomment", "author_student_id"),
                ("community_postentity", "created_by_id"),
                ("community_postreply", "created_by_id"),
                ("results_student_reported_score", "student_id"),
                ("student_support_session", "student_id"),
                ("video_inactivevideoentitlement", "student_id"),
                ("video_directvideoentitlement", "student_id"),
            ]:
                if not _table_exists(_safe_tbl(tbl)):
                    continue
                cursor.execute(
                    f"SELECT id FROM {_safe_tbl(tbl)} "
                    f"WHERE {_safe_col(col)} IN {student_id_clause} "
                    "AND (tenant_id IS NULL OR tenant_id <> %s) LIMIT 1",
                    [*student_id_params, tenant.id],
                )
                row = cursor.fetchone()
                if row:
                    raise StudentLifecycleError(
                        "cross_tenant_reference",
                        f"{tbl}.{col} has cross-tenant reference for deleted student",
                    )

        def _assert_submission_relation_tenants(
            submission_id_clause: str,
            submission_id_params: list[int],
        ) -> None:
            for tbl in [
                "submissions_submissionanswer",
                "submissions_omr_recognition_run",
                "submissions_omr_detected_answer",
                "submissions_omr_student_match",
                "submissions_submissionmedia",
            ]:
                if not _table_exists(_safe_tbl(tbl)):
                    continue
                cursor.execute(
                    f"SELECT id FROM {_safe_tbl(tbl)} "
                    f"WHERE submission_id IN {submission_id_clause} "
                    "AND (tenant_id IS NULL OR tenant_id <> %s) LIMIT 1",
                    [*submission_id_params, tenant.id],
                )
                if cursor.fetchone():
                    raise StudentLifecycleError(
                        "cross_tenant_reference",
                        f"{tbl}.submission_id has a cross-tenant reference",
                    )

            if _table_exists(_safe_tbl("results_exam_result")):
                cursor.execute(
                    "SELECT result.id FROM results_exam_result result "
                    "JOIN exams_exam exam ON exam.id = result.exam_id "
                    f"WHERE result.submission_id IN {submission_id_clause} "
                    "AND (exam.tenant_id IS NULL OR exam.tenant_id <> %s) LIMIT 1",
                    [*submission_id_params, tenant.id],
                )
                if cursor.fetchone():
                    raise StudentLifecycleError(
                        "cross_tenant_reference",
                        "results_exam_result has a cross-tenant exam reference",
                    )

            if _table_exists(_safe_tbl("submissions_omruploadbatchitem")):
                cursor.execute(
                    "SELECT item.id FROM submissions_omruploadbatchitem item "
                    "JOIN submissions_omruploadbatch batch ON batch.id = item.batch_id "
                    f"WHERE (item.submission_id IN {submission_id_clause} "
                    f"OR item.duplicate_of_submission_id IN {submission_id_clause}) "
                    "AND ((item.tenant_id IS NOT NULL AND item.tenant_id <> %s) "
                    "OR batch.tenant_id <> %s) LIMIT 1",
                    [*submission_id_params, *submission_id_params, tenant.id, tenant.id],
                )
                if cursor.fetchone():
                    raise StudentLifecycleError(
                        "cross_tenant_reference",
                        "submissions_omruploadbatchitem has a cross-tenant batch reference",
                    )

        student_id_clause, student_id_params = _in_clause(student_ids)
        user_id_clause, user_id_params = _in_clause(user_ids)
        membership_removable_user_ids = _tenant_account_cleanup_user_ids(
            tenant=tenant,
            user_ids=user_ids,
            exclude_student_ids=student_ids,
        )
        removable_user_clause, removable_user_params = _in_clause(
            membership_removable_user_ids
        )

        _assert_no_cross_tenant_student_refs(student_id_clause, student_id_params)

        cursor.execute(
            f"SELECT id FROM enrollment_enrollment WHERE student_id IN {student_id_clause} AND tenant_id = %s",
            [*student_id_params, tenant.id],
        )
        enrollment_ids = [row[0] for row in cursor.fetchall()]
        if enrollment_ids:
            enrollment_id_clause, enrollment_id_params = _in_clause(enrollment_ids)
            cursor.execute(
                f"SELECT id FROM submissions_submission "
                f"WHERE enrollment_id IN {enrollment_id_clause} "
                "AND (tenant_id IS NULL OR tenant_id <> %s) LIMIT 1",
                [*enrollment_id_params, tenant.id],
            )
            if cursor.fetchone():
                raise StudentLifecycleError(
                    "cross_tenant_reference",
                    "submissions_submission has a cross-tenant enrollment reference",
                )

            for model_label, tenant_paths in PERMANENT_DELETE_ENROLLMENT_TENANT_PATHS.items():
                related_model = apps.get_model(model_label)
                related = related_model._base_manager.filter(enrollment_id__in=enrollment_ids)
                for tenant_path, nullable in tenant_paths:
                    mismatched = related
                    if nullable:
                        mismatched = mismatched.exclude(**{f"{tenant_path}__isnull": True})
                    if mismatched.exclude(**{tenant_path: tenant.id}).exists():
                        raise StudentLifecycleError(
                            "cross_tenant_reference",
                            f"{model_label}.{tenant_path} has a cross-tenant enrollment reference",
                        )

        submission_ids: list[int] = []
        if enrollment_ids:
            enrollment_id_clause, enrollment_id_params = _in_clause(enrollment_ids)
            cursor.execute(
                f"SELECT id FROM submissions_submission "
                f"WHERE enrollment_id IN {enrollment_id_clause} AND tenant_id = %s",
                [*enrollment_id_params, tenant.id],
            )
            submission_ids.extend(row[0] for row in cursor.fetchall())
        if user_ids:
            cursor.execute(
                f"SELECT id FROM submissions_submission "
                f"WHERE user_id IN {user_id_clause} AND tenant_id = %s",
                [*user_id_params, tenant.id],
            )
            submission_ids.extend(row[0] for row in cursor.fetchall())
        submission_ids = list(dict.fromkeys(submission_ids))

        wrong_note_pdf_ids: list[int] = []
        if enrollment_ids and _table_exists(_safe_tbl("results_wrong_note_pdf")):
            enrollment_id_clause, enrollment_id_params = _in_clause(enrollment_ids)
            cursor.execute(
                f"SELECT id FROM results_wrong_note_pdf "
                f"WHERE enrollment_id IN {enrollment_id_clause}",
                enrollment_id_params,
            )
            wrong_note_pdf_ids = [row[0] for row in cursor.fetchall()]

        cleanup_intent_ids: tuple[int, ...] = tuple()
        if submission_ids:
            submission_id_clause, submission_id_params = _in_clause(submission_ids)
            _assert_submission_relation_tenants(
                submission_id_clause,
                submission_id_params,
            )
        if submission_ids or wrong_note_pdf_ids:
            try:
                cleanup_intent_ids = delete_submission_storage_for_permanent_delete(
                    tenant_id=tenant.id,
                    submission_ids=submission_ids,
                    wrong_note_pdf_ids=wrong_note_pdf_ids,
                )
            except ValueError as exc:
                raise StudentLifecycleError(
                    "storage_cleanup_scope_mismatch",
                    "삭제 대상 파일의 저장 범위를 확인할 수 없어 영구 삭제를 중단했습니다.",
                ) from exc

        if submission_ids:
            if _table_exists(_safe_tbl("submissions_omruploadbatchitem")):
                cursor.execute(
                    f"UPDATE submissions_omruploadbatchitem SET submission_id = NULL "
                    f"WHERE submission_id IN {submission_id_clause} "
                    "AND (tenant_id IS NULL OR tenant_id = %s) "
                    "AND batch_id IN (SELECT id FROM submissions_omruploadbatch WHERE tenant_id = %s)",
                    [*submission_id_params, tenant.id, tenant.id],
                )
                cursor.execute(
                    "UPDATE submissions_omruploadbatchitem "
                    "SET duplicate_of_submission_id = NULL "
                    f"WHERE duplicate_of_submission_id IN {submission_id_clause} "
                    "AND (tenant_id IS NULL OR tenant_id = %s) "
                    "AND batch_id IN (SELECT id FROM submissions_omruploadbatch WHERE tenant_id = %s)",
                    [*submission_id_params, tenant.id, tenant.id],
                )
            for tbl in [
                "submissions_submissionanswer",
                "submissions_omr_detected_answer",
                "submissions_omr_student_match",
                "submissions_omr_recognition_run",
            ]:
                if _table_exists(_safe_tbl(tbl)):
                    cursor.execute(
                        f"DELETE FROM {_safe_tbl(tbl)} "
                        f"WHERE submission_id IN {submission_id_clause} AND tenant_id = %s",
                        [*submission_id_params, tenant.id],
                    )
            if _table_exists(_safe_tbl("results_exam_result")):
                cursor.execute(
                    f"DELETE FROM results_exam_result "
                    f"WHERE submission_id IN {submission_id_clause} "
                    "AND exam_id IN (SELECT id FROM exams_exam WHERE tenant_id = %s)",
                    [*submission_id_params, tenant.id],
                )
            cursor.execute(
                f"DELETE FROM submissions_submission "
                f"WHERE id IN {submission_id_clause} AND tenant_id = %s",
                [*submission_id_params, tenant.id],
            )

        for tbl in [
            "results_student_reported_score",
            "student_support_session",
            "video_inactivevideoentitlement",
            "video_directvideoentitlement",
        ]:
            if _table_exists(_safe_tbl(tbl)):
                cursor.execute(
                    f"DELETE FROM {_safe_tbl(tbl)} "
                    f"WHERE student_id IN {student_id_clause} AND tenant_id = %s",
                    [*student_id_params, tenant.id],
                )

        if enrollment_ids:
            enrollment_id_clause, enrollment_id_params = _in_clause(enrollment_ids)
            for tbl, where_template in [
                ("lectures_sectionassignment", "enrollment_id IN {enrollment_ids}"),
                (
                    "results_result_item",
                    "result_id IN (SELECT id FROM results_result WHERE enrollment_id IN {enrollment_ids})",
                ),
                ("results_result", "enrollment_id IN {enrollment_ids}"),
                ("results_exam_attempt", "enrollment_id IN {enrollment_ids}"),
                ("results_fact", "enrollment_id IN {enrollment_ids}"),
                ("results_wrong_note_pdf", "enrollment_id IN {enrollment_ids}"),
                ("homework_results_homeworkscore", "enrollment_id IN {enrollment_ids}"),
                ("homework_assignment", "enrollment_id IN {enrollment_ids}"),
                ("homework_enrollment", "enrollment_id IN {enrollment_ids}"),
            ]:
                if _table_exists(_safe_tbl(tbl)):
                    where_sql = where_template.format(enrollment_ids=enrollment_id_clause)
                    cursor.execute(
                        f"DELETE FROM {_safe_tbl(tbl)} WHERE {where_sql}",
                        enrollment_id_params,
                    )

            for tbl in [
                "attendance_attendance",
                "enrollment_sessionenrollment",
                "exams_exam_enrollment",
                "video_videopermission",
                "video_videoprogress",
                "video_videoplaybacksession",
                "video_videoplaybackevent",
                "progress_sessionprogress",
                "progress_lectureprogress",
                "progress_cliniclink",
                "progress_risklog",
            ]:
                if _table_exists(_safe_tbl(tbl)):
                    cursor.execute(
                        f"DELETE FROM {_safe_tbl(tbl)} WHERE enrollment_id IN {enrollment_id_clause}",
                        enrollment_id_params,
                    )

        if _table_exists(_safe_tbl("student_invoice")):
            cursor.execute(
                f"SELECT id FROM student_invoice WHERE student_id IN {student_id_clause} AND tenant_id = %s",
                [*student_id_params, tenant.id],
            )
            invoice_ids = [row[0] for row in cursor.fetchall()]
            if invoice_ids:
                invoice_id_clause, invoice_id_params = _in_clause(invoice_ids)
                if _table_exists(_safe_tbl("fee_payment")):
                    cursor.execute(
                        f"DELETE FROM fee_payment WHERE invoice_id IN {invoice_id_clause} AND tenant_id = %s",
                        [*invoice_id_params, tenant.id],
                    )
                if _table_exists(_safe_tbl("student_invoice_item")):
                    cursor.execute(
                        f"DELETE FROM student_invoice_item WHERE invoice_id IN {invoice_id_clause} AND tenant_id = %s",
                        [*invoice_id_params, tenant.id],
                    )
            cursor.execute(
                f"DELETE FROM student_invoice WHERE student_id IN {student_id_clause} AND tenant_id = %s",
                [*student_id_params, tenant.id],
            )
        if _table_exists(_safe_tbl("fee_payment")):
            cursor.execute(
                f"DELETE FROM fee_payment WHERE student_id IN {student_id_clause} AND tenant_id = %s",
                [*student_id_params, tenant.id],
            )
        if _table_exists(_safe_tbl("student_fee")):
            cursor.execute(
                f"DELETE FROM student_fee WHERE student_id IN {student_id_clause} AND tenant_id = %s",
                [*student_id_params, tenant.id],
            )

        cursor.execute(
            f"DELETE FROM enrollment_enrollment WHERE student_id IN {student_id_clause} AND tenant_id = %s",
            [*student_id_params, tenant.id],
        )
        cursor.execute(
            f"DELETE FROM students_studenttag WHERE student_id IN {student_id_clause}",
            student_id_params,
        )
        if _table_exists(_safe_tbl("students_studentregistrationrequest")):
            cursor.execute(
                f"UPDATE students_studentregistrationrequest SET student_id = NULL "
                f"WHERE student_id IN {student_id_clause} AND tenant_id = %s",
                [*student_id_params, tenant.id],
            )
        for tbl in [
            "clinic_sessionparticipant",
            "clinic_submission",
            "video_videolike",
        ]:
            if _table_exists(_safe_tbl(tbl)):
                cursor.execute(
                    f"DELETE FROM {_safe_tbl(tbl)} "
                    f"WHERE student_id IN {student_id_clause} AND tenant_id = %s",
                    [*student_id_params, tenant.id],
                )
        if _table_exists(_safe_tbl("video_videocomment")):
            cursor.execute(
                f"SELECT id FROM video_videocomment "
                f"WHERE author_student_id IN {student_id_clause} AND tenant_id = %s",
                [*student_id_params, tenant.id],
            )
            comment_ids = [row[0] for row in cursor.fetchall()]
            if comment_ids:
                comment_id_clause, comment_id_params = _in_clause(comment_ids)
                cursor.execute(
                    f"DELETE FROM video_videocomment WHERE parent_id IN {comment_id_clause} AND tenant_id = %s",
                    [*comment_id_params, tenant.id],
                )
                cursor.execute(
                    f"DELETE FROM video_videocomment WHERE id IN {comment_id_clause} AND tenant_id = %s",
                    [*comment_id_params, tenant.id],
                )
        for tbl in ["community_postentity", "community_postreply"]:
            if _table_exists(_safe_tbl(tbl)):
                cursor.execute(
                    f"UPDATE {_safe_tbl(tbl)} SET created_by_id = NULL "
                    f"WHERE created_by_id IN {student_id_clause} AND tenant_id = %s",
                    [*student_id_params, tenant.id],
                )
        cursor.execute(
            f"DELETE FROM students_student WHERE id IN {student_id_clause} AND tenant_id = %s",
            [*student_id_params, tenant.id],
        )

        if not user_ids:
            return tuple(cleanup_intent_ids)

        tenant_id = tenant.id
        if membership_removable_user_ids and _table_exists(_safe_tbl("core_pending_password_reset")):
            cursor.execute(
                f"DELETE FROM core_pending_password_reset WHERE user_id IN {removable_user_clause} AND tenant_id = %s",
                [*removable_user_params, tenant_id],
            )
        if membership_removable_user_ids:
            cursor.execute(
                f"DELETE FROM core_tenantmembership WHERE user_id IN {removable_user_clause} "
                "AND tenant_id = %s AND role = 'student'",
                [*removable_user_params, tenant_id],
            )
            cursor.execute(
                f"SELECT id FROM accounts_user WHERE id IN {removable_user_clause} AND NOT EXISTS ("
                "  SELECT 1 FROM core_tenantmembership WHERE user_id = accounts_user.id"
                ")",
                removable_user_params,
            )
            orphan_user_ids = [row[0] for row in cursor.fetchall()]
        if not orphan_user_ids:
            orphan_user_ids = []

        if orphan_user_ids:
            orphan_user_clause, orphan_user_params = _in_clause(orphan_user_ids)
            if _table_exists(_safe_tbl("core_pending_password_reset")):
                cursor.execute(
                    f"DELETE FROM core_pending_password_reset WHERE user_id IN {orphan_user_clause}",
                    orphan_user_params,
                )
            if _table_exists(_safe_tbl("token_blacklist_outstandingtoken")):
                if _table_exists(_safe_tbl("token_blacklist_blacklistedtoken")):
                    cursor.execute(
                        "DELETE FROM token_blacklist_blacklistedtoken "
                        "WHERE token_id IN ("
                        f"SELECT id FROM token_blacklist_outstandingtoken WHERE user_id IN {orphan_user_clause}"
                        ")",
                        orphan_user_params,
                    )
                cursor.execute(
                    f"DELETE FROM token_blacklist_outstandingtoken WHERE user_id IN {orphan_user_clause}",
                    orphan_user_params,
                )
    deletable_user_ids = _deletable_orphan_user_ids(orphan_user_ids)
    if deletable_user_ids:
        get_user_model().objects.filter(id__in=deletable_user_ids).delete()
    _reactivate_preserved_users_with_active_membership(
        uid for uid in user_ids if uid not in set(deletable_user_ids)
    )
    return tuple(cleanup_intent_ids)
