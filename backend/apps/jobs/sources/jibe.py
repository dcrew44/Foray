"""iCIMS career sites, the "Jibe" kind (#57): `careers.amd.com`, `careers.pnnl.gov`.

An employer on iCIMS runs one of two things. This is the newer: a career site on the employer's
own domain whose `/api/jobs` answers JSON with every posting's description inline, a hundred to
a page, newest first. (The older, `careers-<name>.icims.com`, is `icims.py`.)

- **The board's id is the site's host.** It is whatever domain the employer chose, so it is
  stored, never derived.
- **A board is its pages.** `fetch_board` walks them, waiting five seconds between pages because
  these sites' `robots.txt` asks for that (`crawl-delay: 5`). A poll asks page one, whose
  `ETag` earns a 304 when nothing changed, and walks the rest only when it differs.
- Not every employer-domain career site is this product: one that is not answers `/api/jobs`
  with an error or a page, which is "no such board" here.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from apps.core.http import HttpClient, HttpError
from apps.jobs.sources.clean import clean_description
from apps.jobs.sources.dedupe import content_hash, dedupe_key, job_id
from apps.jobs.sources.js import js_trim
from apps.jobs.sources.normalize import detect_remote, parse_salary, primary_location
from apps.jobs.sources.types import BoardNotFound, NormalizedJob, js_date

SOURCE = "jibe"
PAGE = 100  # the most the endpoint allows; 200 is a 422
MAX_PAGES = 60
CRAWL_DELAY_S = 5.0

_HOST = re.compile(r"^(?:[a-z0-9-]+\.)+[a-z]{2,}$")


def page_url(slug: str, page: int = 1) -> str:
    if not _HOST.match(slug):
        raise BoardNotFound(SOURCE, slug)
    return f"https://{slug}/api/jobs?page={page}&limit={PAGE}"


def board_url(slug: str) -> str:
    """Page one: what a poll asks, and all of a board with a hundred postings or fewer."""
    return page_url(slug, 1)


async def _sleep(seconds: float) -> None:
    # Looked up at call time, so tests can skip the waiting.
    await asyncio.sleep(seconds)


def _locations(job: dict[str, Any]) -> list[str]:
    full = job.get("full_location")
    if isinstance(full, str) and js_trim(full):
        return list(dict.fromkeys(part for part in (js_trim(p) for p in full.split(";")) if part))
    named = [job.get("city"), job.get("state"), job.get("country_code")]
    place = ", ".join(js_trim(part) for part in named if isinstance(part, str) and js_trim(part))
    return [place] if place else []


def normalize_job(slug: str, job: dict[str, Any]) -> NormalizedJob:
    """One posting from `jobs[].data`."""
    title = str(job["title"])
    locations = _locations(job)
    location = primary_location(locations)
    description_raw = job.get("description")
    description_raw = description_raw if isinstance(description_raw, str) else None
    cleaned = clean_description(description_raw)
    # `location_type: ANY` is a posting with no fixed place; there is no remote flag as such.
    remote = detect_remote(
        location=" ".join(locations),
        title=title,
        is_remote_flag=True if job.get("location_type") == "ANY" else None,
        description=cleaned.text,
    )
    named = job.get("hiring_organization")
    company_name = js_trim(named) if isinstance(named, str) and js_trim(named) else slug
    salary = parse_salary(cleaned.text)
    external_id = str(job.get("req_id") or job["slug"])
    meta = job.get("meta_data")
    url = meta.get("canonical_url") if isinstance(meta, dict) else None
    apply_url = job.get("apply_url")
    posted = job.get("posted_date")
    employment = job.get("employment_type")
    return NormalizedJob(
        id=job_id(SOURCE, slug, external_id),
        source=SOURCE,
        external_id=external_id,
        company_slug=slug,
        company_name=company_name,
        title=title,
        locations=locations,
        location_norm=location,
        remote=remote,
        employment_type=employment if isinstance(employment, str) else None,
        salary=salary,
        description_raw=description_raw,
        description_text=cleaned.text or None,
        url=url if isinstance(url, str) and url else f"https://{slug}/jobs/{external_id}",
        apply_url=apply_url if isinstance(apply_url, str) and apply_url else None,
        posted_at=js_date(posted) if isinstance(posted, str) and posted else None,
        raw=job,
        content_hash=content_hash(title, locations, description_raw, salary.min, salary.max),
        dedupe_key=dedupe_key(
            company_name, title, (location.city or location.raw) if location else None
        ),
    )


def _read(slug: str, body: str) -> tuple[list[NormalizedJob], int]:
    """One page's postings, and how many the board says it has."""
    try:
        page = json.loads(body)
    except ValueError:
        raise BoardNotFound(SOURCE, slug) from None  # a page of HTML: not this product
    if not isinstance(page, dict) or not isinstance(page.get("jobs"), list):
        raise BoardNotFound(SOURCE, slug)
    jobs = [
        normalize_job(slug, item["data"])
        for item in page["jobs"]
        if isinstance(item, dict)
        and isinstance(item.get("data"), dict)
        and item["data"].get("title")
    ]
    total = page.get("totalCount")
    return jobs, total if isinstance(total, int) and not isinstance(total, bool) else len(jobs)


def parse_board(slug: str, body: str) -> list[NormalizedJob]:
    """Page one alone. `fetch_board` is what reads a board with more than one page."""
    return _read(slug, body)[0]


async def _page(client: HttpClient, slug: str, number: int) -> tuple[list[NormalizedJob], int]:
    try:
        body = await client.fetch_text(page_url(slug, number))
    except HttpError as error:
        # 404: no such path. 422, 500, 403: a career site that is not this product.
        if number == 1 and error.status in (403, 404, 422, 500):
            raise BoardNotFound(SOURCE, slug) from error
        raise
    return _read(slug, body)


async def count(client: HttpClient, slug: str) -> int:
    """How many postings the site says it has, from page one alone."""
    return (await _page(client, slug, 1))[1]


async def fetch_board(client: HttpClient, slug: str) -> list[NormalizedJob]:
    found: dict[str, NormalizedJob] = {}
    for number in range(1, MAX_PAGES + 1):
        if number > 1:
            await _sleep(CRAWL_DELAY_S)
        jobs, total = await _page(client, slug, number)
        for job in jobs:
            found.setdefault(job.id, job)
        if not jobs or number * PAGE >= total:
            break
    return list(found.values())
