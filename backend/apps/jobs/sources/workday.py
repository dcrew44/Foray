"""Workday career sites (#57).

A Workday board is one career site of one tenant: `nvidia.wd5.myworkdayjobs.com/
NVIDIAExternalCareerSite`. The public page is an app, and the backend it talks to
(`/wday/cxs/{tenant}/{site}`) answers plain JSON to anyone, which is what this reads. Nothing
documents it: everything here is behaviour observed on sixty tenants in November 2026.

- **The board's id is its address**, stored as the careers URL without the scheme: `{host}/{site}`
  on `myworkdayjobs.com` (the tenant is the first label of the host), and
  `{host}/recruiting/{tenant}/{site}` on `myworkdaysite.com`. The `wdN` data centre is part of
  the host and cannot be guessed.
- **The listing** is a POST, twenty postings a page, with no descriptions and no dates beyond
  "Posted 3 Days Ago". It comes in two server variants. One gives an exact `total` on every
  page. The other gives it on the first page only, caps it at 2,000, and answers any offset past
  2,000 with page one again, so "stop at an empty page" would loop for ever: the walk stops at
  the first page's total, at a short page, or at the cap, and a board at exactly 2,000 is
  reported incomplete. Items without an `externalPath` are placeholders and are skipped.
- **A posting** is one more request, which gives the description, every location and
  `startDate`, the day it was posted. That is why this is a `ListedAdapter`: a board is listed
  cheaply and only postings the mirror has not read are fetched (`refresh.refresh_listed`).
- **The probe** is page one: its total and the ids, titles and places on it, without `postedOn`,
  whose wording changes at midnight.

A site that does not exist answers 404 and an unknown tenant (or the wrong `wdN`) 422; both are
`BoardNotFound`. A site with nothing posted answers 200 with no postings.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from apps.core.http import HttpClient, HttpError
from apps.jobs.sources.clean import clean_description
from apps.jobs.sources.dedupe import content_hash, dedupe_key, job_id
from apps.jobs.sources.js import encode_uri_component, js_trim
from apps.jobs.sources.normalize import detect_remote, parse_salary, primary_location
from apps.jobs.sources.types import (
    BoardNotFound,
    ListedPosting,
    Listing,
    NormalizedJob,
    RemoteKind,
    js_date,
)

SOURCE = "workday"
PAGE = 20  # the most the endpoint allows; 21 is a 400
CAP = 2000  # where one server variant stops counting
MAX_PAGES = 500  # 10,000 postings: past this a board is not worth mirroring whole
LIST_CONCURRENCY = 4  # pages of one board's listing asked for at once

_HOST = re.compile(r"^(?:[a-z0-9-]+\.)?wd\d+\.myworkday(?:jobs|site)\.com$")
_SEGMENT = re.compile(r"^[A-Za-z0-9_-]+$")
_LOCALE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
# A careers link on any page: `tenant.wdN.myworkdayjobs.com[/en-US]/Site`, or
# `wdN.myworkdaysite.com[/en-US]/recruiting/tenant/Site`.
LINK = re.compile(
    r"(?P<host>[a-z0-9-]+\.wd\d+\.myworkdayjobs\.com)/(?:[a-z]{2}-[A-Z]{2}/)?(?P<site>[A-Za-z0-9_-]+)"
    r"|(?P<host2>wd\d+\.myworkdaysite\.com)/(?:[a-z]{2}-[A-Z]{2}/)?recruiting/"
    r"(?P<tenant2>[A-Za-z0-9_-]+)/(?P<site2>[A-Za-z0-9_-]+)"
)
# Path segments under a Workday host that are not career sites.
NOT_SITES = frozenset({"wday", "job", "jobs", "login", "userHome", "assets", "robots.txt"})

WORKPLACE: dict[str, RemoteKind] = {
    "remote": "remote",
    "hybrid": "hybrid",
    "flex": "hybrid",
    "onsite": "onsite",
    "on-site": "onsite",
    "office": "onsite",
}
# "US, CA, Santa Clara" and "USA, TX, Remote": country first, which no location parser expects.
_COUNTRY_FIRST = re.compile(
    r"^(?:USA?|United States(?: of America)?)\s*[,-]\s*([A-Z]{2})\s*[,-]\s*(.+)$"
)
# "Lemont, IL USA": the country after the state, which the parser would read into the state.
_COUNTRY_LAST = re.compile(r"^(.+,\s*[A-Z]{2})[\s,]+(?:USA?|United States(?: of America)?)$")


@dataclass(frozen=True, slots=True)
class Board:
    host: str
    tenant: str
    site: str

    @property
    def api(self) -> str:
        tenant, site = encode_uri_component(self.tenant), encode_uri_component(self.site)
        return f"https://{self.host}/wday/cxs/{tenant}/{site}"

    @property
    def public(self) -> str:
        """The career site's address, which every posting's URL starts with."""
        if self.host.endswith("myworkdaysite.com"):
            return f"https://{self.host}/recruiting/{self.tenant}/{self.site}"
        return f"https://{self.host}/{self.site}"

    @property
    def key(self) -> str:
        """The middle of a job id: the tenant and the site, which no colon or slash can be in."""
        return f"{self.tenant}.{self.site}"

    @property
    def slug(self) -> str:
        return self.public.removeprefix("https://")


def parse_slug(slug: str) -> Board:
    """The board a stored slug names. A slug that is not one is a board that does not exist."""
    host, _, path = slug.partition("/")
    parts = [part for part in path.split("/") if part]
    if _HOST.match(host) and all(_SEGMENT.match(part) for part in parts):
        if len(parts) == 3 and parts[0] == "recruiting":
            return Board(host, parts[1], parts[2])
        if len(parts) == 1 and host.count(".") == 3:
            return Board(host, host.split(".")[0], parts[0])
    raise BoardNotFound(SOURCE, slug)


def boards_in(text: str) -> list[Board]:
    """Every career site linked in a page or a pasted URL, in order, once each."""
    found: list[Board] = []
    for match in LINK.finditer(text):
        if match.group("host"):
            host, site = match.group("host").lower(), match.group("site")
            board = Board(host, host.split(".")[0], site)
        else:
            board = Board(
                match.group("host2").lower(), match.group("tenant2"), match.group("site2")
            )
        if board.site not in NOT_SITES and not _LOCALE.match(board.site) and board not in found:
            found.append(board)
    return found


# --- the listing ---------------------------------------------------------------------------------


async def _page(
    client: HttpClient, board: Board, slug: str, offset: int, search: str = ""
) -> dict[str, Any]:
    body = {"appliedFacets": {}, "limit": PAGE, "offset": offset, "searchText": search}
    try:
        text = await client.post_json(f"{board.api}/jobs", body)
    except HttpError as error:
        if error.status in (404, 422):
            raise BoardNotFound(SOURCE, slug) from error
        raise
    page = json.loads(text)
    if not isinstance(page, dict):
        raise ValueError(f"Unexpected workday response for {slug!r}")
    return page


def _raw_items(page: dict[str, Any]) -> list[Any]:
    items = page.get("jobPostings")
    return items if isinstance(items, list) else []


def posting_id(item: dict[str, Any]) -> str:
    """The stable id: the last segment of `externalPath` (`Title-Slug_JR2019801`)."""
    return str(item["externalPath"]).rsplit("/", 1)[-1]


def _listed(item: dict[str, Any]) -> ListedPosting:
    shown = json.dumps([item.get("title"), item.get("locationsText")], ensure_ascii=False)
    version = hashlib.sha1(shown.encode(), usedforsecurity=False).hexdigest()[:16]
    return ListedPosting(posting_id(item), version, item)


def _real(items: list[Any]) -> list[dict[str, Any]]:
    return [item for item in items if isinstance(item, dict) and item.get("externalPath")]


async def count(client: HttpClient, slug: str, search: str = "") -> int:
    """How many postings the site lists (at most `CAP` on the variant that stops counting), or
    how many answer the site's own search for `search`."""
    page = await _page(client, parse_slug(slug), slug, 0, search)
    total = page.get("total")
    listed = len(_real(_raw_items(page)))
    return max(total, listed) if isinstance(total, int) and not isinstance(total, bool) else listed


async def probe(client: HttpClient, slug: str) -> str:
    """A fingerprint of page one: it changes when a posting is added (they are listed newest
    first, roughly), removed (the total moves) or renamed."""
    page = await _page(client, parse_slug(slug), slug, 0)
    shown = [
        page.get("total"),
        [[posting_id(item), item.get("title")] for item in _real(_raw_items(page))],
    ]
    encoded = json.dumps(shown, ensure_ascii=False).encode()
    return hashlib.sha1(encoded, usedforsecurity=False).hexdigest()


async def list_board(client: HttpClient, slug: str) -> Listing:
    board = parse_slug(slug)
    first = await _page(client, board, slug, 0)
    total = first.get("total")
    total = total if isinstance(total, int) and not isinstance(total, bool) else 0
    raw = _raw_items(first)
    found: dict[str, ListedPosting] = {
        listed.external_id: listed for listed in map(_listed, _real(raw))
    }
    # Not "until a page is empty": one server variant answers an offset past its cap with page
    # one again. The first page's total is the only one both variants give, so it says how many
    # pages to ask for, a few at a time (a page takes a second or two, and a big board has a
    # hundred).
    offsets = range(PAGE, min(total, MAX_PAGES * PAGE), PAGE) if len(raw) == PAGE else range(0)
    gate = client.gate(f"listing:{SOURCE}:{slug}", LIST_CONCURRENCY)

    async def more(offset: int) -> list[Any]:
        async with gate:
            return _raw_items(await _page(client, board, slug, offset))

    try:
        async with asyncio.TaskGroup() as group:
            pages = [group.create_task(more(offset)) for offset in offsets]
    except ExceptionGroup as errors:
        raise errors.exceptions[0] from None  # what one page said, not that a group failed
    for page in pages:
        for listed in map(_listed, _real(page.result())):
            found.setdefault(listed.external_id, listed)
    complete = total != CAP and total <= MAX_PAGES * PAGE
    return Listing(list(found.values()), complete=complete)


# --- one posting ---------------------------------------------------------------------------------


def _place(text: str) -> str:
    """ "US, CA, Santa Clara" as "Santa Clara, CA": the order the location parser reads, without
    a country code it would take for a second state."""
    text = js_trim(text)
    match = _COUNTRY_FIRST.match(text)
    if match:
        return f"{js_trim(match.group(2))}, {match.group(1)}"
    match = _COUNTRY_LAST.match(text)
    return match.group(1) if match else text


def _locations(info: dict[str, Any]) -> tuple[list[str], RemoteKind | None]:
    """The places a posting names, and the workplace when a tenant lists one among them
    ("On-site" as an additional location)."""
    more = info.get("additionalLocations")
    named = [info.get("location"), *(more if isinstance(more, list) else [])]
    places = list(
        dict.fromkeys(_place(value) for value in named if isinstance(value, str) and js_trim(value))
    )
    words = [place for place in places if place.lower() in WORKPLACE]
    if len(words) == len(places):
        return places, None
    workplace = WORKPLACE[words[0].lower()] if words else None
    return [place for place in places if place not in words], workplace


def normalize_job(slug: str, info: dict[str, Any], company_name: str) -> NormalizedJob:
    """One posting from a detail's `jobPostingInfo`."""
    board = parse_slug(slug)
    title = str(info["title"])
    locations, listed_workplace = _locations(info)
    location = primary_location(locations)
    description_raw = info.get("jobDescription")
    description_raw = description_raw if isinstance(description_raw, str) else None
    cleaned = clean_description(description_raw)

    workplace = info.get("remoteType")
    remote = WORKPLACE.get(workplace.lower() if isinstance(workplace, str) else "")
    remote = remote or listed_workplace
    if remote is None:
        remote = detect_remote(location=" ".join(locations), title=title, description=cleaned.text)

    salary = parse_salary(cleaned.text)
    external_id = str(info["jobPostingId"])
    url = info.get("externalUrl")
    time_type = info.get("timeType")
    return NormalizedJob(
        id=job_id(SOURCE, board.key, external_id),
        source=SOURCE,
        external_id=external_id,
        company_slug=board.key,
        company_name=company_name,
        title=title,
        locations=locations,
        location_norm=location,
        remote=remote,
        employment_type=time_type if isinstance(time_type, str) else None,
        salary=salary,
        description_raw=description_raw,
        description_text=cleaned.text or None,
        url=url if isinstance(url, str) and url else f"{board.public}/job/{external_id}",
        # The apply flow is the posting's own page.
        apply_url=None,
        # `startDate` is the day it was posted; the listing only says "Posted 3 Days Ago".
        posted_at=js_date(info["startDate"]) if isinstance(info.get("startDate"), str) else None,
        raw=info,
        content_hash=content_hash(title, locations, description_raw, salary.min, salary.max),
        dedupe_key=dedupe_key(
            company_name, title, (location.city or location.raw) if location else None
        ),
    )


async def read_posting(
    client: HttpClient, slug: str, listed: ListedPosting, company_name: str
) -> NormalizedJob | None:
    """The posting's detail, or None when it has gone since the listing was read."""
    board = parse_slug(slug)
    try:
        # The location segment of the path is ignored by the server; the id alone finds it.
        text = await client.fetch_text(
            f"{board.api}/job/{encode_uri_component(listed.external_id)}"
        )
    except HttpError as error:
        if error.status == 404:
            return None
        raise
    detail = json.loads(text)
    info = detail.get("jobPostingInfo") if isinstance(detail, dict) else None
    if not isinstance(info, dict) or not info.get("title") or not info.get("jobPostingId"):
        return None
    return normalize_job(slug, info, company_name)
