# PATH: apps/domains/homework_results/views/homework_score_viewset.py
# 역할: HomeworkScore 조회/수정 + quick_patch(upsert)로 점수입력(% or raw/max) 지원

"""
HomeworkScore API (Admin / Teacher)

Endpoint:
- GET    /homework/scores/?enrollment_id=&session=&lecture=&is_locked=
- PATCH  /homework/scores/{id}/
- PATCH  /homework/scores/quick/

설계 계약 (LOCKED):
- Homework 합불(passed)은 PATCH 시점에만 계산
- SessionScores API는 passed / clinic_required 값을 그대로 신뢰

Quick Patch (MVP):
- homework_id + enrollment_id 기반 upsert
- score 입력 방식 2개 지원:
  - percent 직접 입력(score=85, max_score=None)
  - raw/max 입력(score=18, max_score=20)
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.db import transaction
from django.db import IntegrityError
from django.db.models import QuerySet
from django.http import Http404
from django.shortcuts import get_object_or_404

from rest_framework.viewsets import ModelViewSet
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status as drf_status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError

from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import SearchFilter, OrderingFilter

from apps.domains.homework_results.models import HomeworkScore, Homework
from apps.domains.homework_results.serializers import (
    HomeworkScoreSerializer,
    HomeworkQuickPatchSerializer,
)
from apps.domains.homework_results.filters import HomeworkScoreFilter

from apps.core.permissions import TenantResolvedAndStaff
from apps.domains.results.contracts import (
    require_homework_score_edit_lease,
)
from apps.support.homework.review_lock import lock_homework_review_target
from apps.support.homework_results.score_dependencies import (
    calc_homework_passed_and_clinic,
    dispatch_progress_pipeline,
    latest_homework_submission,
    sync_homework_clinic_link,
    validate_enrollment_belongs_to_tenant,
)


logger = logging.getLogger(__name__)


def _safe_user_id(request) -> int | None:
    return getattr(getattr(request, "user", None), "id", None)


def _locked_response(obj: HomeworkScore) -> Response:
    return Response(
        {
            "detail": "score block is locked",
            "code": "LOCKED",
            "lock_reason": getattr(obj, "lock_reason", None),
        },
        status=drf_status.HTTP_409_CONFLICT,
    )


def _cell_conflict_response(
    obj: HomeworkScore | None,
    expected_updated_at,
) -> Response:
    serialized = HomeworkScoreSerializer(obj).data if obj is not None else {}
    meta = serialized.get("meta")
    return Response(
        {
            "detail": "다른 화면에서 이 과제 점수가 먼저 저장되었습니다.",
            "code": "SCORE_CELL_CONFLICT",
            "server_value": {
                "score": serialized.get("score"),
                "max_score": serialized.get("max_score"),
                "meta_status": meta.get("status") if isinstance(meta, dict) else None,
                "updated_at": serialized.get("updated_at"),
            },
            "expected_updated_at": (
                expected_updated_at.isoformat()
                if expected_updated_at is not None
                else None
            ),
        },
        status=drf_status.HTTP_409_CONFLICT,
    )


def _apply_score_and_policy(
    *,
    obj: HomeworkScore,
    score: float | None,
    max_score: float | None,
    request,
    save_fields: list[str],
) -> HomeworkScore:
    """
    HomeworkScore에 점수 반영 + HomeworkPolicy 계산
    """
    configured_max_score = obj.homework.default_max_score
    if (
        obj.homework.grading_mode == Homework.GradingMode.COMPLETION
        and score is not None
        and float(score) not in (0.0, configured_max_score)
    ):
        raise ValidationError(
            {"score": "완료형 과제는 미완료(0) 또는 완료(1)만 저장할 수 있습니다."}
        )
    if score is not None and float(score) > configured_max_score:
        raise ValidationError(
            {
                "score": (
                    f"점수({float(score):g})가 설정된 과제 만점"
                    f"({configured_max_score:g})을 초과할 수 없습니다."
                )
            }
        )

    obj.score = score
    obj.max_score = configured_max_score if score is not None else None

    passed, clinic_required, _ = calc_homework_passed_and_clinic(
        session=obj.session,
        homework=obj.homework,
        score=score,
        max_score=obj.max_score,
    )

    obj.passed = bool(passed)
    obj.clinic_required = bool(clinic_required)
    obj.updated_by_user_id = _safe_user_id(request)

    obj.save(update_fields=save_fields + ["updated_at"])
    return obj


def _maybe_fix_submission(
    submission,
    *,
    score_obj: HomeworkScore,
    teacher_approved: bool | None,
) -> None:
    """
    Submission 구조는 프로젝트마다 다를 수 있으므로
    "필드가 존재할 때만" 방어적으로 보정한다.
    """
    if hasattr(submission, "homework_submitted"):
        submission.homework_submitted = True

    if hasattr(submission, "homework_teacher_approved") and teacher_approved is not None:
        submission.homework_teacher_approved = bool(teacher_approved)

    if hasattr(submission, "meta"):
        meta = submission.meta if isinstance(submission.meta, dict) else {}
        meta = {**meta}

        homework_meta = meta.get("homework")
        if not isinstance(homework_meta, dict):
            homework_meta = {}

        homework_meta.update(
            {
                "homework_score_id": score_obj.id,
                "homework_id": score_obj.homework_id,
                "score": score_obj.score,
                "max_score": score_obj.max_score,
                "passed": score_obj.passed,
                "clinic_required": score_obj.clinic_required,
                "teacher_approved": getattr(
                    submission, "homework_teacher_approved", None
                ),
            }
        )

        meta["homework"] = homework_meta
        submission.meta = meta

    update_fields = ["updated_at"]
    for f in ["homework_submitted", "homework_teacher_approved", "meta"]:
        if hasattr(submission, f):
            update_fields.append(f)

    submission.save(update_fields=list(dict.fromkeys(update_fields)))


class HomeworkScoreViewSet(ModelViewSet):
    """
    HomeworkScore 관리 ViewSet
    """

    serializer_class = HomeworkScoreSerializer
    permission_classes = [IsAuthenticated, TenantResolvedAndStaff]

    def get_queryset(self) -> QuerySet[HomeworkScore]:
        return (
            HomeworkScore.objects
            .select_related("session", "session__lecture", "homework")
            .filter(
                session__lecture__tenant=self.request.tenant,
                attempt_index=1,  # 성적 탭은 1차(성적 산출 대상)만 조회
            )
        )

    filter_backends = [
        DjangoFilterBackend,
        SearchFilter,
        OrderingFilter,
    ]
    filterset_class = HomeworkScoreFilter

    search_fields = [
        "enrollment_id",
        "session__title",
        "session__lecture__title",
        "homework__title",
    ]

    ordering_fields = [
        "id",
        "created_at",
        "updated_at",
        "is_locked",
        "score",
        "passed",
    ]
    ordering = ["-updated_at", "-id"]

    # =================================================
    # PATCH /homework/scores/{id}/
    # =================================================
    @transaction.atomic
    def partial_update(self, request, *args, **kwargs):
        obj: HomeworkScore = self.get_object()

        validate_enrollment_belongs_to_tenant(obj.enrollment_id, request.tenant)
        assignment = lock_homework_review_target(
            tenant=request.tenant,
            session_id=obj.session_id,
            enrollment_id=obj.enrollment_id,
            homework_id=obj.homework_id,
        )
        if assignment is None:
            return Response(
                {"enrollment_id": "이 과제의 배정 대상 수강생만 점수를 입력할 수 있습니다."},
                status=drf_status.HTTP_400_BAD_REQUEST,
            )
        require_homework_score_edit_lease(
            request,
            session_id=obj.session_id,
            enrollment_id=obj.enrollment_id,
            homework_id=obj.homework_id,
        )
        obj = self.get_queryset().select_for_update(of=("self",)).get(pk=obj.pk)

        if getattr(obj, "is_locked", False):
            return _locked_response(obj)

        serializer = self.get_serializer(obj, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)

        vd = serializer.validated_data
        status_marker = object()
        meta_status = vd.pop("status", status_marker)
        next_score = vd.get("score", obj.score)
        next_max = vd.get("max_score", obj.max_score)
        if meta_status == HomeworkScore.MetaStatus.NOT_SUBMITTED:
            next_score = None
            next_max = None
        teacher_approved = vd.get("teacher_approved")

        progress_info = {"dispatched": False, "reason": None}

        with transaction.atomic():
            serializer.save(
                passed=obj.passed,
                clinic_required=obj.clinic_required,
                updated_by_user_id=_safe_user_id(request),
            )

            score_obj: HomeworkScore = serializer.instance
            if meta_status is not status_marker:
                meta = dict(score_obj.meta or {})
                if meta_status == HomeworkScore.MetaStatus.NOT_SUBMITTED:
                    meta["status"] = HomeworkScore.MetaStatus.NOT_SUBMITTED
                else:
                    meta.pop("status", None)
                score_obj.meta = meta or None
                score_obj.save(update_fields=["meta", "updated_at"])

            score_obj = _apply_score_and_policy(
                obj=score_obj,
                score=next_score,
                max_score=next_max,
                request=request,
                save_fields=[
                    "score",
                    "max_score",
                    "passed",
                    "clinic_required",
                    "updated_by_user_id",
                ],
            )

            submission = latest_homework_submission(
                enrollment_id=score_obj.enrollment_id,
                homework_id=score_obj.homework_id,
            )

            if submission:
                _maybe_fix_submission(
                    submission,
                    score_obj=score_obj,
                    teacher_approved=teacher_approved,
                )

                sub_id = int(submission.id)

                def _dispatch():
                    try:
                        dispatch_progress_pipeline(submission_id=sub_id)
                    except Exception:
                        pass

                transaction.on_commit(_dispatch)
                progress_info = {"dispatched": True, "reason": None}
            else:
                progress_info = {"dispatched": False, "reason": "NO_SUBMISSION"}

            try:
                with transaction.atomic():
                    sync_homework_clinic_link(
                        enrollment_id=score_obj.enrollment_id,
                        session=score_obj.session,
                        homework_id=score_obj.homework_id,
                        passed=bool(score_obj.passed),
                        score=score_obj.score,
                        max_score=score_obj.max_score,
                    )
            except Exception:
                logger.exception(
                    "partial_update: clinic link sync failed (hw=%s, enrollment=%s)",
                    score_obj.homework_id, score_obj.enrollment_id,
                )

        data = self.get_serializer(score_obj).data
        data["progress"] = progress_info

        return Response(data, status=drf_status.HTTP_200_OK)

    # =================================================
    # PATCH /homework/scores/quick/
    # =================================================
    @action(detail=False, methods=["patch"], url_path="quick")
    @transaction.atomic
    def quick_patch(self, request):
        try:
            serializer = HomeworkQuickPatchSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)

            homework_id = serializer.validated_data["homework_id"]
            enrollment_id = serializer.validated_data["enrollment_id"]
            requested_session_id = serializer.validated_data.get("session_id")

            validate_enrollment_belongs_to_tenant(enrollment_id, request.tenant)

            score = serializer.validated_data.get("score")
            max_score = serializer.validated_data.get("max_score")
            meta_status = serializer.validated_data.get("meta_status")
            expected_version_provided = "expected_updated_at" in serializer.validated_data
            expected_updated_at = serializer.validated_data.get("expected_updated_at")

            homework = get_object_or_404(
                Homework.objects.select_related(
                    "session",
                    "session__lecture",
                    "session__lecture__tenant",
                ).filter(tenant=self.request.tenant),
                id=homework_id,
            )
            session = homework.session
            if session is None:
                return Response(
                    {"detail": "템플릿 과제는 점수를 입력할 수 없습니다."},
                    status=drf_status.HTTP_400_BAD_REQUEST,
                )
            if getattr(session.lecture, "tenant_id", None) != request.tenant.id:
                raise Http404
            if requested_session_id is not None and requested_session_id != session.id:
                return Response(
                    {"session_id": "과제의 차시와 요청 차시가 일치하지 않습니다."},
                    status=drf_status.HTTP_400_BAD_REQUEST,
                )
            assignment = lock_homework_review_target(
                tenant=request.tenant,
                session_id=session.id,
                enrollment_id=enrollment_id,
                homework_id=homework_id,
            )
            legacy_score = None
            if assignment is None:
                # HomeworkAssignment 도입 전 생성된 점수행은 학생 상세 이력에
                # 계속 노출된다. 새 미배정 점수 생성은 거부하되, 이미 존재하는
                # 정확한 1차 점수행은 그 행 자체를 잠근 뒤 수정할 수 있게 한다.
                legacy_score = (
                    HomeworkScore.objects.select_for_update()
                    .filter(
                        homework=homework,
                        session=session,
                        enrollment_id=enrollment_id,
                        attempt_index=1,
                    )
                    .select_related("session", "homework")
                    .first()
                )
                if legacy_score is None:
                    return Response(
                        {"enrollment_id": "이 과제의 배정 대상 수강생만 점수를 입력할 수 있습니다."},
                        status=drf_status.HTTP_400_BAD_REQUEST,
                    )
            require_homework_score_edit_lease(
                request,
                session_id=session.id,
                enrollment_id=enrollment_id,
                homework_id=homework_id,
            )

            with transaction.atomic():
                obj = legacy_score
                if obj is None:
                    obj = (
                        HomeworkScore.objects.select_for_update()
                        .filter(
                            homework_id=homework_id,
                            session=session,
                            enrollment_id=enrollment_id,
                            attempt_index=1,
                        )
                        .select_related("session", "homework")
                        .first()
                    )

                if obj and obj.is_locked:
                    return _locked_response(obj)

                if expected_version_provided:
                    if obj is None and expected_updated_at is not None:
                        return _cell_conflict_response(None, expected_updated_at)
                    if obj is not None and obj.updated_at != expected_updated_at:
                        return _cell_conflict_response(obj, expected_updated_at)

                if not obj:
                    try:
                        with transaction.atomic():
                            obj = HomeworkScore.objects.create(
                                homework=homework,
                                session=session,
                                enrollment_id=enrollment_id,
                                attempt_index=1,
                                score=None,
                                max_score=None,
                                updated_by_user_id=_safe_user_id(request),
                            )
                    except IntegrityError:
                        obj = (
                            HomeworkScore.objects.filter(
                                homework_id=homework_id,
                                session=session,
                                enrollment_id=enrollment_id,
                                attempt_index=1,
                            )
                            .select_related("session", "homework")
                            .first()
                        )
                        if not obj:
                            raise
                        if obj.is_locked:
                            return _locked_response(obj)
                        if expected_version_provided:
                            return _cell_conflict_response(obj, expected_updated_at)

                if meta_status == HomeworkScore.MetaStatus.NOT_SUBMITTED:
                    obj.meta = {"status": HomeworkScore.MetaStatus.NOT_SUBMITTED}
                    obj.score = None
                    obj.max_score = None
                    obj.updated_by_user_id = _safe_user_id(request)
                    passed, clinic_required, _ = calc_homework_passed_and_clinic(
                        session=obj.session,
                        homework=obj.homework,
                        score=None,
                        max_score=None,
                    )
                    obj.passed = bool(passed)
                    obj.clinic_required = bool(clinic_required)
                    obj.save(update_fields=["meta", "score", "max_score", "passed", "clinic_required", "updated_by_user_id", "updated_at"])
                else:
                    obj = _apply_score_and_policy(
                        obj=obj,
                        score=score,
                        max_score=max_score,
                        request=request,
                        save_fields=[
                            "score",
                            "max_score",
                            "passed",
                            "clinic_required",
                            "updated_by_user_id",
                        ],
                    )
                    if obj.meta and isinstance(obj.meta, dict) and obj.meta.get("status") == HomeworkScore.MetaStatus.NOT_SUBMITTED:
                        obj.meta = {k: v for k, v in obj.meta.items() if k != "status"}
                        if not obj.meta:
                            obj.meta = None
                        obj.save(update_fields=["meta", "updated_at"])

                try:
                    with transaction.atomic():
                        sync_homework_clinic_link(
                            enrollment_id=enrollment_id,
                            session=session,
                            homework_id=homework_id,
                            passed=bool(obj.passed),
                            score=obj.score,
                            max_score=obj.max_score,
                        )
                except Exception:
                    logger.exception(
                        "quick_patch: clinic link sync failed (hw=%s, enrollment=%s)",
                        homework_id, enrollment_id,
                    )

            return Response(
                HomeworkScoreSerializer(obj).data,
                status=drf_status.HTTP_200_OK,
            )
        except (Http404, ValidationError):
            raise
        except Exception as e:
            logger.exception("quick_patch failed: %s", e)
            if getattr(settings, "DEBUG", False):
                return Response(
                    {
                        "detail": str(e),
                        "type": type(e).__name__,
                        "hint": "백엔드 콘솔에 전체 traceback이 출력됩니다. DEBUG=True로 실행 중인지 확인하세요.",
                    },
                    status=drf_status.HTTP_500_INTERNAL_SERVER_ERROR,
                )
            raise
