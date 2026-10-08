"""`apps.chat.convert` (design §7.4, §12.3): stored UI messages → the model history of a turn.

Splitting at `step-start`, merging consecutive requests, synthesized returns for dangling calls,
tolerance of unknown tools and parts, the window, and (§15.4) that pydantic-ai 2.54.0 accepts the
converted history through `run_stream_events(user_prompt=..., message_history=...)`.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from apps.chat import convert
from apps.chat.convert import (
    INTERRUPTED,
    TurnInput,
    message_size,
    to_model_messages,
    turn_input,
    window,
)
from tests.contract.harness import GOLDENS

SCENARIOS = json.loads((GOLDENS / "scenarios.json").read_text())["scenarios"]


def golden(name: str) -> dict[str, Any]:
    return json.loads((GOLDENS / f"{name}.persisted.json").read_text())


def user(message_id: str, text: str, metadata: Any = None) -> dict[str, Any]:
    return {
        "id": message_id,
        "role": "user",
        "parts": [{"type": "text", "text": text}],
        "metadata": metadata,
    }


def assistant(message_id: str, *parts: dict[str, Any]) -> dict[str, Any]:
    return {"id": message_id, "role": "assistant", "parts": list(parts), "metadata": None}


STEP = {"type": "step-start"}


def text(value: str, **extra: Any) -> dict[str, Any]:
    return {"type": "text", "text": value, "state": "done", **extra}


def tool(name: str, call_id: str, state: str, **fields: Any) -> dict[str, Any]:
    return {"type": f"tool-{name}", "toolCallId": call_id, "state": state, **fields}


def shape(messages: list[ModelMessage]) -> list[tuple[str, list[tuple[Any, ...]]]]:
    """Messages without timestamps or ids, for comparison."""
    out: list[tuple[str, list[tuple[Any, ...]]]] = []
    for message in messages:
        parts: list[tuple[Any, ...]] = []
        for part in message.parts:
            match part:
                case UserPromptPart():
                    parts.append(("user", part.content))
                case TextPart():
                    parts.append(("text", part.content))
                case ToolCallPart():
                    parts.append(("call", part.tool_name, part.tool_call_id, part.args))
                case ToolReturnPart():
                    parts.append(("return", part.tool_name, part.tool_call_id, part.content))
                case _:
                    parts.append((type(part).__name__,))
        out.append(("request" if isinstance(message, ModelRequest) else "response", parts))
    return out


def assert_paired(messages: list[ModelMessage]) -> None:
    """Every call has exactly one return in the next request, and nothing else is returned."""
    for i, message in enumerate(messages):
        if isinstance(message, ModelResponse):
            calls = [p.tool_call_id for p in message.parts if isinstance(p, ToolCallPart)]
            if not calls:
                continue
            following = messages[i + 1]
            assert isinstance(following, ModelRequest)
            returns = [p.tool_call_id for p in following.parts if isinstance(p, ToolReturnPart)]
            assert returns == calls
    all_calls = {p.tool_call_id for m in messages for p in m.parts if isinstance(p, ToolCallPart)}
    all_returns = [
        p.tool_call_id for m in messages for p in m.parts if isinstance(p, ToolReturnPart)
    ]
    assert sorted(all_returns) == sorted(all_calls)


# --- conversion ------------------------------------------------------------------------------


def test_user_messages_become_requests_and_consecutive_ones_merge() -> None:
    notice = user("n1", "Your search finished.", {"kind": "run_update", "runId": "r", "eventId": 3})
    history = to_model_messages(
        [
            user("u1", "Find me jobs"),
            assistant("a1", STEP, text("On it.")),
            notice,
            user("u2", "Show me the best one"),
        ]
    )
    assert shape(history) == [
        ("request", [("user", "Find me jobs")]),
        ("response", [("text", "On it.")]),
        ("request", [("user", "Your search finished."), ("user", "Show me the best one")]),
    ]


def test_assistant_messages_split_at_step_start() -> None:
    history = to_model_messages([user("u1", "x"), golden("tool_two_steps")])
    jobs = golden("tool_two_steps")["parts"][2]["output"]
    assert shape(history) == [
        ("request", [("user", "x")]),
        ("response", [("text", "Let me look."), ("call", "list_jobs", "call_1", {"limit": 2})]),
        ("request", [("return", "list_jobs", "call_1", jobs)]),
        ("response", [("text", "One match: Backend Engineer at Café Numérique.")]),
    ]
    parallel = to_model_messages([golden("tool_parallel")])
    assert [kind for kind, _ in shape(parallel)] == ["response", "request", "response"]
    assert [p[0] for p in shape(parallel)[0][1]] == ["call", "call"]
    assert_paired(parallel)


def test_failed_calls_return_the_error_text() -> None:
    raised = shape(to_model_messages([golden("tool_raises")]))
    assert raised[0][1] == [("call", "get_job", "call_1", {"id": "raise"})]
    assert raised[1][1] == [("return", "get_job", "call_1", "Error: An error occurred.")]
    invalid = shape(to_model_messages([golden("invalid_tool_input")]))
    assert invalid[0][1] == [("call", "list_jobs", "call_1", {"limit": "two"})]  # from rawInput
    assert invalid[1][1] == [("return", "list_jobs", "call_1", "Error: An error occurred.")]
    unknown = shape(to_model_messages([golden("unknown_tool")]))
    assert unknown[0][1] == [("call", "no_such_tool", "call_1", {})]  # a dynamic-tool part
    nulled = shape(to_model_messages([golden("tool_returns_undefined")]))
    assert nulled[1][1] == [("return", "get_job", "call_1", None)]


@pytest.mark.parametrize(
    ("state", "fields", "args"),
    [
        ("input-available", {"input": {"id": "m-1"}}, {"id": "m-1"}),
        ("input-streaming", {"input": {"query": "rust"}}, {"query": "rust"}),
        ("input-streaming", {}, {}),
        ("approval-requested", {"input": {"jobId": "j"}, "approval": {"id": "a"}}, {"jobId": "j"}),
        ("approval-responded", {"input": {}, "approval": {"id": "a", "approved": True}}, {}),
    ],
)
def test_calls_without_output_get_the_interrupted_return(
    state: str, fields: dict[str, Any], args: dict[str, Any]
) -> None:
    """Stop or a crash leaves a call with no result: it gets a synthesized return, so no
    provider sees a dangling call and the model knows it may have run."""
    history = to_model_messages(
        [
            user("u1", "go"),
            assistant(
                "a1", STEP, text("Starting."), tool("start_search_run", "c1", state, **fields)
            ),
        ]
    )
    assert shape(history)[1:] == [
        ("response", [("text", "Starting."), ("call", "start_search_run", "c1", args)]),
        ("request", [("return", "start_search_run", "c1", INTERRUPTED)]),
    ]
    assert INTERRUPTED == (
        "This call was interrupted before it returned; it may or may not have taken effect."
    )


def test_reasoning_metadata_and_other_parts_are_dropped() -> None:
    openai = {"openai": {"itemId": "msg_1", "reasoningEncryptedContent": "..."}}
    message = assistant(
        "a1",
        STEP,
        {
            "type": "reasoning",
            "id": "r1",
            "text": "Thinking",
            "state": "done",
            "providerMetadata": openai,
        },
        text("Hello", providerMetadata=openai),
        {"type": "source-url", "sourceId": "s", "url": "https://example.com"},
        {"type": "file", "mediaType": "image/png", "url": "data:image/png;base64,AA=="},
        {"type": "data-progress", "id": "p", "data": {"n": 1}},
        {"type": "custom", "kind": "a.b"},
        tool(
            "get_job",
            "c1",
            "output-available",
            input={"id": "x"},
            output={"ok": True},
            callProviderMetadata=openai,
            resultProviderMetadata=openai,
        ),
        text(""),
    )
    history = to_model_messages([message])
    assert shape(history) == [
        ("response", [("text", "Hello"), ("call", "get_job", "c1", {"id": "x"})]),
        ("request", [("return", "get_job", "c1", {"ok": True})]),
    ]
    response = history[0]
    assert isinstance(response, ModelResponse)
    first = response.parts[0]
    assert isinstance(first, TextPart)
    assert first.id is None and first.provider_name is None and first.provider_details is None


def test_unknown_and_retired_tools_still_convert() -> None:
    """Nothing is validated against today's tools, so old chats never brick."""
    message = assistant(
        "a1",
        STEP,
        tool("retired_tool", "c1", "output-available", input="not an object", output="plain text"),
        {
            "type": "dynamic-tool",
            "toolName": "mcp_thing",
            "toolCallId": "c2",
            "state": "output-error",
            "input": {"q": 1},
            "errorText": "Nope.",
        },
        tool("get_job", "c3", "output-weird", input={"id": "x"}),
        tool("get_job", "c4", "output-denied", input={"id": "y"}),
        {"type": "tool-get_job", "state": "output-available", "output": {}},  # no toolCallId
        {"type": "tool-", "toolCallId": "c5", "state": "output-available"},  # no tool name
        {"type": "never-heard-of-it", "foo": 1},
    )
    history = to_model_messages([message])
    assert shape(history) == [
        (
            "response",
            [
                ("call", "retired_tool", "c1", {"input": "not an object"}),
                ("call", "mcp_thing", "c2", {"q": 1}),
                ("call", "get_job", "c3", {"id": "x"}),
                ("call", "get_job", "c4", {"id": "y"}),
            ],
        ),
        (
            "request",
            [
                ("return", "retired_tool", "c1", "plain text"),
                ("return", "mcp_thing", "c2", "Error: Nope."),
                ("return", "get_job", "c3", INTERRUPTED),
                ("return", "get_job", "c4", "Error: The user declined this call."),
            ],
        ),
    ]
    assert to_model_messages([{"role": "system", "parts": [text("x")]}, {"id": "broken"}]) == []


@pytest.mark.parametrize("name", [s["name"] for s in SCENARIOS])
def test_every_golden_message_converts(name: str) -> None:
    history = to_model_messages([user("u1", "x"), golden(name)])
    assert_paired(history)
    assert isinstance(history[0], ModelRequest)


# --- the window --------------------------------------------------------------------------------


def conversation(turns: int, *, size: int = 10) -> list[dict[str, Any]]:
    """`turns` user/assistant pairs, each assistant reply with one tool call and its result."""
    messages: list[dict[str, Any]] = []
    for n in range(turns):
        messages.append(user(f"u{n}", f"question {n} " + "q" * size))
        messages.append(
            assistant(
                f"a{n}",
                STEP,
                text("Looking."),
                tool(
                    "list_jobs",
                    f"call_{n}",
                    "output-available",
                    input={"limit": n},
                    output={"count": n, "pad": "x" * size},
                ),
                STEP,
                text(f"answer {n}"),
            )
        )
    return messages


def test_window_keeps_the_first_message_and_the_latest_forty() -> None:
    messages = [*conversation(50), user("last", "and now?")]  # 101 messages
    kept = window(messages)
    assert kept[0] is messages[0]
    assert kept[-1] is messages[-1]
    # the latest 40 are messages[61:]; [61] is a reply whose request was cut, so it goes too
    assert kept[1:] == messages[62:]
    assert len(kept) == 40
    users = [user(f"u{n}", f"question {n}") for n in range(60)]
    assert window(users) == [users[0], *users[20:]]  # the first, plus the latest 40


def test_window_without_a_gap_keeps_the_reply_after_the_first_message() -> None:
    messages = [*conversation(20), user("last", "x")]  # 41 messages: first + latest 40
    assert window(messages) == messages


def test_window_respects_the_character_budget() -> None:
    huge = assistant("big", STEP, text("y" * 70_000))
    messages = [user("u0", "first"), user("u1", "second"), huge, user("u2", "third")]
    assert window(messages) == [messages[0], messages[3]]
    assert message_size(huge) == 70_000
    sized = conversation(30, size=5_000)
    kept = window([*sized, user("end", "go")])
    total = sum(message_size(m) for m in kept[1:])
    assert total <= convert.WINDOW_CHARS
    assert kept[0] is sized[0]
    assert kept[1]["role"] == "user"


@pytest.mark.parametrize("budget", [1, 500, 2_000, 7_777, 25_000])
def test_window_never_splits_call_return_pairs(
    monkeypatch: pytest.MonkeyPatch, budget: int
) -> None:
    monkeypatch.setattr(convert, "WINDOW_CHARS", budget)
    messages = [*conversation(30, size=300), user("end", "go")]
    kept = window(messages)
    assert kept[0] is messages[0] and kept[-1] is messages[-1]
    assert all(m in messages for m in kept)
    assert_paired(to_model_messages(kept))


def test_window_of_nothing() -> None:
    assert window([]) == []
    replies = [assistant("a", STEP, text("hi"))]
    assert window(replies) == replies


# --- the turn -------------------------------------------------------------------------------------


def test_turn_input_splits_off_the_latest_user_request() -> None:
    messages = [
        user("u1", "Find jobs"),
        assistant("a1", STEP, text("Found 3.")),
        user("u2", "More?"),
    ]
    turn = turn_input(messages)
    assert turn.user_prompt == "More?"
    assert shape(turn.message_history) == [
        ("request", [("user", "Find jobs")]),
        ("response", [("text", "Found 3.")]),
    ]
    assert turn.as_kwargs() == {"user_prompt": "More?", "message_history": turn.message_history}
    two_parts = {"id": "u", "role": "user", "parts": [text("a"), text("b")]}
    assert turn_input([two_parts]).user_prompt == "a\nb"
    assert turn_input([assistant("a", STEP, text("x"))]).user_prompt is None
    assert turn_input([]) == TurnInput(None, [])


def _recording_agent(seen: list[list[ModelMessage]]) -> Agent[None, str]:
    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        seen.append(list(messages))
        yield "ok"

    agent = Agent(FunctionModel(stream_function=stream))

    @agent.tool_plain
    async def start_search_run(prompt: str) -> dict[str, Any]:
        """Start a search."""
        return {"ok": True}

    return agent


async def _run(agent: Agent[None, str], **kwargs: Any) -> None:
    async with agent.run_stream_events(**kwargs) as events:
        async for _ in events:
            pass


HISTORY = [
    user("u1", "Find me a backend role"),
    assistant(
        "a1",
        STEP,
        text("Starting a search."),
        tool("start_search_run", "c1", "input-available", input={"prompt": "backend"}),
    ),  # stopped mid-tool
    user("n1", "Your search finished.", {"kind": "run_update", "runId": "r", "eventId": 1}),
    assistant(
        "a2",
        STEP,
        text("It found 3 roles."),
        tool("retired", "c2", "output-error", input={}, errorText="Gone."),
    ),
    user("u2", "Show me the best one"),
]


async def test_pydantic_ai_accepts_the_converted_history() -> None:
    """§15.4: `run_stream_events(user_prompt=..., message_history=...)` takes the converter's
    history: interrupted calls, merged notices, retired tools and all."""
    seen: list[list[ModelMessage]] = []
    turn = turn_input(HISTORY)
    await _run(_recording_agent(seen), **turn.as_kwargs())
    [received] = seen
    assert_paired(received)
    assert shape(received) == [
        ("request", [("user", "Find me a backend role")]),
        (
            "response",
            [
                ("text", "Starting a search."),
                ("call", "start_search_run", "c1", {"prompt": "backend"}),
            ],
        ),
        (
            "request",
            [("return", "start_search_run", "c1", INTERRUPTED), ("user", "Your search finished.")],
        ),
        ("response", [("text", "It found 3 roles."), ("call", "retired", "c2", {})]),
        (
            "request",
            [("return", "retired", "c2", "Error: Gone."), ("user", "Show me the best one")],
        ),
    ]


async def test_pydantic_ai_accepts_the_full_history_without_a_prompt() -> None:
    """The §15.4 fallback: the whole history ending in a request, and `user_prompt=None`."""
    seen: list[list[ModelMessage]] = []
    await _run(_recording_agent(seen), message_history=to_model_messages(HISTORY))
    [received] = seen
    assert_paired(received)
    assert shape(received)[-1] == (
        "request",
        [("return", "retired", "c2", "Error: Gone."), ("user", "Show me the best one")],
    )
