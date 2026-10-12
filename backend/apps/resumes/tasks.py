"""procrastinate tasks of the resumes app (design §9.2). procrastinate's Django app imports every
installed app's `tasks` module at startup, in the API process too, so `start_run` finds these by
name. The steps themselves live in `pipeline.py`.

| Task | Lane | Timeout | Retry |
|---|---|---|---|
| `resume.import` | interactive | 10 min | 0 (calls `extract`) |
| `resume.tailor` | interactive | 30 min | 0 (calls `tailor` and `verify`) |
| `resume.render` | interactive | 10 min | 1 (no model call) |

`run_step` keeps a render run `running` across its one retry.

Every job holds `lock="run:<id>"` and `queueing_lock="run:<id>"`. Defers go through
`apps.core.queue.defer_in_tx` (here, via `lifecycle.start_run`).
"""

from __future__ import annotations

from apps.resumes import pipeline
from apps.runs.step import TASKS, run_step

_IMPORT = TASKS[pipeline.IMPORT_TASK]
_TAILOR = TASKS[pipeline.TAILOR_TASK]
_RENDER = TASKS[pipeline.RENDER_TASK]

resume_import = run_step(pipeline.IMPORT_TASK, queue=_IMPORT.lane, timeout_s=_IMPORT.timeout_s)(
    pipeline.import_resume
)
resume_tailor = run_step(pipeline.TAILOR_TASK, queue=_TAILOR.lane, timeout_s=_TAILOR.timeout_s)(
    pipeline.tailor
)
resume_render = run_step(
    pipeline.RENDER_TASK, queue=_RENDER.lane, timeout_s=_RENDER.timeout_s, retry=1
)(pipeline.render)
