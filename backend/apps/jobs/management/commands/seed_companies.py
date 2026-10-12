"""`pnpm seed:companies [--file path] [--fetch]` (design §10.5).

Loads `apps/jobs/seed.json` (or a file of the same shape, which may include careers-page entries
with `careersUrls`) into the registry. Idempotent: re-running updates the rows it created, and
seeding never changes a company's status. `--fetch` then refreshes the loaded companies (the
active ones) into the mirror and prints the totals; an immediate second `--fetch` reports 0
inserted and 0 updated.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandParser
from django.db import connections

from apps.core.http import HttpClient
from apps.jobs.models import Company, CompanyStatus, DiscoveredVia, SourceType
from apps.jobs.refresh import RefreshOutcome, refresh_companies
from apps.jobs.registry import CompanyInput, count_by_status, upsert_company

SEED_PATH = Path(__file__).resolve().parents[2] / "seed.json"


class Command(BaseCommand):
    help = "Load the seed companies into the registry, and optionally fetch their boards."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--file", help="a JSON list of companies (default: the seed list)")
        parser.add_argument("--fetch", action="store_true", help="refresh the loaded companies")

    def handle(self, *args: Any, **options: Any) -> None:
        path = Path(options["file"]) if options["file"] else SEED_PATH
        entries: list[dict[str, Any]] = json.loads(path.read_text())
        self.stdout.write(f"seeding {len(entries)} companies\u2026")

        loaded: list[Company] = []
        for entry in entries:
            company, _created = upsert_company(
                CompanyInput(
                    name=entry["name"],
                    slug=entry["slug"],
                    source_type=entry["sourceType"],
                    ats_slug=entry.get("atsSlug"),
                    domain=entry.get("domain"),
                    careers_urls=entry.get("careersUrls") or [],
                    tags=entry.get("tags") or [],
                    discovered_via=DiscoveredVia.SEED,
                )
            )
            loaded.append(company)

        self.stdout.write(f"registered {len(loaded)}")
        for status, count in count_by_status().items():
            self.stdout.write(f"  {status}: {count}")

        if not options["fetch"]:
            return

        active = [company for company in loaded if company.status == CompanyStatus.ACTIVE]
        self.stdout.write(f"\nfetching {len(active)} boards\u2026")
        connections.close_all()  # the refresh reaches the database through db() threads
        started = time.monotonic()
        outcomes = asyncio.run(_refresh(active))
        self._report(active, outcomes, time.monotonic() - started)

    def _report(
        self, companies: list[Company], outcomes: list[RefreshOutcome], seconds: float
    ) -> None:
        ok = [o for o in outcomes if o.status == "ok"]
        gone = [o for o in outcomes if o.status == "not-found"]
        failed = [o for o in outcomes if o.status == "error"]
        jobs = sum(o.jobs for o in ok)
        inserted = sum(o.ingest.inserted for o in ok if o.ingest)
        updated = sum(o.ingest.updated for o in ok if o.ingest)
        unchanged = sum(o.ingest.unchanged for o in ok if o.ingest)

        self.stdout.write(
            f"\n{len(ok)} boards ok \u00b7 {len(gone)} gone \u00b7 {len(failed)} errored "
            f"in {seconds:.1f}s"
        )
        self.stdout.write(
            f"{jobs} postings seen → {inserted} inserted, {updated} updated, {unchanged} unchanged"
        )
        for company, outcome in zip(companies, outcomes, strict=True):
            if company.source_type == SourceType.CAREERS_PAGE and outcome.status == "ok":
                self.stdout.write(
                    f"  careers page {outcome.company}: {outcome.jobs} postings, "
                    f"{outcome.model_calls} model calls"
                )
        for outcome in (gone + failed)[:10]:
            self.stdout.write(f"  {outcome.status}: {outcome.company} {outcome.error or ''}")


async def _refresh(companies: list[Company]) -> list[RefreshOutcome]:
    async with HttpClient() as client:
        return await refresh_companies(client, companies)
