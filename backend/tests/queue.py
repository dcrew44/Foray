"""The real job table in tests, no in-memory connector (design §12.1).

Every defer goes through `defer_in_tx` on Django's connection, so jobs land in the test
database's `procrastinate_jobs`, inside the test's transaction, exactly as in production.

- `deferred(task_name=None)`: the `todo` jobs, for assertions.
- `run_deferred(queues=None)` / `await arun_deferred(queues=None)`: run a real procrastinate
  Worker in-process with the worker connector until every runnable job is done (locks
  honoured), then return. Tests that use them need `django_db(transaction=True)`: the Worker and
  `db()` threads use their own connections, so they only see committed rows.
- `clear_queue()`: empty procrastinate's tables. Its models are unmanaged, so a transactional
  test's flush leaves them alone; `tests/conftest.py` calls this around every transactional test.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from django.db import connection
from procrastinate.connector import BaseAsyncConnector
from procrastinate.contrib.django.django_connector import DjangoConnector

from apps.core.queue import app


@dataclass(frozen=True)
class DeferredJob:
    id: int
    task_name: str
    queue: str
    args: dict[str, Any]
    lock: str | None
    queueing_lock: str | None


QUEUE_TABLES = (
    "procrastinate_events",
    "procrastinate_periodic_defers",
    "procrastinate_jobs",
    "procrastinate_workers",
)


def clear_queue() -> None:
    with connection.cursor() as cursor:
        cursor.execute(f"TRUNCATE {', '.join(QUEUE_TABLES)} RESTART IDENTITY CASCADE")


def deferred(task_name: str | None = None) -> list[DeferredJob]:
    """The `todo` jobs visible on this thread's connection, oldest first."""
    sql = (
        "SELECT id, task_name, queue_name, args, lock, queueing_lock FROM procrastinate_jobs "
        "WHERE status = 'todo'"
    )
    params: list[Any] = []
    if task_name is not None:
        sql += " AND task_name = %s"
        params.append(task_name)
    with connection.cursor() as cursor:
        cursor.execute(sql + " ORDER BY id", params)
        rows = cursor.fetchall()
    # Django's psycopg setup hands jsonb back as text (JSONField decodes it), so decode here.
    return [
        DeferredJob(id_, name, queue, json.loads(args) if isinstance(args, str) else args, lk, qlk)
        for id_, name, queue, args, lk, qlk in rows
    ]


def worker_connector() -> BaseAsyncConnector:
    """The connector `manage.py worker` swaps in: a `PsycopgConnector` on the same database."""
    django_connector = app.connector
    assert isinstance(django_connector, DjangoConnector)
    return django_connector.get_worker_connector()


async def arun_deferred(queues: Iterable[str] | None = None) -> None:
    """Process every runnable job with a real Worker on the worker connector, then return."""
    with app.replace_connector(worker_connector()):
        async with app.open_async():
            if queues is None:
                await app.run_worker_async(
                    wait=False, install_signal_handlers=False, listen_notify=False
                )
            else:
                await app.run_worker_async(
                    queues=list(queues),
                    wait=False,
                    install_signal_handlers=False,
                    listen_notify=False,
                )


def run_deferred(queues: Iterable[str] | None = None) -> None:
    """Sync form of `arun_deferred`, for sync tests (no event loop may be running)."""
    asyncio.run(arun_deferred(queues))
