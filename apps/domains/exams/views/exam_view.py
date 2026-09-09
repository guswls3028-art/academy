from __future__ import annotations

from django.db import transaction
from django.db.models import Exists, Max, OuterRef
from django.shortcuts import get_object_or_404

from rest_framework.viewsets import ModelViewSet
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import ValidationError, PermissionDenied
from rest_framework.response import Response

from apps.core.permissions import TenantResolvedAndMember
from apps.api.common.query_params import parse_query_bool, parse_query_int
from apps.core.optimistic_concurrency import assert_expected_updated_at
from apps.domains.exams.models import Exam, ExamEnrollment
from apps.domains.exams.serializers.exam import ExamSerializer
from apps.domains.exams.serializers.exam_create import ExamCreateSerializer
from apps.domains.exams.serializers.exam_update import ExamUpdateSerializer
from apps.domains.exams.services.structure_copy_service import (
    copy_exam_structure,
    ensure_regular_exam_owns_structure,
)
from apps.domains.exams.services.template_resolver import resolve_structure_exam
from apps.support.exams.view_dependencies import (
    IsTeacherOrAdmin,
    active_enrollment_ids_for_exam_assignment,
    dispatch_progress_for_exam,
    highest_current_or_initial_exam_score,
    refresh_exam_target_projections,
    get_session_model,
    regular_exam_delete_blocker,
    resolve_removed_exam_clinic_links,
)


class ExamViewSet(ModelViewSet):
    """
    Exam 생성/조회/수정/삭제 API (봉인)

    봉인 규칙:
    - create/update/delete는 Teacher/Admin만
    - template: subject 필수, session_id/template_exam_id 입력 금지
    - regular: session_id 필수, template_exam_id는 양식 연결용 옵션
    - update/patch에서 exam_type/subject/template_exam 변경 시도는 즉시 400
    - template 삭제: derived regular 존재 시 금지
    """

    queryset = Exam.objects.all()
    permission_classes = [IsAuthenticated, TenantResolvedAndMember]

    # ================================
    # Serializer 선택
    # ================================
    def get_serializer_class(self):
        if self.action == "create":
            return ExamCreateSerializer
        if self.action in {"update", "partial_update"}:
            return ExamUpdateSerializer
        return ExamSerializer

    # ================================
    # Permissions
    # ================================
    def get_permissions(self):
        if self.action in {"list", "retrieve"}:
            # 다른 도메인과 일관: list/retrieve도 테넌트 멤버 검증을 거쳐야 한다
            # (queryset이 테넌트 스코프이지만, 헤더 기반 X-Tenant-Code 변조 시
            #  비멤버 인증사용자가 도달할 수 있어 1차 게이트를 추가).
            return [IsAuthenticated(), TenantResolvedAndMember()]
        return [IsAuthenticated(), TenantResolvedAndMember(), IsTeacherOrAdmin()]

    # ================================
    # 🔥 핵심 FIX: create 응답을 read serializer로
    # ================================
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        self.perform_create(serializer)
        instance = serializer.instance

        # ✅ 응답은 반드시 read serializer
        read_serializer = ExamSerializer(instance)
        headers = self.get_success_headers(read_serializer.data)

        return Response(
            read_serializer.data,
            status=201,
            headers=headers,
        )

    # ================================
    # Immutable 필드 방어
    # ================================
    def _reject_immutable_fields_on_update(self, request):
        forbidden = {"exam_type", "subject"}
        incoming = set(request.data.keys())
        bad = sorted(list(incoming & forbidden))
        if bad:
            raise ValidationError(
                {"detail": f"Immutable fields in update are forbidden: {bad}"}
            )
        # template_exam_id는 regular 생성 시 미지정 가능 → 시험 설정에서 한 번 지정 허용

    # ================================
    # CREATE 로직
    # ================================
    def perform_create(self, serializer):
        tenant = getattr(self.request, "tenant", None)
        if not tenant:
            raise PermissionDenied("Tenant is required.")

        exam_type = serializer.validated_data.get("exam_type")

        # =========================
        # TEMPLATE CREATE
        # =========================
        if exam_type == Exam.ExamType.TEMPLATE:
            if self.request.data.get("session_id"):
                raise ValidationError(
                    {"session_id": "template exam must not receive session_id"}
                )
            if self.request.data.get("template_exam_id"):
                raise ValidationError(
                    {"template_exam_id": "template exam must not receive template_exam_id"}
                )

            serializer.save(
                exam_type=Exam.ExamType.TEMPLATE,
                template_exam=None,
                tenant=tenant,
            )
            return

        # =========================
        # REGULAR CREATE
        # =========================
        template_exam_id = self.request.data.get("template_exam_id")
        source_exam_id = self.request.data.get("source_exam_id")
        if template_exam_id and source_exam_id:
            raise ValidationError(
                {"source_exam_id": "template_exam_id와 함께 사용할 수 없습니다."}
            )

        template_exam = None
        source_exam = None
        subject = ""

        if template_exam_id:
            try:
                template_exam_id = int(template_exam_id)
            except (TypeError, ValueError):
                raise ValidationError({"template_exam_id": "must be integer"})

            try:
                template_exam = Exam.objects.filter(tenant=tenant).get(id=template_exam_id)
            except Exam.DoesNotExist:
                raise ValidationError({"template_exam_id": "invalid"})
            if template_exam.exam_type != Exam.ExamType.TEMPLATE:
                raise ValidationError({"template_exam_id": "must be template exam"})
            subject = template_exam.subject

        if source_exam_id:
            try:
                source_exam_id = int(source_exam_id)
            except (TypeError, ValueError):
                raise ValidationError({"source_exam_id": "must be integer"})

            try:
                source_exam = Exam.objects.filter(
                    tenant=tenant,
                    exam_type=Exam.ExamType.REGULAR,
                    is_active=True,
                ).get(id=source_exam_id)
            except Exam.DoesNotExist:
                raise ValidationError({"source_exam_id": "invalid"})
            subject = source_exam.subject

        session_id = self.request.data.get("session_id")
        if not session_id:
            raise ValidationError({"session_id": "required"})

        try:
            session_id = int(session_id)
        except (TypeError, ValueError):
            raise ValidationError({"session_id": "must be integer"})

        with transaction.atomic():
            Session = get_session_model()
            try:
                session = (
                    Session.objects
                    .select_for_update(of=("self",), no_key=True)
                    .select_related("lecture")
                    .get(id=session_id, lecture__tenant=tenant)
                )
            except Session.DoesNotExist:
                raise ValidationError({"session_id": "invalid"})

            # 템플릿 없이 생성 시 강의(Lecture) 과목을 시험 과목으로 자동 반영
            if not subject and getattr(session, "lecture", None):
                subject = (getattr(session.lecture, "subject", None) or "").strip()

            max_order = (
                Exam.objects
                .filter(tenant=tenant, sessions=session)
                .aggregate(value=Max("display_order"))
                .get("value")
            )
            exam = serializer.save(
                exam_type=Exam.ExamType.REGULAR,
                subject=subject,
                template_exam=template_exam,
                tenant=tenant,
                display_order=int(max_order or 0) + 1,
            )

            with refresh_exam_target_projections(exam=exam):
                exam.sessions.add(session)
                active_enrollment_ids = active_enrollment_ids_for_exam_assignment(
                    tenant=tenant,
                    session=session,
                )
                ExamEnrollment.objects.bulk_create(
                    [
                        ExamEnrollment(exam=exam, enrollment_id=enrollment_id)
                        for enrollment_id in active_enrollment_ids
                    ],
                    ignore_conflicts=True,
                )
            if template_exam is not None:
                copy_exam_structure(source_exam=template_exam, target_exam=exam)
            elif source_exam is not None:
                structure_source = resolve_structure_exam(source_exam)
                if int(structure_source.tenant_id) != int(tenant.id):
                    raise ValidationError({"source_exam_id": "invalid"})
                copy_exam_structure(source_exam=structure_source, target_exam=exam)

    # ================================
    # UPDATE 방어 + pass_score 변경 시 ClinicLink 해소 재계산
    # ================================
    def perform_update(self, serializer):
        exam = serializer.save()
        if exam.exam_type == Exam.ExamType.REGULAR and exam.template_exam_id:
            ensure_regular_exam_owns_structure(exam)

    def update(self, request, *args, **kwargs):
        self._reject_immutable_fields_on_update(request)
        return self._update_with_recalc(request, partial=False, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        self._reject_immutable_fields_on_update(request)
        return self._update_with_recalc(request, partial=True, **kwargs)

    def _update_with_recalc(self, request, *, partial: bool, **kwargs):
        """
        2026-05-13: pass_score 변경 시 progress pipeline 재실행.
        ClinicTriggerService.auto_create_per_exam 가 exam_meta.passed 기준으로
        ClinicLink 생성/해소를 idempotent 하게 처리하므로, pipeline 만 트리거하면
        하향(예: 70→50) 시 PASS 학생의 미해소 ClinicLink 가 자동 해소됨.
        """
        with transaction.atomic():
            obj: Exam = get_object_or_404(
                self.filter_queryset(self.get_queryset().select_for_update()),
                pk=kwargs["pk"],
            )
            self.check_object_permissions(request, obj)
            assert_expected_updated_at(request=request, instance=obj)
            prev_pass = float(getattr(obj, "pass_score", 0) or 0)

            serializer = self.get_serializer(
                obj,
                data=request.data,
                partial=partial,
            )
            serializer.is_valid(raise_exception=True)
            next_max_score = serializer.validated_data.get("max_score")
            if (
                next_max_score is not None
                and float(next_max_score) < float(obj.max_score or 0)
            ):
                highest_score = highest_current_or_initial_exam_score(exam=obj)
                if highest_score is not None and highest_score > float(next_max_score):
                    raise ValidationError(
                        {
                            "max_score": (
                                f"현재 대표 또는 1차 점수({highest_score:g})보다 "
                                "낮게 설정할 수 없습니다. 점수를 먼저 수정해 주세요."
                            )
                        }
                    )
            self.perform_update(serializer)
            updated = serializer.instance
            response = Response(
                ExamSerializer(
                    updated,
                    context=self.get_serializer_context(),
                ).data
            )

        try:
            new_pass = response.data.get("pass_score") if hasattr(response, "data") else None
            if prev_pass is not None and new_pass is not None and float(new_pass) != prev_pass:
                exam_id_for_pipeline = int(response.data.get("id") or kwargs.get("pk") or 0)
                if exam_id_for_pipeline:
                    dispatch_progress_for_exam(exam_id=exam_id_for_pipeline)
        except Exception:
            # progress pipeline 실패해도 update 자체는 유지 (응답 반영됨)
            import logging
            logging.getLogger(__name__).exception(
                "ExamViewSet update: progress dispatch after pass_score change failed"
            )

        return response

    # ================================
    # DELETE 봉인
    # ================================
    def _regular_delete_blocker(self, obj: Exam) -> str | None:
        return regular_exam_delete_blocker(obj)

    def _delete_session_id(self, request) -> int | None:
        return parse_query_int(request.query_params, "session_id", min_value=1)

    def _session_for_delete(self, request, obj: Exam, session_id: int):
        Session = get_session_model()
        session = Session.objects.filter(
            id=session_id,
            lecture__tenant=request.tenant,
        ).first()
        if session is None:
            raise ValidationError({"session_id": "invalid"})
        if not obj.sessions.filter(id=session_id).exists():
            raise ValidationError(
                {"session_id": "exam is not linked to this session"}
            )
        return session

    def _resolve_removed_exam_clinic_links(self, request, obj: Exam, session_id: int) -> int:
        return resolve_removed_exam_clinic_links(
            tenant_id=int(request.tenant.id),
            session_id=int(session_id),
            exam_id=int(obj.id),
            user_id=getattr(request.user, "id", None),
        )

    def _unlink_from_session(
        self,
        request,
        obj: Exam,
        session_id: int,
        *,
        preserve_history: bool = False,
        blocker: str | None = None,
    ):
        session = self._session_for_delete(request, obj, session_id)

        has_other_sessions = obj.sessions.exclude(id=session_id).exists()
        removed_clinic_link_count = self._resolve_removed_exam_clinic_links(
            request,
            obj,
            session_id,
        )
        if has_other_sessions:
            obj.sessions.remove(session)
            return Response(
                {
                    "detail": "Exam was removed from this session.",
                    "action": "unlinked",
                    "exam_id": int(obj.id),
                    "session_id": int(session_id),
                    "removed_clinic_link_count": int(removed_clinic_link_count),
                },
                status=200,
            )

        if preserve_history:
            obj.sessions.remove(session)
            enrollment_count, _ = ExamEnrollment.objects.filter(exam=obj).delete()
            if obj.is_active:
                obj.is_active = False
                obj.save(update_fields=["is_active", "updated_at"])
            return Response(
                {
                    "detail": "Exam was removed from this session and historical records were preserved.",
                    "action": "archived",
                    "exam_id": int(obj.id),
                    "session_id": int(session_id),
                    "preserved_blocker": blocker,
                    "removed_enrollment_count": int(enrollment_count),
                    "removed_clinic_link_count": int(removed_clinic_link_count),
                },
                status=200,
            )
        return None

    def destroy(self, request, *args, **kwargs):
        obj: Exam = self.get_object()
        session_id = self._delete_session_id(request)

        if session_id is not None and obj.exam_type != Exam.ExamType.REGULAR:
            raise ValidationError(
                {"session_id": "session-scoped delete is allowed only for regular exams"}
            )

        if obj.exam_type == Exam.ExamType.TEMPLATE and obj.derived_exams.exists():
            raise PermissionDenied(
                "This template is used by regular exams and cannot be deleted."
            )

        if obj.exam_type == Exam.ExamType.REGULAR:
            if session_id is not None:
                with transaction.atomic():
                    blocker = self._regular_delete_blocker(obj)
                    response = self._unlink_from_session(
                        request,
                        obj,
                        session_id,
                        preserve_history=bool(blocker),
                        blocker=blocker,
                    )
                    if response is not None:
                        return response
                    return super().destroy(request, *args, **kwargs)

            blocker = self._regular_delete_blocker(obj)
            if blocker:
                raise PermissionDenied(
                    f"This regular exam has {blocker} and cannot be deleted."
                )

        return super().destroy(request, *args, **kwargs)

    # ================================
    # Query Filters
    # ================================
    def get_queryset(self):
        tenant = getattr(self.request, "tenant", None)
        if not tenant:
            return Exam.objects.none()
        # Exam.tenant is the ownership SSOT. Joining through sessions here both
        # admitted a cross-tenant fallback and forced DISTINCT, which PostgreSQL
        # cannot combine with the SELECT FOR UPDATE used by policy saves.
        qs = Exam.objects.filter(tenant=tenant)

        exam_type = self.request.query_params.get("exam_type")
        if exam_type:
            exam_type = str(exam_type).lower()
            qs = qs.filter(exam_type=exam_type)

        session_id = parse_query_int(
            self.request.query_params, "session_id", min_value=1
        )
        if session_id is not None:
            qs = qs.filter(sessions__id=session_id)

        lecture_id = parse_query_int(
            self.request.query_params, "lecture_id", min_value=1
        )
        if lecture_id is not None:
            # A regular exam may be linked to more than one session in the
            # same lecture. Filtering through the M2M join duplicates that
            # exam in list/count responses. EXISTS keeps one Exam row and is
            # also compatible with the row lock used by policy updates.
            matching_session = Exam.sessions.through.objects.filter(
                exam_id=OuterRef("pk"),
                session__lecture_id=lecture_id,
                session__lecture__tenant=self.request.tenant,
            )
            qs = qs.annotate(
                _matches_lecture=Exists(matching_session),
            ).filter(_matches_lecture=True)

        include_inactive = parse_query_bool(
            self.request.query_params, "include_inactive", default=False
        )
        if session_id or lecture_id:
            qs = qs.filter(
                exam_type=Exam.ExamType.REGULAR,
                is_active=True,
            )
        elif exam_type == Exam.ExamType.REGULAR and not include_inactive:
            qs = qs.filter(is_active=True)
        elif not exam_type and not include_inactive:
            qs = qs.exclude(
                exam_type=Exam.ExamType.REGULAR,
                is_active=False,
            )

        if session_id:
            return qs.order_by("display_order", "created_at", "id")

        return qs.order_by("-created_at", "-id")
