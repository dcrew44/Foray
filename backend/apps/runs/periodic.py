"""The periodic schedule (design §9.3), installed by `manage.py worker` only.

| Task | Schedule (UTC) | Queueing lock |
|---|---|---|
| `maintenance.reap` | every minute | `maintenance:reap` |
| `approvals.expire` | every minute | `approvals:expire` |
| `maintenance.prune` | daily 03:17 | `maintenance:prune` |
| `companies.refresh_scheduled` | `0 */6 * * *` | `company-refresh:cron` |

- **No periodic task has a `lock`**, only a `queueing_lock` (one waiting copy at most;
  procrastinate's deferrer catches the `AlreadyEnqueued`). A periodic holding a lock could lock
  itself out after a kill (see `reaper.py`).
- **"Missed once."** `PROCRASTINATE_PERIODIC_DEFAULTS["max_delay"] = 6 h`: on startup the deferrer
  defers only the latest missed tick of each task, and only if it is under 6 h old, which is
  pg-boss's `missed: "once"`. `procrastinate_periodic_defers` keeps a restart within the same
  window from deferring that tick twice.
- **Why here and not `@app.periodic`.** Every procrastinate Worker runs the periodic deferrer, the
  in-process test Workers of `tests/queue.py` included. Registering at import would make every
  test that drains the queue also defer (and run) the reaper, the expiry sweep and the cron.
  The worker command installs the schedule before it starts its Workers; nothing else does.
  A task whose owner has not landed it yet (the cron is step 5C's) is skipped with a log line,
  and one an owner registered with `@app.periodic` itself is left as it is. A periodic task
  receives a `timestamp` keyword (the tick it was deferred for).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from procrastinate import App

__all__ = ["SCHEDULE", "Periodic", "install"]

log = logging.getLogger("foray.worker")


@dataclass(frozen=True, slots=True)
class Periodic:
    task: str
    cron: str
    queueing_lock: str


SCHEDULE: tuple[Periodic, ...] = (
    Periodic("maintenance.reap", "* * * * *", "maintenance:reap"),
    Periodic("approvals.expire", "* * * * *", "approvals:expire"),
    Periodic("maintenance.prune", "17 3 * * *", "maintenance:prune"),
    Periodic("companies.refresh_scheduled", "0 */6 * * *", "company-refresh:cron"),
)


def install(app: App, schedule: tuple[Periodic, ...] = SCHEDULE) -> list[str]:
    """Register the schedule on `app`'s periodic registry. Returns the task names installed."""
    registry = app.periodic_registry
    installed: list[str] = []
    for entry in schedule:
        task = app.tasks.get(entry.task)
        if task is None:
            log.info("periodic %s skipped: the task is not registered yet", entry.task)
            continue
        if any(key[0] == entry.task for key in registry.periodic_tasks):
            continue  # its owner registered it with @app.periodic
        registry.register_task(
            task=task,
            cron=entry.cron,
            periodic_id="",
            configure_kwargs={"queueing_lock": entry.queueing_lock},
        )
        installed.append(entry.task)
    return installed
