"""`manage.py watch_now [--email e] [--days N] [--fan-out]` (DEBUG only): a watch now, rather
than after the next six-hourly refresh (#5).

Starts one user's watch (the given email, else the oldest account) and prints `run: <id>`, or
`already running: <id>` when one is active. `--days N` widens its window to the postings first
seen in the last N days, past the usual week. `--fan-out` instead defers `watch.fan_out`, which
starts a watch for every user who is watching, as the scheduled refresh does; it prints
`job: <id>`. The worker runs either.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandParser
from procrastinate.exceptions import AlreadyEnqueued

from apps.accounts.models import User
from apps.core.clock import database_now
from apps.core.queue import defer_in_tx
from apps.matching import tasks, watch


class Command(BaseCommand):
    help = "Start a watch over the newest postings now (DEBUG only)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--email", help="whose watch (default: the oldest account)")
        parser.add_argument("--days", type=int, help="look back this many days instead")
        parser.add_argument(
            "--fan-out", action="store_true", help="start every watching user's watch instead"
        )

    def handle(
        self, *args: Any, email: str | None, days: int | None, fan_out: bool, **options: Any
    ) -> None:
        if not settings.DEBUG:
            self.stderr.write("watch_now is for local development only.")
            raise SystemExit(1)
        if fan_out:
            try:
                job_id = defer_in_tx(tasks.watch_fan_out, queueing_lock="watch:fan-out")
            except AlreadyEnqueued:
                self.stdout.write("A fan-out is already waiting for the worker.")
                return
            self.stdout.write(f"job: {job_id}")
            return
        users = User.objects.order_by("created_at")
        user = users.filter(email__iexact=email).first() if email else users.first()
        if user is None:
            self.stderr.write(f"No user {email}." if email else "No users yet.")
            raise SystemExit(1)
        if days is not None and days <= 0:
            self.stderr.write("Give a positive number of days.")
            raise SystemExit(1)
        since = database_now() - timedelta(days=days) if days else None
        run, reused = watch.start_watch(user.pk, since=since)
        self.stdout.write(f"already running: {run.id}" if reused else f"run: {run.id}")
