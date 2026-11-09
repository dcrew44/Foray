"""The notifications tasks (design §9.2, §9.3; #59). procrastinate's Django app imports every
installed app's `tasks` module at startup.

| Task | Lane | Timeout | Does |
|---|---|---|---|
| `notify.send` | system | 2 min | sends the notifications that are due |
| `notify.test` | system | 1 min | Settings' test notification, answered with an event |

`notify.send` sends one user's notifications when a watch deferred it for them
(`outbox.queue_matches`, queueing lock `notify:<user>`), and everyone's when the periodic
schedule did (`runs/periodic.py`, every minute, with the tick as `timestamp`). It works on no
run and holds no lock: `outbox.claim` leases the rows it takes, so two jobs never send the same
one. Neither task retries as a job; a failed send is retried a row at a time, by the sweep, and
a failed test is simply reported.

`notify.test` is how the Settings button works without a request waiting on ntfy (invariant
12), and it proves the same path a real notification takes: the worker, the guarded client, the
configured server and the saved topic. Like `worker.ping`, its answer comes back through the
event feed, as `{jobId, sent}` with the sentence to show as the event's message.
"""

from __future__ import annotations

import asyncio
import logging

from procrastinate import JobContext

from apps.core.db import db
from apps.core.queue import app
from apps.notifications import outbox, send
from apps.runs import events
from apps.runs.catalogue import EventKind, Level, NotificationTestData
from apps.runs.step import TASKS

__all__ = ["notify_send", "notify_test"]

log = logging.getLogger("foray.notifications")


@app.task(name=outbox.SEND_TASK, queue=TASKS[outbox.SEND_TASK].lane)
async def notify_send(user_id: str | None = None, timestamp: int | None = None) -> None:
    """Send what is due, for one user or (from the schedule) for everyone."""
    async with asyncio.timeout(TASKS[outbox.SEND_TASK].timeout_s):
        outcome = await send.send_due(user_id)
    if outcome.sent or outcome.failed:
        log.info("notify: %d sent, %d failed", outcome.sent, outcome.failed)


@app.task(name=outbox.TEST_TASK, queue=TASKS[outbox.TEST_TASK].lane, pass_context=True)
async def notify_test(context: JobContext, user_id: str) -> None:
    """One test notification to the user's saved topic, and how it went as an event."""
    job_id = context.job.id
    assert job_id is not None  # a job a worker fetched has its row
    async with asyncio.timeout(TASKS[outbox.TEST_TASK].timeout_s):
        trial = await send.send_trial(user_id)
        await db(
            events.emit,
            user_id,
            EventKind.NOTIFICATION_TEST,
            trial.message,
            level=Level.INFO if trial.sent else Level.WARN,
            data=NotificationTestData(job_id=job_id, sent=trial.sent).dump(),
        )
    log.info("notify: test job %s %s", job_id, "sent" if trial.sent else "not sent")
