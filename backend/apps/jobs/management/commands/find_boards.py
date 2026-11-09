"""`manage.py find_boards` (#58): collect board ids as candidates for the registry.

From Common Crawl's index (`--crawl CC-MAIN-2026-43`, or `--latest 2` for the newest two), or
from a file somebody already made (`--file candidates.tsv`: `source<TAB>id`, optionally followed
by how many crawls it was seen in and the newest of them; or `--urls anything.yml`, which takes
every board URL in a text file; `--found-in list:owner/repo` says where a file that is not from
a crawl came from). Nothing is asked of any board here; that is
`grow_registry`.
"""

from __future__ import annotations

import asyncio
import csv
import re
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.core.db import db
from apps.core.http import HttpClient
from apps.jobs import crawl_index
from apps.jobs.candidates import Seen, add_candidates, candidate_counts, seen_in_urls

_URL = re.compile(r"https?://[^\s\"'<>)\]]+")


class Command(BaseCommand):
    help = "Collect board ids from Common Crawl's index, or a file, as registry candidates."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--crawl", action="append", default=[], help="a crawl id (repeatable)")
        parser.add_argument("--latest", type=int, default=0, help="the newest N crawls")
        parser.add_argument(
            "--sources",
            default="greenhouse,ashby,lever,workday,gem,workable,recruitee",
            help="which boards to look for in a crawl",
        )
        parser.add_argument("--file", help="a TSV of candidates instead of a crawl")
        parser.add_argument(
            "--urls", help="any text file: every board URL in it is a candidate (with --found-in)"
        )
        parser.add_argument("--found-in", help="where a file's rows came from (not a crawl)")

    def handle(self, *args: Any, **options: Any) -> None:
        if options["urls"]:
            if not options["found_in"]:
                raise CommandError("Say where the file came from: --found-in list:owner/repo")
            urls = _URL.findall(Path(options["urls"]).read_text(errors="replace"))
            added = add_candidates(seen_in_urls(urls, options["found_in"]))
            self.stdout.write(f"{added} boards among {len(urls)} URLs in {options['urls']}")
        elif options["file"]:
            added = add_candidates(_from_file(Path(options["file"]), options["found_in"]))
            self.stdout.write(f"{added} candidates from {options['file']}")
        else:
            sources = [name for name in options["sources"].split(",") if name]
            unknown = [name for name in sources if name not in crawl_index.PREFIXES]
            if unknown:
                raise CommandError(f"No crawl prefix for: {', '.join(unknown)}")
            asyncio.run(self._from_crawls(options["crawl"], options["latest"], sources))
        for source, statuses in candidate_counts().items():
            shown = ", ".join(f"{count} {status}" for status, count in statuses.items())
            self.stdout.write(f"  {source}: {shown}")

    async def _from_crawls(self, crawls: list[str], latest: int, sources: list[str]) -> None:
        async with HttpClient() as client:
            if latest:
                crawls = [*crawls, *await crawl_index.latest_crawls(client, latest)]
            if not crawls:
                raise CommandError("Name a crawl (--crawl CC-MAIN-2026-43) or ask for --latest N")
            for crawl in dict.fromkeys(crawls):
                for source in sources:
                    urls: list[str] = []
                    for prefix in crawl_index.PREFIXES[source]:
                        urls.extend(
                            [url async for url in crawl_index.urls_under(client, crawl, prefix)]
                        )
                    sightings = [
                        seen
                        for seen in seen_in_urls(urls, f"commoncrawl:{crawl}", crawl)
                        if seen.source == source
                    ]
                    added = await db(add_candidates, sightings)
                    self.stdout.write(f"{crawl} {source}: {len(urls)} URLs, {added} boards")


def _from_file(path: Path, found_in: str | None) -> list[Seen]:
    """`source, id[, crawls seen, newest crawl]` rows; a header row and unknown sources are
    skipped by `add_candidates`. A crawl given as `2026-43` is `CC-MAIN-2026-43`."""
    sightings: list[Seen] = []
    with path.open(newline="") as handle:
        for row in csv.reader(handle, delimiter="\t"):
            if len(row) < 2:
                continue
            source, slug = row[0].strip(), row[1].strip()
            seen = int(row[2]) if len(row) > 2 and row[2].isdigit() else 1
            newest = row[3].strip() if len(row) > 3 else ""
            if newest and not newest.startswith("CC-MAIN-"):
                newest = f"CC-MAIN-{newest}"
            place = found_in or (f"commoncrawl:{newest}" if newest else "file")
            sightings.append(Seen(source, slug, place, seen, "" if found_in else newest))
    return sightings
