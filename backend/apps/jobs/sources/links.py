"""Recognising a board in a URL or a page, for the sources whose board id is not one slug (#57).

Greenhouse, Lever, Ashby and the rest name a board by a slug that is also a fair name for the
company (`discovery.ATS_PATTERNS`). These do not: a Workday board is a host and a site, a
SmartRecruiters one an identifier with its capitals, an iCIMS portal a subdomain with a prefix.
So each link found here carries the board's slug exactly as its adapter wants it, and a `label`
that is what a person would call the company (`nvidia`, `ServiceNow`, `gdms`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from apps.jobs.sources import icims, smartrecruiters, workday
from apps.jobs.sources.types import BoardSource

__all__ = ["BoardLink", "board_links", "site_label"]

_PORTAL_PREFIX = re.compile(r"^(?:[a-z0-9]+-)*(?:careers?|jobs)-")
_NOT_PORTALS = frozenset({"www", "api", "developer", "community", "careers", "login"})


@dataclass(frozen=True, slots=True)
class BoardLink:
    source: BoardSource
    slug: str
    label: str


def site_label(host: str) -> str:
    """What to call the employer behind a career site's host: `careers.pnnl.gov` is `pnnl`."""
    labels = [label for label in host.lower().split(".") if label]
    return labels[-2] if len(labels) >= 2 else host.lower()


def board_links(text: str) -> list[BoardLink]:
    """Workday, SmartRecruiters and iCIMS boards linked in a page or a pasted URL, in that
    order, once each."""
    found: list[BoardLink] = []

    def add(link: BoardLink) -> None:
        if link not in found:
            found.append(link)

    for board in workday.boards_in(text):
        add(BoardLink("workday", board.slug, board.tenant))
    for match in smartrecruiters.LINK.finditer(text):
        add(BoardLink("smartrecruiters", match.group(1), match.group(1)))
    for match in icims.LINK.finditer(text):
        subdomain = match.group(1).lower()
        if subdomain not in _NOT_PORTALS:
            add(BoardLink("icims", subdomain, _PORTAL_PREFIX.sub("", subdomain) or subdomain))
    return found
