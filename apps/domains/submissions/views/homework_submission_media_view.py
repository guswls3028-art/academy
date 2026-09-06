from __future__ import annotations

from django.db import transaction
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import (
    TenantResolvedAndMember,
    is_effective_staff,
)
from apps.domains.submissions.models import Submission, SubmissionMedia
from apps.domains.submissions.services.homework_media import (
    HomeworkMediaUploadFailed,
    homework_media_limits_payload,
    legacy_homework_media_removed_at,
    serialize_legacy_homework_media,
    serialize_homework_media,
    store_homework_media,
)
from apps.infrastructure.storage.r2 import generate_presigned_get_url
from apps.support.homework.review_lock import lock_homework_review_target
from apps.support.submissions.dependencies import (
    enrollment_belongs_to_tenant,
    homework_submission_is_teacher_reviewed,
    request_is_parent,
    student_owns_enrollment,
    target_enrollment_assignment_exists,
)


def _parse_positive_int(value, *, field_name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValidationError({field_name: "올바른 값이 아닙니다."})
    if parsed <= 0:
        raise ValidationError({field_name: "올바른 값이 아닙니다."})
    return parsed


def _require_student_homework_access(request, *, homework_id: int, enrollment_id: int):
    tenant = getattr(request, "tenant", None)
    if not tenant:
        raise PermissionDenied("학원 정보를 확인할 수 없습니다.")
    is_parent = request_is_parent(request)
    student = getattr(request.user, "student_profile", None)
    if is_parent:
        raise PermissionDenied("학부모 계정은 과제 파일을 변경할 수 없습니다.")
    if not student:
        raise PermissionDenied("학생 계정으로 이용해 주세요.")
    if not enrollment_belongs_to_tenant(enrollment_id=enrollment_id, tenant=tenant):
        raise PermissionDenied("해당 수강 정보에 접근할 수 없습니다.")
    if not student_owns_enrollment(
        enrollment_id=enrollment_id,
        student=student,
        tenant=tenant,
    ):
        raise PermissionDenied("해당 수강 정보에 접근할 수 없습니다.")
    if not target_enrollment_assignment_exists(
        Submission.TargetType.HOMEWORK,
        homework_id,
        enrollment_id,
        tenant,
    ):
        raise PermissionDenied("현재 제출할 수 있는 과제가 아닙니다.")
    return tenant


def _student_submission_parents(*, tenant, user, enrollment_id: int, homework_id: int):
    return (
        Submission.objects.filter(
            tenant=tenant,
            user=user,
            enrollment_id=enrollment_id,
            target_type=Submission.TargetType.HOMEWORK,
            target_id=homework_id,
            source__in=[
                Submission.Source.HOMEWORK_IMAGE,
                Submission.Source.HOMEWORK_VIDEO,
            ],
        )
        .exclude(status=Submission.Status.SUPERSEDED)
        .prefetch_related("media_files")
        .order_by("id")
    )


def _reviewed_response() -> Response:
    return Response(
        {
            "code": "HOMEWORK_MEDIA_REVIEWED",
            "detail": "완료 또는 통과한 과제 파일은 변경할 수 없습니다. 보완이 필요하면 선생님에게 상태 변경을 요청해 주세요.",
        },
        status=status.HTTP_409_CONFLICT,
    )


class HomeworkSubmissionMediaCollectionView(APIView):
    permission_classes = [IsAuthenticated, TenantResolvedAndMember]

    @extend_schema(exclude=True)
    def get(self, request, homework_id: int):
        enrollment_id = _parse_positive_int(
            request.query_params.get("enrollment_id"),
            field_name="enrollment_id",
        )
        tenant = _require_student_homework_access(
            request,
            homework_id=int(homework_id),
            enrollment_id=enrollment_id,
        )
        files: list[dict] = []
        for parent in _student_submission_parents(
            tenant=tenant,
            user=request.user,
            enrollment_id=enrollment_id,
            homework_id=int(homework_id),
        ):
            if parent.file_key and not legacy_homework_media_removed_at(parent):
                files.append(serialize_legacy_homework_media(parent))
            files.extend(
                serialize_homework_media(media)
                for media in parent.media_files.all()
                if media.removed_at is None
            )
        files.sort(key=lambda item: (int(item["position"]), str(item["created_at"] or ""), str(item["id"])))
        return Response(
            {
                "files": files,
                "limits": homework_media_limits_payload(),
            }
        )

    @extend_schema(exclude=True)
    def post(self, request, homework_id: int):
        enrollment_id = _parse_positive_int(
            request.data.get("enrollment_id"),
            field_name="enrollment_id",
        )
        tenant = _require_student_homework_access(
            request,
            homework_id=int(homework_id),
            enrollment_id=enrollment_id,
        )
        upload_file = request.FILES.get("file")
        if not upload_file:
            raise ValidationError({"file": "파일을 선택해 주세요."})
        upload_failure = None
        with transaction.atomic():
            assignment = lock_homework_review_target(
                tenant=tenant,
                enrollment_id=enrollment_id,
                homework_id=int(homework_id),
            )
            if assignment is None:
                raise PermissionDenied("현재 제출할 수 있는 과제가 아닙니다.")
            if homework_submission_is_teacher_reviewed(
                tenant=tenant,
                enrollment_id=enrollment_id,
                homework_id=int(homework_id),
            ):
                return _reviewed_response()
            try:
                media, deduplicated = store_homework_media(
                    tenant=tenant,
                    user=request.user,
                    enrollment_id=enrollment_id,
                    homework_id=int(homework_id),
                    upload_file=upload_file,
                    client_file_id=request.data.get("client_file_id"),
                    upload_batch_id=request.data.get("upload_batch_id"),
                    position=request.data.get("position"),
                )
            except HomeworkMediaUploadFailed as exc:
                # Commit the deterministic failed row so the same client identity
                # can reconcile the object on a later retry.
                upload_failure = exc
        if upload_failure is not None:
            raise upload_failure
        payload = serialize_homework_media(media)
        payload["deduplicated"] = deduplicated
        return Response(
            payload,
            status=status.HTTP_200_OK if deduplicated else status.HTTP_201_CREATED,
        )


def _owned_submission(
    *,
    tenant,
    user,
    enrollment_id: int,
    homework_id: int,
    submission_id: int,
    for_update: bool = False,
):
    queryset = Submission.objects
    if for_update:
        queryset = queryset.select_for_update()
    return queryset.filter(
        id=submission_id,
        tenant=tenant,
        user=user,
        enrollment_id=enrollment_id,
        target_type=Submission.TargetType.HOMEWORK,
        target_id=homework_id,
    ).first()


class HomeworkSubmissionMediaDetailView(APIView):
    permission_classes = [IsAuthenticated, TenantResolvedAndMember]

    @extend_schema(exclude=True)
    def delete(self, request, homework_id: int, media_id: str):
        enrollment_id = _parse_positive_int(
            request.data.get("enrollment_id"),
            field_name="enrollment_id",
        )
        tenant = _require_student_homework_access(
            request,
            homework_id=int(homework_id),
            enrollment_id=enrollment_id,
        )
        with transaction.atomic():
            assignment = lock_homework_review_target(
                tenant=tenant,
                enrollment_id=enrollment_id,
                homework_id=int(homework_id),
            )
            if assignment is None:
                raise PermissionDenied("현재 제출할 수 있는 과제가 아닙니다.")
            if homework_submission_is_teacher_reviewed(
                tenant=tenant,
                enrollment_id=enrollment_id,
                homework_id=int(homework_id),
            ):
                return _reviewed_response()

            if str(media_id).startswith("legacy-"):
                submission_id = _parse_positive_int(
                    str(media_id).removeprefix("legacy-"),
                    field_name="media_id",
                )
                parent = _owned_submission(
                    tenant=tenant,
                    user=request.user,
                    enrollment_id=enrollment_id,
                    homework_id=int(homework_id),
                    submission_id=submission_id,
                    for_update=True,
                )
                if not parent or not parent.file_key:
                    return Response(status=status.HTTP_404_NOT_FOUND)
                meta = dict(parent.meta or {})
                if not meta.get("homework_media_legacy_removed_at"):
                    meta["homework_media_legacy_removed_at"] = timezone.now().isoformat()
                    meta["homework_media_legacy_removed_by_id"] = int(request.user.id)
                    parent.meta = meta
                    parent.save(update_fields=["meta", "updated_at"])
                return Response(status=status.HTTP_204_NO_CONTENT)

            parsed_media_id = _parse_positive_int(media_id, field_name="media_id")
            media = (
                SubmissionMedia.objects.select_for_update()
                .filter(
                    id=parsed_media_id,
                    tenant=tenant,
                    submission__tenant=tenant,
                    submission__user=request.user,
                    submission__enrollment_id=enrollment_id,
                    submission__target_type=Submission.TargetType.HOMEWORK,
                    submission__target_id=int(homework_id),
                )
                .first()
            )
            if not media:
                return Response(status=status.HTTP_404_NOT_FOUND)
            if media.removed_at is None:
                now = timezone.now()
                media.status = SubmissionMedia.Status.REMOVED
                media.removed_at = now
                media.removed_by = request.user
                media.save(
                    update_fields=["status", "removed_at", "removed_by", "updated_at"]
                )
            return Response(status=status.HTTP_204_NO_CONTENT)


def _preview_target(*, tenant, homework_id: int, media_id: str):
    if str(media_id).startswith("legacy-"):
        submission_id = _parse_positive_int(
            str(media_id).removeprefix("legacy-"),
            field_name="media_id",
        )
        submission = Submission.objects.filter(
            id=submission_id,
            tenant=tenant,
            target_type=Submission.TargetType.HOMEWORK,
            target_id=homework_id,
        ).first()
        if not submission or not submission.file_key:
            return None
        legacy = serialize_legacy_homework_media(submission)
        if legacy.get("removed_at"):
            return None
        return submission, submission.file_key, legacy

    parsed_media_id = _parse_positive_int(media_id, field_name="media_id")
    media = (
        SubmissionMedia.objects.select_related("submission")
        .filter(
            id=parsed_media_id,
            tenant=tenant,
            submission__tenant=tenant,
            submission__target_type=Submission.TargetType.HOMEWORK,
            submission__target_id=homework_id,
        )
        .first()
    )
    if not media or media.removed_at:
        return None
    return media.submission, media.object_key, serialize_homework_media(media)


class HomeworkSubmissionMediaPreviewView(APIView):
    permission_classes = [IsAuthenticated, TenantResolvedAndMember]

    @extend_schema(exclude=True)
    def get(self, request, homework_id: int, media_id: str):
        tenant = getattr(request, "tenant", None)
        if not tenant:
            raise PermissionDenied("학원 정보를 확인할 수 없습니다.")
        target = _preview_target(
            tenant=tenant,
            homework_id=int(homework_id),
            media_id=str(media_id),
        )
        if not target:
            return Response(status=status.HTTP_404_NOT_FOUND)
        submission, object_key, media_payload = target
        is_staff = is_effective_staff(request.user, tenant)
        is_owner = submission.user_id == request.user.id and getattr(request.user, "student_profile", None) is not None
        if not is_staff:
            if not is_owner:
                raise PermissionDenied("이 과제 파일을 볼 수 없습니다.")
            _require_student_homework_access(
                request,
                homework_id=int(homework_id),
                enrollment_id=int(submission.enrollment_id or 0),
            )
        if not media_payload.get("legacy") and media_payload.get("status") != SubmissionMedia.Status.UPLOADED:
            return Response(
                {"code": "HOMEWORK_MEDIA_NOT_READY", "detail": "업로드가 끝난 파일만 미리 볼 수 있습니다."},
                status=status.HTTP_409_CONFLICT,
            )
        if not object_key or object_key == "pending":
            return Response(
                {"code": "HOMEWORK_MEDIA_NOT_READY", "detail": "파일이 아직 준비되지 않았습니다."},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(
            {
                "url": generate_presigned_get_url(key=object_key, expires_in=600),
                "media_kind": media_payload["media_kind"],
                "mime_type": media_payload["mime_type"],
                "original_filename": media_payload["original_filename"],
                "expires_in": 600,
            }
        )
