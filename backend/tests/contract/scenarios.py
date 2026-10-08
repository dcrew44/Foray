"""Streams the contract check validates (design §12.4, step 3E).

- `canned()`: hand-built streams through `UIMessageStream`: text, a streamed tool call, a `null`
  output, an error, an abort, `: ping` comments, metadata, data parts, input errors, an abort
  in the middle of streamed tool input, reasoning.
- `agent_scenarios()`: a `FunctionModel` agent run through the real `ForayVercelStream`, teed
  into a `UIMessageAccumulator` exactly as the chat view does it.

Each gives the body as sent (`sse`), the message the server would save (`message`) and the
error text the client should see (`error_text`, `None` when there is none).
"""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic_ai import Agent, UsageLimits
from pydantic_ai.messages import ModelMessage, ModelRequest, RetryPromptPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaThinkingPart, DeltaToolCall, FunctionModel

from apps.chat.uistream import (
    DONE,
    GENERIC_ERROR,
    PING,
    STEP_LIMIT_ERROR,
    Chunk,
    ForayVercelStream,
    UIMessageAccumulator,
    UIMessageStream,
    frame,
)
from apps.core.errors import UserFacingError

TEXT = 'Here is the answer: Zoë’s café ✓ "quoted"\n</script> & done.'
JOBS = {
    "count": 1,
    "jobs": [
        {
            "matchId": "m-1",
            "title": "Backend Engineer",
            "company": "Café Numérique",
            "salary": None,
            "score": 0.85,
        }
    ],
}
BUDGET_TEXT = "This month's model budget of $90 is spent. Raise LLM_MONTHLY_BUDGET_USD to continue."
USER_FACING_TEXT = "That company could not be found."


@dataclass
class Played:
    name: str
    sse: str
    message: dict[str, Any]
    chunks: list[Chunk] = field(default_factory=list[Chunk])
    error_text: str | None = None


# --- hand-built streams -------------------------------------------------------------------------


def _played(name: str, s: UIMessageStream, error_text: str | None = None) -> Played:
    return Played(name, s.sse(), s.message, list(s.chunks), error_text)


def _begin(message_id: str, metadata: Any = None) -> UIMessageStream:
    s = UIMessageStream(message_id)
    s.start(metadata)
    s.start_step()
    return s


def _next_step(s: UIMessageStream) -> None:
    s.finish_step()
    s.start_step()


def _end(s: UIMessageStream, reason: str = "stop", metadata: Any = None) -> None:
    s.finish_step()
    s.finish(reason, metadata)


# Tool input cut off mid-stream: the client shows `parsePartialJson` of what arrived.
PARTIAL_INPUTS = [
    ("call_1", "list_jobs", '{"query":"rust eng'),
    ("call_2", "get_job", '{"id":"greenhouse:acme:1","x":'),
    ("call_3", "tailor_resume", '{"a":[1,2,{"b":tr'),
    ("call_4", "move_job_stage", '{"n":-'),
    ("call_5", "update_profile_preferences", '{"s":"caf\\u00e'),
    ("call_6", "list_companies", '{"limit":12.5e'),
    ("call_7", "get_run_status", "nul"),
    ("call_8", "list_pending_approvals", '{"k":[{"a":1},'),
    ("call_9", "evaluate_job", '{"esc":"a\\'),
    ("call_10", "get_job", '{"a":NaN}'),
    ("call_11", "get_job", '{"__proto__":{"x":1}}'),
    ("call_12", "get_job", ' { "a" : [ 1 , 2 '),
    ("call_13", "get_job", "[1,2,"),
    ("call_14", "get_job", '{"a":"b\\"c'),
    ("call_15", "get_job", '{"a":{"b":{"c":nu'),
    ("call_16", "get_job", '"just a str'),
    ("call_17", "get_job", "12"),
    ("call_18", "get_job", "-"),
    ("call_19", "get_job", '{"a":fals'),
    ("call_20", "get_job", '{"a":true,"b":'),
    ("call_21", "get_job", '{"a":[{"b":1e5},"x\\u12'),
    ("call_22", "get_job", '{"a":tx'),
]


def canned() -> list[Played]:
    played: list[Played] = []

    s = _begin("asst_text")
    s.text(TEXT)
    _end(s)
    played.append(_played("text", s))

    s = _begin("asst_tool")
    s.text("Let me look.")
    s.tool_input_start("call_1", "list_jobs")
    s.tool_input_delta("call_1", '{"limit":')
    s.tool_input_delta("call_1", "2}")
    s.tool_input_available("call_1", "list_jobs", {"limit": 2})
    s.tool_output_available("call_1", JOBS)
    _next_step(s)
    s.text("One match.")
    _end(s)
    played.append(_played("tool", s))

    s = _begin("asst_null_output")
    s.tool_call("call_1", "get_job", {"id": "x"}, None)
    _next_step(s)
    s.text("It returned nothing.")
    _end(s)
    played.append(_played("null_output", s))

    s = _begin("asst_error")
    s.text_start("t1")
    s.text_delta("t1", "Partial ")
    s.close_with_error(GENERIC_ERROR)
    played.append(_played("error", s, GENERIC_ERROR))

    s = _begin("asst_abort")
    s.text_start("t1")
    s.text_delta("t1", "Stopped mid")
    s.abort("The agent run was cancelled.")
    played.append(_played("abort", s))

    s = _begin("asst_pings")
    s.text("Slow, but here.")
    _end(s)
    pinged = PING + "".join(frame(c) + PING for c in s.chunks) + DONE
    played.append(Played("pings", pinged, s.message, list(s.chunks)))

    s = _begin("asst_metadata", {"kind": "follow_up", "runId": "run-1", "eventId": 7})
    s.text("Your search finished.")
    s.metadata({"extra": {"a": 1}})
    _end(s, "stop", {"extra": {"b": 2}})
    played.append(_played("metadata", s))

    s = _begin("asst_data")
    s.data("progress", {"n": 1}, id="p1")
    s.data("progress", {"n": 2}, id="p1")  # replaces p1 in place
    s.data("note", {"seen": True}, transient=True)  # never stored
    s.data("empty", None)
    s.text("Done.")
    _end(s)
    played.append(_played("data", s))

    s = _begin("asst_input_errors")
    s.tool_input_start("call_1", "list_jobs")
    s.tool_input_delta("call_1", '{"limit":"two"}')
    s.tool_input_error("call_1", "list_jobs", {"limit": "two"}, "Bad input.")
    s.tool_input_error("call_2", "no_such_tool", {}, "Unknown tool.", dynamic=True)
    s.tool_input_available("call_3", "get_job", {"id": "raise"})
    s.tool_output_error("call_3", "That step failed on our side.")
    _next_step(s)
    s.text("Never mind.")
    _end(s)
    played.append(_played("input_errors", s))

    s = _begin("asst_abort_mid_input")
    for call_id, name, partial in PARTIAL_INPUTS:
        s.tool_input_start(call_id, name)
        s.tool_input_delta(call_id, partial)
    s.abort()
    played.append(_played("abort_mid_input", s))

    s = _begin("asst_reasoning")
    s.reasoning("Thinking...")
    s.text("Done thinking.")
    _end(s)
    played.append(_played("reasoning", s))

    return played


# --- FunctionModel agent through ForayVercelStream ----------------------------------------------

type Step = Sequence[str | Mapping[int, DeltaToolCall | DeltaThinkingPart] | BaseException]


def _answered(messages: list[ModelMessage]) -> bool:
    last = messages[-1]
    return isinstance(last, ModelRequest) and any(
        isinstance(p, ToolReturnPart | RetryPromptPart) for p in last.parts
    )


def scripted_agent(first: Step, then: Step | None = None) -> Agent[None, str]:
    """An agent whose model streams `first`, then `then` once a tool result is back."""

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[Any]:
        step = then if then is not None and _answered(messages) else first
        for item in step:
            if isinstance(item, BaseException):
                raise item
            yield item

    agent = Agent(FunctionModel(stream_function=stream))

    @agent.tool_plain
    async def get_job(id: str) -> dict[str, Any] | None:
        """One job."""
        if id == "none":
            return None
        return {"ok": True, "jobId": id, "title": "Backend Engineer", "salary": None, "pay": 1.5}

    @agent.tool_plain
    async def list_jobs(limit: int) -> dict[str, Any]:
        """Some jobs."""
        return JOBS

    return agent


def budget_exceeded(message: str = BUDGET_TEXT) -> Exception:
    """`llm.budget.BudgetExceeded` once step 3B has landed; a stand-in with the same
    `user_message` attribute (§8.6) until then."""
    try:
        cls = importlib.import_module("apps.llm.budget").BudgetExceeded
    except ImportError, AttributeError:
        return _StandInBudgetExceeded(message)
    return cls(user_message=message)  # §8.6; a TypeError here means the real class changed


class _StandInBudgetExceeded(Exception):
    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


async def play(
    name: str,
    agent: Agent[None, str],
    prompt: str = "hi",
    *,
    usage_limits: UsageLimits | None = None,
    ping_every: int = 0,
    error_text: str | None = None,
) -> Played:
    """One turn, teed and framed the way the chat view does it."""
    message_id = f"asst_{name}"
    acc = UIMessageAccumulator(message_id)
    frames: list[str] = []
    chunks: list[Chunk] = []
    async with agent.run_stream_events(prompt, usage_limits=usage_limits) as events:
        async for chunk in ForayVercelStream(message_id=message_id).chunks(events):
            acc.apply(chunk)
            chunks.append(chunk)
            frames.append(frame(chunk))
            if ping_every and len(chunks) % ping_every == 0:
                frames.append(PING)
    frames.append(DONE)
    return Played(name, "".join(frames), acc.message, chunks, error_text)


def _call(name: str, args: str, call_id: str = "call_1") -> dict[int, DeltaToolCall]:
    return {0: DeltaToolCall(name=name, json_args=args, tool_call_id=call_id)}


AGENT_SCENARIOS: dict[str, Callable[[], Awaitable[Played]]] = {
    "agent_text": lambda: play("agent_text", scripted_agent(["Here is ", TEXT])),
    "agent_tool_output": lambda: play(
        "agent_tool_output",
        scripted_agent([_call("get_job", '{"id":"greenhouse:acme:1"}')], ["Found it."]),
    ),
    "agent_tool_none": lambda: play(
        "agent_tool_none", scripted_agent([_call("get_job", '{"id":"none"}')], ["Nothing."])
    ),
    "agent_two_tools": lambda: play(
        "agent_two_tools",
        scripted_agent(
            [
                "Checking both.",
                _call("get_job", '{"id":"a"}'),
                {
                    1: DeltaToolCall(
                        name="list_jobs", json_args='{"limit":1}', tool_call_id="call_2"
                    )
                },
            ],
            ["Both are in."],
        ),
    ),
    "agent_invalid_input": lambda: play(
        "agent_invalid_input",
        scripted_agent([_call("list_jobs", '{"limit":"two"}')], ["Retried."]),
    ),
    "agent_unknown_tool": lambda: play(
        "agent_unknown_tool", scripted_agent([_call("no_such_tool", "{}")], ["Never mind."])
    ),
    "agent_reasoning": lambda: play(
        "agent_reasoning",
        scripted_agent(
            [{5: DeltaThinkingPart(content="Thinking...", signature="sig-1")}, "Done thinking."]
        ),
    ),
    "agent_pings": lambda: play(
        "agent_pings", scripted_agent(["Slow ", "but here."]), ping_every=1
    ),
    "agent_step_limit": lambda: play(
        "agent_step_limit",
        scripted_agent([_call("list_jobs", '{"limit":1}')], [_call("list_jobs", '{"limit":1}')]),
        usage_limits=UsageLimits(request_limit=2),
        error_text=STEP_LIMIT_ERROR,
    ),
    "agent_budget": lambda: play(
        "agent_budget", scripted_agent([budget_exceeded()]), error_text=BUDGET_TEXT
    ),
    "agent_user_facing": lambda: play(
        "agent_user_facing",
        scripted_agent(["Looking ", UserFacingError(USER_FACING_TEXT)]),
        error_text=USER_FACING_TEXT,
    ),
    "agent_generic_error": lambda: play(
        "agent_generic_error",
        scripted_agent(["Partial ", RuntimeError("password=hunter2 at db-internal:5432")]),
        error_text=GENERIC_ERROR,
    ),
    "agent_error_after_tool": lambda: play(
        "agent_error_after_tool",
        scripted_agent([_call("get_job", '{"id":"a"}')], [RuntimeError("provider overloaded")]),
        error_text=GENERIC_ERROR,
    ),
}


async def agent_scenarios() -> list[Played]:
    return [await run() for run in AGENT_SCENARIOS.values()]
