"""One worker per database (#51).

Two workers on one database refresh the same boards twice and double every request rate: the
gate that keeps multi-board refreshes apart (`jobs.refresh.MULTI_BOARD_GATE`) and the model
limiter each live in one process. So a worker holds a Postgres session advisory lock for as
long as it runs, and a second worker started against the same database waits behind it, doing
nothing: it installs no schedule and reaps no jobs until the lock is its own.

That is what lets a worker stay up between dev sessions (`pnpm worker:up`): the worker inside
`pnpm dev:all` waits behind it instead of running beside it, and takes over when it stops. The
lock belongs to its connection, so a worker that dies releases it without anyone noticing; one
whose connection breaks takes the lock back at once, or stops if another worker got there first.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

import psycopg
from procrastinate.contrib.django.utils import connector_params

__all__ = ["LOCK_ID", "WorkerLock"]

log = logging.getLogger("foray.worker")

# "Foray" in ASCII, then 1: any constant will do, as long as nothing else locks it.
LOCK_ID = 0x466F72617901
# How often a waiting worker asks again, and a running one checks that it still holds the lock.
WAIT_S = 5.0
CHECK_S = 15.0


async def _pause(stop: asyncio.Event, seconds: float) -> bool:
    """Waits up to `seconds`; True when `stop` was set meanwhile."""
    with suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), seconds)
    return stop.is_set()


class WorkerLock:
    def __init__(self) -> None:
        self._connection: psycopg.AsyncConnection | None = None
        # Set when the lock was lost to another worker: this one must stop.
        self.lost = asyncio.Event()

    async def _take(self) -> bool:
        """One attempt, on a new connection that is kept only when it holds the lock."""
        connection = await psycopg.AsyncConnection.connect(**connector_params(), autocommit=True)
        try:
            cursor = await connection.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_ID])
            row = await cursor.fetchone()
        except BaseException:
            await connection.close()
            raise
        if row is not None and row[0]:
            self._connection = connection
            return True
        await connection.close()
        return False

    async def acquire(self, stop: asyncio.Event) -> bool:
        """Waits until this process is the database's worker. False when `stop` came first."""
        announced = False
        while not stop.is_set():
            try:
                if await self._take():
                    return True
            except psycopg.OperationalError as error:
                log.warning("could not ask for the worker lock (%s); trying again", error)
            else:
                if not announced:
                    announced = True
                    log.info("another worker is running on this database; waiting for it to stop")
            if await _pause(stop, WAIT_S):
                break
        return False

    async def watch(self, stop: asyncio.Event) -> None:
        """Checks the lock's connection until `stop`. When it has broken, the lock went with it:
        take it back, or set `lost` if another worker already has."""
        while not await _pause(stop, CHECK_S):
            assert self._connection is not None, "watch() follows acquire()"
            try:
                await self._connection.execute("SELECT 1")
                continue
            except psycopg.Error as error:
                log.warning("the worker lock's connection broke (%s); taking the lock again", error)
            with suppress(Exception):
                await self._connection.close()
            self._connection = None
            while self._connection is None and not stop.is_set():
                try:
                    if not await self._take():
                        log.error("another worker took over this database; stopping this one")
                        self.lost.set()
                        return
                except psycopg.OperationalError:
                    if await _pause(stop, WAIT_S):
                        return

    async def release(self) -> None:
        if self._connection is not None:
            with suppress(Exception):
                await self._connection.close()
            self._connection = None
