"""Lever job boards (design §10.2; `map/worker.md` §14.2).

The trap here is that `description` is not the description: it is the opening plus the body, and
the requirements live in `lists[]`, which it does not include. On a real posting that is several
thousand characters of exactly the content a match is scored against, so the full posting is
reassembled.
"""

from __future__ import annotations

import json
from typing import Any

from apps.core.http import HttpClient, HttpError
from apps.jobs.sources.clean import clean_description
from apps.jobs.sources.dedupe import content_hash, dedupe_key, job_id
from apps.jobs.sources.js import encode_uri_component, js_lower, js_truthy
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

SOURCE = "lever"
BASE = "https://api.lever.co/v0/postings"

WORKPLACE: dict[str, RemoteKind] = {
    "remote": "remote",
    "hybrid": "hybrid",
    "onsite": "onsite",
    "on-site": "onsite",
}


def board_url(slug: str) -> str:
    return f"{BASE}/{encode_uri_component(slug)}?mode=json"


def full_description(posting: dict[str, Any]) -> str | None:
    """`description` omits `lists[]`, so the posting is stitched back together."""
    parts: list[str] = []
    if js_truthy(posting.get("description")):
        parts.append(posting["description"])
    for item in posting.get("lists") or []:
        if js_truthy(item.get("text")):
            parts.append(f"<h3>{item['text']}</h3>")
        if js_truthy(item.get("content")):
            parts.append(item["content"])
    if js_truthy(posting.get("additional")):
        parts.append(posting["additional"])
    return "\n".join(parts) if parts else None


def _period_from_interval(interval: Any) -> SalaryPeriod | None:
    """`per-year-salary`, `per-hour-wage`, `per-month-salary` (a case-sensitive substring)."""
    if not js_truthy(interval) or not isinstance(interval, str):
        return None
    if "hour" in interval:
        return "hour"
    if "month" in interval:
        return "month"
    if "year" in interval:
        return "year"
    return None


def normalize_job(slug: str, posting: dict[str, Any]) -> NormalizedJob:
    categories = posting.get("categories") or {}
    all_locations = categories.get("allLocations")
    if all_locations is not None:
        # An empty `allLocations: []` is kept as no locations: `??` does not fall back on it.
        locations = [location for location in all_locations if js_truthy(location)]
    elif js_truthy(categories.get("location")):
        locations = [categories["location"]]
    else:
        locations = []

    location = primary_location(locations)
    description_raw = full_description(posting)
    cleaned = clean_description(description_raw)

    remote = WORKPLACE.get(js_lower(posting.get("workplaceType")) or "") or detect_remote(
        location=" ".join(locations), title=posting["text"], description=cleaned.text
    )

    # Lever is the one board here with a clean numeric range, so it is preferred over anything
    # the description says, but still only when the numbers are plausible for their interval.
    salary_range = posting.get("salaryRange") or {}
    structured: NormalizedSalary | None = None
    if js_truthy(salary_range.get("min")):
        structured = NormalizedSalary(
            min=number_or_none(salary_range.get("min")),
            max=number_or_none(salary_range.get("max")),
            currency=salary_range.get("currency"),
            period=_period_from_interval(salary_range.get("interval")),
        )
    salary = (
        structured
        if structured is not None and is_plausible_salary(structured)
        else parse_salary(cleaned.text)
    )

    created_at = posting.get("createdAt")
    return NormalizedJob(
        id=job_id(SOURCE, slug, posting["id"]),
        source=SOURCE,
        external_id=posting["id"],
        company_slug=slug,
        company_name=slug,  # Lever gives no company name
        title=posting["text"],
        locations=locations,
        location_norm=location,
        remote=remote,
        employment_type=categories.get("commitment"),
        salary=salary,
        description_raw=description_raw,
        description_text=cleaned.text or None,
        url=posting["hostedUrl"],
        # Lever's apply form is a separate page from the posting.
        apply_url=posting.get("applyUrl"),
        posted_at=js_date(created_at) if js_truthy(created_at) else None,
        raw=posting,
        content_hash=content_hash(
            posting["text"], locations, description_raw, salary.min, salary.max
        ),
        dedupe_key=dedupe_key(
            slug, posting["text"], (location.city or location.raw) if location else None
        ),
    )


async def fetch_board(client: HttpClient, slug: str) -> list[NormalizedJob]:
    try:
        body = await client.fetch_text(board_url(slug))
    except HttpError as error:
        if error.status == 404:
            raise BoardNotFound(SOURCE, slug) from error
        raise

    # A real board with nothing open returns `[]`, which is not a 404.
    postings = json.loads(body)
    return (
        [normalize_job(slug, posting) for posting in postings] if isinstance(postings, list) else []
    )
