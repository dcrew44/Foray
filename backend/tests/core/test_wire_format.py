"""The wire is camelCase (design §5.1, §5.2).

ninja 1.7.1 dumps responses and schemas with each operation's explicit `by_alias`, so camelCase
holds only if every router comes from `core.router.router()`. These tests walk the real API:
every mounted router and operation is `by_alias=True`, every component in the exported
`openapi.json` has camelCase property names, and every parameterless GET answers with camelCase
keys. A probe router with a snake_case field proves the mechanism end to end, and the export
proves `export_openapi_schema` honours the registry merge in `foray.api` (§15.4).
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command
from django.http import Http404
from django.test import Client, RequestFactory
from ninja import NinjaAPI
from ninja.errors import AuthenticationError
from ninja.testing import TestClient

from apps.accounts.models import User
from apps.chat.tools import INPUT_MODELS, SalaryFloorInput
from apps.core import errors, openapi
from apps.core.errors import ApiError, messages
from apps.core.router import ApiModel, router
from foray.api import api

CAMEL = re.compile(r"^[a-z][a-zA-Z0-9]*$")

TOOL_ARGUMENTS = "§7.2: tool arguments keep today's snake_case names"

# Component names whose keys are snake_case on purpose (§2.1: preferences and evaluations keep
# today's spelling). Each entry needs a reason.
SNAKE_CASE_COMPONENTS: dict[str, str] = {
    "Preferences": "§2.1: preferences keep today's snake_case keys",
    "FastScore": "§2.1: evaluations keep today's snake_case keys (the match page embeds them)",
    "DeepEval": "§2.1: evaluations keep today's snake_case keys (the match page embeds them)",
    "MatchedRequirement": "§2.1: part of DeepEval",
    "EvalGap": "§2.1: part of DeepEval",
    # The chat tools' inputs (`apps.chat.tools.INPUT_MODELS`, registered by `chat.api`).
    "GetProfileSummaryInput": TOOL_ARGUMENTS,
    "GetResumeInput": TOOL_ARGUMENTS,
    "UpdateProfilePreferencesInput": TOOL_ARGUMENTS,
    "SalaryFloorInput": TOOL_ARGUMENTS,
    "AddCompanyInput": TOOL_ARGUMENTS,
    "DiscoverCompaniesInput": TOOL_ARGUMENTS,
    "ListCompaniesInput": TOOL_ARGUMENTS,
    "StartSearchRunInput": TOOL_ARGUMENTS,
    "GetRunStatusInput": TOOL_ARGUMENTS,
    "CancelRunInput": TOOL_ARGUMENTS,
    "ListJobsInput": TOOL_ARGUMENTS,
    "GetJobInput": TOOL_ARGUMENTS,
    "ListPendingApprovalsInput": TOOL_ARGUMENTS,
    "AnswerApprovalInput": TOOL_ARGUMENTS,
    "MoveJobStageInput": TOOL_ARGUMENTS,
    "EvaluateJobInput": TOOL_ARGUMENTS,
    "TailorResumeInput": TOOL_ARGUMENTS,
}

# Response keys whose *values* are one of the components above, so their own keys stay
# snake_case: the page endpoints embed the stored preferences (§5.3).
SNAKE_CASE_VALUES: dict[str, str] = {
    "preferences": "a `Preferences` (Home's onboarding state, the profile page)",
}


def _mounts() -> list[tuple[str, Any]]:
    return [(bound.prefix, bound) for bound in api._get_bound_routers()]


def _operations() -> Iterator[tuple[str, Any, Any]]:
    for prefix, bound in _mounts():
        for path, path_view in bound.path_operations.items():
            for operation in path_view.operations:
                yield prefix + path, path_view, operation


def _camel_keys(value: Any, where: str) -> list[str]:
    bad: list[str] = []
    if isinstance(value, dict):
        for key, inner in value.items():
            if not CAMEL.match(str(key)):
                bad.append(f"{where}.{key}")
            if key in SNAKE_CASE_VALUES:
                continue
            bad.extend(_camel_keys(inner, f"{where}.{key}"))
    elif isinstance(value, list):
        for index, inner in enumerate(value):
            bad.extend(_camel_keys(inner, f"{where}[{index}]"))
    return bad


def test_every_router_is_by_alias() -> None:
    mounted = {prefix: bound for prefix, bound in _mounts()}
    expected = {
        "/auth", "/me", "/preferences", "/settings",
        "/live", "/runs", "/approvals", "/worker",
        "/searches", "/matches", "/companies",
        "/uploads", "/documents", "/resume", "/resumes",
        "/conversations", "/pages", "/health",
    }  # fmt: skip
    assert expected <= set(mounted), f"missing prefixes: {expected - set(mounted)}"
    for prefix, bound in mounted.items():
        if prefix == "":
            # The API's default router: nothing may be mounted on it (§5.1).
            assert not bound.path_operations, "use a router from core.router, not api.get(...)"
            continue
        assert bound.template.by_alias is True, f"{prefix} is not built by core.router.router()"
    for path, _, operation in _operations():
        assert operation.by_alias is True, f"{path} {operation.methods} dumps snake_case"


class ProbeOut(ApiModel):
    fast_score: int
    nested_value: dict[str, int]


class ProbeIn(ApiModel):
    user_rating: int


class RegisteredOnly(ApiModel):
    run_id: str
    event_kind: str


@pytest.fixture
def probe_client() -> TestClient:
    probe = router()

    @probe.get("/probe", response=ProbeOut, auth=None)
    def get_probe(request: Any) -> dict[str, Any]:
        return {"fast_score": 5, "nested_value": {"a": 1}}

    @probe.post("/probe", response=ProbeOut, auth=None)
    def post_probe(request: Any, body: ProbeIn) -> dict[str, Any]:
        return {"fast_score": body.user_rating, "nested_value": {}}

    return TestClient(probe)


def test_camel_case_response_and_schema(probe_client: TestClient, tmp_path: Path) -> None:
    # The mechanism: a router from core.router answers and documents camelCase.
    response = probe_client.get("/probe")
    assert response.json() == {"fastScore": 5, "nestedValue": {"a": 1}}
    # Inputs accept camelCase and snake_case.
    assert probe_client.post("/probe", json={"userRating": 3}).json()["fastScore"] == 3
    assert probe_client.post("/probe", json={"user_rating": 4}).json()["fastScore"] == 4

    probe_api = NinjaAPI(urls_namespace="wire-probe")
    probe_api.add_router("", probe_client.router_or_app)  # type: ignore[arg-type]
    probe_schema = probe_api.get_openapi_schema(path_prefix="")["components"]["schemas"]
    assert set(probe_schema["ProbeOut"]["properties"]) == {"fastScore", "nestedValue"}

    # The real API, as `pnpm api:types` exports it.
    out = tmp_path / "openapi.json"
    call_command("export_openapi_schema", api="foray.api.api", output=str(out), indent=2)
    schema = json.loads(out.read_text())
    assert schema["paths"]["/api/health"]["get"]
    for name, component in schema.get("components", {}).get("schemas", {}).items():
        if name in SNAKE_CASE_COMPONENTS:
            continue
        for prop in component.get("properties", {}):
            assert CAMEL.match(prop), f"component {name} has the snake_case property {prop}"


def test_chat_tool_inputs_are_exported_with_their_argument_names(tmp_path: Path) -> None:
    """§5.4, §6.6: `chat-types.ts` names every tool's input from the schema, and each keeps the
    snake_case arguments the model sends (§7.2)."""
    out = tmp_path / "openapi.json"
    call_command("export_openapi_schema", api="foray.api.api", output=str(out))
    components = json.loads(out.read_text())["components"]["schemas"]
    inputs = [*dict.fromkeys(INPUT_MODELS.values()), SalaryFloorInput]
    assert {model.__name__ for model in inputs} == {
        name for name, reason in SNAKE_CASE_COMPONENTS.items() if reason == TOOL_ARGUMENTS
    }
    for model in inputs:
        component = components[model.__name__]
        assert set(component.get("properties", {})) == set(model.model_fields), model.__name__
        assert component["additionalProperties"] is False, model.__name__


def test_export_includes_registered_schemas(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(openapi, "_REGISTRY", {})
    openapi.register_schema(RegisteredOnly)
    out = tmp_path / "openapi.json"
    call_command("export_openapi_schema", api="foray.api.api", output=str(out))
    components = json.loads(out.read_text())["components"]["schemas"]
    assert set(components["RegisteredOnly"]["properties"]) == {"runId", "eventKind"}


@pytest.mark.django_db
def test_every_parameterless_get_answers_camel_case() -> None:
    user = User.objects.create_user("wire@example.test")
    client = Client()
    client.force_login(user)
    checked = []
    for path, _, operation in _operations():
        if "GET" not in operation.methods or "{" in path or not operation.include_in_schema:
            continue
        response = client.get(f"/api{path}")
        assert response.status_code == 200, f"GET /api{path} answered {response.status_code}"
        assert response["Content-Type"].startswith("application/json")
        assert response["Cache-Control"] == "no-store", f"GET /api{path} may be cached"
        assert _camel_keys(response.json(), path) == []
        checked.append(path)
    assert "/health" in checked


def _probe_api_client() -> TestClient:
    """A throwaway API with Foray's error handlers and one operation per failure kind."""
    probe = router()

    @probe.post("/items/{item_id}", auth=None)
    @messages(invalid="That change could not be read.", not_found="That match no longer exists.")
    def change_item(request: Any, item_id: uuid.UUID, body: ProbeIn) -> dict[str, bool]:
        if body.user_rating == 409:
            raise ApiError(409, "That was already answered.", runId="r-1")
        if body.user_rating == 500:
            raise RuntimeError("internal detail that must not leak")
        return {"ok": True}

    @probe.get("/plain/{item_id}", auth=None)
    def plain(request: Any, item_id: uuid.UUID) -> dict[str, bool]:
        raise Http404

    probe_api = NinjaAPI(urls_namespace="errors-probe")
    errors.install(probe_api)
    probe_api.add_router("", probe)
    return TestClient(probe_api)


def test_errors_use_the_envelope() -> None:
    client = _probe_api_client()
    item = f"/items/{uuid.uuid4()}"
    assert client.post(item, json={"userRating": 1}).json() == {"ok": True}

    conflict = client.post(item, json={"userRating": 409})
    assert conflict.status_code == 409
    assert conflict.json() == {"ok": False, "message": "That was already answered.", "runId": "r-1"}

    unreadable = client.post(item, json={"userRating": "lots"})
    assert unreadable.status_code == 400
    assert unreadable.json() == {"ok": False, "message": "That change could not be read."}

    malformed_id = client.post("/items/not-a-uuid", json={"userRating": 1})
    assert malformed_id.status_code == 404
    assert malformed_id.json() == {"ok": False, "message": "That match no longer exists."}

    unexpected = client.post(item, json={"userRating": 500})
    assert unexpected.status_code == 500
    assert unexpected.json() == {"ok": False, "message": errors.UNEXPECTED}

    missing = client.get(f"/plain/{uuid.uuid4()}")
    assert missing.status_code == 404
    assert missing.json() == {"ok": False, "message": errors.DEFAULT_NOT_FOUND}
    assert client.get("/plain/nope").json()["message"] == errors.DEFAULT_NOT_FOUND


def test_not_signed_in_is_401_json() -> None:
    request = RequestFactory().get("/api/me")
    response = api.on_exception(request, AuthenticationError())
    assert response.status_code == 401
    assert json.loads(response.content) == {"ok": False, "message": "Not signed in."}


def test_chat_view_refuses_in_plain_text() -> None:
    # Step 6 replaced the 503 stub: the chat view answers `useChat` in plain text, and a body with
    # no chat id is unreadable (deviations/step-6.md §4, §5).
    response = Client().post("/api/chat", data="{}", content_type="application/json")
    assert response.status_code == 400
    assert response["Content-Type"].startswith("text/plain")
    assert response.content == b"That request could not be read."
