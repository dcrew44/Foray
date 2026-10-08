"""Django settings. Every value comes from `foray.env` (design §1.3); nothing reads os.environ."""

from __future__ import annotations

from urllib.parse import urlsplit

import dj_database_url

from foray.env import REPO_ROOT, load_env

ENV = load_env()

BASE_DIR = REPO_ROOT / "backend"

SECRET_KEY = ENV.secret_key.get_secret_value()
DEBUG = ENV.debug

PUBLIC_ORIGIN = ENV.public_origin
# Next's rewrite replaces Host with the upstream; server components call api:8000 in Docker.
ALLOWED_HOSTS = [
    host
    for host in dict.fromkeys(["localhost", "127.0.0.1", "api", urlsplit(PUBLIC_ORIGIN).hostname])
    if host
]
# The Origin check (§4.3) accepts these for unsafe methods under /api/.
ALLOWED_ORIGINS = frozenset(
    {PUBLIC_ORIGIN} | ({"http://localhost:3000", "http://127.0.0.1:3000"} if DEBUG else set())
)

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.postgres",
    "procrastinate.contrib.django",
    "ninja",  # for its export_openapi_schema command (pnpm api:types) and the docs templates
    "apps.core",
    "apps.accounts",
    "apps.llm",
    "apps.jobs",
    "apps.matching",
    "apps.resumes",
    "apps.runs",
    "apps.chat",
    "apps.pages",
    "apps.legacy",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Its get_host() call is what enforces ALLOWED_HOSTS; nothing else in this stack asks.
    "django.middleware.common.CommonMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "foray.middleware.origin_check",  # §4.3 (no CsrfViewMiddleware)
    "foray.middleware.sliding_session",  # §4.1 (skips /api/chat and X-Foray-SSR requests)
    "foray.middleware.stream_headers",  # no-transform on every text/event-stream
]

ROOT_URLCONF = "foray.urls"
ASGI_APPLICATION = "foray.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,  # apps/resumes/templates/resume.html (§11.5)
        "OPTIONS": {"autoescape": True},
    }
]

DB_POOL_MAX = ENV.db_pool_max
DATABASES = {
    "default": {
        **dj_database_url.parse(ENV.database_url, conn_max_age=0),
        "CONN_MAX_AGE": 0,  # the pool refuses persistent connections
        "CONN_HEALTH_CHECKS": False,
        "OPTIONS": {"pool": {"min_size": 2, "max_size": DB_POOL_MAX, "timeout": 10}},
    }
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_USER_MODEL = "accounts.User"
AUTHENTICATION_BACKENDS = ["django.contrib.auth.backends.ModelBackend"]
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "apps.accounts.hashers.BetterAuthScryptHasher",  # import-only, step 3A (§4.2)
]
AUTH_PASSWORD_VALIDATORS: list[dict[str, str]] = []  # length rules live in the sign-up endpoint

SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_NAME = "sessionid"
SESSION_COOKIE_AGE = 7 * 86400
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = PUBLIC_ORIGIN.startswith("https")

USE_TZ = True
TIME_ZONE = "UTC"
USE_I18N = False
LANGUAGE_CODE = "en-us"
APPEND_SLASH = False

DATA_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024
FILE_UPLOAD_HANDLERS = [
    "apps.resumes.parse.sniff.MaxSizeUploadHandler",  # aborts > 10 MB (§5.2); written with uploads
    "django.core.files.uploadhandler.MemoryFileUploadHandler",
]
FILE_UPLOAD_MAX_MEMORY_SIZE = 11 * 1024 * 1024

STORAGE_DIR = ENV.storage_dir
BACKUP_DIR = ENV.backup_dir

PROCRASTINATE_PERIODIC_DEFAULTS = {"max_delay": 6 * 3600}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,  # keeps uvicorn's loggers and Django's error logging
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}},
    "handlers": {
        "stdout": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "formatter": "plain",
        }
    },
    "root": {"handlers": ["stdout"], "level": "WARNING"},
    "loggers": {
        "foray": {"level": "INFO"},
        "django": {"level": "INFO"},
        "procrastinate": {"level": "WARNING"},
    },
}
