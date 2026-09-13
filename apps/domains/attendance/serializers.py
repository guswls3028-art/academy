# apps/domains/attendance/serializers.py

from rest_framework import serializers
from drf_spectacular.utils import extend_schema_serializer
from .models import Attendance
from apps.support.attendance.serializer_dependencies import (
    clinic_highlight_map_for_attendance,
    enrollment_queryset_for_attendance_serializer,
    session_queryset_for_attendance_serializer,
)


def attendance_status_choices():
    return Attendance._meta.get_field("status").choices


@extend_schema_serializer(component_name="SessionAttendance")
class AttendanceSerializer(serializers.ModelSerializer):
    confirm_secession = serializers.BooleanField(required=False, write_only=True)
    secession_scope = serializers.ChoiceField(
        choices=("session", "lecture"), required=False, write_only=True,
        help_text="session: 이 차시만 퇴원. lecture 또는 생략: 기존 강의 전체 퇴원.",
    )
    session = serializers.PrimaryKeyRelatedField(
        queryset=session_queryset_for_attendance_serializer(),
    )
    enrollment_id = serializers.PrimaryKeyRelatedField(
        source="enrollment",
        queryset=enrollment_queryset_for_attendance_serializer(),
    )
    student_id = serializers.IntegerField(
        source="enrollment.student_id",
        read_only=True,
    )
    name = serializers.CharField(
        source="enrollment.student.name",
        read_only=True,
    )
    parent_phone = serializers.CharField(
        source="enrollment.student.parent_phone",
        read_only=True,
    )
    phone = serializers.CharField(
        source="enrollment.student.phone",
        read_only=True,
        allow_null=True,
    )
    lecture_title = serializers.CharField(
        source="session.lecture.title",
        read_only=True,
    )
    lecture_color = serializers.CharField(
        source="session.lecture.color",
        read_only=True,
        default="#3b82f6",
    )
    profile_photo_url = serializers.SerializerMethodField()
    name_highlight_clinic_target = serializers.SerializerMethodField()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = self.context.get("request")
        tenant = getattr(request, "tenant", None) if request else None
        if tenant:
            self.fields["session"].queryset = session_queryset_for_attendance_serializer(tenant)
            self.fields["enrollment_id"].queryset = enrollment_queryset_for_attendance_serializer(tenant)

    class Meta:
        model = Attendance
        fields = [
            "id",
            "session",
            "enrollment_id",
            "student_id",
            "status",
            "memo",
            "planned_arrival_date",
            "planned_arrival_time",
            "name",
            "parent_phone",
            "phone",
            "lecture_title",
            "lecture_color",
            "profile_photo_url",
            "name_highlight_clinic_target",
            "confirm_secession",
            "secession_scope",
        ]

    def update(self, instance, validated_data):
        # Withdrawal controls are consumed by the view's atomic workflow.
        validated_data.pop("confirm_secession", None)
        validated_data.pop("secession_scope", None)
        return super().update(instance, validated_data)

    def validate(self, attrs):
        request = self.context.get("request")
        tenant = getattr(request, "tenant", None) if request else None
        instance = self.instance

        session = attrs.get("session", getattr(instance, "session", None))
        enrollment = attrs.get("enrollment", getattr(instance, "enrollment", None))

        if instance is not None:
            if "session" in attrs and session and session.id != instance.session_id:
                raise serializers.ValidationError(
                    {"session": "출결의 차시는 단건 수정으로 변경할 수 없습니다."}
                )
            if "enrollment" in attrs and enrollment and enrollment.id != instance.enrollment_id:
                raise serializers.ValidationError(
                    {"enrollment_id": "출결의 수강 등록은 단건 수정으로 변경할 수 없습니다."}
                )

        if tenant is not None:
            if session is not None and session.lecture.tenant_id != tenant.id:
                raise serializers.ValidationError(
                    {"session": "현재 학원의 차시만 사용할 수 있습니다."}
                )
            if enrollment is not None and enrollment.tenant_id != tenant.id:
                raise serializers.ValidationError(
                    {"enrollment_id": "현재 학원의 수강 등록만 사용할 수 있습니다."}
                )

        if session is not None and enrollment is not None:
            if enrollment.lecture_id != session.lecture_id:
                raise serializers.ValidationError(
                    {"enrollment_id": "해당 차시의 강의에 등록된 수강생만 출결에 추가할 수 있습니다."}
                )

        plan_fields = {"planned_arrival_date", "planned_arrival_time"}
        if plan_fields.intersection(attrs) and session is not None:
            if session.session_type != session.SessionType.SUPPLEMENT:
                raise serializers.ValidationError(
                    {"planned_arrival_date": "등원 예정은 보강 차시에서만 입력할 수 있습니다."}
                )

        planned_date = attrs.get(
            "planned_arrival_date",
            getattr(instance, "planned_arrival_date", None),
        )
        planned_time = attrs.get(
            "planned_arrival_time",
            getattr(instance, "planned_arrival_time", None),
        )
        if planned_time is not None and planned_date is None:
            raise serializers.ValidationError(
                {"planned_arrival_time": "예정 시간을 입력하려면 날짜도 선택해 주세요."}
            )

        return attrs

    def get_profile_photo_url(self, obj):
        student = getattr(getattr(obj, "enrollment", None), "student", None)
        if not student:
            return None
        r2_key = getattr(student, "profile_photo_r2_key", None) or ""
        if not r2_key:
            return None
        try:
            from django.conf import settings
            from academy.adapters.storage.r2_presign import create_presigned_get_url
            return create_presigned_get_url(r2_key, expires_in=3600, bucket=settings.R2_STORAGE_BUCKET)
        except Exception:
            return None

    def _get_clinic_highlight_map(self):
        ctx = self.context
        if "_clinic_highlight_map" in ctx:
            return ctx["_clinic_highlight_map"]

        request = ctx.get("request")
        tenant = getattr(request, "tenant", None) if request else None
        if not tenant:
            ctx["_clinic_highlight_map"] = {}
            return {}

        instances = getattr(self.parent, "instance", None) if self.parent else None
        enrollment_ids = set()
        if instances is not None and hasattr(instances, "__iter__"):
            for att in instances:
                eid = getattr(att, "enrollment_id", None)
                if eid:
                    enrollment_ids.add(int(eid))

        if not enrollment_ids:
            ctx["_clinic_highlight_map"] = {}
            return {}

        highlight_map = clinic_highlight_map_for_attendance(
            tenant=tenant,
            enrollment_ids=enrollment_ids,
        )
        ctx["_clinic_highlight_map"] = highlight_map
        return highlight_map

    def get_name_highlight_clinic_target(self, obj):
        highlight_map = self._get_clinic_highlight_map()
        eid = getattr(obj, "enrollment_id", None)
        return highlight_map.get(int(eid), False) if eid else False


class AttendanceMatrixStudentSerializer(serializers.Serializer):
    student_id = serializers.IntegerField()
    name = serializers.CharField()
    phone = serializers.CharField(allow_null=True)
    parent_phone = serializers.CharField(allow_null=True)
    profile_photo_url = serializers.CharField(allow_null=True, required=False)
    name_highlight_clinic_target = serializers.BooleanField(default=False, required=False)
    attendance = serializers.DictField()
