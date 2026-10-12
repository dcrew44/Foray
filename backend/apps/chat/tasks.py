"""procrastinate tasks of the chat app (design §7.7, §9.2).

`chat.follow_up(event_id)` reports a run's event in the chat the run was started from. It hangs
off an event, not a run: no `runs` row, no `run:<id>` lock. `events.emit()` defers it in the
event's own transaction with `queueing_lock="follow-up:<event_id>"`, and it re-defers itself
the same way while a user turn streams.

Its retry strategy is `step.shutdown_only_retry()`: a follow-up aborted by a worker shutdown (the
dev worker restarts on every save) runs once more, because procrastinate hands the shutdown's
`CancelledError` to the strategy; any other failure is not retried. The re-run is idempotent (one
notice per event) and read-only, so at most one discarded generation is paid twice (R11's one
exception). Defers go through `apps.core.queue.defer_in_tx`.
"""

from __future__ import annotations

import asyncio
import logging

from apps.chat.follow_up import run_follow_up
from apps.core.queue import app
from apps.runs.events import FOLLOW_UP_TASK
from apps.runs.step import INTERACTIVE, TASKS, shutdown_only_retry

log = logging.getLogger("foray.chat")


@app.task(name=FOLLOW_UP_TASK, queue=INTERACTIVE, retry=shutdown_only_retry())
async def chat_follow_up(event_id: int) -> None:
    async with asyncio.timeout(TASKS[FOLLOW_UP_TASK].timeout_s):  # 3 minutes
        outcome = await run_follow_up(event_id)
    log.info("chat follow-up for event %s: %s", event_id, outcome)
