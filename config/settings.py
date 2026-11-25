import os
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# Provider credentials are server-only. The optional file is useful for local
# development; deployment injects the key via the private backend environment.
FIREWORKS_API_KEY = os.environ.get("FIREWORKS_API_KEY", "")
if not FIREWORKS_API_KEY and os.environ.get("FIREWORKS_API_KEY_FILE"):
    FIREWORKS_API_KEY = Path(os.environ["FIREWORKS_API_KEY_FILE"]).read_text().strip()
FIREWORKS_MODEL = os.environ.get("FIREWORKS_MODEL", "accounts/fireworks/models/minimax-m3p1-flash")
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
if not ELEVENLABS_API_KEY and os.environ.get("ELEVENLABS_API_KEY_FILE"):
    ELEVENLABS_API_KEY = Path(os.environ["ELEVENLABS_API_KEY_FILE"]).read_text().strip()
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "")
ELEVENLABS_STT_MODEL = os.environ.get("ELEVENLABS_STT_MODEL", "scribe_v3")
ELEVENLABS_TTS_MODEL = os.environ.get("ELEVENLABS_TTS_MODEL", "eleven_v4_turbo")
VOICE_PUBLIC_ORIGIN = os.environ.get("VOICE_PUBLIC_ORIGIN", "https://atm.aaron-henriquez.com")


def positive_env_int(name, default):
    try:
        value = int(os.environ.get(name, default))
    except ValueError:
        raise ImproperlyConfigured(f"{name} must be a positive integer") from None
    if value < 1:
        raise ImproperlyConfigured(f"{name} must be a positive integer")
    return value


PM_MAX_MESSAGES = positive_env_int("PM_MAX_MESSAGES", 1000)
PM_MAX_HISTORY_BYTES = positive_env_int("PM_MAX_HISTORY_BYTES", 1200000)
PM_MAX_OUTPUT_TOKENS = positive_env_int("PM_MAX_OUTPUT_TOKENS", 8192)
# Application budget, not a claim about an arbitrary configured provider/model.
PM_CONTEXT_WINDOW_TOKENS = positive_env_int("PM_CONTEXT_WINDOW_TOKENS", 524288)
if PM_MAX_OUTPUT_TOKENS >= PM_CONTEXT_WINDOW_TOKENS:
    raise ImproperlyConfigured("PM_MAX_OUTPUT_TOKENS must be less than PM_CONTEXT_WINDOW_TOKENS")

# Push-to-talk has no client-side cutoff. This is a generous abuse ceiling only:
# 600 s is far beyond any normal hold, and the byte ceiling defaults to the same
# 10 minutes of mono 24 kHz PCM16 audio (48,000 bytes per second) plus a header.
VOICE_MAX_CLIP_SECONDS = positive_env_int("VOICE_MAX_CLIP_SECONDS", 600)
VOICE_MAX_CLIP_BYTES = positive_env_int(
    "VOICE_MAX_CLIP_BYTES", VOICE_MAX_CLIP_SECONDS * 24000 * 2 + 1024
)

# Multi-AI chat rooms (GH #149). A provider participant may only select a model
# from this allowlist, which is what lets two participants in one room run
# different models while the provider origin itself stays allowlisted in
# voice_runtime.provider_request.
CHAT_ROOM_MODELS = tuple(
    name.strip()
    for name in os.environ.get("CHAT_ROOM_MODELS", FIREWORKS_MODEL).split(",")
    if name.strip()
)
CHAT_ROOM_MAX_PARTICIPANTS = positive_env_int("CHAT_ROOM_MAX_PARTICIPANTS", 6)
CHAT_ROOM_MAX_ROUNDS = positive_env_int("CHAT_ROOM_MAX_ROUNDS", 3)
# Voice has no tool-round cap; a room multiplies every turn by its participants.
CHAT_ROOM_MAX_TOOL_ROUNDS = positive_env_int("CHAT_ROOM_MAX_TOOL_ROUNDS", 4)
# Prompt window only. Nothing is ever pruned from the stored transcript.
CHAT_ROOM_CONTEXT_MESSAGES = positive_env_int("CHAT_ROOM_CONTEXT_MESSAGES", 60)
CHAT_ROOM_CONTEXT_BYTES = positive_env_int("CHAT_ROOM_CONTEXT_BYTES", 400000)
CHAT_ROOM_MAX_OUTPUT_TOKENS = positive_env_int("CHAT_ROOM_MAX_OUTPUT_TOKENS", 2048)
CHAT_ROOM_MAX_MESSAGES = positive_env_int("CHAT_ROOM_MAX_MESSAGES", 2000)
CHAT_ROOM_MAX_HISTORY_BYTES = positive_env_int("CHAT_ROOM_MAX_HISTORY_BYTES", 4000000)
CHAT_ROOM_MAX_OPEN_ROOMS_PER_USER = positive_env_int("CHAT_ROOM_MAX_OPEN_ROOMS_PER_USER", 10)
CHAT_ROOM_TURN_LEASE_SECONDS = positive_env_int("CHAT_ROOM_TURN_LEASE_SECONDS", 300)
CHAT_ROOM_PAGE_LIMIT = positive_env_int("CHAT_ROOM_PAGE_LIMIT", 50)
CHAT_ROOM_LIVE_TTL = positive_env_int("CHAT_ROOM_LIVE_TTL", 15)
if CHAT_ROOM_MAX_OUTPUT_TOKENS >= PM_CONTEXT_WINDOW_TOKENS:
    raise ImproperlyConfigured(
        "CHAT_ROOM_MAX_OUTPUT_TOKENS must be less than PM_CONTEXT_WINDOW_TOKENS"
    )
if FIREWORKS_API_KEY and not CHAT_ROOM_MODELS:
    raise ImproperlyConfigured("CHAT_ROOM_MODELS must list at least one model id")


def env_bool(name, default=False):
    return os.environ.get(name, str(default)).lower() in {"1", "true", "yes"}


DEBUG = env_bool("DJANGO_DEBUG", True)
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured("Set DJANGO_SECRET_KEY when DJANGO_DEBUG=false")
    SECRET_KEY = "development-only-agent-coordinator-secret-key-not-for-production"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]").split(",")
CSRF_TRUSTED_ORIGINS = [
    value for value in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if value
]
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "drf_spectacular",
    "channels",
    "coordinator.apps.CoordinatorConfig",
    "accounts.apps.AccountsConfig",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"
REDIS_URL = os.environ.get("REDIS_URL", "")
CHANNEL_LAYERS = {
    "default": (
        {"BACKEND": "channels_redis.core.RedisChannelLayer", "CONFIG": {"hosts": [REDIS_URL]}}
        if REDIS_URL
        else {"BACKEND": "channels.layers.InMemoryChannelLayer"}
    )
}
# Live observations are ephemeral overlays, never the durable command authority.
# Redis shares them across ASGI workers; local memory is for one-process development.
CACHES = {
    "default": (
        {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
            "KEY_PREFIX": "coordinator-live",
        }
        if REDIS_URL
        else {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "coordinator-live",
            "OPTIONS": {"MAX_ENTRIES": 600},
        }
    )
}
COORDINATOR_DEMO = env_bool("COORDINATOR_DEMO")


def env_int(name, default):
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ImproperlyConfigured(f"{name} must be an integer") from exc


# OpenCode worker integration (#8 contract). Freshness of the last authenticated
# sync decides bridge connectivity, not Agent.last_seen or task heartbeats.
WORKER_OFFLINE_SECONDS = env_int("WORKER_OFFLINE_SECONDS", 10)
# Maximum commands a worker may keep in the queue for future dispatch.
WORKER_COMMAND_QUEUE_CAP = env_int("WORKER_COMMAND_QUEUE_CAP", 100)

# Bound for POST /api/worker/sync bodies; oversized envelopes are rejected (413).
WORKER_SYNC_MAX_BYTES = env_int("WORKER_SYNC_MAX_BYTES", 8 * 1024 * 1024)
# Allow Django's request-size guard to accommodate valid worker sync envelopes.
DATA_UPLOAD_MAX_MEMORY_SIZE = WORKER_SYNC_MAX_BYTES
# Sync protocol major version supported by this server; bridge minor versions are additive.
WORKER_SYNC_PROTOCOL_VERSION = 1
# Bounded central observation retention (#9): explicit limits, not a full archive.
WORKER_SESSION_KEEP = env_int("WORKER_SESSION_KEEP", 500)
WORKER_MESSAGE_KEEP = env_int("WORKER_MESSAGE_KEEP", 600)
WORKER_EVENT_KEEP = env_int("WORKER_EVENT_KEEP", 5000)
WORKER_EVENT_MAX_BYTES = env_int("WORKER_EVENT_MAX_BYTES", 64 * 1024)
WORKER_LIVE_MAX_BYTES = env_int("WORKER_LIVE_MAX_BYTES", 256 * 1024)
WORKER_LIVE_TTL = env_int("WORKER_LIVE_TTL", 15)

# One-way Notion mirror (GH #94). Empty token disables the sync worker.
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "").strip()
NOTION_PROJECTS_DATA_SOURCE_ID = os.environ.get("NOTION_PROJECTS_DATA_SOURCE_ID", "").strip()
NOTION_TASKS_DATA_SOURCE_ID = os.environ.get("NOTION_TASKS_DATA_SOURCE_ID", "").strip()
NOTION_SYNC_INTERVAL_SECONDS = env_int("NOTION_SYNC_INTERVAL_SECONDS", 20)
# Minimum gap between heartbeat-only pushes for one task.
NOTION_HEARTBEAT_PUSH_SECONDS = env_int("NOTION_HEARTBEAT_PUSH_SECONDS", 600)
DATABASES = {
    "default": dj_database_url.parse(
        os.environ.get("DATABASE_URL") or f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        # ASGI request/consumer threads must not retain persistent Django connections.
        conn_max_age=0,
        conn_health_checks=True,
    )
}
if DATABASES["default"]["ENGINE"] == "django.db.backends.sqlite3":
    DATABASES["default"]["OPTIONS"] = {"timeout": 20}
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
STATIC_ROOT = Path(os.environ.get("STATIC_ROOT", BASE_DIR / "staticfiles"))
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", not DEBUG)
SECURE_REDIRECT_EXEMPT = [r"^healthz$"]
SESSION_COOKIE_SECURE = env_bool("DJANGO_SECURE_COOKIES", not DEBUG)
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE
if env_bool("DJANGO_TRUST_PROXY"):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

# Django admin is a superuser-only operations console (#53). Role owners manage
# people from the SPA. Set DJANGO_ADMIN_ENABLED=false to remove it, or move it with
# DJANGO_ADMIN_PATH (e.g. "ops-7f3a/"); the Caddy front door reads ADMIN_PATH.
ADMIN_ENABLED = env_bool("DJANGO_ADMIN_ENABLED", True)
ADMIN_PATH = os.environ.get("DJANGO_ADMIN_PATH", "admin/").strip("/") + "/"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "coordinator.auth.AgentKeyAuthentication",
        "accounts.authentication.UserTokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["coordinator.auth.IsAgentOrRole"],
    "EXCEPTION_HANDLER": "coordinator.exceptions.exception_handler",
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_PAGINATION_CLASS": "coordinator.pagination.TaskPagination",
    "PAGE_SIZE": 100,
    # Login attempts per client address + username (accounts.api.LoginThrottle).
    "DEFAULT_THROTTLE_RATES": {
        "login": os.environ.get("LOGIN_THROTTLE_RATE", "10/min"),
        "invite": os.environ.get("INVITE_THROTTLE_RATE", "20/min"),
    },
    # Count of trusted proxies in front of Django (e.g. TLS proxy + Caddy = 2), so
    # throttles key on the real client and clients cannot spoof X-Forwarded-For.
    "NUM_PROXIES": int(
        os.environ.get("DJANGO_NUM_PROXIES", "1" if env_bool("DJANGO_TRUST_PROXY") else "0")
    ),
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Agent Task Coordinator API",
    "DESCRIPTION": (
        "Versioned HTTP contract shared by the web dashboard, mobile clients, MCP and bridges."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    # Same audience as the API itself; offline generation uses `manage.py spectacular`.
    "SERVE_PERMISSIONS": ["coordinator.auth.IsAgentOrRole"],
    # Only the versioned mount is documented; /api/ stays as an undocumented alias.
    "PREPROCESSING_HOOKS": ["coordinator.openapi.only_versioned_endpoints"],
    "POSTPROCESSING_HOOKS": [
        "drf_spectacular.hooks.postprocess_schema_enums",
        "coordinator.openapi.responses_include_every_field",
    ],
    "SCHEMA_PATH_PREFIX": r"/api/v1",
    "COMPONENT_SPLIT_REQUEST": True,
    # Several request/response "status" fields use different choice sets.
    "ENUM_NAME_OVERRIDES": {
        "TaskStatusEnum": "coordinator.models.Task.Status",
        "TaskStatusUpdateEnum": ["in_progress", "blocked", "review", "done", "cancelled"],
        "CommandResolutionEnum": ["done", "error"],
        "PlanningJobReportStatusEnum": ["heartbeat", "done", "blocked", "review"],
        "PipeStageRoleEnum": "coordinator.models.PipeStage.Role",
        "PipeStageStatusEnum": "coordinator.models.PipeStage.Status",
        "PipeStatusEnum": "coordinator.models.Pipe.Status",
        "PipeFailureEnum": "coordinator.models.Pipe.Failure",
        "AccountRoleEnum": "accounts.models.Role",
    },
}
