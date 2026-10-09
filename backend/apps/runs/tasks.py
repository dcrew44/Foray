"""The system lane's tasks (design §9.2, §9.3, §9.4). None of them works on a run.

| Task | Does |
|---|---|
| `worker.ping` | emits `worker.ping` `{jobId, workerPid}` for the user who asked (Settings' Test) |
| `maintenance.reap` | fails stalled jobs, then the runs no live job holds (`reaper.py`) |
| `approvals.expire` | lapses approvals past their 7 days (`approvals.expire_lapsed`) |
| `maintenance.prune` | drops queue jobs done 14+ days ago, expired sessions, day-old throttles |
| `debug.sleep` | sleeps holding `lock="debug"`: the lock-recovery probe (`manage.py debug_sleep`) |

The periodic schedule lives in `runs/periodic.py` and is installed by `manage.py worker`; a
periodic task receives the tick it was deferred for as `timestamp`. Defers go through
`apps.core.queue.defer_in_tx`; product data (events, ledger, runs, messages) is never pruned.
"""

from __future__ import annotations

import asyncio
import logging
import os
from importlib import import_module
from typing import Any

from django.conf import settings
from procrastinate import JobContext

from apps.core import throttle
from apps.core.db import db
from apps.core.queue import app
from apps.runs import approvals, events, reaper
from apps.runs.catalogue import EventKind, WorkerPingData
from apps.runs.step import SYSTEM, TASKS

log = logging.getLogger("foray.worker")

PING_DEFAULT = "Worker is alive."
PRUNE_AFTER_HOURS = 14 * 24


def _timeout(name: str) -> int:
    return TASKS[name].timeout_s


@app.task(name="worker.ping", queue=SYSTEM, pass_context=True)
async def worker_ping(context: JobContext, user_id: str, note: str | None = None) -> None:
    """Answer a test job from Settings through the same feed every run uses."""
    job_id = context.job.id
    assert job_id is not None  # a job a worker fetched has its row
    async with asyncio.timeout(_timeout("worker.ping")):
        await db(
            events.emit,
            user_id,
            EventKind.WORKER_PING,
            note or PING_DEFAULT,
            data=WorkerPingData(job_id=job_id, worker_pid=os.getpid()).dump(),
        )
    log.info("answered ping job %s", job_id)


@app.task(name="maintenance.reap", queue=SYSTEM)
async def maintenance_reap(timestamp: int | None = None) -> None:
    async with asyncio.timeout(_timeout("maintenance.reap")):
        stalled = await reaper.fail_stalled_jobs()
        result = await db(reaper.reap_runs)
    if stalled or result.interrupted or result.lost:
        log.info(
            "reap: %s stalled job(s) failed, %s interrupted run(s), %s lost continuation(s)",
            len(stalled),
            result.interrupted,
            result.lost,
        )


@app.task(name="approvals.expire", queue=SYSTEM)
async def approvals_expire(timestamp: int | None = None) -> None:
    async with asyncio.timeout(_timeout("approvals.expire")):
        await db(approvals.expire_lapsed)


def _clear_sessions() -> None:
    engine: Any = import_module(settings.SESSION_ENGINE)
    engine.SessionStore.clear_expired()


@app.task(name="maintenance.prune", queue=SYSTEM)
async def maintenance_prune(timestamp: int | None = None) -> None:
    """Infrastructure tables only (§9.3); retention of product data is a Phase 4 decision."""
    async with asyncio.timeout(_timeout("maintenance.prune")):
        await app.job_manager.delete_old_jobs(
            nb_hours=PRUNE_AFTER_HOURS,
            include_failed=True,
            include_cancelled=True,
            include_aborted=True,
        )
        await db(_clear_sessions)
        counters = await db(throttle.prune)
    log.info("prune: done (%s throttle counter(s) dropped)", counters)


@app.task(name="debug.sleep", queue=SYSTEM)
async def debug_sleep(seconds: float) -> None:
    """Hold `lock="debug"` for `seconds` (no run, no events)."""
    log.info("debug.sleep: sleeping %ss", seconds)
    await asyncio.sleep(seconds)
    log.info("debug.sleep: done")
