"""The runs management commands: `debug_sleep`, the `worker` refusals, `acceptance_checks`
(design §9.1, §12.6, §15.2 step 4)."""

from __future__ import annotations

import hashlib
from datetime import timedelta
from io import StringIO
from typing import Any

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management import CommandError, call_command
from django.utils import timezone

from apps.core.storage import storage
from apps.legacy.models import ImportRecord
from apps.runs import events, lifecycle
from apps.runs.management.commands import acceptance_checks
from apps.runs.management.commands import worker as worker_command
from tests.factories import (
    make_conversation,
    make_document,
    make_llm_call,
    make_run,
    make_user,
)
from tests.runs.support import age_run, dead_worker, doing_job, jobs, sql


def _call(name: str, *args: str) -> tuple[int, str, str]:
    out, err = StringIO(), StringIO()
    try:
        call_command(name, *args, stdout=out, stderr=err)
    except SystemExit as exit_:
        return int(exit_.code or 0), out.getvalue(), err.getvalue()
    return 0, out.getvalue(), err.getvalue()


# --- debug_sleep ---------------------------------------------------------------------------------


@pytest.mark.django_db
def test_debug_sleep_defers_a_locked_job(settings: Any) -> None:
    settings.DEBUG = True
    code, out, _ = _call("debug_sleep", "300")
    assert code == 0
    [job] = jobs(task_name="debug.sleep")
    assert out.strip() == f"job: {job['id']}"
    assert (job["queue"], job["lock"], job["queueing_lock"], job["args"]) == (
        "system",
        "debug",
        None,
        {"seconds": 300.0},
    )


@pytest.mark.django_db
def test_debug_sleep_is_for_development_only(settings: Any) -> None:
    settings.DEBUG = False
    assert _call("debug_sleep", "5") == (1, "", "debug_sleep is for local development only.\n")
    settings.DEBUG = True
    assert _call("debug_sleep", "0")[0] == 1
    assert jobs() == []


# --- worker --------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_worker_refuses_unapplied_migrations(monkeypatch: pytest.MonkeyPatch) -> None:
    assert worker_command.unapplied_migrations() == []  # the test database is migrated
    monkeypatch.setattr(worker_command, "unapplied_migrations", lambda: ["runs.0002_x"])
    started: list[Any] = []
    monkeypatch.setattr(worker_command, "serve", lambda *a, **k: started.append(a))
    code, _, err = _call("worker")
    assert code == 1
    assert err.strip() == "There are unapplied migrations. Run pnpm db:migrate (runs.0002_x)."
    assert started == []


@pytest.mark.django_db
def test_worker_refuses_a_legacy_looking_database(monkeypatch: pytest.MonkeyPatch) -> None:
    def legacy(connection: Any) -> None:
        raise ImproperlyConfigured('Database "foray" has a pgboss schema: refusing to write to it.')

    monkeypatch.setattr(worker_command, "assert_app_database", legacy)
    started: list[Any] = []
    monkeypatch.setattr(worker_command, "serve", lambda *a, **k: started.append(a))
    code, _, err = _call("worker")
    assert (code, started) == (1, [])
    assert "refusing to write to it" in err


def test_worker_lanes() -> None:
    assert worker_command.lanes() == [("interactive", 4), ("background", 3), ("system", 2)]


# --- acceptance_checks ---------------------------------------------------------------------------


def _healthy(user: Any) -> None:
    dead_worker(1)  # a heartbeat a second old: the worker is alive
    run = make_run(user, status="running")
    lifecycle.complete(run.id, {"evaluated": 0, "matchIds": []}, "Nothing to evaluate.")
    chat = make_conversation(user)
    make_llm_call(user, purpose="chat", conversation=chat)
    make_llm_call(user, purpose="score", run=run)
    make_llm_call(None, purpose="extract")  # the cron's system row
    data = b"%PDF-1.7 a resume"
    document = make_document(user, sha256=hashlib.sha256(data).hexdigest())
    storage().put(document.storage_path, data)


@pytest.mark.django_db
def test_acceptance_checks_pass_on_a_healthy_database() -> None:
    _healthy(make_user())
    code, out, _ = _call("acceptance_checks")
    assert code == 0, out
    lines = out.splitlines()
    assert len([line for line in lines if line.startswith("PASS")]) == 8
    assert lines[-1] == "(0 imported run(s) skipped by the run-level checks)"


@pytest.mark.django_db
def test_acceptance_checks_report_each_broken_invariant() -> None:
    user = make_user()
    _healthy(user)
    orphan = make_run(user, status="running")
    age_run(orphan.id, 300)
    silent = make_run(user, status="completed")  # finished without its run.* event
    late = make_run(user, status="running")
    lifecycle.fail(late.id, "It broke.")
    events.emit(user.pk, "scoring.done", "after the end", run_id=late.id)  # not the last
    stuck = doing_job("debug.sleep", worker_id=None, queue="system")
    sql(
        "INSERT INTO procrastinate_events (job_id, type, at) "
        "VALUES (%s, 'started', now() - interval '2 hours')",
        [stuck],
    )
    make_llm_call(user, purpose="score")  # a pipeline call with no run
    missing = make_document(user)  # its file was never written
    stale_chat = make_conversation(user, turn_started_at=timezone.now() - timedelta(minutes=5))

    code, out, _ = _call("acceptance_checks")
    assert code == 1
    failed = "\n".join(line for line in out.splitlines() if line.startswith("FAIL"))
    for expected in (str(orphan.id), str(silent.id), str(late.id), str(stuck), str(missing.id)):
        assert expected in failed
    assert str(stale_chat.id) in failed
    assert "unattributed" in failed
    assert out.count("FAIL") == 6  # everything but the heartbeat and the budget


@pytest.mark.django_db
def test_acceptance_checks_skip_imported_runs() -> None:
    user = make_user()
    _healthy(user)
    legacy_tailor = make_run(user, status="completed", kind="tailor")  # ended with tailor.done
    events.emit(user.pk, "tailor.done", "Your resume is ready.", run_id=legacy_tailor.id)
    ImportRecord.objects.create(
        finished_at=timezone.now(),
        status="ok",
        legacy_db="foray",
        counts={},
        checksums={},
        report={},
    )
    code, out, _ = _call("acceptance_checks")
    assert code == 0, out
    assert "imported run(s) skipped" in out.splitlines()[-1]
    assert not out.splitlines()[-1].startswith("(0 ")
    checks, skipped = acceptance_checks.run_checks()
    assert skipped >= 1 and all(check.ok for check in checks)


@pytest.mark.django_db
def test_acceptance_checks_since_bounds_the_ledger_check() -> None:
    user = make_user()
    _healthy(user)
    old = make_llm_call(user, purpose="score")
    sql("UPDATE llm_calls SET created_at = now() - interval '1 day' WHERE id = %s", [str(old.id)])
    assert _call("acceptance_checks")[0] == 1
    since = (timezone.now() - timedelta(hours=1)).isoformat()
    assert _call("acceptance_checks", "--since", since)[0] == 0
    with pytest.raises(CommandError):
        _call("acceptance_checks", "--since", "yesterday")
