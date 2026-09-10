# PATH: apps/domains/clinic/models.py

import datetime

from django.db import models
from django.conf import settings

from apps.api.common.models import TimestampModel
from apps.core.models import Tenant


# --------------------------------------------------
# Clinic Session
# --------------------------------------------------

class Session(TimestampModel):
    GRADE_CHOICES = [
        (1, "1학년"), (2, "2학년"), (3, "3학년"),
        (4, "4학년"), (5, "5학년"), (6, "6학년"),
    ]

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="clinic_sessions",
        db_index=True,  # ✅ tenant_id 인덱스 추가
    )

    title = models.CharField(max_length=255, blank=True, default="")
    date = models.DateField()
    start_time = models.TimeField()

    duration_minutes = models.PositiveIntegerField(default=60)

    location = models.CharField(max_length=255)
    max_participants = models.PositiveIntegerField()

    # 대상 학년: null이면 전체 학년 대상, 1/2/3이면 해당 학년만
    target_grade = models.PositiveSmallIntegerField(
        choices=GRADE_CHOICES,
        null=True,
        blank=True,
        help_text="대상 학년. 비어있으면 전체 학년.",
    )

    # 대상 학교 유형: null이면 전체, MIDDLE/HIGH이면 해당 유형만
    SCHOOL_TYPE_CHOICES = [("ELEMENTARY", "초등"), ("MIDDLE", "중등"), ("HIGH", "고등")]
    target_school_type = models.CharField(
        max_length=10,
        choices=SCHOOL_TYPE_CHOICES,
        null=True,
        blank=True,
        help_text="대상 학교 유형. 비어있으면 전체.",
    )

    # 대상 반: section_mode=true + clinic_mode=regular에서 사용. null이면 전체
    section = models.ForeignKey(
        "lectures.Section",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="clinic_sessions",
        help_text="대상 반. section_mode에서만 사용. 비어있으면 전체.",
    )

    # 대상 강의: 비어있으면 전체, 지정하면 해당 강의 수강생만
    target_lectures = models.ManyToManyField(
        "lectures.Lecture",
        blank=True,
        related_name="clinic_sessions_by_lecture",
        help_text="대상 강의. 비어있으면 전체 학생.",
    )

    memo = models.TextField(blank=True, default="", help_text="세션 메모 (운영용)")

    allow_time_preference = models.BooleanField(
        default=False,
        db_default=False,
        help_text="학생이 세션 범위 안의 희망 시작·종료 시각을 요청할 수 있으면 True.",
    )
    allow_multi_slot_booking = models.BooleanField(
        default=False,
        db_default=False,
        help_text="같은 날짜의 다른 클리닉 시간대도 함께 예약할 수 있으면 True.",
    )
    booking_mode = models.CharField(
        max_length=16,
        choices=(("fixed_slot", "고정 시간대"), ("time_range", "시간 범위")),
        default="fixed_slot",
        db_default="fixed_slot",
        help_text="세션 생성 시 고정된 예약 방식 스냅샷입니다.",
    )
    booking_interval_minutes = models.PositiveSmallIntegerField(
        default=60,
        db_default=60,
        help_text="시간 범위 예약의 선택 간격 스냅샷입니다.",
    )
    booking_max_stay_minutes = models.PositiveIntegerField(
        default=240,
        db_default=240,
        help_text="시간 범위 예약의 최대 체류 분 스냅샷입니다.",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_clinic_sessions",
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(booking_mode__in=("fixed_slot", "time_range")),
                name="clinic_session_booking_mode_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(booking_interval_minutes__in=(30, 60)),
                name="clinic_session_booking_interval_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    booking_max_stay_minutes__gte=models.F("booking_interval_minutes")
                ),
                name="clinic_session_booking_stay_gte_interval",
            ),
            # ✅ 학년 지정 세션: 같은 시간/장소/학년 중복 방지
            models.UniqueConstraint(
                fields=["tenant", "date", "start_time", "location", "target_grade"],
                name="uniq_clinic_session_per_tenant_time_loc_grade",
                condition=models.Q(target_grade__isnull=False),
            ),
            # ✅ 전체 학년 세션: 같은 시간/장소 중복 방지
            models.UniqueConstraint(
                fields=["tenant", "date", "start_time", "location"],
                name="uniq_clinic_session_per_tenant_time_location",
                condition=models.Q(target_grade__isnull=True),
            ),
        ]

    def __str__(self):
        grade = f" ({self.get_target_grade_display()})" if self.target_grade else ""
        title = f" {self.title}" if self.title else ""
        return f"{self.date} {self.start_time}{title}{grade} @ {self.location}"


# --------------------------------------------------
# Session Participant
# --------------------------------------------------

class SessionParticipant(TimestampModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"  # 학생 예약 신청 대기
        BOOKED = "booked", "Booked"
        ATTENDED = "attended", "Attended"
        NO_SHOW = "no_show", "NoShow"
        CANCELLED = "cancelled", "Cancelled"
        REJECTED = "rejected", "Rejected"  # 선생이 거부

    class Source(models.TextChoices):
        AUTO = "auto", "Auto"
        MANUAL = "manual", "Manual"
        STUDENT_REQUEST = "student_request", "Student Request"  # 학생 신청

    class CheckoutMode(models.TextChoices):
        ARRIVAL_RECORDED = "arrival_recorded", "Arrival recorded"
        ARRIVAL_NOT_RECORDED = "arrival_not_recorded", "Arrival not recorded"

    class Reason(models.TextChoices):
        EXAM = "exam", "Exam"
        HOMEWORK = "homework", "Homework"
        BOTH = "both", "Both"

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="clinic_participants",
        db_index=True,  # ✅ tenant_id 인덱스 추가
    )

    session = models.ForeignKey(
        Session,
        on_delete=models.CASCADE,
        related_name="participants",
        null=True,  # ✅ 학생 신청 시 세션이 없을 수 있음
        blank=True,
    )
    
    # ✅ 학생 신청 시 요청한 날짜/시간 (세션이 없을 때 사용)
    requested_date = models.DateField(null=True, blank=True)
    requested_start_time = models.TimeField(null=True, blank=True)
    
    student = models.ForeignKey(
        "students.Student",
        on_delete=models.CASCADE,
        related_name="clinic_participations",
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.BOOKED,
    )

    source = models.CharField(
        max_length=20,
        choices=Source.choices,
        default=Source.AUTO,
    )
    enrollment = models.ForeignKey(
        "enrollment.Enrollment",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="clinic_participations",
    )
    clinic_reason = models.CharField(
        max_length=20,
        choices=Reason.choices,
        null=True,
        blank=True,
    )

    participant_role = models.CharField(
        max_length=20,
        choices=(("target", "Target"), ("manual", "Manual")),
        default="target",
    )

    status_changed_at = models.DateTimeField(null=True, blank=True)
    status_changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="clinic_participant_status_changes",
    )

    checked_in_at = models.DateTimeField(null=True, blank=True)
    is_late = models.BooleanField(default=False)

    checked_out_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="클리닉 하원 처리 시각",
    )
    checked_out_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="clinic_check_outs",
    )
    checkout_mode = models.CharField(
        max_length=24,
        choices=CheckoutMode.choices,
        blank=True,
        default="",
        db_default="",
        help_text=(
            "하원 처리 당시 등원 기록의 존재 여부. arrival_not_recorded는 등원을 "
            "추정하거나 생성하지 않은 명시적 예외 처리다."
        ),
    )

    completed_at = models.DateTimeField(null=True, blank=True, help_text="자율학습 완료 시각")
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="clinic_completions",
    )
    completion_history = models.JSONField(
        default=list,
        db_default=models.Value([], output_field=models.JSONField()),
        blank=True,
        help_text="완료/완료 취소의 append-only 감사 이력",
    )

    memo = models.TextField(blank=True, null=True)
    student_request_memo = models.TextField(
        blank=True,
        default="",
        db_default="",
        help_text="학생·학부모가 남긴 요청사항. 작성 출처가 명확한 경우에만 저장.",
    )
    preferred_start_time = models.TimeField(
        null=True,
        blank=True,
        help_text="세션 안에서 요청한 희망 시작 시각. 실제 예약 시작 시각과 별개.",
    )
    preferred_end_time = models.TimeField(
        null=True,
        blank=True,
        help_text="세션 안에서 요청한 희망 종료 시각. 실제 예약 종료 시각과 별개.",
    )
    booking_start_time = models.TimeField(
        null=True,
        blank=True,
        help_text="시간 범위 방식에서 확정된 실제 예약 시작 시각입니다.",
    )
    booking_end_time = models.TimeField(
        null=True,
        blank=True,
        help_text="시간 범위 방식에서 확정된 실제 예약 종료 시각입니다.",
    )
    staff_memo = models.TextField(
        blank=True,
        default="",
        db_default="",
        help_text="학생·학부모에게 노출하지 않는 교직원 인수인계 메모",
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(booking_start_time__isnull=True, booking_end_time__isnull=True)
                    | models.Q(booking_start_time__isnull=False, booking_end_time__isnull=False)
                ),
                name="clinic_participant_booking_range_pair",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(booking_start_time__isnull=True)
                    | models.Q(booking_start_time__lt=models.F("booking_end_time"))
                    | models.Q(
                        booking_start_time__gt=datetime.time.min,
                        booking_end_time=datetime.time.min,
                    )
                ),
                name="clinic_participant_booking_range_order",
            ),
            models.UniqueConstraint(
                fields=["tenant", "session", "student"],
                name="uniq_clinic_participant_active",
                condition=models.Q(session__isnull=False, status__in=["pending", "booked"]),
            ),
            models.UniqueConstraint(
                fields=["tenant", "requested_date", "requested_start_time", "student"],
                name="uniq_clinic_participant_request_per_tenant",
                condition=models.Q(session__isnull=True),  # session이 없을 때만 유니크 제약
            ),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.student.name} in {self.session} ({self.status})"


class SessionParticipantPlanItem(TimestampModel):
    """Auditable selection of one unresolved ClinicLink for today's clinic work."""

    participant = models.ForeignKey(
        SessionParticipant,
        on_delete=models.CASCADE,
        related_name="plan_items",
    )
    clinic_link = models.ForeignKey(
        "progress.ClinicLink",
        on_delete=models.CASCADE,
        related_name="session_participant_plan_items",
    )
    selected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="selected_clinic_participant_plan_items",
    )
    removed_at = models.DateTimeField(null=True, blank=True)
    removed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="removed_clinic_participant_plan_items",
    )
    removal_reason = models.CharField(max_length=80, blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["participant", "clinic_link"],
                condition=models.Q(removed_at__isnull=True),
                name="uniq_active_clinic_participant_plan_item",
            ),
        ]
        indexes = [
            models.Index(fields=["participant", "removed_at"]),
            models.Index(fields=["clinic_link", "removed_at"]),
        ]
        ordering = ["clinic_link_id", "id"]


# --------------------------------------------------
# Clinic Test
# --------------------------------------------------

class Test(TimestampModel):
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="clinic_tests",
        db_index=True,  # ✅ tenant_id 인덱스 추가
    )

    session = models.ForeignKey(
        Session,
        on_delete=models.CASCADE,
        related_name="tests",
    )
    title = models.CharField(max_length=255)
    round = models.PositiveIntegerField(default=1)
    date = models.DateField()

    class Meta:
        ordering = ["-date", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "session", "round"],
                name="uniq_clinic_test_per_tenant_session_round",
            )
        ]

    def __str__(self):
        return f"{self.title} ({self.round}차)"


# --------------------------------------------------
# Submission
# --------------------------------------------------

def submission_upload_path(instance, filename):
    return f"clinic/submissions/{instance.student_id}/{instance.test_id}/{filename}"


class Submission(TimestampModel):
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="clinic_submissions",
        db_index=True,  # ✅ tenant_id 인덱스 추가
    )

    test = models.ForeignKey(
        Test,
        on_delete=models.CASCADE,
        related_name="submissions",
    )
    student = models.ForeignKey(
        "students.Student",
        on_delete=models.CASCADE,
        related_name="clinic_submissions",
    )

    file = models.FileField(upload_to=submission_upload_path, null=True, blank=True)
    score = models.FloatField(null=True, blank=True)

    status = models.CharField(
        max_length=20,
        choices=(
            ("pending", "Pending"),
            ("passed", "Passed"),
            ("failed", "Failed"),
        ),
        default="pending",
    )
    remark = models.TextField(blank=True, null=True)
    graded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "test", "student"],
                name="uniq_clinic_submission_per_tenant",
            )
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.student.name} - {self.test.title}"
