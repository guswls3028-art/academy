from __future__ import annotations

import logging
import re

from django.utils.html import strip_tags


logger = logging.getLogger(__name__)

_E2E_MARKER_RE = re.compile(r"\[E2E(?:-[^\]]+)?\]", re.IGNORECASE)


def notify_qna_created(post, *, actor_user=None) -> int:
    """Keep Q&A external delivery closed until its exact provider contract exists."""
    del actor_user
    if getattr(post, "post_type", "") != "qna" or not getattr(
        post,
        "created_by_id",
        None,
    ):
        return 0
    logger.info(
        "qna created alimtalk skipped: post=%s exact provider contract inspecting",
        getattr(post, "id", None),
    )
    return 0


def notify_qna_answered(
    post,
    reply,
    *,
    send_to: str = "student",
    actor_user=None,
) -> int:
    """Keep Q&A external delivery closed until its exact provider contract exists."""
    del reply, actor_user
    if getattr(post, "post_type", "") != "qna" or not getattr(
        post,
        "created_by_id",
        None,
    ):
        return 0
    logger.info(
        "qna answer alimtalk skipped: post=%s send_to=%s exact provider contract inspecting",
        getattr(post, "id", None),
        send_to,
    )
    return 0


def should_suppress_qna_notification(post) -> bool:
    """Identify production E2E probes without using their content for delivery."""
    if not post:
        return False
    for attr in ("title", "content"):
        value = _clean(getattr(post, attr, "") or "")
        if value and _E2E_MARKER_RE.search(value):
            return True
    return False


def _clean(value: object) -> str:
    return re.sub(r"\s+", " ", strip_tags(str(value or ""))).strip()
