"""procrastinate tasks of this app (design §9.2, §9.3, §10.5). procrastinate's Django app
imports every installed app's `tasks` module at startup. Defers go through
`apps.core.queue.defer_in_tx` (runs through `lifecycle.start_run`).

- `company.add` (interactive, 15 min, retry 0): a `company_discovery` run
  (`pipeline.run_company_add`).
- `company.refresh` (background, 60 min, retry 0: careers pages call `extract`): a
  `company_refresh` run (`pipeline.run_company_refresh`).
- `companies.refresh_scheduled` (background, 60 min, retry 0): the scheduled poll of the boards
  that are due (#55), with no run, no user and no events. When it brought postings in, when it
  failed, and on every sixth hour's tick whatever it found, it defers `watch.fan_out`
  (`matching/tasks.py`), which starts each watching user's watch (#5).

The cron's schedule (`*/15 * * * *`, `queueing_lock="company-refresh:cron"`, no `lock`) is in
`runs/periodic.py`, which `manage.py worker` installs; it passes the tick as `timestamp`.
"""

from __future__ import annotations

import asyncio
import logging

from procrastinate.exceptions import AlreadyEnqueued

from apps.core.db import db
from apps.core.queue import app, defer_in_tx
from apps.jobs import pipeline
from apps.runs.step import BACKGROUND, INTERACTIVE, TASKS, run_step, task_for

__all__ = ["companies_refresh_scheduled", "company_add", "company_refresh", "watches_due"]

log = logging.getLogger("foray.jobs")

company_add = run_step(
    pipeline.ADD_TASK, queue=INTERACTIVE, timeout_s=TASKS[pipeline.ADD_TASK].timeout_s
)(pipeline.run_company_add)

company_refresh = run_step(
    pipeline.REFRESH_TASK, queue=BACKGROUND, timeout_s=TASKS[pipeline.REFRESH_TASK].timeout_s
)(pipeline.run_company_refresh)


WATCH_FAN_OUT = "watch.fan_out"


def _defer_watches() -> None:
    """Defers `watch.fan_out` by name, so this app does not import matching. One waiting copy."""
    try:
        defer_in_tx(task_for(WATCH_FAN_OUT), queueing_lock="watch:fan-out")
    except AlreadyEnqueued:
        log.info("watch.fan_out is already waiting")


# Most passes of a board polled every 15 minutes bring nothing in, and a watch with nothing new
# has nothing to do. But a match the daily review cap held back gets its next chance only when
# a watch runs, so the ticks on these hours start the watches regardless.
BACKLOG_TICK_S = 6 * 3600


def watches_due(inserted: int, timestamp: int | None) -> bool:
    """Whether this pass starts the watches: it brought postings in, or it is a sixth hour's
    tick (or a pass deferred by hand, which carries no tick)."""
    return inserted > 0 or not timestamp or timestamp % BACKLOG_TICK_S == 0


@app.task(name=pipeline.SCHEDULED_TASK, queue=BACKGROUND)
async def companies_refresh_scheduled(timestamp: int | None = None) -> None:
    """Poll the boards that are due, holding `MULTI_BOARD_GATE` (§9.3), then defer the watches
    when there is something for them (`watches_due`). A failed pass still defers them (what
    earlier passes brought in is new); a worker shutting down does not."""
    try:
        async with asyncio.timeout(TASKS[pipeline.SCHEDULED_TASK].timeout_s):
            summary = await pipeline.refresh_scheduled()
    except Exception:
        await db(_defer_watches)
        raise
    if watches_due(summary.inserted, timestamp):
        await db(_defer_watches)
