"""Stored UI messages → the model history of one turn (design §7.4).

`messages` rows hold AI SDK `UIMessage`s only; the model history is derived per turn. This is
Foray's own converter (`VercelAIAdapter.load_messages` rejects v7 fields):

- A **user message** becomes `ModelRequest([UserPromptPart(text)])`. Consecutive requests merge
  into one (a run-update notice followed by the user's reply; a step's tool returns followed by
  the next user message).
- An **assistant message** is split at each `step-start`. Each block becomes a `ModelResponse`
  of its `TextPart`s and `ToolCallPart`s, then a `ModelRequest` of their `ToolReturnPart`s.
- A **failed call** (`output-error`) returns `"Error: <errorText>"`.
- A **call with no output** (`input-streaming`/`input-available`, left by Stop or a crash, or an
  approval state) returns `INTERRUPTED`, so providers never see a dangling call and the model
  does not forget a side effect that may have happened.
- **Reasoning, provider metadata, sources, files and data parts are dropped.** History replays
  as plain text and tool turns, which every provider accepts statelessly.
- **Nothing is validated against today's tools**, so a chat that used a renamed or removed tool
  still converts.
- **Window:** the first user message, plus the most recent messages up to `WINDOW_MESSAGES` and
  `WINDOW_CHARS` (text and tool JSON). Cuts fall only at message boundaries, and every assistant
  message carries its own call/return pairs, so pairs never split. When older messages were cut,
  the kept tail starts at a user message (a reply is never shown without its request).

`turn_input(rows)` gives the agent's arguments: the latest user message as `user_prompt`, the
rest as `message_history`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelRequestPart,
    ModelResponse,
    ModelResponsePart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

type UIMessage = Mapping[str, Any]

INTERRUPTED: Final = (
    "This call was interrupted before it returned; it may or may not have taken effect."
)
DENIED: Final = "The user declined this call."
WINDOW_MESSAGES: Final = 40
WINDOW_CHARS: Final = 60_000


@dataclass(frozen=True)
class TurnInput:
    """`agent.run_stream_events(user_prompt=..., message_history=...)` for one turn."""

    user_prompt: str | None
    message_history: list[ModelMessage] = field(default_factory=list[ModelMessage])

    def as_kwargs(self) -> dict[str, Any]:
        return {"user_prompt": self.user_prompt, "message_history": self.message_history}


def turn_input(messages: Sequence[UIMessage]) -> TurnInput:
    """The windowed conversation, split into the latest user request and what came before."""
    kept = window(messages)
    if kept and kept[-1].get("role") == "user":
        return TurnInput(user_text(kept[-1]) or None, to_model_messages(kept[:-1]))
    return TurnInput(None, to_model_messages(kept))


def to_model_messages(messages: Sequence[UIMessage]) -> list[ModelMessage]:
    """Every message, converted (no window)."""
    history: list[ModelMessage] = []
    for message in messages:
        role = message.get("role")
        if role == "user":
            text = user_text(message)
            if text:
                _append_request(history, [UserPromptPart(text)])
        elif role == "assistant":
            for block in _blocks(_parts(message)):
                response, returns = _convert_block(block)
                if response:
                    history.append(ModelResponse(parts=response))
                if returns:
                    _append_request(history, returns)
    return history


def user_text(message: UIMessage) -> str:
    """The text of a message's text parts, joined by newlines."""
    texts = [p.get("text") for p in _parts(message) if p.get("type") == "text"]
    return "\n".join(t for t in texts if isinstance(t, str) and t)


def window(messages: Sequence[UIMessage]) -> list[UIMessage]:
    """The first user message plus the latest messages within the count and size budgets."""
    if not messages:
        return []
    first = next((i for i, m in enumerate(messages) if m.get("role") == "user"), None)
    end = start = len(messages)
    chars = 0
    while start > 0 and end - start < WINDOW_MESSAGES:
        size = message_size(messages[start - 1])
        if start < end and chars + size > WINDOW_CHARS:
            break  # the latest message is always kept
        chars += size
        start -= 1
    if first is None or first >= start:
        return list(messages[start:])
    tail = list(messages[start:])
    if start > first + 1:  # a gap: do not open on a reply whose request was cut
        while tail and tail[0].get("role") != "user":
            tail.pop(0)
    return [messages[first], *tail]


def message_size(message: UIMessage) -> int:
    """What a message contributes to the model's context: its text and its tool JSON."""
    size = 0
    for part in _parts(message):
        kind = str(part.get("type", ""))
        if kind == "text":
            size += len(str(part.get("text") or ""))
        elif _tool_name(part) is not None:
            for key in ("input", "rawInput", "output"):
                if key in part:
                    size += len(_json(part[key]))
            size += len(str(part.get("errorText") or ""))
    return size


# --- internals ----------------------------------------------------------------------------------


def _parts(message: UIMessage) -> list[Mapping[str, Any]]:
    parts = message.get("parts")
    return [p for p in parts if isinstance(p, Mapping)] if isinstance(parts, list) else []


def _blocks(parts: list[Mapping[str, Any]]) -> list[list[Mapping[str, Any]]]:
    blocks: list[list[Mapping[str, Any]]] = [[]]
    for part in parts:
        if part.get("type") == "step-start":
            if blocks[-1]:
                blocks.append([])
            continue
        blocks[-1].append(part)
    return [b for b in blocks if b]


def _tool_name(part: Mapping[str, Any]) -> str | None:
    kind = str(part.get("type", ""))
    if kind == "dynamic-tool":
        name = part.get("toolName")
        return name if isinstance(name, str) and name else None
    if kind.startswith("tool-") and len(kind) > len("tool-"):
        return kind[len("tool-") :]
    return None


def _convert_block(
    block: list[Mapping[str, Any]],
) -> tuple[list[ModelResponsePart], list[ModelRequestPart]]:
    response: list[ModelResponsePart] = []
    returns: list[ModelRequestPart] = []
    for part in block:
        if part.get("type") == "text":
            text = part.get("text")
            if isinstance(text, str) and text:
                response.append(TextPart(text))
            continue
        name = _tool_name(part)
        call_id = part.get("toolCallId")
        if name is None or not isinstance(call_id, str) or not call_id:
            continue  # reasoning, sources, files, data parts and anything unknown
        response.append(ToolCallPart(tool_name=name, args=_args(part), tool_call_id=call_id))
        returns.append(ToolReturnPart(tool_name=name, content=_result(part), tool_call_id=call_id))
    return response, returns


def _args(part: Mapping[str, Any]) -> dict[str, Any]:
    value = part["input"] if "input" in part else part.get("rawInput")
    if isinstance(value, dict):
        return value
    return {} if value is None else {"input": value}


def _result(part: Mapping[str, Any]) -> Any:
    state = part.get("state")
    if state == "output-available":
        return part.get("output")
    if state == "output-error":
        return f"Error: {part.get('errorText') or ''}"
    if state == "output-denied":
        return f"Error: {DENIED}"
    return INTERRUPTED  # no output yet, or a state this code does not know: the result is unknown


def _append_request(history: list[ModelMessage], parts: Sequence[ModelRequestPart]) -> None:
    if history and isinstance(history[-1], ModelRequest):
        history[-1] = ModelRequest(parts=[*history[-1].parts, *parts])
    else:
        history.append(ModelRequest(parts=list(parts)))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
