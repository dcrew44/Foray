"""R9: `db()` runs sync functions only, on `db-*` threads, and hands every connection back."""

from __future__ import annotations

import threading
from collections.abc import Awaitable

import pytest
from django.db import connection, connections

from apps.core.db import db

pytestmark = pytest.mark.django_db(transaction=True)


def _checked_out() -> int:
    pool = getattr(connections["default"], "pool", None)
    assert pool is not None, "the test database must use Django's connection pool"
    stats = pool.get_stats()
    return stats["pool_size"] - stats["pool_available"]


def _select_one() -> int:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        row = cursor.fetchone()
    assert row is not None
    return row[0]


async def test_db_rejects_coroutines() -> None:
    async def coroutine_function() -> int:
        return 1

    with pytest.raises(TypeError, match="sync functions only"):
        await db(coroutine_function)

    def returns_awaitable() -> Awaitable[int]:
        return coroutine_function()

    with pytest.raises(TypeError, match="returned an awaitable"):
        await db(returns_awaitable)


async def test_db_returns_connection() -> None:
    assert await db(_select_one) == 1  # the pool now exists
    before = await db(_checked_out)
    for _ in range(100):
        assert await db(_select_one) == 1
    # The probe itself holds one connection while it reads the stats, before and after.
    assert await db(_checked_out) == before


async def test_db_runs_on_db_threads() -> None:
    def thread_name() -> str:
        return threading.current_thread().name

    name = await db(thread_name)
    assert name.startswith("db")
    assert name != threading.current_thread().name


async def test_db_passes_arguments_and_exceptions() -> None:
    def add(a: int, *, b: int) -> int:
        return a + b

    assert await db(add, 2, b=3) == 5

    def boom() -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await db(boom)
