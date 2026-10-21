"""Recruitee job boards: each company's own `<slug>.recruitee.com/api/offers/`.

Like Lever, `description` is not the whole posting: the requirements are a separate field, so
the two are stitched back together. The workplace is three booleans rather than one value, and
compensation is structured but arrives as strings, with `"0"` standing in for "not given".
Dates are written `2026-10-15 09:31:36 UTC`, which no ISO parser reads as is.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

from apps.core.http import HttpClient, HttpError
from apps.jobs.sources.clean import clean_description
from apps.jobs.sources.dedupe import content_hash, dedupe_key, job_id
from apps.jobs.sources.js import js_lower, js_trim, js_truthy
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

SOURCE = "recruitee"

# A board's slug is its subdomain, so it can only be a DNS label.
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z", re.IGNORECASE)
_UTC_SUFFIX = re.compile(r" UTC\Z")


def board_url(slug: str) -> str:
    return f"https://{slug.lower()}.recruitee.com/api/offers/"


def _period(value: Any) -> SalaryPeriod | None:
    period = js_lower(value) or ""
    if "hour" in period:
        return "hour"
    if "month" in period:
        return "month"
    if "year" in period or "annual" in period:
        return "year"
    return None


def _amount(value: Any) -> float | None:
    """Amounts arrive as decimal strings ("5700"); a number is taken as it is."""
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
        return value if math.isfinite(value) else None
    return number_or_none(value)


def _salary_from(offer: dict[str, Any]) -> NormalizedSalary | None:
    salary = offer.get("salary") or {}
    low = _amount(salary.get("min"))
    if not low:  # absent, or the "0" a board writes when it gives no figure
        return None
    structured = NormalizedSalary(
        min=low,
        max=_amount(salary.get("max")),
        currency=salary.get("currency"),
        period=_period(salary.get("period")),
    )
    return structured if is_plausible_salary(structured) else None


def full_description(offer: dict[str, Any]) -> str | None:
    parts = [offer[key] for key in ("description", "requirements") if js_truthy(offer.get(key))]
    return "\n".join(parts) if parts else None


def _place(location: dict[str, Any]) -> str:
    parts = (location.get("city"), location.get("state"), location.get("country"))
    return ", ".join(js_trim(part) for part in parts if isinstance(part, str) and js_trim(part))


def _locations(offer: dict[str, Any]) -> list[str]:
    places = [_place(entry) for entry in offer.get("locations") or [] if isinstance(entry, dict)]
    places = [place for place in places if place]
    if not places and isinstance(offer.get("location"), str) and js_trim(offer["location"]):
        places = [js_trim(offer["location"])]
    return list(dict.fromkeys(places))


def _workplace(offer: dict[str, Any]) -> RemoteKind | None:
    if offer.get("remote") is True:
        return "remote"
    if offer.get("hybrid") is True:
        return "hybrid"
    if offer.get("on_site") is True:
        return "onsite"
    return None


def _date(value: Any) -> Any:
    return _UTC_SUFFIX.sub("+00:00", value) if isinstance(value, str) else value


def normalize_job(slug: str, offer: dict[str, Any]) -> NormalizedJob:
    locations = _locations(offer)
    location = primary_location(locations)
    description_raw = full_description(offer)
    cleaned = clean_description(description_raw)

    remote = _workplace(offer) or detect_remote(
        location=" ".join(locations), title=offer["title"], description=cleaned.text
    )
    salary = _salary_from(offer) or parse_salary(cleaned.text)

    published = offer.get("published_at")
    if not js_truthy(published):
        published = offer.get("created_at")
    company_name = js_trim(offer.get("company_name") or "") or slug
    external_id = str(offer["id"])

    return NormalizedJob(
        id=job_id(SOURCE, slug, external_id),
        source=SOURCE,
        external_id=external_id,
        company_slug=slug,
        company_name=company_name,
        title=offer["title"],
        locations=locations,
        location_norm=location,
        remote=remote,
        employment_type=offer.get("employment_type_code"),
        salary=salary,
        description_raw=description_raw,
        description_text=cleaned.text or None,
        url=offer["careers_url"],
        apply_url=offer.get("careers_apply_url"),
        posted_at=js_date(_date(published)) if js_truthy(published) else None,
        raw=offer,
        content_hash=content_hash(
            offer["title"], locations, description_raw, salary.min, salary.max
        ),
        dedupe_key=dedupe_key(
            company_name, offer["title"], (location.city or location.raw) if location else None
        ),
    )


async def fetch_board(client: HttpClient, slug: str) -> list[NormalizedJob]:
    if not _LABEL.match(slug):
        raise BoardNotFound(SOURCE, slug)
    try:
        body = await client.fetch_text(board_url(slug))
    except HttpError as error:
        if error.status == 404:
            raise BoardNotFound(SOURCE, slug) from error
        raise

    board = json.loads(body)
    if not isinstance(board, dict):
        raise ValueError(f"Unexpected recruitee response for {slug!r}")
    # The public feed lists published offers only, but a stray draft is not a posting.
    return [
        normalize_job(slug, offer)
        for offer in board.get("offers") or []
        if offer.get("status") in (None, "published")
    ]
