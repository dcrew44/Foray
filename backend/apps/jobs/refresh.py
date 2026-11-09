"""Re-fetching companies into the mirror (design §10.5; `map/worker.md` §18.4).

- A board refresh is a poll (`sources/poll.py`, #55): it asks whether the board changed, and
  only then reads it whole, ingests it with deactivation (and the empty-board guard, §10.4) and
  records the fetch. An unchanged board records a successful check and touches no posting. Only
  a 404 marks a company `invalid`: the board is gone, so it stops being retried. A timeout or a
  500 records an error and leaves it active, and each failure in a row doubles its wait
  (`registry.poll_interval`). `force` skips the question (a person pressed "Fetch now").
- A careers-page refresh crawls with the listing cache (§10.5), ingests without deactivation (an
  extraction that reads fewer roles is more likely a page that changed shape than a company that
  closed half its jobs), sets *and clears* `needs_browser`, and writes the cache back. A
  careers page is never marked invalid.
- `refresh_companies` runs 6 at a time, at most 4 of them against one source, and returns
  outcomes in input order.

**`MULTI_BOARD_GATE`.** Multi-board refreshes never overlap: the scheduled refresh (every 15
minutes) and every stale-set `company_refresh` run hold this lock for their whole fan-out, so
two of them never double the load on free public APIs. Single-company refreshes stay ungated. It
assumes one worker process, as the LLM limiter does (§8.7).

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
from django.db.models.functions import Now
from django.utils import timezone

from apps.core.db import db
from apps.core.http import HttpClient
from apps.jobs.ingest import IngestResult, ingest_jobs
from apps.jobs.level import is_early_career_title
from apps.jobs.models import Company, FetchStatus, SourceType
from apps.jobs.registry import MAX_FAILURES, record_fetch
from apps.jobs.schemas import ListingCache, dump_listing_cache, read_listing_cache
from apps.jobs.sources import board_adapter
from apps.jobs.sources.careers_page import fetch_careers_page
from apps.jobs.sources.poll import PollState, poll_board
from apps.jobs.sources.types import BoardNotFound

log = logging.getLogger("foray.jobs")

# A board fetched longer ago than this is refreshed before a search. The scheduled refresh
# keeps its own, shorter intervals (`registry.due_companies`), so with a worker running nothing
# is ever this old.
BOARD_STALE_AFTER = timedelta(hours=2)
REFRESH_CONCURRENCY = 6
# We are a guest on these APIs: no source sees more than this many of the pool at once.
PER_SOURCE_CONCURRENCY = 4

MULTI_BOARD_GATE = asyncio.Lock()

type RefreshStatus = Literal["ok", "not-found", "unsupported", "error"]


def stale_cutoff(now: datetime | None = None) -> datetime:
    return (now or timezone.now()) - BOARD_STALE_AFTER


@dataclass(slots=True)
class RefreshOutcome:
    company: str  # the company's name
    slug: str
    status: RefreshStatus
    jobs: int  # postings on the board (the last count known, when it is unchanged)
    ingest: IngestResult | None = None
    error: str | None = None
    # The board answered that nothing changed since the last read: nothing was ingested.
    unchanged: bool = False
    # Careers pages: model calls made (0 when every listing page was a cache hit).
    model_calls: int = 0


async def refresh_company(
    client: HttpClient, company: Company, *, force: bool = False
) -> RefreshOutcome:
    if company.source_type == SourceType.CAREERS_PAGE:
        return await _refresh_careers_page(client, company)

    adapter = board_adapter(company.source_type)
    if adapter is None or not company.ats_slug:
        return RefreshOutcome(company.name, company.slug, "unsupported", 0)

    state = PollState.read(company.poll_state)
    try:
        polled = await poll_board(
            client,
            adapter,
            company.ats_slug,
            state,
            company_id=company.id,
            now=timezone.now(),
            force=force,
        )
        remembered = polled.state.dump() if polled.state != state else None
        if polled.jobs is None:
            await db(record_fetch, company.id, FetchStatus.OK, None, poll_state=remembered)
            return RefreshOutcome(
                company.name, company.slug, "ok", company.last_fetch_job_count or 0, unchanged=True
            )
        jobs = polled.jobs
        if not jobs and (company.last_fetch_job_count or 0) > 0:
            # The empty-board guard is about to hold (§10.4): one empty answer closes nothing,
            # and only a second one does. Remembering this answer would make the next poll say
            # "unchanged" and never ask the second time, so the state is dropped instead.
            remembered = {}
        # A full board fetch is the only place a missing posting can be concluded closed.
        ingest = await db(
            ingest_jobs,
            company.id,
            jobs,
            deactivate_missing=True,
            previous_ok=company.last_ok_at,
        )
        await db(
            record_fetch,
            company.id,
            FetchStatus.OK,
            len(jobs),
            poll_state=remembered,
            relevant=any(is_early_career_title(job.title) for job in jobs),
        )
    except BoardNotFound as error:
        await db(record_fetch, company.id, FetchStatus.NOT_FOUND, 0, mark_invalid=True)
        return RefreshOutcome(company.name, company.slug, "not-found", 0, error=str(error))
    except Exception as error:
        log.warning("refresh of %s failed: %s", company.slug, error)
        await db(record_fetch, company.id, FetchStatus.ERROR, None)
        return RefreshOutcome(company.name, company.slug, "error", 0, error=_message(error))
    return RefreshOutcome(company.name, company.slug, "ok", len(jobs), ingest=ingest)


async def refresh_companies(
    client: HttpClient,
    companies: Sequence[Company],
    concurrency: int = REFRESH_CONCURRENCY,
    *,
    force: bool = False,
) -> list[RefreshOutcome]:
    """Refreshes with a small pool (we are a guest on these APIs); results in input order."""
    gate = asyncio.Semaphore(concurrency)
    per_source: dict[str, asyncio.Semaphore] = {}

    async def one(company: Company) -> RefreshOutcome:
        source = per_source.setdefault(
            company.source_type, asyncio.Semaphore(PER_SOURCE_CONCURRENCY)
        )
        async with source, gate:
            return await refresh_company(client, company, force=force)

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
        if status == FetchStatus.ERROR:
            company.fetch_failures = min(int(company.fetch_failures) + 1, MAX_FAILURES)
        else:
            company.last_ok_at = Now()
            company.fetch_failures = 0
        if job_count is not None:
            company.last_fetch_job_count = job_count
        if needs_browser is not None:
            company.needs_browser = needs_browser
        company.listing_cache = dump_listing_cache(listing_cache)
        company.save()


def _message(error: BaseException) -> str:
    return str(error) or type(error).__name__
