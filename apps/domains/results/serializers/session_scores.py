# PATH: apps/domains/results/serializers/session_scores.py
"""
SessionScores Serializer (Score Tab)

✅ 설계 고정(중요)
- 이 Serializer는 "표시용 DTO" 이다.
- 도메인 로직/판정/정책 계산을 하지 않는다.
- View에서 만들어준 dict를 그대로 validate/serialize만 수행한다.

✅ 시험 점수 정합성 (백엔드 유지)
- score(합산) = objective_score + subjective_score (subjective_score = sum(ResultItem))
- passed = (score >= pass_score), 서버에서만 계산하여 내려줌.

✅ 프론트 계약
- score === null 은 "미산출/미응시/처리중" 의미
- is_locked / lock_reason 은 입력 비활성화 + tooltip 용도
"""


from __future__ import annotations

from rest_framework import serializers


class ScoreBlockSerializer(serializers.Serializer):
    score = serializers.FloatField(allow_null=True)
    max_score = serializers.FloatField(allow_null=True)

    # 객관식/주관식 (시험만). 합산 = score = objective + subjective
    objective_score = serializers.FloatField(allow_null=True, required=False, default=None)
    subjective_score = serializers.FloatField(allow_null=True, required=False, default=None)

    passed = serializers.BooleanField(allow_null=True)
    clinic_required = serializers.BooleanField()

    is_locked = serializers.BooleanField()
    lock_reason = serializers.CharField(allow_null=True, allow_blank=True)

    # 과제만: 미제출 등 meta.status (NOT_SUBMITTED)
    meta = serializers.DictField(allow_null=True, required=False, default=None)
    updated_at = serializers.DateTimeField(allow_null=True, required=False, default=None)

    # 시험 성취 SSOT: 1차 성적과 보강/재시험 최종 통과를 분리한다.
    remediated = serializers.BooleanField(allow_null=True, required=False, default=None)
    final_pass = serializers.BooleanField(allow_null=True, required=False, default=None)
    achievement = serializers.CharField(allow_null=True, allow_blank=True, required=False, default=None)
    is_provisional = serializers.BooleanField(required=False, default=False)
    clinic_retake = serializers.DictField(allow_null=True, required=False, default=None)
    correction_status = serializers.ChoiceField(
        choices=["PENDING", "COMPLETED", "NOT_REQUIRED"],
        allow_null=True,
        required=False,
        default=None,
    )
    correction_completed_at = serializers.DateTimeField(
        allow_null=True,
        required=False,
        default=None,
    )
    correction_note = serializers.CharField(
        allow_blank=True,
        required=False,
        default="",
    )
    correction_updated_at = serializers.DateTimeField(
        allow_null=True,
        required=False,
        default=None,
    )
    teacher_resolved = serializers.BooleanField(required=False, default=False)


class AttemptSummarySerializer(serializers.Serializer):
    attempt_index = serializers.IntegerField()
    score = serializers.FloatField(allow_null=True)
    max_score = serializers.FloatField(allow_null=True, required=False, default=None)
    pass_score = serializers.FloatField(allow_null=True, required=False, default=None)
    passed = serializers.BooleanField(allow_null=True)
    at = serializers.DateTimeField(allow_null=True)
    source = serializers.CharField()
    meta_status = serializers.CharField(allow_null=True, allow_blank=True, required=False, default=None)


class ExamScoreBlockSerializer(serializers.Serializer):
    exam_id = serializers.IntegerField()
    title = serializers.CharField(allow_blank=True)
    pass_score = serializers.FloatField()

    block = ScoreBlockSerializer()

    # 주관식 점수 입력 모드용 문항별 점수 (ResultItem)
    items = serializers.ListField(
        child=serializers.DictField(),
        allow_empty=True,
        required=False,
        default=list,
    )

    # V1.1.1: 차수별 편집 지원
    attempt_count = serializers.IntegerField(default=0)
    clinic_link_id = serializers.IntegerField(allow_null=True, default=None)
    attempts = AttemptSummarySerializer(many=True, required=False, default=list)


class HomeworkScoreBlockSerializer(serializers.Serializer):
    homework_id = serializers.IntegerField()
    title = serializers.CharField(allow_blank=True)

    block = ScoreBlockSerializer()

    # V1.1.1: 차수별 편집 지원
    attempt_count = serializers.IntegerField(default=0)
    clinic_link_id = serializers.IntegerField(allow_null=True, default=None)


class SessionScoreRowSerializer(serializers.Serializer):
    enrollment_id = serializers.IntegerField()
    student_id = serializers.IntegerField(allow_null=True)
    student_name = serializers.CharField(allow_blank=True)
    attendance_status = serializers.CharField(
        allow_null=True,
        allow_blank=True,
        required=False,
        default=None,
    )
    assessment_todo_eligible = serializers.BooleanField(default=True)

    exams = ExamScoreBlockSerializer(many=True)
    homeworks = HomeworkScoreBlockSerializer(many=True)

    updated_at = serializers.DateTimeField(allow_null=True)

    # 행 단위 클리닉 대상 여부(ClinicLink 기준). 판정 컬럼 "대상"|"합격" 표시용
    clinic_required = serializers.BooleanField(default=False)
    # 최종 차시 진행 상태 SSOT. completed면 미제출/1차 불합격 잔상이 있어도 현재 클리닉 대상이 아니다.
    progress_completed = serializers.BooleanField(default=False)
    progress_status = serializers.ChoiceField(
        choices=["completed", "in_progress"],
        default="in_progress",
    )
    # 클리닉 대상이면서 해당 주차 클리닉 미수강 시 이름만 노란 형광펜 하이라이트(수강 완료 시 제거)
    name_highlight_clinic_target = serializers.BooleanField(default=False)
    # 현재 대표 결과 기준 누적 시험 미응시 횟수. 1회 이상이면 이름 음영 표시.
    exam_not_submitted_count = serializers.IntegerField(default=0, min_value=0)
    correction_pending_count = serializers.IntegerField(default=0, min_value=0)
    # 점수 합불과 별개인 오답 확인 상태를 우선하고, 미응시 등 점수 없는 클리닉 대상도 포함한다.
    name_highlight_followup_required = serializers.BooleanField(default=False)

    # ✅ 학생 SSOT 표시용: 아바타 + 강의 딱지
    profile_photo_url = serializers.CharField(allow_null=True, allow_blank=True, required=False, default=None)
    lecture_title = serializers.CharField(allow_null=True, allow_blank=True, required=False, default=None)
    lecture_color = serializers.CharField(allow_null=True, allow_blank=True, required=False, default=None)
    lecture_chip_label = serializers.CharField(allow_null=True, allow_blank=True, required=False, default=None)


class AssessmentCorrectionUpdateSerializer(serializers.Serializer):
    enrollment_id = serializers.IntegerField(min_value=1)
    source_type = serializers.ChoiceField(choices=["exam", "homework"])
    source_id = serializers.IntegerField(min_value=1)
    completed = serializers.BooleanField()
    note = serializers.CharField(
        allow_blank=True,
        max_length=500,
        required=False,
    )
    expected_updated_at = serializers.DateTimeField(
        allow_null=True,
        required=False,
    )
