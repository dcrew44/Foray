"""What a listed board still has to read, kept between passes (#84).

A board whose listing has no descriptions (`sources.types.ListedAdapter`, #57) costs a request
per posting, so a big one is read over several passes (`refresh._refresh_listed`). Listing it
again on each of them is the expensive part: Workday shows twenty postings a page, so a
2,000-posting site is a hundred requests to learn what the pass before already knew. So the
pass that lists a board writes down what it could not read yet, and the passes after it go on
from that with no probe and no listing, until nothing is left.

What is written down is only each posting's id and listing version, which is all any listed
adapter's `read_posting` needs. A posting that has closed since the listing answers "gone" when
it is read and is dropped; one that opened since is found when the board is next listed, which
is the pass after the backlog ends if the board's fingerprint moved meanwhile.

A backlog older than `MAX_AGE` is not gone on with: the board is listed afresh. That bounds a
posting that never reads (it stays at the back of the queue) to one listing a day, where it
used to cost one every pass.

Sync, like every ORM function; async callers use `await db(...)`.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from apps.jobs.models import BoardBacklog
from apps.jobs.sources.types import ListedPosting

__all__ = ["MAX_AGE", "Backlog", "drop_backlog", "read_backlog", "save_backlog"]

MAX_AGE = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class Backlog:
    postings: list[ListedPosting]
    fingerprint: str
    listed_at: datetime


def _pairs(stored: object) -> list[ListedPosting]:
    """The stored `[[external_id, version], ...]`, leaving out anything that is not a pair of
    strings (a row nobody here wrote)."""
    if not isinstance(stored, list):
        return []
    return [
        ListedPosting(pair[0], pair[1], None)
        for pair in stored
        if isinstance(pair, list)
        and len(pair) == 2
        and isinstance(pair[0], str)
        and isinstance(pair[1], str)
    ]


def read_backlog(company_id: uuid.UUID, now: datetime) -> Backlog | None:
    """What the board's last listing left to read, or None when there is nothing to go on
    with: no backlog, an empty one, or one made longer than `MAX_AGE` ago."""
    row = BoardBacklog.objects.filter(company_id=company_id).first()
    if row is None or now - row.listed_at > MAX_AGE:
        return None
    postings = _pairs(row.postings)
    if not postings:
        return None
    return Backlog(postings, row.fingerprint, row.listed_at)


def save_backlog(
    company_id: uuid.UUID,
    postings: Sequence[ListedPosting],
    fingerprint: str,
    listed_at: datetime,
) -> None:
    """Writes down what is left of a listing made at `listed_at`, replacing what was there."""
    BoardBacklog.objects.update_or_create(
        company_id=company_id,
        defaults={
            "postings": [[posting.external_id, posting.version] for posting in postings],
            "fingerprint": fingerprint,
            "listed_at": listed_at,
        },
    )


def drop_backlog(company_id: uuid.UUID) -> None:
    BoardBacklog.objects.filter(company_id=company_id).delete()
