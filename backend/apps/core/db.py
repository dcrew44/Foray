"""The one bridge from async code to the ORM (design §9.10, R9).

Async code never touches the ORM directly, and sync code never awaits. A transaction therefore
lives only inside a sync function run by `db()`, and an LLM call, which is async, can never
happen inside one.

Each call checks a connection out of the pool on a `db-*` thread and hands it back before
returning, so a long job or stream never pins one. The executor is as large as the pool, so a
burst of calls queues on threads, never on `PoolTimeout`.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import connections

DB_EXECUTOR = ThreadPoolExecutor(max_workers=settings.DB_POOL_MAX, thread_name_prefix="db")


async def db[**P, T](fn: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
    """Run a sync ORM function on a db thread and hand its connection back to the pool."""
    if inspect.iscoroutinefunction(fn):
        raise TypeError("db() runs sync functions only")

    def call() -> T:
        try:
            result = fn(*args, **kwargs)
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()  # never awaited; closing it avoids a RuntimeWarning
                raise TypeError("db() function returned an awaitable")
            return result
        finally:
            connections.close_all()  # resolved inside this thread; returns it to the pool

    # `executor` is read at call time, so a test can swap DB_EXECUTOR for a smaller one.
    return await sync_to_async(call, thread_sensitive=False, executor=DB_EXECUTOR)()
