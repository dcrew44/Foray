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
from django.db.models.functions import Now
from django.test.utils import CaptureQueriesContext
from pydantic import SecretStr

from apps.accounts.profile import SLOTS, onboarding_state
from apps.accounts.schemas import SAMPLE_PREFERENCES
from apps.jobs import pipeline
from apps.jobs.models import Company, Job
from apps.matching.brief import describe_brief, parse_brief
from apps.matching.queries import list_matches
from apps.pages import build
from apps.pages.schemas import HomeWaiting, StageCounts
from apps.resumes.models import Resume
from apps.runs.models import Run
from tests.factories import (
    make_approval,
    make_company,
    make_document,
    make_job,
    make_llm_call,
    make_match,
    make_resume,
    make_run,
    make_user,
    make_variant,
    sample_deep_eval,
    sample_fast_eval,
)
from tests.pages.conftest import Graph, full_graph, get_page, signed_in

pytestmark = pytest.mark.django_db

SNAPSHOTS = Path(__file__).resolve().parent / "snapshots"
UPDATE = os.environ.get("FORAY_UPDATE_SNAPSHOTS") == "1"

# §5.3: each page endpoint's own queries.
BUDGETS = {
    "home": 10,
    "matches": 5,
    # Four of the five are spent: the match with its job, its tailored resume, whether a base
    # resume exists, and (#7) the one read behind the posting's signals, pinned exactly by
    # `test_match_page_reads_once_for_its_signals`.
    "match": 5,
    "activity": 6,
    "chat": 3,
    "companies": 3,
    # The contacts, the registry, the open postings of the companies shown, the lookups (#63).
    "contacts": 4,
    # One more than before #21 (the saved answers), and two more for #61: the resume variants,
    # and the newest rendered snapshot of each (not read when there are no variants).
    "profile": 8,
    "apply": 2,
    # One for each part (#65, #6): the spans, the buckets, the grouped count behind the funnel.
    "outcomes": 3,
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
    "contacts": "/api/pages/contacts?q=engineer",
    "profile": "/api/pages/profile",
    "settings": "/api/pages/settings",
    "apply": "/api/pages/apply",
    "outcomes": "/api/pages/outcomes",
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
        "watchEnabled": True,
        "watchDailyReviews": 10,
        "ntfyTopic": "",
        "quietHoursStart": "22:00",
        "quietHoursEnd": "08:00",
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
    # Reviewed strong or apply this week and still waiting: the shortlisted one has no review.
    assert [row["matchId"] for row in body["newThisWeek"]] == [str(graph.matches["found"].id)]
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

    bogus = page(graph, "matches", stage="nope", closed="yes", lane="vip")
    assert (bogus["view"], bogus["stage"], bogus["showClosed"]) == ("board", None, False)
    # A lane that is not one narrows nothing, like a stage that is not one.
    assert (board["lane"], bogus["lane"], len(bogus["matches"])) == (None, None, 3)


def test_matches_page_is_not_ready_without_a_resume() -> None:
    body = get_page(signed_in(make_user()), "/api/pages/matches").json()
    assert body["ready"] is False
    assert body["matches"] == []


def test_matches_page_rows_lead_with_the_verdict(django_assert_max_num_queries: Any) -> None:
    """Every row says its verdict, one line of why and any blocker (#38), reviewed or not, from
    the evaluations the row query already reads: the page's budget does not move."""
    user = make_user(with_resume=True)
    review = {
        **sample_deep_eval(40),
        "summary": "The stack fits. The role needs a clearance you do not hold.",
        "gaps": [
            {"gap": "Kafka", "severity": "learnable", "note": "Named once."},
            {"gap": "Active clearance", "severity": "disqualifying", "note": "Required."},
        ],
    }
    triage = {**sample_fast_eval(58), "hard_requirement_misses": ["Requires 8+ years"]}
    reviewed = make_match(user, fast_score=88, deep_score=92)
    blocked = make_match(user, fast_score=70, deep_score=40, deep_eval=review, stage="applied")
    fast_only = make_match(user, fast_score=75)
    fast_blocked = make_match(user, fast_score=58, fast_eval=triage)
    client = signed_in(user)
    for view in ("board", "list"):
        with django_assert_max_num_queries(BUDGETS["matches"] + AUTH_QUERIES):
            body = get_page(client, f"/api/pages/matches?view={view}").json()
        rows = {row["matchId"]: row for row in body["matches"]}
        assert {
            name: tuple(rows[str(match.id)][key] for key in ("recommendation", "reason", "blocker"))
            for name, match in (
                ("reviewed", reviewed),
                ("blocked", blocked),
                ("fast only", fast_only),
                ("fast blocked", fast_blocked),
            )
        } == {
            "reviewed": ("strong", "A close fit for a backend role on a payments team.", None),
            "blocked": ("skip", "The stack fits.", "Active clearance"),
            "fast only": (None, "Backend work in Python", None),
            "fast blocked": (None, "Backend work in Python", "Requires 8+ years"),
        }, view


def test_matches_page_filters_by_lane(django_assert_max_num_queries: Any) -> None:
    """Every row says its apply lane (#39), and `lane=` narrows the board and the list to one,
    in the read the page already makes: its budget does not move. The counts stay the totals."""
    user = make_user(with_resume=True)
    made = {
        "strong": make_match(user, fast_score=88, deep_score=92),
        "apply": make_match(user, fast_score=80, deep_score=72, stage="shortlisted"),
        "maybe": make_match(user, fast_score=70, deep_score=55),
        "fast only": make_match(user, fast_score=75),
        "strong, taken out": make_match(user, fast_score=85, deep_score=90, lane_override="none"),
        "maybe, coached": make_match(user, fast_score=60, deep_score=50, lane_override="coached"),
        "fast only, automated": make_match(user, fast_score=65, lane_override="automated"),
        "apply, archived": make_match(user, fast_score=66, deep_score=70, stage="archived"),
    }
    names = {str(match.id): name for name, match in made.items()}
    lanes = {
        "coached": {"strong", "maybe, coached"},
        "automated": {"apply", "fast only, automated", "apply, archived"},
        "none": {"maybe", "fast only", "strong, taken out"},
    }
    client = signed_in(user)

    def read(**query: str) -> dict[str, Any]:
        path = "/api/pages/matches?" + "&".join(f"{key}={value}" for key, value in query.items())
        with django_assert_max_num_queries(BUDGETS["matches"] + AUTH_QUERIES):
            response = get_page(client, path)
        assert response.status_code == 200, response.content
        return response.json()

    everything = read(view="list")
    assert everything["lane"] is None
    assert {names[row["matchId"]]: row["lane"] for row in everything["matches"]} == {
        name: lane for lane, found in lanes.items() for name in found
    }
    for lane, found in lanes.items():
        listed = read(view="list", lane=lane)
        assert (listed["view"], listed["lane"]) == ("list", lane)
        assert {names[row["matchId"]] for row in listed["matches"]} == found
        assert {row["lane"] for row in listed["matches"]} == {lane}
        # The board shows open matches only, in the same lane; its closed ones on request.
        board = read(lane=lane)
        assert (board["view"], board["lane"], board["showClosed"]) == ("board", lane, False)
        assert {names[row["matchId"]] for row in board["matches"]} == found - {"apply, archived"}
        with_closed = read(lane=lane, closed="1")
        assert {names[row["matchId"]] for row in with_closed["matches"]} == found
        assert listed["counts"] == board["counts"] == everything["counts"]
    # A stage link keeps its lane: the list, narrowed by Django to the lane and by the client
    # to the stage.
    staged = read(stage="shortlisted", lane="automated")
    assert (staged["view"], staged["stage"], staged["lane"]) == ("list", "shortlisted", "automated")
    assert {names[row["matchId"]] for row in staged["matches"]} == lanes["automated"]


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
    # A strong review, and the user has not moved it: the coached lane, from the review (#39).
    assert [body["match"][key] for key in ("lane", "laneOverride", "reviewLane")] == [
        "coached",
        None,
        "coached",
    ]
    assert body["match"]["tailoredResumeId"] == str(graph.tailored.pk)
    job = body["job"]
    assert job["title"] == "Avery Backend Engineer"
    assert job["companyName"] == "Stripe"
    assert (job["salaryMin"], job["salaryMax"]) == (150000, 190000)  # numbers, not strings
    assert (job["salaryCurrency"], job["salaryPeriod"]) == ("USD", "year")
    assert job["source"] == "greenhouse"
    assert job["applyUrl"] == "https://stripe.example.com/apply/1"
    assert "raw" not in job and "descriptionRaw" not in job  # §5.2: never sent
    assert job["signals"] == []  # an ordinary posting: open, new, posted once (#7)
    # What the posting asks (#56): nothing read yet for this one.
    assert job["facts"] == {
        "read": False,
        "level": "unknown",
        "levelFrom": None,
        "yearsMin": None,
        "yearsPreferred": None,
        "workAuth": "unknown",
        "clearance": "none",
        "yearsEvidence": None,
        "workAuthEvidence": None,
        "clearanceEvidence": None,
    }
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


def test_match_page_leads_with_the_rows_verdict(django_assert_max_num_queries: Any) -> None:
    """The page's reason and blocker are its row's (#38), reviewed or not, within the budget."""
    user = make_user()
    review = {
        **sample_deep_eval(45),
        "summary": "Close on the stack. The degree is a hard requirement.",
        "gaps": [{"gap": "A PhD in statistics", "severity": "disqualifying", "note": "A must."}],
    }
    triage = {**sample_fast_eval(40), "hard_requirement_misses": ["Requires 8+ years"]}
    reviewed = make_match(user, fast_score=70, deep_score=45, deep_eval=review, fast_eval=triage)
    fast_only = make_match(user, fast_score=40, fast_eval=triage)
    plain = make_match(user, fast_score=72)
    unscored = make_match(user)
    client = signed_in(user)
    rows = {row.match_id: row for row in list_matches(user.pk)}
    for match, lead in (
        (reviewed, ("Close on the stack.", "A PhD in statistics")),
        (fast_only, ("Backend work in Python", "Requires 8+ years")),
        (plain, ("Backend work in Python", None)),
        (unscored, (None, None)),
    ):
        with django_assert_max_num_queries(BUDGETS["match"] + AUTH_QUERIES):
            body = get_page(client, f"/api/pages/matches/{match.id}").json()["match"]
        assert (body["reason"], body["blocker"]) == lead
        assert (rows[match.id].reason, rows[match.id].blocker) == lead


def test_match_page_says_its_lane_as_its_row_does(django_assert_max_num_queries: Any) -> None:
    """The page's lane is its row's (#39), with what the user chose and what the review alone
    gives beside it, so the page can offer the way back. Within the budget."""
    user = make_user()
    cases = [
        (make_match(user, fast_score=80, deep_score=90), ("coached", None, "coached")),
        (make_match(user, fast_score=80, deep_score=72), ("automated", None, "automated")),
        (make_match(user, fast_score=80, deep_score=50), ("none", None, "none")),
        (make_match(user, fast_score=80), ("none", None, "none")),
        (make_match(user), ("none", None, "none")),
        (
            make_match(user, fast_score=80, deep_score=90, lane_override="automated"),
            ("automated", "automated", "coached"),
        ),
        (
            make_match(user, fast_score=80, deep_score=72, lane_override="none"),
            ("none", "none", "automated"),
        ),
        (make_match(user, fast_score=80, lane_override="coached"), ("coached", "coached", "none")),
        (
            make_match(user, deep_score=50, deep_eval={"bogus": True}, lane_override="coached"),
            ("coached", "coached", "none"),
        ),
    ]
    client = signed_in(user)
    rows = {row.match_id: row for row in list_matches(user.pk)}
    for match, lanes in cases:
        with django_assert_max_num_queries(BUDGETS["match"] + AUTH_QUERIES):
            body = get_page(client, f"/api/pages/matches/{match.id}").json()["match"]
        assert (body["lane"], body["laneOverride"], body["reviewLane"]) == lanes
        assert rows[match.id].lane == lanes[0]


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
    assert target("render_variant") == "Your Avery Backend variant"
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
    variant = {"r": {"kind": "variant", "match_id": None, "variant__name": None}}
    assert _target("resume_render", {"resumeId": "r"}, resumes=variant) == "A resume variant"
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


def test_match_page_shows_what_the_posting_asks() -> None:
    graph = full_graph()
    Job.objects.filter(id=graph.jobs["backend"].id).update(
        level="mid",
        years_min=3,
        work_auth="no_sponsorship",
        clearance="eligible",
        facts_version=1,
        facts={
            "levelFrom": "years",
            "evidence": {
                "years": "3+ years of backend experience",
                "workAuth": "We are unable to sponsor visas.",
                "clearance": "Ability to obtain a Secret security clearance.",
            },
        },
    )
    facts = page(graph, "match")["job"]["facts"]
    assert facts == {
        "read": True,
        "level": "mid",
        "levelFrom": "years",
        "yearsMin": 3,
        "yearsPreferred": None,
        "workAuth": "no_sponsorship",
        "clearance": "eligible",
        "yearsEvidence": "3+ years of backend experience",
        "workAuthEvidence": "We are unable to sponsor visas.",
        "clearanceEvidence": "Ability to obtain a Secret security clearance.",
    }


def test_match_page_shows_a_closed_and_reposted_posting() -> None:
    """The legitimacy signals (#7, `jobs/legitimacy.py`), closed first."""
    graph = full_graph()
    job = graph.jobs["backend"]
    now = datetime.now(UTC)
    # The role's third posting in the window; this one has closed since.
    for _ in range(2):
        make_job(company=graph.company, title=job.title, posted_at=now, is_active=False)
    Job.objects.filter(id=job.id).update(is_active=False, posted_at=now, last_seen_at=now)

    body = page(graph, "match")
    assert body["job"]["isActive"] is False
    assert body["job"]["signals"] == [
        {
            "kind": "closed",
            "label": "Closed",
            "detail": f"Last seen on the employer's board on {now:%B} {now.day}.",
        },
        {"kind": "reposted", "label": "Reposted", "detail": f"Posted 3 times since {now:%B}."},
    ]


def test_match_page_shows_an_aggregators_stale_posting_that_applies_elsewhere() -> None:
    user = make_user()
    old = datetime.now(UTC) - timedelta(days=74)
    listed = make_job(
        source="jsearch",
        company=make_company(name="TechNova Solutions"),
        apply_url="https://unlockthegenius.netlify.app/?jobs=en-job-entry-level-developer",
        posted_at=old,
        first_seen_at=old,
        raw={"job_id": "abc", "employer_website": "http://www.technovasolutions.com"},
    )
    Job.objects.filter(id=listed.id).update(company=None)  # as the aggregator's postings are
    match = make_match(user, job=listed)

    job = get_page(signed_in(user), f"/api/pages/matches/{match.id}").json()["job"]
    assert job["signals"] == [
        {
            "kind": "aggregator_link",
            "label": "Apply link goes elsewhere",
            "detail": (
                "The apply link goes to unlockthegenius.netlify.app, not the employer's site."
            ),
        },
        {"kind": "long_open", "label": "Open a long time", "detail": "Open for 74 days."},
    ]

    # The same posting applying at the employer's own site, 60 days in: nothing to say.
    Job.objects.filter(id=listed.id).update(
        apply_url="https://careers.technovasolutions.com/jobs/42",
        posted_at=datetime.now(UTC) - timedelta(days=60),
    )
    job = get_page(signed_in(user), f"/api/pages/matches/{match.id}").json()["job"]
    assert job["signals"] == []


def test_match_page_reads_once_for_its_signals(django_assert_num_queries: Any) -> None:
    """The match page's own reads, exactly: the match with its job, the tailored resume, the
    base resume check, and one for the signals (the repost count, the company's domain and the
    database's present together), however many signals there are."""
    graph = full_graph()
    job = graph.jobs["backend"]
    for _ in range(3):
        make_job(company=graph.company, title=job.title, is_active=False)
    Job.objects.filter(id=job.id).update(is_active=False)
    client = signed_in(graph.user)
    with django_assert_num_queries(4 + AUTH_QUERIES):
        body = get_page(client, f"/api/pages/matches/{graph.matches['found'].id}").json()
    assert [signal["kind"] for signal in body["job"]["signals"]] == ["closed", "reposted"]


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


def test_companies_page_reports_the_radar() -> None:
    graph = full_graph()
    empty = page(graph, "companies")["radar"]
    assert (empty["postings"], empty["medianS"], empty["p90S"], empty["bySource"]) == (
        0,
        None,
        None,
        [],
    )
    assert (empty["days"], empty["hotBoards"], empty["coldBoards"]) == (7, 0, 2)

    Job.objects.filter(company=graph.company).update(seen_lag_s=240)
    Company.objects.filter(id=graph.company.id).update(last_relevant_at=Now(), last_ok_at=Now())
    radar = page(graph, "companies")["radar"]
    measured = Job.objects.filter(company=graph.company).count()
    assert (radar["postings"], radar["medianS"], radar["p90S"]) == (measured, 240, 240)
    assert radar["bySource"] == [
        {"source": "greenhouse", "postings": measured, "medianS": 240, "p90S": 240}
    ]
    assert (radar["hotBoards"], radar["coldBoards"], radar["careersPages"]) == (1, 1, 0)
    assert radar["lastCheckedAt"] is not None


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
        "answers",
        "variants",
        "variantLimit",
    }
    assert (body["hasResume"], body["ready"]) == (True, True)
    assert body["resume"]["basics"]["name"]
    assert body["preferences"] == SAMPLE_PREFERENCES.dump()
    assert body["slots"] == [slot.model_dump(by_alias=True) for slot in SLOTS]
    assert body["progress"]["complete"] is True
    assert body["base"]["version"] == 1
    assert body["render"] == {"documentId": str(graph.base_pdf.pk), "version": 1}
    [variant] = body["variants"]
    assert variant == {
        "id": str(graph.variant.pk),
        "name": "Avery Backend",
        "keywords": ["backend", "platform"],
        "isDefault": False,
        "spec": graph.variant.spec,
        # Its snapshot is what it composes to from the newest base, so the PDF is current.
        "pdf": {"documentId": str(graph.variant_pdf.pk), "current": True},
        "updatedAt": variant["updatedAt"],
    }
    assert [item["id"] for item in variant["spec"]["work"]] == [
        item["id"] for item in body["resume"]["work"]
    ]
    assert body["variantLimit"] == 6


def test_profile_page_reads_any_number_of_variants_in_two_queries(
    django_assert_max_num_queries: Any,
) -> None:
    """Six variants (the most a user may keep), each with two rendered snapshots, cost the page
    no more reads than one: the newest PDF of each comes back in one query."""
    graph = full_graph()
    newest: dict[str, str] = {str(graph.variant.pk): str(graph.variant_pdf.pk)}
    for name in ("Data", "Platform", "Frontend", "Mobile", "Security"):
        variant = make_variant(graph.user, name=name, keywords=[name.lower()])
        for _ in range(2):
            pdf = make_document(
                graph.user, kind="resume_pdf", filename=f"Avery Quinn - Resume - {name}.pdf"
            )
            make_resume(graph.user, kind="variant", variant=variant, rendered_document=pdf)
            newest[str(variant.pk)] = str(pdf.pk)
    # A newer snapshot that was never printed does not hide the PDF of the one before it.
    make_resume(graph.user, kind="variant", variant=graph.variant, data={"work": []})
    client = signed_in(graph.user)
    with django_assert_max_num_queries(BUDGETS["profile"] + AUTH_QUERIES):
        body = get_page(client, "/api/pages/profile").json()
    assert [item["name"] for item in body["variants"]] == [
        "Avery Backend",
        "Data",
        "Platform",
        "Frontend",
        "Mobile",
        "Security",
    ]  # oldest first
    assert {item["id"]: item["pdf"]["documentId"] for item in body["variants"]} == newest
    assert all(item["pdf"]["current"] for item in body["variants"])


def test_profile_page_without_a_resume() -> None:
    body = get_page(signed_in(make_user()), "/api/pages/profile").json()
    assert (body["hasResume"], body["ready"], body["resume"]) == (False, False, None)
    assert (body["base"], body["render"], body["variants"]) == (None, None, [])
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
        "watchEnabled": True,
        "watchDailyReviews": 10,
        "ntfyTopic": "",
        "quietHoursStart": "22:00",
        "quietHoursEnd": "08:00",
        "timeZone": "UTC",  # `users.timezone`: the clock quiet hours are read on
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
    assert body["ntfyServer"] == "https://ntfy.sh"  # where the phone subscribes (#59)


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
