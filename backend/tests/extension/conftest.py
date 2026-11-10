"""Fixtures for the browser extension's tests: a real Chromium with the extension loaded, a
fake Foray, and the replica forms. See `support.py`.

Set `FORAY_EXTENSION_HEADED=1` to watch a test in a window.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack
from pathlib import Path

import pytest
from playwright.sync_api import Playwright

from tests.extension.support import Browser, FakeForay, Pages, browser, playwright_chromium


@pytest.fixture
def chromium() -> Iterator[Playwright]:
    """Playwright with its own Chromium, for one test.

    Not for the session: Playwright's sync API keeps an event loop running on this thread
    for as long as it is open, and while it does Django refuses every database call and
    pytest-asyncio cannot start a loop of its own. Opened and closed around each test, it
    leaves the rest of the suite alone."""
    with playwright_chromium() as playwright:
        yield playwright


@pytest.fixture
def foray() -> Iterator[FakeForay]:
    server = FakeForay()
    server.start()
    yield server
    server.stop()


type Open = Callable[..., Browser]


@pytest.fixture
def open_browser(chromium: Playwright, foray: FakeForay, tmp_path: Path) -> Iterator[Open]:
    """`open_browser(pages, connected=True)`: the browser, closed when the test ends."""
    with ExitStack() as stack:

        def start(pages: Pages | None = None, *, connected: bool = True) -> Browser:
            manager: AbstractContextManager[Browser] = browser(
                chromium,
                foray,
                tmp_path,
                pages=pages,
                connected=connected,
                headless=os.environ.get("FORAY_EXTENSION_HEADED") != "1",
            )
            return stack.enter_context(manager)

        yield start
