"""Fixtures for the legacy import tests (design §3.6).

- `legacy_template` (session): a database built from `schema.sql` and seeded with
  `tests/legacy/seed.py`, named after pytest's own test database (`test_foray_app` →
  `test_foray_legacy`, `test_foray_app_<lane>` → `test_foray_legacy_<lane>`), plus a legacy file
  tree. Dropped at the end of the session.
- `legacy` (per test): a private copy of that database (`CREATE DATABASE … TEMPLATE`) and of the
  file tree, a fresh backup marker whose counts are the copy's, a scratch `STORAGE_DIR`, and
  `settings.ENV` pointing the importer at all of it. A test may change its copy freely.

The import's target is pytest's test database, through Django's connection.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, LiteralString, cast

import psycopg
import pytest
from django.db import connection
from psycopg import sql
from psycopg.conninfo import make_conninfo

from apps.legacy import source
from apps.legacy.backup import iso_z, stamp_of
from tests.legacy import seed

SCHEMA = Path(__file__).with_name("schema.sql")


def server_params() -> dict[str, Any]:
    s = connection.settings_dict
    return {
        "host": s["HOST"] or "localhost",
        "port": int(s["PORT"] or 5432),
        "user": s["USER"],
        "password": s["PASSWORD"],
    }


def url_of(dbname: str) -> str:
    p = server_params()
    return f"postgres://{p['user']}:{p['password']}@{p['host']}:{p['port']}/{dbname}"


def app_test_db_name() -> str:
    return str(connection.settings_dict.get("TEST", {}).get("NAME") or "test_foray_app")


def template_name() -> str:
    name = app_test_db_name()
    if name.startswith("test_foray_app"):
        return "test_foray_legacy" + name.removeprefix("test_foray_app")
    return f"{name}_legacy"


def admin() -> psycopg.Connection[Any]:
    return psycopg.connect(dbname="postgres", autocommit=True, **server_params())


def drop_database(name: str) -> None:
    with admin() as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )


def run_sql(dbname: str, statement: str, params: Any = None) -> list[tuple[Any, ...]]:
    """Run fixed test SQL on a database with a writable session."""
    with psycopg.connect(make_conninfo(dbname=dbname, **server_params()), autocommit=True) as c:
        cur = c.execute(cast(LiteralString, statement), params)
        return cur.fetchall() if cur.description else []


@dataclass
class Templates:
    database: str
    storage: Path


@pytest.fixture(scope="session")
def legacy_template(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Templates]:
    name = template_name()
    drop_database(name)
    with admin() as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        with psycopg.connect(make_conninfo(dbname=name, **server_params())) as conn:
            conn.execute(cast(LiteralString, SCHEMA.read_text()))
            seed.seed(conn)
            conn.commit()
        storage = tmp_path_factory.mktemp("legacy-data")
        seed.write_files(storage)
        yield Templates(name, storage)
    finally:
        drop_database(name)


@dataclass
class LegacyEnv:
    name: str
    url: str
    storage: Path
    backups: Path
    target_storage: Path

    def execute(self, statement: str, params: Any = None) -> list[tuple[Any, ...]]:
        return run_sql(self.name, statement, params)

    def write_marker(self, *, created_at: datetime | None = None) -> Path:
        """A backup marker for this copy, as `backup_legacy` writes it, with a stand-in dump."""
        moment = created_at or datetime.now(UTC)
        stamp = stamp_of(moment)
        dump = self.backups / f"{stamp}-foray.dump"
        dump.write_bytes(f"stand-in dump of {self.name} at {stamp}".encode())
        with source.session(self.url) as conn:
            counts = source.table_counts(conn)
        marker = self.backups / f"{stamp}.ok"
        marker.write_text(
            json.dumps(
                {
                    "dump": dump.name,
                    "dump_sha256": hashlib.sha256(dump.read_bytes()).hexdigest(),
                    "counts": dict(sorted(counts.items())),
                    "created_at": iso_z(moment),
                },
                indent=2,
            )
            + "\n"
        )
        return marker


def clone(template: str) -> str:
    name = f"{template}_{uuid.uuid4().hex[:8]}"
    statement = sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
        sql.Identifier(name), sql.Identifier(template)
    )
    for attempt in range(20):  # autovacuum may briefly hold the template
        try:
            with admin() as conn:
                conn.execute(statement)
            return name
        except psycopg.errors.ObjectInUse:
            if attempt == 19:
                raise
            time.sleep(0.25)
    raise AssertionError("unreachable")


@pytest.fixture
def legacy(legacy_template: Templates, tmp_path: Path, settings: Any) -> Iterator[LegacyEnv]:
    name = clone(legacy_template.database)
    storage = tmp_path / "legacy-data"
    shutil.copytree(legacy_template.storage, storage)
    env = LegacyEnv(
        name=name,
        url=url_of(name),
        storage=storage,
        backups=tmp_path / "backups",
        target_storage=tmp_path / "storage",
    )
    env.backups.mkdir()
    settings.ENV = settings.ENV.model_copy(
        update={"legacy_database_url": env.url, "legacy_storage_dir": storage}
    )
    settings.STORAGE_DIR = env.target_storage
    settings.BACKUP_DIR = env.backups
    env.write_marker()
    try:
        yield env
    finally:
        drop_database(name)
