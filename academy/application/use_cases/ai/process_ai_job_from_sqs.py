"""
AI Job SQS 처리 Use Case — 도메인/포트만 사용 (Django/boto3 미사용)

상태 전이·멱등은 여기서 결정; DB/SQS 연동은 UoW·Queue 어댑터가 수행.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from academy.application.ports.repositories import AIJobRepository
from academy.application.ports.unit_of_work import UnitOfWork

logger = logging.getLogger(__name__)


# 기본 lease 2분 (SQS visibility와 맞추기 위해 어댑터/설정에서 오버라이드 가능)
DEFAULT_LEASE_SECONDS = 120


@dataclass
class PreparedJob:
    """prepare_ai_job 성공 시 반환. inference 실행 후 complete_ai_job / fail_ai_job 호출."""
    job_id: str
    job_type: str
    tier: str
    payload: dict[str, Any]
    receipt_handle: str
    tenant_id: Optional[str] = None
    source_domain: Optional[str] = None
    source_id: Optional[str] = None


def prepare_ai_job(
    uow: UnitOfWork,
    job_id: str,
    receipt_handle: str,
    tier: str,
    payload: dict[str, Any],
    job_type: str = "",
    tenant_id: Optional[str] = None,
    source_domain: Optional[str] = None,
    source_id: Optional[str] = None,
    worker_id: str = "sqs-worker",
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    now: Optional[datetime] = None,
) -> Optional[PreparedJob]:
    """
    Job을 RUNNING으로 전이 (멱등: 이미 DONE/FAILED면 None 반환).
    호출 후 inference 실행, 완료 시 complete_ai_job 또는 fail_ai_job 호출.
    """
    if now is None:
        from datetime import timezone
        now = datetime.now(timezone.utc)
    lease_expires_at = now + timedelta(seconds=lease_seconds)

    with uow:
        repo: AIJobRepository = uow.ai_jobs
        existing = repo.get_for_update(job_id)
        if existing is None:
            logger.warning("AI_JOB_NOT_FOUND | job_id=%s", job_id)
            return None
        if not tenant_id:
            logger.error(
                "AI_JOB_MISSING_TENANT_ID_REJECTED | job_id=%s | source_domain=%s | source_id=%s",
                job_id, source_domain, source_id,
            )
            if not existing.is_terminal():
                repo.mark_failed(job_id, "missing_tenant_id_in_sqs_message", tier, now)
            return None
        if str(existing.tenant_id or "") != str(tenant_id):
            logger.error(
                "AI_JOB_TENANT_MISMATCH_REJECTED | job_id=%s | message_tenant_id=%s | db_tenant_id=%s",
                job_id, tenant_id, existing.tenant_id,
            )
            if not existing.is_terminal():
                repo.mark_failed(job_id, "tenant_mismatch_in_sqs_message", tier, now)
            return None
        payload_tenant_id = payload.get("tenant_id") if isinstance(payload, dict) else None
        if payload_tenant_id is not None and str(payload_tenant_id) != str(tenant_id):
            logger.error(
                "AI_JOB_PAYLOAD_TENANT_MISMATCH_REJECTED | job_id=%s | "
                "message_tenant_id=%s | payload_tenant_id=%s",
                job_id, tenant_id, payload_tenant_id,
            )
            if not existing.is_terminal():
                repo.mark_failed(job_id, "payload_tenant_mismatch_in_sqs_message", tier, now)
            return None
        if not repo.mark_running(job_id, worker_id, lease_expires_at, now):
            return None
    return PreparedJob(
        job_id=job_id,
        job_type=job_type,
        tier=tier,
        payload=payload,
        receipt_handle=receipt_handle,
        tenant_id=tenant_id,
        source_domain=source_domain,
        source_id=source_id,
    )


def complete_ai_job(
    uow: UnitOfWork,
    job_id: str,
    result_payload: Optional[dict] = None,
    now: Optional[datetime] = None,
) -> bool:
    """RUNNING → DONE. 동일한 성공 DONE만 True로 멱등 복구."""
    if now is None:
        from datetime import timezone
        now = datetime.now(timezone.utc)
    with uow:
        return uow.ai_jobs.mark_done(job_id, now, result_payload=result_payload)


def fail_ai_job(
    uow: UnitOfWork,
    job_id: str,
    error_message: str,
    tier: str = "basic",
    now: Optional[datetime] = None,
) -> bool:
    """RUNNING → tier별 실패 결과. 동일한 실패 결과만 True로 멱등 복구."""
    if now is None:
        from datetime import timezone
        now = datetime.now(timezone.utc)
    with uow:
        return uow.ai_jobs.mark_failed(job_id, error_message, tier, now)
