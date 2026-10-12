"""Repository settings that keep data and keys private (step H2): what git ignores, what Docker
publishes, how its services come back, and where the dev servers listen.

They are files, not code, so nothing else would notice a regression: a `.env.legacy` full of
live keys swept into a commit, or Postgres (every user's data and the session table) reachable
from the LAN with the committed password.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]


def _compose() -> dict[str, Any]:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load((REPO / "docker-compose.yml").read_text())


def _ignored(path: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "--quiet", "--no-index", path],
        cwd=REPO,
        capture_output=True,
        check=False,
    )
    assert result.returncode in (0, 1), result.stderr
    return result.returncode == 0


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_git_ignores_every_env_file_but_the_template() -> None:
    if not (REPO / ".git").exists():
        pytest.skip("not a git checkout")
    # Step 13 copies the main checkout's .env (live API keys) to .env.legacy.
    for secret in (".env", ".env.legacy", ".env.local", ".env.production.local", ".env.backup"):
        assert _ignored(secret), secret
    assert not _ignored(".env.example")
    # The store step 12 moves aside before the import.
    assert _ignored("storage.pre-cutover-20261009/user/resume_pdf/x.pdf")
    assert _ignored("storage/x.pdf") and _ignored("backups/x.dump")


def test_postgres_is_published_on_loopback_only() -> None:
    db = _compose()["services"]["db"]
    assert db["ports"] == ["127.0.0.1:5432:5432"]
    # The password is the operator's (.env), not a value fixed in the repository.
    assert db["environment"]["POSTGRES_PASSWORD"].startswith("${POSTGRES_PASSWORD")
    for name in ("migrate", "api", "worker"):
        environment = _compose()["services"][name].get("environment") or {}
        url = environment.get("DATABASE_URL", "")
        assert url.startswith("postgres://foray:${POSTGRES_PASSWORD"), name


def test_long_running_services_come_back_on_their_own() -> None:
    """A database outage used to stop the worker for good (it exited 1 and nothing restarted
    it); the services restart unless stopped by hand."""
    services = _compose()["services"]
    for name in ("db", "api", "worker", "web"):
        assert services[name].get("restart") == "unless-stopped", name
    assert services["migrate"].get("restart") == "no"


def test_dev_servers_listen_on_loopback_only() -> None:
    scripts = json.loads((REPO / "package.json").read_text())["scripts"]
    assert scripts["dev"] == "next dev -H 127.0.0.1"  # next dev's default is 0.0.0.0
    assert "--host" not in scripts["dev:api"]  # uvicorn's default is 127.0.0.1
