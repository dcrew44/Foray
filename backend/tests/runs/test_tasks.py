"""The task registry and the procrastinate behaviour the design relies on (design §9.1–9.3,
§15.4; R11).

§15.4 assumptions checked here, against the installed procrastinate 3.10 and the real database:
- `context.job.attempts` is 0 on the first attempt and 1 on the retry;
- `shutdown_graceful_timeout` (`--shutdown-timeout`) cancels a job still running after N s;
- `procrastinate_cancel_job_v1(id, abort, delete_job)` exists with that signature;
- `max_delay = 6 h` gives exactly one catch-up deferral after a stop ("missed once");
- a `chat.follow_up`-style task (`shutdown_only_retry()`) is retried once after a shutdown abort
  and not after an ordinary exception.
(The stalled-jobs API is checked in `test_reaper.py`.)
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from typing import Any, cast

import pytest
from django.conf import settings
from django.contrib.sessions.models import Session
from django.utils import timezone
from procrastinate.periodic import PeriodicDeferrer, PeriodicRegistry
from procrastinate.retry import RetryStrategy

from apps.core.db import db
from apps.core.models import RateLimit
from apps.core.queue import app, defer_in_tx
from apps.runs import lifecycle, periodic, step
from apps.runs import tasks as run_tasks
from apps.runs.management.commands import worker as worker_command
from apps.runs.step import TASKS
from tests.factories import make_approval, make_event, make_run, make_user
from tests.queue import run_deferred, worker_connector
from tests.runs import support
from tests.runs.support import event_kinds, jobs, refresh, running_worker, wait_for

# Tasks that may retry, and how (R11): `resume.render` (no model call) retries once; the
# maintenance tasks and the ping may retry but do not; `chat.follow_up` retries only a shutdown.
MAY_RETRY = {"resume.render", "worker.ping", "maintenance.reap", "approvals.expire"}
MAY_RETRY |= {"maintenance.prune"}


def _ours(name: str) -> bool:
    return not name.startswith(("tests.", "builtin:", "procrastinate."))


def test_money_tasks_never_retry() -> None:
    checked = []
    for name, task in app.tasks.items():
        if not _ours(name):
            continue
        strategy = task.retry_strategy
        if name == "chat.follow_up":
            assert isinstance(strategy, RetryStrategy), name
            assert strategy.max_attempts == 1
            assert set(strategy.retry_exceptions or ()) == {asyncio.CancelledError}
        elif name == "resume.render":
            assert isinstance(strategy, RetryStrategy) and strategy.max_attempts == 1, name
        elif name not in MAY_RETRY:
            assert strategy is None, f"{name} can call a model and must not retry"
        checked.append(name)
    assert {"worker.ping", "maintenance.reap", "approvals.expire", "maintenance.prune"} <= set(
        checked
    )
    follow_up = step.shutdown_only_retry()
    assert follow_up.max_attempts == 1
    assert set(follow_up.retry_exceptions or ()) == {asyncio.CancelledError}


def test_every_task_sits_on_its_lane() -> None:
    for name, task in app.tasks.items():
        if not _ours(name):
            continue
        assert name in TASKS, f"{name} is not in the task table (runs/step.py TASKS)"
        assert task.queue == TASKS[name].lane, name
    assert set(step.LANES) == {spec.lane for spec in TASKS.values()}


def test_no_periodic_task_holds_a_lock() -> None:
    for entry in periodic.SCHEDULE:
        assert entry.task in TASKS and TASKS[entry.task].lane in step.LANES
        assert entry.queueing_lock
        task = app.tasks.get(entry.task)
        if task is not None:
            assert task.lock is None, entry.task
    registry = PeriodicRegistry()

    class Stub:
        periodic_registry = registry
        tasks = app.tasks

    installed = periodic.install(cast(Any, Stub()))
    assert set(installed) >= {"maintenance.reap", "approvals.expire", "maintenance.prune"}
    for periodic_task in registry.periodic_tasks.values():
        assert "lock" not in periodic_task.configure_kwargs
        assert periodic_task.configure_kwargs.get("queueing_lock")
    assert periodic.install(cast(Any, Stub())) == []  # already installed: nothing twice


def test_placeholders_keep_the_task_table_names_and_lanes() -> None:
    for name in ("search.evaluate", "search.shortlist", "chat.follow_up"):
        task = step.task_for(name)
        assert (task.name, task.queue) == (name, TASKS[name].lane)
    with pytest.raises(KeyError):
        step.task_for("no.such.task")


@pytest.mark.django_db(transaction=True)
def test_retry_one_step_keeps_the_run_running_across_the_retry() -> None:
    """§12.3: a retry-1 task (`resume.render`) re-raises while attempts remain, so procrastinate
    retries the same job and the run stays running; `attempts` is 0, then 1 (§15.4)."""
    user = make_user()
    run, _ = lifecycle.start_run(
        user.pk,
        "resume_render",
        input={"resumeId": "r", "prompt": "Render"},
        dedupe_key="render:r",
        task=support.retry_step,
    )
    run_deferred(["interactive"])
    calls = [c for c in support.CALLS if c["task"] == "tests.retry_step"]
    assert [c["status"] for c in calls] == ["running", "running"]
    run = refresh(run)
    assert run.status == "completed"
    assert event_kinds(run.id) == ["run.completed"]  # no run.failed in between
    [job] = jobs(lock=f"run:{run.id}")
    assert (job["status"], job["attempts"]) == ("succeeded", 2)


@pytest.mark.django_db(transaction=True)
def test_retry_one_step_fails_the_run_after_its_last_attempt() -> None:
    user = make_user()
    run, _ = lifecycle.start_run(
        user.pk,
        "resume_render",
        input={},
        dedupe_key="render:r2",
        task=support.retry_step,
        task_kwargs={"failures": 5},
    )
    run_deferred(["interactive"])
    run = refresh(run)
    assert (run.status, run.error) == ("failed", "Could not render the PDF: attempt 1 failed")
    assert event_kinds(run.id) == ["run.failed"]


@pytest.mark.django_db(transaction=True)
def test_job_attempts_start_at_zero() -> None:
    defer_in_tx(support.follow_up_like, action="raise")
    run_deferred(["interactive"])
    [call] = [c for c in support.CALLS if c["task"] == "tests.follow_up_like"]
    assert call["attempts"] == 0


async def _status(job_id: int) -> str | None:
    return await db(support.job_status, job_id)


@pytest.mark.django_db(transaction=True)
async def test_shutdown_timeout_cancels_a_job_still_running() -> None:
    """`manage.py worker --shutdown-timeout N`: on SIGTERM a running job gets N seconds, then
    its asyncio task is cancelled and the job ends `aborted`."""
    job_id = await db(defer_in_tx, run_tasks.debug_sleep, lock="debug", seconds=60)
    stop = asyncio.Event()
    with support.empty_periodic_registry():
        serving = asyncio.create_task(
            worker_command.serve(1, stop=stop, install_signal_handlers=False)
        )
        await wait_for(lambda: support.job_status(job_id) == "doing")
        started = time.monotonic()
        stop.set()
        code = await asyncio.wait_for(serving, 20)
    elapsed = time.monotonic() - started
    assert code == 0
    assert await _status(job_id) == "aborted"
    assert 0.9 <= elapsed < 8, elapsed


@pytest.mark.django_db(transaction=True)
async def test_cancel_job_v1_and_stop_abort_a_running_step() -> None:
    """`procrastinate_cancel_job_v1(id, true, false)` cancels a queued job outright and asks a
    running one to abort; an async step is then cancelled (its run already reads cancelled)."""
    user = await db(make_user)
    run, _ = await db(
        lifecycle.start_run,
        user.pk,
        "search",
        input={},
        dedupe_key="search",
        task=support.step,
        task_kwargs={"action": "sleep", "seconds": 60},
    )
    async with running_worker(["background"]):
        [job] = await db(jobs, lock=f"run:{run.id}")
        await wait_for(lambda: support.job_status(job["id"]) == "doing")
        assert await db(lifecycle.cancel, user.pk, run.id)
        await wait_for(lambda: support.job_status(job["id"]) == "aborted")
    run = await db(refresh, run)
    assert run.status == "cancelled"
    assert await db(event_kinds, run.id) == ["run.cancelled"]

    queued = await db(defer_in_tx, run_tasks.debug_sleep, lock="x", seconds=1)
    [(result,)] = await db(
        support.sql, "SELECT procrastinate_cancel_job_v1(%s, true, false)", [queued]
    )
    assert result == queued
    assert await _status(queued) == "cancelled"


@pytest.mark.django_db(transaction=True)
async def test_follow_up_style_task_is_retried_once_after_a_shutdown_only() -> None:
    """§7.7: `shutdown_only_retry()` retries a job a worker shutdown aborted (procrastinate hands
    the `CancelledError` to the strategy), once; an ordinary exception is not retried."""
    job_id = await db(defer_in_tx, support.follow_up_like, seconds=60)
    async with running_worker(["interactive"], shutdown_timeout=0.2):
        await wait_for(lambda: support.job_status(job_id) == "doing")
    # The shutdown aborted it and the strategy put it back: todo, attempts 1.
    [job] = await db(jobs, task_name="tests.follow_up_like")
    assert (job["status"], job["attempts"]) == ("todo", 1)

    async with running_worker(["interactive"], shutdown_timeout=0.2):
        await wait_for(lambda: support.job_status(job_id) == "doing")
    [job] = await db(jobs, task_name="tests.follow_up_like")
    assert (job["status"], job["attempts"]) == ("aborted", 2)  # once only
    assert [c["attempts"] for c in support.CALLS] == [0, 1]

    failing = await db(defer_in_tx, support.follow_up_like, action="raise")
    async with running_worker(["interactive"]):
        await wait_for(lambda: support.job_status(failing) == "failed")
    await asyncio.sleep(0.3)
    assert await _status(failing) == "failed"  # not retried


@pytest.mark.django_db(transaction=True)
async def test_max_delay_catches_up_once() -> None:
    """§15.4: with `max_delay = 6 h`, a worker that starts after a stop defers only the latest
    missed tick of each periodic task: one catch-up for a minutely task stopped 3 minutes, and
    one for the 6-hourly cron 5 h after its tick (procrastinate's default 600 s would skip it)."""
    assert dict(app.periodic_defaults) == {"max_delay": 6 * 3600}
    assert settings.PROCRASTINATE_PERIODIC_DEFAULTS == {"max_delay": 6 * 3600}
    registry = PeriodicRegistry()
    tick_task = cast(Any, support.sleep_task)  # periodic tasks take a `timestamp` keyword
    minutely = registry.register_task(
        task=tick_task, cron="* * * * *", periodic_id="tick", configure_kwargs={}
    )
    six_hourly = registry.register_task(
        task=tick_task, cron="0 */6 * * *", periodic_id="cron", configure_kwargs={}
    )

    def deferrer() -> PeriodicDeferrer:  # a fresh process: nothing deferred yet in memory
        return PeriodicDeferrer(registry=registry, **app.periodic_defaults)

    start = 1_790_000_000 // 3600 * 3600 + 5  # 5 s past a whole hour (and minute)
    first = list(deferrer().get_previous_tasks(at=start))
    assert [(pt.periodic_id, ts) for pt, ts in first if pt is minutely] == [("tick", start - 5)]

    restarted = list(deferrer().get_previous_tasks(at=start + 180))  # stopped for 3 minutes
    assert [ts for pt, ts in restarted if pt is minutely] == [start + 180 - 5]

    tick = start // (6 * 3600) * (6 * 3600)
    late = list(deferrer().get_previous_tasks(at=tick + 5 * 3600))
    assert [ts for pt, ts in late if pt is six_hourly] == [tick]
    short = PeriodicDeferrer(registry=registry)  # procrastinate's default max_delay (600 s)
    assert [ts for pt, ts in short.get_previous_tasks(at=tick + 5 * 3600) if pt is six_hourly] == []

    # Against the database: deferring the restart's ticks writes one job per task, and a second
    # worker deferring the same tick (or this one again) writes nothing more.
    with app.replace_connector(worker_connector()):
        async with app.open_async():
            await deferrer().defer_jobs(iter(restarted))
            await deferrer().defer_jobs(iter(list(deferrer().get_previous_tasks(at=start + 185))))
    deferred = await db(jobs, task_name="tests.sleep")
    assert len(deferred) == len({pt.periodic_id for pt, _ in restarted})
    assert all(job["args"]["timestamp"] for job in deferred)


# --- the maintenance tasks -----------------------------------------------------------------------


def _finished_job(status: str, days_ago: int) -> int:
    """A job that finished `days_ago` days ago: procrastinate dates a job by its latest event,
    which its triggers write on insert and on each status change."""
    [(job_id,)] = support.sql(
        "INSERT INTO procrastinate_jobs (queue_name, task_name, args) "
        "VALUES ('system', 'tests.sleep', '{}') RETURNING id"
    )
    support.sql(
        "UPDATE procrastinate_jobs SET status = %s::procrastinate_job_status WHERE id = %s",
        [status, job_id],
    )
    support.sql(
        "UPDATE procrastinate_events SET at = now() - make_interval(days => %s) WHERE job_id = %s",
        [days_ago, job_id],
    )
    return job_id


@pytest.mark.django_db(transaction=True)
def test_prune_drops_old_queue_jobs_sessions_and_throttle_counters() -> None:
    """§9.3: queue jobs finished 14+ days ago (succeeded, failed, cancelled, aborted), expired
    sessions, and throttle counters over a day old (`throttle.prune()`, the 3A handoff). Nothing
    recent, and no product data."""
    old = [_finished_job(status, 15) for status in ("succeeded", "failed", "cancelled", "aborted")]
    recent = _finished_job("succeeded", 3)
    now = timezone.now()
    Session.objects.create(session_key="expired-1", session_data="", expire_date=now)
    Session.objects.create(session_key="live-1", session_data="", expire_date=now + timedelta(1))
    RateLimit.objects.create(key="chat:u", window_start=now - timedelta(days=2), count=3)
    RateLimit.objects.create(key="chat:u", window_start=now, count=1)
    user = make_user()
    event = make_event(user, kind="run.completed", run=make_run(user, status="completed"))

    defer_in_tx(run_tasks.maintenance_prune, queueing_lock="maintenance:prune")
    run_deferred(["system"])

    remaining = {job["id"] for job in jobs()}
    assert not remaining & set(old)
    assert recent in remaining
    assert list(Session.objects.values_list("session_key", flat=True)) == ["live-1"]
    assert list(RateLimit.objects.values_list("window_start", flat=True)) == [now]
    assert event_kinds(event.run_id) == ["run.completed"]  # product history is kept


@pytest.mark.django_db(transaction=True)
def test_expire_task_runs_the_sweep() -> None:
    user = make_user()
    run = make_run(user, status="awaiting_approval")
    approval = make_approval(user, run=run, expires_at=timezone.now() - timedelta(minutes=1))
    defer_in_tx(run_tasks.approvals_expire, queueing_lock="approvals:expire")
    run_deferred(["system"])
    assert refresh(run).status == "running"
    assert event_kinds(run.id) == ["approval.expired"]
    [continuation] = jobs(lock=f"run:{run.id}")
    assert continuation["queueing_lock"] == f"approval:{approval.id}"
