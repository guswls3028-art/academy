# apps/support/messaging/views_notification.py
"""
수동 알림 발송 API — preview → confirm 2단계.

자동 트리거가 아닌 선생의 명시적 발송만 지원.
preview 없이 confirm 직접 호출 불가.
"""

import json
import logging

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status as http_status
from rest_framework.permissions import IsAuthenticated
from apps.core.permissions import TenantResolvedAndStaff

from apps.domains.messaging.notification_dispatch import (
    build_attendance_preview,
    build_student_list_preview,
    create_preview_token,
    consume_preview_token_and_execute,
)
from apps.domains.messaging.scheduled import MessagingHourlyQuotaExceeded
from apps.domains.messaging.permissions import can_send_messages

logger = logging.getLogger(__name__)

MAX_MANUAL_NOTIFICATION_RECIPIENTS = 200
MAX_MANUAL_NOTIFICATION_REQUEST_BYTES = 200_000
MAX_MANUAL_CONTEXT_KEYS = 50
MAX_MANUAL_CONTEXT_KEY_LENGTH = 50
MAX_MANUAL_CONTEXT_VALUE_LENGTH = 1_000
MAX_MANUAL_CONTEXT_TOTAL_CHARS = 5_000
_SENSITIVE_RECIPIENT_KEYS = {"phone_raw", "alimtalk_replacements"}


def _safe_recipients(recipients):
    return [
        {k: v for k, v in recipient.items() if k not in _SENSITIVE_RECIPIENT_KEYS}
        for recipient in recipients
    ]


def _parse_positive_int(value):
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _normalize_student_ids(raw_ids):
    if not raw_ids or not isinstance(raw_ids, list):
        return None, "student_ids 목록이 필요합니다."
    if len(raw_ids) > MAX_MANUAL_NOTIFICATION_RECIPIENTS:
        return None, f"한 번에 최대 {MAX_MANUAL_NOTIFICATION_RECIPIENTS}명까지 미리보기할 수 있습니다."

    normalized = []
    for raw_id in raw_ids:
        parsed_id = _parse_positive_int(raw_id)
        if parsed_id is None:
            return None, "student_ids는 양의 정수 목록이어야 합니다."
        normalized.append(parsed_id)
    return list(dict.fromkeys(normalized)), None


def _request_exceeds_size_limit(data) -> bool:
    try:
        encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        return True
    return len(encoded) > MAX_MANUAL_NOTIFICATION_REQUEST_BYTES


def _normalize_context(raw_context, field_name):
    if not isinstance(raw_context, dict):
        return None, f"{field_name}는 객체여야 합니다."
    if len(raw_context) > MAX_MANUAL_CONTEXT_KEYS:
        return None, f"{field_name}는 최대 {MAX_MANUAL_CONTEXT_KEYS}개 변수만 허용합니다."

    normalized = {}
    total_chars = 0
    for raw_key, raw_value in raw_context.items():
        if not isinstance(raw_key, str) or not raw_key or len(raw_key) > MAX_MANUAL_CONTEXT_KEY_LENGTH:
            return None, f"{field_name} 변수명은 1~{MAX_MANUAL_CONTEXT_KEY_LENGTH}자 문자열이어야 합니다."
        if isinstance(raw_value, (dict, list, tuple, set)) or raw_value is None:
            return None, f"{field_name} 변수 값은 문자열 또는 숫자여야 합니다."
        value = str(raw_value)
        if len(value) > MAX_MANUAL_CONTEXT_VALUE_LENGTH:
            return None, f"{field_name} 변수 값은 최대 {MAX_MANUAL_CONTEXT_VALUE_LENGTH}자까지 허용합니다."
        total_chars += len(raw_key) + len(value)
        if total_chars > MAX_MANUAL_CONTEXT_TOTAL_CHARS:
            return None, f"{field_name} 전체 변수 길이는 최대 {MAX_MANUAL_CONTEXT_TOTAL_CHARS}자까지 허용합니다."
        normalized[raw_key] = value
    return normalized, None


def _normalize_context_per_student(raw_context):
    if not isinstance(raw_context, dict):
        return None, "context_per_student는 객체여야 합니다."
    if len(raw_context) > MAX_MANUAL_NOTIFICATION_RECIPIENTS:
        return None, (
            f"context_per_student는 최대 {MAX_MANUAL_NOTIFICATION_RECIPIENTS}명의 값만 허용합니다."
        )

    normalized = {}
    for raw_student_id, student_context in raw_context.items():
        student_id = _parse_positive_int(raw_student_id)
        if student_id is None:
            return None, "context_per_student의 학생 ID는 양의 정수여야 합니다."
        parsed_context, error = _normalize_context(
            student_context,
            f"context_per_student[{student_id}]",
        )
        if error:
            return None, error
        normalized[student_id] = parsed_context
    return normalized, None


def _format_context_keys(keys):
    return ", ".join(sorted(str(key) for key in keys))


def _context_source_override_detail(context_conflicts, per_student_conflicts):
    parts = []
    if context_conflicts:
        parts.append(f"context: {_format_context_keys(context_conflicts)}")
    if per_student_conflicts:
        parts.append(f"context_per_student: {_format_context_keys(per_student_conflicts)}")
    return "context_source가 생성한 변수는 요청 값으로 덮어쓸 수 없습니다. " + "; ".join(parts)


def _messaging_access_error(request, tenant):
    if not can_send_messages(request, tenant):
        return Response(
            {"detail": "알림톡 발송 권한이 없습니다. 관리자 또는 강사 권한이 필요합니다."},
            status=http_status.HTTP_403_FORBIDDEN,
        )
    from apps.domains.messaging.policy import (
        get_messaging_disabled_reason,
        is_messaging_disabled,
    )

    if is_messaging_disabled(tenant.id):
        return Response(
            {
                "detail": get_messaging_disabled_reason(tenant.id),
                "code": "messaging_disabled",
            },
            status=http_status.HTTP_403_FORBIDDEN,
        )
    return None


class AttendanceNotificationPreviewView(APIView):
    """
    출결 알림 미리보기.

    POST /api/v1/messaging/attendance-notification/preview/
    {
        "session_id": int,
        "notification_type": "check_in" | "absent",
        "send_to": "parent" | "student"  (기본: "parent")
    }

    Returns:
    {
        "preview_token": "uuid",
        "recipients": [...],
        "total_count": int,
        "excluded_count": int,
        "message_preview": str,
        "session_title": str,
        "lecture_title": str,
    }
    """
    permission_classes = [IsAuthenticated, TenantResolvedAndStaff]

    def post(self, request):
        tenant = getattr(request, "tenant", None)
        if not tenant:
            return Response({"detail": "테넌트 정보가 없습니다."}, status=http_status.HTTP_400_BAD_REQUEST)
        access_error = _messaging_access_error(request, tenant)
        if access_error:
            return access_error

        if _request_exceeds_size_limit(request.data):
            return Response(
                {"detail": "미리보기 요청은 최대 200KB까지 허용합니다."},
                status=http_status.HTTP_400_BAD_REQUEST,
            )

        session_id = request.data.get("session_id")
        notification_type = request.data.get("notification_type")  # "check_in" | "absent"
        send_to = request.data.get("send_to", "parent")

        if not session_id or not notification_type:
            return Response(
                {"detail": "session_id와 notification_type은 필수입니다."},
                status=http_status.HTTP_400_BAD_REQUEST,
            )
        parsed_session_id = _parse_positive_int(session_id)
        if parsed_session_id is None:
            return Response(
                {"detail": "session_id는 양의 정수여야 합니다."},
                status=http_status.HTTP_400_BAD_REQUEST,
            )
        if send_to not in ("parent", "student"):
            return Response(
                {"detail": "send_to는 'parent' 또는 'student'만 가능합니다."},
                status=http_status.HTTP_400_BAD_REQUEST,
            )

        if notification_type not in ("check_in", "absent"):
            return Response(
                {"detail": "notification_type은 'check_in' 또는 'absent'만 가능합니다."},
                status=http_status.HTTP_400_BAD_REQUEST,
            )

        preview = build_attendance_preview(tenant, parsed_session_id, notification_type, send_to)

        if "error" in preview:
            return Response({"detail": preview["error"]}, status=http_status.HTTP_400_BAD_REQUEST)

        if preview["total_count"] == 0:
            # 발송 대상 없으면 토큰 미발급
            return Response({
                "preview_token": None,
                "recipients": _safe_recipients(preview["recipients"]),
                "total_count": 0,
                "excluded_count": preview["excluded_count"],
                "message_preview": preview.get("message_template_body", ""),
                "session_title": preview.get("session_title", ""),
                "lecture_title": preview.get("lecture_title", ""),
            })

        staff_id = getattr(getattr(request, "user", None), "staff_profile_id", None)
        try:
            token = create_preview_token(
                tenant=tenant,
                preview_data=preview,
                staff_id=staff_id,
                session_type="attendance",
                session_id=parsed_session_id,
                notification_type=notification_type,
                send_to=send_to,
            )
        except ValueError:
            logger.warning(
                "attendance preview token exceeded storage limit tenant=%s session=%s recipients=%s",
                tenant.id,
                parsed_session_id,
                preview["total_count"],
            )
            return Response(
                {"detail": "생성된 미리보기 크기가 너무 큽니다. 대상을 나누어 다시 시도해주세요."},
                status=http_status.HTTP_400_BAD_REQUEST,
            )

        return Response({
            "preview_token": token,
            "recipients": _safe_recipients(preview["recipients"]),
            "total_count": preview["total_count"],
            "excluded_count": preview["excluded_count"],
            "message_preview": preview.get("message_template_body", ""),
            "session_title": preview.get("session_title", ""),
            "lecture_title": preview.get("lecture_title", ""),
        })


class AttendanceNotificationConfirmView(APIView):
    """
    출결 알림 확정 발송.

    POST /api/v1/messaging/attendance-notification/confirm/
    {
        "preview_token": "uuid"
    }

    preview_token 없이 직접 호출 불가.
    동일 token 재사용 불가 (1회용).
    """
    permission_classes = [IsAuthenticated, TenantResolvedAndStaff]

    def post(self, request):
        tenant = getattr(request, "tenant", None)
        if not tenant:
            return Response({"detail": "테넌트 정보가 없습니다."}, status=http_status.HTTP_400_BAD_REQUEST)
        access_error = _messaging_access_error(request, tenant)
        if access_error:
            return access_error

        preview_token = request.data.get("preview_token")
        if not preview_token:
            return Response(
                {"detail": "preview_token은 필수입니다. 미리보기 없이 직접 발송할 수 없습니다."},
                status=http_status.HTTP_400_BAD_REQUEST,
            )

        try:
            result = consume_preview_token_and_execute(preview_token, tenant)
        except MessagingHourlyQuotaExceeded as exc:
            return Response(
                {"detail": str(exc)},
                status=http_status.HTTP_429_TOO_MANY_REQUESTS,
            )
        if "error" in result:
            return Response({"detail": result["error"]}, status=result["status"])

        batch_result = result["batch_result"]

        return Response({
            "batch_id": batch_result["batch_id"],
            "sent_count": batch_result["sent_count"],
            "pending_count": batch_result.get("pending_count", 0),
            "accepted_count": batch_result.get("accepted_count", 0),
            "failed_count": batch_result.get("failed_count", 0),
            "blocked_count": batch_result.get("blocked_count", 0),
        })


class ManualNotificationPreviewView(APIView):
    """
    범용 수동 알림 미리보기.

    POST /api/v1/messaging/manual-notification/preview/
    {
        "trigger": "exam_score_published" | "withdrawal_complete" | ...,
        "student_ids": [1, 2, 3],
        "send_to": "parent" | "student",
        "context": {"시험명": "중간고사", ...}   (optional)
    }
    """
    permission_classes = [IsAuthenticated, TenantResolvedAndStaff]

    ALLOWED_TRIGGERS = {
        # 출결 (attendance 템플릿)
        "check_in_complete", "absent_occurred", "lecture_session_reminder",
        # 시험/성적 (score 템플릿)
        "exam_score_published", "exam_not_taken", "retake_assigned",
        "exam_scheduled_days_before", "exam_start_minutes_before",
        # 과제 (score 템플릿)
        "assignment_not_submitted", "assignment_registered", "assignment_due_hours_before",
        # 성적 리포트 (score 템플릿)
        "monthly_report_generated",
        # 퇴원/결제 (score 템플릿)
        "withdrawal_complete", "payment_complete", "payment_due_days_before",
        # 클리닉 (clinic_info 템플릿)
        "clinic_reminder", "clinic_reservation_created", "clinic_reservation_changed",
        "clinic_cancelled", "clinic_check_in", "clinic_absent",
        "clinic_self_study_completed", "clinic_result_notification",
        "counseling_reservation_created",
        # 영상 (score 템플릿)
        "video_encoding_complete",
        # 매치업 보고서 (score 템플릿) — 강사→학원 owner/admin 수동 발송 가능
        "matchup_report_submitted",
    }
    def post(self, request):
        tenant = getattr(request, "tenant", None)
        if not tenant:
            return Response({"detail": "테넌트 정보가 없습니다."}, status=http_status.HTTP_400_BAD_REQUEST)
        access_error = _messaging_access_error(request, tenant)
        if access_error:
            return access_error

        if _request_exceeds_size_limit(request.data):
            return Response(
                {"detail": "미리보기 요청은 최대 200KB까지 허용합니다."},
                status=http_status.HTTP_400_BAD_REQUEST,
            )

        trigger = request.data.get("trigger")
        student_ids = request.data.get("student_ids", [])
        send_to = request.data.get("send_to", "parent")
        context, context_error = _normalize_context(request.data.get("context") or {}, "context")
        context_source = request.data.get("context_source", None)
        if context_error:
            return Response({"detail": context_error}, status=http_status.HTTP_400_BAD_REQUEST)
        # 학생별 개별 변수 (성적 등) — key: student_id(int)
        context_per_student, context_per_student_error = _normalize_context_per_student(
            request.data.get("context_per_student") or {}
        )
        if context_per_student_error:
            return Response(
                {"detail": context_per_student_error},
                status=http_status.HTTP_400_BAD_REQUEST,
            )

        if not trigger:
            return Response({"detail": "trigger는 필수입니다."}, status=http_status.HTTP_400_BAD_REQUEST)
        if trigger not in self.ALLOWED_TRIGGERS:
            return Response({"detail": f"'{trigger}'는 수동 발송 대상이 아닙니다."}, status=http_status.HTTP_400_BAD_REQUEST)
        if send_to not in ("parent", "student"):
            return Response(
                {"detail": "send_to는 'parent' 또는 'student'만 가능합니다."},
                status=http_status.HTTP_400_BAD_REQUEST,
            )
        if context_source is not None:
            from apps.support.messaging.manual_context_sources import (
                ManualContextSourceError,
                resolve_manual_notification_context_source,
            )

            try:
                resolved_source = resolve_manual_notification_context_source(
                    tenant=tenant,
                    trigger=trigger,
                    context_source=context_source,
                    actor=getattr(request, "user", None),
                )
            except ManualContextSourceError as exc:
                return Response({"detail": str(exc)}, status=http_status.HTTP_400_BAD_REQUEST)

            if len(resolved_source.student_ids) > MAX_MANUAL_NOTIFICATION_RECIPIENTS:
                return Response(
                    {"detail": f"한 번에 최대 {MAX_MANUAL_NOTIFICATION_RECIPIENTS}명까지 미리보기할 수 있습니다."},
                    status=http_status.HTTP_400_BAD_REQUEST,
                )
            protected_context_keys = set(resolved_source.context)
            context_conflicts = protected_context_keys.intersection(context)
            per_student_conflicts = {
                key
                for student_context in context_per_student.values()
                for key in student_context
                if key in protected_context_keys
            }
            if context_conflicts or per_student_conflicts:
                return Response(
                    {
                        "detail": _context_source_override_detail(
                            context_conflicts,
                            per_student_conflicts,
                        ),
                    },
                    status=http_status.HTTP_400_BAD_REQUEST,
                )
            source_context, source_context_error = _normalize_context(
                resolved_source.context,
                "context_source",
            )
            if source_context_error:
                logger.error(
                    "manual context source exceeded safety bounds tenant=%s trigger=%s error=%s",
                    tenant.id,
                    trigger,
                    source_context_error,
                )
                return Response(
                    {"detail": "연결된 데이터가 알림톡 미리보기 허용 범위를 초과했습니다."},
                    status=http_status.HTTP_400_BAD_REQUEST,
                )
            source_context.update(context)
            context, combined_context_error = _normalize_context(source_context, "context")
            if combined_context_error:
                return Response(
                    {"detail": combined_context_error},
                    status=http_status.HTTP_400_BAD_REQUEST,
                )
            student_ids = resolved_source.student_ids
        else:
            student_ids, ids_error = _normalize_student_ids(student_ids)
            if ids_error:
                return Response({"detail": ids_error}, status=http_status.HTTP_400_BAD_REQUEST)

        preview = build_student_list_preview(
            tenant=tenant,
            trigger=trigger,
            student_ids=student_ids,
            send_to=send_to,
            shared_context=context,
            context_per_student=context_per_student or None,
        )

        if "error" in preview:
            return Response({"detail": preview["error"]}, status=http_status.HTTP_400_BAD_REQUEST)

        if preview["total_count"] == 0:
            return Response({
                "preview_token": None,
                "recipients": _safe_recipients(preview["recipients"]),
                "total_count": 0,
                "excluded_count": preview["excluded_count"],
                "trigger": trigger,
            })

        staff_id = getattr(getattr(request, "user", None), "staff_profile_id", None)
        try:
            token = create_preview_token(
                tenant=tenant,
                preview_data=preview,
                staff_id=staff_id,
                session_type="manual",
                session_id=0,
                notification_type=trigger,
                send_to=send_to,
            )
        except ValueError:
            logger.warning(
                "manual preview token exceeded storage limit tenant=%s trigger=%s recipients=%s",
                tenant.id,
                trigger,
                preview["total_count"],
            )
            return Response(
                {"detail": "생성된 미리보기 크기가 너무 큽니다. 대상을 나누어 다시 시도해주세요."},
                status=http_status.HTTP_400_BAD_REQUEST,
            )

        return Response({
            "preview_token": token,
            "recipients": _safe_recipients(preview["recipients"]),
            "total_count": preview["total_count"],
            "excluded_count": preview["excluded_count"],
            "trigger": trigger,
            "message_preview": preview.get("message_template_body", ""),
        })


class ManualNotificationConfirmView(APIView):
    """
    범용 수동 알림 확정 발송. Confirm 로직은 출결과 동일 (토큰 소비 → 발송).

    POST /api/v1/messaging/manual-notification/confirm/
    { "preview_token": "uuid" }
    """
    permission_classes = [IsAuthenticated, TenantResolvedAndStaff]

    def post(self, request):
        tenant = getattr(request, "tenant", None)
        if not tenant:
            return Response({"detail": "테넌트 정보가 없습니다."}, status=http_status.HTTP_400_BAD_REQUEST)
        access_error = _messaging_access_error(request, tenant)
        if access_error:
            return access_error

        preview_token = request.data.get("preview_token")
        if not preview_token:
            return Response(
                {"detail": "preview_token은 필수입니다. 미리보기 없이 직접 발송할 수 없습니다."},
                status=http_status.HTTP_400_BAD_REQUEST,
            )

        try:
            result = consume_preview_token_and_execute(preview_token, tenant)
        except MessagingHourlyQuotaExceeded as exc:
            return Response(
                {"detail": str(exc)},
                status=http_status.HTTP_429_TOO_MANY_REQUESTS,
            )
        if "error" in result:
            return Response({"detail": result["error"]}, status=result["status"])

        batch_result = result["batch_result"]

        return Response({
            "batch_id": batch_result["batch_id"],
            "sent_count": batch_result["sent_count"],
            "pending_count": batch_result.get("pending_count", 0),
            "accepted_count": batch_result.get("accepted_count", 0),
            "failed_count": batch_result.get("failed_count", 0),
            "blocked_count": batch_result.get("blocked_count", 0),
        })
