# PATH: apps/domains/students/views/student_views.py

import logging
import uuid

from django.db import transaction
from django.utils import timezone
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Exists, F, OuterRef, Value
from django.db.models.functions import NullIf, Trim

from rest_framework.viewsets import ModelViewSet
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.pagination import PageNumberPagination
from rest_framework.exceptions import APIException, NotFound, ValidationError

from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import OrderingFilter

from apps.core.parsing import parse_bool
from apps.api.common.query_params import parse_query_bool
from apps.api.common.upload_validation import (
    DEFAULT_MAX_EXCEL_SIZE,
    EXCEL_CONTENT_TYPES,
    EXCEL_EXTENSIONS,
    validate_uploaded_file,
)
from apps.core.permissions import IsStudent, TenantResolvedAndStaff
from apps.core.models import TenantMembership
from apps.core.models.user import user_display_username

from apps.infrastructure.storage.r2 import delete_object_r2_excel, upload_fileobj_to_r2_excel
from apps.support.students.view_dependencies import (
    dispatch_job,
    get_excel_parsing_job_status_response,
    protect_excel_initial_password,
    send_event_notification,
)

from academy.adapters.db.django import repositories_students as student_repo
from ..models import Student
from ..filters import StudentFilter, StudentSearchFilter
from ..ordering import student_name_codepoint_ordering
from ..selectors import student_for_tenant_user, students_for_tenant
from ..services import (
    StudentLifecycleError,
    StudentImportPasswordError,
    StudentProfileUpdateError,
    build_student_import_password_policy,
    create_student_account,
    import_students_from_rows,
    permanently_delete_students,
    resolve_student_import_conflicts,
    restore_student,
    soft_delete_student,
    update_student_profile,
)
from ..serializers import (
    StudentListSerializer,
    StudentDetailSerializer,
    AddTagSerializer,
    StudentBulkCreateSerializer,
)

logger = logging.getLogger(__name__)


# ======================================================
# Student
# ======================================================

class AccountNoticeDeliveryFailed(APIException):
    status_code = 503
    default_detail = "계정 안내 알림톡 발송에 실패했습니다. 잠시 후 다시 시도해 주세요."
    default_code = "account_notice_delivery_failed"


class StudentProfilePhotoStorageFailed(APIException):
    status_code = 503
    default_detail = "프로필 사진 저장에 실패했습니다. 잠시 후 다시 시도해 주세요."
    default_code = "student_profile_photo_storage_failed"


class StudentListPagination(PageNumberPagination):
    """SSOT: 프론트엔드가 총 개수(count)와 results를 기대하므로 응답에 count 포함."""
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 100

    def get_paginated_response(self, data):
        return Response({
            "count": self.page.paginator.count,
            "page_size": self.page.paginator.per_page,
            "next": self.get_next_link(),
            "previous": self.get_previous_link(),
            "results": data,
        })


class StableStudentOrderingFilter(OrderingFilter):
    """Keep every student list deterministic, with 가나다순 as the fallback."""

    def get_ordering(self, request, queryset, view):
        ordering = list(super().get_ordering(request, queryset, view) or ["name", "id"])
        fields = [field.lstrip("-") for field in ordering]
        primary_descending = ordering[0].startswith("-")

        if "name" not in fields and fields[0] != "id":
            id_ordering = None
            if "id" in fields:
                id_index = fields.index("id")
                id_ordering = ordering.pop(id_index)
                fields.pop(id_index)
            ordering.append("name")
            fields.append("name")
            if id_ordering is not None:
                ordering.append(id_ordering)
                fields.append("id")

        if "id" not in fields:
            ordering.append("-id" if primary_descending else "id")
        return student_name_codepoint_ordering(ordering, name_field="name")


class StudentViewSet(ModelViewSet):
    """
    학생 관리 ViewSet

    ✔ tenant 단위 완전 분리
    ✔ 학생 생성 시 User 계정 자동 생성
    ✔ phone = username
    ✔ 초기 비밀번호는 교사가 설정
    ✔ 학생 CRUD는 관리자만 가능

    ✅ 봉인 강화:
    - Student 생성 시 TenantMembership(role=student) 반드시 생성
    - Student 삭제 시 User 삭제(고아유저 방지)
    """

    permission_classes = [IsAuthenticated, TenantResolvedAndStaff]
    pagination_class = StudentListPagination

    # ------------------------------
    # Tenant-aware QuerySet
    # ------------------------------
    def get_queryset(self):
        """
        🔐 핵심 보안 포인트
        - request.tenant 기준으로만 학생 노출
        - list: ?deleted=true 시 삭제된 학생만, 기본은 활성 학생만
        """
        qs = students_for_tenant(self.request.tenant, deleted="any")
        qs = qs.select_related("user").annotate(
            _account_access_active=Exists(
                TenantMembership.objects.filter(
                    tenant=self.request.tenant,
                    user_id=OuterRef("user_id"),
                    role="student",
                    is_active=True,
                    user__is_active=True,
                )
            )
        )

        if self.action == "list":
            show_deleted = parse_query_bool(
                self.request.query_params,
                "deleted",
                default=False,
            )
            if show_deleted:
                qs = qs.filter(deleted_at__isnull=False)
            else:
                qs = qs.filter(deleted_at__isnull=True)
            qs = qs.prefetch_related("tags", "enrollments__lecture")
        elif self.action == "retrieve":
            qs = qs.prefetch_related("tags", "enrollments__lecture")

        return qs

    # ------------------------------
    # Serializer 선택
    # ------------------------------
    def get_serializer_class(self):
        if self.action == "create":
            from ..serializers import StudentCreateSerializer
            return StudentCreateSerializer

        if self.action == "list":
            return StudentListSerializer

        if self.action in ("update", "partial_update"):
            from ..serializers import StudentUpdateSerializer
            return StudentUpdateSerializer

        return StudentDetailSerializer

    # ------------------------------
    # Student account graph 생성 (봉인)
    # ------------------------------
    @transaction.atomic
    def create(self, request, *args, **kwargs):
        """
        학생 생성 시 처리 흐름

        1. 삭제된 학생 체크 (전화번호 또는 이름+학부모전화)
        2. 입력값 검증 (StudentCreateSerializer)
        3. create_student_account SSOT로 Parent/User/Student/Membership 생성
        4. 계정 안내는 첫 실제 수강 확정 시 발송
        """
        tenant = request.tenant
        raw_data = request.data
        name = str(raw_data.get("name", "")).strip()
        parent_phone = str(raw_data.get("parent_phone", "")).strip()
        phone = str(raw_data.get("phone", "")).strip() if raw_data.get("phone") else None

        # 삭제된 학생 체크 (전화번호 또는 이름+학부모전화)
        deleted_student = None
        if phone:
            deleted_student = student_repo.student_filter_tenant_phone_deleted(tenant, phone).first()
        if not deleted_student and name and parent_phone:
            deleted_student = student_repo.student_filter_tenant_name_parent_phone_deleted(tenant, name, parent_phone)

        if deleted_student:
            return Response(
                {
                    "code": "deleted_student_exists",
                    "detail": "삭제 대기중인 학생입니다. 복구하시겠습니까?",
                    "deleted_student": StudentDetailSerializer(deleted_student, context={"request": request}).data,
                },
                status=409,
            )

        # 활성 학생 중복 체크 (전화번호 또는 이름+학부모전화)
        active_duplicate = None
        if phone:
            active_duplicate = Student.objects.filter(
                tenant=tenant, deleted_at__isnull=True, phone=phone
            ).first()
        if not active_duplicate and name and parent_phone:
            active_duplicate = student_repo.student_filter_tenant_name_parent_phone_active(
                tenant, name, parent_phone
            )
        if active_duplicate:
            return Response(
                {
                    "code": "duplicate_student",
                    "detail": "이미 있는 학생입니다.",
                    "existing_student": StudentDetailSerializer(active_duplicate, context={"request": request}).data,
                },
                status=409,
            )

        serializer = self.get_serializer(
            data=request.data,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)

        data = serializer.validated_data
        data.pop("send_welcome_message", None)

        password = data.pop("initial_password")

        result = create_student_account(
            tenant=request.tenant,
            student_data=data,
            password=password,
            must_change_password=True,
        )
        student = result.student

        output = StudentDetailSerializer(
            student,
            context={"request": request},
        )
        return Response(output.data, status=201)

    # ------------------------------
    # UPDATE: parent_phone 변경 시 parent FK 동기화
    # ------------------------------
    @transaction.atomic
    def perform_update(self, serializer):
        student_before = serializer.instance
        old_phone = student_before.phone or ""
        old_parent_phone = student_before.parent_phone or ""
        old_ps_number = student_before.ps_number or ""
        try:
            result = update_student_profile(
                student=serializer.instance,
                tenant=self.request.tenant,
                data=dict(serializer.validated_data),
                identity_field="ps_number",
            )
        except StudentProfileUpdateError as e:
            raise ValidationError(e.detail)
        serializer.instance = result.student
        student = result.student

        from apps.domains.students.services.account_notifications import (
            send_parent_account_credentials_notice,
            send_student_account_credentials_notice,
        )

        new_phone = student.phone or ""
        if (student.ps_number or "") != old_ps_number:
            if not send_student_account_credentials_notice(student=student):
                raise AccountNoticeDeliveryFailed()
        elif new_phone and new_phone != old_phone:
            if not send_student_account_credentials_notice(student=student, to=new_phone):
                raise AccountNoticeDeliveryFailed()

        if (student.parent_phone or "") != old_parent_phone:
            if not send_parent_account_credentials_notice(
                student=student,
                parent=getattr(student, "parent", None),
                parent_password=result.parent_password_for_notice,
                to=student.parent_phone,
            ):
                raise AccountNoticeDeliveryFailed()

    # ------------------------------
    # DELETE: 소프트 삭제 (30일 보관)
    # ------------------------------
    @transaction.atomic
    def destroy(self, request, *args, **kwargs):
        student = self.get_object()
        try:
            deletion = soft_delete_student(student, tenant=request.tenant)
        except StudentLifecycleError as e:
            if e.code == "already_deleted":
                return Response({"detail": e.detail}, status=400)
            raise ValidationError(e.detail)
        # 퇴원 알림 발송 (학부모)
        _student = deletion.student  # closure 캡처용
        _tenant = request.tenant
        _student_id = student.id
        _deleted_at = deletion.student.deleted_at.isoformat()
        transaction.on_commit(lambda: send_event_notification(
            tenant=_tenant, trigger="withdrawal_complete",
            student=_student, send_to="parent",
            context={
                "강의명": "-",
                "차시명": "-",
                "_domain_object_id": f"withdrawal:{_student_id}:{_deleted_at}",
            },
        ))
        return Response(status=204)

    # ------------------------------
    # Filtering / Searching / Ordering
    # ------------------------------
    filter_backends = [
        DjangoFilterBackend,
        StudentSearchFilter,
        StableStudentOrderingFilter,
    ]
    filterset_class = StudentFilter
    search_fields = ["ps_number", "omr_code", "name", "high_school", "middle_school", "major", "phone", "parent_phone"]
    ordering_fields = [
        "id",
        "created_at",
        "updated_at",
        "deleted_at",
        "name",
        "ps_number",
        "phone",
        "parent_phone",
        "is_managed",
        "gender",
        "high_school",
        "high_school_class",
        "grade",
    ]
    ordering = ["name", "id"]

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)
        requested = (self.request.query_params.get("ordering") or "").strip()
        if requested not in {"high_school_class", "-high_school_class"}:
            return queryset

        # 빈 반은 항상 마지막, 같은 반은 이름순으로 고정해 페이지를 넘겨도
        # 같은 학생이 앞뒤 페이지를 오가지 않게 한다.
        descending = requested.startswith("-")
        queryset = queryset.annotate(
            _normalized_high_school_class=NullIf(
                Trim("high_school_class"),
                Value(""),
            )
        )
        class_order = F("_normalized_high_school_class")
        class_order = (
            class_order.desc(nulls_last=True)
            if descending
            else class_order.asc(nulls_last=True)
        )
        return queryset.order_by(
            class_order,
            *student_name_codepoint_ordering(
                ("name", "id"),
                name_field="name",
            ),
        )

    # ------------------------------
    # Tag 관리
    # ------------------------------
    @action(detail=True, methods=["post"])
    def add_tag(self, request, pk=None):
        student = self.get_object()
        serializer = AddTagSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # 🔐 tenant-scoped tag lookup: 다른 테넌트 태그 연결 방지
        tag = student_repo.tag_get(serializer.validated_data["tag_id"], tenant=request.tenant)
        student_repo.student_tag_get_or_create(student, tag)

        return Response({"status": "ok"}, status=201)

    @action(detail=True, methods=["post"])
    def remove_tag(self, request, pk=None):
        student = self.get_object()
        serializer = AddTagSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        student_repo.student_tag_filter_delete(student, serializer.validated_data["tag_id"])

        return Response({"status": "ok"}, status=200)

    # --------------------------------------------------
    # 엑셀 일괄 등록 (워커 전용) — 파일 업로드 → excel_parsing job
    # --------------------------------------------------
    @action(detail=False, methods=["post"], url_path="bulk_create_from_excel")
    def bulk_create_from_excel(self, request):
        """
        학생 엑셀 일괄 등록 — 워커 전담.
        POST: multipart — file (엑셀), password_mode,
        initial_password(fixed 방식일 때).
        응답: 202 { job_id, status }.
        """
        import logging
        tenant = getattr(request, "tenant", None)
        if not tenant:
            return Response(
                {"detail": "tenant가 필요합니다."},
                status=400,
            )
        upload_file = request.FILES.get("file")
        initial_password = (request.data.get("initial_password") or "").strip()
        password_mode = (request.data.get("password_mode") or "fixed").strip()
        send_welcome = parse_bool(
            request.data.get("send_welcome_message", True),
            field_name="send_welcome_message",
        )
        if not upload_file:
            raise ValidationError({"detail": "file(엑셀)은 필수입니다."})
        try:
            password_policy = build_student_import_password_policy(
                password_mode=password_mode,
                initial_password=initial_password,
            )
        except StudentImportPasswordError as exc:
            raise ValidationError({"detail": str(exc)}) from exc
        validate_uploaded_file(
            upload_file,
            allowed_extensions=EXCEL_EXTENSIONS,
            allowed_content_types=EXCEL_CONTENT_TYPES,
            max_size=DEFAULT_MAX_EXCEL_SIZE,
            label="엑셀 파일",
        )

        try:
            ext = "xlsx"
            if getattr(upload_file, "name", "") and "." in upload_file.name:
                ext = upload_file.name.rsplit(".", 1)[-1].lower() or "xlsx"
            file_key = f"excel/{tenant.id}/{uuid.uuid4().hex}.{ext}"
            bucket = getattr(settings, "R2_EXCEL_BUCKET", getattr(settings, "EXCEL_BUCKET_NAME", "academy-excel"))
            upload_fileobj_to_r2_excel(
                fileobj=upload_file,
                key=file_key,
                content_type=getattr(upload_file, "content_type", None)
                or "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            payload = {
                "file_key": file_key,
                "bucket": bucket,
                "tenant_id": tenant.id,
                "password_mode": password_policy.mode,
                **protect_excel_initial_password(password_policy.fixed_password),
                "send_welcome_message": send_welcome,
            }
            try:
                out = dispatch_job(
                    job_type="excel_parsing",
                    payload=payload,
                    tenant_id=str(tenant.id),
                    source_domain="students",
                    source_id=None,
                    tier="basic",
                    idempotency_key=f"excel:{file_key}",
                )
            except Exception:
                try:
                    delete_object_r2_excel(key=file_key)
                except Exception:
                    logger.exception(
                        "Failed to remove errored student Excel upload: key=%s",
                        file_key,
                    )
                raise
            if not out.get("ok"):
                try:
                    delete_object_r2_excel(key=file_key)
                except Exception:
                    logger.exception(
                        "Failed to remove rejected student Excel upload: key=%s",
                        file_key,
                    )
                return Response(
                    {"detail": out.get("error", "job 등록 실패")},
                    status=400,
                )
            return Response(
                {"job_id": out["job_id"], "status": "PENDING"},
                status=202,
            )
        except ValidationError:
            raise
        except Exception as e:
            logging.getLogger(__name__).exception("bulk_create_from_excel failed: %s", e)
            return Response(
                {"detail": "서버 오류가 발생했습니다.", "error": str(e)[:200]},
                status=500,
            )

    @action(detail=False, methods=["get"], url_path=r"excel_job_status/(?P<job_id>[^/.]+)")
    def excel_job_status(self, request, job_id=None):
        """
        엑셀 일괄등록(excel_parsing) job 상태 조회 (폴링용).
        GET /api/v1/students/excel_job_status/<job_id>/
        """
        tenant = getattr(request, "tenant", None)
        if not tenant:
            return Response({"detail": "tenant가 필요합니다."}, status=400)
        payload = get_excel_parsing_job_status_response(
            job_id=job_id,
            tenant_id=str(tenant.id),
        )
        if payload is None:
            raise NotFound("해당 job을 찾을 수 없습니다.")
        return Response(payload)

    # --------------------------------------------------
    # Anchor API: /students/me/ (원본 100% 유지)
    # --------------------------------------------------
    @action(
        detail=False,
        methods=["post"],
        url_path="bulk_create",
    )
    def bulk_create(self, request):
        """
        JSON 일괄 등록 (레거시·비엑셀용). 엑셀 등록은 bulk_create_from_excel + 워커 사용.
        POST body: { "initial_password": "...", "students": [ {...}, ... ] }
        """
        serializer = StudentBulkCreateSerializer(
            data=request.data,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)

        password = serializer.validated_data["initial_password"]
        students_data = serializer.validated_data["students"]

        if len(students_data) > 200:
            return Response(
                {"detail": "최대 200건까지 일괄 처리할 수 있습니다."},
                status=400,
            )
        send_welcome = True
        tenant = request.tenant

        result = import_students_from_rows(
            tenant_id=tenant.id,
            students_data=students_data,
            initial_password=password,
            send_welcome_message=send_welcome,
        )
        return Response(result, status=201)

    @action(
        detail=False,
        methods=["post"],
        url_path="bulk_resolve_conflicts",
    )
    def bulk_resolve_conflicts(self, request):
        """
        충돌 해결 후 재시도 — 삭제된 학생과 번호 충돌 시 복원 또는 영구 삭제 후 재등록
        POST body: {
          "initial_password": "...",
          "send_welcome_message": false,
          "resolutions": [ { "row": 1, "student_id": 123, "action": "restore"|"delete", "student_data": {...} } ]
        }
        """
        password = request.data.get("initial_password") or ""
        if len(str(password)) < 4:
            return Response({"detail": "초기 비밀번호는 4자 이상이어야 합니다."}, status=400)
        send_welcome = True
        resolutions = request.data.get("resolutions") or []
        if not isinstance(resolutions, (list, tuple)):
            return Response({"detail": "resolutions는 배열이어야 합니다."}, status=400)

        result = resolve_student_import_conflicts(
            tenant=request.tenant,
            resolutions=resolutions,
            initial_password=password,
            send_welcome_message=parse_bool(
                send_welcome,
                field_name="send_welcome_message",
            ),
        )
        return Response(result, status=200)

    @action(
        detail=False,
        methods=["post"],
        url_path="bulk_delete",
    )
    def bulk_delete(self, request):
        """
        선택 학생 일괄 소프트 삭제 (30일 보관)
        POST body: { "ids": [1, 2, 3, ...] }
        """
        ids = request.data.get("ids") or []
        if not isinstance(ids, (list, tuple)):
            return Response({"detail": "ids는 배열이어야 합니다."}, status=400)
        if len(ids) > 200:
            return Response({"detail": "최대 200건까지 일괄 처리할 수 있습니다."}, status=400)
        ids = [int(x) for x in ids if isinstance(x, (int, str)) and str(x).isdigit()]
        if not ids:
            return Response({"detail": "삭제할 ID가 없습니다."}, status=400)

        tenant = request.tenant
        to_delete = list(student_repo.student_filter_tenant_ids_active(tenant, ids))
        deleted_at = timezone.now()
        with transaction.atomic():
            for student in to_delete:
                soft_delete_student(student, tenant=tenant, deleted_at=deleted_at)
        return Response({"deleted": len(to_delete)}, status=200)

    @action(
        detail=False,
        methods=["post"],
        url_path="bulk_restore",
    )
    def bulk_restore(self, request):
        """
        삭제된 학생 일괄 복원
        POST body: { "ids": [1, 2, 3, ...] }
        """
        ids = request.data.get("ids") or []
        if not isinstance(ids, (list, tuple)):
            return Response({"detail": "ids는 배열이어야 합니다."}, status=400)
        ids = [int(x) for x in ids if isinstance(x, (int, str)) and str(x).isdigit()]
        if not ids:
            return Response({"detail": "복원할 ID가 없습니다."}, status=400)

        tenant = request.tenant
        to_restore = list(student_repo.student_filter_tenant_ids_deleted(tenant, ids))
        restored = []
        skipped = []
        for student in to_restore:
            try:
                restore_student(student, tenant=tenant)
            except StudentLifecycleError as exc:
                skipped.append({"id": student.id, "code": exc.code, "reason": exc.detail})
                continue
            restored.append(student.id)
        result = {"restored": len(restored)}
        if skipped:
            result["skipped"] = skipped
        return Response(result, status=200)

    @action(
        detail=False,
        methods=["post"],
        url_path="bulk_permanent_delete",
    )
    def bulk_permanent_delete(self, request):
        """
        삭제된 학생 즉시 영구 삭제
        POST body: { "ids": [1, 2, 3, ...] }
        """
        ids = request.data.get("ids") or []
        if not isinstance(ids, (list, tuple)):
            return Response({"detail": "ids는 배열이어야 합니다."}, status=400)
        ids = [int(x) for x in ids if isinstance(x, (int, str)) and str(x).isdigit()]
        if not ids:
            return Response({"detail": "삭제할 ID가 없습니다."}, status=400)

        tenant = request.tenant
        try:
            result = permanently_delete_students(tenant=tenant, student_ids=ids)
        except StudentLifecycleError as exc:
            return Response(
                {"code": exc.code, "detail": exc.detail},
                status=409,
            )
        except Exception as e:
            logger.exception(
                "bulk_permanent_delete failed: %s (student_ids=%s)",
                e, ids,
            )
            return Response(
                {"detail": f"영구 삭제 중 오류: {e}"},
                status=500,
            )
        return Response(
            {
                "deleted": result.deleted_count,
                "storage_cleanup": {
                    "pending": result.storage_cleanup_pending_count,
                    "failed": result.storage_cleanup_failed_count,
                },
            },
            status=200,
        )

    @action(
        detail=False,
        methods=["get"],
        url_path="deleted_duplicates_check",
    )
    def deleted_duplicates_check(self, request):
        """
        삭제된 학생 중 (이름+학부모전화) 중복 검사 — 고객 셀프 복구용.
        GET → { "duplicate_groups": int, "records_to_remove": int }
        """

        tenant = request.tenant
        dup_groups = student_repo.student_filter_tenant_deleted_dup_groups(tenant)
        groups_list = list(dup_groups)
        records_to_remove = sum(g["cnt"] - 1 for g in groups_list)
        return Response({
            "duplicate_groups": len(groups_list),
            "records_to_remove": records_to_remove,
        })

    @action(
        detail=False,
        methods=["post"],
        url_path="deleted_duplicates_fix",
    )
    def deleted_duplicates_fix(self, request):
        """
        삭제된 학생 중 (이름+학부모전화) 중복 정리 — 그룹당 1명만 유지, 나머지 영구 삭제.
        POST → { "removed": int }
        """
        tenant = request.tenant
        dup_groups = student_repo.student_filter_tenant_deleted_dup_groups(tenant)
        groups_list = list(dup_groups)
        if not groups_list:
            return Response({"removed": 0}, status=200)

        removed = 0
        with transaction.atomic():
            for g in groups_list:
                keep = student_repo.student_filter_dup_keep_first(
                    tenant.id, g["name"], g["parent_phone"]
                )
                to_remove = list(
                    student_repo.student_filter_dup_to_remove(
                        tenant.id, g["name"], g["parent_phone"], keep.id
                    )
                )
                result = permanently_delete_students(
                    tenant=tenant,
                    student_ids=[s.id for s in to_remove],
                )
                removed += result.deleted_count
        return Response({"removed": removed}, status=200)

    @action(
        detail=False,
        methods=["get", "patch"],
        url_path="me",
        permission_classes=[IsAuthenticated, IsStudent],
    )
    def me(self, request):
        """
        학생 본인 정보 조회 + 수정 (Anchor API)

        🔒 보안 포인트
        - request.user + request.tenant 기준 강제
        - 다른 학원 / 다른 학생 접근 불가
        """
        student = student_for_tenant_user(request.tenant, request.user)
        if not student:
            raise NotFound("학생 프로필이 없습니다.")

        if request.method == "GET":
            serializer = StudentDetailSerializer(
                student,
                context={"request": request},
            )
            return Response(serializer.data)

        # PATCH: 프로필 수정 (아이디 변경, 비밀번호 변경, 기본정보 수정)
        data = request.data
        tenant = request.tenant
        user = student.user
        old_phone = student.phone or ""
        old_parent_phone = student.parent_phone or ""

        # --- 기본 정보 필드 유효성 검증 (setattr 전에 수행) ---
        from ..services.school import ALL_SCHOOL_TYPES, get_valid_grades
        STRING_FIELD_LIMITS = {
            "name": 100, "phone": 20, "parent_phone": 20,
            "gender": 10, "address": 255, "elementary_school": 100,
            "high_school": 100, "middle_school": 100,
            "origin_middle_school": 100,
            "high_school_class": 100, "major": 50, "memo": None,
        }

        if "school_type" in data and data["school_type"] not in ALL_SCHOOL_TYPES:
            return Response(
                {"detail": f"school_type은 {sorted(ALL_SCHOOL_TYPES)} 중 하나여야 합니다."},
                status=400,
            )
        school_type_for_grade = data.get("school_type", student.school_type)
        if "grade" in data and data["grade"] is not None:
            try:
                grade_val = int(data["grade"])
            except (TypeError, ValueError):
                return Response(
                    {"detail": "grade는 정수여야 합니다."}, status=400,
                )
            valid_grades = get_valid_grades(school_type_for_grade)
            if grade_val not in valid_grades:
                return Response(
                    {"detail": f"grade는 {sorted(valid_grades)} 중 하나여야 합니다."},
                    status=400,
                )
        for field, max_len in STRING_FIELD_LIMITS.items():
            if field in data and data[field] is not None:
                if not isinstance(data[field], str):
                    return Response(
                        {"detail": f"{field}은(는) 문자열이어야 합니다."},
                        status=400,
                    )
                if max_len and len(data[field]) > max_len:
                    return Response(
                        {"detail": f"{field}은(는) {max_len}자 이하여야 합니다."},
                        status=400,
                    )

        username_changed = False
        current_pw = (data.get("current_password") or "").strip()
        new_pw = (data.get("new_password") or "").strip()
        password_changed = bool(current_pw and new_pw)
        if password_changed:
            if len(new_pw) < 4:
                return Response(
                    {"detail": "새 비밀번호는 4자 이상이어야 합니다."},
                    status=400,
                )

        with transaction.atomic():
            # 아이디 변경
            new_username = (data.get("username") or "").strip()
            if new_username and new_username != user_display_username(user):
                from apps.core.models.user import user_internal_username
                internal = user_internal_username(tenant, new_username)
                # 테넌트 내 중복 검사 (다른 테넌트는 같은 아이디 허용)
                if get_user_model().objects.filter(username=internal).exclude(id=user.id).exists():
                    return Response(
                        {"detail": "이미 사용 중인 아이디입니다."},
                        status=400,
                    )
                user.username = internal
                user.save(update_fields=["username"])
                # ps_number도 동기화
                student.ps_number = new_username
                student.save(update_fields=["ps_number"])
                username_changed = True

            # 비밀번호 변경
            if password_changed:
                from apps.core.services.password import (
                    CurrentPasswordMismatch,
                    PasswordNoticeDeliveryError,
                    change_password_with_notice,
                )
                from apps.domains.students.services.account_notifications import (
                    send_user_password_changed_notice,
                )
                try:
                    change_password_with_notice(
                        user,
                        current_password=current_pw,
                        new_password=new_pw,
                        send_notice=send_user_password_changed_notice,
                    )
                except CurrentPasswordMismatch:
                    raise ValidationError({"detail": "현재 비밀번호가 일치하지 않습니다."})
                except PasswordNoticeDeliveryError as exc:
                    raise AccountNoticeDeliveryFailed(str(exc))

            try:
                result = update_student_profile(
                    student=student,
                    tenant=tenant,
                    data=dict(data),
                    ignore_blank_name=True,
                )
                student = result.student
            except StudentProfileUpdateError as e:
                raise ValidationError(e.detail)

            from apps.domains.students.services.account_notifications import (
                send_parent_account_credentials_notice,
                send_student_account_credentials_notice,
            )

            new_phone = student.phone or ""
            phone_changed = bool(new_phone) and new_phone != old_phone
            if not password_changed and (username_changed or phone_changed):
                if not send_student_account_credentials_notice(
                    student=student,
                    to=new_phone if phone_changed else None,
                ):
                    raise AccountNoticeDeliveryFailed()

            if (student.parent_phone or "") != old_parent_phone:
                if not send_parent_account_credentials_notice(
                    student=student,
                    parent=getattr(student, "parent", None),
                    parent_password=result.parent_password_for_notice,
                    to=student.parent_phone,
                ):
                    raise AccountNoticeDeliveryFailed()

            # Legacy /students/me/ compatibility uses the same R2-only photo
            # boundary as /student-app/me/. Keep it last so earlier account
            # notice failures cannot leave an uploaded replacement behind.
            if "profile_photo" in request.FILES:
                from apps.domains.students.services.profile_photo import (
                    StudentProfilePhotoStorageError,
                    StudentProfilePhotoValidationError,
                    replace_student_profile_photo,
                )
                try:
                    student = replace_student_profile_photo(
                        student=student,
                        photo=request.FILES["profile_photo"],
                    )
                except StudentProfilePhotoValidationError as exc:
                    raise ValidationError({"detail": str(exc)})
                except StudentProfilePhotoStorageError as exc:
                    raise StudentProfilePhotoStorageFailed(str(exc))

        serializer = StudentDetailSerializer(
            student,
            context={"request": request},
        )
        return Response(serializer.data)
