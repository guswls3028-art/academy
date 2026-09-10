# PATH: apps/core/management/commands/check_dev_alerts.py
"""
/dev 운영 알림 룰.

화이트리스트 룰을 평가해서 임계치 초과 시 Slack incoming webhook으로 전송한다.
제품 메시징은 공용 카카오 알림톡만 사용하며 이 운영 명령은 SMS를 보내지 않는다.
크론에서 호출: python manage.py check_dev_alerts [--dry-run] [--silent]

Webhook 설정:
  DEV_ALERTS_WEBHOOK_URL=https://hooks.slack.com/services/...
  비어 있으면 실패 종료. 수신처 없이 평가만 하려면 --dry-run을 명시한다.
"""
from __future__ import annotations

import json
import hashlib
import logging
import urllib.error
import urllib.request
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, Max, Sum
from django.utils import timezone

logger = logging.getLogger(__name__)

USER_INCIDENT_ACTIONS = (
    "user_incident.manual",
    "user_incident.frontend_exception",
    "user_incident.backend_5xx",
)
LEGACY_SMS_DELIVERY_ACTION = "alerts.user_incident_sms"
SLACK_DELIVERY_ACTION = "alerts.user_incident_slack"
INCIDENT_RETENTION_DAYS = 2


class Rule:
    def __init__(self, key: str, label: str, evaluate, severity: str = "warning"):
        self.key = key
        self.label = label
        self.evaluate = evaluate  # callable() -> dict | None
        self.severity = severity


def _exempt_ids() -> list[int]:
    ids = list(getattr(settings, "BILLING_EXEMPT_TENANT_IDS", []) or [])
    owner_id = getattr(settings, "OWNER_TENANT_ID", None)
    if owner_id is not None and owner_id not in ids:
        ids.append(owner_id)
    return ids


# ── 룰 평가자 ──

def rule_expiring_3d():
    from apps.core.models import Program
    today = timezone.localdate()
    qs = (
        Program.objects.exclude(tenant_id__in=_exempt_ids())
        .filter(
            subscription_status="active",
            subscription_expires_at__gte=today,
            subscription_expires_at__lte=today + timedelta(days=3),
        )
        .select_related("tenant")
        .order_by("subscription_expires_at")
    )
    rows = [
        {
            "tenant": p.tenant.code,
            "name": p.tenant.name,
            "expires_at": str(p.subscription_expires_at),
            "days_remaining": p.days_remaining,
        }
        for p in qs[:50]
    ]
    if not rows:
        return None
    return {
        "title": f"⏰ 만료 3일 이내 — {len(rows)}건",
        "rows": rows,
        "total": len(rows),
    }


def rule_overdue_invoices():
    from apps.billing.models import Invoice
    qs = (
        Invoice.objects.filter(status__in=["OVERDUE", "FAILED"])
        .exclude(tenant_id__in=_exempt_ids())
        .select_related("tenant")
        .order_by("-due_date")
    )
    rows = [
        {
            "tenant": inv.tenant.code if inv.tenant else "—",
            "invoice": inv.invoice_number,
            "amount": int(inv.total_amount or 0),
            "due_date": str(inv.due_date) if inv.due_date else "—",
            "status": inv.status,
        }
        for inv in qs[:50]
    ]
    if not rows:
        return None
    total_amount = qs.aggregate(t=Sum("total_amount"))["t"] or 0
    return {
        "title": f"💸 연체/실패 인보이스 — {len(rows)}건 / {int(total_amount):,}원",
        "rows": rows,
        "total": len(rows),
    }


def rule_stale_processing_payments(min_age_minutes: int | None = None):
    """Provider outcome unknown transactions require manual reconciliation."""
    from django.db.models import Q

    from apps.billing.models import PaymentTransaction

    threshold = int(
        min_age_minutes
        if min_age_minutes is not None
        else getattr(settings, "BILLING_PROCESSING_ALERT_MINUTES", 15)
    )
    cutoff = timezone.now() - timedelta(minutes=threshold)
    qs = (
        PaymentTransaction.objects.filter(status="PROCESSING")
        .exclude(tenant_id__in=_exempt_ids())
        .filter(
            Q(processing_started_at__lte=cutoff)
            | Q(processing_started_at__isnull=True, created_at__lte=cutoff)
        )
        .select_related("tenant", "invoice")
        .order_by("processing_started_at", "id")
    )
    total = qs.count()
    rows = [
        {
            "tenant": tx.tenant.code,
            "transaction_id": tx.id,
            "invoice": tx.invoice.invoice_number,
            "started_at": (
                tx.processing_started_at or tx.created_at
            ).isoformat(timespec="seconds"),
        }
        for tx in qs[:50]
    ]
    if not rows:
        return None
    return {
        "title": (
            f"🚨 결제 공급사 결과 확인 필요 — {total}건 "
            f"({threshold}분+ PROCESSING)"
        ),
        "rows": rows,
        "total": total,
    }


def rule_card_reconciliation_required(max_age_hours: int = 24):
    """Alert immediately when provider card state and local state may diverge."""
    from apps.core.models import OpsAuditLog

    since = timezone.now() - timedelta(hours=max_age_hours)
    qs = (
        OpsAuditLog.objects.filter(
            action="billing.card_reconciliation_required",
            created_at__gte=since,
        )
        .select_related("target_tenant")
        .order_by("-created_at")
    )
    total = qs.count()
    rows = [
        {
            "tenant": log.target_tenant.code if log.target_tenant else "—",
            "operation": (log.payload or {}).get("operation", "unknown"),
            "billing_key_id": (log.payload or {}).get("billing_key_id"),
            "at": log.created_at.isoformat(timespec="seconds"),
        }
        for log in qs[:50]
    ]
    if not rows:
        return None
    return {
        "title": f"🚨 카드 공급사 상태 대사 필요 — {total}건",
        "rows": rows,
        "total": total,
    }


def rule_partial_refund_reconciliation_required(max_age_hours: int = 24):
    from apps.core.models import OpsAuditLog

    since = timezone.now() - timedelta(hours=max_age_hours)
    qs = (
        OpsAuditLog.objects.filter(
            action="billing.partial_refund_reconciliation_required",
            created_at__gte=since,
        )
        .select_related("target_tenant")
        .order_by("-created_at")
    )
    total = qs.count()
    rows = [
        {
            "tenant": log.target_tenant.code if log.target_tenant else "—",
            "transaction_id": (log.payload or {}).get("transaction_id"),
            "invoice_id": (log.payload or {}).get("invoice_id"),
            "refunded_amount": (log.payload or {}).get("refunded_amount"),
            "at": log.created_at.isoformat(timespec="seconds"),
        }
        for log in qs[:50]
    ]
    if not rows:
        return None
    return {
        "title": f"🚨 부분환불 회계·구독 대사 필요 — {total}건",
        "rows": rows,
        "total": total,
    }


def rule_audit_failed_24h(threshold: int = 5):
    from apps.core.models import OpsAuditLog
    since = timezone.now() - timedelta(hours=24)
    qs = OpsAuditLog.objects.filter(created_at__gte=since, result="failed").order_by("-created_at")
    count = qs.count()
    if count < threshold:
        return None
    rows = [
        {
            "action": r.action,
            "actor": r.actor_username or "—",
            "summary": r.summary,
            "error": r.error,
            "at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in qs[:20]
    ]
    return {
        "title": f"🔥 24h 실패 작업 {count}건 (임계치 {threshold})",
        "rows": rows,
        "total": count,
    }


def rule_unanswered_inbox(min_age_hours: int = 24):
    """생성된 지 N시간 이상 지난 비공개 지원 티켓과 플랫폼 도입 문의."""
    from apps.core.models import LandingConsultRequest
    from apps.core.services.platform_inbox import PROMO_LEAD_SOURCES
    from apps.domains.community.models import PostEntity, platform_support_q
    from django.db.models import F, Max, Q

    since = timezone.now() - timedelta(hours=min_age_hours)
    support_tickets = (
        PostEntity.objects.filter(post_type="board", created_at__lte=since)
        .filter(platform_support_q())
        .annotate(
            _latest_platform_reply=Max(
                "replies__created_at",
                filter=Q(replies__author_role="platform_staff"),
            ),
            _latest_requester_reply=Max(
                "replies__created_at",
                filter=~Q(replies__author_role="platform_staff"),
            ),
        )
        .filter(
            Q(_latest_platform_reply__isnull=True)
            | Q(_latest_requester_reply__gt=F("_latest_platform_reply"))
        )
        .select_related("tenant")
        .order_by("-created_at")
    )
    support_total = support_tickets.count()
    rows = [
        {
            "tenant": p.tenant.code if p.tenant else "—",
            "source": "support",
            "title": (p.title or "")[:60],
            "at": p.created_at.isoformat() if p.created_at else None,
        }
        for p in support_tickets[:15]
    ]
    owner_tenant_id = getattr(settings, "OWNER_TENANT_ID", None)
    lead_total = 0
    if owner_tenant_id is not None:
        leads = (
            LandingConsultRequest.objects.filter(
                tenant_id=owner_tenant_id,
                source__in=PROMO_LEAD_SOURCES,
                resolved_at__isnull=True,
                created_at__lte=since,
            )
            .select_related("tenant")
            .order_by("-created_at")
        )
        lead_total = leads.count()
        rows.extend(
            {
                "tenant": lead.tenant.code if lead.tenant else "—",
                "source": lead.source,
                "title": (lead.interest or "도입 문의")[:60],
                "at": lead.created_at.isoformat() if lead.created_at else None,
            }
            for lead in leads[:15]
        )
    if not rows:
        return None
    return {
        "title": f"📬 24h+ 미답변 문의 {support_total + lead_total}건",
        "rows": rows,
        "total": support_total + lead_total,
    }


def _incident_fingerprint(*parts: object) -> str:
    raw = json.dumps(parts, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _delivered_incident_fingerprints() -> set[str]:
    from apps.core.models import OpsAuditLog

    since = timezone.now() - timedelta(days=INCIDENT_RETENTION_DAYS)
    delivered: set[str] = set()
    payloads = OpsAuditLog.objects.filter(
        action__in=(LEGACY_SMS_DELIVERY_ACTION, SLACK_DELIVERY_ACTION),
        created_at__gte=since,
    ).values_list("result", "payload")
    for result, payload in payloads:
        attempt_state = str((payload or {}).get("attempt_state") or "")
        if result != "success" and attempt_state not in {
            "",
            "created",
            "registered",
            "ambiguous",
        }:
            continue
        for fingerprint in (payload or {}).get("fingerprints", []):
            if isinstance(fingerprint, str):
                delivered.add(fingerprint)
    return delivered


def _incident_scan_since():
    """늦게 확정 실패한 attempt도 재평가하도록 보존 범위 전체를 읽는다."""
    return timezone.now() - timedelta(days=INCIDENT_RETENTION_DAYS)


def rule_user_incidents(window_minutes: int | None = None):
    """최근 사용자 오류와 명시적 버그 제보를 PII 없이 묶고, 발송 완료 건은 제외한다."""
    from apps.core.models import OpsAuditLog

    since = (
        timezone.now() - timedelta(minutes=window_minutes)
        if window_minutes is not None
        else _incident_scan_since()
    )
    grouped: dict[str, dict] = {}

    incident_logs = (
        OpsAuditLog.objects.filter(
            action__in=USER_INCIDENT_ACTIONS,
            created_at__gte=since,
        )
        .select_related("target_tenant")
        .order_by("created_at", "id")
    )
    for log in incident_logs.iterator(chunk_size=500):
        payload = log.payload or {}
        tenant = log.target_tenant.code if log.target_tenant else "public"
        route = str(payload.get("route") or "/unknown")[:120]
        if log.action == "user_incident.manual":
            source = "report"
            fingerprint = _incident_fingerprint("manual", log.id)
        else:
            source = (
                "frontend"
                if log.action == "user_incident.frontend_exception"
                else "backend"
            )
            error_name = str(payload.get("error_name") or payload.get("exception_name") or "")
            bucket = int(log.created_at.timestamp() // (15 * 60))
            fingerprint = _incident_fingerprint(
                source,
                log.target_tenant_id,
                payload.get("method") or "",
                route,
                error_name[:100],
                bucket,
            )
        row = grouped.setdefault(
            fingerprint,
            {
                "fingerprint": fingerprint,
                "source": source,
                "tenant_id": log.target_tenant_id,
                "tenant": tenant,
                "route": route,
                "status": payload.get("status"),
                "count": 0,
                "at": log.created_at.isoformat(timespec="seconds"),
            },
        )
        row["count"] += 1
        row["at"] = log.created_at.isoformat(timespec="seconds")

    from apps.domains.community.models import (
        PostEntity,
        platform_support_q,
        support_kind_for_post,
    )

    bug_posts = (
        PostEntity.objects.filter(
            post_type="board",
            status="published",
            created_at__gte=since,
        )
        .filter(platform_support_q())
        .select_related("tenant")
        .order_by("created_at", "id")
    )
    for post in bug_posts.iterator(chunk_size=200):
        if support_kind_for_post(post) != "bug":
            continue
        fingerprint = _incident_fingerprint("bug_post", post.id)
        grouped[fingerprint] = {
            "fingerprint": fingerprint,
            "source": "bug_post",
            "tenant_id": post.tenant_id,
            "tenant": post.tenant.code if post.tenant else "public",
            "route": "/developer/bug",
            "status": None,
            "count": 1,
            "at": post.created_at.isoformat(timespec="seconds"),
        }

    delivered = _delivered_incident_fingerprints()
    rows = [
        row
        for fingerprint, row in grouped.items()
        if fingerprint not in delivered
    ]
    rows.sort(key=lambda row: row["at"])
    if not rows:
        return None
    total = sum(int(row["count"]) for row in rows)
    return {
        "title": f"사용자 오류/문제 신고 - {total}건",
        "rows": [
            {key: value for key, value in row.items() if key != "fingerprint"}
            for row in rows
        ],
        "fingerprints": [row["fingerprint"] for row in rows],
        "total": total,
    }


def rule_stale_workers(min_age_minutes: int = 5):
    """N분+ heartbeat 미갱신 워커. SQS 워커 process 멈춤 즉시 감지."""
    from apps.core.models import WorkerHeartbeatModel
    from apps.shared.utils.heartbeat import HEARTBEAT_RETENTION_HOURS
    now = timezone.now()
    cutoff = now - timedelta(minutes=min_age_minutes)
    alert_floor = now - timedelta(hours=HEARTBEAT_RETENTION_HOURS)
    stale_workers = (
        WorkerHeartbeatModel.objects.values("name")
        .annotate(last_seen=Max("last_seen_at"))
        .filter(last_seen__lt=cutoff, last_seen__gte=alert_floor)
        .order_by("name")
    )
    total = stale_workers.count()
    rows = []
    for worker in stale_workers[:30]:
        h = (
            WorkerHeartbeatModel.objects.filter(name=worker["name"], last_seen_at=worker["last_seen"])
            .order_by("instance")
            .first()
        )
        rows.append({
            "worker": worker["name"],
            "instance": h.instance if h else "—",
            "last_seen": worker["last_seen"].isoformat(timespec="seconds") if worker["last_seen"] else None,
            "version": (h.version if h else "") or "—",
        })
    if not rows:
        return None
    return {
        "title": f"💔 워커 heartbeat 정지 {total}건 ({min_age_minutes}분+ 미갱신)",
        "rows": rows,
        "total": total,
    }


def rule_circuit_breaker_open():
    """현재 open 상태인 외부 API circuit (in-memory state는 alert에서 안 잡힘 → ops_audit 기반)."""
    from apps.core.models import OpsAuditLog
    # 최근 30분 내 circuit_open 액션 (해소되지 않은 상태)
    since = timezone.now() - timedelta(minutes=30)
    qs = OpsAuditLog.objects.filter(action="circuit.open", created_at__gte=since).order_by("-created_at")
    seen_keys: set[str] = set()
    rows: list[dict] = []
    for log in qs[:50]:
        # summary 형식: "{name} (failures={n})" — name 단위로 첫 등장만 표시
        name = (log.summary or "").split(" ")[0] or "unknown"
        if name in seen_keys:
            continue
        seen_keys.add(name)
        rows.append({
            "circuit": name,
            "at": log.created_at.isoformat(timespec="seconds") if log.created_at else None,
            "summary": (log.summary or "")[:80],
        })
    if not rows:
        return None
    return {
        "title": f"⚡ 외부 API circuit open {len(rows)}개",
        "rows": rows,
        "total": len(rows),
    }


def rule_messaging_delivery_health(window_minutes: int = 30):
    """Alert on shared Alimtalk balance risk and confirmed retryable rejections."""
    from apps.domains.messaging.models import NotificationLog
    from apps.domains.messaging.services.solapi_client import get_solapi_client

    since = timezone.now() - timedelta(minutes=window_minutes)
    failure_groups = (
        NotificationLog.objects.filter(
            sent_at__gte=since,
            status__in=["retryable_failed", "ambiguous"],
            failure_reason__icontains="NotEnoughBalance",
        )
        .values("source_tenant__code", "tenant__code", "status")
        .annotate(count=Count("id"))
        .order_by("source_tenant__code", "tenant__code", "status")
    )
    rows = [
        {
            "tenant": row["source_tenant__code"] or row["tenant__code"] or "unknown",
            "state": row["status"],
            "count": row["count"],
            "window_minutes": window_minutes,
        }
        for row in failure_groups
    ]

    threshold = Decimal(
        str(getattr(settings, "MESSAGING_PROVIDER_LOW_BALANCE_ALERT_THRESHOLD", 10_000))
    )
    try:
        client = get_solapi_client()
        if client is None:
            rows.append({"provider_balance_check": "client_unavailable"})
        else:
            response = client.get_balance()
            raw_balance = getattr(response, "balance", None)
            balance = Decimal(str(raw_balance))
            if balance < threshold:
                rows.append(
                    {
                        "provider_balance": str(balance),
                        "alert_threshold": str(threshold),
                    }
                )
    except (InvalidOperation, TypeError, ValueError):
        rows.append({"provider_balance_check": "invalid_response"})
    except Exception as exc:
        logger.warning("Solapi balance check failed (%s)", type(exc).__name__)
        rows.append({"provider_balance_check": "request_failed"})

    if not rows:
        return None
    return {
        "title": f"📨 알림톡 공급자 잔액/재시도 확인 필요 — {len(rows)}건",
        "rows": rows,
        "total": sum(int(row.get("count", 1)) for row in rows),
    }


RULES: list[Rule] = [
    Rule("user_incidents", "사용자 오류/문제 신고", rule_user_incidents, "danger"),
    Rule(
        "messaging_delivery_health",
        "알림톡 공급자 잔액/재시도",
        rule_messaging_delivery_health,
        "danger",
    ),
    Rule("expiring_3d", "만료 3일 이내", rule_expiring_3d, "warning"),
    Rule("overdue_invoices", "연체/실패 인보이스", rule_overdue_invoices, "danger"),
    Rule(
        "stale_processing_payments",
        "장기 PROCESSING 결제",
        rule_stale_processing_payments,
        "danger",
    ),
    Rule(
        "card_reconciliation_required",
        "카드 공급사 상태 대사 필요",
        rule_card_reconciliation_required,
        "danger",
    ),
    Rule(
        "partial_refund_reconciliation_required",
        "부분환불 회계·구독 대사 필요",
        rule_partial_refund_reconciliation_required,
        "danger",
    ),
    Rule("audit_failed_24h", "24h 실패 작업 임계 초과", rule_audit_failed_24h, "danger"),
    Rule("unanswered_inbox", "24h+ 미답변 문의", rule_unanswered_inbox, "warning"),
    Rule("stale_workers", "워커 heartbeat 정지", rule_stale_workers, "danger"),
    Rule("circuit_open", "외부 API circuit open", rule_circuit_breaker_open, "danger"),
]


# ── Slack 전송 ──

def _post_slack(webhook_url: str, payload: dict) -> bool:
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            return 200 <= resp.status < 300
    except Exception as exc:
        # Webhook URLs and provider responses can contain credentials or PII.
        logger.warning("Slack webhook failed (%s)", type(exc).__name__)
        return False


def _build_slack_blocks(triggered: list[tuple[Rule, dict]]) -> dict:
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "🚨 Academy Dev Alerts"},
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"_{timezone.now().isoformat(timespec='seconds')}_"}],
        },
        {"type": "divider"},
    ]
    for rule, data in triggered:
        title = data.get("title") or rule.label
        rows: list[dict] = data.get("rows") or []
        sample = rows[:5]
        body_lines = []
        for r in sample:
            body_lines.append("• " + " · ".join(f"{k}={v}" for k, v in r.items() if v is not None and v != ""))
        if len(rows) > len(sample):
            body_lines.append(f"… (+{len(rows) - len(sample)} more)")
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{title}*\n" + "\n".join(body_lines or ["—"])},
        })
    return {"blocks": blocks, "text": "Academy Dev Alerts"}


def _record_user_incident_slack_delivery(data: dict) -> None:
    """Slack이 수락한 사용자 오류 fingerprint를 저장해 반복 알림을 막는다."""
    from apps.core.models import OpsAuditLog

    OpsAuditLog.objects.create(
        action=SLACK_DELIVERY_ACTION,
        summary=f"User incident Slack delivery ({data.get('total', 0)} events)",
        payload={
            "fingerprints": list(data.get("fingerprints") or []),
            "event_count": max(0, int(data.get("total") or 0)),
        },
        result="success",
    )


def _record_cron_invocation(opts: dict, *, result: str, error: str = "") -> None:
    """Scheduled/manual command 실행 결과를 /dev 감사 로그에 남긴다."""
    try:
        from apps.core.models import OpsAuditLog

        selected_rules = list(opts.get("rule") or [])
        OpsAuditLog.objects.create(
            action="cron.check_dev_alerts",
            summary=f"check_dev_alerts {result}",
            payload={
                "rules": selected_rules or ["all"],
                "dry_run": bool(opts.get("dry_run")),
            },
            result=result,
            error=error[:255],
        )
    except Exception as exc:
        raise CommandError(
            f"check_dev_alerts invocation audit failed ({type(exc).__name__})"
        ) from None


# ── Command ──

class Command(BaseCommand):
    help = "/dev 운영 알림 룰 평가 + Slack webhook 전송"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="평가 결과만 출력 (Slack 전송 X).",
        )
        parser.add_argument(
            "--silent",
            action="store_true",
            help="무경고 출력을 생략하고 경고는 규칙 키와 건수만 출력.",
        )
        parser.add_argument(
            "--rule",
            action="append",
            default=[],
            help="이 옵션을 반복하면 해당 룰만 평가 (기본: 전체).",
        )

    def handle(self, *args, **opts):
        try:
            result = self._handle(*args, **opts)
        except CommandError as exc:
            _record_cron_invocation(opts, result="failed", error=str(exc))
            raise
        except Exception as exc:
            error = f"check_dev_alerts failed ({type(exc).__name__})"
            _record_cron_invocation(opts, result="failed", error=error)
            raise CommandError(error) from None
        _record_cron_invocation(opts, result="success")
        return result

    def _handle(self, *args, **opts):
        dry_run = opts["dry_run"]
        silent = opts["silent"]
        only = set(opts["rule"] or [])
        if only - {rule.key for rule in RULES}:
            raise CommandError("Unknown alert rule selection")
        rules = [rule for rule in RULES if not only or rule.key in only]

        failures: list[str] = []
        triggered: list[tuple[Rule, dict]] = []
        for rule in rules:
            try:
                result = rule.evaluate()
            except Exception as exc:
                error = f"Rule {rule.key} evaluation failed ({type(exc).__name__})"
                logger.warning(error)
                failures.append(error)
                continue
            if result:
                triggered.append((rule, result))

        for rule, data in triggered:
            if silent:
                self.stdout.write(f"[{rule.key}] rows={len(data.get('rows') or [])}")
                continue
            self.stdout.write(self.style.WARNING(f"\n[{rule.key}] {data.get('title')}"))
            for row in (data.get("rows") or [])[:10]:
                self.stdout.write("  " + json.dumps(row, ensure_ascii=False))

        if dry_run:
            self.stdout.write(self.style.NOTICE("\n--dry-run: Slack 전송 생략."))
        else:
            webhook_url = (getattr(settings, "DEV_ALERTS_WEBHOOK_URL", "") or "").strip()
            if not webhook_url:
                failures.append("DEV_ALERTS_WEBHOOK_URL is not configured")
            elif triggered:
                payload = _build_slack_blocks(triggered)
                if not _post_slack(webhook_url, payload):
                    failures.append("Slack delivery failed")
                else:
                    for rule, data in triggered:
                        if rule.key == "user_incidents":
                            _record_user_incident_slack_delivery(data)
                    self.stdout.write(self.style.SUCCESS(
                        f"\nSlack 전송 OK ({len(triggered)} rule(s))."
                    ))

        if failures:
            raise CommandError("; ".join(failures))
        if not triggered and not silent:
            self.stdout.write(
                self.style.SUCCESS(
                    "All clear — no rules triggered."
                )
            )
