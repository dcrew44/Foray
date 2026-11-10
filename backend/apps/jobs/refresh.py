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
- A board on a source that lists postings without their descriptions (Workday,
  SmartRecruiters, iCIMS portals: `ListedAdapter`, #57) is read incrementally
  (`_refresh_listed`): one cheap probe says whether anything changed; if it did, the listing
  says what is open, only postings the mirror has not read in that version are fetched (a few
  hundred a pass), and what a complete listing no longer names is closed. What a pass could
  not read is written down (`backlog.py`), and the board goes on from that on later passes
  without being asked or listed again. Such a board is being caught up, and so is one never
  read: the fifteen-minute pass leaves both to `companies.catch_up`, which runs beside it
  (#84), so a backlog of big boards never delays the poll of the boards already followed.
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
from apps.jobs.backlog import drop_backlog, read_backlog, save_backlog
from apps.jobs.ingest import (
    IngestResult,
    close_missing,
    ingest_jobs,
    listing_versions,
    set_listing_versions,
)
from apps.jobs.level import is_early_career_title
from apps.jobs.models import Company, FetchStatus, Job, JobLevel, SourceType
from apps.jobs.registry import MAX_FAILURES, is_catching_up, record_fetch
from apps.jobs.schemas import ListingCache, dump_listing_cache, read_listing_cache
from apps.jobs.sources import board_adapter, listed_adapter
from apps.jobs.sources.careers_page import fetch_careers_page
from apps.jobs.sources.poll import PollState, full_read_due, poll_board
from apps.jobs.sources.types import BoardNotFound, ListedAdapter, ListedPosting, NormalizedJob

log = logging.getLogger("foray.jobs")

# A board fetched longer ago than this is refreshed before a search. The scheduled refresh
# keeps its own, shorter intervals (`registry.due_companies`), so with a worker running nothing
# is ever this old.
BOARD_STALE_AFTER = timedelta(hours=2)
REFRESH_CONCURRENCY = 6
# We are a guest on these APIs: no source sees more than this many of the pool at once.
PER_SOURCE_CONCURRENCY = 4
# A listed board (#57) costs a request per posting. One pass reads at most this many of one
# board's, a few at a time; the rest wait for the next pass. "A few at a time" is per host: a
# Workday tenant and an iCIMS portal each answer on their own, SmartRecruiters is one API for
# every company, and no pass has more than `ALL_POSTINGS_CONCURRENCY` such requests in the air.
POSTINGS_PER_PASS = 300
POSTING_CONCURRENCY = 4
ALL_POSTINGS_CONCURRENCY = 16
OWN_HOST_SOURCES = frozenset({"workday", "icims"})
# And one pass reads at most this many postings of one source, whatever the number of boards
# (#58): a burst of new postings on boards already followed cannot hold the fifteen-minute
# poll of every other board.
SOURCE_POSTINGS_PER_PASS = 1200
# Boards being caught up (never read, or part-read) are read by a pass of their own
# (`pipeline.catch_up`, #84) with a budget of its own, so a backlog never spends what a board
# already followed needs for the posting that went up a minute ago. Its size is about the
# hosts, not the clock: a source whose boards each answer on their own host is read more of
# in a pass than one API that serves every company. Once a source's is spent, the rest of its
# boards wait their turn without being asked anything.
CATCH_UP_POSTINGS_PER_PASS = 3000

# Sources that answer 429 to anything faster (#86): boards of these are asked one at a time,
# this many seconds apart, by the scheduled refresh and by `candidates.grow` alike.
SOURCE_PACE_S: dict[str, float] = {"workable": 1.0}

MULTI_BOARD_GATE = asyncio.Lock()
# One catch-up at a time. It is not the gate above: the two passes read different boards, and
# the timed one must never wait for this one.
CATCH_UP_GATE = asyncio.Lock()

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

    listed = listed_adapter(company.source_type)
    if listed is not None and company.ats_slug:
        return await _refresh_listed(client, company, listed, force=force)

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


def _lists_early_career(company_id: uuid.UUID) -> bool:
    """Whether the company has an open early-career posting, by the facts read at ingest."""
    early = (JobLevel.INTERN, JobLevel.NEW_GRAD, JobLevel.ENTRY)
    return Job.objects.filter(company_id=company_id, is_active=True, level__in=early).exists()


async def _read_postings(
    client: HttpClient, adapter: ListedAdapter, company: Company, wanted: Sequence[ListedPosting]
) -> tuple[list[tuple[ListedPosting, NormalizedJob]], list[ListedPosting]]:
    """Reads each wanted posting, a few at a time for its host. Returns those read and those
    that failed; one that has gone since the listing is neither."""
    slug = company.ats_slug or ""
    gate = client.gate(f"postings:{_host(adapter.source, slug)}", POSTING_CONCURRENCY)
    overall = client.gate("postings", ALL_POSTINGS_CONCURRENCY)

    async def one(posting: ListedPosting) -> NormalizedJob | None | Exception:
        async with gate, overall:
            try:
                return await adapter.read_posting(client, slug, posting, company.name)
            except Exception as error:
                return error

    async with asyncio.TaskGroup() as group:
        tasks = [group.create_task(one(posting)) for posting in wanted]
    read: list[tuple[ListedPosting, NormalizedJob]] = []
    failed: list[ListedPosting] = []
    for posting, task in zip(wanted, tasks, strict=True):
        result = task.result()
        if isinstance(result, Exception):
            failed.append(posting)
            log.debug("reading %s:%s failed: %s", company.slug, posting.external_id, result)
        elif result is not None:
            read.append((posting, result))
    return read, failed


def _host(source: str, slug: str) -> str:
    """Whose server a listed board is on, as a key for the client's gates: the tenant's own
    host for a source that has one (two Workday sites of one company share it, and Workday
    answers 429 when they are read side by side), the source itself for one shared API."""
    if source not in OWN_HOST_SOURCES:
        return source
    return f"{source}:{slug.split('/', 1)[0].lower()}"


def _budget(source: str, catching_up: bool) -> tuple[str, int]:
    """The pass's budget a listed board's postings are taken from: its name and its size."""
    if not catching_up:
        return f"postings:{source}", SOURCE_POSTINGS_PER_PASS
    own_host = source in OWN_HOST_SOURCES
    return f"catch-up:{source}", (
        CATCH_UP_POSTINGS_PER_PASS if own_host else SOURCE_POSTINGS_PER_PASS
    )


async def _refresh_listed(
    client: HttpClient, company: Company, adapter: ListedAdapter, *, force: bool
) -> RefreshOutcome:
    """One pass over a board whose postings are read one request each (see the module
    docstring)."""
    slug = company.ats_slug or ""
    state = PollState.read(company.poll_state)
    now = timezone.now()
    catching_up = is_catching_up(company)
    budget, size = _budget(adapter.source, catching_up)
    try:
        if catching_up and not force and client.left(budget, size) == 0:
            # Its turn has not come this pass: nothing is asked, and it is not recorded as
            # fetched, so it stays at the front of the next pass's queue.
            return RefreshOutcome(
                company.name, company.slug, "ok", company.last_fetch_job_count or 0, unchanged=True
            )

        # A part-read board goes on with what its last listing named: no probe, no listing.
        # A person who asked for the board gets it listed afresh.
        left_over = await db(read_backlog, company.id, now) if state.pending and not force else None
        listing = None
        if left_over is not None:
            fingerprint, wanted = left_over.fingerprint, left_over.postings
        else:
            # Boards on one host are asked and listed one after another: a listing is already
            # several requests at a time.
            async with client.gate(f"listing:{_host(adapter.source, slug)}", 1):
                fingerprint = await adapter.probe(client, slug)
                settled = (
                    state.url == adapter.source
                    and state.digest == fingerprint
                    and not state.pending
                )
                if settled and not force and not full_read_due(state, company.id, now):
                    await db(record_fetch, company.id, FetchStatus.OK, None)
                    return RefreshOutcome(
                        company.name,
                        company.slug,
                        "ok",
                        company.last_fetch_job_count or 0,
                        unchanged=True,
                    )
                listing = await adapter.list_board(client, slug)
            known = await db(listing_versions, company.id)
            wanted = [
                posting
                for posting in listing.postings
                if force or known.get(posting.external_id) != posting.version
            ]

        # A person who asked for this board is not made to wait for a pass's budget.
        allowed = min(len(wanted), POSTINGS_PER_PASS)
        if not force:
            allowed = client.take(budget, size, allowed)
        read, failed = await _read_postings(client, adapter, company, wanted[:allowed])
        if wanted and failed and not read:
            raise RuntimeError(f"none of {len(failed)} postings could be read")

        # A board being caught up is not being watched yet: what a later pass of the catch-up
        # brings in was there all along, and is not timed.
        previous_ok = None if state.pending else company.last_ok_at
        ingest = await db(
            ingest_jobs, company.id, [job for _, job in read], previous_ok=previous_ok
        )
        await db(set_listing_versions, {job.id: posting.version for posting, job in read})
        # Only a listing known to be whole says what has closed.
        if listing is not None and listing.complete:
            ingest.deactivated = await db(
                close_missing, company.id, [posting.external_id for posting in listing.postings]
            )
        # More to read than one pass takes, or some that failed: the next pass goes on with
        # them, the ones that failed last. (One that has gone since the listing is not kept.)
        left = [*wanted[allowed:], *failed]
        pending = bool(left)
        if pending:
            listed_at = left_over.listed_at if left_over is not None else now
            await db(save_backlog, company.id, left, fingerprint, listed_at)
        elif state.pending:
            await db(drop_backlog, company.id)
        # One empty listing closes nothing (the empty-board guard, §10.4); forgetting its
        # fingerprint is what makes the next pass list again and so see the second.
        guard_held = (
            listing is not None and not listing.postings and (company.last_fetch_job_count or 0) > 0
        )
        remembered = PollState(
            url=adapter.source,
            digest=None if guard_held else fingerprint,
            read_at=state.read_at if pending else now,
            pending=pending,
        )
        # Going on from a backlog says nothing new about how many postings the board lists.
        listed = len(listing.postings) if listing is not None else None
        await db(
            record_fetch,
            company.id,
            FetchStatus.OK,
            listed,
            poll_state=remembered.dump(),
            relevant=await db(_lists_early_career, company.id),
        )
    except BoardNotFound as error:
        await db(record_fetch, company.id, FetchStatus.NOT_FOUND, 0, mark_invalid=True)
        return RefreshOutcome(company.name, company.slug, "not-found", 0, error=str(error))
    except Exception as error:
        log.warning("refresh of %s failed: %s", company.slug, error)
        await db(record_fetch, company.id, FetchStatus.ERROR, None)
        return RefreshOutcome(company.name, company.slug, "error", 0, error=_message(error))
    jobs = listed if listed is not None else company.last_fetch_job_count or 0
    return RefreshOutcome(company.name, company.slug, "ok", jobs, ingest=ingest)


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
        pace = SOURCE_PACE_S.get(company.source_type)
        source = per_source.setdefault(
            company.source_type, asyncio.Semaphore(1 if pace else PER_SOURCE_CONCURRENCY)
        )
        async with source:
            if pace:
                await client.pace(f"boards:{company.source_type}", pace)
            async with gate:
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
