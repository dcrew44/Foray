"""Extra OpenAPI components (design §5.1, §5.4).

Some types the frontend needs never appear in an operation's signature: the event and run kinds,
`LiveSnapshot`, the chat tools' input and output models. Each app's `api.py` registers them at
import with `register_schema(Model)`, and `foray.api.ForayAPI.get_openapi_schema` merges them
into `components.schemas`, so `openapi-typescript` generates them too.
"""

from __future__ import annotations

from typing import Any

from pydantic import TypeAdapter

REF_TEMPLATE = "#/components/schemas/{model}"

_REGISTRY: dict[str, Any] = {}


def register_schema[T](tp: T, name: str | None = None) -> T:
    """Add a pydantic model, enum or other type to the schema. Usable as a class decorator."""
    key = name or getattr(tp, "__name__", None)
    if not key:
        raise ValueError(f"register_schema() needs a name for {tp!r}")
    existing = _REGISTRY.get(key)
    if existing is not None and existing is not tp:
        raise ValueError(f"Two different types are registered as {key!r}")
    _REGISTRY[key] = tp
    return tp


def registered() -> dict[str, Any]:
    return dict(_REGISTRY)


def extra_schemas() -> dict[str, dict[str, Any]]:
    """Every registered type's JSON schema (serialization mode, camelCase aliases), plus the
    definitions they reference, keyed by component name."""
    components: dict[str, dict[str, Any]] = {}
    for key, tp in _REGISTRY.items():
        schema = TypeAdapter(tp).json_schema(
            by_alias=True, mode="serialization", ref_template=REF_TEMPLATE
        )
        for def_name, definition in schema.pop("$defs", {}).items():
            components.setdefault(def_name, definition)
        components[key] = schema
    return components
