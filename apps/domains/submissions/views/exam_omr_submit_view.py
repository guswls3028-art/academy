from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import ValidationError
from django.db import transaction

from apps.api.common.upload_validation import (
    DEFAULT_MAX_OMR_SIZE,
    OMR_CONTENT_TYPES,
    OMR_EXTENSIONS,
    validate_uploaded_file,
)
from apps.core.permissions import TenantResolvedAndStaff
from apps.domains.submissions.models import Submission
from apps.domains.submissions.serializers.submission import (
    SubmissionCreateSerializer,
    SubmissionUploadCleanupRequired,
)
from apps.domains.submissions.services.dispatcher import (
    dispatch_submission,
    resolve_omr_sheet_for_exam,
)
from apps.support.submissions.dependencies import exam_belongs_to_tenant
from apps.domains.submissions.services.omr_submission_guards import (
    allow_duplicate_requested,
    duplicate_conflict_payload,
    ensure_exam_enrollment_candidate,
    find_conflicting_exam_submission,
)
from apps.domains.submissions.services.lifecycle import (
    ensure_ai_submission_key_attachable,
    schedule_unreferenced_ai_object_cleanup,
)


class ExamOMRSubmitView(APIView):
    # OMR 스캔은 운영자(또는 교사)가 학생 대리 업로드하는 경로.
    # 과거 TenantResolvedAndMember로 두어 학생이 타 수강의 enrollment_id로 제출 가능했음.
    permission_classes = [IsAuthenticated, TenantResolvedAndStaff]

    def post(self, request, exam_id: int):
        tenant = getattr(request, "tenant", None)
        if not tenant:
            return Response({"detail": "Tenant required"}, status=403)

        enrollment_id = request.data.get("enrollment_id")
        sheet_id = request.data.get("sheet_id")
        file_key = request.data.get("file_key")
        file_obj = request.FILES.get("file")

        if not enrollment_id or not (file_key or file_obj):
            return Response(
                {"detail": "enrollment_id and file or file_key required"}, status=400
            )

        try:
            enrollment_id_int = int(enrollment_id)
        except (TypeError, ValueError):
            return Response({"detail": "enrollment_id must be an integer"}, status=400)

        if file_obj:
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

        # exam 테넌트 검증 — 크로스 테넌트 시험 제출 방지 (tenant FK 직접 검증)
        if not exam_belongs_to_tenant(exam_id=int(exam_id), tenant=tenant):
            return Response({"detail": "해당 시험을 찾을 수 없습니다."}, status=400)

        if not ensure_exam_enrollment_candidate(
            tenant=tenant,
            exam_id=int(exam_id),
            enrollment_id=enrollment_id_int,
        ):
            return Response({"detail": "해당 시험에 등록되지 않은 학생입니다."}, status=403)

        try:
            requested_sheet_id = int(sheet_id) if sheet_id not in (None, "") else None
            sheet = resolve_omr_sheet_for_exam(
                tenant=tenant,
                exam_id=int(exam_id),
                requested_sheet_id=requested_sheet_id,
            )
        except (TypeError, ValueError) as e:
            return Response({"detail": str(e)}, status=400)

        conflict = find_conflicting_exam_submission(
            tenant=tenant,
            exam_id=int(exam_id),
            enrollment_id=enrollment_id_int,
        )
        if conflict and not allow_duplicate_requested(request):
            return Response(duplicate_conflict_payload(conflict), status=409)

        payload = {"sheet_id": int(sheet.id)}
        if file_obj:
            ser = SubmissionCreateSerializer(
                data={
                    "enrollment_id": enrollment_id_int,
                    "target_type": Submission.TargetType.EXAM,
                    "target_id": int(exam_id),
                    "source": Submission.Source.OMR_SCAN,
                    "payload": payload,
                    "file": file_obj,
                }
            )
            ser.is_valid(raise_exception=True)
            try:
                with transaction.atomic():
                    submission = ser.save(user=request.user, tenant=tenant)
            except Exception as error:
                if isinstance(error, SubmissionUploadCleanupRequired):
                    cleanup_error = error
                else:
                    uploaded_key = getattr(ser, "uploaded_object_key", None)
                    if not uploaded_key:
                        raise
                    cleanup_error = SubmissionUploadCleanupRequired(
                        tenant_id=getattr(ser, "uploaded_tenant_id", tenant.id),
                        key=uploaded_key,
                    )
                schedule_unreferenced_ai_object_cleanup(
                    tenant_id=cleanup_error.tenant_id,
                    key=cleanup_error.key,
                )
                return Response(
                    {"detail": "업로드 정리를 예약했습니다. 파일을 다시 선택해 주세요."},
                    status=503,
                )
        else:
            file_key_str = str(file_key or "").strip()
            if not file_key_str.startswith(f"tenants/{tenant.id}/ai/submissions/"):
                return Response({"detail": "file_key does not belong to this tenant"}, status=400)
            try:
                with transaction.atomic():
                    ensure_ai_submission_key_attachable(
                        tenant_id=tenant.id,
                        key=file_key_str,
                    )
                    submission = Submission.objects.create(
                        tenant=tenant,
                        user=request.user,
                        enrollment_id=enrollment_id_int,
                        target_type=Submission.TargetType.EXAM,
                        target_id=int(exam_id),
                        source=Submission.Source.OMR_SCAN,
                        file_key=file_key_str,
                        payload=payload,
                    )
            except ValueError:
                return Response(
                    {
                        "code": "submission_storage_cleanup_conflict",
                        "detail": "정리 중인 제출 파일은 새 답안에 연결할 수 없습니다.",
                    },
                    status=409,
                )

        dispatch_submission(submission)
        submission.refresh_from_db(fields=["status"])

        return Response(
            {"submission_id": submission.id, "status": submission.status}, status=201
        )
