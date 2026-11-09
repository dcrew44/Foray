"""SmartRecruiters (#57): the public Posting API, `api.smartrecruiters.com/v1/companies/{id}`.

Documented and anonymous: the vendor's own reference lists the list and detail operations with
no security requirement, for building career sites. (Its `robots.txt` on the API host turns
every crawler but LinkedIn's away, so Foray reads only the companies someone added by hand and
does not go looking for more: `discovery` never guesses a SmartRecruiters identifier.)

- **The board's id** is the company identifier in `jobs.smartrecruiters.com/{id}/...`, often with
  a digit (`LinkedIn3`, `AECOM2`). It is case-insensitive on the way in.
- **The listing** is a hundred postings a page, newest release first, without descriptions, so
  this is a `ListedAdapter`. A posting's `releasedDate` moves when the employer re-posts it,
  which is the only way its content changes: that date is its version.
- **An unknown company is not a 404.** It answers 200 with nothing, exactly like a real company
  with nothing open. The two are told apart by the company's own page: it redirects away for a
  company that does not exist.
- **The probe** is one posting (`limit=1`): the total and the newest release.

`creator` (a recruiter's name) is personal data and is dropped before anything is stored.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from apps.core.http import HttpClient, HttpError
from apps.jobs.sources.clean import clean_description
from apps.jobs.sources.dedupe import content_hash, dedupe_key, job_id
from apps.jobs.sources.js import encode_uri_component, js_trim
from apps.jobs.sources.normalize import detect_remote, parse_salary, primary_location
from apps.jobs.sources.types import BoardNotFound, ListedPosting, Listing, NormalizedJob, js_date

SOURCE = "smartrecruiters"
BASE = "https://api.smartrecruiters.com/v1/companies"
PAGE = 100  # the documented maximum
MAX_PAGES = 100

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]+$")
# A posting or a company page: `jobs.smartrecruiters.com/{id}/...`,
# `careers.smartrecruiters.com/{id}`.
LINK = re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/(?!oneclick-ui|ni/)([A-Za-z0-9_-]+)")
_SECTIONS = ("companyDescription", "jobDescription", "qualifications", "additionalInformation")


def _checked(slug: str) -> str:
    if not _IDENTIFIER.match(slug):
        raise BoardNotFound(SOURCE, slug)
    return encode_uri_component(slug)


def list_url(slug: str, *, limit: int = PAGE, offset: int = 0) -> str:
    return f"{BASE}/{_checked(slug)}/postings?limit={limit}&offset={offset}"


def posting_url(slug: str, posting_id: str) -> str:
    return f"{BASE}/{_checked(slug)}/postings/{encode_uri_component(posting_id)}"


def company_page(slug: str) -> str:
    return f"https://careers.smartrecruiters.com/{_checked(slug)}"


async def _page(client: HttpClient, slug: str, *, limit: int, offset: int) -> dict[str, Any]:
    try:
        page = json.loads(await client.fetch_text(list_url(slug, limit=limit, offset=offset)))
    except HttpError as error:
        if error.status == 404:
            raise BoardNotFound(SOURCE, slug) from error
        raise
    if not isinstance(page, dict):
        raise ValueError(f"Unexpected smartrecruiters response for {slug!r}")
    return page


def _content(page: dict[str, Any]) -> list[dict[str, Any]]:
    content = page.get("content")
    items = content if isinstance(content, list) else []
    return [item for item in items if isinstance(item, dict) and item.get("id")]


async def _exists(client: HttpClient, slug: str) -> bool:
    """Whether the company exists at all: its page answers, where an unknown one redirects."""
    status, _location = await client.status_of(company_page(slug))
    return status == 200


async def count(client: HttpClient, slug: str) -> int:
    page = await _page(client, slug, limit=1, offset=0)
    total = page.get("totalFound")
    total = total if isinstance(total, int) and not isinstance(total, bool) else 0
    if total == 0 and not await _exists(client, slug):
        raise BoardNotFound(SOURCE, slug)
    return total


async def probe(client: HttpClient, slug: str) -> str:
    """The total and the newest release: a new posting leads the list, a closed one moves the
    total."""
    page = await _page(client, slug, limit=1, offset=0)
    newest = [[item["id"], item.get("releasedDate")] for item in _content(page)]
    shown = json.dumps([page.get("totalFound"), newest]).encode()
    return hashlib.sha1(shown, usedforsecurity=False).hexdigest()


async def list_board(client: HttpClient, slug: str) -> Listing:
    found: dict[str, ListedPosting] = {}
    total = 0
    for number in range(MAX_PAGES):
        page = await _page(client, slug, limit=PAGE, offset=number * PAGE)
        content = _content(page)
        counted = page.get("totalFound")
        total = counted if isinstance(counted, int) and not isinstance(counted, bool) else 0
        for item in content:
            version = f"{item.get('releasedDate')}|{item.get('name')}"
            found.setdefault(str(item["id"]), ListedPosting(str(item["id"]), version, item))
        if not content or (number + 1) * PAGE >= total:
            break
    if not found and not await _exists(client, slug):
        raise BoardNotFound(SOURCE, slug)
    return Listing(list(found.values()), complete=total <= MAX_PAGES * PAGE)


def _description(detail: dict[str, Any]) -> str | None:
    """The four sections of the ad, as the public page shows them, under their own titles."""
    job_ad = detail.get("jobAd")
    sections = job_ad.get("sections") if isinstance(job_ad, dict) else None
    if not isinstance(sections, dict):
        return None
    parts: list[str] = []
    for name in _SECTIONS:
        section = sections.get(name)
        if not isinstance(section, dict):
            continue
        text = section.get("text")
        if isinstance(text, str) and js_trim(text):
            title = section.get("title")
            if isinstance(title, str) and js_trim(title):
                parts.append(f"<h3>{title}</h3>")
            parts.append(text)
    return "\n".join(parts) if parts else None


def _locations(detail: dict[str, Any]) -> list[str]:
    """One place per posting. `fullLocation` spells the country out ("San Diego, CALIFORNIA,
    United States"), which the location parser reads; the bare code it would take for a state."""
    location = detail.get("location")
    if not isinstance(location, dict):
        return []
    full = location.get("fullLocation")
    if isinstance(full, str) and js_trim(full):
        return [js_trim(full)]
    named = [location.get("city"), location.get("region")]
    place = ", ".join(js_trim(part) for part in named if isinstance(part, str) and js_trim(part))
    return [place] if place else []


def normalize_job(slug: str, detail: dict[str, Any], company_name: str) -> NormalizedJob:
    """One posting from its detail. `slug` is the company identifier as registered."""
    title = str(detail["name"])
    locations = _locations(detail)
    location = primary_location(locations)
    description_raw = _description(detail)
    cleaned = clean_description(description_raw)

    placed = detail.get("location")
    place: dict[str, Any] = placed if isinstance(placed, dict) else {}
    if place.get("hybrid") is True:
        remote = "hybrid"
    else:
        remote = detect_remote(
            location=" ".join(locations),
            title=title,
            is_remote_flag=True if place.get("remote") is True else None,
            description=cleaned.text,
        )

    company = detail.get("company")
    named = company.get("name") if isinstance(company, dict) else None
    name = js_trim(named) if isinstance(named, str) and js_trim(named) else company_name
    employment = detail.get("typeOfEmployment")
    salary = parse_salary(cleaned.text)
    external_id = str(detail["id"])
    released = detail.get("releasedDate")
    url = detail.get("postingUrl")
    apply_url = detail.get("applyUrl")
    return NormalizedJob(
        id=job_id(SOURCE, slug, external_id),
        source=SOURCE,
        external_id=external_id,
        company_slug=slug,
        company_name=name,
        title=title,
        locations=locations,
        location_norm=location,
        remote=remote,
        employment_type=employment.get("label") if isinstance(employment, dict) else None,
        salary=salary,
        description_raw=description_raw,
        description_text=cleaned.text or None,
        url=url
        if isinstance(url, str) and url
        else f"https://jobs.smartrecruiters.com/{slug}/{external_id}",
        apply_url=apply_url if isinstance(apply_url, str) and apply_url else None,
        posted_at=js_date(released) if isinstance(released, str) and released else None,
        # A recruiter's name is not ours to keep.
        raw={key: value for key, value in detail.items() if key != "creator"},
        content_hash=content_hash(title, locations, description_raw, salary.min, salary.max),
        dedupe_key=dedupe_key(name, title, (location.city or location.raw) if location else None),
    )


async def read_posting(
    client: HttpClient, slug: str, listed: ListedPosting, company_name: str
) -> NormalizedJob | None:
    try:
        detail = json.loads(await client.fetch_text(posting_url(slug, listed.external_id)))
    except HttpError as error:
        if error.status == 404:
            return None
        raise
    if not isinstance(detail, dict) or not detail.get("id") or not detail.get("name"):
        return None
    return normalize_job(slug, detail, company_name)
