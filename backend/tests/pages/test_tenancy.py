"""The tenancy sweep (design §12.3, register row R8).

Two users with full object graphs (matches, resumes, documents, runs, approvals, conversations,
events, ledger rows). Then:

- **Every route with a path id**, read from the OpenAPI schema, called as user B with user A's id,
  answers 404 with the envelope, and leaves A's rows exactly as they were. Each route is listed
  in `ROUTES` with the A row it is called with. Routes on **shared** resources are exempt by
  `SHARED`: companies are the shared mirror, so B may pause and refresh them (bug #94). A new
  path-id route on neither list fails the sweep.
- **Every list endpoint** (each GET without a path id, found the same way) answers B with none
  of A's ids or texts, while A's own answer shows them (so the check could fail).
- **The chat view** refuses A's conversation id, and **an id in a body** (`POST
  /api/resumes/tailor {matchId}`) is not found either.
- **A reaches every listed route** with the same ids, so B's 404 is a refusal, not a typo.

The chat view claims a conversation on a `db()` thread, which needs committed rows, so these
tests are transactional.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from django.test import Client

from apps.chat.models import Conversation, Message
from apps.core.ids import generate_message_id
from apps.core.storage import storage
from apps.matching.models import JobMatch
from apps.resumes.models import Document, Resume
from apps.runs.models import Approval, Event, Run
from foray.api import api
from tests.factories import make_company
from tests.pages.conftest import ORIGIN, SSR, Graph, full_graph, signed_in

pytestmark = pytest.mark.django_db(transaction=True)


@dataclass(frozen=True)
class Route:
    method: str
    path: str  # as the OpenAPI schema names it
    target: str  # the A row whose id goes into the path
    body: dict[str, Any] | None = None  # a readable body, so a refusal cannot be a 400
    owner_status: int = 200  # what A gets


# Every route with a path id that reaches per-user rows, in an order A can call them in (reads
# first; the run cancelled is not the one the approval resumes; the chat is deleted last).
ROUTES: tuple[Route, ...] = (
    Route("GET", "/api/runs/{id}", "running"),
    Route("GET", "/api/runs/{id}/events", "search"),
    Route("GET", "/api/documents/{id}", "upload"),
    Route("GET", "/api/resumes/{id}/preview", "tailored"),
    Route("GET", "/api/pages/matches/{id}", "match"),
    Route("GET", "/api/conversations/{id}", "conversation"),
    Route("PATCH", "/api/matches/{id}", "match", {"stage": "applied", "notes": "Moved."}),
    Route("POST", "/api/matches/{id}/evaluate", "match", {}, 202),
    Route("POST", "/api/approvals/{id}/decision", "approval", {"approved": True}),
    Route("POST", "/api/runs/{id}/cancel", "running", {}),
    Route("DELETE", "/api/conversations/{id}", "conversation"),
)

# Routes on the shared mirror: anyone signed in may use them with any slug (§12.3, bug #94).
SHARED = {
    ("PATCH", "/api/companies/{slug}"),
    ("POST", "/api/companies/{slug}/refresh"),
}

# List endpoints that have no per-user rows to show A, so they cannot show A's texts either.
NOT_PERSONAL = {"/api/health", "/api/auth/config", "/api/pages/companies"}

_PATH_PARAM = re.compile(r"\{[^}]+\}")


def _schema() -> dict[str, Any]:
    return api.get_openapi_schema(path_prefix="/api")


def _path_id_operations() -> set[tuple[str, str]]:
    return {
        (method.upper(), path)
        for path, operations in _schema()["paths"].items()
        if _PATH_PARAM.search(path)
        for method in operations
    }


def _list_endpoints() -> list[str]:
    return sorted(
        path
        for path, operations in _schema()["paths"].items()
        if "get" in operations and not _PATH_PARAM.search(path)
    )


@pytest.fixture
def graphs(settings: Any, tmp_path: Path) -> Iterator[tuple[Graph, Graph]]:
    """A and B share the mirror's companies; everything else is their own."""
    settings.STORAGE_DIR = str(tmp_path / "storage")
    stripe = make_company(name="Stripe", slug="stripe")
    linear = make_company(name="Linear", slug="linear", source_type="ashby")
    a = full_graph(marker="Alphamark", company=stripe, other_company=linear)
    b = full_graph(marker="Bravomark", company=stripe, other_company=linear)
    for graph in (a, b):
        storage().put(graph.upload.storage_path, b"%PDF-1.7 synthetic upload")
    yield a, b


def _row(graph: Graph, target: str) -> Any:
    if target == "match":
        return graph.matches["found"]
    if target in graph.runs:
        return graph.runs[target]
    return getattr(graph, target)


def _call(client: Client, route: Route, row_id: Any) -> Any:
    path = _PATH_PARAM.sub(str(row_id), route.path)
    if route.method == "GET":
        return client.get(path, headers=SSR)
    send = getattr(client, route.method.lower())
    data = json.dumps(route.body) if route.body is not None else None
    return send(path, data=data, content_type="application/json", headers={"Origin": ORIGIN})


def _state(graph: Graph) -> dict[str, Any]:
    """Everything the routes could change about a user's rows."""
    user_id = graph.user.pk
    return {
        "matches": sorted(
            JobMatch.objects.for_user(user_id).values_list("id", "stage", "notes", "updated_at")
        ),
        "runs": sorted(
            Run.objects.for_user(user_id).values_list("id", "status", "stats", "updated_at")
        ),
        "approvals": sorted(
            Approval.objects.for_user(user_id).values_list("id", "status", "decided_at")
        ),
        "events": Event.objects.for_user(user_id).count(),
        "conversations": sorted(
            Conversation.objects.for_user(user_id).values_list("id", "title", "updated_at")
        ),
        "messages": Message.objects.for_user(user_id).count(),
        "documents": sorted(Document.objects.for_user(user_id).values_list("id", flat=True)),
        "resumes": sorted(Resume.objects.for_user(user_id).values_list("id", "version")),
    }


def _leaks(body: str, graph: Graph) -> list[str]:
    text = body.lower()
    return [token for token in [*graph.markers, *graph.ids] if token.lower() in text]


def test_every_path_id_route_is_listed() -> None:
    listed = {(route.method, route.path) for route in ROUTES}
    assert not listed & SHARED
    assert listed | SHARED == _path_id_operations(), (
        "a route with a path id must be in ROUTES (per-user) or SHARED (the mirror)"
    )


def test_tenancy_sweep(graphs: tuple[Graph, Graph]) -> None:
    a, b = graphs
    as_b = signed_in(b.user)
    before = _state(a)

    # Every per-user route refuses A's ids to B, and changes nothing of A's.
    for route in ROUTES:
        response = _call(as_b, route, _row(a, route.target).pk)
        where = f"{route.method} {route.path}"
        assert response.status_code == 404, f"{where} answered {response.status_code}"
        assert response.json()["ok"] is False, where
    assert _state(a) == before

    # Every list endpoint keeps A's rows out of B's answers.
    as_a = signed_in(a.user)
    query = {
        "/api/live": "?since=0",
        "/api/matches": "?ids=" + ",".join(str(match.id) for match in a.matches.values()),
        "/api/pages/matches": "?closed=1",
    }
    lists = _list_endpoints()
    assert "/api/pages/home" in lists and "/api/conversations" in lists
    for path in lists:
        url = path + query.get(path, "")
        theirs = as_b.get(url, headers=SSR)
        assert theirs.status_code == 200, url
        assert _leaks(theirs.content.decode(), a) == [], f"{url} shows B what is A's"
        if path not in NOT_PERSONAL:
            mine = as_a.get(url, headers=SSR)
            assert _leaks(mine.content.decode(), a), f"{url} shows A nothing of A's"

    # The chat view refuses A's conversation id.
    message = {
        "id": generate_message_id(),
        "role": "user",
        "parts": [{"type": "text", "text": "Hi"}],
    }
    chat = as_b.post(
        "/api/chat",
        data=json.dumps({"id": str(a.conversation.pk), "message": message}),
        content_type="application/json",
        headers={"Origin": ORIGIN},
    )
    assert chat.status_code == 404
    assert chat.content.decode() == "That chat does not exist."

    # An id in a body is A's too: B cannot tailor for A's match.
    tailor = as_b.post(
        "/api/resumes/tailor",
        data=json.dumps({"matchId": str(a.matches["found"].pk)}),
        content_type="application/json",
        headers={"Origin": ORIGIN},
    )
    assert tailor.status_code == 404
    assert _state(a) == before
    assert not Run.objects.for_user(b.user.pk).filter(kind="tailor", status="queued").exists()


def test_the_owner_reaches_every_route(graphs: tuple[Graph, Graph]) -> None:
    a, _ = graphs
    as_a = signed_in(a.user)
    for route in ROUTES:
        response = _call(as_a, route, _row(a, route.target).pk)
        where = f"{route.method} {route.path}"
        assert response.status_code == route.owner_status, f"{where}: {response.content[:200]!r}"


def test_shared_routes_reach_the_mirror(graphs: tuple[Graph, Graph]) -> None:
    """The exempt routes really are shared: B pauses the company A's matches came from."""
    a, b = graphs
    response = signed_in(b.user).patch(
        f"/api/companies/{a.company.slug}",
        data=json.dumps({"status": "paused"}),
        content_type="application/json",
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 200
    assert response.json()["company"]["status"] == "paused"


def test_a_malformed_id_is_404_on_every_route(graphs: tuple[Graph, Graph]) -> None:
    _, b = graphs
    as_b = signed_in(b.user)
    for route in ROUTES:
        for bad in ("not-a-uuid", uuid.uuid4()):
            response = _call(as_b, route, bad)
            assert response.status_code == 404, f"{route.method} {route.path} {bad}"
