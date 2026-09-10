# PATH: apps/domains/staffs/serializers.py
# 원칙: 테넌트별 완전 격리. 직원/User는 해당 테넌트 컨텍스트 내에서만 사용.
import logging
from datetime import datetime, timedelta

from rest_framework import serializers
from django.contrib.auth import get_user_model
from django.db import transaction, IntegrityError

from .models import (
    Staff,
    WorkType,
    StaffWorkType,
    WorkRecord,
    ExpenseRecord,
    WorkMonthLock,
    PayrollSnapshot,
)
from academy.adapters.db.django import repositories_staffs as staff_repo
from academy.adapters.db.django import repositories_teachers as teacher_repo
from academy.adapters.db.django import repositories_students as students_repo
from academy.adapters.db.django import repositories_core as core_repo

User = get_user_model()


class StaffWorkRangeQuerySerializer(serializers.Serializer):
    date_from = serializers.DateField(required=True)
    date_to = serializers.DateField(required=True)


class StaffWorkStartRequestSerializer(serializers.Serializer):
    work_type = serializers.IntegerField(required=True, min_value=1)


class StaffWorkEndRequestSerializer(serializers.Serializer):
    meal_minutes = serializers.IntegerField(required=False, min_value=0)
    adjustment_amount = serializers.IntegerField(required=False)


class StaffWorkCurrentStatusSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=["OFF", "WORKING", "BREAK"])
    work_record_id = serializers.IntegerField(required=False)
    date = serializers.DateField(required=False)
    started_at = serializers.TimeField(required=False)
    work_type = serializers.IntegerField(required=False)
    work_type_name = serializers.CharField(required=False)
    hourly_wage = serializers.IntegerField(required=False, allow_null=True)
    break_minutes = serializers.IntegerField(required=False)
    break_total_seconds = serializers.IntegerField(required=False)
    break_started_at = serializers.DateTimeField(required=False)


class CurrentlyWorkingStaffSerializer(serializers.Serializer):
    staff_id = serializers.IntegerField()
    staff_name = serializers.CharField()
    role = serializers.ChoiceField(choices=["owner", "TEACHER", "ASSISTANT"])
    date = serializers.DateField(required=False)
    started_at = serializers.TimeField(required=False)
    work_type = serializers.IntegerField(required=False)
    work_type_name = serializers.CharField(required=False)
    break_minutes = serializers.IntegerField(required=False)
    break_total_seconds = serializers.IntegerField(required=False)
    break_started_at = serializers.DateTimeField(required=False)


class StaffWorkSummarySerializer(serializers.Serializer):
    staff_id = serializers.IntegerField()
    work_hours = serializers.FloatField()
    work_amount = serializers.IntegerField()
    expense_amount = serializers.IntegerField()
    total_amount = serializers.IntegerField()
    reference_business_income_tax = serializers.IntegerField()
    reference_local_income_tax = serializers.IntegerField()
    reference_deduction_total = serializers.IntegerField()
    reference_net_work_amount = serializers.IntegerField()
    reference_transfer_amount = serializers.IntegerField()


class StaffPayrollOverviewQuerySerializer(serializers.Serializer):
    year = serializers.IntegerField(min_value=2020, max_value=2100)
    month = serializers.IntegerField(min_value=1, max_value=12)


class StaffPayrollWorkTypeBreakdownSerializer(serializers.Serializer):
    work_type_id = serializers.IntegerField(allow_null=True)
    work_type_name = serializers.CharField(allow_null=True)
    color = serializers.CharField(allow_null=True)
    record_count = serializers.IntegerField()
    work_hours = serializers.FloatField()
    work_amount = serializers.IntegerField()


class StaffPayrollOverviewRowSerializer(serializers.Serializer):
    staff_id = serializers.IntegerField()
    name = serializers.CharField()
    position = serializers.CharField()
    position_label = serializers.CharField()
    account_role = serializers.CharField()
    is_active = serializers.BooleanField()
    can_manage_staff = serializers.BooleanField()
    pay_type = serializers.CharField()
    work_hours = serializers.FloatField()
    work_amount = serializers.IntegerField()
    approved_expense_amount = serializers.IntegerField()
    pending_expense_amount = serializers.IntegerField()
    pending_expense_count = serializers.IntegerField()
    total_amount = serializers.IntegerField()
    reference_business_income_tax = serializers.IntegerField()
    reference_local_income_tax = serializers.IntegerField()
    reference_deduction_total = serializers.IntegerField()
    reference_net_work_amount = serializers.IntegerField()
    reference_transfer_amount = serializers.IntegerField()
    work_type_breakdown = StaffPayrollWorkTypeBreakdownSerializer(many=True)
    open_work_record_count = serializers.IntegerField()
    incomplete_work_record_count = serializers.IntegerField()
    duplicate_work_record_count = serializers.IntegerField()
    abnormal_long_work_record_count = serializers.IntegerField()
    manually_edited_work_record_count = serializers.IntegerField()
    advisory_issue_count = serializers.IntegerField()
    assigned_work_type_count = serializers.IntegerField()
    settlement_status = serializers.CharField()
    can_close = serializers.BooleanField()


class StaffPayrollOverviewTotalsSerializer(serializers.Serializer):
    staff_count = serializers.IntegerField()
    work_hours = serializers.FloatField()
    work_amount = serializers.IntegerField()
    approved_expense_amount = serializers.IntegerField()
    pending_expense_amount = serializers.IntegerField()
    total_amount = serializers.IntegerField()
    reference_business_income_tax = serializers.IntegerField()
    reference_local_income_tax = serializers.IntegerField()
    reference_deduction_total = serializers.IntegerField()
    reference_net_work_amount = serializers.IntegerField()
    reference_transfer_amount = serializers.IntegerField()
    advisory_issue_count = serializers.IntegerField()
    needs_review_count = serializers.IntegerField()
    closed_count = serializers.IntegerField()
    work_type_breakdown = StaffPayrollWorkTypeBreakdownSerializer(many=True)


class StaffPayrollOverviewSerializer(serializers.Serializer):
    year = serializers.IntegerField()
    month = serializers.IntegerField()
    date_from = serializers.DateField()
    date_to = serializers.DateField()
    totals = StaffPayrollOverviewTotalsSerializer()
    rows = StaffPayrollOverviewRowSerializer(many=True)


# ---------------------------
# WorkType
# ---------------------------

class WorkTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkType
        fields = [
            "id",
            "name",
            "base_hourly_wage",
            "color",
            "description",
            "is_active",
            "created_at",
            "updated_at",
        ]
        ref_name = "StaffWorkTypeDefinition"


# ---------------------------
# StaffWorkType
# ---------------------------

class StaffWorkTypeSerializer(serializers.ModelSerializer):
    staff = serializers.PrimaryKeyRelatedField(
        queryset=Staff.objects.none(),
    )
    work_type = WorkTypeSerializer(read_only=True)
    work_type_id = serializers.PrimaryKeyRelatedField(
        source="work_type",
        queryset=staff_repo.work_type_empty_queryset(),
        write_only=True,
    )
    effective_hourly_wage = serializers.IntegerField(read_only=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = self.context.get("request") if self.context else None
        tenant = getattr(request, "tenant", None) if request else None
        self.fields["staff"].queryset = (
            staff_repo.staff_queryset_tenant(tenant) if tenant else Staff.objects.none()
        )
        self.fields["work_type_id"].queryset = (
            staff_repo.work_type_queryset_tenant(tenant) if tenant else staff_repo.work_type_empty_queryset()
        )

    class Meta:
        model = StaffWorkType
        fields = [
            "id",
            "staff",
            "work_type",
            "work_type_id",
            "hourly_wage",
            "effective_hourly_wage",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]
        ref_name = "StaffWorkType"


# ---------------------------
# Staff (LIST / DETAIL)
# ---------------------------

class StaffListSerializer(serializers.ModelSerializer):
    staff_work_types = StaffWorkTypeSerializer(many=True, read_only=True)
    role = serializers.SerializerMethodField()
    account_role = serializers.SerializerMethodField()
    position_label = serializers.CharField(
        source="get_position_display",
        read_only=True,
    )
    can_manage_staff = serializers.SerializerMethodField()
    profile_photo_url = serializers.SerializerMethodField()

    class Meta:
        model = Staff
        fields = [
            "id",
            "name",
            "phone",
            "profile_photo_url",
            "is_active",
            "is_manager",
            "can_manage_staff",
            "pay_type",
            "position",
            "position_label",
            "role",
            "account_role",
            "staff_work_types",
            "created_at",
            "updated_at",
        ]
        ref_name = "StaffList"

    def get_profile_photo_url(self, obj):
        if not obj.profile_photo:
            return None
        request = self.context.get("request")
        if request:
            return request.build_absolute_uri(obj.profile_photo.url)
        return obj.profile_photo.url

    def get_role(self, obj):
        # Account-backed staff use TenantMembership as the role SSOT.
        # Legacy rows without an account retain the name/phone compatibility lookup.
        membership_roles = self.context.get("membership_roles")
        if membership_roles is not None and getattr(obj, "user_id", None):
            membership_role = membership_roles.get(obj.user_id)
            if membership_role == "teacher":
                return "TEACHER"
            if membership_role in ("staff", "admin"):
                return "ASSISTANT"
            if membership_role == "owner":
                return "OWNER"
        teacher_keys = self.context.get("teacher_keys")
        if teacher_keys is not None:
            if (obj.name, obj.phone or "") in teacher_keys:
                return "TEACHER"
            return "ASSISTANT"
        # 컨텍스트가 없는 경우(단독 사용): 안전한 폴백.
        if getattr(obj, "user_id", None):
            if core_repo.membership_exists_staff(obj.tenant, obj.user, staff_roles=("owner",)):
                return "OWNER"
        if teacher_repo.teacher_exists_tenant_name_phone(obj.tenant, obj.name, obj.phone or ""):
            return "TEACHER"
        return "ASSISTANT"

    def get_account_role(self, obj) -> str:
        cached = getattr(obj, "_staff_account_role_code", None)
        if cached is not None:
            return cached
        membership_roles = self.context.get("membership_roles")
        membership_role = (
            membership_roles.get(obj.user_id)
            if membership_roles is not None and getattr(obj, "user_id", None)
            else None
        )
        if membership_role is None and getattr(obj, "user_id", None):
            membership = core_repo.membership_get(obj.tenant, obj.user)
            membership_role = membership.role if membership else None
        account_role = {
            "owner": "OWNER",
            "admin": "ADMIN",
            "teacher": "TEACHER",
            "staff": "STAFF",
        }.get(membership_role, "NONE")
        obj._staff_account_role_code = account_role
        return account_role

    def get_can_manage_staff(self, obj) -> bool:
        account_role = self.get_account_role(obj)
        return account_role in ("OWNER", "ADMIN")


class StaffDetailSerializer(serializers.ModelSerializer):
    staff_work_types = StaffWorkTypeSerializer(many=True, read_only=True)
    role = serializers.SerializerMethodField()
    account_role = serializers.SerializerMethodField()
    position_label = serializers.CharField(
        source="get_position_display",
        read_only=True,
    )
    can_manage_staff = serializers.SerializerMethodField()
    profile_photo_url = serializers.SerializerMethodField()

    user_username = serializers.SerializerMethodField()

    def get_user_username(self, obj):
        if not getattr(obj, "user", None):
            return ""
        from apps.core.models.user import user_display_username
        return user_display_username(obj.user)
    user_is_staff = serializers.BooleanField(
        source="user.is_staff",
        read_only=True,
    )

    class Meta:
        model = Staff
        fields = [
            "id",
            "user",
            "user_username",
            "user_is_staff",
            "name",
            "phone",
            "profile_photo_url",
            "is_active",
            "is_manager",
            "can_manage_staff",
            "pay_type",
            "position",
            "position_label",
            "role",
            "account_role",
            "staff_work_types",
            "created_at",
            "updated_at",
        ]
        ref_name = "StaffDetail"

    def get_profile_photo_url(self, obj):
        if not obj.profile_photo:
            return None
        request = self.context.get("request")
        if request:
            return request.build_absolute_uri(obj.profile_photo.url)
        return obj.profile_photo.url

    def get_role(self, obj):
        if getattr(obj, "user_id", None):
            membership = core_repo.membership_get_full(obj.tenant, obj.user)
            if membership and membership.role == "owner":
                return "OWNER"
            if membership and membership.role == "teacher":
                return "TEACHER"
            if membership and membership.role in ("staff", "admin"):
                return "ASSISTANT"
        if teacher_repo.teacher_exists_tenant_name_phone(obj.tenant, obj.name, obj.phone or ""):
            return "TEACHER"
        return "ASSISTANT"

    def get_account_role(self, obj) -> str:
        cached = getattr(obj, "_staff_account_role_code", None)
        if cached is not None:
            return cached
        membership = (
            core_repo.membership_get(obj.tenant, obj.user)
            if getattr(obj, "user_id", None)
            else None
        )
        account_role = {
            "owner": "OWNER",
            "admin": "ADMIN",
            "teacher": "TEACHER",
            "staff": "STAFF",
        }.get(membership.role if membership else None, "NONE")
        obj._staff_account_role_code = account_role
        return account_role

    def get_can_manage_staff(self, obj) -> bool:
        account_role = self.get_account_role(obj)
        return account_role in ("OWNER", "ADMIN")


# ======================================================
# 🔥 Staff CREATE / UPDATE / DELETE (ROLE 포함)
# ======================================================

class StaffCreateUpdateSerializer(serializers.ModelSerializer):
    is_manager = serializers.BooleanField(read_only=True)
    role = serializers.ChoiceField(
        choices=[("TEACHER", "강사"), ("ASSISTANT", "조교")],
        write_only=True,
        required=True,
    )
    username = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=150)
    password = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=128)

    def validate_name(self, value):
        normalized = (value or "").strip()
        if not normalized:
            raise serializers.ValidationError("이름을 입력해 주세요.")
        if len(normalized) > 50:
            raise serializers.ValidationError("이름은 50자 이내로 입력해 주세요.")
        return normalized

    def validate_phone(self, value):
        normalized = (value or "").replace("-", "").replace(" ", "").strip()
        if normalized and (not normalized.isdigit() or not 9 <= len(normalized) <= 11):
            raise serializers.ValidationError(
                "전화번호는 숫자 9~11자리로 입력해 주세요."
            )
        return normalized

    class Meta:
        model = Staff
        fields = [
            "id",
            "user",
            "username",
            "password",
            "name",
            "phone",
            "is_active",
            "is_manager",
            "pay_type",
            "position",
            "role",
        ]
        read_only_fields = ["id", "user"]
        ref_name = "StaffWrite"

    def validate(self, attrs):
        initial = getattr(self, "initial_data", {})
        if "user" in initial:
            raise serializers.ValidationError(
                {"user": "직원 계정은 직접 연결할 수 없습니다. 아이디/초기 비밀번호로 생성해 주세요."}
            )
        if "is_manager" in initial:
            raise serializers.ValidationError(
                {
                    "is_manager": (
                        "직원관리 권한은 계정 역할로 결정됩니다. "
                        "대표 또는 관리자 역할을 사용해 주세요."
                    )
                }
            )
        username = str(initial.get("username") or "").strip()
        password = str(initial.get("password") or "")
        if bool(username) != bool(password):
            raise serializers.ValidationError(
                {
                    "username": "로그인 아이디와 초기 비밀번호는 함께 입력하거나 둘 다 비워 주세요.",
                    "password": "로그인 아이디와 초기 비밀번호는 함께 입력하거나 둘 다 비워 주세요.",
                }
            )
        if password and len(password.strip()) < 4:
            raise serializers.ValidationError(
                {"password": "초기 비밀번호는 4자 이상이어야 합니다."}
            )
        requested_pay_type = attrs.get("pay_type")
        current_pay_type = getattr(self.instance, "pay_type", None)
        if requested_pay_type == "MONTHLY" and current_pay_type != "MONTHLY":
            raise serializers.ValidationError(
                {
                    "pay_type": (
                        "월급 정산은 기본급·일할·공제 정책이 설정되지 않아 선택할 수 없습니다. "
                        "현재는 시급 정산만 지원합니다."
                    )
                }
            )
        if self.instance is None and "position" not in attrs:
            attrs["position"] = (
                "INSTRUCTOR"
                if attrs.get("role") == "TEACHER"
                else "ASSISTANT"
            )
        return attrs

    # =========================
    # CREATE
    # =========================
    def create(self, validated_data):
        request = self.context.get("request")
        tenant = getattr(request, "tenant", None) if request else None
        if not tenant:
            raise serializers.ValidationError(
                {"detail": "테넌트를 확인할 수 없습니다. 요청 헤더 또는 접속 주소를 확인해 주세요."}
            )

        role = validated_data.pop("role")
        username = (validated_data.pop("username", None) or "").strip()
        password = (validated_data.pop("password", None) or "")

        try:
            with transaction.atomic():
                user = None
                if username and password:
                    user = students_repo.user_create_user(
                        username=username,
                        password=password,
                        tenant=tenant,
                        name=validated_data.get("name") or username,
                        phone=validated_data.get("phone") or "",
                    )
                    core_repo.membership_ensure_active(
                        tenant=tenant,
                        user=user,
                        role="teacher" if role == "TEACHER" else "staff",
                        protected_existing_roles=("owner", "admin"),
                    )
                    validated_data["user"] = user

                validated_data["tenant"] = tenant
                staff = super().create(validated_data)

                if role == "TEACHER":
                    self._create_teacher(staff)
                    self._grant_user_staff_permission(staff)

                return staff
        except IntegrityError as e:
            err_msg = str(e).lower()
            if "username" in err_msg or ("unique" in err_msg and "username" in err_msg):
                raise serializers.ValidationError(
                    {"username": "이미 사용 중인 로그인 아이디입니다."}
                )
            if "phone" in err_msg or "uniq_staff_phone" in err_msg or "uniq_teacher_phone" in err_msg:
                raise serializers.ValidationError(
                    {"phone": "이미 등록된 전화번호입니다."}
                )
            raise serializers.ValidationError(
                {"detail": "이미 등록된 정보와 충돌합니다. 로그인 아이디·전화번호를 확인해 주세요."}
            )
        except ValueError as e:
            raise serializers.ValidationError({"detail": str(e)})
        except Exception as e:
            logging.exception("Staff create failed: %s", e)
            raise serializers.ValidationError(
                {"detail": "직원 등록 중 오류가 발생했습니다. 입력값을 확인해 주세요."}
            )

    # =========================
    # UPDATE (Teacher + TenantMembership lifecycle synchronization)
    # =========================
    def update(self, instance, validated_data):
        requested_role = validated_data.pop("role", None)
        validated_data.pop("username", None)
        validated_data.pop("password", None)

        try:
            with transaction.atomic():
                instance = Staff.objects.select_for_update().get(pk=instance.pk)
                # All lifecycle decisions use the locked row so clock-in,
                # concurrent profile edits, and offboarding cannot interleave.
                old_name = instance.name
                old_phone = instance.phone or ""
                is_active_before = instance.is_active
                wants_reactivation = (
                    not is_active_before
                    and validated_data.get("is_active") is True
                )
                wants_deactivation = (
                    is_active_before
                    and validated_data.get("is_active") is False
                )
                membership = (
                    core_repo.membership_get_for_update(
                        instance.tenant,
                        instance.user,
                    )
                    if instance.user_id
                    else None
                )
                legacy_teacher_count = (
                    0
                    if membership is not None
                    else teacher_repo.teacher_count_tenant_name_phone(
                        instance.tenant,
                        old_name,
                        old_phone,
                    )
                )
                if legacy_teacher_count > 1 and (
                    requested_role is not None
                    or "name" in validated_data
                    or "phone" in validated_data
                    or "is_active" in validated_data
                ):
                    raise serializers.ValidationError(
                        {
                            "detail": (
                                "동명이인 강사 기록이 여러 건이라 자동 동기화할 수 없습니다. "
                                "계정을 연결하거나 중복 강사 기록을 먼저 정리해 주세요."
                            )
                        }
                    )
                if membership and membership.role in ("owner", "admin") and (
                    validated_data.get("is_active") is False
                    or wants_reactivation
                ):
                    raise serializers.ValidationError(
                        {
                            "is_active": (
                                "대표/관리자 계정은 직원 화면에서 비활성화하거나 "
                                "재활성화할 수 없습니다."
                            )
                        }
                    )
                if wants_reactivation and requested_role is None:
                    raise serializers.ValidationError(
                        {
                            "role": (
                                "재활성화할 직원 역할(TEACHER 또는 ASSISTANT)을 "
                                "명시해 주세요."
                            )
                        }
                    )
                if requested_role is not None and wants_deactivation:
                    raise serializers.ValidationError(
                        {"role": "비활성화와 역할 변경을 동시에 요청할 수 없습니다."}
                    )
                if (
                    membership
                    and membership.role == "admin"
                    and requested_role not in (None, "ASSISTANT")
                ):
                    raise serializers.ValidationError(
                        {"role": "관리자 역할은 직원 화면에서 변경할 수 없습니다."}
                    )
                if (
                    requested_role is not None
                    and not is_active_before
                    and not wants_reactivation
                ):
                    raise serializers.ValidationError(
                        {"role": "비활성 직원의 역할은 재활성화 요청과 함께 지정해 주세요."}
                    )
                resulting_is_active = validated_data.get(
                    "is_active",
                    instance.is_active,
                )
                if wants_deactivation and staff_repo.work_record_open_exists(instance):
                    raise serializers.ValidationError(
                        {
                            "is_active": (
                                "진행 중인 근무를 먼저 퇴근 처리한 뒤 퇴사 처리해 주세요."
                            )
                        }
                    )
                if wants_deactivation:
                    validated_data["is_manager"] = False
                was_teacher = (
                    membership.role == "teacher"
                    if membership is not None
                    else legacy_teacher_count == 1
                )
                staff = super().update(instance, validated_data)

                new_name = staff.name
                new_phone = staff.phone or ""
                name_or_phone_changed = (old_name != new_name) or (old_phone != new_phone)

                # 1) 이름/전화 변경 → Teacher 레코드 동기화 (old 값으로 찾아서 new 값으로 업데이트)
                if name_or_phone_changed and was_teacher:
                    teacher_repo.teacher_update_name_phone(
                        staff.tenant, old_name, old_phone, new_name, new_phone,
                    )
                if name_or_phone_changed and staff.user_id:
                    staff.user.name = new_name
                    staff.user.phone = new_phone
                    staff.user.save(update_fields=["name", "phone"])

                # 2) 비활성화 → Teacher도 비활성화 (이름/전화 동기화 후이므로 new 값 사용)
                if is_active_before and staff.is_active is False:
                    if was_teacher:
                        teacher_repo.teacher_update_is_active_by_name_phone(
                            staff.tenant, new_name, new_phone, False,
                        )
                    if staff.user_id:
                        from apps.core.services.tenant_access import (
                            TenantAccessMutationError,
                            deactivate_tenant_membership,
                        )
                        try:
                            deactivate_tenant_membership(
                                user=staff.user,
                                tenant=staff.tenant,
                                allowed_roles=("teacher", "staff"),
                            )
                        except TenantAccessMutationError as exc:
                            raise serializers.ValidationError(
                                {"is_active": str(exc)}
                            ) from exc

                # 3) 재활성화 → Teacher도 활성화
                if not is_active_before and staff.is_active is True:
                    role = (
                        "teacher" if requested_role == "TEACHER" else
                        "staff"
                    )
                    if role == "teacher":
                        teacher_repo.teacher_ensure_active_by_name_phone(
                            staff.tenant, new_name, new_phone,
                        )
                    else:
                        teacher_repo.teacher_update_is_active_by_name_phone(
                            staff.tenant, new_name, new_phone, False,
                        )
                    if staff.user_id:
                        core_repo.membership_ensure_active(
                            tenant=staff.tenant,
                            user=staff.user,
                            role=role,
                            protected_existing_roles=("owner", "admin"),
                        )
                        from apps.core.services.tenant_access import reconcile_user_tenant_access
                        reconcile_user_tenant_access(staff.user)

                # Active role edits are a real lifecycle change, never a silent
                # no-op. Keep Teacher profile and membership role atomic.
                if is_active_before and staff.is_active and requested_role is not None:
                    if membership and membership.role == "admin":
                        # 목록의 ASSISTANT 표시는 직원관리 화면 분류일 뿐,
                        # admin membership을 staff로 강등하라는 요청이 아니다.
                        return staff
                    role = "teacher" if requested_role == "TEACHER" else "staff"
                    if membership and membership.role == "owner":
                        raise serializers.ValidationError(
                            {"role": "대표/관리자 역할은 직원 화면에서 변경할 수 없습니다."}
                        )
                    if role == "teacher":
                        teacher_repo.teacher_ensure_active_by_name_phone(
                            staff.tenant, new_name, new_phone,
                        )
                    else:
                        teacher_repo.teacher_update_is_active_by_name_phone(
                            staff.tenant, new_name, new_phone, False,
                        )
                    if staff.user_id:
                        core_repo.membership_ensure_active(
                            tenant=staff.tenant,
                            user=staff.user,
                            role=role,
                            protected_existing_roles=("owner", "admin"),
                        )
                        from apps.core.services.tenant_access import reconcile_user_tenant_access
                        reconcile_user_tenant_access(staff.user)

                return staff
        except IntegrityError as e:
            err_msg = str(e).lower()
            if "phone" in err_msg or "uniq_staff_phone" in err_msg or "uniq_teacher_phone" in err_msg:
                raise serializers.ValidationError(
                    {"phone": "이미 등록된 전화번호입니다."}
                )
            raise serializers.ValidationError(
                {"detail": "정보 수정 중 충돌이 발생했습니다. 입력값을 확인해 주세요."}
            )
        except ValueError as e:
            raise serializers.ValidationError({"role": str(e)}) from e

    # =========================
    # DELETE (Staff + Teacher + User)
    # =========================
    def delete(self, instance):
        membership = (
            core_repo.membership_get_full(instance.tenant, instance.user)
            if instance.user_id
            else None
        )
        if membership and membership.is_active and membership.role in ("owner", "admin"):
            raise serializers.ValidationError("대표/관리자는 직원 화면에서 삭제할 수 없습니다.")

        user = instance.user
        tenant = instance.tenant

        with transaction.atomic():
            instance = Staff.objects.select_for_update().get(
                tenant=tenant,
                pk=instance.pk,
            )
            protected_history = {
                "근무기록": instance.work_records.exists(),
                "비용": instance.expense_records.exists(),
                "월마감": instance.work_month_locks.exists(),
                "급여 스냅샷": instance.payroll_snapshots.exists(),
            }
            existing_history = [
                label for label, exists in protected_history.items() if exists
            ]
            if existing_history:
                raise serializers.ValidationError(
                    {
                        "detail": (
                            f"{', '.join(existing_history)} 이력이 있어 삭제할 수 없습니다. "
                            "직원 수정에서 퇴사 처리해 주세요."
                        )
                    }
                )
            membership = (
                core_repo.membership_get_full(tenant, user)
                if user
                else None
            )
            legacy_teacher_count = (
                0
                if membership is not None
                else teacher_repo.teacher_count_tenant_name_phone(
                    tenant,
                    instance.name,
                    instance.phone or "",
                )
            )
            if legacy_teacher_count > 1:
                raise serializers.ValidationError(
                    {
                        "detail": (
                            "동명이인 강사 기록이 여러 건이라 자동 삭제할 수 없습니다. "
                            "중복 강사 기록을 먼저 정리해 주세요."
                        )
                    }
                )
            is_teacher = (
                membership.role == "teacher"
                if membership is not None
                else legacy_teacher_count == 1
            )
            if is_teacher:
                teacher_repo.teacher_delete_by_name_phone(
                    tenant,
                    instance.name,
                    instance.phone or "",
                )
            instance.delete()
            if user:
                # User를 hard-delete하지 않고 비활성화 + 해당 테넌트 멤버십만 제거.
                # hard-delete는 Student, Attendance 등을 cascade로 파괴할 수 있으므로 절대 금지.
                # User.is_active/is_staff 는 전역 속성이므로, 다른 테넌트 멤버십이 남아 있거나
                # User.tenant 가 이 Staff의 tenant가 아니면 절대 변경하지 않는다.
                from apps.core.services.tenant_access import deactivate_tenant_membership
                deactivate_tenant_membership(
                    user=user,
                    tenant=tenant,
                    allowed_roles=("teacher", "staff"),
                )

    # =========================
    # Helpers
    # =========================
    def _create_teacher(self, staff: Staff):
        teacher_repo.teacher_create(
            staff.tenant,
            staff.name,
            staff.phone or "",
            is_active=True,
        )

    def _grant_user_staff_permission(self, staff: Staff):
        if not staff.user:
            return

        user: User = staff.user
        if not user.is_staff:
            user.is_staff = True
            user.save(update_fields=["is_staff"])


# ---------------------------
# WorkRecord
# ---------------------------

class WorkRecordSerializer(serializers.ModelSerializer):
    staff_name = serializers.CharField(source="staff.name", read_only=True)
    work_type_name = serializers.CharField(source="work_type.name", read_only=True)

    class Meta:
        model = WorkRecord
        fields = [
            "id",
            "staff",
            "staff_name",
            "work_type",
            "work_type_name",
            "date",
            "start_time",
            "end_time",
            "break_minutes",
            "meal_minutes",
            "work_hours",
            "amount",
            "adjustment_amount",
            "resolved_hourly_wage",
            "is_manually_edited",
            "memo",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["resolved_hourly_wage", "is_manually_edited", "created_at", "updated_at"]
        ref_name = "StaffWorkRecord"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = self.context.get("request")
        if request and hasattr(request, "tenant") and request.tenant:
            tenant = request.tenant
            self.fields["staff"].queryset = Staff.objects.filter(tenant=tenant)
            self.fields["work_type"].queryset = WorkType.objects.filter(tenant=tenant)
        else:
            self.fields["staff"].queryset = Staff.objects.none()
            self.fields["work_type"].queryset = WorkType.objects.none()

    def validate(self, attrs):
        attrs = super().validate(attrs)
        start_time = attrs.get(
            "start_time",
            getattr(self.instance, "start_time", None),
        )
        end_time = attrs.get(
            "end_time",
            getattr(self.instance, "end_time", None),
        )
        date = attrs.get("date", getattr(self.instance, "date", None))
        break_minutes = attrs.get(
            "break_minutes",
            getattr(self.instance, "break_minutes", 0),
        ) or 0
        meal_minutes = attrs.get(
            "meal_minutes",
            getattr(self.instance, "meal_minutes", 0),
        ) or 0

        if self.instance is None:
            work_type = attrs.get("work_type")
            if work_type is not None and not work_type.is_active:
                raise serializers.ValidationError(
                    {"work_type": "비활성 근무유형으로 새 근무기록을 만들 수 없습니다."}
                )

        if start_time and end_time and date:
            start_dt = datetime.combine(date, start_time)
            end_dt = datetime.combine(date, end_time)
            if end_dt == start_dt:
                raise serializers.ValidationError(
                    {"end_time": "종료 시간은 시작 시간과 같을 수 없습니다."}
                )
            if end_dt < start_dt:
                end_dt += timedelta(days=1)
            worked_minutes = int((end_dt - start_dt).total_seconds() // 60)
            if break_minutes + meal_minutes >= worked_minutes:
                raise serializers.ValidationError(
                    {
                        "break_minutes": (
                            "휴게·식사시간 합계는 전체 근무시간보다 짧아야 합니다."
                        )
                    }
                )

        initial_keys = set(getattr(self, "initial_data", {}).keys())
        override_keys = {"work_hours", "amount"} & initial_keys
        if self.instance is not None and override_keys and len(override_keys) != 2:
            raise serializers.ValidationError(
                {
                    "work_hours": "근무시간과 금액을 수동 수정할 때는 둘 다 입력해 주세요.",
                    "amount": "근무시간과 금액을 수동 수정할 때는 둘 다 입력해 주세요.",
                }
            )
        return attrs


# ---------------------------
# ExpenseRecord
# ---------------------------

class ExpenseRecordSerializer(serializers.ModelSerializer):
    staff_name = serializers.CharField(source="staff.name", read_only=True)
    approved_by_name = serializers.CharField(
        source="approved_by.username",
        read_only=True,
    )

    class Meta:
        model = ExpenseRecord
        fields = [
            "id",
            "staff",
            "staff_name",
            "date",
            "title",
            "amount",
            "memo",
            "status",
            "approved_at",
            "approved_by",
            "approved_by_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "approved_at",
            "approved_by",
            "created_at",
            "updated_at",
        ]
        ref_name = "StaffExpenseRecord"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = self.context.get("request")
        if request and hasattr(request, "tenant") and request.tenant:
            tenant = request.tenant
            self.fields["staff"].queryset = Staff.objects.filter(tenant=tenant)

    def validate_amount(self, value):
        if value <= 0:
            raise serializers.ValidationError("금액은 1원 이상이어야 합니다.")
        return value


# ---------------------------
# WorkMonthLock / Payroll
# ---------------------------

class WorkMonthLockSerializer(serializers.ModelSerializer):
    staff_name = serializers.CharField(source="staff.name", read_only=True)
    locked_by_name = serializers.CharField(source="locked_by.username", read_only=True)

    class Meta:
        model = WorkMonthLock
        fields = [
            "id",
            "staff",
            "staff_name",
            "year",
            "month",
            "is_locked",
            "locked_by",
            "locked_by_name",
            "created_at",
        ]
        read_only_fields = ["locked_by", "created_at"]
        ref_name = "WorkMonthLock"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = self.context.get("request")
        tenant = getattr(request, "tenant", None) if request else None
        self.fields["staff"].queryset = Staff.objects.filter(tenant=tenant) if tenant else Staff.objects.none()

    def validate_staff(self, staff):
        request = self.context.get("request")
        tenant = getattr(request, "tenant", None) if request else None
        if not tenant or staff.tenant_id != tenant.id:
            raise serializers.ValidationError("해당 직원을 찾을 수 없습니다.")
        return staff


class PayrollSnapshotSerializer(serializers.ModelSerializer):
    generated_by_name = serializers.CharField(source="generated_by.username", read_only=True)
    staff_name = serializers.SerializerMethodField()

    def get_staff_name(self, obj):
        # Additive migration keeps legacy rows untouched; old rows safely fall
        # back to the current name, while new snapshots preserve the close-time name.
        return obj.staff_name or obj.staff.name

    class Meta:
        model = PayrollSnapshot
        fields = [
            "id",
            "staff",
            "staff_name",
            "year",
            "month",
            "work_hours",
            "work_amount",
            "approved_expense_amount",
            "total_amount",
            "generated_by",
            "generated_by_name",
            "created_at",
        ]
        read_only_fields = fields
        ref_name = "PayrollSnapshot"
