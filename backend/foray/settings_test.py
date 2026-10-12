"""Test settings: isolated from the developer's `.env` and shell (design §1.3, §12.1).

Before importing `foray.settings` this module
1. sets `FORAY_SKIP_DOTENV=1`, so the repo-root `.env` is never read;
2. deletes every variable `Env` declares from `os.environ`, so nothing exported in the shell
   leaks in;
3. sets the complete test environment.

pytest-django connects only to the test database it builds from migrations: `test_foray_app`,
or `FORAY_TEST_DB_NAME` when parallel build lanes share one Postgres server.
"""

import os
import tempfile
from pathlib import Path

from foray.env import Env

os.environ["FORAY_SKIP_DOTENV"] = "1"
for _field in Env.model_fields:
    os.environ.pop(_field.upper(), None)

_TMP = Path(tempfile.mkdtemp(prefix="foray-test-"))

os.environ.update(
    {
        "DATABASE_URL": os.environ.get(
            "FORAY_TEST_DATABASE_URL", "postgres://foray:foray@localhost:5432/foray_app"
        ),
        "SECRET_KEY": "test-secret-key-0123456789-0123456789-0123456789-a",  # 50 characters
        "DEBUG": "true",
        "PUBLIC_ORIGIN": "http://localhost:3000",
        "SIGNUP_OPEN": "true",
        "TRUSTED_PROXY_HOPS": "0",
        "APP_TIMEZONE": "UTC",
        "LLM_DEFAULT": "openai:gpt-5.6-luna",
        "LLM_EVALUATE": "openai:gpt-5.6-terra",
        "LLM_TAILOR": "openai:gpt-5.6-terra",
        "LLM_MONTHLY_BUDGET_USD": "90",
        "LLM_FAKE": "false",
        "OPENAI_API_KEY": "test",
        "DEEPSEEK_API_KEY": "test",
        "ANTHROPIC_API_KEY": "test",
        "GOOGLE_GENERATIVE_AI_API_KEY": "test",
        "JSEARCH_ENABLED": "false",
        "STORAGE_DIR": str(_TMP / "storage"),
        "BACKUP_DIR": str(_TMP / "backups"),
    }
)

from foray.settings import *  # noqa: E402, F403
from foray.settings import DATABASES  # noqa: E402

DATABASES["default"]["TEST"] = {"NAME": os.environ.get("FORAY_TEST_DB_NAME", "test_foray_app")}
