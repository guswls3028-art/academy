# apps/support/messaging/serializers.py
import json

from django.utils import timezone
from rest_framework import serializers

from apps.core.models import Tenant
from apps.domains.messaging.effective_templates import resolve_effective_template_status
from apps.domains.messaging.models import (
    MAX_MESSAGE_TEMPLATE_BODY_LENGTH,
    AutoSendConfig,
    MessageTemplate,
    ScheduledNotification,
)


class MessagingInfoSerializer(serializers.ModelSerializer):
    """GET/PATCH 응답: 테넌트 메시징 정보"""

    # 자체 연동 키 — GET 시 마스킹 처리
    own_solapi_api_key = serializers.SerializerMethodField()
    own_solapi_api_secret = serializers.SerializerMethodField()
    own_ppurio_api_key = serializers.SerializerMethodField()
    own_ppurio_account = serializers.CharField(read_only=True)
    has_own_credentials = serializers.SerializerMethodField()
    channel_source = serializers.CharField(read_only=True)
    resolved_pf_id = serializers.CharField(read_only=True)
    delivery_policy = serializers.CharField(read_only=True)
    alimtalk_available = serializers.BooleanField(read_only=True)
    tenant_messaging_enabled = serializers.BooleanField(
        source="messaging_is_active",
        read_only=True,
    )
    messaging_ops_hold = serializers.BooleanField(read_only=True)
    can_manage_messaging = serializers.BooleanField(read_only=True)
    messaging_disabled = serializers.BooleanField(read_only=True)
    messaging_disabled_reason = serializers.CharField(read_only=True)

    class Meta:
        model = Tenant
        fields = [
            "kakao_pfid", "messaging_sender", "messaging_provider",
            "own_solapi_api_key", "own_solapi_api_secret",
            "own_ppurio_api_key", "own_ppurio_account",
            "has_own_credentials",
            "channel_source", "resolved_pf_id", "delivery_policy",
            "alimtalk_available", "tenant_messaging_enabled",
            "messaging_ops_hold", "can_manage_messaging",
            "messaging_disabled", "messaging_disabled_reason",
        ]

    @staticmethod
    def _mask(value: str) -> str:
        if not value:
            return ""
        if len(value) <= 4:
            return "****"
        return "****" + value[-4:]

    def get_own_solapi_api_key(self, obj) -> str:
        return self._mask(obj.own_solapi_api_key)

    def get_own_solapi_api_secret(self, obj) -> str:
        return self._mask(obj.own_solapi_api_secret)

    def get_own_ppurio_api_key(self, obj) -> str:
        return self._mask(obj.own_ppurio_api_key)

    def get_has_own_credentials(self, obj) -> bool:
        provider = (obj.messaging_provider or "solapi").strip().lower()
        if provider == "ppurio":
            return bool(obj.own_ppurio_api_key and obj.own_ppurio_account)
        return bool(obj.own_solapi_api_key and obj.own_solapi_api_secret)


class MessagingActivationSerializer(serializers.Serializer):
    """학원 대표/관리자가 직접 제어하는 알림톡 전체 사용 설정."""

    tenant_messaging_enabled = serializers.BooleanField()


class NotificationLogSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    sent_at = serializers.DateTimeField()
    success = serializers.BooleanField()
    status = serializers.CharField()
    claimed_at = serializers.DateTimeField(allow_null=True)
    amount_deducted = serializers.DecimalField(max_digits=10, decimal_places=2)
    recipient_summary = serializers.CharField()
    template_summary = serializers.CharField()
    failure_reason = serializers.CharField()
    message_body = serializers.CharField()
    message_mode = serializers.CharField()
    source_tenant_id = serializers.IntegerField(allow_null=True)
    target_type = serializers.CharField()
    target_id = serializers.CharField()
    target_name = serializers.CharField()


class MessageTemplateSerializer(serializers.ModelSerializer):
    body = serializers.CharField(max_length=MAX_MESSAGE_TEMPLATE_BODY_LENGTH)
    category = serializers.ChoiceField(
        choices=[*MessageTemplate.Category.choices, ("student", "학생")],
        required=False,
    )
    has_content_var = serializers.SerializerMethodField()
    alimtalk_envelope_type = serializers.SerializerMethodField()
    alimtalk_readiness = serializers.SerializerMethodField()

    class Meta:
        model = MessageTemplate
        fields = [
            "id",
            "category",
            "name",
            "subject",
            "body",
            "is_system",
            "is_user_default",
            "retired_at",
            "solapi_template_id",
            "solapi_status",
            "has_content_var",
            "alimtalk_envelope_type",
            "alimtalk_readiness",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "is_system",
            "solapi_template_id",
            "solapi_status",
            "has_content_var",
            "created_at",
            "updated_at",
            "retired_at",
        ]

    @staticmethod
    def _alimtalk_envelope(obj) -> tuple[str, str]:
        return "", ""

    def get_alimtalk_envelope_type(self, obj) -> str:
        return self._alimtalk_envelope(obj)[0]

    def get_alimtalk_readiness(self, obj) -> str:
        template_type, template_id = self._alimtalk_envelope(obj)
        if template_type and template_id:
            return "ready"
        if template_type:
            return "provider_template_missing"
        if obj.is_system and obj.solapi_template_id and obj.solapi_status == "APPROVED":
            return "ready"
        if obj.is_system:
            return "system_managed"
        return "envelope_selection_required"

    @staticmethod
    def get_has_content_var(obj) -> bool:
        """본문에 #{공지내용} 또는 #{내용} 변수가 있는지 — 자유양식 발송 가능 여부"""
        body = obj.body or ""
        return "#{공지내용}" in body or "#{내용}" in body

    @staticmethod
    def validate_category(value: str) -> str:
        # Frontend blockCategory has "student"; persisted template categories do not.
        if value == "student":
            return MessageTemplate.Category.DEFAULT
        return value


class SendMessageRequestSerializer(serializers.Serializer):
    """알림톡 발송 요청: 학생/학부모 수신자 + 직접 입력 본문 또는 템플릿 ID."""
    client_request_id = serializers.UUIDField(
        required=False,
        help_text="한 발송 동작의 학생/학부모 요청을 묶는 PII-free 요청 식별자",
    )
    student_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        max_length=200,
        allow_empty=True,
        required=False,
        default=list,
        help_text="수신 대상 학생 ID 목록 (send_to가 student/parent일 때 사용)",
    )
    staff_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        max_length=200,
        allow_empty=True,
        required=False,
        default=list,
        help_text="legacy field. 직원 대상 범용 발송은 비활성화됨.",
    )
    send_to = serializers.ChoiceField(
        choices=[("student", "학생"), ("parent", "학부모"), ("staff", "직원")],
        default="parent",
        help_text="학생/학부모 번호로 보낼지",
    )
    message_mode = serializers.ChoiceField(
        choices=[("alimtalk", "알림톡만")],
        default="alimtalk",
        required=False,
        help_text="alimtalk",
    )
    template_id = serializers.IntegerField(required=False, allow_null=True)
    template_version = serializers.CharField(required=False, allow_blank=True, max_length=64)
    manual_event = serializers.CharField(required=False, allow_blank=True, max_length=64)
    preflight_identity = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=128,
        help_text="발송 전 확인에서 발급한 서명된 1회용 발송 정체성",
    )
    raw_body = serializers.CharField(required=False, allow_blank=True, max_length=5000)
    raw_subject = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        max_length=200,
    )
    scheduled_send_at = serializers.DateTimeField(
        required=False,
        allow_null=True,
        help_text="예약 발송 시각. 비어 있으면 즉시 발송합니다.",
    )
    block_category = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        max_length=40,
        help_text=(
            "본문 개인화 검증에 사용하는 발송 진입점 범주. "
            "승인 봉투 선택에는 사용하지 않으며 manual_event가 유일한 기준입니다."
        ),
    )
    alimtalk_extra_vars = serializers.DictField(
        child=serializers.CharField(allow_blank=True, max_length=1000),
        required=False,
        default=dict,
        help_text="알림톡 추가 치환 변수 (예: {시험명: '수학', 시험성적: '80/100'})",
    )
    alimtalk_extra_vars_per_student = serializers.DictField(
        required=False,
        default=dict,
        help_text="학생별 개별 치환 변수 (key: student_id, value: {변수명: 값})",
    )

    def validate(self, attrs):
        send_to = attrs.get("send_to") or "parent"
        student_ids = attrs.get("student_ids") or []
        if send_to == "staff":
            raise serializers.ValidationError(
                {"send_to": "직원 대상 범용 발송은 비활성화되었습니다. 알림톡 운영/계정 경로만 사용할 수 있습니다."}
            )
        if not student_ids:
            raise serializers.ValidationError(
                {"student_ids": "학생/학부모 수신 시 최소 1명의 학생을 선택해 주세요."}
            )
        per_student = attrs.get("alimtalk_extra_vars_per_student") or {}
        if len(attrs.get("alimtalk_extra_vars") or {}) > 50:
            raise serializers.ValidationError({
                "alimtalk_extra_vars": "치환값은 최대 50개까지 보낼 수 있습니다.",
            })
        if len(per_student) > 200:
            raise serializers.ValidationError({
                "alimtalk_extra_vars_per_student": "학생별 치환값은 최대 200명까지 보낼 수 있습니다.",
            })
        for student_key, values in per_student.items():
            if len(str(student_key)) > 20 or not isinstance(values, dict):
                raise serializers.ValidationError({
                    "alimtalk_extra_vars_per_student": "학생별 치환값 형식이 올바르지 않습니다.",
                })
            if len(values) > 50 or any(
                len(str(key)) > 50 or len(str(value)) > 1000
                for key, value in values.items()
            ):
                raise serializers.ValidationError({
                    "alimtalk_extra_vars_per_student": "학생별 치환값의 항목 또는 길이가 허용 범위를 초과했습니다.",
                })
        if len(json.dumps(attrs, ensure_ascii=False, default=str).encode("utf-8")) > 200_000:
            raise serializers.ValidationError({
                "detail": "발송 요청 크기가 너무 큽니다. 대상을 나누어 다시 시도해 주세요.",
            })
        if not attrs.get("template_id") and not (attrs.get("raw_body") or "").strip():
            raise serializers.ValidationError(
                {"raw_body": "직접 입력 본문을 넣거나 템플릿을 선택해 주세요."}
            )
        if not attrs.get("template_id") and (attrs.get("raw_body") or "").strip():
            if not (attrs.get("block_category") or "").strip():
                raise serializers.ValidationError(
                    {
                        "block_category": (
                            "템플릿 없이 직접 발송할 때는 발송 진입점 카테고리가 필요합니다. "
                            "미리보기/확인 발송 경로를 사용해 주세요."
                        )
                    }
                )
        scheduled_send_at = attrs.get("scheduled_send_at")
        if scheduled_send_at is not None and scheduled_send_at <= timezone.now():
            raise serializers.ValidationError(
                {"scheduled_send_at": "예약 발송 시각은 현재 이후여야 합니다."}
            )
        return attrs


class ScheduledNotificationSerializer(serializers.ModelSerializer):
    recipient_summary = serializers.SerializerMethodField()
    message_preview = serializers.SerializerMethodField()
    target_type = serializers.SerializerMethodField()
    target_id = serializers.SerializerMethodField()
    target_name = serializers.SerializerMethodField()
    message_mode = serializers.SerializerMethodField()

    class Meta:
        model = ScheduledNotification
        fields = [
            "id",
            "dispatch_key",
            "trigger",
            "send_at",
            "status",
            "recipient_summary",
            "message_preview",
            "target_type",
            "target_id",
            "target_name",
            "message_mode",
            "created_at",
            "sent_at",
            "attempt_count",
            "next_attempt_at",
            "last_attempt_at",
            "error_message",
        ]

    @staticmethod
    def _payload(obj) -> dict:
        return obj.payload if isinstance(obj.payload, dict) else {}

    def get_recipient_summary(self, obj) -> str:
        payload = self._payload(obj)
        target_name = (payload.get("target_name") or "").strip()
        to = (payload.get("to") or "").strip()
        if to and len(to) >= 7:
            to = f"{to[:3]}****{to[-4:]}"
        return " / ".join(part for part in [target_name, to] if part)

    def get_message_preview(self, obj) -> str:
        from apps.domains.messaging.security import (
            SENSITIVE_MESSAGE_PLACEHOLDER,
            is_sensitive_notification,
        )

        payload = self._payload(obj)
        if is_sensitive_notification(trigger=obj.trigger, payload=payload):
            return SENSITIVE_MESSAGE_PLACEHOLDER
        text = (payload.get("text") or "").strip()
        return text[:160]

    def get_target_type(self, obj) -> str:
        return (self._payload(obj).get("target_type") or "").strip()

    def get_target_id(self, obj) -> str:
        from apps.domains.messaging.security import sanitize_notification_target_id

        return sanitize_notification_target_id(self._payload(obj).get("target_id"))

    def get_target_name(self, obj) -> str:
        return (self._payload(obj).get("target_name") or "").strip()

    def get_message_mode(self, obj) -> str:
        return (self._payload(obj).get("message_mode") or "").strip()


class AutoSendConfigSerializer(serializers.ModelSerializer):
    template_name = serializers.CharField(source="template.name", read_only=True, default="")
    template_subject = serializers.CharField(source="template.subject", read_only=True, default="")
    template_body = serializers.CharField(source="template.body", read_only=True, default="")
    template_solapi_status = serializers.CharField(
        source="template.solapi_status", read_only=True, default=""
    )
    template_is_system = serializers.BooleanField(
        source="template.is_system", read_only=True, default=False
    )
    effective_solapi_template_id = serializers.SerializerMethodField()
    effective_template_solapi_status = serializers.SerializerMethodField()
    effective_template_source = serializers.SerializerMethodField()
    effective_template_is_approved = serializers.SerializerMethodField()
    effective_template_type = serializers.SerializerMethodField()
    # delay_mode/delay_value — 마이그레이션 전에도 안전하게 동작 (컬럼 미존재 시 기본값)
    delay_mode = serializers.SerializerMethodField()
    delay_value = serializers.SerializerMethodField()

    @staticmethod
    def _effective_status(obj):
        cached = getattr(obj, "_effective_template_status_cache", None)
        if cached is None:
            cached = resolve_effective_template_status(obj)
            obj._effective_template_status_cache = cached
        return cached

    def get_effective_solapi_template_id(self, obj) -> str:
        return self._effective_status(obj).solapi_template_id

    def get_effective_template_solapi_status(self, obj) -> str:
        return self._effective_status(obj).solapi_status

    def get_effective_template_source(self, obj) -> str:
        return self._effective_status(obj).source

    def get_effective_template_is_approved(self, obj) -> bool:
        return self._effective_status(obj).is_approved

    def get_effective_template_type(self, obj) -> str:
        return self._effective_status(obj).template_type

    def get_delay_mode(self, obj) -> str:
        try:
            return (obj.delay_mode or "immediate") if hasattr(obj, "delay_mode") else "immediate"
        except Exception:
            return "immediate"

    def get_delay_value(self, obj):
        try:
            return obj.delay_value if hasattr(obj, "delay_value") else None
        except Exception:
            return None

    class Meta:
        model = AutoSendConfig
        fields = [
            "id",
            "trigger",
            "template",
            "template_name",
            "template_subject",
            "template_body",
            "template_solapi_status",
            "template_is_system",
            "effective_solapi_template_id",
            "effective_template_solapi_status",
            "effective_template_source",
            "effective_template_is_approved",
            "effective_template_type",
            "enabled",
            "message_mode",
            "minutes_before",
            "delay_mode",
            "delay_value",
            "show_actual_time",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class AutoSendConfigUpdateSerializer(serializers.Serializer):
    """PATCH: 개별 config 수정"""
    template_id = serializers.IntegerField(required=False, allow_null=True)
    enabled = serializers.BooleanField(required=False)
    message_mode = serializers.ChoiceField(
        choices=[("alimtalk", "알림톡만")],
        required=False,
    )
    minutes_before = serializers.IntegerField(required=False, allow_null=True, min_value=0)
    delay_mode = serializers.ChoiceField(
        choices=[("immediate", "즉시 발송"), ("delay_minutes", "N분 후 발송"), ("scheduled_hour", "지정 시각 발송")],
        required=False,
    )
    delay_value = serializers.IntegerField(required=False, allow_null=True, min_value=0)
    show_actual_time = serializers.BooleanField(required=False)

    def validate(self, attrs):
        delay_mode = attrs.get("delay_mode")
        delay_value = attrs.get("delay_value")
        if delay_mode == "scheduled_hour" and delay_value is not None and delay_value > 23:
            raise serializers.ValidationError(
                {"delay_value": "지정 시각은 0~23 사이여야 합니다."}
            )
        return attrs
