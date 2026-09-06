"""
AI Job Repository — Django ORM 구현 (메서드 내부에서만 apps.domains.ai import)
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from academy.domain.ai.entities import AIJob, AIJobStatus  # noqa: F401


logger = logging.getLogger(__name__)


def _persistent_job_result(
    job_type: str,
    result_payload: Optional[dict],
    *,
    now: datetime,
) -> Optional[dict]:
    if job_type != "excel_parsing" or not isinstance(result_payload, dict):
        return result_payload
    from apps.domains.ai.services.excel_job_secrets import secure_excel_result

    return secure_excel_result(result_payload, now=now)


def _public_job_result(
    job_type: str,
    stored_payload: Optional[dict],
    *,
    include_excel_credentials: bool = False,
) -> Optional[dict]:
    if job_type != "excel_parsing" or not isinstance(stored_payload, dict):
        return stored_payload
    from apps.domains.ai.services.excel_job_secrets import public_excel_result

    return public_excel_result(
        stored_payload,
        include_credentials=include_excel_credentials,
    )


def cache_terminal_job_status(job, *, result_payload: Optional[dict] = None) -> None:
    """Publish the committed DB terminal state through the shared Redis facade."""
    try:
        from apps.domains.ai.redis_status_cache import cache_job_status

        cache_job_status(
            tenant_id=str(job.tenant_id or ""),
            job_id=job.job_id,
            status=job.status,
            job_type=job.job_type,
            error_message=job.error_message or None,
            result=_public_job_result(job.job_type, result_payload),
            ttl=None,
        )
    except Exception as e:
        logger.warning("Failed to cache terminal AI job in Redis: %s", e)


def _scrub_terminal_job_payload(job) -> bool:
    if job.job_type == "excel_parsing":
        from apps.domains.ai.services.excel_job_secrets import scrub_excel_job_payload

        scrubbed = scrub_excel_job_payload(job.payload or {})
    elif job.job_type == "problem_studio_package":
        from apps.domains.tools.problem_studio.services import scrub_problem_studio_job_payload

        scrubbed = scrub_problem_studio_job_payload(job.payload or {})
    elif job.job_type == "teacher_problem_explanation":
        scrubbed = dict(job.payload or {})
        scrubbed.pop("source_image_key", None)
    else:
        return False
    if scrubbed == (job.payload or {}):
        return False
    job.payload = scrubbed
    return True


def _model_to_entity(m) -> Optional[AIJob]:
    if m is None:
        return None
    return AIJob(
        job_id=m.job_id,
        job_type=m.job_type,
        status=AIJobStatus(m.status) if m.status else AIJobStatus.PENDING,
        payload=m.payload or {},
        tenant_id=m.tenant_id,
        source_domain=m.source_domain,
        source_id=m.source_id,
        tier=m.tier or "basic",
        attempt_count=int(m.attempt_count or 0),
        max_attempts=int(m.max_attempts or 5),
        locked_by=m.locked_by,
        locked_at=m.locked_at,
        lease_expires_at=m.lease_expires_at,
        idempotency_key=m.idempotency_key,
        error_message=(m.error_message or "")[:2000],
        updated_at=getattr(m, "updated_at", None),
    )


class DjangoAIJobRepository:
    """AIJobRepository 구현. ORM 접근은 모두 메서드 내부에서 lazy import."""

    def get_by_job_id(self, job_id: str) -> Optional[AIJob]:
        from apps.domains.ai.models import AIJobModel
        m = AIJobModel.objects.filter(job_id=job_id).first()
        return _model_to_entity(m)

    def get_for_update(self, job_id: str) -> Optional[AIJob]:
        """호출자가 이미 UoW 트랜잭션 내에 있어야 함 (select_for_update 락 유지)."""
        from apps.domains.ai.models import AIJobModel
        m = AIJobModel.objects.select_for_update().filter(job_id=job_id).first()
        return _model_to_entity(m)

    def save(self, job: AIJob) -> None:
        import logging
        logger = logging.getLogger(__name__)  # ✅ logger 정의
        
        from django.utils import timezone
        from apps.domains.ai.models import AIJobModel
        now = timezone.now()
        
        # DB 저장
        model, created = AIJobModel.objects.update_or_create(
            job_id=job.job_id,
            defaults={
                "job_type": job.job_type,
                "status": job.status.value,
                "payload": job.payload,
                "tenant_id": job.tenant_id,
                "source_domain": job.source_domain,
                "source_id": job.source_id,
                "tier": job.tier,
                "attempt_count": job.attempt_count,
                "max_attempts": job.max_attempts,
                "locked_by": job.locked_by,
                "locked_at": job.locked_at,
                "lease_expires_at": job.lease_expires_at,
                "idempotency_key": job.idempotency_key,
                "error_message": job.error_message,
                "updated_at": now,
            },
        )
        
        # ✅ 완료/실패 시 Redis에 상태 저장 (TTL 없음, result 포함)
        if job.status.value in ["DONE", "FAILED"]:
            try:
                from apps.domains.ai.redis_status_cache import cache_job_status
                
                # ✅ result 가져오기 (방어적 처리)
                result_payload = None
                if job.status.value == "DONE":
                    getter = getattr(self, "get_result_payload_for_job", None)
                    if callable(getter):
                        try:
                            result_payload = getter(model)
                        except Exception as e:
                            logger.debug("Failed to get result payload: %s", e)
                
                cache_job_status(
                    tenant_id=str(job.tenant_id),
                    job_id=job.job_id,
                    status=job.status.value,
                    job_type=job.job_type,
                    error_message=job.error_message,
                    result=result_payload,  # 완료 시 result 포함 (있으면)
                    ttl=None,  # TTL 없음
                )
            except Exception as e:
                logger.warning("Failed to cache job status in Redis: %s", e)
        
        # ✅ RUNNING 상태도 Redis에 저장 (TTL 6시간)
        # AI Job은 "RUNNING"이 실제 처리 중 상태임
        elif job.status.value == "RUNNING":
            try:
                from apps.domains.ai.redis_status_cache import cache_job_status
                cache_job_status(
                    tenant_id=str(job.tenant_id),
                    job_id=job.job_id,
                    status=job.status.value,
                    job_type=job.job_type,
                    ttl=21600,  # 6시간
                )
            except Exception as e:
                logger.warning("Failed to cache job status in Redis: %s", e)

    def mark_running(
        self,
        job_id: str,
        worker_id: str,
        lease_expires_at: datetime,
        now: datetime,
    ) -> bool:
        import logging, os
        from django.db import connection
        _log = logging.getLogger("ai.mark_running")
        _log.info(
            "MARK_RUNNING_ENTER | job_id=%s worker=%s pid=%s db_alias=%s autocommit=%s in_atomic=%s",
            job_id, worker_id, os.getpid(),
            connection.alias, connection.get_autocommit(), connection.in_atomic_block,
        )
        from apps.domains.ai.models import AIJobModel
        try:
            job = AIJobModel.objects.select_for_update().filter(job_id=job_id).first()
        except Exception as exc:
            _log.exception("MARK_RUNNING_EXCEPTION | job_id=%s error=%s", job_id, exc)
            return False
        if not job:
            _log.warning("MARK_RUNNING_NOT_FOUND | job_id=%s", job_id)
            return False
        if job.status == "RUNNING":
            # RUNNING 재진입 — 다른 워커가 이미 점유 중이면 중복 처리 방지를 위해 거절.
            # 같은 worker_id라도 lease 만료 전 재진입은 SQS 재배달/중복 수신으로 보고 거절한다.
            # lease 만료 시에만 takeover 허용.
            same_worker = job.locked_by and str(job.locked_by) == str(worker_id)
            lease_expired = job.lease_expires_at is not None and job.lease_expires_at <= now
            if lease_expired:
                _log.warning(
                    "MARK_RUNNING_LEASE_EXPIRED_TAKEOVER | job_id=%s prev_worker=%s new_worker=%s",
                    job_id, job.locked_by, worker_id,
                )
                # fall through to takeover (status 재설정)
            elif same_worker:
                _log.warning("MARK_RUNNING_DUPLICATE_SAME_WORKER_REJECTED | job_id=%s worker=%s", job_id, worker_id)
                return False
            else:
                _log.warning(
                    "MARK_RUNNING_DUPLICATE_REJECTED | job_id=%s prev_worker=%s new_worker=%s",
                    job_id, job.locked_by, worker_id,
                )
                return False
        elif job.status not in ("PENDING", "RETRYING"):
            _log.warning("MARK_RUNNING_BAD_STATUS | job_id=%s status=%r", job_id, job.status)
            return False
        job.status = "RUNNING"
        job.locked_by = str(worker_id)
        job.locked_at = now
        if job.started_at is None:
            job.started_at = now
        job.lease_expires_at = lease_expires_at
        job.last_heartbeat_at = now
        job.save(update_fields=["status", "locked_by", "locked_at", "started_at", "lease_expires_at", "last_heartbeat_at", "updated_at"])
        # ✅ Redis에 RUNNING 기록 (진행 상황 위젯에서 진행률 표시용)
        try:
            from apps.domains.ai.redis_status_cache import cache_job_status
            cache_job_status(
                tenant_id=str(job.tenant_id or ""),
                job_id=job.job_id,
                status="RUNNING",
                job_type=job.job_type,
                ttl=21600,
            )
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Failed to cache RUNNING in Redis: %s", e)
        return True

    def mark_done(self, job_id: str, now: datetime, result_payload: Optional[dict] = None) -> bool:
        from django.db import transaction
        from apps.domains.ai.models import AIJobModel, AIResultModel
        with transaction.atomic():
            job = AIJobModel.objects.filter(job_id=job_id).first()
            if not job:
                return False
            if job.status == "DONE":
                return self._recover_done_job(
                    job,
                    now=now,
                    result_payload=result_payload,
                )
            if job.status != "RUNNING":
                logger.warning(
                    "AI_JOB_TERMINAL_TRANSITION_REJECTED | job_id=%s from=%s to=DONE",
                    job_id,
                    job.status,
                )
                return False

            persistent_result = _persistent_job_result(
                job.job_type,
                result_payload,
                now=now,
            )
            payload_scrubbed = _scrub_terminal_job_payload(job)
            updates = {
                "status": "DONE",
                "error_message": "",
                "last_error": "",
                "locked_by": None,
                "locked_at": None,
                "lease_expires_at": None,
                "completed_at": now,
                "updated_at": now,
            }
            if payload_scrubbed:
                updates["payload"] = job.payload
            updated = AIJobModel.objects.filter(
                pk=job.pk,
                status="RUNNING",
            ).update(**updates)
            if updated != 1:
                current = AIJobModel.objects.filter(pk=job.pk).first()
                if current and current.status == "DONE":
                    return self._recover_done_job(
                        current,
                        now=now,
                        result_payload=result_payload,
                    )
                return False

            job.status = "DONE"
            job.error_message = ""
            job.last_error = ""
            job.locked_by = None
            job.locked_at = None
            job.lease_expires_at = None
            job.completed_at = now
            stored_result = None
            if persistent_result is not None:
                result, _ = AIResultModel.objects.get_or_create(
                    job=job,
                    defaults={"payload": persistent_result},
                )
                stored_result = result.payload
            transaction.on_commit(
                lambda: cache_terminal_job_status(job, result_payload=stored_result)
            )
            return True

    def _recover_done_job(
        self,
        job,
        *,
        now: datetime,
        result_payload: Optional[dict],
    ) -> bool:
        """Repair an interrupted DONE write without accepting an opposite outcome."""
        from django.db import transaction
        from apps.domains.ai.models import AIJobModel, AIResultModel

        if job.error_message or job.last_error:
            return False
        persistent_result = _persistent_job_result(
            job.job_type,
            result_payload,
            now=now,
        )
        updates = {}
        if job.completed_at is None:
            updates["completed_at"] = now
        if _scrub_terminal_job_payload(job):
            updates["payload"] = job.payload
        if updates:
            updates["updated_at"] = now
            AIJobModel.objects.filter(pk=job.pk, status="DONE").update(**updates)
        stored_result = None
        if persistent_result is not None:
            result, _ = AIResultModel.objects.get_or_create(
                job=job,
                defaults={"payload": persistent_result},
            )
            stored_result = result.payload
        else:
            stored_result = AIResultModel.objects.filter(job=job).values_list(
                "payload",
                flat=True,
            ).first()
        transaction.on_commit(
            lambda: cache_terminal_job_status(job, result_payload=stored_result)
        )
        return True

    def mark_failed(
        self,
        job_id: str,
        error_message: str,
        tier: str,
        now: datetime,
    ) -> bool:
        from django.db import transaction
        from apps.domains.ai.models import AIJobModel
        from apps.domains.ai.services.status_resolver import status_for_exception
        with transaction.atomic():
            job = AIJobModel.objects.filter(job_id=job_id).first()
            if not job:
                return False
            final_str, _ = status_for_exception(
                tier or job.tier or "basic",
                job.job_type,
            )
            if job.status == final_str:
                return self._recover_failed_job(job, now=now)
            if job.status != "RUNNING":
                logger.warning(
                    "AI_JOB_TERMINAL_TRANSITION_REJECTED | job_id=%s from=%s to=%s",
                    job_id,
                    job.status,
                    final_str,
                )
                return False

            err = (error_message or "")[:2000]
            payload_scrubbed = _scrub_terminal_job_payload(job)
            updates = {
                "status": final_str,
                "error_message": err,
                "last_error": err,
                "locked_by": None,
                "locked_at": None,
                "lease_expires_at": None,
                "completed_at": now,
                "updated_at": now,
            }
            if payload_scrubbed:
                updates["payload"] = job.payload
            updated = AIJobModel.objects.filter(
                pk=job.pk,
                status="RUNNING",
            ).update(**updates)
            if updated != 1:
                current = AIJobModel.objects.filter(pk=job.pk).first()
                if current and current.status == final_str:
                    return self._recover_failed_job(current, now=now)
                return False

            job.status = final_str
            job.error_message = err
            job.last_error = err
            job.locked_by = None
            job.locked_at = None
            job.lease_expires_at = None
            job.completed_at = now
            transaction.on_commit(lambda: cache_terminal_job_status(job))
            return True

    def _recover_failed_job(self, job, *, now: datetime) -> bool:
        """Repair metadata/cache for the same failure outcome only."""
        from django.db import transaction
        from apps.domains.ai.models import AIJobModel

        # Basic/lite failures intentionally resolve to DONE with an error. A clean
        # DONE row belongs to a successful completion and is an opposite winner.
        if job.status == "DONE" and not (job.error_message or job.last_error):
            return False
        updates = {}
        if job.completed_at is None:
            updates["completed_at"] = now
        if _scrub_terminal_job_payload(job):
            updates["payload"] = job.payload
        if updates:
            updates["updated_at"] = now
            AIJobModel.objects.filter(pk=job.pk, status=job.status).update(**updates)
        transaction.on_commit(lambda: cache_terminal_job_status(job))
        return True

    def get_job_model_for_status(self, job_id: str, tenant_id: str, job_type: Optional[str] = None):
        """API 상태 조회용: tenant 일치 시 모델 인스턴스 반환 (adapters 내부에서만 .objects. 사용). job_type 지정 시 해당 타입만."""
        from apps.domains.ai.models import AIJobModel
        qs = AIJobModel.objects.filter(job_id=job_id, tenant_id=tenant_id)
        if job_type is not None:
            qs = qs.filter(job_type=job_type)
        return qs.first()

    def get_result_payload_for_job(
        self,
        job_model,
        *,
        include_excel_credentials: bool = False,
    ) -> Optional[dict]:
        """job(AIJobModel)에 대한 결과 payload 반환."""
        from apps.domains.ai.models import AIResultModel
        row = AIResultModel.objects.filter(job=job_model).first()
        if not row:
            return None
        return _public_job_result(
            job_model.job_type,
            row.payload,
            include_excel_credentials=include_excel_credentials,
        )


# ---------------------------------------------------------------------------
# 모듈 레벨 함수 (gateway, callbacks, publisher, internal_ai_job_view 등에서 사용)
# ---------------------------------------------------------------------------


def get_job_model_for_status(job_id: str, tenant_id: str, job_type: Optional[str] = None):
    """API 상태 조회용: tenant 일치 시 모델 인스턴스 반환. job_type 지정 시 해당 타입만."""
    from apps.domains.ai.models import AIJobModel
    qs = AIJobModel.objects.filter(job_id=job_id, tenant_id=tenant_id)
    if job_type is not None:
        qs = qs.filter(job_type=job_type)
    return qs.first()


def get_job_model_by_job_id(job_id: str):
    """job_id로 AIJobModel 조회 (tenant 무관, callbacks/publisher/internal용)."""
    from apps.domains.ai.models import AIJobModel
    return AIJobModel.objects.filter(job_id=str(job_id)).first()


def job_create(job_id: str, job_type: str, payload: dict, status: str = "PENDING", tier: str = "basic",
               tenant_id=None, source_domain=None, source_id=None, idempotency_key=None,
               force_rerun=False, rerun_reason=None, **extra):
    """AIJobModel 1건 생성. 반환: job_model."""
    from apps.domains.ai.models import AIJobModel
    return AIJobModel.objects.create(
        job_id=job_id,
        job_type=job_type,
        payload=payload,
        status=status,
        tier=tier,
        tenant_id=tenant_id,
        source_domain=source_domain,
        source_id=source_id,
        idempotency_key=idempotency_key,
        force_rerun=force_rerun,
        rerun_reason=rerun_reason or "",
        **extra,
    )


def job_get_by_idempotency_key(key: str):
    """idempotency_key로 AIJobModel 1건 조회."""
    from apps.domains.ai.models import AIJobModel
    return AIJobModel.objects.get(idempotency_key=key)


def job_save_failed(job_model, error_message: str, last_error: str) -> None:
    """job_model 상태를 FAILED로 저장 (publish 실패 시)."""
    from django.db import transaction
    from apps.domains.ai.models import AIJobModel

    _TERMINAL = {"DONE", "FAILED", "REJECTED_BAD_INPUT"}
    with transaction.atomic():
        locked = AIJobModel.objects.select_for_update().filter(pk=job_model.pk).first()
        if not locked or locked.status in _TERMINAL:
            return
        locked.status = "FAILED"
        locked.error_message = error_message
        locked.last_error = last_error
        update_fields = ["status", "error_message", "last_error", "updated_at"]
        if _scrub_terminal_job_payload(locked):
            update_fields.append("payload")
        locked.save(update_fields=update_fields)


def result_exists_for_job(job_model) -> bool:
    """해당 job에 대한 AIResultModel 존재 여부."""
    from apps.domains.ai.models import AIResultModel
    return AIResultModel.objects.filter(job=job_model).exists()


def result_create(job_model, payload) -> None:
    """AIResultModel 1건 생성."""
    from apps.domains.ai.models import AIResultModel
    AIResultModel.objects.create(job=job_model, payload=payload)


def get_airuntime_config(key: str):
    """AIRuntimeConfigModel key로 1건 조회."""
    from apps.domains.ai.models import AIRuntimeConfigModel
    return AIRuntimeConfigModel.objects.filter(key=key).first()


def get_tenant_config(tenant_id: str):
    """TenantConfigModel tenant_id로 1건 조회."""
    from apps.domains.ai.models import TenantConfigModel
    return TenantConfigModel.objects.filter(tenant_id=tenant_id).first()
