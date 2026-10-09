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
