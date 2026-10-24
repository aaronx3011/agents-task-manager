import os
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


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
    "channels",
    "coordinator.apps.CoordinatorConfig",
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
WORKER_MESSAGE_KEEP = env_int("WORKER_MESSAGE_KEEP", 120)
WORKER_EVENT_KEEP = env_int("WORKER_EVENT_KEEP", 5000)
WORKER_EVENT_MAX_BYTES = env_int("WORKER_EVENT_MAX_BYTES", 64 * 1024)
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

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "coordinator.auth.AgentKeyAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["coordinator.auth.IsAgentOrStaff"],
    "EXCEPTION_HANDLER": "coordinator.exceptions.exception_handler",
    "DEFAULT_PAGINATION_CLASS": "coordinator.pagination.TaskPagination",
    "PAGE_SIZE": 100,
}
