from rest_framework import serializers


class TeacherOpsAnalyzeSerializer(serializers.Serializer):
    images = serializers.ListField(
        child=serializers.ImageField(),
        allow_empty=False,
        min_length=1,
        max_length=5,
    )
    message = serializers.CharField(
        required=True,
        allow_blank=False,
        trim_whitespace=True,
        max_length=1000,
    )
    previous_proposal_token = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=20000,
    )

    def validate_images(self, values):
        for value in values:
            if value.size > 8 * 1024 * 1024:
                raise serializers.ValidationError("사진은 장당 8MB 이하만 올릴 수 있습니다.")
            content_type = str(getattr(value, "content_type", "") or "").lower()
            if content_type not in {"image/jpeg", "image/png", "image/webp"}:
                raise serializers.ValidationError("JPG, PNG, WEBP 사진만 올릴 수 있습니다.")
        return values


class TeacherOpsConfirmRowSerializer(serializers.Serializer):
    row_id = serializers.CharField(max_length=36)
    enabled = serializers.BooleanField(default=True)
    name = serializers.CharField(max_length=100, trim_whitespace=True)
    student_phone = serializers.CharField(max_length=32, allow_blank=True, required=False)
    parent_phone = serializers.CharField(max_length=32, allow_blank=True, required=False)
    initial_password = serializers.CharField(
        min_length=4,
        max_length=128,
        allow_blank=True,
        required=False,
        write_only=True,
    )
    school = serializers.CharField(max_length=255, allow_blank=True, required=False)
    school_type = serializers.ChoiceField(
        choices=[("ELEMENTARY", "초등"), ("MIDDLE", "중등"), ("HIGH", "고등")],
        required=False,
        default="HIGH",
    )
    grade = serializers.CharField(max_length=20, allow_blank=True, required=False)
    selected_lecture_id = serializers.IntegerField(min_value=1, allow_null=True, required=False)
    session_order = serializers.IntegerField(min_value=1, max_value=500, allow_null=True, required=False)
    remove_enrollment_id = serializers.IntegerField(min_value=1, allow_null=True, required=False)


class TeacherOpsConfirmSerializer(serializers.Serializer):
    proposal_token = serializers.CharField(max_length=20000)
    rows = TeacherOpsConfirmRowSerializer(many=True, allow_empty=False, max_length=5)

    def validate_rows(self, rows):
        row_ids = [row["row_id"] for row in rows]
        if len(set(row_ids)) != len(row_ids):
            raise serializers.ValidationError("같은 학생 요청이 중복되었습니다.")
        if not any(row.get("enabled", True) for row in rows):
            raise serializers.ValidationError("처리할 학생을 한 명 이상 선택해 주세요.")
        return rows
