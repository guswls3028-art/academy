# PATH: apps/domains/attendance/views.py

import logging
from django.core import signing
from django.db import transaction
from django.db.models import Case, IntegerField, Value, When
from rest_framework.viewsets import ModelViewSet
from rest_framework.decorators import action
from rest_framework.filters import SearchFilter
from rest_framework.pagination import PageNumberPagination
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.response import Response
from rest_framework import status
from rest_framework.exceptions import NotFound

from academy.adapters.db.django import repositories_enrollment as enroll_repo
from apps.core.parsing import parse_bool
from apps.api.common.query_params import parse_query_int
from .models import Attendance
from .serializers import (
    AttendanceSerializer,
    AttendanceMatrixStudentSerializer,
)
from .filters import AttendanceFilter
from .services import create_attendance_roster
from .services.arrival_overview import build_arrival_overview

from rest_framework.permissions import IsAuthenticated
from apps.core.permissions import TenantResolvedAndStaff

from apps.support.attendance.view_dependencies import (
    Enrollment,
    ExamEnrollment,
    HomeworkAssignment,
    Session,
    SessionEnrollment,
    compute_clinic_highlight_map,
    deactivate_fees_for_enrollment,
    dispatch_job,
    get_exams_for_session,
)

logger = logging.getLogger(__name__)

BULK_PRESENT_UNDO_SALT = "attendance.bulk-present.undo.v1"
BULK_PRESENT_UNDO_MAX_AGE_SECONDS = 600
ATTENDANCE_STATUS_ORDER = (
    "UNSET",
    "PRESENT",
    "ONLINE",
    "SUPPLEMENT",
    "LATE",
    "EARLY_LEAVE",
    "ABSENT",
    "RUNAWAY",
    "MATERIAL",
    "INACTIVE",
    "SECESSION",
)
ATTENDANCE_ORDERING_FIELDS = {
    "id": "id",
    "name": "enrollment__student__name",
    "status": "_attendance_status_order",
    "parent_phone": "enrollment__student__parent_phone",
    "phone": "enrollment__student__phone",
}


def _apply_attendance_ordering(queryset, requested_ordering):
    """Apply one whitelisted, stable ordering before list pagination."""
    raw_ordering = (requested_ordering or "name").strip()
    descending = raw_ordering.startswith("-")
    key = raw_ordering[1:] if descending else raw_ordering
    if key not in ATTENDANCE_ORDERING_FIELDS:
        key = "name"
        descending = False

    if key == "status":
        queryset = queryset.annotate(
            _attendance_status_order=Case(
                *(
                    When(status=status_code, then=Value(rank))
                    for rank, status_code in enumerate(ATTENDANCE_STATUS_ORDER)
                ),
                default=Value(len(ATTENDANCE_STATUS_ORDER)),
                output_field=IntegerField(),
            )
        )

    prefix = "-" if descending else ""
    ordering = [f"{prefix}{ATTENDANCE_ORDERING_FIELDS[key]}"]
    if key not in {"id", "name"}:
        ordering.append("enrollment__student__name")
    if key != "id":
        ordering.append("id")
    return queryset.order_by(*ordering)


def _secession_status_conflict(instance, requested_status):
    if (
        instance.status == "SECESSION"
        and requested_status is not None
        and requested_status != "SECESSION"
    ):
        return Response(
            {"detail": "퇴원 처리된 출결 상태는 일반 수정으로 되돌릴 수 없습니다."},
            status=status.HTTP_409_CONFLICT,
        )
    return None


class AttendanceListPagination(PageNumberPagination):
    """출결 목록 — 학생 도메인과 동일하게 page_size 쿼리 허용, 총계 표기용 count 반환."""
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 500


class AttendanceViewSet(ModelViewSet):
    """
    lectures/attendance
    """

    serializer_class = AttendanceSerializer
    permission_classes = [IsAuthenticated, TenantResolvedAndStaff]
    pagination_class = AttendanceListPagination

    filter_backends = [DjangoFilterBackend, SearchFilter]
    filterset_class = AttendanceFilter
    search_fields = [
        "enrollment__student__name",
        "enrollment__student__phone",
        "enrollment__student__parent_phone",
    ]

    def get_queryset(self):
        tenant = getattr(self.request, "tenant", None)
        qs = (
            Attendance.objects
            .filter(tenant=tenant)
            .filter(enrollment__student__deleted_at__isnull=True)
            .select_related(
                "session",
                "session__lecture",
                "enrollment",
                "enrollment__student",
            )
        )
        action = getattr(self, "action", None)
        if action == "list":
            qs = _apply_attendance_ordering(
                qs,
                self.request.query_params.get("ordering"),
            )
        elif action in {"destroy", "partial_update", "update"}:
            qs = qs.select_for_update()
        return qs

    def perform_create(self, serializer):
        tenant = getattr(self.request, "tenant", None)
        if not tenant:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("Tenant is required.")
        serializer.save(tenant=tenant)

    def create(self, request, *args, **kwargs):
        return Response(
            {"detail": "출결 등록은 bulk_create 엔드포인트를 사용해야 합니다."},
            status=status.HTTP_405_METHOD_NOT_ALLOWED,
        )

    @action(detail=False, methods=["get"], url_path="arrival-overview")
    def arrival_overview(self, request):
        tenant = getattr(request, "tenant", None)
        if tenant is None:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("Tenant is required.")
        return Response(build_arrival_overview(tenant=tenant))

    @transaction.atomic
    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        conflict = _secession_status_conflict(instance, request.data.get("status"))
        if conflict is not None:
            return conflict
        return super().update(request, *args, **kwargs)

    @transaction.atomic
    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        tenant = getattr(request, "tenant", None)
        enrollment = instance.enrollment
        session = instance.session
        exam_ids = list(get_exams_for_session(session).values_list("id", flat=True))

        instance.delete()
        counts = self._remove_session_targets(tenant=tenant, enrollment=enrollment, session=session, exam_ids=exam_ids)

        logger.info(
            "ATTENDANCE_DELETE enrollment_id=%s session_id=%s tenant_id=%s — "
            "attendance removed, session_enrollments=%s, exam_enrollments=%s, homework_assignments=%s",
            enrollment.id, session.id, tenant.id if tenant else None, *counts,
        )
        return Response(status=status.HTTP_204_NO_CONTENT)

    @staticmethod
    def _remove_session_targets(*, tenant, enrollment, session, exam_ids):
        session_enrollment_deleted, _ = SessionEnrollment.objects.filter(
            tenant=tenant,
            session=session,
            enrollment=enrollment,
        ).delete()

        exam_enrollment_deleted = 0
        if exam_ids:
            remaining_exam_ids = set(
                SessionEnrollment.objects.filter(
                    tenant=tenant,
                    enrollment=enrollment,
                    session__exams__id__in=exam_ids,
                )
                .values_list("session__exams__id", flat=True)
                .distinct()
            )
            removable_exam_ids = [exam_id for exam_id in exam_ids if exam_id not in remaining_exam_ids]
            exam_enrollment_deleted, _ = ExamEnrollment.objects.filter(
                exam_id__in=removable_exam_ids,
                enrollment=enrollment,
                enrollment__tenant=tenant,
            ).delete()

        homework_assignment_deleted, _ = HomeworkAssignment.objects.filter(
            tenant=tenant,
            session=session,
            enrollment=enrollment,
        ).delete()

        return session_enrollment_deleted, exam_enrollment_deleted, homework_assignment_deleted

    # =========================================================
    # 0️⃣ 퇴원 처리 (SECESSION → 수강등록 비활성화 + 시험/과제 대상 제외)
    # =========================================================
    @transaction.atomic
    def partial_update(self, request, *args, **kwargs):
        instance = self.get_object()
        new_status = request.data.get("status")

        conflict = _secession_status_conflict(instance, new_status)
        if conflict is not None:
            return conflict

        if new_status == "SECESSION":
            # Omitted scope preserves the existing client contract during rollout.
            scope = request.data.get("secession_scope", "lecture")
            if scope not in ("session", "lecture"):
                return Response(
                    {"detail": "퇴원 범위는 session 또는 lecture여야 합니다."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if not parse_bool(request.data.get("confirm_secession", False), field_name="confirm_secession"):
                return Response(
                    {"detail": "퇴원 처리는 confirm_secession: true를 포함해야 합니다."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            tenant = getattr(request, "tenant", None)
            enrollment = instance.enrollment

            if scope == "session":
                self._remove_session_targets(
                    tenant=tenant, enrollment=enrollment, session=instance.session,
                    exam_ids=list(get_exams_for_session(instance.session).values_list("id", flat=True)),
                )
                instance.status = "SECESSION"
                instance.save(update_fields=["status"])
                logger.info(
                    "SESSION_SECESSION enrollment_id=%s session_id=%s tenant_id=%s",
                    enrollment.id, instance.session_id, tenant.id,
                )
                return Response(AttendanceSerializer(instance).data)

            if instance.status == "SECESSION" and enrollment.status == "INACTIVE":
                return Response(AttendanceSerializer(instance).data)

            # 수강등록 비활성화
            Enrollment.objects.filter(
                id=enrollment.id, tenant=tenant
            ).update(status="INACTIVE")
            deactivate_fees_for_enrollment(enrollment)

            # 해당 수강등록의 모든 출결을 SECESSION으로 변경
            Attendance.objects.filter(
                tenant=tenant, enrollment=enrollment
            ).update(status="SECESSION")

            # 시험 응시 대상에서 제거 (enrollment__tenant 방어 필터)
            ExamEnrollment.objects.filter(
                enrollment=enrollment, enrollment__tenant=tenant,
            ).delete()

            # 과제 대상에서 제거
            HomeworkAssignment.objects.filter(
                tenant=tenant, enrollment=enrollment
            ).delete()

            logger.info(
                "SECESSION enrollment_id=%s student_id=%s tenant_id=%s — "
                "enrollment INACTIVE, exam/homework enrollments removed",
                enrollment.id,
                enrollment.student_id,
                tenant.id,
            )

            instance.refresh_from_db()
            return Response(AttendanceSerializer(instance).data)

        # 일반 강의 출결 저장은 알림 발송과 분리한다. 입실/결석 안내는
        # 전용 preview → confirm 수동 발송 경로에서만 요청할 수 있다.
        return super().partial_update(request, *args, **kwargs)

    # =========================================================
    # 0-1️⃣ 전체 현장 출석 (세션 내 모든 출결을 PRESENT로 일괄 변경)
    # =========================================================
    @transaction.atomic
    @action(detail=False, methods=["post"], url_path="bulk_set_present")
    def bulk_set_present(self, request):
        tenant = getattr(request, "tenant", None)
        session_id = request.data.get("session")
        if not session_id:
            return Response(
                {"detail": "session은 필수입니다"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        session = Session.objects.select_related("lecture").filter(id=session_id).first()
        if not session:
            raise NotFound("세션을 찾을 수 없습니다.")
        if session.lecture.tenant_id != tenant.id:
            raise NotFound("세션을 찾을 수 없습니다.")

        # 변경 대상과 직전 상태를 잠근 채 수집한다. 응답의 서명 토큰으로만
        # 같은 테넌트/차시에서 원자적으로 되돌릴 수 있다.
        target_qs = Attendance.objects.filter(
            tenant=tenant, session=session,
        ).exclude(
            status="PRESENT",
        ).exclude(
            status="SECESSION",
        ).exclude(
            enrollment__status="INACTIVE",
        )
        target_rows = list(
            target_qs.select_for_update()
            .order_by("id")
            .values_list("id", "status")
        )
        target_ids = [attendance_id for attendance_id, _ in target_rows]

        updated = Attendance.objects.filter(id__in=target_ids).update(status="PRESENT")

        undo_token = None
        if target_rows:
            undo_token = signing.dumps(
                {
                    "version": 1,
                    "tenant_id": tenant.id,
                    "session_id": session.id,
                    "changes": target_rows,
                },
                salt=BULK_PRESENT_UNDO_SALT,
                compress=True,
            )

        # 일반 강의 전체 출석은 행정 작업 — 알림톡 발송하지 않음.
        # 입실/결석 안내는 출결 저장과 분리된 수동 preview → confirm 경로만 사용한다.

        return Response(
            {
                "updated": updated,
                "session": session.id,
                "undo_token": undo_token,
                "undo_expires_in": BULK_PRESENT_UNDO_MAX_AGE_SECONDS if undo_token else None,
            },
            status=status.HTTP_200_OK,
        )

    # =========================================================
    # 0-2️⃣ 전체 현장 출석 되돌리기 (서명된 직전 상태를 원자적으로 복원)
    # =========================================================
    @transaction.atomic
    @action(detail=False, methods=["post"], url_path="bulk_undo_present")
    def bulk_undo_present(self, request):
        tenant = getattr(request, "tenant", None)
        undo_token = request.data.get("undo_token")
        if not isinstance(undo_token, str) or not undo_token:
            return Response(
                {"detail": "undo_token은 필수입니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            payload = signing.loads(
                undo_token,
                salt=BULK_PRESENT_UNDO_SALT,
                max_age=BULK_PRESENT_UNDO_MAX_AGE_SECONDS,
            )
        except signing.SignatureExpired:
            return Response(
                {"detail": "되돌리기 가능 시간이 지났습니다. 현재 출결을 확인해 주세요."},
                status=status.HTTP_409_CONFLICT,
            )
        except signing.BadSignature:
            return Response(
                {"detail": "유효하지 않은 되돌리기 요청입니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not isinstance(payload, dict) or payload.get("version") != 1:
            return Response(
                {"detail": "지원하지 않는 되돌리기 요청입니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if payload.get("tenant_id") != tenant.id:
            raise NotFound("되돌릴 출결 기록을 찾을 수 없습니다.")

        session_id = payload.get("session_id")
        changes = payload.get("changes")
        if not isinstance(session_id, int) or not isinstance(changes, list) or not changes:
            return Response(
                {"detail": "되돌릴 출결 기록이 없습니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        previous_status_by_id = {}
        valid_statuses = {choice for choice, _ in Attendance._meta.get_field("status").choices}
        for change in changes:
            if (
                not isinstance(change, (list, tuple))
                or len(change) != 2
                or not isinstance(change[0], int)
                or not isinstance(change[1], str)
                or change[1] not in valid_statuses
                or change[1] in {"PRESENT", "SECESSION"}
            ):
                return Response(
                    {"detail": "유효하지 않은 출결 복원 정보입니다."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            previous_status_by_id[change[0]] = change[1]

        if len(previous_status_by_id) != len(changes):
            return Response(
                {"detail": "중복된 출결 복원 정보입니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        session = (
            Session.objects.select_related("lecture")
            .filter(id=session_id, lecture__tenant=tenant)
            .first()
        )
        if not session:
            raise NotFound("세션을 찾을 수 없습니다.")

        rows = list(
            Attendance.objects.select_for_update()
            .filter(
                tenant=tenant,
                session=session,
                id__in=previous_status_by_id,
            )
            .select_related("enrollment")
            .order_by("id")
        )
        if (
            len(rows) != len(previous_status_by_id)
            or any(row.status != "PRESENT" or row.enrollment.status == "INACTIVE" for row in rows)
        ):
            return Response(
                {"detail": "일부 출결이 이미 변경되어 안전하게 되돌릴 수 없습니다. 현재 상태를 확인해 주세요."},
                status=status.HTTP_409_CONFLICT,
            )

        for row in rows:
            row.status = previous_status_by_id[row.id]
        Attendance.objects.bulk_update(rows, ["status"])

        return Response(
            {"restored": len(rows), "session": session.id},
            status=status.HTTP_200_OK,
        )

    # =========================================================
    # 1️⃣ 세션 기준 학생 등록
    # =========================================================
    @action(detail=False, methods=["post"])
    def bulk_create(self, request):
        tenant = getattr(request, "tenant", None)

        session_id = request.data.get("session")
        student_ids = request.data.get("students", [])

        created = create_attendance_roster(
            tenant=tenant,
            session_id=session_id,
            student_ids=student_ids,
        )

        # 차시 학생 등록(bulk_create)은 행정 작업 — 입실 알림톡을 발송하지 않음.
        # 일반 강의 입실/결석 안내는 별도 수동 preview → confirm 경로만 사용한다.

        return Response(
            AttendanceSerializer(created, many=True).data,
            status=status.HTTP_201_CREATED,
        )

    # =========================================================
    # 2️⃣ 강의 × 차시 출결 매트릭스
    # =========================================================
    @action(detail=False, methods=["get"], url_path="matrix")
    def matrix(self, request):
        tenant = getattr(request, "tenant", None)

        lecture_id = parse_query_int(request.query_params, "lecture", min_value=1)
        if lecture_id is None:
            return Response(
                {"detail": "lecture 파라미터는 필수입니다"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        lecture = enroll_repo.get_lecture_by_id_tenant_raw(lecture_id, tenant)
        if not lecture:
            raise NotFound("강의를 찾을 수 없습니다.")

        sessions = enroll_repo.get_sessions_for_lecture_ordered(lecture)

        enrollment_ids = list(enroll_repo.get_session_enrollment_enrollment_ids(tenant, lecture))
        enrollments = enroll_repo.get_enrollments_by_ids_all(enrollment_ids, tenant)

        attendances = enroll_repo.get_attendances_for_lecture(tenant, lecture, enrollments)

        attendance_map = {
            (a.enrollment_id, a.session_id): a
            for a in attendances
        }

        # ✅ 클리닉 하이라이트 일괄 계산
        highlight_map = compute_clinic_highlight_map(
            tenant=tenant,
            enrollment_ids=set(en.id for en in enrollments),
        )

        students_payload = []

        for en in enrollments:
            # profile_photo_url
            profile_photo_url = None
            r2_key = getattr(en.student, "profile_photo_r2_key", None) or ""
            if r2_key:
                try:
                    from django.conf import settings as _settings
                    from academy.adapters.storage.r2_presign import create_presigned_get_url
                    profile_photo_url = create_presigned_get_url(r2_key, expires_in=3600, bucket=_settings.R2_STORAGE_BUCKET)
                except Exception:
                    pass

            row = {
                "student_id": en.student.id,
                "name": en.student.name,
                "phone": en.student.phone,
                "parent_phone": en.student.parent_phone,
                "profile_photo_url": profile_photo_url,
                "name_highlight_clinic_target": highlight_map.get(en.id, False),
                "attendance": {},
            }

            for s in sessions:
                att = attendance_map.get((en.id, s.id))
                if att:
                    row["attendance"][str(s.id)] = {
                        "attendance_id": att.id,
                        "status": att.status,
                    }

            students_payload.append(row)

        return Response(
            {
                "lecture": {
                    "id": lecture.id,
                    "title": lecture.title,
                    "color": (lecture.color or "#3b82f6"),
                },
                "sessions": [
                    {
                        "id": s.id,
                        "order": s.order,
                        "session_type": s.session_type,
                        "regular_order": s.regular_order,
                        "display_label": s.display_label,
                        "title": s.title,
                        "date": s.date,
                    }
                    for s in sessions
                ],
                "students": AttendanceMatrixStudentSerializer(
                    students_payload, many=True
                ).data,
            }
        )

    # =========================================================
    # 3️⃣ 엑셀 내보내기 (워커 비동기)
    # POST /api/v1/lectures/attendance/excel/ body: { "lecture_id": int }
    # 응답: { "job_id", "status": "PENDING" } → 클라이언트는 GET /api/v1/jobs/<job_id>/ 폴링 후 result.download_url 로 다운로드
    # =========================================================
    @action(detail=False, methods=["post"], url_path="excel")
    def excel(self, request):
        tenant = getattr(request, "tenant", None)
        if not tenant:
            return Response(
                {"detail": "tenant가 필요합니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        lecture_id = request.data.get("lecture_id") or request.query_params.get("lecture")
        if not lecture_id:
            return Response(
                {"detail": "lecture_id(또는 lecture)는 필수입니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        lecture = enroll_repo.get_lecture_by_id_tenant_raw(lecture_id, tenant)
        if not lecture:
            raise NotFound("강의를 찾을 수 없습니다.")

        out = dispatch_job(
            job_type="attendance_excel_export",
            payload={
                "tenant_id": str(tenant.id),
                "lecture_id": int(lecture.id),
            },
            tenant_id=str(tenant.id),
            source_domain="attendance",
            source_id=str(lecture.id),
            tier="basic",
        )
        if not out.get("ok"):
            return Response(
                {"detail": out.get("error", "job 등록 실패")},
                status=status.HTTP_400_BAD_REQUEST,
            )
        logger.info(
            "ATTENDANCE_EXCEL_EXPORT dispatch job_id=%s tenant_id=%s lecture_id=%s",
            out["job_id"],
            tenant.id,
            lecture.id,
        )
        return Response(
            {"job_id": out["job_id"], "status": "PENDING"},
            status=status.HTTP_202_ACCEPTED,
        )
