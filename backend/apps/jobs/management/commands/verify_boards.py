"""`pnpm verify:boards` (design §10.5): check that board slugs are real, without touching the
database. Used to build and re-check the seed list: a slug that 404s makes every later refresh
waste a request and look like a failure.

    pnpm verify:boards --source greenhouse --slugs stripe,figma
    pnpm verify:boards --guess "Stripe" "Figma" "Notion"
    pnpm verify:boards --file candidates.json   # [{name, source?, slug?}]
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from apps.core.http import HttpClient
from apps.core.jsonio import js_json_indent
from apps.jobs.sources import BOARD_ADAPTERS
from apps.jobs.verify import VerifyOutcome, find_board, slug_candidates, verify_board

CONCURRENCY = 6


async def pooled[T, R](
    items: Sequence[T], worker: Callable[[T], Awaitable[R]], limit: int = CONCURRENCY
) -> list[R]:
    gate = asyncio.Semaphore(limit)

    async def one(item: T) -> R:
        async with gate:
            return await worker(item)

    return list(await asyncio.gather(*(one(item) for item in items)))


class Command(BaseCommand):
    help = "Check that board slugs are real (touches no database)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--source", choices=list(BOARD_ADAPTERS))
        parser.add_argument("--slugs")
        parser.add_argument("--guess", nargs="*")
        parser.add_argument("--file")
        parser.add_argument("--json", action="store_true")

    def handle(self, *args: Any, **options: Any) -> None:
        source, slugs = options["source"], options["slugs"]
        if source and slugs:
            outcomes = asyncio.run(_verify(source, [s.strip() for s in slugs.split(",")]))
            for outcome in outcomes:
                line = f"{outcome.status.ljust(10)} {outcome.source}:{outcome.slug}"
                if outcome.job_count is not None:
                    line += f"  {outcome.job_count} jobs"
                if outcome.error is not None:
                    line += f"  {outcome.error}"
                self.stdout.write(line)
            return

        if options["guess"]:
            names = [name for name in options["guess"] if not name.startswith("--")]
        elif options["file"]:
            names = [entry["name"] for entry in json.loads(Path(options["file"]).read_text())]
        else:
            names = []
        if not names:
            self.stderr.write(
                "usage: pnpm verify:boards --source <s> --slugs a,b | --guess Name... "
                "| --file list.json"
            )
            sys.exit(2)

        results = asyncio.run(_guess(names))
        found: list[dict[str, Any]] = []
        for name, outcome in results:
            if outcome is not None and outcome.status == "ok":
                self.stdout.write(
                    f"✓ {name.ljust(28)} {outcome.source}:{outcome.slug} ({outcome.job_count} jobs)"
                )
                found.append(
                    {
                        "name": name,
                        "sourceType": outcome.source,
                        "atsSlug": outcome.slug,
                        "slug": outcome.slug,
                    }
                )
            else:
                self.stdout.write(f"✗ {name.ljust(28)} no board found")

        self.stdout.write(f"\n{len(found)}/{len(names)} resolved")
        if options["json"]:
            self.stdout.write(js_json_indent(found))


async def _verify(source: Any, slugs: list[str]) -> list[VerifyOutcome]:
    async with HttpClient() as client:
        return await pooled(slugs, lambda slug: verify_board(client, source, slug))


async def _guess(names: list[str]) -> list[tuple[str, VerifyOutcome | None]]:
    async with HttpClient() as client:

        async def resolve(name: str) -> tuple[str, VerifyOutcome | None]:
            for candidate in slug_candidates(name):
                outcome = await find_board(client, candidate)
                if outcome is not None and outcome.status == "ok":
                    return name, outcome
            return name, None

        return await pooled(names, resolve)
