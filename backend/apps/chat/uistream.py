"""The AI SDK v7 UI message stream, owned by Foray (design §7.3, research/ui-stream-protocol.md).

The browser's `useChat` (ai 7.0.106) parses every SSE event against a zod union and folds the
chunks into one assistant `UIMessage`. This module writes that wire and keeps the server's copy
of the message in step with the client's:

- `frame(chunk)` encodes one chunk as `data: <json>\\n\\n`: compact, `allow_nan=False`, optional
  `None` keys dropped (the client rejects `null` in an optional field), the required
  `unknown`-typed keys (`input`, `output`, `data`, `messageMetadata`) kept as `null`.
- `UIMessageAccumulator` is the Python twin of `processUIMessageStream`. It is byte-equal to the
  message the TS server persisted, so `acc.message` is what a reload shows.
- `UIMessageStream` is a hand emitter for canned replies, errors and tests.
- `ForayVercelStream` is pydantic-ai's event-to-chunk mapper with Foray's overrides: the pinned
  reply id, no `pydantic_ai` metadata, a tool's `None` output kept, and agent errors mapped to the
  user's text. `VercelAIAdapter` is not used (§7.3).

A chunk is a plain dict in wire shape (camelCase keys). The chat view tees every chunk it sends
into an accumulator and frames it:

    acc = UIMessageAccumulator(reply_id)
    async for chunk in ForayVercelStream(turn).chunks(events):   # dicts; no [DONE]
        acc.apply(chunk)
        yield frame(chunk)
    ...
    yield DONE                                                   # after the reply is saved
"""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import AsyncIterator, Iterator, Mapping
from typing import Any, Final, Protocol

from pydantic_ai import UsageLimitExceeded
from pydantic_ai.run import AgentRunResultEvent
from pydantic_ai.ui.vercel_ai import VercelAIEventStream
from pydantic_ai.ui.vercel_ai.response_types import (
    BaseChunk,
    DoneChunk,
    ErrorChunk,
    MessageMetadataChunk,
    StartChunk,
)

from apps.core.errors import UserFacingError

log = logging.getLogger("foray.chat")

type Chunk = dict[str, Any]

DONE: Final = "data: [DONE]\n\n"
PING: Final = ": ping\n\n"  # an SSE comment: a heartbeat the client parser ignores

UI_HEADERS: Final[Mapping[str, str]] = {
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-cache, no-transform",  # no-transform: Next's proxy must not gzip-buffer
    "X-Vercel-AI-UI-Message-Stream": "v1",
    "X-Accel-Buffering": "no",
}

GENERIC_ERROR: Final = "An error occurred."
STEP_LIMIT_ERROR: Final = (
    "That needed more steps than one reply allows. Ask again, or split it into smaller requests."
)

FINISH_REASONS: Final = frozenset(
    {"stop", "length", "content-filter", "tool-calls", "error", "other"}
)


def ui_headers(conversation_id: object) -> dict[str, str]:
    """The response headers of one chat turn (§7.3)."""
    return {**UI_HEADERS, "X-Conversation-Id": str(conversation_id)}


# --- the wire ---------------------------------------------------------------------------------

# Keys the client schema requires even when null (`z.unknown()` rejects a missing key, accepts
# null). Every other key is optional, and an optional key must be absent rather than null.
_REQUIRED_NULLABLE: Final = {
    "tool-input-available": "input",
    "tool-input-error": "input",
    "tool-output-available": "output",
    "message-metadata": "messageMetadata",
}


def _required_nullable(chunk_type: object) -> str | None:
    if isinstance(chunk_type, str) and chunk_type.startswith("data-"):
        return "data"
    return _REQUIRED_NULLABLE.get(chunk_type) if isinstance(chunk_type, str) else None


def wire_chunk(chunk: Mapping[str, Any]) -> Chunk:
    """The chunk exactly as the client receives it: `None` dropped from optional keys, the
    required nullable key present (a tool returning `None` streams `"output":null`, as TS did)."""
    keep = _required_nullable(chunk.get("type"))
    body = {k: v for k, v in chunk.items() if v is not None or k == keep}
    if keep is not None and keep not in body:
        body[keep] = None
    return body


def frame(chunk: Mapping[str, Any]) -> str:
    """One SSE event. Raises `ValueError` for NaN or infinity, which the browser cannot parse."""
    text = json.dumps(wire_chunk(chunk), separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return f"data: {text}\n\n"


# --- the accumulator: processUIMessageStream in Python ------------------------------------------


class _Undefined:
    """JavaScript's `undefined`. TS assigns `undefined` to keys it does not have a value for; the
    key keeps its place in the object (which fixes the key order `JSON.stringify` writes) but is
    not written. `UIMessageAccumulator.state` keeps these slots; `.message` drops them."""

    _instance: _Undefined | None = None

    def __new__(cls) -> _Undefined:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "undefined"

    def __bool__(self) -> bool:
        return False

    def __copy__(self) -> _Undefined:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> _Undefined:
        return self

    def __reduce__(self) -> tuple[type[_Undefined], tuple[()]]:
        return (_Undefined, ())


UNDEFINED: Final = _Undefined()


def _get(obj: Mapping[str, Any], key: str) -> Any:
    return obj.get(key, UNDEFINED)


def _nullish(value: Any) -> bool:
    return value is None or value is UNDEFINED


def _clean(value: Any) -> Any:
    """What `JSON.parse(JSON.stringify(value))` gives: `undefined` keys dropped, a deep copy."""
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if v is not UNDEFINED}
    if isinstance(value, list):
        return [None if v is UNDEFINED else _clean(v) for v in value]
    return value


class UIMessageStreamError(ValueError):
    """A chunk out of order (the client would end the turn with `AI_UIMessageStreamError`)."""


def merge_objects(base: Any, overrides: Any) -> Any:
    """`mergeObjects` (ai/src/util/merge-objects.ts): a deep merge; arrays and primitives replace,
    `undefined` overrides are skipped, prototype keys ignored."""
    if base is UNDEFINED and overrides is UNDEFINED:
        return UNDEFINED
    if base is UNDEFINED:
        return overrides
    if overrides is UNDEFINED:
        return base
    if not isinstance(base, dict) or not isinstance(overrides, dict):
        return overrides
    result = dict(base)
    for key, value in overrides.items():
        if key in ("__proto__", "constructor", "prototype") or value is UNDEFINED:
            continue
        current = base.get(key, UNDEFINED)
        if isinstance(value, dict) and isinstance(current, dict):
            result[key] = merge_objects(current, value)
        else:
            result[key] = value
    return result


def _is_static_tool(part: Mapping[str, Any]) -> bool:
    return str(part.get("type", "")).startswith("tool-")


def _is_tool(part: Mapping[str, Any]) -> bool:
    return _is_static_tool(part) or part.get("type") == "dynamic-tool"


def _static_tool_name(part: Mapping[str, Any]) -> str:
    return str(part["type"]).split("-", 1)[1]


def _pick(chunk: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {k: chunk[k] for k in keys if k in chunk}


# Parts pushed as their chunk arrives, with the keys TS copies, in its order.
_PUSHED: Final = {
    "file": ("mediaType", "url", "providerMetadata"),
    "reasoning-file": ("mediaType", "url", "providerMetadata"),
    "custom": ("kind", "providerMetadata"),
    "source-url": ("sourceId", "url", "title", "providerMetadata"),
    "source-document": ("sourceId", "mediaType", "title", "filename", "providerMetadata"),
}
# The slots of a new tool part, in the order TS creates them (around `toolMetadata`).
_NEW_STATIC_SLOTS: Final = (
    "input",
    "output",
    "rawInput",
    "errorText",
    "providerExecuted",
    "preliminary",
)
_NEW_DYNAMIC_SLOTS: Final = (
    "input",
    "output",
    "errorText",
    "preliminary",
    "providerExecuted",
    "title",
)
# What TS passes on from a tool-input chunk to `updateToolPart`.
_CALL_KEYS: Final = (
    "toolCallId",
    "toolName",
    "providerExecuted",
    "providerMetadata",
    "title",
    "toolMetadata",
)


class UIMessageAccumulator:
    """Folds chunks into one assistant `UIMessage`, exactly as the client does.

    A port of `processUIMessageStream` (ai 7.0.106, `src/ui/process-ui-message-stream.ts`),
    including its ordering errors and `parsePartialJson` for streaming tool input. `state` is
    the message with JavaScript's `undefined` slots; `message` is what `JSON.stringify` writes,
    which is what the chat view saves. `last_message` continues an assistant message (the AI SDK
    approval round trip; Foray itself always starts a new reply).
    """

    def __init__(self, message_id: str, last_message: Mapping[str, Any] | None = None) -> None:
        if last_message is not None and last_message.get("role") == "assistant":
            self.state: dict[str, Any] = copy.deepcopy(dict(last_message))
        else:
            self.state = {"id": message_id, "metadata": UNDEFINED, "role": "assistant", "parts": []}
        self._text: dict[str, dict[str, Any]] = {}
        self._reasoning: dict[str, dict[str, Any]] = {}
        self._partial: dict[str, dict[str, Any]] = {}
        self._step_open = False
        self.finished = False
        self.finish_reason: str | None = None
        self.error: str | None = None  # the last `error` chunk's text
        self.aborted = False

    @property
    def message(self) -> dict[str, Any]:
        """The message as JSON would carry it (a deep copy)."""
        return _clean(self.state)

    @property
    def parts(self) -> list[dict[str, Any]]:
        return self.state["parts"]

    # -- folding ----------------------------------------------------------------------------

    def apply(self, chunk: Mapping[str, Any]) -> None:  # noqa: C901 (one switch, as in TS)
        c = wire_chunk(chunk)
        t = c.get("type")
        if t in ("text-start", "reasoning-start"):
            kind = t.removesuffix("-start")
            part = {"type": kind, "id": c["id"]} if kind == "reasoning" else {"type": kind}
            part |= {"text": "", "providerMetadata": _get(c, "providerMetadata")}
            part["state"] = "streaming"
            (self._text if kind == "text" else self._reasoning)[c["id"]] = part
            self.parts.append(part)
        elif t in ("text-delta", "text-end", "reasoning-delta", "reasoning-end"):
            active = self._text if t.startswith("text") else self._reasoning
            part = active.get(c["id"])
            if part is None:
                kind = "text" if t.startswith("text") else "reasoning"
                raise UIMessageStreamError(
                    f"Received {t} for missing {kind} part with ID {c['id']!r}."
                )
            if t.endswith("-delta"):
                part["text"] += c["delta"]
            pm = _get(c, "providerMetadata")
            part["providerMetadata"] = pm if not _nullish(pm) else _get(part, "providerMetadata")
            if t.endswith("-end"):
                part["state"] = "done"
                del active[c["id"]]
        elif t in _PUSHED:
            self.parts.append({"type": t, **{k: _get(c, k) for k in _PUSHED[t]}})
        elif t == "tool-input-start":
            keys = ("toolName", "dynamic", "title", "toolMetadata")
            self._partial[c["toolCallId"]] = {"text": "", **{k: _get(c, k) for k in keys}}
            options = {**_pick(c, _CALL_KEYS), "state": "input-streaming", "input": UNDEFINED}
            self._update(bool(c.get("dynamic")), options)
        elif t == "tool-input-delta":
            partial = self._partial.get(c["toolCallId"])
            if partial is None:
                raise UIMessageStreamError(
                    f"Received tool-input-delta for missing tool call with ID {c['toolCallId']!r}."
                )
            partial["text"] += c["inputTextDelta"]
            options = {
                "toolCallId": c["toolCallId"],
                "state": "input-streaming",
                "input": parse_partial_json(partial["text"]),
                **{k: partial[k] for k in ("toolName", "title", "toolMetadata")},
            }
            self._update(bool(partial["dynamic"]), options)
        elif t == "tool-input-available":
            options = {**_pick(c, _CALL_KEYS), "state": "input-available", "input": c["input"]}
            self._update(bool(c.get("dynamic")), options)
        elif t == "tool-input-error":
            # An existing part keeps its kind; the error carries no title.
            existing = next(
                (
                    p
                    for p in self._step_parts()
                    if _is_tool(p) and p["toolCallId"] == c["toolCallId"]
                ),
                None,
            )
            dynamic = (
                bool(c.get("dynamic")) if existing is None else existing["type"] == "dynamic-tool"
            )
            options = {**_pick(c, _CALL_KEYS), "title": UNDEFINED, "state": "output-error"}
            options["errorText"] = c["errorText"]
            if dynamic:
                options["input"] = c["input"]
            else:
                options["rawInput"] = c["input"]  # static parts keep invalid input as rawInput
            self._update(dynamic, options)
        elif t == "tool-approval-request":
            part = self._tool(c["toolCallId"])
            part["state"] = "approval-requested"
            approval: dict[str, Any] = {"id": c["approvalId"]}
            if not _nullish(_get(c, "approvalDescriptor")):
                approval["descriptor"] = c["approvalDescriptor"]
            if not _nullish(_get(c, "reason")):
                approval["requestReason"] = c["reason"]
            if c.get("isAutomatic") is True:
                approval["isAutomatic"] = True
            if not _nullish(_get(c, "signature")):
                approval["signature"] = c["signature"]
            part["approval"] = approval
        elif t == "tool-approval-response":
            part = next(
                (
                    p
                    for p in self.parts
                    if _is_tool(p)
                    and isinstance(p.get("approval"), dict)
                    and p["approval"].get("id") == c["approvalId"]
                ),
                None,
            )
            if part is None:
                raise UIMessageStreamError(
                    f"No tool invocation found for approval ID {c['approvalId']!r}."
                )
            previous = part.get("approval")
            approval = dict(previous) if isinstance(previous, dict) else {"id": c["approvalId"]}
            part["state"] = "approval-responded"
            approval["id"] = c["approvalId"]
            approval["approved"] = c["approved"]
            if not _nullish(_get(c, "reason")):
                approval["reason"] = c["reason"]
            part["approval"] = approval
            if not _nullish(_get(c, "providerExecuted")):
                part["providerExecuted"] = c["providerExecuted"]
            if not _nullish(_get(c, "providerMetadata")):
                part["callProviderMetadata"] = c["providerMetadata"]
        elif t == "tool-output-denied":
            self._tool(c["toolCallId"])["state"] = "output-denied"
        elif t in ("tool-output-available", "tool-output-error"):
            part = self._tool(c["toolCallId"])
            dynamic = part["type"] == "dynamic-tool"
            options = {
                "toolCallId": c["toolCallId"],
                "toolName": part["toolName"] if dynamic else _static_tool_name(part),
                **{k: _get(part, k) for k in ("input", "title", "toolMetadata")},
                **_pick(c, ("providerExecuted", "providerMetadata")),
            }
            if t == "tool-output-available":
                options |= {"state": "output-available", "output": c["output"]}
                options["preliminary"] = _get(c, "preliminary")
            else:
                options |= {"state": "output-error", "errorText": c["errorText"]}
                if not dynamic:
                    options["rawInput"] = _get(part, "rawInput")
            self._update(dynamic, options, part)
        elif t == "start-step":
            self.parts.append({"type": "step-start"})
            self._step_open = True
        elif t == "finish-step":
            self._step_open = False
        elif t == "reset-step":
            current = self._step_parts()
            self._text, self._reasoning, self._partial = {}, {}, {}
            if current:
                del self.parts[len(self.parts) - len(current) :]
        elif t == "start":
            if not _nullish(_get(c, "messageId")):
                self.state["id"] = c["messageId"]
            self._metadata(_get(c, "messageMetadata"))
        elif t == "finish":
            if not _nullish(_get(c, "finishReason")):
                self.finish_reason = c["finishReason"]
            self._metadata(_get(c, "messageMetadata"))
            self.finished = True
        elif t == "message-metadata":
            self._metadata(c["messageMetadata"])
        elif t == "error":
            self.error = c["errorText"]
        elif t == "abort":
            self.aborted = True  # TS has no case for it: the message is left as it is
        elif isinstance(t, str) and t.startswith("data-"):
            if c.get("transient"):
                return
            existing = None
            if not _nullish(_get(c, "id")):
                existing = next(
                    (p for p in self.parts if p.get("type") == t and p.get("id") == c["id"]), None
                )
            if existing is not None:
                existing["data"] = c["data"]
            else:
                self.parts.append(copy.deepcopy(c))

    def close_with_error(self, text: str) -> list[Chunk]:
        """End the reply after an error the stream class never saw (the turn timeout, a bug in
        the view): close the open text and reasoning parts, send the error, close the step and
        finish. Returns the chunks, already applied, for the caller to frame."""
        chunks: list[Chunk] = [{"type": "text-end", "id": i} for i in list(self._text)]
        chunks += [{"type": "reasoning-end", "id": i} for i in list(self._reasoning)]
        chunks.append({"type": "error", "errorText": text})
        if self._step_open:
            chunks.append({"type": "finish-step"})
        if not self.finished:
            chunks.append({"type": "finish", "finishReason": "error"})
        for chunk in chunks:
            self.apply(chunk)
        return chunks

    # -- helpers ----------------------------------------------------------------------------

    def _step_parts(self) -> list[dict[str, Any]]:
        parts = self.parts
        i = len(parts) - 1
        while i >= 0 and parts[i].get("type") != "step-start":
            i -= 1
        return parts[i + 1 :]

    def _tool(self, call_id: str) -> dict[str, Any]:
        for part in self._step_parts():
            if _is_tool(part) and part.get("toolCallId") == call_id:
                return part
        for part in reversed(self.parts):
            if _is_tool(part) and part.get("toolCallId") == call_id:
                return part
        raise UIMessageStreamError(f"No tool invocation found for tool call ID {call_id!r}.")

    def _metadata(self, metadata: Any) -> None:
        if _nullish(metadata):
            return
        current = _get(self.state, "metadata")
        self.state["metadata"] = metadata if _nullish(current) else merge_objects(current, metadata)

    def _update(
        self, dynamic: bool, o: Mapping[str, Any], existing: dict[str, Any] | None = None
    ) -> None:
        """`updateToolPart` / `updateDynamicToolPart`, keeping TS's assignment order."""
        state = o["state"]
        result_side = state in ("output-available", "output-error")
        pm = _get(o, "providerMetadata")
        part = existing
        if part is None:
            kind = "dynamic-tool" if dynamic else None
            part = next(
                (
                    p
                    for p in self._step_parts()
                    if (p.get("type") == kind if dynamic else _is_static_tool(p))
                    and p.get("toolCallId") == o["toolCallId"]
                ),
                None,
            )
        if part is not None:
            part["state"] = state
            if dynamic:
                part["toolName"] = o["toolName"]
            for key in ("input", "output", "errorText"):
                part[key] = _get(o, key)
            raw = _get(o, "rawInput")
            part["rawInput"] = raw if not (dynamic and _nullish(raw)) else _get(part, "rawInput")
            part["preliminary"] = _get(o, "preliminary")
            if _get(o, "title") is not UNDEFINED:
                part["title"] = o["title"]
            if _get(o, "toolMetadata") is not UNDEFINED:
                part["toolMetadata"] = o["toolMetadata"]
            executed = _get(o, "providerExecuted")
            part["providerExecuted"] = (
                executed if not _nullish(executed) else _get(part, "providerExecuted")
            )
            if not _nullish(pm):
                part["resultProviderMetadata" if result_side else "callProviderMetadata"] = pm
            return
        new: dict[str, Any]
        if dynamic:
            new = {"type": "dynamic-tool", "toolName": o["toolName"], "toolCallId": o["toolCallId"]}
            new |= {"state": state, **{k: _get(o, k) for k in _NEW_DYNAMIC_SLOTS}}
        else:
            new = {"type": f"tool-{o['toolName']}", "toolCallId": o["toolCallId"], "state": state}
            new["title"] = _get(o, "title")
        if _get(o, "toolMetadata") is not UNDEFINED:
            new["toolMetadata"] = o["toolMetadata"]
        if not dynamic:
            new |= {k: _get(o, k) for k in _NEW_STATIC_SLOTS}
        if not _nullish(pm):
            new["resultProviderMetadata" if result_side else "callProviderMetadata"] = pm
        self.parts.append(new)


# --- parsePartialJson ---------------------------------------------------------------------------


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


def _secure_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = {k for k, _ in pairs}
    if "__proto__" in keys:
        raise ValueError("__proto__ is not allowed")
    obj = dict(pairs)
    proto = obj.get("constructor")
    if isinstance(proto, dict) and "prototype" in proto:
        raise ValueError("constructor.prototype is not allowed")
    return obj


def _parse_json(text: str) -> Any:
    """`JSON.parse` through `secureJsonParse`: no NaN or Infinity, no prototype keys."""
    return json.loads(text, parse_constant=_reject_constant, object_pairs_hook=_secure_object)


def parse_partial_json(text: str) -> Any:
    """`parsePartialJson(text).value`: the value, the repaired value, or `UNDEFINED`."""
    for candidate in (text, fix_json(text)):
        try:
            return _parse_json(candidate)
        except ValueError:
            continue
    return UNDEFINED


_OBJECT_STATES: Final = frozenset(
    {
        "OBJECT_KEY",
        "OBJECT_AFTER_KEY",
        "OBJECT_AFTER_COMMA",
        "OBJECT_START",
        "OBJECT_BEFORE_VALUE",
        "OBJECT_AFTER_VALUE",
    }
)
_ARRAY_STATES: Final = frozenset({"ARRAY_START", "ARRAY_AFTER_COMMA", "ARRAY_AFTER_VALUE"})
_LITERALS: Final = ("true", "false", "null")
_VALUE_START: Final = {
    '"': "STRING",
    **dict.fromkeys("ftn", "LITERAL"),
    **dict.fromkeys("-0123456789", "NUMBER"),
    "{": "OBJECT_START",
    "[": "ARRAY_START",
}


def fix_json(text: str) -> str:  # noqa: C901 (a port of ai/src/util/fix-json.ts, state for state)
    """Close a JSON prefix so that it parses: the scanner of `fixJson` in the AI SDK."""
    stack = ["ROOT"]
    last_valid = -1
    literal_start = 0
    unicode_digits = 0

    def value_start(char: str, i: int, swap: str) -> None:
        nonlocal last_valid, literal_start
        state = _VALUE_START.get(char)
        if state is None:
            return
        if char != "-":
            last_valid = i
        if state == "LITERAL":
            literal_start = i
        stack.pop()
        stack.append(swap)
        stack.append(state)

    def after_object_value(char: str, i: int) -> None:
        nonlocal last_valid
        if char == ",":
            stack.pop()
            stack.append("OBJECT_AFTER_COMMA")
        elif char == "}":
            last_valid = i
            stack.pop()

    def after_array_value(char: str, i: int) -> None:
        nonlocal last_valid
        if char == ",":
            stack.pop()
            stack.append("ARRAY_AFTER_COMMA")
        elif char == "]":
            last_valid = i
            stack.pop()

    for i, char in enumerate(text):
        state = stack[-1]
        if state == "ROOT":
            value_start(char, i, "FINISH")
        elif state == "OBJECT_START":
            if char == '"':
                stack.pop()
                stack.append("OBJECT_KEY")
            elif char == "}":
                last_valid = i
                stack.pop()
        elif state == "OBJECT_AFTER_COMMA":
            if char == '"':
                stack.pop()
                stack.append("OBJECT_KEY")
        elif state == "OBJECT_KEY":
            if char == '"':
                stack.pop()
                stack.append("OBJECT_AFTER_KEY")
        elif state == "OBJECT_AFTER_KEY":
            if char == ":":
                stack.pop()
                stack.append("OBJECT_BEFORE_VALUE")
        elif state == "OBJECT_BEFORE_VALUE":
            value_start(char, i, "OBJECT_AFTER_VALUE")
        elif state == "OBJECT_AFTER_VALUE":
            after_object_value(char, i)
        elif state == "STRING":
            if char == '"':
                stack.pop()
                last_valid = i
            elif char == "\\":
                stack.append("STRING_ESCAPE")
            else:
                last_valid = i
        elif state == "ARRAY_START":
            if char == "]":
                last_valid = i
                stack.pop()
            else:
                last_valid = i
                value_start(char, i, "ARRAY_AFTER_VALUE")
        elif state == "ARRAY_AFTER_VALUE":
            if char == ",":
                stack.pop()
                stack.append("ARRAY_AFTER_COMMA")
            elif char == "]":
                last_valid = i
                stack.pop()
            else:
                last_valid = i
        elif state == "ARRAY_AFTER_COMMA":
            value_start(char, i, "ARRAY_AFTER_VALUE")
        elif state == "STRING_ESCAPE":
            stack.pop()
            if char == "u":
                unicode_digits = 0
                stack.append("STRING_UNICODE_ESCAPE")
            else:
                last_valid = i
        elif state == "STRING_UNICODE_ESCAPE":
            if char in "0123456789abcdefABCDEF":
                unicode_digits += 1
                if unicode_digits == 4:
                    stack.pop()
                    last_valid = i
        elif state == "NUMBER":
            if char.isascii() and char.isdigit():
                last_valid = i
            elif char in "eE-.":
                pass
            elif char in ",}]":
                stack.pop()
                if char in ",]" and stack[-1] == "ARRAY_AFTER_VALUE":
                    after_array_value(char, i)
                if char in ",}" and stack[-1] == "OBJECT_AFTER_VALUE":
                    after_object_value(char, i)
            else:
                stack.pop()
        elif state == "LITERAL":
            partial = text[literal_start : i + 1]
            if not any(word.startswith(partial) for word in _LITERALS):
                stack.pop()
                if stack[-1] == "OBJECT_AFTER_VALUE":
                    after_object_value(char, i)
                elif stack[-1] == "ARRAY_AFTER_VALUE":
                    after_array_value(char, i)
            else:
                last_valid = i

    result = text[: last_valid + 1]
    for state in reversed(stack):
        if state == "STRING":
            result += '"'
        elif state in _OBJECT_STATES:
            result += "}"
        elif state in _ARRAY_STATES:
            result += "]"
        elif state == "LITERAL":
            partial = text[literal_start:]
            for word in _LITERALS:
                if word.startswith(partial):
                    result += word[len(partial) :]
                    break
    return result


# --- the hand emitter ---------------------------------------------------------------------------


class UIMessageStream:
    """Chunks for one assistant message, built by hand: canned replies, errors and tests.

    Every method returns the chunk (or chunks) and folds it into `self.accumulator`, which
    raises `UIMessageStreamError` for anything the client would reject as out of order: a bug
    fails on the server instead of as an opaque error in the browser. Frame chunks with `frame()`;
    `sse()` renders everything emitted so far.
    """

    def __init__(self, message_id: str, *, last_message: Mapping[str, Any] | None = None) -> None:
        self.message_id = message_id
        self.accumulator = UIMessageAccumulator(message_id, last_message)
        self.chunks: list[Chunk] = []
        self._ids = 0

    @property
    def message(self) -> dict[str, Any]:
        return self.accumulator.message

    def emit(self, chunk: Chunk) -> Chunk:
        self.accumulator.apply(chunk)
        self.chunks.append(chunk)
        return chunk

    def sse(self, *, done: bool = True) -> str:
        return "".join(frame(c) for c in self.chunks) + (DONE if done else "")

    def _next_id(self, prefix: str) -> str:
        self._ids += 1
        return f"{prefix}{self._ids}"

    # envelope
    def start(self, metadata: Any = None) -> Chunk:
        return self.emit(
            {"type": "start", "messageId": self.message_id, "messageMetadata": metadata}
        )

    def finish(self, reason: str | None = None, metadata: Any = None) -> Chunk:
        if reason is not None and reason not in FINISH_REASONS:
            raise ValueError(f"Not a finish reason: {reason!r}")
        return self.emit({"type": "finish", "finishReason": reason, "messageMetadata": metadata})

    def metadata(self, metadata: Any) -> Chunk:
        return self.emit({"type": "message-metadata", "messageMetadata": metadata})

    def error(self, text: str) -> Chunk:
        return self.emit({"type": "error", "errorText": text})

    def close_with_error(self, text: str) -> list[Chunk]:
        chunks = self.accumulator.close_with_error(text)
        self.chunks.extend(chunks)
        return chunks

    def abort(self, reason: str | None = None) -> Chunk:
        return self.emit({"type": "abort", "reason": reason})

    def start_step(self) -> Chunk:
        return self.emit({"type": "start-step"})

    def finish_step(self) -> Chunk:
        return self.emit({"type": "finish-step"})

    # text and reasoning
    def text(self, text: str, *, id: str | None = None) -> list[Chunk]:
        part_id = id or self._next_id("txt_")
        chunks = [self.text_start(part_id)]
        if text:
            chunks.append(self.text_delta(part_id, text))
        chunks.append(self.text_end(part_id))
        return chunks

    def text_start(self, id: str) -> Chunk:
        return self.emit({"type": "text-start", "id": id})

    def text_delta(self, id: str, delta: str) -> Chunk:
        return self.emit({"type": "text-delta", "id": id, "delta": delta})

    def text_end(self, id: str) -> Chunk:
        return self.emit({"type": "text-end", "id": id})

    def reasoning(self, text: str, *, id: str | None = None) -> list[Chunk]:
        part_id = id or self._next_id("rsn_")
        return [
            self.emit({"type": "reasoning-start", "id": part_id}),
            self.emit({"type": "reasoning-delta", "id": part_id, "delta": text}),
            self.emit({"type": "reasoning-end", "id": part_id}),
        ]

    # tools
    def tool_input_start(
        self, call_id: str, name: str, *, dynamic: bool | None = None, title: str | None = None
    ) -> Chunk:
        return self.emit(
            {
                "type": "tool-input-start",
                "toolCallId": call_id,
                "toolName": name,
                "dynamic": dynamic,
                "title": title,
            }
        )

    def tool_input_delta(self, call_id: str, delta: str) -> Chunk:
        return self.emit(
            {"type": "tool-input-delta", "toolCallId": call_id, "inputTextDelta": delta}
        )

    def tool_input_available(
        self, call_id: str, name: str, input: Any, *, dynamic: bool | None = None
    ) -> Chunk:
        return self.emit(
            {
                "type": "tool-input-available",
                "toolCallId": call_id,
                "toolName": name,
                "input": input,
                "dynamic": dynamic,
            }
        )

    def tool_input_error(
        self, call_id: str, name: str, input: Any, error_text: str, *, dynamic: bool | None = None
    ) -> Chunk:
        return self.emit(
            {
                "type": "tool-input-error",
                "toolCallId": call_id,
                "toolName": name,
                "input": input,
                "dynamic": dynamic,
                "errorText": error_text,
            }
        )

    def tool_output_available(self, call_id: str, output: Any) -> Chunk:
        return self.emit({"type": "tool-output-available", "toolCallId": call_id, "output": output})

    def tool_output_error(self, call_id: str, error_text: str) -> Chunk:
        return self.emit(
            {"type": "tool-output-error", "toolCallId": call_id, "errorText": error_text}
        )

    def tool_call(self, call_id: str, name: str, input: Any, output: Any) -> list[Chunk]:
        """A finished call: its input, then its output."""
        return [
            self.tool_input_available(call_id, name, input),
            self.tool_output_available(call_id, output),
        ]

    def data(
        self, name: str, data: Any, *, id: str | None = None, transient: bool | None = None
    ) -> Chunk:
        return self.emit({"type": f"data-{name}", "id": id, "data": data, "transient": transient})


# --- pydantic-ai's mapper, with Foray's overrides -----------------------------------------------


class _HasReplyId(Protocol):
    @property
    def reply_id(self) -> str: ...


def _related(exc: BaseException) -> Iterator[BaseException]:
    """The exception, the leaves of an exception group, and the explicit causes."""
    seen: set[int] = set()
    pending: list[BaseException] = [exc]
    while pending:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        if current.__cause__ is not None:
            pending.append(current.__cause__)


def agent_error_text(exc: BaseException) -> str:
    """The text a chat turn shows for an agent error (§7.3). Nothing internal reaches the client:
    only `BudgetExceeded` (its `user_message`, §8.6), `UserFacingError` and the request-limit
    backstop have their own sentence; anything else is logged with its traceback and shown as
    "An error occurred."."""
    for current in _related(exc):
        budget_text = getattr(current, "user_message", None)
        if isinstance(budget_text, str) and budget_text:  # llm.budget.BudgetExceeded
            log.warning("chat turn refused: %s", budget_text)
            return budget_text
        if isinstance(current, UserFacingError):
            log.info("chat turn ended: %s", current)
            return str(current)
        if isinstance(current, UsageLimitExceeded):
            log.warning("chat turn hit the request limit: %s", current)
            return STEP_LIMIT_ERROR
    log.error("chat turn failed", exc_info=exc)
    return GENERIC_ERROR


def to_chunk(event: BaseChunk) -> Chunk:
    """A pydantic-ai chunk as a wire dict (camelCase, `None` dropped, required keys kept)."""
    return wire_chunk(event.model_dump(mode="json", by_alias=True, exclude_none=True))


class ForayVercelStream(VercelAIEventStream[Any, Any]):
    """`VercelAIEventStream(sdk_version=7)` used standalone over `agent.run_stream_events(...)`.

    Overrides (pydantic-ai 2.54.0):
    - `encode_event`: `frame()`, which restores a tool's `None` output as `"output":null`;
    - `before_stream` (the start hook): `start` with the pinned reply id, never a generated one;
    - `handle_run_result` (the run-result hook): drops the `pydantic_ai` message metadata;
    - `on_error`: the user's text for the error (`agent_error_text`). This is the only place an
      agent error is seen: `transform_stream` catches every `Exception` of the run, closes the
      open parts, then calls it.

    `ForayVercelStream(turn)` takes the reply id from `turn.reply_id`;
    `ForayVercelStream(message_id=...)` names it directly. `chunks(events)` yields wire dicts
    and leaves `[DONE]` to the caller, which sends it only after the reply is saved.
    """

    def __init__(self, turn: _HasReplyId | None = None, *, message_id: str | None = None) -> None:
        reply_id = message_id if message_id is not None else getattr(turn, "reply_id", None)
        if not reply_id:
            raise ValueError("ForayVercelStream needs the reply's message id")
        super().__init__(sdk_version=7, server_message_id=reply_id)

    def encode_event(self, event: BaseChunk) -> str:
        if isinstance(event, DoneChunk):
            return DONE
        return frame(to_chunk(event))

    async def before_stream(self) -> AsyncIterator[BaseChunk]:
        yield StartChunk(message_id=self.server_message_id)

    async def handle_run_result(self, event: AgentRunResultEvent[Any]) -> AsyncIterator[BaseChunk]:
        async for chunk in super().handle_run_result(event):
            if not isinstance(chunk, MessageMetadataChunk):
                yield chunk

    async def on_error(self, error: Exception) -> AsyncIterator[BaseChunk]:
        text = agent_error_text(error)
        async for chunk in super().on_error(error):
            yield ErrorChunk(error_text=text) if isinstance(chunk, ErrorChunk) else chunk

    async def chunks(self, events: AsyncIterator[Any]) -> AsyncIterator[Chunk]:
        """The turn's chunks as wire dicts, from `start` to `finish`, without `[DONE]`."""
        async for event in self.transform_stream(events):
            if not isinstance(event, DoneChunk):
                yield to_chunk(event)
