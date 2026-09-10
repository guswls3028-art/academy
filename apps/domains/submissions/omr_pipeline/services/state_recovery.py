"""
OMR submission 상태 자동 복구.

워커 / 큐 / DB 장애로 OMR submission 이 SUBMITTED·DISPATCHED·EXTRACTING·
GRADING 중 어느 단계에서든 30 분 이상 진행이 안 되면 hung 으로 본다.

이전: 학원장이 "왜 이 답안지가 아직 채점 안 됐지" 하고 신고할 때까지
        운영자가 직접 manage shell 에서 reconcile 해야 했다 (운영 blind spot).

이 모듈은 두 단계로 분리한다:

- detect_stuck_submissions(): timeout 넘은 sub 들 식별 + 알람 audit.
- recover_stuck_submissions(): 식별된 sub 를 FAILED 로 전환 + meta.state_recovery
    audit 기록. 학원장 화면에 "처리 실패" 로 즉시 표면화 → retry endpoint 로
    다시 dispatch 가능.

호출:
- cron / EventBridge 에서 `python manage.py recover_stuck_omr_submissions` 5분마다.
- 운영 점검 시 same command --dry-run 으로 detect 만.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.db import transaction
from django.utils import timezone

from apps.domains.submissions.models import Submission
from apps.domains.submissions.services.lifecycle import (
    InvalidTransitionError,
    STUCK_RECOVERABLE_STATUSES,
    fail_submission,
)


logger = logging.getLogger(__name__)


# 단계별 timeout. SQS visibility timeout · worker normal duration 보다 충분히 큼.
# 운영 incidents 에서 자동 복구가 멈춰서는 안 되므로 보수적으로 잡는다.
RECOVERY_TIMEOUTS_MIN: dict[str, int] = {
    status: 30 for status in STUCK_RECOVERABLE_STATUSES
}


@dataclass(frozen=True)
class StuckSubmissionAlert:
    """timeout 넘은 sub 의 식별 정보 (운영 알람·로그용)."""

    submission_id: int
    status: str
    age_min: float
    tenant_id: int
    target_type: str
    target_id: int
    updated_at: datetime


@dataclass
class RecoveryReport:
    """recover_stuck_submissions() 의 단일 호출 결과."""

    detected: list[StuckSubmissionAlert] = field(default_factory=list)
    recovered: list[int] = field(default_factory=list)
    failed_transitions: list[tuple[int, str]] = field(default_factory=list)
    skipped: list[int] = field(default_factory=list)


def detect_stuck_submissions(
    *,
    timeouts: dict[str, int] | None = None,
    source: str = Submission.Source.OMR_SCAN,
) -> list[StuckSubmissionAlert]:
    """
    timeout 넘은 OMR submission 들 목록 (state 변경 X).

    Args:
        timeouts: status → 분 단위 timeout. None 이면 RECOVERY_TIMEOUTS_MIN 사용.
        source: 필터링 source. 기본은 omr_scan.
    """
    now = timezone.now()
    timeouts = timeouts if timeouts is not None else RECOVERY_TIMEOUTS_MIN
    out: list[StuckSubmissionAlert] = []
    for status, minutes in timeouts.items():
        cutoff = now - timedelta(minutes=int(minutes))
        qs = (
            Submission.objects.filter(
                status=status,
                source=source,
                updated_at__lt=cutoff,
            )
            .only("id", "status", "updated_at", "tenant_id", "target_type", "target_id")
            .order_by("updated_at")
        )
        for s in qs:
            age = (now - s.updated_at).total_seconds() / 60.0
            out.append(
                StuckSubmissionAlert(
                    submission_id=int(s.id),
                    status=str(s.status),
                    age_min=round(age, 1),
                    tenant_id=int(s.tenant_id or 0),
                    target_type=str(s.target_type),
                    target_id=int(s.target_id or 0),
                    updated_at=s.updated_at,
                )
            )
    return out


def recover_stuck_submissions(
    *,
    actor: str = "state_recovery",
    dry_run: bool = False,
    timeouts: dict[str, int] | None = None,
    source: str = Submission.Source.OMR_SCAN,
) -> RecoveryReport:
    """
    감지된 stuck sub 를 FAILED 로 전환하고 meta.state_recovery audit 기록.

    재진입 안전: race 로 같은 sub 가 이미 FAILED / DONE / SUPERSEDED 로 가면
    skip. 자동 복구가 학원장 운영 데이터를 덮어쓰지 않는다.
    """
    detected = detect_stuck_submissions(timeouts=timeouts, source=source)
    report = RecoveryReport(detected=detected)
    if not detected:
        return report

    if dry_run:
        for alert in detected:
            logger.warning(
                "OMR_STATE_RECOVERY_DRYRUN | sub=%s | status=%s | age_min=%s",
                alert.submission_id, alert.status, alert.age_min,
            )
        return report

    recovery_now = timezone.now()
    now_iso = recovery_now.isoformat()
    active_timeouts = timeouts if timeouts is not None else RECOVERY_TIMEOUTS_MIN

    for alert in detected:
        timeout_minutes = active_timeouts.get(alert.status)
        if timeout_minutes is None:
            report.skipped.append(alert.submission_id)
            continue
        cutoff = recovery_now - timedelta(minutes=int(timeout_minutes))
        try:
            with transaction.atomic():
                try:
                    sub = Submission.objects.select_for_update().get(
                        id=alert.submission_id,
                        status=alert.status,
                        source=source,
                        updated_at=alert.updated_at,
                        updated_at__lt=cutoff,
                    )
                except Submission.DoesNotExist:
                    report.skipped.append(alert.submission_id)
                    continue
                try:
                    fail_submission(
                        sub,
                        error_message=f"stuck:{alert.status}_timeout",
                        actor=actor,
                    )
                except InvalidTransitionError as exc:
                    report.failed_transitions.append((alert.submission_id, str(exc)))
                    continue
                meta = dict(sub.meta or {})
                meta["state_recovery"] = {
                    "at": now_iso,
                    "from_status": alert.status,
                    "age_min": alert.age_min,
                    "reason": f"{alert.status}_timeout",
                    "actor": actor,
                }
                sub.meta = meta
                sub.save(update_fields=["meta", "updated_at"])
                report.recovered.append(alert.submission_id)
                logger.error(
                    "OMR_STATE_RECOVERY | sub=%s | from=%s | age_min=%s | tenant=%s",
                    alert.submission_id, alert.status, alert.age_min, alert.tenant_id,
                )
        except Exception:
            logger.exception(
                "OMR_STATE_RECOVERY_FAILED | sub=%s", alert.submission_id
            )
            report.failed_transitions.append((alert.submission_id, "exception"))

    return report
