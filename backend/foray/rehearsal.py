"""`pnpm rehearsal`: the whole stack against a scratch database, with fake models.

A rehearsal runs the real pipelines end to end: the worker polls boards, watches fan out, runs
complete. Against the everyday database that would score the owner's real matches with fake
models and leave throwaway rows among real ones. So a rehearsal gets its own database
(`foray_rehearsal`, on the same Postgres as `DATABASE_URL`), its own storage folder and
`LLM_FAKE=1`, all set in the process environment, which wins over `.env`. Nothing in `.env`
changes, and the everyday database is never opened.

    pnpm rehearsal                  # make the scratch database ready, then `pnpm dev:all`
    pnpm rehearsal manage <args …>  # `manage.py <args>` there, e.g.
                                    #   pnpm rehearsal manage create_test_user --with-resume
    pnpm rehearsal -- <command …>   # any other command in the rehearsal environment, e.g.
                                    #   pnpm rehearsal -- pnpm dev:session test-…@example.test
    pnpm rehearsal:reset            # drop the scratch database and its files

"Ready" means created, migrated and, when its registry is empty, seeded with a few real boards
(`apps/jobs/rehearsal_seed.json`) and fetched once, so there are real postings to search.

This module sets the environment before anything loads settings, so it imports no Django and
nothing from `foray.env` that validates.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
from dotenv import dotenv_values
from psycopg import sql

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
DATABASE = "foray_rehearsal"
STORAGE = REPO_ROOT / "storage" / "rehearsal"
SEED = "apps/jobs/rehearsal_seed.json"

__all__ = ["DATABASE", "STORAGE", "main", "rehearsal_environ", "rehearsal_url"]


def rehearsal_url(url: str, database: str | None = None) -> str:
    """`url` with its database replaced: same server, same role, the scratch database."""
    database = database or DATABASE
    parts = urlsplit(url)
    if parts.scheme not in ("postgres", "postgresql") or not parts.hostname:
        raise SystemExit("DATABASE_URL must be a postgres:// URL to derive the rehearsal database.")
    if parts.path.lstrip("/") == database:
        raise SystemExit(f"DATABASE_URL already names {database}; rehearse from the real setup.")
    return urlunsplit(parts._replace(path=f"/{database}"))


def rehearsal_environ(base: Mapping[str, str], dotenv: Mapping[str, str | None]) -> dict[str, str]:
    """The process environment of a rehearsal: `base` with the scratch database, the scratch
    storage folder and fake models. `DATABASE_URL` comes from `base`, else from `.env`."""
    url = base.get("DATABASE_URL") or dotenv.get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL is not set (in the environment or .env).")
    return {
        **base,
        "DATABASE_URL": rehearsal_url(url),
        "STORAGE_DIR": str(STORAGE),
        "LLM_FAKE": "1",
        "DEBUG": "true",
        "FORAY_REHEARSAL": "1",
    }


def _admin(env: Mapping[str, str]) -> psycopg.Connection:
    """A connection to the server's own `postgres` database, to create or drop the scratch one."""
    return psycopg.connect(rehearsal_url(env["DATABASE_URL"], "postgres"), autocommit=True)


def ensure_database(env: Mapping[str, str]) -> bool:
    """Creates the scratch database when it is missing. Returns whether it did."""
    with _admin(env) as connection:
        exists = connection.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", [DATABASE]
        ).fetchone()
        if exists:
            return False
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DATABASE)))
    return True


def drop_database(env: Mapping[str, str]) -> None:
    with _admin(env) as connection:
        connection.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(DATABASE))
        )


def _manage(env: Mapping[str, str], *args: str, capture: bool = False) -> str:
    done = subprocess.run(
        [sys.executable, "manage.py", *args],
        cwd=BACKEND,
        env=dict(env),
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
    )
    return done.stdout or ""


def prepare(env: Mapping[str, str]) -> None:
    """Create, migrate and (when the registry is empty) seed the scratch database."""
    if ensure_database(env):
        print(f"rehearsal: created the database {DATABASE}")
    STORAGE.mkdir(parents=True, exist_ok=True)
    _manage(env, "migrate", "--no-input", "-v", "0")
    with psycopg.connect(env["DATABASE_URL"]) as connection:
        row = connection.execute("SELECT count(*) FROM companies").fetchone()
    if row is not None and row[0] == 0:
        print("rehearsal: seeding a few real boards and fetching them once")
        _manage(env, "seed_companies", "--file", SEED, "--fetch")


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    env = rehearsal_environ(os.environ, dotenv_values(REPO_ROOT / ".env"))

    if args[:1] == ["reset"]:
        drop_database(env)
        shutil.rmtree(STORAGE, ignore_errors=True)
        print(f"rehearsal: dropped {DATABASE} and {STORAGE.relative_to(REPO_ROOT)}/")
        return 0

    if args[:1] == ["--"]:
        args = args[1:]
    prepare(env)
    print(f"rehearsal: {DATABASE}, fake models, files in {STORAGE.relative_to(REPO_ROOT)}/")
    sys.stdout.flush()
    if args[:1] == ["manage"]:
        os.chdir(BACKEND)
        command = [sys.executable, "manage.py", *args[1:]]
    else:
        os.chdir(REPO_ROOT)
        command = args or ["pnpm", "dev:all"]
    os.execvpe(command[0], command, env)  # the command takes over this process and its signals


if __name__ == "__main__":
    raise SystemExit(main())
