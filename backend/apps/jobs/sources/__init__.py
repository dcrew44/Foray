"""Job sources (design §10.2). Board adapters by the source id stored on a company.

Invariant 3: nothing outside `sources/<source>.py` branches on a source's quirks. The board
adapters share one shape; JSearch (`jsearch.search_jsearch`) and careers pages
(`careers_page.fetch_careers_page`) are functions with the same `NormalizedJob` output.
"""

from __future__ import annotations

from apps.jobs.sources import ashby, gem, greenhouse, lever, recruitee, workable
from apps.jobs.sources.types import BoardAdapter, BoardSource

BOARD_ADAPTERS: dict[str, BoardAdapter] = {
    "greenhouse": BoardAdapter("greenhouse", greenhouse.fetch_board),
    "lever": BoardAdapter("lever", lever.fetch_board),
    "ashby": BoardAdapter("ashby", ashby.fetch_board),
    "workable": BoardAdapter("workable", workable.fetch_board),
    "recruitee": BoardAdapter("recruitee", recruitee.fetch_board),
    "gem": BoardAdapter("gem", gem.fetch_board),
}

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
# after about a thousand misses, so a Workable board is read only from a link that names it.
GUESS_SOURCES: tuple[BoardSource, ...] = tuple(s for s in BOARD_SOURCES if s != "workable")


def board_adapter(source: str) -> BoardAdapter | None:
    return BOARD_ADAPTERS.get(source)
