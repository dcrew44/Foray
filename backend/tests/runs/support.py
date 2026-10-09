"""Test tasks and helpers for the runs tests (imported by the test modules and `conftest.py`).

The tasks are registered on the real procrastinate app under `tests.*` names, so the R11 and
lane checks of `test_tasks.py` skip them:

- `tests.step` (background, retry 0) does what its `action` keyword says: complete, fail with a
  user-facing error, raise, run out of budget, time out, raise a gate, sleep, or return without
  finishing (`forget`);
- `tests.retry_step` (interactive, retry 1, like `resume.render`) fails its first attempt;
- `tests.sleep` (a plain task, no run) sleeps;
- `tests.follow_up_like` (interactive, `shutdown_only_retry()`, like `chat.follow_up`) sleeps or
  raises.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager, suppress
from decimal import Decimal
from typing import Any

from django.db import connection, connections

from apps.core.db import db
from apps.core.errors import UserFacingError
from apps.core.queue import app
from apps.llm.budget import BudgetExceeded
from apps.runs import approvals, lifecycle
from apps.runs.catalogue import ApprovalKind
from apps.runs.models import Event, Run
from apps.runs.step import BACKGROUND, INTERACTIVE, run_step, shutdown_only_retry
from tests.queue import worker_connector

CALLS: list[dict[str, Any]] = []


def batch_payload(n: int = 3) -> dict[str, Any]:
    names = ["Acme", "Beta", "Gamma", "Delta", "Epsilon"]
    return {
        "items": [
            {
                "matchId": f"00000000-0000-4000-8000-00000000000{i}",
                "jobId": f"greenhouse:{names[i].lower()}:{i}",
                "title": "Engineer",
                "company": names[i],
                "fastScore": 80 - i,
            }
            for i in range(n)
        ],
        "estimate": {"usd": 0.15, "perItemUsd": 0.05, "model": "openai:gpt-5.6-terra"},
        "note": None,
    }


@run_step("tests.step", queue=BACKGROUND, timeout_s=120)
async def step(run: Run, action: str = "forget", seconds: float = 0) -> None:
    CALLS.append({"task": "tests.step", "action": action, "run": str(run.id)})
    match action:
        case "complete":
            await db(lifecycle.complete, run.id, {"done": True}, "Done.")
        case "user_error":
            raise UserFacingError("That board is gone.")
        case "raise":
            raise ValueError("boom\nsecond line")
        case "budget":
            raise BudgetExceeded(Decimal(90))
        case "timeout":
            raise TimeoutError
        case "gate":
            await db(
                approvals.request_approval,
                run.id,
                ApprovalKind.DEEP_EVAL_BATCH,
                "Deep-evaluate 3 roles",
                batch_payload(),
                stats={"phase": "awaiting_evaluation", "toEvaluate": 3},
            )
        case "sleep":
            await asyncio.sleep(seconds)
        case "forget":
            pass
        case _:
            raise AssertionError(action)


@run_step("tests.retry_step", queue=INTERACTIVE, timeout_s=600, retry=1)
async def retry_step(run: Run, failures: int = 1) -> None:
    current = await db(lambda: Run.objects.unscoped("tests").get(id=run.id))
    attempt = len([c for c in CALLS if c["task"] == "tests.retry_step" and c["run"] == str(run.id)])
    CALLS.append({"task": "tests.retry_step", "run": str(run.id), "status": current.status})
    if attempt < failures:
        raise ValueError(f"attempt {attempt} failed")
    await db(lifecycle.complete, run.id, {"pages": 1}, "Rendered.")


@app.task(name="tests.sleep", queue="system")
async def sleep_task(seconds: float) -> None:
    await asyncio.sleep(seconds)


@app.task(
    name="tests.follow_up_like",
    queue=INTERACTIVE,
    retry=shutdown_only_retry(),
    pass_context=True,
)
async def follow_up_like(context: Any, action: str = "sleep", seconds: float = 30) -> None:
    CALLS.append({"task": "tests.follow_up_like", "attempts": context.job.attempts})
    if action == "raise":
        raise ValueError("an ordinary failure")
    await asyncio.sleep(seconds)


# --- database helpers ----------------------------------------------------------------------------


def sql(query: str, params: list[Any] | None = None) -> list[tuple[Any, ...]]:
    with connection.cursor() as cursor:
        cursor.execute(query, params or [])
        return cursor.fetchall() if cursor.description else []


def jobs(*, lock: str | None = None, task_name: str | None = None) -> list[dict[str, Any]]:
    """Every procrastinate job (any status) visible on this thread's connection, oldest first."""
    where, params = ["true"], []
    if lock is not None:
        where.append("lock = %s")
        params.append(lock)
    if task_name is not None:
        where.append("task_name = %s")
        params.append(task_name)
    rows = sql(
        "SELECT id, task_name, queue_name, status::text, lock, queueing_lock, args, attempts, "
        f"abort_requested FROM procrastinate_jobs WHERE {' AND '.join(where)} ORDER BY id",
        params,
    )
    keys = ("id", "task_name", "queue", "status", "lock", "queueing_lock", "args", "attempts")
    found = []
    for row in rows:
        job: dict[str, Any] = dict(zip(keys, row[:8], strict=True))
        job["abort_requested"] = row[8]
        if isinstance(job["args"], str):
            job["args"] = json.loads(job["args"])
        found.append(job)
    return found


def job_status(job_id: int) -> str | None:
    rows = sql("SELECT status::text FROM procrastinate_jobs WHERE id = %s", [job_id])
    return rows[0][0] if rows else None


def in_another_session[T](fn: Callable[[], T]) -> T:
    """Run `fn` on another thread, so on another pooled connection (a different session)."""

    def call() -> T:
        try:
            return fn()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(call).result()


def job_seen_elsewhere(job_id: int) -> bool:
    return in_another_session(lambda: job_status(job_id) is not None)


def age_run(run_id: Any, seconds: int) -> None:
    sql(
        "UPDATE runs SET updated_at = now() - make_interval(secs => %s) WHERE id = %s",
        [seconds, str(run_id)],
    )


def dead_worker(seconds_ago: int = 300) -> int:
    [(worker_id,)] = sql(
        "INSERT INTO procrastinate_workers (last_heartbeat) "
        "VALUES (now() - make_interval(secs => %s)) RETURNING id",
        [seconds_ago],
    )
    return worker_id


def doing_job(
    task_name: str,
    *,
    worker_id: int | None,
    lock: str | None = None,
    queue: str = "background",
    args: dict[str, Any] | None = None,
) -> int:
    """A job left `doing`, as a killed worker leaves it."""
    [(job_id,)] = sql(
        "INSERT INTO procrastinate_jobs (queue_name, task_name, lock, args, status, worker_id) "
        "VALUES (%s, %s, %s, %s::jsonb, 'doing', %s) RETURNING id",
        [queue, task_name, lock, json.dumps(args or {}), worker_id],
    )
    return job_id


def event_kinds(run_id: Any) -> list[str]:
    return list(
        Event.objects.unscoped("tests")
        .filter(run_id=run_id)
        .order_by("id")
        .values_list("kind", flat=True)
    )


def refresh(run: Run) -> Run:
    return Run.objects.unscoped("tests").get(id=run.id)


# --- running a real worker in a test -------------------------------------------------------------


@contextmanager
def empty_periodic_registry() -> Iterator[None]:
    """Keep the test's Workers from deferring the real schedule (`manage.py worker` installs it
    on the global app)."""
    registry = app.periodic_registry
    saved = registry.periodic_tasks
    registry.periodic_tasks = {}
    try:
        yield
    finally:
        registry.periodic_tasks = saved


@asynccontextmanager
async def running_worker(
    queues: list[str] | None = None, *, shutdown_timeout: float = 0.5
) -> AsyncIterator[asyncio.Task[None]]:
    """A real Worker on the worker connector, running in the background while the block runs,
    stopped (gracefully, then by cancelling its jobs) when it exits."""
    with app.replace_connector(worker_connector()), empty_periodic_registry():
        async with app.open_async():
            options: dict[str, Any] = {} if queues is None else {"queues": queues}
            task = asyncio.create_task(
                app.run_worker_async(
                    **options,
                    install_signal_handlers=False,
                    shutdown_graceful_timeout=shutdown_timeout,
                    fetch_job_polling_interval=0.2,
                    abort_job_polling_interval=0.2,
                )
            )
            try:
                yield task
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task


async def wait_for(check: Callable[[], Any], within: float = 10.0) -> Any:
    """Poll a sync check through `db()` until it returns something truthy."""
    deadline = time.monotonic() + within
    while True:
        value = await db(check)
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {check}")
        await asyncio.sleep(0.05)
