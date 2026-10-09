"""`reset_app_db`, `mark_cutover` and the rehearsal's name allowlists (design §3.1, §3.2, §12.3).

The `reset_app_db` allowlist case §12.3 lists under `core/test_guards.py` lives here, with the
command (step 1's file is not this step's). No test here drops a real database: the allowlisted
path is exercised by the step 3F rehearsal, on `foray_rehearsal`.
"""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection

from apps.legacy import rehearsal
from apps.legacy.management.commands import reset_app_db
from apps.legacy.models import ImportRecord, ImportStatus

pytestmark = pytest.mark.django_db


@pytest.fixture
def no_admin_connections(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Fail the test if anything opens a maintenance connection (where CREATE/DROP run)."""
    opened: list[str] = []

    def refuse(params: dict[str, Any]) -> Any:
        opened.append("admin")
        raise AssertionError("a scratch-database statement was about to be sent")

    monkeypatch.setattr(rehearsal, "_admin", refuse)
    monkeypatch.setattr(reset_app_db, "recreate_database", lambda *a, **k: refuse({}))
    return opened


def test_reset_app_db_refuses_names_outside_its_allowlist(no_admin_connections: list[str]) -> None:
    name = connection.settings_dict["NAME"]
    with pytest.raises(CommandError, match=f'Refusing to reset "{name}"'):
        call_command("reset_app_db", stdout=StringIO())
    assert no_admin_connections == []
    assert {"foray_app", "foray_rehearsal"} == rehearsal.RESETTABLE


def test_reset_app_db_is_debug_only(settings: Any, no_admin_connections: list[str]) -> None:
    settings.ENV = settings.ENV.model_copy(update={"debug": False})
    with pytest.raises(CommandError, match="local development only"):
        call_command("reset_app_db", stdout=StringIO())
    assert no_admin_connections == []


def test_reset_app_db_refuses_after_cutover(
    monkeypatch: pytest.MonkeyPatch, no_admin_connections: list[str]
) -> None:
    # Let this database's name through the allowlist; the cutover record must still stop it.
    name = str(connection.settings_dict["NAME"])
    monkeypatch.setattr(reset_app_db, "RESETTABLE", frozenset({name}))
    ImportRecord.objects.create(
        status=ImportStatus.OK,
        legacy_db="foray",
        counts={},
        checksums={},
        report={},
        cutover=True,
    )
    with pytest.raises(CommandError, match="marked as the cutover"):
        call_command("reset_app_db", stdout=StringIO())
    assert no_admin_connections == []


def test_mark_cutover_needs_an_import() -> None:
    with pytest.raises(CommandError, match="No successful import"):
        call_command("mark_cutover", stdout=StringIO())
    record = ImportRecord.objects.create(
        status=ImportStatus.OK, legacy_db="foray", counts={}, checksums={}, report={}
    )
    out = StringIO()
    call_command("mark_cutover", stdout=out)
    record.refresh_from_db()
    assert record.cutover and "as the cutover" in out.getvalue()
    out = StringIO()
    call_command("mark_cutover", stdout=out)
    assert "already marked" in out.getvalue()


@pytest.mark.parametrize("name", ["foray", "foray_app", "postgres", "test_foray_app", "template1"])
def test_rehearsal_drops_only_its_two_databases(name: str, no_admin_connections: list[str]) -> None:
    with pytest.raises(rehearsal.ScratchError, match="Refusing to touch database"):
        rehearsal.drop_database(name, {}, allowed=rehearsal.REHEARSAL)
    with pytest.raises(rehearsal.ScratchError, match="Refusing to touch database"):
        rehearsal.create_database(name, {}, allowed=rehearsal.REHEARSAL)
    assert no_admin_connections == []
    assert {"foray_rehearsal", "foray_restore"} == rehearsal.REHEARSAL


def test_rehearsal_store_is_removed_only_when_ours(tmp_path: Path) -> None:
    store = tmp_path / "scratch-store"
    rehearsal.create_store(store)
    (store / "user" / "resume_pdf").mkdir(parents=True)
    (store / "user" / "resume_pdf" / "a.pdf").write_bytes(b"%PDF")
    real = tmp_path / "storage"
    real.mkdir()
    (real / "keep.txt").write_text("keep")
    with pytest.raises(rehearsal.ScratchError, match="not a rehearsal store"):
        rehearsal.drop_store(real, protected=[])
    with pytest.raises(rehearsal.ScratchError, match="overlaps"):
        rehearsal.drop_store(store, protected=[store / "user"])
    with pytest.raises(rehearsal.ScratchError, match="not empty"):
        rehearsal.create_store(real)
    assert rehearsal.drop_store(store, protected=[real])
    assert not store.exists() and (real / "keep.txt").exists()
    assert not rehearsal.drop_store(store, protected=[real])
