# apps/domains/student_app/profile/views.py
import logging

from django.db import transaction
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework.exceptions import APIException

from apps.core.models.user import user_display_username
from apps.domains.student_app.permissions import IsStudentOrParent, get_request_student
from apps.support.student_app.profile_dependencies import (
    StudentProfileUpdateError,
    send_parent_account_credentials_notice,
    send_student_account_credentials_notice,
    send_user_password_changed_notice,
    update_student_profile,
)
logger = logging.getLogger(__name__)


class AccountNoticeDeliveryFailed(APIException):
    status_code = 503
    default_detail = "계정 안내 알림톡 발송에 실패했습니다. 잠시 후 다시 시도해 주세요."
    default_code = "account_notice_delivery_failed"


def _get_profile_photo_url(student):
    """Return presigned R2 URL for profile photo, or None."""
    r2_key = getattr(student, "profile_photo_r2_key", None) or ""
    if r2_key:
        try:
            from django.conf import settings
            from academy.adapters.storage.r2_presign import create_presigned_get_url
            return create_presigned_get_url(r2_key, expires_in=3600, bucket=settings.R2_STORAGE_BUCKET)
        except Exception:
            logger.warning("Failed to generate presigned URL for profile photo r2_key=%s", r2_key)
    # 로컬 /media/ URL은 프로덕션에서 404 → 반환하지 않음.
    # R2 키 없으면 None (프론트에서 이름 이니셜 표시).
    return None


def _profile_response(request, student, *, is_parent_read_only=False, parent_display_name=None):
    url = _get_profile_photo_url(student)
    payload = {
        "id": student.id,
        "name": student.name,
        "profile_photo_url": url,
        "ps_number": getattr(student, "ps_number", "") or "",
        "username": user_display_username(student.user) if student.user_id else "",
        "parent_phone": getattr(student, "parent_phone", "") or "",
        "phone": (student.phone or "").strip(),
        "gender": (student.gender or "").strip() or None,
        "address": (student.address or "").strip() or None,
        "school_type": getattr(student, "school_type", "HIGH") or "HIGH",
        "elementary_school": (getattr(student, "elementary_school", None) or "").strip() or None,
        "high_school": (getattr(student, "high_school", None) or "").strip() or None,
        "middle_school": (getattr(student, "middle_school", None) or "").strip() or None,
        "origin_middle_school": (getattr(student, "origin_middle_school", None) or "").strip() or None,
        "grade": getattr(student, "grade", None),
        "high_school_class": (getattr(student, "high_school_class", None) or "").strip() or None,
        "major": (getattr(student, "major", None) or "").strip() or None,
        "memo": (getattr(student, "memo", None) or "").strip() or None,
    }
    if is_parent_read_only:
        payload["isParentReadOnly"] = True
        payload["displayName"] = parent_display_name or "학생 학부모님"
    return Response(payload)


class StudentProfileView(APIView):
    """
    GET/PATCH /api/student-app/me/
    - 학생 또는 학부모 접근 (학부모는 연결된 학생 프로필 읽기 전용)
    - GET: 프로필 정보. 학부모일 때 isParentReadOnly, displayName("{이름} 학생 학부모님") 추가
    - PATCH: 학생만 가능. 학부모는 403
    """
    permission_classes = [IsAuthenticated, IsStudentOrParent]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request):
        student = get_request_student(request)
        if not student:
            return Response({"detail": "학생 프로필이 없습니다."}, status=404)
        is_parent = getattr(request.user, "parent_profile", None) is not None
        if is_parent:
            display_name = f"{student.name} 학생 학부모님" if student.name else "학생 학부모님"
            return _profile_response(request, student, is_parent_read_only=True, parent_display_name=display_name)
        return _profile_response(request, student)

    def patch(self, request):
        if getattr(request.user, "parent_profile", None) is not None:
            return Response({"detail": "학부모는 프로필을 수정할 수 없습니다."}, status=403)
        student = get_request_student(request)
        if not student:
            return Response({"detail": "학생 프로필이 없습니다."}, status=404)

        # 1) 프로필 사진 (multipart) → R2 업로드
        photo = request.FILES.get("profile_photo")
        if photo:
            from apps.support.students.profile_photo_dependencies import (
                StudentProfilePhotoStorageError,
                StudentProfilePhotoValidationError,
                replace_student_profile_photo,
            )
            try:
                student = replace_student_profile_photo(student=student, photo=photo)
            except StudentProfilePhotoValidationError as exc:
                return Response({"detail": str(exc)}, status=400)
            except StudentProfilePhotoStorageError as exc:
                logger.warning("Profile photo replacement failed student_id=%s", student.id)
                return Response({"detail": str(exc)}, status=503)
            return _profile_response(request, student)

        # 2) JSON: name, username, 비밀번호 변경
        data = getattr(request, "data", None) or {}
        if not data and request.content_type and "application/json" in request.content_type:
            import json
            try:
                data = json.loads(request.body) if getattr(request, "body", None) else {}
            except Exception:
                pass

        current_password = data.get("current_password")
        new_password = data.get("new_password")
        password_changed = current_password is not None and new_password is not None
        if password_changed:
            if not str(new_password).strip() or len(str(new_password)) < 4:
                return Response({"detail": "새 비밀번호는 4자 이상이어야 합니다."}, status=400)

        old_username = user_display_username(student.user) if student.user_id else ""
        old_phone = student.phone or ""
        old_parent_phone = student.parent_phone or ""
        try:
            with transaction.atomic():
                result = update_student_profile(
                    student=student,
                    tenant=request.tenant,
                    data=dict(data),
                    identity_field="username",
                    ignore_blank_name=True,
                    allow_parent_phone_change=False,
                )
                student = result.student

                new_phone = student.phone or ""
                username_changed = bool((data.get("username") or "").strip()) and (student.ps_number or "") != old_username
                phone_changed = bool(new_phone) and new_phone != old_phone
                parent_phone_changed = (student.parent_phone or "") != old_parent_phone

                if password_changed:
                    from apps.core.services.password import (
                        CurrentPasswordMismatch,
                        PasswordNoticeDeliveryError,
                        change_password_with_notice,
                    )
                    try:
                        change_password_with_notice(
                            request.user,
                            current_password=str(current_password),
                            new_password=str(new_password),
                            send_notice=send_user_password_changed_notice,
                        )
                    except CurrentPasswordMismatch:
                        raise StudentProfileUpdateError({"detail": "현재 비밀번호가 일치하지 않습니다."})
                    except PasswordNoticeDeliveryError as exc:
                        raise AccountNoticeDeliveryFailed(str(exc))

                if not password_changed and (username_changed or phone_changed):
                    if not send_student_account_credentials_notice(
                        student=student,
                        to=new_phone if phone_changed else None,
                    ):
                        raise AccountNoticeDeliveryFailed()

                if parent_phone_changed:
                    if not send_parent_account_credentials_notice(
                        student=student,
                        parent=getattr(student, "parent", None),
                        parent_password=result.parent_password_for_notice,
                        to=student.parent_phone,
                    ):
                        raise AccountNoticeDeliveryFailed()
        except StudentProfileUpdateError as e:
            return Response(e.detail if isinstance(e.detail, dict) else {"detail": str(e.detail)}, status=400)

        return _profile_response(request, student)
