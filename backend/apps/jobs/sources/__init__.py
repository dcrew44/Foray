"""Job sources (design §10.2). Board adapters by the source id stored on a company.

Invariant 3: nothing outside `sources/<source>.py` branches on a source's quirks. The board
adapters share one shape; JSearch (`jsearch.search_jsearch`) and careers pages
(`careers_page.fetch_careers_page`) are functions with the same `NormalizedJob` output.
"""

from __future__ import annotations

from types import ModuleType

from apps.jobs.sources import ashby, gem, greenhouse, lever, recruitee, workable
from apps.jobs.sources.types import BoardAdapter, BoardSource


def _adapter(source: BoardSource, module: ModuleType, *, probe: bool = False) -> BoardAdapter:
    return BoardAdapter(
        source,
        module.fetch_board,
        module.board_url,
        module.parse_board,
        module.probe_url if probe else None,
    )


BOARD_ADAPTERS: dict[str, BoardAdapter] = {
    "greenhouse": _adapter("greenhouse", greenhouse, probe=True),
    "lever": _adapter("lever", lever),
    "ashby": _adapter("ashby", ashby),
    "workable": _adapter("workable", workable),
    "recruitee": _adapter("recruitee", recruitee),
    "gem": _adapter("gem", gem),
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
