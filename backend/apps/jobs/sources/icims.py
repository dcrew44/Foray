"""iCIMS career portals, the classic kind (#57): `careers-<name>.icims.com`.

The older of the two things an iCIMS employer may run (the newer is `jibe.py`). It has no JSON,
but it has what search engines are given:

- **`/sitemap.xml`** lists every open posting with a real `lastmod`: the listing, the probe and
  each posting's version in one request.
- **Each posting's page** (`/jobs/{id}/job?in_iframe=1`) carries one schema.org `JobPosting` as
  JSON-LD, with the full description. Without `in_iframe=1` the page is a branded wrapper with
  no JSON-LD. A posting that has closed answers 410.

So this is a `ListedAdapter`: the sitemap names what is open and a page is read only for a
posting whose `lastmod` the mirror has not seen.

- **The board's id is the subdomain** (`careers-gdms`). The prefix varies and cannot be guessed.
- A tenant that has moved to the newer product still has the subdomain, but its sitemap answers
  403: that is "no such board" here, and the employer's own career site is the board now.
- `datePosted` is the tenant's local midnight. A tenant that hides the date makes the page invent
  one from the time of the request, different every time: a `datePosted` that is not on the
  hour is treated as missing. `UNAVAILABLE` is the portal's placeholder for a blank field.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import datetime
from typing import Any

from apps.core.http import HTML_ACCEPT, HttpClient, HttpError
from apps.jobs.sources.clean import clean_description
from apps.jobs.sources.dedupe import content_hash, dedupe_key, job_id
from apps.jobs.sources.js import js_trim
from apps.jobs.sources.normalize import detect_remote, parse_salary, primary_location
from apps.jobs.sources.types import BoardNotFound, ListedPosting, Listing, NormalizedJob, js_date

SOURCE = "icims"
BLANK = "UNAVAILABLE"

_SUBDOMAIN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
# A portal link on any page: `careers-gdms.icims.com`.
LINK = re.compile(r"//([a-z0-9][a-z0-9-]*)\.icims\.com", re.IGNORECASE)
_ENTRY = re.compile(
    r"<url>\s*<loc>([^<]*?/jobs/(\d+)/[^<]*)</loc>\s*(?:<lastmod>([^<]+)</lastmod>)?", re.IGNORECASE
)
_JSON_LD = re.compile(
    r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
SITEMAP_MAX_BYTES = 8 * 1024 * 1024


def _host(slug: str) -> str:
    if not _SUBDOMAIN.match(slug):
        raise BoardNotFound(SOURCE, slug)
    return f"{slug}.icims.com"


def sitemap_url(slug: str) -> str:
    return f"https://{_host(slug)}/sitemap.xml"


def posting_url(slug: str, posting_id: str) -> str:
    return f"https://{_host(slug)}/jobs/{posting_id}/job?in_iframe=1"


async def _sitemap(client: HttpClient, slug: str) -> str:
    try:
        return await client.fetch_text(
            sitemap_url(slug), accept="application/xml,text/xml", max_bytes=SITEMAP_MAX_BYTES
        )
    except HttpError as error:
        # 404: no such portal. 403: the tenant moved to the newer product (`jibe.py`).
        if error.status in (403, 404):
            raise BoardNotFound(SOURCE, slug) from error
        raise


async def count(client: HttpClient, slug: str) -> int:
    return len((await list_board(client, slug)).postings)


_MOVED = re.compile(r"location\.href\s*=\s*'https?:\\?/\\?/([a-z0-9.-]+)", re.IGNORECASE)


async def moved_to(client: HttpClient, slug: str) -> str | None:
    """The host of the newer career site a tenant has moved to (`jibe.py`), when its portal's
    search page is only a script that sends the browser there."""
    try:
        page = await client.fetch_text(
            f"https://{_host(slug)}/jobs/search?ss=1&in_iframe=1", accept=HTML_ACCEPT, retries=1
        )
    except HttpError:
        return None
    found = _MOVED.search(page) if len(page) < 2000 else None
    return found.group(1).lower() if found else None


async def probe(client: HttpClient, slug: str) -> str:
    """The sitemap itself: every open posting and when each last changed."""
    body = await _sitemap(client, slug)
    return hashlib.sha1(body.encode(), usedforsecurity=False).hexdigest()


async def list_board(client: HttpClient, slug: str) -> Listing:
    found: dict[str, ListedPosting] = {}
    for location, posting_id, lastmod in _ENTRY.findall(await _sitemap(client, slug)):
        item = {"loc": html.unescape(location), "lastmod": lastmod or None}
        found.setdefault(posting_id, ListedPosting(posting_id, lastmod or "", item))
    return Listing(list(found.values()))


def _said(value: Any) -> str | None:
    """A field's text, or None when the portal left it blank."""
    if not isinstance(value, str):
        return None
    text = js_trim(value)
    return text if text and text != BLANK else None


def _locations(posting: dict[str, Any]) -> list[str]:
    places: list[str] = []
    listed = posting.get("jobLocation")
    for entry in listed if isinstance(listed, list) else [listed]:
        address = entry.get("address") if isinstance(entry, dict) else None
        if not isinstance(address, dict):
            continue
        country = _said(address.get("addressCountry"))
        parts = [
            _said(address.get("addressLocality")),
            _said(address.get("addressRegion")),
            # "Manassas, VA" reads as a US place; a trailing "US" would be taken for a state.
            country if country not in ("US", "USA") else None,
        ]
        place = ", ".join(part for part in parts if part)
        if place:
            places.append(place)
    return list(dict.fromkeys(places))


def _posted(value: Any) -> datetime | None:
    """`datePosted`, unless the page made it up (see the module docstring)."""
    posted = js_date(value) if isinstance(value, str) and value else None
    if posted is None or posted.minute or posted.second or posted.microsecond:
        return None
    return posted


def json_ld(page: str) -> dict[str, Any] | None:
    """The page's `JobPosting`, or None when it has none."""
    for block in _JSON_LD.findall(page):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("@type") == "JobPosting":
            return data
    return None


def normalize_job(
    slug: str, posting_id: str, posting: dict[str, Any], company_name: str
) -> NormalizedJob:
    """One posting from its page's JSON-LD."""
    title = html.unescape(str(posting["title"]))
    locations = _locations(posting)
    location = primary_location(locations)
    description_raw = posting.get("description")
    description_raw = description_raw if isinstance(description_raw, str) else None
    cleaned = clean_description(description_raw)
    remote = detect_remote(location=" ".join(locations), title=title, description=cleaned.text)

    organization = posting.get("hiringOrganization")
    named = _said(organization.get("name")) if isinstance(organization, dict) else None
    name = named or company_name
    employment = _said(posting.get("employmentType"))
    salary = parse_salary(cleaned.text)
    url = _said(posting.get("url"))
    return NormalizedJob(
        id=job_id(SOURCE, slug, posting_id),
        source=SOURCE,
        external_id=posting_id,
        company_slug=slug,
        company_name=name,
        title=title,
        locations=locations,
        location_norm=location,
        remote=remote,
        employment_type=employment if employment != "OTHER" else None,
        salary=salary,
        description_raw=description_raw,
        description_text=cleaned.text or None,
        url=url or f"https://{_host(slug)}/jobs/{posting_id}/job",
        # Applying starts from the posting's own page.
        apply_url=None,
        posted_at=_posted(posting.get("datePosted")),
        raw=posting,
        content_hash=content_hash(title, locations, description_raw, salary.min, salary.max),
        dedupe_key=dedupe_key(name, title, (location.city or location.raw) if location else None),
    )


async def read_posting(
    client: HttpClient, slug: str, listed: ListedPosting, company_name: str
) -> NormalizedJob | None:
    try:
        page = await client.fetch_text(posting_url(slug, listed.external_id), accept=HTML_ACCEPT)
    except HttpError as error:
        if error.status in (404, 410):
            return None
        raise
    posting = json_ld(page)
    if posting is None or not posting.get("title"):
        return None
    return normalize_job(slug, listed.external_id, posting, company_name)
