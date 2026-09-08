# PATH: apps/domains/clinic/views/participant_views.py
import logging

from django.db import transaction
from django.db.models import Prefetch
from drf_spectacular.utils import OpenApiParameter, OpenApiTypes, extend_schema
from rest_framework import mixins, viewsets, status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import serializers

from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import SearchFilter, OrderingFilter

from ..models import SessionParticipant, SessionParticipantPlanItem
from ..serializers import (
    ClinicSessionParticipantBulkCreateResponseSerializer,
    ClinicSessionParticipantBulkCreateSerializer,
    ClinicSessionParticipantSerializer,
    ClinicSessionParticipantCreateSerializer,
)
from ..filters import ONSITE_PARTICIPANT_ORDERING, ParticipantFilter
from ..services import (
    change_participant_booking,
    change_participant_status,
    checkout_participant,
    complete_participant,
    create_participant,
    create_participants_bulk,
    replace_participant_clinic_plan,
    uncomplete_participant,
    update_participant_staff_memo,
)

from apps.core.permissions import TenantResolvedAndMember, TenantResolvedAndStaff
from apps.api.common.query_params import parse_query_int
from apps.core.services.tenant_access import STAFF_ROLES, get_active_membership_role
from apps.support.clinic.session_dependencies import (
    get_student_for_clinic_request,
    retry_failed_clinic_notification,
    send_clinic_event_notification,
    send_clinic_reminder_for_participant,
)

logger = logging.getLogger(__name__)

CLINIC_RECIPIENT_TARGETS = ("student", "parent", "both")


class ClinicRecipientActionSerializer(serializers.Serializer):
    send_to = serializers.ChoiceField(choices=CLINIC_RECIPIENT_TARGETS)


class ClinicCheckoutRequestSerializer(serializers.Serializer):
    send_to = serializers.ChoiceField(
        choices=CLINIC_RECIPIENT_TARGETS,
        required=False,
        write_only=True,
    )
    confirm_without_arrival = serializers.BooleanField(required=False, default=False)
    expected_session_id = serializers.IntegerField(min_value=1, required=False)
    expected_student_id = serializers.IntegerField(min_value=1, required=False)


class ClinicReminderRequestSerializer(ClinicRecipientActionSerializer):
    mode = serializers.ChoiceField(choices=("once", "repeat"), default="once")
    interval_minutes = serializers.IntegerField(min_value=10, required=False)
    repeat_until = serializers.DateTimeField(required=False)


class ClinicReminderResponseSerializer(serializers.Serializer):
    ok = serializers.BooleanField(required=False)
    status = serializers.CharField(required=False)
    sent = serializers.IntegerField(required=False)
    scheduled = serializers.IntegerField(required=False)
    skipped = serializers.IntegerField(required=False)
    detail = serializers.CharField(required=False)


class ClinicStaffMemoSerializer(serializers.Serializer):
    staff_memo = serializers.CharField(allow_blank=True, max_length=2000)


class ClinicNotificationRetryRequestSerializer(serializers.Serializer):
    log_id = serializers.IntegerField(min_value=1)


class ClinicNotificationRetryResponseSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=("accepted",))
    outbox_id = serializers.IntegerField(min_value=1)
    origin_id = serializers.CharField()


class ClinicPlanReplaceSerializer(serializers.Serializer):
    planned_clinic_link_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=True,
        max_length=200,
    )


def _validated_send_to(request, *, default: str) -> str:
    value = request.data.get("send_to", default)
    if value not in CLINIC_RECIPIENT_TARGETS:
        raise serializers.ValidationError(
            {"send_to": "send_to는 student, parent, both 중 하나여야 합니다."}
        )
    return value


def _schedule_change_send_to(request, *, default: str) -> str:
    role = get_active_membership_role(
        getattr(request, "user", None),
        getattr(request, "tenant", None),
    )
    if role == "student":
        return "both"
    return _validated_send_to(request, default=default)


def _get_request_student_for_clinic(request):
    tenant = getattr(request, "tenant", None)
    user = getattr(request, "user", None)
    role = get_active_membership_role(user, tenant)
    if role in STAFF_ROLES:
        return None
    if role not in ("student", "parent"):
        raise PermissionDenied("클리닉 이용 권한을 확인할 수 없습니다.")
    student = get_student_for_clinic_request(request)
    if student is None:
        raise PermissionDenied("선택한 학생 정보를 확인할 수 없습니다.")
    return student


def _send_clinic_notification(
    tenant,
    student,
    trigger,
    context=None,
    *,
    send_to="both",
    include_target_results=False,
):
    """Queue the exact approved clinic Alimtalk for selected recipients only."""
    event_context = dict(context or {})
    event_context.setdefault("_source_domain", "clinic")
    event_context.setdefault("_source_use_case", f"clinic.{trigger}")
    targets = ("parent", "student") if send_to == "both" else (send_to,)
    requested = 0
    failed = 0
    target_results = []
    for target in targets:
        try:
            queued = send_clinic_event_notification(
                tenant=tenant,
                trigger=trigger,
                student=student,
                send_to=target,
                context=event_context,
            )
            requested += int(bool(queued))
            failed += int(not queued)
            target_results.append({"target": target, "requested": bool(queued)})
        except Exception:
            failed += 1
            target_results.append({"target": target, "requested": False})
            logger.exception(
                "clinic notification failed: trigger=%s student=%s target=%s",
                trigger,
                getattr(student, "id", "?"),
                target,
            )
    result = {
        "requested": requested,
        "failed": failed,
        "send_to": send_to,
    }
    if include_target_results:
        result["targets"] = target_results
    return result


# ============================================================
# Participant
# ============================================================
class ParticipantViewSet(
    mixins.CreateModelMixin,
    mixins.RetrieveModelMixin,
    mixins.ListModelMixin,
    viewsets.GenericViewSet,
):
    """
    ✅ 클리닉 예약 / 출석 / 미이행 / 취소 관리
    - 운영 핵심 엔드포인트
    """

    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_class = ParticipantFilter
    search_fields = ["student__name", "session__location"]
    ordering_fields = ["created_at", "updated_at", "session__date", "id"]
    ordering = ["-created_at", "-id"]

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name="onsite_date",
                type=OpenApiTypes.DATE,
                location=OpenApiParameter.QUERY,
                required=False,
                description=(
                    "현장 운영 날짜(YYYY-MM-DD). 현재 tenant에서 session.date가 같은 "
                    "attended + checked_in_at 존재 + checked_out_at 미존재 참가자만 "
                    "checked_in_at, session.start_time, id 오름차순으로 pagination 전에 "
                    "정렬합니다. is_late, completed_at, ClinicLink와 today-plan은 포함 "
                    "판정에 영향을 주지 않습니다."
                ),
            )
        ]
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)
        if "onsite_date" in self.request.query_params:
            return queryset.order_by(*ONSITE_PARTICIPANT_ORDERING)
        return queryset

    def get_permissions(self):
        if self.action in (
            "update",
            "partial_update",
            "destroy",
            "complete",
            "checkout",
            "replace_planned_clinic_links",
            "uncomplete",
            "remind",
            "set_staff_memo",
            "retry_notification",
        ):
            return [TenantResolvedAndStaff()]
        return [IsAuthenticated(), TenantResolvedAndMember()]

    def get_queryset(self):
        tenant = getattr(self.request, "tenant", None)
        if not tenant:
            raise serializers.ValidationError(
                {"tenant": "테넌트 컨텍스트가 필요합니다. (호스트 또는 X-Tenant-Code 확인)"}
            )
        qs = (
            SessionParticipant.objects
            .filter(tenant=tenant)
            .filter(student__deleted_at__isnull=True)  # 삭제된 학생 제외
            .select_related(
                "student",
                "student__parent__user",
                "session",
                "status_changed_by",
                "completed_by",
                "checked_out_by",
                "enrollment__lecture",
            )
            .prefetch_related(
                "session__target_lectures",
                Prefetch(
                    "plan_items",
                    queryset=(
                        SessionParticipantPlanItem.objects.filter(removed_at__isnull=True)
                        .select_related(
                            "clinic_link__enrollment",
                            "clinic_link__session",
                        )
                        .order_by("clinic_link_id", "id")
                    ),
                    to_attr="_active_plan_items",
                ),
            )
        )

        # 학생이 조회하는 경우: 자신의 예약 신청만 조회
        student = _get_request_student_for_clinic(self.request)
        if student:
            qs = qs.filter(student=student)

        return qs

    def get_serializer_class(self):
        if self.action == "create":
            return ClinicSessionParticipantCreateSerializer
        if self.action == "bulk_create":
            return ClinicSessionParticipantBulkCreateSerializer
        return ClinicSessionParticipantSerializer

    def create(self, request, *args, **kwargs):
        """
        ✅ 예약 생성
        - 선생: student, enrollment_id 직접 지정 가능
        - 학생: student 자동 설정, source="student_request", status="pending"
        - session 또는 (requested_date + requested_start_time) 중 하나 사용
        """
        tenant = getattr(request, "tenant", None)
        if not tenant:
            raise serializers.ValidationError(
                {"tenant": "테넌트 컨텍스트가 필요합니다. (호스트 또는 X-Tenant-Code 확인)"}
            )

        request_student = _get_request_student_for_clinic(request)
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        result = create_participant(
            tenant=tenant,
            validated_data=serializer.validated_data,
            request_student=request_student,
        )
        obj = result.participant
        if result.notification:
            _t = tenant
            _event = result.notification
            transaction.on_commit(
                lambda: _send_clinic_notification(
                    _t,
                    _event.student,
                    _event.trigger,
                    _event.context,
                )
            )

        out = ClinicSessionParticipantSerializer(
            obj, context={"request": request}
        ).data
        return Response(out, status=status.HTTP_201_CREATED)

    @extend_schema(
        request=ClinicSessionParticipantBulkCreateSerializer,
        responses={201: ClinicSessionParticipantBulkCreateResponseSerializer},
    )
    @action(detail=False, methods=["post"], url_path="bulk-create")
    def bulk_create(self, request, *args, **kwargs):
        """Create every selected same-day slot atomically for a student or staff selection."""
        tenant = getattr(request, "tenant", None)
        if not tenant:
            raise serializers.ValidationError(
                {"tenant": "테넌트 컨텍스트가 필요합니다. (호스트 또는 X-Tenant-Code 확인)"}
            )

        request_student = _get_request_student_for_clinic(request)
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = create_participants_bulk(
            tenant=tenant,
            request_student=request_student,
            **serializer.validated_data,
        )

        if result.notifications:
            events = result.notifications

            def send_notifications():
                for event in events:
                    _send_clinic_notification(
                        tenant,
                        event.student,
                        event.trigger,
                        event.context,
                    )

            transaction.on_commit(send_notifications)

        response_data = {
            "count": len(result.participants),
            "participants": ClinicSessionParticipantSerializer(
                result.participants,
                many=True,
                context={"request": request},
            ).data,
        }
        return Response(response_data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["patch"])
    def set_status(self, request, pk=None):
        """
        PATCH /clinic/participants/{id}/set_status/
        - 상태 변경 + audit 기록
        - 학생/학부모: 자신의 pending/booked 예약 취소 가능. 필수 대상자는 같은 주 예약 1개 유지
        - 선생: 모든 상태 변경 가능
        """
        next_status = request.data.get("status")
        try:
            is_late = serializers.BooleanField().run_validation(
                request.data.get("is_late", False)
            )
        except serializers.ValidationError as exc:
            raise serializers.ValidationError({"is_late": exc.detail}) from exc
        if next_status != SessionParticipant.Status.ATTENDED and is_late:
            raise serializers.ValidationError(
                {"is_late": "지각 여부는 등원 처리할 때만 지정할 수 있습니다."}
            )

        request_student = _get_request_student_for_clinic(request)
        send_to = (
            "both"
            if request_student is not None
            and next_status == SessionParticipant.Status.CANCELLED
            else _schedule_change_send_to(request, default="parent")
        )
        if request_student is None and not TenantResolvedAndStaff().has_permission(request, self):
            raise PermissionDenied("클리닉 상태 변경은 스태프만 가능합니다.")
        staff_memo = None
        if request_student is None:
            staff_memo = request.data.get("staff_memo", request.data.get("memo"))

        result = change_participant_status(
            tenant=getattr(request, "tenant", None),
            participant_id=self.get_object().pk,
            next_status=next_status,
            actor=request.user,
            request_student=request_student,
            staff_memo=staff_memo,
            is_late=is_late,
        )
        obj = result.participant
        notification_result = None
        if result.notification:
            notification_result = _send_clinic_notification(
                getattr(request, "tenant", None),
                result.notification.student,
                result.notification.trigger,
                result.notification.context,
                send_to=send_to,
                include_target_results=request_student is not None,
            )

        out = ClinicSessionParticipantSerializer(
            obj, context={"request": request}
        ).data
        out["notification"] = notification_result
        if obj.status == SessionParticipant.Status.ATTENDED:
            out["attendance_label"] = "지각 등원" if obj.is_late else "등원"
        return Response(out)

    @extend_schema(
        request=ClinicStaffMemoSerializer,
        parameters=[OpenApiParameter("id", OpenApiTypes.INT, OpenApiParameter.PATH)],
        responses={200: ClinicSessionParticipantSerializer},
    )
    @action(detail=True, methods=["patch"], url_path="staff-memo")
    def set_staff_memo(self, request, pk=None):
        payload = ClinicStaffMemoSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        participant = update_participant_staff_memo(
            tenant=getattr(request, "tenant", None),
            participant_id=self.get_object().pk,
            staff_memo=payload.validated_data["staff_memo"],
        )
        return Response(
            ClinicSessionParticipantSerializer(
                participant,
                context={"request": request},
            ).data
        )

    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        """
        POST /clinic/participants/{id}/complete/
        자율학습 완료 처리 — 이력 기록 + 알림톡 트리거

        ATTENDED 상태에서만 completed_at을 기록한다.
        참석 처리 전 상태에서는 완료 처리할 수 없으며, 완료 취소도 참석 상태를 유지한다.
        """
        send_to = _validated_send_to(request, default="parent")
        result = complete_participant(
            tenant=getattr(request, "tenant", None),
            participant_id=self.get_object().pk,
            actor=request.user,
        )
        obj = result.participant
        notification_result = None
        if result.notification:
            notification_result = _send_clinic_notification(
                getattr(request, "tenant", None),
                result.notification.student,
                result.notification.trigger,
                result.notification.context,
                send_to=send_to,
            )

        out = ClinicSessionParticipantSerializer(
            obj, context={"request": request}
        ).data
        out["notification"] = notification_result
        return Response(out)

    @extend_schema(
        request=ClinicCheckoutRequestSerializer,
        parameters=[OpenApiParameter("id", OpenApiTypes.INT, OpenApiParameter.PATH)],
        responses={200: ClinicSessionParticipantSerializer, 400: OpenApiTypes.OBJECT},
    )
    @action(detail=True, methods=["post"])
    def checkout(self, request, pk=None):
        """Record departure independently from self-study completion."""
        payload = ClinicCheckoutRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        send_to = payload.validated_data.get("send_to", "parent")
        result = checkout_participant(
            tenant=getattr(request, "tenant", None),
            participant_id=self.get_object().pk,
            actor=request.user,
            confirm_without_arrival=payload.validated_data["confirm_without_arrival"],
            expected_session_id=payload.validated_data.get("expected_session_id"),
            expected_student_id=payload.validated_data.get("expected_student_id"),
        )
        obj = result.participant

        out = ClinicSessionParticipantSerializer(
            obj, context={"request": request}
        ).data
        notification_result = None
        if result.notification:
            notification_result = _send_clinic_notification(
                getattr(request, "tenant", None),
                result.notification.student,
                result.notification.trigger,
                result.notification.context,
                send_to=send_to,
            )
        out["notification"] = notification_result
        return Response(out)

    @extend_schema(
        request=ClinicPlanReplaceSerializer,
        parameters=[OpenApiParameter("id", OpenApiTypes.INT, OpenApiParameter.PATH)],
        responses={200: ClinicSessionParticipantSerializer, 400: OpenApiTypes.OBJECT},
    )
    @action(detail=True, methods=["put"], url_path="planned-clinic-links")
    def replace_planned_clinic_links(self, request, pk=None):
        """Replace the staff-authored, session-scoped set of today's ClinicLinks."""
        payload = ClinicPlanReplaceSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        participant = replace_participant_clinic_plan(
            tenant=getattr(request, "tenant", None),
            participant_id=self.get_object().pk,
            clinic_link_ids=payload.validated_data["planned_clinic_link_ids"],
            actor=request.user,
        )
        out = ClinicSessionParticipantSerializer(
            participant,
            context={"request": request},
        ).data
        return Response(out)

    @extend_schema(
        request=ClinicReminderRequestSerializer,
        parameters=[OpenApiParameter("id", OpenApiTypes.INT, OpenApiParameter.PATH)],
        responses={
            200: ClinicReminderResponseSerializer,
            404: ClinicReminderResponseSerializer,
            409: ClinicReminderResponseSerializer,
            503: ClinicReminderResponseSerializer,
        },
    )
    @action(detail=True, methods=["post"])
    def remind(self, request, pk=None):
        """
        POST /clinic/participants/{id}/remind/
        승인된 단일 예약 학생에게 클리닉 재촉 알림톡을 발송한다.
        """
        participant = self.get_object()
        if participant.status != SessionParticipant.Status.BOOKED:
            return Response(
                {"detail": "참석 전인 승인 예약만 재촉할 수 있습니다."},
                status=status.HTTP_409_CONFLICT,
            )

        send_to = _validated_send_to(request, default="student")
        mode = request.data.get("mode", "once")
        if mode not in ("once", "repeat"):
            raise serializers.ValidationError({"mode": "mode는 once 또는 repeat여야 합니다."})
        interval_minutes = None
        repeat_until = None
        if mode == "repeat":
            interval_minutes = serializers.IntegerField(min_value=10).run_validation(
                request.data.get("interval_minutes")
            )
            repeat_until = serializers.DateTimeField().run_validation(
                request.data.get("repeat_until")
            )

        reminder_kwargs = {
            "tenant_id": getattr(request, "tenant", None).id,
            "participant_id": participant.id,
            "actor_id": getattr(request.user, "id", None),
        }
        if send_to != "student":
            reminder_kwargs["send_to"] = send_to
        if mode == "repeat":
            reminder_kwargs.update(
                repeat_interval_minutes=interval_minutes,
                repeat_until=repeat_until,
            )
        result = send_clinic_reminder_for_participant(**reminder_kwargs)
        if result.get("status") == "not_found":
            return Response(result, status=status.HTTP_404_NOT_FOUND)
        if result.get("status") == "invalid_status":
            return Response(result, status=status.HTTP_409_CONFLICT)
        if not result.get("sent") and not result.get("scheduled"):
            return Response(
                {
                    **result,
                    "detail": "재촉 알림톡을 보내지 못했습니다. 알림 설정과 학생 전화번호를 확인해 주세요.",
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response({"ok": True, **result})

    @extend_schema(
        request=ClinicNotificationRetryRequestSerializer,
        parameters=[OpenApiParameter("id", OpenApiTypes.INT, OpenApiParameter.PATH)],
        responses={
            200: ClinicNotificationRetryResponseSerializer,
            404: OpenApiTypes.OBJECT,
            409: OpenApiTypes.OBJECT,
        },
    )
    @action(detail=True, methods=["post"], url_path="retry-notification")
    def retry_notification(self, request, pk=None):
        """Retry a confirmed failed clinic Alimtalk from its exact durable payload."""

        participant = self.get_object()
        try:
            log_id = int(request.data.get("log_id"))
        except (TypeError, ValueError) as exc:
            raise serializers.ValidationError(
                {"log_id": "재시도할 발송 로그가 필요합니다."}
            ) from exc
        result = retry_failed_clinic_notification(
            tenant=request.tenant,
            participant=participant,
            log_id=log_id,
            actor_id=request.user.id,
        )
        if result["result"] == "not_found":
            return Response(
                {"detail": result["detail"]},
                status=status.HTTP_404_NOT_FOUND,
            )
        if result["result"] == "conflict":
            return Response(
                {"detail": result["detail"]},
                status=status.HTTP_409_CONFLICT,
            )
        return Response({
            "status": result["status"],
            "outbox_id": result["outbox_id"],
            "origin_id": result["origin_id"],
        })

    @action(detail=True, methods=["post"])
    def uncomplete(self, request, pk=None):
        """
        POST /clinic/participants/{id}/uncomplete/
        완료 취소
        """
        result = uncomplete_participant(
            tenant=getattr(request, "tenant", None),
            participant_id=self.get_object().pk,
            actor=request.user,
        )
        obj = result.participant

        out = ClinicSessionParticipantSerializer(
            obj, context={"request": request}
        ).data
        return Response(out)

    @action(detail=True, methods=["post"], url_path="change-booking")
    def change_booking(self, request, pk=None):
        """
        POST /clinic/participants/{id}/change-booking/
        Atomic booking change: secure new session first, then cancel old.
        If new booking fails, old booking is preserved (transaction rollback).

        Request body: { "new_session_id": int, "student_request_memo": str (optional) }
        """
        new_session_id = request.data.get("new_session_id")
        memo = request.data.get("memo")
        student_request_memo = request.data.get("student_request_memo")
        try:
            preferred_start_time = serializers.TimeField(
                required=False, allow_null=True
            ).run_validation(request.data.get("preferred_start_time"))
            preferred_end_time = serializers.TimeField(
                required=False, allow_null=True
            ).run_validation(request.data.get("preferred_end_time"))
            booking_start_time = serializers.TimeField(
                required=False, allow_null=True
            ).run_validation(request.data.get("booking_start_time"))
            booking_end_time = serializers.TimeField(
                required=False, allow_null=True
            ).run_validation(request.data.get("booking_end_time"))
        except serializers.ValidationError as exc:
            raise serializers.ValidationError({"preferred_time": exc.detail}) from exc
        send_to = _schedule_change_send_to(request, default="parent")

        tenant = getattr(request, "tenant", None)
        if not tenant:
            return Response(
                {"detail": "테넌트 컨텍스트가 필요합니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        result = change_participant_booking(
            tenant=tenant,
            participant_id=pk,
            new_session_id=new_session_id,
            request_student=_get_request_student_for_clinic(request),
            actor=request.user,
            memo=memo,
            student_request_memo=student_request_memo,
            preferred_start_time=preferred_start_time,
            preferred_end_time=preferred_end_time,
            booking_start_time=booking_start_time,
            booking_end_time=booking_end_time,
        )
        new_booking = result.participant
        notification_result = None
        if result.notification:
            notification_result = _send_clinic_notification(
                tenant,
                result.notification.student,
                result.notification.trigger,
                result.notification.context,
                send_to=send_to,
            )

        out = ClinicSessionParticipantSerializer(
            new_booking, context={"request": request}
        ).data
        out["notification"] = notification_result
        return Response(out, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"])
    def by_session(self, request):
        """
        GET /clinic/participants/by_session/?session_id=12
        """
        session_id = parse_query_int(request.query_params, "session_id", min_value=1)
        if session_id is None:
            return Response(
                {"detail": "session_id is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        qs = self.get_queryset().filter(session_id=session_id)
        data = ClinicSessionParticipantSerializer(
            qs, many=True, context={"request": request}
        ).data
        return Response(data)
