"""Deterministic fake models for `LLM_FAKE=1` (DEBUG only; design §8.9).

Every purpose maps to a pydantic-ai `FunctionModel`. The fakes still go through all four
wrappers of the stack, so budget checks, rate limiting and ledger rows all happen: rows are
labelled `fake` and cost 0.

**Schema-driven by default.** For a structured call the fake answers with the smallest valid
instance of the output tool's JSON schema: required keys only, nulls where allowed, the minimum
of each range, the first enum member. So the fakes work for output types other lanes write,
without this module importing their schemas. Anything else gets a short text reply.

**Overrides.** The lane that owns a purpose registers a better answer, in its app's `fakes.py`
(imported on first use, like Django's admin autodiscovery):

    from apps.llm import fake

    @fake.register("score")
    def score(messages, info):
        if fake.output_title(info) != "FastScore":
            return None  # not mine: the next override, or the schema-driven default
        return {"score": ..., ...}

An override receives the request's messages and the `AgentInfo` (`info.function_tools` are the
tools this turn offers, `info.output_tools` the structured output tool) and returns:

- `None`: not handled here;
- a `dict` or pydantic model: the structured output (a call to the output tool);
- a `str`: a text reply;
- a `ModelResponse`: full control, e.g. `fake.tool_call("get_resume", {})`.

It may be sync or async. Several overrides may share a purpose (`extract` serves resumes and
careers pages); they are tried in registration order. On a streamed request, a reply that calls
tools streams the calls only (any text beside them is dropped); a text reply streams as text.

**Switches** (both need `LLM_FAKE`):
- `LLM_FAKE_FAIL=<purpose>[,...]` makes those fakes raise `RuntimeError("Fake failure")`;
- `LLM_FAKE_DELAY_MS=<n>` streams text replies in 20 chunks spread over `n` ms and makes every
  other reply wait `n` ms, so Stop can land mid-reply.
"""

from __future__ import annotations

import asyncio
import inspect
import math
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any, overload

from django.conf import settings
from pydantic import BaseModel
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel
from pydantic_ai.tools import ToolDefinition

__all__ = [
    "DEFAULT_TEXT",
    "FakeFailure",
    "Override",
    "Reply",
    "fake_model",
    "latest_user_text",
    "minimal_instance",
    "offered_tools",
    "output_schema",
    "output_title",
    "register",
    "text",
    "tool_call",
    "unregister",
]

DEFAULT_TEXT = "This is a fake reply from Foray's test model. Nothing was sent to a provider."
STREAM_CHUNKS = 20

type Reply = ModelResponse | str | Mapping[str, Any] | BaseModel | None
type Override = Callable[[list[ModelMessage], AgentInfo], Reply | Awaitable[Reply]]

_OVERRIDES: dict[str, list[Override]] = {}
_discovered = False


class FakeFailure(RuntimeError):
    """What `LLM_FAKE_FAIL` raises: `RuntimeError("Fake failure")`."""

    def __init__(self) -> None:
        super().__init__("Fake failure")


# --- registration -------------------------------------------------------------------------------


@overload
def register(purpose: str) -> Callable[[Override], Override]: ...
@overload
def register(purpose: str, fn: Override) -> Override: ...
def register(purpose: str, fn: Override | None = None) -> Any:
    """Add an override for `purpose`. Usable as `register("score", fn)` or as a decorator."""
    from foray.env import PURPOSES

    if purpose not in PURPOSES:
        raise ValueError(f"Not an LLM purpose: {purpose!r}")
    if fn is None:
        return lambda f: register(purpose, f)
    found = _OVERRIDES.setdefault(purpose, [])
    if fn not in found:
        found.append(fn)
    return fn


def unregister(purpose: str, fn: Override) -> None:
    found = _OVERRIDES.get(purpose, [])
    if fn in found:
        found.remove(fn)


def discover() -> None:
    """Import every installed app's `fakes` module once, so lanes' overrides register."""
    global _discovered
    if _discovered:
        return
    _discovered = True
    from django.utils.module_loading import autodiscover_modules

    autodiscover_modules("fakes")


# --- helpers for overrides ----------------------------------------------------------------------


def text(content: str) -> ModelResponse:
    return ModelResponse(parts=[TextPart(content=content)])


def tool_call(
    name: str, args: Mapping[str, Any] | None = None, *, call_id: str | None = None
) -> ModelResponse:
    """A response that calls one tool (a function tool or the output tool)."""
    part = ToolCallPart(tool_name=name, args=dict(args or {}))
    if call_id is not None:
        part.tool_call_id = call_id
    return ModelResponse(parts=[part])


def _user_text(part: UserPromptPart) -> str:
    if isinstance(part.content, str):
        return part.content
    return "\n".join(item for item in part.content if isinstance(item, str))


def latest_user_text(messages: list[ModelMessage]) -> str:
    """The newest user prompt's text ("" when the newest request has none)."""
    for message in reversed(messages):
        if isinstance(message, ModelRequest):
            texts = [_user_text(p) for p in message.parts if isinstance(p, UserPromptPart)]
            if texts:
                return "\n".join(texts)
    return ""


def offered_tools(info: AgentInfo) -> set[str]:
    """The function tools this turn offers (not the output tool)."""
    return {tool.name for tool in info.function_tools}


def _output_tool(info: AgentInfo) -> ToolDefinition | None:
    return info.output_tools[0] if info.output_tools else None


def output_schema(info: AgentInfo) -> dict[str, Any] | None:
    tool = _output_tool(info)
    return tool.parameters_json_schema if tool is not None else None


def output_title(info: AgentInfo) -> str | None:
    """The output type's name (pydantic's schema `title`), to tell callers of one purpose apart."""
    schema = output_schema(info)
    title = schema.get("title") if schema else None
    return title if isinstance(title, str) else None


# --- the smallest valid instance of a JSON schema -----------------------------------------------


_FORMATS = {
    "date-time": "1970-01-01T00:00:00Z",
    "date": "1970-01-01",
    "time": "00:00:00",
    "uuid": "00000000-0000-0000-0000-000000000000",
    "email": "fake@example.com",
    "uri": "https://example.com/",
}


def _resolve(ref: str, root: Mapping[str, Any]) -> Mapping[str, Any]:
    node: Any = root
    for key in ref.removeprefix("#/").split("/"):
        node = node[key.replace("~1", "/").replace("~0", "~")]
    return node


def _allows_null(schema: Mapping[str, Any]) -> bool:
    kind = schema.get("type")
    if kind == "null" or (isinstance(kind, list) and "null" in kind):
        return True
    return any(_allows_null(option) for key in ("anyOf", "oneOf") for option in schema.get(key, []))


def _number(schema: Mapping[str, Any], *, integer: bool) -> int | float:
    """The range's minimum; 0 when unbounded below and 0 is allowed."""
    high = schema.get("maximum")
    exclusive_high = schema.get("exclusiveMaximum")
    if (low := schema.get("minimum")) is not None:
        value = low
    elif (exclusive_low := schema.get("exclusiveMinimum")) is not None:
        ceiling = high if high is not None else exclusive_high
        step = exclusive_low + 1
        value = step if ceiling is None or step <= ceiling else (exclusive_low + ceiling) / 2
    elif high is not None and high < 0:
        value = high
    elif exclusive_high is not None and exclusive_high <= 0:
        value = exclusive_high - 1
    else:
        value = 0
    return math.ceil(value) if integer else value


def minimal_instance(schema: Mapping[str, Any], root: Mapping[str, Any] | None = None) -> Any:
    """Required keys only, nulls where allowed, each range's minimum, the first enum member."""
    root = schema if root is None else root
    if "$ref" in schema:
        return minimal_instance(_resolve(schema["$ref"], root), root)
    if "const" in schema:
        return schema["const"]
    if schema.get("enum"):
        return schema["enum"][0]
    if _allows_null(schema):
        return None
    for key in ("anyOf", "oneOf", "allOf"):
        if options := schema.get(key):
            return minimal_instance(options[0], root)
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = kind[0] if kind else None
    if kind is None and "properties" in schema:
        kind = "object"
    match kind:
        case "object":
            properties = schema.get("properties", {})
            return {
                name: minimal_instance(properties.get(name, {}), root)
                for name in schema.get("required", [])
            }
        case "array":
            prefix = schema.get("prefixItems", [])
            items = schema.get("items", {}) if isinstance(schema.get("items"), Mapping) else {}
            count = max(schema.get("minItems", 0), len(prefix))
            return [
                minimal_instance(prefix[i] if i < len(prefix) else items, root)
                for i in range(count)
            ]
        case "string":
            value = _FORMATS.get(schema.get("format", ""), "")
            return value.ljust(schema.get("minLength", 0), "x")
        case "integer":
            return _number(schema, integer=True)
        case "number":
            return _number(schema, integer=False)
        case "boolean":
            return False
        case _:
            return None


# --- the model ----------------------------------------------------------------------------------


def _check_fail(purpose: str) -> None:
    if purpose in settings.ENV.llm_fake_fail:
        raise FakeFailure()


def _delay_seconds() -> float:
    return settings.ENV.llm_fake_delay_ms / 1000


def _default(info: AgentInfo) -> Reply:
    """Structured calls get the schema's smallest instance; everything else a short text."""
    schema = output_schema(info)
    if schema is not None and (not info.allow_text_output or not info.function_tools):
        return minimal_instance(schema)
    return DEFAULT_TEXT


async def _reply(purpose: str, messages: list[ModelMessage], info: AgentInfo) -> Reply:
    for override in list(_OVERRIDES.get(purpose, [])):
        result = override(messages, info)
        if inspect.isawaitable(result):
            result = await result
        if result is not None:
            return result
    return _default(info)


def as_response(reply: Reply, info: AgentInfo) -> ModelResponse:
    if isinstance(reply, ModelResponse):
        return reply
    if isinstance(reply, str):
        return text(reply)
    if isinstance(reply, BaseModel):
        reply = reply.model_dump(mode="json", by_alias=True)
    tool = _output_tool(info)
    if tool is None or reply is None:
        raise TypeError("A structured fake reply needs an output tool on the request")
    return tool_call(tool.name, reply)


def fake_model(purpose: str) -> FunctionModel:
    """The fake for one purpose: deterministic, free, and still a real pydantic-ai model."""
    discover()

    async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        _check_fail(purpose)
        response = as_response(await _reply(purpose, messages, info), info)
        if delay := _delay_seconds():
            await asyncio.sleep(delay)
        return response

    async def stream(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | DeltaToolCalls]:
        _check_fail(purpose)
        response = as_response(await _reply(purpose, messages, info), info)
        delay = _delay_seconds()
        calls = [part for part in response.parts if isinstance(part, ToolCallPart)]
        if calls:
            if delay:
                await asyncio.sleep(delay)
            yield {
                index: DeltaToolCall(
                    name=call.tool_name,
                    json_args=call.args_as_json_str(),
                    tool_call_id=call.tool_call_id,
                )
                for index, call in enumerate(calls)
            }
            return
        content = "".join(part.content for part in response.parts if isinstance(part, TextPart))
        chunks = _chunks(content, STREAM_CHUNKS if delay else 1)
        for chunk in chunks:
            if delay:
                await asyncio.sleep(delay / len(chunks))
            yield chunk

    return FunctionModel(respond, stream_function=stream, model_name=f"fake-{purpose}")


def _chunks(content: str, count: int) -> list[str]:
    if not content:
        return [""]
    count = max(1, min(count, len(content)))
    size, extra = divmod(len(content), count)
    chunks: list[str] = []
    start = 0
    for index in range(count):
        end = start + size + (1 if index < extra else 0)
        chunks.append(content[start:end])
        start = end
    return chunks
