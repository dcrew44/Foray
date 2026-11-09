"""Reading board URLs out of Common Crawl's index (#58).

Common Crawl publishes, for every crawl, an index of the URLs it fetched, sorted by SURT key
(`io,greenhouse,boards)/stripe`): about 300 gzip files of 3,000-line blocks, and one
`cluster.idx` that names the first key of every block. The query server in front of them is
slow and rate-limited; the files themselves sit on a CDN and answer byte ranges. So this reads
the files: it bisects `cluster.idx` by range for a key prefix (a few dozen 8 KB requests, never
the 130 MB file), then fetches only the blocks that can hold the prefix, which for every URL
under `greenhouse.io` is a handful.

Only URLs come out of here, and only for the board hosts in `PREFIXES`; `candidates` turns them
into board ids. Captures that answered 404 or 410 are left out.
"""

from __future__ import annotations

import gzip
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

from apps.core.http import HttpClient

__all__ = ["PREFIXES", "Block", "blocks_for", "latest_crawls", "urls_under"]

log = logging.getLogger("foray.jobs")

DATA = "https://data.commoncrawl.org/cc-index/collections/{crawl}/indexes/{name}"
COLLECTIONS = "https://index.commoncrawl.org/collinfo.json"
# The SURT prefix under which each source's boards are filed.
PREFIXES: dict[str, tuple[str, ...]] = {
    "greenhouse": ("io,greenhouse",),
    "ashby": ("com,ashbyhq",),
    "lever": ("co,lever",),
    "workday": ("com,myworkdayjobs", "com,myworkdaysite"),
    "smartrecruiters": ("com,smartrecruiters",),
}
PEEK = 8 * 1024  # enough for a whole line of cluster.idx wherever the peek lands
CHUNK = 256 * 1024
BLOCK_MAX = 8 * 1024 * 1024
GONE = frozenset({"404", "410"})


@dataclass(frozen=True, slots=True)
class Block:
    """One gzip block of an index file: where it is, and how long."""

    name: str
    offset: int
    length: int


def _key(line: bytes) -> bytes:
    return line.split(b" ", 1)[0]


def _block(line: bytes) -> Block | None:
    fields = line.decode("utf-8", "replace").rstrip("\n").split("\t")
    if len(fields) < 4 or not fields[2].isdigit() or not fields[3].isdigit():
        return None
    return Block(fields[1], int(fields[2]), int(fields[3]))


async def blocks_for(client: HttpClient, crawl: str, prefix: str) -> list[Block]:
    """The blocks of `crawl`'s index that can hold a key starting with `prefix`: the one before
    the first such key (a block is named by its first key, so the prefix may begin inside it)
    and every block whose first key has it."""
    url = DATA.format(crawl=crawl, name="cluster.idx")
    wanted = prefix.encode()
    _, size = await client.fetch_range(url, 0, 1)
    if size is None:
        raise RuntimeError(f"{url} did not say how long it is")

    # Bisect by byte for a place whose next whole line is still before the prefix.
    low, high = 0, size
    while high - low > PEEK:
        middle = (low + high) // 2
        peek, _ = await client.fetch_range(url, middle, PEEK)
        start = peek.find(b"\n") + 1
        end = peek.find(b"\n", start)
        if start == 0 or end == -1:
            high = middle  # no whole line in the peek: the end of the file
            continue
        if _key(peek[start:end]) < wanted:
            low = middle
        else:
            high = middle

    found: list[Block] = []
    before: Block | None = None
    position, rest, first = low, b"", low != 0
    while position < size:
        data, _ = await client.fetch_range(url, position, CHUNK)
        if not data:
            break
        position += len(data)
        lines = (rest + data).split(b"\n")
        rest = lines.pop()
        if first:  # the read began inside a line
            lines, first = lines[1:], False
        for line in lines:
            key = _key(line)
            block = _block(line)
            if block is None:
                continue
            if key.startswith(wanted):
                found.append(block)
            elif key < wanted:
                before = block
            else:
                return [*([before] if before else []), *found]
    return [*([before] if before else []), *found]


async def urls_under(client: HttpClient, crawl: str, prefix: str) -> AsyncIterator[str]:
    """Every URL `crawl` captured whose key starts with `prefix`, except captures of pages
    that were gone."""
    wanted = prefix.encode()
    blocks = await blocks_for(client, crawl, prefix)
    log.info("%s %s: %s blocks", crawl, prefix, len(blocks))
    for block in blocks:
        if block.length > BLOCK_MAX:
            continue
        packed, _ = await client.fetch_range(
            DATA.format(crawl=crawl, name=block.name), block.offset, block.length
        )
        for line in gzip.decompress(packed).splitlines():
            if not line.startswith(wanted):
                continue
            brace = line.find(b"{")
            if brace == -1:
                continue
            try:
                capture = json.loads(line[brace:])
            except ValueError:
                continue
            url = capture.get("url")
            if isinstance(url, str) and str(capture.get("status", "")) not in GONE:
                yield url


async def latest_crawls(client: HttpClient, count: int = 1) -> list[str]:
    """The ids of the newest `count` crawls, newest first (`CC-MAIN-2026-43`)."""
    listed = json.loads(await client.fetch_text(COLLECTIONS, timeout=60))
    ids = [item.get("id") for item in listed if isinstance(item, dict)]
    return [crawl for crawl in ids if isinstance(crawl, str)][:count]
