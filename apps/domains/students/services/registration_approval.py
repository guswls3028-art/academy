from __future__ import annotations

from dataclasses import dataclass

from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.db.models import Count, Q

from apps.core.models import TenantMembership
from apps.core.models.user import user_internal_username
from apps.core.services.login_identifier import normalize_login_identifier
from apps.core.services.password import adopt_password_hash
from apps.support.students.lifecycle_dependencies import (
    locked_parent_account_by_phone_for_registration,
    locked_parent_account_for_registration,
)

from ..models import Student, StudentRegistrationRequest
from .creation import create_student_account
from .identity import (
    StudentIdentityError,
    canonical_student_phone,
    derive_student_omr_code,
    phone_digits,
    resolve_student_login_id,
)
from .lifecycle import StudentLifecycleError, restore_student
from .profile import StudentProfileUpdateError, update_student_profile
from .registration_policy import is_student_self_registration_enabled


class RegistrationApprovalError(ValueError):
    def __init__(
        self,
        detail: str,
        *,
        status_code: int = 400,
        code: str = "",
        context: dict[str, object] | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code
        self.code = code
        self.context = context or {}


@dataclass(frozen=True)
class RegistrationApprovalNotice:
    student_name: str
    student_phone: str
    student_id: str
    student_password: str
    parent_phone: str
    parent_password: str


@dataclass(frozen=True)
class RegistrationApprovalResult:
    registration: StudentRegistrationRequest
    student: Student
    notice: RegistrationApprovalNotice


def _resolve_login_id(
    tenant,
    reg: StudentRegistrationRequest,
    *,
    exclude_student_id: int | None = None,
    exclude_user_id: int | None = None,
) -> str:
    try:
        student_phone = canonical_student_phone(
            phone=reg.phone,
            parent_phone=reg.parent_phone,
        )
        login_id = resolve_student_login_id(
            tenant=tenant,
            requested_id=reg.username,
            phone=student_phone,
            exclude_student_id=exclude_student_id,
            exclude_user_id=exclude_user_id,
        )
        if (
            normalize_login_identifier(reg.username)
            and login_id == phone_digits(reg.parent_phone)
        ):
            raise StudentIdentityError(
                {"username": "학부모 전화번호는 학생 로그인 아이디로 사용할 수 없습니다."}
            )
        return login_id
    except StudentIdentityError as exc:
        raise RegistrationApprovalError(str(exc.detail), status_code=400) from exc


def _registration_identity_query(tenant, reg: StudentRegistrationRequest) -> Q:
    query = Q(
        name=str(reg.name or "").strip(),
        parent_phone=phone_digits(reg.parent_phone),
    )
    student_phone = phone_digits(reg.phone)
    if student_phone:
        query |= Q(phone=student_phone) | Q(user__phone=student_phone)
    requested_id = normalize_login_identifier(reg.username)
    if requested_id:
        query |= Q(ps_number=requested_id) | Q(
            user__username=user_internal_username(tenant, requested_id)
        )
    return query


def _deleted_recovery_identity_query(reg: StudentRegistrationRequest) -> Q:
    """Exact profile identity required before staff may select a deleted row."""
    query = Q(
        name=str(reg.name or "").strip(),
        parent_phone=phone_digits(reg.parent_phone),
    )
    student_phone = phone_digits(reg.phone)
    parent_phone = phone_digits(reg.parent_phone)
    if student_phone and student_phone != parent_phone:
        query &= Q(phone=student_phone) | Q(user__phone=student_phone)
    return query


def _lock_deleted_recovery_graph(
    *,
    tenant,
    reg: StudentRegistrationRequest,
    student_id: int,
) -> Student:
    """Lock and revalidate the exact historic graph before any restore write."""
    candidate = (
        Student.objects.filter(
            pk=student_id,
            tenant=tenant,
            deleted_at__isnull=False,
        )
        .filter(_deleted_recovery_identity_query(reg))
        .values("id", "user_id")
        .first()
    )
    if candidate is None:
        raise RegistrationApprovalError(
            "선택한 삭제 학생이 이 가입 신청과 일치하지 않습니다. 목록을 새로 확인해 주세요.",
            status_code=409,
        )
    if candidate["user_id"] is None:
        raise RegistrationApprovalError(
            "선택한 학생의 로그인 계정 연결을 먼저 확인해 주세요.",
            status_code=409,
        )

    parent_phone = phone_digits(reg.parent_phone)
    parent = locked_parent_account_by_phone_for_registration(
        tenant_id=tenant.id,
        phone=parent_phone,
    )
    if parent is None or parent.user_id is None:
        raise RegistrationApprovalError(
            "선택한 학생의 기존 학부모 계정 연결을 먼저 확인해 주세요.",
            status_code=409,
        )

    User = get_user_model()
    user_ids = sorted({candidate["user_id"], parent.user_id})
    locked_users = {
        user.id: user
        for user in User.objects.select_for_update().filter(pk__in=user_ids).order_by("id")
    }
    student_user = locked_users.get(candidate["user_id"])
    parent_user = locked_users.get(parent.user_id)
    if student_user is None or parent_user is None:
        raise RegistrationApprovalError(
            "선택한 학생·학부모 로그인 계정 연결을 먼저 확인해 주세요.",
            status_code=409,
        )

    student = Student.objects.select_for_update().get(pk=student_id)
    if not (
        Student.objects.filter(
            pk=student.pk,
            tenant=tenant,
            deleted_at__isnull=False,
            user_id=student_user.id,
        )
        .filter(_deleted_recovery_identity_query(reg))
        .exists()
    ):
        raise RegistrationApprovalError(
            "선택한 삭제 학생 정보가 승인 중 변경되었습니다. 목록을 새로 확인해 주세요.",
            status_code=409,
        )
    if student.parent_id not in (None, parent.id):
        raise RegistrationApprovalError(
            "선택한 학생의 학부모 연결이 가입 신청과 일치하지 않습니다.",
            status_code=409,
        )
    if Student.objects.filter(user_id=student_user.id).exclude(pk=student.pk).exists():
        raise RegistrationApprovalError(
            "선택한 로그인 계정이 다른 학생과도 연결되어 있습니다.",
            status_code=409,
        )

    memberships = {
        membership.user_id: membership
        for membership in (
            TenantMembership.objects.select_for_update()
            .filter(tenant=tenant, user_id__in=user_ids)
            .order_by("user_id")
        )
    }
    membership = memberships.get(student_user.id)
    if membership is None or membership.role != "student" or membership.is_active:
        raise RegistrationApprovalError(
            "선택한 삭제 학생의 멤버십 이력이 일치하지 않습니다.",
            status_code=409,
        )
    if TenantMembership.objects.filter(user_id=student_user.id).exclude(tenant=tenant).exists():
        raise RegistrationApprovalError(
            "다른 학원에도 연결된 로그인 계정은 이 경로에서 복구할 수 없습니다.",
            status_code=409,
        )
    if student_user.tenant_id not in (None, tenant.id):
        raise RegistrationApprovalError(
            "선택한 학생 로그인 계정의 테넌트 연결이 일치하지 않습니다.",
            status_code=409,
        )
    if student_user.is_active:
        raise RegistrationApprovalError(
            "선택한 삭제 학생의 로그인 계정 상태가 예상과 다릅니다.",
            status_code=409,
        )
    _validate_active_login_user(
        tenant=tenant,
        user=parent_user,
        expected_username=f"p_{tenant.id}_{parent_phone}",
        expected_phone=parent_phone,
        role="parent",
        label="학부모",
    )
    student.user = student_user
    return student


def _registration_identity_lock_keys(tenant, reg: StudentRegistrationRequest) -> tuple[str, ...]:
    prefix = f"student-registration-approval:{tenant.id}:"
    name = str(reg.name or "").strip()
    parent_phone = phone_digits(reg.parent_phone)
    student_phone = phone_digits(reg.phone)
    requested_id = normalize_login_identifier(reg.username)
    keys = {f"{prefix}name-parent:{name}:{parent_phone}"}
    if student_phone:
        keys.add(f"{prefix}identity-value:{student_phone}")
    if requested_id:
        keys.add(f"{prefix}identity-value:{requested_id}")
    return tuple(sorted(keys))


def _acquire_registration_identity_locks(tenant, reg: StudentRegistrationRequest) -> None:
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        for key in _registration_identity_lock_keys(tenant, reg):
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                [key],
            )


def _validate_active_membership(*, tenant, user, role: str, label: str) -> None:
    membership = (
        TenantMembership.objects.select_for_update()
        .filter(tenant=tenant, user_id=user.id)
        .first()
    )
    if membership is None or membership.role != role or not membership.is_active:
        raise RegistrationApprovalError(
            f"기존 {label} 계정의 활성 멤버십 연결이 일치하지 않습니다.",
            status_code=409,
        )


def _validate_active_login_user(
    *,
    tenant,
    user,
    expected_username: str,
    expected_phone: str,
    role: str,
    label: str,
) -> None:
    if user.tenant_id != tenant.id:
        raise RegistrationApprovalError(
            f"기존 {label} 로그인 계정의 테넌트 연결이 일치하지 않습니다.",
            status_code=409,
        )
    if user.username != expected_username:
        raise RegistrationApprovalError(
            f"기존 {label} 로그인 ID 연결이 일치하지 않습니다.",
            status_code=409,
        )
    if phone_digits(user.phone) != expected_phone:
        raise RegistrationApprovalError(
            f"기존 {label} 로그인 계정의 연락처 연결이 일치하지 않습니다.",
            status_code=409,
        )
    if not user.is_active:
        raise RegistrationApprovalError(
            f"기존 {label} 로그인 계정이 비활성 상태입니다.",
            status_code=409,
        )
    _validate_active_membership(
        tenant=tenant,
        user=user,
        role=role,
        label=label,
    )


def _validate_existing_student_graph(
    *,
    tenant,
    reg,
    student,
    locked_user,
    parent,
    locked_parent_user,
) -> None:
    if student.tenant_id != tenant.id or locked_user.tenant_id != tenant.id:
        raise RegistrationApprovalError(
            "기존 학생 계정의 테넌트 연결이 일치하지 않습니다.",
            status_code=409,
        )
    if student.user_id != locked_user.id:
        raise RegistrationApprovalError(
            "기존 학생 계정 연결이 승인 중 변경되었습니다.",
            status_code=409,
        )
    if student.deleted_at is not None:
        raise RegistrationApprovalError(
            "같은 식별값의 삭제 학생이 있습니다. 기존 학생을 확인해 주세요.",
            status_code=409,
        )
    if str(student.name or "").strip() != str(reg.name or "").strip():
        raise RegistrationApprovalError(
            "가입 신청 정보와 기존 학생 이름이 일치하지 않습니다.",
            status_code=409,
        )

    registration_parent_phone = phone_digits(reg.parent_phone)
    if phone_digits(student.parent_phone) != registration_parent_phone:
        raise RegistrationApprovalError(
            "가입 신청 정보와 기존 학생의 학부모 연락처가 일치하지 않습니다.",
            status_code=409,
        )

    if parent is None or student.parent_id != parent.id:
        raise RegistrationApprovalError(
            "기존 학생의 학부모 계정 연결을 먼저 확인해 주세요.",
            status_code=409,
        )
    if parent.tenant_id != tenant.id or phone_digits(parent.phone) != registration_parent_phone:
        raise RegistrationApprovalError(
            "기존 학부모 계정 연결이 가입 신청 정보와 일치하지 않습니다.",
            status_code=409,
        )
    if locked_parent_user is None or parent.user_id != locked_parent_user.id:
        raise RegistrationApprovalError(
            "기존 학생의 학부모 로그인 계정 연결을 먼저 확인해 주세요.",
            status_code=409,
        )

    stored_student_phone = phone_digits(student.phone)
    stored_user_phone = phone_digits(locked_user.phone)
    if stored_student_phone != stored_user_phone:
        raise RegistrationApprovalError(
            "기존 학생과 로그인 계정의 연락처 연결이 일치하지 않습니다.",
            status_code=409,
        )
    _validate_active_login_user(
        tenant=tenant,
        user=locked_user,
        expected_username=user_internal_username(tenant, student.ps_number),
        expected_phone=stored_student_phone,
        role="student",
        label="학생",
    )
    _validate_active_login_user(
        tenant=tenant,
        user=locked_parent_user,
        expected_username=f"p_{tenant.id}_{registration_parent_phone}",
        expected_phone=registration_parent_phone,
        role="parent",
        label="학부모",
    )
    registration_student_phone = phone_digits(reg.phone)
    existing_identity_phones = {
        value
        for value in (stored_student_phone, registration_parent_phone)
        if value
    }
    if registration_student_phone and registration_student_phone not in existing_identity_phones:
        raise RegistrationApprovalError(
            "가입 신청 정보와 기존 학생·학부모 연락처가 일치하지 않습니다.",
            status_code=409,
        )


def _resolve_existing_student(*, tenant, reg: StudentRegistrationRequest) -> Student | None:
    identity_query = _registration_identity_query(tenant, reg)
    candidates = list(
        Student.objects.filter(tenant=tenant)
        .filter(identity_query)
        .values("id", "user_id", "parent_id", "deleted_at")
        .order_by("id")[:3]
    )
    deleted = [candidate for candidate in candidates if candidate["deleted_at"] is not None]
    if deleted:
        deleted_candidates = list(
            Student.objects.filter(tenant=tenant, deleted_at__isnull=False)
            .filter(_deleted_recovery_identity_query(reg))
            .annotate(enrollment_count=Count("enrollments", distinct=True))
            .values("id", "created_at", "deleted_at", "enrollment_count")
            .order_by("-created_at", "-id")[:10]
        )
        raise RegistrationApprovalError(
            "같은 학생으로 보이는 삭제 이력이 있습니다. 복구할 과거 계정을 직접 선택해 주세요.",
            status_code=409,
            code="deleted_student_conflict",
            context={
                "candidates": [
                    {
                        "student_id": candidate["id"],
                        "created_at": candidate["created_at"],
                        "deleted_at": candidate["deleted_at"],
                        "enrollment_count": candidate["enrollment_count"],
                    }
                    for candidate in deleted_candidates
                ]
            },
        )
    if len(candidates) > 1:
        raise RegistrationApprovalError(
            "같은 가입 식별값을 가진 활성 학생이 여러 명입니다. 학생 정보를 먼저 정리해 주세요.",
            status_code=409,
        )
    if not candidates:
        return None

    candidate = candidates[0]
    User = get_user_model()
    # Global graph lock order is Parent -> related Users by id -> Student.
    # Parent account ensure uses Parent -> User, while #277 requires every
    # persisted student User to be locked before its Student row.
    parent = None
    if candidate["parent_id"]:
        parent = locked_parent_account_for_registration(
            tenant_id=tenant.id,
            parent_id=candidate["parent_id"],
        )
    user_ids = {candidate["user_id"]}
    if parent is not None and parent.user_id:
        user_ids.add(parent.user_id)
    locked_users = {
        user.id: user
        for user in User.objects.select_for_update()
        .filter(pk__in=[user_id for user_id in user_ids if user_id])
        .order_by("id")
    }
    locked_user = locked_users.get(candidate["user_id"])
    if locked_user is None:
        raise RegistrationApprovalError(
            "기존 학생 로그인 계정 연결을 먼저 확인해 주세요.",
            status_code=409,
        )
    # Keep nullable Parent/User joins out of the locking query; PostgreSQL
    # rejects FOR UPDATE on the nullable side of an outer join.
    student = Student.objects.select_for_update().get(pk=candidate["id"])
    if not Student.objects.filter(pk=student.pk, tenant=tenant).filter(identity_query).exists():
        raise RegistrationApprovalError(
            "기존 학생 식별정보가 승인 중 변경되었습니다. 다시 확인해 주세요.",
            status_code=409,
        )
    locked_parent_user = locked_users.get(parent.user_id) if parent is not None else None
    _validate_existing_student_graph(
        tenant=tenant,
        reg=reg,
        student=student,
        locked_user=locked_user,
        parent=parent,
        locked_parent_user=locked_parent_user,
    )
    if StudentRegistrationRequest.objects.filter(student=student).exclude(pk=reg.pk).exists():
        raise RegistrationApprovalError(
            "기존 학생이 이미 다른 가입 신청과 연결되어 있습니다.",
            status_code=409,
        )
    return student


def _validate_unlinked_account_graph(*, tenant, reg: StudentRegistrationRequest) -> None:
    registration_parent_phone = phone_digits(reg.parent_phone)
    parent = locked_parent_account_by_phone_for_registration(
        tenant_id=tenant.id,
        phone=registration_parent_phone,
    )
    student_phone = phone_digits(reg.phone)
    same_phone_user_ids = []
    if student_phone:
        same_phone_user_ids = list(
            get_user_model().objects.filter(tenant=tenant, phone=student_phone)
            .order_by("id")
            .values_list("id", flat=True)[:3]
        )
    user_ids = set(same_phone_user_ids)
    if parent and parent.user_id:
        user_ids.add(parent.user_id)
    locked_users = {
        user.id: user
        for user in get_user_model().objects.select_for_update()
        .filter(pk__in=user_ids)
        .order_by("id")
    }
    locked_parent_user = None
    if parent and parent.user_id:
        locked_parent_user = locked_users.get(parent.user_id)
        if locked_parent_user is None:
            raise RegistrationApprovalError(
                "기존 학부모 로그인 계정 연결을 먼저 확인해 주세요.",
                status_code=409,
            )
        _validate_active_login_user(
            tenant=tenant,
            user=locked_parent_user,
            expected_username=f"p_{tenant.id}_{registration_parent_phone}",
            expected_phone=registration_parent_phone,
            role="parent",
            label="학부모",
        )

    if not student_phone:
        return
    for user_id in same_phone_user_ids:
        if locked_parent_user is not None and user_id == locked_parent_user.id:
            continue
        raise RegistrationApprovalError(
            "같은 학생 연락처를 사용하는 기존 계정이 있습니다. 계정 연결을 먼저 확인해 주세요.",
            status_code=409,
        )


def _approve_with_existing_student(
    *,
    reg: StudentRegistrationRequest,
    student: Student,
) -> RegistrationApprovalResult:
    reg.status = StudentRegistrationRequest.APPROVED
    reg.student = student
    reg.initial_password_plain = ""
    reg.save(update_fields=["status", "student", "initial_password_plain", "updated_at"])
    notice = RegistrationApprovalNotice(
        student_name=student.name,
        student_phone=student.phone or "",
        student_id=student.ps_number,
        student_password="변경되지 않음",
        parent_phone=student.parent_phone,
        parent_password="변경되지 않음",
    )
    return RegistrationApprovalResult(
        registration=reg,
        student=student,
        notice=notice,
    )


def approve_registration_request(
    *,
    tenant,
    registration_id: int,
) -> RegistrationApprovalResult:
    """
    Approve one student registration request.

    Owns only the durable state transition and account creation graph. HTTP response
    shape and message delivery remain caller concerns.
    """
    with transaction.atomic():
        if not is_student_self_registration_enabled(tenant):
            raise RegistrationApprovalError(
                "이 학원은 운영정책상 학생 회원가입을 사용하지 않습니다.",
                status_code=403,
            )
        # Lock only the registration row. Joining nullable student here breaks
        # PostgreSQL FOR UPDATE because it becomes a nullable outer join.
        reg = (
            StudentRegistrationRequest.objects.select_for_update()
            .get(pk=registration_id, tenant=tenant)
        )
        if reg.status != StudentRegistrationRequest.PENDING:
            raise RegistrationApprovalError("이미 처리된 신청입니다.", status_code=400)

        _acquire_registration_identity_locks(tenant, reg)
        existing_student = _resolve_existing_student(tenant=tenant, reg=reg)
        if existing_student is not None:
            return _approve_with_existing_student(reg=reg, student=existing_student)

        _validate_unlinked_account_graph(tenant=tenant, reg=reg)

        ps_number = _resolve_login_id(tenant, reg)
        parent_phone = reg.parent_phone or ""
        student_phone = reg.phone or None
        result = create_student_account(
            tenant=tenant,
            password_hash=reg.initial_password,
            account_notice_student_password="가입 신청 시 입력한 비밀번호",
            account_notice_parent_password="가입 신청 시 입력한 비밀번호",
            student_data={
                "name": reg.name,
                "parent_phone": parent_phone,
                "phone": student_phone,
                "ps_number": ps_number,
                "omr_code": derive_student_omr_code(
                    phone=student_phone,
                    parent_phone=parent_phone,
                ),
                "uses_identifier": not (student_phone and student_phone.strip()),
                "school_type": reg.school_type,
                "elementary_school": reg.elementary_school or None,
                "high_school": reg.high_school or None,
                "middle_school": reg.middle_school or None,
                "high_school_class": reg.high_school_class or None,
                "major": reg.major or None,
                "grade": reg.grade,
                "gender": reg.gender or None,
                "memo": reg.memo or None,
                "address": reg.address or None,
                "origin_middle_school": reg.origin_middle_school or None,
            },
        )

        reg.status = StudentRegistrationRequest.APPROVED
        reg.student = result.student
        reg.initial_password_plain = ""
        reg.save(update_fields=["status", "student", "initial_password_plain", "updated_at"])

    created_student = result.student
    notice = RegistrationApprovalNotice(
        student_name=reg.name,
        student_phone=created_student.phone or "",
        student_id=created_student.ps_number,
        student_password="가입 신청 시 입력한 비밀번호",
        parent_phone=parent_phone,
        parent_password=result.parent_password_for_notice or "변경되지 않음",
    )
    return RegistrationApprovalResult(
        registration=reg,
        student=result.student,
        notice=notice,
    )


def resolve_deleted_registration_request(
    *,
    tenant,
    registration_id: int,
    student_id: int,
) -> RegistrationApprovalResult:
    """Explicitly restore one selected deleted identity and approve signup.

    The ordinary approval remains fail-closed. This path requires the staff
    caller to select an exact same-tenant deleted candidate returned by the
    conflict response; no automatic winner is inferred when duplicates exist.
    """
    with transaction.atomic():
        if not is_student_self_registration_enabled(tenant):
            raise RegistrationApprovalError(
                "이 학원은 운영정책상 학생 회원가입을 사용하지 않습니다.",
                status_code=403,
            )
        reg = StudentRegistrationRequest.objects.select_for_update().get(
            pk=registration_id,
            tenant=tenant,
        )
        if reg.status != StudentRegistrationRequest.PENDING:
            raise RegistrationApprovalError("이미 처리된 신청입니다.", status_code=409)

        _acquire_registration_identity_locks(tenant, reg)
        identity_query = _registration_identity_query(tenant, reg)
        candidate = _lock_deleted_recovery_graph(
            tenant=tenant,
            reg=reg,
            student_id=student_id,
        )
        if (
            Student.objects.filter(tenant=tenant, deleted_at__isnull=True)
            .filter(identity_query)
            .exists()
        ):
            raise RegistrationApprovalError(
                "같은 식별값의 활성 학생이 있습니다. 학생 정보를 먼저 확인해 주세요.",
                status_code=409,
            )

        login_id = _resolve_login_id(
            tenant,
            reg,
            exclude_student_id=candidate.id,
            exclude_user_id=candidate.user_id,
        )
        profile_data = {
            "name": reg.name,
            "phone": reg.phone,
            "parent_phone": reg.parent_phone,
            "school_type": reg.school_type,
            "elementary_school": reg.elementary_school,
            "high_school": reg.high_school,
            "middle_school": reg.middle_school,
            "high_school_class": reg.high_school_class,
            "major": reg.major,
            "grade": reg.grade,
            "gender": reg.gender,
            "memo": reg.memo,
            "address": reg.address,
            "origin_middle_school": reg.origin_middle_school,
            "ps_number": login_id,
        }
        try:
            restored = restore_student(candidate, tenant=tenant)
            profile_result = update_student_profile(
                student=restored.student,
                tenant=tenant,
                data=profile_data,
                identity_field="ps_number",
            )
        except (StudentLifecycleError, StudentProfileUpdateError) as exc:
            detail = getattr(exc, "detail", str(exc))
            raise RegistrationApprovalError(str(detail), status_code=409) from exc

        student = profile_result.student
        user = get_user_model().objects.select_for_update().get(pk=student.user_id)
        if user.tenant_id != tenant.id:
            raise RegistrationApprovalError(
                "선택한 학생 로그인 계정의 테넌트 연결이 일치하지 않습니다.",
                status_code=409,
            )
        user.username = user_internal_username(tenant, login_id)
        user.phone = student.phone or ""
        user.save(update_fields=["username", "phone"])
        adopt_password_hash(user, reg.initial_password, must_change_password=False)

        reg.status = StudentRegistrationRequest.APPROVED
        reg.student = student
        reg.initial_password_plain = ""
        reg.save(update_fields=["status", "student", "initial_password_plain", "updated_at"])

    notice = RegistrationApprovalNotice(
        student_name=student.name,
        student_phone=student.phone or "",
        student_id=student.ps_number,
        student_password="가입 신청 시 입력한 비밀번호",
        parent_phone=student.parent_phone,
        parent_password="변경되지 않음",
    )
    return RegistrationApprovalResult(registration=reg, student=student, notice=notice)
