"""procrastinate tasks of the applications app (#60).

`application.prepare(application_id)` reads one posting's form and plans its answers. It hangs
off an application, not a run: the Apply page is where its result shows, and an event
(`application.prepared`) tells open pages to draw it. `prepare.start` defers it in the
transaction that creates the row, with `queueing_lock="prepare:<id>"`.

It calls no model, so a second attempt costs nothing: one aborted by a worker shutdown (the dev
worker restarts on every save) runs again, and the row it left in `preparing` is filled then.
"""

from __future__ import annotations

import asyncio
import logging

from apps.applications.prepare import PREPARE_TASK, prepare
from apps.core.queue import app
from apps.runs.step import INTERACTIVE, TASKS, shutdown_only_retry

log = logging.getLogger("foray.applications")


@app.task(name=PREPARE_TASK, queue=INTERACTIVE, retry=shutdown_only_retry())
async def application_prepare(application_id: str) -> None:
    async with asyncio.timeout(TASKS[PREPARE_TASK].timeout_s):
        outcome = await prepare(application_id)
    log.info("application %s: %s", application_id, outcome)
