"""Solapi Kakao template API adapter.

솔라피 알림톡 템플릿 등록(검수 신청) — REST API 연동

- POST /kakao/v2/templates (channelId=PFID, name, content, categoryCode)
- #{변수명} 형식 검증
- API Key 인증: HMAC-SHA256 (date + salt → signature)
"""

import hmac
import hashlib
import logging
import re
import secrets
from datetime import datetime, timezone

import requests

from apps.shared.utils.circuit_breaker import circuit_breaker

logger = logging.getLogger(__name__)

SOLAPI_BASE = "https://api.solapi.com"
TEMPLATE_CREATE_PATH = "/kakao/v2/templates"
TEMPLATE_LIST_PATH = "/kakao/v2/templates"

_CLONE_OPTIONAL_FIELDS = (
    "buttons",
    "quickReplies",
    "header",
    "highlight",
    "item",
    "extra",
    "ad",
    "emphasizeTitle",
    "emphasizeSubtitle",
    "securityFlag",
    "imageId",
)


def _has_meaningful_value(value) -> bool:
    if isinstance(value, dict):
        return any(_has_meaningful_value(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_meaningful_value(item) for item in value)
    return value not in (None, "", False)

# #{변수명} 형식 검증 시 참고 (필요 시 확장)
VARIABLE_PATTERN = re.compile(r"#\{[^}]+\}")


def validate_template_variables(*texts: str) -> tuple[bool, list[str]]:
    """
    템플릿 본문/제목에 #{변수명} 형식이 유지되는지 검증.
    - #{변수명} 형태가 올바르게 닫혀 있는지 확인 ( #{ ... } )
    - 닫히지 않은 #{ 가 있으면 에러
    반환: (성공 여부, 에러 메시지 리스트)
    """
    errors = []
    for raw in texts:
        if not raw or not raw.strip():
            continue
        idx = 0
        while True:
            open_brace = raw.find("#{", idx)
            if open_brace == -1:
                break
            close_brace = raw.find("}", open_brace)
            if close_brace == -1:
                errors.append("'#{'에 대응하는 '}'가 없습니다.")
                break
            idx = close_brace + 1
    return (len(errors) == 0, errors)


def _create_auth_header(api_key: str, api_secret: str) -> str:
    """HMAC-SHA256 Authorization 헤더 생성 (Solapi 규격)."""
    date_time = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    salt = secrets.token_hex(16)
    data = date_time + salt
    signature = hmac.new(
        api_secret.encode(),
        data.encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"HMAC-SHA256 apiKey={api_key}, date={date_time}, salt={salt}, signature={signature}"


@circuit_breaker(
    name="solapi_template",
    failure_threshold=5,
    window_seconds=30,
    cooldown_seconds=60,
    expected_exceptions=[requests.RequestException, ValueError],
)
def create_kakao_template(
    api_key: str,
    api_secret: str,
    channel_id: str,
    name: str,
    content: str,
    category_code: str = "TE",
    message_type: str = "BA",
    emphasize_type: str = "NONE",
) -> dict:
    """
    솔라피 알림톡 템플릿 등록(검수 신청).

    Args:
        api_key: SOLAPI API Key
        api_secret: SOLAPI API Secret
        channel_id: 카카오 채널 ID (테넌트 kakao_pfid)
        name: 템플릿 이름
        content: 본문 (변수 #{변수명} 포함 가능)
        category_code: 카테고리 코드 (기본 TE)
        message_type: BA(기본형) 등
        emphasize_type: NONE 등

    Returns:
        {"templateId": "KA01TP..."} 또는 예외

    Raises:
        ValueError: 검증 실패 또는 API 에러
    """
    channel_id = (channel_id or "").strip()
    if not channel_id:
        raise ValueError("channelId(PFID)가 필요합니다.")
    if not (name or "").strip():
        raise ValueError("템플릿 이름이 필요합니다.")
    if not (content or "").strip():
        raise ValueError("템플릿 본문이 필요합니다.")

    ok, errs = validate_template_variables(content)
    if not ok:
        raise ValueError("변수 검증 실패: " + "; ".join(errs))

    url = SOLAPI_BASE + TEMPLATE_CREATE_PATH
    headers = {
        "Authorization": _create_auth_header(api_key, api_secret),
        "Content-Type": "application/json",
    }
    body = {
        "channelId": channel_id,
        "name": name.strip(),
        "content": content.strip(),
        "categoryCode": (category_code or "TE").strip(),
        "messageType": message_type,
        "emphasizeType": emphasize_type,
    }

    logger.info("Solapi template create request channelId=%s name=%s", channel_id, name[:30])
    resp = requests.post(url, json=body, headers=headers, timeout=30)

    if resp.status_code != 200:
        try:
            err_body = resp.json()
            msg = err_body.get("errorMessage") or err_body.get("message") or resp.text
        except Exception:
            msg = resp.text
        logger.warning("Solapi template create failed status=%s body=%s", resp.status_code, msg)
        raise ValueError(f"솔라피 템플릿 등록 실패: {msg}")

    data = resp.json()
    template_id = (data.get("templateId") or data.get("id") or "").strip()
    if not template_id:
        raise ValueError("솔라피 응답에 templateId가 없습니다.")
    return {"templateId": template_id, "raw": data}


def clone_kakao_template(
    *,
    api_key: str,
    api_secret: str,
    channel_id: str,
    source_template: dict,
    name: str,
) -> dict:
    """Create an exact behavior-preserving copy on another channel."""

    body = {
        "channelId": (channel_id or "").strip(),
        "name": (name or "").strip(),
        "content": str(source_template.get("content") or "").strip(),
        "categoryCode": str(source_template.get("categoryCode") or "").strip(),
        "messageType": str(source_template.get("messageType") or "BA").strip(),
        "emphasizeType": str(source_template.get("emphasizeType") or "NONE").strip(),
    }
    if not all(body.values()):
        raise ValueError("채널 복제에 필요한 템플릿 필드가 없습니다.")
    for field in _CLONE_OPTIONAL_FIELDS:
        value = source_template.get(field)
        if _has_meaningful_value(value):
            body[field] = value

    response = requests.post(
        SOLAPI_BASE + TEMPLATE_CREATE_PATH,
        json=body,
        headers={
            "Authorization": _create_auth_header(api_key, api_secret),
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    if response.status_code != 200:
        try:
            error = response.json()
            message = error.get("errorMessage") or error.get("message") or response.text
        except Exception:
            message = response.text
        raise ValueError(f"솔라피 템플릿 복제 실패: {message}")
    return response.json()


def request_kakao_template_inspection(
    *,
    api_key: str,
    api_secret: str,
    template_id: str,
    comment: str = "",
) -> dict:
    """Submit a newly created template for Kakao inspection."""

    template_id = (template_id or "").strip()
    if not template_id:
        raise ValueError("templateId가 필요합니다.")
    response = requests.put(
        f"{SOLAPI_BASE}{TEMPLATE_CREATE_PATH}/{template_id}/inspection",
        json={"comment": comment} if comment else {},
        headers={
            "Authorization": _create_auth_header(api_key, api_secret),
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    if response.status_code != 200:
        try:
            error = response.json()
            message = error.get("errorMessage") or error.get("message") or response.text
        except Exception:
            message = response.text
        raise ValueError(f"솔라피 템플릿 검수 요청 실패: {message}")
    return response.json()


@circuit_breaker(
    name="solapi_template_list",
    failure_threshold=5,
    window_seconds=30,
    cooldown_seconds=60,
    expected_exceptions=[requests.RequestException, ValueError],
)
def list_kakao_templates(
    api_key: str,
    api_secret: str,
    channel_id: str,
    limit: int = 100,
    status_filter: str | None = None,
) -> list[dict]:
    """
    솔라피 알림톡 템플릿 목록 조회 — 콘솔에 등록된 양식 전체.

    학원장이 솔라피 콘솔에서 직접 만들거나 검수 신청한 양식이 SaaS DB와
    drift할 때 pull sync 용도. 검수 상태(APPROVED/PENDING/REJECTED)도 함께
    가져와서 SaaS DB의 solapi_status를 최신화.

    Returns:
        [{"templateId": ..., "name": ..., "content": ..., "status": ..., ...}, ...]
        솔라피 페이지네이션은 startKey 방식 — 최대 5페이지 (500건) 안전 한도.
    """
    channel_id = (channel_id or "").strip()
    if not channel_id:
        raise ValueError("channelId(PFID)가 필요합니다.")

    url = SOLAPI_BASE + TEMPLATE_LIST_PATH
    headers = {"Authorization": _create_auth_header(api_key, api_secret)}
    params: dict = {"channelId": channel_id, "limit": min(max(limit, 1), 100)}
    if status_filter:
        params["status"] = status_filter

    collected: list[dict] = []
    seen_ids: set[str] = set()
    page = 0
    start_key: str | None = None
    while page < 5:  # 안전 한도 — 500건 초과는 별도 페이지네이션 필요
        page += 1
        # 매번 새 인증 헤더 (signature 재사용 방지 — solapi 4xx)
        headers["Authorization"] = _create_auth_header(api_key, api_secret)
        if start_key:
            params["startKey"] = start_key
        logger.info("Solapi template list request channelId=%s page=%d", channel_id, page)
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        if resp.status_code != 200:
            try:
                err_body = resp.json()
                msg = err_body.get("errorMessage") or err_body.get("message") or resp.text
            except Exception:
                msg = resp.text
            logger.warning(
                "Solapi template list failed status=%s body=%s", resp.status_code, msg,
            )
            raise ValueError(f"솔라피 템플릿 조회 실패: {msg}")
        data = resp.json()
        items = data.get("templateList") or data.get("list") or data.get("data") or []
        if not isinstance(items, list):
            if isinstance(items, dict):
                items = list(items.values())
            else:
                items = []
        # 중복 차단 — solapi가 nextKey를 응답하지 않거나 같은 페이지를 반환하는 경우 안전망
        new_chunk = []
        for it in items:
            tid = (it.get("templateId") or it.get("id") or "").strip()
            if not tid or tid in seen_ids:
                continue
            seen_ids.add(tid)
            new_chunk.append(it)
        if not new_chunk:
            # 새 양식이 0개면 페이지네이션 종료 (무한루프 방지)
            break
        collected.extend(new_chunk)
        # 페이지네이션 키 추출
        next_key = (
            data.get("nextKey")
            or (data.get("paging") or {}).get("nextKey")
            or None
        )
        if not next_key or next_key == start_key:
            break
        start_key = next_key
    return collected
