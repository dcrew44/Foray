"""The reaper (design §9.3, §9.8; R3, R11).

Liveness is "a `todo`/`doing` job holds `run:<id>`". A killed worker's job is failed as stalled
(never retried), which releases the lock; then the run nobody holds is failed with the
"Interrupted" sentence. No periodic task holds a lock, so a reap killed mid-way cannot lock the
reaper out.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from apps.core.queue import app, defer_in_tx
from apps.runs import lifecycle, reaper
from apps.runs import tasks as run_tasks
from apps.runs.management.commands import worker as worker_command
from tests.clock import python_clock, skewed
from tests.factories import make_approval, make_run, make_user
from tests.queue import run_deferred, worker_connector
from tests.runs import support
from tests.runs.support import age_run, dead_worker, doing_job, event_kinds, jobs, refresh


@pytest.mark.django_db
def test_reaper_fails_orphaned_runs() -> None:
    user = make_user()
    queued = make_run(user, status="queued")
    running = make_run(user, status="running")
    fresh = make_run(user, status="running")  # within the 60 s margin
    held = make_run(user, status="running")
    defer_in_tx(support.step, lock=f"run:{held.id}", run_id=str(held.id))
    for run in (queued, running, held):
        age_run(run.id, 120)

    result = reaper.reap_runs()

    assert (result.interrupted, result.lost) == (2, 0)
    for run in (queued, running):
        run = refresh(run)
        assert (run.status, run.error) == ("failed", reaper.INTERRUPTED)
        assert event_kinds(run.id) == ["run.failed"]
    assert refresh(fresh).status == "running"
    assert refresh(held).status == "running"
    assert reaper.reap_runs() == reaper.ReapResult(0, 0)  # idempotent


@pytest.mark.django_db
def test_reaper_spares_live_continuation() -> None:
    user = make_user()
    # Autopilot hand-off: the run is running and its continuation waits in the queue.
    handed_off = make_run(user, status="running", mode="autopilot")
    defer_in_tx(
        support.step,
        lock=f"run:{handed_off.id}",
        queueing_lock="approval:x",
        run_id=str(handed_off.id),
    )
    # A paused run whose approval is still open is waiting on the user, not lost.
    paused = make_run(user, status="awaiting_approval")
    make_approval(user, run=paused)
    # A run whose step is running right now.
    working = make_run(user, status="running")
    doing_job("tests.step", worker_id=None, lock=f"run:{working.id}")
    for run in (handed_off, paused, working):
        age_run(run.id, 3600)

    assert reaper.reap_runs() == reaper.ReapResult(0, 0)
    assert [refresh(r).status for r in (handed_off, paused, working)] == [
        "running",
        "awaiting_approval",
        "running",
    ]


@pytest.mark.django_db
def test_reaper_fails_lost_continuation() -> None:
    user = make_user()
    lost = make_run(user, status="awaiting_approval")
    make_approval(user, run=lost, status="approved")  # decided, but its job is gone
    recent = make_run(user, status="awaiting_approval")
    make_approval(user, run=recent, status="approved")
    age_run(lost.id, 11 * 60)
    age_run(recent.id, 5 * 60)

    assert reaper.reap_runs() == reaper.ReapResult(0, 1)
    lost = refresh(lost)
    assert (lost.status, lost.error) == ("failed", reaper.LOST)
    assert event_kinds(lost.id) == ["run.failed"]
    assert refresh(recent).status == "awaiting_approval"


@pytest.mark.django_db(transaction=True)
def test_reaper_never_retries_a_stalled_money_job() -> None:
    """R11: a job a killed worker left `doing` is failed, not retried, even for a retry-1 task
    (a re-run of a model job would pay twice)."""
    user = make_user()
    run = make_run(user, status="running", kind="resume_render")
    worker_id = dead_worker()
    job_id = doing_job(
        "tests.retry_step",
        worker_id=worker_id,
        lock=f"run:{run.id}",
        queue="interactive",
        args={"run_id": str(run.id)},
    )
    age_run(run.id, 120)
    defer_in_tx(run_tasks.maintenance_reap, queueing_lock="maintenance:reap")
    run_deferred(["system"])

    [job] = jobs(lock=f"run:{run.id}")
    assert (job["id"], job["status"]) == (job_id, "failed")
    assert support.CALLS == []  # never ran again
    run = refresh(run)
    assert (run.status, run.error) == ("failed", reaper.INTERRUPTED)


@pytest.mark.django_db(transaction=True)
def test_reaper_recovers_from_a_stalled_reap() -> None:
    """A reap job killed mid-way is left `doing` with a dead worker. Periodics hold no lock, so
    the next reap still runs: it fails the stalled reap and the orphaned run too (§9.3)."""
    user = make_user()
    run = make_run(user, status="running")
    worker_id = dead_worker()
    stalled_reap = doing_job("maintenance.reap", worker_id=worker_id, queue="system")
    stalled_step = doing_job(
        "tests.step", worker_id=worker_id, lock=f"run:{run.id}", args={"run_id": str(run.id)}
    )
    age_run(run.id, 300)

    defer_in_tx(run_tasks.maintenance_reap, queueing_lock="maintenance:reap", timestamp=0)
    run_deferred(["system"])

    statuses = {job["id"]: job["status"] for job in jobs()}
    assert statuses[stalled_reap] == "failed"
    assert statuses[stalled_step] == "failed"
    assert sorted(statuses.values()) == ["failed", "failed", "succeeded"]  # the new reap ran
    run = refresh(run)
    assert (run.status, run.error) == ("failed", reaper.INTERRUPTED)
    assert event_kinds(run.id) == ["run.failed"]


@pytest.mark.django_db(transaction=True)
def test_stalled_jobs_api_sees_dead_and_pruned_workers() -> None:
    """§15.4: `get_stalled_jobs(seconds_since_heartbeat=30)` returns `doing` jobs whose worker
    is stale or already pruned (worker_id NULL), not a live worker's; `finish_job` fails them."""
    dead = dead_worker(300)
    alive = dead_worker(1)
    on_dead = doing_job("tests.sleep", worker_id=dead, queue="system", lock="a")
    orphan = doing_job("tests.sleep", worker_id=None, queue="system", lock="b")
    on_alive = doing_job("tests.sleep", worker_id=alive, queue="system", lock="c")

    async def sweep() -> list[int]:
        with app.replace_connector(worker_connector()):
            async with app.open_async():
                return await reaper.fail_stalled_jobs()

    assert sorted(asyncio.run(sweep())) == sorted([on_dead, orphan])
    statuses = {job["id"]: job["status"] for job in jobs()}
    assert statuses == {on_dead: "failed", orphan: "failed", on_alive: "doing"}


@pytest.mark.django_db(transaction=True)
def test_startup_sweep_fails_a_stalled_job(monkeypatch: pytest.MonkeyPatch) -> None:
    """`manage.py worker` fails stalled jobs before its Workers start, so a lock a killed worker
    held is released without waiting for a reap; then a job waiting on that lock runs.

    No periodic schedule is installed, so no `maintenance.reap` can rescue the stalled job: only
    the startup sweep can release the lock. If nothing does, the test fails within 20 s (the
    waiting job never runs) instead of hanging."""
    installed: list[str] = []

    def no_schedule(*_: object, **__: object) -> list[str]:
        installed.append("called")  # serve() still asks for the schedule; it gets none
        return []

    monkeypatch.setattr(worker_command.periodic, "install", no_schedule)
    worker_id = dead_worker()
    stalled = doing_job("debug.sleep", worker_id=worker_id, queue="system", lock="debug")
    waiting = defer_in_tx(run_tasks.debug_sleep, lock="debug", seconds=0.01)

    async def main() -> int:
        stop = asyncio.Event()

        async def stop_when_done() -> None:
            try:
                await support.wait_for(lambda: support.job_status(waiting) == "succeeded", 20)
            finally:
                stop.set()  # a timeout must stop the worker, not leave serve() running

        watcher = asyncio.create_task(stop_when_done())
        with support.empty_periodic_registry():
            code = await asyncio.wait_for(
                worker_command.serve(5, stop=stop, install_signal_handlers=False), 60
            )
        await watcher  # re-raises the timeout when the waiting job never ran
        return code

    assert asyncio.run(main()) == 0
    assert installed == ["called"]
    assert support.job_status(stalled) == "failed"
    assert support.job_status(waiting) == "succeeded"
    # Nothing but the startup sweep could have failed the stalled job.
    assert support.jobs(task_name="maintenance.reap") == []
    assert [job["id"] for job in support.jobs()] == [stalled, waiting]
    # A clean stop unregisters every Worker: the worker reads as offline at once (§9.7).
    assert support.sql(
        "SELECT count(*) FROM procrastinate_workers WHERE id <> %s", [worker_id]
    ) == [(0,)]


@pytest.mark.django_db
def test_lifecycle_fail_is_what_the_reaper_uses() -> None:
    user = make_user()
    run = make_run(user, status="queued")
    age_run(run.id, 120)
    assert reaper.reap_runs().interrupted == 1
    assert lifecycle.fail(run.id, "again") is False  # already failed: conditional
    finished_at = refresh(run).finished_at
    # `lifecycle.fail` writes `finished_at = now()` in SQL: compare with the database's clock.
    [(database_now,)] = support.sql("SELECT clock_timestamp()")
    assert finished_at is not None and finished_at <= database_now


@pytest.mark.django_db
@pytest.mark.parametrize("skew_s", [-120, 120])
def test_the_orphan_rule_reads_the_databases_clock(skew_s: int) -> None:
    """The 60 s orphan rule compares `runs.updated_at` with the database's `now()`. A run's
    timestamps used to come from Python's clock: with it two minutes behind, a run created
    this instant read as two minutes old and was failed at once; ahead, a dead run lived two
    minutes longer. Every stored timestamp is the database's now (step H2). `python_clock`
    moves `default=timezone.now` field defaults too, so either regression fails here."""
    with python_clock(skewed(timedelta(seconds=skew_s))):
        user = make_user()
        just_now = make_run(user, status="running")  # nothing holds it, but it is fresh
        quiet = make_run(user, status="queued")
        # Its last write was 70 s ago: move what was stored back, whichever clock stored it.
        support.sql(
            "UPDATE runs SET updated_at = updated_at - make_interval(secs => 70) WHERE id = %s",
            [str(quiet.id)],
        )

        result = reaper.reap_runs()

    assert result.interrupted == 1
    assert refresh(just_now).status == "running"
    assert refresh(quiet).status == "failed"
