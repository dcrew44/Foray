"""Scratch databases and stores, behind name allowlists (design §3.1, §3.6, step 3F).

Only these names are ever created or dropped here:

- `reset_app_db` recreates `foray_app` or `foray_rehearsal` (`RESETTABLE`);
- the import rehearsal restores a backup into `foray_restore` and imports into
  `foray_rehearsal`, and drops exactly those two afterwards (`REHEARSAL`).

Any other name is refused before a statement is sent. Drops are never forced, so a database
somebody is still connected to is refused by Postgres itself. A scratch store is a directory
this module created (it holds `STORE_SENTINEL`); nothing else is ever removed.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

RESTORE_DB = "foray_restore"
REHEARSAL_DB = "foray_rehearsal"
RESETTABLE = frozenset({"foray_app", REHEARSAL_DB})
REHEARSAL = frozenset({REHEARSAL_DB, RESTORE_DB})
STORE_SENTINEL = ".foray-rehearsal-store"

type Runner = Callable[..., subprocess.CompletedProcess[bytes]]


class ScratchError(Exception):
    """A refused or failed scratch operation."""


def admin_params(settings_dict: dict[str, Any]) -> dict[str, Any]:
    """Server connection parameters from a Django database settings dict (no database name)."""
    return {
        "host": settings_dict.get("HOST") or "localhost",
        "port": int(settings_dict.get("PORT") or 5432),
        "user": settings_dict.get("USER"),
        "password": settings_dict.get("PASSWORD"),
    }


def _allowed(name: str, allowed: frozenset[str]) -> None:
    if name not in allowed:
        raise ScratchError(
            f'Refusing to touch database "{name}": only {", ".join(sorted(allowed))} may be.'
        )


def _admin(params: dict[str, Any]) -> psycopg.Connection[Any]:
    return psycopg.connect(dbname="postgres", autocommit=True, **params)


def database_exists(name: str, params: dict[str, Any]) -> bool:
    with _admin(params) as conn:
        row = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", [name]).fetchone()
    return row is not None


def create_database(name: str, params: dict[str, Any], *, allowed: frozenset[str]) -> None:
    _allowed(name, allowed)
    if database_exists(name, params):
        raise ScratchError(f'Database "{name}" already exists; drop it first.')
    with _admin(params) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))


def drop_database(name: str, params: dict[str, Any], *, allowed: frozenset[str]) -> bool:
    """Drop `name` if it exists. Returns whether it existed."""
    _allowed(name, allowed)
    existed = database_exists(name, params)
    if existed:
        try:
            with _admin(params) as conn:
                conn.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))
        except psycopg.errors.ObjectInUse as exc:
            raise ScratchError(
                f'"{name}" is in use: stop whatever is connected to it first ({exc}).'.strip()
            ) from None
    return existed


def recreate_database(name: str, params: dict[str, Any], *, allowed: frozenset[str]) -> None:
    drop_database(name, params, allowed=allowed)
    create_database(name, params, allowed=allowed)


def restore_dump(
    dump: Path,
    *,
    params: dict[str, Any],
    container: str,
    name: str = RESTORE_DB,
    run: Runner = subprocess.run,
) -> None:
    """Create `name` (rehearsal allowlist only) and `pg_restore` the dump into it."""
    create_database(name, params, allowed=REHEARSAL)
    user = str(params.get("user") or "foray")
    with dump.open("rb") as handle:
        result = run(
            [
                "docker",
                "exec",
                "-i",
                container,
                "pg_restore",
                "-U",
                user,
                "-d",
                name,
                "--exit-on-error",
                "--no-owner",
            ],
            stdin=handle,
            capture_output=True,
        )
    if result.returncode != 0:
        detail = (result.stderr or b"").decode("utf-8", "replace").strip()
        raise ScratchError(f"pg_restore into {name} failed ({result.returncode}): {detail}")


def create_store(path: Path) -> None:
    """A fresh scratch `STORAGE_DIR`, marked as ours."""
    if path.exists() and any(path.iterdir()):
        raise ScratchError(f"{path} exists and is not empty.")
    path.mkdir(parents=True, exist_ok=True)
    (path / STORE_SENTINEL).write_text("A scratch STORAGE_DIR for the legacy import rehearsal.\n")


def drop_store(path: Path, *, protected: list[Path]) -> bool:
    """Remove a scratch store this module created. Returns whether it existed."""
    if not path.exists():
        return False
    resolved = path.resolve()
    for other in protected:
        other = other.resolve()
        if resolved == other or other.is_relative_to(resolved) or resolved.is_relative_to(other):
            raise ScratchError(f"Refusing to remove {path}: it overlaps {other}.")
    if not (path / STORE_SENTINEL).is_file():
        raise ScratchError(f"Refusing to remove {path}: it is not a rehearsal store.")
    shutil.rmtree(path)
    return True
