# PATH: apps/domains/students/models.py

import uuid

from django.db import IntegrityError, models, transaction
from django.conf import settings

from apps.api.common.models import TimestampModel
from apps.core.models import Tenant
from apps.core.db import TenantQuerySet  # ✅ 추가
from apps.support.students.lifecycle_dependencies import (
    inventory_student_ps_metadata_exists,
    student_inventory_namespace_is_attributable,
    update_inventory_student_ps,
)
from apps.support.students.namespace_lock import (
    lock_student_creation_tenant_reference,
    lock_student_creation_user_reference,
    lock_student_ps_namespaces,
)


def generate_student_custom_field_key() -> str:
    return f"cf_{uuid.uuid4().hex[:12]}"


class StudentInventoryNamespaceConflict(ValueError):
    """A student PS cannot safely claim unattributed legacy inventory metadata."""


class StudentInventoryNamespaceChanged(StudentInventoryNamespaceConflict):
    """The pre-lock predecessor snapshot changed and the claim must retry."""


def _student_ps_predecessor_snapshot(*, tenant_id: int, target_ps: str) -> tuple[tuple[int, str], ...]:
    return tuple(
        (student_id, ps_number)
        for student_id, ps_number in Student.objects.filter(
            tenant_id=tenant_id,
            deleted_at__isnull=False,
            ps_number__endswith=f"_{target_ps}",
        )
        .order_by("id")
        .values_list("id", "ps_number")
        if ps_number == f"_del_{student_id}_{target_ps}"
    )


def _prepare_student_ps_inventory_claim(
    *,
    tenant,
    target_ps: str,
    predecessor_snapshot: tuple[tuple[int, str], ...],
    exclude_student_id: int | None = None,
) -> None:
    current_predecessors = _student_ps_predecessor_snapshot(
        tenant_id=tenant.id,
        target_ps=target_ps,
    )
    if current_predecessors != predecessor_snapshot:
        raise StudentInventoryNamespaceChanged(
            "student storage namespace changed; retry the identity change"
        )
    competing_claim = Student.objects.filter(
        tenant=tenant,
        ps_number=target_ps,
    )
    if exclude_student_id is not None:
        competing_claim = competing_claim.exclude(pk=exclude_student_id)
    if competing_claim.exists():
        raise StudentInventoryNamespaceConflict(
            "student storage namespace is already claimed"
        )
    if not inventory_student_ps_metadata_exists(
        tenant_id=tenant.id,
        ps_number=target_ps,
    ):
        return
    if len(current_predecessors) != 1:
        raise StudentInventoryNamespaceConflict(
            "student storage namespace has ambiguous legacy ownership"
        )
    update_inventory_student_ps(
        tenant=tenant,
        old_ps=target_ps,
        new_ps=current_predecessors[0][1],
    )


class Student(TimestampModel):
    # 🔐 tenant-safe manager (실수 방지)
    objects = TenantQuerySet.as_manager()

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="students",
        help_text="소속 학원 (Tenant)",
        db_index=True,  # ✅ tenant_id 인덱스 추가
    )

    # ✅ 봉인: Student는 User 없이 존재 불가 / User 삭제되면 Student도 같이 삭제
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=False,
        blank=False,
        related_name="student_profile",
        help_text="학생 로그인 계정 (필수)",
    )

    # ✅ NEW: PS 번호 (학원 공식 학생 ID)
    ps_number = models.CharField(
        max_length=50,
        null=False,
        blank=False,
        help_text="PS 번호 (학원 학생 ID)",
    )

    # ✅ NEW: OMR 식별자 (학생 전화번호 또는 부모 전화번호 뒤 8자리)
    omr_code = models.CharField(
        max_length=8,
        null=False,
        blank=False,
        help_text="OMR 자동채점 식별자 (학생 전화번호 또는 부모 전화번호 뒤 8자리)",
    )

    name = models.CharField(max_length=50)

    gender = models.CharField(
        max_length=1,
        choices=[("M", "남"), ("F", "여")],
        null=True,
        blank=True,
    )

    grade = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
    )

    SCHOOL_TYPE_CHOICES = (
        ("ELEMENTARY", "초등"),
        ("MIDDLE", "중등"),
        ("HIGH", "고등"),
    )

    school_type = models.CharField(
        max_length=12,
        choices=SCHOOL_TYPE_CHOICES,
        default="HIGH",
    )

    # 학생 전화번호 (선택사항, 없으면 null)
    phone = models.CharField(
        max_length=20,
        null=True,
        blank=True,
        help_text="정규화된 전화번호 (하이픈 제거, 예: 01012345678)",
    )
    # 부모 전화번호 (필수)
    parent_phone = models.CharField(
        max_length=20,
        null=False,
        blank=False,
        help_text="정규화된 전화번호 (하이픈 제거, 예: 01012345678)",
    )

    uses_identifier = models.BooleanField(
        default=False,
        help_text="True면 학생 전화 없음, 식별자(010+8자리)로 가입. 표시 시 '식별자 XXXX-XXXX'",
    )

    parent = models.ForeignKey(
        "parents.Parent",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="students",
    )

    elementary_school = models.CharField(max_length=100, null=True, blank=True)
    high_school = models.CharField(max_length=100, null=True, blank=True)
    high_school_class = models.CharField(max_length=100, null=True, blank=True)
    major = models.CharField(max_length=50, null=True, blank=True)
    middle_school = models.CharField(max_length=100, null=True, blank=True)
    origin_middle_school = models.CharField(
        max_length=100, null=True, blank=True,
        help_text="출신중학교 (고등학생 선택 입력)",
    )

    memo = models.TextField(null=True, blank=True)
    address = models.CharField(max_length=255, null=True, blank=True, help_text="주소 (선택)")
    custom_fields = models.JSONField(
        default=dict,
        db_default={},
        blank=True,
        help_text="테넌트별 학생 사용자 정의 필드 값. 정의의 stable key를 사용한다.",
    )
    pending_account_notice_student_password_ciphertext = models.TextField(
        null=True,
        blank=True,
        default="",
        help_text="첫 수강 확정 시 계정 안내에 사용할 학생 비밀번호 안내값의 암호문",
    )
    pending_account_notice_parent_password_ciphertext = models.TextField(
        null=True,
        blank=True,
        default="",
        help_text="첫 수강 확정 시 계정 안내에 사용할 학부모 비밀번호 안내값의 암호문",
    )
    pending_account_notice_since = models.DateTimeField(
        null=True,
        blank=True,
        help_text="학생 생성 후 첫 실제 수강 확정을 기다리는 계정 안내 생성 시각",
    )
    pending_account_notice_origin_type = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_default="",
        help_text="첫 수강 계정 안내를 만든 원천 종류(excel_import 등)",
    )
    pending_account_notice_origin_id = models.CharField(
        max_length=128,
        blank=True,
        default="",
        db_default="",
        help_text="첫 수강 계정 안내를 만든 job/batch 식별자",
    )
    is_managed = models.BooleanField(default=True)

    # 학생이 학생앱에서만 설정 (관리자 편집 불가)
    profile_photo = models.ImageField(
        upload_to="student_profile/%Y/%m/",
        null=True,
        blank=True,
        help_text="학생이 학생앱에서 업로드한 프로필 사진 (레거시 로컬)",
    )
    profile_photo_r2_key = models.CharField(
        max_length=500,
        blank=True,
        default="",
        help_text="R2에 저장된 프로필 사진 키. 설정 시 profile_photo 대신 사용",
    )

    deleted_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="삭제일시. 설정 시 30일 보관 후 자동 삭제",
    )

    schedule_hidden_before = models.DateField(
        null=True,
        blank=True,
        help_text="학생앱 일정 휴지통: 이 날짜를 포함하여 그 이전 차시/클리닉 예약은 학생 화면에서 숨김. 실제 데이터는 그대로 유지.",
    )

    schedule_hidden_ids = models.JSONField(
        default=list,
        blank=True,
        help_text="학생앱 일정 개별 숨김 ID 목록. 양수=LectureSession.id, 음수=ClinicSessionParticipant.id*-1. me/ 응답 ID 규약과 동일. 실제 데이터는 그대로 유지.",
    )

    tags = models.ManyToManyField(
        "Tag",
        through="StudentTag",
        related_name="students",
        blank=True,
    )

    class Meta:
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["tenant", "created_at"]),  # ✅ 복합 인덱스 추가
        ]
        constraints = [
            # ✅ tenant 단위 User 유일 (기존 유지, 단 user는 이제 null 불가)
            models.UniqueConstraint(
                fields=["tenant", "user"],
                name="uniq_student_user_per_tenant",
            ),
            # ✅ NEW: tenant 단위 PS 번호 유일
            models.UniqueConstraint(
                fields=["tenant", "ps_number"],
                name="uniq_student_ps_number_per_tenant",
            ),
            # OMR 코드는 unique 제거 (쌍둥이 등 중복 허용, 자동 채점 후 수동 매칭)
        ]

    def save(self, *args, **kwargs):
        if not self.pk:
            target_ps = str(self.ps_number or "").strip()
            max_attempts = (
                1 if transaction.get_connection().in_atomic_block else 3
            )
            for attempt in range(max_attempts):
                try:
                    with transaction.atomic():
                        # Permanent delete takes Tenant FOR UPDATE before the
                        # namespace. New Student inserts take the compatible FK
                        # KEY SHARE gate first so neither side can hold the
                        # namespace while waiting on the Tenant row.
                        lock_student_creation_tenant_reference(
                            tenant_id=self.tenant_id
                        )
                        lock_student_creation_user_reference(user_id=self.user_id)
                        predecessor_snapshot = _student_ps_predecessor_snapshot(
                            tenant_id=self.tenant_id,
                            target_ps=target_ps,
                        )
                        lock_student_ps_namespaces(
                            tenant_id=self.tenant_id,
                            ps_numbers=(
                                target_ps,
                                *(row[1] for row in predecessor_snapshot),
                            ),
                        )
                        _prepare_student_ps_inventory_claim(
                            tenant=self.tenant,
                            target_ps=target_ps,
                            predecessor_snapshot=predecessor_snapshot,
                        )
                        return super().save(*args, **kwargs)
                except StudentInventoryNamespaceChanged:
                    if attempt == max_attempts - 1:
                        raise
            raise AssertionError("unreachable student namespace retry state")

        update_fields = kwargs.get("update_fields")
        persists_ps_number = update_fields is None or "ps_number" in update_fields
        if not persists_ps_number:
            try:
                persisted_identity = Student.objects.values("user_id", "tenant_id").get(
                    pk=self.pk
                )
            except Student.DoesNotExist:
                return super().save(*args, **kwargs)
            if self.user_id != persisted_identity["user_id"]:
                raise ValueError("Student.user cannot be changed through Student.save().")
            if self.tenant_id != persisted_identity["tenant_id"]:
                raise ValueError("Student.tenant cannot be changed through Student.save().")
            return super().save(*args, **kwargs)

        try:
            persisted_identity = Student.objects.values(
                "user_id",
                "tenant_id",
                "ps_number",
            ).get(pk=self.pk)
        except Student.DoesNotExist:
            return super().save(*args, **kwargs)

        persisted_user_id = persisted_identity["user_id"]
        persisted_tenant_id = persisted_identity["tenant_id"]
        if self.user_id != persisted_user_id:
            raise ValueError("Student.user cannot be changed through Student.save().")
        if self.tenant_id != persisted_tenant_id:
            raise ValueError("Student.tenant cannot be changed through Student.save().")

        with transaction.atomic():
            user_model = self._meta.get_field("user").remote_field.model
            locked_user = user_model.objects.select_for_update().get(
                pk=persisted_user_id
            )
            old = (
                Student.objects.select_for_update()
                .only("ps_number", "user_id", "tenant_id", "deleted_at")
                .get(pk=self.pk)
            )
            if old.user_id != persisted_user_id or self.user_id != old.user_id:
                raise ValueError("Student account link changed while saving identity.")
            if old.tenant_id != persisted_tenant_id or self.tenant_id != old.tenant_id:
                raise ValueError("Student tenant changed while saving identity.")
            if old.ps_number != self.ps_number:
                from apps.core.models.user import user_internal_username

                new_username = user_internal_username(self.tenant, self.ps_number)
                if locked_user.username != new_username:
                    try:
                        with transaction.atomic():
                            locked_user.username = new_username
                            locked_user.save(update_fields=["username"])
                    except IntegrityError as exc:
                        raise StudentInventoryNamespaceConflict(
                            "student identity namespace is already claimed"
                        ) from exc
                self.user = locked_user
            predecessor_snapshot = (
                _student_ps_predecessor_snapshot(
                    tenant_id=persisted_tenant_id,
                    target_ps=self.ps_number,
                )
                if old.ps_number != self.ps_number
                else tuple()
            )
            lock_student_ps_namespaces(
                tenant_id=persisted_tenant_id,
                ps_numbers=(
                    old.ps_number,
                    self.ps_number,
                    *(row[1] for row in predecessor_snapshot),
                ),
            )

            if old.ps_number != self.ps_number:
                if old.deleted_at is None and not student_inventory_namespace_is_attributable(
                    tenant_id=persisted_tenant_id,
                    student_id=self.pk,
                    ps_number=old.ps_number,
                ):
                    raise StudentInventoryNamespaceConflict(
                        "student storage source namespace has ambiguous legacy ownership"
                    )
                _prepare_student_ps_inventory_claim(
                    tenant=self.tenant,
                    target_ps=self.ps_number,
                    predecessor_snapshot=predecessor_snapshot,
                    exclude_student_id=self.pk,
                )
                # 인벤토리 student_ps 연쇄 업데이트 (ps_number 변경 시 고아 방지)
                old_ps = old.ps_number
                new_ps = self.ps_number
                if old_ps and new_ps:
                    update_inventory_student_ps(
                        tenant=self.tenant,
                        old_ps=old_ps,
                        new_ps=new_ps,
                    )
            return super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class StudentCustomFieldDefinition(TimestampModel):
    TEXT = "text"
    NUMBER = "number"
    DATE = "date"
    SELECT = "select"
    FIELD_TYPE_CHOICES = (
        (TEXT, "텍스트"),
        (NUMBER, "숫자"),
        (DATE, "날짜"),
        (SELECT, "선택"),
    )

    objects = TenantQuerySet.as_manager()

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="student_custom_field_definitions",
        db_index=True,
    )
    key = models.CharField(
        max_length=32,
        default=generate_student_custom_field_key,
        editable=False,
        help_text="표시명 변경과 무관한 안정적인 학생 사용자 정의 필드 키",
    )
    label = models.CharField(max_length=50)
    field_type = models.CharField(
        max_length=12,
        choices=FIELD_TYPE_CHOICES,
        default=TEXT,
    )
    aliases = models.JSONField(
        default=list,
        blank=True,
        help_text="Excel 헤더 호환 별칭. 이전 표시명도 유지한다.",
    )
    options = models.JSONField(
        default=list,
        blank=True,
        help_text="select 타입에서 허용하는 값 목록",
    )
    position = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_student_custom_field_definitions",
    )

    class Meta:
        ordering = ["position", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "key"],
                name="uniq_student_custom_field_key_per_tenant",
            ),
            models.UniqueConstraint(
                fields=["tenant", "label"],
                name="uniq_student_custom_field_label_per_tenant",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "is_active", "position"]),
        ]

    def __str__(self):
        return f"{self.tenant_id}:{self.label}"


class Tag(models.Model):
    tenant = models.ForeignKey(
        "core.Tenant",
        on_delete=models.CASCADE,
        related_name="tags",
    )
    name = models.CharField(max_length=50)
    color = models.CharField(max_length=20, default="#000000")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "name"],
                name="uniq_tag_name_per_tenant",
            )
        ]

    def __str__(self):
        return self.name


class StudentTag(models.Model):
    student = models.ForeignKey(
        Student,
        on_delete=models.CASCADE,
        related_name="student_tags",
    )
    tag = models.ForeignKey(Tag, on_delete=models.CASCADE)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["student", "tag"],
                name="uniq_student_tag",
            )
        ]

    def __str__(self):
        return f"{self.student.name} - {self.tag.name}"


class StudentSupportSession(TimestampModel):
    """Server-revocable staff support access to one exact student account."""

    class EndReason(models.TextChoices):
        MANUAL = "manual", "학생 화면에서 종료"
        WINDOW_CLOSED = "window_closed", "지원 창 닫힘"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="student_support_sessions",
    )
    student = models.ForeignKey(
        Student,
        on_delete=models.CASCADE,
        related_name="support_sessions",
    )
    operator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    expires_at = models.DateTimeField(db_index=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    end_reason = models.CharField(
        max_length=24,
        choices=EndReason.choices,
        blank=True,
        default="",
    )

    class Meta:
        db_table = "student_support_session"
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["tenant", "student", "-created_at"],
                name="stu_sup_student_created_idx",
            ),
            models.Index(
                fields=["operator", "expires_at"],
                name="stu_sup_operator_exp_idx",
            ),
        ]

    def __str__(self):
        return f"support-session:{self.id} student={self.student_id}"


class StudentRegistrationRequest(TimestampModel):
    """
    학생 회원가입 신청 (로그인 페이지 셀프 등록).
    선생이 승인하면 Student + User + TenantMembership 생성 후 status=approved.
    """
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    STATUS_CHOICES = [(PENDING, "대기"), (APPROVED, "승인됨"), (REJECTED, "거절")]

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="student_registration_requests",
        db_index=True,
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=PENDING,
        db_index=True,
    )

    name = models.CharField(max_length=50)
    username = models.CharField(
        max_length=50,
        blank=True,
        default="",
        help_text="희망 로그인 아이디 (비어 있으면 승인 시 자동 부여)",
    )
    initial_password = models.CharField(max_length=256)  # 해시 저장 후 승인 시 User.password로 직접 이전
    initial_password_plain = models.CharField(
        max_length=128, blank=True, default="",
        help_text="Deprecated. 원문 비밀번호는 저장하지 않으며 기존 값은 마이그레이션으로 비운다.",
    )
    parent_phone = models.CharField(max_length=20)
    phone = models.CharField(max_length=20, null=True, blank=True)

    school_type = models.CharField(max_length=12, default="HIGH")
    elementary_school = models.CharField(max_length=100, null=True, blank=True)
    high_school = models.CharField(max_length=100, null=True, blank=True)
    middle_school = models.CharField(max_length=100, null=True, blank=True)
    high_school_class = models.CharField(max_length=100, null=True, blank=True)
    major = models.CharField(max_length=50, null=True, blank=True)
    grade = models.PositiveSmallIntegerField(null=True, blank=True)
    gender = models.CharField(max_length=1, null=True, blank=True)
    memo = models.TextField(null=True, blank=True)
    address = models.CharField(max_length=255, null=True, blank=True, help_text="주소 (선택)")
    origin_middle_school = models.CharField(
        max_length=100, null=True, blank=True,
        help_text="출신중학교 (고등학생 선택 입력)",
    )

    # 승인 시 생성된 학생 (승인 후에만 설정)
    student = models.ForeignKey(
        Student,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="registration_requests",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["tenant", "status"])]

    def __str__(self):
        return f"{self.name} ({self.get_status_display()})"
