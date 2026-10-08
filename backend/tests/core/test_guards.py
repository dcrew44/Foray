"""R16: no Django process writes to a legacy-looking database (design §3.1).

- `migrate` against a scratch database holding a `drizzle` schema refuses before
  `django_migrations` exists (the `pre_migrate` receiver runs before the recorder's
  `ensure_schema`).
- `assert_app_database` recognises each legacy marker.
- `env.py` refuses a `DATABASE_URL` named `foray` statically, without connecting.

`reset_app_db`'s name allowlist is tested with that command (step 3F).
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, LiteralString, cast

import psycopg
import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import connection, connections
from django.db.backends.postgresql.base import DatabaseWrapper
from psycopg import sql

from apps.core.guards import assert_app_database, legacy_markers
from foray.env import ConfigurationError, build_env

BACKEND = Path(__file__).resolve().parents[2]
REFUSAL = "looks like the legacy Foray database; refusing to write to it."


def _server_params() -> dict[str, Any]:
    s = connection.settings_dict
    return {
        "host": s["HOST"] or "localhost",
        "port": int(s["PORT"] or 5432),
        "user": s["USER"],
        "password": s["PASSWORD"],
    }


def _url(dbname: str) -> str:
    p = _server_params()
    return f"postgres://{p['user']}:{p['password']}@{p['host']}:{p['port']}/{dbname}"


@pytest.fixture
def scratch_db() -> Iterator[str]:
    """An empty database on the test server, dropped afterwards."""
    name = f"foray_guard_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(dbname="postgres", autocommit=True, **_server_params()) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield name
    finally:
        with psycopg.connect(dbname="postgres", autocommit=True, **_server_params()) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


def _run(dbname: str, statement: str) -> list[tuple[object, ...]]:
    with psycopg.connect(dbname=dbname, autocommit=True, **_server_params()) as conn:
        cur = conn.execute(cast(LiteralString, statement))  # fixed test SQL only
        return cur.fetchall() if cur.description else []


def test_migrate_refuses_legacy_target(scratch_db: str) -> None:
    _run(scratch_db, "CREATE SCHEMA drizzle")
    env = {**os.environ, "FORAY_TEST_DATABASE_URL": _url(scratch_db)}
    env["DJANGO_SETTINGS_MODULE"] = "foray.settings_test"
    result = subprocess.run(
        [sys.executable, "manage.py", "migrate", "--noinput"],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode != 0
    assert REFUSAL in result.stderr
    assert scratch_db in result.stderr
    # Nothing was written: no django_migrations, no tables at all.
    assert _run(scratch_db, "SELECT to_regclass('public.django_migrations')") == [(None,)]
    assert _run(scratch_db, "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'") == [(0,)]


@pytest.mark.parametrize(
    ("ddl", "marker"),
    [
        ("CREATE SCHEMA drizzle", "a drizzle schema"),
        ("CREATE SCHEMA pgboss", "a pgboss schema"),
        ('CREATE TABLE public."user" (id text)', 'a public."user" table'),
        ("CREATE TABLE public.profiles (id text)", "a public.profiles table"),
    ],
)
@pytest.mark.django_db
def test_guard_recognises_each_legacy_marker(scratch_db: str, ddl: str, marker: str) -> None:
    # A second wrapper under the "default" alias: contrib.postgres's connection_created hook
    # looks the alias up, and pytest-django only allows queries on the test's databases.
    settings_dict = cast(Any, {**connection.settings_dict, "NAME": scratch_db, "OPTIONS": {}})
    wrapper = DatabaseWrapper(settings_dict, alias="default")
    try:
        assert legacy_markers(wrapper) == (scratch_db, [])
        assert_app_database(wrapper)  # an empty database passes
        _run(scratch_db, ddl)
        with pytest.raises(ImproperlyConfigured, match=REFUSAL) as refused:
            assert_app_database(wrapper)
        assert marker in str(refused.value)
    finally:
        wrapper.close()


@pytest.mark.django_db
def test_test_database_passes_the_guard() -> None:
    assert_app_database(connections["default"])


def test_env_refuses_legacy_database_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgres://foray:foray@localhost:5432/foray")
    with pytest.raises(ConfigurationError) as refused:
        build_env()
    assert any(line.startswith("DATABASE_URL") for line in refused.value.lines)


def test_env_refusal_exits_without_connecting() -> None:
    # Port 1 has nothing listening: a connection attempt would fail differently and slowly.
    env = {
        **os.environ,
        "FORAY_SKIP_DOTENV": "1",
        "DATABASE_URL": "postgres://foray:foray@127.0.0.1:1/foray",
    }
    result = subprocess.run(
        [sys.executable, "-c", "from foray.env import load_env; load_env()"],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 1
    assert result.stderr.startswith("Configuration error:")
    assert "DATABASE_URL names the database" in result.stderr
    assert "foray:foray" not in result.stderr  # values are never printed
