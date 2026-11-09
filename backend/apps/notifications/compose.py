"""What a notification says (#59). Pure: no database, no clock of its own.

A match's notification is its company and title, the verdict of the deep review, how long the
posting has been up, and a link to the match. The company and the title are third-party text:
they go to a phone, not to a model, so they are only put on one line and cut to a sane length.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings

from apps.matching.models import JobMatch
from apps.matching.schemas import DeepEval

__all__ = [
    "MAX_SINGLE",
    "Message",
    "age_phrase",
    "match_message",
    "one_line",
    "summary_message",
    "trial_message",
]

# A watch that announces more than this sends the best ones each on their own and one line
# for the rest, so a busy morning is six buzzes at most.
MAX_SINGLE = 5

COMPANY_MAX = 60
TITLE_MAX = 140
MATCH_TAG = "briefcase"  # an ntfy emoji shortcode, shown before the title
TEST_TAG = "white_check_mark"

# The words the match page uses (`src/lib/format.ts` RECOMMENDATION_LABEL).
VERDICT = {"strong": "Strong fit", "apply": "Worth applying"}

_BREAKS = re.compile(r"[\s\x00-\x1f\x7f-\x9f]+")  # \s takes the Unicode line breaks too


@dataclass(frozen=True, slots=True)
class Message:
    title: str
    body: str
    click: str  # where a tap on the notification goes
    tags: tuple[str, ...] = ()


def one_line(text: str, limit: int) -> str:
    """Third-party text on one line: runs of whitespace and control characters become one
    space, and anything past `limit` characters is cut with an ellipsis."""
    flat = _BREAKS.sub(" ", text).strip()
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _count(n: int, unit: str) -> str:
    return f"{n} {unit}" if n == 1 else f"{n} {unit}s"


def age_phrase(age: timedelta) -> str:
    """`40 minutes ago`, `3 hours ago`, `2 days ago`; under a minute (or a date a little in the
    future, as a board's clock may run ahead) is `just now`."""
    minutes = int(age.total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{_count(minutes, 'minute')} ago"
    hours = minutes // 60
    if hours < 48:
        return f"{_count(hours, 'hour')} ago"
    return f"{_count(hours // 24, 'day')} ago"


def match_url(match_id: object) -> str:
    return f"{settings.PUBLIC_ORIGIN}/matches/{match_id}"


def match_message(match: JobMatch, now: datetime) -> Message:
    """One match (its `job` loaded): `Acme: Software Engineer, Payments`, then `Strong fit.
    Posted 40 minutes ago.` A careers-page posting carries no date, so it says when Foray
    first saw it instead."""
    job = match.job
    company = one_line(job.company_name, COMPANY_MAX)
    title = one_line(job.title, TITLE_MAX)
    evaluation = DeepEval.read(match.deep_eval)
    verdict = VERDICT.get(evaluation.recommendation) if evaluation is not None else None
    if job.posted_at is not None:
        age = f"Posted {age_phrase(now - job.posted_at)}."
    else:
        age = f"First seen {age_phrase(now - job.first_seen_at)}."
    return Message(
        title=f"{company}: {title}" if company else title,
        body=f"{verdict}. {age}" if verdict else age,
        click=match_url(match.id),
        tags=(MATCH_TAG,),
    )


def summary_message(more: int) -> Message:
    """The one line for everything past `MAX_SINGLE`, linking to the Matches page."""
    return Message(
        title=f"And {more} more new {'match' if more == 1 else 'matches'}",
        body="Open Foray to see them all.",
        click=f"{settings.PUBLIC_ORIGIN}/matches",
        tags=(MATCH_TAG,),
    )


def trial_message() -> Message:
    """What Settings' "Send a test notification" sends."""
    return Message(
        title="Foray can reach your phone",
        body="New matches will arrive here, outside your quiet hours.",
        click=f"{settings.PUBLIC_ORIGIN}/settings",
        tags=(TEST_TAG,),
    )
