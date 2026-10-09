"""Runs endpoints (design §5.1, §5.3, §6.3, §9.6, §9.9).

- `GET /api/live?since=`: the `LiveSnapshot`, read-only; `cursor` is the last delivered event id
  (§9.5).
- `GET /api/runs/{id}`: one `LiveRun`.
- `GET /api/runs/{id}/events`: `{events: LiveEvent[]}`, at most 1,000, by id.
- `POST /api/runs/{id}/cancel`: 200 "Stopped. Everything found so far was kept."; 409 (or 404 for
  an unknown id) "That run had already finished.".
- `POST /api/approvals/{id}/decision`: `{ok, message}` with today's messages, decided exactly once
  (R6).
- `POST /api/worker/ping`: `{ok, jobId}`; the worker answers with a `worker.ping` event.

Every id is a UUID: a malformed or another user's id is a 404, indistinguishable from a missing
one. The strings are today's (`map/http-surface.md` §3.6–3.10, §4.1, §4.9, §4.15).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, cast

from django.http import HttpRequest
from pydantic import StrictBool, StrictStr

from apps.accounts.models import User
from apps.core.errors import DEFAULT_NOT_FOUND, ApiError, messages
from apps.core.openapi import register_schema
from apps.core.queue import defer_in_tx
from apps.core.router import ApiModel, router
from apps.core.schemas import OkMessageOut
from apps.runs import approvals as approval_service
from apps.runs import lifecycle
from apps.runs import live as live_service
from apps.runs import tasks as run_tasks
from apps.runs.catalogue import (
    ApprovalKind,
    ApprovalStatus,
    CompanyAddOutput,
    DecidedVia,
    EventKind,
    ImportOutput,
    Level,
    Mode,
    RunKind,
    RunStatus,
    WorkerPingData,
)
from apps.runs.live import LiveEvent, LiveRun, LiveSnapshot
from apps.runs.models import Run

log = logging.getLogger("foray.runs")

live = router(tags=["live"])
runs = router(tags=["runs"])
approvals = router(tags=["approvals"])
worker = router(tags=["worker"])

ROUTERS = {"/live": live, "/runs": runs, "/approvals": approvals, "/worker": worker}

RUN_FINISHED = "That run had already finished."
ANSWER_UNREADABLE = "That answer could not be read."
REQUEST_GONE = "That request no longer exists."
ALREADY_ANSWERED = "That was already answered."
REQUEST_EXPIRED = "That request has expired."
COULD_NOT_RESUME = "Could not resume the run just now. Try again in a moment."
PING_NOTE = "The worker answered a test job from Settings."

# Named types the frontend needs that no operation signature carries (§5.1, §5.4).
for _type in (EventKind, RunKind, RunStatus, Mode, ApprovalKind, ApprovalStatus, DecidedVia, Level):
    register_schema(_type)
register_schema(LiveSnapshot)
# Run outputs and event data the frontend reads (`LiveRun.output` and `LiveEvent.data` are open
# objects): the import's `usedVision`, the add's `registered`/`company`/`trail`, the ping's pid.
for _model in (ImportOutput, CompanyAddOutput, WorkerPingData):
    register_schema(_model)


def _user_id(request: HttpRequest) -> str:
    return cast(User, request.user).pk


# --- schemas -------------------------------------------------------------------------------------


class RunEventsOut(ApiModel):
    events: list[LiveEvent]


class DecisionIn(ApiModel):
    approved: StrictBool
    # null means "every item", the same as leaving it out.
    item_ids: list[StrictStr] | None = None


class PingOut(ApiModel):
    ok: bool
    job_id: int


# --- /live ---------------------------------------------------------------------------------------


@live.get("", response=LiveSnapshot, summary="Live snapshot")
def get_live(request: HttpRequest, since: str | None = None) -> LiveSnapshot:
    """The app shell's one poll. `since` is read like JavaScript's `Number()`: anything that is
    not a positive number means the first poll."""
    return live_service.snapshot(_user_id(request), live_service.parse_since(since))


# --- /runs ---------------------------------------------------------------------------------------


@runs.get("/{id}", response=LiveRun, summary="One run")
def get_run(request: HttpRequest, id: uuid.UUID) -> LiveRun:
    run = Run.objects.for_user(_user_id(request)).filter(id=id).first()
    if run is None:
        raise ApiError(404, DEFAULT_NOT_FOUND)
    return live_service.to_live_run(run)


@runs.get("/{id}/events", response=RunEventsOut, summary="A run's events")
def get_run_events(request: HttpRequest, id: uuid.UUID) -> dict[str, Any]:
    found = live_service.run_events(_user_id(request), id)
    if found is None:
        raise ApiError(404, DEFAULT_NOT_FOUND)
    return {"events": found}


@runs.post("/{id}/cancel", response=OkMessageOut, summary="Stop a run")
@messages(not_found=RUN_FINISHED)
def cancel_run(request: HttpRequest, id: uuid.UUID) -> dict[str, Any]:
    user_id = _user_id(request)
    if lifecycle.cancel(user_id, id):
        return {"ok": True, "message": lifecycle.STOPPED}
    if Run.objects.for_user(user_id).filter(id=id).exists():
        raise ApiError(409, RUN_FINISHED)
    raise ApiError(404, RUN_FINISHED)


# --- /approvals ----------------------------------------------------------------------------------


@approvals.post("/{id}/decision", response=OkMessageOut, summary="Answer an approval")
@messages(invalid=ANSWER_UNREADABLE, not_found=REQUEST_GONE)
def decide_approval(request: HttpRequest, id: uuid.UUID, body: DecisionIn) -> dict[str, Any]:
    decision = {"approved": body.approved, "itemIds": body.item_ids}
    try:
        result = approval_service.decide(_user_id(request), id, decision, DecidedVia.UI)
    except Exception:
        # The decision, the run's move and the continuation roll back together (§9.6).
        log.exception("approval %s: the decision could not be saved", id)
        raise ApiError(500, COULD_NOT_RESUME) from None
    if result.outcome == "not_found" or result.approval is None:
        raise ApiError(404, REQUEST_GONE)
    if result.outcome == "already_decided":
        raise ApiError(409, ALREADY_ANSWERED)
    if result.outcome == "expired":
        raise ApiError(409, REQUEST_EXPIRED)
    return {"ok": True, "message": approval_service.decision_reply(result.approval)}


# --- /worker -------------------------------------------------------------------------------------


@worker.post("/ping", response=PingOut, summary="Send the worker a test job")
def ping_worker(request: HttpRequest) -> dict[str, Any]:
    job_id = defer_in_tx(run_tasks.worker_ping, user_id=_user_id(request), note=PING_NOTE)
    return {"ok": True, "job_id": job_id}
