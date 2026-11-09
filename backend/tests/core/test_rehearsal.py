"""`pnpm rehearsal` (`foray/rehearsal.py`): a rehearsal runs against a scratch database with fake
models, set in the process environment, and never opens the everyday one."""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

import psycopg
import pytest
from django.conf import settings

from foray import rehearsal


def test_the_rehearsal_url_keeps_the_server_and_swaps_the_database() -> None:
    url = "postgres://foray:s3cret@127.0.0.1:5432/foray_app?sslmode=disable"
    assert rehearsal.rehearsal_url(url) == (
        "postgres://foray:s3cret@127.0.0.1:5432/foray_rehearsal?sslmode=disable"
    )
    assert rehearsal.rehearsal_url("postgresql://u@db/x") == "postgresql://u@db/foray_rehearsal"


@pytest.mark.parametrize(
    "url",
    ["sqlite:///foray.db", "postgres:///foray_app", "postgres://foray@db:5432/foray_rehearsal"],
)
def test_it_refuses_a_url_it_cannot_rehearse_beside(url: str) -> None:
    with pytest.raises(SystemExit):
        rehearsal.rehearsal_url(url)


def test_the_environment_is_the_scratch_database_storage_and_fake_models() -> None:
    env = rehearsal.rehearsal_environ(
        {"PATH": "/bin", "LLM_FAKE": "0", "STORAGE_DIR": "/real/storage"},
        {"DATABASE_URL": "postgres://foray:pw@localhost:5432/foray_app", "DEBUG": "false"},
    )
    assert env == {
        "PATH": "/bin",
        "DATABASE_URL": "postgres://foray:pw@localhost:5432/foray_rehearsal",
        "STORAGE_DIR": str(rehearsal.STORAGE),
        "LLM_FAKE": "1",
        "DEBUG": "true",
        "FORAY_REHEARSAL": "1",
    }
    assert rehearsal.STORAGE.parts[-2:] == ("storage", "rehearsal")
    # A real environment variable wins over `.env`, as it does everywhere else.
    chosen = rehearsal.rehearsal_environ(
        {"DATABASE_URL": "postgres://a@elsewhere/app"}, {"DATABASE_URL": "postgres://b@db/app"}
    )
    assert chosen["DATABASE_URL"] == "postgres://a@elsewhere/foray_rehearsal"
    with pytest.raises(SystemExit):
        rehearsal.rehearsal_environ({}, {})


def _server_url() -> str:
    db: dict[str, Any] = settings.DATABASES["default"]
    auth = quote(db["USER"]) + (f":{quote(db['PASSWORD'])}" if db.get("PASSWORD") else "")
    return f"postgres://{auth}@{db['HOST']}:{db['PORT'] or 5432}/{db['NAME']}"


def test_it_creates_and_drops_only_the_scratch_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Against the test server, under a name of this test's own: created once, found the second
    time, dropped by name."""
    name = f"foray_rehearsal_test_{os.getpid()}"
    monkeypatch.setattr(rehearsal, "DATABASE", name)
    env = {"DATABASE_URL": rehearsal.rehearsal_url(_server_url(), name)}

    def exists() -> bool:
        admin = rehearsal.rehearsal_url(env["DATABASE_URL"], "postgres")
        with psycopg.connect(admin, autocommit=True) as connection:
            found = connection.execute("SELECT 1 FROM pg_database WHERE datname = %s", [name])
            return found.fetchone() is not None

    try:
        assert rehearsal.ensure_database(env) is True
        assert rehearsal.ensure_database(env) is False
        assert exists()
    finally:
        rehearsal.drop_database(env)
    assert not exists()
