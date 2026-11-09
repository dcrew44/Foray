"""Posting facts in the mirror (#56): what `facts.read_facts` reads, as the `jobs` columns.

Ingest writes them with every new or changed posting (`ingest.py`). Postings that were in the
mirror before facts existed, or were read by an older `facts.VERSION`, are caught up here a
batch at a time: after each scheduled refresh (`jobs.tasks`) and by `manage.py read_facts`.
Sync; async callers use `db()`.
"""

from __future__ import annotations

import json
from typing import Any

from django.db import connection, transaction

from apps.jobs import facts

__all__ = ["COLUMNS", "STALE_BATCH", "count_stale", "fact_values", "read_stale"]

# In the order `fact_values` returns them.
COLUMNS = ("level", "years_min", "years_preferred", "work_auth", "clearance", "facts")
# How many postings one catch-up pass reads: a few seconds of pattern matching.
STALE_BATCH = 5000
_STALE = "facts_version IS NULL OR facts_version < %s"


def fact_values(title: str, text: str | None) -> list[Any]:
    """The values of `COLUMNS` for one posting (`facts` as JSON text)."""
    read = facts.read_facts(title, text)
    return [
        str(read.level),
        read.years_min,
        read.years_preferred,
        str(read.work_auth),
        str(read.clearance),
        json.dumps(read.dump(), ensure_ascii=False),
    ]


def count_stale() -> int:
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT count(*) FROM jobs WHERE {_STALE}", [facts.VERSION])
        row = cursor.fetchone()
    return int(row[0]) if row else 0


def read_stale(limit: int = STALE_BATCH) -> int:
    """Reads the facts of up to `limit` postings that have none, or an older version's, newest
    first (what a search reaches first). Returns how many it read; 0 means none are left."""
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT id, title, description_text FROM jobs WHERE {_STALE} "
            "ORDER BY first_seen_at DESC LIMIT %s",
            [facts.VERSION, limit],
        )
        rows = cursor.fetchall()
    if not rows:
        return 0
    values = [[job_id, *fact_values(title, text)] for job_id, title, text in rows]
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "UPDATE jobs j SET level = v.level, years_min = v.years_min::smallint, "
            "years_preferred = v.years_preferred::smallint, work_auth = v.work_auth, "
            "clearance = v.clearance, facts = v.facts::jsonb, facts_version = %s "
            "FROM (VALUES "
            + ", ".join(["(%s, %s, %s, %s, %s, %s, %s)"] * len(values))
            + ") AS v(id, level, years_min, years_preferred, work_auth, clearance, facts) "
            "WHERE j.id = v.id",
            [facts.VERSION, *(value for row in values for value in row)],
        )
    return len(rows)
