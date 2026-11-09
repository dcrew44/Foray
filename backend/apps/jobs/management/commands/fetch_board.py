"""`pnpm fetch:board <source> <slug> [--json] [--limit n]` (design §10.5).

Fetches one company's board and prints what the adapter made of it, without touching the
database: the quickest way to see whether a slug is real and whether normalisation is right.

    pnpm fetch:board greenhouse anthropic
    pnpm fetch:board lever matchgroup --json
    pnpm fetch:board ashby vanta --limit 3
    pnpm fetch:board workday nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite

A board on a source that lists postings without their descriptions (Workday, SmartRecruiters,
iCIMS portals) costs a request per posting, so only the first `--limit` are read; the count is
the listing's.

Exits 1 when the board does not exist and 2 on a usage error, as `scripts/fetch-board.ts` did.
"""

from __future__ import annotations

import asyncio
import sys
import time
from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from apps.core.http import HttpClient
from apps.core.jsonio import js_json_indent, js_locale
from apps.jobs.sources import BOARD_ADAPTERS, LISTED_ADAPTERS, SOURCES
from apps.jobs.sources.types import BoardNotFound, NormalizedJob


class Command(BaseCommand):
    help = "Print what an adapter makes of one board (touches no database)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("source", nargs="?")
        parser.add_argument("slug", nargs="?")
        parser.add_argument("--json", action="store_true")
        parser.add_argument("--limit", type=int, default=5)

    def handle(self, *args: Any, **options: Any) -> None:
        source, slug = options["source"], options["slug"]
        if not source or not slug or source not in SOURCES:
            sources = " | ".join(SOURCES)
            self.stderr.write(f"usage: pnpm fetch:board <{sources}> <slug> [--json] [--limit n]")
            sys.exit(2)

        limit: int = options["limit"]
        started = time.monotonic()
        try:
            jobs, total = asyncio.run(_fetch(source, slug, limit))
        except BoardNotFound:
            self.stderr.write(f'No {source} board at slug "{slug}".')
            sys.exit(1)
        elapsed = round((time.monotonic() - started) * 1000)

        if options["json"]:
            self.stdout.write(js_json_indent([job.dump() for job in jobs[:limit]]))
            return

        # A trailing newline stops OutputWrapper adding one, so "\n\n" is one blank line.
        self.stdout.write(f"{source}:{slug} \u2014 {total} postings in {elapsed}ms\n\n")
        for job in jobs[:limit]:
            self.stdout.write("\n".join(_describe(job)) + "\n\n")

        with_pay = sum(1 for job in jobs if job.salary.min)
        remote = sum(1 for job in jobs if job.remote == "remote")
        bay_area = sum(
            1 for job in jobs if job.location_norm and job.location_norm.metro == "bay-area"
        )
        read = "" if len(jobs) == total else f" (of the {len(jobs)} read)"
        self.stdout.write(
            f"totals: {total} postings \u00b7 {with_pay} with pay \u00b7 {remote} remote"
            f" \u00b7 {bay_area} Bay Area{read}"
        )


async def _fetch(source: str, slug: str, limit: int) -> tuple[list[NormalizedJob], int]:
    """The postings read and how many the board has."""
    async with HttpClient() as client:
        listed = LISTED_ADAPTERS.get(source)
        if listed is None:
            jobs = await BOARD_ADAPTERS[source].fetch_board(client, slug)
            return jobs, len(jobs)
        listing = await listed.list_board(client, slug)
        read = [
            await listed.read_posting(client, slug, posting, slug)
            for posting in listing.postings[:limit]
        ]
        return [job for job in read if job is not None], len(listing.postings)


def _describe(job: NormalizedJob) -> list[str]:
    salary = job.salary
    pay = (
        f"{salary.currency or ''}{js_locale(salary.min)}\u2013"
        f"{js_locale(salary.max) if salary.max is not None else '?'}/{salary.period or '?'}"
        if salary.min
        else "\u2014"
    )
    metro = job.location_norm.metro if job.location_norm else None
    posted = job.posted_at.strftime("%Y-%m-%d") if job.posted_at else "\u2014"
    lines = [
        job.title,
        f"  id        {job.id}",
        f"  where     {' | '.join(job.locations) or '\u2014'}  [{job.remote}]"
        + (f"  metro={metro}" if metro else ""),
        f"  pay       {pay}",
        f"  posted    {posted}" + (f"   type={job.employment_type}" if job.employment_type else ""),
        f"  text      raw {len(job.description_raw or '')} chars → cleaned "
        f"{len(job.description_text or '')}",
        f"  url       {job.url}",
    ]
    if job.apply_url:
        lines.append(f"  apply     {job.apply_url}")
    lines.append(f"  dedupe    {job.dedupe_key[:12]}\u2026")
    return lines
