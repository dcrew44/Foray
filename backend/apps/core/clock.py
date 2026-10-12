"""The one clock stored timestamps use: the database's (step H2).

Model timestamps default to `Now()` (Postgres `statement_timestamp()`), raw SQL writes `now()`,
and comparisons with "now" are made in SQL. Python code that must compare a stored timestamp with
the present in Python asks the database for the present here, so a skew between this process's
clock and the database's (the Docker VM's clock falls behind the host's after a sleep) can never
move a boundary. Sync: run it through `db()` from async code.
"""

from __future__ import annotations

from datetime import UTC, datetime

from django.db import connection

__all__ = ["database_now", "month_start"]


def database_now() -> datetime:
    """The database's `statement_timestamp()`, the value `Now()` writes."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT statement_timestamp()")
        row = cursor.fetchone()
    assert row is not None
    value: datetime = row[0]
    return value


def month_start(now: datetime | None = None) -> datetime:
    """The first of `now`'s month (default: the database's present), UTC: where spend is counted
    from, as the budget guard counts it (`date_trunc('month', now() AT TIME ZONE 'UTC')`)."""
    at = (now if now is not None else database_now()).astimezone(UTC)
    return datetime(at.year, at.month, 1, tzinfo=UTC)
