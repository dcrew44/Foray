"""pydantic-ai's VercelAIEventStream as a standalone event->chunk mapper, with the wire owned by us."""
from __future__ import annotations
import asyncio, json, logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

from pydantic_ai import Agent, DeferredToolRequests
from pydantic_ai.messages import ModelRequest, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.ui.vercel_ai import VercelAIEventStream
from pydantic_ai.ui.vercel_ai.response_types import (
    BaseChunk, DoneChunk, ErrorChunk, MessageMetadataChunk, StartChunk, ToolOutputAvailableChunk)

from uistream import UIMessageAccumulator, frame, DONE

log = logging.getLogger("chat")


@dataclass
class ForayVercelStream(VercelAIEventStream):
    """Fixes found in pydantic-ai-slim 2.54.0 against ai 7.0.106."""

    def encode_event(self, event: BaseChunk) -> str:
        if isinstance(event, DoneChunk):
            return DONE
        d = event.model_dump(mode="json", by_alias=True, exclude_none=True)
        if isinstance(event, ToolOutputAvailableChunk):
            d.setdefault("output", None)  # client requires the key; TS SDK sends null
        return frame(d)

    async def before_stream(self) -> AsyncIterator[BaseChunk]:
        yield StartChunk(message_id=self.server_message_id)  # always pinned by caller

    async def handle_run_result(self, event):
        async for c in super().handle_run_result(event):
            if not isinstance(c, MessageMetadataChunk):  # drop pydantic_ai timestamp metadata
                yield c

    async def on_error(self, error: Exception) -> AsyncIterator[BaseChunk]:
        log.exception("chat turn failed", exc_info=error)
        async for c in super().on_error(error):
            yield ErrorChunk(error_text="The model or the network failed mid-answer.") if isinstance(c, ErrorChunk) else c


async def stream_fn(messages, info: AgentInfo):
    if any(isinstance(p, ToolReturnPart) for m in messages for p in getattr(m, "parts", [])):
        yield "Moved it."
        return
    yield {0: DeltaToolCall(name="move_job_stage", json_args='{"stage":"applied"}', tool_call_id="call_1")}

agent = Agent(FunctionModel(stream_function=stream_fn), output_type=[str, DeferredToolRequests])

@agent.tool_plain
def move_job_stage(stage: str) -> None:
    return None


async def main():
    history = [ModelRequest(parts=[UserPromptPart(content="move it")])]  # from OUR store, not the client
    es = ForayVercelStream(sdk_version=7, server_message_id="asst_srv_9")  # run_input=None: standalone
    acc = UIMessageAccumulator("asst_srv_9")
    frames = []
    async with agent.run_stream_events(message_history=history) as events:
        async for chunk in es.transform_stream(events):
            s = es.encode_event(chunk)
            frames.append(s)
            if s != DONE:
                acc.apply(json.loads(s[len("data: "):]))
    sse = "".join(frames)
    open("../out/patched-stream.sse", "w").write(sse)
    print(sse)
    print("accumulated:", json.dumps(acc.message))

asyncio.run(main())
