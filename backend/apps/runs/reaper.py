"""The reaper (design §9.3, §9.8; R3, R11).

Liveness is a property of the job table, not of a process: every job that works on a run holds
the procrastinate lock `run:<id>`, so a run is alive exactly while a `todo`/`doing` job holds it.
That makes several workers, `--reload` and rolling restarts safe (bugs #1, #47).

`maintenance.reap` (every minute) does two things:

1. **Stalled jobs.** A job left `doing` by a killed worker (no heartbeat for 30 s, or its worker
   row already pruned) is finished `failed`, never retried: a model job must not re-spend (R11).
   This also releases its `lock`, which procrastinate would otherwise hold forever. The worker
   runs the same sweep once at startup (`manage.py worker`), so recovery never depends on a reap
   job being fetchable.

   A heartbeat is only evidence about *another* process. Foray's worker runs on a laptop, and a
   sleep (or a stopped debugger, or the Docker VM's clock catching up) leaves every heartbeat
   minutes old while the process is fine: the first reap after waking used to fail the search,
   the tailor and the sibling reaps this very worker was running, and then the runs as
   "Interrupted" (step 13). So the sweep (a) never fails a job this process is running right
   now, even one whose worker row a restarted lane pruned (`track_running`, the worker
   middleware `manage.py worker` installs, keeps that set), and (b) is skipped while one of
   this process's own workers has a stale heartbeat: the whole machine was paused, so other
   workers' heartbeats are stale for the same reason. The heartbeats catch up within 10 s and
   the next reap, a minute later, judges again.
2. **Runs.** Each candidate is failed in its own transaction (`FOR UPDATE … SKIP LOCKED`):
   - an active (`queued`/`running`) run untouched for 60 s that no live job holds:
     "Interrupted: the worker stopped while this was running. Start it again to retry.";
   - an `awaiting_approval` run untouched for 10 minutes with no pending approval and no live job
     (its continuation was lost): "Its next step was lost. Start it again."

   `lifecycle.fail` emits `run.failed`, which toasts and follows up in chat.

Detection time for a killed worker's run: 30 s stall detection, plus the 60 s margin, plus the
1-minute period, so within about 2 minutes.

No periodic task holds a `lock`: if the reaper did, a kill during a reap would leave a `doing`
reap job that only a reap could fail. Overlapping reaps are harmless, since every step is
conditional and row-locked.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from django.db import connection, transaction
from procrastinate import JobContext, jobs
from procrastinate.worker import Worker

from apps.core.queue import app
from apps.runs import lifecycle

__all__ = [
    "INTERRUPTED",
    "LOST",
    "STALL_SECONDS",
    "ReapResult",
    "fail_stalled_jobs",
    "reap_runs",
    "running_here",
    "track_running",
]

log = logging.getLogger("foray.worker")

STALL_SECONDS = 30
INTERRUPTED = "Interrupted: the worker stopped while this was running. Start it again to retry."
LOST = "Its next step was lost. Start it again."

_NO_LIVE_JOB = """
NOT EXISTS (SELECT 1 FROM procrastinate_jobs j
             WHERE j.lock = 'run:' || r.id::text AND j.status IN ('todo', 'doing'))
"""

ORPHANED = f"""
SELECT r.id FROM runs r
 WHERE r.status IN ('queued', 'running')
   AND r.updated_at < now() - interval '60 seconds'
   AND {_NO_LIVE_JOB}
"""

LOST_CONTINUATIONS = f"""
SELECT r.id FROM runs r
 WHERE r.status = 'awaiting_approval' AND r.updated_at < now() - interval '10 minutes'
   AND NOT EXISTS (SELECT 1 FROM approvals a WHERE a.run_id = r.id AND a.status = 'pending')
   AND {_NO_LIVE_JOB}
"""


_OWN_HEARTBEAT_STALE = """
SELECT EXISTS (SELECT 1 FROM procrastinate_workers
                WHERE id = ANY(%(worker_ids)s)
                  AND last_heartbeat < now() - make_interval(secs => %(seconds)s)) AS stale
"""

# The jobs this process is running right now: job id → the id of the procrastinate worker (lane)
# that fetched it. Only `track_running` writes it, on the worker's event loop.
_RUNNING: dict[int, int | None] = {}


@dataclass(frozen=True, slots=True)
class ReapResult:
    interrupted: int
    lost: int


async def track_running(
    call_next: Callable[[], Awaitable[Any]], context: JobContext, worker: Worker
) -> Any:
    """Worker middleware (`manage.py worker` passes it to every lane): records the job while it
    runs in this process, so the stalled-job sweep can tell it is alive."""
    job_id = context.job.id
    assert job_id is not None  # a job a worker fetched has its row
    _RUNNING[job_id] = worker.worker_id
    try:
        return await call_next()
    finally:
        _RUNNING.pop(job_id, None)


def running_here() -> dict[int, int | None]:
    """A copy of the jobs this process is running (see `track_running`)."""
    return dict(_RUNNING)


async def _own_heartbeat_is_stale(worker_ids: list[int]) -> bool:
    row = await app.job_manager.connector.execute_query_one_async(
        query=_OWN_HEARTBEAT_STALE, worker_ids=worker_ids, seconds=STALL_SECONDS
    )
    return bool(row["stale"])


async def fail_stalled_jobs() -> list[int]:
    """Finish every stalled `doing` job as `failed` (never retried), except the ones this process
    is running, and nothing while this process's own heartbeat is stale (see the docstring).
    Needs the worker connector (the jobs API is async). Returns the ids it failed."""
    running = running_here()
    own_workers = sorted({worker_id for worker_id in running.values() if worker_id is not None})
    if own_workers and await _own_heartbeat_is_stale(own_workers):
        log.warning(
            "stalled-job sweep skipped: this worker's own heartbeat is over %ss old, so the "
            "machine was paused (asleep?); the next reap judges again",
            STALL_SECONDS,
        )
        return []
    stalled = await app.job_manager.get_stalled_jobs(seconds_since_heartbeat=STALL_SECONDS)
    failed: list[int] = []
    for job in stalled:
        assert job.id is not None
        if job.id in running:
            continue  # this process is running it: alive, whatever its heartbeat says
        try:
            await app.job_manager.finish_job_by_id_async(
                job_id=job.id, status=jobs.Status.FAILED, delete_job=False
            )
        except Exception:  # finished meanwhile by its own worker, or by another reap
            log.warning("could not fail stalled job %s (%s)", job.id, job.task_name, exc_info=True)
            continue
        failed.append(job.id)
        log.warning("failed stalled job %s (%s, lock %s)", job.id, job.task_name, job.lock)
    return failed


def _reap(candidates_sql: str, message: str) -> int:
    with connection.cursor() as cursor:
        cursor.execute(candidates_sql)
        ids = [row[0] for row in cursor.fetchall()]
    reaped = 0
    for run_id in ids:
        with transaction.atomic():
            with connection.cursor() as cursor:
                # Still a candidate, and nobody else is touching it: checked again under the lock.
                cursor.execute(
                    candidates_sql + " AND r.id = %s FOR UPDATE OF r SKIP LOCKED", [run_id]
                )
                if cursor.fetchone() is None:
                    continue
            if lifecycle.fail(run_id, message):
                reaped += 1
                log.warning("reaped run %s: %s", run_id, message)
    return reaped


def reap_runs() -> ReapResult:
    """Sync (through `db()`): fail every active run no live job holds (see the docstring)."""
    return ReapResult(
        interrupted=_reap(ORPHANED, INTERRUPTED), lost=_reap(LOST_CONTINUATIONS, LOST)
    )
