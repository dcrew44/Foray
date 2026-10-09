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
from dataclasses import dataclass

from django.db import connection, transaction
from procrastinate import jobs

from apps.core.queue import app
from apps.runs import lifecycle

__all__ = [
    "INTERRUPTED",
    "LOST",
    "STALL_SECONDS",
    "ReapResult",
    "fail_stalled_jobs",
    "reap_runs",
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


@dataclass(frozen=True, slots=True)
class ReapResult:
    interrupted: int
    lost: int


async def fail_stalled_jobs() -> list[int]:
    """Finish every stalled `doing` job as `failed` (never retried). Needs the worker connector
    (the jobs API is async). Returns the ids it failed."""
    stalled = await app.job_manager.get_stalled_jobs(seconds_since_heartbeat=STALL_SECONDS)
    failed: list[int] = []
    for job in stalled:
        assert job.id is not None
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
