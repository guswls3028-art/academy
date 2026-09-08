"""Persistent, production-shaped development API settings.

This module imports the production safety defaults, then opens only the local
SSM-tunnel ergonomics needed for review. External write targets must use the
dedicated development database, queues, and Cloudflare R2 bucket.
"""

import os

from .base import *  # noqa: F403

from django.core.exceptions import ImproperlyConfigured


if os.getenv("ACADEMY_RUNTIME_ENV", "").strip().lower() != "development":
    raise ImproperlyConfigured(
        "development.py requires ACADEMY_RUNTIME_ENV=development."
    )

DEBUG = False
API_BASE_URL = "http://127.0.0.1:8000"
SECURE_PROXY_SSL_HEADER = None
USE_X_FORWARDED_HOST = False
ALLOWED_HOSTS = [
    "localhost",
    "127.0.0.1",
    ".ap-northeast-2.compute.internal",
    *[f"172.30.{a}.{b}" for a in range(4) for b in range(256)],
]
CORS_ALLOWED_ORIGIN_REGEXES = []
CORS_ALLOWED_ORIGINS = [
    "http://localhost:4173",
    "http://localhost:5173",
    "http://localhost:5174",
]
CSRF_TRUSTED_ORIGINS = list(CORS_ALLOWED_ORIGINS)
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
SECURE_SSL_REDIRECT = False
SECURE_HSTS_SECONDS = 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# SSM port forwarding has no tenant hostname. The tenant is still mandatory:
# development requests must supply an explicit X-Tenant-Code and never fall
# back to a default or cross-tenant lookup.
TENANT_HEADER_NAME = "X-Tenant-Code"
TENANT_QUERY_PARAM_NAME = None
TENANT_DEFAULT_CODE = None
TENANT_STRICT = True
TENANT_ALLOW_INACTIVE = False

_required_queue_prefix = "academy-v1-development-"
_required_bucket_prefix = "academy-development-"
_database_name = os.getenv("DB_NAME", "")
_database_user = os.getenv("DB_USER", "")
_queue_names = (
    os.getenv("AI_SQS_QUEUE_NAME_LITE", ""),
    os.getenv("AI_SQS_QUEUE_NAME_BASIC", ""),
    os.getenv("AI_SQS_QUEUE_NAME_PREMIUM", ""),
    os.getenv("TOOLS_SQS_QUEUE_NAME", ""),
    os.getenv("MESSAGING_SQS_QUEUE_NAME", ""),
)
_bucket_names = (
    os.getenv("R2_AI_BUCKET", ""),
    os.getenv("R2_VIDEO_BUCKET", ""),
    os.getenv("R2_STORAGE_BUCKET", ""),
    os.getenv("R2_EXCEL_BUCKET", ""),
    os.getenv("R2_ADMIN_BUCKET", ""),
)
if not _database_name.startswith("academy_api_development"):
    raise ImproperlyConfigured("Development API must use the development database.")
if not _database_user.startswith("academy_api_development"):
    raise ImproperlyConfigured("Development API must use the development database role.")
if any(not name.startswith(_required_queue_prefix) for name in _queue_names):
    raise ImproperlyConfigured("Development API queues must use the development prefix.")
if any(not name.startswith(_required_bucket_prefix) for name in _bucket_names):
    raise ImproperlyConfigured("Development API buckets must use the development prefix.")
if VIDEO_BATCH_JOB_QUEUE or VIDEO_BATCH_JOB_DEFINITION:  # noqa: F405
    raise ImproperlyConfigured(
        "Development API video submission stays disabled until dedicated "
        "development Batch resources are configured."
    )
if not os.getenv("R2_ENDPOINT", "").startswith("https://") or not os.getenv(
    "R2_ENDPOINT", ""
).endswith(".r2.cloudflarestorage.com"):
    raise ImproperlyConfigured(
        "Development object storage must use the Cloudflare R2 endpoint."
    )
if os.getenv("R2_REGION", "") != "auto":
    raise ImproperlyConfigured("Development object storage must use R2 region auto.")
if not os.getenv("R2_ACCESS_KEY") or not os.getenv("R2_SECRET_KEY"):
    raise ImproperlyConfigured("Development R2 credentials must be dedicated and explicit.")
if CDN_HLS_BASE_URL.rstrip("/") != "https://cdn.hakwonplus.com":  # noqa: F405
    raise ImproperlyConfigured(
        "Development video playback must use the canonical protected CDN URL."
    )
if len((CDN_HLS_SIGNING_SECRET or "").strip()) < 32:  # noqa: F405
    raise ImproperlyConfigured(
        "Development video playback requires an isolated signing secret."
    )
if CDN_HLS_SIGNING_KEY_ID != "v1":  # noqa: F405
    raise ImproperlyConfigured(
        "Development video playback requires the active v1 signing key ID."
    )
if os.getenv("SOLAPI_MOCK", "").strip().lower() not in {"1", "true", "yes"}:
    raise ImproperlyConfigured("Development messaging must be mock-only.")
if os.getenv("TOSS_AUTO_BILLING_ENABLED", "").strip().lower() in {
    "1",
    "true",
    "yes",
}:
    raise ImproperlyConfigured("Development automatic billing must be disabled.")
