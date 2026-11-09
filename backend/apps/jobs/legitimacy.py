"""Signs that a posting may not be worth an application (#7): it has closed, an aggregator's
apply link leads somewhere that is not the employer, the role keeps being posted again, or it
has been open a long time.

Deterministic, like a posting's facts (`facts.py`): rules over what the mirror already holds,
with no model and no fetch. Unlike the facts, nothing is stored. A signal is true of today (a
posting is open for 61 days only on its 61st), so it is worked out when the match page is read:

- each rule is a pure function of plain values that returns a `Signal` or None;
- `read_context` is the one query: what a rule needs that the posting's own row does not hold;
- `signals` runs the rules over a `Job` in the order the page shows them.

A new signal is a kind in `SignalKind` (the page's schema and the frontend's types follow from
it: `pnpm api:types`), a rule here, and its place in `signals`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ada_url import URL
from django.db import connection

from apps.jobs.discovery import ATS_PATTERNS, UNSUPPORTED_ATS, normalize_domain
from apps.jobs.models import Job, JobSource
from apps.jobs.sources import icims, smartrecruiters, workday
from apps.jobs.sources.dedupe import normalize_company
from apps.jobs.sources.links import site_label

__all__ = [
    "LONG_OPEN_DAYS",
    "REPOST_MIN",
    "REPOST_WINDOW_DAYS",
    "Context",
    "Signal",
    "SignalKind",
    "aggregator_link",
    "closed",
    "is_board_link",
    "is_employer_host",
    "long_open",
    "read_context",
    "reposted",
    "signals",
    "stated_domain",
]

# Also the order `signals` answers in. A plain alias, because the page's schema uses it.
SignalKind = Literal["closed", "aggregator_link", "reposted", "long_open"]

# The thresholds #7 settled on. A role is "reposted" from its third posting in about half a
# year, and "open a long time" once it has been up for more than sixty days.
REPOST_MIN = 3
REPOST_WINDOW_DAYS = 180
LONG_OPEN_DAYS = 60
# Sources that search other people's listings instead of reading an employer's own board.
AGGREGATORS: frozenset[str] = frozenset({JobSource.JSEARCH})


@dataclass(frozen=True, slots=True)
class Signal:
    kind: SignalKind
    label: str  # a few words
    detail: str  # one sentence, written for the user


@dataclass(frozen=True, slots=True)
class Context:
    """What the rules need that a posting's own row does not hold."""

    # This posting and the closed ones of the same role first seen in the window (itself among
    # them, when it is that recent).
    postings: int
    since: datetime | None  # when the earliest of them was posted, or first seen if undated
    domain: str | None  # the registered company's own domain, when the posting has a company
    now: datetime  # the database's present, which every age here is measured against


# --- the rules -----------------------------------------------------------------------------------


def closed(
    *, is_active: bool, last_seen_at: datetime, now: datetime, zone: tzinfo
) -> Signal | None:
    """The posting is no longer on the employer's board (`ingest.py` keeps it, inactive)."""
    if is_active:
        return None
    return Signal(
        "closed", "Closed", f"Last seen on the employer's board on {_day(last_seen_at, now, zone)}."
    )


def aggregator_link(
    *, source: str, apply_url: str | None, employer: str, domain: str | None
) -> Signal | None:
    """An aggregator's posting whose apply link leads neither to the employer's own site nor
    to a job board Foray knows. `domain` is the employer's, when anything says what it is."""
    if source not in AGGREGATORS or not apply_url:
        return None
    address = _address(apply_url)
    if address is None:
        return None
    host, _path = address
    if is_employer_host(host, employer=employer, domain=domain) or is_board_link(apply_url):
        return None
    return Signal(
        "aggregator_link",
        "Apply link goes elsewhere",
        f"The apply link goes to {host.removeprefix('www.')}, not the employer's site.",
    )


def reposted(
    postings: int, since: datetime | None, *, now: datetime, zone: tzinfo
) -> Signal | None:
    """The same role at the same company, posted again and again."""
    if postings < REPOST_MIN or since is None:
        return None
    return Signal(
        "reposted", "Reposted", f"Posted {postings:,} times since {_month(since, now, zone)}."
    )


def long_open(
    *, is_active: bool, posted_at: datetime | None, first_seen_at: datetime, now: datetime
) -> Signal | None:
    """Still open long after it was posted, or after it was first seen when the source gives no
    date. Whole days: a posting is open "for 60 days" until the 61st is complete."""
    days = (now - (posted_at or first_seen_at)).days
    if not is_active or days <= LONG_OPEN_DAYS:
        return None
    return Signal("long_open", "Open a long time", f"Open for {days:,} days.")


def signals(job: Job, context: Context, *, time_zone: str | None = None) -> list[Signal]:
    """A posting's signals, the one that decides whether applying is possible first. An
    ordinary posting has none. Dates are said in `time_zone` (an IANA name; UTC without one)."""
    zone = _zone(time_zone)
    found = (
        closed(is_active=job.is_active, last_seen_at=job.last_seen_at, now=context.now, zone=zone),
        aggregator_link(
            source=job.source,
            apply_url=job.apply_url,
            employer=job.company_name,
            domain=context.domain or stated_domain(job.raw),
        ),
        reposted(context.postings, context.since, now=context.now, zone=zone),
        long_open(
            is_active=job.is_active,
            posted_at=job.posted_at,
            first_seen_at=job.first_seen_at,
            now=context.now,
        ),
    )
    return [signal for signal in found if signal is not None]


# --- the query -----------------------------------------------------------------------------------

# A repost is this posting and the earlier ones of the same role that have closed. The ingest
# dedupe key (`sources.dedupe.dedupe_key`; `jobs_dedupe_key_idx`) finds the company and the
# place, but it normalises titles loosely: every "Member of Technical Staff (…)" at one company
# shares a key, and forty different roles are not one role posted forty times. So the title
# must also be the same, letter for letter. Another posting of the title that is open now is
# another opening, not a repost. That key is also built to make an aggregator's copy collapse
# with the board's own, so only postings from this posting's source are counted. A posting
# found again keeps its external id.
_CONTEXT = (
    "SELECT count(DISTINCT external_id), min(COALESCE(posted_at, first_seen_at)), "
    "(SELECT domain FROM companies WHERE id = %s), statement_timestamp() "
    "FROM jobs WHERE dedupe_key = %s AND source = %s "
    "AND lower(title) = lower(%s) AND (external_id = %s OR NOT is_active) "
    f"AND first_seen_at >= statement_timestamp() - interval '{REPOST_WINDOW_DAYS} days'"
)


def read_context(job: Job) -> Context:
    """How often the posting's role has been posted lately and since when, its company's
    domain, and the database's present. Sync; one query."""
    with connection.cursor() as cursor:
        cursor.execute(
            _CONTEXT, [job.company_id, job.dedupe_key, job.source, job.title, job.external_id]
        )
        row = cursor.fetchone()
    assert row is not None  # an aggregate without GROUP BY always answers one row
    postings, since, domain, now = row
    return Context(postings=int(postings), since=since, domain=domain or None, now=now)


# --- where a link leads --------------------------------------------------------------------------

# The boards Foray reads (`discovery.ATS_PATTERNS`, and the three `sources/links.py` finds) and
# the ones it only recognises.
_BOARD_PATTERNS: tuple[re.Pattern[str], ...] = (
    *(pattern for _source, pattern in ATS_PATTERNS),
    workday.LINK,
    smartrecruiters.LINK,
    icims.LINK,
    UNSUPPORTED_ATS,
)
# A job link need not name its board (`apply.workable.com/j/<code>`), which most patterns want.
_ANY_BOARD = "board"


def _address(url: str) -> tuple[str, str] | None:
    """An http(s) link's host (lowercase) and path, or None when it is not one."""
    try:
        parsed = URL(url.strip())
    except ValueError:
        return None
    host = parsed.hostname.rstrip(".")
    if parsed.protocol not in ("https:", "http:") or not host:
        return None
    return host, parsed.pathname


def _within(host: str, domain: str) -> bool:
    """Whether `host` is `domain` or one of its subdomains."""
    return host == domain or host.endswith(f".{domain}")


def is_board_link(url: str) -> bool:
    """Whether a link goes to a job board Foray knows. The patterns were written to find a
    board in a page, so they are not anchored. Here a match has to start in the link's host and
    name that host or a parent of it, which a board's address in some other site's path, query
    or subdomain does not."""
    address = _address(url)
    if address is None:
        return False
    host, path = address
    for text in (f"//{host}{path}", f"//{host}/{_ANY_BOARD}"):
        for pattern in _BOARD_PATTERNS:
            for match in pattern.finditer(text):
                named = match.group(0).removeprefix("//").split("/", 1)[0].lower()
                if match.start() < len(host) + 2 and _within(host, named):
                    return True
    return False


def is_employer_host(host: str, *, employer: str, domain: str | None = None) -> bool:
    """Whether a host is the employer's own: its known domain or a subdomain of it, or a domain
    named exactly for the employer (`amazon.jobs`, or `careers.acme.com` when nothing says
    what Acme's domain is). A longer name that only starts with the employer's
    (`acme-careers.com`) is not taken on trust."""
    known = normalize_domain(domain) if domain else None
    if known and _within(host, known):
        return True
    name = normalize_company(employer)
    return bool(name) and site_label(host) in (name.replace(" ", ""), name.replace(" ", "-"))


def stated_domain(raw: Any) -> str | None:
    """The employer's website as an aggregator's payload states it (JSearch's
    `employer_website`): the only word on the employer's domain for an aggregator's posting,
    which is ingested with no company (`matching.search.run_aggregator`)."""
    website = raw.get("employer_website") if isinstance(raw, dict) else None
    return normalize_domain(website) if isinstance(website, str) else None


# --- wording -------------------------------------------------------------------------------------


def _zone(time_zone: str | None) -> tzinfo:
    try:
        return ZoneInfo(time_zone or "UTC")
    except ZoneInfoNotFoundError, ValueError:
        return ZoneInfo("UTC")


def _day(when: datetime, now: datetime, zone: tzinfo) -> str:
    """`November 3`, with the year when it is not this one."""
    local = when.astimezone(zone)
    year = "" if local.year == now.astimezone(zone).year else f", {local.year}"
    return f"{local:%B} {local.day}{year}"


def _month(when: datetime, now: datetime, zone: tzinfo) -> str:
    """`May`, with the year when it is not this one."""
    local = when.astimezone(zone)
    return f"{local:%B}" if local.year == now.astimezone(zone).year else f"{local:%B} {local.year}"
