"""Gem job boards (`jobs.gem.com/<slug>`), through Gem's public job board API.

The payload is modelled on Greenhouse's, with two differences that matter: `content` is plain
HTML rather than entity-escaped, and `location_type` states the workplace outright. As on
Greenhouse, pay is only ever in the description. Ids are numeric for postings migrated from
Greenhouse and opaque strings for newer ones.
"""

from __future__ import annotations

import json
from typing import Any

from apps.core.http import HttpClient, HttpError
from apps.jobs.sources.clean import clean_description
from apps.jobs.sources.dedupe import content_hash, dedupe_key, job_id
from apps.jobs.sources.js import encode_uri_component, js_lower, js_trim, js_truthy
from apps.jobs.sources.normalize import detect_remote, parse_salary, primary_location
from apps.jobs.sources.types import BoardNotFound, NormalizedJob, RemoteKind, js_date

SOURCE = "gem"
BASE = "https://api.gem.com/job_board/v0"

WORKPLACE: dict[str, RemoteKind] = {
    "remote": "remote",
    "hybrid": "hybrid",
    "in_office": "onsite",
    "onsite": "onsite",
}


def board_url(slug: str) -> str:
    return f"{BASE}/{encode_uri_component(slug)}/job_posts/"


def _name_of(value: Any) -> str:
    name = value.get("name") if isinstance(value, dict) else None
    return js_trim(name) if isinstance(name, str) else ""


def _locations(job: dict[str, Any]) -> list[str]:
    """Every office the posting names, the primary location first."""
    names = [_name_of(job.get("location"))] + [
        _name_of(office.get("location")) or _name_of(office)
        for office in job.get("offices") or []
        if isinstance(office, dict)
    ]
    return list(dict.fromkeys(name for name in names if name))


def normalize_job(slug: str, job: dict[str, Any]) -> NormalizedJob:
    locations = _locations(job)
    location = primary_location(locations)
    description_raw = job.get("content")
    cleaned = clean_description(description_raw)

    remote = WORKPLACE.get(js_lower(job.get("location_type")) or "") or detect_remote(
        location=" ".join(locations), title=job["title"], description=cleaned.text
    )

    salary = parse_salary(cleaned.text)
    posted = job.get("first_published_at")
    if not js_truthy(posted):
        posted = job.get("created_at")
    external_id = str(job["id"])

    return NormalizedJob(
        id=job_id(SOURCE, slug, external_id),
        source=SOURCE,
        external_id=external_id,
        company_slug=slug,
        company_name=slug,  # Gem gives no company name
        title=job["title"],
        locations=locations,
        location_norm=location,
        remote=remote,
        employment_type=job.get("employment_type"),
        salary=salary,
        description_raw=description_raw,
        description_text=cleaned.text or None,
        url=job["absolute_url"],
        # Gem's posting page is also its application form.
        apply_url=None,
        posted_at=js_date(posted) if js_truthy(posted) else None,
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
        if error.status == 404:
            raise BoardNotFound(SOURCE, slug) from error
        raise

    posts = json.loads(body)
    if not isinstance(posts, list):
        raise ValueError(f"Unexpected gem response for {slug!r}")
    return [normalize_job(slug, job) for job in posts if isinstance(job, dict)]
