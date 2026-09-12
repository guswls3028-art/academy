# PATH: apps/domains/homework/views/homework_assignment_view.py

from __future__ import annotations

from importlib import import_module
from typing import List, Set

from django.db import transaction
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status

from apps.core.permissions import TenantResolvedAndStaff

from apps.domains.homework.models import HomeworkAssignment
from apps.domains.homework.serializers.homework_assignment_serializer import (
    HomeworkAssignmentRowSerializer,
    HomeworkAssignmentUpdateSerializer,
)
from apps.support.homework.view_dependencies import (
    active_enrollment_ids_for_session,
    active_session_enrollments_for_session,
    get_homework_for_assignment,
)


class HomeworkAssignmentManageView(APIView):
    permission_classes = [IsAuthenticated, TenantResolvedAndStaff]

    def _get_homework(self, request, *, for_update: bool = False):
        hid = request.query_params.get("homework_id")
        if not hid:
            raise ValueError("homework_id is required")

        tenant = getattr(request, "tenant", None)

        return get_homework_for_assignment(
            homework_id=int(hid),
            tenant=tenant,
            for_update=for_update,
        )

    def get(self, request):
        try:
            homework = self._get_homework(request)
        except Exception as e:
            return Response({"detail": str(e)}, status=400)

        tenant = getattr(request, "tenant", None)
        session_id = homework.session_id

        # ✅ 퇴원(INACTIVE) 수강생 제외
        session_enrollments = active_session_enrollments_for_session(
            tenant=tenant,
            session_id=session_id,
        )

        selected_ids: Set[int] = set(
            HomeworkAssignment.objects
            .filter(
                tenant=tenant,
                homework=homework,
            )
            .values_list("enrollment_id", flat=True)
        )

        items: List[dict] = []
        request_obj = request
        for se in session_enrollments:
            enrollment = getattr(se, "enrollment", None)
            student_name = ""
            profile_photo_url = None
            lecture_title = ""
            lecture_color = ""
            lecture_chip_label = ""

            # 학생 상세 필드
            parent_phone = ""
            student_phone = ""
            school = ""
            grade = None

            if enrollment is not None:
                student = getattr(enrollment, "student", None)
                if student is not None:
                    student_name = str(getattr(student, "name", "") or "")
                    if getattr(student, "profile_photo", None):
                        try:
                            profile_photo_url = request_obj.build_absolute_uri(
                                student.profile_photo.url
                            )
                        except Exception:
                            profile_photo_url = None
                    parent_phone = getattr(student, "parent_phone", "") or ""
                    student_phone = getattr(student, "phone", "") or ""
                    school_type = getattr(student, "school_type", None)
                    if school_type == "ELEMENTARY":
                        school = getattr(student, "elementary_school", "") or ""
                    elif school_type == "HIGH":
                        school = getattr(student, "high_school", "") or ""
                    else:
                        school = getattr(student, "middle_school", "") or ""
                    grade = getattr(student, "grade", None)

                lecture = getattr(enrollment, "lecture", None)
                if lecture is not None:
                    lecture_title = str(getattr(lecture, "title", "") or "")
                    lecture_color = str(getattr(lecture, "color", "") or "") or "#3b82f6"
                    lecture_chip_label = str(getattr(lecture, "chip_label", "") or "") or ""

            items.append(
                {
                    "enrollment_id": se.enrollment_id,
                    "student_name": student_name,
                    "is_selected": se.enrollment_id in selected_ids,
                    "profile_photo_url": profile_photo_url,
                    "lecture_title": lecture_title or None,
                    "lecture_color": lecture_color or None,
                    "lecture_chip_label": lecture_chip_label or None,
                    "parent_phone": parent_phone or None,
                    "student_phone": student_phone or None,
                    "school": school or None,
                    "grade": grade,
                }
            )

        return Response(
            {
                "homework_id": homework.id,
                "session_id": session_id,
                "items": HomeworkAssignmentRowSerializer(items, many=True).data,
            }
        )

    @transaction.atomic
    def put(self, request):
        try:
            homework = self._get_homework(request, for_update=True)
        except Exception as e:
            return Response({"detail": str(e)}, status=400)

        tenant = getattr(request, "tenant", None)

        ser = HomeworkAssignmentUpdateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        incoming_ids = set(map(int, ser.validated_data["enrollment_ids"]))

        valid_ids = active_enrollment_ids_for_session(
            tenant=tenant,
            session_id=homework.session_id,
        )

        invalid = list(incoming_ids - valid_ids)
        if invalid:
            return Response(
                {
                    "detail": "세션 등록 학생이 아닌 enrollment_id 포함",
                    "invalid_enrollment_ids": invalid,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        existing_ids = set(
            HomeworkAssignment.objects.filter(
                tenant=tenant,
                homework=homework,
                session=homework.session,
                enrollment_id__in=valid_ids,
            ).values_list("enrollment_id", flat=True)
        )
        removed_ids = sorted(existing_ids - incoming_ids)
        added_ids = sorted(incoming_ids - existing_ids)

        # Replace only the currently editable (active session roster) rows.
        # Inactive students are hidden from this editor, but their historical
        # assignment and score relationship must remain intact.
        HomeworkAssignment.objects.filter(
            tenant=tenant,
            homework=homework,
            session=homework.session,
            enrollment_id__in=valid_ids,
        ).delete()

        HomeworkAssignment.objects.bulk_create(
            [
                HomeworkAssignment(
                    tenant=tenant,
                    homework=homework,
                    session=homework.session,
                    enrollment_id=eid,
                )
                for eid in sorted(incoming_ids)
            ]
        )

        removed_clinic_link_count = 0
        if removed_ids:
            resolve_removed_source_clinic_links = import_module(
                "apps.domains.progress.dispatcher"
            ).resolve_removed_source_clinic_links

            removed_clinic_link_count = resolve_removed_source_clinic_links(
                tenant_id=int(tenant.id),
                session_id=int(homework.session_id),
                source_type="homework",
                source_id=int(homework.id),
                enrollment_ids=removed_ids,
                user_id=getattr(request.user, "id", None),
                reason="homework_assignment_removed",
            )

        if added_ids:
            schedule_homework_target_progress = import_module(
                "apps.domains.progress.dispatcher"
            ).schedule_homework_target_progress
            schedule_homework_target_progress(
                session_id=int(homework.session_id), enrollment_ids=added_ids,
            )

        return Response(
            {
                "homework_id": homework.id,
                "selected_count": len(incoming_ids),
                "removed_assignment_count": len(removed_ids),
                "removed_clinic_link_count": int(removed_clinic_link_count),
            },
            status=status.HTTP_200_OK,
        )
