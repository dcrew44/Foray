"""The `run_step` wrapper, `defer_step` and the task table (design §9.1, §9.2).

Every job that works on a run carries `lock=f"run:{run_id}"`. That serialises a run's steps, so
an autopilot continuation never overlaps its parent, and it is the reaper's one liveness test
(R3). `defer_step` is the only way to defer such a job; it goes through `defer_in_tx`, so the job
commits with the caller's transaction or not at all (R1, R6).

`run_step(name, queue=..., timeout_s=...)` turns `async def step(run, **kwargs)` into a
procrastinate task that:

1. claims the run (`queued|running -> running`; a cancelled, finished or still-paused run is
   left alone, which also makes a redelivery a no-op);
2. attributes every model call to the run (`llm_context`);
3. runs the step under its own timeout (pg-boss's `expireInSeconds`);
4. turns a failure into `lifecycle.fail` with a user-facing sentence: the timeout sentence, the
   budget sentence, a `UserFacingError`'s text, or `FAILURE_PREFIX[kind] + <short error>`;
5. fails the run at once when the step returned without finishing it and no other job holds it
   (`lifecycle.fail_if_abandoned`, R4).

`CancelledError` (a worker shutdown, or Stop through `lifecycle.cancel`) is never caught: the job
ends aborted, and the reaper fails the run after a shutdown (after a Stop it is already
cancelled). A retry-1 task (`resume.render`) re-raises while attempts remain, so procrastinate
retries the same job and the run stays `running` (bug #50). Every retry here is immediate and
due by the database's clock (`ImmediateRetry`).

`TASKS` is the design's task table: every task's lane and timeout. `acceptance_checks` reads the
timeouts, `task_for()` reads the lanes, and `tests/runs/test_tasks.py` checks that every
registered task sits on its lane and that no task that can call a model retries (R11).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, cast

from procrastinate import JobContext, RetryStrategy
from procrastinate.jobs import Job
from procrastinate.retry import RetryDecision
from procrastinate.tasks import Task

from apps.core.db import db
from apps.core.errors import UserFacingError
from apps.core.queue import app, defer_in_tx
from apps.llm.budget import BudgetExceeded
from apps.llm.context import llm_context
from apps.runs import lifecycle
from apps.runs.catalogue import FAILURE_PREFIX
from apps.runs.models import Run

__all__ = [
    "BACKGROUND",
    "INTERACTIVE",
    "LANES",
    "SYSTEM",
    "TASKS",
    "ImmediateRetry",
    "defer_step",
    "run_lock",
    "run_step",
    "short",
    "shutdown_only_retry",
    "task_for",
]

log = logging.getLogger("foray.runs")

type Id = str | uuid.UUID
type AnyTask = Task[Any, Any, Any]

INTERACTIVE = "interactive"  # someone is watching a card
BACKGROUND = "background"  # minutes-long and bursty
SYSTEM = "system"  # liveness, expiry and the reaper never queue behind model work
LANES = (INTERACTIVE, BACKGROUND, SYSTEM)

MINUTE = 60


@dataclass(frozen=True, slots=True)
class TaskSpec:
    lane: str
    timeout_s: int


# The task table of §9.2 and §9.3. Owners register the tasks themselves; this is what they must
# match. `debug.sleep` is the lock-recovery probe of `manage.py debug_sleep` (DEBUG only).
TASKS: dict[str, TaskSpec] = {
    "search.run": TaskSpec(BACKGROUND, 60 * MINUTE),
    "search.evaluate": TaskSpec(BACKGROUND, 60 * MINUTE),
    "search.shortlist": TaskSpec(BACKGROUND, 10 * MINUTE),
    "match.evaluate": TaskSpec(INTERACTIVE, 30 * MINUTE),
    "resume.import": TaskSpec(INTERACTIVE, 10 * MINUTE),
    "resume.tailor": TaskSpec(INTERACTIVE, 30 * MINUTE),
    "resume.render": TaskSpec(INTERACTIVE, 10 * MINUTE),
    "company.add": TaskSpec(INTERACTIVE, 15 * MINUTE),
    "company.refresh": TaskSpec(BACKGROUND, 60 * MINUTE),
    "companies.refresh_scheduled": TaskSpec(BACKGROUND, 60 * MINUTE),
    "chat.follow_up": TaskSpec(INTERACTIVE, 3 * MINUTE),
    "worker.ping": TaskSpec(SYSTEM, 1 * MINUTE),
    "maintenance.reap": TaskSpec(SYSTEM, 5 * MINUTE),
    "approvals.expire": TaskSpec(SYSTEM, 5 * MINUTE),
    "maintenance.prune": TaskSpec(SYSTEM, 30 * MINUTE),
    "debug.sleep": TaskSpec(SYSTEM, 60 * MINUTE),
}

# Tasks that run without a run row: their names never appear in `FAILURE_PREFIX`.
SYSTEM_TASKS = frozenset(
    {
        "chat.follow_up",
        "companies.refresh_scheduled",
        "worker.ping",
        "maintenance.reap",
        "approvals.expire",
        "maintenance.prune",
        "debug.sleep",
    }
)


def run_lock(run_id: Id) -> str:
    """The procrastinate lock every job of a run holds (§9.2)."""
    return f"run:{run_id}"


# Earlier than any clock's "now": a retry scheduled here is due at once, by the database's clock.
RETRY_DUE_NOW: Final = datetime(1970, 1, 1, tzinfo=UTC)


class ImmediateRetry(RetryStrategy):
    """procrastinate's `RetryStrategy` with no wait, whose retry is due by the database's clock.

    procrastinate stamps a retry's `scheduled_at` with Python's `utcnow()`, and its `fetch_job`
    compares that with the database's `now()`. When this process's clock runs ahead of the
    database's (the Docker VM's clock falls behind the Mac's after a sleep), an immediate retry
    sat in the database's future for the length of the skew: a worker draining the queue missed
    it, and the run stayed `running` until the retry fell due. A retry with no wait is scheduled
    at the epoch instead, which is in the past by any clock, so the database alone decides.
    With a wait configured, procrastinate's own decision stands (it has no other clock to use).
    """

    def get_retry_decision(self, *, exception: BaseException, job: Job) -> RetryDecision | None:
        decision = super().get_retry_decision(exception=exception, job=job)
        if decision is None or self.wait or self.linear_wait or self.exponential_wait:
            return decision
        return RetryDecision(retry_at=RETRY_DUE_NOW)


def immediate_retry(attempts: int) -> ImmediateRetry | bool:
    """A task's `retry=`: False for no retry (R11), else `attempts` immediate retries."""
    return ImmediateRetry(max_attempts=attempts) if attempts else False


def shutdown_only_retry() -> RetryStrategy:
    """`chat.follow_up`'s strategy (§7.7): retried once after a worker shutdown aborted it
    (procrastinate hands the `CancelledError` to the strategy), never after an ordinary failure.
    A Stop is not retried either: `procrastinate_retry_job_v2` fails a job whose abort was
    requested."""
    # The annotation says `type[Exception]`; the worker passes any BaseException through.
    exceptions = cast(set[type[Exception]], {asyncio.CancelledError})
    return ImmediateRetry(max_attempts=1, retry_exceptions=exceptions)


async def _missing_task(**kwargs: Any) -> None:
    raise RuntimeError("This task is deferred by name; its owner registers the real one.")


_placeholders: dict[str, AnyTask] = {}


def task_for(task: AnyTask | str) -> AnyTask:
    """A task object, or the registered task of that name.

    A name in `TASKS` whose owner has not registered it in this process (the continuation tasks
    before the matching step, `chat.follow_up` before chat) gets a stand-in with the same name
    and lane, so the job row is exactly what the real task would write; a worker that has the
    real task runs it. Any other unknown name is an error.
    """
    if not isinstance(task, str):
        return task
    found = app.tasks.get(task)
    if found is not None:
        return found
    spec = TASKS.get(task)
    if spec is None:
        raise KeyError(f"No task named {task!r} is registered")
    placeholder = _placeholders.get(task)
    if placeholder is None:
        log.warning("task %s is not registered here; deferring it by name", task)
        placeholder = Task(_missing_task, blueprint=app, name=task, queue=spec.lane)
        _placeholders[task] = placeholder
    return placeholder


def defer_step(task: AnyTask | str, run_id: Id, *, queueing_lock: str, **kwargs: Any) -> int:
    """Sync. Defer a job that works on `run_id`, inside the caller's transaction (§9.2)."""
    return defer_in_tx(
        task_for(task),
        lock=run_lock(run_id),
        queueing_lock=queueing_lock,
        run_id=str(run_id),
        **kwargs,
    )


def short(error: BaseException, limit: int = 300) -> str:
    """An exception as one line of feed copy: its first line, at most `limit` characters."""
    text = str(error).strip() or type(error).__name__
    first = text.splitlines()[0]
    return first if len(first) <= limit else first[: limit - 1] + "…"


def timeout_message(timeout_s: int) -> str:
    return f"This step took longer than {timeout_s // 60} minutes and was stopped."


type Step = Callable[..., Awaitable[None]]


def run_step(name: str, *, queue: str, timeout_s: int, retry: int = 0) -> Callable[[Step], AnyTask]:
    """Register `async def step(run, **kwargs)` as the task `name` (see the module docstring)."""
    if queue not in LANES:
        raise ValueError(f"{queue!r} is not a lane")

    def deco(step: Step) -> AnyTask:
        @app.task(name=name, queue=queue, retry=immediate_retry(retry), pass_context=True)
        async def task(context: JobContext, run_id: str, **kwargs: Any) -> None:
            run = await db(lifecycle.claim, run_id)
            if run is None:
                return  # cancelled, finished, still paused, or a redelivery
            await _run(context, run, step, kwargs)
            job_id = context.job.id
            assert job_id is not None
            await db(lifecycle.fail_if_abandoned, run.id, job_id)

        async def _run(context: JobContext, run: Run, step: Step, kwargs: dict[str, Any]) -> None:
            with llm_context(
                user_id=run.user_id, run_id=run.id, conversation_id=run.conversation_id
            ):
                try:
                    async with asyncio.timeout(timeout_s):
                        await step(run, **kwargs)
                except TimeoutError:
                    log.warning("run %s: %s timed out after %ss", run.id, name, timeout_s)
                    await db(lifecycle.fail, run.id, timeout_message(timeout_s))
                except BudgetExceeded as error:
                    await db(lifecycle.fail, run.id, error.user_message)
                except UserFacingError as error:
                    await db(lifecycle.fail, run.id, str(error))
                except Exception as error:
                    if context.job.attempts < retry:
                        # procrastinate retries the same job; the run stays running and its
                        # lock job is todo again.
                        log.warning("run %s: %s failed, retrying", run.id, name)
                        raise
                    log.exception("run %s: %s failed", run.id, name)
                    await db(
                        lifecycle.fail, run.id, FAILURE_PREFIX.get(run.kind, "") + short(error)
                    )

        task.__doc__ = step.__doc__
        return task

    return deco
