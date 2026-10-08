"""Job sources (design §10.2). Board adapters by the source id stored on a company.

Invariant 3: nothing outside `sources/<source>.py` branches on a source's quirks. The three
board adapters share one shape; JSearch (`jsearch.search_jsearch`) and careers pages
(`careers_page.fetch_careers_page`) are functions with the same `NormalizedJob` output.
"""

from __future__ import annotations

from apps.jobs.sources import ashby, greenhouse, lever
from apps.jobs.sources.types import BoardAdapter, BoardSource

BOARD_ADAPTERS: dict[str, BoardAdapter] = {
    "greenhouse": BoardAdapter("greenhouse", greenhouse.fetch_board),
    "lever": BoardAdapter("lever", lever.fetch_board),
    "ashby": BoardAdapter("ashby", ashby.fetch_board),
}

BOARD_SOURCES: tuple[BoardSource, ...] = ("greenhouse", "lever", "ashby")


def board_adapter(source: str) -> BoardAdapter | None:
    return BOARD_ADAPTERS.get(source)
