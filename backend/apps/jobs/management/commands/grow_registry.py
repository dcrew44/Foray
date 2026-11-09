"""`manage.py grow_registry` (#58): ask candidate boards whether they are real, and register
the ones that answer with postings.

    manage.py grow_registry --limit 3000                      # Greenhouse, Ashby and Lever
    manage.py grow_registry --sources workday --matching "software engineer" --limit 500
    manage.py grow_registry --limit 0 --write-seed            # only rewrite the seed list

Candidates come from `find_boards`. Reports the registry and the early-career software roles it
watches before and after, which is the measure of how much wider the radar got. `--write-seed`
then rewrites `apps/jobs/seed.json` from the boards seen in a public index or a licensed list.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from django.core.management.base import BaseCommand, CommandParser
from django.db import connections

from apps.core.http import HttpClient
from apps.jobs import candidates, radar
from apps.jobs.management.commands.seed_companies import SEED_PATH
from apps.jobs.models import Job
from apps.jobs.registry import count_by_status


class Command(BaseCommand):
    help = "Verify candidate boards live and register the ones with postings."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--sources", default=",".join(candidates.DEFAULT_SOURCES))
        parser.add_argument("--limit", type=int, default=500, help="candidates to ask this run")
        parser.add_argument(
            "--matching",
            default="",
            help="Workday only: register a site only if its own search finds this text",
        )
        parser.add_argument(
            "--any-board",
            action="store_true",
            help="register a board even when nothing it lists is a software role",
        )
        parser.add_argument("--retry", action="store_true", help="also ask those that failed")
        parser.add_argument("--write-seed", action="store_true", help="rewrite the seed list")

    def handle(self, *args: Any, **options: Any) -> None:
        before = self._measure()
        self._show("before", before)
        sources = [name for name in options["sources"].split(",") if name]
        if options["limit"] > 0:
            connections.close_all()  # the work reaches the database through db() threads
            started = time.monotonic()
            summary = asyncio.run(self._grow(sources, options))
            statuses = ", ".join(f"{n} {status}" for status, n in summary.by_status.most_common())
            self.stdout.write(
                f"asked {summary.asked} boards in {time.monotonic() - started:.0f}s: {statuses}"
            )
            self.stdout.write(
                f"registered {summary.registered} companies, {summary.postings} postings"
            )
            self._show("after", self._measure())
        for source, by_status in candidates.candidate_counts().items():
            shown = ", ".join(f"{count} {status}" for status, count in by_status.items())
            self.stdout.write(f"  candidates {source}: {shown}")
        if options["write_seed"]:
            rows = candidates.seed_rows()
            SEED_PATH.write_text(json.dumps(rows, indent=1, ensure_ascii=False) + "\n")
            self.stdout.write(f"wrote {len(rows)} companies to {SEED_PATH}")

    async def _grow(self, sources: list[str], options: dict[str, Any]) -> candidates.GrowSummary:
        def progress(summary: candidates.GrowSummary) -> None:
            if summary.asked % 250 == 0:
                self.stdout.write(f"  …{summary.asked} asked, {summary.registered} registered")

        async with HttpClient() as client:
            return await candidates.grow(
                client,
                sources=sources,
                limit=options["limit"],
                matching=options["matching"],
                any_board=options["any_board"],
                retry=options["retry"],
                progress=progress,
            )

    def _measure(self) -> dict[str, int]:
        return {
            "active companies": count_by_status().get("active", 0),
            "open postings": Job.objects.filter(is_active=True).count(),
            "early-career software roles": radar.early_career_software_roles(),
        }

    def _show(self, label: str, numbers: dict[str, int]) -> None:
        shown = ", ".join(f"{value:,} {name}" for name, value in numbers.items())
        self.stdout.write(f"{label}: {shown}")
