# PATH: apps/domains/submissions/views/homework_submissions_list_view.py
from __future__ import annotations

from typing import Any, Dict, Optional

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from apps.core.permissions import TenantResolvedAndStaff
from apps.domains.submissions.models import Submission
from apps.domains.submissions.services.homework_media import (
    homework_media_set_fingerprint_for_submissions,
    serialize_homework_media,
    serialize_legacy_homework_media,
)
from apps.support.submissions.dependencies import (
    clinic_highlight_map_for_enrollments,
    enrollment_map_for_submission_list,
    homework_submission_review_map,
    homework_submission_target_exists,
)


def _student_name_from_enrollment(enrollment) -> str:
    if not enrollment:
        return ""
    student = getattr(enrollment, "student", None)
    if student:
        name = getattr(student, "name", None)
        if name and isinstance(name, str) and name.strip():
            return name.strip()
    for attr in ("student_name", "name", "full_name"):
        value = getattr(enrollment, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _lecture_info_from_enrollment(enrollment) -> Dict[str, Any]:
    lecture = getattr(enrollment, "lecture", None) if enrollment else None
    if not lecture:
        return {}
    return {
        "lecture_title": getattr(lecture, "title", ""),
        "lecture_color": getattr(lecture, "color", None),
        "lecture_chip_label": getattr(lecture, "chip_label", None),
    }


def _get_photo_url(student) -> Optional[str]:
    """R2 presigned URL for student profile photo."""
    if not student:
        return None
    r2_key = getattr(student, "profile_photo_r2_key", None) or ""
    if not r2_key:
        return None
    try:
        from django.conf import settings
        from academy.adapters.storage.r2_presign import create_presigned_get_url
        return create_presigned_get_url(r2_key, expires_in=3600, bucket=settings.R2_STORAGE_BUCKET)
    except Exception:
        return None


class HomeworkSubmissionsListView(APIView):
    permission_classes = [IsAuthenticated, TenantResolvedAndStaff]

    def get(self, request, homework_id: int):
        tenant = getattr(request, "tenant", None)
        if not tenant:
            return Response([], status=200)

        # 테넌트 격리: homework가 해당 테넌트 소속인지 검증
        if not homework_submission_target_exists(homework_id=int(homework_id), tenant=tenant):
            return Response([], status=200)

        qs = (
            Submission.objects.filter(
                tenant=tenant,
                target_type=Submission.TargetType.HOMEWORK,
                target_id=int(homework_id),
            )
            .prefetch_related("media_files")
            .order_by("-id")[:200]
        )

        # ✅ 클리닉 하이라이트 일괄 계산
        submissions_list = list(qs)
        enrollment_ids = set()
        submissions_by_enrollment: dict[int, list[Submission]] = {}
        for s in submissions_list:
            eid = getattr(s, "enrollment_id", None)
            if eid:
                enrollment_ids.add(int(eid))
                submissions_by_enrollment.setdefault(int(eid), []).append(s)
        media_fingerprint_by_enrollment = {
            enrollment_id: homework_media_set_fingerprint_for_submissions(submissions)
            for enrollment_id, submissions in submissions_by_enrollment.items()
        }

        highlight_map = clinic_highlight_map_for_enrollments(
            tenant=tenant,
            enrollment_ids=enrollment_ids,
        )

        # enrollment_id → (student, lecture) 일괄 조회
        # 🔐 tenant 강제: Submission tenant 스코프와 무관하게 enrollment_id 참조 자체에는
        # 강제 제약이 없으므로 오염 시 다른 tenant 학생 노출 위험 → 명시적으로 차단.
        enrollment_map = enrollment_map_for_submission_list(
            tenant=tenant,
            enrollment_ids=enrollment_ids,
        )
        review_map = homework_submission_review_map(
            tenant=tenant,
            enrollment_ids=enrollment_ids,
            homework_id=int(homework_id),
        )

        items: list[Dict[str, Any]] = []
        for s in submissions_list:
            enrollment_id = getattr(s, "enrollment_id", None)
            enrollment = enrollment_map.get(int(enrollment_id)) if enrollment_id else None
            student = getattr(enrollment, "student", None) if enrollment else None

            # student name
            student_name = _student_name_from_enrollment(enrollment)

            # lecture info
            lecture_info = _lecture_info_from_enrollment(enrollment)

            source = getattr(s, "source", "")
            files = []
            if getattr(s, "file_key", None):
                files.append(serialize_legacy_homework_media(s))
            files.extend(serialize_homework_media(media) for media in s.media_files.all())
            files.sort(
                key=lambda item: (
                    int(item["position"]),
                    str(item["created_at"] or ""),
                    str(item["id"]),
                )
            )
            primary_file = next(
                (item for item in files if not item.get("removed_at")),
                files[0] if files else None,
            )
            primary_name = (
                str(primary_file.get("original_filename") or "")
                if primary_file
                else ""
            )
            compat_file_type = (
                primary_name.rsplit(".", 1)[-1].lower()
                if "." in primary_name
                else None
            )

            student_id = int(getattr(student, "id", 0)) if student else 0
            student_phone_v = getattr(student, "phone", "") if student else ""
            parent_phone_v = getattr(student, "parent_phone", "") if student else ""

            items.append(
                {
                    "id": int(s.id),
                    "enrollment_id": int(enrollment_id) if enrollment_id else 0,
                    "student_id": student_id,
                    "student_name": student_name,
                    "student_phone": student_phone_v or None,
                    "parent_phone": parent_phone_v or None,
                    "status": str(getattr(s, "status", "")),
                    "source": str(source),
                    # 기존 단건 소비자가 파일 유무·형식·크기를 읽는 호환 필드.
                    # 버킷 object key는 더 이상 클라이언트에 노출하지 않는다.
                    "file_type": compat_file_type,
                    "file_size": primary_file.get("file_size") if primary_file else None,
                    "files": files,
                    "media_set_fingerprint": media_fingerprint_by_enrollment.get(
                        int(enrollment_id) if enrollment_id else 0
                    ),
                    "created_at": s.created_at.isoformat() if hasattr(s, "created_at") and s.created_at else None,
                    "profile_photo_url": _get_photo_url(student),
                    "name_highlight_clinic_target": highlight_map.get(int(enrollment_id), False) if enrollment_id else False,
                    **review_map.get(
                        int(enrollment_id) if enrollment_id else 0,
                        {
                            "teacher_reviewed": False,
                            "teacher_review_source": None,
                            "teacher_review_note": "",
                            "teacher_reviewed_at": None,
                            "teacher_review_updated_at": None,
                        },
                    ),
                    **lecture_info,
                }
            )

        return Response(items, status=200)
