"""Re-fetching companies into the mirror (design §10.5; `map/worker.md` §18.4).

- A board refresh fetches the whole board, ingests it with deactivation (and the empty-board
  guard, §10.4) and records the fetch. Only a 404 marks a company `invalid`: the board is gone,
  so it stops being retried every six hours. A timeout or a 500 records an error and leaves it
  active.
- A careers-page refresh crawls with the listing cache (§10.5), ingests without deactivation (an
  extraction that reads fewer roles is more likely a page that changed shape than a company that
  closed half its jobs), sets *and clears* `needs_browser`, and writes the cache back. A
  careers page is never marked invalid.
- `refresh_companies` runs 6 at a time and returns outcomes in input order.

**`MULTI_BOARD_GATE`.** Multi-board refreshes never overlap: the six-hourly cron and every
stale-set `company_refresh` run hold this lock for their whole fan-out, so two of them never
double the load on free public APIs. Single-company refreshes stay ungated. It assumes one worker
process, as the LLM limiter does (§8.7).

Model calls made here (careers pages) are attributed to whatever run is current: the refresh
sets no LLM context of its own (§8.8).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from django.db import transaction
from django.utils import timezone

from apps.core.db import db
from apps.core.http import HttpClient
from apps.jobs.ingest import IngestResult, ingest_jobs
from apps.jobs.models import Company, FetchStatus, SourceType
from apps.jobs.registry import record_fetch
from apps.jobs.schemas import ListingCache, dump_listing_cache, read_listing_cache
from apps.jobs.sources import board_adapter
from apps.jobs.sources.careers_page import fetch_careers_page
from apps.jobs.sources.types import BoardNotFound

log = logging.getLogger("foray.jobs")

# A board fetched longer ago than this is refreshed before a search and by the six-hourly cron.
BOARD_STALE_AFTER = timedelta(hours=2)
REFRESH_CONCURRENCY = 6

MULTI_BOARD_GATE = asyncio.Lock()

type RefreshStatus = Literal["ok", "not-found", "unsupported", "error"]


def stale_cutoff(now: datetime | None = None) -> datetime:
    return (now or timezone.now()) - BOARD_STALE_AFTER


@dataclass(slots=True)
class RefreshOutcome:
    company: str  # the company's name
    slug: str
    status: RefreshStatus
    jobs: int
    ingest: IngestResult | None = None
    error: str | None = None
    # Careers pages: model calls made (0 when every listing page was a cache hit).
    model_calls: int = 0


async def refresh_company(client: HttpClient, company: Company) -> RefreshOutcome:
    if company.source_type == SourceType.CAREERS_PAGE:
        return await _refresh_careers_page(client, company)

    adapter = board_adapter(company.source_type)
    if adapter is None or not company.ats_slug:
        return RefreshOutcome(company.name, company.slug, "unsupported", 0)

    try:
        jobs = await adapter.fetch_board(client, company.ats_slug)
        # A full board fetch is the only place a missing posting can be concluded closed.
        ingest = await db(ingest_jobs, company.id, jobs, deactivate_missing=True)
        await db(record_fetch, company.id, FetchStatus.OK, len(jobs))
    except BoardNotFound as error:
        await db(record_fetch, company.id, FetchStatus.NOT_FOUND, 0, mark_invalid=True)
        return RefreshOutcome(company.name, company.slug, "not-found", 0, error=str(error))
    except Exception as error:
        log.warning("refresh of %s failed: %s", company.slug, error)
        await db(record_fetch, company.id, FetchStatus.ERROR, None)
        return RefreshOutcome(company.name, company.slug, "error", 0, error=_message(error))
    return RefreshOutcome(company.name, company.slug, "ok", len(jobs), ingest=ingest)


async def refresh_companies(
    client: HttpClient, companies: Sequence[Company], concurrency: int = REFRESH_CONCURRENCY
) -> list[RefreshOutcome]:
    """Refreshes with a small pool (we are a guest on these APIs); results in input order."""
    gate = asyncio.Semaphore(concurrency)

    async def one(company: Company) -> RefreshOutcome:
        async with gate:
            return await refresh_company(client, company)

    async with asyncio.TaskGroup() as group:
        tasks = [group.create_task(one(company)) for company in companies]
    return [task.result() for task in tasks]


async def _refresh_careers_page(client: HttpClient, company: Company) -> RefreshOutcome:
    if not company.careers_urls:
        return RefreshOutcome(company.name, company.slug, "unsupported", 0)

    cache = read_listing_cache(company.listing_cache)
    reached: ListingCache = {}
    try:
        result = await fetch_careers_page(
            client,
            name=company.name,
            slug=company.slug,
            careers_urls=list(company.careers_urls),
            cache=cache,
            reached=reached,
        )
        ingest = await db(ingest_jobs, company.id, result.jobs)
        await db(
            _finish_careers_refresh,
            company.id,
            status=FetchStatus.NEEDS_BROWSER if result.needs_browser else FetchStatus.OK,
            job_count=len(result.jobs),
            needs_browser=result.needs_browser,
            listing_cache=result.listing_cache,
        )
    except Exception as error:
        log.warning("careers-page refresh of %s failed: %s", company.slug, error)
        # Keep every extraction this crawl paid for, and the entries it did not get to.
        await db(
            _finish_careers_refresh,
            company.id,
            status=FetchStatus.ERROR,
            job_count=None,
            needs_browser=None,
            listing_cache={**cache, **reached},
        )
        return RefreshOutcome(company.name, company.slug, "error", 0, error=_message(error))
    return RefreshOutcome(
        company.name,
        company.slug,
        "ok",
        len(result.jobs),
        ingest=ingest,
        model_calls=result.model_calls,
    )


def _finish_careers_refresh(
    company_id: uuid.UUID,
    *,
    status: str,
    job_count: int | None,
    needs_browser: bool | None,
    listing_cache: ListingCache,
) -> None:
    """One transaction: the fetch record, the `needs_browser` flag (set and cleared), and the
    listing cache written back."""
    with transaction.atomic():
        company = Company.objects.select_for_update().get(id=company_id)
        company.last_fetched_at = timezone.now()
        company.last_fetch_status = status
        if job_count is not None:
            company.last_fetch_job_count = job_count
        if needs_browser is not None:
            company.needs_browser = needs_browser
        company.listing_cache = dump_listing_cache(listing_cache)
        company.save()


def _message(error: BaseException) -> str:
    return str(error) or type(error).__name__
