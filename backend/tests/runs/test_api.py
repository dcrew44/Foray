"""The runs endpoints (design §5.2, §5.3, §6.3, §9.6, §9.9): `/api/live`, `/api/runs/*`,
`/api/approvals/{id}/decision` and `/api/worker/ping`, with today's strings."""

from __future__ import annotations

import os
import re
import uuid
from datetime import timedelta
from typing import Any

import pytest
from django.test import Client
from django.utils import timezone

from apps.runs import events, step
from apps.runs.models import Approval
from tests.factories import make_approval, make_event, make_run, make_user
from tests.queue import run_deferred
from tests.runs.support import batch_payload, dead_worker, event_kinds, jobs, refresh, sql

ISO_Z = r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d{3})?Z$"


def _is_iso_z(value: Any) -> bool:
    return isinstance(value, str) and re.match(ISO_Z, value) is not None


# --- /api/live -----------------------------------------------------------------------------------


@pytest.mark.django_db
def test_live_snapshot_shape(signed_in: Any) -> None:
    api, user = signed_in()
    search = make_run(user, status="running", input={"prompt": "Staff roles", "hints": None})
    events.emit(user.pk, "run.started", "Searching: Staff roles", run_id=search.id)
    paused = make_run(user, status="awaiting_approval")
    approval = make_approval(user, run=paused, payload=batch_payload(1))
    old = make_run(user, status="completed")
    sql("UPDATE runs SET updated_at = now() - interval '11 minutes' WHERE id = %s", [str(old.id)])
    recent = make_run(user, status="failed", error="It broke.")
    other_user = make_user()
    make_run(other_user, status="running")
    make_event(other_user, kind="run.started")

    response = api.get("/api/live")
    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store"
    body = response.json()
    assert set(body) == {"cursor", "events", "runs", "approvals", "worker"}

    [event] = body["events"]
    assert body["cursor"] == event["id"]
    assert set(event) == {
        "id", "runId", "level", "kind", "message", "data", "approvalId", "createdAt"
    }  # fmt: skip
    assert (event["runId"], event["kind"], event["level"], event["data"]) == (
        str(search.id),
        "run.started",
        "info",
        None,
    )
    assert _is_iso_z(event["createdAt"])

    runs = {run["id"]: run for run in body["runs"]}
    assert set(runs) == {str(search.id), str(paused.id), str(recent.id)}  # not the old one
    live_run = runs[str(search.id)]
    assert set(live_run) == {
        "id", "kind", "status", "mode", "prompt", "summary", "stats", "output", "error",
        "createdAt", "startedAt", "finishedAt",
    }  # fmt: skip
    assert (live_run["kind"], live_run["status"], live_run["mode"], live_run["prompt"]) == (
        "search",
        "running",
        "approve_each",
        "Staff roles",
    )
    assert live_run["stats"] == {} and live_run["output"] is None
    assert live_run["finishedAt"] is None and _is_iso_z(live_run["startedAt"])
    assert runs[str(recent.id)]["error"] == "It broke."

    [live_approval] = body["approvals"]
    assert live_approval == {
        "id": str(approval.id),
        "runId": str(paused.id),
        "kind": "deep_eval_batch",
        "title": approval.title,
        "payload": batch_payload(1),
        "createdAt": live_approval["createdAt"],
    }
    assert body["worker"] == {"online": False, "lastSeenAt": None, "pid": None}


@pytest.mark.django_db
def test_live_pages_with_since(signed_in: Any) -> None:
    api, user = signed_in()
    first = events.emit(user.pk, "run.started", "one")
    second = events.emit(user.pk, "run.completed", "two")
    body = api.get(f"/api/live?since={first.id}").json()
    assert [e["id"] for e in body["events"]] == [second.id]
    assert body["cursor"] == second.id
    body = api.get(f"/api/live?since={second.id}").json()
    assert (body["events"], body["cursor"]) == ([], second.id)
    # Read like JavaScript's Number(): garbage, zero and negatives mean the first poll.
    for since in ("abc", "0", "-4", "", "NaN", "Infinity", "1_0"):
        body = api.get(f"/api/live?since={since}").json()
        assert [e["id"] for e in body["events"]] == [first.id, second.id], since
        assert body["cursor"] == second.id
    assert api.get(f"/api/live?since={first.id}.5").json()["cursor"] == second.id
    assert api.get(f"/api/live?since=0x{first.id:x}").json()["events"][0]["id"] == second.id


@pytest.mark.django_db
def test_live_lists_only_answerable_approvals(signed_in: Any) -> None:
    api, user = signed_in()
    open_run = make_run(user, status="awaiting_approval")
    answerable = make_approval(user, run=open_run)
    lapsed = make_approval(
        user,
        run=make_run(user, status="awaiting_approval"),
        expires_at=timezone.now() - timedelta(seconds=1),
    )
    on_running = make_approval(user, run=make_run(user, status="running"))  # bug #41: joined
    ids = [a["id"] for a in api.get("/api/live").json()["approvals"]]
    assert ids == [str(answerable.id)]
    assert str(lapsed.id) not in ids and str(on_running.id) not in ids
    # Live is read-only now: the lapsed approval is left for the periodic sweep.
    assert Approval.objects.for_user(user.pk).get(id=lapsed.id).status == "pending"


@pytest.mark.django_db
def test_live_worker_online_from_procrastinate_heartbeats(signed_in: Any) -> None:
    api, _ = signed_in()
    dead_worker(300)
    worker = api.get("/api/live").json()["worker"]
    assert worker["online"] is False and _is_iso_z(worker["lastSeenAt"])
    dead_worker(2)
    worker = api.get("/api/live").json()["worker"]
    assert worker["online"] is True and worker["pid"] is None


@pytest.mark.django_db
def test_live_needs_a_session() -> None:
    response = Client().get("/api/live")
    assert response.status_code == 401
    assert response.json() == {"ok": False, "message": "Not signed in."}


# --- /api/runs -----------------------------------------------------------------------------------


@pytest.mark.django_db
def test_get_run_and_its_events(signed_in: Any) -> None:
    api, user = signed_in()
    run = make_run(user, status="completed", output={"evaluated": 1, "matchIds": []})
    events.emit(user.pk, "evaluation.start", "Evaluating 1 role.", run_id=run.id)
    events.emit(user.pk, "run.completed", "Evaluated 1 role.", run_id=run.id, data={"x": 1})
    events.emit(user.pk, "run.started", "another run's")

    body = api.get(f"/api/runs/{run.id}").json()
    assert (body["id"], body["status"], body["output"]) == (
        str(run.id),
        "completed",
        {"evaluated": 1, "matchIds": []},
    )
    listed = api.get(f"/api/runs/{run.id}/events").json()["events"]
    assert [e["kind"] for e in listed] == ["evaluation.start", "run.completed"]
    assert listed[1]["data"] == {"x": 1}

    other_api, _ = signed_in()
    for path in (f"/api/runs/{run.id}", f"/api/runs/{run.id}/events"):
        assert other_api.get(path).status_code == 404  # another user's: indistinguishable
    for path in ("/api/runs/not-a-uuid", f"/api/runs/{uuid.uuid4()}/events"):
        response = api.get(path)
        assert response.status_code == 404
        assert response.json() == {"ok": False, "message": "That no longer exists."}


@pytest.mark.django_db
def test_cancel_endpoint(signed_in: Any) -> None:
    api, user = signed_in()
    run = make_run(user, status="running")
    response = api.post(f"/api/runs/{run.id}/cancel")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "message": "Stopped. Everything found so far was kept."}
    assert refresh(run).status == "cancelled"
    assert event_kinds(run.id) == ["run.cancelled"]

    again = api.post(f"/api/runs/{run.id}/cancel")
    assert again.status_code == 409
    assert again.json() == {"ok": False, "message": "That run had already finished."}
    for missing in (uuid.uuid4(), "nope"):
        response = api.post(f"/api/runs/{missing}/cancel")
        assert response.status_code == 404
        assert response.json() == {"ok": False, "message": "That run had already finished."}
    other_api, _ = signed_in()
    mine = make_run(user, status="running")
    assert other_api.post(f"/api/runs/{mine.id}/cancel").status_code == 404
    assert refresh(mine).status == "running"


@pytest.mark.django_db
def test_cancel_needs_a_same_origin_request(signed_in: Any) -> None:
    api, user = signed_in()
    run = make_run(user, status="running")
    response = api.client.post(
        f"/api/runs/{run.id}/cancel",
        content_type="application/json",
        headers={"Origin": "http://evil.example"},
    )
    assert response.status_code == 403
    assert refresh(run).status == "running"


# --- /api/approvals ------------------------------------------------------------------------------


def _paused(user: Any, kind: str = "deep_eval_batch") -> Approval:
    run = make_run(user, status="awaiting_approval")
    return make_approval(user, run=run, kind=kind, payload=batch_payload(3))


@pytest.mark.django_db
def test_decision_endpoint_messages(signed_in: Any) -> None:
    api, user = signed_in()
    approval = _paused(user)
    ids = [item["matchId"] for item in batch_payload(3)["items"]]
    response = api.post(
        f"/api/approvals/{approval.id}/decision", {"approved": True, "itemIds": ids[:1]}
    )
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "message": "Approved. Deep-evaluating 1 role in the background.",
    }
    [job] = jobs(lock=f"run:{approval.run_id}")
    assert job["task_name"] == "search.evaluate"
    assert refresh(approval.run).status == "running"

    again = api.post(f"/api/approvals/{approval.id}/decision", {"approved": False})
    assert again.status_code == 409
    assert again.json() == {"ok": False, "message": "That was already answered."}

    skipped = _paused(user)
    body = api.post(f"/api/approvals/{skipped.id}/decision", {"approved": False}).json()
    assert body == {"ok": True, "message": "Skipped the deep evaluation."}
    shortlist = _paused(user, "shortlist_proposal")
    body = api.post(
        f"/api/approvals/{shortlist.id}/decision", {"approved": True, "itemIds": None}
    ).json()
    assert body == {"ok": True, "message": "Approved. Shortlisting 3 roles."}
    left = _paused(user, "shortlist_proposal")
    body = api.post(f"/api/approvals/{left.id}/decision", {"approved": False}).json()
    assert body == {"ok": True, "message": "Left the shortlist as it was."}


@pytest.mark.django_db
def test_decision_endpoint_refusals(signed_in: Any) -> None:
    api, user = signed_in()
    approval = _paused(user)
    for body in ({"approved": "yes"}, {}, {"approved": True, "itemIds": [1]}, "[]"):
        response = api.post(f"/api/approvals/{approval.id}/decision", body)
        assert response.status_code == 400, body
        assert response.json() == {"ok": False, "message": "That answer could not be read."}
    # A body that is not JSON at all is refused by ninja before validation, with its own
    # sentence (handoff to core.errors: deviations/step-4.md).
    response = api.post(f"/api/approvals/{approval.id}/decision", "not json")
    assert response.status_code == 400 and response.json()["ok"] is False
    for missing in ("approval-7", uuid.uuid4()):
        response = api.post(f"/api/approvals/{missing}/decision", {"approved": True})
        assert response.status_code == 404
        assert response.json() == {"ok": False, "message": "That request no longer exists."}
    other_api, _ = signed_in()
    response = other_api.post(f"/api/approvals/{approval.id}/decision", {"approved": True})
    assert response.status_code == 404

    lapsed = _paused(user)
    Approval.objects.for_user(user.pk).filter(id=lapsed.id).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )
    response = api.post(f"/api/approvals/{lapsed.id}/decision", {"approved": True})
    assert response.status_code == 409
    assert response.json() == {"ok": False, "message": "That request has expired."}
    assert Approval.objects.for_user(user.pk).get(id=approval.id).status == "pending"


@pytest.mark.django_db
def test_decision_endpoint_when_the_continuation_cannot_be_deferred(
    signed_in: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, user = signed_in()
    approval = _paused(user)

    def broken(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("the queue is down")

    monkeypatch.setattr(step, "defer_in_tx", broken)
    response = api.post(f"/api/approvals/{approval.id}/decision", {"approved": True})
    assert response.status_code == 500
    assert response.json() == {
        "ok": False,
        "message": "Could not resume the run just now. Try again in a moment.",
    }
    assert Approval.objects.for_user(user.pk).get(id=approval.id).status == "pending"
    assert refresh(approval.run).status == "awaiting_approval"


# --- /api/worker/ping ----------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_ping_defers_a_job_the_worker_answers(signed_in: Any) -> None:
    api, user = signed_in()
    response = api.post("/api/worker/ping")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True and isinstance(body["jobId"], int)
    [job] = jobs(task_name="worker.ping")
    assert (job["id"], job["queue"], job["lock"], job["args"]) == (
        body["jobId"],
        "system",
        None,
        {"user_id": user.pk, "note": "The worker answered a test job from Settings."},
    )
    run_deferred(["system"])
    [event] = api.get("/api/live").json()["events"]
    assert (event["kind"], event["message"], event["runId"]) == (
        "worker.ping",
        "The worker answered a test job from Settings.",
        None,
    )
    assert event["data"] == {"jobId": body["jobId"], "workerPid": os.getpid()}
