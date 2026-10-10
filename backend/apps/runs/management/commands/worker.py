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

A lane that loses the database (a Postgres restart or an outage longer than the pool's 30 s
wait: procrastinate's heartbeat and fetch have no error handling, so the lane ends) is started
again after a pause, 1 s doubling to 30 s, for as long as the worker is not stopping; the other
lanes keep running. Any other way a lane ends still stops the worker with exit code 1 (the
prod compose restarts it).

One SIGINT/SIGTERM handler stops all three: each stops fetching, gives its running jobs
`--shutdown-timeout` seconds, then cancels them (in-flight model calls included, each ledgered);
the reaper fails their runs, and a cancelled `chat.follow_up` is retried once. Each Worker
unregisters on the way out, so `worker.online` goes false at once after a clean stop (and about
45 s after a crash). The Playwright browser closes last, within 10 s and still under the stop
handler (a second Ctrl-C must not interrupt it): a close that hangs is logged and abandoned, so
`stopped` is always logged. No `task_middleware` is passed, which
would drop Django's per-task connection cleanup; timeouts live in `run_step` instead.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from typing import Any

import psycopg
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandParser
from django.db import DEFAULT_DB_ALIAS, connections
from django.db.migrations.executor import MigrationExecutor
from procrastinate.contrib.django.django_connector import DjangoConnector
from procrastinate.exceptions import ConnectorException

from apps.core.guards import assert_app_database
from apps.core.queue import app
from apps.runs import periodic, reaper
from apps.runs.step import BACKGROUND, INTERACTIVE, SYSTEM

log = logging.getLogger("foray.worker")

DEFAULT_SHUTDOWN_TIMEOUT = 60.0
# How long the PDF browser gets to close after the lanes stop (design §13.5: Playwright closes
# last). Together with `pnpm dev:worker`'s 5 s graceful window it fits watchfiles' 15 s.
BROWSER_CLOSE_TIMEOUT_S = 10.0
UNAPPLIED = "There are unapplied migrations. Run pnpm db:migrate"
# A lane that lost the database starts again after this pause, doubling up to the maximum; one
# that had been running at least RESTART_RESET_AFTER_S starts its backoff over. Read at call time.
RESTART_DELAY_S = 1.0
RESTART_DELAY_MAX_S = 30.0
RESTART_RESET_AFTER_S = 60.0


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


async def _close_browser_bounded() -> None:
    """Closes the PDF browser, giving up after `BROWSER_CLOSE_TIMEOUT_S`: a close that never
    answers must not keep the worker from exiting (and from logging `stopped`)."""
    try:
        await asyncio.wait_for(_close_browser(), BROWSER_CLOSE_TIMEOUT_S)
    except TimeoutError:
        log.warning(
            "the PDF browser did not close within %ss; exiting without it", BROWSER_CLOSE_TIMEOUT_S
        )


def lost_the_database(error: BaseException) -> bool:
    """Whether a lane ended because its database connection failed (procrastinate wraps psycopg's
    errors in `ConnectorException`; a pool timeout is a psycopg `OperationalError`)."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ConnectorException | psycopg.OperationalError):
            return True
        current = current.__cause__ or current.__context__
    return False


async def _run_lane(
    lane: str, concurrency: int, shutdown_timeout: float, stop: asyncio.Event
) -> None:
    """One lane's procrastinate Worker, started again whenever it loses the database, until the
    worker stops. Any other ending returns or raises to `serve()`."""
    loop = asyncio.get_running_loop()
    delay = RESTART_DELAY_S
    while True:
        started = loop.time()
        try:
            await app.run_worker_async(
                queues=[lane],
                concurrency=concurrency,
                name=lane,
                install_signal_handlers=False,
                shutdown_graceful_timeout=shutdown_timeout,
            )
            return
        except Exception as error:
            if stop.is_set() or not lost_the_database(error):
                raise
            if loop.time() - started >= RESTART_RESET_AFTER_S:
                delay = RESTART_DELAY_S
            log.error(
                "lane %s lost the database (%s: %s); starting it again in %ss",
                lane,
                type(error).__name__,
                error,
                delay,
            )
        try:
            await asyncio.wait_for(stop.wait(), delay)
            return  # stopping: nothing to start again
        except TimeoutError:
            pass
        delay = min(delay * 2, RESTART_DELAY_MAX_S)


async def serve(
    shutdown_timeout: float,
    *,
    stop: asyncio.Event | None = None,
    install_signal_handlers: bool = True,
) -> int:
    """Run the three lanes until `stop` is set (SIGINT/SIGTERM set it). Returns the exit code:
    1 when a lane stopped on its own for any reason but a lost database connection, which
    `_run_lane` recovers from by starting the lane again."""
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
                        _run_lane(lane, concurrency, shutdown_timeout, stop),
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
        # Still under our handlers: after a terminal's Ctrl-C a second SIGINT follows (watchfiles
        # forwards its own), and with Python's default handler back it would interrupt the
        # Playwright close mid-way, leaving it waiting forever and `stopped` never logged.
        await _close_browser_bounded()
        for signum in signals:
            loop.remove_signal_handler(signum)
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
