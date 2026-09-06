# apps/support/messaging/alimtalk_content_builders.py
# SSOT 문서: backend/docs/domain/messaging-alimtalk.md §0 (학원장 mental model 박스 필독)
"""Approved Alimtalk provider-envelope contracts.

Every business event resolves only its explicitly configured provider identity
and exact variable contract. Category, saved-letter name/body, newest/default
rows, and other provider templates are never fallback inputs. A teacher's
saved letter is content only and becomes active solely after explicit selection
for the current send. Missing or drifting provider contracts fail closed before
the provider boundary.

See backend/docs/domain/messaging-alimtalk.md and
backend/docs/ssot/messaging-policy.md.
"""

from __future__ import annotations

# ──────────────────────────────────────────
# 통합 Solapi 템플릿 ID (검수 통과 후 사용)
# ──────────────────────────────────────────
# 카카오 검수 승인 완료 시 True로 변경 → 즉시 통합 템플릿 사용 시작
# 미승인 상태에서 True로 두면 Solapi 발송 거부됨 — 반드시 승인 확인 후 변경
UNIFIED_TEMPLATES_ENABLED = True

# ITEM_LIST 4종 — header/highlight/item.list 자동 렌더, "[성적표 안내]" 등 카테고리 prefix가 카카오 측에 박혀있음
SOLAPI_CLINIC_INFO = "KA01TP2604061058318608Hy40ZnTFZT"      # 클리닉 일정 안내
SOLAPI_CLINIC_CHANGE = "KA01TP260406110706969XS06XRZveEk"    # 클리닉 일정 변경
SOLAPI_SCORE = "KA01TP260406105458211774JKJ3OU55"            # 성적표발송 — 진짜 성적 트리거 전용
SOLAPI_ATTENDANCE = "KA01TP260406121126868FGddLmrDFUC"       # 수업출석안내

# Solapi read-only contract snapshot for the currently approved score envelope.
# These values contain no tenant or recipient data. Changing the provider
# template requires updating this snapshot and its contract test together so a
# queued score notification cannot silently switch envelopes.
SCORE_PROVIDER_TEMPLATE_NAME = "성적표발송"
SCORE_PROVIDER_TEMPLATE_VERSION = "Wy7Z91sBXK"
SCORE_PROVIDER_CONTENT_FINGERPRINT = "a5605726f724dd9b"
SCORE_PROVIDER_HEADER_FINGERPRINT = "dbaf4d19b3af2b21"

# NONE 2종 — emphasizeType=NONE, "안녕하세요, #{학원명}입니다. / #{학생이름2}학생님, ... / ... / #{사이트링크}"
# 카카오 검수 통과 + 살아있는 양식만. (clinic/exam/assignment/generic 전용 NONE은 사용자가 정리하며 삭제했음)
SOLAPI_NOTICE_WITHDRAWAL = "KA01TP260324051933087KD2pyRhJonv"   # [HakwonPlus] 퇴원 처리 완료
# 2026-07-08 Solapi 실등록 감사 결과: 기존 결제 완료 SID가 provider 목록에 없음.
# 논리 매핑은 유지하되, 재등록/재승인 전까지 fail-closed 상태로 둔다.
SOLAPI_NOTICE_PAYMENT    = ""   # [HakwonPlus] 결제 완료 안내

# ──────────────────────────────────────────
# 템플릿 타입 상수
# ──────────────────────────────────────────

# ITEM_LIST 4종 — 카카오 측에 카테고리 prefix("[성적표 안내]" 등)가 박혀있어 의미 일치 트리거만 사용
TYPE_CLINIC_INFO = "clinic_info"        # 장소/날짜/시간
TYPE_CLINIC_CHANGE = "clinic_change"    # 기존일정/변동사항/수정자
TYPE_SCORE = "score"                    # 강의명/차시명 — 진짜 성적 안내만 ("[성적표 안내]" prefix)
TYPE_ATTENDANCE = "attendance"          # 강의명/차시명/강의날짜/강의시간

# NONE 2종 — 본문 고정형, 사이트링크/학생이름2/학원명만 변수
TYPE_NOTICE_WITHDRAWAL = "notice_withdrawal"
TYPE_NOTICE_PAYMENT    = "notice_payment"

MANUAL_EVENT_TO_TEMPLATE_TYPE: dict[str, str] = {
    "lesson_result": TYPE_SCORE,
    "attendance_notice": TYPE_ATTENDANCE,
    "clinic_reservation_notice": TYPE_CLINIC_INFO,
    "clinic_change_notice": TYPE_CLINIC_CHANGE,
}

TEMPLATE_TYPE_TO_SOLAPI_ID = {
    TYPE_CLINIC_INFO: SOLAPI_CLINIC_INFO,
    TYPE_CLINIC_CHANGE: SOLAPI_CLINIC_CHANGE,
    TYPE_SCORE: SOLAPI_SCORE,
    TYPE_ATTENDANCE: SOLAPI_ATTENDANCE,
    TYPE_NOTICE_WITHDRAWAL: SOLAPI_NOTICE_WITHDRAWAL,
    TYPE_NOTICE_PAYMENT: SOLAPI_NOTICE_PAYMENT,
}

# NONE 양식 식별 (ITEM_LIST 변수 23자 제한 / item.list 자동 렌더 미적용)
NONE_TEMPLATE_TYPES = frozenset({
    TYPE_NOTICE_WITHDRAWAL,
    TYPE_NOTICE_PAYMENT,
})
TEACHER_MEMO_KEYS = ("선생님메모",)

# ──────────────────────────────────────────
# 트리거 → 템플릿 타입 매핑
# ──────────────────────────────────────────

TRIGGER_TO_TEMPLATE_TYPE: dict[str, str] = {
    # 클리닉 일정 안내 (장소/날짜/시간)
    "clinic_reservation_created": TYPE_CLINIC_INFO,
    "clinic_reminder": TYPE_CLINIC_INFO,
    "clinic_check_in": TYPE_CLINIC_INFO,
    "clinic_check_out": TYPE_CLINIC_INFO,
    "clinic_absent": TYPE_CLINIC_INFO,
    "clinic_self_study_completed": TYPE_CLINIC_INFO,
    "clinic_result_notification": TYPE_CLINIC_INFO,
    "counseling_reservation_created": TYPE_CLINIC_INFO,

    # 클리닉 일정 변경/취소
    "clinic_reservation_changed": TYPE_CLINIC_CHANGE,
    "clinic_cancelled": TYPE_CLINIC_CHANGE,

    # 수업출석안내 (강의명/차시명/강의날짜/강의시간)
    "check_in_complete": TYPE_ATTENDANCE,
    "absent_occurred": TYPE_ATTENDANCE,
    "lecture_session_reminder": TYPE_ATTENDANCE,

    # 시험/과제 일정·미응시·미제출 — 학생/학부모에게 "강의/차시 컨텍스트 안내" 의미.
    # 학원장이 #{선생님메모} 자유 편집으로 시험 명/과제 명 등 구체 안내.
    # (2026-05-30 사용자 directive: 전부 공용 알림톡 통일)
    "exam_scheduled_days_before": TYPE_ATTENDANCE,
    "exam_start_minutes_before": TYPE_ATTENDANCE,
    "exam_not_taken": TYPE_ATTENDANCE,
    "assignment_registered": TYPE_ATTENDANCE,
    "assignment_due_hours_before": TYPE_ATTENDANCE,
    "assignment_not_submitted": TYPE_ATTENDANCE,

    # 재시험 배정 — 보강 클리닉과 같은 "장소/날짜/시간" 컨텍스트.
    "retake_assigned": TYPE_CLINIC_INFO,

    # 성적표발송 — 진짜 성적 통보만 (강의명/차시명 + "[성적표 안내]" prefix 의미상 일치)
    "exam_score_published": TYPE_SCORE,
    "monthly_report_generated": TYPE_SCORE,

    # 퇴원 / 결제 — 카테고리별 전용 NONE 양식 (본문 고정 시스템 안내)
    "withdrawal_complete": TYPE_NOTICE_WITHDRAWAL,
    "payment_complete": TYPE_NOTICE_PAYMENT,
    "payment_due_days_before": TYPE_NOTICE_PAYMENT,

    # ── 매핑 보류 (4종 ITEM_LIST + 2종 NONE 봉투 중 의미상 적절한 매칭 없음) ─
    # video_encoding_complete / matchup_report_submitted / qna_answered /
    # counsel_answered 4건. 현 봉투(강의·차시·성적·클리닉·퇴원·결제)와 의미가
    # 어느 쪽도 맞지 않아 자동 매핑하면 학원장이 "왜 이 봉투로?" 의문이 든다.
    # → exact 공급사 계약을 등록하고 코드 계약을 갱신할 때까지 발송 0건.
}


def get_template_type(trigger: str) -> str | None:
    """트리거에 해당하는 통합 템플릿 타입 반환. 매핑 없으면 None."""
    return TRIGGER_TO_TEMPLATE_TYPE.get(trigger)


def get_solapi_template_id(trigger: str) -> str | None:
    """트리거에 해당하는 Solapi 템플릿 ID 반환. 통합 템플릿 미활성 시 None."""
    if not UNIFIED_TEMPLATES_ENABLED:
        return None
    tt = get_template_type(trigger)
    if tt:
        return TEMPLATE_TYPE_TO_SOLAPI_ID.get(tt)
    return None


def _append_replacement(
    replacements: list[dict[str, str]],
    key: str,
    value: str,
) -> None:
    replacements.append({"key": key, "value": value})


def get_unified_for_manual_send(manual_event: str) -> tuple[str | None, str | None]:
    """Resolve only an explicit manual business event; never infer/fallback."""
    template_type = MANUAL_EVENT_TO_TEMPLATE_TYPE.get((manual_event or "").strip())
    if not template_type or not UNIFIED_TEMPLATES_ENABLED:
        return None, None
    return template_type, TEMPLATE_TYPE_TO_SOLAPI_ID.get(template_type)


def build_manual_replacements(
    template_type: str,
    content_body: str,
    context: dict,
    tenant_name: str,
    student_name: str,
    site_url: str,
) -> list[dict[str, str]]:
    """
    수동 발송용 통합 템플릿 replacements 빌드.
    build_unified_replacements()와 동일 로직이나, trigger 대신 template_type을 직접 받음.
    """
    import re

    # 서브변수 치환용 dict
    all_vars = {
        "학원이름": tenant_name,
        "학원명": tenant_name,
        "학생이름": student_name,
        "학생이름2": student_name[-2:] if len(student_name) >= 2 else student_name,
        "학생이름3": student_name,
        "사이트링크": site_url,
    }
    context_var_mapping: dict[str, list[str]] = {
        "클리닉장소": ["장소", "클리닉장소", "place"],
        "클리닉날짜": ["날짜", "클리닉날짜", "date"],
        "클리닉시간": ["시간", "클리닉시간", "time"],
        "클리닉기존일정": ["클리닉기존일정", "clinic_old_schedule"],
        "클리닉변동사항": ["클리닉변동사항", "clinic_changes"],
        "클리닉수정자": ["클리닉수정자", "clinic_modifier"],
        "강의명": ["강의명", "lecture_name"],
        "차시명": ["차시명", "session_name"],
        "강의날짜": ["날짜", "강의날짜", "date"],
        "강의시간": ["시간", "강의시간", "time"],
        "날짜": ["날짜", "date"],
        "시간": ["시간", "time"],
    }

    for var_name, ctx_keys in context_var_mapping.items():
        for ctx_key in ctx_keys:
            if ctx_key in context and context[ctx_key]:
                all_vars[var_name] = str(context[ctx_key])
                break

    for k, v in context.items():
        if not k.startswith("_") and k not in all_vars:
            all_vars[k] = str(v) if v else ""

    # content_body 내 #{서브변수} 치환 → #{선생님메모} 값
    built_content = content_body
    for k, v in all_vars.items():
        built_content = built_content.replace(f"#{{{k}}}", v)

    built_content = re.sub(r"#\{[^}]+\}", "", built_content)
    built_content = re.sub(r"\n{3,}", "\n\n", built_content).strip()

    # NONE 양식은 #{공지내용}으로, ITEM_LIST 양식은 #{선생님메모}로 들어감
    all_vars["공지내용"] = built_content
    memo_value = built_content

    # Solapi replacements — ITEM_LIST 변수 23자 제한 (선생님메모/사이트링크/공지내용 제외)
    is_none_type = template_type in NONE_TEMPLATE_TYPES
    registered_vars = TEMPLATE_TYPE_VARIABLES.get(template_type, [])
    replacements = []
    for var_name in registered_vars:
        if var_name in TEACHER_MEMO_KEYS:
            _append_replacement(replacements, var_name, memo_value)
        elif var_name == "공지내용":
            _append_replacement(replacements, var_name, built_content)
        elif var_name == "사이트링크":
            _append_replacement(replacements, var_name, site_url)
        elif var_name in all_vars:
            val = all_vars[var_name]
            if not is_none_type and len(val) > ITEM_LIST_VAR_MAX_LEN:
                val = val[:ITEM_LIST_VAR_MAX_LEN - 1] + "…"
            _append_replacement(replacements, var_name, val)
        else:
            _append_replacement(replacements, var_name, "-")

    return replacements



# ──────────────────────────────────────────
# 템플릿 타입별 등록 변수 (Solapi에 전달해야 하는 전체 변수)
# ──────────────────────────────────────────

# 카카오 검수 시 등록된 변수 전체를 보내야 함 (ITEM_LIST 템플릿).
# 누락하면 3063(잘못된 파라미터), 값이 23자 초과하면 3076(길이초과) 에러.
# 선생님메모에 핵심 정보를 조합하므로 나머지 변수는 요약값만 전달.
TEMPLATE_TYPE_VARIABLES: dict[str, list[str]] = {
    TYPE_CLINIC_INFO: [
        "학원이름", "학생이름", "클리닉장소", "클리닉날짜", "클리닉시간", "선생님메모", "사이트링크",
    ],
    TYPE_CLINIC_CHANGE: [
        "학원이름", "학생이름", "클리닉기존일정", "클리닉변동사항", "클리닉수정자", "선생님메모", "사이트링크",
    ],
    TYPE_SCORE: [
        "학원이름", "학생이름", "강의명", "차시명", "선생님메모", "사이트링크",
    ],
    TYPE_ATTENDANCE: [
        "학원이름", "학생이름", "강의명", "차시명", "강의날짜", "강의시간", "선생님메모", "사이트링크",
    ],
    # NONE 2종 — 카카오 등록 variables (Solapi 상세 조회로 검증된 set)
    TYPE_NOTICE_WITHDRAWAL: ["학원명", "학생이름2"],                  # 본문 고정, 사이트링크 없음
    TYPE_NOTICE_PAYMENT:    ["학원명", "학생이름2", "사이트링크"],     # 본문 고정 + 사이트링크
}

PROVIDER_TEMPLATE_CONTRACTS: dict[str, dict[str, object]] = {
    TYPE_SCORE: {
        "template_id": SOLAPI_SCORE,
        "template_version": SCORE_PROVIDER_TEMPLATE_VERSION,
        "structure_fingerprint": "54f3fb7aca49daaf",
        "content_fingerprint": SCORE_PROVIDER_CONTENT_FINGERPRINT,
        "header_fingerprint": SCORE_PROVIDER_HEADER_FINGERPRINT,
        "variables": tuple(TEMPLATE_TYPE_VARIABLES[TYPE_SCORE]),
    },
    TYPE_ATTENDANCE: {
        "template_id": SOLAPI_ATTENDANCE,
        "template_version": "CXHFcwKdJU",
        "structure_fingerprint": "7f443b87eb8d8c95",
        "variables": tuple(TEMPLATE_TYPE_VARIABLES[TYPE_ATTENDANCE]),
    },
    TYPE_CLINIC_INFO: {
        "template_id": SOLAPI_CLINIC_INFO,
        "template_version": "W6jQe04a0p",
        "structure_fingerprint": "9e8c96df5beebac2",
        "variables": tuple(TEMPLATE_TYPE_VARIABLES[TYPE_CLINIC_INFO]),
    },
    TYPE_CLINIC_CHANGE: {
        "template_id": SOLAPI_CLINIC_CHANGE,
        "template_version": "h1gIqH2CAP",
        "structure_fingerprint": "7fe0e9e234d26730",
        "variables": tuple(TEMPLATE_TYPE_VARIABLES[TYPE_CLINIC_CHANGE]),
    },
    TYPE_NOTICE_WITHDRAWAL: {
        "template_id": SOLAPI_NOTICE_WITHDRAWAL,
        "template_version": "4YYJgCcZrx",
        "structure_fingerprint": "dd9f1bc5faa889e5",
        "variables": tuple(TEMPLATE_TYPE_VARIABLES[TYPE_NOTICE_WITHDRAWAL]),
    },
}


def get_provider_template_contract(template_type: str) -> dict[str, object] | None:
    contract = PROVIDER_TEMPLATE_CONTRACTS.get(template_type)
    return dict(contract) if contract else None

# ITEM_LIST 변수 값 길이 제한 (카카오 정책: 23자)
ITEM_LIST_VAR_MAX_LEN = 23


def render_alimtalk_preview_text(
    template_type: str,
    replacements: list[dict[str, str]],
) -> str:
    """Render the approved envelope as staff will see it in KakaoTalk.

    Values must come from the same replacement list passed to Solapi so the
    preview cannot drift from tenant, student, truncation, or fallback rules.
    """
    values = {
        str(item.get("key") or ""): str(item.get("value") or "")
        for item in replacements
        if item.get("key")
    }

    def value(key: str) -> str:
        return values.get(key) or "-"

    if template_type == TYPE_CLINIC_INFO:
        return (
            f"{value('학원이름')}입니다.\n\n"
            f"{value('학생이름')}학생님.\n\n"
            "클리닉 안내 드립니다.\n"
            f"장소\n{value('클리닉장소')}\n\n"
            f"날짜\n{value('클리닉날짜')}\n\n"
            f"시간\n{value('클리닉시간')}\n\n"
            f"{value('선생님메모')}\n"
            f"{value('사이트링크')}"
        ).strip()
    if template_type == TYPE_CLINIC_CHANGE:
        return (
            f"{value('학원이름')}입니다.\n\n"
            f"{value('학생이름')}학생님. 클리닉 일정이 변경되었습니다.\n\n"
            "일정 변경 안내 드립니다.\n"
            f"기존일정\n{value('클리닉기존일정')}\n\n"
            f"변동사항\n{value('클리닉변동사항')}\n\n"
            f"수정자\n{value('클리닉수정자')}\n\n"
            f"{value('선생님메모')}\n"
            f"{value('사이트링크')}"
        ).strip()
    if template_type == TYPE_SCORE:
        return (
            f"{value('학원이름')}입니다.\n\n"
            f"{value('학생이름')}학생님.\n\n"
            "성적표 안내 드립니다.\n"
            f"강의\n{value('강의명')}\n\n"
            f"차시\n{value('차시명')}\n\n"
            f"{value('선생님메모')}\n"
            f"{value('사이트링크')}"
        ).strip()
    if template_type == TYPE_ATTENDANCE:
        return (
            f"{value('학원이름')}입니다.\n\n"
            f"{value('학생이름')}학생님.\n\n"
            "출석 안내 드립니다.\n"
            f"강의\n{value('강의명')}\n\n"
            f"차시\n{value('차시명')}\n\n"
            f"날짜\n{value('강의날짜')}\n\n"
            f"시간\n{value('강의시간')}\n\n"
            f"{value('선생님메모')}\n"
            f"{value('사이트링크')}"
        ).strip()
    if template_type == TYPE_NOTICE_WITHDRAWAL:
        return (
            f"안녕하세요, {value('학원명')}입니다.\n\n"
            f"{value('학생이름2')}학생님, 퇴원 처리가 완료되었습니다.\n\n"
            "그동안 학원을 이용해 주셔서 감사합니다.\n"
            "재등록을 원하시면 언제든 문의해 주세요."
        )
    if template_type == TYPE_NOTICE_PAYMENT:
        return (
            f"안녕하세요, {value('학원명')}입니다.\n\n"
            f"{value('학생이름2')}학생님, 결제가 완료되었습니다.\n\n"
            "수업·결제 내역은 아래 링크에서 확인하실 수 있습니다.\n"
            f"{value('사이트링크')}"
        ).strip()
    return value("선생님메모")


def build_unified_replacements(
    trigger: str,
    content_body: str,
    context: dict,
    tenant_name: str,
    student_name: str,
    site_url: str,
) -> list[dict[str, str]]:
    """
    통합 템플릿용 Solapi replacements 빌드.

    1. content_body 내의 #{서브변수}를 context 값으로 치환 → #{선생님메모} 값
    2. 템플릿 타입의 등록 변수 전체를 replacements로 반환

    Args:
        trigger: AutoSendConfig 트리거명
        content_body: 사용자 편집 가능한 #{선생님메모} 콘텐츠 (서브변수 포함)
        context: 도메인 컨텍스트 (강의명, 장소 등)
        tenant_name: 학원명
        student_name: 학생 전체 이름
        site_url: 테넌트 사이트 URL

    Returns:
        Solapi replacements list: [{"key": "선생님메모", "value": "..."}, ...]
    """
    import re

    template_type = get_template_type(trigger)
    if not template_type:
        return []

    # 서브변수 치환용 dict
    all_vars = {
        "학원이름": tenant_name,
        "학원명": tenant_name,  # alias — 사용자 커스텀 body에서 #{학원명} 사용 가능
        "학생이름": student_name,
        "학생이름2": student_name[-2:] if len(student_name) >= 2 else student_name,  # 성 제외
        "학생이름3": student_name,  # 전체 이름 (하위 호환)
        "사이트링크": site_url,
    }
    # 도메인 컨텍스트 매핑: Solapi 변수명 → [context에서 찾을 키 목록] (한국어 우선, 영어 하위호환)
    # 호출자는 한국어 키(장소, 날짜, 시간 등)를 전달함.
    context_var_mapping: dict[str, list[str]] = {
        # clinic_info — 호출자: {"장소": "301호", "날짜": "2026-04-08", "시간": "14:00"}
        "클리닉장소": ["장소", "place"],
        "클리닉날짜": ["날짜", "date"],
        "클리닉시간": ["시간", "time"],
        "클리닉명": ["클리닉명", "clinic_name"],
        # clinic_change — 호출자: 미구현, 향후 한국어 키 사용 예정
        "클리닉기존일정": ["클리닉기존일정", "clinic_old_schedule"],
        "클리닉변동사항": ["클리닉변동사항", "clinic_changes"],
        "클리닉수정자": ["클리닉수정자", "clinic_modifier"],
        # score / attendance — 호출자: {"강의명": "수학", "차시명": "1차시", "날짜": "...", "시간": "..."}
        "강의명": ["강의명", "lecture_name"],
        "차시명": ["차시명", "session_name"],
        "강의날짜": ["날짜", "date"],
        "강의시간": ["시간", "time"],
        "시험명": ["시험명", "exam_name"],
        "과제명": ["과제명", "assignment_name"],
        "성적": ["성적", "score"],
        "시험성적": ["시험성적", "exam_score"],
        "클리닉합불": ["클리닉합불", "clinic_result"],
        # payment — 결제/납부 트리거
        "납부금액": ["납부금액", "amount"],
        "청구월": ["청구월", "billing_month"],
        # common
        "날짜": ["날짜", "date"],
        "시간": ["시간", "time"],
        "장소": ["장소", "place"],
    }

    for var_name, ctx_keys in context_var_mapping.items():
        for ctx_key in ctx_keys:
            if ctx_key in context and context[ctx_key]:
                all_vars[var_name] = str(context[ctx_key])
                break

    # 직접 한국어 키로 전달된 context도 반영
    for k, v in context.items():
        if not k.startswith("_") and k not in all_vars:
            all_vars[k] = str(v) if v else ""

    # content_body 내 #{서브변수} 치환
    built_content = content_body
    for k, v in all_vars.items():
        built_content = built_content.replace(f"#{{{k}}}", v)

    # 미치환 optional 변수 제거
    built_content = re.sub(r"#\{[^}]+\}", "", built_content)
    built_content = re.sub(r"\n{3,}", "\n\n", built_content).strip()

    # NONE 양식은 #{공지내용}으로, ITEM_LIST 양식은 #{선생님메모}로 들어감
    all_vars["공지내용"] = built_content
    memo_value = built_content

    # Solapi replacements 빌드 — 등록된 모든 변수에 값 제공
    # ITEM_LIST 변수는 23자 이하로 잘라야 함 (선생님메모/사이트링크/공지내용 제외)
    is_none_type = template_type in NONE_TEMPLATE_TYPES
    registered_vars = TEMPLATE_TYPE_VARIABLES.get(template_type, [])
    replacements = []
    for var_name in registered_vars:
        if var_name in TEACHER_MEMO_KEYS:
            _append_replacement(replacements, var_name, memo_value)
        elif var_name == "공지내용":
            _append_replacement(replacements, var_name, built_content)
        elif var_name == "사이트링크":
            _append_replacement(replacements, var_name, site_url)
        elif var_name in all_vars:
            val = all_vars[var_name]
            if not is_none_type and len(val) > ITEM_LIST_VAR_MAX_LEN:
                val = val[:ITEM_LIST_VAR_MAX_LEN - 1] + "…"
            _append_replacement(replacements, var_name, val)
        else:
            _append_replacement(replacements, var_name, "-")

    return replacements
