"""The legacy side, read-only (design §3.1, §3.2, R16).

The old database is read only through psycopg sessions opened with
`default_transaction_read_only=on`, never through the ORM, and the old file tree is only ever
opened for reading. Two kinds of proof that neither changed:

- **Table fingerprints** `(count(*), sum(xmin::text::bigint))` per public table, read on a fresh
  autocommit connection. Any insert, update or delete gives the touched rows a new `xmin`, so a
  write changes the fingerprint at once. `pg_stat_user_tables` counters are only a diagnostic:
  with PG18's `stats_fetch_consistency = cache` they are frozen per transaction and flushed by
  other backends only after an idle interval, so a before/after comparison could miss a write.
- **File fingerprints**: size, mtime and sha256 of every file under `LEGACY_STORAGE_DIR`.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import psycopg
from psycopg import IsolationLevel, sql

# Every legacy session: read-only by default, never killed for idling in its long snapshot, and
# rendering timestamps in UTC.
LEGACY_OPTIONS = (
    "-c default_transaction_read_only=on -c idle_in_transaction_session_timeout=0 -c TimeZone=UTC"
)

# The tables the Drizzle SQL creates in every legacy database (the test fixture included);
# `assert_legacy_source` checks them in this order.
LEGACY_MARKER_TABLES = ('public."user"', "public.profiles")

PUBLIC_TABLES_SQL = """
SELECT c.relname
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
 ORDER BY c.relname
"""


class LegacyError(Exception):
    """The legacy side is not what the importer may read."""


def database_name(url: str) -> str:
    return unquote(urlsplit(url).path.lstrip("/"))


def connect(url: str, *, snapshot: bool = False) -> psycopg.Connection[Any]:
    """A read-only session on the legacy database.

    `snapshot=False`: autocommit, for guard and fingerprint queries. `snapshot=True`: every
    statement runs in one `REPEATABLE READ READ ONLY` transaction, begun by the first statement
    and held until the caller closes or commits.
    """
    conn = psycopg.connect(url, options=LEGACY_OPTIONS, autocommit=not snapshot)
    try:
        if snapshot:
            conn.isolation_level = IsolationLevel.REPEATABLE_READ
            conn.read_only = True
        row = conn.execute("SHOW transaction_read_only").fetchone()
        if row is None or row[0] != "on":
            raise LegacyError("The legacy session is not read-only; refusing to continue.")
    except BaseException:
        conn.close()
        raise
    return conn


@contextmanager
def session(url: str, *, snapshot: bool = False) -> Iterator[psycopg.Connection[Any]]:
    conn = connect(url, snapshot=snapshot)
    try:
        yield conn
    finally:
        conn.close()


def assert_legacy_source(conn: psycopg.Connection[Any]) -> None:
    """Refuse a source that lacks the legacy tables, so a swapped URL pair fails here (§3.1)."""
    row = conn.execute(
        "SELECT current_database(), to_regclass('public.\"user\"') IS NOT NULL, "
        "to_regclass('public.profiles') IS NOT NULL"
    ).fetchone()
    assert row is not None
    name, *present = row
    missing = [table for table, ok in zip(LEGACY_MARKER_TABLES, present, strict=True) if not ok]
    if missing:
        raise LegacyError(
            f'Database "{name}" has no {" or ".join(missing)} table: it does not look like the '
            "legacy Foray database; refusing to import from it."
        )


def public_tables(conn: psycopg.Connection[Any]) -> list[str]:
    return [row[0] for row in conn.execute(PUBLIC_TABLES_SQL)]


def table_counts(conn: psycopg.Connection[Any]) -> dict[str, int]:
    """Row counts of every public table, on `conn` (inside its transaction, if any)."""
    counts: dict[str, int] = {}
    for table in public_tables(conn):
        query = sql.SQL("SELECT count(*) FROM public.{}").format(sql.Identifier(table))
        row = conn.execute(query).fetchone()
        assert row is not None
        counts[table] = int(row[0])
    return counts


def fingerprints(url: str) -> dict[str, list[str]]:
    """`{table: [count, sum(xmin)]}` for every public table, on a fresh autocommit session."""
    result: dict[str, list[str]] = {}
    with session(url) as conn:
        for table in public_tables(conn):
            query = sql.SQL(
                "SELECT count(*), coalesce(sum(xmin::text::bigint), 0) FROM public.{}"
            ).format(sql.Identifier(table))
            row = conn.execute(query).fetchone()
            assert row is not None
            result[table] = [str(row[0]), str(row[1])]
    return result


def stat_counters(url: str) -> dict[str, list[int]]:
    """`pg_stat_user_tables` insert/update/delete counters: a diagnostic only (see above)."""
    with session(url) as conn:
        conn.execute("SELECT pg_stat_clear_snapshot()")
        rows = conn.execute(
            "SELECT relname, n_tup_ins, n_tup_upd, n_tup_del FROM pg_stat_user_tables "
            "WHERE schemaname = 'public' ORDER BY relname"
        ).fetchall()
    return {name: [int(ins), int(upd), int(dele)] for name, ins, upd, dele in rows}


def active_queue_jobs(conn: psycopg.Connection[Any]) -> list[dict[str, Any]]:
    """pg-boss jobs still `created`, `retry` or `active`: reported, never fatal (§3.2)."""
    row = conn.execute("SELECT to_regclass('pgboss.job') IS NOT NULL").fetchone()
    if row is None or not row[0]:
        return []
    rows = conn.execute(
        "SELECT name, state::text, count(*) FROM pgboss.job "
        "WHERE state::text IN ('created', 'retry', 'active') GROUP BY 1, 2 ORDER BY 1, 2"
    ).fetchall()
    return [{"queue": name, "state": state, "jobs": int(count)} for name, state, count in rows]


# --- files ---------------------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def walk_files(root: Path) -> list[Path]:
    """Every regular file and symlink under `root`, sorted by relative path (byte order)."""
    found: list[Path] = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        found.extend(Path(directory) / name for name in filenames)
    return sorted(found, key=lambda path: path.relative_to(root).as_posix().encode())


def file_fingerprints(root: Path) -> dict[str, dict[str, Any]]:
    """`{relative path: {size, mtime_ns, sha256}}` for every file under `root`. A symlink is
    recorded by its target, never followed."""
    result: dict[str, dict[str, Any]] = {}
    for path in walk_files(root):
        rel = path.relative_to(root).as_posix()
        info = path.lstat()
        if path.is_symlink():
            result[rel] = {"symlink": os.readlink(path), "mtime_ns": info.st_mtime_ns}
        else:
            result[rel] = {
                "size": info.st_size,
                "mtime_ns": info.st_mtime_ns,
                "sha256": sha256_file(path),
            }
    return result


def diff_mappings(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """One line per key that was added, removed or changed."""
    lines: list[str] = []
    for key in sorted(before.keys() | after.keys()):
        if key not in after:
            lines.append(f"{key}: gone (was {before[key]})")
        elif key not in before:
            lines.append(f"{key}: new ({after[key]})")
        elif before[key] != after[key]:
            lines.append(f"{key}: {before[key]} -> {after[key]}")
    return lines
