"""Run lifecycle (design §9.4, §9.9; R1, R2, R4).

Every function is sync, runs in one transaction and makes every status change conditional, so a
late finish can never overwrite the user's Stop. `emit()` is always the last statement (§9.5).

```
queued ──claim──▶ running
running ──request_approval (approve_each)──▶ awaiting_approval
awaiting_approval ──decide / expire (continuation deferred, same transaction)──▶ running
running ──request_approval (autopilot: auto_approved, continuation deferred)──▶ running
running ──complete──▶ completed            running ──fail──▶ failed
queued | running | awaiting_approval ──cancel──▶ cancelled
queued | running ──reaper: no todo/doing job holds run:<id>──▶ failed
awaiting_approval ──reaper: no pending approval and no live job for 10 min──▶ failed
```

A run finishes only through `complete()`, `fail()` or `cancel()`. Each acts on an active run
only and emits exactly one terminal event (`run.completed`, `run.failed`, `run.cancelled`), the
run's last (R4, R17).
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping
from typing import Any

from django.db import IntegrityError, connection, transaction
from django.db.models.functions import Coalesce, Now
from procrastinate.tasks import Task

from apps.core.choices import JsonModel
from apps.runs import events, step
from apps.runs.catalogue import (
    ACTIVE,
    ApprovalStatus,
    EventKind,
    Level,
    Mode,
    RunKind,
    RunStatus,
)
from apps.runs.models import Approval, Run

__all__ = [
    "ABANDONED",
    "STOPPED",
    "cancel",
    "claim",
    "complete",
    "fail",
    "fail_if_abandoned",
    "is_cancelled",
    "merge_input",
    "merge_stats",
    "start_run",
]

log = logging.getLogger("foray.runs")

type Id = str | uuid.UUID

ONE_ACTIVE_PER_KEY = "runs_one_active_per_key"
ABANDONED = "The background step ended without finishing this run."
STOPPED = "Stopped. Everything found so far was kept."

_ACTIVE_SQL = "('queued', 'running', 'awaiting_approval')"


def dump(value: Any) -> Any:
    """A JSON column's value: a model is dumped with its wire keys; anything else passes."""
    return value.dump() if isinstance(value, JsonModel) else value


def jsonb(value: Any) -> str:
    """A value as compact JSON text, for a `%s::jsonb` parameter."""
    return json.dumps(dump(value), ensure_ascii=False, separators=(",", ":"))


def _violates(error: IntegrityError, constraint: str) -> bool:
    diag = getattr(error.__cause__, "diag", None)
    return getattr(diag, "constraint_name", None) == constraint


def as_uuid(value: Id) -> uuid.UUID | None:
    """An id from a caller (a chat tool's argument, say) as a UUID; None when malformed, which
    callers report as "not found", never as a database error."""
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def _runs(reason: str):  # every user's runs, for system code
    return Run.objects.unscoped(reason)


# --- starting ------------------------------------------------------------------------------------


def start_run(
    user_id: str,
    kind: str,
    *,
    input: Any,
    mode: str = Mode.APPROVE_EACH,
    conversation_id: Id | None = None,
    dedupe_key: str | None,
    task: Task[Any, Any, Any] | str,
    task_kwargs: Mapping[str, Any] | None = None,
) -> tuple[Run, bool]:
    """Insert a queued run and defer its first job in one transaction: commit gives both, a
    rollback neither (R1). Returns `(run, reused)`: when an active run already holds
    `(user, dedupe_key)`, that run comes back with `reused=True` and nothing is written (R2)."""
    if kind not in RunKind.values:
        raise ValueError(f"{kind!r} is not a run kind")
    if mode not in Mode.values:
        raise ValueError(f"{mode!r} is not a run mode")
    value = dump(input)
    # The other starter may finish between our failed insert and the read; then insert again.
    for _ in range(3):
        with transaction.atomic():
            try:
                with transaction.atomic():  # a savepoint: the conflict must not poison a caller
                    run = Run.objects.create_for(
                        user_id,
                        kind=kind,
                        mode=mode,
                        status=RunStatus.QUEUED,
                        input=value,
                        conversation_id=conversation_id,
                        dedupe_key=dedupe_key,
                    )
            except IntegrityError as error:
                if not _violates(error, ONE_ACTIVE_PER_KEY):
                    raise
                existing = (
                    Run.objects.for_user(user_id)
                    .filter(dedupe_key=dedupe_key, status__in=ACTIVE)
                    .order_by("created_at")
                    .first()
                )
                if existing is not None:
                    return existing, True
                continue
            step.defer_step(
                task, run.id, queueing_lock=step.run_lock(run.id), **(task_kwargs or {})
            )
            return run, False
    raise RuntimeError(f"Could not start a {kind} run for {dedupe_key!r}")


def claim(run_id: Id) -> Run | None:
    """`queued|running -> running` in one conditional UPDATE. None when the run is cancelled,
    finished or still awaiting approval: the task then returns without doing anything."""
    with transaction.atomic():
        claimed = (
            _runs("claim: the worker holds the run's job")
            .filter(id=run_id, status__in=(RunStatus.QUEUED, RunStatus.RUNNING))
            .update(
                status=RunStatus.RUNNING, started_at=Coalesce("started_at", Now()), updated_at=Now()
            )
        )
        if not claimed:
            return None
        return _runs("claim: the claimed run").get(id=run_id)


# --- progress ------------------------------------------------------------------------------------


def _merge(column: str, run_id: Id, patch: Mapping[str, Any]) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE runs SET {column} = {column} || %s::jsonb, updated_at = now() WHERE id = %s",
            [jsonb(dict(patch)), str(run_id)],
        )


def merge_stats(run_id: Id, patch: Mapping[str, Any] | None = None, /, **fields: Any) -> None:
    """`stats = stats || patch`: a shallow merge, so concurrent steps never clobber each other.
    Keys are the camelCase ones the UI reads (`merge_stats(id, phase="scoring", scored=40)`)."""
    _merge("stats", run_id, {**(patch or {}), **fields})


def merge_input(run_id: Id, patch: Mapping[str, Any] | None = None, /, **fields: Any) -> None:
    """`input = input || patch` (the search caches its brief and criteria here)."""
    _merge("input", run_id, {**(patch or {}), **fields})


# --- finishing -----------------------------------------------------------------------------------


def complete(
    run_id: Id,
    output: Any,
    message: str,
    *,
    level: str = Level.INFO,
    stats: Mapping[str, Any] | None = None,
) -> bool:
    """Finish an active run as completed (phase `done`) and emit `run.completed` with the output
    as its data. False, with nothing written, when the run had already finished."""
    value = dump(output)
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE runs SET status = 'completed', output = %s::jsonb, error = NULL, "
                "stats = stats || %s::jsonb, finished_at = now(), updated_at = now() "
                f"WHERE id = %s AND status IN {_ACTIVE_SQL} RETURNING user_id",
                [
                    None if value is None else jsonb(value),
                    jsonb({"phase": "done", **(stats or {})}),
                    str(run_id),
                ],
            )
            row = cursor.fetchone()
        if row is None:
            return False
        events.emit(
            row[0], EventKind.RUN_COMPLETED, message, run_id=run_id, level=level, data=value
        )
    return True


def fail(run_id: Id, message: str) -> bool:
    """Finish an active run as failed with `message` (shown on its card) and emit `run.failed`
    with the same words. False when the run had already finished."""
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE runs SET status = 'failed', error = %s, finished_at = now(), "
                f"updated_at = now() WHERE id = %s AND status IN {_ACTIVE_SQL} RETURNING user_id",
                [message, str(run_id)],
            )
            row = cursor.fetchone()
        if row is None:
            return False
        events.emit(row[0], EventKind.RUN_FAILED, message, run_id=run_id, level=Level.ERROR)
    return True


def cancel(user_id: str, run_id: Id) -> bool:
    """Stop the user's run (§9.9): cancelled, its pending approvals expired, its queued job
    cancelled and its running job asked to abort (procrastinate cancels the asyncio task, so
    in-flight model calls stop), then `run.cancelled`, emitted here and only here. False when
    the run is not the user's or is no longer active."""
    run_uuid = as_uuid(run_id)
    if run_uuid is None:
        return False
    with transaction.atomic():
        run = Run.objects.for_user(user_id).select_for_update().filter(id=run_uuid).first()
        if run is None or run.status not in ACTIVE:
            return False
        _runs("cancel: the row is locked and the user's").filter(id=run.id).update(
            status=RunStatus.CANCELLED, finished_at=Now(), updated_at=Now()
        )
        Approval.objects.for_user(user_id).filter(
            run_id=run.id, status=ApprovalStatus.PENDING
        ).update(status=ApprovalStatus.EXPIRED)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT procrastinate_cancel_job_v1(j.id, true, false) FROM procrastinate_jobs j "
                "WHERE j.lock = %s AND j.status IN ('todo', 'doing')",
                [step.run_lock(run.id)],
            )
        events.emit(user_id, EventKind.RUN_CANCELLED, STOPPED, run_id=run.id, level=Level.WARN)
    return True


def is_cancelled(run_id: Id) -> bool:
    """For pipelines, between stages, batches and items: True once the run has stopped for any
    reason, not only a Stop. A run the reaper (or anything else) failed may still have its task
    alive (a laptop that slept, step 13), and that task must stop spending and writing too."""
    return (
        not _runs("is_cancelled: the pipeline's own run")
        .filter(id=run_id, status__in=ACTIVE)
        .exists()
    )


def fail_if_abandoned(run_id: Id, job_id: int) -> bool:
    """After a step returns: fail the run when it is still `queued` or `running` and no other
    `todo`/`doing` job holds it, so a handler that forgets to finish shows at once (R4)."""
    with transaction.atomic():
        run = (
            _runs("fail_if_abandoned: the step's own run")
            .select_for_update()
            .filter(id=run_id, status__in=(RunStatus.QUEUED, RunStatus.RUNNING))
            .first()
        )
        if run is None:
            return False
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM procrastinate_jobs WHERE lock = %s "
                "AND status IN ('todo', 'doing') AND id <> %s)",
                [step.run_lock(run.id), job_id],
            )
            row = cursor.fetchone()
        if row and row[0]:
            return False
        log.error("run %s (%s): its step ended without finishing it", run.id, run.kind)
        return fail(run.id, ABANDONED)
