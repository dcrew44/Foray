"""The mail task (design §9.2, §9.3; #26). procrastinate's Django app imports every installed
app's `tasks` module at startup.

| Task | Lane | Timeout | Does |
|---|---|---|---|
| `mail.check` | system | 10 min | reads new employers' answers from connected mailboxes |

`mail.check` checks one user's mailbox when connecting it deferred the job
(`connect.save_account`, queueing lock `mail:<user>`), and every connected mailbox when the
periodic schedule did (`runs/periodic.py`, every 15 minutes, with the tick as `timestamp`). It
works on no run, holds no lock and calls no model. A check that a worker shutdown interrupted
is retried once; any other failure is recorded on the account and left to the next tick, which
reads what this one did not get to.
"""

from __future__ import annotations

import asyncio
import logging

from apps.core.queue import app
from apps.mail import check
from apps.mail.connect import CHECK_TASK
from apps.runs.step import TASKS, shutdown_only_retry

__all__ = ["mail_check"]

log = logging.getLogger("foray.mail")


@app.task(name=CHECK_TASK, queue=TASKS[CHECK_TASK].lane, retry=shutdown_only_retry())
async def mail_check(user_id: str | None = None, timestamp: int | None = None) -> None:
    """Check one user's mailbox, or (from the schedule) every connected one."""
    async with asyncio.timeout(TASKS[CHECK_TASK].timeout_s):
        outcome = await (check.check_user(user_id) if user_id else check.check_all())
    if outcome.checked or outcome.failed:
        log.info(
            "mail: %d checked, %d failed, %d stored, %d matched",
            outcome.checked,
            outcome.failed,
            outcome.stored,
            outcome.matched,
        )
