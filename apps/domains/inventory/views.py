# PATH: apps/domains/inventory/views.py
# 저장소 API — R2 업로드 후 DB 메타데이터, 비어있지 않은 폴더 삭제 방지

import logging
from functools import wraps

from django.http import HttpResponse, JsonResponse
from django.views import View
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
from django.db import transaction

from apps.core.authentication import TokenVersionJWTAuthentication as JWTAuthentication

from apps.core.models import Program
from apps.support.inventory.storage_cleanup_dependencies import (
    compensate_unattached_storage_object,
    ensure_storage_inventory_key_attachable,
)
from apps.support.inventory.student_dependencies import (
    active_student_id_for_storage,
    student_storage_namespace_has_legacy_conflict,
)
from apps.support.students.namespace_lock import lock_student_ps_namespaces
from .r2_path import build_r2_key, safe_filename, folder_path_string
from academy.adapters.db.django import repositories_inventory as inv_repo
from .services import (
    inventory_move_lock_token,
    move_file as do_move_file,
    move_folder as do_move_folder,
    delete_folder_recursive as do_delete_folder_recursive,
)
from apps.support.inventory.matchup_dependencies import (
    cleanup_matchup_problem_images,
    document_has_protected_matchup_problems,
    get_matchup_document_for_inventory_file,
    promote_inventory_file_to_matchup,
    promoted_matchup_document_map,
    protected_matchup_document_delete_detail,
)
from apps.support.results.student_reported_scores import (
    create_student_score_submissions,
    inventory_file_has_reported_score,
    score_submission_map_for_inventory_files,
    serialize_reported_score,
    validate_student_score_submissions,
)

# R2 Storage 버킷 (인벤토리 전용)
try:
    from apps.infrastructure.storage.r2 import (
        upload_fileobj_to_r2_storage,
        generate_presigned_get_url_storage,
        copy_object_r2_storage,
        delete_object_r2_storage,
    )
except ImportError:
    upload_fileobj_to_r2_storage = None
    generate_presigned_get_url_storage = None
    copy_object_r2_storage = None
    delete_object_r2_storage = None

STORAGE_QUOTA_BYTES = 200 * 1024**3
SCORE_EVIDENCE_MAX_BYTES = 20 * 1024**2
logger = logging.getLogger(__name__)


def _score_evidence_signature_matches(file_obj, content_type: str) -> bool:
    head = file_obj.read(12)
    file_obj.seek(0)
    if content_type == "application/pdf":
        return head.startswith(b"%PDF-")
    if content_type in {"image/jpeg", "image/jpg"}:
        return head.startswith(b"\xff\xd8\xff")
    if content_type == "image/png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    return False


def _tenant_required(view_func):
    def wrapped(request, *args, **kwargs):
        if not getattr(request, "tenant", None):
            return JsonResponse({"detail": "Tenant required"}, status=400)
        return view_func(request, *args, **kwargs)
    return wrapped


def _jwt_required(view_func):
    """JWT 인증 필수. 미인증 시 401 (저장소 API는 로그인 사용자만 허용)."""
    def wrapped(request, *args, **kwargs):
        auth = JWTAuthentication()
        result = auth.authenticate(request)
        if result is None:
            return JsonResponse(
                {"detail": "Authentication required", "code": "auth_required"},
                status=401,
            )
        request.user, request.auth = result[0], result[1]
        return view_func(request, *args, **kwargs)
    return wrapped


def _inventory_namespace_mutation(view_func):
    """Serialize every scoped metadata mutation with moves and ownership changes."""
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        scope = (
            request.GET.get("scope")
            or request.POST.get("scope")
            or "admin"
        ).lower()
        student_ps = _requested_student_ps(request)
        if scope == "student" and not student_ps:
            return view_func(request, *args, **kwargs)
        with transaction.atomic():
            lock_student_ps_namespaces(
                tenant_id=request.tenant.id,
                ps_numbers=(
                    inventory_move_lock_token(
                        scope=scope,
                        student_ps=student_ps,
                    ),
                ),
            )
            if scope == "student" and active_student_id_for_storage(
                tenant_id=request.tenant.id,
                ps_number=student_ps,
            ) is None:
                return JsonResponse(
                    {
                        "detail": "활성 학생 저장소를 확인할 수 없습니다.",
                        "code": "student_storage_owner_missing",
                    },
                    status=409,
                )
            return view_func(request, *args, **kwargs)
    return wrapped


def _is_tenant_staff(request):
    """요청 사용자가 테넌트의 스태프(owner/admin/staff/teacher/assistant)인지 확인."""
    user = getattr(request, "user", None)
    tenant = getattr(request, "tenant", None)
    if not user or not tenant:
        return False
    from apps.core.models import TenantMembership
    if TenantMembership.objects.filter(
        user=user, tenant=tenant, is_active=True,
        role__in=["owner", "admin", "staff", "teacher", "assistant"],
    ).exists():
        return True
    return bool(
        (getattr(user, "is_superuser", False) or getattr(user, "is_staff", False))
        and getattr(user, "tenant_id", None) == tenant.id
    )


def _requested_student_ps(request) -> str:
    student_ps = (request.GET.get("student_ps") or "").strip()
    if student_ps:
        return student_ps
    student_ps = (request.POST.get("student_ps") or "").strip()
    if student_ps:
        return student_ps
    import json
    try:
        body = json.loads(request.body)
    except Exception:
        body = {}
    return str(body.get("student_ps") or "").strip()


def _student_scope_profile(request):
    user = getattr(request, "user", None)
    tenant = getattr(request, "tenant", None)
    if not user or not tenant:
        return None
    if getattr(user, "parent_profile", None) is not None:
        from apps.domains.student_app.permissions import get_request_student

        return get_request_student(request, require_explicit_parent_child=True)
    student = getattr(user, "student_profile", None)
    if (
        student is not None
        and student.tenant_id == tenant.id
        and student.deleted_at is None
    ):
        return student
    return None


def _check_scope_permission(request, scope=None):
    """Admin is staff-only; student scope is the exact student/selected child."""
    if scope is None:
        import json
        try:
            body = json.loads(request.body)
        except Exception:
            body = {}
        scope = (body.get("scope") or request.GET.get("scope") or "admin").lower()
    if scope == "admin" and not _is_tenant_staff(request):
        return JsonResponse({"detail": "관리자 권한이 필요합니다."}, status=403)
    if scope == "student" and not _is_tenant_staff(request):
        try:
            student_profile = _student_scope_profile(request)
        except Exception:
            return JsonResponse({"detail": "선택한 자녀 정보를 확인할 수 없습니다."}, status=403)
        if not student_profile:
            return JsonResponse({"detail": "학생 정보가 없습니다."}, status=403)
        student_ps = _requested_student_ps(request)
        if student_ps and student_ps != student_profile.ps_number:
            return JsonResponse({"detail": "다른 학생의 자료에 접근할 수 없습니다."}, status=403)
        if student_storage_namespace_has_legacy_conflict(
            tenant_id=request.tenant.id,
            student_id=student_profile.id,
            ps_number=student_profile.ps_number,
        ):
            return JsonResponse(
                {
                    "detail": "이 학생번호의 이전 저장자료를 확인 중입니다. 담당 선생님에게 문의해 주세요.",
                    "code": "student_storage_namespace_conflict",
                },
                status=409,
            )
    return None  # OK


def _folder_matches_scope(folder, scope: str, student_ps: str) -> bool:
    if not folder or folder.scope != scope:
        return False
    if scope == "student":
        return folder.student_ps == student_ps
    return True


def _folder_chain_matches_scope(folder, scope: str, student_ps: str) -> bool:
    current = folder
    while current:
        if not _folder_matches_scope(current, scope, student_ps):
            return False
        current = current.parent
    return True


def _inventory_file_permission_error(request, inv_file):
    if inv_file.scope == "admin":
        if not _is_tenant_staff(request):
            return JsonResponse({"detail": "관리자 권한이 필요합니다."}, status=403)
        return None

    if inv_file.scope != "student":
        return JsonResponse({"detail": "Invalid scope"}, status=400)

    if _is_tenant_staff(request):
        return None

    try:
        student_profile = _student_scope_profile(request)
    except Exception:
        return JsonResponse({"detail": "선택한 자녀 정보를 확인할 수 없습니다."}, status=403)
    if not student_profile:
        return JsonResponse({"detail": "학생 정보가 없습니다."}, status=403)
    if inv_file.student_ps != student_profile.ps_number:
        return JsonResponse({"detail": "다른 학생의 자료에 접근할 수 없습니다."}, status=403)
    if student_storage_namespace_has_legacy_conflict(
        tenant_id=request.tenant.id,
        student_id=student_profile.id,
        ps_number=student_profile.ps_number,
    ):
        return JsonResponse(
            {
                "detail": "이 학생번호의 이전 저장자료를 확인 중입니다. 담당 선생님에게 문의해 주세요.",
                "code": "student_storage_namespace_conflict",
            },
            status=409,
        )
    return None


def _find_inventory_file_by_r2_key(tenant, r2_key: str):
    inv_file = inv_repo.inventory_file_filter(tenant, "admin").filter(r2_key=r2_key).order_by("id").first()
    if inv_file:
        return inv_file
    return inv_repo.inventory_file_filter(tenant, "student").filter(r2_key=r2_key).order_by("id").first()


class _InventoryUploadFailure(Exception):
    def __init__(self, *, status: int, detail: str, code: str = ""):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.code = code


class _InventoryPersistenceFailure(Exception):
    def __init__(self, *, stage: str, cause: Exception):
        super().__init__(str(cause))
        self.stage = stage
        self.__cause__ = cause


def _attach_inventory_upload(
    *,
    tenant,
    scope: str,
    student_ps: str,
    scope_student,
    folder,
    file_obj,
    display_name: str,
    description: str,
    icon: str,
    r2_key: str,
    validated_scores,
):
    with transaction.atomic():
        lock_student_ps_namespaces(
            tenant_id=tenant.id,
            ps_numbers=(
                inventory_move_lock_token(
                    scope=scope,
                    student_ps=student_ps,
                ),
            ),
        )
        if scope == "student":
            owner_id = active_student_id_for_storage(
                tenant_id=tenant.id,
                ps_number=student_ps,
            )
            if owner_id is None or (
                scope_student is not None and owner_id != scope_student.id
            ):
                raise _InventoryUploadFailure(
                    status=409,
                    detail="활성 학생 저장소를 확인할 수 없습니다.",
                    code="student_storage_owner_missing",
                )

        if folder is not None:
            folder = (
                folder.__class__.objects.select_for_update()
                .filter(pk=folder.pk, tenant=tenant)
                .first()
            )
            if folder is None or not _folder_chain_matches_scope(
                folder,
                scope,
                student_ps,
            ):
                raise _InventoryUploadFailure(
                    status=409,
                    detail="저장 폴더가 변경되었습니다. 다시 선택해 주세요.",
                    code="inventory_storage_folder_changed",
                )

        used = inv_repo.inventory_file_aggregate_size(tenant)
        if used + file_obj.size > STORAGE_QUOTA_BYTES:
            raise _InventoryUploadFailure(
                status=403,
                detail="기본 저장공간 200GB를 초과했습니다. 운영팀에 문의해 주세요.",
                code="quota_exceeded",
            )
        try:
            ensure_storage_inventory_key_attachable(
                tenant_id=tenant.id,
                key=r2_key,
            )
        except ValueError as exc:
            raise _InventoryUploadFailure(
                status=409,
                detail="같은 저장 경로가 이미 사용 중입니다. 다시 시도해 주세요.",
                code="inventory_storage_key_conflict",
            ) from exc

        try:
            inv_file = inv_repo.inventory_file_create(
                tenant=tenant,
                scope=scope,
                student_ps=student_ps,
                folder=folder,
                display_name=display_name or file_obj.name,
                description=description,
                icon=icon,
                r2_key=r2_key,
                original_name=file_obj.name,
                size_bytes=file_obj.size,
                content_type=file_obj.content_type or "application/octet-stream",
            )
        except Exception as exc:
            raise _InventoryPersistenceFailure(stage="metadata", cause=exc) from exc

        reported_scores = []
        if validated_scores is not None:
            try:
                reported_scores = create_student_score_submissions(
                    evidence_file=inv_file,
                    validated_rows=validated_scores,
                )
            except Exception as exc:
                raise _InventoryPersistenceFailure(stage="score", cause=exc) from exc
        return inv_file, reported_scores


def _persist_inventory_upload(
    *,
    tenant,
    scope: str,
    student_ps: str,
    scope_student,
    folder,
    file_obj,
    display_name: str,
    description: str,
    icon: str,
    r2_key: str,
    validated_scores,
):
    try:
        upload_fileobj_to_r2_storage(
            fileobj=file_obj,
            key=r2_key,
            content_type=file_obj.content_type or "application/octet-stream",
        )
    except Exception as exc:
        try:
            compensate_unattached_storage_object(
                tenant_id=tenant.id,
                key=r2_key,
                uncertain_write=True,
            )
        except Exception:
            logger.exception(
                "Inventory uncertain upload cleanup intent failed tenant=%s scope=%s",
                tenant.id,
                scope,
            )
            raise _InventoryUploadFailure(
                status=502,
                detail="파일 원본 저장 상태를 확인하지 못했습니다. 관리자에게 문의해 주세요.",
                code="inventory_storage_upload_cleanup_failed",
            ) from exc
        raise _InventoryUploadFailure(
            status=502,
            detail="파일 원본을 저장하지 못했습니다. 다시 시도해 주세요.",
            code="inventory_storage_upload_failed",
        ) from exc

    try:
        return _attach_inventory_upload(
            tenant=tenant,
            scope=scope,
            student_ps=student_ps,
            scope_student=scope_student,
            folder=folder,
            file_obj=file_obj,
            display_name=display_name,
            description=description,
            icon=icon,
            r2_key=r2_key,
            validated_scores=validated_scores,
        )
    except Exception as exc:
        if not isinstance(exc, (_InventoryUploadFailure, _InventoryPersistenceFailure)):
            exc = _InventoryPersistenceFailure(stage="attach", cause=exc)
        stage = exc.stage if isinstance(exc, _InventoryPersistenceFailure) else "attach"
        logger.warning(
            "Inventory %s rejected after R2 upload tenant=%s scope=%s",
            stage,
            tenant.id,
            scope,
        )
        try:
            cleanup_outcome = compensate_unattached_storage_object(
                tenant_id=tenant.id,
                key=r2_key,
            )
        except Exception:
            logger.exception(
                "Inventory orphan cleanup failed tenant=%s scope=%s",
                tenant.id,
                scope,
            )
            cleanup_detail = (
                "성적 정보 저장과 원본 정리에 실패했습니다. 관리자에게 문의해 주세요."
                if stage == "score"
                else "파일 정보 저장과 원본 정리에 실패했습니다. 관리자에게 문의해 주세요."
            )
            cleanup_code = (
                "score_storage_cleanup_failed"
                if stage == "score"
                else "inventory_storage_cleanup_failed"
            )
            raise _InventoryUploadFailure(
                status=502,
                detail=cleanup_detail,
                code=cleanup_code,
            ) from exc

        if cleanup_outcome == "pending":
            cleanup_detail = (
                "성적 정보 저장과 원본 정리를 재시도하고 있습니다. 관리자에게 문의해 주세요."
                if stage == "score"
                else "파일 정보 저장과 원본 정리를 재시도하고 있습니다. 관리자에게 문의해 주세요."
            )
            cleanup_code = (
                "score_storage_cleanup_failed"
                if stage == "score"
                else "inventory_storage_cleanup_failed"
            )
            raise _InventoryUploadFailure(
                status=502,
                detail=cleanup_detail,
                code=cleanup_code,
            ) from exc

        if isinstance(exc, _InventoryUploadFailure):
            raise
        if stage == "score":
            raise _InventoryUploadFailure(
                status=500,
                detail="성적표 제출 정보를 저장하지 못했습니다.",
            ) from exc
        raise _InventoryUploadFailure(
            status=500,
            detail="파일 정보를 저장하지 못했습니다. 다시 시도해 주세요.",
            code="inventory_metadata_save_failed",
        ) from exc


@method_decorator(csrf_exempt, name="dispatch")
class QuotaView(View):
    """GET /storage/quota/ — 테넌트 사용량 및 플랜 한도."""

    @method_decorator(_tenant_required)
    @method_decorator(_jwt_required)
    def get(self, request):
        tenant = request.tenant
        try:
            program = Program.ensure_for_tenant(tenant=tenant)
            used = inv_repo.inventory_file_aggregate_size(tenant)
            return JsonResponse({
                "usedBytes": used,
                "limitBytes": STORAGE_QUOTA_BYTES,
                "plan": program.billing_plan,
            })
        except Exception as e:
            return JsonResponse(
                {"detail": str(e) if settings.DEBUG else "Internal Server Error"},
                status=500,
            )


@method_decorator(csrf_exempt, name="dispatch")
class InventoryListView(View):
    """GET /storage/inventory/?scope=admin|student&student_ps=... — 폴더/파일 목록 (전체, 클라이언트에서 필터)."""

    @method_decorator(_tenant_required)
    @method_decorator(_jwt_required)
    def get(self, request):
        scope = (request.GET.get("scope") or "admin").lower()
        if scope not in ("admin", "student"):
            return JsonResponse({"detail": "Invalid scope"}, status=400)
        perm_err = _check_scope_permission(request, scope)
        if perm_err:
            return perm_err
        student_ps = (request.GET.get("student_ps") or "").strip()
        if scope == "student" and not student_ps:
            return JsonResponse({"detail": "student_ps required for student scope"}, status=400)

        try:
            tenant = request.tenant
            student_ps_arg = student_ps if scope == "student" else None
            qs_folders = inv_repo.inventory_folder_filter(tenant, scope, student_ps_arg)
            qs_files = inv_repo.inventory_file_filter(tenant, scope, student_ps_arg)

            # 매치업 승격된 파일 ID set (admin scope에만 의미 있음)
            promoted_map: dict[int, dict] = {}
            reported_score_map: dict[int, dict] = {}
            file_ids = [f.id for f in qs_files]
            if scope == "admin":
                promoted_map = promoted_matchup_document_map(
                    tenant=tenant,
                    inventory_file_ids=file_ids,
                )
            else:
                reported_score_map = score_submission_map_for_inventory_files(
                    tenant=tenant,
                    file_ids=file_ids,
                )

            folders = [
                {"id": str(f.id), "name": f.name, "parentId": str(f.parent_id) if f.parent_id else None}
                for f in qs_folders
            ]
            files = []
            for f in qs_files:
                row = {
                    "id": str(f.id),
                    "name": f.original_name,
                    "displayName": f.display_name,
                    "description": f.description or "",
                    "icon": f.icon or "file-text",
                    "folderId": str(f.folder_id) if f.folder_id else None,
                    "sizeBytes": f.size_bytes,
                    "r2Key": f.r2_key,
                    "contentType": f.content_type or "",
                    "createdAt": f.created_at.isoformat() if f.created_at else "",
                }
                doc_info = promoted_map.get(f.id)
                if doc_info:
                    row["matchup"] = {
                        "documentId": doc_info["id"],
                        "status": doc_info["status"],
                        "problemCount": doc_info["problem_count"],
                    }
                reported_scores = reported_score_map.get(f.id, [])
                if reported_scores:
                    row["scoreSubmissions"] = reported_scores
                    row["scoreSubmission"] = reported_scores[0]
                files.append(row)
            return JsonResponse({"folders": folders, "files": files})
        except Exception as e:
            return JsonResponse(
                {"detail": str(e) if settings.DEBUG else "Internal Server Error"},
                status=500,
            )


@method_decorator(csrf_exempt, name="dispatch")
class FolderCreateView(View):
    """
    POST /storage/inventory/folders/ — 폴더 생성.

    필수: tenant(요청 host로 해석), name
    선택: scope(기본 admin), student_ps(scope=student일 때 필수), parent_id(숫자 또는 null/빈값=루트)

    404 가능 원인:
    - 미들웨어: 요청 host가 TenantDomain에 없음 (새 테넌트면 해당 host 등록 필요)
    - parent_id에 해당하는 폴더가 해당 테넌트에 없음
    500: create 실패 시 DEBUG면 상세 메시지 반환
    """

    @method_decorator(_tenant_required)
    @method_decorator(_jwt_required)
    def post(self, request):
        import json
        try:
            body = json.loads(request.body)
        except Exception:
            return JsonResponse({"detail": "Invalid JSON"}, status=400)
        scope = (body.get("scope") or "admin").lower()
        student_ps = (body.get("student_ps") or "").strip()
        parent_id = body.get("parent_id")
        name = (body.get("name") or "").strip()
        if scope not in ("admin", "student"):
            return JsonResponse({"detail": "Invalid scope"}, status=400)
        if not name:
            return JsonResponse({"detail": "name required"}, status=400)
        if scope == "student" and not student_ps:
            return JsonResponse({"detail": "student_ps required for student scope"}, status=400)

        perm_err = _check_scope_permission(request, scope)
        if perm_err:
            return perm_err
        tenant = request.tenant
        pid = None
        if parent_id is not None and parent_id != "":
            try:
                pid = int(parent_id)
            except (TypeError, ValueError):
                return JsonResponse({"detail": "parent_id must be a number"}, status=400)

        try:
            with transaction.atomic():
                lock_student_ps_namespaces(
                    tenant_id=tenant.id,
                    ps_numbers=(
                        inventory_move_lock_token(
                            scope=scope,
                            student_ps=student_ps,
                        ),
                    ),
                )
                if scope == "student":
                    if active_student_id_for_storage(
                        tenant_id=tenant.id,
                        ps_number=student_ps,
                    ) is None:
                        return JsonResponse(
                            {
                                "detail": "활성 학생 저장소를 확인할 수 없습니다.",
                                "code": "student_storage_owner_missing",
                            },
                            status=409,
                        )
                if pid is not None:
                    parent = inv_repo.inventory_folder_lock(tenant, pid)
                    if not parent:
                        return JsonResponse({"detail": "Parent folder not found"}, status=404)
                    if not _folder_chain_matches_scope(parent, scope, student_ps):
                        return JsonResponse({"detail": "Parent folder scope mismatch"}, status=403)
                folder = inv_repo.inventory_folder_create(
                    tenant,
                    pid,
                    name,
                    scope,
                    student_ps or "",
                )
        except Exception as e:
            return JsonResponse(
                {"detail": str(e) if settings.DEBUG else "Failed to create folder"},
                status=500,
            )
        return JsonResponse({
            "id": str(folder.id),
            "name": folder.name,
            "parentId": str(folder.parent_id) if folder.parent_id else None,
        })


@method_decorator(csrf_exempt, name="dispatch")
class FileUploadView(View):
    """POST /storage/inventory/upload/ — R2 업로드 후 DB 저장. 플랜 한도 체크."""

    @method_decorator(_tenant_required)
    @method_decorator(_jwt_required)
    def post(self, request):
        scope = (request.POST.get("scope") or "admin").lower()
        student_ps = (request.POST.get("student_ps") or "").strip()
        if scope not in ("admin", "student"):
            return JsonResponse({"detail": "Invalid scope"}, status=400)

        perm_err = _check_scope_permission(request, scope)
        if perm_err:
            return perm_err
        scope_student = (
            _student_scope_profile(request)
            if scope == "student" and not _is_tenant_staff(request)
            else None
        )

        folder_id = request.POST.get("folder_id")
        display_name = (request.POST.get("display_name") or "").strip()
        description = (request.POST.get("description") or "").strip()
        icon = (request.POST.get("icon") or "file-text").strip()
        file_obj = request.FILES.get("file")
        if not file_obj:
            return JsonResponse({"detail": "file required"}, status=400)

        # 파일 크기 제한: 2GB (사용자 요청 — 악용 risk 없는 학원 SaaS)
        MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024
        if file_obj.size > MAX_FILE_SIZE:
            return JsonResponse({"detail": "파일 크기가 2GB를 초과합니다."}, status=400)

        # 파일 타입 화이트리스트
        ALLOWED_CONTENT_TYPES = {
            "application/pdf",
            "application/msword",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.ms-excel",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.ms-powerpoint",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "text/plain",
            "application/zip",
        }
        ALLOWED_TYPE_PREFIXES = ("image/", "video/")
        ct = getattr(file_obj, "content_type", "") or ""
        if ct not in ALLOWED_CONTENT_TYPES and not any(ct.startswith(p) for p in ALLOWED_TYPE_PREFIXES):
            return JsonResponse(
                {"detail": "허용되지 않는 파일 형식입니다."},
                status=400,
            )

        if scope == "student" and not student_ps:
            return JsonResponse({"detail": "student_ps required for student scope"}, status=400)

        score_submission_flag = (request.POST.get("score_submission") or "").strip().lower()
        is_score_submission = score_submission_flag in ("1", "true", "yes")
        validated_scores = None
        if is_score_submission:
            if scope != "student":
                return JsonResponse({"detail": "학생 성적표는 학생 저장소로만 제출할 수 있습니다."}, status=400)
            if ct not in {"application/pdf", "image/png", "image/jpeg", "image/jpg"}:
                return JsonResponse({"detail": "성적표는 PDF, PNG, JPG 파일만 제출할 수 있습니다."}, status=400)
            if file_obj.size > SCORE_EVIDENCE_MAX_BYTES:
                return JsonResponse({"detail": "성적표 파일은 20MB 이하여야 합니다."}, status=400)
            if not _score_evidence_signature_matches(file_obj, ct):
                return JsonResponse({"detail": "파일 내용과 성적표 파일 형식이 일치하지 않습니다."}, status=400)
            try:
                validated_scores = validate_student_score_submissions(
                    tenant=request.tenant,
                    user=request.user,
                    student_ps=student_ps,
                    payload=request.POST,
                    student=scope_student,
                )
            except ValueError as exc:
                return JsonResponse({"detail": str(exc)}, status=400)

        tenant = request.tenant
        if scope == "student":
            preflight_owner_id = active_student_id_for_storage(
                tenant_id=tenant.id,
                ps_number=student_ps,
            )
            if preflight_owner_id is None or (
                scope_student is not None and preflight_owner_id != scope_student.id
            ):
                return JsonResponse(
                    {
                        "detail": "활성 학생 저장소를 확인할 수 없습니다.",
                        "code": "student_storage_owner_missing",
                    },
                    status=409,
                )
        # Quota
        Program.ensure_for_tenant(tenant=tenant)
        used = inv_repo.inventory_file_aggregate_size(tenant)
        if used + file_obj.size > STORAGE_QUOTA_BYTES:
            return JsonResponse(
                {
                    "detail": "기본 저장공간 200GB를 초과했습니다. 운영팀에 문의해 주세요.",
                    "code": "quota_exceeded",
                },
                status=403,
            )

        folder = None
        folder_path = ""
        if folder_id:
            try:
                parsed_folder_id = int(folder_id)
            except (TypeError, ValueError):
                return JsonResponse({"detail": "folder_id must be a number"}, status=400)
            folder = inv_repo.inventory_folder_get(tenant, parsed_folder_id)
            if not folder:
                return JsonResponse({"detail": "Folder not found"}, status=404)
            if not _folder_chain_matches_scope(folder, scope, student_ps):
                return JsonResponse({"detail": "Folder scope mismatch"}, status=403)
            path_parts = []
            p = folder
            while p:
                path_parts.append(p.name)
                p = p.parent
            folder_path = folder_path_string(reversed(path_parts))

        safe_name = safe_filename(file_obj.name)
        r2_key = build_r2_key(
            tenant_id=tenant.id,
            scope=scope,
            student_ps=student_ps,
            folder_path=folder_path,
            file_name=safe_name,
        )

        # 매치업 승격 토글 — 실패 응답이면 R2/DB 상태가 바뀌지 않도록 업로드 전 검증.
        promote_flag = (request.POST.get("promote_to_matchup") or "").strip().lower()
        promote = promote_flag in ("1", "true", "yes")
        if promote:
            if scope != "admin":
                return JsonResponse(
                    {"detail": "선생님 저장소 파일만 매치업으로 승격할 수 있습니다."},
                    status=400,
                )
            matchup_allowed = {"application/pdf", "image/png", "image/jpeg", "image/jpg"}
            if (file_obj.content_type or "") not in matchup_allowed:
                return JsonResponse(
                    {"detail": "매치업은 PDF/PNG/JPG만 지원합니다."},
                    status=400,
                )

        if upload_fileobj_to_r2_storage is None:
            return JsonResponse(
                {"detail": "파일 저장소를 사용할 수 없습니다.", "code": "storage_unavailable"},
                status=503,
            )
        try:
            inv_file, reported_scores = _persist_inventory_upload(
                tenant=tenant,
                scope=scope,
                student_ps=student_ps,
                scope_student=scope_student,
                folder=folder,
                file_obj=file_obj,
                display_name=display_name,
                description=description,
                icon=icon,
                r2_key=r2_key,
                validated_scores=validated_scores,
            )
        except _InventoryUploadFailure as exc:
            payload = {"detail": exc.detail}
            if exc.code:
                payload["code"] = exc.code
            return JsonResponse(payload, status=exc.status)

        matchup_doc_id = None
        matchup_promote_failed = False
        matchup_error = ""
        if promote:
            try:
                doc = promote_inventory_file_to_matchup(
                    inv_file,
                    title=inv_file.display_name,
                    subject=(request.POST.get("subject") or ""),
                    grade_level=(request.POST.get("grade_level") or ""),
                )
                matchup_doc_id = doc.id
                matchup_ai_job_id = doc.ai_job_id or ""
            except Exception as e:
                import logging
                logging.getLogger(__name__).exception(
                    "Promote to matchup failed for inventory_file %s", inv_file.id
                )
                # InventoryFile은 살려두고 부분 실패 명시
                matchup_doc_id = None
                matchup_ai_job_id = ""
                matchup_promote_failed = True
                matchup_error = str(e)[:200]
        else:
            matchup_ai_job_id = ""

        payload = {
            "id": str(inv_file.id),
            "name": inv_file.original_name,
            "displayName": inv_file.display_name,
            "description": inv_file.description,
            "icon": inv_file.icon,
            "folderId": str(inv_file.folder_id) if inv_file.folder_id else None,
            "sizeBytes": inv_file.size_bytes,
            "r2Key": inv_file.r2_key,
            "contentType": inv_file.content_type,
            "createdAt": inv_file.created_at.isoformat() if inv_file.created_at else "",
        }
        if matchup_doc_id is not None:
            payload["matchupDocumentId"] = matchup_doc_id
            # 프론트 Workbox fallback 분기 단순화를 위해 키는 항상 내려준다.
            payload["matchupAiJobId"] = matchup_ai_job_id or None
        if matchup_promote_failed:
            payload["matchupPromoteFailed"] = True
            payload["matchupError"] = matchup_error
        if reported_scores:
            serialized_scores = [serialize_reported_score(row) for row in reported_scores]
            payload["scoreSubmissions"] = serialized_scores
            payload["scoreSubmission"] = serialized_scores[0]
        return JsonResponse(payload)


@method_decorator(csrf_exempt, name="dispatch")
class FolderDeleteView(View):
    """DELETE /storage/inventory/folders/:id/
       — 기본: 비어있을 때만 삭제 (안전)
       — ?recursive=true: 하위 폴더/파일/매치업 cascade + R2 객체 정리.
    PATCH — 폴더 이름 변경."""

    @method_decorator(_tenant_required)
    @method_decorator(_jwt_required)
    @method_decorator(_inventory_namespace_mutation)
    def delete(self, request, folder_id):
        tenant = request.tenant
        scope = (request.GET.get("scope") or "admin").lower()
        student_ps = (request.GET.get("student_ps") or "").strip()
        recursive = (request.GET.get("recursive") or "").lower() in ("true", "1", "yes")

        # 🔐 scope 권한 검증: 학생은 admin scope 접근 불가
        perm_err = _check_scope_permission(request, scope)
        if perm_err:
            return perm_err

        folder = inv_repo.inventory_folder_get(tenant, folder_id)
        if not folder:
            return JsonResponse({"detail": "Not found"}, status=404)
        if folder.scope != scope or (scope == "student" and folder.student_ps != student_ps):
            return JsonResponse({"detail": "Forbidden"}, status=403)

        if recursive:
            # 하위 포함 한방 삭제 — 매치업 problem 이미지/원본 R2 객체/cascade DB 모두 정리
            result = do_delete_folder_recursive(
                tenant=tenant, folder=folder, scope=scope, student_ps=student_ps,
            )
            if result.get("ok") is False:
                status = int(result.pop("status", 400))
                return JsonResponse(result, status=status)
            return JsonResponse(result, status=200)

        if inv_repo.inventory_folder_has_children(tenant, folder):
            return JsonResponse({"detail": "비어있지 않은 폴더는 지울 수 없습니다. 먼저 하위 파일·폴더를 비우거나 삭제하세요.", "code": "folder_not_empty"}, status=400)
        if inv_repo.inventory_folder_has_files(tenant, folder):
            return JsonResponse({"detail": "비어있지 않은 폴더는 지울 수 없습니다. 먼저 하위 파일·폴더를 비우거나 삭제하세요.", "code": "folder_not_empty"}, status=400)

        folder.delete()
        return HttpResponse(status=204)

    @method_decorator(_tenant_required)
    @method_decorator(_jwt_required)
    @method_decorator(_inventory_namespace_mutation)
    def patch(self, request, folder_id):
        import json
        tenant = request.tenant
        scope = (request.GET.get("scope") or "admin").lower()
        student_ps = (request.GET.get("student_ps") or "").strip()

        # 🔐 scope 권한 검증
        perm_err = _check_scope_permission(request, scope)
        if perm_err:
            return perm_err

        folder = inv_repo.inventory_folder_get(tenant, folder_id)
        if not folder:
            return JsonResponse({"detail": "Not found"}, status=404)
        if folder.scope != scope or (scope == "student" and folder.student_ps != student_ps):
            return JsonResponse({"detail": "Forbidden"}, status=403)

        try:
            body = json.loads(request.body)
        except Exception:
            return JsonResponse({"detail": "Invalid JSON"}, status=400)

        name = (body.get("name") or "").strip()
        if not name:
            return JsonResponse({"detail": "이름을 입력해주세요."}, status=400)
        if len(name) > 100:
            return JsonResponse({"detail": "이름은 100자 이하로 입력해주세요."}, status=400)

        folder.name = name
        folder.save(update_fields=["name", "updated_at"])
        return JsonResponse({"id": str(folder.id), "name": folder.name})


@method_decorator(csrf_exempt, name="dispatch")
class FileDeleteView(View):
    """DELETE /storage/inventory/files/:id/ — DB 삭제 후 R2 객체 삭제.
    PATCH — 파일 표시명/설명 변경."""

    @method_decorator(_tenant_required)
    @method_decorator(_jwt_required)
    @method_decorator(_inventory_namespace_mutation)
    def delete(self, request, file_id):
        tenant = request.tenant
        scope = (request.GET.get("scope") or "admin").lower()
        student_ps = (request.GET.get("student_ps") or "").strip()

        # 🔐 scope 권한 검증: 학생은 admin scope 파일 삭제 불가
        perm_err = _check_scope_permission(request, scope)
        if perm_err:
            return perm_err

        inv_file = inv_repo.inventory_file_get(tenant, file_id)
        if not inv_file:
            return JsonResponse({"detail": "Not found"}, status=404)
        if inv_file.scope != scope or (scope == "student" and inv_file.student_ps != student_ps):
            return JsonResponse({"detail": "Forbidden"}, status=403)

        if inventory_file_has_reported_score(tenant=tenant, file_id=inv_file.id):
            return JsonResponse(
                {
                    "detail": "검수 기록과 연결된 성적표 원본은 삭제할 수 없습니다.",
                    "code": "reported_score_evidence_protected",
                },
                status=409,
            )

        r2_key = inv_file.r2_key

        # 🔐 매치업 problem 이미지 R2 cleanup (cascade로 doc/problem 삭제 전)
        # InventoryFile cascade → MatchupDocument → MatchupProblem만 일어남.
        # MatchupProblem.image_key R2 객체는 누가 안 지움 → orphan 방지.
        matchup_doc = get_matchup_document_for_inventory_file(inv_file)
        if matchup_doc is not None:
            try:
                if document_has_protected_matchup_problems(matchup_doc):
                    return JsonResponse(
                        {
                            "detail": protected_matchup_document_delete_detail(),
                            "code": "protected_matchup_document",
                        },
                        status=409,
                    )
            except Exception:
                import logging
                logging.getLogger(__name__).exception(
                    "matchup protected-delete check failed for inv_file %s", inv_file.id,
                )
                return JsonResponse(
                    {
                        "detail": "매치업 보호 상태 확인에 실패했습니다.",
                        "code": "matchup_protection_check_failed",
                    },
                    status=500,
                )
            try:
                cleanup_matchup_problem_images(matchup_doc)
            except Exception:
                import logging
                logging.getLogger(__name__).warning(
                    "matchup problem images cleanup failed for inv_file %s", inv_file.id,
                    exc_info=True,
                )

        # 원본을 먼저 지우고 성공했을 때만 DB 연결을 해제한다. 실패를 삼키고
        # 204를 반환하면 개인정보가 R2에 남은 채 마지막 key 참조가 사라진다.
        if r2_key and delete_object_r2_storage is None:
            return JsonResponse(
                {"detail": "파일 저장소를 사용할 수 없습니다.", "code": "storage_unavailable"},
                status=503,
            )
        if r2_key:
            try:
                delete_object_r2_storage(key=r2_key)
            except Exception:
                import logging
                logging.getLogger(__name__).exception(
                    "Failed to delete R2 object: %s", r2_key, exc_info=True
                )
                return JsonResponse(
                    {
                        "detail": "원본 파일 삭제에 실패했습니다. 잠시 후 다시 시도해 주세요.",
                        "code": "storage_delete_failed",
                    },
                    status=502,
                )

        inv_file.delete()  # CASCADE: MatchupDocument → MatchupProblem 함께 삭제

        return HttpResponse(status=204)

    @method_decorator(_tenant_required)
    @method_decorator(_jwt_required)
    @method_decorator(_inventory_namespace_mutation)
    def patch(self, request, file_id):
        import json
        tenant = request.tenant
        scope = (request.GET.get("scope") or "admin").lower()
        student_ps = (request.GET.get("student_ps") or "").strip()

        # 🔐 scope 권한 검증
        perm_err = _check_scope_permission(request, scope)
        if perm_err:
            return perm_err

        inv_file = inv_repo.inventory_file_get(tenant, file_id)
        if not inv_file:
            return JsonResponse({"detail": "Not found"}, status=404)
        if inv_file.scope != scope or (scope == "student" and inv_file.student_ps != student_ps):
            return JsonResponse({"detail": "Forbidden"}, status=403)

        try:
            body = json.loads(request.body)
        except Exception:
            return JsonResponse({"detail": "Invalid JSON"}, status=400)

        fields = []
        if "displayName" in body:
            name = (body["displayName"] or "").strip()
            if not name:
                return JsonResponse({"detail": "이름을 입력해주세요."}, status=400)
            if len(name) > 200:
                return JsonResponse({"detail": "이름은 200자 이하로 입력해주세요."}, status=400)
            inv_file.display_name = name
            fields.append("display_name")
        if "description" in body:
            inv_file.description = (body["description"] or "").strip()[:500]
            fields.append("description")

        if not fields:
            return JsonResponse({"detail": "변경할 내용이 없습니다."}, status=400)

        fields.append("updated_at")
        inv_file.save(update_fields=fields)
        return JsonResponse({
            "id": str(inv_file.id),
            "displayName": inv_file.display_name,
            "description": inv_file.description,
        })


@method_decorator(csrf_exempt, name="dispatch")
class PresignView(View):
    """POST /storage/inventory/presign/ — R2 presigned GET URL."""

    @method_decorator(_tenant_required)
    @method_decorator(_jwt_required)
    def post(self, request):
        import json
        try:
            body = json.loads(request.body)
        except Exception:
            return JsonResponse({"detail": "Invalid JSON"}, status=400)
        tenant = request.tenant
        file_id = body.get("file_id") or body.get("fileId")
        r2_key = (body.get("r2_key") or "").strip()
        try:
            expires_in = min(int(body.get("expires_in") or 3600), 3600)  # cap at 1 hour
        except (TypeError, ValueError):
            return JsonResponse({"detail": "expires_in must be a number"}, status=400)

        inv_file = None
        if file_id not in (None, ""):
            try:
                parsed_file_id = int(file_id)
            except (TypeError, ValueError):
                return JsonResponse({"detail": "file_id must be a number"}, status=400)
            inv_file = inv_repo.inventory_file_get(tenant, parsed_file_id)
            if not inv_file:
                return JsonResponse({"detail": "File not found"}, status=404)
            if r2_key and inv_file.r2_key != r2_key:
                return JsonResponse({"detail": "file_id and r2_key mismatch"}, status=400)
        elif r2_key:
            inv_file = _find_inventory_file_by_r2_key(tenant, r2_key)
            if not inv_file:
                return JsonResponse({"detail": "File not found"}, status=404)
        else:
            return JsonResponse({"detail": "file_id required"}, status=400)

        perm_err = _inventory_file_permission_error(request, inv_file)
        if perm_err:
            return perm_err
        expected_prefix = f"tenants/{tenant.id}/"
        if not inv_file.r2_key.startswith(expected_prefix):
            return JsonResponse(
                {"detail": "Access denied: file key does not belong to this tenant"},
                status=403,
            )

        if not generate_presigned_get_url_storage:
            return JsonResponse({"url": ""}, status=200)
        url = generate_presigned_get_url_storage(key=inv_file.r2_key, expires_in=expires_in)
        return JsonResponse({"url": url})


@method_decorator(csrf_exempt, name="dispatch")
class MoveView(View):
    """
    POST /storage/inventory/move/
    Body: { type: "file"|"folder", source_id: str, target_folder_id: str|null, on_duplicate?: "overwrite"|"rename" }
    Copy & Delete 방식: R2 복사 성공 → DB 업데이트 → R2 원본 삭제. 실패 시 원본 삭제 안 함.
    """

    @method_decorator(_tenant_required)
    @method_decorator(_jwt_required)
    def post(self, request):
        import json
        try:
            body = json.loads(request.body)
        except Exception:
            return JsonResponse({"detail": "Invalid JSON"}, status=400)
        move_type = (body.get("type") or "file").lower()
        source_id = body.get("source_id")
        target_folder_id = body.get("target_folder_id")
        on_duplicate_raw = body.get("on_duplicate")
        on_duplicate = (on_duplicate_raw.strip().lower() if on_duplicate_raw and str(on_duplicate_raw).strip() else None)
        if move_type not in ("file", "folder") or not source_id:
            return JsonResponse({"detail": "type and source_id required"}, status=400)
        scope = (body.get("scope") or request.GET.get("scope") or "admin").lower()
        student_ps = (body.get("student_ps") or request.GET.get("student_ps") or "").strip()
        if scope == "student" and not student_ps:
            return JsonResponse({"detail": "student_ps required for student scope"}, status=400)

        # 🔐 scope 권한 검증: 학생은 admin scope 이동 불가
        perm_err = _check_scope_permission(request, scope)
        if perm_err:
            return perm_err

        tenant = request.tenant
        try:
            sid = int(source_id)
        except (TypeError, ValueError):
            return JsonResponse({"detail": "Invalid source_id"}, status=400)
        tid = None
        if target_folder_id is not None and target_folder_id != "":
            try:
                tid = int(target_folder_id)
            except (TypeError, ValueError):
                return JsonResponse({"detail": "Invalid target_folder_id"}, status=400)

        if move_type == "file":
            result = do_move_file(
                tenant=tenant,
                scope=scope,
                student_ps=student_ps,
                source_file_id=sid,
                target_folder_id=tid,
                on_duplicate=on_duplicate,
            )
        else:
            result = do_move_folder(
                tenant=tenant,
                scope=scope,
                student_ps=student_ps,
                source_folder_id=sid,
                target_folder_id=tid,
                on_duplicate=on_duplicate,
            )

        if not result.get("ok"):
            status = result.get("status", 400)
            payload = {"detail": result.get("detail", "Move failed")}
            if result.get("code"):
                payload["code"] = result["code"]
            if result.get("existing_name") is not None:
                payload["existing_name"] = result["existing_name"]
            return JsonResponse(payload, status=status)
        return JsonResponse({"ok": True})
