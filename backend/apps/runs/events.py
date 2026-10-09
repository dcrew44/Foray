"""Events, the progress feed, with a commit-safe cursor (design §9.5, §7.7; R5, R7).

`emit()` is the only writer of `events`. An event's bigserial id is the live cursor, but an id is
allocated at INSERT and becomes visible at COMMIT, so a poll between two out-of-order commits
would skip the earlier id forever (`map/_gaps.md` §3.5). `emit()` therefore takes a per-user
transaction-scoped advisory lock **before** `nextval` and holds it until commit: for one user,
the next id is allocated only after the previous emitting transaction has committed or rolled
back, ids become visible in id order, and `id > since` never skips.

Two rules keep this deadlock-free and cheap:
- `emit()` comes after every row lock its transaction takes; only inserts of new rows (such as a
  procrastinate defer) may follow it;
- no transaction spans a model call or a fetch (R9), so the lock is held for milliseconds.

A follow-up kind (`approval.requested`, `run.completed`, `run.failed`) on a run started from chat
defers `chat.follow_up(event_id)` in the same transaction, through `defer_in_tx`, so the job
becomes visible exactly when the event does, and at most once per event (queueing lock
`follow-up:<event_id>`).
"""

from __future__ import annotations

import uuid
from typing import Any

from django.db import connection, transaction

from apps.core.queue import defer_in_tx
from apps.runs import lifecycle, step
from apps.runs.catalogue import DEFAULT_LEVEL, FOLLOW_UP_KINDS, Level
from apps.runs.models import Event, Run

__all__ = ["ADVISORY_CLASS", "FOLLOW_UP_TASK", "emit", "run_conversation"]

type Id = str | uuid.UUID

# The first key of the per-user lock: pg_advisory_xact_lock(4242, hashtext(user_id)).
ADVISORY_CLASS = 4242
FOLLOW_UP_TASK = "chat.follow_up"


def run_conversation(run_id: Id) -> uuid.UUID | None:
    """The chat a run reports back to, if it was started from one."""
    return (
        Run.objects.unscoped("emit: does the run report back to a chat?")
        .filter(id=run_id)
        .values_list("conversation_id", flat=True)
        .first()
    )


def emit(
    user_id: str,
    kind: str,
    message: str,
    *,
    run_id: Id | None = None,
    level: str | None = None,
    data: Any = None,
    approval_id: Id | None = None,
) -> Event:
    """Sync. Write one event; the last statement of its transaction (or alone, in autocommit)."""
    with transaction.atomic():
        with connection.cursor() as cursor:
            # Per user, until commit: serialises id allocation (see the module docstring).
            cursor.execute(
                "SELECT pg_advisory_xact_lock(%s, hashtext(%s))", [ADVISORY_CLASS, user_id]
            )
        event = Event.objects.create_for(
            user_id,
            run_id=run_id,
            kind=kind,
            message=message,
            level=level or DEFAULT_LEVEL.get(kind, Level.INFO),
            data=lifecycle.dump(data),
            approval_id=approval_id,
        )
        if kind in FOLLOW_UP_KINDS and run_id and run_conversation(run_id):
            defer_in_tx(
                step.task_for(FOLLOW_UP_TASK),
                queueing_lock=f"follow-up:{event.id}",
                event_id=event.id,
            )
    return event
