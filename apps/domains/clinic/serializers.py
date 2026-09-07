# PATH: apps/domains/clinic/serializers.py

from datetime import datetime, timedelta
from rest_framework import serializers
from .models import Session, SessionParticipant, Test, Submission
from .services.lifecycle import booking_availability_for_session
from apps.core.permissions import is_effective_staff
from apps.support.clinic.session_dependencies import (
    active_students_for_clinic_tenant,
    clinic_highlight_map_for_enrollments,
    empty_enrollment_queryset,
    empty_lecture_queryset,
    enrollments_for_clinic_tenant,
    lectures_for_tenant,
    sections_for_tenant,
    student_has_current_required_clinic_target,
    storage_presigned_get_url,
)


class ClinicSessionSerializer(serializers.ModelSerializer):
    participant_count = serializers.SerializerMethodField()
    booked_count = serializers.SerializerMethodField()

    tenant = serializers.PrimaryKeyRelatedField(read_only=True)
    created_by = serializers.PrimaryKeyRelatedField(read_only=True)

    # section FK: 쓰기 시 section_id, 읽기 시 id+label
    section_label = serializers.CharField(source="section.label", read_only=True, default=None)
    section_type = serializers.CharField(source="section.section_type", read_only=True, default=None)

    # 대상 강의: 쓰기 시 id 배열, 읽기 시 id+title
    target_lecture_ids = serializers.PrimaryKeyRelatedField(
        source="target_lectures",
        queryset=empty_lecture_queryset(),  # __init__에서 tenant 필터 적용
        many=True,
        required=False,
    )
    target_lecture_names = serializers.SerializerMethodField()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._range_available_slots = {}
        request = self.context.get("request")
        if request and hasattr(request, "tenant") and request.tenant:
            self.fields["target_lecture_ids"].child_relation.queryset = (
                lectures_for_tenant(request.tenant)
            )
            if "section" in self.fields:
                self.fields["section"].queryset = sections_for_tenant(request.tenant)

    # ✅ 파생 필드: 종료 시간 (저장 X)
    end_time = serializers.SerializerMethodField()

    # ✅ [ADD] 운영 판단 필드
    available_slots = serializers.SerializerMethodField()
    is_full = serializers.SerializerMethodField()

    # ✅ [ADD] 상태/소스 요약
    status_summary = serializers.SerializerMethodField()
    source_summary = serializers.SerializerMethodField()
    has_auto_targets = serializers.SerializerMethodField()

    class Meta:
        model = Session
        exclude = ("target_lectures",)

    def validate(self, attrs):
        instance = self.instance
        mode = attrs.get("booking_mode", getattr(instance, "booking_mode", "fixed_slot"))
        interval = attrs.get(
            "booking_interval_minutes",
            getattr(instance, "booking_interval_minutes", 60),
        )
        max_stay = attrs.get(
            "booking_max_stay_minutes",
            getattr(instance, "booking_max_stay_minutes", 240),
        )
        if interval not in (30, 60):
            raise serializers.ValidationError(
                {"booking_interval_minutes": "예약 간격은 30분 또는 60분이어야 합니다."}
            )
        if max_stay < interval or max_stay % interval:
            raise serializers.ValidationError(
                {"booking_max_stay_minutes": "최대 체류 시간은 예약 간격의 양의 배수여야 합니다."}
            )
        if instance and instance.participants.filter(
            status__in=(
                SessionParticipant.Status.PENDING,
                SessionParticipant.Status.BOOKED,
                SessionParticipant.Status.ATTENDED,
            )
        ).exists():
            changed = any(
                key in attrs and attrs[key] != getattr(instance, key)
                for key in (
                    "booking_mode",
                    "booking_interval_minutes",
                    "booking_max_stay_minutes",
                )
            )
            if changed:
                raise serializers.ValidationError(
                    {"booking_policy": "활성 예약이 있는 세션의 예약 방식은 변경할 수 없습니다."}
                )
        if mode == "time_range" and attrs.get(
            "allow_multi_slot_booking",
            getattr(instance, "allow_multi_slot_booking", False),
        ):
            raise serializers.ValidationError(
                {"allow_multi_slot_booking": "시간 범위 방식은 여러 세션 동시 예약과 함께 사용할 수 없습니다."}
            )
        duration = attrs.get("duration_minutes", getattr(instance, "duration_minutes", 60))
        if mode == "time_range" and duration % interval:
            raise serializers.ValidationError(
                {"duration_minutes": "시간 범위 세션의 운영 시간은 예약 간격의 배수여야 합니다."}
            )
        return attrs

    def get_participant_count(self, obj: Session):
        return getattr(obj, "participant_count", 0)

    def get_booked_count(self, obj: Session):
        return getattr(obj, "booked_count", 0)

    def get_end_time(self, obj: Session):
        if not obj.start_time or not obj.duration_minutes:
            return None
        dt = datetime.combine(obj.date, obj.start_time)
        return (dt + timedelta(minutes=obj.duration_minutes)).time()

    def get_available_slots(self, obj):
        if obj.booking_mode == "time_range" and obj.max_participants is not None:
            if obj.pk not in self._range_available_slots:
                availability = booking_availability_for_session(tenant=obj.tenant_id, session=obj)
                self._range_available_slots[obj.pk] = max(
                    (slot["remaining_capacity"] for slot in availability["slots"]), default=0,
                )
            return self._range_available_slots[obj.pk]
        cnt = getattr(obj, "booked_count", None)
        if obj.max_participants is None or cnt is None:
            return None
        return max(obj.max_participants - cnt, 0)

    def get_is_full(self, obj):
        if obj.booking_mode == "time_range":
            return self.get_available_slots(obj) == 0
        cnt = getattr(obj, "booked_count", None)
        if obj.max_participants is None or cnt is None:
            return False
        return cnt >= obj.max_participants

    def get_status_summary(self, obj):
        return {
            "pending": getattr(obj, "pending_count", 0),
            "booked": getattr(obj, "booked_confirmed_count", getattr(obj, "booked_count", 0)),
            "reserved": getattr(obj, "booked_count", 0),
            "attended": getattr(obj, "attended_count", 0),
            "no_show": getattr(obj, "no_show_count", 0),
            "cancelled": getattr(obj, "cancelled_count", 0),
        }

    def get_source_summary(self, obj):
        return {
            "auto": getattr(obj, "auto_count", 0),
            "manual": getattr(obj, "manual_count", 0),
        }

    def get_has_auto_targets(self, obj):
        return getattr(obj, "auto_count", 0) > 0

    def get_target_lecture_names(self, obj):
        lectures = obj.target_lectures.all()
        if not lectures:
            return []
        return [
            {
                "id": lec.id,
                "title": lec.title,
                "color": getattr(lec, "color", None),
                "chip_label": getattr(lec, "chip_label", None),
            }
            for lec in lectures
        ]


class ClinicSessionParticipantSerializer(serializers.ModelSerializer):
    completion_history = serializers.JSONField(read_only=True)
    preferred_start_time = serializers.TimeField(read_only=True)
    preferred_end_time = serializers.TimeField(read_only=True)
    student_request_memo = serializers.CharField(read_only=True)
    student_name = serializers.CharField(source="student.name", read_only=True)
    session_date = serializers.SerializerMethodField()  # ✅ session이 없을 수 있으므로 SerializerMethodField 사용
    session_start_time = serializers.SerializerMethodField()
    session_location = serializers.SerializerMethodField()
    session_title = serializers.SerializerMethodField()
    planned_clinic_link_ids = serializers.SerializerMethodField()
    can_self_cancel = serializers.SerializerMethodField()
    self_cancel_reason = serializers.SerializerMethodField()

    # ✅ 파생 노출
    session_duration_minutes = serializers.SerializerMethodField()
    session_end_time = serializers.SerializerMethodField()

    # ✅ 학생 SSOT 표시용: 강의 딱지 + 클리닉 하이라이트 + 아바타
    lecture_title = serializers.SerializerMethodField()
    lecture_color = serializers.SerializerMethodField()
    lecture_chip_label = serializers.SerializerMethodField()
    name_highlight_clinic_target = serializers.SerializerMethodField()
    profile_photo_url = serializers.SerializerMethodField()

    # ✅ [ADD] 변경자 이름 노출
    status_changed_by_name = serializers.CharField(
        source="status_changed_by.username",
        read_only=True,
        default=None,
    )

    # FK 전환 후 API 호환성: enrollment → enrollment_id로 노출
    enrollment_id = serializers.PrimaryKeyRelatedField(
        source="enrollment", read_only=True,
    )

    completed_by_name = serializers.CharField(
        source="completed_by.username",
        read_only=True,
        default=None,
    )
    checked_out_by_name = serializers.CharField(
        source="checked_out_by.username",
        read_only=True,
        default=None,
    )
    recipient_contacts = serializers.SerializerMethodField()

    class Meta:
        model = SessionParticipant
        fields = "__all__"
        extra_kwargs = {
            "enrollment": {"write_only": True, "required": False},
        }

    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get("request")
        if request is None or not is_effective_staff(
            getattr(request, "user", None),
            getattr(request, "tenant", None),
        ):
            data.pop("staff_memo", None)
            data.pop("memo", None)
            data.pop("completion_history", None)
            data.pop("recipient_contacts", None)
        return data

    def _self_cancel_policy(self, obj):
        from apps.domains.clinic.services import participant_self_cancel_policy

        cache = self.context.setdefault("_clinic_self_cancel_policy", {})
        if obj.pk not in cache:
            required_cache = self.context.setdefault(
                "_clinic_self_cancel_required_target",
                {},
            )
            if obj.student_id not in required_cache:
                required_cache[obj.student_id] = student_has_current_required_clinic_target(
                    tenant=obj.tenant,
                    student=obj.student,
                )
            cache[obj.pk] = participant_self_cancel_policy(
                tenant=obj.tenant,
                participant=obj,
                has_current_required_target=required_cache[obj.student_id],
            )
        return cache[obj.pk]

    def _self_cancel_is_staff_request(self):
        cache_key = "_clinic_self_cancel_is_staff_request"
        if cache_key not in self.context:
            request = self.context.get("request")
            self.context[cache_key] = bool(
                request is not None
                and is_effective_staff(
                    getattr(request, "user", None),
                    getattr(request, "tenant", None),
                )
            )
        return self.context[cache_key]

    def get_can_self_cancel(self, obj):
        if self._self_cancel_is_staff_request():
            return False
        return self._self_cancel_policy(obj).allowed

    def get_self_cancel_reason(self, obj):
        if self._self_cancel_is_staff_request():
            return "교직원은 클리닉 명단에서 취소할 수 있습니다."
        return self._self_cancel_policy(obj).reason

    def get_recipient_contacts(self, obj: SessionParticipant) -> list[dict[str, str]]:
        student = obj.student
        contacts = []
        if student.phone:
            contacts.append({
                "role": "student",
                "name": student.name,
                "phone": student.phone,
            })
        if student.parent_phone:
            parent_name = f"{student.name} 학부모"
            parent = getattr(student, "parent", None)
            parent_user = getattr(parent, "user", None) if parent else None
            if parent_user:
                parent_name = (
                    parent_user.get_full_name()
                    or getattr(parent_user, "name", "")
                    or parent_name
                )
            contacts.append({
                "role": "parent",
                "name": parent_name,
                "phone": student.parent_phone,
            })
        return contacts

    def get_session_date(self, obj):
        """session이 있으면 session.date, 없으면 requested_date"""
        return obj.session.date if obj.session else obj.requested_date
    
    def get_session_start_time(self, obj):
        """session이 있으면 session.start_time, 없으면 requested_start_time"""
        return obj.session.start_time if obj.session else obj.requested_start_time
    
    def get_session_location(self, obj):
        """session이 있으면 session.location, 없으면 None"""
        return obj.session.location if obj.session else None

    def get_session_title(self, obj):
        """session이 있으면 학생/관리자 일정에 표시할 제목"""
        return obj.session.title if obj.session else ""

    def get_planned_clinic_link_ids(self, obj) -> list[int]:
        from .services.lifecycle import planned_clinic_link_ids_for_participant

        return planned_clinic_link_ids_for_participant(obj)
    
    def get_session_duration_minutes(self, obj):
        """session이 있으면 duration_minutes, 없으면 None"""
        return obj.session.duration_minutes if obj.session else None
    
    def get_session_end_time(self, obj):
        if not obj.session or not obj.session.start_time or not obj.session.duration_minutes:
            return None
        dt = datetime.combine(obj.session.date, obj.session.start_time)
        return (dt + timedelta(minutes=obj.session.duration_minutes)).time()

    def get_lecture_title(self, obj):
        enrollment = getattr(obj, "enrollment", None)
        lecture = getattr(enrollment, "lecture", None) if enrollment else None
        return getattr(lecture, "title", None) if lecture else None

    def get_lecture_color(self, obj):
        enrollment = getattr(obj, "enrollment", None)
        lecture = getattr(enrollment, "lecture", None) if enrollment else None
        return getattr(lecture, "color", None) if lecture else None

    def get_lecture_chip_label(self, obj):
        enrollment = getattr(obj, "enrollment", None)
        lecture = getattr(enrollment, "lecture", None) if enrollment else None
        return getattr(lecture, "chip_label", None) if lecture else None

    def _get_clinic_highlight_map(self) -> dict[int, bool]:
        """list 직렬화 시 패스카드와 동일한 하이라이트 맵을 1회 계산한다.

        get_name_highlight_clinic_target N+1 회피용. tenant 격리는 ClinicLink.tenant FK로 직접 필터.
        """
        ctx = self.context
        if "_clinic_highlight_map" in ctx:
            return ctx["_clinic_highlight_map"]

        request = ctx.get("request")
        tenant = getattr(request, "tenant", None) if request else None
        if not tenant:
            ctx["_clinic_highlight_map"] = {}
            return {}

        # parent의 instance(list)에서 enrollment_id 수집
        enrollment_ids = set()
        instances = getattr(self.parent, "instance", None) if self.parent else None
        if instances is not None and hasattr(instances, "__iter__"):
            for p in instances:
                eid = getattr(p, "enrollment_id", None)
                if eid:
                    enrollment_ids.add(int(eid))
        elif hasattr(self, "instance") and self.instance:
            eid = getattr(self.instance, "enrollment_id", None)
            if eid:
                enrollment_ids.add(int(eid))

        if not enrollment_ids:
            ctx["_clinic_highlight_map"] = {}
            return {}

        highlight_map = clinic_highlight_map_for_enrollments(
            tenant=tenant,
            enrollment_ids=enrollment_ids,
        )
        ctx["_clinic_highlight_map"] = highlight_map
        return highlight_map

    def get_name_highlight_clinic_target(self, obj):
        """학생 패스카드가 CLINIC_REQUIRED일 때만 True."""
        eid = getattr(obj, "enrollment_id", None)
        if not eid:
            return False
        # bulk 계산한 map에서 O(1) 룩업으로 N+1 회피.
        return self._get_clinic_highlight_map().get(int(eid), False)

    def get_profile_photo_url(self, obj):
        """학생 프로필 사진 R2 presigned URL"""
        student = getattr(obj, "student", None)
        if not student:
            return None
        r2_key = getattr(student, "profile_photo_r2_key", None) or ""
        if not r2_key:
            return None
        try:
            return storage_presigned_get_url(r2_key, expires_in=3600)
        except Exception:
            return None


class ClinicSessionParticipantCreateSerializer(serializers.ModelSerializer):
    """
    ✅ 예약 등록(생성) 전용
    - 선생: student, enrollment_id 직접 지정 가능
    - 학생: student 생략 가능 (자동 설정), source="student_request", status="pending"
    - session 또는 (requested_date + requested_start_time) 중 하나 필수
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = self.context.get("request")
        if request and hasattr(request, "tenant") and request.tenant:
            tenant = request.tenant
            self.fields["session"].queryset = Session.objects.filter(tenant=tenant)
            self.fields["enrollment_id"].queryset = enrollments_for_clinic_tenant(tenant)
            self.fields["student"].queryset = active_students_for_clinic_tenant(tenant)

    # FK 전환 호환: 프론트가 enrollment_id로 보내면 enrollment FK로 매핑
    enrollment_id = serializers.PrimaryKeyRelatedField(
        source="enrollment",
        queryset=empty_enrollment_queryset(),  # __init__에서 tenant-scoped로 교체
        required=False,
        allow_null=True,
    )

    class Meta:
        model = SessionParticipant
        fields = [
            "session",
            "requested_date",  # ✅ 학생 신청 시 날짜
            "requested_start_time",  # ✅ 학생 신청 시 시간
            "preferred_start_time",
            "preferred_end_time",
            "booking_start_time",
            "booking_end_time",
            "student_request_memo",
            "student",
            "status",
            "memo",
            "source",
            "enrollment_id",
            "clinic_reason",
            "participant_role",
        ]
        extra_kwargs = {
            "student": {"required": False},  # 학생 신청 시 생략 가능
            "session": {"required": False},  # 학생 신청 시 세션이 없을 수 있음
            "requested_date": {"required": False},
            "requested_start_time": {"required": False},
        }
    
    def validate(self, attrs):
        """session 또는 (requested_date + requested_start_time) 중 하나는 필수"""
        session = attrs.get("session")
        requested_date = attrs.get("requested_date")
        requested_start_time = attrs.get("requested_start_time")
        
        if not session and not (requested_date and requested_start_time):
            raise serializers.ValidationError(
                "session 또는 (requested_date + requested_start_time) 중 하나는 필수입니다."
            )
        
        if session and (requested_date or requested_start_time):
            raise serializers.ValidationError(
                "session과 requested_date/requested_start_time을 동시에 사용할 수 없습니다."
            )
        
        return attrs


class ClinicSessionParticipantBulkCreateSerializer(serializers.Serializer):
    """Create one student's or several staff-selected students' same-day slots atomically."""

    session_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        min_length=1,
        max_length=20,
    )
    student_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        default=list,
        max_length=100,
    )
    enrollment_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        default=list,
        max_length=100,
    )
    student_request_memo = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        max_length=2000,
    )
    memo = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        max_length=2000,
    )
    preferred_start_time = serializers.TimeField(required=False, allow_null=True)
    preferred_end_time = serializers.TimeField(required=False, allow_null=True)
    booking_start_time = serializers.TimeField(required=False, allow_null=True)
    booking_end_time = serializers.TimeField(required=False, allow_null=True)

    def validate(self, attrs):
        session_ids = attrs["session_ids"]
        student_ids = attrs.get("student_ids", [])
        enrollment_ids = attrs.get("enrollment_ids", [])
        if len(set(session_ids)) != len(session_ids):
            raise serializers.ValidationError(
                {"session_ids": "같은 시간대를 중복해서 선택할 수 없습니다."}
            )
        if len(set(student_ids)) != len(student_ids):
            raise serializers.ValidationError(
                {"student_ids": "같은 학생을 중복해서 선택할 수 없습니다."}
            )
        if len(set(enrollment_ids)) != len(enrollment_ids):
            raise serializers.ValidationError(
                {"enrollment_ids": "같은 수강 대상을 중복해서 선택할 수 없습니다."}
            )
        if student_ids and enrollment_ids:
            raise serializers.ValidationError(
                {"detail": "student_ids와 enrollment_ids 중 하나만 선택해 주세요."}
            )
        participant_count = len(session_ids) * max(
            len(student_ids),
            len(enrollment_ids),
            1,
        )
        if participant_count > 500:
            raise serializers.ValidationError(
                {"detail": "한 번에 만들 수 있는 클리닉 예약은 최대 500건입니다."}
            )
        if len(session_ids) > 1 and (
            attrs.get("preferred_start_time") is not None
            or attrs.get("preferred_end_time") is not None
        ):
            raise serializers.ValidationError(
                {"preferred_time": "희망 시간은 한 시간대만 선택했을 때 입력할 수 있습니다."}
            )
        if len(session_ids) > 1 and (
            attrs.get("booking_start_time") is not None
            or attrs.get("booking_end_time") is not None
        ):
            raise serializers.ValidationError(
                {"booking_time": "실제 예약 시간은 한 세션만 선택했을 때 입력할 수 있습니다."}
            )
        return attrs


class ClinicSessionParticipantBulkCreateResponseSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    participants = ClinicSessionParticipantSerializer(many=True)


class ClinicSessionBulkCreateSerializer(serializers.Serializer):
    """
    POST /clinic/sessions/bulk-create/ 전용 직렬화기
    - dates 배열 (최대 20일) + 공통 세션 필드
    """
    title = serializers.CharField(required=False, allow_blank=True, default="")
    start_time = serializers.TimeField()
    duration_minutes = serializers.IntegerField(min_value=1)
    location = serializers.CharField(max_length=200)
    max_participants = serializers.IntegerField(min_value=1, default=20)
    target_grade = serializers.IntegerField(required=False, allow_null=True, default=None)
    target_school_type = serializers.CharField(required=False, allow_null=True, default=None)
    section_id = serializers.IntegerField(required=False, allow_null=True, default=None)
    allow_multi_slot_booking = serializers.BooleanField(required=False)
    booking_mode = serializers.ChoiceField(
        choices=("fixed_slot", "time_range"),
        required=False,
    )
    booking_interval_minutes = serializers.ChoiceField(
        choices=(30, 60),
        required=False,
    )
    booking_max_stay_minutes = serializers.IntegerField(min_value=30, required=False)
    target_lecture_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, default=[]
    )
    dates = serializers.ListField(
        child=serializers.DateField(), min_length=1, max_length=20
    )

    def validate_target_grade(self, value):
        if value is not None and value not in range(1, 7):  # 1~6 (초등 포함)
            raise serializers.ValidationError("학년은 1~6 중 하나여야 합니다.")
        return value

    def validate_target_school_type(self, value):
        if value is not None and value not in ("ELEMENTARY", "MIDDLE", "HIGH"):
            raise serializers.ValidationError("학교 유형은 ELEMENTARY, MIDDLE, HIGH 중 하나여야 합니다.")
        return value

    def validate(self, attrs):
        interval = attrs.get("booking_interval_minutes", 60)
        max_stay = attrs.get("booking_max_stay_minutes", 240)
        if max_stay % interval:
            raise serializers.ValidationError(
                {"booking_max_stay_minutes": "최대 체류 시간은 예약 간격의 배수여야 합니다."}
            )
        if attrs.get("booking_mode") == "time_range" and len(attrs["dates"]) > 1:
            raise serializers.ValidationError(
                {"dates": "시간 범위 방식은 한 날짜씩 생성해 주세요."}
            )
        if attrs.get("booking_mode") == "time_range" and attrs["duration_minutes"] % interval:
            raise serializers.ValidationError(
                {"duration_minutes": "시간 범위 세션의 운영 시간은 예약 간격의 배수여야 합니다."}
            )
        return attrs


class ClinicTestSerializer(serializers.ModelSerializer):
    class Meta:
        model = Test
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = self.context.get("request")
        if request and hasattr(request, "tenant") and request.tenant:
            tenant = request.tenant
            if "session" in self.fields:
                self.fields["session"].queryset = Session.objects.filter(tenant=tenant)


class ClinicSubmissionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Submission
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = self.context.get("request")
        if request and hasattr(request, "tenant") and request.tenant:
            tenant = request.tenant
            if "test" in self.fields:
                self.fields["test"].queryset = Test.objects.filter(tenant=tenant)
            if "student" in self.fields:
                self.fields["student"].queryset = active_students_for_clinic_tenant(tenant)

    def validate_score(self, value):
        if value is None:
            return value
        if float(value) < 0:
            raise serializers.ValidationError("score는 0 이상이어야 합니다.")
        return value
