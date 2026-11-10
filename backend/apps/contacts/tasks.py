"""procrastinate tasks of the contacts app (#25).

| Task | Lane | Timeout | Does |
|---|---|---|---|
| `outreach.draft` | interactive | 5 min | an `outreach` run: one note to one contact |

It is a run step (`lock="run:<id>"`, the ledger context, the timeout, failures turned into
`run.failed`) and it calls a model, so it never retries (R11). Importing a contacts file is no
task at all: it runs in its request.
"""

from __future__ import annotations

from apps.contacts import outreach
from apps.runs.models import Run
from apps.runs.step import TASKS, run_step


@run_step(
    outreach.DRAFT_TASK,
    queue=TASKS[outreach.DRAFT_TASK].lane,
    timeout_s=TASKS[outreach.DRAFT_TASK].timeout_s,
)
async def outreach_draft(run: Run) -> None:
    """Draft a short note to someone the user knows at the match's company."""
    await outreach.run_draft(run)
