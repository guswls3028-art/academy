# apps/support/messaging/views/send_views.py
"""
알림톡 발송 뷰 — 학생/학부모 대상 수동 발송.
"""

import re

from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from apps.core.permissions import TenantResolvedAndStaff
from apps.domains.messaging.models import MessageTemplate
from apps.domains.messaging.permissions import can_send_messages
from apps.domains.messaging.selectors import HOURLY_SEND_LIMIT, get_hourly_notification_usage
from apps.domains.messaging.serializers import SendMessageRequestSerializer
from apps.domains.messaging.services.grade_personalization import (
    validate_grade_personalization,
)
from apps.domains.messaging.services.recipients import resolve_student_message_recipients


CONTENT_PLACEHOLDERS = ("#{공지내용}", "#{내용}", "#{선생님메모}")


def _dispatch_or_schedule_message(*, tenant_id: int, trigger: str, payload: dict, scheduled_send_at):
    if scheduled_send_at:
        from apps.domains.messaging.scheduled import schedule_notification_at

        schedule_notification_at(
            tenant_id=tenant_id,
            trigger=trigger,
            send_at=scheduled_send_at,
            payload=payload,
        )
        return "scheduled"

    from apps.domains.messaging.models import ScheduledNotification
    from apps.domains.messaging.scheduled import dispatch_notification_now

    notification = dispatch_notification_now(
        tenant_id=tenant_id,
        trigger=trigger,
        payload=payload,
    )
    if notification.status == ScheduledNotification.Status.SENT:
        return "enqueued"
    if notification.status == ScheduledNotification.Status.PENDING:
        return "scheduled"
    return "failed"


class SendMessageView(APIView):
    """
    POST: 선택 학생(들)의 학생/학부모 번호로 알림톡 발송 (SQS enqueue → 워커가 Solapi 발송).
    - student_ids + send_to "student"|"parent": 학생/학부모 전화로 발송
    """
    permission_classes = [IsAuthenticated, TenantResolvedAndStaff]

    def post(self, request):
        tenant = request.tenant
        if not can_send_messages(request, tenant):
            return Response(
                {"detail": "알림톡 발송 권한이 없습니다. 관리자 또는 강사 권한이 필요합니다."},
                status=status.HTTP_403_FORBIDDEN,
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
                status=status.HTTP_403_FORBIDDEN,
            )

        ser = SendMessageRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        send_to = data["send_to"]
        message_mode = "alimtalk"
        template_id = data.get("template_id")
        raw_body = (data.get("raw_body") or "").strip()
        raw_subject = (data.get("raw_subject") or "").strip()
        scheduled_send_at = data.get("scheduled_send_at")

        # 공용 알림톡 정책: 발신번호는 owner 설정을 worker가 사용한다.
        sender = ""

        from apps.domains.messaging.services import get_tenant_site_url
        from apps.domains.messaging.policy import MessagingPolicyError

        # 학생/학부모 수신
        student_ids = data.get("student_ids") or []
        recipients = resolve_student_message_recipients(
            tenant,
            student_ids,
            send_to=send_to,
        )
        if not recipients:
            return Response(
                {"detail": "선택한 학생을 찾을 수 없거나 삭제된 학생입니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(recipients) > 200:
            return Response(
                {"detail": f"한 번에 최대 200명까지 발송할 수 있습니다. (선택: {len(recipients)}명)"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        block_category = (data.get("block_category") or "").strip()
        extra_vars_per_student, personalization_issue = validate_grade_personalization(
            block_category=block_category,
            raw_per_student=data.get("alimtalk_extra_vars_per_student") or {},
            recipients=recipients,
        )
        if personalization_issue:
            return Response(
                {
                    "detail": personalization_issue.detail,
                    "code": personalization_issue.code,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        expected_dispatches = sum(
            1 for recipient in recipients if recipient.phone and len(recipient.phone) >= 10
        )
        recent_count = get_hourly_notification_usage(tenant)
        if (
            not scheduled_send_at
            and recent_count + expected_dispatches > HOURLY_SEND_LIMIT
        ):
            return Response(
                {
                    "detail": (
                        f"시간당 발송 한도({HOURLY_SEND_LIMIT}건)를 초과했습니다. "
                        "잠시 후 다시 시도해 주세요."
                    )
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        body_base = (raw_body or "").strip()
        subject_base = (raw_subject or "").strip()
        t = None
        solapi_template_id = ""
        user_custom_content = ""
        use_unified = False       # 통합 승인 봉투 사용 여부
        unified_template_type = None  # score / attendance / clinic_info / clinic_change / notice_*

        if template_id:
            t = MessageTemplate.objects.filter(tenant=tenant, pk=template_id).first()
            if not t:
                return Response(
                    {"detail": "템플릿을 찾을 수 없습니다."},
                    status=status.HTTP_404_NOT_FOUND,
                )
            tpl_body = t.body or ""
            if body_base and any(marker in tpl_body for marker in CONTENT_PLACEHOLDERS):
                user_custom_content = body_base
            if not body_base:
                body_base = (t.body or "").strip()
            if not subject_base:
                subject_base = (t.subject or "").strip()

        # ── 알림톡: 통합 승인 봉투 우선 사용 ──
        # 시스템 기본양식(signup: 가입승인/비번찾기)만 자체 Solapi 템플릿 유지
        alimtalk_extra_vars = data.get("alimtalk_extra_vars") or {}

        if message_mode == "alimtalk":
            from apps.domains.messaging.alimtalk_content_builders import (
                build_manual_replacements,
                get_unified_for_manual_send,
            )
            category = (t.category if t else "") or ""
            tpl_name = (t.name if t else "") or ""
            unified_tt, unified_sid = get_unified_for_manual_send(
                block_category,
                category,
                tpl_name,
                alimtalk_extra_vars,
            )

            if unified_tt and not unified_sid:
                return Response(
                    {
                        "detail": (
                            "이 발송 유형의 카카오 승인 봉투가 공급사에 등록되어 있지 않아 "
                            "현재 발송할 수 없습니다. 승인 SID 등록 후 다시 시도해 주세요."
                        ),
                        "code": "unified_template_unavailable",
                        "template_type": unified_tt,
                    },
                    status=status.HTTP_409_CONFLICT,
                )
            if unified_tt and unified_sid:
                # 통합 승인 봉투 사용
                use_unified = True
                unified_template_type = unified_tt
                solapi_template_id = unified_sid
            else:
                if not solapi_template_id:
                    use_unified = False

        if message_mode == "alimtalk" and solapi_template_id and not use_unified:
            if t and getattr(t, "solapi_status", None) != "APPROVED":
                return Response(
                    {"detail": "선택한 알림톡 템플릿이 아직 카카오 승인 상태가 아닙니다. 승인된 봉투를 선택해 주세요."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        if message_mode == "alimtalk" and not solapi_template_id:
            return Response(
                {
                    "detail": (
                        "알림톡 발송에는 카카오 승인 봉투가 필요합니다. "
                        "양식 선택에서 출석/성적/클리닉/일정변경 봉투를 선택해 주세요."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not body_base:
            return Response(
                {"detail": "발송할 본문이 비어 있습니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        enqueued = 0
        scheduled = 0
        skipped_no_phone = 0
        enqueue_failed = 0
        for recipient in recipients:
            phone = recipient.phone
            if not phone or len(phone) < 10:
                skipped_no_phone += 1
                continue
            name = recipient.student_name
            name_2 = name[-2:] if len(name) >= 2 else name
            name_3 = name
            site_url = get_tenant_site_url(request.tenant) or ""
            academy_name = (tenant.name or "").strip()

            # 학생별 개별 변수 merge
            student_extra = dict(extra_vars_per_student.get(recipient.student_id, {}))

            # SSOT (2026-05-13): 학생별 치환된 본문 우선. frontend SessionScoresEntryPage 일괄 path가
            # substituteScoreVars 결과를 _body_subst 로 보냄. backend가 그대로 사용 → 모든 score
            # sub-variable(#{시험1명}, #{시험1점수}, #{과제N...}, #{시험총점}) 치환됨. 학원장 limglish 보고
            # "본문 변수 미치환 → 빈 자리" 결함 fix.
            student_body_override = student_extra.pop("_body_subst", None)
            if block_category == "grades":
                # Grade sends are fail-closed above: never substitute the shared
                # request body (which may contain another student's scores).
                student_body = student_body_override.strip()
            else:
                student_body = student_body_override or body_base

            merged_context = {**alimtalk_extra_vars, **student_extra}

            # 알림톡 text 필드용 변수 치환 — merged_context 전체 key 순회 (고정 list 제거).
            text = (
                student_body.replace("#{학생이름}", name)
                .replace("#{학생이름2}", name_2)
                .replace("#{학생이름3}", name_3)
                .replace("#{학원명}", academy_name)
                .replace("#{학원이름}", academy_name)
                .replace("#{사이트링크}", site_url)
            )
            for var_key, var_val in merged_context.items():
                if var_key.startswith("_"):
                    continue  # internal hint (e.g. _body_subst) skip
                text = text.replace(f"#{{{var_key}}}", str(var_val or ""))
            text = re.sub(r"#\{[^}]+\}", "", text)
            text = re.sub(r"\n{3,}", "\n\n", text).strip()
            if subject_base:
                text = subject_base + "\n" + text

            alimtalk_replacements = None
            template_id_solapi = None

            if message_mode == "alimtalk" and solapi_template_id:
                template_id_solapi = solapi_template_id

                if use_unified and unified_template_type:
                    # ── 통합 승인 봉투: build_manual_replacements로 정확한 변수 세트 빌드 ──
                    # SSOT (2026-05-13): student_body (학생별 치환된 본문) 사용 → 봉투의 #{선생님메모} 변수에
                    # 정확한 학생별 점수가 들어감.
                    alimtalk_replacements = build_manual_replacements(
                        template_type=unified_template_type,
                        content_body=student_body,
                        context=merged_context,
                        tenant_name=academy_name,
                        student_name=name,
                        site_url=site_url,
                    )
                else:
                    # ── 시스템 기본양식: 기존 방식 유지 (가입승인/비번 등) ──
                    alimtalk_replacements = [
                        {"key": "학생이름", "value": name},
                        {"key": "학생이름2", "value": name_2},
                        {"key": "학생이름3", "value": name_3},
                        {"key": "학원명", "value": academy_name},
                        {"key": "사이트링크", "value": site_url},
                    ]
                    for var_key, var_val in merged_context.items():
                        if var_key.startswith("_"):
                            continue  # internal hint
                        if var_val and var_key not in ("학생이름", "학생이름2", "학생이름3", "사이트링크"):
                            alimtalk_replacements.append({"key": var_key, "value": str(var_val)})
                    if user_custom_content:
                        alimtalk_replacements.append({"key": "공지내용", "value": user_custom_content})
                        alimtalk_replacements.append({"key": "내용", "value": user_custom_content})
                        alimtalk_replacements.append({"key": "선생님메모", "value": user_custom_content})

            try:
                dispatch_result = _dispatch_or_schedule_message(
                    tenant_id=tenant.id,
                    trigger="manual_send",
                    scheduled_send_at=scheduled_send_at,
                    payload={
                        "tenant_id": tenant.id,
                        "to": phone,
                        "text": text,
                        "sender": sender,
                        "message_mode": message_mode,
                        "template_id": template_id_solapi,
                        "alimtalk_replacements": alimtalk_replacements,
                        "event_type": "manual_send",
                        "target_type": "student" if send_to != "parent" else "parent",
                        "target_id": recipient.student_id,
                        "target_name": name,
                    },
                )
            except MessagingPolicyError as e:
                return Response(
                    {"detail": str(e) or "알림톡 발송 정책에 의해 차단되었습니다."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            if dispatch_result == "enqueued":
                enqueued += 1
            elif dispatch_result == "scheduled":
                scheduled += 1
            else:
                enqueue_failed += 1

        accepted = enqueued + scheduled
        detail = f"{'예약됨' if scheduled_send_at else '발송 예정'} {accepted}건"
        if enqueue_failed:
            detail += f" (큐 등록 실패 {enqueue_failed}건)"
        if skipped_no_phone:
            detail += f" (전화번호 없음 {skipped_no_phone}건)"
        return Response({
            "detail": detail + ".",
            "enqueued": enqueued,
            "scheduled": scheduled,
            "enqueue_failed": enqueue_failed,
            "skipped_no_phone": skipped_no_phone,
        }, status=status.HTTP_200_OK)
