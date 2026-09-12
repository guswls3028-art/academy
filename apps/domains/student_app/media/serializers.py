from rest_framework import serializers


class StudentVideoListItemSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    session_id = serializers.IntegerField(allow_null=True)
    enrollment_id = serializers.IntegerField(allow_null=True, required=False)
    title = serializers.CharField()

    status = serializers.CharField()
    source_type = serializers.CharField(required=False, default="s3")
    youtube_video_id = serializers.CharField(required=False, allow_blank=True, default="")
    youtube_url = serializers.CharField(required=False, allow_blank=True, default="")
    thumbnail_url = serializers.CharField(allow_null=True, required=False)
    duration = serializers.IntegerField(allow_null=True, required=False)

    # 진행률 (0-100)
    progress = serializers.FloatField(required=False, default=0)
    completed = serializers.BooleanField(required=False, default=False)
    last_position = serializers.IntegerField(required=False, default=0)
    updated_at = serializers.DateTimeField(required=False, allow_null=True)
    created_at = serializers.DateTimeField(required=False, allow_null=True)
    order = serializers.IntegerField(required=False, default=0)

    # 소셜 카운터
    view_count = serializers.IntegerField(required=False, default=0)
    like_count = serializers.IntegerField(required=False, default=0)
    comment_count = serializers.IntegerField(required=False, default=0)
    is_liked = serializers.BooleanField(required=False, default=False)

    # 정책(단일 진실)
    allow_skip = serializers.BooleanField()
    max_speed = serializers.FloatField()
    show_watermark = serializers.BooleanField()

    # 학생별 적용 룰 (Legacy) — enrollment 없을 때 None
    effective_rule = serializers.ChoiceField(
        choices=["free", "once", "blocked"],
        required=False,
        allow_null=True,
    )
    # 접근 모드 — enrollment 없을 때 None (전체공개 등)
    access_mode = serializers.ChoiceField(
        choices=["FREE_REVIEW", "PROCTORED_CLASS", "BLOCKED"],
        required=False,
        allow_null=True,
    )


class StudentVideoPlaybackRequestSerializer(serializers.Serializer):
    event_protocol_version = serializers.ChoiceField(choices=[1, 2], default=1)


class StudentVideoPlaybackSerializer(serializers.Serializer):
    """
    학생 플레이어가 신뢰하는 단일 진실 payload
    """
    video = StudentVideoListItemSerializer()
    hls_url = serializers.CharField(allow_null=True, required=False)
    mp4_url = serializers.CharField(allow_null=True, required=False)
    play_url = serializers.CharField(allow_null=True, required=False)  # ✅ 재생 URL 추가

    # 모든 모드는 현재 권한 토큰을 받으며, 감시 session_id는 PROCTORED_CLASS만 받는다.
    playback_token = serializers.CharField(allow_null=True, required=False)
    playback_session_id = serializers.CharField(allow_null=True, required=False)
    playback_expires_at = serializers.IntegerField(allow_null=True, required=False)
    policy_version = serializers.IntegerField(min_value=1)
    event_protocol_version = serializers.ChoiceField(choices=[1, 2], default=1)

    policy = serializers.DictField()


class StudentVideoAccessCheckSerializer(serializers.Serializer):
    ok = serializers.BooleanField()
    access_mode = serializers.ChoiceField(
        choices=["FREE_REVIEW", "PROCTORED_CLASS"],
    )
    monitoring_enabled = serializers.BooleanField()
    policy_version = serializers.IntegerField(min_value=1)


class StudentVideoForwardSkipRequestSerializer(serializers.Serializer):
    enrollment_id = serializers.IntegerField(min_value=1, required=False)
    enrollment = serializers.IntegerField(min_value=1, required=False)


class StudentVideoForwardSkipResponseSerializer(serializers.Serializer):
    enabled = serializers.BooleanField()
    step_seconds = serializers.IntegerField(min_value=0)
    ratio_percent = serializers.IntegerField(min_value=0, max_value=100)
    max_seconds = serializers.IntegerField(min_value=0)
    limit_seconds = serializers.IntegerField(min_value=0)
    used_seconds = serializers.IntegerField(min_value=0)
    remaining_seconds = serializers.IntegerField(min_value=0)
    unavailable_reason = serializers.CharField(allow_blank=True)
    granted_seconds = serializers.IntegerField(min_value=0)
