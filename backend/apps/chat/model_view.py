"""What the chat model reads of a tool's result (design §7.2; #30).

A tool's output is stored as its UI part and rendered by `tool-part.tsx`, so it keeps plain
values: a card shows "Staff Engineer", not a tag. The model reads a different copy. Before every
model request, `PostingFields` rewrites the tool returns it is about to send: each value a
posting supplied, or a review wrote about one (a title, a company, a place, a headline, the
text of an evaluation), is wrapped with `untrusted.field()`. The rule in the system prompt then
covers it like the full description `get_job` already wraps.

Only the request changes. The stored messages, the UI stream and `RunContext.messages` keep the
plain values, and a replayed history goes through the same rewrite on every request, so the
model never reads one of these fields unwrapped and a provider's prompt cache still sees the
same prefix each time.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
from pydantic_ai.models import ModelRequestContext

from apps.llm.untrusted import field

__all__ = ["POSTING_FIELDS", "PostingFields", "model_view", "wrap_fields"]

type Path = tuple[str, ...]

_EACH: Final = "*"  # a path step into every item of a list

_POSTING: Final[tuple[Path, ...]] = (("title",), ("company",), ("locations", _EACH))
_FAST_EVAL: Final[tuple[Path, ...]] = (
    ("fit_reasons", _EACH),
    ("red_flags", _EACH),
    ("hard_requirement_misses", _EACH),
)
_DEEP_EVAL: Final[tuple[Path, ...]] = (
    ("matched_requirements", _EACH, "requirement"),
    ("gaps", _EACH, "gap"),
    ("gaps", _EACH, "note"),
    ("risk_flags", _EACH),
    ("talking_points", _EACH),
    ("summary",),
)


def _under(prefix: Path, paths: Sequence[Path]) -> tuple[Path, ...]:
    return tuple((*prefix, *path) for path in paths)


# Tool -> where its output holds third-party text. Outputs keep the keys `tools.OUTPUT_MODELS`
# describes (camelCase at the top, the stored snake_case inside an evaluation).
POSTING_FIELDS: Final[Mapping[str, tuple[Path, ...]]] = {
    "list_jobs": (*_under(("jobs", _EACH), _POSTING), ("jobs", _EACH, "headline")),
    "get_job": (
        *_POSTING,
        *_under(("match", "fastEval"), _FAST_EVAL),
        *_under(("match", "deepEval"), _DEEP_EVAL),
    ),
    "list_pending_approvals": _under(
        ("approvals", _EACH, "items", _EACH), (("title",), ("company",), ("summary",))
    ),
    "list_companies": (("sample", _EACH, "name"),),
}


def _wrap_at(value: Any, path: Path) -> Any:
    """`value` with the strings at `path` wrapped; anything off the path is the same object."""
    if not path:
        return field(value) if isinstance(value, str) else value
    step, rest = path[0], path[1:]
    if step == _EACH:
        if not isinstance(value, list):
            return value
        return [_wrap_at(item, rest) for item in value]
    if not isinstance(value, Mapping) or step not in value:
        return value
    return {**value, step: _wrap_at(value[step], rest)}


def wrap_fields(tool_name: str, content: Any) -> Any:
    """A tool's output as the model reads it. Outputs of other shapes (an imported chat's, a
    failure's) pass through: a path that is not there is skipped."""
    for path in POSTING_FIELDS.get(tool_name, ()):
        content = _wrap_at(content, path)
    return content


def _part(part: Any) -> Any:
    if not isinstance(part, ToolReturnPart) or part.tool_name not in POSTING_FIELDS:
        return part
    return dataclasses.replace(part, content=wrap_fields(part.tool_name, part.content))


def model_view(messages: Sequence[ModelMessage]) -> list[ModelMessage]:
    """The messages with every tool return rewritten; new objects wherever something changed,
    so the persistent history is never touched."""
    return [
        dataclasses.replace(message, parts=[_part(part) for part in message.parts])
        if isinstance(message, ModelRequest)
        and any(isinstance(part, ToolReturnPart) for part in message.parts)
        else message
        for message in messages
    ]


@dataclass
class PostingFields(AbstractCapability[Any]):
    """Rewrites each request's tool returns with `model_view` (see the module docstring)."""

    async def before_model_request(
        self, ctx: RunContext[Any], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        return dataclasses.replace(request_context, messages=model_view(request_context.messages))
