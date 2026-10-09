"""`manage.py acceptance_checks [--since ISO]`: the database-level register rows, re-checked after
an acceptance run (design §12.6). Prints PASS/FAIL per line and exits 1 on any FAIL.

**Scope of the run-level lines.** Imported runs are history and stay as they were (the legacy
tailor run ends with `tailor.done` and no `run.*` event), and nothing may synthesise events to
satisfy a check. So the first two lines cover only runs created after the latest successful
`legacy_imports.finished_at` (all runs when there is no import), and the output says how many
imported runs were skipped. The ledger line covers rows created since `--since` (default: the
same boundary).

1. No `queued`/`running` run untouched for 2 minutes without a `todo`/`doing` job holding
   `run:<id>` (R3).
2. Every terminal run has exactly one terminal `run.*` event, matching its status, and it is the
   run's highest event id (R4).
3. No procrastinate job is `doing` past its task's timeout.
4. The newest worker heartbeat is under 45 s old.
5. Every ledger row with a user is attributed: chat rows carry `conversation_id`, every other
   purpose (pipelines, follow-ups' runs) carries `run_id`. System rows (no user) are exempt.
   Deleting a chat sets its rows' `conversation_id` to NULL (acceptance item 13 deletes one), so
   a chat row with neither link fails only when one of the user's chats that still exist was
   open at that moment; the others are counted as rows of deleted chats.
6. Month-to-date spend is below `LLM_MONTHLY_BUDGET_USD`.
7. Every `documents.storage_path` exists in `STORAGE_DIR` with a matching sha256.
8. No conversation has `turn_started_at` older than 200 s.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import connection
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.chat.models import Conversation
from apps.core.storage import StorageError, storage
from apps.legacy.models import ImportRecord
from apps.llm.budget import dollars, month_to_date_spend_sync
from apps.resumes.models import Document
from apps.runs.step import TASKS

SAMPLE = 5  # ids shown per failing line
DEFAULT_TIMEOUT_S = 60 * 60
TURN_MARKER_MAX = timedelta(seconds=200)


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    detail: str


def _rows(sql: str, params: list[Any] | None = None) -> list[tuple[Any, ...]]:
    with connection.cursor() as cursor:
        cursor.execute(sql, params or [])
        return cursor.fetchall()


def _ids(rows: list[tuple[Any, ...]]) -> str:
    shown = ", ".join(str(row[0]) for row in rows[:SAMPLE])
    return shown + (f" and {len(rows) - SAMPLE} more" if len(rows) > SAMPLE else "")


def import_boundary() -> datetime | None:
    """When the latest successful import finished; runs created before it are history."""
    return (
        ImportRecord.objects.filter(status="ok", finished_at__isnull=False)
        .order_by("-finished_at")
        .values_list("finished_at", flat=True)
        .first()
    )


def _after(boundary: datetime | None) -> tuple[str, list[Any]]:
    return ("r.created_at > %s", [boundary]) if boundary else ("true", [])


def check_orphaned_runs(boundary: datetime | None) -> Check:
    scope, params = _after(boundary)
    rows = _rows(
        f"""SELECT r.id FROM runs r
             WHERE {scope} AND r.status IN ('queued', 'running')
               AND r.updated_at < now() - interval '2 minutes'
               AND NOT EXISTS (SELECT 1 FROM procrastinate_jobs j
                                WHERE j.lock = 'run:' || r.id::text
                                  AND j.status IN ('todo', 'doing'))""",
        params,
    )
    detail = f"{len(rows)} active run(s) with no live job: {_ids(rows)}" if rows else "none"
    return Check("every active run is held by a live job (R3)", not rows, detail)


def check_terminal_events(boundary: datetime | None) -> Check:
    scope, params = _after(boundary)
    rows = _rows(
        f"""SELECT r.id FROM runs r
             WHERE {scope} AND r.status IN ('completed', 'failed', 'cancelled')
               AND (
                 (SELECT count(*) FROM events e
                   WHERE e.run_id = r.id
                     AND e.kind IN ('run.completed', 'run.failed', 'run.cancelled')) <> 1
                 OR NOT EXISTS (SELECT 1 FROM events e
                                 WHERE e.run_id = r.id AND e.kind = 'run.' || r.status
                                   AND e.id = (SELECT max(m.id) FROM events m
                                                WHERE m.run_id = r.id)))""",
        params,
    )
    detail = (
        f"{len(rows)} terminal run(s) without exactly one final event last: {_ids(rows)}"
        if rows
        else "every terminal run ends with its one run.* event"
    )
    return Check("one terminal event per run, and it is the last (R4)", not rows, detail)


def check_stuck_jobs() -> Check:
    rows = _rows(
        """SELECT j.id, j.task_name, extract(epoch FROM now() - max(ev.at))
             FROM procrastinate_jobs j
             JOIN procrastinate_events ev ON ev.job_id = j.id AND ev.type = 'started'
            WHERE j.status = 'doing'
            GROUP BY j.id, j.task_name"""
    )
    stuck = [
        row
        for row in rows
        if float(row[2] or 0) > (TASKS[row[1]].timeout_s if row[1] in TASKS else DEFAULT_TIMEOUT_S)
    ]
    detail = (
        f"{len(stuck)} job(s) doing past their timeout: {_ids(stuck)}"
        if stuck
        else f"{len(rows)} job(s) doing, none past its timeout"
    )
    return Check("no job runs past its timeout", not stuck, detail)


def check_heartbeat() -> Check:
    [(age,)] = _rows(
        "SELECT extract(epoch FROM now() - max(last_heartbeat)) FROM procrastinate_workers"
    )
    if age is None:
        return Check("the worker is alive", False, "no worker is registered")
    seconds = float(age)
    return Check("the worker is alive", seconds < 45, f"newest heartbeat {seconds:.0f} s ago")


# A chat turn's ledger rows fall between its chat's `created_at` and `updated_at` (bumped at every
# turn start and finish); the slack covers the two clocks those columns are written with.
CHAT_OPEN_SLACK = timedelta(seconds=2)


def check_attribution(since: datetime | None) -> Check:
    params: list[Any] = [since] if since else []
    window = "l.created_at >= %s" if since else "true"
    # A pipeline row (any purpose but chat) needs its run.
    unattributed = _rows(
        f"""SELECT l.id FROM llm_calls l
             WHERE {window} AND l.user_id IS NOT NULL
               AND l.purpose <> 'chat' AND l.run_id IS NULL""",
        params,
    )
    # A chat row needs its chat (a follow-up's row has its run too). One with neither link is a
    # miss when a chat of the user that still exists was open then; otherwise its chat was
    # deleted since, which sets the link to NULL.
    chat_rows = _rows(
        f"""SELECT l.id,
                   EXISTS (SELECT 1 FROM conversations c
                            WHERE c.user_id = l.user_id
                              AND l.created_at BETWEEN c.created_at - %s AND c.updated_at + %s)
              FROM llm_calls l
             WHERE {window} AND l.user_id IS NOT NULL
               AND l.purpose = 'chat' AND l.conversation_id IS NULL AND l.run_id IS NULL""",
        [CHAT_OPEN_SLACK, CHAT_OPEN_SLACK, *params],
    )
    unattributed += [row for row in chat_rows if row[1]]
    deleted = len(chat_rows) - len([row for row in chat_rows if row[1]])
    extra = f" ({deleted} row(s) of deleted chats)" if deleted else ""
    detail = (
        f"{len(unattributed)} unattributed ledger row(s): {_ids(unattributed)}{extra}"
        if unattributed
        else f"all attributed{extra}"
    )
    return Check("every user's model call is attributed", not unattributed, detail)


def check_budget() -> Check:
    spend = month_to_date_spend_sync()
    budget = settings.ENV.llm_monthly_budget_usd
    return Check(
        "month-to-date spend is below the budget",
        spend < budget,
        f"${dollars(spend)} of ${dollars(budget)}",
    )


def check_documents() -> Check:
    store = storage()
    bad: list[tuple[Any, ...]] = []
    total = 0
    documents = Document.objects.unscoped("acceptance checks: every stored file")
    for doc_id, path, sha256 in documents.values_list("id", "storage_path", "sha256"):
        total += 1
        try:
            digest = hashlib.sha256()
            with store.open(path) as handle:
                while chunk := handle.read(1 << 20):
                    digest.update(chunk)
        except OSError, StorageError:
            bad.append((doc_id, "missing"))
            continue
        if digest.hexdigest() != sha256:
            bad.append((doc_id, "sha256 differs"))
    detail = (
        f"{len(bad)} of {total} document(s) missing or changed: {_ids(bad)}"
        if bad
        else (f"{total} document(s) present and intact")
    )
    return Check("every stored document is present and intact", not bad, detail)


def check_turn_markers() -> Check:
    stale = list(
        Conversation.objects.unscoped("acceptance checks: stale turn markers")
        .filter(turn_started_at__lt=timezone.now() - TURN_MARKER_MAX)
        .values_list("id")
    )
    detail = f"{len(stale)} conversation(s) with a stale marker: {_ids(stale)}" if stale else "none"
    return Check("no chat turn marker is older than 200 s", not stale, detail)


def run_checks(since: datetime | None = None) -> tuple[list[Check], int]:
    """Every check, and how many imported runs the run-level lines skipped."""
    boundary = import_boundary()
    skipped = 0
    if boundary is not None:
        [(skipped,)] = _rows("SELECT count(*) FROM runs WHERE created_at <= %s", [boundary])
    checks = [
        check_orphaned_runs(boundary),
        check_terminal_events(boundary),
        check_stuck_jobs(),
        check_heartbeat(),
        check_attribution(since or boundary),
        check_budget(),
        check_documents(),
        check_turn_markers(),
    ]
    return checks, int(skipped)


class Command(BaseCommand):
    help = "Re-check the database-level invariants after an acceptance run; exit 1 on any FAIL."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--since",
            help="ISO 8601 start of the acceptance window for the ledger check "
            "(default: the latest import, else every row)",
        )

    def handle(self, *args: Any, since: str | None = None, **options: Any) -> None:
        start = None
        if since:
            start = parse_datetime(since)
            if start is None:
                raise CommandError(f"--since is not an ISO 8601 date-time: {since!r}")
            if timezone.is_naive(start):
                start = timezone.make_aware(start, UTC)
        checks, skipped = run_checks(start)
        for check in checks:
            self.stdout.write(f"{'PASS' if check.ok else 'FAIL'}  {check.name}: {check.detail}")
        self.stdout.write(f"({skipped} imported run(s) skipped by the run-level checks)")
        if not all(check.ok for check in checks):
            raise SystemExit(1)
