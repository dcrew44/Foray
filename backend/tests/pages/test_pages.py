"""The page endpoints (design §5.3, §12.3): key-shape snapshots, query budgets and what each page
says, ported from the TypeScript pages (`map/next-frontend.md` §3).

- **Key shapes.** Each page's answer for a full user graph is reduced to its keys and JSON types
  and compared with `snapshots/<page>.json`. A change to a page's shape fails here first, and
  `pnpm check`'s type drift catches it on the frontend. Regenerate deliberately with
  `FORAY_UPDATE_SNAPSHOTS=1`.
- **Query budgets** (§5.3) count the endpoint's own queries. Every signed-in request also costs
  exactly two before the endpoint runs, the session row and the user row (pinned by
  `test_signed_in_request_costs_two_queries`), so a page is measured against its budget plus
  those two. With the database session engine nothing can avoid them, and the chat page's own
  two reads (the onboarding state, the top match) already take its budget of 3.
- **Wording.** Home's headline and suggestions, the chat page's suggestions and the activity
  page's run targets are today's sentences, verbatim.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext
from pydantic import SecretStr

from apps.accounts.profile import SLOTS, onboarding_state
from apps.accounts.schemas import SAMPLE_PREFERENCES
from apps.jobs import pipeline
from apps.matching.brief import describe_brief, parse_brief
from apps.matching.queries import list_matches
from apps.pages import build
from apps.pages.schemas import HomeWaiting, StageCounts
from apps.resumes.models import Resume
from apps.runs.models import Run
from tests.factories import (
    make_approval,
    make_company,
    make_job,
    make_llm_call,
    make_match,
    make_resume,
    make_run,
    make_user,
)
from tests.pages.conftest import Graph, full_graph, get_page, signed_in

pytestmark = pytest.mark.django_db

SNAPSHOTS = Path(__file__).resolve().parent / "snapshots"
UPDATE = os.environ.get("FORAY_UPDATE_SNAPSHOTS") == "1"

# §5.3: each page endpoint's own queries.
BUDGETS = {
    "home": 10,
    "matches": 5,
    "match": 5,
    "activity": 6,
    "chat": 3,
    "companies": 3,
    "profile": 5,
    "settings": 6,
}
# The session row and the user row, read before any endpoint runs.
AUTH_QUERIES = 2

PAGES = {
    "home": "/api/pages/home",
    "matches": "/api/pages/matches?closed=1",
    "match": "/api/pages/matches/{match}",
    "activity": "/api/pages/activity",
    "chat": "/api/pages/chat",
    "companies": "/api/pages/companies",
    "profile": "/api/pages/profile",
    "settings": "/api/pages/settings",
}


def shape(value: Any) -> Any:
    """Keys and JSON types only: objects keep their keys, arrays list their items' distinct
    shapes in order of appearance."""
    if isinstance(value, dict):
        return {key: shape(inner) for key, inner in value.items()}
    if isinstance(value, list):
        shapes: list[Any] = []
        for item in value:
            found = shape(item)
            if found not in shapes:
                shapes.append(found)
        return shapes
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    return "string"


def check_snapshot(name: str, body: Any) -> None:
    path = SNAPSHOTS / f"{name}.json"
    found = shape(body)
    if UPDATE:
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(found, indent=2, sort_keys=True) + "\n")
    assert path.exists(), f"no snapshot for {name}: run once with FORAY_UPDATE_SNAPSHOTS=1"
    assert found == json.loads(path.read_text()), f"the {name} page changed shape"


def page(graph: Graph, name: str, **query: str) -> dict[str, Any]:
    """The page as `graph.user` sees it, with exactly the query given."""
    path = PAGES[name].split("?")[0].format(match=graph.matches["found"].id)
    if query:
        path += "?" + "&".join(f"{key}={value}" for key, value in query.items())
    response = get_page(signed_in(graph.user), path)
    assert response.status_code == 200, response.content
    return response.json()


# --- shapes and budgets --------------------------------------------------------------------------


def test_signed_in_request_costs_two_queries() -> None:
    """What every authenticated request costs before its endpoint runs; `/api/me` itself reads
    nothing more."""
    client = signed_in(make_user())
    with CaptureQueriesContext(connection) as captured:
        response = get_page(client, "/api/me")
    assert response.status_code == 200
    sql = [query["sql"] for query in captured.captured_queries]
    assert len(sql) == AUTH_QUERIES, sql
    assert '"django_session"' in sql[0]
    assert '"users"' in sql[1]


@pytest.mark.parametrize("name", PAGES)
def test_page_shape_and_query_budget(name: str, django_assert_max_num_queries: Any) -> None:
    graph = full_graph()
    client = signed_in(graph.user)
    path = PAGES[name].format(match=graph.matches["found"].id)
    with django_assert_max_num_queries(BUDGETS[name] + AUTH_QUERIES):
        response = get_page(client, path)
    assert response.status_code == 200, response.content
    assert response["Cache-Control"] == "no-store"
    check_snapshot(name, response.json())


@pytest.mark.parametrize("name", [name for name in PAGES if name != "match"])
def test_pages_answer_a_new_user(name: str, django_assert_max_num_queries: Any) -> None:
    """A user with nothing yet: every page answers, within its budget."""
    client = signed_in(make_user())
    with django_assert_max_num_queries(BUDGETS[name] + AUTH_QUERIES):
        response = get_page(client, PAGES[name])
    assert response.status_code == 200, response.content


def test_pages_need_a_session() -> None:
    graph = full_graph()
    from django.test import Client

    for name in PAGES:
        response = Client().get(PAGES[name].format(match=graph.matches["found"].id))
        assert response.status_code == 401
        assert response.json() == {"ok": False, "message": "Not signed in."}


def test_snapshots_are_all_used() -> None:
    assert {path.stem for path in SNAPSHOTS.glob("*.json")} == set(PAGES)


def test_the_exported_schema_has_no_duplicate_keys(tmp_path: Path) -> None:
    """`openapi-typescript` refuses a duplicated JSON key, so `pnpm api:types` (and the pages'
    generated types) depend on this. A response added through `openapi_extra` under a string
    status ("200") once sat beside ninja's own 200 (deviations/step-7.md)."""
    out = tmp_path / "openapi.json"
    call_command("export_openapi_schema", api="foray.api.api", output=str(out), indent=2)
    duplicates: list[str] = []

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        keys = [key for key, _ in items]
        duplicates.extend(key for key in set(keys) if keys.count(key) > 1)
        return dict(items)

    schema = json.loads(out.read_text(), object_pairs_hook=pairs)
    assert duplicates == []
    assert {path for path in schema["paths"] if path.startswith("/api/pages/")} == {
        PAGES[name].split("?")[0].format(match="{id}") for name in PAGES
    }


# --- home ----------------------------------------------------------------------------------------


def test_home_page() -> None:
    graph = full_graph()
    body = page(graph, "home")
    assert body["user"] == {"name": "Avery Quinn"}
    onboarding = body["onboarding"]
    assert set(onboarding) == {
        "hasResume",
        "resume",
        "preferences",
        "progress",
        "settings",
        "ready",
    }
    assert onboarding["ready"] is True and onboarding["hasResume"] is True
    assert onboarding["preferences"] == SAMPLE_PREFERENCES.dump()  # snake_case keys, as stored
    assert onboarding["progress"]["total"] == 8
    assert onboarding["settings"] == {
        "defaultMode": "approve_each",
        "scoreThreshold": 55,
        "deepEvalTopN": 15,
    }
    assert body["counts"] == {
        "found": 1,
        "shortlisted": 1,
        "applied": 1,
        "interviewing": 0,
        "offer": 0,
        "rejected": 0,
        "archived": 1,
    }
    assert [row["matchId"] for row in body["best"]] == [
        str(graph.matches["found"].id),
        str(graph.matches["shortlisted"].id),
    ]
    assert body["spend"] == {"usd": pytest.approx(0.225), "calls": 3}
    assert body["waiting"] == {"approvals": 1, "running": 1}
    assert body["suggestions"] == [
        "Find new software engineer roles",
        "Which of my matches fit me best, and why?",
        "Tailor my resume for the Stripe role",  # the best match without a tailored resume
    ]
    assert body["headline"] == {
        "headline": "Avery, one decision is waiting on you.",
        "detail": None,
    }


def _headline(
    user: Any, *, approvals: int = 0, running: int = 0, first_name: str = "Avery"
) -> dict:
    onboarding = onboarding_state(user)
    from apps.matching.queries import count_by_stage

    counts = StageCounts(**count_by_stage(user.pk))
    best = list_matches(user.pk, stages=("found", "shortlisted"), limit=6)
    waiting = HomeWaiting(approvals=approvals, running=running)
    return build.describe_state(onboarding, counts, best, waiting, first_name).model_dump()


def test_home_headline_follows_describe_state() -> None:
    assert _headline(make_user()) == {
        "headline": "Let's set up your search.",
        "detail": "Start with your resume. Foray builds your profile from it, asks a few "
        "questions about what you want, then goes looking.",
    }
    halfway = make_user(with_resume=True, preferences={"target_roles": ["Engineer"]})
    assert _headline(halfway) == {
        "headline": "A few answers and Foray can start searching.",
        "detail": "Still needed: seniority, locations, remote preference, work authorisation. "
        "Answer in chat, or fill them in on your profile.",
    }
    ready = make_user(with_resume=True)
    assert _headline(ready, approvals=2)["headline"] == "Avery, 2 decisions are waiting on you."
    assert _headline(ready, approvals=1, first_name="")["headline"] == (
        "one decision is waiting on you."
    )
    assert _headline(ready, running=1) == {
        "headline": "Foray is searching right now.",
        "detail": "Results land below as they come in.",
    }
    assert _headline(ready) == {
        "headline": "Ready when you are.",
        "detail": "Ask for what you want below, or run a search with your saved preferences.",
    }
    make_match(ready, fast_score=60)
    make_match(ready, fast_score=58)
    make_match(ready, stage="shortlisted", fast_score=50)
    make_match(ready, stage="applied", fast_score=90)
    assert _headline(ready) == {
        "headline": "3 roles in your queue.",
        "detail": "1 shortlisted, 1 applied so far.",
    }
    make_match(ready, deep_score=85)
    assert _headline(ready)["headline"] == "1 strong match waiting for a look."


def test_home_headline_uses_the_first_word_of_the_name() -> None:
    user = make_user(with_resume=True, name="Avery Jo Quinn")
    make_approval(user)
    graph_body = get_page(signed_in(user), "/api/pages/home").json()
    assert graph_body["headline"]["headline"] == "Avery, one decision is waiting on you."


def test_plural_counts_like_to_locale_string() -> None:
    assert build.plural(1, "role") == "1 role"
    assert build.plural(1204, "role") == "1,204 roles"
    assert build.plural(2, "strong match", "strong matches") == "2 strong matches"


def test_home_suggestions_by_stage() -> None:
    fresh = make_user()
    assert get_page(signed_in(fresh), "/api/pages/home").json()["suggestions"] == []
    halfway = make_user(with_resume=True, preferences={})
    assert get_page(signed_in(halfway), "/api/pages/home").json()["suggestions"] == [
        "Help me set up what I'm looking for",
        "What does my resume say about me?",
    ]
    preferences = {**SAMPLE_PREFERENCES.dump(), "target_roles": ["Staff Engineer"]}
    ready = make_user(with_resume=True, preferences=preferences)
    assert get_page(signed_in(ready), "/api/pages/home").json()["suggestions"] == [
        "Find new staff engineer roles",
        "Which of my matches fit me best, and why?",
        "What should I improve on my resume?",
    ]


# --- matches -------------------------------------------------------------------------------------


def test_matches_page_views() -> None:
    graph = full_graph()
    ids = {stage: str(match.id) for stage, match in graph.matches.items()}

    board = page(graph, "matches")
    assert (board["view"], board["stage"], board["showClosed"], board["ready"]) == (
        "board",
        None,
        False,
        True,
    )
    assert [row["matchId"] for row in board["matches"]] == [
        ids["found"],
        ids["applied"],
        ids["shortlisted"],
    ]  # open stages only, best first (92, 75, 70)
    assert board["counts"]["archived"] == 1

    closed = page(graph, "matches", closed="1")
    assert (closed["view"], closed["showClosed"]) == ("board", True)
    assert len(closed["matches"]) == 4

    listed = page(graph, "matches", view="list")
    assert (listed["view"], listed["stage"], len(listed["matches"])) == ("list", None, 4)

    # A stage link from Home lands on the list, filtered by the client.
    staged = page(graph, "matches", stage="applied")
    assert (staged["view"], staged["stage"], len(staged["matches"])) == ("list", "applied", 4)

    bogus = page(graph, "matches", stage="nope", closed="yes")
    assert (bogus["view"], bogus["stage"], bogus["showClosed"]) == ("board", None, False)


def test_matches_page_is_not_ready_without_a_resume() -> None:
    body = get_page(signed_in(make_user()), "/api/pages/matches").json()
    assert body["ready"] is False
    assert body["matches"] == []


# --- one match -----------------------------------------------------------------------------------


def test_match_page() -> None:
    graph = full_graph()
    match = graph.matches["found"]
    body = page(graph, "match")
    assert body["hasBaseResume"] is True
    assert body["match"]["id"] == str(match.id)
    assert body["match"]["stage"] == "found"
    assert (body["match"]["fastScore"], body["match"]["deepScore"]) == (88, 92)
    assert body["match"]["deepEval"] == match.deep_eval  # stored keys, snake_case
    assert body["match"]["fastEval"] == match.fast_eval
    assert body["match"]["notes"] == "Avery replied."
    assert body["match"]["tailoredResumeId"] == str(graph.tailored.pk)
    job = body["job"]
    assert job["title"] == "Avery Backend Engineer"
    assert job["companyName"] == "Stripe"
    assert (job["salaryMin"], job["salaryMax"]) == (150000, 190000)  # numbers, not strings
    assert (job["salaryCurrency"], job["salaryPeriod"]) == ("USD", "year")
    assert job["source"] == "greenhouse"
    assert job["applyUrl"] == "https://stripe.example.com/apply/1"
    assert "raw" not in job and "descriptionRaw" not in job  # §5.2: never sent
    tailored = body["tailored"]
    assert tailored["id"] == str(graph.tailored.pk)
    assert tailored["documentId"] == str(graph.tailored_pdf.pk)
    assert tailored["changes"] == graph.tailored.changes
    assert tailored["verification"] == graph.tailored.verification


def test_match_page_without_extras_and_with_an_unreadable_evaluation() -> None:
    user = make_user()
    match = make_match(user, deep_score=50, deep_eval={"bogus": True}, fast_score=None)
    body = get_page(signed_in(user), f"/api/pages/matches/{match.id}").json()
    assert body["hasBaseResume"] is False
    assert body["tailored"] is None
    assert body["match"]["deepEval"] is None  # read leniently: unreadable is absent
    assert body["match"]["fastEval"] is None


def test_match_page_is_404_for_anything_not_yours() -> None:
    graph = full_graph()
    other = make_user()
    client = signed_in(other)
    for path in (
        f"/api/pages/matches/{graph.matches['found'].id}",
        f"/api/pages/matches/{uuid.uuid4()}",
        "/api/pages/matches/not-a-uuid",
    ):
        response = get_page(client, path)
        assert response.status_code == 404, path
        assert response.json() == {"ok": False, "message": "That match no longer exists."}


# --- activity ------------------------------------------------------------------------------------


def test_activity_page_targets_and_costs() -> None:
    graph = full_graph()
    body = page(graph, "activity")
    by_id = {summary["run"]["id"]: summary for summary in body["runs"]}
    order = [summary["run"]["id"] for summary in body["runs"]]
    runs = graph.runs
    assert order == [str(run.id) for run in reversed(list(runs.values()))]  # newest first

    def target(key: str) -> str | None:
        return by_id[str(runs[key].id)]["target"]

    search_summary = describe_brief(parse_brief({"roles": ["Backend Engineer"]}))
    assert search_summary is not None
    assert target("search") == search_summary
    assert target("waiting") == "Avery platform roles"  # no brief: the prompt
    assert target("rank_deep") == "Avery Backend Engineer at Stripe and 1 more"
    assert target("tailor") == "Avery Backend Engineer at Stripe"
    assert target("running") == "Avery Platform Engineer at Stripe"
    assert target("render_base") == "Your base resume"
    assert target("render_tailored") == "Tailored for Avery Backend Engineer at Stripe"
    assert target("import") == "Avery resume.pdf"
    assert target("add") == "Avery.example"
    assert target("names") == "Linear, Retool"
    assert target("refresh_one") == "Stripe"
    assert target("refresh_stale") == "Boards not fetched lately"

    assert by_id[str(runs["search"].id)]["costUsd"] == pytest.approx(0.012)
    assert by_id[str(runs["rank_deep"].id)]["costUsd"] == pytest.approx(0.21)
    assert by_id[str(runs["import"].id)]["costUsd"] == 0
    assert by_id[str(runs["search"].id)]["conversationId"] == str(graph.conversation.pk)
    assert by_id[str(runs["import"].id)]["conversationId"] is None
    assert by_id[str(runs["waiting"].id)]["run"]["status"] == "awaiting_approval"


def _target(kind: str, value: Any, roles: dict[str, str] | None = None, resumes: Any = None) -> Any:
    return build.run_target(Run(kind=kind, input=value), None, roles or {}, resumes or {})


def test_run_targets_for_missing_and_odd_inputs() -> None:
    known = str(uuid.uuid4())
    roles = {known: "Engineer at Acme"}
    # A missing key gives null, as today.
    for kind in (
        "search",
        "rank_deep",
        "tailor",
        "resume_render",
        "profile_import",
        "company_discovery",
        "company_refresh",
    ):
        assert _target(kind, {}) is None, kind
    assert _target("search", "not an object") is None
    assert _target("rank_deep", {"matchIds": [known]}, roles) == "Engineer at Acme"
    assert _target("rank_deep", {"matchIds": ["x"]}) == "1 role"
    assert _target("rank_deep", {"matchIds": ["x", "y", "z"]}) == "3 roles"
    assert _target("rank_deep", {"matchIds": [known, "y"]}, roles) == "Engineer at Acme and 1 more"
    assert _target("tailor", {"matchId": "unknown"}) is None
    tailored = {"r": {"kind": "tailored", "match_id": None}}
    assert _target("resume_render", {"resumeId": "r"}, resumes=tailored) == "A tailored resume"
    assert _target("company_discovery", {"names": ["A", 3, "B"]}) == "A, B"
    assert _target("company_discovery", {"query": "linear.app", "names": ["A"]}) == "linear.app"
    assert _target("company_refresh", {"slug": "stripe"}) is None  # no name stored
    assert _target("company_refresh", {"stale": False}) is None


def test_activity_page_reads_in_batches(django_assert_max_num_queries: Any) -> None:
    """Thirty runs that each point at a resume and a match cost no more reads than one."""
    graph = full_graph()
    for _ in range(30):
        make_run(
            graph.user,
            kind="resume_render",
            status="completed",
            input={"resumeId": str(graph.tailored.pk), "prompt": "Render"},
        )
    client = signed_in(graph.user)
    with django_assert_max_num_queries(BUDGETS["activity"] + AUTH_QUERIES):
        body = get_page(client, "/api/pages/activity").json()
    assert len(body["runs"]) == build.ACTIVITY_LIMIT
    assert {summary["target"] for summary in body["runs"]} == {
        "Tailored for Avery Backend Engineer at Stripe"
    }


# --- chat ----------------------------------------------------------------------------------------


def test_chat_page_suggestions() -> None:
    graph = full_graph()
    assert page(graph, "chat")["suggestions"] == [
        {
            "title": "Find new roles",
            "prompt": "Find new software engineer in San Francisco Bay Area for me.",
            "prefill": False,
        },
        {
            "title": "Compare my best matches",
            "prompt": "Which of my matches fit me best, and why? Be specific.",
            "prefill": False,
        },
        {
            "title": "Tailor my resume",
            "prompt": "Tailor my resume for the Avery Backend Engineer role at Stripe.",
            "prefill": False,
        },
        {
            "title": "Add a company",
            "prompt": "Start tracking this company for me: ",
            "prefill": True,
        },
    ]


def test_chat_page_suggestions_before_a_search() -> None:
    fresh = get_page(signed_in(make_user()), "/api/pages/chat").json()["suggestions"]
    assert [s["title"] for s in fresh] == ["How does this work?"]
    halfway = make_user(with_resume=True, preferences={})
    titles = [
        s["title"] for s in get_page(signed_in(halfway), "/api/pages/chat").json()["suggestions"]
    ]
    assert titles == ["Set up my search", "What's on my resume?"]
    ready = get_page(signed_in(make_user(with_resume=True)), "/api/pages/chat").json()
    assert ready["suggestions"][2] == {
        "title": "Improve my resume",
        "prompt": "What would make my resume stronger for the roles I want?",
        "prefill": False,
    }


# --- companies -----------------------------------------------------------------------------------


def test_companies_page() -> None:
    graph = full_graph()
    linear = graph.jobs["product"].company
    assert linear is not None
    linear.status = "paused"
    linear.save(update_fields=["status", "updated_at"])
    body = page(graph, "companies")
    assert [row["slug"] for row in body["companies"]] == ["linear", "stripe"]  # by name
    assert body["active"] == 1
    assert body["openPostings"] == 3  # Stripe 2, Linear 1 (its closed posting is not counted)
    # The same row the company endpoints answer with (step 5C's `company_row`).
    for row in body["companies"]:
        company = graph.company if row["slug"] == "stripe" else linear
        assert row == json.loads(json.dumps(pipeline.row_for(company.id)))


def test_companies_page_is_shared() -> None:
    full_graph()
    other = get_page(signed_in(make_user()), "/api/pages/companies").json()
    assert [row["slug"] for row in other["companies"]] == ["linear", "stripe"]


# --- profile -------------------------------------------------------------------------------------


def test_profile_page() -> None:
    graph = full_graph()
    body = page(graph, "profile")
    assert set(body) == {
        "hasResume",
        "ready",
        "resume",
        "preferences",
        "progress",
        "slots",
        "base",
        "render",
    }
    assert (body["hasResume"], body["ready"]) == (True, True)
    assert body["resume"]["basics"]["name"]
    assert body["preferences"] == SAMPLE_PREFERENCES.dump()
    assert body["slots"] == [slot.model_dump(by_alias=True) for slot in SLOTS]
    assert body["progress"]["complete"] is True
    assert body["base"]["version"] == 1
    assert body["render"] == {"documentId": str(graph.base_pdf.pk), "version": 1}


def test_profile_page_without_a_resume() -> None:
    body = get_page(signed_in(make_user()), "/api/pages/profile").json()
    assert (body["hasResume"], body["ready"], body["resume"]) == (False, False, None)
    assert (body["base"], body["render"]) == (None, None)
    assert set(body["preferences"]) == set(SAMPLE_PREFERENCES.dump())
    assert all(value is None for value in body["preferences"].values())
    assert len(body["slots"]) == 8
    assert [slot["id"] for slot in body["progress"]["blocking"]] == [
        "target_roles",
        "seniority",
        "locations",
        "remote_pref",
        "work_auth",
    ]


def test_profile_page_with_an_unreadable_or_newer_base() -> None:
    graph = full_graph()
    make_resume(graph.user, data={"work": "not a list"})  # version 2, unrendered and unreadable
    body = page(graph, "profile")
    assert body["base"]["version"] == 2  # the newest version, whether or not it reads
    assert body["resume"] is None and body["hasResume"] is False
    assert body["render"] == {"documentId": str(graph.base_pdf.pk), "version": 1}
    assert Resume.objects.for_user(graph.user.pk).filter(kind="base").count() == 2


# --- settings ------------------------------------------------------------------------------------


def test_settings_page() -> None:
    graph = full_graph()
    body = page(graph, "settings")
    assert body["user"] == {"name": "Avery Quinn", "email": graph.user.email}
    assert body["settings"] == {
        "defaultMode": "approve_each",
        "scoreThreshold": 55,
        "deepEvalTopN": 15,
    }
    assert body["spend"] == {
        "usd": pytest.approx(0.225),
        "calls": 3,
        "inputTokens": 3600,
        "outputTokens": 450,
    }
    assert [(row["purpose"], row["calls"]) for row in body["byPurpose"]] == [
        ("evaluate", 1),
        ("score", 1),
        ("chat", 1),
    ]
    assert [
        (row["model"], row["calls"], row["failures"], row["avgLatencyMs"])
        for row in body["byModel"]
    ] == [
        ("gpt-5.6-terra", 1, 0, 12000),
        ("gpt-5.6-luna", 1, 0, 900),
        ("avery-chat-model", 1, 1, 900),
    ]
    today = datetime.now(UTC).date().isoformat()
    assert body["byDay"] == [{"day": today, "usd": pytest.approx(0.225)}]
    assert body["monthStart"] == datetime.now(UTC).strftime("%Y-%m-01T00:00:00Z")
    assert body["aggregator"] == {"used": 0, "budget": 180, "enabled": False}
    assert [row["purpose"] for row in body["purposes"]] == [
        "chat",
        "extract",
        "score",
        "evaluate",
        "tailor",
        "verify",
        "merge",
    ]
    evaluate = next(row for row in body["purposes"] if row["purpose"] == "evaluate")
    assert evaluate == {
        "purpose": "evaluate",
        "ref": "openai:gpt-5.6-terra",
        "error": None,
        "reasoning": "medium",
        "rate": {"input": 2, "output": 12},
    }
    assert body["review"]["model"] == "openai:gpt-5.6-terra"
    assert body["review"]["perItemUsd"] > 0
    assert body["providers"] == {"present": ["openai", "google", "anthropic", "deepseek"]}


def test_settings_page_shows_key_presence_only(settings: Any) -> None:
    """Invariant 1: keys never leave the server; the page says only which are set."""
    secrets = {
        "openai_api_key": "sk-openai-NEVER-SENT",
        "anthropic_api_key": "sk-anthropic-NEVER-SENT",
        "deepseek_api_key": "sk-deepseek-NEVER-SENT",
        "google_generative_ai_api_key": "google-NEVER-SENT",
        "rapidapi_key": "rapid-NEVER-SENT",
    }
    settings.ENV = settings.ENV.model_copy(
        update={name: SecretStr(value) for name, value in secrets.items()}
    )
    response = get_page(signed_in(make_user()), "/api/pages/settings")
    assert response.status_code == 200
    assert "NEVER-SENT" not in response.content.decode()
    assert response.json()["providers"]["present"] == ["openai", "google", "anthropic", "deepseek"]


def test_settings_page_counts_only_this_users_month() -> None:
    graph = full_graph()
    make_llm_call(None, purpose="score")  # the cron's: no user
    other = make_user()
    make_llm_call(other, purpose="chat")
    body = page(graph, "settings")
    assert body["spend"]["calls"] == 3


def test_home_page_counts_only_this_users_month() -> None:
    """Home's spend line is the signed-in user's ledger rows of this UTC month: not the cron's
    (no user), not another user's, not last month's (§5.3)."""
    graph = full_graph()
    make_llm_call(None, purpose="score", cost_usd=Decimal("1.5"))  # the cron's: no user
    other = make_user()
    make_llm_call(other, purpose="chat", cost_usd=Decimal("2.5"))
    make_llm_call(
        graph.user,
        purpose="chat",
        cost_usd=Decimal("4"),
        created_at=build.month_start() - timedelta(seconds=1),  # last month
    )
    body = page(graph, "home")
    assert body["spend"] == {"usd": pytest.approx(0.225), "calls": 3}


def test_new_company_jobs_do_not_leak_into_matches() -> None:
    """Companies and postings are shared; matches are not."""
    graph = full_graph()
    make_job(company=make_company(name="Acme"), title="Unmatched role")
    body = page(graph, "matches", closed="1")
    assert "Unmatched role" not in json.dumps(body)
