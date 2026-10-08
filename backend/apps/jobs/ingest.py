"""Writing fetched postings into the shared mirror (design §10.4; `map/worker.md` §17.4).

Sync, one transaction per batch of 500; async callers use `await db(ingest_jobs, ...)`.

1. **Dedupe** the incoming jobs by id, the last copy winning: JSearch can repeat a `job_id`, and
   `ON CONFLICT DO UPDATE` cannot touch one row twice in a statement.
2. **Read** the stored `content_hash` of each id in the batch.
3. **Unchanged** postings get `last_seen_at`, `fetched_at` and `is_active = true` (a posting that
   reappears unchanged is open again; it used to stay inactive).
4. **New or changed** postings are upserted with `RETURNING (xmax = 0)`, which gives exact insert
   and update counts. `first_seen_at` is never updated, and `description_raw` changes only when
   the posting changed (invariant 4).
5. **Deactivation**, on complete and successful board fetches only (invariant 7): active
   postings of the company that were not in the fetch become inactive. They are kept, never
   deleted: the mirror is history. The **empty-board guard**: when a fetch returns nothing and
   the previous fetch found postings, nothing is deactivated (the caller then records a count
   of 0), so only a second consecutive empty fetch closes a company's postings. Careers pages
   and the aggregator never deactivate.

Text and JSON are written with lone surrogates replaced (psycopg refuses them).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from django.db import connection, transaction
from django.db.backends.utils import CursorWrapper

from apps.jobs.sources.dedupe import scrub
from apps.jobs.sources.types import NormalizedJob

BATCH_SIZE = 500


@dataclass(slots=True)
class IngestResult:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    deactivated: int = 0

    def add(self, other: IngestResult) -> None:
        self.inserted += other.inserted
        self.updated += other.updated
        self.unchanged += other.unchanged
        self.deactivated += other.deactivated


_COLUMNS = (
    "id, source, external_id, company_id, company_name, title, locations, location_norm, remote, "
    "employment_type, salary_min, salary_max, salary_currency, salary_period, description_raw, "
    "description_text, url, apply_url, posted_at, raw, content_hash, dedupe_key, is_active, "
    "first_seen_at, last_seen_at, fetched_at"
)
_ROW = (
    "(%s, %s, %s, %s, %s, %s, %s::text[], %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
    "%s::jsonb, %s, %s, true, now(), now(), now())"
)
# The source's columns; first_seen_at is deliberately not among them.
_UPDATED = (
    "company_id",
    "company_name",
    "title",
    "locations",
    "location_norm",
    "remote",
    "employment_type",
    "salary_min",
    "salary_max",
    "salary_currency",
    "salary_period",
    "description_raw",
    "description_text",
    "url",
    "apply_url",
    "posted_at",
    "raw",
    "content_hash",
    "dedupe_key",
)
_UPSERT_TAIL = (
    " ON CONFLICT (id) DO UPDATE SET "
    + ", ".join(f"{column} = EXCLUDED.{column}" for column in _UPDATED)
    + ", is_active = true, last_seen_at = now(), fetched_at = now()"
    + " RETURNING (xmax = 0) AS inserted"
)


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(scrub(value), ensure_ascii=False)


def _text(value: str | None) -> str | None:
    return None if value is None else scrub(value)


def _row(company_id: uuid.UUID | None, job: NormalizedJob) -> list[Any]:
    return [
        job.id,
        job.source,
        job.external_id,
        company_id,
        _text(job.company_name),
        _text(job.title),
        [scrub(location) for location in job.locations],
        _json(job.location_norm.dump() if job.location_norm else None),
        job.remote,
        _text(job.employment_type),
        job.salary.min,
        job.salary.max,
        _text(job.salary.currency),
        job.salary.period,
        _text(job.description_raw),
        _text(job.description_text),
        _text(job.url),
        _text(job.apply_url),
        job.posted_at,
        _json(job.raw),
        job.content_hash,
        job.dedupe_key,
    ]


def ingest_jobs(
    company_id: uuid.UUID | None,
    jobs: Sequence[NormalizedJob],
    *,
    deactivate_missing: bool = False,
) -> IngestResult:
    """Upserts `jobs` into the mirror for `company_id` (None for aggregator rows)."""
    unique = list({job.id: job for job in jobs}.values())
    result = IngestResult()
    for offset in range(0, len(unique), BATCH_SIZE):
        with transaction.atomic(), connection.cursor() as cursor:
            result.add(_ingest_batch(cursor, company_id, unique[offset : offset + BATCH_SIZE]))

    if deactivate_missing and company_id is not None:
        result.deactivated = _deactivate_missing(company_id, [job.id for job in unique])
    return result


def _ingest_batch(
    cursor: CursorWrapper, company_id: uuid.UUID | None, batch: Sequence[NormalizedJob]
) -> IngestResult:
    result = IngestResult()
    cursor.execute(
        "SELECT id, content_hash FROM jobs WHERE id = ANY(%s)", [[job.id for job in batch]]
    )
    known = dict(cursor.fetchall())

    unchanged = [job.id for job in batch if known.get(job.id) == job.content_hash]
    changed = [job for job in batch if known.get(job.id) != job.content_hash]

    # Unchanged postings still need last_seen_at moved forward: that is how we know the job is
    # still open, as distinct from still in the table.
    if unchanged:
        cursor.execute(
            "UPDATE jobs SET last_seen_at = now(), fetched_at = now(), is_active = true "
            "WHERE id = ANY(%s)",
            [unchanged],
        )
        result.unchanged = len(unchanged)

    if changed:
        params = [value for job in changed for value in _row(company_id, job)]
        cursor.execute(
            f"INSERT INTO jobs ({_COLUMNS}) VALUES "
            + ", ".join([_ROW] * len(changed))
            + _UPSERT_TAIL,
            params,
        )
        inserted = sum(1 for (was_inserted,) in cursor.fetchall() if was_inserted)
        result.inserted = inserted
        result.updated = len(changed) - inserted
    return result


def _deactivate_missing(company_id: uuid.UUID, seen: list[str]) -> int:
    with transaction.atomic(), connection.cursor() as cursor:
        if not seen:
            # The empty-board guard: one empty answer after a non-empty one closes nothing.
            cursor.execute(
                "SELECT last_fetch_job_count FROM companies WHERE id = %s FOR UPDATE",
                [company_id],
            )
            row = cursor.fetchone()
            if row is not None and (row[0] or 0) > 0:
                return 0
        cursor.execute(
            "UPDATE jobs SET is_active = false, fetched_at = now() "
            "WHERE company_id = %s AND is_active AND NOT (id = ANY(%s))",
            [company_id, seen],
        )
        return cursor.rowcount
