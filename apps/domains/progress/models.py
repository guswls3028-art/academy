# apps/domains/progress/models.py
from __future__ import annotations

from django.db import models

from apps.api.common.models import TimestampModel


class ProgressPolicy(TimestampModel):
    """
    강의별 진행/통과 정책 (커스텀 가능)

    ✅ 1:N 시험 구조 대응 포인트
    - exam_aggregate_strategy: Session에 여러 Exam이 있을 때 Result를 어떻게 집계할지
    - exam_pass_source:
        - POLICY: ProgressPolicy.exam_pass_score 사용
        - EXAM: exams.Exam.pass_score 사용 (시험별 커트라인)
    """

    class HomeworkPassType(models.TextChoices):
        SUBMIT = "SUBMIT", "제출만"
        SCORE = "SCORE", "점수"
        TEACHER_APPROVAL = "TEACHER_APPROVAL", "강사승인"

    class ExamAggregateStrategy(models.TextChoices):
        MAX = "MAX", "최고점"
        AVG = "AVG", "평균"
        LATEST = "LATEST", "최근 제출"

    class ExamPassSource(models.TextChoices):
        POLICY = "POLICY", "정책 기준"
        EXAM = "EXAM", "시험 기준"

    lecture = models.OneToOneField(
        "lectures.Lecture",
        on_delete=models.CASCADE,
        related_name="progress_policy",
    )

    # ---------- Video ----------
    video_required_rate = models.PositiveIntegerField(default=90)  # 0~100

    # ---------- Exam (n~m 주차) ----------
    exam_start_session_order = models.PositiveIntegerField(default=2)
    exam_end_session_order = models.PositiveIntegerField(default=9999)

    # (레거시/정책형 커트라인)
    exam_pass_score = models.FloatField(default=60.0)

    exam_aggregate_strategy = models.CharField(
        max_length=10,
        choices=ExamAggregateStrategy.choices,
        default=ExamAggregateStrategy.MAX,
        help_text="Session에 여러 시험이 있을 때 Result 집계 방식",
    )

    exam_pass_source = models.CharField(
        max_length=10,
        choices=ExamPassSource.choices,
        default=ExamPassSource.EXAM,
        help_text="합격 기준을 정책(POLICY)으로 볼지, 시험(EXAM)마다 볼지",
    )

    # ---------- Homework (n~m 주차) ----------
    homework_start_session_order = models.PositiveIntegerField(default=2)
    homework_end_session_order = models.PositiveIntegerField(default=9999)
    homework_pass_type = models.CharField(
        max_length=30,
        choices=HomeworkPassType.choices,
        default=HomeworkPassType.TEACHER_APPROVAL,
    )

    # ======================================================
    # ✅ STEP 1: Homework cutline/rounding 정책 (프론트 설정 가능)
    # - 초기값: cutline 80%, round_unit 5%
    # ======================================================
    homework_cutline_percent = models.PositiveIntegerField(
        default=80,
        help_text="Homework pass cutline (%). 예: 80",
    )
    homework_round_unit = models.PositiveIntegerField(
        default=5,
        help_text="Homework percent rounding unit (%). 예: 5이면 5% 단위 반올림",
    )

    class Meta:
        ordering = ["-id"]

    def __str__(self):
        return f"Policy(lecture={self.lecture_id})"


class SessionProgress(TimestampModel):
    """
    Enrollment x Session 단위 진행 스냅샷
    """

    class AttendanceType(models.TextChoices):
        ONLINE = "online", "Online"
        OFFLINE = "offline", "Offline"

    enrollment = models.ForeignKey(
        "enrollment.Enrollment",
        on_delete=models.CASCADE,
        db_column="enrollment_id",
        related_name="session_progress_rows",
    )
    session = models.ForeignKey(
        "lectures.Session",
        on_delete=models.CASCADE,
        related_name="progress_rows",
    )

    # ----- attendance / video -----
    attendance_type = models.CharField(
        max_length=10,
        choices=AttendanceType.choices,
        default=AttendanceType.ONLINE,
    )
    video_progress_rate = models.PositiveIntegerField(default=0)  # 0~100
    video_completed = models.BooleanField(default=False)

    # ----- exam aggregate -----
    exam_attempted = models.BooleanField(default=False)
    exam_aggregate_score = models.FloatField(null=True, blank=True)
    exam_passed = models.BooleanField(default=False)
    exam_meta = models.JSONField(null=True, blank=True)

    # ----- homework -----
    homework_submitted = models.BooleanField(default=False)
    homework_passed = models.BooleanField(default=False)

    # ----- final -----
    completed = models.BooleanField(default=False)
    completed_at = models.DateTimeField(null=True, blank=True)

    # ----- meta -----
    calculated_at = models.DateTimeField(null=True, blank=True)
    meta = models.JSONField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["enrollment", "session"],
                name="unique_session_progress_per_enrollment",
            )
        ]
        ordering = ["-updated_at", "-id"]

    def __str__(self):
        return (
            f"SessionProgress(enroll={self.enrollment_id}, "
            f"session={self.session_id}, completed={self.completed})"
        )


class LectureProgress(TimestampModel):
    class RiskLevel(models.TextChoices):
        NORMAL = "NORMAL", "Normal"
        WARNING = "WARNING", "Warning"
        DANGER = "DANGER", "Danger"

    enrollment = models.OneToOneField(
        "enrollment.Enrollment",
        on_delete=models.CASCADE,
        db_column="enrollment_id",
        related_name="lecture_progress_rows",
    )
    lecture = models.ForeignKey(
        "lectures.Lecture",
        on_delete=models.CASCADE,
        related_name="lecture_progress_rows",
    )

    total_sessions = models.PositiveIntegerField(default=0)
    completed_sessions = models.PositiveIntegerField(default=0)
    failed_sessions = models.PositiveIntegerField(default=0)

    consecutive_failed_sessions = models.PositiveIntegerField(default=0)
    risk_level = models.CharField(
        max_length=10,
        choices=RiskLevel.choices,
        default=RiskLevel.NORMAL,
    )

    last_session = models.ForeignKey(
        "lectures.Session",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="last_lecture_progress_rows",
    )
    last_updated = models.DateTimeField(null=True, blank=True)

    meta = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ["-updated_at", "-id"]

    def __str__(self):
        return f"LectureProgress(enroll={self.enrollment_id}, lecture={self.lecture_id}, risk={self.risk_level})"


class ClinicLink(TimestampModel):
    """
    학습 실패 → 클리닉 대상 연결 (remediation case)

    핵심 의미: "이 학생(enrollment)이 이 차시(session)에서 실패하여 클리닉 대상이 됨"
    해소(resolved_at)는 실제 시험/과제 통과 또는 관리자 명시 해소 시에만 설정.
    예약/출석은 운영 이벤트이며 해소 트리거가 아님.
    """

    class Reason(models.TextChoices):
        AUTO_FAILED = "AUTO_FAILED", "자동(차시 미통과)"
        AUTO_RISK = "AUTO_RISK", "자동(위험 알림)"
        MANUAL_REQUEST = "MANUAL_REQUEST", "수동(학생/학부모 요청)"
        TEACHER_RECOMMEND = "TEACHER_RECOMMEND", "강사 추천"

    class ResolutionType(models.TextChoices):
        EXAM_PASS = "EXAM_PASS", "시험 통과"
        HOMEWORK_PASS = "HOMEWORK_PASS", "과제 통과"
        MANUAL_OVERRIDE = "MANUAL_OVERRIDE", "관리자 수동 해소"
        WAIVED = "WAIVED", "면제"
        CARRIED_OVER = "CARRIED_OVER", "다음 차수로 이월"
        SOURCE_REMOVED = "SOURCE_REMOVED", "원본 삭제"
        NOT_SUBMITTED = "NOT_SUBMITTED", "시험 미응시 전환"
        GRADING_RETRACTED = "GRADING_RETRACTED", "채점 미완료 투영 철회"
        BOOKING_LEGACY = "BOOKING_LEGACY", "레거시(예약 기반)"

    tenant = models.ForeignKey(
        "core.Tenant",
        on_delete=models.CASCADE,
        related_name="clinic_links",
        db_index=True,
    )
    enrollment = models.ForeignKey(
        "enrollment.Enrollment",
        on_delete=models.CASCADE,
        related_name="clinic_links",
        db_column="enrollment_id",
        db_index=True,
    )
    session = models.ForeignKey(
        "lectures.Session",
        on_delete=models.CASCADE,
        related_name="clinic_links",
    )

    reason = models.CharField(max_length=30, choices=Reason.choices)
    is_auto = models.BooleanField(default=False)
    approved = models.BooleanField(default=False)

    # --- resolution fields ---
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_type = models.CharField(
        max_length=30,
        choices=ResolutionType.choices,
        null=True,
        blank=True,
        help_text=(
            "해소 유형: 시험통과/과제통과/수동해소/면제/원본삭제/"
            "시험미응시/채점미완료투영철회/레거시"
        ),
    )
    resolution_evidence = models.JSONField(
        null=True,
        blank=True,
        help_text="해소 근거: {exam_id, attempt_id, homework_id, score, ...}",
    )

    # ✅ 감사 이력: 해소/복원/이월 전이 기록 (append-only)
    # 각 entry: {"at": iso, "action": "resolve|unresolve|carry_over", "resolution_type": "...", "evidence": {...}}
    resolution_history = models.JSONField(
        default=list,
        blank=True,
        help_text="해소/복원 전이 이력(append-only): 이전 evidence 및 액션 기록",
    )

    # --- source tracking (V1.1.2: 시험/과제별 개별 추적) ---
    source_type = models.CharField(
        max_length=20,
        choices=[("exam", "시험"), ("homework", "과제")],
        null=True,
        blank=True,
        help_text="출처 유형: exam 또는 homework",
    )
    source_id = models.IntegerField(
        null=True,
        blank=True,
        help_text="출처 ID: exam.id 또는 homework.id",
    )

    # --- cycle tracking ---
    cycle_no = models.PositiveIntegerField(
        default=1,
        help_text="클리닉 차수: 1차, 2차, 3차...",
    )

    memo = models.TextField(null=True, blank=True)
    meta = models.JSONField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["enrollment_id", "created_at"]),
            models.Index(fields=["session", "created_at"]),
            models.Index(fields=["reason"]),
            models.Index(fields=["resolved_at"]),
            models.Index(fields=["resolution_type"]),
            models.Index(fields=["source_type", "source_id"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["enrollment", "session", "source_type", "source_id", "cycle_no"],
                name="uniq_cliniclink_per_source_cycle",
                condition=models.Q(source_type__isnull=False, source_id__isnull=False),
            ),
        ]
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"ClinicLink(enroll={self.enrollment_id}, session={self.session_id}, reason={self.reason}, cycle={self.cycle_no})"


class AssessmentCorrection(TimestampModel):
    """시험 오답/과제 검사를 점수와 독립적으로 교사가 확인한 상태."""

    class SourceType(models.TextChoices):
        EXAM = "exam", "시험"
        HOMEWORK = "homework", "과제"

    tenant = models.ForeignKey(
        "core.Tenant",
        on_delete=models.CASCADE,
        related_name="assessment_corrections",
    )
    enrollment = models.ForeignKey(
        "enrollment.Enrollment",
        on_delete=models.CASCADE,
        related_name="assessment_corrections",
    )
    session = models.ForeignKey(
        "lectures.Session",
        on_delete=models.CASCADE,
        related_name="assessment_corrections",
    )
    source_type = models.CharField(max_length=20, choices=SourceType.choices)
    source_id = models.PositiveIntegerField()
    completed = models.BooleanField(default=False)
    completed_at = models.DateTimeField(null=True, blank=True)
    note = models.TextField(
        blank=True,
        default="",
        help_text="교사가 남긴 미완료 범위 또는 확인 메모.",
    )
    source_updated_at_snapshot = models.DateTimeField(
        null=True,
        blank=True,
        help_text="완료 확인 당시 원본 점수의 updated_at. 점수가 바뀌면 완료 상태를 무효화한다.",
    )
    source_fingerprint = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        help_text="완료 확인 당시 시험 점수·답안 내용의 SHA-256 지문.",
    )
    updated_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_assessment_corrections",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "enrollment", "session", "source_type", "source_id"],
                name="uniq_assess_correction_source",
            ),
        ]
        indexes = [
            models.Index(
                fields=["session", "enrollment"],
                name="progress_ac_session_enroll_idx",
            ),
            models.Index(
                fields=["tenant", "completed"],
                name="progress_ac_tenant_done_idx",
            ),
        ]
        ordering = ["-updated_at", "-id"]


class RiskLog(TimestampModel):
    class RiskLevel(models.TextChoices):
        WARNING = "WARNING", "Warning"
        DANGER = "DANGER", "Danger"

    class Rule(models.TextChoices):
        CONSECUTIVE_INCOMPLETE = "CONSECUTIVE_INCOMPLETE", "연속 미완료"
        CONSECUTIVE_LOW_SCORE = "CONSECUTIVE_LOW_SCORE", "연속 저점수"
        OTHER = "OTHER", "기타"

    enrollment = models.ForeignKey(
        "enrollment.Enrollment",
        on_delete=models.CASCADE,
        related_name="risk_logs",
        db_column="enrollment_id",
        db_index=True,
    )
    session = models.ForeignKey(
        "lectures.Session",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="risk_logs",
    )

    risk_level = models.CharField(max_length=10, choices=RiskLevel.choices)
    rule = models.CharField(max_length=40, choices=Rule.choices)

    reason = models.TextField(null=True, blank=True)
    meta = models.JSONField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["enrollment_id", "created_at"]),
            models.Index(fields=["risk_level", "created_at"]),
            models.Index(fields=["rule", "created_at"]),
        ]
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"RiskLog(enroll={self.enrollment_id}, level={self.risk_level}, rule={self.rule})"
