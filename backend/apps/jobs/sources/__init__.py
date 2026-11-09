"""Job sources (design §10.2). Board adapters by the source id stored on a company.

Invariant 3: nothing outside `sources/<source>.py` branches on a source's quirks. Adapters come
in two shapes: a `BoardAdapter` reads a whole board in one go, and a `ListedAdapter` (Workday,
SmartRecruiters, iCIMS portals: a request per posting) lists it and reads only what is new.
JSearch (`jsearch.search_jsearch`) and careers pages (`careers_page.fetch_careers_page`) are
functions with the same `NormalizedJob` output.
"""

from __future__ import annotations

from types import ModuleType

from apps.jobs.sources import (
    ashby,
    gem,
    greenhouse,
    icims,
    jibe,
    lever,
    recruitee,
    smartrecruiters,
    workable,
    workday,
)
from apps.jobs.sources.types import BoardAdapter, BoardSource, ListedAdapter


def _adapter(source: BoardSource, module: ModuleType, *, probe: bool = False) -> BoardAdapter:
    return BoardAdapter(
        source,
        module.fetch_board,
        module.board_url,
        module.parse_board,
        module.probe_url if probe else None,
    )


def _listed(source: BoardSource, module: ModuleType) -> ListedAdapter:
    return ListedAdapter(source, module.count, module.probe, module.list_board, module.read_posting)


# Sources whose listing carries every posting whole: a board is one read.
BOARD_ADAPTERS: dict[str, BoardAdapter] = {
    "greenhouse": _adapter("greenhouse", greenhouse, probe=True),
    "lever": _adapter("lever", lever),
    "ashby": _adapter("ashby", ashby),
    "workable": _adapter("workable", workable),
    "recruitee": _adapter("recruitee", recruitee),
    "gem": _adapter("gem", gem),
    # Page one is the probe; the board is all its pages.
    "jibe": BoardAdapter(
        "jibe", jibe.fetch_board, jibe.board_url, jibe.parse_board, jibe.board_url, jibe.count
    ),
}

# Sources whose listing has no descriptions: read incrementally (#57).
LISTED_ADAPTERS: dict[str, ListedAdapter] = {
    "workday": _listed("workday", workday),
    "smartrecruiters": _listed("smartrecruiters", smartrecruiters),
    "icims": _listed("icims", icims),
}

# Every source a company's board can be on.
SOURCES: tuple[BoardSource, ...] = (
    "greenhouse",
    "lever",
    "ashby",
    "workable",
    "recruitee",
    "gem",
    "jibe",
    "workday",
    "smartrecruiters",
    "icims",
)

# Source order is preference order when one slug verifies on several boards (`find_board`).
BOARD_SOURCES: tuple[BoardSource, ...] = (
    "greenhouse",
    "lever",
    "ashby",
    "workable",
    "recruitee",
    "gem",
)

# The boards a slug may be guessed against. Workable's widget API bans an address for a day
# after about a thousand misses, so a Workable board is read only from a link that names it;
# and a Workday site, a SmartRecruiters identifier or an iCIMS host cannot be guessed from a
# company's name at all.
GUESS_SOURCES: tuple[BoardSource, ...] = tuple(s for s in BOARD_SOURCES if s != "workable")


def board_adapter(source: str) -> BoardAdapter | None:
    return BOARD_ADAPTERS.get(source)


def listed_adapter(source: str) -> ListedAdapter | None:
    return LISTED_ADAPTERS.get(source)
