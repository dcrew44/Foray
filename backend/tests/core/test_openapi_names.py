"""One OpenAPI component name, one schema (design §5.4).

ninja 1.7.1 builds each operation's schemas on its own and merges their definitions into
`components.schemas` with a plain `dict.update` (`OpenAPISchema.add_schema_definitions`, "TODO:
check if schema definitions are unique"); `ForayAPI` then adds the registry's with `setdefault`.
Two types that share a name therefore silently replace each other in `openapi.json`, and with it
in the generated TypeScript. These tests catch that before it happens:

- every definition ninja (or the registry) produces under one name is the same schema;
- every component name comes from one Python type, so a later edit to one copy cannot drift
  (shared shapes live in one module, e.g. `apps.core.schemas.OkMessageOut`).

A throwaway API with a deliberate clash proves both checks can fail.
"""

from __future__ import annotations

import dataclasses
import enum
import json
from collections.abc import Iterator
from typing import Any, TypeAliasType, get_args, is_typeddict

import pytest
from ninja import NinjaAPI
from ninja.constants import NOT_SET
from ninja.openapi.schema import OpenAPISchema
from pydantic import BaseModel, create_model

from apps.core import openapi
from apps.core.router import ApiModel, router
from foray.api import api as foray_api

# --- what ninja merges ----------------------------------------------------------------------------


def _definitions(api: NinjaAPI, monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """Every definition handed to ninja's merge while the schema is built, by component name
    (canonical JSON, de-duplicated), plus the registry's."""
    seen: dict[str, list[str]] = {}
    merge = OpenAPISchema.add_schema_definitions

    def record(self: OpenAPISchema, definitions: dict[str, Any]) -> None:
        for name, definition in definitions.items():
            text = json.dumps(definition, sort_keys=True)
            if text not in seen.setdefault(name, []):
                seen[name].append(text)
        merge(self, definitions)

    with monkeypatch.context() as patch:
        patch.setattr(OpenAPISchema, "add_schema_definitions", record)
        api.get_openapi_schema(path_prefix="")
    if api is foray_api:
        for name, definition in openapi.extra_schemas().items():
            text = json.dumps(definition, sort_keys=True)
            if text not in seen.setdefault(name, []):
                seen[name].append(text)
    return seen


def _clashing_schemas(api: NinjaAPI, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    return sorted(name for name, texts in _definitions(api, monkeypatch).items() if len(texts) > 1)


# --- which types produce them ---------------------------------------------------------------------


def _operations(api: NinjaAPI) -> Iterator[Any]:
    for bound in api._get_bound_routers():
        for path_view in bound.path_operations.values():
            yield from path_view.operations


def _named(tp: Any, found: dict[str, set[Any]], seen: set[int]) -> None:
    """Walks an annotation and records every type pydantic would give a component name."""
    if id(tp) in seen:
        return
    seen.add(id(tp))
    if isinstance(tp, TypeAliasType):
        found.setdefault(tp.__name__, set()).add(tp)
        _named(tp.__value__, found, seen)
        return
    if isinstance(tp, type):
        if issubclass(tp, BaseModel):
            found.setdefault(tp.__name__, set()).add(tp)
            for field in tp.model_fields.values():
                _named(field.annotation, found, seen)
        elif issubclass(tp, enum.Enum):
            found.setdefault(tp.__name__, set()).add(tp)
        elif dataclasses.is_dataclass(tp):
            found.setdefault(tp.__name__, set()).add(tp)
            for field in dataclasses.fields(tp):
                _named(field.type, found, seen)
        elif is_typeddict(tp):
            found.setdefault(tp.__name__, set()).add(tp)
            for annotation in tp.__annotations__.values():
                _named(annotation, found, seen)
        return
    for arg in get_args(tp):  # unions, lists, Annotated, Literal (whose values add nothing)
        _named(arg, found, seen)


def _named_types(api: NinjaAPI) -> dict[str, set[Any]]:
    found: dict[str, set[Any]] = {}
    seen: set[int] = set()
    for operation in _operations(api):
        # ninja's own parameter and response wrappers are flattened away; their fields are not.
        for model in operation.models:
            for field in model.model_fields.values():
                _named(field.annotation, found, seen)
        for model in operation.response_models.values():
            if model is not NOT_SET and model is not None:
                _named(model.model_fields["response"].annotation, found, seen)
    if api is foray_api:
        for name, tp in openapi.registered().items():
            found.setdefault(name, set()).add(tp)
            _named(tp, found, seen)
    return found


def _shared_names(api: NinjaAPI) -> dict[str, list[str]]:
    return {
        name: sorted(f"{tp.__module__}.{tp.__qualname__}" for tp in types)
        for name, types in _named_types(api).items()
        if len(types) > 1
    }


# --- the real API ---------------------------------------------------------------------------------


def test_no_component_name_has_two_schemas(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _clashing_schemas(foray_api, monkeypatch) == []


def test_each_component_name_comes_from_one_type() -> None:
    found = _named_types(foray_api)
    assert "OkMessageOut" in found and "RunStarted" in found  # the walk reaches the routers
    assert _shared_names(foray_api) == {}, (
        "define the shape once (apps.core.schemas) and import it, or give each its own name"
    )


# --- the checks themselves ------------------------------------------------------------------------


def _probe_api() -> NinjaAPI:
    """Two routers, each answering its own class named `Clash`, with different fields (what two
    modules each defining a `Clash` would amount to)."""
    first, second = router(), router()
    clash_a = create_model("Clash", __base__=ApiModel, ok=bool)
    clash_b = create_model("Clash", __base__=ApiModel, ok=bool, message=str)

    @first.get("/a", response=clash_a, auth=None)
    def a(request: Any) -> dict[str, Any]:
        return {"ok": True}

    @second.get("/b", response=clash_b, auth=None)
    def b(request: Any) -> dict[str, Any]:
        return {"ok": True, "message": "hi"}

    probe = NinjaAPI(urls_namespace="openapi-names-probe")
    probe.add_router("/one", first)
    probe.add_router("/two", second)
    return probe


def test_the_checks_catch_a_clash(monkeypatch: pytest.MonkeyPatch) -> None:
    probe = _probe_api()
    assert _clashing_schemas(probe, monkeypatch) == ["Clash"]
    assert list(_shared_names(probe)) == ["Clash"]
    # And ninja indeed keeps only one of them: what the frontend would have been typed with.
    components = probe.get_openapi_schema(path_prefix="")["components"]["schemas"]
    assert set(components["Clash"]["properties"]) == {"ok", "message"}
