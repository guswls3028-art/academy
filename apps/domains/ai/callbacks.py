# apps/domains/ai/callbacks.py
"""
AI Job 완료 후 도메인별 후속 처리를 담당한다.

핵심 규칙:
- AI Job의 상태(DONE/FAILED)는 이미 UoW에서 처리된 상태로 진입한다.
- 이 모듈은 AI 결과를 "도메인 엔티티에 반영"하는 역할만 한다.
- 멱등성 보장: 동일 job에 대해 중복 호출해도 안전해야 한다.
- callback 실패가 AI Job 상태를 되돌리지 않는다.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from django.db import close_old_connections, connection

from apps.support.ai.callback_dependencies import (
    get_auto_segmentation_snapshot_model,
    get_exam_question_proposal_model,
    get_exam_segmentation_models,
    get_matchup_document_models,
    get_matchup_page_state_model,
    get_matchup_problem_model,
    get_post_entity_model,
    get_submission_ai_result_applier,
    get_wrong_note_pdf_callback_dependencies,
    handle_matchup_proposal_path,
    invalidate_matchup_tenant_similar_cache,
)

logger = logging.getLogger(__name__)


def _close_old_connections_if_safe() -> None:
    """Release worker connections without closing an active transaction."""
    if not connection.in_atomic_block:
        close_old_connections()


def dispatch_ai_result_to_domain(
    *,
    job_id: str,
    status: str,
    result_payload: Optional[Dict[str, Any]],
    error: Optional[str],
    source_domain: Optional[str],
    source_id: Optional[str],
    tier: str = "basic",
) -> bool:
    """
    AI Job 완료 후 도메인별 후속 처리 디스패처.
    source_domain에 따라 적절한 도메인 핸들러로 라우팅한다.
    """
    if source_domain == "tools_problem_studio":
        try:
            from apps.domains.tools.contracts import (
                beta_run_id_from_job_payload,
                settle_beta_run,
            )
            from apps.domains.ai.models import AIJobModel

            job_payload = (
                AIJobModel.objects.filter(job_id=job_id)
                .values_list("payload", flat=True)
                .first()
            )
            from apps.domains.tools.contracts import (
                settle_explanation_step_failure,
            )

            settle_explanation_step_failure(
                job_id=job_id,
                status=status,
                payload=job_payload,
                error=error or "",
            )
            settle_beta_run(
                run_id=beta_run_id_from_job_payload(job_payload),
                job_id=job_id,
                terminal_status=status,
                error=error or "",
            )
        except Exception:
            logger.exception(
                "AI_CALLBACK_PROBLEM_STUDIO_BETA_FAILED | job_id=%s",
                job_id,
            )
            return False
        return True

    if source_domain == "results_wrong_note_pdf":
        try:
            _handle_wrong_note_pdf_result(
                job_id=job_id,
                status=status,
                result_payload=result_payload or {},
                error=error,
                source_id=source_id,
            )
        except Exception:
            logger.exception(
                "AI_CALLBACK_WRONG_NOTE_PDF_FAILED | job_id=%s | source_id=%s",
                job_id,
                source_id,
            )
            return False
        return True

    # exams 도메인: question_segmentation 결과 처리
    if source_domain == "exams":
        try:
            _handle_exam_ai_result(
                job_id=job_id,
                status=status,
                result_payload=result_payload or {},
                error=error,
                source_id=source_id,
            )
        except Exception:
            logger.exception(
                "AI_CALLBACK_EXAM_FAILED | job_id=%s | source_id=%s",
                job_id, source_id,
            )
            return False
        return True

    # matchup 도메인: matchup_analysis 결과 처리
    if source_domain == "matchup":
        try:
            _handle_matchup_ai_result(
                job_id=job_id,
                status=status,
                result_payload=result_payload or {},
                error=error,
                source_id=source_id,
            )
        except Exception:
            logger.exception(
                "AI_CALLBACK_MATCHUP_FAILED | job_id=%s | source_id=%s",
                job_id, source_id,
            )
            return False
        return True

    # matchup_index: 시험 문제 인덱싱 결과
    if source_domain == "matchup_index":
        try:
            _handle_matchup_index_result(
                job_id=job_id,
                status=status,
                result_payload=result_payload or {},
                error=error,
                source_id=source_id,
            )
        except Exception:
            logger.exception(
                "AI_CALLBACK_MATCHUP_INDEX_FAILED | job_id=%s | source_id=%s",
                job_id, source_id,
            )
            return False
        return True

    # matchup_manual: 수동 크롭 problem OCR + 임베딩 결과
    if source_domain == "matchup_manual":
        try:
            _handle_matchup_manual_result(
                job_id=job_id,
                status=status,
                result_payload=result_payload or {},
                error=error,
                source_id=source_id,
            )
        except Exception:
            logger.exception(
                "AI_CALLBACK_MATCHUP_MANUAL_FAILED | job_id=%s | problem_id=%s",
                job_id, source_id,
            )
            return False
        return True

    # community_qna: 학생 Q&A 매치업 검색 결과
    if source_domain == "community_qna":
        try:
            _handle_qna_matchup_search_result(
                job_id=job_id,
                status=status,
                result_payload=result_payload or {},
                source_id=source_id,
            )
        except Exception:
            logger.exception(
                "AI_CALLBACK_QNA_MATCHUP_FAILED | job_id=%s | post_id=%s",
                job_id, source_id,
            )
            return False
        return True

    if source_domain != "submissions":
        logger.debug(
            "AI_CALLBACK_SKIP | source_domain=%s job_id=%s (not submissions)",
            source_domain, job_id,
        )
        return True

    if not source_id:
        logger.warning(
            "AI_CALLBACK_SKIP | source_id empty | job_id=%s",
            job_id,
        )
        return True

    t0 = time.monotonic()
    logger.info(
        "AI_CALLBACK_START | job_id=%s | submission_id=%s | status=%s | tier=%s",
        job_id, source_id, status, tier,
    )

    try:
        _handle_submission_ai_result(
            job_id=job_id,
            submission_id=int(source_id),
            status=status,
            result_payload=result_payload or {},
            error=error,
            tier=tier,
        )
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        logger.info(
            "AI_CALLBACK_SUCCESS | job_id=%s | submission_id=%s | elapsed_ms=%d",
            job_id, source_id, elapsed_ms,
        )
        return True
    except Exception:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        logger.exception(
            "AI_CALLBACK_FAILED | job_id=%s | submission_id=%s | elapsed_ms=%d",
            job_id, source_id, elapsed_ms,
        )
        return False


def _handle_wrong_note_pdf_result(
    *,
    job_id: str,
    status: str,
    result_payload: Dict[str, Any],
    error: Optional[str],
    source_id: Optional[str],
) -> None:
    """Apply a tools-worker result to the tenant-scoped WrongNotePDF record."""
    from django.db import transaction
    from django.utils import timezone

    from apps.domains.ai.models import AIJobModel
    WrongNotePDF, wrong_note_pdf_storage_key = (
        get_wrong_note_pdf_callback_dependencies()
    )

    ai_job = AIJobModel.objects.filter(job_id=job_id).first()
    if not ai_job or not ai_job.tenant_id or not source_id:
        raise ValueError("Wrong-note PDF callback scope is missing.")
    if ai_job.source_domain != "results_wrong_note_pdf":
        raise ValueError("Wrong-note PDF callback source domain mismatch.")
    if str(ai_job.source_id or "") != str(source_id):
        raise ValueError("Wrong-note PDF callback source id mismatch.")

    pdf_job_id = int(source_id)
    payload_job_id = result_payload.get("wrong_note_pdf_job_id")
    if payload_job_id is not None and str(payload_job_id) != str(pdf_job_id):
        raise ValueError("Wrong-note PDF callback payload id mismatch.")

    with transaction.atomic():
        pdf_job = (
            WrongNotePDF.objects.select_for_update()
            .select_related("enrollment__tenant")
            .get(id=pdf_job_id, enrollment__tenant_id=int(ai_job.tenant_id))
        )
        if pdf_job.status in {
            WrongNotePDF.Status.DONE,
            WrongNotePDF.Status.FAILED,
        }:
            return

        outcome = str(result_payload.get("outcome") or "")
        if status == "FAILED":
            next_status = WrongNotePDF.Status.FAILED
            file_path = ""
            error_message = str(
                error or "PDF를 만들지 못했습니다. 잠시 후 다시 시도해 주세요."
            )
        elif outcome == WrongNotePDF.Status.DONE:
            file_path = str(result_payload.get("file_path") or "")
            expected_path = wrong_note_pdf_storage_key(
                job=pdf_job,
                tenant=pdf_job.enrollment.tenant,
            )
            if file_path != expected_path:
                raise ValueError("Wrong-note PDF callback file path mismatch.")
            next_status = WrongNotePDF.Status.DONE
            error_message = ""
        elif outcome == WrongNotePDF.Status.FAILED:
            file_path = str(result_payload.get("file_path") or "")
            if file_path:
                expected_path = wrong_note_pdf_storage_key(
                    job=pdf_job,
                    tenant=pdf_job.enrollment.tenant,
                )
                if file_path != expected_path:
                    raise ValueError("Wrong-note PDF failed callback file path mismatch.")
            next_status = WrongNotePDF.Status.FAILED
            error_message = str(
                result_payload.get("error_message")
                or "PDF를 만들지 못했습니다. 잠시 후 다시 시도해 주세요."
            )
        else:
            raise ValueError("Wrong-note PDF callback outcome is invalid.")

        WrongNotePDF.objects.filter(
            id=pdf_job.id,
            enrollment__tenant_id=int(ai_job.tenant_id),
            status__in=[
                WrongNotePDF.Status.PENDING,
                WrongNotePDF.Status.RUNNING,
            ],
        ).update(
            status=next_status,
            file_path=file_path,
            error_message=error_message,
            updated_at=timezone.now(),
        )


def _handle_submission_ai_result(
    *,
    job_id: str,
    submission_id: int,
    status: str,
    result_payload: Dict[str, Any],
    error: Optional[str],
    tier: str,
) -> None:
    """
    Submission 도메인의 AI 결과 처리.

    1. AI 결과를 Submission에 반영 (상태 전이: DISPATCHED → ANSWERS_READY/NEEDS_ID/FAILED)
    2. ANSWERS_READY가 되면 채점 파이프라인 실행
    """
    from academy.application.use_cases.omr.grading_readiness import (
        grade_omr_submission_if_ready,
    )
    apply_ai_result = get_submission_ai_result_applier()

    # 🔐 tenant 교차검증: AI job의 tenant_id와 submission의 tenant_id 일치 확인
    ai_job = None
    if job_id:
        from apps.domains.ai.models import AIJobModel
        from academy.adapters.db.django.repositories_submissions import (
            get_submission_tenant_id,
        )

        ai_job = AIJobModel.objects.filter(job_id=job_id).first()
        if ai_job and ai_job.tenant_id:
            try:
                sub_tenant_id = get_submission_tenant_id(submission_id)
                if sub_tenant_id and str(ai_job.tenant_id) != str(sub_tenant_id):
                    logger.error(
                        "TENANT_ISOLATION_VIOLATION | _handle_submission_ai_result | "
                        "job_id=%s | job_tenant=%s | submission_tenant=%s | submission_id=%s",
                        job_id, ai_job.tenant_id, sub_tenant_id, submission_id,
                    )
                    return
            except Exception:
                logger.exception(
                    "TENANT_CHECK_ERROR | job_id=%s | submission_id=%s",
                    job_id, submission_id,
                )

    # apply_ai_result는 payload에서 submission_id를 꺼냄
    payload = dict(result_payload)
    payload["submission_id"] = submission_id
    payload["job_id"] = job_id
    payload["status"] = status
    payload["error"] = error

    # tenant_id를 payload에 전달하여 apply_omr_ai_result에서도 교차검증 가능
    if ai_job and ai_job.tenant_id:
        payload["tenant_id"] = str(ai_job.tenant_id)

    returned_id = apply_ai_result(payload)

    if not returned_id:
        logger.warning(
            "AI_CALLBACK_APPLY_NULL | submission_id=%s | job_id=%s",
            submission_id, job_id,
        )
        return

    try:
        decision = grade_omr_submission_if_ready(
            int(returned_id),
            actor=f"ai_callback.{job_id}",
        )
        if decision.graded:
            logger.info(
                "AI_CALLBACK_GRADING_TRIGGERED | submission_id=%s | job_id=%s",
                returned_id, job_id,
            )
        else:
            logger.info(
                "AI_CALLBACK_GRADING_SKIPPED | submission_id=%s | status=%s | "
                "reason=%s | missing=%s | job_id=%s",
                returned_id, decision.status, decision.reason,
                decision.readiness.missing, job_id,
            )
    except Exception:
        logger.exception(
            "AI_CALLBACK_GRADING_ERROR | submission_id=%s | job_id=%s",
            returned_id, job_id,
        )


def _handle_exam_ai_result(
    *,
    job_id: str,
    status: str,
    result_payload: Dict[str, Any],
    error: Optional[str],
    source_id: Optional[str],
) -> None:
    """
    Exam 도메인 AI 결과 처리 (question_segmentation).

    결과에서 문항 박스 추출 → Sheet/ExamQuestion 자동 생성.
    해설이 포함되어 있으면 QuestionExplanation도 생성.
    """
    if status == "FAILED":
        try:
            from apps.domains.ai.models import AIJobModel

            Exam, _, _, _ = get_exam_segmentation_models()
            ai_job = AIJobModel.objects.filter(job_id=job_id).first()
            if ai_job and ai_job.tenant_id and source_id:
                Exam.objects.filter(
                    id=int(source_id),
                    tenant_id=int(ai_job.tenant_id),
                    segmentation_status=Exam.SegmentationStatus.PROCESSING,
                ).update(segmentation_status=Exam.SegmentationStatus.FAILED)
        except Exception:
            logger.exception(
                "AI_CALLBACK_EXAM_FAILED_STATUS_UPDATE_ERROR | job_id=%s",
                job_id,
            )
        logger.warning(
            "AI_CALLBACK_EXAM_FAILED_STATUS | job_id=%s | error=%s",
            job_id, error,
        )
        return

    exam_id = result_payload.get("exam_id") or source_id
    if not exam_id:
        logger.warning("AI_CALLBACK_EXAM_NO_EXAM_ID | job_id=%s", job_id)
        return

    boxes = result_payload.get("boxes", [])
    questions_data = result_payload.get("questions", [])
    explanations_data = result_payload.get("explanations", [])
    answers_data = result_payload.get("answers", [])
    question_image_keys = result_payload.get("question_image_keys") or {}

    if result_payload.get("conversion_required"):
        try:
            from apps.domains.ai.models import AIJobModel

            Exam, _, _, _ = get_exam_segmentation_models()
            ai_job = AIJobModel.objects.filter(job_id=job_id).first()
            if ai_job and ai_job.tenant_id:
                Exam.objects.filter(
                    id=int(exam_id),
                    tenant_id=int(ai_job.tenant_id),
                    segmentation_status=Exam.SegmentationStatus.PROCESSING,
                ).update(
                    segmentation_status=Exam.SegmentationStatus.CONVERSION_REQUIRED,
                )
        except Exception:
            logger.exception(
                "AI_CALLBACK_EXAM_CONVERSION_STATUS_ERROR | job_id=%s",
                job_id,
            )
        return

    if not boxes and not questions_data:
        try:
            from apps.domains.ai.models import AIJobModel

            Exam, _, _, _ = get_exam_segmentation_models()
            ai_job = AIJobModel.objects.filter(job_id=job_id).first()
            if ai_job and ai_job.tenant_id:
                Exam.objects.filter(
                    id=int(exam_id),
                    tenant_id=int(ai_job.tenant_id),
                    segmentation_status=Exam.SegmentationStatus.PROCESSING,
                ).update(segmentation_status=Exam.SegmentationStatus.FAILED)
        except Exception:
            logger.exception(
                "AI_CALLBACK_EXAM_EMPTY_STATUS_UPDATE_ERROR | job_id=%s",
                job_id,
            )
        logger.info(
            "AI_CALLBACK_EXAM_NO_BOXES | job_id=%s | exam_id=%s",
            job_id, exam_id,
        )
        return

    try:
        from django.db import transaction
        Exam, Sheet, ExamQuestion, QuestionExplanation = get_exam_segmentation_models()

        with transaction.atomic():
            exam = Exam.objects.select_for_update().get(id=int(exam_id))

            # 🔐 tenant 교차검증: AI job의 tenant_id와 exam의 tenant_id 일치 확인
            if job_id:
                from apps.domains.ai.models import AIJobModel
                ai_job = AIJobModel.objects.filter(job_id=job_id).first()
                if ai_job and ai_job.tenant_id and hasattr(exam, "tenant_id"):
                    if str(ai_job.tenant_id) != str(exam.tenant_id):
                        logger.error(
                            "AI_CALLBACK_TENANT_MISMATCH | job_id=%s | job_tenant=%s | exam_tenant=%s | exam_id=%s",
                            job_id, ai_job.tenant_id, exam.tenant_id, exam_id,
                        )
                        return

            is_guided_regular = exam.exam_type == Exam.ExamType.REGULAR
            if (
                is_guided_regular
                and exam.segmentation_status
                != Exam.SegmentationStatus.PROCESSING
            ):
                logger.warning(
                    "AI_CALLBACK_EXAM_REGULAR_NOT_PROCESSING | "
                    "job_id=%s | exam_id=%s | status=%s",
                    job_id,
                    exam_id,
                    exam.segmentation_status,
                )
                return

            if is_guided_regular and ExamQuestion.objects.filter(
                sheet__exam=exam,
            ).exists():
                exam.segmentation_status = Exam.SegmentationStatus.FAILED
                exam.save(
                    update_fields=["segmentation_status", "updated_at"]
                )
                logger.warning(
                    "AI_CALLBACK_EXAM_REGULAR_STRUCTURE_COLLISION | "
                    "job_id=%s | exam_id=%s",
                    job_id,
                    exam_id,
                )
                return

            if is_guided_regular:
                ExamQuestionProposal = get_exam_question_proposal_model()

                explanation_by_number = {
                    int(item.get("question_number")): item
                    for item in explanations_data
                    if item.get("question_number")
                }
                answer_by_number = {
                    int(item.get("question_number")): item
                    for item in answers_data
                    if item.get("question_number")
                }
                answer_source_requested = bool(
                    result_payload.get("answer_source_requested")
                )
                explanation_source_requested = bool(
                    result_payload.get("explanation_source_requested")
                )
                paired_source_status = str(
                    result_payload.get("paired_source_status") or ""
                )
                source_issues = list(result_payload.get("source_issues") or [])
                missing_answer_numbers = {
                    int(number)
                    for number in result_payload.get("missing_answer_numbers") or []
                }
                missing_explanation_numbers = {
                    int(number)
                    for number in result_payload.get("missing_explanation_numbers") or []
                }
                ExamQuestionProposal.objects.filter(exam=exam).delete()
                proposals = []
                for position, q_data in enumerate(questions_data or [], start=1):
                    detected_number = int(
                        q_data.get("original_number")
                        or q_data.get("number")
                        or position
                    )
                    proposed_number = int(q_data.get("number") or position)
                    bbox = list(q_data.get("bbox") or [0, 0, 0, 0])
                    explanation = explanation_by_number.get(detected_number, {})
                    answer = answer_by_number.get(detected_number, {})
                    image_key = (
                        question_image_keys.get(proposed_number)
                        or question_image_keys.get(str(proposed_number))
                        or ""
                    )
                    proposals.append(
                        ExamQuestionProposal(
                            exam=exam,
                            position=position,
                            number=proposed_number,
                            detected_number=detected_number,
                            page_index=max(int(q_data.get("page_index") or 0), 0),
                            region_meta={
                                "x": int(bbox[0]) if len(bbox) > 0 else 0,
                                "y": int(bbox[1]) if len(bbox) > 1 else 0,
                                "w": int(bbox[2]) if len(bbox) > 2 else 0,
                                "h": int(bbox[3]) if len(bbox) > 3 else 0,
                                "source_render_mode": str(
                                    explanation.get("source_render_mode") or ""
                                ),
                                "source_attachment_image_key": str(
                                    explanation.get("source_attachment_image_key") or ""
                                ),
                                "source_attachment_requires_review": bool(
                                    explanation.get("source_attachment_requires_review")
                                ),
                                "answer_candidate": str(
                                    answer.get("answer") or ""
                                )[:500],
                                "answer_source_image_key": str(
                                    answer.get("source_image_key") or ""
                                ),
                                "answer_source_requested": answer_source_requested,
                                "explanation_source_requested": (
                                    explanation_source_requested
                                ),
                                "paired_source_status": paired_source_status,
                                "source_issues": source_issues,
                                "answer_missing": (
                                    detected_number in missing_answer_numbers
                                ),
                                "explanation_missing": (
                                    detected_number in missing_explanation_numbers
                                ),
                            },
                            problem_image_key=str(image_key),
                            explanation_text=str(explanation.get("text") or "")[:2000],
                            explanation_image_key=str(explanation.get("image_key") or ""),
                            match_confidence=explanation.get("match_confidence"),
                            problem_crop_ratio=float(q_data.get("problem_crop_ratio") or 1.0),
                            source_job_id=str(job_id or "")[:64],
                            engine=str(result_payload.get("segmentation_method") or "")[:32],
                        )
                    )
                if not proposals:
                    exam.segmentation_status = Exam.SegmentationStatus.FAILED
                    exam.save(update_fields=["segmentation_status", "updated_at"])
                    return
                ExamQuestionProposal.objects.bulk_create(proposals)
                exam.segmentation_status = Exam.SegmentationStatus.REVIEW_REQUIRED
                exam.save(update_fields=["segmentation_status", "updated_at"])
                logger.info(
                    "AI_CALLBACK_EXAM_REVIEW_REQUIRED | job_id=%s | exam_id=%s | proposals=%d",
                    job_id,
                    exam_id,
                    len(proposals),
                )
                return

            # Sheet 가져오기 또는 생성
            sheet, _ = Sheet.objects.get_or_create(
                exam=exam,
                defaults={"name": "MAIN", "total_questions": 0},
            )

            # 문항 개수 결정
            total = len(questions_data) if questions_data else len(boxes)
            if total == 0:
                return

            sheet.total_questions = total
            if is_guided_regular:
                if exam.grading_mode == Exam.GradingMode.CHOICE:
                    choice_count = total
                elif exam.grading_mode == Exam.GradingMode.WRITTEN:
                    choice_count = 0
                else:
                    choice_count = (
                        min(
                            max(int(exam.choice_question_count or 1), 1),
                            total - 1,
                        )
                        if total > 1
                        else 0
                    )
                sheet.choice_count = choice_count
                sheet.essay_count = max(total - choice_count, 0)
                if int(exam.choice_question_count or 0) != choice_count:
                    exam.choice_question_count = choice_count
            sheet.save(
                update_fields=[
                    "total_questions",
                    "choice_count",
                    "essay_count",
                    "updated_at",
                ]
            )

            # 기존 문항 정리 (범위 밖 삭제)
            existing_numbers = set(
                ExamQuestion.objects.filter(sheet=sheet).values_list("number", flat=True)
            )
            new_numbers = set(range(1, total + 1))
            to_delete = existing_numbers - new_numbers
            if to_delete:
                ExamQuestion.objects.filter(sheet=sheet, number__in=to_delete).delete()

            # 문항 생성/갱신
            created_questions = []
            guided_base_score = (
                round(float(exam.max_score or 0.0) / total, 2)
                if is_guided_regular and total > 0
                else 0.0
            )
            for idx in range(1, total + 1):
                # questions_data가 있으면 사용, 없으면 boxes에서 직접
                if questions_data and idx <= len(questions_data):
                    q_data = questions_data[idx - 1]
                    bbox = q_data.get("bbox", [0, 0, 0, 0])
                    region_meta = {
                        "x": int(bbox[0]) if len(bbox) > 0 else 0,
                        "y": int(bbox[1]) if len(bbox) > 1 else 0,
                        "w": int(bbox[2]) if len(bbox) > 2 else 0,
                        "h": int(bbox[3]) if len(bbox) > 3 else 0,
                        "page_index": q_data.get("page_index", 0),
                        # 세그멘테이션에서 감지한 원본 번호 — dedup으로 바뀌었을 수 있음
                        "detected_number": q_data.get(
                            "original_number", q_data.get("number", idx)
                        ),
                    }
                elif boxes and idx <= len(boxes):
                    b = boxes[idx - 1]
                    region_meta = {
                        "x": int(b[0]) if len(b) > 0 else 0,
                        "y": int(b[1]) if len(b) > 1 else 0,
                        "w": int(b[2]) if len(b) > 2 else 0,
                        "h": int(b[3]) if len(b) > 3 else 0,
                    }
                else:
                    region_meta = {"x": 0, "y": 0, "w": 0, "h": 0}

                # image_key: 워커가 크롭하여 R2에 업로드한 이미지 키
                # question_image_keys는 {문항번호(int): r2_key(str)} 형태
                q_image_key = question_image_keys.get(idx) or question_image_keys.get(str(idx)) or ""

                defaults = {
                    "region_meta": region_meta,
                    "image_key": q_image_key,
                }
                if is_guided_regular:
                    defaults["question_kind"] = (
                        ExamQuestion.QuestionKind.CHOICE
                        if idx <= int(sheet.choice_count or 0)
                        else ExamQuestion.QuestionKind.ESSAY
                    )
                    defaults["score"] = (
                        round(
                            float(exam.max_score or 0.0)
                            - guided_base_score * (total - 1),
                            2,
                        )
                        if idx == total
                        else guided_base_score
                    )

                obj, _ = ExamQuestion.objects.update_or_create(
                    sheet=sheet,
                    number=idx,
                    defaults=defaults,
                )
                created_questions.append(obj)

            # 해설 생성 (있는 경우만)
            if explanations_data:
                # question_number → ExamQuestion 매핑
                q_by_number = {q.number: q for q in created_questions}

                for exp in explanations_data:
                    q_num = exp.get("question_number")
                    text = exp.get("text", "")
                    if not q_num or q_num not in q_by_number:
                        continue

                    QuestionExplanation.objects.update_or_create(
                        question=q_by_number[q_num],
                        defaults={
                            "text": text[:2000],
                            "source": QuestionExplanation.Source.AI_EXTRACTED,
                            "match_confidence": 1.0 if text else 0.5,
                        },
                    )

            logger.info(
                "AI_CALLBACK_EXAM_SUCCESS | job_id=%s | exam_id=%s | questions=%d | explanations=%d",
                job_id, exam_id, len(created_questions), len(explanations_data),
            )

            if is_guided_regular:
                exam.segmentation_status = Exam.SegmentationStatus.READY
                exam.save(
                    update_fields=[
                        "segmentation_status",
                        "choice_question_count",
                        "updated_at",
                    ]
                )

            # 매치업 자동 인덱싱: 시험 문제 → MatchupProblem
            try:
                from apps.domains.ai.gateway import dispatch_job as _dispatch
                _dispatch(
                    job_type="matchup_index_exam",
                    payload={
                        "exam_id": str(exam_id),
                        "tenant_id": str(exam.tenant_id),
                    },
                    tenant_id=str(exam.tenant_id),
                    source_domain="matchup_index",
                    source_id=str(exam_id),
                )
                logger.info("MATCHUP_INDEX_DISPATCHED | exam_id=%s", exam_id)
            except Exception:
                logger.warning("MATCHUP_INDEX_DISPATCH_FAILED | exam_id=%s", exam_id, exc_info=True)

    except Exam.DoesNotExist:
        logger.warning(
            "AI_CALLBACK_EXAM_NOT_FOUND | job_id=%s | exam_id=%s",
            job_id, exam_id,
        )
    except Exception:
        logger.exception(
            "AI_CALLBACK_EXAM_ERROR | job_id=%s | exam_id=%s",
            job_id, exam_id,
        )
        raise


def _handle_matchup_ai_result(
    *,
    job_id: str,
    status: str,
    result_payload: Dict[str, Any],
    error: Optional[str],
    source_id: Optional[str],
) -> None:
    """
    Matchup 도메인 AI 결과 처리 (matchup_analysis).

    결과에서 추출된 문제 목록을 MatchupProblem으로 생성하고
    MatchupDocument 상태를 업데이트한다.
    """
    MatchupDocument, MatchupProblem = get_matchup_document_models()

    _close_old_connections_if_safe()

    if not source_id:
        logger.warning("AI_CALLBACK_MATCHUP_NO_SOURCE_ID | job_id=%s", job_id)
        return

    try:
        doc = MatchupDocument.objects.get(id=int(source_id))
    except MatchupDocument.DoesNotExist:
        logger.warning(
            "AI_CALLBACK_MATCHUP_DOC_NOT_FOUND | job_id=%s | source_id=%s (deleted?)",
            job_id, source_id,
        )
        return

    current_job_id = str(doc.ai_job_id or "")
    incoming_job_id = str(job_id or "")
    if incoming_job_id and current_job_id and incoming_job_id != current_job_id:
        logger.warning(
            "AI_CALLBACK_MATCHUP_STALE_JOB_SKIP | doc_id=%s | incoming_job_id=%s | current_job_id=%s | status=%s",
            doc.id, incoming_job_id, current_job_id, status,
        )
        return

    # 테넌트 교차검증
    if job_id:
        from apps.domains.ai.models import AIJobModel
        ai_job = AIJobModel.objects.filter(job_id=job_id).first()
        if ai_job and ai_job.tenant_id:
            if str(ai_job.tenant_id) != str(doc.tenant_id):
                logger.error(
                    "TENANT_ISOLATION_VIOLATION | _handle_matchup_ai_result | "
                    "job_id=%s | job_tenant=%s | doc_tenant=%s | doc_id=%s",
                    job_id, ai_job.tenant_id, doc.tenant_id, source_id,
                )
                return

    if status == "FAILED":
        doc.status = "failed"
        doc.error_message = error or "AI 분석 실패"
        doc.save(update_fields=["status", "error_message", "updated_at"])
        logger.warning(
            "AI_CALLBACK_MATCHUP_FAILED_STATUS | job_id=%s | doc_id=%s | error=%s",
            job_id, source_id, error,
        )
        return

    problems_data = result_payload.get("problems", [])

    # Phase D (2026-05-09 basic_definition_2026_05_09 SSOT) — PageState='manual'
    # 페이지의 자동 cut 결과 silent drop. 사용자 directive 의 핵심:
    # 'auto + manual 결과 = 같은 최종 problem set 으로 병합. state=manual 페이지는
    # 학원장 manual cut 만 final, 자동 cut noise 차단'.
    # default on (ENV flag X). PageState row 없는 doc 은 manual_pages 빈 → no-op.
    # safe 변경 — 기존 자료 영향 0 (PageState 도입 전 자료 모두 row 0).
    try:
        MatchupPageState = get_matchup_page_state_model()
        manual_pages = set(
            MatchupPageState.objects.filter(
                document=doc, state="manual",
            ).values_list("page_index", flat=True)
        )
        if manual_pages:
            before = len(problems_data)
            problems_data = [
                p for p in problems_data
                if (p.get("meta") or {}).get("page_index") not in manual_pages
            ]
            dropped = before - len(problems_data)
            if dropped > 0:
                logger.info(
                    "MATCHUP_MANUAL_PAGE_DROP | doc=%s | dropped=%d/%d | manual_pages=%s",
                    doc.id, dropped, before, sorted(manual_pages),
                )
    except Exception:
        logger.exception(
            "MATCHUP_MANUAL_PAGE_DROP_FAIL | doc=%s | continuing with raw problems_data",
            doc.id,
        )

    # Phase C (2026-05-09 basic_definition_2026_05_09 SSOT) — page-fallback 차단.
    # 사용자 directive: "페이지 전체 crop 기본 금지". bbox=null 또는 면적 95%+ box 는
    # 학원장 노동 가치 0 (페이지 통째 problem = 매치업 노이즈) — silent drop.
    # ENV flag MATCHUP_BLOCK_PAGE_FALLBACK 점진 rollout. default off → 운영 영향 0.
    import os
    if os.environ.get("MATCHUP_BLOCK_PAGE_FALLBACK", "0") == "1":
        def _is_page_fallback(p):
            meta_p = p.get("meta") or {}
            bbox = meta_p.get("bbox")
            if not bbox:
                return True  # bbox null = 페이지 전체로 worker 가 fallback 한 case
            if isinstance(bbox, dict):
                w = float(bbox.get("w") or 0)
                h = float(bbox.get("h") or 0)
                if w >= 0.95 and h >= 0.95:
                    return True  # 페이지 면적 95%+ = 페이지 통째
            return False
        before_count = len(problems_data)
        problems_data = [p for p in problems_data if not _is_page_fallback(p)]
        blocked_count = before_count - len(problems_data)
        if blocked_count > 0:
            logger.info(
                "MATCHUP_PAGE_FALLBACK_BLOCKED | doc=%s | blocked=%d / total=%d",
                doc.id, blocked_count, before_count,
            )

    # Phase E (2026-05-09 basic_definition_2026_05_09 SSOT) — Proposal-first path 점진 rollout.
    # ENV flag MATCHUP_PROPOSAL_FIRST_TENANTS (콤마 구분 tenant id list) 매치 시
    # 신규 path. default 빈 list → 모든 doc 기존 path (운영 영향 0).
    # 점진: T1 sandbox 검증 → 사용자 명시 승인 → T2.
    proposal_first_tenants_raw = os.environ.get("MATCHUP_PROPOSAL_FIRST_TENANTS", "")
    proposal_first_tenants = {
        int(t) for t in proposal_first_tenants_raw.split(",") if t.strip().isdigit()
    }
    if doc.tenant_id in proposal_first_tenants:
        try:
            handle_matchup_proposal_path(
                job_id=job_id,
                doc=doc,
                problems_data=problems_data,
                result_payload=result_payload,
            )
        except Exception:
            logger.exception(
                "PROPOSAL_FIRST_PATH_FAILED | job_id=%s | doc_id=%s | falling back to legacy path",
                job_id, doc.id,
            )
            # fail-soft: helper 실패 시 legacy path 로 대체 (운영 흐름 보호)
        else:
            return  # proposal path 성공 — legacy bulk_create skip

    # 기존 문제 삭제 (재시도 시 중복 방지). manual=true / manual_owner_pinned=true 둘 다
    # 보호 — 학원장이 ManualCropModal 또는 적중보고서에서 직접 자른/별 토글한 problem 보존.
    # 아래 bulk_create(ignore_conflicts=True)가 unique(document, number) 충돌을
    # silent drop하므로, 같은 번호의 자동 결과는 자연스럽게 manual에 우선권을 양보.
    #
    # JSONB NULL semantics 회피 (운영 사고 2026-05-03): 이전 코드
    # `exclude(meta__manual=True)`는 `NOT ((meta->'manual') = 'true')`로 변환되어
    # 키가 없는 row(skeleton 등)에서 NULL=true → NULL → false로 평가 → exclude에서 빠짐
    # → delete 0건 → 이후 bulk_create가 unique(doc, number) 충돌로 silent drop →
    # skeleton row가 영구히 살아남음. T2 1355 problems가 dead state로 invalid 인덱싱.
    # ID 기반 명시 exclude로 NULL semantics 우회.
    #
    # pinned_ids 보호 (Phase E side fix, 2026-05-09): services.retry_document 는
    # manual_ids ∪ pinned_ids 둘 다 protected, 그러나 callback 은 manual_ids 만 보호
    # 하던 결함. 학원장이 적중 보고서에서 별 토글한 problem (manual_owner_pinned=true)
    # 이 reanalyze 후 callback 단계에서 손실되는 위험 차단.
    manual_ids = list(
        doc.problems.filter(meta__manual=True).values_list("id", flat=True)
    )
    pinned_ids = list(
        doc.problems.filter(meta__manual_owner_pinned=True).values_list("id", flat=True)
    )
    protected_ids = list(set(manual_ids) | set(pinned_ids))
    doc.problems.exclude(id__in=protected_ids).delete()

    # bulk create
    problem_objs = []
    for idx, p in enumerate(problems_data, start=1):
        problem_objs.append(MatchupProblem(
            tenant_id=doc.tenant_id,
            document=doc,
            number=p.get("number", 0),
            text=p.get("text", ""),
            image_key=p.get("image_key", ""),
            embedding=p.get("embedding"),
            image_embedding=p.get("image_embedding"),
            meta=p.get("meta", {}),
        ))
        if idx % 50 == 0:
            _close_old_connections_if_safe()

    if problem_objs:
        MatchupProblem.objects.bulk_create(problem_objs, ignore_conflicts=True)

    # AutoSegmentationSnapshot instrument (V11 BOTTLENECK §7.1, 2026-05-10) —
    # 자동 cut audit 행. ManualCorrectionDelta hook 이 이후 IoU 매칭으로
    # original_bbox/iou_with_ai/proposal_fk 자동 채우는 base. fine-tune loop 가동.
    # try/except fail-soft — 본 callback path 영향 0 (instrument only).
    try:
        AutoSegmentationSnapshot = get_auto_segmentation_snapshot_model()

        engine_used = (result_payload.get("segmentation_method") or "").lower()
        if engine_used and "yolo" in engine_used:
            engine_used = "yolo_v11"  # 운영 V11 그대로
        elif not engine_used:
            engine_used = "unknown"
        engine_version = (result_payload.get("model_version") or "")[:64]

        snapshot_objs = []
        for p in problems_data:
            meta_p = p.get("meta") or {}
            bbox = meta_p.get("bbox")
            if not bbox:
                continue  # bbox null = page-fallback / Phase C 차단됨
            snapshot_objs.append(AutoSegmentationSnapshot(
                tenant_id=doc.tenant_id,
                document=doc,
                job_id=str(job_id or "")[:64],
                page_index=int(meta_p.get("page_index") or 0),
                detected_problem_number=int(p.get("number") or 0),
                bbox=bbox if isinstance(bbox, dict) else {"raw": list(bbox)[:4]},
                engine=(
                    engine_used if engine_used in
                    {"yolo", "yolo_v11", "yolo_v12", "yolo_v13", "vlm",
                     "ocr", "native_pdf", "hybrid", "manual_assist", "unknown"}
                    else "unknown"
                ),
                engine_version=engine_version,
                confidence=float(meta_p.get("confidence") or 0.0),
                class_id=int(meta_p.get("class_id") or 0),
                class_name=(meta_p.get("class_name") or "problem")[:32],
                post_process_stage=(meta_p.get("post_process_stage") or "")[:32],
            ))
        if snapshot_objs:
            AutoSegmentationSnapshot.objects.bulk_create(
                snapshot_objs, ignore_conflicts=True,
            )
            logger.info(
                "AUTO_SEGMENTATION_SNAPSHOT | doc=%s | rows=%d | engine=%s",
                doc.id, len(snapshot_objs), engine_used,
            )
    except Exception:
        logger.exception(
            "AUTO_SEGMENTATION_SNAPSHOT_FAIL | doc=%s | continuing legacy path",
            doc.id,
        )

    # bulk_create + ignore_conflicts로 unique(document, number) 충돌 row가
    # silent drop될 수 있음 (segmentation 중복 번호 등). UI 좌측 라벨이
    # 디스패치 수가 아닌 실제 DB row 수와 일치하도록 재카운트.
    doc.status = "done"
    doc.problem_count = MatchupProblem.objects.filter(document=doc).count()
    doc.error_message = ""
    # segmentation_method를 meta에 저장 (UI 뱃지 + 관측용)
    meta = doc.meta or {}
    seg_method = result_payload.get("segmentation_method")
    if seg_method:
        meta["segmentation_method"] = seg_method

    # 학원장 실측 갭 fix (2026-05-05): 처리 status="done" 안에 진짜 결함을 숨기지 말 것.
    #   기존: 워커가 FAILED 외 응답이면 무조건 done. problem 0 / 폴백-only 도 success.
    #   학원장 dashboard "완료 N / 실패 0" 카운터가 거짓 안전망이 됨.
    #
    # meta.processing_quality (5단계 — TDD test_matchup_split_ideal_scenarios.py):
    #   "precise_split" — bbox null < 30%. 문항 단위 정밀 분리. 매치업 정상 가치.
    #   "coarse_split"  — bbox null 30~50%. 일부 페이지 폴백 (검수 권장).
    #   "needs_review"  — bbox null 50~70%. 다수 폴백 (학원장 직접 자르기 보강 필요).
    #   "page_fallback" — bbox null 70%+ AND problem_count > 0. 거의 모두 페이지=problem.
    #   "no_problems"   — problem 0건. 분리 자체 X (학원장 매뉴얼 처리).
    #
    # 측정 — DB에서 bbox null 카운트로 정밀도 산출. metric에 가려졌던 폴백 결함 노출.
    real_problem_count = doc.problem_count
    if real_problem_count == 0:
        meta["processing_quality"] = "no_problems"
        meta["bbox_null_ratio"] = None
        # 추천 pool 필터 (Phase 4, 2026-05-05): problem 0건 = 인덱싱 무용
        meta["indexable"] = False
    else:
        from django.db.models import Q
        bbox_null_count = MatchupProblem.objects.filter(
            document=doc,
        ).filter(
            # bbox=null in JSON meta — DB에서 직접 검사
            # (meta__bbox__isnull은 JSONField 값 null 검사)
            Q(meta__bbox__isnull=True) | Q(meta__bbox=None),
        ).count()
        bbox_ratio = bbox_null_count / real_problem_count
        meta["bbox_null_ratio"] = round(bbox_ratio, 3)
        # 추천 pool 자동 필터 (Phase 4, 2026-05-05): bbox null 비율 기반.
        # services.find_similar_problems가 meta.indexable=False인 doc을 풀에서 제외.
        # 학원장은 검수 후 직접 자르기로 indexable=True 토글 가능.
        # 학원장 실측 갭 fix:
        #   88 doc page_fallback이 매치업 풀에 페이지 임베딩 노이즈로 들어가
        #   매치업 자동 추천 작동률 0%였음. indexable 필터로 정밀 분리만 풀에 진입.
        if bbox_ratio >= 0.7:
            meta["processing_quality"] = "page_fallback"
            meta["indexable"] = False  # 페이지 통째 problem = 매칭 노이즈
        elif bbox_ratio >= 0.5:
            meta["processing_quality"] = "needs_review"
            meta["indexable"] = False  # 학원장 검수 권고 — 검수 후 토글
        elif bbox_ratio >= 0.3:
            meta["processing_quality"] = "coarse_split"
            meta["indexable"] = True   # 부분 정밀, 사용 가능
        else:
            meta["processing_quality"] = "precise_split"
            meta["indexable"] = True   # 정밀 분리, 풀 진입 OK
    # 워커가 캐시한 페이지 PNG 키 — ManualCropModal 첫 진입 즉시 (PDF 다운로드/렌더 회피)
    page_keys = result_payload.get("page_image_keys")
    page_dims = result_payload.get("page_dimensions")
    if page_keys:
        meta["page_image_keys"] = list(page_keys)
        if page_dims:
            meta["page_dimensions"] = list(page_dims)
    # paper_type 페이지별 분포 + Source 부적합 경고 (어드민 UI 배너용)
    paper_type_summary = result_payload.get("paper_type_summary")
    if paper_type_summary:
        meta["paper_type_summary"] = paper_type_summary

    # Phase 1 (2026-05-09 학원장 directive) — paper_type 신호로 source_type 보정.
    # 학원장이 자료 유형 7가지를 인지할 필요 없음. 시스템이 자동 분류.
    # 보정 정책 (academy/domain/tools/source_type_derive.py):
    # - source_type_origin == "user" 면 보호 (chip 으로 명시 변경한 값).
    # - 이미 specific value(student_exam_photo 등) 면 보호.
    # - paper_type 매핑이 confidence 100% 인 경우만 보정 (모호하면 유지).
    # - 라벨만 갱신. 워커 strategy 라우팅 영향 없음 (분석은 이미 끝).
    # 학원장 명시 변경 마커 — 두 가지 모두 보호 (legacy + 신규).
    _is_user_set = bool(
        meta.get("source_type_origin") == "user"
        or meta.get("source_type_user_override")
    )
    if paper_type_summary and not _is_user_set:
        from academy.domain.tools.source_type_derive import (
            derive_source_type_from_paper_type,
        )
        primary = paper_type_summary.get("primary") if isinstance(paper_type_summary, dict) else None
        cur_st = meta.get("source_type")
        derived = derive_source_type_from_paper_type(primary, cur_st)
        if derived:
            meta["source_type"] = derived
            meta["source_type_origin"] = "paper_type_derived"
            logger.info(
                "MATCHUP_SOURCE_TYPE_DERIVED | doc=%s | %s -> %s | paper_type_primary=%s",
                doc.id, cur_st, derived, primary,
            )

    doc.meta = meta
    doc.save(update_fields=[
        "status", "problem_count", "error_message", "meta", "updated_at",
    ])

    # Stage 6.3V — LayoutFingerprint 측정 누적 (운영 영향 0 instrumentation).
    #   본 호출은 read-only measurement + UPSERT 만. 어떤 예외도 본 흐름에 전파되지
    #   않는다 (collect_and_save 가 모두 swallow + warning log). doc.status="done"
    #   확정 후 + tenant 검증 통과 후 + transaction 안에서 호출.
    try:
        from academy.application.use_cases.ai.segmentation.fingerprint_collector import (
            collect_and_save as _collect_fingerprint,
        )
        cropped_count = MatchupProblem.objects.filter(
            document=doc,
        ).exclude(
            meta__bbox__isnull=True,
        ).count()
        _collect_fingerprint(
            doc=doc,
            result_payload=result_payload,
            problem_count=doc.problem_count,
            cropped_problem_count=cropped_count,
        )
    except Exception as _fp_err:  # noqa: BLE001
        # 호출 자체가 실패해도 본 callback 흐름은 영향 0
        logger.warning(
            "AI_CALLBACK_MATCHUP_FINGERPRINT_OUTER_FAIL | job_id=%s | doc_id=%s | err=%s",
            job_id, source_id, _fp_err,
        )

    # 검색 캐시 무효화 (P1 fix 2026-05-11): reanalyze 가 problem 풀 재구성하므로
    # 기존 캐시는 dead pid + stale embedding 보유. manual_crop / merge / delete
    # path 와 일관. fail-soft — invalidate 실패가 callback 본 흐름에 영향 0.
    try:
        invalidate_matchup_tenant_similar_cache(doc.tenant_id)
    except Exception:
        logger.exception(
            "AI_CALLBACK_MATCHUP_CACHE_INVALIDATE_FAILED | doc_id=%s | tenant=%s",
            source_id, doc.tenant_id,
        )

    logger.info(
        "AI_CALLBACK_MATCHUP_SUCCESS | job_id=%s | doc_id=%s | problems=%d | seg=%s",
        job_id, source_id, len(problem_objs), seg_method,
    )
    _close_old_connections_if_safe()


def _handle_qna_matchup_search_result(
    *,
    job_id: str,
    status: str,
    result_payload: Dict[str, Any],
    source_id: Optional[str],
) -> None:
    """Q&A 매치업 검색 결과를 PostEntity.meta에 저장."""
    if status == "FAILED":
        logger.warning("AI_CALLBACK_QNA_MATCHUP_FAILED | job_id=%s | post_id=%s", job_id, source_id)
        return

    post_id = result_payload.get("post_id") or source_id
    if not post_id:
        return

    PostEntity = get_post_entity_model()

    # 테넌트 교차검증
    tenant_id_from_job = None
    if job_id:
        from apps.domains.ai.models import AIJobModel
        ai_job = AIJobModel.objects.filter(job_id=job_id).first()
        if ai_job:
            tenant_id_from_job = ai_job.tenant_id

    try:
        filter_kwargs = {"id": int(post_id)}
        if tenant_id_from_job:
            filter_kwargs["tenant_id"] = tenant_id_from_job
        post = PostEntity.objects.get(**filter_kwargs)
    except PostEntity.DoesNotExist:
        logger.warning("AI_CALLBACK_QNA_POST_NOT_FOUND | post_id=%s", post_id)
        return

    results = result_payload.get("results", [])
    ocr_text = result_payload.get("ocr_text", "")

    meta = post.meta or {}
    meta["matchup_results"] = results
    meta["matchup_ocr_text"] = ocr_text[:500]
    post.meta = meta
    post.save(update_fields=["meta"])

    logger.info(
        "AI_CALLBACK_QNA_MATCHUP_SUCCESS | post_id=%s | results=%d",
        post_id, len(results),
    )


def _handle_matchup_index_result(
    *,
    job_id: str,
    status: str,
    result_payload: Dict[str, Any],
    error: Optional[str],
    source_id: Optional[str],
) -> None:
    """
    시험 문제 인덱싱 결과 처리 (matchup_index_exam).
    source_id = exam_id. Document 없이 MatchupProblem 직접 생성.
    """
    MatchupProblem = get_matchup_problem_model()

    _close_old_connections_if_safe()

    if status == "FAILED":
        logger.warning(
            "AI_CALLBACK_MATCHUP_INDEX_FAILED | job_id=%s | exam_id=%s | error=%s",
            job_id, source_id, error,
        )
        return

    exam_id = source_id
    if not exam_id:
        return

    problems_data = result_payload.get("problems", [])
    if not problems_data:
        logger.info("AI_CALLBACK_MATCHUP_INDEX_EMPTY | job_id=%s | exam_id=%s", job_id, exam_id)
        return

    # tenant_id 추출
    tenant_id = None
    if job_id:
        from apps.domains.ai.models import AIJobModel
        ai_job = AIJobModel.objects.filter(job_id=job_id).first()
        if ai_job:
            tenant_id = ai_job.tenant_id

    if not tenant_id:
        logger.warning("AI_CALLBACK_MATCHUP_INDEX_NO_TENANT | job_id=%s", job_id)
        return

    # 기존 인덱싱 결과 삭제 (재인덱싱 시 중복 방지)
    MatchupProblem.objects.filter(
        tenant_id=tenant_id,
        source_type="exam",
        source_exam_id=int(exam_id),
    ).delete()

    problem_objs = []
    for idx, p in enumerate(problems_data, start=1):
        problem_objs.append(MatchupProblem(
            tenant_id=tenant_id,
            document=None,
            number=p.get("number", 0),
            text=p.get("text", ""),
            image_key=p.get("image_key", ""),
            embedding=p.get("embedding"),
            meta={},
            source_type="exam",
            source_exam_id=int(exam_id),
            source_question_number=p.get("source_question_number", p.get("number", 0)),
            source_lecture_title=p.get("lecture_title", ""),
            source_session_title=p.get("session_title", ""),
            source_exam_title=p.get("exam_title", ""),
        ))
        if idx % 50 == 0:
            _close_old_connections_if_safe()

    if problem_objs:
        MatchupProblem.objects.bulk_create(problem_objs, ignore_conflicts=True)

    logger.info(
        "AI_CALLBACK_MATCHUP_INDEX_SUCCESS | job_id=%s | exam_id=%s | indexed=%d",
        job_id, exam_id, len(problem_objs),
    )
    _close_old_connections_if_safe()


def _handle_matchup_manual_result(
    *,
    job_id: str,
    status: str,
    result_payload: Dict[str, Any],
    error: Optional[str],
    source_id: Optional[str],
) -> None:
    """수동 크롭 problem OCR/임베딩 결과 반영.

    source_id = problem_id. 단일 problem 레코드의 text/embedding/format을 채운다.
    """
    from apps.domains.ai.models import AIJobModel
    MatchupProblem = get_matchup_problem_model()

    if status == "FAILED":
        logger.warning(
            "AI_CALLBACK_MATCHUP_MANUAL_FAILED | job_id=%s | problem_id=%s | error=%s",
            job_id, source_id, error,
        )
        return

    ai_job = AIJobModel.objects.filter(job_id=job_id).only("tenant_id", "source_id").first()
    if not ai_job or not ai_job.tenant_id or not ai_job.source_id:
        logger.warning(
            "AI_CALLBACK_MATCHUP_MANUAL_NO_JOB_SCOPE | job_id=%s | source_id=%s",
            job_id, source_id,
        )
        return

    problem_id = str(ai_job.source_id)
    payload_problem_id = result_payload.get("problem_id")
    if source_id and str(source_id) != problem_id:
        logger.warning(
            "AI_CALLBACK_MATCHUP_MANUAL_SOURCE_MISMATCH | job_id=%s | job_source_id=%s | callback_source_id=%s",
            job_id, problem_id, source_id,
        )
        return
    if payload_problem_id and str(payload_problem_id) != problem_id:
        logger.warning(
            "AI_CALLBACK_MATCHUP_MANUAL_PAYLOAD_MISMATCH | job_id=%s | job_source_id=%s | payload_problem_id=%s",
            job_id, problem_id, payload_problem_id,
        )
        return

    text = (result_payload.get("text") or "").strip()
    embedding = result_payload.get("embedding")
    image_embedding = result_payload.get("image_embedding")
    fmt = result_payload.get("format") or "choice"

    try:
        scoped_problem_id = int(problem_id)
        scoped_tenant_id = int(ai_job.tenant_id)
    except (TypeError, ValueError):
        logger.warning(
            "AI_CALLBACK_MATCHUP_MANUAL_BAD_SCOPE | job_id=%s | tenant_id=%s | problem_id=%s",
            job_id, ai_job.tenant_id, problem_id,
        )
        return

    try:
        problem = MatchupProblem.objects.get(id=scoped_problem_id, tenant_id=scoped_tenant_id)
    except MatchupProblem.DoesNotExist:
        logger.warning(
            "AI_CALLBACK_MATCHUP_MANUAL_MISSING | job_id=%s | tenant_id=%s | problem_id=%s",
            job_id, scoped_tenant_id, scoped_problem_id,
        )
        return

    update_fields = []
    if text and not (problem.text or "").strip():
        problem.text = text
        update_fields.append("text")
    if embedding is not None:
        problem.embedding = embedding
        update_fields.append("embedding")
    if image_embedding is not None:
        problem.image_embedding = image_embedding
        update_fields.append("image_embedding")

    meta = dict(problem.meta or {})
    if "format" not in meta or meta.get("format") in (None, "", "choice"):
        meta["format"] = fmt
        problem.meta = meta
        update_fields.append("meta")

    if update_fields:
        update_fields.append("updated_at")
        problem.save(update_fields=update_fields)

    logger.info(
        "AI_CALLBACK_MATCHUP_MANUAL_SUCCESS | job_id=%s | problem_id=%s | text_len=%d | has_embedding=%s",
        job_id, problem_id, len(text), embedding is not None,
    )


def detect_stuck_dispatched() -> list[dict]:
    """
    AIJob이 완료되었는데 Submission이 아직 DISPATCHED인 건을 감지한다.
    운영 모니터링/reconcile 전 진단용.
    """
    from datetime import timedelta
    from django.utils import timezone
    from apps.domains.ai.models import AIJobModel
    from academy.adapters.db.django.repositories_submissions import (
        list_stuck_dispatched_submission_ids,
    )

    cutoff = timezone.now() - timedelta(minutes=30)
    stuck = list_stuck_dispatched_submission_ids(cutoff)

    results = []
    for sub_id in stuck:
        ai_job = (
            AIJobModel.objects
            .filter(source_domain="submissions", source_id=str(sub_id))
            .order_by("-created_at")
            .first()
        )
        results.append({
            "submission_id": sub_id,
            "ai_job_id": ai_job.job_id if ai_job else None,
            "ai_job_status": ai_job.status if ai_job else None,
            "stuck": ai_job and ai_job.status in ("DONE", "FAILED", "REJECTED_BAD_INPUT"),
        })

    stuck_count = sum(1 for r in results if r["stuck"])
    if stuck_count > 0:
        logger.error(
            "AI_STUCK_DISPATCHED_DETECTED | count=%d | submissions=%s",
            stuck_count,
            [r["submission_id"] for r in results if r["stuck"]],
        )

    return results
