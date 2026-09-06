from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.db.models import Q
from django.utils import timezone

from apps.core.models import WorkerHeartbeatModel
from apps.domains.messaging.alimtalk_content_builders import (
    build_manual_replacements,
    get_unified_for_manual_send,
    render_alimtalk_preview_text,
)
from apps.domains.messaging.effective_templates import resolve_effective_template_status
from apps.domains.messaging.models import AutoSendConfig, MessageTemplate, ScheduledNotification
from apps.domains.messaging.policy import (
    get_messaging_disabled_reason,
    get_owner_tenant_id,
    get_trigger_implementation_status,
    is_messaging_disabled,
)
from apps.domains.messaging.selectors import (
    HOURLY_SEND_LIMIT,
    get_hourly_notification_usage,
    get_provider_daily_dispatch_limit,
    get_provider_daily_notification_usage,
    notification_logs_for_business_tenant,
)
from apps.domains.messaging.services.recipients import resolve_student_message_recipients
from apps.domains.messaging.services.grade_personalization import (
    normalize_per_student_context,
    validate_grade_personalization,
)


MAX_MANUAL_RECIPIENTS = 200
WORKER_STALE_AFTER_MINUTES = 5


@dataclass(frozen=True)
class PreflightIssue:
    code: str
    title: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "title": self.title, "detail": self.detail}


@dataclass(frozen=True)
class TemplatePlan:
    ok: bool
    source: str
    name: str = ""
    solapi_template_id: str = ""
    solapi_status: str = ""
    detail: str = ""
    uses_unified_template: bool = False
    template_type: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "source": self.source,
            "name": self.name,
            "solapi_template_id": self.solapi_template_id,
            "solapi_status": self.solapi_status,
            "detail": self.detail,
            "uses_unified_template": self.uses_unified_template,
            "template_type": self.template_type,
        }


def _resolve_template_for_manual_send(tenant, data: dict[str, Any]) -> TemplatePlan:
    message_mode = "alimtalk"
    template_id = data.get("template_id")
    raw_body = (data.get("raw_body") or "").strip()
    raw_subject = (data.get("raw_subject") or "").strip()
    block_category = (data.get("block_category") or "").strip()
    extra_vars = data.get("alimtalk_extra_vars") or {}

    if message_mode != "alimtalk":
        return TemplatePlan(ok=False, source="unsupported", detail="현재 수동 발송은 알림톡만 지원합니다.")

    template = None
    if template_id:
        template = MessageTemplate.objects.filter(tenant=tenant, pk=template_id).first()
        if not template:
            return TemplatePlan(ok=False, source="missing", detail="선택한 템플릿을 찾을 수 없습니다.")

    body_base = raw_body or ((template.body or "").strip() if template else "")
    if not body_base:
        return TemplatePlan(ok=False, source="empty_body", detail="발송할 본문이 비어 있습니다.")

    category = (template.category if template else "") or ""
    template_name = (template.name if template else "") or ""
    unified_type, unified_sid = get_unified_for_manual_send(
        block_category,
        category,
        template_name,
        extra_vars,
    )
    if unified_type and not unified_sid:
        return TemplatePlan(
            ok=False,
            source="unified_missing",
            name=template_name or "시스템 통합 알림톡",
            detail=(
                "이 발송 유형의 카카오 승인 봉투가 공급사에 등록되어 있지 않아 "
                "현재 발송할 수 없습니다. 승인 SID 등록 후 다시 시도해 주세요."
            ),
            uses_unified_template=True,
        )
    if unified_type and unified_sid:
        return TemplatePlan(
            ok=True,
            source="unified",
            name=template_name or "시스템 통합 알림톡",
            solapi_template_id=unified_sid,
            solapi_status="APPROVED",
            detail="카카오 검수 완료된 시스템 봉투로 발송됩니다.",
            uses_unified_template=True,
            template_type=unified_type,
        )

    if raw_subject:
        return TemplatePlan(
            ok=False,
            source="missing",
            detail="알림톡 발송에는 카카오 승인 봉투가 필요합니다. 출석/성적/클리닉/일정변경 중 하나를 선택해 주세요.",
        )
    return TemplatePlan(
        ok=False,
        source="missing",
        detail="알림톡 발송에는 카카오 승인 봉투가 필요합니다. 출석/성적/클리닉/일정변경 중 하나를 선택해 주세요.",
    )


def _phone_summary(phones: list[str]) -> dict[str, int]:
    valid = [phone for phone in phones if phone and len(phone) >= 10]
    unique = set(valid)
    return {
        "valid_phone": len(valid),
        "skipped_no_phone": len(phones) - len(valid),
        "duplicate_phone": max(0, len(valid) - len(unique)),
        "unique_phone": len(unique),
    }


def _build_manual_preview_recipients(
    tenant,
    data: dict[str, Any],
    recipients,
    template_plan: TemplatePlan,
) -> list[dict[str, Any]]:
    """Build safe preview rows from the exact values used for Solapi."""
    if not template_plan.ok or not template_plan.template_type:
        return []

    from apps.domains.messaging.services import get_tenant_site_url

    template_id = data.get("template_id")
    template = (
        MessageTemplate.objects.filter(tenant=tenant, pk=template_id).first()
        if template_id
        else None
    )
    body_base = (data.get("raw_body") or "").strip()
    if not body_base and template:
        body_base = (template.body or "").strip()

    shared_context = data.get("alimtalk_extra_vars") or {}
    per_student_context, _invalid = normalize_per_student_context(
        data.get("alimtalk_extra_vars_per_student") or {}
    )
    is_grade_send = (data.get("block_category") or "").strip() == "grades"

    tenant_name = (tenant.name or "").strip()
    site_url = get_tenant_site_url(tenant) or ""
    preview_recipients = []
    for recipient in recipients:
        phone = recipient.phone or ""
        excluded = not phone or len(phone) < 10
        student_context = dict(per_student_context.get(recipient.student_id, {}))
        student_body_override = student_context.pop("_body_subst", None)
        if is_grade_send:
            student_body = (
                student_body_override.strip()
                if isinstance(student_body_override, str)
                else ""
            )
        else:
            student_body = student_body_override or body_base
        merged_context = {**shared_context, **student_context}
        replacements = build_manual_replacements(
            template_type=template_plan.template_type,
            content_body=student_body,
            context=merged_context,
            tenant_name=tenant_name,
            student_name=recipient.student_name,
            site_url=site_url,
        )
        preview_recipients.append(
            {
                "student_id": recipient.student_id,
                "student_name": recipient.student_name,
                "phone": phone[:3] + "****" + phone[-4:] if len(phone) >= 7 else phone,
                "excluded": excluded,
                "exclude_reason": (
                    "전화번호 없음"
                    if not phone
                    else "전화번호 형식 오류"
                    if excluded
                    else ""
                ),
                "full_message_body": render_alimtalk_preview_text(
                    template_plan.template_type,
                    replacements,
                ),
            }
        )
    return preview_recipients


def build_send_preflight(tenant, data: dict[str, Any]) -> dict[str, Any]:
    send_to = data.get("send_to") or "parent"
    scheduled_send_at = data.get("scheduled_send_at")
    blockers: list[PreflightIssue] = []
    warnings: list[PreflightIssue] = []

    if is_messaging_disabled(tenant.id):
        blockers.append(
            PreflightIssue(
                "messaging_disabled",
                "알림톡 발송 중지",
                get_messaging_disabled_reason(tenant.id),
            )
        )

    selected_count = 0
    resolved_count = 0
    phones: list[str] = []

    student_ids = list(dict.fromkeys(int(student_id) for student_id in (data.get("student_ids") or [])))
    selected_count = len(student_ids)
    recipients = resolve_student_message_recipients(tenant, student_ids, send_to=send_to)
    resolved_count = len(recipients)
    phones = [recipient.phone for recipient in recipients]

    _per_student_context, personalization_issue = validate_grade_personalization(
        block_category=(data.get("block_category") or "").strip(),
        raw_per_student=data.get("alimtalk_extra_vars_per_student") or {},
        recipients=recipients,
    )
    if personalization_issue:
        blockers.append(
            PreflightIssue(
                personalization_issue.code,
                personalization_issue.title,
                personalization_issue.detail,
            )
        )

    invalid_or_deleted = max(0, selected_count - resolved_count)
    phone_summary = _phone_summary(phones)
    valid_phone = phone_summary["valid_phone"]

    if selected_count == 0:
        blockers.append(PreflightIssue("no_recipient", "수신자 없음", "발송 대상을 먼저 선택해 주세요."))
    if resolved_count == 0 and selected_count:
        blockers.append(PreflightIssue("recipient_not_found", "대상 확인 필요", "선택한 대상이 없거나 삭제되었습니다."))
    if resolved_count > MAX_MANUAL_RECIPIENTS:
        blockers.append(
            PreflightIssue(
                "recipient_limit",
                "대상 초과",
                f"한 번에 최대 {MAX_MANUAL_RECIPIENTS}명까지 발송할 수 있습니다.",
            )
        )
    if valid_phone == 0 and resolved_count:
        blockers.append(PreflightIssue("no_valid_phone", "전화번호 없음", "발송 가능한 전화번호가 없습니다."))
    elif phone_summary["skipped_no_phone"] > 0:
        warnings.append(
            PreflightIssue(
                "missing_phone",
                "전화번호 누락",
                f"{phone_summary['skipped_no_phone']}건은 전화번호가 없어 제외됩니다.",
            )
        )
    if phone_summary["duplicate_phone"] > 0:
        warnings.append(
            PreflightIssue(
                "duplicate_phone",
                "중복 번호",
                f"동일 번호가 {phone_summary['duplicate_phone']}건 있습니다. 실제 발송 대상인지 확인해 주세요.",
            )
        )
    if invalid_or_deleted > 0:
        warnings.append(
            PreflightIssue(
                "invalid_or_deleted",
                "선택 대상 일부 제외",
                f"{invalid_or_deleted}건은 삭제되었거나 현재 테넌트 대상이 아닙니다.",
            )
        )

    template_plan = _resolve_template_for_manual_send(tenant, data)
    if not template_plan.ok:
        blockers.append(PreflightIssue("template_not_ready", "알림톡 봉투 확인 필요", template_plan.detail))

    recent_count = get_hourly_notification_usage(tenant)
    remaining_hourly = max(0, HOURLY_SEND_LIMIT - recent_count)
    provider_daily_limit = get_provider_daily_dispatch_limit()
    provider_daily_used = get_provider_daily_notification_usage()
    remaining_provider_daily = max(0, provider_daily_limit - provider_daily_used)
    expected_dispatches = valid_phone
    if not scheduled_send_at:
        if expected_dispatches > remaining_hourly:
            blockers.append(
                PreflightIssue(
                    "hourly_limit",
                    "시간당 발송 한도 초과",
                    f"최근 1시간 발송 {recent_count}건 기준으로 {remaining_hourly}건만 추가 발송할 수 있습니다.",
                )
            )
        elif remaining_hourly <= 50:
            warnings.append(
                PreflightIssue(
                    "hourly_limit_near",
                    "발송 한도 임박",
                    f"최근 1시간 내 {recent_count}건을 발송했습니다.",
                )
            )
        if expected_dispatches > remaining_provider_daily:
            blockers.append(
                PreflightIssue(
                    "provider_daily_limit",
                    "일일 발송 안전 한도 초과",
                    (
                        f"오늘 공급자 계정 발송 예약 {provider_daily_used}건 기준으로 "
                        f"{remaining_provider_daily}건만 추가 발송할 수 있습니다."
                    ),
                )
            )
        elif remaining_provider_daily <= 50:
            warnings.append(
                PreflightIssue(
                    "provider_daily_limit_near",
                    "일일 발송 안전 한도 임박",
                    f"오늘 공급자 계정 발송 예약이 {provider_daily_used}건입니다.",
                )
            )

    if scheduled_send_at:
        if scheduled_send_at <= timezone.now():
            blockers.append(PreflightIssue("schedule_in_past", "예약 시각 확인", "예약 발송 시각은 현재 이후여야 합니다."))
    else:
        heartbeat = _latest_worker_heartbeat()
        if heartbeat["status"] in {"unknown", "stale"}:
            warnings.append(
                PreflightIssue(
                    "worker_attention",
                    "워커 상태 확인",
                    "즉시 발송은 큐 등록 후 워커가 처리합니다. 발송 내역에서 최종 결과를 확인해 주세요.",
                )
            )

    return {
        "ok": not blockers,
        "can_send": not blockers,
        "mode": "scheduled" if scheduled_send_at else "now",
        "send_to": send_to,
        "recipient": {
            "selected": selected_count,
            "resolved": resolved_count,
            "valid_phone": valid_phone,
            "skipped_no_phone": phone_summary["skipped_no_phone"],
            "duplicate_phone": phone_summary["duplicate_phone"],
            "unique_phone": phone_summary["unique_phone"],
            "invalid_or_deleted": invalid_or_deleted,
            "limit": MAX_MANUAL_RECIPIENTS,
        },
        "template": template_plan.as_dict(),
        "preview_recipients": _build_manual_preview_recipients(
            tenant,
            data,
            recipients,
            template_plan,
        ),
        "limits": {
            "hourly_limit": HOURLY_SEND_LIMIT,
            "sent_last_hour": recent_count,
            "remaining_this_hour": remaining_hourly,
            "provider_daily_limit": provider_daily_limit,
            "provider_daily_used": provider_daily_used,
            "provider_daily_remaining": remaining_provider_daily,
        },
        "blockers": [issue.as_dict() for issue in blockers],
        "warnings": [issue.as_dict() for issue in warnings],
    }


def _latest_worker_heartbeat() -> dict[str, Any]:
    now = timezone.now()
    heartbeat = WorkerHeartbeatModel.objects.filter(name="messaging").order_by("-last_seen_at").first()
    if not heartbeat:
        return {
            "status": "unknown",
            "last_seen_at": None,
            "age_seconds": None,
            "instance": "",
            "version": "",
        }
    age_seconds = max(0, int((now - heartbeat.last_seen_at).total_seconds()))
    status = "stale" if age_seconds > WORKER_STALE_AFTER_MINUTES * 60 else "ok"
    return {
        "status": status,
        "last_seen_at": heartbeat.last_seen_at,
        "age_seconds": age_seconds,
        "instance": heartbeat.instance,
        "version": heartbeat.version,
    }


def build_messaging_operations_status(tenant) -> dict[str, Any]:
    now = timezone.now()
    since_24h = now - timedelta(hours=24)
    since_1h = now - timedelta(hours=1)
    scheduled_qs = ScheduledNotification.objects.filter(tenant=tenant)
    business_log_qs = notification_logs_for_business_tenant(tenant)
    log_qs = business_log_qs.filter(sent_at__gte=since_24h)
    unresolved_qs = business_log_qs.filter(status__in=["sending", "ambiguous"])
    hourly_used = get_hourly_notification_usage(tenant, now=now)
    provider_daily_limit = get_provider_daily_dispatch_limit()
    provider_daily_used = get_provider_daily_notification_usage(now=now)

    pending_qs = scheduled_qs.filter(status=ScheduledNotification.Status.PENDING)
    ready_pending_qs = pending_qs.filter(
        Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now)
    )
    retry_waiting = pending_qs.filter(next_attempt_at__gt=now).count()
    dispatching_qs = scheduled_qs.filter(
        status=ScheduledNotification.Status.DISPATCHING
    )
    stale_dispatching = dispatching_qs.filter(
        Q(last_attempt_at__isnull=True)
        | Q(last_attempt_at__lte=now - timedelta(minutes=5))
    ).count()
    failed_scheduled_24h = scheduled_qs.filter(
        status=ScheduledNotification.Status.FAILED,
    ).filter(
        Q(last_attempt_at__gte=since_24h)
        | Q(last_attempt_at__isnull=True, created_at__gte=since_24h)
    ).count()
    pending = pending_qs.count()
    overdue = ready_pending_qs.filter(
        send_at__lte=now - timedelta(minutes=2)
    ).count()
    due_now = ready_pending_qs.filter(send_at__lte=now).count()

    processing = log_qs.filter(status="processing").count()
    recent_sending = log_qs.filter(status="sending").count()
    retryable_failed = log_qs.filter(status="retryable_failed").count()
    recent_ambiguous = log_qs.filter(status="ambiguous").count()
    sending = unresolved_qs.filter(status="sending").count()
    ambiguous = unresolved_qs.filter(status="ambiguous").count()
    action_required = sending + ambiguous
    unresolved_under_1h = unresolved_qs.filter(sent_at__gte=since_1h).count()
    unresolved_1h_to_24h = unresolved_qs.filter(
        sent_at__gte=since_24h,
        sent_at__lt=since_1h,
    ).count()
    unresolved_over_24h = unresolved_qs.filter(
        Q(sent_at__lt=since_24h) | Q(sent_at__isnull=True)
    ).count()
    failed = log_qs.filter(success=False).exclude(
        status__in=["processing", "sending", "retryable_failed", "ambiguous"]
    ).count()
    sent = log_qs.filter(success=True).count()

    auto_configs = AutoSendConfig.objects.filter(tenant=tenant).select_related("template")
    enabled_configs = list(auto_configs.filter(enabled=True))
    enabled_without_template = 0
    enabled_unapproved_template = 0
    enabled_manual_only = 0
    enabled_issues: list[dict[str, str]] = []
    for config in enabled_configs:
        if get_trigger_implementation_status(config.trigger) != "implemented":
            enabled_manual_only += 1
            enabled_issues.append(
                {
                    "trigger": config.trigger,
                    "code": "trigger_not_implemented",
                    "detail": "자동발송 실행 경로가 구현 상태가 아닙니다.",
                }
            )
        effective_template = resolve_effective_template_status(config)
        if not effective_template.solapi_template_id:
            enabled_without_template += 1
            enabled_issues.append(
                {
                    "trigger": config.trigger,
                    "code": "provider_template_missing",
                    "detail": (
                        "매핑된 카카오 승인 봉투 SID가 공급사에 없습니다. "
                        "이 트리거는 fail-closed 상태입니다."
                    ),
                }
            )
            continue
        if not effective_template.is_approved:
            enabled_unapproved_template += 1
            enabled_issues.append(
                {
                    "trigger": config.trigger,
                    "code": "provider_template_unapproved",
                    "detail": "연결된 카카오 봉투가 승인 상태가 아닙니다.",
                }
            )

    approved_count = MessageTemplate.objects.filter(tenant=tenant, solapi_status="APPROVED").count()
    owner_id = get_owner_tenant_id()
    owner_approved_count = 0
    if int(tenant.id) != owner_id:
        owner_approved_count = MessageTemplate.objects.filter(tenant_id=owner_id, solapi_status="APPROVED").count()

    risks: list[dict[str, str]] = []
    if overdue:
        risks.append({"code": "scheduled_overdue", "title": "예약 지연", "detail": f"예약 {overdue}건이 예정 시각을 지났습니다."})
    if stale_dispatching:
        risks.append(
            {
                "code": "scheduled_dispatch_stale",
                "title": "큐 등록 상태 확인",
                "detail": f"큐 등록 중 상태가 5분을 넘긴 예약이 {stale_dispatching}건입니다.",
            }
        )
    if failed or failed_scheduled_24h:
        risks.append({"code": "recent_failures", "title": "최근 실패", "detail": f"최근 24시간 실패 로그 {failed}건, 예약 실패 {failed_scheduled_24h}건입니다."})
    if action_required:
        risks.append(
            {
                "code": "provider_outcome_ambiguous",
                "title": "공급사 결과 확인 필요",
                "detail": (
                    "공급사 호출 경계에 있거나 결과를 확정하지 못한 발송이 "
                    f"{action_required}건(sending {sending}, ambiguous {ambiguous})입니다. "
                    "중복 방지를 위해 자동 재발송하지 않았습니다."
                ),
            }
        )
    if provider_daily_used >= provider_daily_limit:
        risks.append(
            {
                "code": "provider_daily_limit_reached",
                "title": "일일 발송 안전 한도 도달",
                "detail": (
                    f"공급자 계정 기준 오늘 {provider_daily_used}건이 예약되어 "
                    "신규 발송은 다음 KST 날짜로 이월됩니다."
                ),
            }
        )
    heartbeat = _latest_worker_heartbeat()
    if (
        heartbeat["status"] in {"unknown", "stale"}
        and not pending
        and not due_now
        and not dispatching_qs.exists()
        and not processing
        and not sending
    ):
        heartbeat = {
            **heartbeat,
            "status": "idle",
            "idle_reason": "scale_to_zero_no_backlog",
        }
    if heartbeat["status"] not in {"ok", "idle"}:
        risks.append({"code": "worker_attention", "title": "워커 확인", "detail": "메시징 워커 heartbeat가 없거나 오래되었습니다."})
    if enabled_without_template or enabled_unapproved_template or enabled_manual_only:
        risks.append(
            {
                "code": "auto_send_template_attention",
                "title": "자동발송 설정 확인",
                "detail": "켜진 자동발송 중 템플릿/구현 상태 확인이 필요한 항목이 있습니다.",
            }
        )

    return {
        "checked_at": now,
        "worker": heartbeat,
        "scheduled": {
            "pending": pending,
            "due_now": due_now,
            "overdue": overdue,
            "retry_waiting": retry_waiting,
            "dispatching": dispatching_qs.count(),
            "stale_dispatching": stale_dispatching,
            "failed_24h": failed_scheduled_24h,
        },
        "log_24h": {
            "sent": sent,
            "failed": failed,
            "processing": processing,
            "sending": recent_sending,
            "retryable_failed": retryable_failed,
            "ambiguous": recent_ambiguous,
            "action_required": recent_sending + recent_ambiguous,
            "total": log_qs.count(),
        },
        "unresolved": {
            "sending": sending,
            "ambiguous": ambiguous,
            "action_required": action_required,
            "age_buckets": {
                "under_1h": unresolved_under_1h,
                "from_1h_to_24h": unresolved_1h_to_24h,
                "over_24h": unresolved_over_24h,
            },
        },
        "rate_limit_hourly": {
            "limit": HOURLY_SEND_LIMIT,
            "used": hourly_used,
            "remaining": max(0, HOURLY_SEND_LIMIT - hourly_used),
        },
        "rate_limit_provider_daily": {
            "limit": provider_daily_limit,
            "used": provider_daily_used,
            "remaining": max(0, provider_daily_limit - provider_daily_used),
            "timezone": "Asia/Seoul",
        },
        "templates": {
            "approved": approved_count,
            "owner_approved": owner_approved_count,
            "freeform_available": False,
            "freeform_template_name": "",
        },
        "auto_send": {
            "enabled": len(enabled_configs),
            "enabled_without_template": enabled_without_template,
            "enabled_unapproved_template": enabled_unapproved_template,
            "enabled_manual_only": enabled_manual_only,
            "issues": enabled_issues,
        },
        "risks": risks,
    }
