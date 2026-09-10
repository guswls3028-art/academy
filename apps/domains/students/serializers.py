# PATH: apps/domains/students/serializers.py

from rest_framework import serializers
from drf_spectacular.utils import extend_schema_field

from apps.core.models import TenantMembership
from apps.core.services.login_identifier import normalize_login_identifier
from apps.domains.students.models import (
    Student,
    StudentCustomFieldDefinition,
    StudentRegistrationRequest,
    Tag,
)
from apps.domains.students.services.custom_fields import (
    MAX_CUSTOM_FIELDS_PER_TENANT,
    MAX_OPTIONS,
    StudentCustomFieldError,
    normalize_custom_field_values,
    normalize_string_list,
    validate_definition_headers,
)
from apps.domains.students.services.identity import (
    StudentIdentityError,
    canonical_student_phone,
    derive_student_omr_code,
    normalize_student_phone,
    phone_digits,
    resolve_student_login_id,
    student_login_id_taken,
)
from apps.support.students.serializer_dependencies import (
    clinic_highlight_map_for_enrollments,
    get_enrollment_model,
)

Enrollment = get_enrollment_model()


def _student_account_state(student: Student) -> str:
    if student.deleted_at is not None:
        return "DELETED"
    if student.user_id is None:
        return "UNLINKED"

    annotated_access = getattr(student, "_account_access_active", None)
    if annotated_access is not None:
        return "ACTIVE" if annotated_access else "INACTIVE"

    user = student.user
    if not user.is_active:
        return "INACTIVE"
    has_access = TenantMembership.objects.filter(
        tenant_id=student.tenant_id,
        user_id=student.user_id,
        role="student",
        is_active=True,
    ).exists()
    return "ACTIVE" if has_access else "INACTIVE"


def _request_tenant(serializer):
    request = serializer.context.get("request")
    tenant = getattr(request, "tenant", None) if request else None
    if tenant is None:
        raise serializers.ValidationError("Tenant가 resolve되지 않았습니다.")
    return tenant


def _validated_custom_field_values(serializer, value):
    try:
        return normalize_custom_field_values(
            tenant=_request_tenant(serializer),
            values=value,
        )
    except StudentCustomFieldError as exc:
        raise serializers.ValidationError(exc.detail) from exc


class StudentCustomFieldDefinitionSerializer(serializers.ModelSerializer):
    class Meta:
        model = StudentCustomFieldDefinition
        fields = [
            "id",
            "key",
            "label",
            "field_type",
            "aliases",
            "options",
            "position",
            "is_active",
            "created_by",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "key", "created_by", "created_at", "updated_at"]

    def validate(self, attrs):
        tenant = _request_tenant(self)
        instance = self.instance
        label = str(attrs.get("label", getattr(instance, "label", "")) or "").strip()
        if not label:
            raise serializers.ValidationError({"label": "표시명은 필수입니다."})

        try:
            aliases = normalize_string_list(
                attrs.get("aliases", getattr(instance, "aliases", [])),
                field_name="aliases",
                max_items=20,
            )
            field_type = attrs.get(
                "field_type",
                getattr(instance, "field_type", StudentCustomFieldDefinition.TEXT),
            )
            options = normalize_string_list(
                attrs.get("options", getattr(instance, "options", [])),
                field_name="options",
                max_items=MAX_OPTIONS,
            )
            if instance is not None and label != instance.label:
                aliases = normalize_string_list(
                    [*aliases, instance.label],
                    field_name="aliases",
                    max_items=20,
                )
            if field_type == StudentCustomFieldDefinition.SELECT and not options:
                raise StudentCustomFieldError(
                    {"options": "선택 타입은 한 개 이상의 선택지가 필요합니다."}
                )
            if field_type != StudentCustomFieldDefinition.SELECT:
                options = []
            validate_definition_headers(
                tenant=tenant,
                label=label,
                aliases=aliases,
                exclude_definition_id=instance.id if instance else None,
            )
        except StudentCustomFieldError as exc:
            raise serializers.ValidationError(exc.detail) from exc

        if (
            instance is None
            and StudentCustomFieldDefinition.objects.filter(tenant=tenant).count()
            >= MAX_CUSTOM_FIELDS_PER_TENANT
        ):
            raise serializers.ValidationError(
                {"detail": f"사용자 정의 컬럼은 최대 {MAX_CUSTOM_FIELDS_PER_TENANT}개입니다."}
            )

        attrs["label"] = label
        attrs["aliases"] = aliases
        attrs["options"] = options
        return attrs


class TagSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tag
        fields = ["id", "name", "color"]
        ref_name = "StudentTagSerializer"


class EnrollmentSerializer(serializers.ModelSerializer):
    lecture_name = serializers.CharField(source="lecture.title", read_only=True)
    lecture_color = serializers.CharField(source="lecture.color", read_only=True, default="#3b82f6")
    lecture_chip_label = serializers.CharField(
        source="lecture.chip_label",
        read_only=True,
        allow_blank=True,
        default="",
    )
    lecture_active = serializers.BooleanField(source="lecture.is_active", read_only=True)

    class Meta:
        model = Enrollment
        fields = [
            "id", "student", "lecture", "status",
            "enrolled_at", "created_at", "updated_at",
            "lecture_name", "lecture_color", "lecture_chip_label", "lecture_active",
        ]
        ref_name = "StudentEnrollment"


class StudentListSerializer(serializers.ModelSerializer):
    tags = TagSerializer(many=True, read_only=True)
    enrollments = EnrollmentSerializer(many=True, read_only=True)
    is_enrolled = serializers.SerializerMethodField()
    profile_photo_url = serializers.SerializerMethodField()
    account_state = serializers.SerializerMethodField()

    class Meta:
        model = Student
        fields = [
            "id", "tenant", "user", "ps_number", "omr_code",
            "name", "gender", "grade", "school_type",
            "phone", "parent_phone", "uses_identifier", "parent",
            "elementary_school", "high_school", "high_school_class", "major",
            "middle_school", "origin_middle_school",
            "memo", "address", "custom_fields", "is_managed", "deleted_at",
            "created_at", "updated_at",
            # computed
            "tags", "enrollments", "is_enrolled", "profile_photo_url", "account_state",
        ]
        ref_name = "StudentList"

    def get_profile_photo_url(self, obj):
        # R2 presigned URL 우선, 로컬 fallback 제거 (프로덕션 404 방지)
        r2_key = getattr(obj, "profile_photo_r2_key", None) or ""
        if r2_key:
            try:
                from django.conf import settings
                from academy.adapters.storage.r2_presign import create_presigned_get_url
                return create_presigned_get_url(r2_key, expires_in=3600, bucket=settings.R2_STORAGE_BUCKET)
            except Exception:
                pass
        return None

    @extend_schema_field(
        serializers.ChoiceField(choices=("ACTIVE", "INACTIVE", "DELETED", "UNLINKED"))
    )
    def get_account_state(self, obj):
        return _student_account_state(obj)

    def _get_clinic_highlight_map(self):
        """클리닉 하이라이트 맵을 context에서 가져오거나 lazy 계산"""
        ctx = self.context
        if "_clinic_highlight_map" in ctx:
            return ctx["_clinic_highlight_map"]

        # 첫 호출 시 일괄 계산
        request = ctx.get("request")
        tenant = getattr(request, "tenant", None) if request else None
        if not tenant:
            ctx["_clinic_highlight_map"] = {}
            return {}

        # list serializer인 경우 parent의 instance에서 enrollment_ids 수집
        enrollment_ids = set()
        instances = getattr(self.parent, "instance", None) if self.parent else None
        if instances is not None and hasattr(instances, "__iter__"):
            for student in instances:
                for enr in getattr(student, "enrollments", {}).all():
                    enrollment_ids.add(int(enr.id))
        elif hasattr(self, "instance") and self.instance:
            for enr in getattr(self.instance, "enrollments", {}).all():
                enrollment_ids.add(int(enr.id))

        if not enrollment_ids:
            ctx["_clinic_highlight_map"] = {}
            return {}

        highlight_map = clinic_highlight_map_for_enrollments(
            tenant=tenant,
            enrollment_ids=enrollment_ids,
        )
        ctx["_clinic_highlight_map"] = highlight_map
        return highlight_map

    def to_representation(self, obj):
        data = super().to_representation(obj)
        data["profile_photo_url"] = self.get_profile_photo_url(obj)

        # 클리닉 하이라이트: 해당 학생의 활성 enrollment 중 하나라도 True이면 True
        highlight_map = self._get_clinic_highlight_map()
        is_highlight = False
        for enr in getattr(obj, "enrollments", {}).all():
            if highlight_map.get(int(enr.id), False):
                is_highlight = True
                break
        data["name_highlight_clinic_target"] = is_highlight

        return data

    def get_is_enrolled(self, obj):
        request = self.context.get("request")
        if not request:
            return False

        lecture_id = request.query_params.get("lecture")
        if lecture_id:
            try:
                lid = int(lecture_id)
            except (TypeError, ValueError):
                return False
            # ViewSet이 enrollments를 prefetch하므로 캐시된 리스트에서 체크 (N+1 회피).
            return any(e.lecture_id == lid for e in obj.enrollments.all())

        return False


class StudentDetailSerializer(serializers.ModelSerializer):
    tags = TagSerializer(many=True, read_only=True)
    enrollments = EnrollmentSerializer(many=True, read_only=True)
    profile_photo_url = serializers.SerializerMethodField()
    account_state = serializers.SerializerMethodField()

    class Meta:
        model = Student
        fields = [
            "id", "tenant", "user", "ps_number", "omr_code",
            "name", "gender", "grade", "school_type",
            "phone", "parent_phone", "uses_identifier", "parent",
            "elementary_school", "high_school", "high_school_class", "major",
            "middle_school", "origin_middle_school",
            "memo", "address", "custom_fields", "is_managed", "deleted_at",
            "created_at", "updated_at",
            # computed
            "tags", "enrollments", "profile_photo_url", "account_state",
        ]
        ref_name = "StudentDetail"

    def get_profile_photo_url(self, obj):
        r2_key = getattr(obj, "profile_photo_r2_key", None) or ""
        if r2_key:
            try:
                from django.conf import settings
                from academy.adapters.storage.r2_presign import create_presigned_get_url
                return create_presigned_get_url(r2_key, expires_in=3600, bucket=settings.R2_STORAGE_BUCKET)
            except Exception:
                pass
        return None

    @extend_schema_field(
        serializers.ChoiceField(choices=("ACTIVE", "INACTIVE", "DELETED", "UNLINKED"))
    )
    def get_account_state(self, obj):
        return _student_account_state(obj)

    def to_representation(self, obj):
        data = super().to_representation(obj)
        data["profile_photo_url"] = self.get_profile_photo_url(obj)
        return data


class AddTagSerializer(serializers.Serializer):
    tag_id = serializers.IntegerField()


class StudentBulkItemSerializer(serializers.Serializer):
    """엑셀 일괄 등록용 단일 학생 데이터"""

    name = serializers.CharField(allow_blank=False, trim_whitespace=True, max_length=100)
    phone = serializers.CharField(allow_blank=True, trim_whitespace=True, required=False, default="", max_length=20)
    parent_phone = serializers.CharField(allow_blank=False, trim_whitespace=True, max_length=20)
    uses_identifier = serializers.BooleanField(required=False, default=False)
    gender = serializers.CharField(allow_blank=True, default="", max_length=10)
    school_type = serializers.ChoiceField(
        choices=[("ELEMENTARY", "초등"), ("MIDDLE", "중등"), ("HIGH", "고등")],
        default="HIGH",
    )
    school = serializers.CharField(allow_blank=True, default="", required=False, max_length=100)
    high_school_class = serializers.CharField(allow_blank=True, default="", required=False, max_length=50)
    major = serializers.CharField(allow_blank=True, default="", required=False, max_length=100)
    grade = serializers.IntegerField(allow_null=True, required=False)
    memo = serializers.CharField(allow_blank=True, default="", required=False, max_length=500)
    custom_fields = serializers.DictField(required=False, default=dict)
    is_managed = serializers.BooleanField(default=True, required=False)

    def validate_custom_fields(self, value):
        return _validated_custom_field_values(self, value)

    def validate_phone(self, value):
        # 학생 전화번호는 선택사항
        if not value:
            return None
        v = str(value or "").replace(" ", "").replace("-", "").replace(".", "")
        if v and (len(v) != 11 or not v.startswith("010")):
            raise serializers.ValidationError("전화번호는 010XXXXXXXX 11자리여야 합니다.")
        return v if v else None

    def validate_parent_phone(self, value):
        v = str(value or "").replace(" ", "").replace("-", "").replace(".", "")
        if not v or len(v) != 11 or not v.startswith("010"):
            raise serializers.ValidationError("학부모 전화번호는 010XXXXXXXX 11자리여야 합니다.")
        return v


class StudentBulkCreateSerializer(serializers.Serializer):
    initial_password = serializers.CharField(min_length=4, write_only=True)
    students = StudentBulkItemSerializer(many=True)


class StudentCreateSerializer(serializers.ModelSerializer):
    custom_fields = serializers.DictField(required=False, default=dict)
    initial_password = serializers.CharField(
        write_only=True,
        required=True,
        min_length=4,
    )
    no_phone = serializers.BooleanField(
        write_only=True,
        required=False,
        default=False,
        help_text="True면 식별자로 가입 (uses_identifier=True)",
    )
    ps_number = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="미입력 시 임의 6자리 자동 부여 (학생이 추후 변경 가능)",
    )
    omr_code = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="validate()에서 자동 생성 (학생/부모 전화번호 뒤 8자리)",
    )

    def validate_parent_phone(self, value):
        try:
            return normalize_student_phone(
                value,
                required=True,
                field_name="parent_phone",
                field_label="학부모 전화번호",
            )
        except StudentIdentityError as exc:
            raise serializers.ValidationError(exc.detail.get("parent_phone", str(exc.detail))) from exc

    def validate_custom_fields(self, value):
        return _validated_custom_field_values(self, value)

    class Meta:
        model = Student
        exclude = (
            "tenant",
            "user",
            "pending_account_notice_student_password_ciphertext",
            "pending_account_notice_parent_password_ciphertext",
            "pending_account_notice_since",
            "pending_account_notice_origin_type",
            "pending_account_notice_origin_id",
        )
        read_only_fields = ("deleted_at", "profile_photo")

    def _require(self, attrs, key: str):
        v = attrs.get(key)
        if v is None:
            raise serializers.ValidationError({key: "필수입니다."})
        if isinstance(v, str) and not v.strip():
            raise serializers.ValidationError({key: "필수입니다."})
        return v

    def validate_phone(self, value):
        # 학생 전화번호는 선택사항 (없으면 None)
        try:
            return normalize_student_phone(
                value,
                required=False,
                field_name="phone",
                field_label="학생 전화번호",
            )
        except StudentIdentityError as exc:
            raise serializers.ValidationError(exc.detail.get("phone", str(exc.detail))) from exc

    # omr_code는 validate에서 자동 설정되므로 별도 validate 불필요

    def validate(self, attrs):
        request = self.context.get("request")
        tenant = getattr(request, "tenant", None) if request else None
        if tenant is None:
            raise serializers.ValidationError("Tenant가 resolve되지 않았습니다.")

        parent_phone = str(self._require(attrs, "parent_phone")).strip()
        name = str(self._require(attrs, "name")).strip()
        try:
            phone_str = canonical_student_phone(
                phone=attrs.get("phone"),
                parent_phone=parent_phone,
            )
        except StudentIdentityError as exc:
            raise serializers.ValidationError(exc.detail) from exc

        if phone_str:
            from academy.adapters.db.django import repositories_students as student_repo
            if student_repo.user_filter_phone_exists(phone_str, tenant=tenant):
                raise serializers.ValidationError({"phone": "이미 사용 중인 전화번호입니다."})

        ps_number_raw = attrs.get("ps_number") or ""
        try:
            ps_number = resolve_student_login_id(
                tenant=tenant,
                requested_id=ps_number_raw,
                phone=phone_str,
            )
            if ps_number == phone_digits(parent_phone):
                raise StudentIdentityError(
                    {"ps_number": "학부모 전화번호는 학생 로그인 아이디로 사용할 수 없습니다."}
                )
        except StudentIdentityError as exc:
            raise serializers.ValidationError(exc.detail) from exc

        try:
            omr_code = derive_student_omr_code(
                phone=phone_str,
                parent_phone=parent_phone,
            )
        except StudentIdentityError as exc:
            raise serializers.ValidationError(exc.detail) from exc

        attrs["ps_number"] = ps_number
        attrs["omr_code"] = omr_code
        attrs["phone"] = phone_str if phone_str else None
        attrs["parent_phone"] = parent_phone
        attrs["name"] = name

        attrs["uses_identifier"] = attrs.pop("no_phone", False) or (phone_str is None)

        # school_level_mode 기반 school_type / grade 검증
        from apps.domains.students.services.school import get_valid_school_types, is_valid_grade
        from apps.core.models import Program
        program = Program.objects.filter(tenant=tenant).first()
        slm = program.feature_flags.get("school_level_mode") if program and program.feature_flags else None
        valid_types = get_valid_school_types(slm)

        school_type = attrs.get("school_type")
        if school_type and school_type not in valid_types:
            labels = {"ELEMENTARY": "초등", "MIDDLE": "중등", "HIGH": "고등"}
            allowed = ", ".join(labels.get(t, t) for t in sorted(valid_types))
            raise serializers.ValidationError(
                {"school_type": f"이 학원에서는 {allowed} 학생만 등록할 수 있습니다."}
            )

        grade = attrs.get("grade")
        if school_type and grade is not None and not is_valid_grade(school_type, grade):
            from apps.domains.students.services.school import GRADE_RANGE
            lo, hi = GRADE_RANGE.get(school_type, (1, 3))
            raise serializers.ValidationError(
                {"grade": f"{school_type} 학생의 학년은 {lo}~{hi}학년이어야 합니다."}
            )

        return attrs


class StudentUpdateSerializer(serializers.ModelSerializer):
    custom_fields = serializers.DictField(required=False)
    parent_initial_password = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
        min_length=4,
    )

    class Meta:
        model = Student
        fields = [
            "id",
            "ps_number",
            "omr_code",
            "name",
            "gender",
            "grade",
            "school_type",
            "phone",
            "parent_phone",
            "parent_initial_password",
            "uses_identifier",
            "elementary_school",
            "high_school",
            "high_school_class",
            "major",
            "middle_school",
            "origin_middle_school",
            "memo",
            "address",
            "custom_fields",
            "is_managed",
        ]
        read_only_fields = ("id", "omr_code")

    def validate(self, attrs):
        request = self.context.get("request")
        tenant = getattr(request, "tenant", None) if request else None
        if tenant is None:
            raise serializers.ValidationError("Tenant가 resolve되지 않았습니다.")

        instance = self.instance

        if "phone" in attrs:
            try:
                attrs["phone"] = normalize_student_phone(
                    attrs.get("phone"),
                    required=False,
                    field_name="phone",
                    field_label="학생 전화번호",
                )
            except StudentIdentityError as exc:
                raise serializers.ValidationError(exc.detail) from exc
        if "parent_phone" in attrs:
            try:
                attrs["parent_phone"] = normalize_student_phone(
                    attrs.get("parent_phone"),
                    required=True,
                    field_name="parent_phone",
                    field_label="학부모 전화번호",
                )
            except StudentIdentityError as exc:
                raise serializers.ValidationError(exc.detail) from exc

        ps_number = attrs.get("ps_number", instance.ps_number)

        if ps_number and student_login_id_taken(
            tenant=tenant,
            display_username=ps_number,
            exclude_student_id=instance.id,
            exclude_user_id=instance.user_id,
        ):
            raise serializers.ValidationError({"ps_number": "이미 사용 중인 아이디입니다."})

        # school_level_mode 기반 school_type / grade 검증
        school_type = attrs.get("school_type", instance.school_type)
        grade = attrs.get("grade", instance.grade)
        if school_type:
            from apps.domains.students.services.school import get_valid_school_types, is_valid_grade
            from apps.core.models import Program
            program = Program.objects.filter(tenant=tenant).first()
            slm = program.feature_flags.get("school_level_mode") if program and program.feature_flags else None
            valid_types = get_valid_school_types(slm)
            if school_type not in valid_types:
                labels = {"ELEMENTARY": "초등", "MIDDLE": "중등", "HIGH": "고등"}
                allowed = ", ".join(labels.get(t, t) for t in sorted(valid_types))
                raise serializers.ValidationError(
                    {"school_type": f"이 학원에서는 {allowed} 학생만 등록할 수 있습니다."}
                )
            if grade is not None and not is_valid_grade(school_type, grade):
                from apps.domains.students.services.school import GRADE_RANGE
                lo, hi = GRADE_RANGE.get(school_type, (1, 3))
                raise serializers.ValidationError(
                    {"grade": f"{school_type} 학생의 학년은 {lo}~{hi}학년이어야 합니다."}
                )

        return attrs

    def validate_custom_fields(self, value):
        return _validated_custom_field_values(self, value)


# ========== 학생 가입 신청 (로그인 전 회원가입) ==========


def _normalize_phone(value):
    try:
        return normalize_student_phone(value, required=False) or ""
    except StudentIdentityError as exc:
        raise serializers.ValidationError(exc.detail) from exc


class RegistrationRequestCreateSerializer(serializers.Serializer):
    """학생이 로그인 페이지에서 제출하는 가입 신청 (필수 필드만)"""

    name = serializers.CharField(max_length=50, trim_whitespace=True)
    username = serializers.CharField(
        max_length=50,
        required=False,
        allow_blank=True,
        allow_null=True,
        default="",
        trim_whitespace=True,
    )
    initial_password = serializers.CharField(
        min_length=4,
        max_length=128,
        write_only=True,
        trim_whitespace=False,
    )
    password_confirmation = serializers.CharField(
        min_length=4,
        max_length=128,
        write_only=True,
        trim_whitespace=False,
    )
    parent_phone = serializers.CharField(max_length=20)
    phone = serializers.CharField(max_length=20, required=False, allow_blank=True, allow_null=True, default="")
    school_type = serializers.ChoiceField(
        choices=[("ELEMENTARY", "초등"), ("MIDDLE", "중등"), ("HIGH", "고등")],
        default="HIGH",
    )
    elementary_school = serializers.CharField(required=False, allow_blank=True, allow_null=True, default="")
    high_school = serializers.CharField(required=False, allow_blank=True, allow_null=True, default="")
    middle_school = serializers.CharField(required=False, allow_blank=True, allow_null=True, default="")
    high_school_class = serializers.CharField(required=False, allow_blank=True, allow_null=True, default="")
    major = serializers.CharField(required=False, allow_blank=True, allow_null=True, default="")
    grade = serializers.IntegerField(required=False, allow_null=True)
    gender = serializers.CharField(required=False, allow_blank=True, allow_null=True, default="", max_length=1)
    memo = serializers.CharField(required=False, allow_blank=True, allow_null=True, default="")
    address = serializers.CharField(required=False, allow_blank=True, allow_null=True, default="", max_length=255)
    origin_middle_school = serializers.CharField(required=False, allow_blank=True, allow_null=True, default="", max_length=100)

    def validate_parent_phone(self, value):
        try:
            return normalize_student_phone(
                value,
                required=True,
                field_name="parent_phone",
                field_label="학부모 전화번호",
            )
        except StudentIdentityError as exc:
            raise serializers.ValidationError(exc.detail.get("parent_phone", str(exc.detail))) from exc

    def validate_phone(self, value):
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        try:
            return normalize_student_phone(
                value,
                required=False,
                field_name="phone",
                field_label="전화번호",
            )
        except StudentIdentityError as exc:
            raise serializers.ValidationError(exc.detail.get("phone", str(exc.detail))) from exc

    def validate(self, attrs):
        if attrs.get("initial_password") != attrs.get("password_confirmation"):
            raise serializers.ValidationError(
                {"password_confirmation": "비밀번호가 일치하지 않습니다."}
            )
        attrs.pop("password_confirmation", None)
        attrs["parent_phone"] = attrs["parent_phone"]
        attrs["phone"] = attrs.get("phone") or None
        attrs["username"] = normalize_login_identifier(attrs.get("username"))
        if attrs["username"] and attrs["username"] == attrs["parent_phone"]:
            raise serializers.ValidationError(
                {"username": "학부모 전화번호는 학생 로그인 아이디로 사용할 수 없습니다."}
            )
        # null → 빈 문자열로 통일 (모델은 null 허용이지만 저장 시 빈 문자열도 허용)
        for key in ("username", "elementary_school", "high_school", "middle_school", "high_school_class", "major", "gender", "memo", "address", "origin_middle_school"):
            if attrs.get(key) is None:
                attrs[key] = ""

        # 회원가입 시 모든 필드 필수 입력 (계열 제외) — Limglish 등 운영 요구
        signup_required = {
            "name": "이름",
            "initial_password": "비밀번호",
            "parent_phone": "학부모 연락처",
            "phone": "휴대전화",
            "grade": "학년",
            "gender": "성별",
            "address": "주소",
        }
        school_type = attrs.get("school_type") or "HIGH"
        if school_type == "HIGH":
            signup_required["high_school"] = "고등학교명"
            signup_required["origin_middle_school"] = "출신중학교"
        elif school_type == "MIDDLE":
            signup_required["middle_school"] = "중학교명"
        elif school_type == "ELEMENTARY":
            signup_required["elementary_school"] = "초등학교명"

        for key, label in signup_required.items():
            val = attrs.get(key)
            if key == "grade":
                if val is None or (isinstance(val, str) and str(val).strip() == ""):
                    raise serializers.ValidationError({key: f"{label}을(를) 입력해 주세요."})
            elif isinstance(val, str):
                if not val.strip():
                    raise serializers.ValidationError({key: f"{label}을(를) 입력해 주세요."})
            elif val is None:
                raise serializers.ValidationError({key: f"{label}을(를) 입력해 주세요."})

        # school_level_mode 기반 school_type / grade 검증
        request = self.context.get("request")
        tenant = getattr(request, "tenant", None) if request else None
        if tenant:
            from apps.domains.students.services.school import get_valid_school_types, is_valid_grade
            from apps.core.models import Program
            program = Program.objects.filter(tenant=tenant).first()
            slm = program.feature_flags.get("school_level_mode") if program and program.feature_flags else None
            valid_types = get_valid_school_types(slm)
            if school_type not in valid_types:
                labels = {"ELEMENTARY": "초등", "MIDDLE": "중등", "HIGH": "고등"}
                allowed = ", ".join(labels.get(t, t) for t in sorted(valid_types))
                raise serializers.ValidationError(
                    {"school_type": f"이 학원에서는 {allowed} 학생만 가입할 수 있습니다."}
                )
            grade = attrs.get("grade")
            if grade is not None and not is_valid_grade(school_type, grade):
                from apps.domains.students.services.school import GRADE_RANGE
                lo, hi = GRADE_RANGE.get(school_type, (1, 3))
                raise serializers.ValidationError(
                    {"grade": f"{school_type} 학생의 학년은 {lo}~{hi}학년이어야 합니다."}
                )

        return attrs


_REGISTRATION_REQUEST_LIST_FIELDS = (
    "id",
    "tenant",
    "status",
    "name",
    "username",
    "parent_phone",
    "phone",
    "school_type",
    "elementary_school",
    "high_school",
    "middle_school",
    "high_school_class",
    "major",
    "grade",
    "gender",
    "memo",
    "address",
    "origin_middle_school",
    "student",
    "created_at",
    "updated_at",
)


class RegistrationRequestListSerializer(serializers.ModelSerializer):
    """스태프용 가입 신청 목록/상세 (initial_password 제외)"""

    class Meta:
        model = StudentRegistrationRequest
        fields = _REGISTRATION_REQUEST_LIST_FIELDS
        read_only_fields = _REGISTRATION_REQUEST_LIST_FIELDS


class SelfRegistrationDisabledErrorSerializer(serializers.Serializer):
    code = serializers.ChoiceField(choices=["self_registration_disabled"])
    detail = serializers.CharField()


class RegistrationRequestBulkIdsSerializer(serializers.Serializer):
    ids = serializers.ListField(child=serializers.IntegerField(min_value=1), allow_empty=False)


class RegistrationRequestBulkFailureSerializer(serializers.Serializer):
    id = serializers.IntegerField(min_value=1)
    detail = serializers.CharField()


class RegistrationRequestBulkApproveResponseSerializer(serializers.Serializer):
    approved = serializers.IntegerField(min_value=0)
    failed = RegistrationRequestBulkFailureSerializer(many=True)


class RegistrationRequestBulkRejectResponseSerializer(serializers.Serializer):
    rejected = serializers.IntegerField(min_value=0)


class RegistrationRequestRejectResponseSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=[StudentRegistrationRequest.REJECTED])
    id = serializers.IntegerField(min_value=1)


class RegistrationRequestAvailabilitySerializer(serializers.Serializer):
    available = serializers.BooleanField()
    reason = serializers.CharField(required=False)


class RegistrationRequestDuplicateCheckRequestSerializer(serializers.Serializer):
    username = serializers.CharField(required=False, allow_blank=True)
    phone = serializers.CharField(required=False, allow_blank=True)


class RegistrationRequestDuplicateCheckResponseSerializer(serializers.Serializer):
    username = RegistrationRequestAvailabilitySerializer(required=False)
    phone = RegistrationRequestAvailabilitySerializer(required=False)


class RegistrationRequestSettingsSerializer(serializers.Serializer):
    auto_approve = serializers.BooleanField()


class DeletedRegistrationCandidateSerializer(serializers.Serializer):
    student_id = serializers.IntegerField(min_value=1)
    created_at = serializers.DateTimeField()
    deleted_at = serializers.DateTimeField()
    enrollment_count = serializers.IntegerField(min_value=0)


class DeletedRegistrationConflictSerializer(serializers.Serializer):
    code = serializers.CharField(default="deleted_student_conflict")
    detail = serializers.CharField()
    candidates = DeletedRegistrationCandidateSerializer(many=True)


class DeletedRegistrationResolveSerializer(serializers.Serializer):
    student_id = serializers.IntegerField(min_value=1)
