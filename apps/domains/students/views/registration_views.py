# PATH: apps/domains/students/views/registration_views.py

import logging
import traceback

from django.db import transaction, IntegrityError
from django.db.models import Q
from django.conf import settings

from rest_framework.viewsets import ModelViewSet
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema

from apps.core.parsing import parse_bool
from apps.core.permissions import TenantResolvedAndStaff, TenantResolved
from apps.api.common.throttles import AlimtalkEndpointThrottle, SignupCheckThrottle

from academy.adapters.db.django import repositories_students as student_repo
from ..models import Student, StudentRegistrationRequest
from ..serializers import (
    DeletedRegistrationConflictSerializer,
    DeletedRegistrationResolveSerializer,
    RegistrationRequestBulkApproveResponseSerializer,
    RegistrationRequestBulkIdsSerializer,
    RegistrationRequestBulkRejectResponseSerializer,
    RegistrationRequestCreateSerializer,
    RegistrationRequestDuplicateCheckRequestSerializer,
    RegistrationRequestDuplicateCheckResponseSerializer,
    RegistrationRequestListSerializer,
    RegistrationRequestRejectResponseSerializer,
    RegistrationRequestSettingsSerializer,
    SelfRegistrationDisabledErrorSerializer,
    StudentDetailSerializer,
)
from ..services import (
    RegistrationApprovalError,
    RegistrationApprovalResult,
    approve_registration_request,
    resolve_deleted_registration_request,
)
from ..services.registration_policy import is_student_self_registration_enabled
from .student_views import StudentListPagination

logger = logging.getLogger(__name__)


def _self_registration_disabled_response(request):
    tenant = getattr(request, "tenant", None)
    if tenant is not None and not is_student_self_registration_enabled(tenant):
        return Response(
            {
                "code": "self_registration_disabled",
                "detail": "이 학원은 운영정책상 학생 회원가입을 사용하지 않습니다.",
            },
            status=403,
        )
    return None


# ======================================================
# 학생 가입 신청 (로그인 전 회원가입 → 선생 승인)
# ======================================================


def _copy_approval_result_to_instance(reg, result: RegistrationApprovalResult) -> None:
    reg.status = result.registration.status
    reg.student = result.student
    reg.student_id = result.student.id
    reg.initial_password_plain = result.registration.initial_password_plain
    reg._approval_result = result


def _approve_registration_request_with_result(request, reg):
    try:
        result = approve_registration_request(
            tenant=request.tenant,
            registration_id=reg.pk,
        )
        _copy_approval_result_to_instance(reg, result)
        return None, result
    except RegistrationApprovalError as e:
        payload = {"detail": e.detail}
        if getattr(getattr(request, "user", None), "is_authenticated", False):
            if e.code:
                payload["code"] = e.code
            payload.update(e.context)
        return Response(payload, status=e.status_code), None
    except Exception as e:
        logger.exception("_approve_registration_request error: %s", e)
        return Response(
            {"detail": str(e) if settings.DEBUG else "승인 처리 중 오류가 발생했습니다."},
            status=500,
        ), None


def _approve_registration_request(request, reg):
    """
    가입 신청 1건 승인 처리. 성공 시 None 반환, 실패 시 Response 반환.
    호출 후 reg.student 가 설정됨.
    """
    error, _result = _approve_registration_request_with_result(request, reg)
    return error


class RegistrationRequestViewSet(ModelViewSet):
    """
    - create: AllowAny + TenantResolved (학생이 로그인 페이지에서 신청)
    - list / retrieve / approve: TenantResolvedAndStaff (선생이 가입신청 목록/승인)
    """
    permission_classes = [IsAuthenticated, TenantResolvedAndStaff]
    serializer_class = RegistrationRequestListSerializer
    pagination_class = StudentListPagination

    def get_queryset(self):
        return StudentRegistrationRequest.objects.filter(
            tenant=self.request.tenant
        ).select_related("tenant", "student").order_by("-created_at")

    def filter_queryset(self, queryset):
        action = getattr(self, "action", None)
        if action == "list":
            status = self.request.query_params.get("status")
            if status in (StudentRegistrationRequest.PENDING, StudentRegistrationRequest.APPROVED, StudentRegistrationRequest.REJECTED):
                queryset = queryset.filter(status=status)
        return queryset

    def get_authenticators(self):
        # create/check_duplicate는 비로그인 요청 허용 → JWT 검사 생략 (만료 토큰 시 401 방지)
        if getattr(self, "action", None) in ("create", "check_duplicate"):
            return []
        return super().get_authenticators()

    def get_throttles(self):
        if getattr(self, "action", None) == "check_duplicate":
            return [SignupCheckThrottle()]
        if getattr(self, "action", None) == "create":
            return [AlimtalkEndpointThrottle()]
        return super().get_throttles()

    def get_permissions(self):
        if getattr(self, "action", None) in ("create", "check_duplicate"):
            return [AllowAny(), TenantResolved()]
        return [IsAuthenticated(), TenantResolvedAndStaff()]

    def get_serializer_class(self):
        if getattr(self, "action", None) == "create":
            return RegistrationRequestCreateSerializer
        return RegistrationRequestListSerializer

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name="status",
                type=str,
                location=OpenApiParameter.QUERY,
                enum=[
                    StudentRegistrationRequest.APPROVED,
                    StudentRegistrationRequest.PENDING,
                    StudentRegistrationRequest.REJECTED,
                ],
                description=(
                    "가입 신청 상태 필터. 자가 가입 비활성 학원에서 "
                    "`status=pending` 조회는 감사 이력을 업무로 노출하지 않고 403을 반환합니다."
                ),
            ),
        ],
        responses={
            200: RegistrationRequestListSerializer(many=True),
            403: OpenApiResponse(
                response=SelfRegistrationDisabledErrorSerializer,
                description=(
                    "자가 가입 비활성 학원에서 `status=pending`으로 조회한 경우에만 반환합니다."
                ),
            ),
        },
    )
    def list(self, request, *args, **kwargs):
        if request.query_params.get("status") == StudentRegistrationRequest.PENDING:
            disabled = _self_registration_disabled_response(request)
            if disabled is not None:
                return disabled
        return super().list(request, *args, **kwargs)

    def _generic_mutation_response(self, request):
        disabled = _self_registration_disabled_response(request)
        if disabled is not None:
            return disabled
        return Response(
            {
                "code": "registration_request_read_only",
                "detail": "가입 신청 이력은 전용 승인·거절 작업으로만 처리할 수 있습니다.",
            },
            status=405,
        )

    @extend_schema(exclude=True)
    def update(self, request, *args, **kwargs):
        return self._generic_mutation_response(request)

    @extend_schema(exclude=True)
    def partial_update(self, request, *args, **kwargs):
        return self._generic_mutation_response(request)

    @extend_schema(exclude=True)
    def destroy(self, request, *args, **kwargs):
        return self._generic_mutation_response(request)

    @extend_schema(
        request=RegistrationRequestDuplicateCheckRequestSerializer,
        responses={
            200: RegistrationRequestDuplicateCheckResponseSerializer,
            403: SelfRegistrationDisabledErrorSerializer,
        },
    )
    @action(detail=False, methods=["post"], url_path="check_duplicate")
    def check_duplicate(self, request):
        """
        회원가입 실시간 중복검사.
        POST body: { "username": "abc", "phone": "01012345678" }
        둘 다 선택적 — 입력된 필드만 검사.
        """
        tenant = getattr(request, "tenant", None)
        if not tenant:
            return Response({"detail": "테넌트를 확인할 수 없습니다."}, status=403)
        disabled = _self_registration_disabled_response(request)
        if disabled is not None:
            return disabled

        username = (request.data.get("username") or "").strip()
        phone = (request.data.get("phone") or "").strip().replace("-", "")

        result = {}

        if username:
            # 1) 이미 등록된 학생 (활성). LIKE 와일드카드 회피 위해 정확 매칭.
            from apps.core.models.user import user_internal_username
            internal = user_internal_username(tenant, username)
            exists = Student.objects.filter(
                tenant=tenant,
                deleted_at__isnull=True,
                user__username=internal,
            ).exists()
            if exists:
                result["username"] = {"available": False, "reason": "이미 사용 중인 아이디입니다."}
            else:
                # 2) 승인 대기 중인 신청
                pending = StudentRegistrationRequest.objects.filter(
                    tenant=tenant,
                    status=StudentRegistrationRequest.PENDING,
                    username=username,
                ).exists()
                if pending:
                    result["username"] = {"available": False, "reason": "승인 대기 중인 아이디입니다. 선생님의 승인을 기다려 주세요."}
                else:
                    result["username"] = {"available": True}

        if phone and len(phone) == 11:
            # 1) 이미 등록된 학생 (활성)
            existing = Student.objects.filter(
                tenant=tenant,
                deleted_at__isnull=True,
            ).filter(
                Q(phone=phone) | Q(parent_phone=phone)
            ).first()
            # 2) 승인 대기 중인 신청
            pending_exists = StudentRegistrationRequest.objects.filter(
                tenant=tenant,
                status=StudentRegistrationRequest.PENDING,
            ).filter(
                Q(phone=phone) | Q(parent_phone=phone)
            ).exists()
            # phone enumeration 완화: 둘 중 하나라도 있으면 동일 generic 메시지로 응답.
            # 이전엔 "이미 등록" / "승인 대기" 로 분기되어 외부에서 phone book scrape 가능했음.
            if existing or pending_exists:
                result["phone"] = {
                    "available": False,
                    "reason": "해당 전화번호는 사용할 수 없습니다. 가입된 계정이 있다면 기존 계정으로 로그인해 주세요.",
                }
            else:
                result["phone"] = {"available": True}

        return Response(result)

    @extend_schema(
        request=RegistrationRequestCreateSerializer,
        responses={
            200: StudentDetailSerializer,
            201: RegistrationRequestListSerializer,
            403: SelfRegistrationDisabledErrorSerializer,
        },
    )
    def create(self, request, *args, **kwargs):
        if not getattr(request, "tenant", None):
            return Response(
                {"detail": "테넌트를 확인할 수 없습니다. 로그인 URL(테넌트 코드 포함)로 접속했는지 확인해 주세요."},
                status=403,
            )
        disabled = _self_registration_disabled_response(request)
        if disabled is not None:
            return disabled
        serializer = self.get_serializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        # 중복 가입 체크: 전화번호 또는 username이 이미 활성 학생으로 존재하는 경우
        data_check = serializer.validated_data
        phone_check = (data_check.get("phone") or "").strip()
        username_check = (data_check.get("username") or "").strip()
        tenant = request.tenant

        existing_student = None
        if phone_check:
            existing_student = Student.objects.filter(
                tenant=tenant,
                deleted_at__isnull=True,
            ).filter(
                Q(phone=phone_check) | Q(parent_phone=phone_check)
            ).select_related("user").first()
        if not existing_student and username_check:
            from apps.core.models.user import user_internal_username
            internal_check = user_internal_username(tenant, username_check)
            existing_student = Student.objects.filter(
                tenant=tenant,
                deleted_at__isnull=True,
                user__username=internal_check,
            ).select_related("user").first()

        if existing_student:
            return Response(
                {
                    "code": "already_registered",
                    "detail": "이미 가입된 아이디입니다.",
                    "student_name": existing_student.name,
                    "student_phone": existing_student.phone or existing_student.parent_phone or "",
                },
                status=409,
            )

        from django.contrib.auth.hashers import make_password
        data = serializer.validated_data.copy()
        raw_password = data.pop("initial_password")
        password = make_password(raw_password)
        # grade: model은 PositiveSmallIntegerField(null=True) → int 또는 None만 허용
        raw_grade = data.get("grade")
        if raw_grade is not None and raw_grade != "":
            try:
                grade = int(raw_grade)
                if grade < 0 or grade > 32767:
                    grade = None
            except (TypeError, ValueError):
                grade = None
        else:
            grade = None

        try:
            req = StudentRegistrationRequest.objects.create(
                tenant=request.tenant,
                status=StudentRegistrationRequest.PENDING,
                initial_password=password,
                initial_password_plain="",
                name=data.get("name", ""),
                username=(data.get("username") or "").strip() or "",
                parent_phone=data.get("parent_phone", ""),
                phone=data.get("phone"),
                school_type=data.get("school_type", "HIGH"),
                elementary_school=(data.get("elementary_school") or "") or None,
                high_school=(data.get("high_school") or "") or None,
                middle_school=(data.get("middle_school") or "") or None,
                high_school_class=(data.get("high_school_class") or "") or None,
                major=(data.get("major") or "") or None,
                grade=grade,
                gender=(data.get("gender") or "").strip() or None,
                memo=(data.get("memo") or "") or None,
                address=(data.get("address") or "").strip() or None,
                origin_middle_school=(data.get("origin_middle_school") or "").strip() or None,
            )
        except IntegrityError as e:
            logger.warning("RegistrationRequest create IntegrityError: %s", e)
            payload = {"detail": "저장 중 제약 조건 오류가 발생했습니다. 입력값(이름·전화번호 등)을 확인해 주세요."}
            if settings.DEBUG:
                payload["error"] = str(e)
            return Response(payload, status=400)
        except Exception as e:
            logger.exception("RegistrationRequest create error: %s", e)
            payload = {"detail": "가입 신청 저장 중 오류가 발생했습니다."}
            if settings.DEBUG:
                payload["error"] = str(e)
                payload["traceback"] = traceback.format_exc()
            return Response(payload, status=500)

        # 자동 승인 설정 시 즉시 승인 처리
        if getattr(request.tenant, "student_registration_auto_approve", False):
            err, result = _approve_registration_request_with_result(request, req)
            if err is not None:
                return err
            return Response(
                StudentDetailSerializer(result.student, context={"request": request}).data,
                status=200,
            )

        try:
            out = RegistrationRequestListSerializer(req, context={"request": request})
            return Response(out.data, status=201)
        except Exception as e:
            logger.exception("RegistrationRequest response serialize error: %s", e)
            payload = {"detail": "가입 신청이 저장되었으나 응답 생성 중 오류가 발생했습니다."}
            if settings.DEBUG:
                payload["error"] = str(e)
            return Response(payload, status=500)

    @extend_schema(
        request=RegistrationRequestBulkIdsSerializer,
        responses={
            200: RegistrationRequestBulkApproveResponseSerializer,
            403: SelfRegistrationDisabledErrorSerializer,
        },
    )
    @action(detail=False, methods=["post"], url_path="bulk_approve")
    def bulk_approve(self, request):
        """
        선택한 가입 신청 일괄 승인.
        POST body: { "ids": [1, 2, 3, ...] }
        응답: { "approved": int, "failed": [ {"id": int, "detail": str}, ... ] }
        """
        disabled = _self_registration_disabled_response(request)
        if disabled is not None:
            return disabled

        ids = request.data.get("ids") or []
        if not isinstance(ids, (list, tuple)):
            return Response({"detail": "ids는 배열이어야 합니다."}, status=400)
        ids = [int(x) for x in ids if isinstance(x, (int, str)) and str(x).isdigit()]
        if not ids:
            return Response({"detail": "승인할 ID가 없습니다."}, status=400)

        tenant = request.tenant
        approved_count = 0
        failed = []

        for rid in ids:
            reg = StudentRegistrationRequest.objects.filter(
                tenant=tenant,
                id=rid,
            ).first()
            if not reg:
                failed.append({"id": rid, "detail": "신청을 찾을 수 없습니다."})
                continue
            if reg.status != StudentRegistrationRequest.PENDING:
                failed.append({"id": rid, "detail": "이미 처리된 신청입니다."})
                continue
            err_response, _result = _approve_registration_request_with_result(request, reg)
            if err_response is not None:
                failed.append({"id": rid, "detail": err_response.data.get("detail", "승인 실패")})
            else:
                approved_count += 1

        return Response({"approved": approved_count, "failed": failed}, status=200)

    @extend_schema(
        methods=["GET"],
        request=None,
        responses={200: RegistrationRequestSettingsSerializer},
    )
    @extend_schema(
        methods=["PATCH"],
        request=RegistrationRequestSettingsSerializer,
        responses={200: RegistrationRequestSettingsSerializer},
    )
    @action(detail=False, methods=["get", "patch"], url_path="settings")
    def registration_settings(self, request):
        """
        가입 신청 설정 조회/수정 (자동 승인).
        GET → { "auto_approve": bool }
        PATCH body: { "auto_approve": bool } → 200 동일 형식
        """
        tenant = request.tenant
        if request.method == "GET":
            try:
                auto_approve = getattr(tenant, "student_registration_auto_approve", False)
            except Exception:
                auto_approve = False
            return Response({
                "auto_approve": bool(auto_approve),
            })
        if request.method == "PATCH":
            auto_approve = request.data.get("auto_approve")
            if auto_approve is not None:
                # parse_bool: "false"/"0" 등 문자열을 안전하게 boolean으로 변환.
                # bool("false") == True 이슈 방지.
                auto_approve = parse_bool(
                    auto_approve, field_name="auto_approve",
                )
                previous_auto_approve = tenant.student_registration_auto_approve
                try:
                    with transaction.atomic():
                        tenant.student_registration_auto_approve = auto_approve
                        tenant.save(update_fields=["student_registration_auto_approve"])
                except Exception:
                    tenant.student_registration_auto_approve = previous_auto_approve
                    logger.exception("registration_settings_save_failed tenant_id=%s", tenant.pk)
                    return Response({
                        "code": "registration_settings_save_failed",
                        "detail": "가입 신청 설정을 저장하지 못했습니다. 잠시 후 다시 시도해 주세요.",
                    }, status=503)
            try:
                auto_approve = getattr(tenant, "student_registration_auto_approve", False)
            except Exception:
                auto_approve = False
            return Response({
                "auto_approve": bool(auto_approve),
            })
        return Response({"detail": "Method not allowed."}, status=405)

    @extend_schema(
        request=None,
        parameters=[OpenApiParameter("id", int, OpenApiParameter.PATH)],
        responses={
            200: StudentDetailSerializer,
            403: SelfRegistrationDisabledErrorSerializer,
            409: DeletedRegistrationConflictSerializer,
        },
    )
    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        """승인 시 Student + User + TenantMembership 생성 후 status=approved."""
        disabled = _self_registration_disabled_response(request)
        if disabled is not None:
            return disabled
        reg = self.get_object()
        if reg.status != StudentRegistrationRequest.PENDING:
            return Response(
                {"detail": "이미 처리된 신청입니다."},
                status=400,
            )
        err, result = _approve_registration_request_with_result(request, reg)
        if err is not None:
            return err
        out = StudentDetailSerializer(result.student, context={"request": request})
        return Response(out.data, status=200)

    @extend_schema(
        request=DeletedRegistrationResolveSerializer,
        parameters=[OpenApiParameter("id", int, OpenApiParameter.PATH)],
        responses={
            200: StudentDetailSerializer,
            403: SelfRegistrationDisabledErrorSerializer,
        },
    )
    @action(detail=True, methods=["post"], url_path="resolve_deleted")
    def resolve_deleted(self, request, pk=None):
        """선생님이 선택한 동일인 삭제 계정을 복구한 뒤 가입 승인."""
        disabled = _self_registration_disabled_response(request)
        if disabled is not None:
            return disabled
        serializer = DeletedRegistrationResolveSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({"detail": "복구할 과거 계정을 선택해 주세요."}, status=400)
        try:
            result = resolve_deleted_registration_request(
                tenant=request.tenant,
                registration_id=int(pk),
                student_id=serializer.validated_data["student_id"],
            )
        except StudentRegistrationRequest.DoesNotExist:
            return Response({"detail": "가입 신청을 찾을 수 없습니다."}, status=404)
        except RegistrationApprovalError as exc:
            return Response({"detail": exc.detail}, status=exc.status_code)
        out = StudentDetailSerializer(result.student, context={"request": request})
        return Response(out.data, status=200)

    @extend_schema(
        request=None,
        responses={
            200: RegistrationRequestRejectResponseSerializer,
            403: SelfRegistrationDisabledErrorSerializer,
        },
    )
    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        """가입 신청 거절 → status=rejected."""
        disabled = _self_registration_disabled_response(request)
        if disabled is not None:
            return disabled
        reg = self.get_object()
        if reg.status != StudentRegistrationRequest.PENDING:
            return Response(
                {"detail": "이미 처리된 신청입니다."},
                status=400,
            )
        with transaction.atomic():
            # Row lock to prevent approve/reject race condition
            reg = StudentRegistrationRequest.objects.select_for_update().get(pk=reg.pk)
            if reg.status != StudentRegistrationRequest.PENDING:
                return Response(
                    {"detail": "이미 처리된 신청입니다."},
                    status=400,
                )
            reg.status = StudentRegistrationRequest.REJECTED
            reg.save(update_fields=["status", "updated_at"])
        return Response({"status": "rejected", "id": reg.id}, status=200)

    @extend_schema(
        request=RegistrationRequestBulkIdsSerializer,
        responses={
            200: RegistrationRequestBulkRejectResponseSerializer,
            403: SelfRegistrationDisabledErrorSerializer,
        },
    )
    @action(detail=False, methods=["post"], url_path="bulk_reject")
    def bulk_reject(self, request):
        """
        선택한 가입 신청 일괄 거절.
        POST body: { "ids": [1, 2, 3, ...] }
        """
        disabled = _self_registration_disabled_response(request)
        if disabled is not None:
            return disabled

        ids = request.data.get("ids") or []
        if not isinstance(ids, (list, tuple)):
            return Response({"detail": "ids는 배열이어야 합니다."}, status=400)
        ids = [int(x) for x in ids if isinstance(x, (int, str)) and str(x).isdigit()]
        if not ids:
            return Response({"detail": "거절할 ID가 없습니다."}, status=400)

        tenant = request.tenant
        with transaction.atomic():
            # Row lock to prevent approve/reject race condition
            updated = StudentRegistrationRequest.objects.select_for_update().filter(
                tenant=tenant,
                id__in=ids,
                status=StudentRegistrationRequest.PENDING,
            ).update(status=StudentRegistrationRequest.REJECTED)
        return Response({"rejected": updated}, status=200)
