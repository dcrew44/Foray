"""Ashby job boards (design §10.2; `map/worker.md` §14.3).

The richest of the three: a complete description in one field, an explicit remote boolean, and
structured compensation, when the board chooses to publish it, which many do not.
"""

from __future__ import annotations

import json
from typing import Any

from apps.core.http import HttpClient, HttpError
from apps.jobs.sources.clean import clean_description
from apps.jobs.sources.dedupe import content_hash, dedupe_key, job_id
from apps.jobs.sources.js import encode_uri_component, js_lower, js_trim, js_truthy
from apps.jobs.sources.normalize import (
    detect_remote,
    is_plausible_salary,
    number_or_none,
    parse_salary,
    primary_location,
)
from apps.jobs.sources.types import (
    BoardNotFound,
    NormalizedJob,
    NormalizedSalary,
    RemoteKind,
    SalaryPeriod,
    js_date,
)

SOURCE = "ashby"
BASE = "https://api.ashbyhq.com/posting-api/job-board"

WORKPLACE: dict[str, RemoteKind] = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite"}


def board_url(slug: str) -> str:
    return f"{BASE}/{encode_uri_component(slug)}?includeCompensation=true"


def _period_from_interval(interval: Any) -> SalaryPeriod | None:
    """Ashby writes the interval as a phrase: "1 YEAR", "1 HOUR"."""
    value = js_lower(interval) or ""
    if "hour" in value:
        return "hour"
    if "month" in value:
        return "month"
    if "year" in value:
        return "year"
    return None


def _salary_from(job: dict[str, Any]) -> NormalizedSalary | None:
    """Equity components carry null amounts; only cash salary is a salary. Structured does not
    mean correct, so it must also be plausible."""
    compensation = job.get("compensation") or {}
    component = next(
        (
            item
            for item in compensation.get("summaryComponents") or []
            if js_lower(item.get("compensationType")) == "salary"
            and number_or_none(item.get("minValue")) is not None
        ),
        None,
    )
    if component is None:
        return None
    salary = NormalizedSalary(
        min=number_or_none(component.get("minValue")),
        max=number_or_none(component.get("maxValue")),
        currency=component.get("currencyCode"),
        period=_period_from_interval(component.get("interval")),
    )
    return salary if is_plausible_salary(salary) else None


def normalize_job(slug: str, job: dict[str, Any]) -> NormalizedJob:
    candidates = [job.get("location")] + [
        item.get("location") for item in job.get("secondaryLocations") or []
    ]
    locations = [
        trimmed
        for trimmed in (js_trim(value) if isinstance(value, str) else None for value in candidates)
        if trimmed
    ]

    location = primary_location(locations)
    description_raw = job.get("descriptionHtml")
    if description_raw is None:
        description_raw = job.get("descriptionPlain")
    cleaned = clean_description(description_raw)

    is_remote = job.get("isRemote")
    remote = WORKPLACE.get(js_lower(job.get("workplaceType")) or "") or detect_remote(
        location=" ".join(locations),
        title=job["title"],
        is_remote_flag=is_remote if isinstance(is_remote, bool) else None,
        description=cleaned.text,
    )

    salary = _salary_from(job)
    if salary is None:
        summary = (job.get("compensation") or {}).get("scrapeableCompensationSalarySummary")
        salary = parse_salary(summary if summary is not None else cleaned.text)

    published = job.get("publishedAt")
    return NormalizedJob(
        id=job_id(SOURCE, slug, job["id"]),
        source=SOURCE,
        external_id=job["id"],
        company_slug=slug,
        company_name=slug,
        title=job["title"],
        locations=locations,
        location_norm=location,
        remote=remote,
        employment_type=job.get("employmentType"),
        salary=salary,
        description_raw=description_raw,
        description_text=cleaned.text or None,
        url=job["jobUrl"],
        apply_url=job.get("applyUrl"),
        posted_at=js_date(published) if js_truthy(published) else None,
        raw=job,
        content_hash=content_hash(job["title"], locations, description_raw, salary.min, salary.max),
        dedupe_key=dedupe_key(
            slug, job["title"], (location.city or location.raw) if location else None
        ),
    )


async def fetch_board(client: HttpClient, slug: str) -> list[NormalizedJob]:
    try:
        body = await client.fetch_text(board_url(slug))
    except HttpError as error:
        # Ashby answers an unknown slug with plain text "Not Found", so the status is checked
        # before anything parses the body.
        if error.status == 404:
            raise BoardNotFound(SOURCE, slug) from error
        raise

    board = json.loads(body)
    if not isinstance(board, dict):
        raise ValueError(f"Unexpected ashby response for {slug!r}")
    # Unlisted postings are not public; a board can also be real but empty.
    return [
        normalize_job(slug, job)
        for job in board.get("jobs") or []
        if job.get("isListed") is not False
    ]
