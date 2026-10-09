"""`manage.py worker [--shutdown-timeout N]`: the one worker process (design §9.1, §13.5).

Before it takes any work it:
1. refuses to start with unapplied migrations ("Run pnpm db:migrate");
2. runs Django's system checks (the command's own `requires_system_checks`);
3. refuses a legacy-looking database (`assert_app_database`, §3.1);
4. swaps the app's connector for the worker connector (a `PsycopgConnector`: Django's connector
   cannot LISTEN). Every defer still goes through `defer_in_tx` on Django's connection (§9.1);
5. installs the periodic schedule (`runs/periodic.py`);
6. fails every stalled job once (`reaper.fail_stalled_jobs`), so a job a killed worker left
   `doing` releases its lock without waiting for a reap job.

Then it runs three procrastinate Workers, one per lane, under `asyncio.gather` in this process:

| Lane | Concurrency |
|---|---|
| `interactive` | 4 |
| `background` | `WORKER_CONCURRENCY` (3) |
| `system` | 2 |

One SIGINT/SIGTERM handler stops all three: each stops fetching, gives its running jobs
`--shutdown-timeout` seconds, then cancels them (in-flight model calls included, each ledgered);
the reaper fails their runs, and a cancelled `chat.follow_up` is retried once. Each Worker
unregisters on the way out, so `worker.online` goes false at once after a clean stop (and about
45 s after a crash). The Playwright browser closes last. No `task_middleware` is passed, which
would drop Django's per-task connection cleanup; timeouts live in `run_step` instead.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandParser
from django.db import DEFAULT_DB_ALIAS, connections
from django.db.migrations.executor import MigrationExecutor
from procrastinate.contrib.django.django_connector import DjangoConnector

from apps.core.guards import assert_app_database
from apps.core.queue import app
from apps.runs import periodic, reaper
from apps.runs.step import BACKGROUND, INTERACTIVE, SYSTEM

log = logging.getLogger("foray.worker")

DEFAULT_SHUTDOWN_TIMEOUT = 60.0
UNAPPLIED = "There are unapplied migrations. Run pnpm db:migrate"


def lanes() -> list[tuple[str, int]]:
    return [(INTERACTIVE, 4), (BACKGROUND, settings.ENV.worker_concurrency), (SYSTEM, 2)]


def unapplied_migrations() -> list[str]:
    executor = MigrationExecutor(connections[DEFAULT_DB_ALIAS])
    plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
    return [f"{migration.app_label}.{migration.name}" for migration, _ in plan]


async def _close_browser() -> None:
    try:
        from apps.resumes.render import close_browser

        await close_browser()
    except Exception:
        log.warning("could not close the PDF browser", exc_info=True)


async def serve(
    shutdown_timeout: float,
    *,
    stop: asyncio.Event | None = None,
    install_signal_handlers: bool = True,
) -> int:
    """Run the three lanes until `stop` is set (SIGINT/SIGTERM set it). Returns the exit code:
    1 when a lane stopped on its own (its side task failed, e.g. the database went away)."""
    loop = asyncio.get_running_loop()
    stop = stop or asyncio.Event()
    signals = (signal.SIGINT, signal.SIGTERM) if install_signal_handlers else ()
    for signum in signals:
        loop.add_signal_handler(signum, stop.set)
    crashed: list[str] = []
    django_connector = app.connector
    assert isinstance(django_connector, DjangoConnector), "the app runs on Django's connector"
    try:
        with app.replace_connector(django_connector.get_worker_connector()):
            async with app.open_async():
                installed = periodic.install(app)
                swept = await reaper.fail_stalled_jobs()
                log.info(
                    "startup: periodic %s; %s stalled job(s) failed",
                    ", ".join(installed) or "none",
                    len(swept),
                )
                workers = {
                    lane: asyncio.create_task(
                        app.run_worker_async(
                            queues=[lane],
                            concurrency=concurrency,
                            name=lane,
                            install_signal_handlers=False,
                            shutdown_graceful_timeout=shutdown_timeout,
                        ),
                        name=f"worker:{lane}",
                    )
                    for lane, concurrency in lanes()
                }
                log.info(
                    "ready (pid %s): %s",
                    os.getpid(),
                    ", ".join(f"{lane}×{n}" for lane, n in lanes()),
                )
                stopping = asyncio.create_task(stop.wait())
                done, _ = await asyncio.wait(
                    {stopping, *workers.values()}, return_when=asyncio.FIRST_COMPLETED
                )
                crashed = [lane for lane, task in workers.items() if task in done]
                if crashed:
                    log.error("lane %s stopped on its own; stopping the others", ", ".join(crashed))
                else:
                    log.info("stopping (running jobs get %ss)", shutdown_timeout)
                for task in workers.values():
                    task.cancel()  # Worker.run turns the cancel into a graceful stop
                results = await asyncio.gather(*workers.values(), return_exceptions=True)
                stopping.cancel()
                for lane, result in zip(workers, results, strict=True):
                    if isinstance(result, Exception):
                        log.error("lane %s failed", lane, exc_info=result)
    finally:
        for signum in signals:
            loop.remove_signal_handler(signum)
        await _close_browser()
    log.info("stopped")
    return 1 if crashed else 0


class Command(BaseCommand):
    help = "Run the background worker: the interactive, background and system lanes."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--shutdown-timeout",
            type=float,
            default=DEFAULT_SHUTDOWN_TIMEOUT,
            help="seconds running jobs get after SIGINT/SIGTERM before they are cancelled",
        )

    def handle(self, *args: Any, shutdown_timeout: float, **options: Any) -> None:
        pending = unapplied_migrations()
        if pending:
            self.stderr.write(f"{UNAPPLIED} ({', '.join(pending[:5])}).")
            raise SystemExit(1)
        try:
            assert_app_database(connections[DEFAULT_DB_ALIAS])
        except ImproperlyConfigured as error:
            self.stderr.write(str(error))
            raise SystemExit(1) from None
        connections.close_all()  # the Workers' threads take their own connections
        code = asyncio.run(serve(shutdown_timeout))
        if code:
            raise SystemExit(code)
