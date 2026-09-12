# ======================================================================
# PATH: apps/api/config/settings/base.py
# ======================================================================
from pathlib import Path
from datetime import timedelta
import os

try:
    from corsheaders.defaults import default_headers
except ImportError:
    default_headers = []

"""
============================================================================
MULTI-TENANT SSOT NOTICE (CRITICAL)

- Tenant resolution is **Host-based only**.
- Headers or query params are intentionally ignored.
- Any fallback / auto-pick / default-tenant logic is a BUG.
- Internal / token endpoints are tenant-free by design.
============================================================================
"""

# ==================================================
# BASE
# ==================================================

BASE_DIR = Path(__file__).resolve().parents[3]

SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key")
DEBUG = os.getenv("DEBUG", "true").lower() == "true"

# ==================================================
# AWS / WORKER INSTANCE (SSOT)
# ==================================================

AWS_REGION = os.getenv("AWS_REGION")
AWS_DEFAULT_REGION = os.getenv("AWS_DEFAULT_REGION", AWS_REGION)

AI_WORKER_INSTANCE_ID = os.getenv("AI_WORKER_INSTANCE_ID")
VIDEO_WORKER_INSTANCE_ID = os.getenv("VIDEO_WORKER_INSTANCE_ID")

# Lambda internal API (B1 backlog-count 등) 인증용
LAMBDA_INTERNAL_API_KEY = os.environ.get("LAMBDA_INTERNAL_API_KEY")
# Internal API 허용 소스 CIDR (쉼표 구분). Lambda VPC(10.1.0.0/16) + API VPC(172.30.0.0/16). 비어 있으면 IP 검사 생략.
INTERNAL_API_ALLOW_IPS = os.environ.get("INTERNAL_API_ALLOW_IPS", "").strip()
# 신뢰 프록시 CIDR (쉼표 구분). 이 범위에서 직접 연결한 peer 의 X-Forwarded-For 만 신뢰.
# 비어 있으면 후방 호환을 위해 XFF 우선 사용 (운영 환경에서는 ALB VPC CIDR 명시 권장).
TRUSTED_PROXY_CIDRS = os.environ.get("TRUSTED_PROXY_CIDRS", "").strip()

# ==================================================
# MULTI TENANT (SSOT – Host Based Only)
# ==================================================

# ❌ Legacy options (kept for backward awareness, intentionally unused)
TENANT_HEADER_NAME = None
TENANT_QUERY_PARAM_NAME = None
TENANT_DEFAULT_CODE = None
TENANT_STRICT = None
TENANT_ALLOW_INACTIVE = None

# Host가 여기 포함될 때만 X-Tenant-Code 헤더로 테넌트 결정 (그 외는 Host → TenantDomain만).
# 로컬 개발: localhost/127.0.0.1 추가 시 로그인과 /core/me/가 동일 테넌트로 동작 (403 방지).
TENANT_HEADER_CODE_ALLOWED_HOSTS = (
    "api.hakwonplus.com",
    "localhost",
    "127.0.0.1",
)

# ==================================================
# INTERNAL / WORKER (TENANT-FREE ZONE) 🔒
# - 아래 경로는 TenantMiddleware 를 반드시 bypass 해야 함.
# - tenant resolve 가 발생하면 운영 사고로 간주한다.
# ==================================================

TENANT_BYPASS_PATH_PREFIXES = [
    "/admin/",
    "/api/v1/token/",
    "/api/v1/token/refresh/",
    "/api-auth/",  # DRF Browsable API 로그인 (tenant 불필요)
    "/internal/",
    "/api/v1/internal/",
    "/swagger",
    "/redoc",
]

# ==================================================
# ALLOWED HOSTS
# - base.py: dev / staging friendly
# - prod.py: must be strict (tenant == host)
# ==================================================

ALLOWED_HOSTS = [
    "127.0.0.1",
    "localhost",
    "hakwonplus.com",
    "www.hakwonplus.com",
    "api.hakwonplus.com",
    "limglish.kr",
    ".limglish.kr",
    "academy-frontend.pages.dev",
    ".trycloudflare.com",
    # 개발용
    "dev-web.hakwonplus.com",
    "dev-api.hakwonplus.com",
]

# ==================================================
# PROXY / FORWARDED HEADERS (ENV CONSISTENCY) ✅
# - dev / staging / prod 동일한 host 해석 보장
# ==================================================

USE_X_FORWARDED_HOST = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

AUTH_USER_MODEL = "core.User"

# ==================================================
# UPLOAD LIMITS
# ==================================================

# PPT generator supports up to 500 images. Django's default multipart file-count
# guard is 100, so keep the framework parser limit aligned with the product cap
# plus a little headroom for adjacent upload fields.
DATA_UPLOAD_MAX_NUMBER_FILES = int(os.getenv("DATA_UPLOAD_MAX_NUMBER_FILES", "600"))

# Large multi-file batches should not keep every file in process memory. Files
# above this threshold are spooled to disk by Django's upload handlers.
FILE_UPLOAD_MAX_MEMORY_SIZE = int(os.getenv("FILE_UPLOAD_MAX_MEMORY_SIZE", str(1024 * 1024)))

# ==================================================
# INSTALLED APPS
# ==================================================

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",

    "apps.api.common",
    "apps.core",

    "apps.domains.students",
    "apps.domains.teachers",
    "apps.domains.staffs",
    "apps.domains.parents",
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
    "apps.domains.inventory",
    "apps.domains.matchup",
    "apps.domains.fees",

    "apps.billing",

    "apps.domains.video",
    "apps.domains.messaging",

    "rest_framework",
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",
    "django_filters",
    "drf_yasg",
    "corsheaders",

    "apps.shared",
    "django_extensions",
    "apps.domains.student_app",
    "apps.domains.teacher_app",
]

# ==================================================
# MIDDLEWARE
# ==================================================

MIDDLEWARE = [
    "apps.api.common.correlation.CorrelationIdMiddleware",
    "apps.api.common.middleware.HealthCheckHostMiddleware",
    "apps.api.common.middleware.SecurityHeadersMiddleware",
    "apps.api.common.middleware.CorsResponseFixMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "apps.api.common.middleware.UnhandledExceptionMiddleware",
    "apps.core.middleware.safe_method_write.SafeMethodDatabaseWriteMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",

    # 🔒 Tenant SSOT (Host-based, after host normalization)
    "apps.core.middleware.tenant.TenantMiddleware",
    "apps.core.middleware.tenant_db_usage.TenantDatabaseUsageMiddleware",

    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.api.common.middleware.SentryContextMiddleware",
]

# ==================================================
# URL / WSGI / ASGI
# ==================================================

ROOT_URLCONF = "apps.api.config.urls"
WSGI_APPLICATION = "apps.api.config.wsgi.application"
ASGI_APPLICATION = "apps.api.config.asgi.application"

# ==================================================
# DATABASE
# ==================================================


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
        # 운영 사고 (2026-04-29 01:00): API 인스턴스 1대가 569 connection 누수.
        # CONN_MAX_AGE=60초 + 사용자 30+ 동시 업로드 트래픽으로 idle conn 풀에서 안 풀림.
        # 60s → 5s로 단축하여 idle conn 빠르게 RDS에 반환. RDS Proxy 도입 전 임시 방어.
        # 환경변수 DB_CONN_MAX_AGE override 가능 (수치 안정 후 다시 60으로 올릴 수 있음).
        "CONN_MAX_AGE": int(os.getenv("DB_CONN_MAX_AGE", "5")),
        "CONN_HEALTH_CHECKS": True,  # V1.1.0: validate connection before reuse (prevents stale-connection errors after RDS recovery)
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

STATIC_URL = "/static/"
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "storage" / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ==================================================
# TEMPLATES
# ==================================================

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [
            os.path.join(BASE_DIR, "domains", "assets", "omr", "renderer", "templates"),
        ],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# ==================================================
# DRF
# ==================================================

REST_FRAMEWORK = {
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "apps.core.authentication.TokenVersionJWTAuthentication",
        "apps.core.authentication.TenantAwareSessionAuthentication",
    ],
    "DEFAULT_PAGINATION_CLASS": "apps.api.common.pagination.AcademyPageNumberPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "60/minute",
        "user": "300/minute",
        "alimtalk_endpoint": "5/hour",
        "messaging_diagnostic": "10/hour",
        "staff_password_reset": "60/hour",
        "signup_check": "30/minute",
    },
}

# ==================================================
# JWT
# ==================================================

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(hours=1),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "AUTH_HEADER_TYPES": ("Bearer",),
}

# ==================================================
# CORS / CSRF
# ==================================================

CORS_ALLOW_ALL_ORIGINS = False
CORS_ALLOW_CREDENTIALS = True

CORS_ALLOWED_ORIGINS = [
    "http://localhost:5173",
    "http://localhost:5174",
    "https://hakwonplus.com",
    "https://www.hakwonplus.com",
    "https://dev.hakwonplus.com",
    "https://academy-frontend.pages.dev",
    "https://limglish.kr",
    "https://www.limglish.kr",
    "https://tchul.com",
    "https://www.tchul.com",
    "https://ymath.co.kr",
    "https://www.ymath.co.kr",
    "https://dev-web.hakwonplus.com",
]

CSRF_TRUSTED_ORIGINS = [
    "https://hakwonplus.com",
    "https://www.hakwonplus.com",
    "https://dev.hakwonplus.com",
    "https://academy-frontend.pages.dev",
    "https://limglish.kr",
    "https://www.limglish.kr",
    "https://tchul.com",
    "https://www.tchul.com",
    "https://ymath.co.kr",
    "https://www.ymath.co.kr",
    "https://*.trycloudflare.com",  # dev only
]

# 중앙 API(api.hakwonplus.com) 호출 시 SPA가 테넌트 식별용으로 보내는 헤더 허용
CORS_ALLOW_HEADERS = list(default_headers) + [
    "X-Client-Version",
    "X-Client",
    "X-Tenant-Code",
    "X-Student-Id",
    "X-Score-Editor-Client",
    "X-Score-Session-Id",
    "X-Expected-Updated-At",
]

# ==================================================
# VIDEO / CDN
# ==================================================

CDN_HLS_BASE_URL = os.getenv(
    "CDN_HLS_BASE_URL",
    "https://cdn.hakwonplus.com",
)
CDN_HLS_SIGNING_SECRET = os.getenv("CDN_HLS_SIGNING_SECRET", "")
CDN_HLS_SIGNING_KEY_ID = os.getenv("CDN_HLS_SIGNING_KEY_ID", "v1")
VIDEO_PLAYBACK_TTL_SECONDS = int(os.getenv("VIDEO_PLAYBACK_TTL_SECONDS", "600"))

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
R2_STORAGE_BUCKET = os.getenv("R2_STORAGE_BUCKET", "academy-storage")
# 엑셀 수강등록 업로드용 (워커와 동일 버킷 사용)
R2_EXCEL_BUCKET = os.getenv("R2_EXCEL_BUCKET", os.getenv("EXCEL_BUCKET_NAME", "academy-excel"))
# dev_app: 테넌트 로고 등 (academy-admin 버킷)
R2_ADMIN_BUCKET = os.getenv("R2_ADMIN_BUCKET", "academy-admin")
R2_ADMIN_PUBLIC_BASE_URL = os.getenv("R2_ADMIN_PUBLIC_BASE_URL", R2_PUBLIC_BASE_URL or "")

# ==================================================
# SITE (메시지 발송용 홈페이지 링크)
# ==================================================

SITE_URL = os.getenv("SITE_URL", "")  # 예: https://academy.example.com

# ==================================================
# SOLAPI (공용 알림톡 발송) — 환경변수 권장, 코드에 키 노출 금지
# ==================================================

SOLAPI_API_KEY = os.getenv("SOLAPI_API_KEY", "")
SOLAPI_API_SECRET = os.getenv("SOLAPI_API_SECRET", "")
SOLAPI_SENDER = os.getenv("SOLAPI_SENDER", "")  # 공용 알림톡 발신 번호 (예: 01012345678)
MESSAGING_PROVIDER_LOW_BALANCE_ALERT_THRESHOLD = int(
    os.getenv("MESSAGING_PROVIDER_LOW_BALANCE_ALERT_THRESHOLD", "10000")
)
# 알림톡: 카카오 검수 완료 템플릿만 ENV로 관리 (코드 수정 없이 교체)
SOLAPI_KAKAO_PF_ID = os.getenv("SOLAPI_KAKAO_PF_ID", "")
SOLAPI_KAKAO_TEMPLATE_ID = os.getenv("SOLAPI_KAKAO_TEMPLATE_ID", "")

# ==================================================
# Web Push (VAPID) — Teacher PWA 푸시 알림
# ==================================================

VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "")
VAPID_CONTACT_EMAIL = os.getenv("VAPID_CONTACT_EMAIL", "devhyun7466@gmail.com")

# 공용 알림톡 owner tenant. SMS/LMS 실발송 허용 용도가 아니다.
OWNER_TENANT_ID = int(os.getenv("OWNER_TENANT_ID", "1"))

# 제품 기능 사용 분석 전용 HMAC 키. Django SECRET_KEY와 분리하며,
# 키가 없으면 기능 플래그가 켜져 있어도 이벤트를 저장하지 않는다.
PRODUCT_ANALYTICS_HASH_KEY = os.getenv("PRODUCT_ANALYTICS_HASH_KEY", "").strip()

# 테넌트별 DB 부하 판단용 저비용 계측. SQL과 parameter는 기록하지 않는다.
# 운영 활성화 전 focused overhead 검증을 거쳐 env에서 명시적으로 켠다.
TENANT_DB_USAGE_ENABLED = os.getenv(
    "TENANT_DB_USAGE_ENABLED",
    "",
).lower() in ("1", "true", "yes")
TENANT_DB_USAGE_SAMPLE_RATE = float(
    os.getenv("TENANT_DB_USAGE_SAMPLE_RATE", "0.1")
)
TENANT_DB_USAGE_SLOW_REQUEST_MS = int(
    os.getenv("TENANT_DB_USAGE_SLOW_REQUEST_MS", "1000")
)

# /dev 운영 콘솔 알림 webhook (Slack incoming webhook URL).
# 비어 있으면 전송 생략 — check_dev_alerts 커맨드는 조건 평가만 수행.
DEV_ALERTS_WEBHOOK_URL = os.getenv("DEV_ALERTS_WEBHOOK_URL", "")
# 운영자 장애 알림은 Slack webhook만 사용한다.
# Runtime 5xx responses must not wait for the incident audit insert.
USER_INCIDENT_AUDIT_ASYNC = True
# 로컬 기능 테스트용 tenant. 이 tenant에서는 알림톡 발송 없이 기능만 동작.
TEST_TENANT_ID = int(os.getenv("TEST_TENANT_ID", "9999"))

# delete_r2 전용 SQS (Encoding = Batch ONLY)
VIDEO_SQS_QUEUE_DELETE_R2 = os.getenv("VIDEO_SQS_QUEUE_DELETE_R2", "academy-video-delete-r2")

# AWS Batch Video Encoding. long path 폐기 (2026-05-10) — 모든 영상이 short queue로.
VIDEO_BATCH_JOB_QUEUE = os.getenv("VIDEO_BATCH_JOB_QUEUE", "academy-v1-video-batch-queue")
VIDEO_BATCH_JOB_DEFINITION = os.getenv("VIDEO_BATCH_JOB_DEFINITION", "academy-v1-video-batch-jobdef")
VIDEO_BATCH_COMPUTE_ENV_NAME = os.getenv("VIDEO_BATCH_COMPUTE_ENV_NAME", "academy-v1-video-batch-ce-200gb")
# EventBridge + Ops JobDef. cron 7종 SSOT: backend/docs/infrastructure/video-cron-jobs.md
VIDEO_RECONCILE_RULE_NAME = os.getenv("VIDEO_RECONCILE_RULE_NAME", "academy-v1-reconcile-video-jobs")
VIDEO_SCAN_STUCK_RULE_NAME = os.getenv("VIDEO_SCAN_STUCK_RULE_NAME", "academy-v1-video-scan-stuck-rate")
VIDEO_ENQUEUE_UPLOADED_RULE_NAME = os.getenv("VIDEO_ENQUEUE_UPLOADED_RULE_NAME", "academy-v1-enqueue-uploaded-videos")
VIDEO_DETECT_STUCK_RULE_NAME = os.getenv("VIDEO_DETECT_STUCK_RULE_NAME", "academy-v1-detect-stuck-videos")
VIDEO_RECOVER_DEAD_RULE_NAME = os.getenv("VIDEO_RECOVER_DEAD_RULE_NAME", "academy-v1-recover-dead-video-jobs")
VIDEO_PURGE_RAW_RULE_NAME = os.getenv("VIDEO_PURGE_RAW_RULE_NAME", "academy-v1-purge-raw-videos")
VIDEO_CLEANUP_ORPHAN_RULE_NAME = os.getenv("VIDEO_CLEANUP_ORPHAN_RULE_NAME", "academy-v1-cleanup-orphan-video-storage")
VIDEO_OPS_JOB_DEF_RECONCILE = os.getenv("VIDEO_OPS_JOB_DEF_RECONCILE", "academy-v1-video-ops-reconcile")
VIDEO_OPS_JOB_DEF_SCANSTUCK = os.getenv("VIDEO_OPS_JOB_DEF_SCANSTUCK", "academy-v1-video-ops-scanstuck")
VIDEO_OPS_JOB_DEF_ENQUEUE_UPLOADED = os.getenv("VIDEO_OPS_JOB_DEF_ENQUEUE_UPLOADED", "academy-v1-video-ops-enqueue-uploaded")
VIDEO_OPS_JOB_DEF_DETECT_STUCK = os.getenv("VIDEO_OPS_JOB_DEF_DETECT_STUCK", "academy-v1-video-ops-detect-stuck")
VIDEO_OPS_JOB_DEF_RECOVER_DEAD = os.getenv("VIDEO_OPS_JOB_DEF_RECOVER_DEAD", "academy-v1-video-ops-recover-dead")
VIDEO_OPS_JOB_DEF_PURGE_RAW = os.getenv("VIDEO_OPS_JOB_DEF_PURGE_RAW", "academy-v1-video-ops-purge-raw")
# cleanup-orphan 은 EventBridge → SSM RunShellScript 패턴이라 Batch jobdef 없음. (cron 7종 중 6종만 Batch 기반.)
VIDEO_OPS_JOB_DEFS = (
    VIDEO_OPS_JOB_DEF_RECONCILE,
    VIDEO_OPS_JOB_DEF_SCANSTUCK,
    VIDEO_OPS_JOB_DEF_ENQUEUE_UPLOADED,
    VIDEO_OPS_JOB_DEF_DETECT_STUCK,
    VIDEO_OPS_JOB_DEF_RECOVER_DEAD,
    VIDEO_OPS_JOB_DEF_PURGE_RAW,
)
# Stuck 판정: heartbeat_age 기반. 일반 20분, 긴 영상(3시간+) 45분.
# 임계값(VIDEO_LONG_DURATION_THRESHOLD_SECONDS)은 stuck threshold 분기에만 사용.
VIDEO_LONG_DURATION_THRESHOLD_SECONDS = int(os.getenv("VIDEO_LONG_DURATION_THRESHOLD_SECONDS", "10800"))
VIDEO_STUCK_HEARTBEAT_STANDARD_MINUTES = int(os.getenv("VIDEO_STUCK_HEARTBEAT_STANDARD_MINUTES", "20"))
VIDEO_STUCK_HEARTBEAT_LONG_MINUTES = int(os.getenv("VIDEO_STUCK_HEARTBEAT_LONG_MINUTES", "45"))
# R2 업로드 multipart + checkpoint (params SSOT)
R2_UPLOAD_PART_SIZE_MB = int(os.getenv("R2_UPLOAD_PART_SIZE_MB", "64"))
R2_UPLOAD_MAX_CONCURRENCY = int(os.getenv("R2_UPLOAD_MAX_CONCURRENCY", "8"))
R2_UPLOAD_MAX_ATTEMPTS = int(os.getenv("R2_UPLOAD_MAX_ATTEMPTS", "8"))
R2_UPLOAD_CHECKPOINT_TABLE = os.getenv("R2_UPLOAD_CHECKPOINT_TABLE", "academy-v1-video-upload-checkpoints")
# DynamoDB video job lock: 1 video 1 job, key=video_id, TTL 12h+, heartbeat로 연장
VIDEO_JOB_LOCK_TABLE_NAME = os.getenv("VIDEO_JOB_LOCK_TABLE_NAME", "academy-v1-video-job-lock")
VIDEO_JOB_LOCK_TTL_SECONDS = int(os.getenv("VIDEO_JOB_LOCK_TTL_SECONDS", "43200"))  # 12h (long 기준)
VIDEO_JOB_LOCK_TTL_ATTRIBUTE = os.getenv("VIDEO_JOB_LOCK_TTL_ATTRIBUTE", "ttl")
# Job timeout (참고용; 실제 timeout은 Batch JobDef.timeout=6h에 반영). long path 폐기로 LONG 상수 제거.
VIDEO_JOB_TIMEOUT_STANDARD_SECONDS = int(os.getenv("VIDEO_JOB_TIMEOUT_STANDARD_SECONDS", "7200"))
# Reconcile orphan safety: min RUNNABLE age (minutes) before terminating; skip if CE desiredvCpus=0
RECONCILE_ORPHAN_MIN_RUNNABLE_MINUTES = int(os.getenv("RECONCILE_ORPHAN_MIN_RUNNABLE_MINUTES", "15"))
RECONCILE_ORPHAN_DISABLED = os.getenv("RECONCILE_ORPHAN_DISABLED", "").lower() in ("1", "true", "yes")
# Daemon mode 폐기됨 (2026-05-10). 모든 영상은 AWS Batch로 처리.
# 옛 VIDEO_WORKER_MODE / DAEMON_MAX_DURATION_SECONDS 설정은 무시된다.
VIDEO_TENANT_MAX_CONCURRENT = int(os.getenv("VIDEO_TENANT_MAX_CONCURRENT", "9999"))
VIDEO_GLOBAL_MAX_CONCURRENT = int(os.getenv("VIDEO_GLOBAL_MAX_CONCURRENT", "9999"))
VIDEO_MAX_JOBS_PER_VIDEO = int(os.getenv("VIDEO_MAX_JOBS_PER_VIDEO", "10"))
VIDEO_CLOUDWATCH_NAMESPACE = os.getenv("VIDEO_CLOUDWATCH_NAMESPACE", "Academy/Video")
AI_SQS_QUEUE_NAME_LITE = os.getenv("AI_SQS_QUEUE_NAME_LITE", "academy-v1-ai-queue")
AI_SQS_QUEUE_NAME_BASIC = os.getenv("AI_SQS_QUEUE_NAME_BASIC", "academy-v1-ai-queue")
AI_SQS_QUEUE_NAME_PREMIUM = os.getenv("AI_SQS_QUEUE_NAME_PREMIUM", "academy-v1-ai-queue")
TOOLS_SQS_QUEUE_NAME = os.getenv("TOOLS_SQS_QUEUE_NAME", "academy-v1-tools-queue")
# 메시지 발송 SQS 큐 (워커가 소비). SSOT: academy-v1-messaging-queue
MESSAGING_SQS_QUEUE_NAME = os.getenv("MESSAGING_SQS_QUEUE_NAME", "academy-v1-messaging-queue")

# Reader-first rollout gate. Keep midnight time-range writes disabled until every
# API/worker instance runs code that interprets 00:00 as the next-day boundary.
CLINIC_MIDNIGHT_TIME_RANGE_WRITES_ENABLED = os.getenv(
    "CLINIC_MIDNIGHT_TIME_RANGE_WRITES_ENABLED",
    "",
).lower() in ("1", "true", "yes")

# ==================================================
# BILLING / SUBSCRIPTION
# ==================================================

# 결제/청구에서 제외할 테넌트 ID (개발/테스트/시스템).
# 이 테넌트들은 자동결제, 인보이스 생성, 만료 처리 대상에서 항상 제외된다.
BILLING_EXEMPT_TENANT_IDS: set[int] = {
    int(x) for x in os.getenv("BILLING_EXEMPT_TENANT_IDS", "1,2,9999").split(",") if x.strip()
}
# Test settings may disable only the request middleware check so unrelated API
# tests do not depend on database-generated tenant IDs. Production stays closed.
BILLING_TEST_BYPASS_SUBSCRIPTION = False

# Toss Payments (PG)
TOSS_PAYMENTS_SECRET_KEY = os.getenv("TOSS_PAYMENTS_SECRET_KEY", "")
TOSS_PAYMENTS_CLIENT_KEY = os.getenv("TOSS_PAYMENTS_CLIENT_KEY", "")
# 자동결제 승인 API 사용 가능 여부 (추가 계약 필요, 계약 전이면 False)
TOSS_AUTO_BILLING_ENABLED = os.getenv("TOSS_AUTO_BILLING_ENABLED", "").lower() in ("1", "true", "yes")
BILLING_BANK_TRANSFER_ENABLED = os.getenv(
    "BILLING_BANK_TRANSFER_ENABLED", ""
).lower() in ("1", "true", "yes")
BILLING_BANK_NAME = os.getenv("BILLING_BANK_NAME", "").strip()
BILLING_BANK_ACCOUNT_NUMBER = os.getenv(
    "BILLING_BANK_ACCOUNT_NUMBER", ""
).strip()
BILLING_BANK_ACCOUNT_HOLDER = os.getenv(
    "BILLING_BANK_ACCOUNT_HOLDER", ""
).strip()
BILLING_KEY_ENCRYPTION_WRITE_ENABLED = os.getenv(
    "BILLING_KEY_ENCRYPTION_WRITE_ENABLED", ""
).lower() in ("1", "true", "yes")
# Billing credentials use a dedicated Fernet KEK, not Django SECRET_KEY. This
# keeps application signing-key rotation independent from payment data and
# permits a bidirectionally compatible two-refresh KEK rotation.
BILLING_KEY_ENCRYPTION_PRIMARY_KEY = os.getenv(
    "BILLING_KEY_ENCRYPTION_PRIMARY_KEY", ""
).strip()
BILLING_KEY_ENCRYPTION_FALLBACK_KEYS = tuple(
    key.strip()
    for key in os.getenv("BILLING_KEY_ENCRYPTION_FALLBACK_KEYS", "").split(",")
    if key.strip()
)

# 유예 기간 (일) — 결제 실패 후 서비스 유지 기간
BILLING_GRACE_PERIOD_DAYS = int(os.getenv("BILLING_GRACE_PERIOD_DAYS", "7"))
# 결제 재시도 정책
BILLING_RETRY_MAX_ATTEMPTS = int(os.getenv("BILLING_RETRY_MAX_ATTEMPTS", "3"))
BILLING_RETRY_INTERVAL_DAYS = int(os.getenv("BILLING_RETRY_INTERVAL_DAYS", "3"))

# AI 호출 quota — 두 단계 분리.
#   1) tracking (default ON): tenant×kind 호출수 누적. ai_usage 테이블 기반. 한도 무관.
#   2) enforcement (default OFF): 누적치가 DEFAULT_LIMITS 초과 시 AIQuotaExceeded raise.
# 운영 절차: tracking으로 실측 → 가격정책 결정 → enforcement 토글로 brake 활성화.
# 한도/단가: apps.domains.ai.services.quota.DEFAULT_LIMITS 참고.
AI_USAGE_TRACKING_ENABLED = os.getenv("AI_USAGE_TRACKING_ENABLED", "1").lower() in ("1", "true", "yes")
AI_QUOTA_ENFORCEMENT_ENABLED = os.getenv("AI_QUOTA_ENFORCEMENT_ENABLED", "").lower() in ("1", "true", "yes")

# ==================================================
# INTERNAL WORKER
# ==================================================

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
MESSAGING_PROVIDER_DAILY_DISPATCH_LIMIT = max(
    1,
    int(os.getenv("MESSAGING_PROVIDER_DAILY_DISPATCH_LIMIT", "900")),
)

# ==================================================
# SENTRY (에러 모니터링) — DSN 미설정 시 비활성화
# ==================================================

SENTRY_DSN = os.getenv("SENTRY_DSN", "")
if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration

    def _sentry_before_send(event, hint):
        """노이즈 필터: health check, DisallowedHost, SuspiciousOperation"""
        exc = hint.get("exc_info")
        if exc:
            exc_type = exc[0]
            if exc_type and exc_type.__name__ in ("DisallowedHost", "SuspiciousOperation"):
                return None
        return event

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        environment=os.getenv("SENTRY_ENVIRONMENT", "production"),
        release=os.getenv("SENTRY_RELEASE", f"academy-backend@{os.getenv('GIT_SHA', 'unknown')}"),
        integrations=[
            DjangoIntegration(transaction_style="url"),
            LoggingIntegration(level=None, event_level="ERROR"),
        ],
        traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.05")),
        send_default_pii=False,
        before_send=_sentry_before_send,
    )
