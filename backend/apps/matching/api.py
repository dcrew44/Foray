"""Matching endpoints (design §5.1, §5.2, §5.3).

- `POST /api/searches {prompt?, mode?, againFrom?}`: `202 {ok, runId, message}`; `422` when a
  required preference is missing; `409 {ok:false, runId, message}` while a search is active.
- `GET /api/matches?ids=a,b`: `{matches: MatchRow[]}`; strict UUIDs, at most 20, best first.
- `PATCH /api/matches/{id} {stage?, notes?, userRating?}`: `{ok, message}`.
- `POST /api/matches/{id}/evaluate`: `202 {ok, runId, message}` (`200` when that evaluation is
  already running).

The strings are today's server actions' (`map/http-surface.md` §3.7, §4.6–§4.8, §4.14). A
malformed or another user's match id is a 404, indistinguishable from a missing one.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Sequence
from typing import Annotated, Any, cast

from django.db import transaction
from django.http import HttpRequest
from ninja import Status
from pydantic import BeforeValidator, Field, StrictStr, field_validator

from apps.accounts.models import User
from apps.accounts.schemas import AutonomyMode
from apps.core import throttle
from apps.core.errors import ApiError, messages
from apps.core.openapi import register_schema
from apps.core.router import ApiModel, router
from apps.matching import queries
from apps.matching import tasks as _tasks  # noqa: F401 (registers the tasks this API defers)
from apps.matching.deep_eval import NoResumeError, cost_phrase, estimate_eval_cost
from apps.matching.evaluate import UnknownMatchError, start_match_evaluation
from apps.matching.models import Stage
from apps.matching.queries import MatchRow, StageName
from apps.matching.search import (
    PreferencesIncompleteError,
    assert_can_search,
    search_inputs,
    start_search,
)
from apps.runs.catalogue import Mode, RunStatus

log = logging.getLogger("foray.matching")

searches = router(tags=["searches"])
matches = router(tags=["matches"])

ROUTERS = {"/searches": searches, "/matches": matches}

register_schema(Stage)

MAX_IDS = 20
STRICT_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)

SEARCH_WAITING = "A search is already waiting on your go-ahead. Answer it on Home first."
SEARCH_RUNNING = "A search is already running. It will finish in a minute or two."
SEARCH_STARTED = "Search started. Foray will check with you before the deep evaluation."
SEARCH_STARTED_AUTOPILOT = "Search started on autopilot."
MOVE_UNREADABLE = "That move could not be read."
CHANGE_UNREADABLE = "That change could not be read."
MATCH_GONE = "That match no longer exists."
NOTHING_TO_CHANGE = "Nothing to change."
SAVED = "Saved."
MATCH_MISSING = "That match does not exist."
EVALUATION_NOT_STARTED = "Could not start the evaluation just now. Try again in a moment."

STAGE_LABELS: dict[str, str] = {
    "found": "Found",
    "shortlisted": "Shortlisted",
    "applied": "Applied",
    "interviewing": "Interviewing",
    "offer": "Offer",
    "rejected": "Rejected",
    "archived": "Archived",
}


def _user_id(request: HttpRequest) -> str:
    return cast(User, request.user).pk


# --- schemas -------------------------------------------------------------------------------------


class SearchIn(ApiModel):
    prompt: StrictStr | None = None
    mode: AutonomyMode | None = None  # validated now (it was not)
    again_from: StrictStr | None = None  # repeat this earlier search of the user's


class RunStarted(ApiModel):
    ok: bool
    run_id: uuid.UUID
    message: str


class MatchesOut(ApiModel):
    matches: list[MatchRow]


class OkMessageOut(ApiModel):
    ok: bool
    message: str


def _json_number(value: Any) -> Any:
    # zod's z.number() takes numbers only: pydantic's lax int would also take "5" and true.
    if isinstance(value, bool | str):
        raise ValueError("not a number")
    return value


Rating = Annotated[int, Field(ge=-10, le=10), BeforeValidator(_json_number)]


class MatchPatch(ApiModel):
    """Any of the three; a field left out is left alone, `null` clears notes or the rating."""

    stage: StageName | None = None
    notes: Annotated[StrictStr, Field(max_length=20_000)] | None = None
    # The UI decides the scale (thumbs as -1/1, or stars as 1-5); this keeps it a small integer.
    user_rating: Rating | None = None

    @field_validator("stage", mode="before")
    @classmethod
    def _stage_not_null(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("a stage cannot be null")
        return value


def _patch_invalid(errors: Sequence[dict[str, Any]]) -> str:
    """ "That move could not be read." when the stage is what failed, else "That change could not
    be read."."""
    for error in errors:
        loc = tuple(error.get("loc", ()))
        if loc[:1] == ("body",):
            loc = loc[2:]
        if loc[:1] == ("stage",):
            return MOVE_UNREADABLE
    return CHANGE_UNREADABLE


# --- /searches -----------------------------------------------------------------------------------


@searches.post("", response={202: RunStarted}, summary="Start a search")
def post_search(request: HttpRequest, body: SearchIn) -> Status[dict[str, Any]]:
    """Today's rules, in order: the gate (422), the inputs, one active search per user (409 with
    the active run's id), then 202."""
    user_id = _user_id(request)
    try:
        profile = assert_can_search(user_id)
    except PreferencesIncompleteError as error:
        raise ApiError(
            422,
            f"Foray still needs your {', '.join(error.missing).lower()} before it can search. "
            "Add them on your profile or tell Foray in chat.",
        ) from None
    prompt, hints, mode = search_inputs(
        user_id,
        prompt=body.prompt,
        mode=body.mode,
        again_from=body.again_from,
        settings=profile.settings,
    )
    started = start_search(user_id, prompt=prompt, hints=hints, mode=mode)
    if started.reused:
        waiting = started.run.status == RunStatus.AWAITING_APPROVAL
        raise ApiError(
            409, SEARCH_WAITING if waiting else SEARCH_RUNNING, runId=str(started.run.id)
        )
    message = SEARCH_STARTED_AUTOPILOT if mode == Mode.AUTOPILOT else SEARCH_STARTED
    return Status(202, {"ok": True, "run_id": started.run.id, "message": message})


# --- /matches ------------------------------------------------------------------------------------


@matches.get("", response=MatchesOut, summary="Matches by id")
def get_matches(request: HttpRequest, ids: str | None = None) -> dict[str, Any]:
    """The rows a run card lists (`output.topMatchIds` or `output.matchIds`). Anything that is
    not a UUID is ignored; at most 20; another user's ids find nothing."""
    wanted = [part.strip() for part in (ids or "").split(",")]
    valid = [part for part in wanted if STRICT_UUID.fullmatch(part)][:MAX_IDS]
    return {"matches": queries.get_match_rows(_user_id(request), valid)}


@matches.patch("/{id}", response=OkMessageOut, summary="Move or annotate a match")
@messages(invalid=_patch_invalid, not_found=MATCH_GONE)
def patch_match(request: HttpRequest, id: uuid.UUID, body: MatchPatch) -> dict[str, Any]:
    user_id = _user_id(request)
    sent = body.model_fields_set
    if not sent & {"stage", "notes", "user_rating"}:
        return {"ok": True, "message": NOTHING_TO_CHANGE}
    changes: dict[str, Any] = {}
    if "notes" in sent:
        changes["notes"] = body.notes
    if "user_rating" in sent:
        changes["user_rating"] = body.user_rating
    with transaction.atomic():
        if body.stage is not None and not queries.set_match_stage(user_id, id, body.stage):
            raise ApiError(404, MATCH_GONE)
        if changes and not queries.update_match(user_id, id, **changes):
            raise ApiError(404, MATCH_GONE)
    if body.stage is None:
        return {"ok": True, "message": SAVED}
    label = STAGE_LABELS[body.stage]
    rows = queries.get_match_rows(user_id, [id])
    message = (
        f"Moved {rows[0].title} at {rows[0].company} to {label}." if rows else f"Moved to {label}."
    )
    return {"ok": True, "message": message}


@matches.post(
    "/{id}/evaluate", response={200: RunStarted, 202: RunStarted}, summary="Deep-evaluate a match"
)
@messages(not_found=MATCH_MISSING)
def post_evaluate(request: HttpRequest, id: uuid.UUID) -> Status[dict[str, Any]]:
    """Queues a deep evaluation and answers at once: it takes tens of seconds, which is not a
    request's to wait for. Throttled (`evaluate:<user>`, 60 an hour)."""
    user_id = _user_id(request)
    limit = throttle.EVALUATE
    throttle.throttle(f"evaluate:{user_id}", limit.limit, limit.window_s)
    try:
        started = start_match_evaluation(user_id, [id])
    except NoResumeError as error:
        raise ApiError(422, str(error)) from None
    except UnknownMatchError:
        raise ApiError(404, MATCH_MISSING) from None
    except Exception:
        log.exception("could not start an evaluation of match %s", id)
        raise ApiError(500, EVALUATION_NOT_STARTED) from None
    match = started.matches[0]
    label = f"{match.title} at {match.company}"
    if started.reused:
        reused = {"ok": True, "run_id": started.run.id, "message": f"Already evaluating {label}."}
        return Status(200, reused)
    cost = cost_phrase(estimate_eval_cost(1).per_item_usd)
    priced = f" ({cost})" if cost else ""
    return Status(
        202,
        {
            "ok": True,
            "run_id": started.run.id,
            "message": f"Evaluating {label}{priced}. This takes about half a minute.",
        },
    )
