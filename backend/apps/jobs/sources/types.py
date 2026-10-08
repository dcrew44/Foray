"""What every source produces, whatever shape it arrived in (design §10.2; `map/worker.md` §13.1).

Adapters normalise; models hold data. There is no source branching outside
`sources/<source>.py` (invariant 3): if scoring or the UI ever needs to know which ATS a job came
from in order to read a field, that field belongs here instead. The three board adapters share
one shape (`BoardAdapter`); JSearch and careers pages are plain functions with the same
`NormalizedJob` output.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Literal

from apps.jobs.schemas import NormalizedLocation

if TYPE_CHECKING:
    from apps.core.http import HttpClient

log = logging.getLogger("foray.jobs")

type JobSourceId = Literal["greenhouse", "lever", "ashby", "jsearch", "web"]
type BoardSource = Literal["greenhouse", "lever", "ashby"]
type RemoteKind = Literal["remote", "hybrid", "onsite", "unknown"]
type SalaryPeriod = Literal["year", "month", "hour"]


@dataclass(slots=True)
class NormalizedSalary:
    min: float | None = None
    max: float | None = None
    currency: str | None = None
    # As the source states it. Never converted between periods.
    period: SalaryPeriod | None = None

    def dump(self) -> dict[str, Any]:
        return {"min": self.min, "max": self.max, "currency": self.currency, "period": self.period}


@dataclass(slots=True)
class NormalizedJob:
    # `source:company_slug:external_id`; careers pages `web:<slug>:<sha1(url)>` (invariant 2).
    id: str
    source: JobSourceId
    external_id: str
    company_slug: str
    company_name: str

    title: str
    locations: list[str]
    location_norm: NormalizedLocation | None
    remote: RemoteKind
    employment_type: str | None

    salary: NormalizedSalary

    # Exactly as fetched, never rewritten (invariant 4).
    description_raw: str | None
    # HTML stripped and known boilerplate removed. Derived, disposable.
    description_text: str | None

    url: str
    apply_url: str | None
    posted_at: datetime | None

    # The source's own payload, so a parser fix can be replayed.
    raw: Any
    content_hash: str
    dedupe_key: str

    def dump(self) -> dict[str, Any]:
        """The TypeScript `NormalizedJob` as JSON (camelCase, its key order), for scripts."""
        return {
            "id": self.id,
            "source": self.source,
            "externalId": self.external_id,
            "companySlug": self.company_slug,
            "companyName": self.company_name,
            "title": self.title,
            "locations": self.locations,
            "locationNorm": self.location_norm.dump() if self.location_norm else None,
            "remote": self.remote,
            "employmentType": self.employment_type,
            "salary": self.salary.dump(),
            "descriptionRaw": self.description_raw,
            "descriptionText": self.description_text,
            "url": self.url,
            "applyUrl": self.apply_url,
            "postedAt": js_iso(self.posted_at),
            "raw": self.raw,
            "contentHash": self.content_hash,
            "dedupeKey": self.dedupe_key,
        }


class BoardNotFound(Exception):
    """A board that does not exist, as opposed to one that is empty. Raised on a 404 only."""

    def __init__(self, source: str, slug: str) -> None:
        super().__init__(f'No {source} board for slug "{slug}"')
        self.source = source
        self.slug = slug


@dataclass(frozen=True, slots=True)
class BoardAdapter:
    """A source that reads one company's board: every current posting, or `BoardNotFound`."""

    source: BoardSource
    fetch_board: Callable[[HttpClient, str], Awaitable[list[NormalizedJob]]] = field(repr=False)


# --- dates ---------------------------------------------------------------------------------------

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_ISO_Z = re.compile(r"[zZ]\Z")


def js_date(value: Any) -> datetime | None:
    """`new Date(value)` for a posting date: an ISO string (with or without an offset) or epoch
    milliseconds, truncated to milliseconds as a JS Date is. An unparseable date is None and a
    debug log, where TypeScript threw at insert and failed the whole board (design §10.2)."""
    try:
        if isinstance(value, bool):
            raise ValueError(value)
        if isinstance(value, int | float):
            return _EPOCH + timedelta(milliseconds=int(value))
        if isinstance(value, str):
            text = value.strip()
            try:
                parsed = datetime.fromisoformat(_ISO_Z.sub("+00:00", text))
            except ValueError:
                parsed = parsedate_to_datetime(text)  # RFC 2822, which JS also accepts
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            parsed = parsed.astimezone(UTC)
            return parsed.replace(microsecond=parsed.microsecond // 1000 * 1000)
    except (ValueError, TypeError, OverflowError) as error:
        log.debug("unparseable posting date %r: %s", value, error)
        return None
    log.debug("unparseable posting date %r", value)
    return None


def js_iso(value: datetime | None) -> str | None:
    """`date.toISOString()`."""
    if value is None:
        return None
    value = value.astimezone(UTC)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"
