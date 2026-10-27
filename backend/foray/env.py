"""The single source of truth for configuration (design §1.3).

Every process (the API, the worker, `manage.py`) builds `Env` once, through `load_env()`, and
fails at boot with one line per problem, as `src/env.ts` does today. Nothing else reads
`os.environ` for application settings.

- `.env` lives at the repo root and is shared with Next. python-dotenv loads it with
  `override=False`, so real environment variables win. `FORAY_SKIP_DOTENV=1` skips the file.
- An empty value means unset. Unknown variables are ignored.
- Relative paths resolve against the repo root, not `backend/`.
- Values are never printed, only variable names and what to do about them.
"""

from __future__ import annotations

import os
import sys
import zoneinfo
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal, get_args
from urllib.parse import unquote, urlsplit

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_core import ErrorDetails
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
DOTENV_PATH = REPO_ROOT / ".env"

Purpose = Literal["chat", "extract", "score", "evaluate", "tailor", "verify", "merge"]
PURPOSES: tuple[Purpose, ...] = get_args(Purpose)

Effort = Literal["none", "minimal", "low", "medium", "high", "xhigh"]

# Provider id -> the variable that makes it usable. compat needs its base URL (the key is optional
# for local servers), as in the TS registry.
PROVIDER_CREDENTIAL = {
    "openai": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_GENERATIVE_AI_API_KEY",
    "compat": "LLM_COMPAT_BASE_URL",
}

LEGACY_DATABASE_NAME = "foray"

MODEL_REF_PATTERN = r"^[a-z0-9-]+:[A-Za-z0-9._/@-]+$"
ModelRef = Annotated[str, Field(pattern=MODEL_REF_PATTERN)]


def _database_name(url: str) -> str:
    return unquote(urlsplit(url).path.lstrip("/"))


def _check_postgres_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in {"postgres", "postgresql"} or not parts.hostname:
        raise ValueError("not a postgres:// URL")
    if not _database_name(value):
        raise ValueError("the URL names no database")
    return value


def _check_http_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("not an http(s) URL")
    return value.rstrip("/")


def _repo_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (REPO_ROOT / path).resolve()


class Env(BaseSettings):
    """Every variable the backend reads. Field names are the variable names, lowercased."""

    model_config = SettingsConfigDict(
        env_file=None,  # load_env() reads .env itself, with override=False
        env_ignore_empty=True,  # FOO= means unset
        extra="ignore",
        case_sensitive=False,
        validate_default=True,
    )

    # --- database and files ---
    database_url: str
    legacy_database_url: str | None = None
    legacy_storage_dir: Path | None = None
    backup_dir: Path = REPO_ROOT / "backups"
    storage_dir: Path = Path("./storage")

    # --- the web side ---
    secret_key: SecretStr
    public_origin: str = "http://localhost:3000"
    debug: bool = False
    signup_open: bool = True
    trusted_proxy_hops: int = Field(default=0, ge=0, le=2)
    app_timezone: str = "UTC"
    django_origin: str = "http://127.0.0.1:8000"  # read by the frontend only

    # --- model providers ---
    openai_api_key: SecretStr | None = None
    deepseek_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    google_generative_ai_api_key: SecretStr | None = None
    llm_compat_base_url: str | None = None
    llm_compat_api_key: SecretStr | None = None

    # --- model per purpose ---
    llm_default: ModelRef
    llm_chat: ModelRef | None = None
    llm_extract: ModelRef | None = None
    llm_score: ModelRef | None = None
    llm_evaluate: ModelRef | None = None
    llm_tailor: ModelRef | None = None
    llm_verify: ModelRef | None = None
    llm_merge: ModelRef | None = None

    llm_reasoning_chat: Effort | None = None
    llm_reasoning_extract: Effort | None = None
    llm_reasoning_score: Effort | None = None
    llm_reasoning_evaluate: Effort | None = None
    llm_reasoning_tailor: Effort | None = None
    llm_reasoning_verify: Effort | None = None
    llm_reasoning_merge: Effort | None = None

    llm_rpm_openai: int = Field(default=300, gt=0)
    llm_rpm_deepseek: int = Field(default=300, gt=0)
    llm_rpm_anthropic: int = Field(default=300, gt=0)
    llm_rpm_google: int = Field(default=300, gt=0)
    llm_rpm_compat: int = Field(default=300, gt=0)

    llm_monthly_budget_usd: Decimal = Field(default=Decimal(90), gt=0)

    llm_fake: bool = False
    llm_fake_fail: Annotated[tuple[Purpose, ...], NoDecode] = ()
    llm_fake_delay_ms: int = Field(default=0, ge=0, le=60_000)

    # --- job sources ---
    rapidapi_key: SecretStr | None = None
    search_api_key: SecretStr | None = None
    jsearch_enabled: bool = False
    jsearch_monthly_budget: int = Field(default=180, gt=0)

    # --- runtime ---
    pdf_browser_channel: str | None = None
    worker_concurrency: int = Field(default=3, ge=1, le=8)
    db_pool_max: int = Field(default=10, ge=4, le=50)

    @field_validator("database_url", "legacy_database_url")
    @classmethod
    def _postgres_url(cls, value: str | None) -> str | None:
        return None if value is None else _check_postgres_url(value)

    @field_validator("public_origin", "django_origin", "llm_compat_base_url")
    @classmethod
    def _http_url(cls, value: str | None) -> str | None:
        return None if value is None else _check_http_url(value)

    @field_validator("backup_dir", "storage_dir")
    @classmethod
    def _resolve_path(cls, value: Path) -> Path:
        return _repo_path(value)

    @field_validator("legacy_storage_dir")
    @classmethod
    def _absolute_path(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("must be an absolute path")
        return value

    @field_validator("secret_key")
    @classmethod
    def _long_secret(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 50:
            raise ValueError("must be at least 50 characters")
        return value

    @field_validator("app_timezone")
    @classmethod
    def _iana_zone(cls, value: str) -> str:
        try:
            zoneinfo.ZoneInfo(value)
        except zoneinfo.ZoneInfoNotFoundError, ValueError:
            raise ValueError("not an IANA time zone name") from None
        return value

    @field_validator("jsearch_enabled", mode="before")
    @classmethod
    def _strict_true_false(cls, value: object) -> object:
        if isinstance(value, str) and value not in {"true", "false"}:
            raise ValueError("must be exactly true or false")
        return value

    @field_validator("llm_fake_fail", mode="before")
    @classmethod
    def _comma_list(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return value

    # --- derived values ---

    def model_ref(self, purpose: Purpose) -> str:
        """`LLM_<PURPOSE>`, else `LLM_DEFAULT`. Never another vendor."""
        return getattr(self, f"llm_{purpose}") or self.llm_default

    def reasoning_override(self, purpose: Purpose) -> Effort | None:
        return getattr(self, f"llm_reasoning_{purpose}")

    @property
    def database_name(self) -> str:
        return _database_name(self.database_url)

    def provider_configured(self, provider: str) -> bool:
        attr = PROVIDER_CREDENTIAL.get(provider)
        return attr is not None and getattr(self, attr.lower()) is not None


class ConfigurationError(Exception):
    """Raised by `build_env()`; `lines` hold one problem each, values never included."""

    def __init__(self, lines: list[str]) -> None:
        super().__init__("\n".join(lines))
        self.lines = lines


# How to fix a field that failed validation. Missing or invalid, the line names the variable.
FIX = {
    "database_url": (
        "set it to the app database's postgres:// URL, "
        "e.g. postgres://foray:foray@localhost:5432/foray_app"
    ),
    "legacy_database_url": "set it to the old database's postgres:// URL, or leave it empty",
    "legacy_storage_dir": "set it to the absolute path of the old data/ folder, or leave it empty",
    "secret_key": (
        "set it to a random value of at least 50 characters, e.g. the output of "
        'python -c "import secrets; print(secrets.token_urlsafe(64))"'
    ),
    "public_origin": "set it to the URL the browser opens, e.g. http://localhost:3000",
    "django_origin": "set it to the URL Next uses to reach Django, e.g. http://127.0.0.1:8000",
    "llm_compat_base_url": "set it to the provider's http(s) base URL, or leave it empty",
    "trusted_proxy_hops": "set it to 0, 1 or 2",
    "app_timezone": "set it to an IANA time zone name such as UTC or America/Los_Angeles",
    "llm_default": 'set it to "provider:model", e.g. openai:gpt-6-luna (see .env.example)',
    "llm_monthly_budget_usd": "set it to a positive number of dollars, e.g. 90",
    "llm_fake_fail": f"list purposes separated by commas, from: {', '.join(PURPOSES)}",
    "llm_fake_delay_ms": "set it to a whole number of milliseconds from 0 to 60000",
    "jsearch_enabled": "set it to exactly true or false",
    "jsearch_monthly_budget": "set it to a positive whole number",
    "worker_concurrency": "set it to a whole number from 1 to 8",
    "db_pool_max": "set it to a whole number from 4 to 50",
}
for _p in PURPOSES:
    FIX[f"llm_{_p}"] = 'set it to "provider:model", e.g. openai:gpt-6-luna, or leave it empty'
    FIX[f"llm_reasoning_{_p}"] = "set it to none, minimal, low, medium, high or xhigh"
for _provider in PROVIDER_CREDENTIAL:
    FIX[f"llm_rpm_{_provider}"] = "set it to a positive whole number of requests per minute"


def _field_lines(exc: ValidationError) -> list[str]:
    lines: list[str] = []
    for error in exc.errors(include_url=False, include_input=False):
        field = str(error["loc"][0]) if error["loc"] else ""
        name = field.upper() or "(environment)"
        problem = "is required" if error["type"] == "missing" else _problem(error)
        fix = FIX.get(field)
        lines.append(f"{name} {problem}." + (f" Fix: {fix}." if fix else ""))
    return lines


def _problem(error: ErrorDetails) -> str:
    msg = error["msg"]
    # Our own validators raise "Value error, <text>"; pydantic's messages never echo the value.
    msg = msg.removeprefix("Value error, ")
    return f"is invalid ({msg[:1].lower() + msg[1:]})"


def _storage_overlap(env: Env) -> str | None:
    """STORAGE_DIR must stay out of the legacy tree, which is never written (§3.1). The import
    refuses an overlap too, but the api and the worker would otherwise boot and quietly write
    new uploads and PDFs into the old data/ folder (a relative STORAGE_DIR=./data in the main
    checkout's .env resolves to exactly that). Symlinks are resolved; nothing is created."""
    if env.legacy_storage_dir is None:
        return None
    storage = env.storage_dir.resolve()
    legacy = env.legacy_storage_dir.resolve()
    if storage == legacy or storage.is_relative_to(legacy):
        return (
            "STORAGE_DIR is LEGACY_STORAGE_DIR or inside it; the legacy files are never written. "
            "Fix: point STORAGE_DIR at the new store (e.g. ./storage)."
        )
    if legacy.is_relative_to(storage):
        return (
            "LEGACY_STORAGE_DIR is inside STORAGE_DIR; keep them apart. Fix: point STORAGE_DIR "
            "at its own folder (e.g. ./storage)."
        )
    return None


def boot_checks(env: Env) -> list[str]:
    """The cross-variable checks of §1.3. Static: nothing here opens a connection."""
    lines: list[str] = []
    if env.database_name == LEGACY_DATABASE_NAME:
        lines.append(
            f'DATABASE_URL names the database "{LEGACY_DATABASE_NAME}", the legacy Foray '
            "database, which the new backend must never write. Fix: point it at foray_app."
        )
    if env.legacy_database_url and _database_name(env.legacy_database_url) == env.database_name:
        lines.append(
            "LEGACY_DATABASE_URL names the same database as DATABASE_URL. Fix: the legacy URL "
            "must name the old database (foray), DATABASE_URL the new one (foray_app)."
        )
    overlap = _storage_overlap(env)
    if overlap:
        lines.append(overlap)
    if env.llm_fake and not env.debug:
        lines.append("LLM_FAKE is only allowed with DEBUG=true. Fix: unset LLM_FAKE.")
    if not env.llm_fake:
        for name in ("llm_fake_fail", "llm_fake_delay_ms"):
            if name in env.model_fields_set:
                lines.append(
                    f"{name.upper()} only works with LLM_FAKE=1. Fix: unset it, or set LLM_FAKE=1."
                )
        for purpose in PURPOSES:
            ref = env.model_ref(purpose)
            source = f"LLM_{purpose.upper()}" if getattr(env, f"llm_{purpose}") else "LLM_DEFAULT"
            provider = ref.split(":", 1)[0]
            if provider not in PROVIDER_CREDENTIAL:
                lines.append(
                    f"{source} (used for {purpose}) names the unknown provider {provider!r}. "
                    f"Fix: use one of {', '.join(PROVIDER_CREDENTIAL)}."
                )
            elif not env.provider_configured(provider):
                lines.append(
                    f"{source} (used for {purpose}) needs {PROVIDER_CREDENTIAL[provider]}, "
                    "which is not set. Fix: set it in .env, or point the purpose at a provider "
                    "you have a key for."
                )
    return list(dict.fromkeys(lines))  # the same provider line can repeat across purposes


def build_env() -> Env:
    """Validate the current environment. Raises `ConfigurationError`."""
    try:
        env = Env()  # pyright: ignore[reportCallIssue]
    except ValidationError as exc:
        raise ConfigurationError(_field_lines(exc)) from None
    problems = boot_checks(env)
    if problems:
        raise ConfigurationError(problems)
    return env


def load_dotenv_file() -> None:
    if os.environ.get("FORAY_SKIP_DOTENV") == "1":
        return
    from dotenv import load_dotenv

    load_dotenv(DOTENV_PATH, override=False)


def load_env() -> Env:
    """Load `.env`, validate, and exit 1 with one line per problem if anything is wrong."""
    load_dotenv_file()
    try:
        return build_env()
    except ConfigurationError as exc:
        print("Configuration error:", file=sys.stderr)
        for line in exc.lines:
            print(f"  {line}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    # `python -m foray.env`: the static boot checks alone, no Django and no connection. The dev
    # scripts run it first because uvicorn's and watchfiles' reloaders outlive a child that
    # exits at boot, so a configuration error would otherwise not end `pnpm dev:api`.
    load_env()
