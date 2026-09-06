"""
AI 도메인 엔티티 — 순수 파이썬 (Django/ORM/requests/boto3 미사용)

상태 전이 규칙은 엔티티 메서드로 표현.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class AIJobStatus(str, Enum):
    """AI Job 상태 (apps.domains.ai.models AIJobModel choices와 동기화)."""
    PENDING = "PENDING"
    VALIDATING = "VALIDATING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    REJECTED_BAD_INPUT = "REJECTED_BAD_INPUT"
    FALLBACK_TO_GPU = "FALLBACK_TO_GPU"
    RETRYING = "RETRYING"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


# 최종 상태 (멱등: 이미 이 상태면 완료 처리 OK)
FINAL_STATUSES = (AIJobStatus.DONE, AIJobStatus.FAILED, AIJobStatus.REJECTED_BAD_INPUT, AIJobStatus.FALLBACK_TO_GPU, AIJobStatus.REVIEW_REQUIRED)


@dataclass
class AIJob:
    """
    AI Job 도메인 엔티티.
    DB/ORM 없이 규칙만 보유.
    """
    job_id: str
    job_type: str
    status: AIJobStatus
    payload: dict[str, Any]
    tenant_id: Optional[str] = None
    source_domain: Optional[str] = None
    source_id: Optional[str] = None
    tier: str = "basic"
    attempt_count: int = 0
    max_attempts: int = 5
    locked_by: Optional[str] = None
    locked_at: Optional[datetime] = None
    lease_expires_at: Optional[datetime] = None
    idempotency_key: Optional[str] = None
    error_message: str = ""
    updated_at: Optional[datetime] = None

    def is_terminal(self) -> bool:
        """이미 최종 상태면 True (멱등 처리 시 스킵)."""
        return self.status in FINAL_STATUSES

    def can_start(self) -> bool:
        """RUNNING으로 전이 가능한지."""
        return self.status in (AIJobStatus.PENDING, AIJobStatus.RETRYING)

    def start(self, worker_id: str, lease_expires_at: datetime, now: datetime) -> None:
        """
        PENDING/RETRYING → RUNNING.
        규칙 위반 시 ValueError.
        """
        if not self.can_start():
            raise ValueError(f"Cannot start job {self.job_id}: status={self.status}")
        self.status = AIJobStatus.RUNNING
        self.locked_by = worker_id
        self.locked_at = now
        self.lease_expires_at = lease_expires_at
        self.updated_at = now

    def complete(self, now: datetime) -> None:
        """RUNNING → DONE."""
        if self.status != AIJobStatus.RUNNING:
            raise ValueError(f"Cannot complete job {self.job_id}: status={self.status}")
        self.status = AIJobStatus.DONE
        self.locked_by = None
        self.locked_at = None
        self.lease_expires_at = None
        self.updated_at = now

    def fail(self, error_message: str, final_status: AIJobStatus, now: datetime) -> None:
        """PENDING/RETRYING/RUNNING → FAILED/REVIEW_REQUIRED 등."""
        if self.status not in (
            AIJobStatus.PENDING,
            AIJobStatus.RETRYING,
            AIJobStatus.RUNNING,
        ):
            raise ValueError(f"Cannot fail job {self.job_id}: status={self.status}")
        self.status = final_status
        self.error_message = (error_message or "")[:2000]
        self.locked_by = None
        self.locked_at = None
        self.lease_expires_at = None
        self.updated_at = now
