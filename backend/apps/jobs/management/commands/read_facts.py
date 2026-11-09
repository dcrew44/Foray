"""`manage.py read_facts [--all]`: read posting facts into the mirror now (#56).

Ingest reads the facts of every new or changed posting, and the scheduled refresh catches up a
batch of older ones each pass. This reads everything that is behind in one go: after the
migration that adds the columns, or after `facts.VERSION` changed. `--all` reads every posting
again whatever version it has.
"""

from __future__ import annotations

import time
from typing import Any

from django.core.management.base import BaseCommand, CommandParser
from django.db import connection

from apps.jobs import fact_store, facts


class Command(BaseCommand):
    help = "Read level, years, work authorization and clearance for postings that lack them."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--all", action="store_true", help="read every posting again")

    def handle(self, *args: Any, **options: Any) -> None:
        if options["all"]:
            with connection.cursor() as cursor:
                cursor.execute("UPDATE jobs SET facts_version = NULL")
        behind = fact_store.count_stale()
        self.stdout.write(f"{behind} postings to read (facts version {facts.VERSION})")
        started = time.monotonic()
        done = 0
        while True:
            read = fact_store.read_stale()
            if not read:
                break
            done += read
            self.stdout.write(f"  {done}/{behind}")
        self.stdout.write(f"read {done} postings in {time.monotonic() - started:.1f}s")
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT level, count(*) FROM jobs WHERE is_active GROUP BY level ORDER BY 2 DESC"
            )
            levels = ", ".join(f"{level} {count}" for level, count in cursor.fetchall())
        self.stdout.write(f"open postings by level: {levels}")
