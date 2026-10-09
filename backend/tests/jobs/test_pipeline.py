"""Company pipelines (step 5C; design §5.3, §9.4, §10.5): adding one company, discovering a
list of names, refreshing one board or the stale set, the six-hourly cron and its gate, and the
`/api/companies*` endpoints.

Everything runs for real against the test database: the endpoints start runs, a real
procrastinate Worker (in-process, `run_deferred` or a background Worker) runs their jobs, and
only the web is canned (`pipeline.http_client` serves a fake site over the guarded client, so
the SSRF guard is the real one). The live Done checks of §15.2 that need `:3000` and the worker
process are proved here the same way:

- `{"query":"linear.app"}` completes, registered (or existed), with a trail;
- `{"query":"http://169.254.169.254"}` completes not-found, with a blocked step in its trail;
- `refresh-stale` with nothing stale completes "None of your boards are stale." at once and
  emits `company.refresh.done`;
- a worker that dies mid-refresh leaves the run to the reaper, which fails it "Interrupted…"
  and emits `run.failed` (both a stopped in-process Worker and the state `kill -9` leaves).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, cast

import httpx2
import pytest
from django.test import Client
from django.utils import timezone
from procrastinate.periodic import PeriodicRegistry
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo, FunctionModel

from apps.accounts.models import User
from apps.core.db import db
from apps.core.errors import ApiError
from apps.core.models import RateLimit
from apps.core.queue import app, defer_in_tx
from apps.core.throttle import TOO_MANY
from apps.jobs import pipeline
from apps.jobs import refresh as refresh_module
from apps.jobs import tasks as job_tasks
from apps.jobs.discovery import DiscoveryResult
from apps.jobs.fakes import careers_listing_reply
from apps.jobs.models import Company, Job
from apps.jobs.refresh import RefreshOutcome
from apps.jobs.sources import ashby, greenhouse
from apps.llm.models import LlmCall
from apps.runs import lifecycle, periodic, reaper
from apps.runs.models import Event, Run
from apps.runs.step import TASKS
from tests.factories import make_company, make_conversation, make_job, make_user
from tests.fakes import fake_models
from tests.jobs.conftest import fixture_json, html, mock_client
from tests.queue import deferred, run_deferred, worker_connector
from tests.runs.support import (
    age_run,
    dead_worker,
    empty_periodic_registry,
    event_kinds,
    sql,
    wait_for,
)
from tests.runs.support import jobs as queue_jobs

LINEAR_BOARD = fixture_json("ashby/linear.json")
ANTHROPIC_BOARD = fixture_json("greenhouse/anthropic.json")
ORIGIN = "http://localhost:3000"

tx = pytest.mark.django_db(transaction=True)


# --- helpers -------------------------------------------------------------------------------------


class Api:
    """`Client` with JSON bodies and the browser's Origin header on unsafe methods (§4.3)."""

    def __init__(self, client: Client) -> None:
        self.client = client

    def post(self, path: str, body: Any = None) -> Any:
        return self.client.post(
            path,
            data=json.dumps({} if body is None else body),
            content_type="application/json",
            headers={"Origin": ORIGIN},
        )

    def patch(self, path: str, body: Any) -> Any:
        return self.client.patch(
            path, data=json.dumps(body), content_type="application/json", headers={"Origin": ORIGIN}
        )


def signed_in() -> tuple[Api, User]:
    user = make_user()
    client = Client()
    client.force_login(user)
    return Api(client), user


type Answer = Callable[[], httpx2.Response | Awaitable[httpx2.Response]]


@dataclass
class Web:
    """The outside world for discovery and refresh: exact URLs answer, anything else is 404.
    An answer may be async (to hold a fetch open)."""

    pages: dict[str, Answer] = field(default_factory=dict)
    fetched: list[str] = field(default_factory=list)

    async def handler(self, request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        self.fetched.append(url)
        answer = self.pages.get(url)
        if answer is None:
            return httpx2.Response(404, text="Not Found")
        result = answer()
        return result if isinstance(result, httpx2.Response) else await result

    def board(self, url: str, payload: Any) -> None:
        self.pages[url] = lambda: httpx2.Response(200, json=payload)

    def page(self, url: str, body: str) -> None:
        self.pages[url] = lambda: html(body)


@pytest.fixture
def web(monkeypatch: pytest.MonkeyPatch) -> Web:
    site = Web()
    monkeypatch.setattr(pipeline, "http_client", lambda: mock_client(cast(Any, site.handler)))
    return site


@pytest.fixture(autouse=True)
def _fresh_gate(monkeypatch: pytest.MonkeyPatch) -> Iterator[asyncio.Lock]:
    """`MULTI_BOARD_GATE` binds to the event loop that first waits on it; every test runs its
    jobs on a loop of its own, so each gets a fresh gate."""
    gate = asyncio.Lock()
    monkeypatch.setattr(refresh_module, "MULTI_BOARD_GATE", gate)
    yield gate


def reload(run: Run) -> Run:
    return Run.objects.unscoped("tests").get(id=run.id)


def run_of(body: dict[str, Any]) -> Run:
    return Run.objects.unscoped("tests").get(id=body["runId"])


def output_of(run: Run) -> dict[str, Any]:
    assert isinstance(run.output, dict), run.output
    return run.output


def run_events(run: Run) -> list[Event]:
    return list(Event.objects.unscoped("tests").filter(run_id=run.id).order_by("id"))


def active_jobs(slug: str) -> int:
    return Job.objects.filter(company__slug=slug, is_active=True).count()


@asynccontextmanager
async def worker(concurrency: int = 1) -> AsyncIterator[None]:
    """A real Worker on the worker connector, in the background while the block runs; stopped
    gracefully (0.5 s), then by cancelling its jobs, when the block exits."""
    with app.replace_connector(worker_connector()), empty_periodic_registry():
        async with app.open_async():
            task = asyncio.create_task(
                app.run_worker_async(
                    concurrency=concurrency,
                    install_signal_handlers=False,
                    shutdown_graceful_timeout=0.5,
                    fetch_job_polling_interval=0.2,
                    abort_job_polling_interval=0.2,
                )
            )
            try:
                yield
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task


# --- POST /api/companies -------------------------------------------------------------------------


@tx
def test_add_registers_the_board_refreshes_it_and_completes_with_its_row(web: Web) -> None:
    web.page(
        "https://linear.app/careers",
        '<a href="https://jobs.ashbyhq.com/linear">Open roles</a>',
    )
    web.board(ashby.board_url("linear"), LINEAR_BOARD)
    api, user = signed_in()

    response = api.post("/api/companies", {"query": " linear.app "})

    assert response.status_code == 202
    body = response.json()
    assert body == {
        "ok": True,
        "runId": body["runId"],
        "message": "Looking for a job board for “linear.app”.",
    }
    run = run_of(body)
    assert (run.kind, run.status, run.dedupe_key) == (
        "company_discovery",
        "queued",
        "add:linear.app",
    )
    assert run.input == {
        "query": "linear.app",
        "names": None,
        "refreshAfter": True,
        "prompt": "Add linear.app",
    }
    [job] = deferred("company.add")
    assert (job.queue, job.lock, job.queueing_lock) == (
        "interactive",
        f"run:{run.id}",
        f"run:{run.id}",
    )

    run_deferred()

    run = reload(run)
    company = Company.objects.get(source_type="ashby", ats_slug="linear")
    open_roles = active_jobs(company.slug)
    assert open_roles > 0
    assert (run.status, run.stats["phase"]) == ("completed", "done")
    output = output_of(run)
    message = f"Added Linear. Its Ashby board has {pipeline.plural(open_roles, 'open role')}."
    assert (output["registered"], output["existed"], output["message"]) == (True, False, message)
    assert [step["step"] for step in output["trail"]] == ["domain", "careers-page"]
    row = output["company"]
    assert row == {
        "slug": company.slug,
        "name": "Linear",
        "domain": "linear.app",
        "sourceType": "ashby",
        "atsSlug": "linear",
        "careersUrl": "https://linear.app/careers",
        "status": "active",
        "needsBrowser": False,
        "tags": [],
        "lastFetchedAt": row["lastFetchedAt"],
        "lastFetchStatus": "ok",
        "lastFetchJobCount": company.last_fetch_job_count,
        "discoveredVia": "user",
        "activeJobs": open_roles,
    }
    assert row["lastFetchedAt"].endswith("Z")
    # The output's row is the companies page's, `toISOString()` included (`CompanyAddOutput`
    # types it as `CompanyRow`; step-8E.md §2).
    assert row == pipeline.company_row(company, open_roles)
    assert (company.added_by_id, company.discovered_via) == (user.pk, "user")
    # Verified once by discovery, then fetched whole by the follow-on refresh.
    assert web.fetched.count(ashby.board_url("linear")) == 2

    discovered, completed = run_events(run)
    assert (discovered.kind, discovered.message, discovered.level) == (
        "company.discovered",
        "Registered Linear (ashby:linear)",
        "info",
    )
    assert discovered.data == {"trail": output["trail"]}
    assert (completed.kind, completed.message, completed.data) == ("run.completed", message, output)


@tx
def test_adding_a_known_company_says_it_was_already_on_the_list(web: Web) -> None:
    make_company(name="Linear", slug="linear", source_type="ashby", ats_slug="linear")
    web.page("https://linear.app/careers", '<a href="https://jobs.ashbyhq.com/linear">Jobs</a>')
    web.board(ashby.board_url("linear"), LINEAR_BOARD)
    api, _ = signed_in()

    run = run_of(api.post("/api/companies", {"query": "linear.app"}).json())
    run_deferred()

    output = output_of(reload(run))
    open_roles = pipeline.plural(active_jobs("linear"), "open role")
    assert (output["registered"], output["existed"]) == (True, True)
    assert output["message"] == (
        f"Linear was already on the list. Its Ashby board has {open_roles}."
    )
    assert output["company"]["slug"] == "linear"
    assert Company.objects.count() == 1


@tx
def test_a_pasted_board_link_is_verified_and_filled(web: Web) -> None:
    web.board(ashby.board_url("linear"), LINEAR_BOARD)
    api, _ = signed_in()

    run = run_of(api.post("/api/companies", {"query": "https://jobs.ashbyhq.com/linear"}).json())
    run_deferred()

    output = output_of(reload(run))
    assert (output["registered"], output["existed"]) == (True, False)
    assert output["trail"] == [{"step": "ats-link", "detail": "pasted ashby:linear"}]
    assert output["company"]["activeJobs"] == active_jobs(output["company"]["slug"]) > 0
    # Named after the board's slug, not the URL; the ATS host is not its domain (step H1).
    assert (output["company"]["name"], output["company"]["domain"]) == ("Linear", None)
    assert output["message"].startswith("Added Linear. Its Ashby board has ")


@tx
def test_an_internal_address_completes_not_found_with_a_blocked_step(web: Web) -> None:
    api, _ = signed_in()

    response = api.post("/api/companies", {"query": "http://169.254.169.254"})
    assert response.status_code == 202
    run = run_of(response.json())
    run_deferred()

    run = reload(run)
    assert run.status == "completed"
    output = output_of(run)
    message = (
        "No job board found at “http://169.254.169.254”. Try a direct link to its jobs page, "
        "or the company's name."
    )
    assert (output["registered"], output["existed"], output["company"]) == (False, False, None)
    assert output["message"] == message
    assert {"step": "blocked", "detail": "That address is not reachable from Foray."} in output[
        "trail"
    ]
    # The guard refused before sending: the address never reached the transport.
    assert not [url for url in web.fetched if "169.254" in url]
    not_found, completed = run_events(run)
    assert (not_found.kind, not_found.level) == ("company.not-found", "warn")
    assert not_found.message == 'Could not find a job board for "http://169.254.169.254"'
    assert (completed.kind, completed.level, completed.message) == (
        "run.completed",
        "warn",
        message,
    )
    assert Company.objects.count() == 0


@tx
def test_a_name_with_no_board_says_what_to_try(web: Web) -> None:
    api, _ = signed_in()
    run = run_of(api.post("/api/companies", {"query": "Nowhere Labs"}).json())
    run_deferred()
    output = output_of(reload(run))
    assert output["message"] == (
        "No job board found for “Nowhere Labs”. Try its website or a link to its jobs page."
    )
    assert output["trail"][-1]["step"] == "search"


@pytest.mark.django_db
def test_add_refuses_empty_long_and_unreadable_queries_before_counting() -> None:
    api, _ = signed_in()
    cases = [
        ({"query": "   "}, pipeline.QUERY_MISSING),
        ({"query": "x" * 301}, pipeline.QUERY_TOO_LONG),
        ({}, pipeline.QUERY_MISSING),
        ({"query": 5}, pipeline.QUERY_MISSING),
    ]
    for body, message in cases:
        response = api.post("/api/companies", body)
        assert (response.status_code, response.json()) == (400, {"ok": False, "message": message})
    assert not RateLimit.objects.filter(key__startswith="company-add:").exists()
    assert Run.objects.unscoped("tests").count() == 0


@pytest.mark.django_db
def test_the_31st_add_in_an_hour_is_throttled() -> None:
    api, user = signed_in()
    for n in range(30):
        assert api.post("/api/companies", {"query": f"Company {n}"}).status_code == 202
    response = api.post("/api/companies", {"query": "One more"})
    assert (response.status_code, response.json()) == (429, {"ok": False, "message": TOO_MANY})
    assert RateLimit.objects.get(key=f"company-add:{user.pk}").count == 31
    # The names start counts against the same key, once per call.
    with pytest.raises(ApiError) as refused:
        pipeline.start_names(user.pk, ["Linear"])
    assert refused.value.status == 429
    other, _ = signed_in()
    assert other.post("/api/companies", {"query": "One more"}).status_code == 202


@pytest.mark.django_db
def test_a_second_add_of_the_same_query_returns_the_active_run() -> None:
    api, _ = signed_in()
    first = api.post("/api/companies", {"query": "Linear"}).json()
    second = api.post("/api/companies", {"query": "  linear "}).json()
    assert second == {
        "ok": True,
        "runId": first["runId"],
        "message": "Already looking for “linear”.",
    }
    assert len(deferred("company.add")) == 1


@pytest.mark.django_db
def test_the_company_routes_need_a_session() -> None:
    client = Client()
    for method, path in [
        ("post", "/api/companies"),
        ("post", "/api/companies/refresh-stale"),
        ("post", "/api/companies/acme/refresh"),
        ("patch", "/api/companies/acme"),
    ]:
        response = getattr(client, method)(
            path, data="{}", content_type="application/json", headers={"Origin": ORIGIN}
        )
        assert (response.status_code, response.json()) == (
            401,
            {"ok": False, "message": "Not signed in."},
        ), path


# --- names (chat's discover_companies) -----------------------------------------------------------


@tx
def test_names_register_each_board_and_report_the_rest(web: Web) -> None:
    web.board(ashby.board_url("linear"), LINEAR_BOARD)
    user = make_user()

    started = pipeline.start_names(user.pk, ["Linear", " Nowhere Labs ", "Linear"])

    assert started.message == (
        "Looking for job boards for 2 companies: Linear, Nowhere Labs. "
        "Progress will appear in the activity feed."
    )
    assert started.run.input == {
        "query": None,
        "names": ["Linear", "Nowhere Labs"],
        "refreshAfter": False,
        "prompt": "Find job boards for Linear and Nowhere Labs",
    }
    assert started.run.dedupe_key is not None and started.run.dedupe_key.startswith("add:names:")

    run_deferred()

    run = reload(started.run)
    message = "Registered Linear. No job board found for Nowhere Labs."
    assert run.status == "completed"
    assert run.output == {"added": ["Linear"], "notFound": ["Nowhere Labs"], "message": message}
    assert [(e.kind, e.message) for e in run_events(run)] == [
        ("company.discovered", "Registered Linear (ashby:linear)"),
        ("company.not-found", 'Could not find a job board for "Nowhere Labs"'),
        ("run.completed", message),
    ]
    company = Company.objects.get(ats_slug="linear")
    assert company.discovered_via == "agent"
    # Names are registered, not filled: the board was only verified.
    assert web.fetched.count(ashby.board_url("linear")) == 1
    assert active_jobs(company.slug) == 0


@pytest.mark.django_db
def test_names_refuse_an_empty_or_long_list_before_counting() -> None:
    user = make_user()
    for names, message in [
        ([], pipeline.NAMES_MISSING),
        (["  ", ""], pipeline.NAMES_MISSING),
        (["Linear", "x" * 301], pipeline.QUERY_TOO_LONG),
    ]:
        with pytest.raises(ApiError) as refused:
            pipeline.start_names(user.pk, names)
        assert (refused.value.status, refused.value.message) == (400, message)
    assert pipeline.NAMES_MISSING == "Name at least one company to look for."
    assert not RateLimit.objects.filter(key__startswith="company-add:").exists()
    assert Run.objects.unscoped("tests").count() == 0


@tx
def test_a_run_whose_request_cannot_be_read_fails_with_a_sentence() -> None:
    """Only the start functions write these inputs; one that does not read fails with the
    sentence itself, not "Could not add that company: …"."""
    make_company(name="Acme", slug="acme", ats_slug="acme")
    user = make_user()
    add = pipeline.start_add(user.pk, "Linear").run
    refresh = pipeline.start_refresh(user.pk, "acme").run
    Run.objects.unscoped("tests").filter(id=add.id).update(input={})
    Run.objects.unscoped("tests").filter(id=refresh.id).update(input={"prompt": "Refresh"})

    run_deferred()

    for run in (reload(add), reload(refresh)):
        assert (run.status, run.error) == ("failed", "This run's request could not be read.")
        [failed] = run_events(run)
        assert (failed.kind, failed.message) == ("run.failed", pipeline.UNREADABLE_INPUT)


@tx
def test_a_stop_between_names_ends_the_run_with_its_cancel(
    web: Web, monkeypatch: pytest.MonkeyPatch
) -> None:
    web.board(ashby.board_url("linear"), LINEAR_BOARD)
    user = make_user()
    started = pipeline.start_names(user.pk, ["Linear", "Vanta"])
    real_discover = pipeline.discover_company
    looked_up: list[str] = []

    async def discover_then_stop(client: Any, name: str, **kwargs: Any) -> Any:
        looked_up.append(name)
        result = await real_discover(client, name, **kwargs)
        await db(lifecycle.cancel, user.pk, started.run.id)
        return result

    monkeypatch.setattr(pipeline, "discover_company", discover_then_stop)
    run_deferred()

    run = reload(started.run)
    assert run.status == "cancelled"
    assert looked_up == ["Linear"]
    # Nothing lands after the Stop: run.cancelled stays the run's last event (R4).
    assert event_kinds(run.id) == ["run.cancelled"]


@tx
def test_a_stop_just_before_a_not_found_add_finishes_leaves_the_cancel_last(
    web: Web, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The not-found pair (`company.not-found`, `run.completed`) is written by `_finish`, which
    writes nothing once the run has stopped (R4)."""
    user = make_user()
    run = pipeline.start_add(user.pk, "Nowhere Labs").run

    async def stop_then_miss(client: Any, name: str, **kwargs: Any) -> DiscoveryResult:
        await db(lifecycle.cancel, user.pk, run.id)
        return DiscoveryResult(
            name=name,
            registered=None,
            source=None,
            slug=None,
            careers_urls=[],
            needs_browser=False,
            trail=[{"step": "search", "detail": "nothing found"}],
        )

    monkeypatch.setattr(pipeline, "discover_company", stop_then_miss)
    run_deferred()

    run = reload(run)
    assert (run.status, run.output) == ("cancelled", None)
    assert event_kinds(run.id) == ["run.cancelled"]


@tx
def test_an_add_started_from_chat_reports_back_there(web: Web) -> None:
    web.board(ashby.board_url("linear"), LINEAR_BOARD)
    user = make_user()
    conversation = make_conversation(user)

    started = pipeline.start_add(
        user.pk, "https://jobs.ashbyhq.com/linear", conversation_id=conversation.id
    )
    assert started.run.conversation_id == conversation.id
    run_deferred(["interactive"])

    completed = run_events(reload(started.run))[-1]
    assert completed.kind == "run.completed"
    follow_ups = queue_jobs(task_name="chat.follow_up")
    assert [job["args"] for job in follow_ups] == [{"event_id": completed.id}]


# --- refreshing one company ----------------------------------------------------------------------


@tx
def test_refreshing_one_company_completes_with_integer_counts(web: Web) -> None:
    make_company(name="Acme", slug="acme", ats_slug="acme")
    web.board(greenhouse.board_url("acme"), ANTHROPIC_BOARD)
    api, _ = signed_in()

    response = api.post("/api/companies/acme/refresh")

    assert response.status_code == 202
    body = response.json()
    assert body == {"ok": True, "runId": body["runId"], "message": "Refreshing Acme."}
    run = run_of(body)
    assert (run.kind, run.dedupe_key) == ("company_refresh", "refresh:acme")
    assert run.input == {"slug": "acme", "name": "Acme", "stale": False, "prompt": "Refresh Acme"}
    [job] = deferred("company.refresh")
    assert (job.queue, job.lock) == ("background", f"run:{run.id}")

    run_deferred()

    run = reload(run)
    n = active_jobs("acme")
    message = f"Acme: {pipeline.plural(n, 'open role')}, {n} new and 0 closed since the last fetch."
    assert run.status == "completed"
    assert run.output == {
        "boards": 1,
        "ok": 1,
        "notFound": 0,
        "errored": 0,
        "jobs": n,
        "inserted": n,
        "closed": 0,
        "message": message,
    }
    assert all(type(value) is int for key, value in output_of(run).items() if key != "message")
    done, completed = run_events(run)
    assert (done.kind, done.level, done.message) == ("company.refresh.done", "debug", message)
    assert done.data == {"boards": 1, "ok": 1, "notFound": 0, "errored": 0}
    assert (completed.kind, completed.level, completed.message) == (
        "run.completed",
        "info",
        message,
    )

    # Fetched again with nothing new: today's ", no changes".
    again = run_of(api.post("/api/companies/acme/refresh").json())
    run_deferred()
    output = output_of(reload(again))
    assert output["message"] == f"Acme: {pipeline.plural(n, 'open role')}, no changes."


@tx
def test_a_refresh_that_fails_completes_with_the_problem(web: Web, no_backoff: list[float]) -> None:
    make_company(name="Gone", slug="gone", ats_slug="gone")
    make_company(name="Flaky", slug="flaky", ats_slug="flaky")
    web.pages[greenhouse.board_url("flaky")] = lambda: httpx2.Response(503, text="busy")
    user = make_user()
    gone = pipeline.start_refresh(user.pk, "gone").run
    flaky = pipeline.start_refresh(user.pk, "flaky").run

    run_deferred()

    gone, flaky = reload(gone), reload(flaky)
    assert gone.status == flaky.status == "completed"
    assert gone.output is not None and flaky.output is not None
    assert gone.output["message"] == "Gone: board not found, so it is marked invalid."
    assert (gone.output["boards"], gone.output["ok"], gone.output["notFound"]) == (1, 0, 1)
    assert Company.objects.get(slug="gone").status == "invalid"
    assert flaky.output["message"].startswith("Flaky: last fetch failed. ")
    assert (flaky.output["ok"], flaky.output["errored"]) == (0, 1)
    assert Company.objects.get(slug="flaky").status == "active"
    for run in (gone, flaky):
        # The counts event is debug, so the feeds show the sentence once (deviations/step-10.md).
        assert [(e.kind, e.level) for e in run_events(run)] == [
            ("company.refresh.done", "debug"),
            ("run.completed", "warn"),
        ]


@tx
def test_a_company_gone_before_its_refresh_runs_completes_with_a_warning(web: Web) -> None:
    make_company(name="Acme", slug="acme", ats_slug="acme")
    user = make_user()
    run = pipeline.start_refresh(user.pk, "acme").run
    Company.objects.filter(slug="acme").delete()

    run_deferred()

    run = reload(run)
    message = "That company is no longer on the list."
    assert run.status == "completed"
    assert run.output == {
        "boards": 0,
        "ok": 0,
        "notFound": 0,
        "errored": 0,
        "jobs": 0,
        "inserted": 0,
        "closed": 0,
        "message": message,
    }
    assert [(e.kind, e.level, e.message, e.data) for e in run_events(run)] == [
        (
            "company.refresh.done",
            "debug",
            message,
            {"boards": 0, "ok": 0, "notFound": 0, "errored": 0},
        ),
        ("run.completed", "warn", message, run.output),
    ]
    assert web.fetched == []


@tx
def test_a_stop_just_before_the_final_pair_leaves_the_cancel_last(
    web: Web, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Stop that lands while the board is fetched: `_finish` checks the run is still active
    under its row lock, so neither `company.refresh.done` nor `run.completed` follows
    `run.cancelled` (R4)."""
    make_company(name="Acme", slug="acme", ats_slug="acme")
    user = make_user()
    run = pipeline.start_refresh(user.pk, "acme").run

    async def stop_then_refresh(client: Any, company: Company) -> RefreshOutcome:
        await db(lifecycle.cancel, user.pk, run.id)
        return RefreshOutcome(company.name, company.slug, "ok", 0)

    monkeypatch.setattr(pipeline, "refresh_company", stop_then_refresh)
    run_deferred()

    run = reload(run)
    assert (run.status, run.output) == ("cancelled", None)
    assert event_kinds(run.id) == ["run.cancelled"]
    assert web.fetched == []


@pytest.mark.django_db
def test_an_unknown_slug_is_no_longer_on_the_list() -> None:
    api, _ = signed_in()
    gone = {"ok": False, "message": "That company is no longer on the list."}
    refresh = api.post("/api/companies/nope/refresh")
    assert (refresh.status_code, refresh.json()) == (404, gone)
    patch = api.patch("/api/companies/nope", {"status": "paused"})
    assert (patch.status_code, patch.json()) == (404, gone)
    assert Run.objects.unscoped("tests").count() == 0


@pytest.mark.django_db
def test_pausing_and_resuming_a_company() -> None:
    # A whole number of milliseconds that is not zero, so both encoders print `.123Z`.
    fetched = timezone.now().replace(microsecond=123000) - timedelta(hours=1)
    acme = make_company(
        name="Acme",
        slug="acme",
        ats_slug="acme",
        domain="acme.com",
        tags=["ai"],
        last_fetched_at=fetched,
        last_fetch_status="ok",
        last_fetch_job_count=2,
    )
    make_job(company=acme)
    make_job(company=acme, is_active=False)
    api, _ = signed_in()

    paused = api.patch("/api/companies/acme", {"status": "paused"})

    assert paused.status_code == 200
    assert paused.json() == {
        "ok": True,
        "company": {
            "slug": "acme",
            "name": "Acme",
            "domain": "acme.com",
            "sourceType": "greenhouse",
            "atsSlug": "acme",
            "careersUrl": None,
            "status": "paused",
            "needsBrowser": False,
            "tags": ["ai"],
            "lastFetchedAt": fetched.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "lastFetchStatus": "ok",
            "lastFetchJobCount": 2,
            "discoveredVia": "seed",
            "activeJobs": 1,
        },
        "message": "Paused Acme. Its board is skipped until you resume it.",
    }
    resumed = api.patch("/api/companies/acme", {"status": "active"})
    assert resumed.json()["message"] == "Acme is active again and will be fetched with the rest."
    assert Company.objects.get(slug="acme").status == "active"

    for body in ({"status": "invalid"}, {"status": None}, {}):
        refused = api.patch("/api/companies/acme", body)
        assert (refused.status_code, refused.json()) == (
            400,
            {"ok": False, "message": "That request could not be read."},
        )


# --- refreshing the stale set --------------------------------------------------------------------


@tx
def test_refresh_stale_refreshes_only_the_stale_boards(web: Web) -> None:
    now = timezone.now()
    make_company(name="Acme", slug="acme", ats_slug="acme")  # never fetched
    make_company(
        name="Beta", slug="beta", ats_slug="beta", last_fetched_at=now - timedelta(hours=3)
    )
    make_company(name="Fresh", slug="fresh", ats_slug="fresh", last_fetched_at=now)
    make_company(name="Paused", slug="paused", ats_slug="paused", status="paused")
    for slug in ("acme", "beta"):
        web.board(greenhouse.board_url(slug), ANTHROPIC_BOARD)
    api, _ = signed_in()

    response = api.post("/api/companies/refresh-stale")

    assert response.status_code == 202
    body = response.json()
    assert body == {
        "ok": True,
        "runId": body["runId"],
        "queued": 2,
        "message": "Refreshing 2 stale boards in the background. The table updates when they are "
        "done.",
    }
    run = run_of(body)
    assert (run.dedupe_key, run.input) == (
        "refresh:stale",
        {"slug": None, "name": None, "stale": True, "prompt": "Refresh the stale boards"},
    )

    run_deferred()

    run = reload(run)
    seen = active_jobs("acme") + active_jobs("beta")
    message = f"Refreshed 2/2 boards — {seen} postings, 0 closed"
    assert run.output == {
        "boards": 2,
        "ok": 2,
        "notFound": 0,
        "errored": 0,
        "jobs": seen,
        "inserted": seen,
        "closed": 0,
        "message": message,
    }
    done, completed = run_events(run)
    assert (done.kind, done.data) == (
        "company.refresh.done",
        {"boards": 2, "ok": 2, "notFound": 0, "errored": 0},
    )
    assert (completed.kind, completed.message) == ("run.completed", message)
    assert greenhouse.board_url("fresh") not in web.fetched
    assert greenhouse.board_url("paused") not in web.fetched


@tx
def test_refresh_stale_with_nothing_stale_still_completes_at_once(web: Web) -> None:
    make_company(name="Fresh", slug="fresh", ats_slug="fresh", last_fetched_at=timezone.now())
    api, _ = signed_in()

    response = api.post("/api/companies/refresh-stale")

    assert response.status_code == 202
    body = response.json()
    assert (body["queued"], body["message"]) == (
        0,
        "Every active board was fetched in the last two hours, so none are stale.",
    )
    started = time.monotonic()
    run_deferred()
    assert time.monotonic() - started < 10

    run = reload(run_of(body))
    assert run.status == "completed"
    assert run.output == {
        "boards": 0,
        "ok": 0,
        "notFound": 0,
        "errored": 0,
        "jobs": 0,
        "inserted": 0,
        "closed": 0,
        "message": "None of your boards are stale.",
    }
    done, completed = run_events(run)
    assert (done.kind, done.message, done.data) == (
        "company.refresh.done",
        "None of your boards are stale.",
        {"boards": 0, "ok": 0, "notFound": 0, "errored": 0},
    )
    assert (completed.kind, completed.message) == (
        "run.completed",
        "None of your boards are stale.",
    )
    assert web.fetched == []


@pytest.mark.django_db
def test_a_second_refresh_stale_returns_the_active_run() -> None:
    api, _ = signed_in()
    first = api.post("/api/companies/refresh-stale").json()
    second = api.post("/api/companies/refresh-stale").json()
    assert (second["runId"], second["message"]) == (
        first["runId"],
        "The stale boards are already being refreshed.",
    )
    assert len(deferred("company.refresh")) == 1


# --- the cron and the gate -----------------------------------------------------------------------


@tx
async def test_a_stale_run_waits_while_the_cron_holds_the_gate(
    web: Web, _fresh_gate: asyncio.Lock
) -> None:
    """The cron and a stale-set run never overlap: the run shows `waiting` while the cron
    refreshes, then finds nothing left to do."""
    cron_fetching = asyncio.Event()
    release = asyncio.Event()
    gate_held: list[bool] = []

    async def slow_board() -> httpx2.Response:
        gate_held.append(_fresh_gate.locked())
        cron_fetching.set()
        await release.wait()
        return httpx2.Response(200, json=ANTHROPIC_BOARD)

    web.pages[greenhouse.board_url("acme")] = slow_board
    user = await db(make_user)
    await db(make_company, name="Acme", slug="acme", ats_slug="acme")  # never fetched: stale
    await db(defer_in_tx, job_tasks.companies_refresh_scheduled, timestamp=0)

    async with worker(concurrency=2):
        await asyncio.wait_for(cron_fetching.wait(), 10)
        started = await db(pipeline.start_refresh_stale, user.pk)
        assert started.queued == 1  # the cron has not finished it yet
        await wait_for(lambda: reload(started.run).stats.get("phase") == "waiting")
        release.set()
        await wait_for(lambda: reload(started.run).status == "completed")

    run = await db(reload, started.run)
    assert run.output is not None
    assert (run.output["boards"], run.output["message"]) == (0, "None of your boards are stale.")
    assert run.stats["phase"] == "done"
    assert gate_held == [True]  # the cron fetched under the gate
    assert web.fetched.count(greenhouse.board_url("acme")) == 1  # and only the cron fetched
    assert await db(active_jobs, "acme") == len(ANTHROPIC_BOARD["jobs"])
    assert await db(lambda: Run.objects.unscoped("tests").count()) == 1  # the cron made no run
    kinds = await db(
        lambda: list(Event.objects.unscoped("tests").order_by("id").values_list("kind", "run_id"))
    )
    assert kinds == [("company.refresh.done", run.id), ("run.completed", run.id)]


@tx
def test_the_cron_refreshes_the_stale_set_with_no_run_user_or_event(web: Web) -> None:
    now = timezone.now()
    make_company(
        name="Acme", slug="acme", ats_slug="acme", last_fetched_at=now - timedelta(hours=7)
    )
    make_company(name="Fresh", slug="fresh", ats_slug="fresh", last_fetched_at=now)
    web.board(greenhouse.board_url("acme"), ANTHROPIC_BOARD)

    defer_in_tx(job_tasks.companies_refresh_scheduled, timestamp=0)
    run_deferred()

    acme = Company.objects.get(slug="acme")
    assert acme.last_fetch_status == "ok" and acme.last_fetched_at is not None
    assert acme.last_fetched_at > now
    assert active_jobs("acme") == len(ANTHROPIC_BOARD["jobs"])
    assert web.fetched == [greenhouse.board_url("acme")]
    assert Run.objects.unscoped("tests").count() == 0
    assert Event.objects.unscoped("tests").count() == 0
    assert [job["status"] for job in queue_jobs(task_name="companies.refresh_scheduled")] == [
        "succeeded"
    ]


def test_the_company_tasks_sit_on_their_lanes_and_the_cron_is_scheduled() -> None:
    for name, lane in [
        ("company.add", "interactive"),
        ("company.refresh", "background"),
        ("companies.refresh_scheduled", "background"),
    ]:
        task = app.tasks[name]
        assert (task.queue, TASKS[name].lane) == (lane, lane), name
        assert task.retry_strategy is None, f"{name} can call a model and must not retry"
    assert app.tasks["companies.refresh_scheduled"].lock is None  # a periodic never holds one

    registry = PeriodicRegistry()

    class Stub:
        periodic_registry = registry
        tasks = app.tasks

    assert "companies.refresh_scheduled" in periodic.install(cast(Any, Stub()))
    [cron] = [
        p for p in registry.periodic_tasks.values() if p.task.name == "companies.refresh_scheduled"
    ]
    assert cron.cron == "0 */6 * * *"
    assert cron.configure_kwargs == {"queueing_lock": "company-refresh:cron"}


# --- a worker that dies mid-refresh --------------------------------------------------------------


@tx
async def test_a_worker_stopped_mid_refresh_leaves_the_run_to_the_reaper(web: Web) -> None:
    fetching = asyncio.Event()

    async def hang() -> httpx2.Response:
        fetching.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    web.pages[greenhouse.board_url("acme")] = hang
    user = await db(make_user)
    await db(make_company, name="Acme", slug="acme", ats_slug="acme")
    started = await db(pipeline.start_refresh_stale, user.pk)

    async with worker():
        await asyncio.wait_for(fetching.wait(), 10)
        await wait_for(lambda: reload(started.run).stats.get("phase") == "refreshing")
    # The worker is gone and its job with it; nothing finished the run.
    run = await db(reload, started.run)
    assert run.status == "running"
    statuses = [job["status"] for job in await db(queue_jobs, lock=f"run:{run.id}")]
    assert statuses and not {"todo", "doing"} & set(statuses)

    await db(age_run, run.id, 120)  # the reaper's 60 s margin has passed
    assert (await db(reaper.reap_runs)).interrupted == 1

    run = await db(reload, started.run)
    assert (run.status, run.error) == ("failed", reaper.INTERRUPTED)
    events = await db(run_events, run)
    assert (events[-1].kind, events[-1].message) == ("run.failed", reaper.INTERRUPTED)


@tx
def test_a_killed_worker_leaves_a_stalled_job_that_the_restart_clears() -> None:
    """The state `kill -9` leaves: the run `running` in phase `refreshing`, its job `doing` on a
    worker whose heartbeat stopped. The restarted worker's startup sweep fails the job, and the
    next reap fails the run "Interrupted…" with a `run.failed` event."""
    make_company(name="Acme", slug="acme", ats_slug="acme")
    user = make_user()
    run = pipeline.start_refresh_stale(user.pk).run
    assert lifecycle.claim(run.id) is not None
    lifecycle.merge_stats(run.id, phase="refreshing")
    [job] = deferred("company.refresh")
    sql(
        "UPDATE procrastinate_jobs SET status = 'doing', worker_id = %s WHERE id = %s",
        [dead_worker(seconds_ago=300), job.id],
    )
    age_run(run.id, 120)
    assert reaper.reap_runs() == reaper.ReapResult(0, 0)  # its job still holds the run

    async def startup_sweep() -> list[int]:
        with app.replace_connector(worker_connector()):
            async with app.open_async():
                return await reaper.fail_stalled_jobs()

    assert asyncio.run(startup_sweep()) == [job.id]
    assert reaper.reap_runs() == reaper.ReapResult(1, 0)
    run = reload(run)
    assert (run.status, run.error) == ("failed", reaper.INTERRUPTED)
    assert event_kinds(run.id) == ["run.failed"]


# --- attribution ---------------------------------------------------------------------------------


@tx
def test_careers_page_extraction_is_billed_to_the_run_and_the_cron_to_nobody(
    web: Web, monkeypatch: pytest.MonkeyPatch
) -> None:
    listing = "https://acme.example.com/careers"
    web.page(listing, '<a href="/careers/1">Engineer</a><a href="/careers/2">Designer</a>')
    for n in (1, 2, 3):
        web.page(f"{listing}/{n}", f"<p>Posting {n}.</p>")

    def reply(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        answer = careers_listing_reply(messages, info)
        assert answer is not None
        return answer

    fake_models(monkeypatch, {"extract": FunctionModel(reply)})
    acme = make_company(
        name="Acme", slug="acme", source_type="careers_page", careers_urls=[listing]
    )
    user = make_user()

    run = pipeline.start_refresh(user.pk, "acme").run
    run_deferred()

    run = reload(run)
    assert run.output is not None
    assert (run.output["ok"], run.output["jobs"]) == (1, 2)
    calls = LlmCall.objects.unscoped("tests").filter(purpose="extract")
    assert [(call.user_id, call.run_id) for call in calls] == [(user.pk, run.id)]

    # The cron, after the listing changed: system work, billed to no user and no run (§8.8).
    web.page(listing, '<a href="/careers/1">Engineer</a><a href="/careers/3">Analyst</a>')
    Company.objects.filter(id=acme.id).update(last_fetched_at=None)
    defer_in_tx(job_tasks.companies_refresh_scheduled, timestamp=0)
    run_deferred()

    cron_calls = calls.exclude(run_id=run.id)
    assert [(call.user_id, call.run_id) for call in cron_calls] == [(None, None)]
    assert Run.objects.unscoped("tests").count() == 1
