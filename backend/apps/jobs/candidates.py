"""Growing the registry from public lists of board ids (#58, #28).

The radar is only as wide as the list of boards it polls, and the list it started with was
somebody else's (CC BY-NC). A board id is a fact that shows in a URL, so Foray collects its own:
`crawl_index` reads them out of Common Crawl's index, a licensed list can be loaded from a file,
and each one lands here as a `BoardCandidate` with where it was seen (`found_in`).

A candidate is only a rumour. `grow` asks each one's board once, newest-seen first (a board in
last month's crawl is far likelier to be alive than one last seen a year ago):

- it answers with postings: the company is registered with the candidate's provenance, and for a
  board read whole the postings are ingested from that same answer, so nothing is fetched twice;
- it answers with none, or there is no such board: remembered, and not asked again;
- it answers, and nothing it lists is a software role (`radar.is_software_title`): remembered as
  skipped. Foray's first users are looking for those, a third of the boards in a crawl belong to
  clinics, schools and shops, and every board followed is polled every hour for good;
- the question failed: asked again next time.

A company already in the registry that is also a candidate gains the candidate's provenance
(`link_known`), which is how a board first learned from the old seed stops being traceable only
to it. Nothing here guesses a name against a board: SmartRecruiters is left out unless asked
for (#69), and a Workday site can be required to answer its own search for some text
(`matching`), since most of the thousands of Workday sites belong to hospitals and retailers.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urlsplit

from django.db import connection, transaction
from django.db.models import Count
from django.db.models.functions import Now
from django.utils import timezone

from apps.core.db import db
from apps.core.http import HTML_ACCEPT, HttpClient
from apps.core.ids import new_id
from apps.jobs import radar
from apps.jobs.ingest import ingest_jobs
from apps.jobs.level import is_early_career_title
from apps.jobs.models import (
    BoardCandidate,
    CandidateStatus,
    Company,
    DiscoveredVia,
    FetchStatus,
    SourceType,
)
from apps.jobs.refresh import SOURCE_PACE_S
from apps.jobs.registry import CompanyInput, record_fetch, upsert_company
from apps.jobs.sources import board_adapter, listed_adapter, workday
from apps.jobs.sources.poll import PollState, poll_board
from apps.jobs.sources.types import BoardNotFound, NormalizedJob

__all__ = [
    "DEFAULT_SOURCES",
    "Checked",
    "GrowSummary",
    "Seen",
    "add_candidates",
    "board_in_url",
    "candidate_counts",
    "display_name",
    "grow",
    "link_known",
    "next_candidates",
    "seed_rows",
    "seen_in_urls",
]

log = logging.getLogger("foray.jobs")

# The sources `grow` asks unless told otherwise. SmartRecruiters is read only for a company
# someone added (#69); the others either need a link to be found or are not collected yet.
DEFAULT_SOURCES = ("greenhouse", "ashby", "lever")
# We are a guest: this many questions at a time to one source.
PER_SOURCE = 4
BATCH = 1000

_GREENHOUSE_HOSTS = frozenset(
    f"{name}{region}.greenhouse.io"
    for name in ("boards", "job-boards")
    for region in ("", ".eu", ".anz")
)
# First path segments on these hosts that are not boards.
_NOT_BOARDS = frozenset(
    {
        "embed", "robots.txt", "favicon.ico", "assets", "packs", "js", "css", "images", "img",
        "fonts", "static", "api", "v0", "v1", "jobs", "job", "careers", "sitemap.xml", "users",
        "login", "404", "500", "index.html", "ads.txt", "security.txt", "admin", "test",
        "undefined", "null", "meeting", "b", "_next", ".well-known", "apply", "search",
        "internal_job_board", "oneclick-ui", "external-referrals", "ni", "my-applications",
        "company", "sr-jobs", "app",
    }
)  # fmt: skip
# A board id a URL can carry without escaping (Ashby allows spaces and dots; those few are left).
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,80}$")
_TITLE = re.compile(r"<title[^>]*>([^<]{1,200})</title>", re.IGNORECASE)
_TITLE_TAIL = re.compile(
    r"\s*(?:[-|@:·–—]\s*)?(?:jobs?|careers?|job board|open (?:roles|positions))\s*$",
    re.IGNORECASE,
)
_SEPARATORS = re.compile(r"[-_]+")
_HOSTED_PAGE = {
    "ashby": "https://jobs.ashbyhq.com/{slug}",
    "lever": "https://jobs.lever.co/{slug}",
}


@dataclass(frozen=True, slots=True)
class Seen:
    """One sighting of a board: where (`found_in`), and for a crawl how often and how lately."""

    source: str
    slug: str
    found_in: str
    seen: int = 1
    newest: str = ""


# --- reading board ids out of URLs ---------------------------------------------------------------


def _segments(path: str) -> list[str]:
    return [unquote(part).strip() for part in path.split("/") if part]


def board_in_url(url: str) -> tuple[str, str] | None:
    """The board a URL belongs to, as `(source, slug)` with the slug the way its adapter wants
    it, or None. Greenhouse and Ashby ids are case-blind and come back lowercased; Lever's and
    SmartRecruiters' are not and are kept as seen. Lever's EU host is another service, which
    Foray does not read."""
    try:
        parts = urlsplit(url if "://" in url else f"https://{url}")
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    segments = _segments(parts.path)
    slug: str | None = None
    source: str

    if host.endswith(".myworkdayjobs.com") or host.endswith(".myworkdaysite.com"):
        boards = workday.boards_in(url)
        return ("workday", boards[0].slug) if boards else None
    if host in _GREENHOUSE_HOSTS:
        source = "greenhouse"
        if segments[:1] == ["embed"]:
            slug = (parse_qs(parts.query).get("for") or [None])[0]
        elif segments:
            slug = segments[0]
    elif host == "boards-api.greenhouse.io" and segments[:2] == ["v1", "boards"]:
        source, slug = "greenhouse", (segments[2] if len(segments) > 2 else None)
    elif host == "jobs.ashbyhq.com":
        source, slug = "ashby", (segments[0] if segments else None)
    elif host == "api.ashbyhq.com" and segments[:2] == ["posting-api", "job-board"]:
        source, slug = "ashby", (segments[2] if len(segments) > 2 else None)
    elif host == "jobs.lever.co":
        source, slug = "lever", (segments[0] if segments else None)
    elif host == "api.lever.co" and segments[:2] == ["v0", "postings"]:
        source, slug = "lever", (segments[2] if len(segments) > 2 else None)
    elif host == "jobs.gem.com":
        source, slug = "gem", (segments[0] if segments else None)
    elif host == "apply.workable.com":
        source, slug = "workable", (segments[0] if segments else None)
    elif host.endswith(".recruitee.com") and host.count(".") == 2:
        source, slug = "recruitee", host.split(".")[0]
    elif host in ("jobs.smartrecruiters.com", "careers.smartrecruiters.com"):
        source = "smartrecruiters"
        first = segments[0].lower() if segments else ""
        if first in ("oneclick-ui", "external-referrals") and segments[1:2] == ["company"]:
            slug = segments[2] if len(segments) > 2 else None
        elif first in ("ni", "my-applications"):
            slug = segments[1] if len(segments) > 1 else None
        elif segments:
            slug = segments[0]
    else:
        return None

    if slug is None:
        return None
    slug = slug.strip()
    if source in ("greenhouse", "ashby", "gem", "workable", "recruitee"):
        slug = slug.lower()
    if slug.lower() in _NOT_BOARDS or not _SLUG.match(slug):
        return None
    return source, slug


def seen_in_urls(urls: Iterable[str], found_in: str, newest: str = "") -> list[Seen]:
    """The boards a crawl's URLs name, once each. Where a case-blind id was seen in several
    spellings (a Workday site, a SmartRecruiters company) the commonest stands for all."""
    spellings: dict[tuple[str, str], Counter[str]] = {}
    for url in urls:
        board = board_in_url(url)
        if board is None:
            continue
        source, slug = board
        key = (source, slug if source == "lever" else slug.lower())
        spellings.setdefault(key, Counter())[slug] += 1
    return [
        Seen(source, counter.most_common(1)[0][0], found_in, 1, newest)
        for (source, _), counter in sorted(spellings.items())
    ]


# --- the candidates table ------------------------------------------------------------------------

_UPSERT = """
    INSERT INTO board_candidates
        (id, source_type, ats_slug, found_in, seen, newest, status, created_at, updated_at)
    VALUES (%s, %s, %s, ARRAY[%s]::text[], %s, %s, 'new', now(), now())
    ON CONFLICT (source_type, ats_slug) DO UPDATE SET
        found_in = board_candidates.found_in || ARRAY(
            SELECT u FROM unnest(EXCLUDED.found_in) AS u
            WHERE NOT (u = ANY (board_candidates.found_in))),
        seen = greatest(board_candidates.seen, EXCLUDED.seen),
        newest = greatest(board_candidates.newest, EXCLUDED.newest),
        updated_at = now()
"""
# A company and a candidate are the same board when source and id agree; Lever's ids alone are
# case-sensitive.
_SAME_BOARD = """
    c.source_type = b.source_type
    AND (c.ats_slug = b.ats_slug
         OR (b.source_type <> 'lever' AND lower(c.ats_slug) = lower(b.ats_slug)))
"""


def add_candidates(sightings: Iterable[Seen]) -> int:
    """Records sightings; one already known gains the new place it was found. Returns how many
    were given. Sync."""
    rows = [
        (new_id(), seen.source, seen.slug, seen.found_in, seen.seen, seen.newest)
        for seen in sightings
        if seen.source in SourceType.values and seen.slug
    ]
    with transaction.atomic(), connection.cursor() as cursor:
        for start in range(0, len(rows), BATCH):
            cursor.executemany(_UPSERT, rows[start : start + BATCH])
        # A board read out of one crawl at a time has been seen in as many as it names.
        cursor.execute(
            """
            UPDATE board_candidates b SET seen = crawls.n
            FROM (SELECT id, count(*) AS n FROM board_candidates, unnest(found_in) AS place
                  WHERE place LIKE 'commoncrawl:%%' GROUP BY id) AS crawls
            WHERE crawls.id = b.id AND crawls.n > b.seen
            """
        )
    return len(rows)


def link_known() -> int:
    """Companies that are also candidates gain the candidates' provenance, and those candidates
    are settled as live. Returns how many companies learned of a new source. Sync."""
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            f"""
            UPDATE companies c SET
                found_in = c.found_in || ARRAY(
                    SELECT u FROM unnest(b.found_in) AS u WHERE NOT (u = ANY (c.found_in))),
                updated_at = now()
            FROM board_candidates b
            WHERE {_SAME_BOARD} AND NOT (b.found_in <@ c.found_in)
            """  # noqa: S608 (a constant join condition, no values)
        )
        linked = cursor.rowcount
        cursor.execute(
            f"""
            UPDATE board_candidates b SET status = 'live', checked_at = now(), updated_at = now()
            FROM companies c
            WHERE {_SAME_BOARD} AND b.status IN ('new', 'error')
            """  # noqa: S608
        )
    return linked


def next_candidates(
    sources: Sequence[str], limit: int, *, retry: bool = False, place: str = ""
) -> list[BoardCandidate]:
    """The candidates to ask next: never asked (and with `retry` those whose question failed),
    newest-seen first, then most-often seen; only those found in `place` when one is given.
    Sync."""
    statuses = [CandidateStatus.NEW, *([CandidateStatus.ERROR] if retry else [])]
    rows = BoardCandidate.objects.filter(status__in=statuses, source_type__in=list(sources))
    if place:
        rows = rows.filter(found_in__contains=[place])
    return list(rows.order_by("-newest", "-seen", "ats_slug")[:limit])


def candidate_counts() -> dict[str, dict[str, int]]:
    """`{source: {status: count}}`. Sync."""
    counts: dict[str, dict[str, int]] = {}
    rows = BoardCandidate.objects.values_list("source_type", "status").annotate(n=Count("id"))
    for source, status, count in rows.order_by("source_type", "status"):
        counts.setdefault(source, {})[status] = count
    return counts


# --- asking a board ------------------------------------------------------------------------------


@dataclass(slots=True)
class Checked:
    status: str
    postings: int | None = None
    name: str = ""
    # A board read whole: what it answered, to ingest without asking again.
    jobs: list[NormalizedJob] | None = None
    state: PollState | None = None


def display_name(slug: str) -> str:
    """A name for a board nobody named: `scale-ai` is "Scale Ai". Good enough to find it by."""
    words = _SEPARATORS.sub(" ", slug).split()
    return " ".join(word[:1].upper() + word[1:] for word in words) or slug


def _title_name(page: str) -> str | None:
    match = _TITLE.search(page)
    if match is None:
        return None
    name = _TITLE_TAIL.sub("", " ".join(html.unescape(match.group(1)).split())).strip()
    return name if 1 < len(name) <= 80 else None


async def _name(client: HttpClient, source: str, slug: str, jobs: Sequence[NormalizedJob]) -> str:
    """What the company calls itself: Greenhouse says in every posting; Ashby and Lever only in
    the title of the page people see."""
    if source == "greenhouse" and jobs and jobs[0].company_name not in ("", slug):
        return " ".join(jobs[0].company_name.split())
    hosted = _HOSTED_PAGE.get(source)
    if hosted is not None:
        try:
            page = await client.fetch_text(
                hosted.format(slug=slug), accept=HTML_ACCEPT, timeout=15, retries=0
            )
        except Exception as error:
            log.debug("no name for %s:%s: %s", source, slug, error)
        else:
            return _title_name(page) or display_name(slug)
    return display_name(slug)


async def _check(
    client: HttpClient, candidate: BoardCandidate, matching: str, any_board: bool
) -> Checked:
    source, slug = candidate.source_type, candidate.ats_slug
    try:
        listed = listed_adapter(source)
        if listed is not None:
            total = await listed.count(client, slug)
            if total == 0:
                return Checked(CandidateStatus.EMPTY, 0)
            if matching and source == "workday":
                if await workday.count(client, slug, matching) == 0:
                    return Checked(CandidateStatus.SKIPPED, total)
                return Checked(
                    CandidateStatus.LIVE, total, display_name(workday.parse_slug(slug).tenant)
                )
            return Checked(CandidateStatus.LIVE, total, display_name(slug))

        adapter = board_adapter(source)
        if adapter is None or not _SLUG.match(slug):
            return Checked(CandidateStatus.SKIPPED)
        polled = await poll_board(
            client,
            adapter,
            slug,
            PollState(),
            company_id=candidate.id,
            now=timezone.now(),
            force=True,
        )
        jobs = polled.jobs or []
        if not jobs:
            return Checked(CandidateStatus.EMPTY, 0)
        if not any_board and not any(radar.is_software_title(job.title) for job in jobs):
            return Checked(CandidateStatus.SKIPPED, len(jobs))
        name = await _name(client, source, slug, jobs)
        return Checked(CandidateStatus.LIVE, len(jobs), name, jobs, polled.state)
    except BoardNotFound:
        return Checked(CandidateStatus.MISSING, 0)
    except Exception as error:
        log.debug("asking %s:%s failed: %s", source, slug, error)
        return Checked(CandidateStatus.ERROR)


def _settle(candidate_id: uuid.UUID, status: str, postings: int | None) -> None:
    BoardCandidate.objects.filter(id=candidate_id).update(
        status=status, postings=postings, checked_at=Now(), updated_at=Now()
    )


def _register(candidate_id: uuid.UUID, checked: Checked) -> tuple[bool, int]:
    """Registers a live candidate's company and ingests what its board answered. Returns
    whether the company is new and how many postings came in. Sync."""
    candidate = BoardCandidate.objects.get(id=candidate_id)
    source, slug = candidate.source_type, candidate.ats_slug
    ours = workday.parse_slug(slug).tenant if source == "workday" else slug
    company, created = upsert_company(
        CompanyInput(
            name=checked.name or display_name(ours),
            slug=ours,
            source_type=source,
            ats_slug=slug,
            discovered_via=DiscoveredVia.CRAWL,
            found_in=list(candidate.found_in),
        )
    )
    inserted = 0
    if checked.jobs is not None:
        # First sight of the board: nothing in it is timed (`previous_ok=None`).
        result = ingest_jobs(company.id, checked.jobs, deactivate_missing=True, previous_ok=None)
        inserted = result.inserted
        record_fetch(
            company.id,
            FetchStatus.OK,
            len(checked.jobs),
            poll_state=checked.state.dump() if checked.state else None,
            relevant=any(is_early_career_title(job.title) for job in checked.jobs),
        )
    _settle(candidate_id, CandidateStatus.LIVE, checked.postings)
    return created, inserted


@dataclass(slots=True)
class GrowSummary:
    asked: int = 0
    registered: int = 0
    postings: int = 0
    by_status: Counter[str] = field(default_factory=Counter)


async def grow(
    client: HttpClient,
    *,
    sources: Sequence[str] = DEFAULT_SOURCES,
    limit: int = 500,
    matching: str = "",
    any_board: bool = False,
    retry: bool = False,
    place: str = "",
    progress: Callable[[GrowSummary], None] | None = None,
) -> GrowSummary:
    """Asks up to `limit` candidates' boards and registers the ones that answer with postings,
    a software role among them unless `any_board` (see the module docstring)."""
    await db(link_known)
    candidates = await db(next_candidates, sources, limit, retry=retry, place=place)
    summary = GrowSummary()
    gates: dict[str, asyncio.Semaphore] = {}

    async def one(candidate: BoardCandidate) -> None:
        pace = SOURCE_PACE_S.get(candidate.source_type)
        gate = gates.setdefault(candidate.source_type, asyncio.Semaphore(1 if pace else PER_SOURCE))
        async with gate:
            if pace:
                await client.pace(f"boards:{candidate.source_type}", pace)
            checked = await _check(client, candidate, matching, any_board)
            if checked.status == CandidateStatus.LIVE:
                try:
                    created, inserted = await db(_register, candidate.id, checked)
                except Exception as error:
                    log.warning("registering %s failed: %s", candidate, error)
                    checked = Checked(CandidateStatus.ERROR)
                    await db(_settle, candidate.id, checked.status, None)
                else:
                    summary.registered += created
                    summary.postings += inserted
            else:
                await db(_settle, candidate.id, checked.status, checked.postings)
        summary.asked += 1
        summary.by_status[str(checked.status)] += 1
        if progress is not None:
            progress(summary)

    async with asyncio.TaskGroup() as group:
        for candidate in candidates:
            group.create_task(one(candidate))
    return summary


PUBLIC_PLACES = ("commoncrawl:", "list:")


def seed_rows() -> list[dict[str, object]]:
    """The registry's verified boards as a seed list anyone may use (#28): active board
    companies seen in a public index or a licensed list, by name, with only those places as
    their provenance. Sync."""
    companies = Company.objects.filter(status="active", ats_slug__isnull=False).order_by(
        "name", "slug"
    )
    rows: list[dict[str, object]] = []
    for company in companies:
        places = [place for place in company.found_in if place.startswith(PUBLIC_PLACES)]
        if places:
            rows.append(
                {
                    "name": company.name,
                    "slug": company.slug,
                    "sourceType": company.source_type,
                    "atsSlug": company.ats_slug,
                    "foundIn": places,
                }
            )
    return rows
