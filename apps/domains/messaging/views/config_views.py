# apps/support/messaging/views/config_views.py
"""
자동발송 설정 뷰 — AutoSendConfig, 기본 템플릿 프로비저닝
"""

from django.db import transaction
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from apps.core.parsing import parse_bool
from apps.core.permissions import TenantResolvedAndStaff
from apps.domains.messaging.effective_templates import (
    prime_effective_owner_templates,
    resolve_effective_template_status,
)
from apps.domains.messaging.models import MessageTemplate, AutoSendConfig
from apps.domains.messaging.permissions import can_manage_messaging_settings
from apps.domains.messaging.policy import is_auto_send_enabled_by_default
from apps.domains.messaging.serializers import AutoSendConfigSerializer


def _default_enabled_for_trigger(trigger: str) -> bool:
    return is_auto_send_enabled_by_default(trigger)


def _can_manage_auto_send(request, tenant) -> bool:
    return can_manage_messaging_settings(request, tenant)


def _auto_send_write_forbidden_response():
    return Response(
        {"detail": "자동발송 설정은 대표 또는 관리자만 변경할 수 있습니다."},
        status=status.HTTP_403_FORBIDDEN,
    )


class AutoSendConfigView(APIView):
    """
    GET: 테넌트의 모든 자동발송 설정 목록 (트리거별)
    PATCH: 트리거별 설정 수정. Body: { "configs": [ { "trigger": "...", "template_id": null|int, "enabled": bool, "message_mode": "alimtalk" }, ... ] }
    """
    permission_classes = [IsAuthenticated, TenantResolvedAndStaff]

    def get(self, request):
        tenant = request.tenant
        triggers = [c[0] for c in AutoSendConfig.Trigger.choices]
        configs = AutoSendConfig.objects.filter(tenant=tenant).select_related("template").defer("delay_mode", "delay_value")

        from apps.domains.messaging.alimtalk_content_builders import get_template_type
        from apps.domains.messaging.policy import get_trigger_policy, get_trigger_implementation_status

        configs = prime_effective_owner_templates(configs)
        by_trigger = {c.trigger: c for c in configs}

        result = []
        for trigger in triggers:
            c = by_trigger.get(trigger)
            policy_mode = get_trigger_policy(trigger)
            impl_status = get_trigger_implementation_status(trigger)
            if c:
                data = AutoSendConfigSerializer(c).data
                data["policy_mode"] = policy_mode
                data["implementation_status"] = impl_status
                result.append(data)
            else:
                result.append({
                    "id": None,
                    "trigger": trigger,
                    "template": None,
                    "template_name": "",
                    "template_subject": "",
                    "template_body": "",
                    "template_solapi_status": "",
                    "effective_solapi_template_id": "",
                    "effective_template_solapi_status": "",
                    "effective_template_source": "missing",
                    "effective_template_is_approved": False,
                    "effective_template_type": get_template_type(trigger) or "",
                    "enabled": False,
                    "message_mode": "alimtalk",
                    "minutes_before": None,
                    "created_at": None,
                    "updated_at": None,
                    "policy_mode": policy_mode,
                    "implementation_status": impl_status,
                })
        return Response(result)

    @transaction.atomic
    def patch(self, request):
        tenant = request.tenant
        if not _can_manage_auto_send(request, tenant):
            return _auto_send_write_forbidden_response()

        configs_data = request.data.get("configs") or []
        if not isinstance(configs_data, list):
            return Response(
                {"detail": "configs는 배열이어야 합니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        from apps.domains.messaging.policy import get_trigger_implementation_status

        def reject(payload):
            transaction.set_rollback(True)
            return Response(payload, status=status.HTTP_400_BAD_REQUEST)

        rejected_triggers = []
        for item in configs_data:
            trigger = (item.get("trigger") or "").strip()
            if not trigger or trigger not in dict(AutoSendConfig.Trigger.choices):
                continue
            should_update_enabled = "enabled" in item
            requested_enabled = (
                parse_bool(item.get("enabled"), field_name="enabled")
                if should_update_enabled
                else None
            )
            # 미구현/DISABLED 트리거는 자동 발송 ON 차단 — 운영자 혼란 방지
            enabled = requested_enabled
            if enabled:
                impl_status = get_trigger_implementation_status(trigger)
                if impl_status != "implemented":
                    enabled = False
                    rejected_triggers.append({"trigger": trigger, "reason": impl_status})
            minutes_before = item.get("minutes_before")
            if minutes_before is not None:
                try:
                    minutes_before = int(minutes_before) if minutes_before != "" else None
                except (TypeError, ValueError):
                    return reject({"minutes_before": "발송 시점 값은 숫자여야 합니다."})
                if minutes_before is not None and minutes_before < 0:
                    return reject({"minutes_before": "발송 시점 값은 0 이상이어야 합니다."})

            config, _ = AutoSendConfig.objects.select_for_update().get_or_create(
                tenant=tenant,
                trigger=trigger,
                defaults={"enabled": False, "message_mode": "alimtalk"},
            )
            if "template_id" in item:
                template_id = item.get("template_id")
                if template_id:
                    try:
                        template_pk = int(template_id)
                    except (TypeError, ValueError):
                        return reject({"template_id": "템플릿 ID는 숫자여야 합니다."})
                    template = MessageTemplate.objects.filter(
                        tenant=tenant,
                        pk=template_pk,
                        retired_at__isnull=True,
                    ).first()
                    if template is None:
                        return reject({"template_id": "해당 템플릿을 찾을 수 없습니다."})
                    config.template = template
                else:
                    config.template = None
            if should_update_enabled:
                config.enabled = enabled
            if "message_mode" in item:
                message_mode = (item.get("message_mode") or "alimtalk").strip().lower()
                if message_mode != "alimtalk":
                    return reject({"message_mode": "공용 알림톡만 설정할 수 있습니다."})
                config.message_mode = message_mode
            if "minutes_before" in item:
                config.minutes_before = minutes_before

            # show_actual_time — 클리닉 출석/결석 알림 시간 표시 모드
            if hasattr(config, "show_actual_time"):
                sat = item.get("show_actual_time")
                if sat is not None:
                    config.show_actual_time = parse_bool(sat, field_name="show_actual_time")

            # delay_mode / delay_value — 마이그레이션 전에도 안전 (hasattr 체크)
            if hasattr(config, "delay_mode"):
                current_delay_mode = (getattr(config, "delay_mode", "") or "immediate").strip().lower()
                requested_delay_mode = item.get("delay_mode")
                delay_mode = (
                    (requested_delay_mode or "").strip().lower()
                    if requested_delay_mode is not None
                    else current_delay_mode
                )
                if delay_mode not in ("immediate", "delay_minutes", "scheduled_hour"):
                    return reject({"delay_mode": "지원하지 않는 발송 지연 방식입니다."})
                delay_value = item.get("delay_value")
                parsed_delay_value = None
                if delay_value is not None:
                    try:
                        parsed_delay_value = int(delay_value) if delay_value != "" else None
                    except (TypeError, ValueError):
                        return reject({"delay_value": "발송 지연 값은 숫자여야 합니다."})
                    if parsed_delay_value is not None and parsed_delay_value < 0:
                        return reject({"delay_value": "발송 지연 값은 0 이상이어야 합니다."})
                    if delay_mode == "scheduled_hour" and parsed_delay_value is not None and not 0 <= parsed_delay_value <= 23:
                        return reject({"delay_value": "지정 시각은 0~23 사이여야 합니다."})
                elif requested_delay_mode is not None and delay_mode != "immediate":
                    if delay_mode != current_delay_mode:
                        return reject({"delay_value": "발송 지연 방식을 바꿀 때는 값을 함께 지정해야 합니다."})
                    current_delay_value = getattr(config, "delay_value", None)
                    if current_delay_value is None:
                        return reject({"delay_value": "발송 지연 값이 필요합니다."})
                    if delay_mode == "scheduled_hour" and not 0 <= int(current_delay_value) <= 23:
                        return reject({"delay_value": "지정 시각은 0~23 사이여야 합니다."})

                config.delay_mode = delay_mode
                if delay_value is not None:
                    if parsed_delay_value is None or delay_mode == "immediate":
                        config.delay_value = None
                    elif delay_mode == "scheduled_hour":
                        config.delay_value = parsed_delay_value
                    else:
                        config.delay_value = max(0, parsed_delay_value)
                elif requested_delay_mode is not None and delay_mode == "immediate":
                    config.delay_value = None

            if config.enabled:
                effective_template = resolve_effective_template_status(config)
                if not effective_template.is_approved:
                    return reject({
                        "template_id": "자동발송을 켜려면 승인된 알림톡 템플릿이 필요합니다.",
                        "trigger": trigger,
                        "effective_template_source": effective_template.source,
                        "effective_solapi_template_id": effective_template.solapi_template_id,
                        "effective_solapi_status": effective_template.solapi_status,
                    })

            config.save()

        if rejected_triggers:
            import logging
            _log = logging.getLogger(__name__)
            _log.info(
                "AutoSendConfig PATCH rejected enable for unimplemented triggers tenant=%s rejected=%s",
                tenant.id, rejected_triggers,
            )
        configs = prime_effective_owner_templates(
            AutoSendConfig.objects.filter(tenant=tenant)
            .select_related("template")
            .defer("delay_mode", "delay_value")
        )
        from apps.domains.messaging.policy import get_trigger_policy, get_trigger_implementation_status
        result = []
        for c in configs:
            data = AutoSendConfigSerializer(c).data
            data["policy_mode"] = get_trigger_policy(c.trigger)
            data["implementation_status"] = get_trigger_implementation_status(c.trigger)
            result.append(data)
        return Response(result)


class ProvisionDefaultTemplatesView(APIView):
    """POST: 기본 템플릿 + 자동발송 config 일괄 생성/리셋.
    - 기존 기본 템플릿(이름이 DEFAULT_TEMPLATES와 동일)은 누락된 config만 연결
    - 학원장이 편집한 제목/본문은 덮어쓰지 않음
    - 사용자가 새로 만든 템플릿은 그대로 유지
    """
    permission_classes = [IsAuthenticated, TenantResolvedAndStaff]

    def post(self, request):
        from ..default_templates import get_default_templates

        tenant = request.tenant
        if not _can_manage_auto_send(request, tenant):
            return _auto_send_write_forbidden_response()

        templates = get_default_templates(tenant.name or "학원")
        existing_configs = {
            c.trigger: c
            for c in AutoSendConfig.objects.filter(tenant=tenant).select_related("template").defer("delay_mode", "delay_value")
        }
        created_templates = 0
        created_configs = 0
        reset_templates = 0
        linked = 0

        # 자유양식 템플릿 이름 변경 마이그레이션 (구 이름 → 신 이름)
        academy_name = tenant.name or "학원"
        _freeform_name_migrations = {
            f"[{academy_name}] 학원 안내": f"[{academy_name}] 공지사항 안내",
            f"[{academy_name}] 결제 안내": f"[{academy_name}] 수납 안내",
            f"[{academy_name}] 클리닉 안내": f"[{academy_name}] 보충수업 안내",
        }
        _old_to_new = {v: k for k, v in _freeform_name_migrations.items()}  # new → old

        for trigger, defaults in templates.items():
            tpl_name = defaults["name"]
            tpl_category = defaults["category"]
            tpl_subject = defaults.get("subject", "")
            tpl_body = defaults["body"]

            existing_tpl = MessageTemplate.objects.filter(
                tenant=tenant, name=tpl_name,
            ).first()

            # 이름 변경된 자유양식: 구 이름으로도 검색하여 rename
            if not existing_tpl and tpl_name in _old_to_new:
                old_name = _old_to_new[tpl_name]
                existing_tpl = MessageTemplate.objects.filter(
                    tenant=tenant, name=old_name,
                ).first()
                if existing_tpl:
                    existing_tpl.name = tpl_name
                    existing_tpl.save(update_fields=["name", "updated_at"])

            if existing_tpl:
                # 기본 템플릿 연결은 복구하되 학원장 작성 본문/제목은 덮어쓰지 않는다.
                changed = False
                if existing_tpl.category != tpl_category:
                    existing_tpl.category = tpl_category
                    changed = True
                if changed:
                    existing_tpl.save(update_fields=["category", "updated_at"])
                    reset_templates += 1
                tpl = existing_tpl
            else:
                tpl = MessageTemplate.objects.create(
                    tenant=tenant,
                    name=tpl_name,
                    category=tpl_category,
                    subject=tpl_subject,
                    body=tpl_body,
                    is_system=True,
                )
                created_templates += 1

            # 자유양식 템플릿 등 유효한 트리거가 아니면 AutoSendConfig 스킵
            valid_triggers = {c[0] for c in AutoSendConfig.Trigger.choices}
            if trigger not in valid_triggers:
                continue

            existing = existing_configs.get(trigger)
            if existing:
                if not existing.template_id:
                    existing.template = tpl
                    existing.save(update_fields=["template", "updated_at"])
                    linked += 1
            else:
                AutoSendConfig.objects.create(
                    tenant=tenant,
                    trigger=trigger,
                    template=tpl,
                    enabled=_default_enabled_for_trigger(trigger),
                    message_mode="alimtalk",
                    minutes_before=defaults.get("minutes_before"),
                )
                created_configs += 1

        total_configs = AutoSendConfig.objects.filter(tenant=tenant).count()

        submitted_reviews = 0
        review_errors = []

        return Response({
            "created_templates": created_templates,
            "created_configs": created_configs,
            "reset_templates": reset_templates,
            "linked": linked,
            "total_templates": MessageTemplate.objects.filter(tenant=tenant).count(),
            "total_configs": total_configs,
            "submitted_reviews": submitted_reviews,
            "review_errors": review_errors,
            "review_note": "",
        }, status=status.HTTP_200_OK)
