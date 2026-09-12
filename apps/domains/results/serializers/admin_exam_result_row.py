# apps/domains/results/serializers/admin_exam_result_row.py
from rest_framework import serializers


class AdminExamResultRowSerializer(serializers.Serializer):
    enrollment_id = serializers.IntegerField()
    student_name = serializers.CharField()

    # =====================================
    # 🔧 PATCH: 점수 필드 명시적 분리
    # - SessionScores / AdminExamResults 공용 계약
    # - 프론트 수정 없이 확장 가능
    # =====================================
    # 미응시(NOT_SUBMITTED)·미채점(provisional) 케이스에서 0 으로 coerce 하면
    # "0/100" 으로 잘못 표시되므로 None 그대로 보존.
    exam_score = serializers.FloatField(allow_null=True)
    exam_max_score = serializers.FloatField(allow_null=True)

    final_score = serializers.FloatField(allow_null=True)
    passed = serializers.BooleanField(allow_null=True)
    clinic_required = serializers.BooleanField()

    # ✅ 성취 SSOT (exam_achievement.py 기준). student_result_service와 동일 계약.
    remediated = serializers.BooleanField(required=False, default=False)
    final_pass = serializers.BooleanField(allow_null=True, required=False, default=None)
    achievement = serializers.CharField(allow_null=True, required=False, default=None)
    clinic_retake = serializers.JSONField(allow_null=True, required=False, default=None)
    is_provisional = serializers.BooleanField(required=False, default=False)
    meta_status = serializers.CharField(allow_null=True, required=False, default=None)
    grading_status = serializers.ChoiceField(
        choices=["subjective_pending"],
        allow_null=True,
        required=False,
        default=None,
    )

    submitted_at = serializers.DateTimeField(allow_null=True)

    # ✅ 클리닉 대상 하이라이트
    name_highlight_clinic_target = serializers.BooleanField(default=False)
    # 현재 대표 결과 기준 누적 시험 미응시 횟수. 1회 이상이면 이름 음영 표시.
    exam_not_submitted_count = serializers.IntegerField(default=0, min_value=0)

    # ✅ 학생 SSOT 표시용: 아바타 + 강의 딱지
    profile_photo_url = serializers.CharField(allow_null=True, allow_blank=True, required=False, default=None)
    lecture_title = serializers.CharField(allow_null=True, allow_blank=True, required=False, default=None)
    lecture_color = serializers.CharField(allow_null=True, allow_blank=True, required=False, default=None)
    lecture_chip_label = serializers.CharField(allow_null=True, allow_blank=True, required=False, default=None)
    lecture_id = serializers.IntegerField()
    pass_score = serializers.FloatField()

    # ===============================
    # 석차 정보
    # ===============================
    rank = serializers.IntegerField(allow_null=True, required=False, default=None)
    ranking_score = serializers.FloatField(allow_null=True, required=False, default=None)
    percentile = serializers.FloatField(allow_null=True, required=False, default=None)
    cohort_size = serializers.IntegerField(allow_null=True, required=False, default=None)
    cohort_avg = serializers.FloatField(allow_null=True, required=False, default=None)

    # ===============================
    # 🔥 Submission 연동 필드 (기존 유지)
    # ===============================
    submission_id = serializers.IntegerField(allow_null=True)
    submission_status = serializers.CharField(allow_null=True)
    result_status = serializers.ChoiceField(
        choices=["NOT_SUBMITTED", "PROCESSING", "PARTIAL", "DONE", "FAILED"],
    )

    # 같은 수강 강의에서 차시를 하나로 확정할 수 있을 때만 오답 확인 상태를 노출한다.
    correction_session_id = serializers.IntegerField(
        allow_null=True,
        required=False,
        default=None,
    )
    correction_status = serializers.ChoiceField(
        choices=["PENDING", "COMPLETED", "NOT_REQUIRED"],
        allow_null=True,
        required=False,
        default=None,
    )
