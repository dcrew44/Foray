"""The live snapshot behind `GET /api/live` (design §6.3, §9.5, §9.7; `map/worker.md` §7).

One read keeps the sidebar badge, the run cards, the approval cards and the toasts agreeing with
each other. It is read-only now: approvals lapse through the periodic `approvals.expire`.

- **events:** the first poll (`since = 0`) gets the latest 60; later polls get
  `id > since ORDER BY id LIMIT 200`. `emit()` makes ids visible in id order per user, so the
  cursor (the last id returned, else `since`) never skips (R5).
- **runs:** active, or updated in the last 10 minutes, newest 20, so cards see the final state.
- **approvals:** pending, unexpired, on a run that is `awaiting_approval` (bug #41), newest 20.
- **worker:** from procrastinate's own heartbeat; `online` is computed in SQL, so the app
  server's clock does not matter (bug #7). `pid` is always null: the ping answer carries it.

The shapes are `live.ts`'s, camelCase on the wire; `to_live_run` also serves `/api/runs/{id}`.
"""

from __future__ import annotations

import importlib
import logging
import math
import re
import uuid
from datetime import datetime, timedelta
from typing import Any

from django.db import connection
from django.db.models import Q
from django.db.models.functions import Now

from apps.core.router import ApiModel
from apps.runs.catalogue import (
    ACTIVE,
    ApprovalKind,
    ApprovalStatus,
    Level,
    Mode,
    RunKind,
    RunStatus,
)
from apps.runs.models import Approval, Event, Run

__all__ = [
    "LiveApproval",
    "LiveEvent",
    "LiveRun",
    "LiveSnapshot",
    "WorkerStatus",
    "parse_since",
    "run_events",
    "snapshot",
    "to_live_approval",
    "to_live_event",
    "to_live_run",
    "worker_status",
]

log = logging.getLogger("foray.runs")

type Id = str | uuid.UUID

FIRST_PAGE = 60  # the first poll: the latest slice, not the whole history
NEXT_PAGE = 200
RECENT = timedelta(minutes=10)  # finished runs stay this long, so cards see the final state
RUNS_LIMIT = 20
APPROVALS_LIMIT = 20
RUN_EVENTS_LIMIT = 1000  # `/api/runs/{id}/events`: a backstop, not a page size
WORKER_STALE = "45 seconds"


class LiveEvent(ApiModel):
    id: int
    run_id: uuid.UUID | None
    level: Level
    kind: str  # `EventKind`, or a legacy kind in imported history
    message: str
    data: Any
    approval_id: uuid.UUID | None
    created_at: datetime


class LiveRun(ApiModel):
    id: uuid.UUID
    kind: RunKind
    status: RunStatus
    mode: Mode
    prompt: str | None
    summary: str | None  # for a search: what it is for, in one line, once the brief is known
    stats: dict[str, Any]
    output: dict[str, Any] | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class LiveApproval(ApiModel):
    id: uuid.UUID
    run_id: uuid.UUID
    kind: ApprovalKind
    title: str
    payload: dict[str, Any]
    created_at: datetime


class WorkerStatus(ApiModel):
    online: bool
    last_seen_at: datetime | None
    pid: int | None  # always null: the ping answer carries the worker's pid (§9.7)


class LiveSnapshot(ApiModel):
    cursor: int
    events: list[LiveEvent]
    runs: list[LiveRun]
    approvals: list[LiveApproval]
    worker: WorkerStatus


# --- rows to shapes ------------------------------------------------------------------------------


def to_live_event(event: Event) -> LiveEvent:
    return LiveEvent(
        id=event.id,
        run_id=event.run_id,
        level=Level(event.level),
        kind=event.kind,
        message=event.message,
        data=event.data,
        approval_id=event.approval_id,
        created_at=event.created_at,
    )


def search_summary(value: dict[str, Any]) -> str | None:
    """`describeBrief(parseBrief(input.brief ?? input.hints))`, through `apps.matching.brief`
    (step 5A); None until that module exists, or when the brief cannot be described."""
    try:
        brief = importlib.import_module("apps.matching.brief")
    except ModuleNotFoundError as error:
        if error.name != "apps.matching.brief":
            raise
        return None
    parse = getattr(brief, "parse_brief", None)
    describe = getattr(brief, "describe_brief", None)
    if parse is None or describe is None:
        return None
    raw = value.get("brief")
    if raw is None:
        raw = value.get("hints")
    try:
        summary = describe(parse(raw))
    except Exception:
        log.exception("could not describe a search brief")
        return None
    return summary if isinstance(summary, str) else None


def to_live_run(run: Run) -> LiveRun:
    value = run.input if isinstance(run.input, dict) else {}
    prompt = value.get("prompt")
    return LiveRun(
        id=run.id,
        kind=RunKind(run.kind),
        status=RunStatus(run.status),
        mode=Mode(run.mode),
        prompt=prompt if isinstance(prompt, str) else None,
        summary=search_summary(value) if run.kind == RunKind.SEARCH else None,
        stats=run.stats if isinstance(run.stats, dict) else {},
        output=run.output if isinstance(run.output, dict) else None,
        error=run.error,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def to_live_approval(approval: Approval) -> LiveApproval:
    return LiveApproval(
        id=approval.id,
        run_id=approval.run_id,
        kind=ApprovalKind(approval.kind),
        title=approval.title,
        payload=approval.payload if isinstance(approval.payload, dict) else {},
        created_at=approval.created_at,
    )


# --- reads ---------------------------------------------------------------------------------------


_RADIX = re.compile(r"0([xob])([0-9a-f]+)", re.IGNORECASE)


def parse_since(raw: str | None) -> int:
    """The `since` query parameter as `Number(since)` read it, floored to an event id: anything
    that is not a finite positive number is 0 (the first poll)."""
    text = (raw or "").strip()
    if not text:
        return 0
    radix = _RADIX.fullmatch(text)
    try:
        if radix:
            value = float(int(radix.group(2), {"x": 16, "o": 8, "b": 2}[radix.group(1).lower()]))
        elif "_" in text:
            return 0  # Python's float() takes digit separators; JavaScript's Number() does not
        else:
            value = float(text)
    except ValueError:
        return 0
    if not math.isfinite(value) or value <= 0:
        return 0
    return math.floor(value)


def worker_status() -> WorkerStatus:
    """Online when the newest procrastinate heartbeat is under 45 s old, by the database clock.
    A worker that stopped cleanly deletes its row, so `lastSeenAt` is then null (§9.7)."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT max(last_heartbeat), "
            f"coalesce(now() - max(last_heartbeat) < interval '{WORKER_STALE}', false) "
            "FROM procrastinate_workers"
        )
        row = cursor.fetchone()
    last_seen, online = row if row else (None, False)
    return WorkerStatus(online=bool(online), last_seen_at=last_seen, pid=None)


def snapshot(user_id: str, since: int) -> LiveSnapshot:
    """Everything the UI needs to stay current, in four reads."""
    events: list[Event]
    if since > 0:
        events = list(
            Event.objects.for_user(user_id).filter(id__gt=since).order_by("id")[:NEXT_PAGE]
        )
    else:
        events = list(Event.objects.for_user(user_id).order_by("-id")[:FIRST_PAGE])
        events.reverse()
    # Every stored timestamp is the database's, so "now" is the database's too (core/clock.py).
    runs = (
        Run.objects.for_user(user_id)
        .filter(Q(status__in=ACTIVE) | Q(updated_at__gt=Now() - RECENT))
        .order_by("-created_at")[:RUNS_LIMIT]
    )
    approvals = (
        Approval.objects.for_user(user_id)
        .filter(status=ApprovalStatus.PENDING, run__status=RunStatus.AWAITING_APPROVAL)
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=Now()))
        .order_by("-created_at")[:APPROVALS_LIMIT]
    )
    return LiveSnapshot(
        cursor=events[-1].id if events else since,
        events=[to_live_event(event) for event in events],
        runs=[to_live_run(run) for run in runs],
        approvals=[to_live_approval(approval) for approval in approvals],
        worker=worker_status(),
    )


def run_events(user_id: str, run_id: Id) -> list[LiveEvent] | None:
    """Every event of the user's run, oldest first (at most 1,000); None when the run is not
    theirs or does not exist."""
    if not Run.objects.for_user(user_id).filter(id=run_id).exists():
        return None
    rows = Event.objects.for_user(user_id).filter(run_id=run_id).order_by("id")[:RUN_EVENTS_LIMIT]
    return [to_live_event(event) for event in rows]
