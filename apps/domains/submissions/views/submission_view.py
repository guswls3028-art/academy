# PATH: apps/domains/submissions/views/submission_view.py
from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.viewsets import ModelViewSet
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import ValidationError

from apps.api.common.upload_validation import (
    DEFAULT_MAX_OMR_SIZE,
    OMR_CONTENT_TYPES,
    OMR_EXTENSIONS,
    validate_uploaded_file,
)
from apps.core.permissions import TenantResolvedAndMember, TenantResolvedAndStaff
from apps.domains.submissions.models import OMRStudentMatch, Submission, SubmissionAnswer
from apps.domains.submissions.serializers.submission import (
    SubmissionUploadCleanupRequired,
    SubmissionSerializer,
    SubmissionCreateSerializer,
)
from apps.domains.submissions.services.dispatcher import (
    dispatch_submission,
    resolve_omr_sheet_for_exam,
)
from apps.domains.submissions.services.omr_submission_guards import (
    OMR_CONFLICT_STATUSES,
    allow_duplicate_requested,
    duplicate_conflict_payload,
    ensure_exam_enrollment_candidate,
    find_conflicting_exam_submission,
)
from apps.domains.submissions.services.lifecycle import (
    InvalidTransitionError,
    ensure_ai_submission_key_attachable,
    fail_submission,
    retry_failed_submission,
    schedule_unreferenced_ai_object_cleanup,
    supersede_submission,
)
from apps.support.submissions.dependencies import (
    allowed_manual_exam_question_ids,
    create_exam_enrollment_assignment,
    enrollment_belongs_to_tenant,
    exam_question_number_by_id,
    get_synced_exam_score,
    rebind_representative_omr_submission,
    request_is_parent,
    student_owns_enrollment,
    target_belongs_to_tenant,
    target_enrollment_assignment_exists,
    validate_exam_enrollment_candidate,
)


logger = logging.getLogger(__name__)


class SubmissionViewSet(ModelViewSet):
    # 기본: Member (학생 CREATE 허용). 관리자 전용 액션은 get_permissions에서 Staff로 승격.
    permission_classes = [IsAuthenticated, TenantResolvedAndMember]

    # 학생/학부모에게도 열린 액션 (본인 소유 생성만). 그 외는 Staff-only.
    STUDENT_ALLOWED_ACTIONS = {"create"}

    def get_permissions(self):
        if self.action in self.STUDENT_ALLOWED_ACTIONS:
            return [IsAuthenticated(), TenantResolvedAndMember()]
        return [IsAuthenticated(), TenantResolvedAndStaff()]

    def get_queryset(self):
        tenant = getattr(self.request, "tenant", None)
        if not tenant:
            return Submission.objects.none()
        qs = Submission.objects.filter(tenant=tenant).order_by("-id")
        # 학생/학부모는 자기 제출만 볼 수 있음 — 프로필 존재로 판별
        # (과거 tenant_role 기반 분기는 해당 플래그가 어디에서도 설정되지 않아 항상 미적용 상태였다.)
        user = self.request.user
        is_student_or_parent = (
            getattr(user, "student_profile", None) is not None
            or getattr(user, "parent_profile", None) is not None
        )
        if is_student_or_parent:
            qs = qs.filter(user=user)
        return qs

    def get_serializer_class(self):
        if self.action in ("create", "admin_omr_upload"):
            return SubmissionCreateSerializer
        return SubmissionSerializer

    def create(self, request, *args, **kwargs):
        try:
            return super().create(request, *args, **kwargs)
        except SubmissionUploadCleanupRequired as exc:
            schedule_unreferenced_ai_object_cleanup(
                tenant_id=exc.tenant_id,
                key=exc.key,
            )
            return Response(
                {
                    "code": "submission_storage_cleanup_pending",
                    "detail": "업로드 정리를 예약했습니다. 잠시 후 다시 시도해 주세요.",
                },
                status=503,
            )

    @extend_schema(
        request=inline_serializer(
            name="OmrRotateRescanRequest",
            fields={
                "rotation_degrees": serializers.ChoiceField(choices=[90, 180, 270]),
                "client_request_id": serializers.CharField(max_length=128),
            },
        ),
        responses={
            200: inline_serializer(
                name="OmrRotateRescanResult",
                fields={
                    "submission_id": serializers.IntegerField(),
                    "status": serializers.CharField(),
                    "rotation_degrees": serializers.ChoiceField(choices=[90, 180, 270]),
                    "created": serializers.BooleanField(),
                },
            ),
            201: inline_serializer(
                name="OmrRotateRescanCreatedResult",
                fields={
                    "submission_id": serializers.IntegerField(),
                    "status": serializers.CharField(),
                    "rotation_degrees": serializers.ChoiceField(choices=[90, 180, 270]),
                    "created": serializers.BooleanField(),
                },
            ),
            409: inline_serializer(
                name="OmrRotateRescanConflict",
                fields={
                    "detail": serializers.CharField(),
                    "code": serializers.CharField(),
                },
            ),
        },
    )
    @action(detail=True, methods=["post"], url_path="rotate-rescan")
    def rotate_rescan(self, request, pk=None):
        """Re-run an immutable OMR source with an explicit cardinal rotation."""
        tenant = getattr(request, "tenant", None)
        if tenant is None:
            return Response({"detail": "Tenant required"}, status=403)
        try:
            rotation = int(request.data.get("rotation_degrees"))
        except (TypeError, ValueError):
            return Response({"detail": "rotation_degrees must be 90, 180, or 270"}, status=400)
        if rotation not in (90, 180, 270):
            return Response({"detail": "rotation_degrees must be 90, 180, or 270"}, status=400)
        client_request_id = str(request.data.get("client_request_id") or "").strip()
        if not client_request_id or len(client_request_id) > 128:
            return Response({"detail": "client_request_id is required"}, status=400)

        with transaction.atomic():
            source = Submission.objects.select_for_update().filter(pk=pk, tenant=tenant).first()
            if source is None:
                return Response({"detail": "답안지를 찾을 수 없습니다."}, status=404)
            if (
                source.source != Submission.Source.OMR_SCAN
                or source.target_type != Submission.TargetType.EXAM
                or not source.file_key
            ):
                return Response({"detail": "원본 파일이 있는 OMR 시험 답안지만 다시 읽을 수 있습니다."}, status=400)

            existing = (
                Submission.objects
                .filter(
                    tenant=tenant,
                    source=Submission.Source.OMR_SCAN,
                    target_type=Submission.TargetType.EXAM,
                    target_id=int(source.target_id),
                    payload__omr_rotation_request__client_request_id=client_request_id,
                    payload__omr_rotation_request__source_submission_id=int(source.id),
                )
                .order_by("id")
                .first()
            )
            if existing is not None:
                existing_request = (
                    existing.payload.get("omr_rotation_request", {})
                    if isinstance(existing.payload, dict)
                    else {}
                )
                existing_rotation = int(existing_request.get("rotation_degrees") or 0)
                if existing_rotation != rotation:
                    return Response(
                        {
                            "detail": "client_request_id가 다른 회전 요청에 이미 사용되었습니다.",
                            "code": "IDEMPOTENCY_PAYLOAD_MISMATCH",
                        },
                        status=409,
                    )
                return Response(
                    {
                        "submission_id": int(existing.id),
                        "status": existing.status,
                        "rotation_degrees": existing_rotation,
                        "created": False,
                    },
                    status=200,
                )

            payload = dict(source.payload or {})
            payload["manual_rotation_degrees"] = rotation
            payload["omr_rotation_request"] = {
                "client_request_id": client_request_id,
                "source_submission_id": int(source.id),
                "rotation_degrees": rotation,
                "requested_by_user_id": getattr(request.user, "id", None),
                "requested_at": timezone.now().isoformat(),
            }
            try:
                ensure_ai_submission_key_attachable(
                    tenant_id=tenant.id,
                    key=str(source.file_key),
                )
            except ValueError:
                return Response(
                    {
                        "code": "submission_storage_cleanup_conflict",
                        "detail": "정리 중인 원본 파일은 다시 사용할 수 없습니다.",
                    },
                    status=409,
                )
            replacement = Submission.objects.create(
                tenant=tenant,
                user=source.user,
                enrollment=source.enrollment,
                target_type=source.target_type,
                target_id=source.target_id,
                source=Submission.Source.OMR_SCAN,
                file_key=source.file_key,
                file_type=source.file_type,
                file_size=source.file_size,
                payload=payload,
                meta={
                    "rotation_rescan": {
                        "source_submission_id": int(source.id),
                        "rotation_degrees": rotation,
                        "client_request_id": client_request_id,
                    }
                },
            )
            dispatch_submission(replacement)

        return Response(
            {
                "submission_id": int(replacement.id),
                "status": replacement.status,
                "rotation_degrees": rotation,
                "created": True,
            },
            status=201,
        )

    @action(detail=False, methods=["post"], url_path="admin/omr-upload")
    def admin_omr_upload(self, request):
        """
        POST /api/v1/submissions/submissions/admin/omr-upload/
        form-data: enrollment_id, target_id (exam_id), file
        """
        tenant = getattr(request, "tenant", None)
        if not tenant:
            return Response({"detail": "Tenant required"}, status=403)

        enrollment_id = request.data.get("enrollment_id")
        target_id = request.data.get("target_id")
        file_obj = request.FILES.get("file")

        if not target_id:
            return Response({"detail": "target_id (exam_id) required"}, status=400)
        if not file_obj:
            return Response({"detail": "file required"}, status=400)
        try:
            validate_uploaded_file(
                file_obj,
                allowed_extensions=OMR_EXTENSIONS,
                allowed_content_types=OMR_CONTENT_TYPES,
                max_size=DEFAULT_MAX_OMR_SIZE,
                label="OMR 파일",
                pdf_single_page=True,
            )
        except ValidationError as e:
            return Response(e.detail, status=400)

        try:
            exam_id = int(target_id)
        except (TypeError, ValueError):
            return Response({"detail": "target_id must be an integer"}, status=400)

        # cross-tenant 방어: 자기 학원 소속 exam_id 인지 검증.
        if not self._validate_target_tenant(Submission.TargetType.EXAM, exam_id, tenant):
            return Response({"detail": "대상이 해당 학원에 속하지 않습니다."}, status=403)
        if enrollment_id:
            try:
                enrollment_id_int = int(enrollment_id)
            except (TypeError, ValueError):
                return Response({"detail": "enrollment_id must be an integer"}, status=400)
            if not ensure_exam_enrollment_candidate(
                tenant=tenant,
                exam_id=exam_id,
                enrollment_id=enrollment_id_int,
            ):
                return Response({"detail": "해당 시험에 등록되지 않은 학생입니다."}, status=403)
        else:
            enrollment_id_int = None

        payload = {}
        if request.data.get("sheet_id"):
            try:
                payload["sheet_id"] = int(request.data.get("sheet_id"))
            except (TypeError, ValueError):
                return Response({"detail": "sheet_id must be integer"}, status=400)
        try:
            sheet = resolve_omr_sheet_for_exam(
                tenant=tenant,
                exam_id=exam_id,
                requested_sheet_id=payload.get("sheet_id"),
            )
        except ValueError as e:
            return Response({"detail": str(e)}, status=400)
        payload["sheet_id"] = int(sheet.id)

        conflict = find_conflicting_exam_submission(
            tenant=tenant,
            exam_id=exam_id,
            enrollment_id=enrollment_id_int,
        )
        if conflict and not allow_duplicate_requested(request):
            return Response(duplicate_conflict_payload(conflict), status=409)

        ser = SubmissionCreateSerializer(
            data={
                "enrollment_id": enrollment_id_int,
                "target_type": Submission.TargetType.EXAM,
                "target_id": exam_id,
                "source": Submission.Source.OMR_SCAN,
                "payload": payload or None,
                "file": file_obj,
            }
        )
        ser.is_valid(raise_exception=True)
        try:
            with transaction.atomic():
                submission = ser.save(user=request.user, tenant=tenant)
        except SubmissionUploadCleanupRequired as exc:
            schedule_unreferenced_ai_object_cleanup(
                tenant_id=exc.tenant_id,
                key=exc.key,
            )
            return Response(
                {
                    "code": "submission_storage_cleanup_pending",
                    "detail": "업로드 정리를 예약했습니다. 잠시 후 다시 시도해 주세요.",
                },
                status=503,
            )
        dispatch_submission(submission)
        submission.refresh_from_db(fields=["status"])

        return Response(
            {"submission_id": submission.id, "status": submission.status},
            status=201,
        )

    def perform_create(self, serializer):
        tenant = getattr(self.request, "tenant", None)
        if not tenant:
            return
        # 🔒 학부모는 시험/과제 제출 권한 없음 (자녀가 본인 폰으로 제출해야 함)
        # parent_profile 존재 = 학부모 토큰. student_profile이 없는 부모 계정은 제출 차단.
        # (학부모-학생 겸용 계정은 student_profile이 있으므로 학생 흐름으로 통과)
        from rest_framework.exceptions import PermissionDenied
        is_parent = request_is_parent(self.request)
        is_student = getattr(self.request.user, "student_profile", None) is not None
        if is_parent and not is_student:
            raise PermissionDenied("학부모 계정은 시험/과제 제출 권한이 없습니다.")
        source = serializer.validated_data.get("source")
        if source == Submission.Source.OMR_SCAN:
            from apps.core.permissions import is_effective_staff
            if not is_effective_staff(self.request.user, tenant):
                raise PermissionDenied("OMR 업로드는 운영자만 사용할 수 있습니다.")
        # target_id(exam/homework)가 해당 테넌트 소속인지 검증
        target_type = serializer.validated_data.get("target_type")
        target_id = serializer.validated_data.get("target_id")
        if target_type and target_id:
            if not self._validate_target_tenant(target_type, target_id, tenant):
                raise PermissionDenied("대상이 해당 학원에 속하지 않습니다.")
        # enrollment_id 소유권 검증: 학생은 자신의 enrollment만 사용 가능
        enrollment_id = serializer.validated_data.get("enrollment_id")
        if enrollment_id:
            if not enrollment_belongs_to_tenant(enrollment_id=enrollment_id, tenant=tenant):
                raise PermissionDenied("해당 수강 정보에 접근할 수 없습니다.")
            student = getattr(self.request.user, "student_profile", None)
            if student:
                if not student_owns_enrollment(
                    enrollment_id=enrollment_id,
                    student=student,
                    tenant=tenant,
                ):
                    raise PermissionDenied("해당 수강 정보에 접근할 수 없습니다.")
            if target_type and target_id and not self._validate_target_enrollment_assignment(
                target_type,
                target_id,
                enrollment_id,
                tenant,
                ensure_exam_enrollment=True,
            ):
                raise PermissionDenied("해당 시험/과제에 등록되지 않은 수강 정보입니다.")
        with transaction.atomic():
            submission = serializer.save(user=self.request.user, tenant=tenant)
        dispatch_submission(submission)

    @staticmethod
    def _validate_target_tenant(target_type, target_id, tenant) -> bool:
        """target_id가 해당 tenant 소속인지 검증."""
        return target_belongs_to_tenant(target_type, target_id, tenant)

    @staticmethod
    def _validate_target_enrollment_assignment(
        target_type,
        target_id,
        enrollment_id,
        tenant,
        *,
        ensure_exam_enrollment: bool = False,
    ) -> bool:
        """target과 enrollment가 같은 수업 배정 맥락인지 검증."""
        return target_enrollment_assignment_exists(
            target_type,
            target_id,
            enrollment_id,
            tenant,
            ensure_exam_enrollment=ensure_exam_enrollment,
        )

    @action(detail=True, methods=["post"])
    def retry(self, request, pk=None):
        with transaction.atomic():
            submission = Submission.objects.select_for_update().get(pk=self.get_object().pk)

            try:
                retry_failed_submission(submission, actor="admin.retry")
            except InvalidTransitionError:
                return Response(
                    {"detail": "Only FAILED submissions can be retried."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        dispatch_submission(submission)

        return Response(
            {
                "submission_id": submission.id,
                "status": submission.status,
            }
        )

    @action(detail=True, methods=["get", "post"], url_path="manual-edit")
    def manual_edit(self, request, pk=None):
        if request.method == "GET":
            return self._manual_edit_get(request, pk)
        return self._manual_edit_post(request, pk)

    def _manual_edit_get(self, request, pk=None):
        """GET: 현재 답안 목록 + identifier + 스캔 이미지 URL 반환 (수동 편집 화면용)."""
        submission: Submission = self.get_object()
        answers = list(SubmissionAnswer.objects.filter(
            submission=submission,
        ))
        question_ids = [int(a.exam_question_id) for a in answers]
        question_number_by_id = exam_question_number_by_id(
            tenant=submission.tenant,
            question_ids=question_ids,
        )
        answers.sort(
            key=lambda a: (
                question_number_by_id.get(int(a.exam_question_id), 10**9),
                int(a.exam_question_id),
            )
        )
        answers_data = []
        for a in answers:
            am = a.meta or {}
            omr = am.get("omr") if isinstance(am, dict) else None
            answers_data.append({
                "question_id": a.exam_question_id,
                "question_no": question_number_by_id.get(
                    int(a.exam_question_id),
                ),
                "answer": a.answer or "",
                "omr": omr if isinstance(omr, dict) else None,
            })
        meta = dict(submission.meta or {})
        identifier = None
        omr = meta.get("omr") or {}
        if isinstance(omr, dict):
            identifier = omr.get("identifier_override") or omr.get("identifier")

        # ✅ 스캔 이미지 presigned URL (6h TTL — 장시간 검토 세션 대응)
        # AI가 정렬 보정본을 생성한 경우 원본보다 보정본을 우선 표시한다.
        scan_image_payload = {
            "scan_image_url": "",
            "original_scan_image_url": "",
            "scan_image_is_aligned": False,
            "scan_image_size": None,
        }
        if submission.file_key and submission.source == Submission.Source.OMR_SCAN:
            from apps.support.omr.scan_images import build_omr_scan_image_payload
            scan_image_payload = build_omr_scan_image_payload(
                submission=submission,
                expires_in=21600,
            )

        # 같은 학생·시험에 다른 OMR이 또 있으면 학원장이 한 번에 골라 채택할 수 있도록 노출.
        # cluster 후보 = (tenant, exam, enrollment_id) 동일 + 진행/완료 status + 본인 제외.
        duplicate_siblings: list[dict] = []
        if (
            submission.target_type == Submission.TargetType.EXAM
            and submission.enrollment_id
            and submission.target_id
        ):
            from apps.support.omr.scan_images import build_omr_scan_image_payload as _build_payload

            sibling_qs = (
                Submission.objects.filter(
                    tenant=submission.tenant,
                    target_type=Submission.TargetType.EXAM,
                    target_id=int(submission.target_id),
                    enrollment_id=int(submission.enrollment_id),
                    status__in=OMR_CONFLICT_STATUSES,
                )
                .exclude(id=submission.id)
                .order_by("-id")
            )
            for sib in sibling_qs:
                sib_meta = sib.meta or {}
                sib_review = sib_meta.get("manual_review") or {}
                sib_payload = {
                    "scan_image_url": "",
                    "original_scan_image_url": "",
                    "scan_image_is_aligned": False,
                    "scan_image_size": None,
                }
                if sib.file_key and sib.source == Submission.Source.OMR_SCAN:
                    sib_payload = _build_payload(submission=sib, expires_in=21600)
                duplicate_siblings.append({
                    "submission_id": int(sib.id),
                    "status": sib.status,
                    "identifier_status": (sib_meta.get("identifier_status") if isinstance(sib_meta, dict) else None),
                    "manual_review_required": bool(sib_review.get("required")),
                    "created_at": sib.created_at.isoformat() if getattr(sib, "created_at", None) else None,
                    "scan_image_url": sib_payload["scan_image_url"],
                    "scan_image_is_aligned": sib_payload["scan_image_is_aligned"],
                })

        return Response({
            "submission_id": submission.id,
            "submission_status": submission.status,
            "enrollment_id": submission.enrollment_id,
            "target_type": submission.target_type,
            "target_id": submission.target_id,
            "identifier": identifier,
            "answers": answers_data,
            **scan_image_payload,
            "meta": {
                "manual_review": meta.get("manual_review"),
                "ai_result": meta.get("ai_result"),
                "identifier_status": meta.get("identifier_status"),
            },
            "duplicate_siblings": duplicate_siblings,
        })

    @transaction.atomic
    def _manual_edit_post(self, request, pk=None):
        submission: Submission = Submission.objects.select_for_update().get(pk=self.get_object().pk)

        identifier = request.data.get("identifier")
        answers = request.data.get("answers") or []
        note = str(request.data.get("note") or "manual_edit")
        tenant = submission.tenant

        # ✅ identifier 검증 + submission.enrollment_id 반영 (tenant 안전성)
        #    { "enrollment_id": N } 형식만 매칭. 이외 형식은 meta로 저장만 하고 enrollment 미반영.
        resolved_enrollment_id: int | None = None
        exam_enrollment_created = False
        should_create_exam_enrollment = False
        allow_duplicate = False
        if isinstance(identifier, dict) and identifier.get("enrollment_id") is not None:
            try:
                candidate_eid = int(identifier["enrollment_id"])
            except (TypeError, ValueError):
                return Response(
                    {"detail": "enrollment_id는 정수여야 합니다."},
                    status=400,
                )

            exam_id = int(submission.target_id or 0) if submission.target_type == Submission.TargetType.EXAM else 0

            # 해당 시험의 enrollment 후보인지 검증 (ExamEnrollment -> SessionEnrollment fallback)
            candidate = validate_exam_enrollment_candidate(
                tenant=tenant,
                exam_id=exam_id,
                enrollment_id=candidate_eid,
            )
            if not candidate.ok:
                return Response({"detail": candidate.detail}, status=400)
            should_create_exam_enrollment = candidate.should_create

            # ✅ 중복 매칭 차단 (기본): 같은 시험의 다른 submission이 이미 같은 enrollment로 active면 409.
            #    override=1 쿼리파라미터로만 덮어쓰기 허용 (운영자 명시적 선택).
            allow_duplicate = allow_duplicate_requested(request)
            if exam_id and not allow_duplicate:
                dup_qs = (
                    Submission.objects
                    .filter(
                        tenant=tenant,
                        target_type=Submission.TargetType.EXAM,
                        target_id=exam_id,
                        enrollment_id=candidate_eid,
                        status__in=OMR_CONFLICT_STATUSES,
                    )
                    .exclude(id=submission.id)
                    .order_by("-id")
                )
                dup = dup_qs.first()
                if dup:
                    return Response(
                        {
                            "detail": "이미 이 학생에 매칭된 답안지가 있습니다. 덮어쓰려면 확인이 필요합니다.",
                            "code": "DUPLICATE_ENROLLMENT",
                            "conflict_submission_id": int(dup.id),
                            "conflict_file_key": dup.file_key or "",
                            "conflict_status": dup.status,
                        },
                        status=409,
                    )

            resolved_enrollment_id = candidate_eid
            if should_create_exam_enrollment:
                exam_enrollment_created = create_exam_enrollment_assignment(
                    exam_id=exam_id,
                    enrollment_id=candidate_eid,
                )

        if (
            submission.status == Submission.Status.NEEDS_IDENTIFICATION
            and not submission.enrollment_id
            and resolved_enrollment_id is None
        ):
            return Response(
                {"detail": "학생 식별이 필요한 답안지는 enrollment_id 매칭 후 저장할 수 있습니다."},
                status=400,
            )

        validated_answers: list[tuple[int, str]] = []
        if answers:
            if submission.target_type != Submission.TargetType.EXAM:
                return Response({"detail": "시험 제출 답안만 수동 수정할 수 있습니다."}, status=400)

            allowed_question_ids = allowed_manual_exam_question_ids(
                tenant=tenant,
                exam_id=int(submission.target_id or 0),
            )
            if allowed_question_ids is None:
                return Response({"detail": "시험을 찾을 수 없습니다."}, status=400)
            if not allowed_question_ids:
                return Response({"detail": "수동 수정 가능한 시험 문항이 없습니다."}, status=400)

            for a in answers:
                if not isinstance(a, dict):
                    continue
                raw_eqid = a.get("exam_question_id") or a.get("question_id")
                if not raw_eqid:
                    continue
                try:
                    eqid = int(raw_eqid)
                except (TypeError, ValueError):
                    return Response({"detail": "question_id는 정수여야 합니다."}, status=400)
                if eqid not in allowed_question_ids:
                    return Response({"detail": "해당 시험의 문항만 수정할 수 있습니다."}, status=400)
                validated_answers.append((eqid, str(a.get("answer") or "")))

        updated = 0

        for eqid, ans in validated_answers:
            SubmissionAnswer.objects.update_or_create(
                submission=submission,
                exam_question_id=eqid,
                defaults={"answer": ans, "tenant": submission.tenant},
            )
            updated += 1

        meta = dict(submission.meta or {})
        meta.setdefault("omr", {})
        meta["omr"]["identifier_override"] = identifier

        meta.setdefault("manual_edits", [])
        meta["manual_edits"].append(
            {
                "at": timezone.now().isoformat(),
                "by_user_id": getattr(request.user, "id", None),
                "note": note,
                "updated_answers_count": updated,
                "identifier": identifier,
                "resolved_enrollment_id": resolved_enrollment_id,
                "exam_enrollment_created": exam_enrollment_created,
            }
        )

        meta.setdefault("manual_review", {})
        meta["manual_review"]["required"] = False
        meta["manual_review"]["resolved_at"] = timezone.now().isoformat()

        # ✅ 검증된 enrollment_id가 있으면 submission에 반영 (+ identifier_status matched)
        save_fields = ["meta", "updated_at"]
        if resolved_enrollment_id is not None:
            submission.enrollment_id = resolved_enrollment_id
            save_fields.append("enrollment_id")
            meta["identifier_status"] = "matched"
            meta["identifier_match_kind"] = "manual"

        submission.meta = meta
        submission.save(update_fields=save_fields)
        if resolved_enrollment_id is not None:
            from apps.domains.submissions.omr_pipeline.services.facts import (
                record_student_match_fact,
            )

            record_student_match_fact(
                submission=submission,
                enrollment_id=resolved_enrollment_id,
                status=OMRStudentMatch.Status.CONFIRMED,
                method=OMRStudentMatch.Method.MANUAL,
                actor=f"admin.manual_edit.user_{getattr(request.user, 'id', '?')}",
                identifier_status="matched",
                identifier_payload=identifier,
            )

        if allow_duplicate and resolved_enrollment_id is not None:
            actor = f"admin.manual_edit.user_{getattr(request.user, 'id', '?')}"
            all_siblings = list(
                Submission.objects.select_for_update()
                .filter(
                    tenant=tenant,
                    target_type=Submission.TargetType.EXAM,
                    target_id=int(submission.target_id),
                    enrollment_id=int(resolved_enrollment_id),
                    source=Submission.Source.OMR_SCAN,
                )
                .exclude(id=submission.id)
            )
            rebind_representative_omr_submission(
                exam_id=int(submission.target_id),
                enrollment_id=int(resolved_enrollment_id),
                replacement_submission_id=int(submission.id),
                allowed_previous_submission_ids={int(sibling.id) for sibling in all_siblings},
                actor=actor,
            )
            now = timezone.now()
            for sibling in all_siblings:
                if sibling.status not in OMR_CONFLICT_STATUSES:
                    continue
                if sibling.status == Submission.Status.DONE:
                    supersede_submission(sibling, actor=actor)
                    reason = "superseded_by_manual_override"
                else:
                    fail_submission(
                        sibling,
                        admin_override=True,
                        error_message="discarded:duplicate",
                        actor=actor,
                    )
                    reason = "duplicate"
                sibling_meta = dict(sibling.meta or {})
                sibling_meta["discarded"] = {
                    "at": now.isoformat(),
                    "by_user_id": getattr(request.user, "id", None),
                    "reason": reason,
                    "kept_sibling_id": int(submission.id),
                }
                sibling.meta = sibling_meta
                sibling.save(update_fields=["meta", "updated_at"])

        try:
            from academy.application.use_cases.omr.grading_readiness import (
                grade_omr_submission_if_ready,
                readiness_payload,
            )

            decision = grade_omr_submission_if_ready(
                int(submission.id),
                actor=f"admin.manual_edit.user_{getattr(request.user, 'id', '?')}",
                allow_done_regrade=True,
            )
        except Exception:
            logger.exception(
                "OMR manual grading failed tenant_id=%s submission_id=%s",
                getattr(tenant, "id", None),
                submission.id,
            )
            transaction.set_rollback(True)
            return Response(
                {
                    "submission_id": submission.id,
                    "status": submission.status,
                    "updated": updated,
                    "code": "OMR_REGRADING_FAILED",
                    "detail": "재채점에 실패했습니다. 변경 사항은 저장되지 않았습니다. 잠시 후 다시 시도해 주세요.",
                },
                status=500,
            )

        submission.refresh_from_db(fields=["status", "enrollment_id"])
        if not decision.graded:
            return Response(
                {
                    "submission_id": submission.id,
                    "status": submission.status,
                    "updated": updated,
                    "graded": False,
                    "result_id": None,
                    "resolved_enrollment_id": resolved_enrollment_id,
                    "enrollment_id": submission.enrollment_id,
                    "score": None,
                    "total_score": None,
                    "max_score": None,
                    "readiness": readiness_payload(decision.readiness),
                }
            )

        synced_score = None
        synced_max_score = None
        if submission.target_type == Submission.TargetType.EXAM and submission.enrollment_id:
            synced_score, synced_max_score = get_synced_exam_score(
                tenant=tenant,
                target_id=int(submission.target_id),
                enrollment_id=int(submission.enrollment_id),
            )

        return Response(
            {
                "submission_id": submission.id,
                "status": submission.status,
                "updated": updated,
                "graded": True,
                "result_id": decision.result_id,
                "resolved_enrollment_id": resolved_enrollment_id,
                "enrollment_id": submission.enrollment_id,
                "score": synced_score,
                "total_score": synced_score,
                "max_score": synced_max_score,
            }
        )

    @action(detail=True, methods=["post"], url_path="accept-from-duplicates")
    def accept_from_duplicates(self, request, pk=None):
        """
        같은 (시험, 학생) 중복 OMR 후보 중 본 submission을 채택.
        - 본 submission: manual_review 해제 → ANSWERS_READY → grade → DONE.
        - 같은 (tenant, exam, enrollment_id) 다른 active sub: 'discarded:duplicate'로 폐기.
        단일 트랜잭션으로 원자성 보장.
        """
        tenant = getattr(request, "tenant", None)
        if not tenant:
            return Response({"detail": "Tenant required"}, status=403)

        with transaction.atomic():
            submission: Submission = Submission.objects.select_for_update().get(pk=pk)
            if submission.tenant_id != getattr(tenant, "id", None):
                return Response({"detail": "tenant mismatch"}, status=403)
            if submission.target_type != Submission.TargetType.EXAM:
                return Response({"detail": "시험 답안지만 채택할 수 있습니다."}, status=400)
            if not submission.enrollment_id:
                return Response(
                    {"detail": "학생 식별이 완료된 답안만 채택할 수 있습니다."},
                    status=400,
                )
            if not submission.target_id:
                return Response({"detail": "시험 정보가 없습니다."}, status=400)

            now = timezone.now()
            actor = f"admin.accept_from_duplicates.user_{getattr(request.user, 'id', '?')}"

            # 같은 (exam, enrollment) 다른 active sub 폐기
            all_siblings = list(
                Submission.objects.select_for_update()
                .filter(
                    tenant=tenant,
                    target_type=Submission.TargetType.EXAM,
                    target_id=int(submission.target_id),
                    enrollment_id=int(submission.enrollment_id),
                    source=Submission.Source.OMR_SCAN,
                )
                .exclude(id=submission.id)
            )
            sibling_qs = [
                sibling
                for sibling in all_siblings
                if sibling.status in OMR_CONFLICT_STATUSES
            ]
            rebind_representative_omr_submission(
                exam_id=int(submission.target_id),
                enrollment_id=int(submission.enrollment_id),
                replacement_submission_id=int(submission.id),
                allowed_previous_submission_ids={int(sibling.id) for sibling in all_siblings},
                actor=actor,
            )
            discarded_count = 0
            superseded_count = 0
            skipped: list[dict] = []
            for sib in sibling_qs:
                # DONE 형제는 SUPERSEDED(이 답안이 채택된 다른 답안으로 대체됨)로,
                # 그 외 active 형제는 FAILED + discarded:duplicate로.
                if sib.status == Submission.Status.DONE:
                    target = Submission.Status.SUPERSEDED
                    err_msg = ""
                    reason = "superseded_by_duplicate_selection"
                else:
                    target = Submission.Status.FAILED
                    err_msg = "discarded:duplicate"
                    reason = "duplicate"
                try:
                    if target == Submission.Status.SUPERSEDED:
                        supersede_submission(sib, actor=actor)
                    else:
                        fail_submission(
                            sib,
                            admin_override=True,
                            error_message=err_msg,
                            actor=actor,
                        )
                except InvalidTransitionError as e:
                    skipped.append({"id": sib.id, "reason": str(e)})
                    continue
                sib_meta = dict(sib.meta or {})
                sib_meta["discarded"] = {
                    "at": now.isoformat(),
                    "by_user_id": getattr(request.user, "id", None),
                    "reason": reason,
                    "kept_sibling_id": int(submission.id),
                }
                sib_meta.setdefault("manual_review", {})
                sib_meta["manual_review"]["required"] = False
                sib_meta["manual_review"]["resolved_at"] = now.isoformat()
                sib.meta = sib_meta
                sib.save(update_fields=["meta", "updated_at"])
                if target == Submission.Status.SUPERSEDED:
                    superseded_count += 1
                else:
                    discarded_count += 1

            # 채택 sub: manual_review 해제 + audit + ANSWERS_READY (이미면 skip) + grade
            meta = dict(submission.meta or {})
            meta.setdefault("manual_review", {})
            meta["manual_review"]["required"] = False
            meta["manual_review"]["resolved_at"] = now.isoformat()
            meta["accepted_from_duplicates"] = {
                "at": now.isoformat(),
                "by_user_id": getattr(request.user, "id", None),
                "discarded_sibling_count": discarded_count,
                "superseded_sibling_count": superseded_count,
            }
            if str(meta.get("identifier_status") or "").startswith("matched_"):
                meta["identifier_status"] = "matched"
            submission.meta = meta
            submission.save(update_fields=["meta", "updated_at"])
            from apps.domains.submissions.omr_pipeline.services.facts import (
                record_student_match_fact,
            )

            record_student_match_fact(
                submission=submission,
                enrollment_id=int(submission.enrollment_id),
                status=OMRStudentMatch.Status.CONFIRMED,
                method=OMRStudentMatch.Method.MANUAL,
                actor=actor,
                identifier_status=str(meta.get("identifier_status") or "matched"),
                identifier_payload=meta.get("omr", {}).get("identifier_override")
                if isinstance(meta.get("omr"), dict)
                else {},
            )

            try:
                from academy.application.use_cases.omr.grading_readiness import (
                    grade_omr_submission_if_ready,
                )

                decision = grade_omr_submission_if_ready(
                    int(submission.id),
                    actor=actor,
                    allow_done_regrade=True,
                )
            except Exception:
                logger.exception(
                    "OMR duplicate resolution grading failed tenant_id=%s submission_id=%s",
                    getattr(tenant, "id", None),
                    submission.id,
                )
                transaction.set_rollback(True)
                return Response(
                    {
                        "code": "OMR_REGRADING_FAILED",
                        "detail": "재채점에 실패했습니다. 변경 사항은 저장되지 않았습니다. 잠시 후 다시 시도해 주세요.",
                    },
                    status=500,
                )

        submission.refresh_from_db()

        synced_score = None
        synced_max_score = None
        if submission.enrollment_id:
            synced_score, synced_max_score = get_synced_exam_score(
                tenant=tenant,
                target_id=int(submission.target_id),
                enrollment_id=int(submission.enrollment_id),
            )

        return Response({
            "submission_id": submission.id,
            "status": submission.status,
            "discarded_count": discarded_count,
            "superseded_count": superseded_count,
            "skipped": skipped[:20],
            "graded": decision.graded,
            "readiness": {
                "can_grade": decision.readiness.can_grade,
                "missing": list(decision.readiness.missing),
                "answer_count": decision.readiness.answer_count,
                "enrollment_id": decision.readiness.enrollment_id,
            },
            "score": synced_score,
            "max_score": synced_max_score,
        }, status=200)

    # 폐기 사유 enum — 운영자 audit 용 세분화. 외 값은 "other" 로 fold.
    _DISCARD_REASONS = {
        "scan_quality",       # 스캔/사진 품질 불량
        "wrong_upload",       # 오업로드
        "duplicate",          # 중복 업로드
        "target_missing",     # 원본 시험/과제 없음 (orphan)
        "operator_discarded", # 단순 운영자 폐기 (default)
        "other",
    }

    @action(detail=False, methods=["post"], url_path="discard-batch")
    def discard_batch(self, request):
        """
        여러 답안지를 일괄 폐기.
        body: {"submission_ids": [int, ...], "reason": "operator_discarded" | ...}
        - 본인 tenant 의 submission 만 처리. 그 외는 silent skip + skipped 카운트로 보고.
        - 이미 DONE/SUPERSEDED 는 transition 차단되어 skipped 처리.
        """
        tenant = getattr(request, "tenant", None)
        if not tenant:
            return Response({"detail": "Tenant required"}, status=403)

        ids = request.data.get("submission_ids") or []
        if not isinstance(ids, list) or not ids:
            return Response({"detail": "submission_ids 필수"}, status=400)
        # 운영자 misuse 방지 — 1회 호출당 최대 500건. 그 이상은 분할 요청 강제.
        if len(ids) > 500:
            return Response(
                {"detail": "한 번에 폐기 가능한 최대 건수는 500건입니다.", "code": "BATCH_TOO_LARGE"},
                status=400,
            )
        try:
            ids = [int(x) for x in ids]
        except (TypeError, ValueError):
            return Response({"detail": "submission_ids 는 정수 배열"}, status=400)

        raw_reason = str(request.data.get("reason") or "operator_discarded").strip()
        reason = raw_reason if raw_reason in self._DISCARD_REASONS else "other"

        discarded = 0
        skipped: list[dict] = []
        now = timezone.now()
        with transaction.atomic():
            qs = Submission.objects.select_for_update().filter(tenant=tenant, id__in=ids)
            for s in qs:
                try:
                    fail_submission(
                        s,
                        admin_override=True,
                        error_message=f"discarded:{reason}",
                        actor=f"admin.discard_batch.user_{getattr(request.user, 'id', '?')}",
                    )
                except InvalidTransitionError as e:
                    skipped.append({"id": s.id, "reason": str(e)})
                    continue

                meta = dict(s.meta or {})
                meta["discarded"] = {
                    "at": now.isoformat(),
                    "by_user_id": getattr(request.user, "id", None),
                    "reason": reason,
                    "batch": True,
                }
                meta.setdefault("manual_review", {})
                meta["manual_review"]["required"] = False
                meta["manual_review"]["resolved_at"] = now.isoformat()
                s.meta = meta
                s.save(update_fields=["meta", "updated_at"])
                discarded += 1

        return Response({
            "discarded": discarded,
            "skipped_count": len(skipped),
            "skipped": skipped[:20],
            "reason": reason,
        }, status=200)

    @action(detail=True, methods=["post"], url_path="discard")
    def discard(self, request, pk=None):
        """
        OMR 답안지 폐기 — 스캔 품질 불량/오업로드/중복 등으로 채점 대상에서 제외.
        - 상태 FAILED로 전환하고 meta.discarded 기록 (감사 목적).
        - 본 제출에 매칭된 enrollment_id는 유지(기록용)하되 채점 미시행.
        body (optional): {"reason": "scan_quality" | "wrong_upload" | "duplicate" | "target_missing" | "other"}
        """
        raw_reason = str(request.data.get("reason") or "operator_discarded").strip()
        reason = raw_reason if raw_reason in self._DISCARD_REASONS else "other"

        with transaction.atomic():
            submission: Submission = self.get_queryset().select_for_update().get(pk=pk)
            try:
                fail_submission(
                    submission,
                    admin_override=True,
                    error_message=f"discarded:{reason}",
                    actor=f"admin.discard.user_{getattr(request.user, 'id', '?')}",
                )
            except InvalidTransitionError as e:
                return Response({"detail": str(e)}, status=409)

            now = timezone.now()
            meta = dict(submission.meta or {})
            meta["discarded"] = {
                "at": now.isoformat(),
                "by_user_id": getattr(request.user, "id", None),
                "reason": reason,
            }
            meta.setdefault("manual_review", {})
            meta["manual_review"]["required"] = False
            meta["manual_review"]["resolved_at"] = now.isoformat()
            submission.meta = meta
            submission.save(update_fields=["meta", "updated_at"])

        return Response(
            {
                "submission_id": submission.id,
                "status": submission.status,
                "discarded": True,
                "reason": reason,
            }
        )
