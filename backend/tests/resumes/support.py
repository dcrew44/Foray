"""Shared helpers for the resume tests: golden and fixture paths, and readers."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, UserContent
from pydantic_ai.models.function import AgentInfo, FunctionModel

from apps.resumes.schema import Resume

TESTS = Path(__file__).resolve().parent.parent
GOLDENS = TESTS / "goldens"
FIXTURES = TESTS / "fixtures"


def golden(path: str) -> Any:
    return json.loads((GOLDENS / path).read_text())


def golden_text(path: str) -> str:
    return (GOLDENS / path).read_text()


def fixture_bytes(path: str) -> bytes:
    return (FIXTURES / path).read_bytes()


def resume(data: dict[str, Any]) -> Resume:
    """A resume from stored (camelCase) data, as `ResumeSchema.parse` built it."""
    return Resume.model_validate(data)


type ModelFunction = Callable[[list[ModelMessage], AgentInfo], ModelResponse | None]


def structured_with(function: ModelFunction) -> Callable[..., Any]:
    """A stand-in for `apps.llm.structured.structured` that runs a real pydantic-ai agent over a
    `FunctionModel`, without the registry, ledger or budget (step 3B's), so this lane's prompts,
    output types and fakes meet pydantic-ai's own output-tool machinery."""

    def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        response = function(messages, info)
        assert response is not None, "the fake declined this request"
        return response

    async def structured(
        purpose: str,
        output: type[Any],
        *,
        instructions: str,
        prompt: str | Sequence[UserContent],
        timeout: float | None = None,  # noqa: ASYNC109 (the signature of the real one)
    ) -> Any:
        agent = Agent(FunctionModel(answer), output_type=output, instructions=instructions)
        return (await agent.run(prompt)).output

    return structured
