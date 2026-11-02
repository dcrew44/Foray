"""The matching tasks (design §9.2). procrastinate's Django app imports every installed app's
`tasks` module at startup, so the API process knows them too; defers go through
`lifecycle.start_run` and `approvals` (`defer_in_tx`).

| Task | Lane | Timeout | Does |
|---|---|---|---|
| `search.run` | background | 60 min | a search up to its first gate (`search.run_search`) |
| `search.evaluate` | background | 60 min | gate 1's continuation: deep evaluation, then gate 2 |
| `search.shortlist` | background | 10 min | gate 2's continuation: shortlisting, then complete |
| `match.evaluate` | interactive | 30 min | a `rank_deep` run: evaluation on demand |
| `watch.run` | background | 30 min | a `watch` run: the new postings, reviewed and announced |
| `watch.fan_out` | system | 5 min | a watch per watching user, after each scheduled refresh |

Every run step holds `lock="run:<id>"` and `retry=0` (they call models: R11). A continuation's
job carries `run_id` and `approval_id` (step 4's contract, `deviations/step-4.md` §2).
`watch.fan_out` has no run: it only starts watches (`watch.start_watches`).
"""

from __future__ import annotations

import asyncio
import logging

from apps.core.db import db
from apps.core.queue import app
from apps.matching import evaluate, search, watch
from apps.runs.models import Run
from apps.runs.step import TASKS, run_step

log = logging.getLogger("foray.matching")


@run_step("search.run", queue=TASKS["search.run"].lane, timeout_s=TASKS["search.run"].timeout_s)
async def search_run(run: Run) -> None:
    """Refresh, aggregate, filter, score, then raise the deep-evaluation gate."""
    await search.run_search(run)


@run_step(
    "search.evaluate",
    queue=TASKS["search.evaluate"].lane,
    timeout_s=TASKS["search.evaluate"].timeout_s,
)
async def search_evaluate(run: Run, approval_id: str) -> None:
    """Deep-evaluate what gate 1 approved, then raise the shortlist gate."""
    await evaluate.continue_search_evaluate(run, approval_id)


@run_step(
    "search.shortlist",
    queue=TASKS["search.shortlist"].lane,
    timeout_s=TASKS["search.shortlist"].timeout_s,
)
async def search_shortlist(run: Run, approval_id: str) -> None:
    """Shortlist what gate 2 approved, then complete the search."""
    await evaluate.continue_search_shortlist(run, approval_id)


@run_step(
    "match.evaluate",
    queue=TASKS["match.evaluate"].lane,
    timeout_s=TASKS["match.evaluate"].timeout_s,
)
async def match_evaluate(run: Run) -> None:
    """Deep-evaluate specific matches on demand."""
    await evaluate.run_match_evaluation(run)


@run_step(watch.WATCH_TASK, queue=TASKS["watch.run"].lane, timeout_s=TASKS["watch.run"].timeout_s)
async def watch_run(run: Run) -> None:
    """Score the postings first seen since the last watch, review the best, announce (#5)."""
    await watch.run_watch(run)


@app.task(name=watch.FAN_OUT_TASK, queue=TASKS["watch.fan_out"].lane)
async def watch_fan_out(timestamp: int | None = None) -> None:
    """A watch for every user who is watching; deferred by the scheduled refresh. No run."""
    async with asyncio.timeout(TASKS["watch.fan_out"].timeout_s):
        started = await db(watch.start_watches)
    log.info("watch: started %d", started)
