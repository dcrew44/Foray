"""procrastinate tasks of this app (design §9.2, §9.3, §10.5). procrastinate's Django app
imports every installed app's `tasks` module at startup. Defers go through
`apps.core.queue.defer_in_tx` (runs through `lifecycle.start_run`).

- `company.add` (interactive, 15 min, retry 0): a `company_discovery` run
  (`pipeline.run_company_add`).
- `company.refresh` (background, 60 min, retry 0: careers pages call `extract`): a
  `company_refresh` run (`pipeline.run_company_refresh`).
- `companies.refresh_scheduled` (background, 60 min, retry 0): the six-hourly cron over the
  stale set, with no run, no user and no events. When it ends, refreshed or not, it defers
  `watch.fan_out` (`matching/tasks.py`), which starts each watching user's watch (#5).

The cron's schedule (`0 */6 * * *`, `queueing_lock="company-refresh:cron"`, no `lock`) is in
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

__all__ = ["companies_refresh_scheduled", "company_add", "company_refresh"]

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


@app.task(name=pipeline.SCHEDULED_TASK, queue=BACKGROUND)
async def companies_refresh_scheduled(timestamp: int | None = None) -> None:
    """Refresh the boards not fetched in 2 h, holding `MULTI_BOARD_GATE` (§9.3), then defer the
    watches. A failed refresh still defers them (what earlier refreshes brought in is new); a
    worker shutting down does not."""
    try:
        async with asyncio.timeout(TASKS[pipeline.SCHEDULED_TASK].timeout_s):
            await pipeline.refresh_scheduled()
    except Exception:
        await db(_defer_watches)
        raise
    await db(_defer_watches)
