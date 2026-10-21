"""Does this slug have a board, and does it have jobs on it? (design §10.5; `map/worker.md` §18.2)

A company is only ever registered after a live call returns postings: that keeps discovery's
guesses from filling the registry with plausible nonsense. The outcomes are deliberately
distinct: a 404 means the slug is wrong, an empty board means the slug is right but nothing is
open, and an error means we do not know yet and should not conclude anything.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from apps.core.http import HttpClient
from apps.jobs.sources import BOARD_ADAPTERS, GUESS_SOURCES
from apps.jobs.sources.js import js_trim
from apps.jobs.sources.types import BoardNotFound, BoardSource

type VerifyStatus = Literal["ok", "empty", "not-found", "error"]


@dataclass(frozen=True, slots=True)
class VerifyOutcome:
    status: VerifyStatus
    source: BoardSource
    slug: str
    job_count: int | None = None
    error: str | None = None


async def verify_board(client: HttpClient, source: BoardSource, slug: str) -> VerifyOutcome:
    try:
        jobs = await BOARD_ADAPTERS[source].fetch_board(client, slug)
    except BoardNotFound:
        return VerifyOutcome("not-found", source, slug)
    except Exception as error:  # an error is "we do not know yet", never a conclusion
        return VerifyOutcome("error", source, slug, error=str(error) or type(error).__name__)
    if jobs:
        return VerifyOutcome("ok", source, slug, job_count=len(jobs))
    return VerifyOutcome("empty", source, slug)


async def find_board(
    client: HttpClient, slug: str, sources: Sequence[BoardSource] = GUESS_SOURCES
) -> VerifyOutcome | None:
    """Tries one slug against every guessable board provider, in parallel. A board with jobs
    beats an empty one (real but useless right now); the first in source order wins."""
    outcomes = await asyncio.gather(*(verify_board(client, source, slug) for source in sources))
    return next((o for o in outcomes if o.status == "ok"), None) or next(
        (o for o in outcomes if o.status == "empty"), None
    )


_APOSTROPHES = re.compile("['\u2019]")
_LEGAL_SUFFIX = re.compile(r"\b(?:inc|llc|ltd|corp|corporation|co|gmbh|plc)\.?\b", re.ASCII)
_WORD_SPLIT = re.compile(r"[^a-z0-9]+")


def slug_candidates(name: str) -> list[str]:
    """Plausible ATS slugs for a company name, in order: the first that verifies is taken.
    Boards are overwhelmingly the name lowercased with punctuation removed."""
    base = js_trim(name).lower()
    base = _APOSTROPHES.sub("", base).replace("&", "and")
    base = js_trim(_LEGAL_SUFFIX.sub(" ", base))
    words = [word for word in _WORD_SPLIT.split(base) if word]
    if not words:
        return []
    joined = "".join(words)
    candidates = [
        joined,
        "-".join(words),
        words[0],
        f"{joined}inc",
        f"{joined}hq",
        f"{joined}jobs",
        f"{joined}careers",
    ]
    return [slug for slug in dict.fromkeys(candidates) if len(slug) >= 2]
