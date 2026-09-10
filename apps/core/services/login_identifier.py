"""Canonical normalization for public login identifiers."""

from __future__ import annotations

import re
import unicodedata


_PHONE_LOGIN_SEPARATORS_RE = re.compile(r"[\s\-().]+")


def normalize_login_identifier(value: object) -> str:
    """Normalize only an unambiguous Korean mobile-number login ID.

    Staff and student identifiers may legitimately contain punctuation, so a
    general punctuation-stripping rule would change valid custom IDs.
    """
    raw = unicodedata.normalize("NFKC", str(value or "")).strip()
    compact = _PHONE_LOGIN_SEPARATORS_RE.sub("", raw)
    return compact if re.fullmatch(r"010\d{8}", compact) else raw
