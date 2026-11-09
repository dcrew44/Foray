"""Asking a board whether it changed before reading it (#55).

A board polled every fifteen minutes is nearly always as it was. So a poll first asks: a
conditional request for the board (or for the adapter's cheaper `probe_url`), carrying the
validators its last answer came with. Three things can say "unchanged", cheapest first:

1. **A 304.** Greenhouse and Lever answer an `ETag` and honour `If-None-Match`; nothing is sent
   back.
2. **The same bytes.** Ashby's `Last-Modified` is its CDN's cache time, renewed every minute, so
   it never earns a 304; but an unchanged board is byte for byte the same, and a digest of the
   body says so without parsing it.
3. Otherwise the board is read: parsed from the answer when the answer is the board, or fetched
   whole when the answer was only the probe.

`PollState` is what a company remembers between polls (`companies.poll_state`). It is trusted
only for the URL it was made for and the `VERSION` that wrote it, and only for a day: once a
day (staggered by company, so two thousand boards do not all fall due in one pass) the board is
read whole whatever it says, which bounds how long a wrong "unchanged" could last. Bump
`VERSION` when a parser changes what it makes of the same bytes.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from apps.core.http import HttpClient, HttpError, Validators
from apps.jobs.sources.types import BoardAdapter, BoardNotFound, NormalizedJob, js_iso

__all__ = ["FULL_READ_EVERY", "VERSION", "PollState", "Polled", "full_read_due", "poll_board"]

VERSION = 1
FULL_READ_EVERY = timedelta(hours=24)
# Each company's daily full read is put off by up to this much, by its id.
FULL_READ_SPREAD = timedelta(hours=6)


@dataclass(frozen=True, slots=True)
class PollState:
    url: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    digest: str | None = None  # sha1 of the body the validators came with
    read_at: datetime | None = None  # when the board was last read whole
    # A listed board (`ListedAdapter`) still has postings to read: the next pass goes on with
    # them whatever the probe says.
    pending: bool = False
    version: int = VERSION

    @classmethod
    def read(cls, stored: Any) -> PollState:
        """A stored state, or the empty one (which makes the next poll a full read) when it is
        missing, malformed or another version's."""
        if not isinstance(stored, dict) or stored.get("version") != VERSION:
            return cls()

        def text(key: str) -> str | None:
            value = stored.get(key)
            return value if isinstance(value, str) and value else None

        # No `readAt` is a listed board still being caught up; one that does not parse is not
        # a state anyone wrote.
        read_at: datetime | None = None
        stamped = text("readAt")
        if stamped is not None:
            try:
                read_at = datetime.fromisoformat(stamped)
            except ValueError:
                return cls()
        return cls(
            url=text("url"),
            etag=text("etag"),
            last_modified=text("lastModified"),
            digest=text("digest"),
            read_at=read_at,
            pending=stored.get("pending") is True,
        )

    def dump(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "url": self.url,
            "etag": self.etag,
            "lastModified": self.last_modified,
            "digest": self.digest,
            "readAt": js_iso(self.read_at),
            "pending": self.pending,
        }

    def validators(self, url: str) -> Validators:
        if self.url != url:
            return Validators()
        return Validators(etag=self.etag, last_modified=self.last_modified)


@dataclass(frozen=True, slots=True)
class Polled:
    """`jobs is None`: the board is as it was. `state` is what to remember either way."""

    jobs: list[NormalizedJob] | None
    state: PollState


def full_read_due(state: PollState, company_id: uuid.UUID, now: datetime) -> bool:
    """Whether the daily whole read has come round (each company's is put off by its id)."""
    if state.read_at is None:
        return True
    spread = FULL_READ_SPREAD * (company_id.int % 1000 / 1000)
    return now - state.read_at >= FULL_READ_EVERY + spread


async def poll_board(
    client: HttpClient,
    adapter: BoardAdapter,
    slug: str,
    state: PollState,
    *,
    company_id: uuid.UUID,
    now: datetime,
    force: bool = False,
) -> Polled:
    """One poll of one board. Raises `BoardNotFound` on a 404 and whatever else the fetch
    raises. `force` reads the board whole (a person pressed "Fetch now")."""
    url = (adapter.probe_url or adapter.board_url)(slug)
    full = force or full_read_due(state, company_id, now)
    try:
        answer = await client.fetch_conditional(
            url, Validators() if full else state.validators(url)
        )
    except HttpError as error:
        if error.status == 404:
            raise BoardNotFound(adapter.source, slug) from error
        raise

    if answer.text is None:
        return Polled(None, state)
    digest = hashlib.sha1(answer.text.encode(), usedforsecurity=False).hexdigest()
    seen = PollState(
        url=url,
        etag=answer.validators.etag,
        last_modified=answer.validators.last_modified,
        digest=digest,
        read_at=state.read_at,
    )
    if not full and state.url == url and state.digest == digest:
        return Polled(None, seen)

    if adapter.probe_url is None:
        jobs = adapter.parse_board(slug, answer.text)
    else:
        jobs = await adapter.fetch_board(client, slug)
    return Polled(
        jobs,
        PollState(
            url=url,
            etag=seen.etag,
            last_modified=seen.last_modified,
            digest=digest,
            read_at=now,
        ),
    )
