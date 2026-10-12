"""`defer_in_tx`, the only way to defer a job, in every process (design §9.1, R1).

The worker swaps the app's connector for a `PsycopgConnector` (the Django connector cannot
LISTEN). After that swap a plain `task.defer()` commits the job on its own pooled connection,
before the caller's transaction does, and it survives a rollback. So every defer goes through a
`JobManager` that always uses Django's connection: the job is written inside the caller's
transaction, invisible to other sessions until it commits, and gone if it rolls back.
procrastinate's NOTIFY trigger fires at commit, so a worker never picks a job up before the rows
it refers to exist.

`tests/test_register.py` bans `.defer(`, `.defer_async(` and `defer_job` everywhere else. Async
callers use `await db(defer_in_tx, task, ...)`.

A delay (`schedule_in`) is measured from the database's clock, the one `fetch_job` reads.
procrastinate would add it to Python's `utcnow()`, and the two clocks drift apart in development
(the Docker VM's clock falls behind the Mac's after a sleep): a 4 s wait then took 4 s plus the
skew, or none at all when the database's clock ran ahead.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from django.db import connection, transaction
from procrastinate import manager
from procrastinate.contrib.django import app
from procrastinate.contrib.django.django_connector import DjangoConnector
from procrastinate.tasks import Task
from procrastinate.types import TimeDeltaParams

__all__ = ["TX_JOBS", "app", "defer_in_tx"]

TX_JOBS = manager.JobManager(DjangoConnector())  # always Django's connection


def defer_in_tx(
    task: Task[Any, Any, Any],
    /,
    *,
    lock: str | None = None,
    queueing_lock: str | None = None,
    schedule_in: TimeDeltaParams | None = None,
    **kwargs: Any,
) -> int:
    """Sync. Writes the job on the calling thread's Django connection, inside the caller's
    transaction if there is one. Raises `AlreadyEnqueued` without poisoning that transaction.
    """
    schedule_at = None if schedule_in is None else _database_clock() + timedelta(**schedule_in)
    job = task.configure(
        lock=lock, queueing_lock=queueing_lock, schedule_at=schedule_at
    ).make_new_job(**kwargs)
    with transaction.atomic():  # a savepoint inside a caller's transaction
        job_id = TX_JOBS.defer_job(job).id
    assert job_id is not None
    return job_id


def _database_clock() -> datetime:
    """The database's current time (not the transaction's start), on Django's connection."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT clock_timestamp()")
        row = cursor.fetchone()
    assert row is not None
    return row[0]
