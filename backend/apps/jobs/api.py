"""Company endpoints (design §5.1, §5.3; `map/http-surface.md` §4.2–4.5).

The registry is shared, not per-user: these need a signed-in user but do not scope by one (one
person adding Stripe means everyone's searches cover Stripe; bug #94 keeps that for now).

- `POST /api/companies {query}`: 202 `{ok, runId, message}`. A `company_discovery` run finds and
  registers the company; its output carries the trail and the new row. Throttled
  `company-add:<user>` (30 an hour).
- `PATCH /api/companies/{slug} {status}`: `status` is `active` or `paused` (it was unchecked);
  200 `{ok, company, message}`.
- `POST /api/companies/{slug}/refresh`: 202 `{ok, runId, message}`, a `company_refresh` run.
- `POST /api/companies/refresh-stale`: 202 `{ok, runId, queued, message}`. It always starts a
  run, which always completes, including "None of your boards are stale.".

A second start while the same run is active answers 202 with that run's id. An unknown slug is
404 "That company is no longer on the list.".
"""

from __future__ import annotations

import uuid
from typing import Any, Literal, cast

from django.http import HttpRequest
from ninja import Status
from pydantic import StrictStr

from apps.accounts.models import User
from apps.core.errors import messages
from apps.core.router import ApiModel, router
from apps.jobs import pipeline
from apps.jobs.pipeline import CompanyRow

companies = router(tags=["companies"])

ROUTERS = {"/companies": companies}


def _user_id(request: HttpRequest) -> str:
    return cast(User, request.user).pk


# --- schemas -------------------------------------------------------------------------------------


class AddCompanyIn(ApiModel):
    query: StrictStr  # a company name, a website or a job-board link


class CompanyRunOut(ApiModel):
    ok: bool
    run_id: uuid.UUID
    message: str


class RefreshStaleRunOut(ApiModel):
    ok: bool
    run_id: uuid.UUID
    queued: int  # the stale boards counted when the run started
    message: str


class CompanyStatusIn(ApiModel):
    status: Literal["active", "paused"]


class CompanyStatusOut(ApiModel):
    ok: bool
    company: CompanyRow
    message: str


def _started(started: pipeline.Started) -> dict[str, Any]:
    return {"ok": True, "run_id": started.run.id, "message": started.message}


# --- routes --------------------------------------------------------------------------------------


@companies.post("", response={202: CompanyRunOut}, summary="Add a company")
@messages(invalid=pipeline.QUERY_MISSING)
def add_company(request: HttpRequest, body: AddCompanyIn) -> Status[dict[str, Any]]:
    return Status(202, _started(pipeline.start_add(_user_id(request), body.query)))


@companies.post(
    "/refresh-stale", response={202: RefreshStaleRunOut}, summary="Refresh stale boards"
)
def refresh_stale(request: HttpRequest) -> Status[dict[str, Any]]:
    started = pipeline.start_refresh_stale(_user_id(request))
    return Status(202, {**_started(started), "queued": started.queued})


@companies.patch("/{slug}", response=CompanyStatusOut, summary="Pause or resume a company")
@messages(not_found=pipeline.COMPANY_GONE)
def set_status(request: HttpRequest, slug: str, body: CompanyStatusIn) -> dict[str, Any]:
    row, message = pipeline.set_status(slug, body.status)
    return {"ok": True, "company": row, "message": message}


@companies.post("/{slug}/refresh", response={202: CompanyRunOut}, summary="Refresh one company")
@messages(not_found=pipeline.COMPANY_GONE)
def refresh_company(request: HttpRequest, slug: str) -> Status[dict[str, Any]]:
    return Status(202, _started(pipeline.start_refresh(_user_id(request), slug)))
