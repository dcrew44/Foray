"""`apps.chat.uistream` (design §7.3, §12.3): the wire and the accumulator.

The accumulator is checked byte for byte against the messages the TS server persisted (the
research reference turns and every step 0 golden), `frame()` against every event the TS route
sent, and `ForayVercelStream` against a `FunctionModel` agent. The Node contract check of the
same streams is `tests/contract/test_contract.py`.
"""

from __future__ import annotations

import copy
import inspect
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pydantic_ai
import pytest
from pydantic_ai.ui import UIEventStream
from pydantic_ai.ui.vercel_ai import VercelAIEventStream
from pydantic_ai.ui.vercel_ai.response_types import DoneChunk, ToolOutputAvailableChunk

from apps.chat.uistream import (
    DONE,
    GENERIC_ERROR,
    PING,
    STEP_LIMIT_ERROR,
    UI_HEADERS,
    UNDEFINED,
    ForayVercelStream,
    UIMessageAccumulator,
    UIMessageStream,
    UIMessageStreamError,
    agent_error_text,
    fix_json,
    frame,
    parse_partial_json,
    ui_headers,
)
from apps.core.errors import UserFacingError
from tests.contract.harness import GOLDENS, parse_sse
from tests.contract.scenarios import (
    AGENT_SCENARIOS,
    BUDGET_TEXT,
    USER_FACING_TEXT,
    budget_exceeded,
)

# The research's two TypeScript reference turns (an approval and its answer), kept with the goldens
# since step H2: step 13 deletes docs/migration/, where they were first recorded.
PROTOTYPES = GOLDENS / "prototypes"
SCENARIOS = json.loads((GOLDENS / "scenarios.json").read_text())["scenarios"]


def js_stringify(value: Any) -> str:
    """`JSON.stringify(value, null, 2)`."""
    return json.dumps(value, ensure_ascii=False, indent=2)


def fold(
    sse: str, message_id: str = "", last: dict[str, Any] | None = None
) -> UIMessageAccumulator:
    acc = UIMessageAccumulator(message_id, last)
    for chunk in parse_sse(sse):
        acc.apply(chunk)
    return acc


def approve(state: dict[str, Any]) -> dict[str, Any]:
    """What the TS capture did to turn 1's in-memory message (undefined slots and all) before
    sending it back as turn 2's last message."""
    message = copy.deepcopy(state)
    for part in message["parts"]:
        if part.get("state") == "approval-requested":
            part["state"] = "approval-responded"
            part["approval"] = {**part["approval"], "approved": True}
    return message


def assert_bytes(acc: UIMessageAccumulator, path: Path) -> None:
    expected = path.read_text(encoding="utf-8")
    newline = "\n" if expected.endswith("\n") else ""
    assert js_stringify(acc.message) + newline == expected


# --- the accumulator against the TS server's persisted messages -------------------------------


def test_accumulator_reproduces_ts_reference_turns() -> None:
    """`ts-turn1/2.persisted.json` (the research's TS reference), byte for byte."""
    turn1 = fold((PROTOTYPES / "ts-turn1.sse").read_text())
    assert_bytes(turn1, PROTOTYPES / "ts-turn1.persisted.json")
    turn2 = fold((PROTOTYPES / "ts-turn2.sse").read_text(), last=approve(turn1.state))
    assert_bytes(turn2, PROTOTYPES / "ts-turn2.persisted.json")


@pytest.mark.parametrize("name", [s["name"] for s in SCENARIOS])
def test_accumulator_reproduces_every_golden(name: str) -> None:
    """Every step 0 UI-stream golden, byte for byte (the approval turns as one round trip)."""
    if name == "approval_turn2":
        turn1 = fold((GOLDENS / "approval_turn1.sse").read_text())
        acc = fold((GOLDENS / "approval_turn2.sse").read_text(), last=approve(turn1.state))
    else:
        acc = fold((GOLDENS / f"{name}.sse").read_text())
    assert_bytes(acc, GOLDENS / f"{name}.persisted.json")


def test_continuation_from_stored_json_matches_ts_order() -> None:
    """From a message read back from JSON (no undefined slots), a new key is appended at the
    end, as `processUIMessageStream` does: same content, TS's key order for that input."""
    stored = next(s for s in SCENARIOS if s["name"] == "approval_turn2")["uiMessages"][-1]
    acc = fold((GOLDENS / "approval_turn2.sse").read_text(), last=stored)
    expected = json.loads((GOLDENS / "approval_turn2.persisted.json").read_text())
    assert acc.message == expected
    gated = acc.message["parts"][4]
    assert list(gated) == ["type", "toolCallId", "state", "input", "approval", "output"]


def test_error_and_abort_do_not_change_the_message() -> None:
    acc = fold((GOLDENS / "stream_error.sse").read_text())
    assert acc.error == "An error occurred."
    assert acc.finish_reason == "error"
    s = UIMessageStream("m")
    s.start()
    s.start_step()
    s.text_start("t")
    s.text_delta("t", "half")
    before = s.message
    s.abort()
    assert s.accumulator.aborted
    assert s.message == before
    assert before["parts"][1] == {"type": "text", "text": "half", "state": "streaming"}


def test_state_keeps_undefined_slots_and_message_drops_them() -> None:
    acc = UIMessageAccumulator("m")
    acc.apply({"type": "start-step"})
    acc.apply({"type": "tool-input-available", "toolCallId": "c", "toolName": "x", "input": {}})
    part = acc.state["parts"][1]
    assert part["output"] is UNDEFINED
    assert list(part) == [
        "type",
        "toolCallId",
        "state",
        "title",
        "input",
        "output",
        "rawInput",
        "errorText",
        "providerExecuted",
        "preliminary",
    ]
    assert acc.message["parts"][1] == {
        "type": "tool-x",
        "toolCallId": "c",
        "state": "input-available",
        "input": {},
    }
    assert copy.deepcopy(UNDEFINED) is UNDEFINED
    assert acc.message is not acc.message  # a fresh copy each time


def test_metadata_merges_deeply() -> None:
    s = UIMessageStream("m")
    s.start({"kind": "follow_up", "runId": "r", "nested": {"a": 1, "list": [1]}})
    s.metadata({"nested": {"b": 2, "list": [2]}, "skip": None})
    s.finish("stop", {"nested": {"a": 3}})
    assert s.message["metadata"] == {
        "kind": "follow_up",
        "runId": "r",
        "nested": {"a": 3, "list": [2], "b": 2},
        "skip": None,
    }
    assert list(s.message) == ["id", "metadata", "role", "parts"]


def test_data_parts_replace_by_id_and_skip_transient() -> None:
    s = UIMessageStream("m")
    s.start()
    s.data("progress", {"n": 1}, id="p")
    s.data("progress", {"n": 2}, id="p")
    s.data("progress", {"n": 3})
    s.data("note", "hidden", transient=True)
    assert s.message["parts"] == [
        {"type": "data-progress", "id": "p", "data": {"n": 2}},
        {"type": "data-progress", "data": {"n": 3}},
    ]


def test_reset_step_drops_the_current_step() -> None:
    acc = UIMessageAccumulator("m")
    for chunk in parse_sse(
        'data: {"type":"start-step"}\n\ndata: {"type":"text-start","id":"a"}\n\n'
        'data: {"type":"text-end","id":"a"}\n\ndata: {"type":"start-step"}\n\n'
        'data: {"type":"text-start","id":"b"}\n\ndata: {"type":"reset-step"}\n\n'
    ):
        acc.apply(chunk)
    assert [p["type"] for p in acc.message["parts"]] == ["step-start", "text", "step-start"]


def test_out_of_order_chunks_raise_like_the_client() -> None:
    s = UIMessageStream("m")
    s.start()
    with pytest.raises(UIMessageStreamError, match="text-delta"):
        s.text_delta("t", "x")
    with pytest.raises(UIMessageStreamError, match="tool-input-delta"):
        s.tool_input_delta("c", "{")
    with pytest.raises(UIMessageStreamError, match="No tool invocation"):
        s.tool_output_available("c", None)
    with pytest.raises(ValueError, match="finish reason"):
        s.finish("done")


def test_close_with_error_closes_open_parts_once() -> None:
    s = UIMessageStream("m")
    s.start()
    s.start_step()
    s.emit({"type": "reasoning-start", "id": "r"})
    s.text_start("t")
    s.text_delta("t", "Half an answer")
    chunks = s.close_with_error("That reply took too long and was stopped.")
    assert [c["type"] for c in chunks] == [
        "text-end",
        "reasoning-end",
        "error",
        "finish-step",
        "finish",
    ]
    assert chunks[-1] == {"type": "finish", "finishReason": "error"}
    assert [p.get("state") for p in s.message["parts"][1:]] == ["done", "done"]
    assert s.accumulator.error == "That reply took too long and was stopped."
    # after a finished turn only the error is added
    again = s.close_with_error(GENERIC_ERROR)
    assert [c["type"] for c in again] == ["error"]


# --- partial tool input ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "fixed"),
    [
        ('{"query":"rust eng', '{"query":"rust eng"}'),
        ('{"a":[1,2,{"b":tr', '{"a":[1,2,{"b":true}]}'),
        ('{"n":-', "{}"),
        ('{"limit":12.5e', '{"limit":12.5}'),
        ('{"k":[{"a":1},', '{"k":[{"a":1}]}'),
        ('{"s":"caf\\u00e', '{"s":"caf"}'),
        ("nul", "null"),
        ("", ""),
        ('{"id":"x",', '{"id":"x"}'),
    ],
)
def test_fix_json_matches_the_sdk(text: str, fixed: str) -> None:
    """`fixJson` outputs (the contract test checks the parsed values against the client)."""
    assert fix_json(text) == fixed


def test_parse_partial_json() -> None:
    assert parse_partial_json('{"a":1}') == {"a": 1}
    assert parse_partial_json('{"a":[1') == {"a": [1]}
    assert parse_partial_json("") is UNDEFINED
    assert parse_partial_json('{"a":NaN}') == {}  # JSON.parse refuses NaN; fixJson drops it
    assert parse_partial_json('{"__proto__":{}}') is UNDEFINED  # secureJsonParse


# --- frame() ------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", [s["name"] for s in SCENARIOS])
def test_frame_reproduces_every_golden_event(name: str) -> None:
    """Re-framing each chunk the TS route sent gives its exact bytes (unicode, quotes,
    `</script>`, newlines, `"output":null`)."""
    sse = (GOLDENS / f"{name}.sse").read_text(encoding="utf-8")
    assert "".join(frame(c) for c in parse_sse(sse)) + DONE == sse


def test_frame_null_rules() -> None:
    assert frame({"type": "start", "messageId": "m", "messageMetadata": None}) == (
        'data: {"type":"start","messageId":"m"}\n\n'
    )
    assert frame({"type": "tool-output-available", "toolCallId": "c"}) == (
        'data: {"type":"tool-output-available","toolCallId":"c","output":null}\n\n'
    )
    assert frame({"type": "tool-input-available", "toolCallId": "c", "toolName": "x"}).endswith(
        '"input":null}\n\n'
    )
    assert frame({"type": "message-metadata", "messageMetadata": None}) == (
        'data: {"type":"message-metadata","messageMetadata":null}\n\n'
    )
    assert frame({"type": "data-x", "data": None, "id": None}) == (
        'data: {"type":"data-x","data":null}\n\n'
    )
    nested = {"type": "tool-output-available", "toolCallId": "c", "output": {"a": None}}
    assert frame(nested).endswith('"output":{"a":null}}\n\n')
    with pytest.raises(ValueError):
        frame({"type": "data-x", "data": float("nan")})


def test_headers() -> None:
    assert UI_HEADERS["Cache-Control"] == "no-cache, no-transform"
    assert ui_headers("c-1") == {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache, no-transform",
        "X-Vercel-AI-UI-Message-Stream": "v1",
        "X-Accel-Buffering": "no",
        "X-Conversation-Id": "c-1",
    }
    assert PING == ": ping\n\n"


# --- ForayVercelStream ----------------------------------------------------------------------


def test_override_points_exist_in_pydantic_ai_2_54_0() -> None:
    """§15.4: the hooks ForayVercelStream overrides exist under these names in 2.54.0."""
    assert pydantic_ai.__version__ == "2.54.0"
    hooks = ("encode_event", "before_stream", "handle_run_result", "on_error")
    for hook in hooks:
        base = getattr(VercelAIEventStream, hook)
        ours = getattr(ForayVercelStream, hook)
        assert ours is not base, hook
        assert list(inspect.signature(ours).parameters) == list(
            inspect.signature(base).parameters
        ), hook
    for hook in ("before_stream", "on_error", "transform_stream"):
        assert hook in vars(UIEventStream), hook
    stream = ForayVercelStream(message_id="m")
    assert stream.sdk_version == 7
    assert stream.server_message_id == "m"


@dataclass(frozen=True)
class Turn:
    reply_id: str


def test_stream_takes_the_reply_id_from_the_turn() -> None:
    assert ForayVercelStream(Turn("r-1")).server_message_id == "r-1"
    with pytest.raises(ValueError, match="message id"):
        ForayVercelStream()


def test_encode_event_restores_none_output_and_done() -> None:
    stream = ForayVercelStream(message_id="m")
    chunk = ToolOutputAvailableChunk(tool_call_id="c", output=None)
    assert stream.encode_event(chunk) == (
        'data: {"type":"tool-output-available","toolCallId":"c","output":null}\n\n'
    )
    assert stream.encode_event(DoneChunk()) == DONE


@pytest.mark.parametrize("name", list(AGENT_SCENARIOS))
async def test_function_model_turns(name: str) -> None:
    played = await AGENT_SCENARIOS[name]()
    types = [c["type"] for c in played.chunks]
    assert played.chunks[0] == {"type": "start", "messageId": f"asst_{name}"}
    assert "message-metadata" not in types
    assert types[-1] == "finish"
    assert played.sse.endswith(DONE)
    assert played.message["id"] == f"asst_{name}"
    errors = [c["errorText"] for c in played.chunks if c["type"] == "error"]
    assert errors == ([played.error_text] if played.error_text else [])
    if played.error_text:
        assert played.chunks[-1] == {"type": "finish", "finishReason": "error"}
    if name == "agent_tool_none":
        assert 'data: {"type":"tool-output-available","toolCallId":"call_1","output":null}' in (
            played.sse
        )
        assert played.message["parts"][1]["output"] is None
    if name == "agent_tool_output":
        assert played.message["parts"][1]["output"] == {
            "ok": True,
            "jobId": "greenhouse:acme:1",
            "title": "Backend Engineer",
            "salary": None,
            "pay": 1.5,
        }
    if name == "agent_invalid_input":
        assert "tool-input-error" in types
    if name == "agent_pings":
        assert played.sse.count(PING) == len(played.chunks)
    if name == "agent_generic_error":
        assert "hunter2" not in played.sse
        assert "db-internal" not in json.dumps(played.message)


def test_agent_error_text(caplog: pytest.LogCaptureFixture) -> None:
    from pydantic_ai import UsageLimitExceeded

    with caplog.at_level(logging.INFO, logger="foray.chat"):
        assert agent_error_text(budget_exceeded()) == BUDGET_TEXT
        assert agent_error_text(UserFacingError(USER_FACING_TEXT)) == USER_FACING_TEXT
        assert agent_error_text(UsageLimitExceeded("request_limit of 8")) == STEP_LIMIT_ERROR
        group = ExceptionGroup("tools", [RuntimeError("x"), UserFacingError("Kept.")])
        assert agent_error_text(group) == "Kept."
        try:
            try:
                raise budget_exceeded()
            except Exception as inner:
                raise RuntimeError("wrapped") from inner
        except RuntimeError as outer:
            assert agent_error_text(outer) == BUDGET_TEXT
        caplog.clear()
        assert agent_error_text(KeyError("secret column")) == GENERIC_ERROR
    [record] = caplog.records
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None and isinstance(record.exc_info[1], KeyError)
