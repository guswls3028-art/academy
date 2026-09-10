from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

from django.contrib.auth import get_user_model
from django.core import signing
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.support.teacher_app.ops_assistant_dependencies import (
    AccessMode,
    Attendance,
    Enrollment,
    Lecture,
    Session,
    Student,
    StudentIdentityError,
    StudentProfileUpdateError,
    Video,
    VideoAccess,
    assess_disposable_enrollment,
    bulk_create_enrollments,
    ensure_session_roster_membership,
    delete_disposable_enrollment,
    get_auto_send_config,
    get_owner_tenant_id,
    normalize_student_phone,
    resolve_access_mode,
    resolve_student_import_row,
    update_student_profile,
)
from apps.support.students.namespace_lock import (
    lock_student_creation_tenant_reference,
)


PROPOSAL_SALT = "teacher-ops-assistant-v2"
PROPOSAL_MAX_AGE_SECONDS = 30 * 60
MAX_ROWS = 5


def _normalized_search(value: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", str(value or "").lower())


def _issue(code: str, message: str, *, blocking: bool = True) -> dict:
    return {"code": code, "message": message, "blocking": blocking}


def _account_notice_template_issues(*, student_phone: str, parent_phone: str) -> list[dict]:
    digits_student = "".join(char for char in str(student_phone or "") if char.isdigit())
    digits_parent = "".join(char for char in str(parent_phone or "") if char.isdigit())
    triggers = ["registration_approved_parent"]
    if digits_student and digits_student != digits_parent:
        triggers.append("registration_approved_student")
    missing = []
    owner_id = get_owner_tenant_id()
    for trigger in triggers:
        config = get_auto_send_config(owner_id, trigger)
        template = getattr(config, "template", None)
        if (
            template is None
            or template.tenant_id != owner_id
            or template.solapi_status != "APPROVED"
            or not str(template.solapi_template_id or "").strip()
        ):
            missing.append(trigger)
    if not missing:
        return []
    return [
        _issue(
            "account_notice_template_missing", "승인된 학생·학부모 초기 안내 알림톡 템플릿이 없어 실행할 수 없습니다."
        )
    ]


def _student_school(student: Student) -> str:
    field = {
        "ELEMENTARY": "elementary_school",
        "MIDDLE": "middle_school",
        "HIGH": "high_school",
    }.get(student.school_type)
    return str(getattr(student, field, "") or "") if field else ""


def _active_lecture_queryset(*, tenant):
    today = timezone.localdate()
    return (
        Lecture.objects.filter(
            tenant=tenant,
            is_active=True,
            is_system=False,
        )
        .filter(Q(start_date__isnull=True) | Q(start_date__lte=today))
        .filter(Q(end_date__isnull=True) | Q(end_date__gte=today))
    )


def active_lecture_options(*, tenant) -> list[dict]:
    return list(_active_lecture_queryset(tenant=tenant).order_by("title", "id").values("id", "title"))


def _lecture_candidates(*, lecture_options: list[dict], hint: str, school: str, grade: str) -> list[dict]:
    needles = [_normalized_search(value) for value in (hint, school, grade) if _normalized_search(value)]
    if not needles:
        return []
    scored: list[tuple[int, dict]] = []
    for option in lecture_options:
        title = _normalized_search(option["title"])
        score = 0
        for index, needle in enumerate(needles):
            if title == needle:
                score += 10 if index == 0 else 4
            elif needle in title or title in needle:
                score += 6 if index == 0 else 2
        if score:
            scored.append((score, option))
    scored.sort(key=lambda item: (-item[0], item[1]["title"], item[1]["id"]))
    if not scored:
        return []
    best = scored[0][0]
    return [option for score, option in scored if score == best][:8]


def _normalize_phone(value: str, *, required: bool, field_name: str, label: str) -> tuple[str, list[dict]]:
    try:
        normalized = normalize_student_phone(
            value,
            required=required,
            field_name=field_name,
            field_label=label,
        )
        return str(normalized or ""), []
    except StudentIdentityError:
        return "", [_issue(f"{field_name}_invalid", f"{label}를 010 뒤 8자리로 확인해 주세요.")]


def _student_match(*, tenant, row: dict) -> tuple[dict, list[dict], list[str]]:
    issues: list[dict] = []
    profile_changes: list[str] = []
    name = str(row.get("name") or "").strip()
    if not name:
        issues.append(_issue("student_name_missing", "학생 이름을 입력해 주세요."))

    student_phone, phone_issues = _normalize_phone(
        str(row.get("student_phone") or ""), required=False, field_name="student_phone", label="학생 전화번호"
    )
    parent_phone, parent_issues = _normalize_phone(
        str(row.get("parent_phone") or ""), required=True, field_name="parent_phone", label="학부모 전화번호"
    )
    issues.extend(phone_issues)
    issues.extend(parent_issues)
    if not student_phone and not parent_phone:
        issues.append(
            _issue(
                "identity_evidence_missing", "이름만으로는 실행할 수 없습니다. 학생 또는 학부모 번호를 확인해 주세요."
            )
        )

    active = Student.objects.filter(tenant=tenant, deleted_at__isnull=True).select_related("user")
    has_phone_evidence = bool(student_phone or parent_phone)
    phone_query = Q()
    if student_phone:
        phone_query |= Q(phone=student_phone)
    if parent_phone:
        phone_query |= Q(parent_phone=parent_phone)
    phone_collisions = list(active.filter(phone_query).order_by("id")[:3]) if has_phone_evidence else []
    student_phone_conflicts = [
        candidate
        for candidate in phone_collisions
        if student_phone and candidate.phone == student_phone and candidate.name != name
    ]
    if student_phone_conflicts:
        issues.append(
            _issue("phone_conflict", "전화번호가 다른 이름의 기존 학생과 겹칩니다. 학생 목록에서 확인해 주세요.")
        )
        return {"status": "conflict", "id": None, "basis": []}, issues, profile_changes

    exact = []
    for candidate in phone_collisions:
        student_phone_match = bool(student_phone and candidate.phone == student_phone)
        parent_phone_match = bool(parent_phone and candidate.parent_phone == parent_phone)
        if candidate.name == name and (student_phone_match or parent_phone_match):
            exact.append(candidate)
    exact_ids = {candidate.id for candidate in exact}
    if len(exact_ids) > 1:
        issues.append(
            _issue("student_ambiguous", "일치하는 기존 학생이 여러 명입니다. 학생 목록에서 중복을 정리해 주세요.")
        )
        return {"status": "ambiguous", "id": None, "basis": []}, issues, profile_changes
    if exact:
        student = exact[0]
        requested_school = _normalized_search(str(row.get("school") or ""))
        stored_school = _normalized_search(_student_school(student))
        if requested_school and stored_school and requested_school != stored_school:
            issues.append(
                _issue("school_conflict", "사진의 학교와 기존 학생의 학교가 다릅니다. 학생 목록에서 확인해 주세요.")
            )
        if student.phone and student_phone and student.phone != student_phone:
            issues.append(_issue("student_phone_conflict", "기존 학생 전화번호와 사진의 번호가 다릅니다."))
        if student.parent_phone and parent_phone and student.parent_phone != parent_phone:
            issues.append(_issue("parent_phone_conflict", "기존 학부모 전화번호와 사진의 번호가 다릅니다."))
        if not student.phone and student_phone:
            profile_changes.extend(["student.phone", "student.ps_number", "user.phone"])
        if not student.parent_phone and parent_phone:
            profile_changes.extend(["student.parent_phone", "parent.link"])
        basis = ["name"]
        if student_phone and student.phone == student_phone:
            basis.append("student_phone")
        if parent_phone and student.parent_phone == parent_phone:
            basis.append("parent_phone")
        if requested_school and stored_school == requested_school:
            basis.append("school")
        return {"status": "existing", "id": student.id, "basis": basis}, issues, profile_changes

    deleted = Student.objects.filter(tenant=tenant, deleted_at__isnull=False, name=name)
    deleted = deleted.filter(phone_query) if has_phone_evidence else deleted.none()
    deleted_matches = list(deleted.order_by("id")[:2])
    if len(deleted_matches) > 1:
        issues.append(
            _issue("deleted_student_ambiguous", "일치하는 삭제 학생이 여러 명입니다. 학생 목록에서 복원해 주세요.")
        )
        return {"status": "ambiguous", "id": None, "basis": []}, issues, profile_changes
    if deleted_matches:
        return {"status": "restore", "id": deleted_matches[0].id, "basis": ["name", "phone"]}, issues, profile_changes

    if not row.get("register_student"):
        issues.append(_issue("student_not_found", "기존 학생을 찾지 못했습니다. 신규 등록 여부를 확인해 주세요."))
    if not student_phone:
        issues.append(_issue("new_student_phone_required", "신규 등록에는 학생 전화번호가 필요합니다."))
    return {"status": "new", "id": None, "basis": ["no_existing_match"]}, issues, profile_changes


def _session_target(
    *, tenant, lecture_id: int | None, session_order: int | None
) -> tuple[dict | None, list[dict], list[dict]]:
    if not lecture_id or not session_order:
        return None, [], []
    sessions = list(
        Session.objects.filter(
            lecture_id=lecture_id,
            lecture__tenant=tenant,
            session_type=Session.SessionType.REGULAR,
            regular_order=session_order,
        ).select_related("section", "lecture")
    )
    if len(sessions) != 1:
        return (
            None,
            [],
            [_issue("session_ambiguous", "해당 회차가 없거나 반별 차시가 여러 개입니다. 정확한 차시를 선택해 주세요.")],
        )
    session = sessions[0]
    videos = list(
        Video.objects.filter(tenant=tenant, session=session, status=Video.Status.READY).order_by("order", "id")
    )
    return (
        {"id": session.id, "label": session.display_label},
        [{"id": video.id, "title": video.title} for video in videos],
        [],
    )


def _correction_options(*, tenant, student_id: int | None, selected_lecture_id: int | None) -> list[dict]:
    if not student_id:
        return []
    options = []
    enrollments = (
        Enrollment.objects.filter(tenant=tenant, student_id=student_id, status="ACTIVE")
        .exclude(lecture_id=selected_lecture_id)
        .select_related("lecture")
    )
    for enrollment in enrollments.order_by("lecture__title", "id"):
        impact = assess_disposable_enrollment(tenant=tenant, enrollment=enrollment).as_dict()
        options.append({"enrollment_id": enrollment.id, "lecture_title": enrollment.lecture.title, "impact": impact})
    return options


def build_preview_row(*, tenant, source_row: dict, override: dict | None = None) -> dict:
    row = {**source_row, **(override or {})}
    issues = [_issue("ocr_review", warning, blocking=False) for warning in source_row.get("warnings", [])]
    student_match, student_issues, profile_changes = _student_match(tenant=tenant, row=row)
    issues.extend(student_issues)

    lecture_options = active_lecture_options(tenant=tenant)
    candidates = _lecture_candidates(
        lecture_options=lecture_options,
        hint=str(row.get("lecture_hint") or ""),
        school=str(row.get("school") or ""),
        grade=str(row.get("grade") or ""),
    )
    selected_lecture_id = row.get("selected_lecture_id")
    if selected_lecture_id:
        selected = next((option for option in lecture_options if option["id"] == int(selected_lecture_id)), None)
        if selected is None:
            issues.append(_issue("lecture_out_of_scope", "선택한 강의는 현재 학원의 활성 기간 강의가 아닙니다."))
            selected_lecture_id = None
    elif len(candidates) == 1:
        selected_lecture_id = candidates[0]["id"]

    needs_lecture = bool(row.get("enroll_lecture") or row.get("open_video"))
    if needs_lecture and not selected_lecture_id:
        issues.append(_issue("lecture_required", "학교·학년·요청을 확인해 강의를 하나 선택해 주세요."))

    session_order = row.get("session_order")
    if row.get("open_video") and not session_order:
        issues.append(_issue("session_required", "영상 수업으로 열 정확한 회차를 입력해 주세요."))
    session_target, video_targets, target_issues = _session_target(
        tenant=tenant,
        lecture_id=int(selected_lecture_id) if selected_lecture_id else None,
        session_order=int(session_order) if session_order else None,
    )
    issues.extend(target_issues)
    if row.get("open_video") and session_target and not video_targets:
        issues.append(_issue("ready_video_not_found", "선택한 차시에 재생 가능한 영상이 없습니다."))
    if row.get("send_account_notice"):
        issues.extend(
            _account_notice_template_issues(
                student_phone=str(row.get("student_phone") or ""),
                parent_phone=str(row.get("parent_phone") or ""),
            )
        )

    correction_options = _correction_options(
        tenant=tenant,
        student_id=student_match.get("id"),
        selected_lecture_id=int(selected_lecture_id) if selected_lecture_id else None,
    )
    remove_enrollment_id = row.get("remove_enrollment_id")
    if row.get("correct_enrollment"):
        if not remove_enrollment_id:
            issues.append(_issue("correction_selection_required", "교정할 기존 수강을 하나 선택해 주세요."))
        else:
            selected_correction = next(
                (item for item in correction_options if item["enrollment_id"] == int(remove_enrollment_id)), None
            )
            if selected_correction is None:
                issues.append(_issue("correction_out_of_scope", "교정할 수강이 현재 학생의 다른 활성 수강이 아닙니다."))
            elif not selected_correction["impact"]["can_remove"]:
                issues.append(
                    _issue(
                        "correction_has_protected_data", "학습·결제·사용자 입력 데이터가 있어 자동 교정할 수 없습니다."
                    )
                )

    return {
        "row_id": row["row_id"],
        "name": str(row.get("name") or "").strip(),
        "student_phone": str(row.get("student_phone") or "").strip(),
        "parent_phone": str(row.get("parent_phone") or "").strip(),
        "school": str(row.get("school") or "").strip(),
        "school_type": str(row.get("school_type") or "HIGH"),
        "grade": str(row.get("grade") or "").strip(),
        "lecture_hint": str(row.get("lecture_hint") or "").strip(),
        "selected_lecture_id": selected_lecture_id,
        "session_order": session_order,
        "remove_enrollment_id": remove_enrollment_id,
        "actions": {
            "register_student": bool(row.get("register_student")),
            "enroll_lecture": bool(row.get("enroll_lecture")),
            "open_video": bool(row.get("open_video")),
            "send_account_notice": bool(row.get("send_account_notice")),
            "correct_enrollment": bool(row.get("correct_enrollment")),
        },
        "student_match": student_match,
        "profile_changes": profile_changes,
        "lecture_candidates": candidates,
        "session_target": session_target,
        "video_targets": video_targets,
        "attendance_target": "ONLINE" if row.get("open_video") else None,
        "notice_targets": ["student", "parent"] if row.get("send_account_notice") else [],
        "correction_options": correction_options,
        "issues": issues,
        "can_confirm": not any(issue["blocking"] for issue in issues),
    }


def _state_fingerprint(*, tenant) -> str:
    state = {
        "students": list(
            Student.objects.filter(tenant=tenant)
            .order_by("id")
            .values_list("id", "updated_at", "deleted_at", "phone", "parent_phone", "ps_number")
        ),
        "enrollments": list(
            Enrollment.objects.filter(tenant=tenant)
            .order_by("id")
            .values_list("id", "updated_at", "status", "student_id", "lecture_id")
        ),
        "lectures": list(
            _active_lecture_queryset(tenant=tenant).order_by("id").values_list("id", "updated_at", "title")
        ),
        "sessions": list(
            Session.objects.filter(lecture__tenant=tenant, lecture__is_active=True)
            .order_by("id")
            .values_list("id", "updated_at", "lecture_id", "regular_order")
        ),
        "videos": list(
            Video.objects.filter(tenant=tenant).order_by("id").values_list("id", "updated_at", "status", "session_id")
        ),
    }
    return hashlib.sha256(json.dumps(state, default=str, separators=(",", ":")).encode()).hexdigest()


def make_proposal(*, tenant, actor, image_sha256: str, source_rows: list[dict]) -> tuple[str, dict]:
    if not source_rows or len(source_rows) > MAX_ROWS:
        raise ValidationError("한 요청에서 학생은 최대 5명까지 처리할 수 있습니다.")
    payload = {
        "version": 2,
        "nonce": str(uuid.uuid4()),
        "tenant_id": tenant.id,
        "actor_id": actor.id,
        "image_sha256": image_sha256,
        "state_fingerprint": _state_fingerprint(tenant=tenant),
        "rows": source_rows,
    }
    return signing.dumps(payload, salt=PROPOSAL_SALT, compress=True), payload


def load_proposal(*, token: str, tenant, actor) -> dict:
    try:
        payload = signing.loads(token, salt=PROPOSAL_SALT, max_age=PROPOSAL_MAX_AGE_SECONDS)
    except signing.SignatureExpired as exc:
        raise ValidationError({"proposal_token": "검토 시간이 지났습니다. 사진을 다시 읽어 주세요."}) from exc
    except signing.BadSignature as exc:
        raise ValidationError({"proposal_token": "확인 정보가 올바르지 않습니다. 사진을 다시 읽어 주세요."}) from exc
    if payload.get("version") != 2:
        raise ValidationError({"proposal_token": "지원하지 않는 확인 정보입니다."})
    if payload.get("tenant_id") != tenant.id or payload.get("actor_id") != actor.id:
        raise PermissionDenied("이 요청을 확인할 권한이 없습니다.")
    return payload


def proposal_digest(payload: dict) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(serialized.encode()).hexdigest()


def _assert_confirmed_row(*, tenant, source_row: dict, override: dict) -> dict:
    if override["row_id"] != source_row["row_id"]:
        raise ValidationError("학생 확인 정보가 서로 맞지 않습니다.")
    for action in ("register_student", "enroll_lecture", "open_video", "send_account_notice", "correct_enrollment"):
        override[action] = bool(source_row.get(action))
    override["lecture_hint"] = source_row.get("lecture_hint", "")
    override["warnings"] = source_row.get("warnings", [])
    preview = build_preview_row(tenant=tenant, source_row=override)
    if not preview["can_confirm"]:
        raise ValidationError(
            {"code": "proposal_needs_review", "detail": "확인이 필요한 항목이 남아 있습니다.", "row": preview}
        )
    return {**override, "preview": preview}


@dataclass(frozen=True)
class ExecutionResult:
    payload: dict
    student_ids: tuple[int, ...]
    lecture_ids: tuple[int, ...]
    video_ids: tuple[int, ...]


def _lock_existing_students_for_execution(*, tenant, student_ids: set[int]) -> dict[int, Student]:
    """Lock assistant identity rows in the global User -> Student order."""
    if not student_ids:
        return {}
    expected_user_by_student = dict(
        Student.objects.filter(tenant=tenant, id__in=student_ids).values_list(
            "id",
            "user_id",
        )
    )
    if set(expected_user_by_student) != set(student_ids):
        raise ValidationError("검토한 학생 정보를 다시 확인해 주세요.")
    list(
        get_user_model().objects.select_for_update()
        .filter(id__in=expected_user_by_student.values())
        .order_by("id")
    )
    locked_students = list(
        Student.objects.select_for_update()
        .filter(tenant=tenant, id__in=student_ids)
        .select_related("user")
        .order_by("id")
    )
    if {
        student.id: student.user_id for student in locked_students
    } != expected_user_by_student:
        raise ValidationError("학생 계정 연결이 변경되었습니다. 다시 확인해 주세요.")
    return {student.id: student for student in locked_students}


def execute_proposal(*, tenant, actor, payload: dict, overrides: list[dict]) -> ExecutionResult:
    del actor
    source_by_id = {str(row["row_id"]): row for row in payload["rows"]}
    confirmed_rows = []
    for override in overrides:
        if not override.get("enabled", True):
            continue
        source_row = source_by_id.get(str(override["row_id"]))
        if source_row is None:
            raise ValidationError("사진에 없던 학생 정보가 포함되었습니다.")
        confirmed_rows.append(_assert_confirmed_row(tenant=tenant, source_row=source_row, override=dict(override)))
    if not confirmed_rows:
        raise ValidationError("처리할 학생을 한 명 이상 선택해 주세요.")

    results: list[dict] = []
    student_ids: list[int] = []
    lecture_ids: list[int] = []
    video_ids: list[int] = []
    with transaction.atomic():
        lock_student_creation_tenant_reference(tenant_id=tenant.id)
        student_lock_ids = {
            int(row["preview"]["student_match"]["id"])
            for row in confirmed_rows
            if row["preview"]["student_match"].get("id")
        }
        lecture_lock_ids = {
            int(row["preview"]["selected_lecture_id"])
            for row in confirmed_rows
            if row["preview"].get("selected_lecture_id")
        }
        session_lock_ids = {
            int(row["preview"]["session_target"]["id"])
            for row in confirmed_rows
            if row["preview"].get("session_target")
        }
        video_lock_ids = {
            int(target["id"]) for row in confirmed_rows for target in row["preview"].get("video_targets", [])
        }
        correction_lock_ids = {
            int(row["remove_enrollment_id"]) for row in confirmed_rows if row.get("remove_enrollment_id")
        }
        locked_students_by_id = _lock_existing_students_for_execution(
            tenant=tenant,
            student_ids=student_lock_ids,
        )
        list(
            Enrollment.objects.select_for_update()
            .filter(tenant=tenant)
            .filter(Q(id__in=correction_lock_ids) | Q(student_id__in=student_lock_ids, lecture_id__in=lecture_lock_ids))
        )
        list(Lecture.objects.select_for_update().filter(tenant=tenant, id__in=lecture_lock_ids))
        list(Session.objects.select_for_update().filter(lecture__tenant=tenant, id__in=session_lock_ids))
        list(Video.objects.select_for_update().filter(tenant=tenant, id__in=video_lock_ids))
        if _state_fingerprint(tenant=tenant) != payload.get("state_fingerprint"):
            raise ValidationError(
                {
                    "code": "proposal_drift",
                    "detail": "검토 후 학생·강의 상태가 바뀌었습니다. 사진을 다시 분석해 주세요.",
                }
            )

        for row in confirmed_rows:
            preview = row["preview"]
            match = preview["student_match"]
            student = None
            created = False
            restored = False
            profile_changed: list[str] = []
            if match["status"] == "existing":
                student = locked_students_by_id.get(int(match["id"]))
                if student is None or student.deleted_at is not None:
                    raise ValidationError("검토한 학생 정보가 변경되었습니다. 다시 확인해 주세요.")
                update_data: dict[str, Any] = {}
                if not student.phone and row.get("student_phone"):
                    update_data["phone"] = row["student_phone"]
                if not student.parent_phone and row.get("parent_phone"):
                    update_data["parent_phone"] = row["parent_phone"]
                if update_data:
                    try:
                        updated = update_student_profile(
                            student=student,
                            tenant=tenant,
                            data=update_data,
                            identity_field="ps_number",
                            strict_school_validation=False,
                        )
                    except StudentProfileUpdateError as exc:
                        raise ValidationError(exc.detail) from exc
                    profile_changed = list(updated.changed_fields)
                    student.refresh_from_db()
            else:
                student_phone = "".join(char for char in row.get("student_phone", "") if char.isdigit())
                resolution = resolve_student_import_row(
                    tenant,
                    {
                        "name": row["name"],
                        "phone": row.get("student_phone", ""),
                        "parent_phone": row.get("parent_phone", ""),
                        "school_type": row.get("school_type", "HIGH"),
                        "school": row.get("school", ""),
                        "grade": row.get("grade", ""),
                        "is_managed": True,
                    },
                    student_phone[-4:],
                    identity_policy="phone_if_available",
                    source_job_id=str(payload["nonce"]),
                )
                student, created, restored = resolution.student, resolution.created, resolution.restored

            if student is None:
                raise ValidationError("학생을 안전하게 특정하지 못했습니다.")

            correction = None
            if row.get("correct_enrollment"):
                correction = delete_disposable_enrollment(
                    tenant=tenant,
                    enrollment_id=int(row["remove_enrollment_id"]),
                    student_id=student.id,
                ).as_dict()

            lecture_id = preview.get("selected_lecture_id")
            enrollment = None
            enrollment_created = False
            notice_origin_id = ""
            notice_expected = 0
            if row.get("enroll_lecture") or row.get("open_video"):
                existing_enrollment = (
                    Enrollment.objects.select_for_update()
                    .filter(tenant=tenant, student=student, lecture_id=lecture_id)
                    .first()
                )
                enrollment_created = existing_enrollment is None or existing_enrollment.status != "ACTIVE"
                notice_origin_id = str(student.pending_account_notice_origin_id or "")
                notice_expected = (
                    int(bool(student.parent_phone)) + int(bool(student.phone) and student.phone != student.parent_phone)
                    if student.pending_account_notice_since
                    else 0
                )
                enrollment = bulk_create_enrollments(tenant=tenant, lecture_id=lecture_id, student_ids=[student.id])[0]

            attendance_evidence = None
            videos_evidence: list[dict] = []
            if row.get("open_video"):
                if enrollment is None or enrollment.status != "ACTIVE":
                    raise ValidationError("활성 수강 등록이 없어 영상 수업을 열 수 없습니다.")
                session = Session.objects.select_for_update().get(
                    id=preview["session_target"]["id"],
                    lecture_id=lecture_id,
                    lecture__tenant=tenant,
                )
                membership = ensure_session_roster_membership(tenant=tenant, session=session, enrollment=enrollment)
                attendance = Attendance.objects.select_for_update().get(pk=membership.attendance.pk)
                if attendance.status not in {"UNSET", "ONLINE"}:
                    raise ValidationError("이미 기록된 오프라인 출결이 있어 ONLINE으로 자동 변경할 수 없습니다.")
                if attendance.status == "UNSET" and any(
                    (
                        attendance.memo,
                        attendance.planned_arrival_date,
                        attendance.planned_arrival_time,
                        attendance.attended_section_id,
                    )
                ):
                    raise ValidationError("출결에 사용자 입력이 있어 ONLINE으로 자동 변경할 수 없습니다.")
                if attendance.status != "ONLINE":
                    attendance.status = "ONLINE"
                    attendance.save(update_fields=["status"])
                target_ids = {target["id"] for target in preview["video_targets"]}
                videos = list(
                    Video.objects.select_for_update().filter(
                        tenant=tenant, id__in=target_ids, session=session, status=Video.Status.READY
                    )
                )
                if {video.id for video in videos} != target_ids:
                    raise ValidationError("영상 상태가 바뀌었습니다. 사진을 다시 분석해 주세요.")
                for video in videos:
                    blocked = (
                        VideoAccess.objects.select_for_update()
                        .filter(video=video, enrollment=enrollment, access_mode=AccessMode.BLOCKED)
                        .first()
                    )
                    if blocked:
                        blocked.access_mode = AccessMode.PROCTORED_CLASS
                        blocked.rule = "once"
                        blocked.proctored_completed_at = None
                        blocked.save(update_fields=["access_mode", "rule", "proctored_completed_at"])
                    mode = resolve_access_mode(video=video, enrollment=enrollment)
                    if mode != AccessMode.PROCTORED_CLASS:
                        raise ValidationError("ONLINE 출결과 영상 수업 권한이 일치하지 않습니다.")
                    videos_evidence.append({"video_id": video.id, "access_mode": mode, "monitoring": True})
                    video_ids.append(video.id)
                attendance_evidence = {
                    "attendance_id": attendance.id,
                    "status": attendance.status,
                    "session_id": session.id,
                }

            account_notice = {
                "requested": bool(row.get("send_account_notice")),
                "state": "not_requested",
                "origin_id": "",
                "expected_recipients": 0,
            }
            if row.get("send_account_notice"):
                if notice_expected:
                    account_notice.update(
                        state="queued", origin_id=notice_origin_id, expected_recipients=notice_expected
                    )
                else:
                    account_notice["state"] = "unavailable_without_pending_credentials"

            active_correct = (
                Enrollment.objects.filter(
                    tenant=tenant, student=student, lecture_id=lecture_id, status="ACTIVE"
                ).count()
                if lecture_id
                else 0
            )
            results.append(
                {
                    "row_id": row["row_id"],
                    "student_ref": student.id,
                    "account_creation": "created" if created else "not_created",
                    "account_restored": restored,
                    "profile_link": {
                        "state": "updated" if profile_changed else "unchanged",
                        "changed_fields": profile_changed,
                    },
                    "enrollment": {
                        "state": "created" if enrollment_created else "already_active",
                        "correct_active_count": active_correct,
                        "wrong_active_removed": bool(correction),
                    },
                    "correction": correction,
                    "attendance": attendance_evidence,
                    "video_access": videos_evidence,
                    "account_notice": account_notice,
                    "real_playback_canary": {"state": "not_run", "reason": "separate_safe_boundary_required"},
                }
            )
            student_ids.append(student.id)
            if lecture_id:
                lecture_ids.append(int(lecture_id))

    return ExecutionResult(
        payload={
            "execution_id": str(payload["nonce"]),
            "rows": results,
            "provider_receipt_note": "알림톡 공급사 접수 성공과 카카오 실제 열람은 서로 다른 상태입니다.",
            "idempotent_replay": False,
        },
        student_ids=tuple(student_ids),
        lecture_ids=tuple(lecture_ids),
        video_ids=tuple(video_ids),
    )
