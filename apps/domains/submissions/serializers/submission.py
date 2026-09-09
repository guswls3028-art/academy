# PATH: apps/domains/submissions/serializers/submission.py
# 변경 요약:
# - OMR_SCAN은 enrollment_id 없이 제출 생성 허용 (식별은 OMR 마킹/AI가 채움)
# - file(FileField)로 업로드 받은 파일을 R2로 업로드 후 file_key 저장

from __future__ import annotations

import logging
import mimetypes
import uuid

from rest_framework import serializers

from apps.domains.submissions.models import Submission

# ✅ API 서버 전용 R2 업로드
from apps.core.r2_paths import ai_submission_key
from apps.infrastructure.storage.r2 import delete_object_r2_ai, upload_fileobj_to_r2


logger = logging.getLogger(__name__)


class SubmissionUploadCleanupRequired(RuntimeError):
    def __init__(self, *, tenant_id: int, key: str):
        super().__init__("uploaded submission object requires durable cleanup")
        self.tenant_id = int(tenant_id)
        self.key = key


class SubmissionSerializer(serializers.ModelSerializer):
    enrollment_id = serializers.IntegerField(required=False, allow_null=True)

    class Meta:
        model = Submission
        fields = (
            "id",
            "tenant",
            "user",
            "enrollment_id",
            "target_type",
            "target_id",
            "source",
            "file_key",
            "file_type",
            "file_size",
            "payload",
            "status",
            "error_message",
            "meta",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "id",
            "user",
            "status",
            "error_message",
            "meta",
            "created_at",
            "updated_at",
        )


class SubmissionCreateSerializer(serializers.ModelSerializer):
    enrollment_id = serializers.IntegerField(required=False, allow_null=True)
    file = serializers.FileField(required=False)

    class Meta:
        model = Submission
        fields = (
            "enrollment_id",
            "target_type",
            "target_id",
            "source",
            "file",
            "payload",
        )

    def validate(self, attrs):
        source = attrs.get("source")
        target_type = attrs.get("target_type")

        # ✅ 핵심 정책:
        # - exam/homework는 일반적으로 enrollment_id 필요
        # - 단, OMR_SCAN은 "식별을 OMR 마킹으로 처리"하므로 enrollment_id 없이 생성 허용
        if target_type in (Submission.TargetType.EXAM, Submission.TargetType.HOMEWORK):
            if source != Submission.Source.OMR_SCAN:
                if not attrs.get("enrollment_id"):
                    raise serializers.ValidationError({"enrollment_id": "required"})

        # 파일 기반 소스는 file 필요
        if source in (
            Submission.Source.OMR_SCAN,
            Submission.Source.HOMEWORK_IMAGE,
            Submission.Source.HOMEWORK_VIDEO,
        ):
            if not attrs.get("file"):
                raise serializers.ValidationError({"file": "file required"})

        # online은 payload 필요
        if source == Submission.Source.ONLINE and not attrs.get("payload"):
            raise serializers.ValidationError({"payload": "required"})

        return attrs

    def create(self, validated_data):
        upload_file = validated_data.pop("file", None)
        tenant = validated_data.pop("tenant", None)
        user = validated_data.pop("user", None)
        if not tenant or not user:
            raise serializers.ValidationError("tenant and user are required.")
        submission = Submission.objects.create(tenant=tenant, user=user, **validated_data)

        if upload_file:
            ext = (upload_file.name or "").split(".")[-1] if "." in (upload_file.name or "") else "bin"
            key = ai_submission_key(
                tenant_id=submission.tenant_id,
                submission_id=submission.id,
                unique_id=uuid.uuid4().hex,
                ext=ext,
            )
            uploaded = False
            try:
                upload_fileobj_to_r2(
                    fileobj=upload_file,
                    key=key,
                    content_type=getattr(upload_file, "content_type", None),
                )
                uploaded = True

                submission.file_key = key
                submission.file_type = (
                    getattr(upload_file, "content_type", None)
                    or mimetypes.guess_type(upload_file.name)[0]
                )
                submission.file_size = getattr(upload_file, "size", None)
                submission.save(update_fields=["file_key", "file_type", "file_size"])
            except Exception as exc:
                if uploaded:
                    try:
                        delete_object_r2_ai(key=key)
                    except Exception:
                        logger.exception(
                            "Failed to compensate uploaded submission object",
                            extra={"submission_id": int(submission.id)},
                        )
                        raise SubmissionUploadCleanupRequired(
                            tenant_id=submission.tenant_id,
                            key=key,
                        ) from exc
                raise

        return submission
