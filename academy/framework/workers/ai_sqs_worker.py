"""
AI SQS Worker — Hexagonal 프레임워크 계층 (thin)

- Use Case + Adapter만 호출.
- SQS 수신 → prepare_ai_job → visibility 연장 → inference → complete/fail → delete.
- 기존 academy.application.use_cases.ai.pipelines.dispatcher.handle_ai_job 사용 (inference).
"""
from __future__ import annotations

import logging
import os
import random
import signal
import sys
import threading
import time
import uuid
from datetime import timedelta
from typing import Any, Callable, Optional

from django.db import close_old_connections, connections

from academy.adapters.db.django.uow import DjangoUnitOfWork
from academy.adapters.queue.sqs.ai_queue import SQSAIQueueAdapter
from academy.adapters.queue.sqs.visibility_extender import SQSVisibilityExtender
from academy.adapters.compute.ec2_control import scale_down_ai_worker_asg_to_baseline_if_idle
from libs.queue import QueueUnavailableError
from academy.application.use_cases.ai.process_ai_job_from_sqs import (
    prepare_ai_job,
    complete_ai_job,
    fail_ai_job,
    PreparedJob,
)

logger = logging.getLogger("academy.ai_sqs_worker")

_TERMINAL_AI_JOB_STATUSES = {
    "DONE",
    "FAILED",
    "REJECTED_BAD_INPUT",
    "FALLBACK_TO_GPU",
    "REVIEW_REQUIRED",
}

# 상수 (기존 sqs_main_cpu와 동일)
SQS_WAIT_TIME_SECONDS = 20
SQS_VISIBILITY_TIMEOUT = int(os.getenv("AI_SQS_VISIBILITY_TIMEOUT", "3600"))  # 1시간 연장
VISIBILITY_EXTEND_INTERVAL = int(os.getenv("AI_VISIBILITY_EXTEND_INTERVAL", "60"))  # 60초마다 연장
BASIC_POLL_WEIGHT = int(os.getenv("AI_WORKER_BASIC_POLL_WEIGHT", "3"))
LITE_POLL_WEIGHT = int(os.getenv("AI_WORKER_LITE_POLL_WEIGHT", "1"))
MIN_JOB_INTERVAL_SECONDS = float(os.getenv("AI_WORKER_MIN_JOB_INTERVAL_SECONDS", "1.0"))
IDLE_SCALE_IN_ENABLED = os.getenv("AI_WORKER_IDLE_SCALE_IN_ENABLED", "1").strip().lower() not in (
    "0",
    "false",
    "no",
)
IDLE_EMPTY_POLLS_BEFORE_SCALE_IN = int(
    os.getenv("AI_WORKER_IDLE_EMPTY_POLLS_BEFORE_SCALE_IN", "15")
)
IDLE_SCALE_IN_CONFIRM_SECONDS = float(
    os.getenv("AI_WORKER_IDLE_SCALE_IN_CONFIRM_SECONDS", "30")
)
IDLE_SCALE_IN_PENDING_JOB_WINDOW_SECONDS = int(
    os.getenv("AI_WORKER_IDLE_SCALE_IN_PENDING_JOB_WINDOW_SECONDS", "21600")
)
IDLE_SCALE_IN_COUNT_TIERS = tuple(
    tier.strip().lower()
    for tier in os.getenv("AI_WORKER_IDLE_SCALE_IN_COUNT_TIERS", "basic,lite,premium").split(",")
    if tier.strip()
) or ("basic", "lite", "premium")
# Lease 전략: (1) 고정. visibility 3600 - safety_margin 60 = 3540. 문서 §8.3.
LEASE_SECONDS = int(os.getenv("AI_JOB_LEASE_SECONDS", "3540"))
# inference 최대 60분. 초과 시 fail_ai_job + delete + extender stop. 문서 §8.3.
INFERENCE_MAX_SECONDS = int(os.getenv("AI_INFERENCE_MAX_SECONDS", "3600"))

_shutdown = False
_current_receipt_handle: Optional[str] = None


def _release_db_connections() -> None:
    """SQS worker는 요청/응답 수명주기가 없으므로 작업 경계에서 DB 세션을 명시 반납한다."""
    try:
        connections.close_all()
    except Exception:
        logger.warning("Failed to close DB connections", exc_info=True)


def _handle_signal(sig, frame) -> None:
    global _shutdown, _current_receipt_handle
    logger.info(
        "Received signal, graceful shutdown | current_job=%s",
        "processing" if _current_receipt_handle else "idle",
    )
    _shutdown = True


def _weighted_poll(queue: SQSAIQueueAdapter, *, tools_only: bool = False) -> tuple[Optional[dict], str]:
    if tools_only:
        tier = "tools"
        msg = queue.receive(tier=tier, wait_time_seconds=SQS_WAIT_TIME_SECONDS)
        return msg, tier

    if os.environ.get("AI_WORKER_PREMIUM_ONLY") == "1":
        tier = "premium"
    else:
        total = BASIC_POLL_WEIGHT + LITE_POLL_WEIGHT
        tier = "basic" if random.randint(1, total) <= BASIC_POLL_WEIGHT else "lite"
    msg = queue.receive(tier=tier, wait_time_seconds=SQS_WAIT_TIME_SECONDS)
    return msg, tier


def _queue_counts_are_idle(counts: dict[str, int]) -> bool:
    return (
        int(counts.get("visible") or 0) == 0
        and int(counts.get("not_visible") or 0) == 0
        and int(counts.get("delayed") or 0) == 0
    )


def _scale_in_count_tiers(current_tier: str) -> tuple[str, ...]:
    tiers = list(IDLE_SCALE_IN_COUNT_TIERS)
    current = (current_tier or "").strip().lower()
    if current and current not in tiers:
        tiers.insert(0, current)
    return tuple(tiers)


def _combined_queue_counts_for_scale_in(
    queue: SQSAIQueueAdapter,
    *,
    current_tier: str,
) -> dict[str, int]:
    combined = {"visible": 0, "not_visible": 0, "delayed": 0}
    for tier_name in _scale_in_count_tiers(current_tier):
        counts = queue.get_counts(tier=tier_name)
        for key in combined:
            combined[key] = max(combined[key], int(counts.get(key) or 0))
    return combined


def _active_running_ai_jobs_exist() -> bool:
    try:
        from django.db.models import Q
        from django.utils import timezone
        from apps.domains.ai.models import AIJobModel

        now = timezone.now()
        pending_since = now - timedelta(
            seconds=max(60, IDLE_SCALE_IN_PENDING_JOB_WINDOW_SECONDS)
        )
        return (
            AIJobModel.objects.filter(status="RUNNING")
            .filter(Q(lease_expires_at__isnull=True) | Q(lease_expires_at__gt=now))
            .exists()
            or AIJobModel.objects.filter(
                status__in=("PENDING", "RETRYING"),
                updated_at__gte=pending_since,
            ).exists()
        )
    except Exception:
        logger.warning("AI_IDLE_SCALE_IN_RUNNING_JOB_CHECK_FAILED", exc_info=True)
        return True


def _try_idle_scale_in(queue: SQSAIQueueAdapter, tier: str) -> bool:
    try:
        counts = _combined_queue_counts_for_scale_in(queue, current_tier=tier)
    except QueueUnavailableError:
        logger.warning("AI_IDLE_SCALE_IN_QUEUE_HEALTH_UNAVAILABLE", exc_info=True)
        return False
    if not _queue_counts_are_idle(counts):
        logger.info("AI_IDLE_SCALE_IN_SKIP | counts=%s", counts)
        return False
    if _active_running_ai_jobs_exist():
        logger.info("AI_IDLE_SCALE_IN_SKIP | active_running_job=true")
        return False
    if IDLE_SCALE_IN_CONFIRM_SECONDS > 0:
        time.sleep(IDLE_SCALE_IN_CONFIRM_SECONDS)
    if _shutdown:
        return False
    try:
        confirmed_counts = _combined_queue_counts_for_scale_in(queue, current_tier=tier)
    except QueueUnavailableError:
        logger.warning(
            "AI_IDLE_SCALE_IN_CONFIRM_QUEUE_HEALTH_UNAVAILABLE",
            exc_info=True,
        )
        return False
    if not _queue_counts_are_idle(confirmed_counts):
        logger.info(
            "AI_IDLE_SCALE_IN_SKIP_AFTER_CONFIRM | before=%s | after=%s",
            counts,
            confirmed_counts,
        )
        return False
    if _active_running_ai_jobs_exist():
        logger.info("AI_IDLE_SCALE_IN_SKIP_AFTER_CONFIRM | active_running_job=true")
        return False
    return scale_down_ai_worker_asg_to_baseline_if_idle(confirmed_counts)



def _dispatch_domain_callback(
    prepared: PreparedJob,
    *,
    status: str,
    result_payload: dict | None,
    error: str | None,
) -> bool:
    """
    AI Job 완료 후 도메인 콜백 디스패치.
    도메인 반영이 실패하면 SQS message를 삭제하지 않고 재배달로 회복시킨다.
    """
    try:
        from apps.domains.ai.callbacks import dispatch_ai_result_to_domain
        handled = dispatch_ai_result_to_domain(
            job_id=prepared.job_id,
            status=status,
            result_payload=result_payload,
            error=error,
            source_domain=prepared.source_domain,
            source_id=prepared.source_id,
            tier=prepared.tier,
        )
        if handled is False:
            logger.error(
                "Domain callback returned failure; keeping SQS message for retry: job_id=%s source=%s/%s",
                prepared.job_id, prepared.source_domain, prepared.source_id,
            )
            return False
        return True
    except Exception:
        logger.exception(
            "Domain callback failed; keeping SQS message for retry: job_id=%s source=%s/%s",
            prepared.job_id, prepared.source_domain, prepared.source_id,
        )
        return False


def _dispatch_terminal_callback_from_message(job_id: str, message: dict, tier_from_msg: str) -> bool:
    """Terminal AIJob 재배달 시 추론은 건너뛰고 저장된 결과로 도메인 callback만 재시도한다."""
    try:
        from apps.domains.ai.models import AIJobModel, AIResultModel

        job = AIJobModel.objects.filter(job_id=job_id).first()
        if not job or job.status not in _TERMINAL_AI_JOB_STATUSES:
            return True

        source_domain = job.source_domain or message.get("source_domain")
        source_id = job.source_id or message.get("source_id")
        if not source_domain or not source_id:
            return True

        result_row = AIResultModel.objects.filter(job=job).first()
        result_payload = result_row.payload if result_row and isinstance(result_row.payload, dict) else {}
        error = job.error_message or job.last_error or None
        callback_status = "FAILED" if error else job.status
        prepared = PreparedJob(
            job_id=job.job_id,
            job_type=job.job_type,
            tier=job.tier or tier_from_msg,
            payload=job.payload or {},
            receipt_handle=message.get("receipt_handle") or "",
            tenant_id=str(job.tenant_id) if job.tenant_id is not None else message.get("tenant_id"),
            source_domain=source_domain,
            source_id=source_id,
        )
        return _dispatch_domain_callback(
            prepared,
            status=callback_status,
            result_payload=result_payload,
            error=error,
        )
    except Exception:
        logger.exception("Terminal domain callback retry failed before dispatch: job_id=%s", job_id)
        return False


def _cleanup_terminal_artifacts(prepared: PreparedJob) -> None:
    """Best-effort cleanup for artifacts no longer needed after DB terminal state."""
    job_type = (prepared.job_type or "").strip().lower()
    if job_type not in {
        "problem_studio_transfer",
        "problem_studio_transcription",
        "problem_review_analysis",
        "teacher_problem_explanation",
    }:
        return
    payload = prepared.payload if isinstance(prepared.payload, dict) else {}
    if job_type == "teacher_problem_explanation":
        source_key = str(payload.get("source_image_key") or "").strip()
        expected_prefix = f"tenants/{prepared.tenant_id}/tools/problem-solver/tmp/"
    elif job_type == "problem_review_analysis":
        source_key = str(payload.get("source_archive_key") or "").strip()
        report_id = str(payload.get("report_id") or "").strip()
        expected_prefix = f"tenants/{prepared.tenant_id}/tools/problem-review/tmp/{report_id}/"
    else:
        source_key = str(payload.get("source_archive_key") or "").strip()
        expected_prefix = f"tenants/{prepared.tenant_id}/tools/problem-studio/tmp/"
    if not source_key or not source_key.startswith(expected_prefix):
        return
    try:
        from apps.infrastructure.storage.r2 import delete_object_r2_storage

        delete_object_r2_storage(key=source_key)
        logger.info(
            "AI_JOB_TERMINAL_ARTIFACT_CLEANUP_DONE | job_id=%s | key=%s",
            prepared.job_id,
            source_key,
        )
    except Exception:
        logger.warning(
            "AI_JOB_TERMINAL_ARTIFACT_CLEANUP_FAILED | job_id=%s | key=%s",
            prepared.job_id,
            source_key,
            exc_info=True,
        )


InferenceHandler = Callable[[Any], Any]


def _to_contract_job(prepared: PreparedJob):
    """PreparedJob을 pipeline handler가 받는 AIJob contract로 변환한다."""
    from apps.shared.contracts.ai_job import AIJob

    job_dict = {
        "id": prepared.job_id,
        "type": prepared.job_type or "ocr",
        "tenant_id": prepared.tenant_id,
        "source_domain": prepared.source_domain,
        "source_id": prepared.source_id,
        "payload": prepared.payload,
        "created_at": "",
    }
    return AIJob.from_dict(job_dict)


def _run_inference(prepared: PreparedJob, inference_handler: InferenceHandler | None = None):
    """기존 handle_ai_job 호출 (contract 변환). Tools worker는 handler를 직접 주입한다."""
    from apps.shared.contracts.ai_result import AIResult
    from academy.application.use_cases.ai.pipelines.tier_enforcer import enforce_tier_limits

    allowed, error_msg = enforce_tier_limits(
        tier=prepared.tier,
        job_type=prepared.job_type,
    )
    if not allowed:
        return AIResult.failed(prepared.job_id, error_msg or "tier_limit")

    job = _to_contract_job(prepared)
    if inference_handler:
        return inference_handler(job)

    from academy.application.use_cases.ai.pipelines.dispatcher import handle_ai_job
    return handle_ai_job(job)


def run_ai_sqs_worker(
    *,
    queue: SQSAIQueueAdapter | None = None,
    worker_kind: str = "ai",
    supported_job_types: set[str] | frozenset[str] | None = None,
    inference_handler: InferenceHandler | None = None,
) -> int:
    """메인 루프. 0 정상 종료, 1 오류."""
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    queue = queue or SQSAIQueueAdapter()
    extender = SQSVisibilityExtender(queue)
    uow_factory = DjangoUnitOfWork
    worker_kind = (worker_kind or "ai").strip().lower()
    supported_job_types = (
        {job_type.strip().lower() for job_type in supported_job_types}
        if supported_job_types
        else None
    )

    consecutive_errors = 0
    max_consecutive_errors = 10
    last_job_finished_at = 0.0
    empty_polls = 0

    try:
        while not _shutdown:
            try:
                # 워커 루프 경계에서 stale DB 커넥션 정리 (누수/반납 지연 보호)
                close_old_connections()
                # Heartbeat — worker family별 이름으로 기록. 실패는 silent.
                try:
                    import os as _os
                    from apps.shared.utils.heartbeat import beat as _beat
                    if worker_kind == "tools":
                        _beat("tools")
                    else:
                        _wt = (_os.getenv("WORKER_TYPE", "CPU") or "CPU").lower()
                        _beat(f"ai_{_wt}")
                except Exception:
                    pass
                try:
                    message, tier = _weighted_poll(queue, tools_only=(worker_kind == "tools"))
                except QueueUnavailableError:
                    logger.warning("SQS unavailable, waiting 60s")
                    time.sleep(60)
                    continue

                if not message:
                    consecutive_errors = 0
                    if worker_kind != "tools" and IDLE_SCALE_IN_ENABLED:
                        empty_polls += 1
                        if empty_polls >= IDLE_EMPTY_POLLS_BEFORE_SCALE_IN:
                            if _try_idle_scale_in(queue, tier):
                                logger.info(
                                    "AI_IDLE_SCALE_IN_REQUESTED | empty_polls=%d",
                                    empty_polls,
                                )
                                return 0
                            empty_polls = 0
                    continue
                empty_polls = 0

                # 워커 처리율 상한(초당 1/N)으로 DB 급격한 burst 완화
                if MIN_JOB_INTERVAL_SECONDS > 0 and last_job_finished_at > 0:
                    elapsed = time.time() - last_job_finished_at
                    if elapsed < MIN_JOB_INTERVAL_SECONDS:
                        time.sleep(MIN_JOB_INTERVAL_SECONDS - elapsed)

                receipt_handle = message.get("receipt_handle")
                job_id = message.get("job_id")
                job_type = message.get("job_type") or message.get("type") or message.get("task") or ""
                tier_from_msg = message.get("tier", tier)
                payload = message.get("payload", {})

                if not receipt_handle or not job_id or not job_type:
                    logger.error("Invalid message: job_id=%s receipt_handle=%s", job_id, bool(receipt_handle))
                    if job_id and receipt_handle:
                        queue.delete(receipt_handle, tier_from_msg)
                    continue

                job_type_normalized = job_type.strip().lower()
                if supported_job_types is not None and job_type_normalized not in supported_job_types:
                    logger.error(
                        "UNSUPPORTED_JOB_FOR_WORKER | worker=%s | job_id=%s | job_type=%s",
                        worker_kind,
                        job_id,
                        job_type,
                    )
                    fail_ai_job(
                        uow_factory(),
                        job_id,
                        f"unsupported_job_type_for_{worker_kind}_worker:{job_type}",
                        tier_from_msg,
                    )
                    queue.delete(receipt_handle, tier_from_msg)
                    continue

                request_id = uuid.uuid4().hex[:8]
                logger.info(
                    "SQS_MESSAGE_RECEIVED | worker=%s | request_id=%s | job_id=%s | tier=%s",
                    worker_kind, request_id, job_id, tier_from_msg,
                )

                global _current_receipt_handle
                _current_receipt_handle = receipt_handle

                # 1) Use Case: prepare (RUNNING 전이)
                prepared = prepare_ai_job(
                    uow_factory(),
                    job_id=job_id,
                    receipt_handle=receipt_handle,
                    tier=tier_from_msg,
                    payload=payload,
                    job_type=job_type,
                    tenant_id=message.get("tenant_id"),
                    source_domain=message.get("source_domain"),
                    source_id=message.get("source_id"),
                    worker_id=f"{worker_kind}-sqs-worker",
                    lease_seconds=LEASE_SECONDS,
                )

                if prepared is None:
                    # 이미 완료/실패된 job의 재배달은 저장된 결과로 domain callback만 재시도한다.
                    # callback 성공 또는 callback 대상 없음일 때만 SQS message를 삭제한다.
                    message_deleted = False
                    callback_ok = _dispatch_terminal_callback_from_message(job_id, message, tier_from_msg)
                    if callback_ok:
                        message_deleted = queue.delete(receipt_handle, tier_from_msg)
                        if not message_deleted:
                            logger.error(
                                "AI_JOB_IDEMPOTENT_DELETE_FAILED | job_id=%s",
                                job_id,
                            )
                    if not callback_ok or not message_deleted:
                        consecutive_errors += 1
                    logger.info(
                        "AI_JOB_IDEMPOTENT_SKIP | job_id=%s callback_ok=%s message_deleted=%s",
                        job_id,
                        str(callback_ok).lower(),
                        str(message_deleted).lower(),
                    )
                    _current_receipt_handle = None
                    if _shutdown:
                        break
                    continue

                # 2) Visibility 연장 시작 (장시간 작업 대비)
                extender.start(
                    receipt_handle=receipt_handle,
                    tier=tier_from_msg,
                    interval_seconds=VISIBILITY_EXTEND_INTERVAL,
                    visibility_timeout_seconds=SQS_VISIBILITY_TIMEOUT,
                )

                result_container: list = []

                def _run_inference_safe() -> None:
                    try:
                        result_container.append(
                            _run_inference(prepared, inference_handler=inference_handler)
                        )
                    except Exception as e:
                        from apps.shared.contracts.ai_result import AIResult
                        result_container.append(AIResult.failed(prepared.job_id, str(e)))
                    finally:
                        # Django DB connections are thread-local. Inference runs in its own
                        # thread, so close from inside that thread too.
                        _release_db_connections()

                try:
                    # 3) Inference (60분 상한. 문서 §8.3)
                    inference_thread = threading.Thread(target=_run_inference_safe, daemon=True)
                    inference_thread.start()
                    inference_thread.join(timeout=INFERENCE_MAX_SECONDS)

                    if inference_thread.is_alive():
                        # Daemon thread는 정지 불가 — 그대로 두면 zombie thread가
                        # CPU/메모리를 누적 점유해 다음 잡 처리 중 wedge(2026-04-29 사고).
                        # 사후 처리(fail_ai_job, callback, SQS delete) 후 프로세스를
                        # 강제 종료해 docker `--restart unless-stopped`로 재기동한다.
                        logger.error(
                            "SQS_JOB_TIMEOUT_60MIN | request_id=%s | job_id=%s | hard exit after cleanup",
                            request_id, job_id,
                        )
                        ok = fail_ai_job(uow_factory(), job_id, "inference_timeout_60min", tier_from_msg)
                        if not ok:
                            logger.error(
                                "AI_JOB_STATE_TRANSITION_FAILED | step=fail_timeout | job_id=%s | "
                                "DB still RUNNING — manual cleanup required", job_id,
                            )
                        callback_ok = False
                        if ok:
                            _cleanup_terminal_artifacts(prepared)
                            callback_ok = _dispatch_domain_callback(
                                prepared, status="FAILED", result_payload=None,
                                error="inference_timeout_60min",
                            )
                        try:
                            if ok and callback_ok and not queue.delete(receipt_handle, tier_from_msg):
                                logger.error(
                                    "AI_JOB_SQS_DELETE_FAILED | job_id=%s | timeout path", job_id,
                                )
                        except Exception:
                            logger.exception("SQS delete during timeout failed (non-fatal)")
                        extender.stop()
                        _release_db_connections()
                        # zombie daemon thread 누적 방지 — 컨테이너 재기동(docker restart=unless-stopped)
                        os._exit(2)

                    # 상태 전이 반환값을 명시적으로 검증 — False면 DB 갱신 실패로
                    # SQS message는 삭제되지만 DB는 RUNNING으로 남아 운영 알람 대상.
                    result = result_container[0] if result_container else None
                    if result is None:
                        ok = fail_ai_job(uow_factory(), job_id, "inference_error_no_result", tier_from_msg)
                        if not ok:
                            logger.error(
                                "AI_JOB_STATE_TRANSITION_FAILED | step=fail | job_id=%s | "
                                "DB still RUNNING — manual cleanup required", job_id,
                            )
                            consecutive_errors += 1
                            continue
                        _cleanup_terminal_artifacts(prepared)
                        callback_ok = _dispatch_domain_callback(
                            prepared, status="FAILED", result_payload=None,
                            error="inference_error_no_result",
                        )
                        if not callback_ok:
                            logger.error(
                                "AI_JOB_DOMAIN_CALLBACK_RETRY_DEFERRED | job_id=%s | status=FAILED",
                                job_id,
                            )
                            consecutive_errors += 1
                            continue
                        if not queue.delete(receipt_handle, tier_from_msg):
                            logger.error(
                                "AI_JOB_SQS_DELETE_FAILED | job_id=%s | "
                                "message will redeliver — idempotency relies on prepare_ai_job",
                                job_id,
                            )
                        consecutive_errors += 1
                    elif result.status == "DONE":
                        ok = complete_ai_job(uow_factory(), job_id, result.result)
                        if not ok:
                            logger.error(
                                "AI_JOB_STATE_TRANSITION_FAILED | step=complete | job_id=%s | "
                                "DB still RUNNING — manual cleanup required", job_id,
                            )
                            consecutive_errors += 1
                            continue
                        _cleanup_terminal_artifacts(prepared)
                        callback_ok = _dispatch_domain_callback(
                            prepared, status="DONE",
                            result_payload=result.result if isinstance(result.result, dict) else {},
                            error=None,
                        )
                        if not callback_ok:
                            logger.error(
                                "AI_JOB_DOMAIN_CALLBACK_RETRY_DEFERRED | job_id=%s | status=DONE",
                                job_id,
                            )
                            consecutive_errors += 1
                            continue
                        if not queue.delete(receipt_handle, tier_from_msg):
                            logger.error(
                                "AI_JOB_SQS_DELETE_FAILED | job_id=%s | "
                                "completed in DB but SQS delete failed — message will redeliver "
                                "(prepare_ai_job idempotency will skip)", job_id,
                            )
                        logger.info("SQS_JOB_COMPLETED | request_id=%s | job_id=%s", request_id, job_id)
                        consecutive_errors = 0
                    else:
                        ok = fail_ai_job(uow_factory(), job_id, result.error or "failed", tier_from_msg)
                        if not ok:
                            logger.error(
                                "AI_JOB_STATE_TRANSITION_FAILED | step=fail | job_id=%s | "
                                "DB still RUNNING — manual cleanup required", job_id,
                            )
                            consecutive_errors += 1
                            continue
                        _cleanup_terminal_artifacts(prepared)
                        callback_ok = _dispatch_domain_callback(
                            prepared, status="FAILED", result_payload=None,
                            error=result.error or "failed",
                        )
                        if not callback_ok:
                            logger.error(
                                "AI_JOB_DOMAIN_CALLBACK_RETRY_DEFERRED | job_id=%s | status=FAILED",
                                job_id,
                            )
                            consecutive_errors += 1
                            continue
                        # DB 종단(FAILED) 전이 후 SQS message 삭제 — 재배달돼도 mark_running이
                        # idempotent skip 처리하므로 재시도 무의미 + DLQ 도달 방지.
                        if not queue.delete(receipt_handle, tier_from_msg):
                            logger.error(
                                "AI_JOB_SQS_DELETE_FAILED | job_id=%s | failed path", job_id,
                            )
                        logger.warning("SQS_JOB_FAILED | request_id=%s | job_id=%s | error=%s", request_id, job_id, result.error)
                        consecutive_errors += 1
                finally:
                    extender.stop()
                    _current_receipt_handle = None
                    last_job_finished_at = time.time()

                if consecutive_errors >= max_consecutive_errors:
                    logger.error("Too many consecutive errors (%s), exit", consecutive_errors)
                    return 1
                if _shutdown:
                    break

            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.exception("Unexpected error: %s", e)
                consecutive_errors += 1
                if consecutive_errors >= max_consecutive_errors:
                    return 1
                time.sleep(5)
            finally:
                # 작업 1건 처리 종료 후 커넥션을 RDS에 즉시 반납한다.
                # close_old_connections()는 정상 persistent connection을 유지할 수 있어
                # 대량 OCR/매치업 배치에서 connection slot 고갈을 막기엔 부족하다.
                _release_db_connections()

        return 0
    finally:
        extender.stop()
        _release_db_connections()


if __name__ == "__main__":
    if os.environ.get("DJANGO_SETTINGS_MODULE"):
        import django
        django.setup()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] [AI-SQS-WORKER] %(message)s",
    )
    sys.exit(run_ai_sqs_worker())
