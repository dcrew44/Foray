"""The matching endpoints (design §5.2, §5.3): `POST /api/searches` with today's rules in order
(gate, inputs, one active search, start), and `/api/matches*` with today's messages.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from apps.accounts.schemas import Settings
from apps.core import throttle
from apps.matching.models import JobMatch
from apps.runs.models import Run
from foray.api import api as ninja_api
from tests.factories import make_job, make_match, make_run, make_user, sample_deep_eval
from tests.queue import deferred

pytestmark = pytest.mark.django_db

GATE = (
    "Foray still needs your target roles, seniority, locations, remote preference, work "
    "authorisation before it can search. Add them on your profile or tell Foray in chat."
)
RUNNING = "A search is already running. It will finish in a minute or two."
WAITING = "A search is already waiting on your go-ahead. Answer it on Home first."
STARTED = "Search started. Foray will check with you before the deep evaluation."


def runs_of(user: Any) -> list[Run]:
    return list(Run.objects.for_user(user.pk).order_by("created_at"))


# --- POST /api/searches --------------------------------------------------------------------------


def test_the_gate_names_what_is_missing(signed_in: Any) -> None:
    api, user = signed_in()
    answer = api.post("/api/searches", {})
    assert answer.status_code == 422
    assert answer.json() == {"ok": False, "message": GATE}
    assert runs_of(user) == [] and deferred() == []


def test_starts_a_search_and_a_second_one_is_refused_with_its_id(signed_in: Any) -> None:
    api, user = signed_in(with_resume=True)
    answer = api.post("/api/searches", {})
    assert answer.status_code == 202
    [run] = runs_of(user)
    assert answer.json() == {"ok": True, "runId": str(run.id), "message": STARTED}
    assert (run.kind, run.status, run.mode) == ("search", "queued", "approve_each")
    assert run.input == {"prompt": "Search with my saved preferences", "hints": None}
    assert run.dedupe_key == "search" and run.conversation_id is None
    [job] = deferred("search.run")
    assert job.args == {"run_id": str(run.id)}
    assert (job.queue, job.lock, job.queueing_lock) == (
        "background",
        f"run:{run.id}",
        f"run:{run.id}",
    )

    again = api.post("/api/searches", {"prompt": "something else"})
    assert again.status_code == 409
    assert again.json() == {"ok": False, "message": RUNNING, "runId": str(run.id)}
    Run.objects.for_user(user.pk).filter(id=run.id).update(status="awaiting_approval")
    waiting = api.post("/api/searches", {})
    assert waiting.status_code == 409
    assert waiting.json()["message"] == WAITING
    assert len(runs_of(user)) == 1 and len(deferred()) == 1

    # Once it has finished, the next search starts.
    Run.objects.for_user(user.pk).filter(id=run.id).update(status="completed")
    assert api.post("/api/searches", {}).status_code == 202


def test_prompt_and_mode(signed_in: Any) -> None:
    api, user = signed_in(with_resume=True)
    answer = api.post("/api/searches", {"prompt": "  roles at Acme  ", "mode": "autopilot"})
    assert answer.json()["message"] == "Search started on autopilot."
    [run] = runs_of(user)
    assert (run.input["prompt"], run.mode) == ("roles at Acme", "autopilot")

    # Without a mode, the user's default from settings.
    other_api, other = signed_in(
        with_resume=True, settings=Settings(default_mode="autopilot").dump()
    )
    assert other_api.post("/api/searches", {}).json()["message"] == "Search started on autopilot."
    assert runs_of(other)[0].mode == "autopilot"


@pytest.mark.parametrize(
    "body",
    [{"mode": "yolo"}, {"prompt": 5}, {"againFrom": []}, {"againFrom": 7}, "not json"],
)
def test_an_unreadable_request_is_a_400(signed_in: Any, body: Any) -> None:
    api, user = signed_in(with_resume=True)
    answer = api.post("/api/searches", body if body != "not json" else "{")
    assert answer.status_code == 400
    assert answer.json()["ok"] is False
    if body != "not json":
        assert answer.json()["message"] == "That request could not be read."
    assert runs_of(user) == []


def test_again_from_repeats_an_earlier_search(signed_in: Any) -> None:
    api, user = signed_in(with_resume=True)
    hints = {"companies": ["Acme"], "seniority": "entry"}
    earlier = make_run(
        user,
        kind="search",
        status="failed",
        mode="autopilot",
        input={
            "prompt": "entry roles at Acme",
            "hints": hints,
            "brief": {"companies": ["Acme"]},
            "criteria": {"title_include": ["x"], "title_exclude": [], "keywords": []},
        },
    )
    answer = api.post("/api/searches", {"againFrom": str(earlier.id)})
    assert answer.status_code == 202
    run = Run.objects.for_user(user.pk).get(id=answer.json()["runId"])
    # The request and hints, not the brief: it is derived again from the current profile.
    assert run.input == {"prompt": "entry roles at Acme", "hints": hints}
    assert run.mode == "autopilot"


@pytest.mark.parametrize("which", ["not a search", "another user's", "garbage"])
def test_again_from_ignores_anything_but_the_users_own_search(signed_in: Any, which: str) -> None:
    api, user = signed_in(with_resume=True)
    if which == "not a search":
        target = str(make_run(user, kind="tailor", status="completed").id)
    elif which == "another user's":
        target = str(make_run(make_user(), kind="search", status="completed").id)
    else:
        target = "run-7"
    answer = api.post("/api/searches", {"againFrom": target})
    assert answer.status_code == 202
    run = Run.objects.for_user(user.pk).get(id=answer.json()["runId"])
    assert run.input == {"prompt": "Search with my saved preferences", "hints": None}
    assert run.mode == "approve_each"


# --- GET /api/matches ----------------------------------------------------------------------------

ROW_KEYS = {
    "matchId",
    "jobId",
    "stage",
    "stageChangedAt",
    "fastScore",
    "deepScore",
    "score",
    "recommendation",
    "headline",
    "title",
    "company",
    "locations",
    "remote",
    "salaryMin",
    "salaryMax",
    "salaryCurrency",
    "salaryPeriod",
    "postedAt",
    "url",
    "applyUrl",
    "isActive",
    "tailoredResumeId",
    "userRating",
    "createdAt",
}


def test_matches_by_id_best_first_own_only(signed_in: Any) -> None:
    api, user = signed_in()
    fast = make_match(user, fast_score=70, job=make_job(salary_min=150000.0, salary_max=None))
    deep = make_match(user, fast_score=60, deep_score=91)
    theirs = make_match(make_user(), fast_score=99)
    query = ",".join([str(fast.id), " junk ", str(theirs.id), f" {deep.id} ", "1234"])
    answer = api.get(f"/api/matches?ids={query}")
    assert answer.status_code == 200 and answer["Cache-Control"] == "no-store"
    rows = answer.json()["matches"]
    assert [row["matchId"] for row in rows] == [str(deep.id), str(fast.id)]
    assert set(rows[0]) == ROW_KEYS
    assert (rows[0]["score"], rows[0]["recommendation"]) == (91, "strong")
    assert rows[0]["headline"] == sample_deep_eval(91)["summary"]
    assert rows[1]["headline"] == "Backend work in Python"  # the first fast reason
    assert (rows[1]["salaryMin"], rows[1]["salaryMax"]) == (150000.0, None)
    assert rows[1]["stageChangedAt"].endswith("Z")
    assert api.get("/api/matches").json() == {"matches": []}


def test_matches_takes_at_most_twenty_ids(signed_in: Any) -> None:
    api, user = signed_in()
    matches = [make_match(user, fast_score=50 + n) for n in range(22)]
    query = ",".join(str(m.id) for m in matches)
    rows = api.get(f"/api/matches?ids={query}").json()["matches"]
    assert sorted(row["matchId"] for row in rows) == sorted(str(m.id) for m in matches[:20])


# --- PATCH /api/matches/{id} ---------------------------------------------------------------------


def test_moves_a_match_and_names_it(signed_in: Any) -> None:
    api, user = signed_in()
    match = make_match(user, job=make_job(title="Backend Engineer"))
    answer = api.patch(f"/api/matches/{match.id}", {"stage": "shortlisted"})
    assert answer.json() == {
        "ok": True,
        "message": f"Moved Backend Engineer at {match.job.company_name} to Shortlisted.",
    }
    moved = JobMatch.objects.for_user(user.pk).get(id=match.id)
    assert moved.stage == "shortlisted"
    # The same stage again is a successful no-op that keeps its timestamp.
    api.patch(f"/api/matches/{match.id}", {"stage": "shortlisted"})
    assert JobMatch.objects.for_user(user.pk).get(id=match.id).stage_changed_at == (
        moved.stage_changed_at
    )


def test_notes_and_rating(signed_in: Any) -> None:
    api, user = signed_in()
    match = make_match(user)
    answer = api.patch(f"/api/matches/{match.id}", {"notes": "Call Dana.", "userRating": -1})
    assert answer.json() == {"ok": True, "message": "Saved."}
    saved = JobMatch.objects.for_user(user.pk).get(id=match.id)
    assert (saved.notes, saved.user_rating) == ("Call Dana.", -1)
    # Null clears; a field left out is left alone.
    api.patch(f"/api/matches/{match.id}", {"notes": None})
    saved = JobMatch.objects.for_user(user.pk).get(id=match.id)
    assert (saved.notes, saved.user_rating) == (None, -1)
    assert api.patch(f"/api/matches/{match.id}", {}).json() == {
        "ok": True,
        "message": "Nothing to change.",
    }
    # All three at once: moved and saved, and the move is what the answer says.
    both = api.patch(f"/api/matches/{match.id}", {"stage": "applied", "notes": "Sent."})
    assert both.json()["message"].endswith("to Applied.")
    assert JobMatch.objects.for_user(user.pk).get(id=match.id).notes == "Sent."


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"stage": "hired"}, "That move could not be read."),
        ({"stage": None}, "That move could not be read."),
        ({"stage": "applied", "notes": 5}, "That change could not be read."),
        ({"notes": "x" * 20_001}, "That change could not be read."),
        ({"userRating": 11}, "That change could not be read."),
        ({"userRating": "5"}, "That change could not be read."),
        ({"userRating": True}, "That change could not be read."),
        ({"userRating": 2.5}, "That change could not be read."),
    ],
)
def test_unreadable_changes(signed_in: Any, body: Any, message: str) -> None:
    api, user = signed_in()
    match = make_match(user)
    answer = api.patch(f"/api/matches/{match.id}", body)
    assert answer.status_code == 400
    assert answer.json() == {"ok": False, "message": message}
    assert JobMatch.objects.for_user(user.pk).get(id=match.id).stage == "found"


@pytest.mark.parametrize("target", ["not-a-uuid", "theirs", "missing"])
def test_a_match_that_is_not_the_users_no_longer_exists(signed_in: Any, target: str) -> None:
    api, _ = signed_in()
    theirs = make_match(make_user())
    match_id = {"not-a-uuid": "not-a-uuid", "theirs": str(theirs.id), "missing": str(uuid.uuid4())}
    for body in ({"stage": "applied"}, {"notes": "x"}):
        answer = api.patch(f"/api/matches/{match_id[target]}", body)
        assert answer.status_code == 404
        assert answer.json() == {"ok": False, "message": "That match no longer exists."}
    assert JobMatch.objects.for_user(theirs.user_id).get(id=theirs.id).stage == "found"


# --- POST /api/matches/{id}/evaluate -------------------------------------------------------------


def test_evaluate_queues_a_run_then_reuses_it(signed_in: Any) -> None:
    api, user = signed_in(with_resume=True)
    match = make_match(user, fast_score=80, job=make_job(title="Data Engineer"))
    label = f"Data Engineer at {match.job.company_name}"
    answer = api.post(f"/api/matches/{match.id}/evaluate")
    assert answer.status_code == 202
    run = Run.objects.for_user(user.pk).get(id=answer.json()["runId"])
    assert answer.json() == {
        "ok": True,
        "runId": str(run.id),
        # The configured evaluate model (terra in the test settings), one typical evaluation.
        "message": f"Evaluating {label} (about $0.03). This takes about half a minute.",
    }
    assert (run.kind, run.input) == ("rank_deep", {"matchIds": [str(match.id)]})
    [job] = deferred("match.evaluate")
    assert (job.queue, job.lock) == ("interactive", f"run:{run.id}")

    again = api.post(f"/api/matches/{match.id}/evaluate")
    assert again.status_code == 200
    assert again.json() == {
        "ok": True,
        "runId": str(run.id),
        "message": f"Already evaluating {label}.",
    }
    assert len(deferred()) == 1


def test_evaluate_refusals(signed_in: Any) -> None:
    api, user = signed_in()
    match = make_match(user)
    no_resume = api.post(f"/api/matches/{match.id}/evaluate")
    assert no_resume.status_code == 422
    assert no_resume.json() == {
        "ok": False,
        "message": "There is no resume on file to evaluate against. Upload one on the profile "
        "page first.",
    }
    for target in ("nope", str(uuid.uuid4()), str(make_match(make_user()).id)):
        missing = api.post(f"/api/matches/{target}/evaluate")
        assert missing.status_code == 404
        assert missing.json() == {"ok": False, "message": "That match does not exist."}
    assert deferred() == []


def test_evaluate_is_throttled(signed_in: Any) -> None:
    api, user = signed_in(with_resume=True)
    match = make_match(user)
    limit = throttle.EVALUATE
    for _ in range(limit.limit):
        throttle.throttle(f"evaluate:{user.pk}", limit.limit, limit.window_s)
    answer = api.post(f"/api/matches/{match.id}/evaluate")
    assert answer.status_code == 429
    assert answer.json() == {
        "ok": False,
        "message": "Too many attempts. Try again in a few minutes.",
    }
    assert deferred() == []


def test_the_api_documents_camel_case() -> None:
    schema = ninja_api.get_openapi_schema()
    assert {"/api/searches", "/api/matches", "/api/matches/{id}", "/api/matches/{id}/evaluate"} <= (
        set(schema["paths"])
    )
    row = schema["components"]["schemas"]["MatchRow"]["properties"]
    assert set(row) == ROW_KEYS
    assert "Stage" in schema["components"]["schemas"]
