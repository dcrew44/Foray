"""`manage.py worker` always finishes its shutdown (design §9.1, §13.5).

The PDF browser closes last, after the lanes have stopped. Seen live under `pnpm dev:worker`: a
terminal's Ctrl-C reaches the whole process group, watchfiles then forwards a second SIGINT, and
that one used to land after the worker had restored Python's default handler, interrupting the
Playwright close mid-way; the close never answered and the worker never exited, without
`stopped` in its log. Now the worker keeps its handler until the browser is closed, and the
close is bounded: a close that still hangs is logged and abandoned.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from typing import Any

import pytest

from apps.resumes import render
from apps.runs.management.commands import worker as worker_command
from tests.runs import support

BOUND = 0.5


@pytest.mark.django_db(transaction=True)
def test_a_browser_close_that_never_returns_still_lets_the_worker_stop(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    closing: list[str] = []

    async def never_closes() -> None:
        closing.append("asked")
        await asyncio.Event().wait()  # nothing ever sets it

    monkeypatch.setattr(render, "close_browser", never_closes)
    monkeypatch.setattr(worker_command, "BROWSER_CLOSE_TIMEOUT_S", BOUND)
    monkeypatch.setattr(worker_command.periodic, "install", lambda *_, **__: [])

    async def main() -> tuple[int, float]:
        stop = asyncio.Event()
        asyncio.get_running_loop().call_later(1.0, stop.set)  # as a SIGINT would, once up
        started = time.monotonic()
        with support.empty_periodic_registry():
            # The outer bound turns a regression into a failure instead of a hung suite.
            code = await asyncio.wait_for(
                worker_command.serve(1, stop=stop, install_signal_handlers=False), 30
            )
        return code, time.monotonic() - started

    with caplog.at_level(logging.INFO, logger="foray.worker"):
        code, took = asyncio.run(main())

    assert code == 0
    assert closing == ["asked"]
    messages = [record.getMessage() for record in caplog.records if record.name == "foray.worker"]
    assert f"the PDF browser did not close within {BOUND}s; exiting without it" in messages
    assert messages[-1] == "stopped"
    # The lanes run for the first second; stopping them is quick, and the close adds its bound.
    assert took < 1.0 + 5 + BOUND


@pytest.mark.django_db(transaction=True)
def test_a_second_sigint_during_the_close_does_not_interrupt_it(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """After a terminal's Ctrl-C, watchfiles forwards a second SIGINT to the worker. It used to
    arrive after the worker had handed SIGINT back to Python's default handler, so it raised
    `KeyboardInterrupt` in the middle of the Playwright close (seen live: `stopped` was never
    logged). The worker keeps its own handler until the browser is closed."""
    closed: list[str] = []

    async def close_hit_by_a_second_sigint() -> None:
        os.kill(os.getpid(), signal.SIGINT)  # the one watchfiles forwards
        await asyncio.sleep(0.2)  # the signal is handled on the loop meanwhile
        closed.append("closed")

    monkeypatch.setattr(render, "close_browser", close_hit_by_a_second_sigint)
    monkeypatch.setattr(worker_command.periodic, "install", lambda *_, **__: [])

    async def main() -> int:
        # The shutdown itself starts with a SIGINT, as Ctrl-C does.
        asyncio.get_running_loop().call_later(1.0, os.kill, os.getpid(), signal.SIGINT)
        with support.empty_periodic_registry():
            return await asyncio.wait_for(worker_command.serve(1), 30)

    with caplog.at_level(logging.INFO, logger="foray.worker"):
        try:
            code = asyncio.run(main())
        except KeyboardInterrupt:
            pytest.fail("a second SIGINT interrupted the shutdown")
    assert code == 0
    assert closed == ["closed"]
    messages = [record.getMessage() for record in caplog.records if record.name == "foray.worker"]
    assert messages[-1] == "stopped"
    # The worker hands SIGINT back to Python once it is done.
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def test_the_worker_bound_fits_the_dev_reload_window() -> None:
    """`pnpm dev:worker` gives running jobs 5 s and watchfiles waits 15 s before a SIGKILL
    (§13.5): the browser's bound must fit what is left, and each half of the close in it."""
    assert worker_command.BROWSER_CLOSE_TIMEOUT_S <= 10
    assert 2 * render.CLOSE_TIMEOUT_S <= worker_command.BROWSER_CLOSE_TIMEOUT_S


# --- a lost database ------------------------------------------------------------------------------


def _serve_for(seconds: float) -> int:
    async def main() -> int:
        stop = asyncio.Event()
        asyncio.get_running_loop().call_later(seconds, stop.set)
        with support.empty_periodic_registry():
            return await asyncio.wait_for(
                worker_command.serve(1, stop=stop, install_signal_handlers=False), 30
            )

    return asyncio.run(main())


@pytest.mark.django_db(transaction=True)
def test_a_lane_that_loses_the_database_is_started_again(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A Postgres restart longer than the pool's 30 s wait used to end a lane (procrastinate's
    heartbeat has no error handling), and serve() then stopped every lane and exited 1, so no
    background work ran until someone restarted the worker. Now the lane starts again."""
    from procrastinate.exceptions import ConnectorException
    from psycopg_pool import PoolTimeout

    from apps.runs.step import SYSTEM

    real = worker_command.app.run_worker_async
    started: list[str] = []

    async def loses_the_database_once(**options: Any) -> None:
        lane = options["name"]
        started.append(lane)
        if lane == SYSTEM and started.count(SYSTEM) <= 2:
            await asyncio.sleep(0.05)
            raise ConnectorException("heartbeat failed") from PoolTimeout(
                "couldn't get a connection after 30.00 sec"
            )
        await real(**options)

    monkeypatch.setattr(worker_command.app, "run_worker_async", loses_the_database_once)
    monkeypatch.setattr(worker_command, "RESTART_DELAY_S", 0.1)
    monkeypatch.setattr(worker_command.periodic, "install", lambda *_, **__: [])
    with caplog.at_level(logging.INFO, logger="foray.worker"):
        code = _serve_for(2.0)

    assert code == 0
    assert started.count(SYSTEM) == 3  # lost twice, started again twice
    assert started.count("interactive") == started.count("background") == 1  # untouched
    messages = [record.getMessage() for record in caplog.records if record.name == "foray.worker"]
    lost = [m for m in messages if m.startswith(f"lane {SYSTEM} lost the database")]
    assert len(lost) == 2
    assert lost[0].endswith("starting it again in 0.1s") and lost[1].endswith("again in 0.2s")
    assert not any("stopped on its own" in m for m in messages)
    assert messages[-1] == "stopped"


@pytest.mark.django_db(transaction=True)
def test_a_lane_that_fails_for_another_reason_still_stops_the_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = worker_command.app.run_worker_async

    async def broken(**options: Any) -> None:
        if options["name"] == "system":
            raise RuntimeError("a bug, not an outage")
        await real(**options)

    monkeypatch.setattr(worker_command.app, "run_worker_async", broken)
    monkeypatch.setattr(worker_command.periodic, "install", lambda *_, **__: [])
    started = time.monotonic()
    assert _serve_for(20.0) == 1
    assert time.monotonic() - started < 15  # it did not wait for the stop


def test_only_connection_failures_count_as_a_lost_database() -> None:
    import psycopg
    from procrastinate.exceptions import ConnectorException

    assert worker_command.lost_the_database(ConnectorException("x"))
    assert worker_command.lost_the_database(psycopg.OperationalError("server closed"))
    wrapped = RuntimeError("outer")
    wrapped.__cause__ = psycopg.OperationalError("inner")
    assert worker_command.lost_the_database(wrapped)
    assert not worker_command.lost_the_database(RuntimeError("a bug"))
    assert not worker_command.lost_the_database(psycopg.errors.UniqueViolation("dup"))
