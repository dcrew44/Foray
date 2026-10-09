"""`manage.py debug_sleep <seconds>` (DEBUG only): the lock-recovery probe (design §15.2 step 4).

Defers one `debug.sleep` job on the system lane with `lock="debug"` and no run. Kill the worker
with `kill -9` while it sleeps and start it again: the job is failed by the startup sweep or the
next reap (within about 2 minutes), which releases the lock, and a second `debug_sleep` then
starts. Prints `job: <id>`.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandParser

from apps.core.queue import defer_in_tx
from apps.runs.tasks import debug_sleep

LOCK = "debug"


class Command(BaseCommand):
    help = "Defer a debug.sleep job holding lock 'debug' (DEBUG only)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("seconds", type=float, help="how long the job sleeps")

    def handle(self, *args: Any, seconds: float, **options: Any) -> None:
        if not settings.DEBUG:
            self.stderr.write("debug_sleep is for local development only.")
            raise SystemExit(1)
        if seconds <= 0:
            self.stderr.write("Give a positive number of seconds.")
            raise SystemExit(1)
        job_id = defer_in_tx(debug_sleep, lock=LOCK, seconds=seconds)
        self.stdout.write(f"job: {job_id}")
