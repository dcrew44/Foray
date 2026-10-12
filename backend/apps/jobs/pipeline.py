"""Company pipelines: adding, discovering by names, refreshing (design §9.4, §10.5, §5.3).

Discovery and refresh make dozens of outbound requests and can take minutes, so neither runs in
a request any more (bug #22): each is a run whose job does the work in the worker.

- `company_discovery` (dedupe `add:<query>`, task `company.add`): discovering → refreshing →
  done; `company.discovered` / `company.not-found` per name, then `run.completed`.
- `company_refresh` (dedupe `refresh:<slug>` or `refresh:stale`, task `company.refresh`):
  (stale set only: waiting) → refreshing → done; `company.refresh.done`, then `run.completed`.

- **Starting** (`start_add`, `start_names`, `start_refresh`, `start_refresh_stale`) is sync and
  is shared by the API and the chat tools. An add is throttled (`company-add:<user>`, 30 an
  hour, one count per call), after the query is checked. A second start while the same run is
  active returns that run (`reused`).
- **`company.add`** ports `addCompanyAction` (`map/http-surface.md` §4.2) for one query and the
  worker's `company.discover` (`map/worker.md` §12.1) for a list of names. One query is
  discovered, then refreshed when it is an API board (`refreshAfter`), and completes with
  `{registered, existed, company, trail, message}` in today's words. A query with no board
  completes too (level `warn`), with the trail that explains the miss.
- **`company.refresh`** ports `refreshCompanyAction` for one slug and the worker's refresh for
  the stale set (active boards not fetched in 2 h). It always completes, including "None of your
  boards are stale.", and always emits `company.refresh.done {boards, ok, notFound, errored}`
  (integers: boards attempted, boards refreshed) first.
- **The six-hourly cron** (`refresh_scheduled`) refreshes the stale set with no run, no user and
  no events. It and every stale-set run hold `refresh.MULTI_BOARD_GATE` for their whole fan-out,
  so two multi-board refreshes never overlap; a stale run that has to wait shows phase
  `waiting` and usually finds nothing stale once the cron is done. Single-company refreshes
  stay ungated.

Events a run emits stay before its terminal event (R4): progress events and the final pair are
written only while the run is active, under its row lock, so nothing lands after a Stop.

Model calls (careers pages) are attributed by `run_step`'s LLM context to the run that triggered
the refresh; the cron sets none (system work, §8.8).
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from django.db import connection, transaction

from apps.core import throttle
from apps.core.db import db
from apps.core.errors import ApiError, UserFacingError
from apps.core.http import HttpClient
from apps.core.jsonio import js_locale
from apps.jobs import refresh as refresh_module
from apps.jobs.discovery import DiscoveryResult, discover_company
from apps.jobs.models import (
    Company,
    CompanyStatus,
    DiscoveredVia,
    Job,
    SourceType,
)
from apps.jobs.refresh import RefreshOutcome, refresh_companies, refresh_company, stale_cutoff
from apps.jobs.registry import get_company_by_slug, set_company_status, stale_companies
from apps.jobs.schemas import CompanyRow
from apps.jobs.sources.js import IA, JS_WS, js_trim
from apps.jobs.sources.types import js_iso
from apps.runs import events, lifecycle
from apps.runs.catalogue import (
    ACTIVE,
    CompanyAddInput,
    CompanyAddOutput,
    CompanyNamesOutput,
    CompanyRefreshInput,
    CompanyRefreshOutput,
    EventKind,
    Level,
    RunKind,
)
from apps.runs.models import Run

__all__ = [
    "ADD_TASK",
    "BOARD_LABEL",
    "COMPANY_GONE",
    "EVERY_BOARD_FRESH",
    "FETCH_PROBLEM",
    "NAMES_MISSING",
    "NONE_STALE",
    "QUERY_MAX",
    "QUERY_MISSING",
    "QUERY_TOO_LONG",
    "REFRESH_TASK",
    "SCHEDULED_TASK",
    "UNREADABLE_INPUT",
    "CompanyRow",
    "RefreshSummary",
    "Started",
    "active_job_count",
    "check_query",
    "company_row",
    "http_client",
    "join_names",
    "plural",
    "refresh_scheduled",
    "row_for",
    "run_company_add",
    "run_company_refresh",
    "set_status",
    "start_add",
    "start_names",
    "start_refresh",
    "start_refresh_stale",
]

log = logging.getLogger("foray.jobs")

type Id = str | uuid.UUID

ADD_TASK = "company.add"
REFRESH_TASK = "company.refresh"
SCHEDULED_TASK = "companies.refresh_scheduled"

QUERY_MAX = 300
QUERY_MISSING = "Type a company name, its website or a job board link."
QUERY_TOO_LONG = "That is too long to be a company name or a link."
COMPANY_GONE = "That company is no longer on the list."
NONE_STALE = "None of your boards are stale."
EVERY_BOARD_FRESH = "Every active board was fetched in the last two hours, so none are stale."
NAMES_MISSING = "Name at least one company to look for."
UNREADABLE_INPUT = "This run's request could not be read."

# The companies page's labels (`company-row.ts`), used in the messages below.
BOARD_LABEL: dict[str, str] = {
    SourceType.GREENHOUSE: "Greenhouse",
    SourceType.LEVER: "Lever",
    SourceType.ASHBY: "Ashby",
    SourceType.CAREERS_PAGE: "Careers page",
}

# What a failed fetch means, in words. `ok` has no entry.
FETCH_PROBLEM: dict[str, str] = {
    "not-found": "Board not found",
    "error": "Last fetch failed",
    "unsupported": "Cannot be fetched",
    "needs-browser": "Needs a browser",
}

# `discovery.ts`'s test for a query that is a website rather than a name.
_LOOKS_LIKE_URL = re.compile(r"^(?:https?://|www\.)|\.[a-z]{2,}(?:/|\Z)", IA)
_WHITESPACE = re.compile(f"[{JS_WS}]+")


def http_client() -> HttpClient:
    """One guarded client per job (§10.1). Tests replace this to serve canned answers."""
    return HttpClient()


def plural(count: int, one: str) -> str:
    """`plural()` from `src/lib/format.ts`: `1 open role`, `1,204 open roles`."""
    return f"{js_locale(count)} {one if count == 1 else one + 's'}"


def join_names(names: Sequence[str]) -> str:
    """`A`, `A and B`, `A, B and C`."""
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


# --- the companies page's row --------------------------------------------------------------------


def company_row(company: Company, active_jobs: int) -> dict[str, Any]:
    """`toCompanyRow`: the row as stored JSON (camelCase, `lastFetchedAt` as `toISOString()`),
    which is also what `CompanyRow` reads."""
    return {
        "slug": company.slug,
        "name": company.name,
        "domain": company.domain,
        "sourceType": company.source_type,
        "atsSlug": company.ats_slug,
        "careersUrl": company.careers_urls[0] if company.careers_urls else None,
        "status": company.status,
        "needsBrowser": company.needs_browser,
        "tags": list(company.tags),
        "lastFetchedAt": js_iso(company.last_fetched_at),
        "lastFetchStatus": company.last_fetch_status,
        "lastFetchJobCount": company.last_fetch_job_count,
        "discoveredVia": company.discovered_via,
        "activeJobs": active_jobs,
    }


def active_job_count(company_id: Id) -> int:
    return Job.objects.filter(company_id=company_id, is_active=True).count()


def row_for(company_id: Id) -> dict[str, Any] | None:
    """The company's row as it is now, or None when it is gone."""
    company = Company.objects.filter(id=company_id).first()
    return None if company is None else company_row(company, active_job_count(company.id))


# --- starting runs (sync; the API and the chat tools) --------------------------------------------


@dataclass(frozen=True, slots=True)
class Started:
    run: Run
    reused: bool  # an active run already held this dedupe key; it is returned instead
    message: str
    queued: int = 0  # refresh-stale: the stale boards counted when it started


def check_query(query: str) -> str:
    """The trimmed query, or a 400 with today's sentence."""
    trimmed = js_trim(query)
    if not trimmed:
        raise ApiError(400, QUERY_MISSING)
    if len(trimmed) > QUERY_MAX:
        raise ApiError(400, QUERY_TOO_LONG)
    return trimmed


def _normalised(text: str) -> str:
    return _WHITESPACE.sub(" ", js_trim(text)).lower()


def _count_add(user_id: str) -> None:
    """`company-add:<user>`, 30 an hour, shared by the endpoint and the chat tools (§4.5).
    Autocommit: callers never hold a transaction here."""
    limit = throttle.COMPANY_ADD
    throttle.throttle(f"company-add:{user_id}", limit.limit, limit.window_s)


def start_add(
    user_id: str,
    query: str,
    *,
    conversation_id: Id | None = None,
    refresh_after: bool = True,
) -> Started:
    """Start a `company_discovery` run for one name, website or board link."""
    q = check_query(query)
    _count_add(user_id)
    run, reused = lifecycle.start_run(
        user_id,
        RunKind.COMPANY_DISCOVERY,
        input=CompanyAddInput(query=q, refresh_after=refresh_after, prompt=f"Add {q}"),
        conversation_id=conversation_id,
        dedupe_key=f"add:{_normalised(q)}",
        task=ADD_TASK,
    )
    message = f"Already looking for “{q}”." if reused else f"Looking for a job board for “{q}”."
    return Started(run, reused, message)


def start_names(
    user_id: str, names: Sequence[str], *, conversation_id: Id | None = None
) -> Started:
    """Start one `company_discovery` run that looks up each name (chat's discover_companies)."""
    cleaned = list(dict.fromkeys(name for name in (js_trim(n) for n in names) if name))
    if not cleaned:
        raise ApiError(400, NAMES_MISSING)
    if any(len(name) > QUERY_MAX for name in cleaned):
        raise ApiError(400, QUERY_TOO_LONG)
    _count_add(user_id)
    key = hashlib.sha1("\n".join(sorted(_normalised(n) for n in cleaned)).encode()).hexdigest()
    run, reused = lifecycle.start_run(
        user_id,
        RunKind.COMPANY_DISCOVERY,
        input=CompanyAddInput(
            names=cleaned,
            refresh_after=False,
            prompt=f"Find job boards for {join_names(cleaned)}",
        ),
        conversation_id=conversation_id,
        dedupe_key=f"add:names:{key}",
        task=ADD_TASK,
    )
    message = (
        f"Looking for job boards for {len(cleaned)} companies: {', '.join(cleaned)}. "
        "Progress will appear in the activity feed."
    )
    return Started(run, reused, message)


def start_refresh(user_id: str, slug: str) -> Started:
    """Start a `company_refresh` run for one company ("Fetch now")."""
    company = get_company_by_slug(slug)
    if company is None:
        raise ApiError(404, COMPANY_GONE)
    run, reused = lifecycle.start_run(
        user_id,
        RunKind.COMPANY_REFRESH,
        input=CompanyRefreshInput(
            slug=company.slug, name=company.name, prompt=f"Refresh {company.name}"
        ),
        dedupe_key=f"refresh:{company.slug}",
        task=REFRESH_TASK,
    )
    message = f"Already refreshing {company.name}." if reused else f"Refreshing {company.name}."
    return Started(run, reused, message)


def start_refresh_stale(user_id: str) -> Started:
    """Start a `company_refresh` run over the stale set. It always starts, so the button always
    gets an answer: with nothing stale the run completes at once (bug #8)."""
    queued = len(stale_companies(stale_cutoff()))
    run, reused = lifecycle.start_run(
        user_id,
        RunKind.COMPANY_REFRESH,
        input=CompanyRefreshInput(stale=True, prompt="Refresh the stale boards"),
        dedupe_key="refresh:stale",
        task=REFRESH_TASK,
    )
    if reused:
        message = "The stale boards are already being refreshed."
    elif queued == 0:
        message = EVERY_BOARD_FRESH
    else:
        message = (
            f"Refreshing {plural(queued, 'stale board')} in the background. "
            "The table updates when they are done."
        )
    return Started(run, reused, message, queued)


def set_status(slug: str, status: str) -> tuple[dict[str, Any], str]:
    """Pause or resume a company (`setStatusAction`). Returns its row and today's message."""
    company = get_company_by_slug(slug)
    if company is None:
        raise ApiError(404, COMPANY_GONE)
    updated = set_company_status(company.id, status) or company
    message = (
        f"Paused {company.name}. Its board is skipped until you resume it."
        if status == CompanyStatus.PAUSED
        else f"{company.name} is active again and will be fetched with the rest."
    )
    return company_row(updated, active_job_count(company.id)), message


# --- writes that keep the run's terminal event last ----------------------------------------------


@dataclass(frozen=True, slots=True)
class _Event:
    kind: str
    message: str
    data: Any = None
    level: str | None = None


def _lock_if_active(run_id: Id) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM runs WHERE id = %s AND status = ANY(%s) FOR UPDATE",
            [str(run_id), list(ACTIVE)],
        )
        return cursor.fetchone() is not None


def _emit_while_active(run_id: Id, user_id: str, event: _Event) -> bool:
    """A progress event, only while the run is active: after a Stop, `run.cancelled` stays the
    run's last event (R4). The run's row lock comes before `emit()`'s advisory lock, the same
    order `cancel()` takes them."""
    with transaction.atomic():
        if not _lock_if_active(run_id):
            return False
        events.emit(
            user_id, event.kind, event.message, run_id=run_id, level=event.level, data=event.data
        )
    return True


def _finish(
    run_id: Id,
    user_id: str,
    output: Any,
    message: str,
    *,
    level: str = Level.INFO,
    before: _Event | None = None,
) -> bool:
    """`before` (if any) then `run.completed`, in one transaction under the run's row lock."""
    with transaction.atomic():
        if not _lock_if_active(run_id):
            return False
        if before is not None:
            events.emit(
                user_id,
                before.kind,
                before.message,
                run_id=run_id,
                level=before.level,
                data=before.data,
            )
        return lifecycle.complete(run_id, output, message, level=level)


# --- company.add ---------------------------------------------------------------------------------


def _discovered_event(name: str, result: DiscoveryResult) -> _Event:
    """`handleCompanyDiscover`'s event for one name."""
    company = result.registered
    if company is not None:
        board = f"{result.source}:{result.slug}" if result.slug else f"{result.source}"
        return _Event(
            EventKind.COMPANY_DISCOVERED,
            f"Registered {company.name} ({board})",
            {"trail": result.trail},
            Level.INFO,
        )
    return _Event(
        EventKind.COMPANY_NOT_FOUND,
        f'Could not find a job board for "{name}"',
        {"trail": result.trail},
        Level.WARN,
    )


def _not_found_message(query: str) -> str:
    if _LOOKS_LIKE_URL.search(query):
        return (
            f"No job board found at “{query}”. Try a direct link to its jobs page, "
            "or the company's name."
        )
    return f"No job board found for “{query}”. Try its website or a link to its jobs page."


def _added_message(row: dict[str, Any], *, existed: bool, has_api: bool) -> str:
    detail = (
        f"Its {BOARD_LABEL.get(row['sourceType'], 'No board')} board has "
        f"{plural(row['activeJobs'], 'open role')}."
        if has_api
        else "Its careers page is read at the next scheduled refresh."
    )
    if existed:
        return f"{row['name']} was already on the list. {detail}"
    return f"Added {row['name']}. {detail}"


def _names_message(added: Sequence[str], not_found: Sequence[str]) -> str:
    if added and not_found:
        return f"Registered {join_names(added)}. No job board found for {join_names(not_found)}."
    if added:
        return f"Registered {join_names(added)}."
    return f"No job board found for {join_names(not_found)}."


async def run_company_add(run: Run) -> None:
    """The `company.add` step: one query, or each of a list of names."""
    request = CompanyAddInput.read(run.input)
    if request is None:
        raise UserFacingError(UNREADABLE_INPUT)
    if request.names is not None:
        await _add_names(run, request.names)
    elif request.query is not None:
        await _add_one(run, request.query, refresh_after=request.refresh_after)
    else:
        raise UserFacingError(UNREADABLE_INPUT)


async def _add_one(run: Run, query: str, *, refresh_after: bool) -> None:
    await db(lifecycle.merge_stats, run.id, phase="discovering")
    async with http_client() as client:
        result = await discover_company(
            client, query, added_by_id=run.user_id, discovered_via=DiscoveredVia.USER
        )
        company = result.registered
        trail = list(result.trail)
        if company is None:
            message = _not_found_message(query)
            output = CompanyAddOutput.model_validate(
                {
                    "registered": False,
                    "existed": False,
                    "company": None,
                    "trail": trail,
                    "message": message,
                }
            )
            await db(
                _finish,
                run.id,
                run.user_id,
                output,
                message,
                level=Level.WARN,
                before=_discovered_event(query, result),
            )
            return

        if not await db(_emit_while_active, run.id, run.user_id, _discovered_event(query, result)):
            return  # stopped meanwhile
        has_api = company.source_type != SourceType.CAREERS_PAGE
        # Fill an API board straight away, so the company is useful now rather than after the
        # next scheduled refresh. A careers page needs the model, so it waits for that refresh.
        if has_api and company.status == CompanyStatus.ACTIVE and refresh_after:
            if await db(lifecycle.is_cancelled, run.id):
                return
            await db(lifecycle.merge_stats, run.id, phase="refreshing")
            await refresh_company(client, company)

    row = await db(row_for, company.id)
    if row is None:  # deleted meanwhile; nothing in the app deletes companies
        raise UserFacingError(COMPANY_GONE)
    existed = not result.created
    message = _added_message(row, existed=existed, has_api=has_api)
    output = CompanyAddOutput.model_validate(
        {"registered": True, "existed": existed, "company": row, "trail": trail, "message": message}
    )
    await db(_finish, run.id, run.user_id, output, message)


async def _add_names(run: Run, names: Sequence[str]) -> None:
    await db(lifecycle.merge_stats, run.id, phase="discovering")
    added: list[str] = []
    not_found: list[str] = []
    async with http_client() as client:
        for name in names:
            if await db(lifecycle.is_cancelled, run.id):
                return
            result = await discover_company(
                client, name, added_by_id=run.user_id, discovered_via=DiscoveredVia.AGENT
            )
            if result.registered is not None:
                added.append(result.registered.name)
            else:
                not_found.append(name)
            event = _discovered_event(name, result)
            if not await db(_emit_while_active, run.id, run.user_id, event):
                return
    message = _names_message(added, not_found)
    output = CompanyNamesOutput(added=added, not_found=not_found, message=message)
    level = Level.INFO if added else Level.WARN
    await db(_finish, run.id, run.user_id, output, message, level=level)


# --- company.refresh -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RefreshSummary:
    boards: int  # boards attempted
    ok: int  # boards refreshed successfully
    not_found: int
    errored: int
    jobs: int  # postings seen on the boards that refreshed
    inserted: int
    closed: int

    @classmethod
    def of(cls, outcomes: Sequence[RefreshOutcome]) -> RefreshSummary:
        ok = [outcome for outcome in outcomes if outcome.status == "ok"]
        return cls(
            boards=len(outcomes),
            ok=len(ok),
            not_found=sum(1 for outcome in outcomes if outcome.status == "not-found"),
            errored=sum(1 for outcome in outcomes if outcome.status == "error"),
            jobs=sum(outcome.jobs for outcome in ok),
            inserted=sum(outcome.ingest.inserted for outcome in ok if outcome.ingest),
            closed=sum(outcome.ingest.deactivated for outcome in ok if outcome.ingest),
        )

    def done_data(self) -> dict[str, int]:
        """`company.refresh.done`'s data, as today."""
        return {
            "boards": self.boards,
            "ok": self.ok,
            "notFound": self.not_found,
            "errored": self.errored,
        }

    def output(self, message: str) -> CompanyRefreshOutput:
        return CompanyRefreshOutput(
            boards=self.boards,
            ok=self.ok,
            not_found=self.not_found,
            errored=self.errored,
            jobs=self.jobs,
            inserted=self.inserted,
            closed=self.closed,
            message=message,
        )

    def message(self) -> str:
        """The worker's refresh line (`handleCompanyRefresh`)."""
        return (
            f"Refreshed {self.ok}/{self.boards} boards — {self.jobs} postings, {self.closed} closed"
        )


NOTHING = RefreshSummary(0, 0, 0, 0, 0, 0, 0)


def _one_message(name: str, outcome: RefreshOutcome) -> str:
    """`refreshCompanyAction`'s message for one company."""
    if outcome.status == "ok":
        inserted = outcome.ingest.inserted if outcome.ingest else 0
        closed = outcome.ingest.deactivated if outcome.ingest else 0
        changes = (
            f", {inserted} new and {closed} closed since the last fetch"
            if inserted or closed
            else ", no changes"
        )
        return f"{name}: {plural(outcome.jobs, 'open role')}{changes}."
    problem = FETCH_PROBLEM.get(outcome.status, "the fetch failed").lower()
    invalid = ", so it is marked invalid" if outcome.status == "not-found" else ""
    error = f" {outcome.error}" if outcome.status == "error" and outcome.error else ""
    return f"{name}: {problem}{invalid}.{error}"


async def _complete_refresh(
    run: Run, summary: RefreshSummary, message: str, *, level: str = Level.INFO
) -> None:
    # `run.completed` carries the same sentence at `level`; the counts event stays out of the
    # feeds (debug), or every refresh would show its sentence twice there. The companies page
    # still re-reads on it: `/api/live` serves every level.
    done = _Event(EventKind.COMPANY_REFRESH_DONE, message, summary.done_data(), Level.DEBUG)
    await db(
        _finish, run.id, run.user_id, summary.output(message), message, level=level, before=done
    )


async def run_company_refresh(run: Run) -> None:
    """The `company.refresh` step: one company, or the stale set."""
    request = CompanyRefreshInput.read(run.input)
    if request is None:
        raise UserFacingError(UNREADABLE_INPUT)
    if request.stale:
        await _refresh_stale(run)
    elif request.slug is not None:
        await _refresh_one(run, request.slug)
    else:
        raise UserFacingError(UNREADABLE_INPUT)


async def _refresh_one(run: Run, slug: str) -> None:
    company = await db(get_company_by_slug, slug)
    if company is None:
        await _complete_refresh(run, NOTHING, COMPANY_GONE, level=Level.WARN)
        return
    await db(lifecycle.merge_stats, run.id, phase="refreshing")
    async with http_client() as client:
        outcome = await refresh_company(client, company)
    summary = RefreshSummary.of([outcome])
    level = Level.INFO if outcome.status == "ok" else Level.WARN
    await _complete_refresh(run, summary, _one_message(company.name, outcome), level=level)


async def _refresh_stale(run: Run) -> None:
    gate = refresh_module.MULTI_BOARD_GATE  # read at call time: one per worker process
    if gate.locked():
        await db(lifecycle.merge_stats, run.id, phase="waiting")
    async with gate:
        if await db(lifecycle.is_cancelled, run.id):
            return
        # Read inside the gate: after a cron refresh there is usually nothing left to do.
        companies = await db(stale_companies, stale_cutoff())
        if not companies:
            await _complete_refresh(run, NOTHING, NONE_STALE)
            return
        await db(lifecycle.merge_stats, run.id, phase="refreshing")
        log.info("company.refresh: %s stale boards (run %s)", len(companies), run.id)
        async with http_client() as client:
            outcomes = await refresh_companies(client, companies)
    summary = RefreshSummary.of(outcomes)
    await _complete_refresh(run, summary, summary.message())


# --- companies.refresh_scheduled -----------------------------------------------------------------


async def refresh_scheduled() -> RefreshSummary:
    """The six-hourly cron: the stale set under the gate, with no run, user or events."""
    async with refresh_module.MULTI_BOARD_GATE:
        companies = await db(stale_companies, stale_cutoff())
        if not companies:
            log.info("companies.refresh_scheduled: nothing stale")
            return NOTHING
        log.info("companies.refresh_scheduled: %s boards", len(companies))
        started = time.monotonic()
        async with http_client() as client:
            outcomes = await refresh_companies(client, companies)
    summary = RefreshSummary.of(outcomes)
    log.info("%s in %.1fs", summary.message(), time.monotonic() - started)
    return summary
