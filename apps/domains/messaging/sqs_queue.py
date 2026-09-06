"""
SQS 기반 메시지 발송 큐

API/서비스에서 enqueue → messaging_worker가 소비하여 Solapi로 발송
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Optional
from uuid import uuid4

from django.conf import settings
from django.utils import timezone

from libs.queue import get_queue_client

logger = logging.getLogger(__name__)


def build_business_idempotency_key(
    tenant_id: int,
    source_tenant_id: int | None,
    channel: str,
    event_type: str = "manual_send",
    target_type: str = "",
    target_id: str = "",
    recipient: str = "",
    occurrence_key: str = "",
    template_id: str = "",
) -> str:
    """Build SHA-256 business idempotency key from domain fields."""
    source_part = "" if source_tenant_id is None else str(int(source_tenant_id))
    canonical = (
        f"msg:{tenant_id}:{source_part}:{channel}:{event_type}:"
        f"{target_type}:{target_id}:{recipient}:{occurrence_key}:{template_id}"
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class MessagingSQSQueue:
    """
    메시지 발송 작업 SQS 큐

    메시지 형식:
    {
        "to": str,
        "text": str,
        "sender": str | None,
        "reservation_id": int | None,
        "message_mode": "alimtalk",  # SMS/LMS 신규 실발송 금지
        "alimtalk_replacements": list[{"key": str, "value": str}] | None,
    }
    """

    # V1 SSOT: academy-v1-messaging-queue
    QUEUE_NAME = "academy-v1-messaging-queue"
    DLQ_NAME = "academy-v1-messaging-queue-dlq"

    def __init__(self, *, wake_messaging_workers: Optional[bool] = None):
        self.queue_client = get_queue_client()
        self.wake_messaging_workers = True if wake_messaging_workers is None else bool(wake_messaging_workers)

    def _get_queue_name(self) -> str:
        return getattr(settings, "MESSAGING_SQS_QUEUE_NAME", self.QUEUE_NAME)

    def _enqueue_wake_capacity(self) -> int:
        raw_value = os.getenv(
            "MESSAGING_WORKER_ENQUEUE_WAKE_CAPACITY",
            str(getattr(settings, "MESSAGING_WORKER_ENQUEUE_WAKE_CAPACITY", 1)),
        )
        try:
            return max(1, int(raw_value))
        except (TypeError, ValueError):
            return 1

    def _wake_messaging_workers_after_enqueue(self) -> None:
        if not self.wake_messaging_workers:
            return
        try:
            from academy.adapters.compute.ec2_control import ensure_messaging_worker_asg_min_capacity

            ensure_messaging_worker_asg_min_capacity(
                min_capacity=self._enqueue_wake_capacity(),
            )
        except Exception:
            logger.warning(
                "Messaging worker wake-up failed after enqueue; message remains queued",
                exc_info=True,
            )

    def enqueue(
        self,
        *,
        tenant_id: int,
        to: str,
        text: str,
        sender: Optional[str] = None,
        reservation_id: Optional[int] = None,
        message_mode: Optional[str] = None,
        alimtalk_replacements: Optional[list[dict]] = None,
        template_id: Optional[str] = None,
        provider_template_type: Optional[str] = None,
        provider_template_version: Optional[str] = None,
        provider_template_structure_fingerprint: Optional[str] = None,
        provider_template_content_fingerprint: Optional[str] = None,
        provider_template_header_fingerprint: Optional[str] = None,
        content_template_id: Optional[int] = None,
        content_template_version: Optional[str] = None,
        content_snapshot_sha256: Optional[str] = None,
        event_type: Optional[str] = None,
        target_type: Optional[str] = None,
        target_id: Optional[int | str] = None,
        target_name: Optional[str] = None,
        source_tenant_id: Optional[int] = None,
        occurrence_key: Optional[str] = None,
        source_domain: Optional[str] = None,
        source_use_case: Optional[str] = None,
        domain_object_id: Optional[str] = None,
        actor_id: Optional[int | str] = None,
        origin_type: Optional[str] = None,
        origin_id: Optional[str] = None,
    ) -> bool:
        """
        발송 작업을 SQS에 추가

        Args:
            tenant_id: 공용 owner 테넌트 ID (업무 테넌트는 source_tenant_id)
            to: 수신 번호
            text: 본문
            sender: 발신 번호
            reservation_id: 예약 ID (워커에서 취소 시 스킵)
            message_mode: "alimtalk"만 허용
            alimtalk_replacements: 알림톡 치환 [{"key": "학생이름2", "value": "길동"}, ...]
            template_id: 알림톡 템플릿 ID (미지정 시 워커 기본값 사용)
        """
        mode = (message_mode or "").strip().lower() or "alimtalk"
        from apps.domains.messaging.policy import (
            MessagingPolicyError,
            get_message_mode_block_reason,
        )

        block_reason = get_message_mode_block_reason(mode)
        if block_reason:
            logger.error(
                "enqueue blocked: non-Alimtalk mode=%s tenant=%s",
                mode,
                tenant_id,
            )
            raise MessagingPolicyError(
                "공용 알림톡만 발송할 수 있습니다.",
                reason=block_reason,
            )

        from apps.domains.messaging.security import build_recipient_fingerprint

        message = {
            "tenant_id": int(tenant_id),
            "to": str(to).replace("-", "").strip(),
            "text": (text or "").strip(),
            "sender": None,
            "created_at": timezone.now().isoformat(),
            "message_mode": mode,
        }
        if reservation_id is not None:
            message["reservation_id"] = int(reservation_id)
        if alimtalk_replacements:
            message["alimtalk_replacements"] = alimtalk_replacements
        if template_id:
            message["template_id"] = str(template_id)
        if provider_template_type:
            message["provider_template_type"] = str(provider_template_type)[:32]
        if provider_template_version:
            message["provider_template_version"] = str(provider_template_version)[:64]
        if provider_template_structure_fingerprint:
            message["provider_template_structure_fingerprint"] = str(
                provider_template_structure_fingerprint
            )[:64]
        if provider_template_content_fingerprint:
            message["provider_template_content_fingerprint"] = str(
                provider_template_content_fingerprint
            )[:64]
        if provider_template_header_fingerprint:
            message["provider_template_header_fingerprint"] = str(
                provider_template_header_fingerprint
            )[:64]
        if content_template_id is not None:
            message["content_template_id"] = int(content_template_id)
        if content_template_version:
            message["content_template_version"] = str(content_template_version)[:64]
        if content_snapshot_sha256:
            message["content_snapshot_sha256"] = str(content_snapshot_sha256)[:64]
        if target_type:
            message["target_type"] = str(target_type)[:20]
        if target_id:
            message["target_id"] = str(target_id)
        if target_name:
            message["target_name"] = str(target_name)[:50]
        if source_tenant_id is not None:
            message["source_tenant_id"] = int(source_tenant_id)
        if source_domain:
            message["source_domain"] = str(source_domain)[:50]
        if source_use_case:
            message["source_use_case"] = str(source_use_case)[:80]
        if domain_object_id:
            message["domain_object_id"] = str(domain_object_id)[:120]
        if actor_id:
            message["actor_id"] = str(actor_id)[:50]
        resolved_origin_type = str(
            origin_type or source_use_case or source_domain or ""
        ).strip()[:64]
        resolved_origin_id = str(
            origin_id or domain_object_id or ""
        ).strip()[:128]
        if resolved_origin_type:
            message["origin_type"] = resolved_origin_type
        if resolved_origin_id:
            message["origin_id"] = resolved_origin_id
        message["recipient_fingerprint"] = build_recipient_fingerprint(message["to"])
        et = (event_type or "").strip()
        if et:
            message["event_type"] = et[:30]
        resolved_occurrence_key = occurrence_key or f"manual-{uuid4().hex}"
        message["occurrence_key"] = str(resolved_occurrence_key)[:128]
        message["business_idempotency_key"] = build_business_idempotency_key(
            tenant_id=int(tenant_id),
            source_tenant_id=int(source_tenant_id) if source_tenant_id is not None else None,
            channel=mode,
            event_type=str(message.get("event_type") or "manual_send"),
            target_type=str(message.get("target_type") or ""),
            target_id=str(message.get("target_id") or ""),
            recipient=message["to"],
            occurrence_key=message["occurrence_key"],
            template_id=str(message.get("template_id") or ""),
        )
        from apps.domains.messaging.security import (
            DELIVERY_IDENTITY_SIGNATURE_VERSION,
            build_delivery_identity_signature,
            build_tenant_binding_signature,
        )

        message["tenant_binding_signature"] = build_tenant_binding_signature(
            tenant_id=int(tenant_id),
            source_tenant_id=(
                int(source_tenant_id) if source_tenant_id is not None else None
            ),
            business_idempotency_key=message["business_idempotency_key"],
        )
        message["tenant_binding_signature_version"] = "v1"
        if message.get("provider_template_version") and message.get(
            "content_snapshot_sha256"
        ):
            message["delivery_identity_version"] = DELIVERY_IDENTITY_SIGNATURE_VERSION
            message["delivery_identity_signature"] = build_delivery_identity_signature(
                message
            )
        if not message["to"] or not message["text"]:
            logger.warning("enqueue skipped: to or text empty")
            return False
        try:
            ok = self.queue_client.send_message(
                queue_name=self._get_queue_name(),
                message=message,
            )
            if ok:
                logger.info("Messaging job enqueued: to=%s", message["to"][:4] + "****")
                self._wake_messaging_workers_after_enqueue()
            return bool(ok)
        except Exception as e:
            logger.exception("Error enqueuing messaging job: %s", e)
            return False

    def receive_message(self, wait_time_seconds: int = 20) -> Optional[dict]:
        """
        SQS에서 메시지 수신 (Long Polling)

        Returns:
            dict: enqueue payload 전체와 receipt_handle/message_id 또는 None.
            business_idempotency_key와 source/target 메타데이터를 유실하지 않는다.
        """
        try:
            raw = self.queue_client.receive_message(
                queue_name=self._get_queue_name(),
                wait_time_seconds=wait_time_seconds,
            )
            if not raw:
                return None
            body = raw.get("Body", "")
            receipt_handle = raw.get("ReceiptHandle")
            if not receipt_handle:
                return None
            if isinstance(body, str):
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    logger.error("Invalid JSON in message body")
                    return None
            else:
                data = body
            if not isinstance(data, dict) or "to" not in data or "text" not in data:
                logger.error(
                    "Invalid message format: message_id=%s payload_type=%s keys=%s",
                    raw.get("MessageId"),
                    type(data).__name__,
                    sorted(str(key) for key in data.keys())[:30]
                    if isinstance(data, dict)
                    else [],
                )
                return None
            message = dict(data)
            message.update(
                {
                    "tenant_id": data.get("tenant_id"),
                    "to": str(data.get("to", "")),
                    "text": str(data.get("text", "")),
                    "sender": (data.get("sender") or "").strip() or None,
                    "receipt_handle": receipt_handle,
                    "message_id": raw.get("MessageId"),
                    "created_at": data.get("created_at"),
                    "reservation_id": data.get("reservation_id"),
                    "message_mode": (data.get("message_mode") or "").strip().lower() or "alimtalk",
                    "template_id": data.get("template_id"),
                    "alimtalk_replacements": data.get("alimtalk_replacements") or [],
                }
            )
            return message
        except Exception as e:
            logger.exception("Error receiving messaging message: %s", e)
            return None

    def delete_message(self, receipt_handle: str) -> bool:
        """처리 완료된 메시지 삭제"""
        try:
            return self.queue_client.delete_message(
                queue_name=self._get_queue_name(),
                receipt_handle=receipt_handle,
            )
        except Exception as e:
            logger.exception("Error deleting message: %s", e)
            return False
