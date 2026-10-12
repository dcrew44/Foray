"""The matching tasks (design §9.2). procrastinate's Django app imports every installed app's
`tasks` module at startup, so the API process knows them too; defers go through
`lifecycle.start_run` and `approvals` (`defer_in_tx`).

| Task | Lane | Timeout | Does |
|---|---|---|---|
| `search.run` | background | 60 min | a search up to its first gate (`search.run_search`) |
| `search.evaluate` | background | 60 min | gate 1's continuation: deep evaluation, then gate 2 |
| `search.shortlist` | background | 10 min | gate 2's continuation: shortlisting, then complete |
| `match.evaluate` | interactive | 30 min | a `rank_deep` run: evaluation on demand |

Every one holds `lock="run:<id>"` and `retry=0` (they call models: R11). A continuation's job
carries `run_id` and `approval_id` (step 4's contract, `deviations/step-4.md` §2).
"""

from __future__ import annotations

from apps.matching import evaluate, search
from apps.runs.models import Run
from apps.runs.step import TASKS, run_step


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
