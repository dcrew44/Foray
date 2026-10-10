"""JSearch: Google for Jobs, via RapidAPI (design §10.2, §10.6; `map/worker.md` §17.5).

The only source that searches across companies rather than reading one board, which stops the
product from only ever finding jobs at companies it already knew. It is also the only one that
costs money per call: the free tier is 200 requests a month and every result page bills one.

The budget lives in `api_usage`. Each request is counted **before** its response is read, in one
autocommit upsert, because a request that was sent and then failed still spent quota; the count
comes back from the upsert, so two concurrent searches can never both read 179 and both decide
they have room.

Endpoint note: the search path is `/search-v2` on the current (v5) API. `/search` answers
"Endpoint does not exist", and paging is by an opaque `cursor`, not a page number.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlencode

from django.conf import settings
from django.db import connection

from apps.core.db import db
from apps.core.http import JSEARCH_MAX_BYTES, HttpClient, HttpError
from apps.jobs.models import ApiUsage
from apps.jobs.sources.clean import clean_description
from apps.jobs.sources.dedupe import content_hash, dedupe_key, job_id
from apps.jobs.sources.js import IA, js_lower, js_trim, js_truthy
from apps.jobs.sources.normalize import (
    detect_remote,
    is_plausible_salary,
    number_or_none,
    primary_location,
)
from apps.jobs.sources.types import (
    BoardSource,
    NormalizedJob,
    NormalizedSalary,
    RemoteKind,
    SalaryPeriod,
    js_date,
)

PROVIDER = "jsearch"
HOST = "jsearch.p.rapidapi.com"
BASE = f"https://{HOST}"
TIMEOUT_S = 45
RETRIES = 1

type DatePosted = Literal["all", "today", "3days", "week", "month"]


class JSearchDisabledError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(f"JSearch unavailable: {reason}")


class JSearchQuotaError(Exception):
    def __init__(self, used: int, budget: int) -> None:
        super().__init__(f"JSearch monthly budget spent ({used}/{budget} requests)")
        self.used = used
        self.budget = budget


def monthly_budget() -> int:
    return settings.ENV.jsearch_monthly_budget


def enabled() -> bool:
    return settings.ENV.jsearch_enabled and settings.ENV.rapidapi_key is not None


# --- the quota -----------------------------------------------------------------------------------


def current_period(at: datetime | None = None) -> str:
    """The UTC month a quota window belongs to: `YYYY-MM`."""
    at = (at or datetime.now(UTC)).astimezone(UTC)
    return f"{at.year}-{at.month:02d}"


def count_request(provider: str, at: datetime | None = None) -> int:
    """Counts one request against this month's quota and returns the new total. One upsert, no
    read first. Sync: run it with `db()`, outside any transaction, so it commits at once."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO api_usage (provider, period, requests, updated_at)
            VALUES (%s, %s, 1, now())
            ON CONFLICT (provider, period) DO UPDATE
               SET requests = api_usage.requests + 1, updated_at = now()
            RETURNING requests
            """,
            [provider, current_period(at)],
        )
        row = cursor.fetchone()
    assert row is not None
    return int(row[0])


def requests_this_period(provider: str, at: datetime | None = None) -> int:
    row = ApiUsage.objects.filter(provider=provider, period=current_period(at)).first()
    return row.requests if row else 0


def jsearch_budget() -> dict[str, Any]:
    """For the Settings meter and the run budget. Sync."""
    used = requests_this_period(PROVIDER)
    budget = monthly_budget()
    return {
        "used": used,
        "budget": budget,
        "remaining": max(budget - used, 0),
        "enabled": enabled(),
    }


# --- normalisation -------------------------------------------------------------------------------

_ATS_IN_LINK: list[tuple[BoardSource, re.Pattern[str]]] = [
    ("greenhouse", re.compile(r"(?:boards|job-boards)\.greenhouse\.io/([a-z0-9_-]+)", IA)),
    ("lever", re.compile(r"jobs\.lever\.co/([a-z0-9_-]+)", IA)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([a-z0-9_-]+)", IA)),
]
_NOT_SLUG = re.compile(r"[^a-z0-9]+")
_EDGE_DASH = re.compile(r"^-|-\Z")


@dataclass(frozen=True, slots=True)
class DiscoveredBoard:
    source: BoardSource
    slug: str
    company_name: str = ""


def _period_of(value: Any) -> SalaryPeriod | None:
    """JSearch reports the period as a word; ours is an enum."""
    period = js_lower(value) or ""
    if "hour" in period:
        return "hour"
    if "month" in period:
        return "month"
    if "year" in period or "annum" in period:
        return "year"
    return None


def ats_from_apply_options(job: dict[str, Any]) -> DiscoveredBoard | None:
    """A board we could read directly, if an apply link points at Greenhouse, Lever or Ashby:
    every such result is an invitation to stop paying for that company."""
    links = [job.get("job_apply_link")] + [
        option.get("apply_link") for option in job.get("apply_options") or []
    ]
    for link in links:
        if not isinstance(link, str) or not link:
            continue
        for source, pattern in _ATS_IN_LINK:
            found = pattern.search(link)
            if found:
                return DiscoveredBoard(source, found.group(1).lower())
    return None


def employer_slug(name: str) -> str:
    """A stable company slug from the employer name; aggregator rows have none."""
    slug = _EDGE_DASH.sub("", _NOT_SLUG.sub("-", name.lower()))[:60]
    return slug or "unknown"


def normalize_job(job: dict[str, Any]) -> NormalizedJob:
    company_name = js_trim(job.get("employer_name") or "") or "Unknown"
    company_slug = employer_slug(company_name)

    joined = job.get("job_location")
    if joined is None:
        joined = ", ".join(
            str(part)
            for part in (job.get("job_city"), job.get("job_state"), job.get("job_country"))
            if js_truthy(part)
        )
    # `job_location: ""` gives no locations, without falling back to city/state/country.
    locations = [joined] if js_truthy(joined) else []
    location = primary_location(locations)

    # Already plain text, but it still goes through cleaning, so trailing boilerplate is
    # stripped the same way.
    description_raw = job.get("job_description")
    cleaned = clean_description(description_raw)

    remote: RemoteKind = (
        "remote"
        if job.get("job_is_remote") is True
        else detect_remote(
            location=" ".join(locations), title=job["job_title"], description=cleaned.text
        )
    )

    reported = NormalizedSalary(
        min=number_or_none(job.get("job_min_salary")),
        max=number_or_none(job.get("job_max_salary")),
        currency="USD",
        period=_period_of(job.get("job_salary_period")),
    )
    salary = reported if is_plausible_salary(reported) else NormalizedSalary()

    direct = next(
        (option for option in job.get("apply_options") or [] if js_truthy(option.get("is_direct"))),
        None,
    )
    apply_link = direct.get("apply_link") if direct else None
    if apply_link is None:
        apply_link = job.get("job_apply_link")

    url = job.get("job_google_link")
    if url is None:
        url = apply_link
    if url is None:
        url = job.get("job_apply_link")
    if url is None:
        url = ""

    posted = job.get("job_posted_at_datetime_utc")
    return NormalizedJob(
        id=job_id(PROVIDER, company_slug, job["job_id"]),
        source="jsearch",
        external_id=job["job_id"],
        company_slug=company_slug,
        company_name=company_name,
        title=job["job_title"],
        locations=locations,
        location_norm=location,
        remote=remote,
        employment_type=job.get("job_employment_type"),
        salary=salary,
        description_raw=description_raw,
        description_text=cleaned.text or None,
        url=url,
        apply_url=apply_link,
        posted_at=js_date(posted) if js_truthy(posted) else None,
        raw=job,
        content_hash=content_hash(
            job["job_title"], locations, description_raw, salary.min, salary.max
        ),
        dedupe_key=dedupe_key(
            company_name, job["job_title"], (location.city or location.raw) if location else None
        ),
    )


# --- searching -----------------------------------------------------------------------------------


@dataclass(slots=True)
class JSearchResult:
    jobs: list[NormalizedJob] = field(default_factory=list)
    # Boards worth registering, found in apply links.
    discovered_boards: list[DiscoveredBoard] = field(default_factory=list)
    requests_used: int = 0
    # The total spent this calendar month, after this call.
    requests_this_month: int = 0


def search_url(
    query: str,
    *,
    country: str = "us",
    date_posted: DatePosted = "month",
    remote_only: bool = False,
    cursor: str | None = None,
) -> str:
    params = {"query": query, "country": country, "date_posted": date_posted}
    if remote_only:
        params["work_from_home"] = "true"
    if cursor:
        params["cursor"] = cursor
    return f"{BASE}/search-v2?{urlencode(params)}"


async def search_jsearch(
    client: HttpClient,
    query: str,
    *,
    country: str = "us",
    date_posted: DatePosted = "month",
    remote_only: bool = False,
    max_requests: int = 1,
) -> JSearchResult:
    """Up to `max_requests` result pages (one request each, plus at most one retry; every
    request sent is counted). Raises `JSearchDisabledError` or `JSearchQuotaError` before
    spending anything; a failure after a first page returns what the earlier pages produced."""
    env = settings.ENV
    if not env.jsearch_enabled:
        raise JSearchDisabledError("JSEARCH_ENABLED is false")
    if env.rapidapi_key is None:
        raise JSearchDisabledError("RAPIDAPI_KEY is not set")
    key = env.rapidapi_key.get_secret_value()

    budget = monthly_budget()
    budget_before = await db(requests_this_period, PROVIDER)
    if budget_before >= budget:
        raise JSearchQuotaError(budget_before, budget)
    max_requests = max(1, min(max_requests, budget - budget_before))

    result = JSearchResult(requests_this_month=budget_before)
    boards: dict[str, DiscoveredBoard] = {}
    cursor: str | None = None

    for _page in range(max_requests):
        url = search_url(
            query, country=country, date_posted=date_posted, remote_only=remote_only, cursor=cursor
        )

        async def counted(attempt: int) -> None:
            # Every attempt is counted before it is sent, a retry included: a request that was
            # sent and failed (or timed out after the server served it) still spent quota. Any
            # attempt that would go past the month's budget is not sent, whether it is a retry
            # or a later page's first try (an earlier retry may have used that page's share).
            if result.requests_this_month >= budget:
                raise JSearchQuotaError(result.requests_this_month, budget)
            result.requests_this_month = await db(count_request, PROVIDER)
            result.requests_used += 1

        try:
            body = await client.fetch_text(
                url,
                timeout=TIMEOUT_S,
                retries=RETRIES,
                headers={"x-rapidapi-key": key, "x-rapidapi-host": HOST},
                max_bytes=JSEARCH_MAX_BYTES,
                on_attempt=counted,
            )
        except HttpError, JSearchQuotaError:
            # A failure mid-paging still returns what earlier pages produced.
            if result.jobs:
                break
            raise

        response = json.loads(body)
        data = (response.get("data") if isinstance(response, dict) else None) or {}
        page_jobs = data.get("jobs") or []
        for job in page_jobs:
            result.jobs.append(normalize_job(job))
            board = ats_from_apply_options(job)
            if board is not None:
                key_ = f"{board.source}:{board.slug}"
                if key_ not in boards:
                    name = js_trim(job.get("employer_name") or "") or board.slug
                    boards[key_] = DiscoveredBoard(board.source, board.slug, name)

        cursor = data.get("cursor")
        if not cursor or not page_jobs:
            break

    result.discovered_boards = list(boards.values())
    return result
