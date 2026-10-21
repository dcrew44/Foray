"""Workable job boards, through the public widget API that careers-site embeds use.

`?details=true` puts the whole description on every posting, so a board is one request. There
is no compensation field, and the only workplace signal is a `telecommuting` boolean. A location
can be `hidden`: the employer keeps it off the posting (a remote role with a nominal office), so
it is not offered as the job's place unless nothing else is known.
"""

from __future__ import annotations

import json
from typing import Any

from apps.core.http import HttpClient, HttpError
from apps.jobs.sources.clean import clean_description
from apps.jobs.sources.dedupe import content_hash, dedupe_key, job_id
from apps.jobs.sources.js import encode_uri_component, js_trim, js_truthy
from apps.jobs.sources.normalize import detect_remote, parse_salary, primary_location
from apps.jobs.sources.types import BoardNotFound, NormalizedJob, js_date

SOURCE = "workable"
BASE = "https://apply.workable.com/api/v1/widget/accounts"


def board_url(slug: str) -> str:
    return f"{BASE}/{encode_uri_component(slug)}?details=true"


def _place(location: dict[str, Any]) -> str:
    parts = (location.get("city"), location.get("region"), location.get("country"))
    return ", ".join(js_trim(part) for part in parts if isinstance(part, str) and js_trim(part))


def _locations(job: dict[str, Any]) -> list[str]:
    entries = [entry for entry in job.get("locations") or [] if isinstance(entry, dict)]
    shown = [_place(entry) for entry in entries if entry.get("hidden") is not True]
    places = [place for place in shown if place]
    if not places:
        # Every place is hidden: the country is still true, the office may not be.
        places = [
            js_trim(entry["country"])
            for entry in entries
            if isinstance(entry.get("country"), str) and js_trim(entry["country"])
        ]
    return list(dict.fromkeys(places))


def normalize_job(slug: str, job: dict[str, Any], company_name: str | None = None) -> NormalizedJob:
    locations = _locations(job)
    location = primary_location(locations)
    description_raw = job.get("description")
    cleaned = clean_description(description_raw)
    telecommuting = job.get("telecommuting")

    remote = detect_remote(
        location=" ".join(locations),
        title=job["title"],
        is_remote_flag=telecommuting if isinstance(telecommuting, bool) else None,
        description=cleaned.text,
    )

    salary = parse_salary(cleaned.text)
    published = job.get("published_on")
    if not js_truthy(published):
        published = job.get("created_at")
    name = js_trim(company_name or "") or slug
    external_id = str(job["shortcode"])

    return NormalizedJob(
        id=job_id(SOURCE, slug, external_id),
        source=SOURCE,
        external_id=external_id,
        company_slug=slug,
        company_name=name,
        title=job["title"],
        locations=locations,
        location_norm=location,
        remote=remote,
        employment_type=js_trim(job.get("employment_type") or "") or None,
        salary=salary,
        description_raw=description_raw,
        description_text=cleaned.text or None,
        url=job["url"],
        apply_url=job.get("application_url"),
        posted_at=js_date(published) if js_truthy(published) else None,
        raw=job,
        content_hash=content_hash(job["title"], locations, description_raw, salary.min, salary.max),
        dedupe_key=dedupe_key(
            name, job["title"], (location.city or location.raw) if location else None
        ),
    )


async def fetch_board(client: HttpClient, slug: str) -> list[NormalizedJob]:
    try:
        body = await client.fetch_text(board_url(slug))
    except HttpError as error:
        # An unknown account is a plain-text "Not Found"; a real one with nothing open is `[]`.
        if error.status == 404:
            raise BoardNotFound(SOURCE, slug) from error
        raise

    board = json.loads(body)
    if not isinstance(board, dict):
        raise ValueError(f"Unexpected workable response for {slug!r}")
    name = board.get("name") if isinstance(board.get("name"), str) else None
    return [normalize_job(slug, job, name) for job in board.get("jobs") or []]
