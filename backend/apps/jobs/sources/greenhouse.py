"""Greenhouse job boards (design §10.2; `map/worker.md` §14.1).

Two quirks shape this adapter. Its `content` is entity-escaped HTML, handled by `clean.py`. And
it has no compensation fields at all: pay lives inside a `pay-range` block in the description,
so the salary is parsed out of the cleaned text rather than read from a column.
"""

from __future__ import annotations

import json
import re
from typing import Any

from apps.core.http import HttpClient, HttpError
from apps.jobs.sources.clean import clean_description
from apps.jobs.sources.dedupe import content_hash, dedupe_key, job_id
from apps.jobs.sources.js import encode_uri_component, js_trim, js_truthy
from apps.jobs.sources.normalize import detect_remote, parse_salary, primary_location
from apps.jobs.sources.types import BoardNotFound, NormalizedJob, js_date

SOURCE = "greenhouse"
BASE = "https://boards-api.greenhouse.io/v1/boards"

_SEPARATORS = re.compile(r"[|;]")


def board_url(slug: str) -> str:
    return f"{BASE}/{encode_uri_component(slug)}/jobs?content=true"


def _split_locations(job: dict[str, Any]) -> list[str]:
    """Greenhouse packs several cities into one free-text string and uses both `|` and `;`,
    sometimes in one value ("New York City, NY; San Francisco, CA | New York City, NY"), and
    repeats itself. Splitting on only one of them leaves a compound string no location parser
    can read, which quietly drops the job out of every metro filter."""
    location = job.get("location")
    name = location.get("name") if isinstance(location, dict) else None
    if isinstance(name, str):
        named = [part for part in (js_trim(p) for p in _SEPARATORS.split(name)) if part]
        if named:
            return list(dict.fromkeys(named))

    offices = job.get("offices") or []
    names: list[str] = []
    for office in offices:
        value = office.get("name")
        if value is None:
            value = office.get("location")
        trimmed = js_trim(value or "")
        if trimmed:
            names.append(trimmed)
    return list(dict.fromkeys(names))


def _location_type_flag(job: dict[str, Any]) -> str | None:
    """Boards may publish a "Location Type" custom field; most do not."""
    entry = next(
        (
            item
            for item in job.get("metadata") or []
            if isinstance(item.get("name"), str) and item["name"].lower() == "location type"
        ),
        None,
    )
    if entry is None or not js_truthy(entry.get("value")):
        return None
    value = entry["value"]
    if isinstance(value, list):
        return " ".join("" if item is None else str(item) for item in value)
    return str(value)


def normalize_job(slug: str, job: dict[str, Any]) -> NormalizedJob:
    locations = _split_locations(job)
    location = primary_location(locations)
    description_raw = job.get("content")
    cleaned = clean_description(description_raw)

    remote = detect_remote(
        location=" ".join([" ".join(locations), _location_type_flag(job) or ""]),
        title=job["title"],
        description=cleaned.text,
    )

    salary = parse_salary(cleaned.text)
    posted = job.get("first_published")
    if posted is None:
        posted = job.get("updated_at")
    company_name = js_trim(job.get("company_name") or "") or slug
    external_id = str(job["id"])

    return NormalizedJob(
        id=job_id(SOURCE, slug, external_id),
        source=SOURCE,
        external_id=external_id,
        company_slug=slug,
        company_name=company_name,
        title=job["title"],
        locations=locations,
        location_norm=location,
        remote=remote,
        employment_type=None,
        salary=salary,
        description_raw=description_raw,
        description_text=cleaned.text or None,
        url=job["absolute_url"],
        # Greenhouse's board page is also its application form.
        apply_url=None,
        posted_at=js_date(posted) if js_truthy(posted) else None,
        raw=job,
        content_hash=content_hash(job["title"], locations, description_raw, salary.min, salary.max),
        dedupe_key=dedupe_key(
            company_name, job["title"], (location.city or location.raw) if location else None
        ),
    )


async def fetch_board(client: HttpClient, slug: str) -> list[NormalizedJob]:
    try:
        body = await client.fetch_text(board_url(slug))
    except HttpError as error:
        if error.status == 404:
            raise BoardNotFound(SOURCE, slug) from error
        raise

    board = json.loads(body)
    if not isinstance(board, dict):
        raise ValueError(f"Unexpected greenhouse response for {slug!r}")
    return [normalize_job(slug, job) for job in board.get("jobs") or []]
