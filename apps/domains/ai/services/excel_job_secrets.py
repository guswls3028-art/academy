from __future__ import annotations

import base64
import hashlib
import json
from datetime import timedelta
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

EXCEL_SECRET_PREFIX = "excel:v1:"
EXCEL_INITIAL_PASSWORD_SECRET_FIELD = "initial_password_secret"
EXCEL_CREDENTIALS_ENVELOPE_FIELD = "_student_initial_credentials"
EXCEL_CREDENTIALS_TTL_SECONDS = 60 * 60


class ExcelJobSecretError(ValueError):
    """Excel import secret cannot be protected or recovered."""


def _fernet() -> Fernet:
    secret_key = str(getattr(settings, "SECRET_KEY", "") or "")
    if not secret_key:
        raise ExcelJobSecretError("excel_job_secret_key_missing")
    digest = hashlib.sha256(
        f"academy:excel-job-secrets:v1:{secret_key}".encode("utf-8")
    ).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_excel_job_secret(value: str) -> str:
    plaintext = str(value or "")
    if not plaintext:
        return ""
    if plaintext.startswith(EXCEL_SECRET_PREFIX):
        return plaintext
    token = _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")
    return f"{EXCEL_SECRET_PREFIX}{token}"


def decrypt_excel_job_secret(value: str) -> str:
    stored = str(value or "")
    if not stored:
        return ""
    if not stored.startswith(EXCEL_SECRET_PREFIX):
        raise ExcelJobSecretError("excel_job_secret_format_invalid")
    token = stored[len(EXCEL_SECRET_PREFIX):].encode("ascii")
    try:
        return _fernet().decrypt(token).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, UnicodeEncodeError) as exc:
        raise ExcelJobSecretError("excel_job_secret_decryption_failed") from exc


def protect_excel_initial_password(initial_password: str) -> dict[str, str]:
    password = str(initial_password or "").strip()
    if not password:
        return {}
    return {
        EXCEL_INITIAL_PASSWORD_SECRET_FIELD: encrypt_excel_job_secret(password),
    }


def recover_excel_initial_password(payload: dict[str, Any]) -> str:
    encrypted = payload.get(EXCEL_INITIAL_PASSWORD_SECRET_FIELD)
    if not encrypted:
        return ""
    return decrypt_excel_job_secret(str(encrypted)).strip()


def secure_excel_result(
    result_payload: dict[str, Any],
    *,
    now=None,
) -> dict[str, Any]:
    persistent = dict(result_payload)
    credentials = persistent.pop("credentials", None)
    if not isinstance(credentials, list) or not credentials:
        return persistent

    serialized = json.dumps(
        credentials,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    secured_at = now or timezone.now()
    persistent[EXCEL_CREDENTIALS_ENVELOPE_FIELD] = {
        "ciphertext": encrypt_excel_job_secret(serialized),
        "expires_at": (
            secured_at + timedelta(seconds=EXCEL_CREDENTIALS_TTL_SECONDS)
        ).isoformat(),
    }
    return persistent


def public_excel_result(
    stored_payload: dict[str, Any],
    *,
    include_credentials: bool = False,
    now=None,
) -> dict[str, Any]:
    public = dict(stored_payload)
    # A rolling/alternate writer must never bypass the encrypted envelope expiry.
    public.pop("credentials", None)
    envelope = public.pop(EXCEL_CREDENTIALS_ENVELOPE_FIELD, None)
    if not include_credentials or not isinstance(envelope, dict):
        return public

    expires_at = parse_datetime(str(envelope.get("expires_at") or ""))
    current = now or timezone.now()
    if expires_at is None or expires_at <= current:
        return public

    ciphertext = str(envelope.get("ciphertext") or "")
    if not ciphertext:
        return public
    try:
        credentials = json.loads(decrypt_excel_job_secret(ciphertext))
    except (ExcelJobSecretError, json.JSONDecodeError):
        return public
    if isinstance(credentials, list):
        public["credentials"] = credentials
    return public


def scrub_excel_job_payload(payload: dict[str, Any]) -> dict[str, Any]:
    scrubbed = dict(payload or {})
    scrubbed.pop("initial_password", None)
    scrubbed.pop(EXCEL_INITIAL_PASSWORD_SECRET_FIELD, None)
    return scrubbed
