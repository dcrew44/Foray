"""Canned vendor responses behind a mock httpx2 transport (`research/llm-library.md` §3, §4).

The real pydantic-ai model classes talk to `Vendor.client()`, which records every request body
and answers with the vendor's own JSON (or SSE, where the SDK streams). Nothing leaves the
process. The usage numbers are the research's table: input 1000 with 600 cached (Anthropic: 100
uncached + 500 cache-write + 400 cache-read), output 300 of which 200 are reasoning.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx2
from pydantic import BaseModel, Field

from apps.llm import registry


class Answer(BaseModel):
    city: str = Field(description="The city named in the question.")
    confident: bool


ARGS = {"city": "San Francisco", "confident": True}
OUTPUT_TOOL = "final_result"


def openai_responses(args: dict[str, Any] = ARGS) -> dict[str, Any]:
    return {
        "id": "resp_1",
        "object": "response",
        "created_at": 1_700_000_000,
        "model": "gpt-5.6-luna",
        "status": "completed",
        "output": [
            {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": OUTPUT_TOOL,
                "arguments": json.dumps(args),
                "status": "completed",
            }
        ],
        "usage": {
            "input_tokens": 1000,
            "input_tokens_details": {"cached_tokens": 600},
            "output_tokens": 300,
            "output_tokens_details": {"reasoning_tokens": 200},
            "total_tokens": 1300,
        },
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
    }


def chat_completion(model: str, args: dict[str, Any] = ARGS) -> dict[str, Any]:
    """DeepSeek and compat (OpenAI Chat Completions)."""
    return {
        "id": "chatcmpl_1",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": OUTPUT_TOOL, "arguments": json.dumps(args)},
                        }
                    ],
                },
            }
        ],
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 300,
            "total_tokens": 1300,
            "prompt_cache_hit_tokens": 600,
            "prompt_cache_miss_tokens": 400,
            "prompt_tokens_details": {"cached_tokens": 600},
            "completion_tokens_details": {"reasoning_tokens": 200},
        },
    }


def anthropic_events(model: str, args: dict[str, Any] = ARGS) -> list[dict[str, Any]]:
    """Anthropic streams internally even for non-streaming calls (max_tokens is the model's
    maximum), so its answer is SSE."""
    return [
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 100,
                    "cache_creation_input_tokens": 500,
                    "cache_read_input_tokens": 400,
                    "output_tokens": 1,
                },
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "tool_use",
                "id": "toolu_1",
                "name": OUTPUT_TOOL,
                "input": {},
            },
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": json.dumps(args)},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use", "stop_sequence": None},
            "usage": {"output_tokens": 300, "output_tokens_details": {"thinking_tokens": 200}},
        },
        {"type": "message_stop"},
    ]


def anthropic_message(model: str, args: dict[str, Any] = ARGS) -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "tool_use", "id": "toolu_1", "name": OUTPUT_TOOL, "input": args}],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 100,
            "cache_creation_input_tokens": 500,
            "cache_read_input_tokens": 400,
            "output_tokens": 300,
            "output_tokens_details": {"thinking_tokens": 200},
        },
    }


def gemini(model: str, args: dict[str, Any] = ARGS) -> dict[str, Any]:
    return {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [{"functionCall": {"name": OUTPUT_TOOL, "args": args}}],
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 1000,
            "cachedContentTokenCount": 600,
            "candidatesTokenCount": 100,
            "thoughtsTokenCount": 200,
            "totalTokenCount": 1300,
        },
        "modelVersion": model,
        "responseId": "r1",
    }


def sse(events: list[dict[str, Any]]) -> str:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)


@dataclass
class Vendor:
    """A mock vendor endpoint for one ref. `bodies` holds every request body it received."""

    ref: str
    bodies: list[dict[str, Any]] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    status: int = 200
    error_body: dict[str, Any] | None = None

    @property
    def provider(self) -> str:
        return self.ref.split(":", 1)[0]

    @property
    def model_name(self) -> str:
        return self.ref.split(":", 1)[1]

    def answer(self, body: dict[str, Any]) -> httpx2.Response:
        if self.status != 200:
            return httpx2.Response(
                self.status, json=self.error_body or {"error": {"message": "boom"}}
            )
        match self.provider:
            case "openai":
                return httpx2.Response(200, json=openai_responses())
            case "deepseek" | "compat":
                return httpx2.Response(200, json=chat_completion(self.model_name))
            case "anthropic":
                if body.get("stream"):
                    return httpx2.Response(
                        200,
                        text=sse(anthropic_events(self.model_name)),
                        headers={"content-type": "text/event-stream"},
                    )
                return httpx2.Response(200, json=anthropic_message(self.model_name))
            case "google":
                return httpx2.Response(200, json=gemini(self.model_name))
        raise AssertionError(f"no canned answer for {self.ref}")

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content or b"{}")
        self.bodies.append(body)
        self.urls.append(str(request.url))
        return self.answer(body)

    def client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.MockTransport(self.handler))

    def base_model(self) -> Any:
        return registry.build_base_model(self.ref, http_client=self.client())

    @property
    def body(self) -> dict[str, Any]:
        assert self.bodies, f"{self.ref} received no request"
        return self.bodies[-1]


def patch_base_models(monkeypatch: Any, vendors: dict[str, Vendor]) -> Callable[[str], Any]:
    """Make `registry.base_model(ref)` build the real model class over each vendor's mock."""

    def base_model(ref: str) -> Any:
        return vendors[ref].base_model()

    monkeypatch.setattr(registry, "base_model", base_model)
    monkeypatch.setattr(registry, "_MODELS", {})
    return base_model
