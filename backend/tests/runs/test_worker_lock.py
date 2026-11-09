"""One worker per database (#51): a second worker waits behind the first and takes over when it
stops, so a worker kept up between dev sessions never runs beside `pnpm dev:all`'s own."""

from __future__ import annotations

import asyncio
import logging

import psycopg
import pytest
from procrastinate.contrib.django.utils import connector_params

from apps.runs import worker_lock
from apps.runs.management.commands import worker as worker_command
from apps.runs.worker_lock import LOCK_ID, WorkerLock
from tests.runs import support

db = pytest.mark.django_db(transaction=True)


def _other_worker() -> psycopg.Connection:
    """A connection holding the lock, as a worker already running would."""
    connection = psycopg.connect(**connector_params(), autocommit=True)
    assert connection.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_ID]).fetchone() == (True,)
    return connection


@db
def test_a_second_worker_waits_and_takes_over_when_the_first_stops(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(worker_lock, "WAIT_S", 0.1)
    monkeypatch.setattr(worker_command.periodic, "install", lambda *_, **__: [])
    first = _other_worker()

    async def main() -> int:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        loop.call_later(1.0, first.close)  # the first worker stops
        loop.call_later(2.5, stop.set)
        with support.empty_periodic_registry():
            return await asyncio.wait_for(
                worker_command.serve(1, stop=stop, install_signal_handlers=False), 30
            )

    with caplog.at_level(logging.INFO, logger="foray.worker"):
        code = asyncio.run(main())

    assert code == 0
    messages = [record.getMessage() for record in caplog.records if record.name == "foray.worker"]
    waiting = messages.index("another worker is running on this database; waiting for it to stop")
    ready = next(n for n, message in enumerate(messages) if message.startswith("ready (pid"))
    # Nothing was installed or reaped while it waited: the startup line comes after the wait.
    startup = next(n for n, message in enumerate(messages) if message.startswith("startup:"))
    assert waiting < startup < ready
    assert messages[-1] == "stopped"


@db
def test_a_worker_stopped_while_waiting_never_starts(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(worker_lock, "WAIT_S", 0.1)
    first = _other_worker()

    async def main() -> int:
        stop = asyncio.Event()
        asyncio.get_running_loop().call_later(0.5, stop.set)
        return await asyncio.wait_for(
            worker_command.serve(1, stop=stop, install_signal_handlers=False), 30
        )

    try:
        with caplog.at_level(logging.INFO, logger="foray.worker"):
            code = asyncio.run(main())
    finally:
        first.close()
    messages = [record.getMessage() for record in caplog.records if record.name == "foray.worker"]
    assert code == 0
    assert not any(message.startswith(("startup:", "ready")) for message in messages)
    assert messages[-1] == "stopped while waiting for the other worker"


@db
def test_a_worker_whose_lock_went_to_another_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lock's connection breaks (a database restart) and another worker is quicker."""
    monkeypatch.setattr(worker_lock, "CHECK_S", 0.1)

    async def main() -> bool:
        stop = asyncio.Event()
        lock = WorkerLock()
        assert await lock.acquire(stop)
        watching = asyncio.create_task(lock.watch(stop))
        assert lock._connection is not None
        await lock._connection.close()  # the connection is gone, and the lock with it
        other = _other_worker()
        try:
            await asyncio.wait_for(lock.lost.wait(), 5)
        finally:
            other.close()
            stop.set()
            await watching
            await lock.release()
        return lock.lost.is_set()

    assert asyncio.run(main()) is True


@db
def test_a_broken_lock_connection_is_taken_back_when_nobody_else_wants_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker_lock, "CHECK_S", 0.1)

    async def main() -> tuple[bool, bool]:
        stop = asyncio.Event()
        lock = WorkerLock()
        assert await lock.acquire(stop)
        watching = asyncio.create_task(lock.watch(stop))
        broken = lock._connection
        assert broken is not None
        await broken.close()
        for _ in range(50):
            await asyncio.sleep(0.1)
            if lock._connection is not None and lock._connection is not broken:
                break
        retaken = lock._connection is not None and lock._connection is not broken
        stop.set()
        await watching
        await lock.release()
        return retaken, lock.lost.is_set()

    assert asyncio.run(main()) == (True, False)
