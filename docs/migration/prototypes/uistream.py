"""A minimal AI SDK v7 UI-message-stream emitter (prototype of the recommended API).

Framework-agnostic: produces `str` SSE frames. Django wraps the generator in a
StreamingHttpResponse; tests write the frames straight to a socket.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

UI_MESSAGE_STREAM_HEADERS = {
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-cache",
    "X-Vercel-AI-UI-Message-Stream": "v1",
    "X-Accel-Buffering": "no",
}

FINISH_REASONS = {"stop", "length", "content-filter", "tool-calls", "error", "other"}


def _dumps(obj: Any) -> str:
    # allow_nan=False: JSON.parse in the browser rejects NaN/Infinity, which Python emits by default.
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


# Keys whose presence the client schema requires even when the value is null/undefined
# (z.unknown() under zod 4 rejects a MISSING key; the TS SDK serializes undefined output as null).
_REQUIRED_NULLABLE = {"tool-output-available": "output", "tool-input-available": "input",
                      "tool-input-error": "input", "message-metadata": "messageMetadata"}


def frame(chunk: dict[str, Any]) -> str:
    """One SSE event. Drops None-valued optional keys (the client's zod schema rejects null for
    `.optional()` fields) but keeps required `unknown` keys as null."""
    keep = _REQUIRED_NULLABLE.get(chunk.get("type"))
    body = {k: v for k, v in chunk.items() if v is not None or k == keep}
    if keep and keep not in body:
        body[keep] = None
    return f"data: {_dumps(body)}\n\n"


DONE = "data: [DONE]\n\n"


def new_id(prefix: str = "") -> str:
    return prefix + uuid.uuid4().hex[:16]


class UIMessageStream:
    """Builds the chunk sequence for ONE assistant message.

    Enforces the client's ordering rules locally so a bug fails on the server,
    not as an opaque AI_UIMessageStreamError in the browser:
      * text-/reasoning-delta and -end need a prior -start with the same id
      * tool-input-delta needs a prior tool-input-start
      * tool-output-*/tool-approval-request/tool-output-denied need a known toolCallId
    """

    def __init__(self, message_id: str | None = None):
        self.message_id = message_id
        self._open_text: set[str] = set()
        self._open_reasoning: set[str] = set()
        self._streaming_tools: set[str] = set()
        self._known_tools: set[str] = set()
        self._in_step = False

    # -- message envelope -------------------------------------------------------
    def start(self, metadata: Any = None) -> str:
        return frame({"type": "start", "messageId": self.message_id, "messageMetadata": metadata})

    def finish(self, finish_reason: str | None = None, metadata: Any = None) -> str:
        assert finish_reason is None or finish_reason in FINISH_REASONS, finish_reason
        return frame({"type": "finish", "finishReason": finish_reason, "messageMetadata": metadata})

    def metadata(self, metadata: Any) -> str:
        return frame({"type": "message-metadata", "messageMetadata": metadata})

    def error(self, text: str) -> str:
        return frame({"type": "error", "errorText": text})

    def abort(self, reason: str | None = None) -> str:
        return frame({"type": "abort", "reason": reason})

    def done(self) -> str:
        return DONE

    # -- steps --------------------------------------------------------------------
    def start_step(self) -> str:
        self._in_step = True
        return frame({"type": "start-step"})

    def finish_step(self) -> str:
        self._in_step = False
        return frame({"type": "finish-step"})

    # -- text / reasoning -----------------------------------------------------------
    def text(self, text: str, id: str | None = None) -> Iterator[str]:
        """Convenience: a whole text part as start/delta/end."""
        id = id or new_id("txt_")
        yield self.text_start(id)
        if text:
            yield self.text_delta(id, text)
        yield self.text_end(id)

    def text_start(self, id: str) -> str:
        self._open_text.add(id)
        return frame({"type": "text-start", "id": id})

    def text_delta(self, id: str, delta: str) -> str:
        assert id in self._open_text, f"text-delta before text-start: {id}"
        return frame({"type": "text-delta", "id": id, "delta": delta})

    def text_end(self, id: str) -> str:
        self._open_text.remove(id)
        return frame({"type": "text-end", "id": id})

    def reasoning_start(self, id: str) -> str:
        self._open_reasoning.add(id)
        return frame({"type": "reasoning-start", "id": id})

    def reasoning_delta(self, id: str, delta: str) -> str:
        assert id in self._open_reasoning, f"reasoning-delta before reasoning-start: {id}"
        return frame({"type": "reasoning-delta", "id": id, "delta": delta})

    def reasoning_end(self, id: str) -> str:
        self._open_reasoning.remove(id)
        return frame({"type": "reasoning-end", "id": id})

    # -- tools --------------------------------------------------------------------------
    def tool_input_start(self, tool_call_id: str, tool_name: str, *, dynamic: bool | None = None,
                         title: str | None = None) -> str:
        self._streaming_tools.add(tool_call_id)
        self._known_tools.add(tool_call_id)
        return frame({"type": "tool-input-start", "toolCallId": tool_call_id, "toolName": tool_name,
                      "dynamic": dynamic, "title": title})

    def tool_input_delta(self, tool_call_id: str, delta: str) -> str:
        assert tool_call_id in self._streaming_tools, f"tool-input-delta before start: {tool_call_id}"
        return frame({"type": "tool-input-delta", "toolCallId": tool_call_id, "inputTextDelta": delta})

    def tool_input_available(self, tool_call_id: str, tool_name: str, input: Any, *,
                             dynamic: bool | None = None, title: str | None = None) -> str:
        self._streaming_tools.discard(tool_call_id)
        self._known_tools.add(tool_call_id)
        return frame({"type": "tool-input-available", "toolCallId": tool_call_id, "toolName": tool_name,
                      "input": input, "dynamic": dynamic, "title": title})

    def tool_input_error(self, tool_call_id: str, tool_name: str, input: Any, error_text: str) -> str:
        self._known_tools.add(tool_call_id)
        return frame({"type": "tool-input-error", "toolCallId": tool_call_id, "toolName": tool_name,
                      "input": input, "errorText": error_text})

    def tool_output_available(self, tool_call_id: str, output: Any, *, preliminary: bool | None = None) -> str:
        self._require_tool(tool_call_id)
        # TS SDK sends null (never omits) for an undefined output; keep the key.
        return f"data: {_dumps({'type': 'tool-output-available', 'toolCallId': tool_call_id, 'output': output, **({'preliminary': preliminary} if preliminary is not None else {})})}\n\n"

    def tool_output_error(self, tool_call_id: str, error_text: str) -> str:
        self._require_tool(tool_call_id)
        return frame({"type": "tool-output-error", "toolCallId": tool_call_id, "errorText": error_text})

    def tool_approval_request(self, tool_call_id: str, approval_id: str | None = None, *,
                              reason: str | None = None) -> str:
        self._require_tool(tool_call_id)
        return frame({"type": "tool-approval-request", "approvalId": approval_id or new_id("appr_"),
                      "toolCallId": tool_call_id, "reason": reason})

    def tool_output_denied(self, tool_call_id: str) -> str:
        self._require_tool(tool_call_id)
        return frame({"type": "tool-output-denied", "toolCallId": tool_call_id})

    # -- data / sources / files -------------------------------------------------------------
    def data(self, name: str, data: Any, *, id: str | None = None, transient: bool | None = None) -> str:
        return frame({"type": f"data-{name}", "id": id, "data": data, "transient": transient})

    def source_url(self, source_id: str, url: str, title: str | None = None) -> str:
        return frame({"type": "source-url", "sourceId": source_id, "url": url, "title": title})

    def source_document(self, source_id: str, media_type: str, title: str, filename: str | None = None) -> str:
        return frame({"type": "source-document", "sourceId": source_id, "mediaType": media_type,
                      "title": title, "filename": filename})

    def file(self, url: str, media_type: str) -> str:
        return frame({"type": "file", "url": url, "mediaType": media_type})

    def _require_tool(self, tool_call_id: str) -> None:
        # A continuation turn may reference a call from the persisted message; callers
        # register those with `known(...)` first.
        assert tool_call_id in self._known_tools, f"unknown toolCallId {tool_call_id}"

    def known(self, *tool_call_ids: str) -> "UIMessageStream":
        self._known_tools.update(tool_call_ids)
        return self


# --------------------------------------------------------------------------------------------------
# Server-side accumulator: the Python twin of ai/src/ui/process-ui-message-stream.ts.
# Folds the chunks we emit into the UIMessage the client ends up with, so `persist on end` saves
# exactly what the user saw (the TS route gets this from createAgentUIStreamResponse's onEnd).
# --------------------------------------------------------------------------------------------------
import copy as _copy


def merge_objects(base: Any, overrides: Any) -> Any:
    if base is None:
        return overrides
    if overrides is None:
        return base
    out = dict(base)
    for k, v in overrides.items():
        if k in ("__proto__", "constructor", "prototype"):
            continue
        b = out.get(k)
        out[k] = merge_objects(b, v) if isinstance(v, dict) and isinstance(b, dict) else v
    return out


class UIMessageAccumulator:
    def __init__(self, message_id: str, last_message: dict | None = None):
        # Continuation (approval round-trip): keep folding into the persisted assistant message.
        if last_message and last_message.get("role") == "assistant":
            self.message = _copy.deepcopy(last_message)
        else:
            self.message = {"id": message_id, "role": "assistant", "parts": []}
        self.text: dict[str, dict] = {}
        self.reasoning: dict[str, dict] = {}
        self.partial_tools: dict[str, dict] = {}
        self.finish_reason: str | None = None
        self.error: str | None = None
        self.aborted = False

    @property
    def parts(self) -> list[dict]:
        return self.message["parts"]

    def _step_parts(self) -> list[dict]:
        i = len(self.parts) - 1
        while i >= 0 and self.parts[i]["type"] != "step-start":
            i -= 1
        return self.parts[i + 1:]

    def _tool(self, tool_call_id: str) -> dict:
        for p in reversed(self.parts):
            if "toolCallId" in p and p["toolCallId"] == tool_call_id:
                return p
        raise ValueError(f"No tool invocation found for tool call ID {tool_call_id!r}")

    def _upsert_tool(self, c: dict, state: str, **fields: Any) -> dict:
        dynamic = c.get("dynamic")
        part = next((p for p in self._step_parts() if p.get("toolCallId") == c["toolCallId"]), None)
        if part is None:
            part = {"type": "dynamic-tool", "toolName": c["toolName"]} if dynamic else {"type": f"tool-{c['toolName']}"}
            part["toolCallId"] = c["toolCallId"]
            self.parts.append(part)
        part["state"] = state
        for k in ("title", "toolMetadata", "providerExecuted"):
            if c.get(k) is not None:
                part[k] = c[k]
        for k, v in fields.items():
            if v is None:
                part.pop(k, None)
            else:
                part[k] = v
        if c.get("providerMetadata") is not None:
            part["callProviderMetadata"] = c["providerMetadata"]
        return part

    def apply(self, c: dict) -> None:  # noqa: C901
        t = c["type"]
        if t == "start":
            if c.get("messageId") is not None:
                self.message["id"] = c["messageId"]
            self._meta(c.get("messageMetadata"))
        elif t in ("finish", "message-metadata"):
            if t == "finish" and c.get("finishReason"):
                self.finish_reason = c["finishReason"]
            self._meta(c.get("messageMetadata"))
        elif t == "start-step":
            self.parts.append({"type": "step-start"})
        elif t in ("finish-step",):
            pass
        elif t == "reset-step":
            n = len(self._step_parts())
            if n:
                del self.parts[-n:]
            self.text, self.reasoning, self.partial_tools = {}, {}, {}
        elif t == "text-start":
            p = {"type": "text", "text": "", "state": "streaming"}
            if c.get("providerMetadata") is not None:
                p["providerMetadata"] = c["providerMetadata"]
            self.text[c["id"]] = p
            self.parts.append(p)
        elif t == "text-delta":
            self.text[c["id"]]["text"] += c["delta"]
        elif t == "text-end":
            self.text.pop(c["id"])["state"] = "done"
        elif t == "reasoning-start":
            p = {"type": "reasoning", "id": c["id"], "text": "", "state": "streaming"}
            if c.get("providerMetadata") is not None:
                p["providerMetadata"] = c["providerMetadata"]
            self.reasoning[c["id"]] = p
            self.parts.append(p)
        elif t == "reasoning-delta":
            self.reasoning[c["id"]]["text"] += c["delta"]
        elif t == "reasoning-end":
            self.reasoning.pop(c["id"])["state"] = "done"
        elif t == "tool-input-start":
            self.partial_tools[c["toolCallId"]] = {"text": "", **c}
            self._upsert_tool(c, "input-streaming", input=None)
        elif t == "tool-input-delta":
            pt = self.partial_tools[c["toolCallId"]]
            pt["text"] += c["inputTextDelta"]
            # (partial JSON parsing elided: the persisted message never ends in input-streaming)
        elif t == "tool-input-available":
            self._upsert_tool(c, "input-available", input=c["input"])
        elif t == "tool-input-error":
            self._upsert_tool(c, "output-error", input=None, rawInput=c.get("input"), errorText=c["errorText"])
        elif t == "tool-approval-request":
            p = self._tool(c["toolCallId"])
            p["state"] = "approval-requested"
            p["approval"] = {"id": c["approvalId"]}
            if c.get("reason") is not None:
                p["approval"]["requestReason"] = c["reason"]
        elif t == "tool-output-available":
            p = self._tool(c["toolCallId"])
            p["state"] = "output-available"
            p["output"] = c["output"]
            if c.get("preliminary") is not None:
                p["preliminary"] = c["preliminary"]
        elif t == "tool-output-error":
            p = self._tool(c["toolCallId"])
            p["state"] = "output-error"
            p["errorText"] = c["errorText"]
        elif t == "tool-output-denied":
            self._tool(c["toolCallId"])["state"] = "output-denied"
        elif t in ("source-url", "source-document", "file", "reasoning-file", "custom"):
            self.parts.append({k: v for k, v in c.items()})
        elif t.startswith("data-"):
            if c.get("transient"):
                return
            existing = next((p for p in self.parts if c.get("id") is not None and p["type"] == t and p.get("id") == c["id"]), None)
            if existing is not None:
                existing["data"] = c["data"]
            else:
                self.parts.append({k: v for k, v in c.items() if k != "transient"})
        elif t == "error":
            self.error = c["errorText"]
        elif t == "abort":
            self.aborted = True

    def _meta(self, md: Any) -> None:
        if md is not None:
            self.message["metadata"] = merge_objects(self.message.get("metadata"), md)


def parse_sse(frames: str) -> list[dict]:
    out = []
    for block in frames.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(l[5:].lstrip(" ") if l.startswith("data:") else "" for l in block.split("\n") if l.startswith("data:"))
        if data and data != "[DONE]":
            out.append(json.loads(data))
    return out
