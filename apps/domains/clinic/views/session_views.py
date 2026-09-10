# PATH: apps/domains/clinic/views/session_views.py
import logging

from django.db import IntegrityError, transaction
from django.db.models import Count, Q, Exists, OuterRef, Prefetch
from django.http import Http404
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, OpenApiTypes, extend_schema
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.exceptions import APIException
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import serializers

from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import SearchFilter, OrderingFilter

from ..models import Session, SessionParticipant
from ..serializers import ClinicSessionSerializer
from ..filters import SessionFilter

from apps.core.permissions import TenantResolvedAndMember, TenantResolvedAndStaff
from apps.api.common.query_params import parse_query_int
from apps.support.clinic.session_dependencies import (
    cancel_pending_clinic_participant_reminders,
    enrollments_for_clinic_tenant,
    get_student_for_clinic_request,
    lectures_for_tenant,
    sections_for_tenant,
    send_clinic_session_reminder,
    unresolve_legacy_booking_links_for_session_delete,
)
from ..services.lifecycle import booking_availability_for_session

logger = logging.getLogger(__name__)


class ClinicAvailabilitySlotSerializer(serializers.Serializer):
    start_time = serializers.TimeField(format="%H:%M")
    end_time = serializers.TimeField(format="%H:%M")
    remaining_capacity = serializers.IntegerField(min_value=0)


class ClinicAvailabilityWindowSerializer(serializers.Serializer):
    start_time = serializers.TimeField(format="%H:%M")
    end_time = serializers.TimeField(format="%H:%M")


class ClinicAvailabilityResponseSerializer(serializers.Serializer):
    booking_mode = serializers.ChoiceField(choices=("fixed_slot", "time_range"))
    interval_minutes = serializers.ChoiceField(choices=(30, 60))
    max_stay_minutes = serializers.IntegerField(min_value=30)
    window = ClinicAvailabilityWindowSerializer()
    slots = ClinicAvailabilitySlotSerializer(many=True)


# ============================================================
# Session
# ============================================================
class SessionViewSet(viewsets.ModelViewSet):
    """
    ✅ 클리닉 세션 CRUD
    - 예약 페이지 / 운영 페이지 공용
    - 모든 participant 통계는 BACKEND 단일진실
    """

    permission_classes = [IsAuthenticated]
    serializer_class = ClinicSessionSerializer

    def get_permissions(self):
        staff_only_actions = {
            "create",
            "update",
            "partial_update",
            "destroy",
            "bulk_create",
            "send_reminder",
            "tree",
        }
        if self.action in staff_only_actions:
            return [IsAuthenticated(), TenantResolvedAndStaff()]
        return [IsAuthenticated(), TenantResolvedAndMember()]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_class = SessionFilter
    search_fields = ["location"]
    ordering_fields = ["date", "start_time", "created_at", "id"]
    ordering = ["-date", "-start_time", "-id"]

    @staticmethod
    def _booking_capacity_prefetch(tenant):
        return Prefetch(
            "participants",
            queryset=SessionParticipant.objects.filter(
                tenant=tenant,
                session__booking_mode="time_range",
                status__in=(
                    SessionParticipant.Status.PENDING,
                    SessionParticipant.Status.BOOKED,
                    SessionParticipant.Status.ATTENDED,
                ),
            ).only("session_id", "booking_start_time", "booking_end_time"),
            to_attr="booking_capacity_participants",
        )

    def get_queryset(self):
        tenant = getattr(self.request, "tenant", None)
        if not tenant:
            raise serializers.ValidationError(
                {"tenant": "테넌트 컨텍스트가 필요합니다. (호스트 또는 X-Tenant-Code 확인)"}
            )
        qs = (
            Session.objects
            .filter(tenant=tenant)
            .prefetch_related("target_lectures", self._booking_capacity_prefetch(tenant))
            .annotate(
                participant_count=Count("participants", distinct=True),
                booked_count=Count(
                    "participants",
                    filter=Q(
                        participants__status__in=[
                            SessionParticipant.Status.BOOKED,
                            SessionParticipant.Status.PENDING,
                        ]
                    ),
                    distinct=True,
                ),
                pending_count=Count(
                    "participants",
                    filter=Q(participants__status=SessionParticipant.Status.PENDING),
                    distinct=True,
                ),
                booked_confirmed_count=Count(
                    "participants",
                    filter=Q(participants__status=SessionParticipant.Status.BOOKED),
                    distinct=True,
                ),
                attended_count=Count(
                    "participants",
                    filter=Q(participants__status=SessionParticipant.Status.ATTENDED),
                    distinct=True,
                ),
                no_show_count=Count(
                    "participants",
                    filter=Q(participants__status=SessionParticipant.Status.NO_SHOW),
                    distinct=True,
                ),
                cancelled_count=Count(
                    "participants",
                    filter=Q(participants__status=SessionParticipant.Status.CANCELLED),
                    distinct=True,
                ),
                auto_count=Count(
                    "participants",
                    filter=Q(participants__source=SessionParticipant.Source.AUTO),
                    distinct=True,
                ),
                manual_count=Count(
                    "participants",
                    filter=Q(participants__source=SessionParticipant.Source.MANUAL),
                    distinct=True,
                ),
            )
        )

        # 학생 조회 시: 본인 조건에 맞는 세션만 노출
        student = get_student_for_clinic_request(self.request)
        if student:
            # 학년 필터: 학년 미설정 학생은 제한 없는 세션만
            if student.grade:
                qs = qs.filter(Q(target_grade__isnull=True) | Q(target_grade=student.grade))
            else:
                qs = qs.filter(target_grade__isnull=True)
            # 학교유형 필터: 미설정 시 제한 없는 세션만
            _no_school_restrict = Q(target_school_type__isnull=True) | Q(target_school_type="")
            if student.school_type:
                qs = qs.filter(_no_school_restrict | Q(target_school_type=student.school_type))
            else:
                qs = qs.filter(_no_school_restrict)
            # 강의 필터: 수강 중인 강의가 대상에 포함되거나 대상 강의가 비어있는 경우
            enrolled_lecture_ids = list(
                enrollments_for_clinic_tenant(tenant).filter(
                    student=student, status="ACTIVE"
                ).values_list("lecture_id", flat=True)
            )
            if enrolled_lecture_ids:
                qs = qs.filter(
                    Q(target_lectures__isnull=True) | Q(target_lectures__id__in=enrolled_lecture_ids)
                ).distinct()
            else:
                qs = qs.filter(target_lectures__isnull=True)

        return qs

    def perform_create(self, serializer):
        """
        ✅ created_by 자동 기록 (운영/감사 기준). 미인증 시 None
        ✅ 멀티테넌트: tenant 없으면 400 (RDS→Aurora 격리 준비)
        ✅ 동일 날짜/시간/장소 중복 생성 시 400 (UniqueConstraint)
        """
        tenant = getattr(self.request, "tenant", None)
        if not tenant:
            raise serializers.ValidationError(
                {"tenant": "테넌트 컨텍스트가 필요합니다. (호스트 또는 X-Tenant-Code 확인)"}
            )
        created_by = self.request.user if self.request.user.is_authenticated else None
        save_kwargs = {
            "tenant": tenant,
            "created_by": created_by,
        }
        effective_booking_mode = serializer.validated_data.get(
            "booking_mode",
            tenant.clinic_booking_mode,
        )
        if "allow_multi_slot_booking" not in serializer.validated_data:
            save_kwargs["allow_multi_slot_booking"] = (
                False
                if effective_booking_mode == "time_range"
                else bool(tenant.clinic_allow_multi_slot_booking_default)
            )
        if "booking_mode" not in serializer.validated_data:
            save_kwargs["booking_mode"] = tenant.clinic_booking_mode
        if "booking_interval_minutes" not in serializer.validated_data:
            save_kwargs["booking_interval_minutes"] = tenant.clinic_booking_interval_minutes
        if "booking_max_stay_minutes" not in serializer.validated_data:
            save_kwargs["booking_max_stay_minutes"] = tenant.clinic_booking_max_stay_minutes
        try:
            serializer.save(**save_kwargs)
        except IntegrityError as e:
            err_str = str(e)
            if "uniq_clinic_session_per_tenant_time_loc" in err_str:
                raise serializers.ValidationError(
                    {"non_field_errors": "같은 날짜·시간·장소·학년의 클리닉이 이미 있습니다. 다른 시간, 장소, 또는 학년을 선택해주세요."}
                )
            raise

    def perform_destroy(self, instance):
        """
        세션 삭제. ClinicLink 해소는 시험/과제 통과에 의해 결정되므로
        세션 삭제 시 해소 상태를 건드리지 않음.
        단, BOOKING_LEGACY 해소만 되돌림 (레거시 호환).

        2026-05-30: cascade delete 직전 active SessionParticipant 들에게
        clinic_cancelled 알림을 발화한다. 이전에는 세션 삭제 시 cascade 로
        participant 가 사라지면서 학생/학부모에게 통지 없이 사라지는 사고가
        있었다. _status_notification(CANCELLED) 와 같은 envelope 을 사용 —
        4종 ITEM_LIST 봉투 SSOT 따름.
        """
        tenant = getattr(self.request, "tenant", None)
        if not tenant:
            raise serializers.ValidationError(
                {"tenant": "테넌트 컨텍스트가 필요합니다. (호스트 또는 X-Tenant-Code 확인)"}
            )

        # cascade 전 active participant 들을 미리 capture (delete 후엔 식별 불가)
        from apps.domains.clinic.services.lifecycle import _status_notification
        from apps.domains.clinic.views.participant_views import _send_clinic_notification

        notifications: list[tuple] = []
        session_id = instance.pk
        with transaction.atomic():
            locked_session = Session.objects.select_for_update().get(
                pk=session_id,
                tenant=tenant,
            )
            participants = list(
                SessionParticipant.objects.select_for_update()
                .select_related("student", "session")
                .filter(tenant=tenant, session=locked_session)
                .order_by("id")
            )
            for participant in participants:
                cancel_pending_clinic_participant_reminders(
                    tenant_id=tenant.id,
                    participant_id=participant.id,
                )
                if participant.status in (
                    SessionParticipant.Status.PENDING,
                    SessionParticipant.Status.BOOKED,
                ):
                    event = _status_notification(
                        participant,
                        SessionParticipant.Status.CANCELLED,
                        actor=getattr(self.request, "user", None),
                    )
                    if event is not None and event.student is not None:
                        notifications.append((event.student, event.trigger, dict(event.context)))
            # 레거시 예약 기반 해소만 되돌림. 실제 pass 기반 해소는 유지.
            # 범위 제한은 support boundary 내부에서 target_lectures 기준으로 적용.
            unresolve_legacy_booking_links_for_session_delete(
                tenant=tenant,
                session=locked_session,
            )
            locked_session.delete()

            # commit 후에 발송 — DB 일관성 보장 + 실패해도 삭제 자체는 완료.
            def _dispatch_clinic_cancelled():
                for student, trigger, context in notifications:
                    try:
                        _send_clinic_notification(
                            tenant=tenant,
                            student=student,
                            trigger=trigger,
                            context=context,
                        )
                    except Exception:
                        logger.exception(
                            "session destroy clinic_cancelled dispatch failed | "
                            "tenant=%s session=%s student=%s",
                            getattr(tenant, "id", "?"), session_id,
                            getattr(student, "id", "?"),
                        )

            transaction.on_commit(_dispatch_clinic_cancelled)

    def retrieve(self, request, *args, **kwargs):
        """단일 세션 조회 시 직렬화 오류 방지 (annotate 필드 누락 등)."""
        try:
            return super().retrieve(request, *args, **kwargs)
        except (Http404, APIException):
            raise
        except Exception:
            logger.exception("ClinicSessionViewSet.retrieve failed")
            return Response(
                {"detail": "세션 조회 중 오류가 발생했습니다."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @action(detail=True, methods=["post"])
    def send_reminder(self, request, pk=None):
        """
        POST /clinic/sessions/{id}/send_reminder/
        - 세션 참가자 리마인더 발송
        """
        session = self.get_object()
        result = send_clinic_session_reminder(session_id=session.id)
        if result.get("status") == "not_found":
            return Response(result, status=status.HTTP_404_NOT_FOUND)
        return Response({"ok": True, **result})

    # ------------------------------------------------------------
    # 운영 페이지 좌측 트리 전용 API
    # ------------------------------------------------------------
    @action(detail=False, methods=["get"])
    def tree(self, request):
        """
        GET /clinic/sessions/tree/?year=YYYY&month=MM
        - 운영 페이지 좌측 트리 전용
        - serializer 우회 (UI 최적화 목적)
        """
        tenant = getattr(request, "tenant", None)
        if not tenant:
            raise serializers.ValidationError(
                {"tenant": "테넌트 컨텍스트가 필요합니다. (호스트 또는 X-Tenant-Code 확인)"}
            )

        year = parse_query_int(
            request.query_params, "year", min_value=1900, max_value=9999
        )
        month = parse_query_int(
            request.query_params, "month", min_value=1, max_value=12
        )
        raw_section = request.query_params.get("section")
        section_id = (
            None
            if raw_section == "unassigned"
            else parse_query_int(request.query_params, "section", min_value=1)
        )

        if not year or not month:
            return Response(
                {"detail": "year and month are required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        qs = (
            Session.objects
            .filter(
                tenant=tenant,
                date__year=year,
                date__month=month,
            )
            .select_related("section")
            .prefetch_related(self._booking_capacity_prefetch(tenant))
            .annotate(
                participant_count=Count("participants"),
                booked_count=Count(
                    "participants",
                    filter=Q(
                        participants__status__in=[
                            SessionParticipant.Status.BOOKED,
                            SessionParticipant.Status.PENDING,
                        ]
                    ),
                ),
                pending_count=Count(
                    "participants",
                    filter=Q(participants__status=SessionParticipant.Status.PENDING),
                ),
                booked_confirmed_count=Count(
                    "participants",
                    filter=Q(participants__status=SessionParticipant.Status.BOOKED),
                ),
                no_show_count=Count(
                    "participants",
                    filter=Q(participants__status=SessionParticipant.Status.NO_SHOW),
                ),
                _has_target_lectures=Exists(
                    Session.target_lectures.through.objects.filter(session_id=OuterRef("pk"))
                ),
            )
            .order_by("date", "start_time", "id")
        )

        if raw_section == "unassigned":
            qs = qs.filter(section__isnull=True)
        elif section_id is not None:
            qs = qs.filter(section_id=section_id)

        capacity_serializer = self.get_serializer()
        data = [
            {
                "id": s.id,
                "title": s.title or "",
                "date": s.date,
                "start_time": s.start_time,
                "location": s.location,
                "target_grade": s.target_grade,
                "target_school_type": s.target_school_type,
                "has_target_lectures": s._has_target_lectures,
                "duration_minutes": s.duration_minutes,
                "participant_count": s.participant_count,
                "booked_count": s.booked_count,
                "pending_count": s.pending_count,
                "booked_confirmed_count": s.booked_confirmed_count,
                "no_show_count": s.no_show_count,
                "max_participants": getattr(s, "max_participants", None),
                "section": s.section_id,
                "section_label": s.section.label if s.section_id else None,
                "section_type": s.section.section_type if s.section_id else None,
                "allow_multi_slot_booking": s.allow_multi_slot_booking,
                "booking_mode": s.booking_mode,
                "booking_interval_minutes": s.booking_interval_minutes,
                "booking_max_stay_minutes": s.booking_max_stay_minutes,
                "is_full": capacity_serializer.get_is_full(s),
            }
            for s in qs
        ]

        return Response(data)

    @action(detail=False, methods=["get"])
    def locations(self, request):
        """
        GET /clinic/sessions/locations/
        - 클리닉 생성 시 장소 불러오기용: 사용된 장소(룸) 목록
        """
        tenant = getattr(request, "tenant", None)
        if not tenant:
            return Response([])
        qs = (
            Session.objects
            .filter(tenant=tenant)
            .values_list("location", flat=True)
            .distinct()
            .order_by("location")
        )
        return Response([x for x in qs if x])

    @extend_schema(
        parameters=[OpenApiParameter("id", OpenApiTypes.INT, OpenApiParameter.PATH)],
        responses={200: ClinicAvailabilityResponseSerializer},
    )
    @action(detail=True, methods=["get"])
    def availability(self, request, pk=None):
        session = self.get_object()
        return Response(
            booking_availability_for_session(
                tenant=request.tenant,
                session=session,
            )
        )

    @action(detail=False, methods=["post"], url_path="bulk-create")
    def bulk_create(self, request, *args, **kwargs):
        """
        POST /clinic/sessions/bulk-create/
        반복 클리닉 세션 일괄 생성 (최대 20일)
        - 과거 날짜는 건너뜀
        - IntegrityError(중복)는 건너뜀
        - tenant는 request.tenant에서 강제 설정
        """
        from ..serializers import ClinicSessionBulkCreateSerializer

        tenant = getattr(request, "tenant", None)
        if not tenant:
            raise serializers.ValidationError(
                {"tenant": "테넌트 컨텍스트가 필요합니다."}
            )

        ser = ClinicSessionBulkCreateSerializer(
            data=request.data,
            context={"request": request},
        )
        ser.is_valid(raise_exception=True)
        data = ser.validated_data

        dates = data.pop("dates")
        target_lecture_ids = data.pop("target_lecture_ids")
        section_id = data.pop("section_id", None)
        allow_multi_slot_booking = data.pop(
            "allow_multi_slot_booking",
            bool(tenant.clinic_allow_multi_slot_booking_default),
        )
        booking_mode = data.pop("booking_mode", tenant.clinic_booking_mode)
        booking_interval_minutes = data.pop(
            "booking_interval_minutes",
            tenant.clinic_booking_interval_minutes,
        )
        booking_max_stay_minutes = data.pop(
            "booking_max_stay_minutes",
            tenant.clinic_booking_max_stay_minutes,
        )
        if booking_mode == "time_range" and "allow_multi_slot_booking" not in request.data:
            allow_multi_slot_booking = False
        if booking_mode == "time_range" and len(dates) > 1:
            raise serializers.ValidationError(
                {"dates": "시간 범위 방식은 한 날짜씩 생성해 주세요."}
            )
        today = timezone.localdate()

        # Validate section belongs to this tenant
        section_obj = None
        if section_id:
            section_obj = sections_for_tenant(tenant).filter(id=section_id).first()
            if not section_obj:
                return Response(
                    {"detail": "선택한 반이 유효하지 않습니다."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # Validate target_lecture_ids belong to this tenant
        if target_lecture_ids:
            valid_count = lectures_for_tenant(tenant).filter(id__in=target_lecture_ids).count()
            if valid_count != len(target_lecture_ids):
                return Response(
                    {"detail": "선택한 강의가 유효하지 않습니다."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        created_by = request.user if request.user.is_authenticated else None

        created = []
        skipped = []

        for d in dates:
            # Skip past dates
            if d < today:
                skipped.append({"date": str(d), "reason": "past_date"})
                continue

            try:
                with transaction.atomic():
                    session = Session.objects.create(
                        tenant=tenant,
                        title=data.get("title", ""),
                        date=d,
                        start_time=data["start_time"],
                        duration_minutes=data["duration_minutes"],
                        location=data["location"],
                        max_participants=data["max_participants"],
                        target_grade=data.get("target_grade"),
                        target_school_type=data.get("target_school_type"),
                        section=section_obj,
                        allow_multi_slot_booking=allow_multi_slot_booking,
                        booking_mode=booking_mode,
                        booking_interval_minutes=booking_interval_minutes,
                        booking_max_stay_minutes=booking_max_stay_minutes,
                        created_by=created_by,
                    )
                    if target_lecture_ids:
                        session.target_lectures.set(target_lecture_ids)
                    created.append({"date": str(d), "id": session.id})
            except IntegrityError:
                skipped.append({"date": str(d), "reason": "duplicate"})

        return Response(
            {
                "created": created,
                "skipped": skipped,
                "created_count": len(created),
                "skipped_count": len(skipped),
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )
