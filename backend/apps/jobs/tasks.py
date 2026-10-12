"""procrastinate tasks of this app (design §9.2, §9.3, §10.5). procrastinate's Django app
imports every installed app's `tasks` module at startup. Defers go through
`apps.core.queue.defer_in_tx` (runs through `lifecycle.start_run`).

- `company.add` (interactive, 15 min, retry 0): a `company_discovery` run
  (`pipeline.run_company_add`).
- `company.refresh` (background, 60 min, retry 0: careers pages call `extract`): a
  `company_refresh` run (`pipeline.run_company_refresh`).
- `companies.refresh_scheduled` (background, 60 min, retry 0): the six-hourly cron over the
  stale set, with no run, no user and no events.

The cron's schedule (`0 */6 * * *`, `queueing_lock="company-refresh:cron"`, no `lock`) is in
`runs/periodic.py`, which `manage.py worker` installs; it passes the tick as `timestamp`.
"""

from __future__ import annotations

import asyncio

from apps.core.queue import app
from apps.jobs import pipeline
from apps.runs.step import BACKGROUND, INTERACTIVE, TASKS, run_step

__all__ = ["companies_refresh_scheduled", "company_add", "company_refresh"]

company_add = run_step(
    pipeline.ADD_TASK, queue=INTERACTIVE, timeout_s=TASKS[pipeline.ADD_TASK].timeout_s
)(pipeline.run_company_add)

company_refresh = run_step(
    pipeline.REFRESH_TASK, queue=BACKGROUND, timeout_s=TASKS[pipeline.REFRESH_TASK].timeout_s
)(pipeline.run_company_refresh)


@app.task(name=pipeline.SCHEDULED_TASK, queue=BACKGROUND)
async def companies_refresh_scheduled(timestamp: int | None = None) -> None:
    """Refresh the boards not fetched in 2 h, holding `MULTI_BOARD_GATE` (§9.3)."""
    async with asyncio.timeout(TASKS[pipeline.SCHEDULED_TASK].timeout_s):
        await pipeline.refresh_scheduled()
