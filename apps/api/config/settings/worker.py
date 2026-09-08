# ======================================================================
# PATH: apps/api/config/settings/worker.py
# ======================================================================
"""
Worker 전용 Django 설정 (API 의존성 없음)

절대 포함하지 않음:
  - corsheaders
  - rest_framework
  - django_extensions
  - admin (contrib.admin)
  - staticfiles
  - sessions (SessionMiddleware)
  - messages
  - TEMPLATES (템플릿 엔진)
"""
from pathlib import Path
import os

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parents[3]

SECRET_KEY = os.getenv("SECRET_KEY", "worker-dev-secret")
DEBUG = False

# ==================================================
# URL / WSGI / ASGI — Worker는 HTTP 서버 아님
# ==================================================
ROOT_URLCONF = None
WSGI_APPLICATION = None
ASGI_APPLICATION = None

# ==================================================
# INSTALLED_APPS — 최소 (ORM만, API/Admin 제외)
# ==================================================
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "apps.core",  # api.common보다 먼저 (api.common가 core.models.base 의존)
    "apps.api.common",
    "apps.domains.students",
    "apps.domains.teachers",
    "apps.domains.parents",
    "apps.domains.staffs",
    "apps.domains.lectures",
    "apps.domains.enrollment",
    "apps.domains.attendance",
    "apps.domains.schedule",
    "apps.domains.community",
    "apps.domains.landing_public",
    "apps.domains.exams",
    "apps.domains.homework",
    "apps.domains.submissions",
    "apps.domains.results",
    "apps.domains.homework_results",
    "apps.domains.clinic",
    "apps.domains.progress",
    "apps.domains.ai.apps.AIDomainConfig",
    "apps.domains.assets",
    "apps.domains.tools.problem_studio.apps.ProblemStudioConfig",
    "apps.domains.inventory",  # MatchupDocument.inventory_file FK NOT NULL (Storage-Matchup 통합)
    "apps.domains.matchup",  # AI 콜백이 MatchupDocument/MatchupProblem 모델 사용
    "apps.domains.student_app",
    "apps.domains.video",
    "apps.domains.messaging",
    "apps.shared",
]

# ==================================================
# MIDDLEWARE — 최소 (Security만)
# ==================================================
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
]

# ==================================================
# DATABASE
# ==================================================
AUTH_USER_MODEL = "core.User"


def _database_options():
    options = {"connect_timeout": 10}
    sslmode = os.getenv("DB_SSL_MODE", "").strip()
    if sslmode:
        options["sslmode"] = sslmode
    return options


DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("DB_NAME"),
        "USER": os.getenv("DB_USER"),
        "PASSWORD": os.getenv("DB_PASSWORD"),
        "HOST": os.getenv("DB_HOST"),
        "PORT": os.getenv("DB_PORT", "5432"),
        # 워커는 작업 중심 (긴 처리 시간) → connection 재사용 가치 낮음 + 풀 고갈 위험 큼.
        # 운영 사고 (2026-04-29 00:21): AI 워커 3대 + API 동시 RDS 연결로 max_conn(~280) 도달.
        # 워커 CONN_MAX_AGE=0으로 매 작업 후 connection 즉시 닫아 풀 고갈 방어.
        # API 쪽은 60s 유지 (요청 reuse 가치 큼).
        "CONN_MAX_AGE": int(os.getenv("DB_CONN_MAX_AGE_WORKER", os.getenv("DB_CONN_MAX_AGE", "0"))),
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": _database_options(),
    }
}

# ==================================================
# GLOBAL
# ==================================================
LANGUAGE_CODE = "ko-kr"
TIME_ZONE = "Asia/Seoul"
USE_I18N = True
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ==================================================
# AWS / SQS
# ==================================================
AWS_REGION = os.getenv("AWS_REGION")
AWS_DEFAULT_REGION = os.getenv("AWS_DEFAULT_REGION", AWS_REGION)

# VIDEO_SQS_QUEUE_NAME removed - Encoding = Batch only. delete_r2 uses VIDEO_SQS_QUEUE_DELETE_R2.
# AI/Messaging SQS: SSOT academy-v1-ai-queue, academy-v1-messaging-queue
AI_SQS_QUEUE_NAME_LITE = os.getenv("AI_SQS_QUEUE_NAME_LITE", "academy-v1-ai-queue")
AI_SQS_QUEUE_NAME_BASIC = os.getenv("AI_SQS_QUEUE_NAME_BASIC", "academy-v1-ai-queue")
AI_SQS_QUEUE_NAME_PREMIUM = os.getenv("AI_SQS_QUEUE_NAME_PREMIUM", "academy-v1-ai-queue")
TOOLS_SQS_QUEUE_NAME = os.getenv("TOOLS_SQS_QUEUE_NAME", "academy-v1-tools-queue")
MESSAGING_SQS_QUEUE_NAME = os.getenv("MESSAGING_SQS_QUEUE_NAME", "academy-v1-messaging-queue")

# ==================================================
# Cloudflare R2
# ==================================================
R2_ACCESS_KEY = os.getenv("R2_ACCESS_KEY")
R2_SECRET_KEY = os.getenv("R2_SECRET_KEY")
R2_ENDPOINT = os.getenv("R2_ENDPOINT")
R2_REGION = os.getenv("R2_REGION", "auto")
R2_PUBLIC_BASE_URL = os.getenv("R2_PUBLIC_BASE_URL")
R2_AI_BUCKET = os.getenv("R2_AI_BUCKET", "academy-ai")
R2_VIDEO_BUCKET = os.getenv("R2_VIDEO_BUCKET", "academy-video")
R2_EXCEL_BUCKET = os.getenv("R2_EXCEL_BUCKET", os.getenv("EXCEL_BUCKET_NAME", "academy-excel"))
R2_STORAGE_BUCKET = os.getenv("R2_STORAGE_BUCKET", "academy-storage")
R2_ADMIN_BUCKET = os.getenv("R2_ADMIN_BUCKET", "academy-admin")
R2_ADMIN_PUBLIC_BASE_URL = os.getenv("R2_ADMIN_PUBLIC_BASE_URL", "")

# ==================================================
# Video Batch (인코딩 제출용 — enqueue_uploaded 등 ops job이 사용)
# ==================================================
VIDEO_BATCH_JOB_QUEUE = os.getenv("VIDEO_BATCH_JOB_QUEUE", "academy-v1-video-batch-queue")
VIDEO_BATCH_JOB_DEFINITION = os.getenv("VIDEO_BATCH_JOB_DEFINITION", "academy-v1-video-batch-jobdef")
VIDEO_BATCH_COMPUTE_ENV_NAME = os.getenv("VIDEO_BATCH_COMPUTE_ENV_NAME", "academy-v1-video-batch-ce-200gb")
if os.getenv("ACADEMY_RUNTIME_ENV", "").strip().lower() == "development" and (
    VIDEO_BATCH_JOB_QUEUE or VIDEO_BATCH_JOB_DEFINITION
):
    raise ImproperlyConfigured(
        "Development video workers must not resolve production Batch resources."
    )
VIDEO_TENANT_MAX_CONCURRENT = int(os.getenv("VIDEO_TENANT_MAX_CONCURRENT", "9999"))
VIDEO_GLOBAL_MAX_CONCURRENT = int(os.getenv("VIDEO_GLOBAL_MAX_CONCURRENT", "9999"))
VIDEO_JOB_LOCK_TABLE_NAME = os.getenv("VIDEO_JOB_LOCK_TABLE_NAME", "academy-v1-video-job-lock")
VIDEO_JOB_LOCK_TTL_SECONDS = int(os.getenv("VIDEO_JOB_LOCK_TTL_SECONDS", "43200"))

# ==================================================
# Video / CDN
# ==================================================
CDN_HLS_BASE_URL = os.getenv("CDN_HLS_BASE_URL", "https://pub-54ae4dcb984d4491b08f6c57023a1621.r2.dev")
CDN_HLS_SIGNING_SECRET = os.getenv("CDN_HLS_SIGNING_SECRET", "")
CDN_HLS_SIGNING_KEY_ID = os.getenv("CDN_HLS_SIGNING_KEY_ID", "v1")
if os.getenv("ACADEMY_RUNTIME_ENV", "").strip().lower() == "development":
    if CDN_HLS_BASE_URL.rstrip("/") != "https://cdn.hakwonplus.com":
        raise ImproperlyConfigured(
            "Development worker video playback must use the canonical protected CDN URL."
        )
    if len(CDN_HLS_SIGNING_SECRET.strip()) < 32:
        raise ImproperlyConfigured(
            "Development worker video playback requires an isolated signing secret."
        )
    if CDN_HLS_SIGNING_KEY_ID != "v1":
        raise ImproperlyConfigured(
            "Development worker video playback requires the active v1 signing key ID."
        )

# ==================================================
# SOLAPI (Messaging Worker)
# ==================================================
SOLAPI_API_KEY = os.getenv("SOLAPI_API_KEY", "")
SOLAPI_API_SECRET = os.getenv("SOLAPI_API_SECRET", "")
SOLAPI_SENDER = os.getenv("SOLAPI_SENDER", "")
SOLAPI_KAKAO_PF_ID = os.getenv("SOLAPI_KAKAO_PF_ID", "")
SOLAPI_KAKAO_TEMPLATE_ID = os.getenv("SOLAPI_KAKAO_TEMPLATE_ID", "")

# ==================================================
# Worker → API 통신
# ==================================================
API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
INTERNAL_WORKER_TOKEN = os.getenv("INTERNAL_WORKER_TOKEN", "")
MESSAGING_TENANT_BINDING_KEY = os.getenv("MESSAGING_TENANT_BINDING_KEY", "").strip()
MESSAGING_TENANT_BINDING_FALLBACK_KEYS = tuple(
    key.strip()
    for key in os.getenv("MESSAGING_TENANT_BINDING_FALLBACK_KEYS", "").split(",")
    if key.strip()
)
MESSAGING_TENANT_BINDING_ENFORCED = os.getenv(
    "MESSAGING_TENANT_BINDING_ENFORCED",
    "true",
).lower() in ("1", "true", "yes")

# ==================================================
# ALLOWED_HOSTS (Django runserver용, Worker는 미사용)
# ==================================================
ALLOWED_HOSTS = ["*"]

# ==================================================
# Sentry (Worker 에러 모니터링)
# ==================================================
SENTRY_DSN = os.getenv("SENTRY_DSN", "")
if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        environment=os.getenv("SENTRY_ENVIRONMENT", "production"),
        release=os.getenv("SENTRY_RELEASE", f"academy-backend@{os.getenv('GIT_SHA', 'unknown')}"),
        integrations=[
            DjangoIntegration(transaction_style="url"),
            LoggingIntegration(level=None, event_level="ERROR"),
        ],
        traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.01")),
        send_default_pii=False,
    )
